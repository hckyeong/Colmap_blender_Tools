"""작업 상태 snapshot 저장과 기존 ImportJob 기반 자동 복원을 조정합니다."""

from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
from typing import Any
from uuid import uuid4

import bpy
import numpy as np

from ..alignment.types import Sim3Transform
from ..constants import (
    GEOMETRY_KIND_COLMAP_POINT_CLOUD,
    GEOMETRY_KIND_TRIANGLE_MESH,
    MARKER_POINT_KIND_COLMAP,
    MARKER_POINT_KIND_MESH_SURFACE,
    MARKER_POINT_KIND_MESH_VERTEX,
    MODEL_STATUS_PENDING,
    PHASE_IDLE,
    PROJECT_PERSISTENCE_SCHEMA_VERSION,
    PROJECT_RESTORE_PHASE_FAILED,
    PROJECT_RESTORE_PHASE_IDLE,
    PROJECT_RESTORE_PHASE_NEEDS_RELINK,
    PROJECT_RESTORE_PHASE_RESTORING,
    ROLE_MOVING,
    ROLE_REFERENCE,
)
from ..display_axes import create_model_display_transform
from ..importer import IMPORT_STAGE_COMPLETED, ImportJobError, create_import_job
from ..mesh_importer import (
    MESH_IMPORT_STAGE_COMPLETED,
    MeshImportJobError,
    create_mesh_import_job,
)
from ..geometry_access import (
    source_position_for_registration_id,
    validated_mesh_surface_position,
)
from ..logging_utils import get_logger
from ..state import (
    MarkerPair,
    MarkerSelectionSnapshot,
    ModelCatalogRecord,
    ModelCatalogSnapshot,
    RUNTIME,
    SelectedPoint,
)
from ..viewport.renderer import request_redraw
from .store import (
    ProjectTextStoreError,
    active_project_text,
    read_active_project_state,
    remove_active_project_state,
    retire_active_project_state,
    write_active_project_state,
)
from .types import (
    ProjectCatalogSnapshot,
    ProjectMarkerPairSnapshot,
    ProjectMarkerPointSnapshot,
    ProjectMarkerSnapshot,
    ProjectModelSnapshot,
    ProjectRestoreIssue,
    ProjectSourceSnapshot,
    ProjectStateSnapshot,
)


_LOGGER = get_logger("project_persistence")
_RESTORE_TIMER_INTERVAL_SECONDS = 0.01


class ProjectPersistenceRuntimeError(RuntimeError):
    """저장 경계 또는 자동 복원 transaction이 계약을 만족하지 못할 때 발생합니다."""


class _ProjectRestoreSourceError(ProjectPersistenceRuntimeError):
    """경로 preflight 실패를 해당 저장 모델과 함께 UI까지 전달합니다."""

    def __init__(self, model_id: str, message: str) -> None:
        """실패한 저장 모델 ID와 사람이 읽을 수 있는 원인을 함께 보관합니다."""

        super().__init__(message)
        self.model_id = str(model_id).strip()


def _utc_now() -> str:
    """schema가 요구하는 UTC ISO-8601 저장 시각을 생성합니다."""

    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _matrix_rows(matrix: object) -> tuple[tuple[float, float, float, float], ...]:
    """불변 Sim(3) 행렬을 JSON snapshot에 쓸 4×4 float 튜플로 복사합니다."""

    values = np.asarray(matrix, dtype=np.float64)
    if values.shape != (4, 4) or not np.isfinite(values).all():
        raise ProjectPersistenceRuntimeError("저장할 alignment 행렬이 유한한 4×4 행렬이 아닙니다.")
    return tuple(tuple(float(item) for item in row) for row in values)


def _relative_sparse_path(sparse_path: str) -> str:
    """현재 저장된 `.blend` 경로 기준의 상대 sparse 경로를 가능할 때만 계산합니다."""

    blend_path = str(getattr(bpy.data, "filepath", "")).strip()
    if not blend_path:
        return ""
    try:
        return os.path.relpath(str(sparse_path), start=str(Path(blend_path).parent))
    except ValueError:
        return ""


def _writer_addon_version() -> tuple[int, int, int]:
    """현재 애드온의 세 부분 버전을 schema 저장값으로 반환합니다."""

    from .. import bl_info

    raw_version = tuple(bl_info.get("version", (0, 0, 0)))
    if len(raw_version) != 3:
        raise ProjectPersistenceRuntimeError("애드온 버전 형식이 세 항목이 아닙니다.")
    return tuple(int(value) for value in raw_version)


def _project_uuid_for_runtime(runtime: Any) -> str:
    """현재 Runtime이 이미 소유한 UUID를 재사용하거나 새 프로젝트 UUID를 생성합니다."""

    restore_state = getattr(runtime, "project_restore", None)
    existing = str(getattr(restore_state, "project_uuid", "")).strip()
    return existing or str(uuid4())


def _missing_source_metadata_model_ids(runtime: Any) -> tuple[str, ...]:
    """기존 fixture처럼 영구 저장 metadata 없이 등록된 모델 ID만 반환합니다."""

    repository = runtime.model_repository
    return tuple(
        model_id
        for model_id in repository.models
        if model_id not in repository.source_metadata
    )


