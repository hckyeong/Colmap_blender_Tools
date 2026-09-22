"""원본 자료를 보존하는 형상별 축 편집·원자 확정·Undo 서비스를 제공합니다."""
from __future__ import annotations
from typing import Any
from uuid import uuid4
import numpy as np
from .axis_settings_state import AxisEditSnapshot, AxisSettingsRuntimeState
from .constants import MARKER_GEOMETRY_SOURCE_MAIN, MARKER_GEOMETRY_SOURCE_COMPANION
from .display_axes import DEFAULT_AXIS_PRESET, RC_OBJ_AXIS_PRESET, ModelDisplayTransform, create_model_display_transform
from .geometry_access import bounds_for_model

AXIS_SETTINGS_COMMAND = "EDIT_GEOMETRY_AXES"


def axis_transform_center(runtime: Any, model_id: str, *, companion: bool = False) -> np.ndarray:
    """대형 원본 배열을 순회하지 않고 저장된 바운딩 박스 중심을 반환합니다."""
    repository = runtime.model_repository
    model = repository.companion_mesh_for_model_id(model_id) if companion else repository.models[model_id]
    minimum, maximum = bounds_for_model(model)
    return minimum * 0.5 + maximum * 0.5


def centered_axis_transform(runtime: Any, model_id: str, transform: ModelDisplayTransform, *, companion: bool = False) -> ModelDisplayTransform:
    """현재 위치를 유지하면서 회전·배율의 기준점을 원본 바운딩 박스 중심으로 옮깁니다."""
    center = axis_transform_center(runtime, model_id, companion=companion)
    if tuple(center) == transform.pivot_source:
        return transform
    base = create_model_display_transform(transform.preset_id, transform.forward_axis, transform.up_axis)
    translation = transform.matrix_float64[:3, 3] - (base.matrix_float64[:3, :3] @ center - transform.matrix_float64[:3, :3] @ center)
    return create_model_display_transform(
        transform.preset_id, transform.forward_axis, transform.up_axis,
        rotation_xyz_degrees=transform.rotation_xyz_degrees,
        translation_xyz=translation, scale_xyz=transform.scale_xyz, pivot_source=center,
    )


def axis_transform_at_world_origin(runtime: Any, model_id: str, transform: ModelDisplayTransform, *, companion: bool = False) -> ModelDisplayTransform:
    """기존 정렬 변환까지 반영해 바운딩 박스 중심이 월드 원점에 오도록 이동합니다."""
    centered = centered_axis_transform(runtime, model_id, transform, companion=companion)
    alignment = runtime.model_repository.alignment_transform_for_model_id(model_id).matrix_float64
    target = np.linalg.solve(alignment, np.array((0.0, 0.0, 0.0, 1.0)))[:3]
    base = create_model_display_transform(centered.preset_id, centered.forward_axis, centered.up_axis)
    translation = target - base.matrix_float64[:3, :3] @ centered.pivot_source
    return create_model_display_transform(
        centered.preset_id, centered.forward_axis, centered.up_axis,
        rotation_xyz_degrees=centered.rotation_xyz_degrees,
        translation_xyz=translation, scale_xyz=centered.scale_xyz, pivot_source=centered.pivot_source,
    )


class AxisSettingsError(RuntimeError):
    """유효하지 않은 축 설정이나 만료된 편집 기준을 나타냅니다."""


def axis_edit_active_block_reason(runtime: Any) -> str | None:
    """축 팝업의 임시 편집이 다른 계산·데이터 변경을 막는 이유를 반환합니다."""
    state = getattr(runtime, "axis_settings", None)
    return "열린 축 설정을 적용하거나 취소한 뒤 진행하십시오." if state is not None and state.is_active else None


