"""마커 Sim(3) 계산과 Blender UI 사이의 비파괴 미리보기 수명주기를 관리합니다."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from time import perf_counter
from typing import TYPE_CHECKING, Any

import numpy as np

from ..constants import (
    MARKER_ALIGNMENT_PHASE_ICP_RUNNING,
    MARKER_ALIGNMENT_PHASE_IDLE,
    MARKER_ALIGNMENT_PHASE_PREVIEW,
    MARKER_ALIGNMENT_POSITION_TOLERANCE,
    MARKER_POINT_KIND_COLMAP,
    MARKER_GEOMETRY_SOURCE_MAIN,
    MARKER_GEOMETRY_SOURCE_COMPANION,
    ROLE_REFERENCE,
    ROLE_MOVING,
    MARKER_POINT_KIND_MESH_SURFACE,
    MARKER_POINT_KIND_MESH_VERTEX,
    MODEL_STATUS_REGISTERED,
)
from ..logging_utils import get_logger
from ..marker_geometry import normalize_marker_geometry_source, resolve_marker_geometry
from ..geometry_access import (
    geometry_kind_for_model,
    source_position_for_registration_id,
    validated_mesh_surface_position,
)
from .marker_sim3 import build_marker_alignment_result
from .types import (
    IcpAlignmentSeed,
    MarkerAlignmentError,
    MarkerAlignmentInput,
    MarkerAlignmentResult,
    MarkerInputError,
    MarkerPreviewStaleError,
    Sim3Transform,
)


_LOGGER = get_logger("alignment.runtime")

if TYPE_CHECKING:
    from ..display_axes import ModelDisplayTransform


@dataclass
class MarkerAlignmentPreviewState:
    """확정 Repository와 분리된 현재 P 모델의 marker preview 수명주기를 보관합니다."""

    phase: str = MARKER_ALIGNMENT_PHASE_IDLE
    result: MarkerAlignmentResult | None = None
    last_error: str = ""
    started_at: float | None = None
    p_view_snapshot: Any = None

    @property
    def is_active(self) -> bool:
        """P에 적용할 marker preview 결과가 현재 유효하게 존재하는지 반환합니다."""

        return self.phase in {
            MARKER_ALIGNMENT_PHASE_PREVIEW,
            MARKER_ALIGNMENT_PHASE_ICP_RUNNING,
        } and self.result is not None

    def clear(self, *, error_message: str = "") -> Any:
        """preview 결과만 원자적으로 비우고 UI가 복원할 P 시점 snapshot을 반환합니다."""

        view_snapshot = self.p_view_snapshot
        self.phase = MARKER_ALIGNMENT_PHASE_IDLE
        self.result = None
        self.started_at = None
        self.p_view_snapshot = None
        self.last_error = str(error_message).strip()
        return view_snapshot


def _repository_for_runtime(runtime: Any) -> Any:
    """Runtime에서 실제 모델 저장소를 찾아 누락을 입력 오류로 변환합니다."""

    repository = getattr(runtime, "model_repository", None)
    if repository is None:
        raise MarkerInputError("마커 정렬에 사용할 COLMAP 모델 저장소가 없습니다.")
    if not isinstance(getattr(repository, "models", None), dict):
        raise MarkerInputError("COLMAP 모델 저장소 형식이 올바르지 않습니다.")
    return repository


def _preview_state_for_runtime(runtime: Any) -> MarkerAlignmentPreviewState:
    """Runtime에 소유된 marker preview 상태를 검증해 반환합니다."""

    preview_state = getattr(runtime, "marker_alignment", None)
    if not isinstance(preview_state, MarkerAlignmentPreviewState):
        raise MarkerInputError("마커 정렬 미리보기 런타임 상태가 없습니다.")
    return preview_state


def _alignment_for_model(repository: Any, model_id: str) -> Sim3Transform:
    """등록 모델의 확정 Sim(3)을 identity 추정 없이 명시적으로 조회합니다."""

    resolver = getattr(repository, "alignment_transform_for_model_id", None)
    if callable(resolver):
        transform = resolver(model_id)
    else:
        transforms = getattr(repository, "alignment_transforms", None)
        if not isinstance(transforms, dict):
            raise MarkerInputError("COLMAP 모델의 확정 alignment 저장소가 없습니다.")
        try:
            transform = transforms[model_id]
        except KeyError as error:
            raise MarkerInputError(
                f"COLMAP 모델의 확정 alignment가 없습니다: {model_id}"
            ) from error
    if not isinstance(transform, Sim3Transform):
        raise MarkerInputError(f"COLMAP 모델 확정 alignment 형식이 올바르지 않습니다: {model_id}")
    return transform


def _display_transform_for_model(repository: Any, model_id: str) -> "ModelDisplayTransform":
    """등록 모델의 불변 표시 축 변환을 누락 없이 조회합니다."""

    from ..display_axes import ModelDisplayTransform

    resolver = getattr(repository, "display_transform_for_model_id", None)
    if callable(resolver):
        transform = resolver(model_id)
    else:
        transforms = getattr(repository, "display_transforms", None)
        transform = transforms.get(model_id) if isinstance(transforms, dict) else None
    if not isinstance(transform, ModelDisplayTransform):
        raise MarkerInputError(f"COLMAP 모델 표시 축 변환이 없습니다: {model_id}")
    return transform


def _source_position_for_point(
    model: Any,
    point3d_id: int,
    point_kind: str = MARKER_POINT_KIND_COLMAP,
) -> np.ndarray:
    """COLMAP POINT3D_ID 또는 메시 원본 정점 ID를 Source Local 좌표로 조회합니다."""

    if int(point3d_id) < 0:
        raise MarkerInputError("마커 원본 점 ID는 음수일 수 없습니다.")
    try:
        return source_position_for_registration_id(model, point_kind, int(point3d_id))
    except (KeyError, TypeError, ValueError) as error:
        label = "OBJ 원본 정점 ID" if str(point_kind).strip().upper() == MARKER_POINT_KIND_MESH_VERTEX else "COLMAP POINT3D_ID"
        raise MarkerInputError(f"마커 {label}를 찾을 수 없습니다: {point3d_id}") from error


def _validate_saved_source_position(selected_point: Any, model: Any) -> np.ndarray:
    """선택한 형상 슬롯의 마커 종류와 실제 원본 점·정점·삼각면 좌표를 검증합니다."""

    point_kind = str(getattr(selected_point, "point_kind", MARKER_POINT_KIND_COLMAP)).strip().upper()
    try:
        geometry_source = normalize_marker_geometry_source(
            getattr(selected_point, "geometry_source", MARKER_GEOMETRY_SOURCE_MAIN),
        )
    except ValueError as error:
        raise MarkerInputError(str(error)) from error
    if geometry_source == MARKER_GEOMETRY_SOURCE_COMPANION and point_kind != MARKER_POINT_KIND_MESH_SURFACE:
        raise MarkerInputError("연결 메시 마커는 원본 삼각면 위치만 사용할 수 있습니다.")
    point3d_id = getattr(selected_point, "point3d_id", None)
    source_point_id = getattr(selected_point, "source_point_id", None)
    if point_kind in {MARKER_POINT_KIND_MESH_VERTEX, MARKER_POINT_KIND_MESH_SURFACE}:
        point_id = source_point_id if source_point_id is not None else point3d_id
    else:
        point_id = point3d_id
    if point_id is None:
        raise MarkerInputError("마커 원본 점 ID가 올바르지 않습니다.")
    try:
        normalized_point_id = int(point_id)
    except (TypeError, ValueError) as error:
        raise MarkerInputError("마커 POINT3D_ID가 올바르지 않습니다.") from error
    saved_position = np.asarray(getattr(selected_point, "local_position", ()), dtype=np.float64)
    if saved_position.shape != (3,) or not np.isfinite(saved_position).all():
        raise MarkerInputError("마커의 저장 Source Local 좌표가 올바르지 않습니다.")
    if point_kind == MARKER_POINT_KIND_MESH_SURFACE:
        try:
            return validated_mesh_surface_position(
                model,
                normalized_point_id,
                saved_position,
            )
        except (KeyError, TypeError, ValueError) as error:
            raise MarkerInputError(
                f"마커 OBJ 삼각면 위치를 확인할 수 없습니다: {normalized_point_id}"
            ) from error
    source_position = _source_position_for_point(model, normalized_point_id, point_kind)
    if not np.allclose(
        saved_position,
        source_position,
        rtol=0.0,
        atol=MARKER_ALIGNMENT_POSITION_TOLERANCE,
    ):
        raise MarkerInputError(
            "마커의 저장 좌표가 현재 원본 등록점 좌표와 다릅니다. "
            "마커를 다시 지정하십시오."
        )
    return source_position



def _geometry_for_selected_point(repository: Any, selected_point: Any):
    """선택한 마커의 실제 원본과 축을 조회하며 누락을 정렬 입력 오류로 변환합니다."""
    try:
        return resolve_marker_geometry(
            repository,
            str(getattr(selected_point, "model_id", "")),
            getattr(selected_point, "geometry_source", MARKER_GEOMETRY_SOURCE_MAIN),
        )
    except (KeyError, TypeError, ValueError, RuntimeError) as error:
        raise MarkerInputError(str(error)) from error


def _project_source_position(
    alignment: Sim3Transform,
    display_transform: ModelDisplayTransform,
    source_position: np.ndarray,
) -> np.ndarray:
    """`alignment × display_axis × source` 순서로 한 점을 Project World에 보냅니다."""

    source_homogeneous = np.ones(4, dtype=np.float64)
    source_homogeneous[:3] = source_position
    projected = alignment.matrix_float64 @ display_transform.matrix_float64 @ source_homogeneous
    weight = float(projected[3])
    if not np.isfinite(projected).all() or abs(weight) <= np.finfo(np.float64).eps:
        raise MarkerInputError("마커 Project World 좌표를 계산하지 못했습니다.")
    return np.ascontiguousarray(projected[:3] / weight, dtype=np.float64)


def _catalog_selected_moving_model_id(runtime: Any) -> str:
    """현재 Data Layers의 단일 3점 마커 P 모델 ID를 검증해 반환합니다."""

    catalog = getattr(runtime, "model_catalog", None)
    selected_model = getattr(catalog, "selected_inactive_model", None)
    selected_model_id = getattr(catalog, "selected_inactive_model_id", None)
    if selected_model is not None:
        selected_model_id = getattr(selected_model, "model_id", selected_model_id)
        if not bool(getattr(selected_model, "is_visible", False)):
            raise MarkerInputError("정렬할 이동 P 데이터 레이어를 표시하십시오.")
    if not selected_model_id:
        raise MarkerInputError("정렬할 이동 P 데이터 레이어를 선택하십시오.")
    return str(selected_model_id)


def _catalog_selected_reference_model_ids(runtime: Any) -> tuple[str, ...]:
    """직접 Align 시작 의사를 나타내는 선택 A 레이어를 카탈로그 순서로 검증해 반환합니다."""

    catalog = getattr(runtime, "model_catalog", None)
    selected_ids = tuple(
        str(model_id).strip()
        for model_id in getattr(catalog, "selected_reference_model_ids", ())
        if str(model_id).strip()
    )
    if not selected_ids:
        raise MarkerInputError("Align을 시작하려면 A 레이어를 하나 이상 선택하십시오.")
    return selected_ids


def _catalog_alignment_moving_model_id(runtime: Any) -> str:
    """수정 키로 직접 Align에 선택한 P 모델 ID를 검증해 반환합니다."""

    catalog = getattr(runtime, "model_catalog", None)
    selected_model = getattr(catalog, "selected_alignment_moving_model", None)
    selected_model_id = getattr(catalog, "selected_moving_model_id", None)
    if selected_model is not None:
        selected_model_id = getattr(selected_model, "model_id", selected_model_id)
        if not bool(getattr(selected_model, "is_visible", False)):
            raise MarkerInputError("정렬할 이동 P 데이터 레이어를 표시하십시오.")
    if not selected_model_id:
        raise MarkerInputError("Ctrl 또는 Shift로 정렬할 이동 P 데이터 레이어를 선택하십시오.")
    return str(selected_model_id)


def _active_reference_model_ids(runtime: Any) -> tuple[str, ...]:
    """숨김 여부와 무관하게 A 그룹 전체 모델 ID를 카탈로그 순서로 반환합니다."""

    catalog = getattr(runtime, "model_catalog", None)
    active_models = getattr(catalog, "active_models", None)
    if not callable(active_models):
        raise MarkerInputError("A 그룹 모델 카탈로그를 읽을 수 없습니다.")
    model_ids = tuple(
        str(getattr(record, "model_id", ""))
        for record in active_models()
        if getattr(record, "registration_status", "") == MODEL_STATUS_REGISTERED
    )
    if not model_ids:
        raise MarkerInputError("정렬의 기준 A 데이터 레이어가 없습니다.")
    return model_ids


def _marker_signature(
    moving_model_id: str,
    pairs: tuple[Any, ...],
    repository: Any,
    reference_model_ids: tuple[str, ...],
) -> tuple[str, tuple[tuple[str, int], ...]]:
    """마커 슬롯·형상 리비전·원본 지문·선택 축·소유 정렬의 변경을 감지합니다."""

    model_ids = {moving_model_id, *reference_model_ids}
    for pair in pairs:
        model_ids.add(str(getattr(getattr(pair, "reference", None), "model_id", "")))
        model_ids.add(str(getattr(getattr(pair, "moving", None), "model_id", "")))
    if "" in model_ids:
        raise MarkerInputError("마커에 소유 모델 ID가 없는 점이 있습니다.")

    digest = hashlib.blake2b(digest_size=20)
    digest.update(b"marker-alignment-v2\0")
    digest.update(moving_model_id.encode("utf-8"))
    digest.update(b"\0")
    for pair in pairs:
        digest.update(str(int(getattr(pair, "pair_id"))).encode("ascii"))
        digest.update(b"\0")
        for selected_point in (getattr(pair, "reference"), getattr(pair, "moving")):
            model_id = str(getattr(selected_point, "model_id", ""))
            raw_source_id = getattr(selected_point, "source_point_id", None)
            raw_point3d_id = getattr(selected_point, "point3d_id", None)
            point_id = int(raw_source_id if raw_source_id is not None else raw_point3d_id)
            point_kind = str(getattr(selected_point, "point_kind", MARKER_POINT_KIND_COLMAP)).strip().upper()
            local_position = np.ascontiguousarray(
                getattr(selected_point, "local_position", ()),
                dtype=np.dtype("<f8"),
            )
            if local_position.shape != (3,) or not np.isfinite(local_position).all():
                raise MarkerInputError("마커 signature에 사용할 Source Local 좌표가 올바르지 않습니다.")
            geometry = _geometry_for_selected_point(repository, selected_point)
            geometry_revision = int(getattr(geometry.model_data, "geometry_revision", -1))
            if geometry_revision < 0:
                raise MarkerInputError("마커 형상의 리비전이 올바르지 않습니다.")
            digest.update(model_id.encode("utf-8"))
            digest.update(b"\0")
            for value in (
                geometry.geometry_source,
                str(geometry_revision),
                geometry.source_fingerprint,
                geometry.display_transform.fingerprint,
            ):
                digest.update(value.encode("utf-8"))
                digest.update(b"\0")
            digest.update(point_kind.encode("ascii"))
            digest.update(b"\0")
            digest.update(str(point_id).encode("ascii"))
            digest.update(b"\0")
            digest.update(local_position.tobytes())
    revisions: list[tuple[str, int]] = []
    for model_id in sorted(model_ids):
        model = repository.models.get(model_id)
        if model is None:
            raise MarkerInputError(f"마커가 참조하는 COLMAP 모델이 없습니다: {model_id}")
        display_transform = _display_transform_for_model(repository, model_id)
        alignment = _alignment_for_model(repository, model_id)
        revision = int(getattr(model, "geometry_revision", -1))
        if revision < 0:
            raise MarkerInputError(f"COLMAP 모델 geometry revision이 올바르지 않습니다: {model_id}")
        revisions.append((model_id, revision))
        digest.update(model_id.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(revision).encode("ascii"))
        digest.update(b"\0")
        digest.update(display_transform.fingerprint.encode("ascii"))
        digest.update(b"\0")
        digest.update(alignment.fingerprint.encode("ascii"))
        digest.update(b"\0")
    return digest.hexdigest(), tuple(revisions)


def _current_alignment_signature(
    moving_model_id: str,
    reference_model_ids: tuple[str, ...],
    repository: Any,
) -> tuple[str, tuple[tuple[str, int], ...]]:
    """현재 A/P geometry·표시축·확정 정렬의 직접 ICP signature를 만듭니다."""

    model_ids = tuple(sorted({moving_model_id, *reference_model_ids}))
    if not moving_model_id or not reference_model_ids or any(not model_id for model_id in model_ids):
        raise MarkerInputError("직접 ICP signature의 A/P 모델 ID가 올바르지 않습니다.")
    digest = hashlib.blake2b(digest_size=20)
    digest.update(b"current-alignment-icp-v1\0")
    digest.update(moving_model_id.encode("utf-8"))
    digest.update(b"\0")
    revisions: list[tuple[str, int]] = []
    for model_id in model_ids:
        model = repository.models.get(model_id)
        if model is None:
            raise MarkerInputError(f"직접 ICP가 참조하는 COLMAP 모델이 없습니다: {model_id}")
        revision = int(getattr(model, "geometry_revision", -1))
        if revision < 0:
            raise MarkerInputError(f"COLMAP 모델 geometry revision이 올바르지 않습니다: {model_id}")
        display_transform = _display_transform_for_model(repository, model_id)
        alignment = _alignment_for_model(repository, model_id)
        revisions.append((model_id, revision))
        digest.update(model_id.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(revision).encode("ascii"))
        digest.update(b"\0")
        digest.update(display_transform.fingerprint.encode("ascii"))
        digest.update(b"\0")
        digest.update(alignment.fingerprint.encode("ascii"))
        digest.update(b"\0")
    return digest.hexdigest(), tuple(revisions)


def create_marker_alignment_input(runtime: Any) -> MarkerAlignmentInput:
    """역할별 종류를 검증하고 각 마커의 선택 형상 축·소유 정렬로 계산 입력을 만듭니다."""

    from ..axis_settings import axis_edit_active_block_reason

    axis_reason = axis_edit_active_block_reason(runtime)
    if axis_reason is not None:
        raise MarkerInputError(axis_reason)

    if not bool(getattr(runtime, "session_active", False)):
        raise MarkerInputError("활성 COLMAP Align 세션에서만 마커 정렬을 시작할 수 있습니다.")
    if bool(getattr(runtime, "import_in_progress", False)):
        raise MarkerInputError("COLMAP 가져오기 중에는 마커 정렬을 시작할 수 없습니다.")
    repository = _repository_for_runtime(runtime)
    selection = getattr(runtime, "marker_selection", None)
    if selection is None:
        raise MarkerInputError("마커 선택 상태가 없습니다.")
    if bool(getattr(selection, "has_draft", False)):
        raise MarkerInputError("마커 생성 또는 편집을 확정하거나 취소한 뒤 정렬하십시오.")
    conflict_resolver = getattr(selection, "geometry_conflict_message", None)
    conflict = conflict_resolver() if callable(conflict_resolver) else None
    if conflict:
        raise MarkerInputError(str(conflict))
    raw_pairs = getattr(selection, "completed_pairs", ())
    pairs = tuple(sorted(tuple(raw_pairs), key=lambda pair: int(getattr(pair, "pair_id", 0))))
    if len(pairs) < 3:
        raise MarkerInputError("마커 정렬에는 완성된 A/P 대응쌍이 최소 3개 필요합니다.")
    pair_ids = tuple(int(getattr(pair, "pair_id", 0)) for pair in pairs)
    if any(pair_id <= 0 for pair_id in pair_ids) or len(set(pair_ids)) != len(pair_ids):
        raise MarkerInputError("완성 마커 쌍 ID가 올바르지 않습니다.")

    moving_model_id = _catalog_selected_moving_model_id(runtime)
    if moving_model_id not in repository.models:
        raise MarkerInputError("선택한 P 데이터 레이어가 COLMAP 저장소에 없습니다.")
    reference_model_ids = _active_reference_model_ids(runtime)
    moving_alignment = _alignment_for_model(repository, moving_model_id)
    role_kind_resolver = getattr(selection, "role_geometry_kind", None)
    role_kinds = {
        role: (role_kind_resolver(role) if callable(role_kind_resolver) else None)
        for role in (ROLE_REFERENCE, ROLE_MOVING)
    }

    reference_positions: list[np.ndarray] = []
    moving_positions: list[np.ndarray] = []
    for pair in pairs:
        reference_point = getattr(pair, "reference", None)
        moving_point = getattr(pair, "moving", None)
        reference_model_id = str(getattr(reference_point, "model_id", ""))
        pair_moving_model_id = str(getattr(moving_point, "model_id", ""))
        if reference_model_id not in reference_model_ids:
            raise MarkerInputError("A 마커가 현재 A 그룹 모델을 참조하지 않습니다.")
        if pair_moving_model_id != moving_model_id:
            raise MarkerInputError("모든 P 마커는 현재 선택한 이동 P 모델을 참조해야 합니다.")
        reference_geometry = _geometry_for_selected_point(repository, reference_point)
        moving_geometry = _geometry_for_selected_point(repository, moving_point)
        for role, geometry in ((ROLE_REFERENCE, reference_geometry), (ROLE_MOVING, moving_geometry)):
            kind = geometry_kind_for_model(geometry.model_data)
            if role_kinds[role] is not None and role_kinds[role] != kind:
                raise MarkerInputError("같은 역할의 마커는 점군 또는 메시 한 종류만 사용할 수 있습니다.")
            role_kinds[role] = kind
        reference_source = _validate_saved_source_position(reference_point, reference_geometry.model_data)
        moving_source = _validate_saved_source_position(moving_point, moving_geometry.model_data)
        reference_positions.append(
            _project_source_position(
                _alignment_for_model(repository, reference_model_id),
                reference_geometry.display_transform,
                reference_source,
            )
        )
        moving_positions.append(
            _project_source_position(
                moving_alignment,
                moving_geometry.display_transform,
                moving_source,
            )
        )
    signature, revisions = _marker_signature(
        moving_model_id,
        pairs,
        repository,
        reference_model_ids,
    )
    return MarkerAlignmentInput(
        moving_model_id=moving_model_id,
        pair_ids=pair_ids,
        reference_project=np.asarray(reference_positions, dtype=np.float64),
        moving_project=np.asarray(moving_positions, dtype=np.float64),
        marker_signature=signature,
        source_revision_snapshot=revisions,
        reference_model_ids=reference_model_ids,
    )


def begin_marker_preview(
    runtime: Any,
    *,
    use_uniform_scale: bool = True,
) -> MarkerAlignmentResult:
    """선택한 마커 스케일 모드의 solver 성공 결과만 preview 상태로 등록합니다."""

    preview_state = _preview_state_for_runtime(runtime)
    if preview_state.is_active:
        raise MarkerInputError("이미 마커 정렬 미리보기가 실행 중입니다. 먼저 취소하십시오.")
    try:
        input_data = create_marker_alignment_input(runtime)
        repository = _repository_for_runtime(runtime)
        base_alignment = _alignment_for_model(repository, input_data.moving_model_id)
        _LOGGER.info(
            "event=marker_alignment_preview_started moving_model_id=%s marker_count=%s",
            input_data.moving_model_id,
            len(input_data.pair_ids),
        )
        result = build_marker_alignment_result(
            input_data,
            base_alignment,
            use_uniform_scale=use_uniform_scale,
        )
    except MarkerAlignmentError as error:
        preview_state.last_error = str(error)
        _LOGGER.warning("event=marker_alignment_failed reason=%s", error)
        raise
    except (TypeError, ValueError, np.linalg.LinAlgError) as error:
        preview_state.last_error = str(error)
        _LOGGER.warning("event=marker_alignment_failed reason=%s", error)
        raise MarkerInputError(str(error)) from error

    preview_state.phase = MARKER_ALIGNMENT_PHASE_PREVIEW
    preview_state.result = result
    preview_state.last_error = ""
    preview_state.started_at = perf_counter()
    preview_state.p_view_snapshot = None
    _LOGGER.info(
        "event=marker_alignment_preview_registered moving_model_id=%s marker_count=%s "
        "representative_pair_ids=%s scale=%.12g rms=%.12g maximum=%.12g normalized_rms=%.12g",
        result.moving_model_id,
        result.metrics.count,
        result.representative_triangle.pair_ids,
        result.metrics.uniform_scale,
        result.metrics.rms,
        result.metrics.maximum,
        result.metrics.normalized_rms,
    )
    return result


def set_marker_preview_view_snapshot(runtime: Any, snapshot: Any) -> None:
    """UI가 preview 취소 시 복원할 P RegionView3D snapshot을 runtime에 보관합니다."""

    preview_state = _preview_state_for_runtime(runtime)
    if not preview_state.is_active:
        raise MarkerInputError("마커 미리보기 없이 P 시점 snapshot을 저장할 수 없습니다.")
    preview_state.p_view_snapshot = snapshot


def cancel_marker_preview(runtime: Any) -> Any:
    """확정 transform·마커·Undo를 건드리지 않고 preview와 P 시점 snapshot만 제거합니다."""

    try:
        from .icp_runtime import IcpAlignmentRuntimeState

        icp_state = getattr(runtime, "icp_alignment", None)
        if isinstance(icp_state, IcpAlignmentRuntimeState) and icp_state.is_running:
            raise MarkerInputError("ICP 실행 중에는 마커 미리보기를 취소할 수 없습니다.")
    except ImportError:
        pass
    preview_state = _preview_state_for_runtime(runtime)
    if not preview_state.is_active:
        return None
    result = preview_state.result
    view_snapshot = preview_state.clear()
    _LOGGER.info(
        "event=marker_alignment_preview_cancelled moving_model_id=%s",
        result.moving_model_id if result is not None else "",
    )
    return view_snapshot


def invalidate_marker_preview(runtime: Any, reason: str) -> Any:
    """마커·모델 snapshot 변경으로 stale해진 preview를 제거하고 이유를 보존합니다."""

    preview_state = _preview_state_for_runtime(runtime)
    message = str(reason).strip() or "마커 정렬 입력이 변경되어 미리보기를 취소했습니다."
    if not preview_state.is_active:
        preview_state.last_error = message
        return None
    result = preview_state.result
    view_snapshot = preview_state.clear(error_message=message)
    _LOGGER.info(
        "event=marker_alignment_preview_invalidated moving_model_id=%s reason=%s",
        result.moving_model_id if result is not None else "",
        message,
    )
    return view_snapshot


def preview_is_current(runtime: Any) -> bool:
    """현재 marker preview가 재계산 없이 같은 마커·모델 snapshot을 참조하는지 확인합니다."""

    preview_state = _preview_state_for_runtime(runtime)
    result = preview_state.result
    if not preview_state.is_active or result is None:
        return False
    try:
        current_input = create_marker_alignment_input(runtime)
    except MarkerAlignmentError as error:
        invalidate_marker_preview(runtime, str(error))
        return False
    if (
        current_input.marker_signature != result.marker_signature
        or current_input.source_revision_snapshot != result.source_revision_snapshot
    ):
        invalidate_marker_preview(runtime, "마커 또는 모델 상태가 변경되어 정렬 미리보기를 취소했습니다.")
        return False
    return True


def effective_alignment_for_model(runtime: Any, model_id: str) -> Sim3Transform:
    """ICP·marker·레이어 미리보기 우선순위 뒤 확정 alignment를 반환합니다."""

    normalized_model_id = str(model_id).strip()
    if not normalized_model_id:
        raise MarkerInputError("effective alignment를 조회할 모델 ID가 비어 있습니다.")
    try:
        from .icp_runtime import effective_icp_alignment_for_model

        icp_alignment = effective_icp_alignment_for_model(runtime, normalized_model_id)
        if icp_alignment is not None:
            return icp_alignment
    except ImportError:
        pass
    preview_state = _preview_state_for_runtime(runtime)
    result = preview_state.result
    if preview_state.is_active and result is not None and result.moving_model_id == normalized_model_id:
        return result.preview_alignment
    if getattr(runtime, "layer_transform", None) is not None:
        from .layer_transform import effective_layer_transform_alignment

        layer_alignment = effective_layer_transform_alignment(runtime, normalized_model_id)
        if layer_alignment is not None:
            return layer_alignment
    repository = _repository_for_runtime(runtime)
    return _alignment_for_model(repository, normalized_model_id)


def create_icp_seed(runtime: Any) -> IcpAlignmentSeed:
    """마커 미리보기 또는 현재 P 정렬에서 backend 독립 ICP seed를 만듭니다."""

    preview_state = _preview_state_for_runtime(runtime)
    if preview_state.is_active:
        return _build_icp_seed_from_preview(runtime)
    return _build_icp_seed_from_current_alignment(runtime)


def _build_icp_seed_from_preview(runtime: Any) -> IcpAlignmentSeed:
    """최신 마커 미리보기와 당시 형상 종속성을 후속 계산의 불변 초기값에 보관합니다."""

    preview_state = _preview_state_for_runtime(runtime)
    result = preview_state.result
    if preview_state.phase != MARKER_ALIGNMENT_PHASE_PREVIEW or result is None:
        raise MarkerInputError("ICP 초기값을 만들 유효한 마커 정렬 미리보기가 없습니다.")
    if not preview_is_current(runtime):
        raise MarkerPreviewStaleError("마커 또는 모델 상태가 변경되어 ICP 초기값을 만들 수 없습니다.")
    current_input = create_marker_alignment_input(runtime)
    seed = IcpAlignmentSeed(
        moving_model_id=result.moving_model_id,
        reference_model_ids=current_input.reference_model_ids,
        initial_alignment_float64=result.preview_alignment.matrix_float64,
        base_committed_alignment_float64=result.base_alignment.matrix_float64,
        marker_result=result,
        model_revision_snapshot=result.source_revision_snapshot,
        marker_signature=result.marker_signature,
        initialization_mode="MARKER_PREVIEW",
        marker_geometry_dependencies=tuple(sorted({
            (
                point.model_id,
                normalize_marker_geometry_source(getattr(point, "geometry_source", MARKER_GEOMETRY_SOURCE_MAIN)),
            )
            for pair in runtime.marker_selection.completed_pairs
            for point in (pair.reference, pair.moving)
        })),
    )
    _LOGGER.info(
        "event=marker_alignment_icp_handoff_started moving_model_id=%s marker_count=%s",
        seed.moving_model_id,
        result.metrics.count,
    )
    return seed


def _build_icp_seed_from_current_alignment(runtime: Any) -> IcpAlignmentSeed:
    """선택된 P와 A 그룹 전체를 사용하는 직접 ICP seed를 만듭니다."""

    from ..axis_settings import axis_edit_active_block_reason

    axis_reason = axis_edit_active_block_reason(runtime)
    if axis_reason is not None:
        raise MarkerInputError(axis_reason)

    if not bool(getattr(runtime, "session_active", False)):
        raise MarkerInputError("활성 COLMAP Align 세션에서만 직접 Align을 시작할 수 있습니다.")
    if bool(getattr(runtime, "import_in_progress", False)):
        raise MarkerInputError("COLMAP 가져오기가 끝난 뒤 직접 Align을 시작하십시오.")
    selection = getattr(runtime, "marker_selection", None)
    if selection is None:
        raise MarkerInputError("마커 선택 상태가 없습니다.")
    if bool(getattr(selection, "has_draft", False)):
        raise MarkerInputError("열린 마커 입력을 확정하거나 취소한 뒤 직접 Align을 시작하십시오.")
    repository = _repository_for_runtime(runtime)
    moving_model_id = _catalog_alignment_moving_model_id(runtime)
    if moving_model_id not in repository.models:
        raise MarkerInputError("선택한 P 데이터 레이어가 COLMAP 저장소에 없습니다.")
    _catalog_selected_reference_model_ids(runtime)
    reference_model_ids = _active_reference_model_ids(runtime)
    signature, revisions = _current_alignment_signature(
        moving_model_id,
        reference_model_ids,
        repository,
    )
    alignment = _alignment_for_model(repository, moving_model_id)
    seed = IcpAlignmentSeed(
        moving_model_id=moving_model_id,
        reference_model_ids=reference_model_ids,
        initial_alignment_float64=alignment.matrix_float64,
        base_committed_alignment_float64=alignment.matrix_float64,
        marker_result=None,
        model_revision_snapshot=revisions,
        marker_signature=signature,
        initialization_mode="CURRENT_ALIGNMENT",
    )
    _LOGGER.info(
        "event=current_alignment_icp_handoff_started moving_model_id=%s reference_model_count=%s",
        seed.moving_model_id,
        len(seed.reference_model_ids),
    )
    return seed


def icp_seed_is_current(runtime: Any, seed: IcpAlignmentSeed) -> bool:
    """ICP 시작 시점의 마커 또는 현재 정렬 seed가 아직 같은지 확인합니다."""

    if not isinstance(seed, IcpAlignmentSeed):
        return False
    try:
        if seed.uses_marker_preview:
            if not preview_is_current(runtime):
                return False
            current = _build_icp_seed_from_preview(runtime)
        else:
            if _preview_state_for_runtime(runtime).is_active:
                return False
            current = _build_icp_seed_from_current_alignment(runtime)
    except (MarkerAlignmentError, TypeError, ValueError):
        return False
    return bool(
        current.initialization_mode == seed.initialization_mode
        and current.moving_model_id == seed.moving_model_id
        and current.reference_model_ids == seed.reference_model_ids
        and current.marker_signature == seed.marker_signature
        and current.model_revision_snapshot == seed.model_revision_snapshot
        and np.allclose(
            current.initial_alignment_float64,
            seed.initial_alignment_float64,
            rtol=0.0,
            atol=1.0e-9,
        )
        and np.allclose(
            current.base_committed_alignment_float64,
            seed.base_committed_alignment_float64,
            rtol=0.0,
            atol=1.0e-9,
        )
    )


__all__ = (
    "MarkerAlignmentPreviewState",
    "begin_marker_preview",
    "cancel_marker_preview",
    "create_icp_seed",
    "create_marker_alignment_input",
    "effective_alignment_for_model",
    "icp_seed_is_current",
    "invalidate_marker_preview",
    "preview_is_current",
    "set_marker_preview_view_snapshot",
)