def _project_source_snapshot(runtime: Any, model_id: str) -> ProjectSourceSnapshot:
    """Repository의 검증 완료 source metadata를 저장 schema 값으로 변환합니다."""

    repository = runtime.model_repository
    try:
        metadata = repository.source_metadata_for_model_id(model_id)
    except Exception as error:
        raise ProjectPersistenceRuntimeError(str(error)) from error
    geometry_kind = str(getattr(metadata, "geometry_kind", GEOMETRY_KIND_COLMAP_POINT_CLOUD)).strip().upper()
    is_mesh = geometry_kind == GEOMETRY_KIND_TRIANGLE_MESH
    return ProjectSourceSnapshot(
        selected_root_absolute=metadata.selected_root_absolute,
        sparse_path_absolute=metadata.sparse_path_absolute,
        sparse_path_relative=_relative_sparse_path(metadata.sparse_path_absolute),
        source_format=metadata.source_format,
        source_fingerprint=metadata.source_fingerprint,
        display_point_limit=metadata.display_point_limit,
        axis_preset=metadata.axis_preset,
        axis_forward=metadata.axis_forward,
        axis_up=metadata.axis_up,
        display_transform_fingerprint=metadata.display_transform_fingerprint,
        geometry_kind=geometry_kind,
        kind="OBJ" if is_mesh else "COLMAP",
        path_absolute=metadata.sparse_path_absolute,
        path_relative=_relative_sparse_path(metadata.sparse_path_absolute),
        display_vertex_limit=metadata.display_point_limit,
    )


def _project_model_snapshot(runtime: Any, record: ModelCatalogRecord) -> ProjectModelSnapshot:
    """하나의 확정 Data Layers 레코드를 source·alignment와 함께 snapshot으로 만듭니다."""

    repository = runtime.model_repository
    alignment = repository.alignment_transform_for_model_id(record.model_id)
    return ProjectModelSnapshot(
        model_id=record.model_id,
        display_name=record.display_name,
        load_order=record.load_order,
        source=_project_source_snapshot(runtime, record.model_id),
        catalog=ProjectCatalogSnapshot(
            registration_status=record.registration_status,
            is_anchor=record.is_anchor,
            is_visible=record.is_visible,
            alignment_provenance=record.alignment_provenance,
        ),
        alignment_matrix_float64=_matrix_rows(alignment.matrix_float64),
    )


def _project_marker_snapshot(runtime: Any) -> ProjectMarkerSnapshot:
    """드래프트를 제외한 완료 마커의 원본 요소 ID와 Source Local 좌표를 저장합니다."""

    selection = runtime.marker_selection
    pairs = tuple(
        ProjectMarkerPairSnapshot(
            pair_id=pair.pair_id,
            reference=ProjectMarkerPointSnapshot(
                model_role=ROLE_REFERENCE,
                model_id=pair.reference.model_id,
                point3d_id=pair.reference.point3d_id,
                point_kind=pair.reference.point_kind,
                source_point_id=pair.reference.source_point_id,
                local_position=pair.reference.local_position,
            ),
            moving=ProjectMarkerPointSnapshot(
                model_role=ROLE_MOVING,
                model_id=pair.moving.model_id,
                point3d_id=pair.moving.point3d_id,
                point_kind=pair.moving.point_kind,
                source_point_id=pair.moving.source_point_id,
                local_position=pair.moving.local_position,
            ),
        )
        for pair in selection.completed_pairs
    )
    return ProjectMarkerSnapshot(
        next_pair_id=selection.next_pair_id,
        active_pair_id=selection.active_pair_id,
        completed_pairs=pairs,
    )


def build_project_state_snapshot(runtime: Any = RUNTIME) -> ProjectStateSnapshot:
    """현재 확정 Runtime만 읽어 Text 기록용 작은 불변 snapshot을 생성합니다."""

    catalog = runtime.model_catalog
    repository = runtime.model_repository
    if set(catalog.models) != set(repository.models):
        raise ProjectPersistenceRuntimeError("Data Layers와 COLMAP Repository 모델 목록이 일치하지 않습니다.")
    models = tuple(_project_model_snapshot(runtime, record) for record in catalog.ordered_models())
    return ProjectStateSnapshot(
        schema_version=PROJECT_PERSISTENCE_SCHEMA_VERSION,
        writer_addon_version=_writer_addon_version(),
        project_uuid=_project_uuid_for_runtime(runtime),
        saved_at_utc=_utc_now(),
        models=models,
        markers=_project_marker_snapshot(runtime),
    )


def _set_persistence_error(runtime: Any, error: Exception | str) -> None:
    """저장 실패를 dirty 상태를 유지한 사용자용 원인·영향·해결 정보로 기록합니다."""

    message = str(error).strip() or "프로젝트 상태를 저장하지 못했습니다."
    restore_state = runtime.project_restore
    restore_state.last_error = message
    restore_state.last_message = (
        "원인: 프로젝트 상태를 `.blend` Text에 기록하지 못했습니다. "
        "영향: 마지막 확정 COLMAP Align 변경이 다음 재열기 때 복원되지 않을 수 있습니다. "
        "해결: 오류를 확인한 뒤 다시 저장하십시오."
    )


def notify_project_state_changed(reason: str, runtime: Any = RUNTIME) -> None:
    """확정 변경만 dirty 처리하고 Scene revision을 증가시켜 Blender 저장을 요청합니다."""

    runtime.project_persistence_dirty = True
    try:
        settings = getattr(getattr(bpy.context, "scene", None), "colmap_align_settings", None)
        if settings is not None:
            settings.project_state_revision = int(settings.project_state_revision) + 1
    except (AttributeError, ReferenceError, RuntimeError, ValueError):
        pass
    _LOGGER.info("event=project_state_dirty reason=%s", str(reason).strip() or "unspecified")