def _other_operation_block_reason(runtime: Any) -> str | None:
    """확정 기준을 동시에 바꾸는 작업이 진행 중인지 확인합니다."""
    if not bool(getattr(runtime, "session_active", False)):
        return "활성 COLMAP Align 작업공간에서 축 설정을 열어 주세요."
    if bool(getattr(runtime, "import_in_progress", False)):
        return "모델 가져오기가 끝난 뒤 축을 설정하십시오."
    from .project_persistence.runtime import restore_input_block_reason

    restore_reason = restore_input_block_reason(runtime)
    if restore_reason is not None:
        return restore_reason
    if bool(getattr(getattr(runtime, "marker_selection", None), "has_draft", False)) or bool(getattr(runtime, "modal_active", False)):
        return "마커 입력을 확정하거나 취소한 뒤 축을 설정하십시오."
    if bool(getattr(getattr(runtime, "layer_transform", None), "is_active", False)):
        return "레이어 변환을 확정하거나 취소한 뒤 축을 설정하십시오."
    marker = getattr(runtime, "marker_alignment", None)
    icp = getattr(runtime, "icp_alignment", None)
    if bool(getattr(marker, "is_active", False)) or bool(getattr(icp, "is_running", False)) or bool(getattr(icp, "has_preview", False)):
        return "정렬 작업과 미리보기를 마친 뒤 축을 설정하십시오."
    for name in ("merge_export", "obj_export"):
        if bool(getattr(getattr(runtime, name, None), "is_running", False)):
            return "내보내기가 끝난 뒤 축을 설정하십시오."
    if bool(getattr(getattr(runtime, "undo_journal", None), "scope_active", False)):
        return "현재 편집을 확정하거나 취소한 뒤 축을 설정하십시오."
    catalog = getattr(runtime, "model_catalog", None)
    if catalog is not None and catalog.has_preview():
        return "정렬 미리보기를 마친 뒤 축을 설정하십시오."
    return None


def axis_edit_block_reason(runtime: Any, model_id: str | None = None) -> str | None:
    """새 축 편집의 세션·작업 잠금·대상 유효성을 한 경계에서 검사합니다."""
    reason = axis_edit_active_block_reason(runtime) or _other_operation_block_reason(runtime)
    if reason is not None:
        return reason
    if model_id is not None and (model_id not in runtime.model_repository.models or model_id not in runtime.model_catalog.models):
        return "축을 설정할 데이터 레이어를 찾을 수 없습니다."
    return None


def _snapshot(runtime: Any, model_id: str) -> AxisEditSnapshot:
    """현재 축과 이를 참조하는 모든 정렬 감사 기록을 원본 복사 없이 묶습니다."""
    repository = runtime.model_repository
    record = runtime.model_catalog.models.get(model_id)
    if record is None or model_id not in repository.models:
        raise AxisSettingsError("축 설정 대상이 더 이상 존재하지 않습니다.")
    companion = repository.companion_mesh_for_model_id(model_id)
    related = tuple(
        (other_id, other.alignment_provenance)
        for other_id, other in sorted(runtime.model_catalog.models.items())
        if other_id != model_id and any(
            owner_id == model_id
            for owner_id, _source in getattr(other.alignment_provenance, "geometry_dependencies", ())
        )
    )
    return AxisEditSnapshot(model_id, repository.display_transform_for_model_id(model_id),
        repository.companion_display_transform_for_model_id(model_id) if companion is not None else None,
        record.alignment_provenance, related)


def _geometry_identity(runtime: Any, model_id: str) -> tuple:
    """같은 ID의 데이터가 교체된 경우도 감지할 메모리·형상 revision 기준을 만듭니다."""
    repository = runtime.model_repository
    model = repository.models.get(model_id)
    companion = repository.companion_mesh_for_model_id(model_id)
    return (id(model), getattr(model, "geometry_revision", None), id(companion), getattr(companion, "geometry_revision", None))