def sync_project_state_if_dirty(*, force: bool = False, runtime: Any = RUNTIME) -> bool:
    """저장 경계에서만 최신 확정 snapshot을 Text owner 교체로 동기화합니다."""

    repository = runtime.model_repository
    if not repository.models:
        runtime.project_persistence_dirty = False
        return False
    missing_metadata_model_ids = _missing_source_metadata_model_ids(runtime)
    if missing_metadata_model_ids:
        try:
            has_active_text = active_project_text() is not None
        except ProjectTextStoreError as error:
            runtime.project_persistence_dirty = True
            _set_persistence_error(runtime, error)
            raise ProjectPersistenceRuntimeError(str(error)) from error
        if has_active_text:
            error = ProjectPersistenceRuntimeError(
                "현재 Runtime에 source metadata가 없는 모델이 있어 기존 프로젝트 상태를 안전하게 갱신할 수 없습니다."
            )
            runtime.project_persistence_dirty = True
            _set_persistence_error(runtime, error)
            raise error
        runtime.project_persistence_dirty = False
        runtime.project_restore.last_error = ""
        runtime.project_restore.last_message = (
            "현재 Runtime에는 source metadata가 없는 내부 fixture 모델이 있어 "
            "COLMAP Align 프로젝트 상태를 Text에 저장하지 않았습니다."
        )
        _LOGGER.info(
            "event=project_state_sync_skipped reason=missing_source_metadata model_count=%s",
            len(missing_metadata_model_ids),
        )
        return False
    try:
        has_active_text = active_project_text() is not None
    except ProjectTextStoreError as error:
        runtime.project_persistence_dirty = True
        _set_persistence_error(runtime, error)
        raise ProjectPersistenceRuntimeError(str(error)) from error
    if not force and not runtime.project_persistence_dirty and has_active_text:
        return False
    try:
        snapshot = build_project_state_snapshot(runtime)
        write_active_project_state(snapshot)
    except (ProjectPersistenceRuntimeError, ProjectTextStoreError, ValueError) as error:
        runtime.project_persistence_dirty = True
        _set_persistence_error(runtime, error)
        raise ProjectPersistenceRuntimeError(str(error)) from error
    runtime.project_restore.project_uuid = snapshot.project_uuid
    runtime.project_persistence_dirty = False
    runtime.project_restore.last_error = ""
    runtime.project_restore.last_message = "COLMAP Align 확정 작업 상태를 `.blend`에 저장했습니다."
    _LOGGER.info(
        "event=project_state_synced project=%s model_count=%s",
        snapshot.project_uuid[:8],
        len(snapshot.models),
    )
    return True


def clear_project_state_after_last_model(runtime: Any = RUNTIME) -> bool:
    """마지막 모델 삭제 후 ACTIVE 애드온 소유 Text만 제거하고 dirty를 정리합니다."""

    if runtime.model_repository.models:
        return False
    try:
        removed = remove_active_project_state()
    except ProjectTextStoreError as error:
        runtime.project_persistence_dirty = True
        _set_persistence_error(runtime, error)
        raise ProjectPersistenceRuntimeError(str(error)) from error
    runtime.project_persistence_dirty = False
    runtime.project_restore.project_uuid = ""
    return removed


def _restore_issue(
    cause: str,
    impact: str,
    solution: str,
    *,
    model_id: str = "",
) -> ProjectRestoreIssue:
    """UI와 테스트가 공통으로 쓰는 원인·영향·해결 복원 오류를 생성합니다."""

    return ProjectRestoreIssue(
        cause=str(cause).strip(),
        impact=str(impact).strip(),
        solution=str(solution).strip(),
        model_id=str(model_id).strip(),
    )


def _restore_error_needs_relink(error: Exception | str) -> bool:
    """경로·source fingerprint 문제만 Relink UI로 보내고 구현·파싱 오류는 실패로 구분합니다."""

    message = str(error).strip().lower()
    return any(
        token in message
        for token in (
            "fingerprint",
            "원본 폴더",
            "원본을 다시 연결",
            "dataset root가 존재하지",
            "source file",
        )
    )


def _set_restore_failure(
    runtime: Any,
    issue: ProjectRestoreIssue,
    *,
    needs_relink: bool,
) -> None:
    """부분 Runtime을 rollback한 뒤 UI가 표시할 실패 또는 Relink 상태를 설정합니다."""

    _rollback_restore_runtime(runtime)
    state = runtime.project_restore
    state.issues = [issue]
    state.last_error = issue.cause
    state.last_message = f"원인: {issue.cause} 영향: {issue.impact} 해결: {issue.solution}"
    state.phase = PROJECT_RESTORE_PHASE_NEEDS_RELINK if needs_relink else PROJECT_RESTORE_PHASE_FAILED
    _LOGGER.warning(
        "event=project_restore_failed project=%s phase=%s model_id=%s",
        state.project_uuid[:8],
        state.phase,
        issue.model_id or "<project>",
    )
    request_redraw()


def _clear_restore_timer(
    runtime: Any,
    *,
    unregister: bool,
    callback: Any | None = None,
) -> None:
    """지정 callback만 해제해 stale timer가 새 복원 예약을 지우지 못하게 합니다."""

    current_callback = getattr(runtime, "project_restore_timer", None)
    target_callback = current_callback if callback is None else callback
    if unregister and callable(target_callback):
        try:
            if bpy.app.timers.is_registered(target_callback):
                bpy.app.timers.unregister(target_callback)
        except (ReferenceError, RuntimeError, ValueError):
            pass
    if callback is None or current_callback is callback:
        runtime.project_restore_timer = None


def cancel_project_restore_for_file_load(runtime: Any = RUNTIME) -> None:
    """파일 교체 전에 generation을 증가시키고 restore timer만 취소합니다."""

    _clear_restore_timer(runtime, unregister=True)
    runtime.project_restore.clear()


def _rollback_restore_runtime(runtime: Any) -> None:
    """복원 transaction 실패 시 이번 Runtime의 Repository·Catalog·GPU 참조를 모두 비웁니다."""

    try:
        runtime.marker_alignment.clear()
        runtime.icp_alignment.clear()
        runtime.merge_export.clear()
    except (AttributeError, RuntimeError):
        pass
    runtime.model_catalog.clear()
    runtime.model_repository.clear()
    runtime.marker_selection.clear()
    runtime.point_clouds.clear()
    runtime.gpu_batches.clear()
    runtime.undo_journal.clear()
    runtime.clear_import_job()


def _blend_relative_candidate(relative_path: str) -> str | None:
    """현재 `.blend`가 저장된 경우에만 상대 source 경로 후보를 절대 경로로 만듭니다."""

    if not relative_path:
        return None
    blend_path = str(getattr(bpy.data, "filepath", "")).strip()
    if not blend_path:
        return None
    return str((Path(blend_path).parent / relative_path).resolve(strict=False))


def _source_input_format(source_format: str) -> str:
    """저장된 COLMAP binary/text 형식을 discovery 입력 enum으로 변환합니다."""

    normalized = str(source_format).strip().upper()
    if normalized not in {"BINARY", "TEXT"}:
        raise ProjectPersistenceRuntimeError("저장된 COLMAP source 형식이 올바르지 않습니다.")
    return normalized


def _source_is_mesh(model: ProjectModelSnapshot) -> bool:
    """저장 snapshot의 source kind가 삼각형 OBJ인지 반환합니다."""

    return str(model.source.kind).strip().upper() == "OBJ" or (
        str(model.source.geometry_kind).strip().upper() == GEOMETRY_KIND_TRIANGLE_MESH
    )


def _source_candidates(runtime: Any, model: ProjectModelSnapshot) -> tuple[str, ...]:
    """사용자가 Relink한 후보를 우선해 v1·v2 상대·절대 경로를 중복 없이 구성합니다."""

    state = runtime.project_restore
    manual = str(state.relink_paths.get(model.model_id, "")).strip()
    candidates: list[str] = []
    if manual:
        candidates.append(manual)
    relative_path = (
        model.source.path_relative or model.source.sparse_path_relative
        if _source_is_mesh(model)
        else model.source.sparse_path_relative
    )
    absolute_path = (
        model.source.path_absolute or model.source.sparse_path_absolute
        if _source_is_mesh(model)
        else model.source.sparse_path_absolute
    )
    relative = _blend_relative_candidate(relative_path)
    if relative:
        candidates.append(relative)
    candidates.append(absolute_path)
    result: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        normalized = str(candidate).strip()
        if not normalized:
            continue
        key = os.path.normcase(os.path.normpath(normalized))
        if key not in seen:
            seen.add(key)
            result.append(normalized)
    return tuple(result)


def _resolve_restore_source(runtime: Any, model: ProjectModelSnapshot) -> str:
    """COLMAP discovery 또는 OBJ 파일 검증으로 저장 source 후보 하나를 찾습니다."""

    if _source_is_mesh(model):
        last_error: Exception | None = None
        for candidate in _source_candidates(runtime, model):
            path = Path(candidate).expanduser()
            try:
                if path.suffix.casefold() != ".obj" or not path.is_file():
                    raise ProjectPersistenceRuntimeError("저장된 OBJ 원본 파일이 존재하지 않습니다.")
                if _sha256_file_for_restore(path) != model.source.source_fingerprint:
                    raise ProjectPersistenceRuntimeError("저장된 OBJ source fingerprint와 현재 원본이 다릅니다.")
                return str(path)
            except (OSError, ProjectPersistenceRuntimeError) as error:
                last_error = error
        detail = str(last_error).strip() if last_error is not None else "경로 후보가 없습니다."
        raise ProjectPersistenceRuntimeError(f"OBJ 원본 파일을 찾을 수 없습니다: {detail}")

    from ..colmap_io import discover_sparse_model

    format_name = _source_input_format(model.source.source_format)
    last_error: Exception | None = None
    for candidate in _source_candidates(runtime, model):
        try:
            layout = discover_sparse_model(candidate, input_format=format_name)
        except (OSError, ValueError, RuntimeError) as error:
            last_error = error
            continue
        if layout.selected_format != model.source.source_format:
            last_error = ProjectPersistenceRuntimeError("저장된 COLMAP source 형식과 발견한 형식이 다릅니다.")
            continue
        return candidate
    detail = str(last_error).strip() if last_error is not None else "경로 후보가 없습니다."
    raise ProjectPersistenceRuntimeError(f"COLMAP 원본 폴더를 찾을 수 없습니다: {detail}")


def _sha256_file_for_restore(path: Path) -> str:
    """복원 preflight에서 OBJ source fingerprint를 스트리밍으로 계산합니다."""

    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _preflight_restore_sources(runtime: Any, snapshot: ProjectStateSnapshot) -> None:
    """대용량 읽기 전에 COLMAP 폴더와 OBJ 파일의 경로·지문을 확인합니다."""

    for model in sorted(snapshot.models, key=lambda item: item.load_order):
        try:
            _resolve_restore_source(runtime, model)
        except ProjectPersistenceRuntimeError as error:
            raise _ProjectRestoreSourceError(model.model_id, str(error)) from error