def begin_axis_edit(runtime: Any, model_id: str, *, window_pointer: int = 0, workspace_pointer: int = 0) -> str:
    """한 데이터셋을 고정하고 확정 저장소와 분리된 편집 토큰을 발급합니다."""
    model_id = str(model_id).strip()
    reason = axis_edit_block_reason(runtime, model_id)
    if reason is not None:
        raise AxisSettingsError(reason)
    state = runtime.axis_settings
    baseline = _snapshot(runtime, model_id)
    state.token = uuid4().hex
    state.model_id = model_id
    state.main_transform = baseline.main_transform
    state.companion_transform = baseline.companion_transform
    state.baseline = baseline
    state.baseline_alignment_fingerprint = runtime.model_repository.alignment_transform_for_model_id(model_id).fingerprint
    state.geometry_identity = _geometry_identity(runtime, model_id)
    state.preview_enabled = True
    state.window_pointer = int(window_pointer)
    state.workspace_pointer = int(workspace_pointer)
    runtime.clear_data_layer_click()
    return state.token


def _validated_state(runtime: Any, token: str) -> AxisSettingsRuntimeState:
    """형상·축·연결된 품질 기록이 바뀐 편집 토큰의 확정을 차단합니다."""
    state = runtime.axis_settings
    if not state.is_active or state.token != str(token) or state.baseline is None:
        raise AxisSettingsError("축 설정 창이 만료되었습니다. 다시 열어 주세요.")
    current = _snapshot(runtime, state.model_id)
    baseline = state.baseline
    if (_geometry_identity(runtime, state.model_id) != state.geometry_identity
        or current.provenance != baseline.provenance
        or current.related_provenances != baseline.related_provenances
        or current.main_transform.fingerprint != baseline.main_transform.fingerprint
        or (current.companion_transform.fingerprint if current.companion_transform else None) != (baseline.companion_transform.fingerprint if baseline.companion_transform else None)
        or runtime.model_repository.alignment_transform_for_model_id(state.model_id).fingerprint != state.baseline_alignment_fingerprint):
        raise AxisSettingsError("편집 중 원본 형상·축 또는 정렬 기준이 바뀌었습니다. 취소 후 다시 열어 주세요.")
    return state


def update_axis_edit(runtime: Any, token: str, *, main_transform: ModelDisplayTransform,
                     companion_transform: ModelDisplayTransform | None = None, preview_enabled: bool = True) -> None:
    """유효한 두 형상 축을 임시 미리보기에만 원자적으로 반영합니다."""
    state = _validated_state(runtime, token)
    if not isinstance(main_transform, ModelDisplayTransform):
        raise AxisSettingsError("주 모델의 축 설정이 올바르지 않습니다.")
    if (state.baseline.companion_transform is None) != (companion_transform is None):
        raise AxisSettingsError("연결 메시 유무와 축 설정이 일치하지 않습니다.")
    if companion_transform is not None and not isinstance(companion_transform, ModelDisplayTransform):
        raise AxisSettingsError("연결 메시의 축 설정이 올바르지 않습니다.")
    state.main_transform = main_transform
    state.companion_transform = companion_transform
    state.preview_enabled = bool(preview_enabled)


def effective_axis_transform(runtime: Any, model_id: str, *, companion: bool = False) -> ModelDisplayTransform:
    """해당 형상의 확정 축 또는 같은 대상의 활성 임시 미리보기 축을 반환합니다."""
    repository = runtime.model_repository
    committed = (repository.companion_display_transform_for_model_id(model_id) if companion else repository.display_transform_for_model_id(model_id))
    state = getattr(runtime, "axis_settings", None)
    if state is not None and state.is_active and state.preview_enabled and state.model_id == model_id:
        candidate = state.companion_transform if companion else state.main_transform
        if isinstance(candidate, ModelDisplayTransform):
            return candidate
    return committed