def _start_restore_import(runtime: Any, model: ProjectModelSnapshot) -> None:
    """저장 source 종류에 맞는 COLMAP 또는 OBJ ImportJob 하나를 시작합니다."""

    source_path = _resolve_restore_source(runtime, model)
    try:
        display_transform = create_model_display_transform(
            model.source.axis_preset,
            model.source.axis_forward,
            model.source.axis_up,
        )
    except ValueError as error:
        raise ProjectPersistenceRuntimeError(f"저장된 Display Axis가 올바르지 않습니다: {error}") from error
    if display_transform.fingerprint != model.source.display_transform_fingerprint:
        raise ProjectPersistenceRuntimeError("저장된 Display Axis fingerprint가 현재 구현과 다릅니다.")
    try:
        selected_root = (
            runtime.project_restore.relink_paths.get(model.model_id)
            or model.source.selected_root_absolute
            or source_path
        )
        if _source_is_mesh(model):
            job = create_mesh_import_job(
                source_path,
                axis_preset=model.source.axis_preset,
                axis_forward=model.source.axis_forward,
                axis_up=model.source.axis_up,
                display_vertex_limit=model.source.display_vertex_limit,
                model_id_factory=lambda: model.model_id,
                display_name_hint=model.display_name,
                expected_source_fingerprint=model.source.source_fingerprint,
                dataset_root=selected_root,
            )
        else:
            job = create_import_job(
                source_path,
                input_format=_source_input_format(model.source.source_format),
                axis_preset=model.source.axis_preset,
                axis_forward=model.source.axis_forward,
                axis_up=model.source.axis_up,
                display_point_limit=model.source.display_point_limit,
                model_id_factory=lambda: model.model_id,
                display_name_hint=model.display_name,
                expected_source_fingerprint=model.source.source_fingerprint,
                dataset_root=selected_root,
            )
    except (ImportJobError, MeshImportJobError, ValueError) as error:
        raise ProjectPersistenceRuntimeError(str(error)) from error
    runtime.import_job = job


def _validate_restored_import(runtime: Any, job: Any, model: ProjectModelSnapshot) -> None:
    """완료된 기존 ImportJob이 저장 snapshot의 안정 모델 identity를 유지했는지 확인합니다."""

    restored = getattr(job, "staged_model", None)
    if restored is None:
        raise ProjectPersistenceRuntimeError("복원 ImportJob이 완료 모델을 반환하지 않았습니다.")
    if restored.model_id != model.model_id:
        raise ProjectPersistenceRuntimeError("복원 ImportJob의 모델 ID가 저장된 안정 ID와 다릅니다.")
    if restored.display_name != model.display_name:
        raise ProjectPersistenceRuntimeError("복원 ImportJob의 표시 이름이 저장 상태와 다릅니다.")
    metadata = runtime.model_repository.source_metadata_for_model_id(model.model_id)
    if metadata.source_fingerprint != model.source.source_fingerprint:
        raise ProjectPersistenceRuntimeError("복원 ImportJob의 source fingerprint가 저장 상태와 다릅니다.")


def _marker_position_lookup(
    runtime: Any,
    snapshot: ProjectMarkerSnapshot,
) -> dict[tuple[str, str, int, tuple[float, float, float] | None], tuple[float, float, float]]:
    """저장된 COLMAP 점·메시 정점·삼각면 참조를 검증해 Source Local 좌표로 복원합니다."""

    requested_by_model: dict[
        str,
        set[tuple[str, int, tuple[float, float, float] | None]],
    ] = {}
    for pair in snapshot.completed_pairs:
        for point in (pair.reference, pair.moving):
            point_kind = str(point.point_kind).strip().upper()
            source_id = int(point.source_point_id if point.source_point_id is not None else point.point3d_id)
            local_position = (
                None
                if point.local_position is None
                else tuple(float(value) for value in point.local_position)
            )
            requested_by_model.setdefault(point.model_id, set()).add(
                (point_kind, source_id, local_position)
            )

    lookup: dict[
        tuple[str, str, int, tuple[float, float, float] | None],
        tuple[float, float, float],
    ] = {}
    maximum_point_id = int(np.iinfo(np.uint64).max)
    for model_id, requested_points in requested_by_model.items():
        model = runtime.model_repository.models.get(model_id)
        if model is None:
            raise ProjectPersistenceRuntimeError("marker가 존재하지 않는 복원 model_id를 참조합니다.")
        if any(point_id > maximum_point_id for _kind, point_id, _position in requested_points):
            raise ProjectPersistenceRuntimeError("marker POINT3D_ID가 uint64 범위를 초과했습니다.")
        for point_kind, point_id, local_position in requested_points:
            try:
                if point_kind == MARKER_POINT_KIND_MESH_SURFACE:
                    if local_position is None:
                        raise ValueError("면 마커 Source Local 좌표가 비어 있습니다.")
                    position = validated_mesh_surface_position(
                        model,
                        point_id,
                        local_position,
                    )
                else:
                    position = source_position_for_registration_id(model, point_kind, point_id)
                    if local_position is not None and not np.allclose(
                        position,
                        np.asarray(local_position, dtype=np.float64),
                        rtol=0.0,
                        atol=1.0e-9,
                    ):
                        raise ValueError("저장된 마커 좌표가 원본 점과 다릅니다.")
            except (KeyError, TypeError, ValueError) as error:
                if point_kind == MARKER_POINT_KIND_MESH_SURFACE:
                    label = "OBJ 삼각면 위치"
                elif point_kind == MARKER_POINT_KIND_MESH_VERTEX:
                    label = "OBJ 원본 정점 ID"
                else:
                    label = "COLMAP POINT3D_ID"
                raise ProjectPersistenceRuntimeError(f"marker 원본 {label}를 찾을 수 없습니다: {point_id}") from error
            if position.shape != (3,) or not np.isfinite(position).all():
                raise ProjectPersistenceRuntimeError("marker 원본 POINT3D 좌표가 유한하지 않습니다.")
            lookup[(model_id, point_kind, point_id, local_position)] = tuple(
                float(value) for value in position
            )
    return lookup