def rc_axis_pair_for_model(runtime: Any, model_id: str) -> tuple[ModelDisplayTransform, ModelDisplayTransform | None]:
    """모델 종류와 연결 유무에 맞는 명시적 RC 초기 축 조합을 만듭니다."""
    repository = runtime.model_repository
    main_is_mesh = repository.geometry_kind_for_model_id(model_id) == "TRIANGLE_MESH"
    main = create_model_display_transform(RC_OBJ_AXIS_PRESET, "Y", "Z") if main_is_mesh else create_model_display_transform(DEFAULT_AXIS_PRESET, "Z", "-Y")
    companion = create_model_display_transform(RC_OBJ_AXIS_PRESET, "Y", "Z") if repository.companion_mesh_for_model_id(model_id) is not None else None
    return main, companion


def _apply_snapshot_atomic(runtime: Any, snapshot: AxisEditSnapshot) -> None:
    """형상별 축과 모든 종속 모델의 감사 기록을 원자적으로 갱신·복원합니다."""
    repository = runtime.model_repository
    before = _snapshot(runtime, snapshot.model_id)
    if (before.companion_transform is None) != (snapshot.companion_transform is None):
        raise AxisSettingsError("축 스냅샷의 연결 메시 구성이 현재 모델과 다릅니다.")
    record_ids = (snapshot.model_id, *(model_id for model_id, _record in snapshot.related_provenances))
    if any(model_id not in runtime.model_catalog.models for model_id in record_ids):
        raise AxisSettingsError("축 설정과 연결된 정렬 모델이 더 이상 존재하지 않습니다.")
    records_before = {model_id: runtime.model_catalog.models[model_id] for model_id in record_ids}
    try:
        repository.set_display_transform(snapshot.model_id, snapshot.main_transform)
        if snapshot.companion_transform is not None:
            repository.set_companion_display_transform(snapshot.model_id, snapshot.companion_transform)
        runtime.model_catalog.set_alignment_provenance(snapshot.model_id, snapshot.provenance)
        for model_id, provenance in snapshot.related_provenances:
            runtime.model_catalog.set_alignment_provenance(model_id, provenance)
    except Exception:
        repository.display_transforms[snapshot.model_id] = before.main_transform
        if before.companion_transform is not None:
            repository.companion_display_transforms[snapshot.model_id] = before.companion_transform
        runtime.model_catalog.models.update(records_before)
        raise


def _marker_source_is_referenced(runtime: Any, model_id: str, geometry_source: str) -> bool:
    """현재 마커와 정밀 정렬의 승인 기록이 지정한 형상 축에 의존하는지 확인합니다."""
    for pair in runtime.marker_selection.completed_pairs:
        for point in (pair.reference, pair.moving):
            if point.model_id == model_id and getattr(point, "geometry_source", MARKER_GEOMETRY_SOURCE_MAIN) == geometry_source:
                return True
    dependency = (model_id, geometry_source)
    return any(
        dependency in getattr(record.alignment_provenance, "geometry_dependencies", ())
        for record in runtime.model_catalog.models.values()
    )


def _invalidate_axis_derivatives(runtime: Any, model_id: str, main_changed: bool, companion_changed: bool) -> None:
    """계산에 사용한 축이 바뀔 때만 원본 마커를 보존하고 이전 파생 결과를 비웁니다."""
    marker_axis_changed = companion_changed and _marker_source_is_referenced(
        runtime, model_id, MARKER_GEOMETRY_SOURCE_COMPANION,
    )
    if main_changed or marker_axis_changed:
        runtime.marker_alignment.clear(error_message="축 설정 변경으로 정렬 기준이 갱신되었습니다.")
        icp = runtime.icp_alignment
        if not icp.is_running and not icp.has_preview and icp.job_handle is None:
            icp.clear(error_message="축 설정 변경으로 정렬 기준이 갱신되었습니다.")


def _invalidated_axis_provenance(runtime: Any, model_id: str, provenance: Any, fingerprint: str):
    """공통 정렬은 유지하면서 더 이상 유효하지 않은 정렬 품질 수치를 제거합니다."""
    from .alignment.types import AlignmentProvenanceRecord

    return AlignmentProvenanceRecord(
        model_id=model_id,
        method="MANUAL",
        input_fingerprint=fingerprint,
        result_fingerprint=runtime.model_repository.alignment_transform_for_model_id(model_id).fingerprint,
        marker_geometry_dependencies=getattr(provenance, "marker_geometry_dependencies", ()),
        solver_geometry_dependencies=getattr(provenance, "solver_geometry_dependencies", ()),
    )


def commit_axis_edit(runtime: Any, token: str) -> bool:
    """형상별 축과 종속 모델 품질을 단일 이력으로 확정하고 원본·공통 정렬을 유지합니다."""
    state = _validated_state(runtime, token)
    reason = _other_operation_block_reason(runtime)
    if reason is not None:
        raise AxisSettingsError(reason)
    before = state.baseline
    main_changed = before.main_transform.fingerprint != state.main_transform.fingerprint
    companion_changed = (before.companion_transform.fingerprint if before.companion_transform else None) != (state.companion_transform.fingerprint if state.companion_transform else None)
    if not main_changed and not companion_changed:
        state.clear()
        return False
    changed_sources = {
        source for source, changed in (
            (MARKER_GEOMETRY_SOURCE_MAIN, main_changed),
            (MARKER_GEOMETRY_SOURCE_COMPANION, companion_changed),
        ) if changed
    }
    changed_dependencies = {(state.model_id, source) for source in changed_sources}
    fingerprint = state.main_transform.fingerprint if main_changed else state.companion_transform.fingerprint
    provenance = before.provenance
    if main_changed or changed_dependencies.intersection(getattr(provenance, "geometry_dependencies", ())):
        provenance = _invalidated_axis_provenance(runtime, state.model_id, provenance, fingerprint)
    related = tuple(
        (
            model_id,
            _invalidated_axis_provenance(runtime, model_id, record, fingerprint)
            if changed_dependencies.intersection(getattr(record, "geometry_dependencies", ()))
            else record,
        )
        for model_id, record in before.related_provenances
    )
    after = AxisEditSnapshot(state.model_id, state.main_transform, state.companion_transform, provenance, related)
    _apply_snapshot_atomic(runtime, after)
    try:
        action = f"{runtime.model_catalog.models[state.model_id].display_name} 축 설정"
        if not runtime.undo_journal.record_committed(before, after, action, AXIS_SETTINGS_COMMAND):
            raise AxisSettingsError("축 설정 Undo 기록을 만들지 못했습니다.")
    except Exception:
        _apply_snapshot_atomic(runtime, before)
        raise
    _invalidate_axis_derivatives(runtime, state.model_id, main_changed, companion_changed)
    state.clear()
    return True


def cancel_axis_edit(runtime: Any, token: str | None = None) -> bool:
    """현재 팝업의 임시 값만 폐기하고 확정 축과 Undo는 유지합니다."""
    state = getattr(runtime, "axis_settings", None)
    if state is None or not state.is_active or (token is not None and str(token) != state.token):
        return False
    state.clear()
    return True


def restore_axis_edit_snapshot(runtime: Any, snapshot: AxisEditSnapshot) -> None:
    """세션 되돌리기·다시 실행에서 형상별 축과 종속 품질 기록을 함께 복원합니다."""
    if not isinstance(snapshot, AxisEditSnapshot):
        raise AxisSettingsError("복원할 축 설정 스냅샷이 올바르지 않습니다.")
    before = _snapshot(runtime, snapshot.model_id)
    _apply_snapshot_atomic(runtime, snapshot)
    _invalidate_axis_derivatives(
        runtime,
        snapshot.model_id,
        before.main_transform.fingerprint != snapshot.main_transform.fingerprint,
        (before.companion_transform.fingerprint if before.companion_transform else None)
        != (snapshot.companion_transform.fingerprint if snapshot.companion_transform else None),
    )