def _restored_marker_snapshot(runtime: Any, snapshot: ProjectMarkerSnapshot) -> MarkerSelectionSnapshot:
    """저장 좌표 없이 원본 종류·ID를 다시 조회해 유휴 MarkerSelection snapshot을 구성합니다."""

    position_lookup = _marker_position_lookup(runtime, snapshot)
    pairs: list[MarkerPair] = []
    for pair in snapshot.completed_pairs:
        pairs.append(
            MarkerPair(
                pair_id=pair.pair_id,
                reference=SelectedPoint(
                    model_role=ROLE_REFERENCE,
                    model_id=pair.reference.model_id,
                    point3d_id=pair.reference.point3d_id,
                    point_kind=pair.reference.point_kind,
                    source_point_id=pair.reference.source_point_id,
                    local_position=position_lookup[
                        (
                            pair.reference.model_id,
                            pair.reference.point_kind,
                            pair.reference.source_point_id,
                            pair.reference.local_position,
                        )
                    ],
                ),
                moving=SelectedPoint(
                    model_role=ROLE_MOVING,
                    model_id=pair.moving.model_id,
                    point3d_id=pair.moving.point3d_id,
                    point_kind=pair.moving.point_kind,
                    source_point_id=pair.moving.source_point_id,
                    local_position=position_lookup[
                        (
                            pair.moving.model_id,
                            pair.moving.point_kind,
                            pair.moving.source_point_id,
                            pair.moving.local_position,
                        )
                    ],
                ),
            )
        )
    return MarkerSelectionSnapshot(
        phase=PHASE_IDLE,
        next_pair_id=snapshot.next_pair_id,
        active_pair_id=snapshot.active_pair_id,
        completed_pairs=tuple(pairs),
        draft=None,
        message="",
    )


def _apply_restored_snapshot(runtime: Any, snapshot: ProjectStateSnapshot) -> None:
    """모든 ImportJob 성공 뒤 alignment·catalog·marker를 한 번에 확정 적용합니다."""

    ordered_models = tuple(sorted(snapshot.models, key=lambda item: item.load_order))
    if tuple(runtime.model_repository.models) != tuple(model.model_id for model in ordered_models):
        raise ProjectPersistenceRuntimeError("복원 모델 순서 또는 모델 ID가 저장 상태와 다릅니다.")
    transforms = {
        model.model_id: Sim3Transform(matrix_float64=model.alignment_matrix_float64)
        for model in ordered_models
    }
    catalog_snapshot = ModelCatalogSnapshot(
        records=tuple(
            ModelCatalogRecord(
                model_id=model.model_id,
                display_name=model.display_name,
                load_order=model.load_order,
                registration_status=model.catalog.registration_status,
                is_anchor=model.catalog.is_anchor,
                is_visible=model.catalog.is_visible,
                alignment_provenance=model.catalog.alignment_provenance,
                geometry_kind=model.source.geometry_kind,
            )
            for model in ordered_models
        ),
        model_order=tuple(model.model_id for model in ordered_models),
        selected_model_id=None,
        selected_reference_model_id=None,
        selected_moving_model_id=None,
        selected_model_ids=(),
    )
    marker_snapshot = _restored_marker_snapshot(runtime, snapshot.markers)
    for model_id, transform in transforms.items():
        runtime.model_repository.set_alignment_transform(model_id, transform)
    runtime.model_catalog.restore_snapshot(catalog_snapshot)
    runtime.marker_selection.restore_snapshot(marker_snapshot)
    runtime.marker_alignment.clear()
    runtime.layer_transform.clear(message="프로젝트 복원으로 레이어 변환 미리보기를 초기화했습니다.")
    runtime.icp_alignment.clear()
    runtime.merge_export.clear()
    runtime.undo_journal.clear()
    runtime.clear_data_layer_click()


def _restore_timer(
    runtime: Any = RUNTIME,
    *,
    generation: int | None = None,
    project_uuid: str | None = None,
    callback: Any | None = None,
) -> float | None:
    """동일 generation·UUID의 ImportJob만 한 tick씩 진행해 stale 결과를 버립니다."""

    state = runtime.project_restore
    snapshot = state.snapshot
    expected_generation = state.generation if generation is None else generation
    expected_project_uuid = state.project_uuid if project_uuid is None else project_uuid
    if (
        callback is not None
        and getattr(runtime, "project_restore_timer", None) is not callback
    ):
        _clear_restore_timer(runtime, unregister=False, callback=callback)
        return None
    if (
        state.phase != PROJECT_RESTORE_PHASE_RESTORING
        or snapshot is None
        or state.generation != expected_generation
        or state.project_uuid != expected_project_uuid
    ):
        _clear_restore_timer(runtime, unregister=False, callback=callback)
        return None
    ordered_models = tuple(sorted(snapshot.models, key=lambda item: item.load_order))
    if state.current_model_index >= len(ordered_models):
        try:
            _apply_restored_snapshot(runtime, snapshot)
        except Exception as error:
            _set_restore_failure(
                runtime,
                _restore_issue(
                    str(error),
                    "저장된 정렬 작업을 부분적으로 적용하지 않았습니다.",
                    "원본 COLMAP과 프로젝트 상태를 확인한 뒤 다시 시도하십시오.",
                ),
                needs_relink=False,
            )
            _clear_restore_timer(runtime, unregister=False, callback=callback)
            return None
        relinked = bool(state.relink_paths)
        state.phase = PROJECT_RESTORE_PHASE_IDLE
        state.snapshot = None
        state.current_model_index = 0
        state.issues.clear()
        state.last_error = ""
        state.last_message = "저장된 COLMAP Align 작업 상태를 복원했습니다."
        state.relink_paths.clear()
        runtime.project_persistence_dirty = relinked
        if relinked:
            notify_project_state_changed("원본 다시 연결 복원 완료", runtime=runtime)
        _clear_restore_timer(runtime, unregister=False, callback=callback)
        try:
            from ..properties import sync_marker_pair_list

            sync_marker_pair_list()
        except (AttributeError, ReferenceError, RuntimeError):
            pass
        request_redraw()
        _LOGGER.info("event=project_restore_completed project=%s", state.project_uuid[:8])
        return None
    model = ordered_models[state.current_model_index]
    job = runtime.import_job
    try:
        if job is None:
            _start_restore_import(runtime, model)
            return _RESTORE_TIMER_INTERVAL_SECONDS
        result = job.step(runtime)
        if result == "RUNNING":
            return _RESTORE_TIMER_INTERVAL_SECONDS
        if job.stage == IMPORT_STAGE_COMPLETED:
            _validate_restored_import(runtime, job, model)
            runtime.clear_import_job()
            state.current_model_index += 1
            request_redraw()
            return _RESTORE_TIMER_INTERVAL_SECONDS
        message = str(getattr(job, "error_message", "")).strip() or "COLMAP 원본 가져오기에 실패했습니다."
        runtime.clear_import_job()
        needs_relink = _restore_error_needs_relink(message)
        _set_restore_failure(
            runtime,
            _restore_issue(
                message,
                "저장된 점군과 정렬 작업을 복원하지 않았습니다.",
                "저장 당시와 같은 COLMAP 원본 폴더를 다시 지정한 뒤 재시도하십시오.",
                model_id=model.model_id,
            ),
            needs_relink=needs_relink,
        )
    except Exception as error:
        _set_restore_failure(
            runtime,
            _restore_issue(
                str(error),
                "저장된 점군과 정렬 작업을 복원하지 않았습니다.",
                "원본 COLMAP 폴더와 프로젝트 상태를 확인한 뒤 재시도하십시오.",
                model_id=model.model_id,
            ),
            needs_relink=_restore_error_needs_relink(error),
        )
    _clear_restore_timer(runtime, unregister=False, callback=callback)
    return None


def _schedule_restore_timer(runtime: Any) -> None:
    """현재 generation·UUID 전용 callback 하나만 등록해 중복 복원을 막습니다."""

    state = runtime.project_restore
    generation = state.generation
    project_uuid = state.project_uuid
    existing_callback = getattr(runtime, "project_restore_timer", None)
    if callable(existing_callback):
        try:
            is_registered = bpy.app.timers.is_registered(existing_callback)
        except (ReferenceError, RuntimeError, ValueError):
            is_registered = False
        if (
            is_registered
            and getattr(existing_callback, "_colmap_align_generation", None) == generation
            and getattr(existing_callback, "_colmap_align_project_uuid", None) == project_uuid
        ):
            return
        _clear_restore_timer(runtime, unregister=is_registered)

    def restore_callback() -> float | None:
        """예약 시점의 stale token을 캡처해 현재 Runtime에만 복원을 적용합니다."""

        return _restore_timer(
            runtime,
            generation=generation,
            project_uuid=project_uuid,
            callback=restore_callback,
        )

    restore_callback._colmap_align_generation = generation
    restore_callback._colmap_align_project_uuid = project_uuid
    runtime.project_restore_timer = restore_callback
    bpy.app.timers.register(restore_callback, first_interval=0.0)


def schedule_project_restore_if_available(runtime: Any = RUNTIME) -> bool:
    """ACTIVE owner Text를 검증해 빈 Runtime에서만 비동기 자동 복원을 예약합니다."""

    if runtime.model_repository.models or runtime.model_catalog.models:
        return False
    state = runtime.project_restore
    if state.phase == PROJECT_RESTORE_PHASE_RESTORING:
        return False
    try:
        snapshot = read_active_project_state()
    except ProjectTextStoreError as error:
        state.generation += 1
        state.snapshot = None
        state.issues = [
            _restore_issue(
                str(error),
                "저장된 COLMAP Align 작업을 복원하지 않았습니다.",
                "프로젝트 Text를 복구하거나 새 프로젝트 시작으로 기존 상태를 명시적으로 은퇴하십시오.",
            )
        ]
        state.phase = PROJECT_RESTORE_PHASE_FAILED
        state.last_error = str(error)
        state.last_message = f"원인: {error} 영향: 자동 복원을 중단했습니다. 해결: 프로젝트 상태를 확인하십시오."
        request_redraw()
        return False
    if snapshot is None:
        return False
    state.generation += 1
    state.project_uuid = snapshot.project_uuid
    state.snapshot = snapshot
    state.current_model_index = 0
    state.issues.clear()
    state.last_error = ""
    state.last_message = "저장된 COLMAP Align 원본을 확인하는 중입니다."
    state.phase = PROJECT_RESTORE_PHASE_RESTORING
    try:
        _preflight_restore_sources(runtime, snapshot)
    except Exception as error:
        _set_restore_failure(
            runtime,
            _restore_issue(
                str(error),
                "저장된 점군과 정렬 작업을 복원하지 않았습니다.",
                "같은 COLMAP 원본 폴더를 다시 지정한 뒤 재시도하십시오.",
                model_id=getattr(error, "model_id", ""),
            ),
            needs_relink=True,
        )
        return False
    _schedule_restore_timer(runtime)
    request_redraw()
    _LOGGER.info("event=project_restore_scheduled project=%s", snapshot.project_uuid[:8])
    return True


def relink_project_source(model_id: str, selected_path: str, runtime: Any = RUNTIME) -> None:
    """사용자가 고른 한 source 후보를 검증 전 metadata 변경 없이 Relink 대기값으로 기록합니다."""

    state = runtime.project_restore
    snapshot = state.snapshot
    normalized_model_id = str(model_id).strip()
    candidate = str(selected_path).strip()
    if state.phase != PROJECT_RESTORE_PHASE_NEEDS_RELINK or snapshot is None:
        raise ProjectPersistenceRuntimeError("원본 다시 연결이 필요한 복원 상태가 아닙니다.")
    if not candidate:
        raise ProjectPersistenceRuntimeError("다시 연결할 COLMAP 폴더를 선택하십시오.")
    if normalized_model_id not in {model.model_id for model in snapshot.models}:
        raise ProjectPersistenceRuntimeError("다시 연결할 저장 모델을 찾을 수 없습니다.")
    state.relink_paths[normalized_model_id] = str(Path(candidate).expanduser())
    state.last_error = ""
    state.last_message = "선택한 원본 후보를 검증한 뒤 전체 프로젝트 복원을 다시 시도합니다."


def retry_project_restore(runtime: Any = RUNTIME) -> bool:
    """이전 실패 snapshot을 같은 전체 rollback 계약으로 다시 preflight·복원합니다."""

    state = runtime.project_restore
    snapshot = state.snapshot
    if snapshot is None:
        raise ProjectPersistenceRuntimeError("재시도할 저장 프로젝트 상태가 없습니다.")
    if state.phase not in {PROJECT_RESTORE_PHASE_NEEDS_RELINK, PROJECT_RESTORE_PHASE_FAILED}:
        raise ProjectPersistenceRuntimeError("복원 오류 또는 원본 다시 연결 상태에서만 재시도할 수 있습니다.")
    if runtime.model_repository.models or runtime.model_catalog.models:
        raise ProjectPersistenceRuntimeError("빈 Runtime에서만 프로젝트 복원을 재시도할 수 있습니다.")
    state.generation += 1
    state.phase = PROJECT_RESTORE_PHASE_RESTORING
    state.current_model_index = 0
    state.issues.clear()
    state.last_error = ""
    try:
        _preflight_restore_sources(runtime, snapshot)
    except Exception as error:
        _set_restore_failure(
            runtime,
            _restore_issue(
                str(error),
                "저장된 점군과 정렬 작업을 복원하지 않았습니다.",
                "같은 COLMAP 원본 폴더를 다시 지정한 뒤 재시도하십시오.",
                model_id=getattr(error, "model_id", ""),
            ),
            needs_relink=True,
        )
        return False
    _schedule_restore_timer(runtime)
    request_redraw()
    return True


def cancel_project_restore(runtime: Any = RUNTIME) -> bool:
    """진행 중인 restore ImportJob을 취소하고 Runtime을 빈 상태로 되돌립니다."""

    state = runtime.project_restore
    if state.phase not in {PROJECT_RESTORE_PHASE_RESTORING, PROJECT_RESTORE_PHASE_NEEDS_RELINK}:
        return False
    job = runtime.import_job
    if job is not None:
        job.request_cancel()
        job.step(runtime)
    _clear_restore_timer(runtime, unregister=True)
    _rollback_restore_runtime(runtime)
    state.generation += 1
    state.phase = PROJECT_RESTORE_PHASE_IDLE
    state.snapshot = None
    state.current_model_index = 0
    state.issues.clear()
    state.last_error = ""
    state.last_message = "프로젝트 자동 복원을 취소했습니다. 저장된 Text는 유지합니다."
    state.relink_paths.clear()
    request_redraw()
    return True


def start_new_project_after_restore_error(runtime: Any = RUNTIME) -> bool:
    """명시 확인 뒤 ACTIVE 상태만 RETIRED로 바꾸고 빈 Runtime에서 새 프로젝트를 허용합니다."""

    state = runtime.project_restore
    if state.phase not in {PROJECT_RESTORE_PHASE_FAILED, PROJECT_RESTORE_PHASE_NEEDS_RELINK}:
        raise ProjectPersistenceRuntimeError("저장된 복원 오류가 있을 때만 새 프로젝트를 시작할 수 있습니다.")
    try:
        retired = retire_active_project_state()
    except ProjectTextStoreError as error:
        raise ProjectPersistenceRuntimeError(str(error)) from error
    _clear_restore_timer(runtime, unregister=True)
    _rollback_restore_runtime(runtime)
    state.clear()
    runtime.project_persistence_dirty = False
    request_redraw()
    return retired


def restore_input_block_reason(runtime: Any = RUNTIME) -> str | None:
    """복원 transaction·오류 중 수동 import와 편집을 막을 정확한 사용자 사유를 반환합니다."""

    phase = runtime.project_restore.phase
    if phase == PROJECT_RESTORE_PHASE_RESTORING:
        return "저장된 프로젝트를 복원하는 동안에는 가져오기와 편집을 할 수 없습니다."
    if phase == PROJECT_RESTORE_PHASE_NEEDS_RELINK:
        return "저장된 프로젝트의 원본 다시 연결 또는 새 프로젝트 시작을 먼저 완료하십시오."
    if phase == PROJECT_RESTORE_PHASE_FAILED:
        return "저장된 프로젝트 복원 오류를 해결하거나 새 프로젝트 시작을 먼저 선택하십시오."
    return None


def restore_status_issue(runtime: Any = RUNTIME) -> ProjectRestoreIssue | None:
    """패널이 표시할 가장 최근 복원 오류 하나를 반환합니다."""

    issues = runtime.project_restore.issues
    return issues[0] if issues else None


__all__ = (
    "ProjectPersistenceRuntimeError",
    "build_project_state_snapshot",
    "cancel_project_restore",
    "cancel_project_restore_for_file_load",
    "clear_project_state_after_last_model",
    "notify_project_state_changed",
    "relink_project_source",
    "restore_input_block_reason",
    "restore_status_issue",
    "retry_project_restore",
    "schedule_project_restore_if_available",
    "start_new_project_after_restore_error",
    "sync_project_state_if_dirty",
)
