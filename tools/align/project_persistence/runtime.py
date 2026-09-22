"""작업 상태 snapshot 저장과 기존 ImportJob 기반 자동 복원을 조정합니다."""

from __future__ import annotations

from dataclasses import replace
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
    MARKER_GEOMETRY_SOURCE_COMPANION,
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
from ..display_axes import (
    ModelDisplayTransform,
    companion_default_display_transform,
    create_model_display_transform,
)
from ..importer import IMPORT_STAGE_COMPLETED, ImportJobError, create_import_job
from ..mesh_importer import (
    MESH_IMPORT_STAGE_COMPLETED,
    MeshImportJobError,
    create_mesh_import_job,
)
from ..mesh_io.embedded_obj_store import (
    EmbeddedObjStoreError,
    build_triangle_proxy_from_embedded_mesh,
    find_embedded_obj_mesh,
    remove_all_embedded_obj_meshes,
    remove_embedded_obj_mesh,
    retire_embedded_obj_mesh,
    validate_embedded_obj_project_cleanup,
    validate_embedded_obj_mesh,
    validate_embedded_obj_identity,
)
from ..model_repository import CompanionMeshSourceMetadata, ImportedModelSourceMetadata
from ..viewport.display_sampling import build_display_indices
from ..geometry_access import (
    source_position_for_registration_id,
    validated_mesh_surface_position,
)
from ..logging_utils import get_logger
from ..marker_geometry import resolve_marker_geometry
from ..state import (
    MarkerPair,
    MarkerSelectionSnapshot,
    ModelCatalogRecord,
    ModelCatalogSnapshot,
    RUNTIME,
    RuntimeState,
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
    ProjectAxesSnapshot,
    ProjectCatalogSnapshot,
    ProjectCompanionSourceSnapshot,
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
    """현재 저장된 `.blend` 경로 기준의 상대 원본 경로를 가능할 때만 계산합니다."""

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
        selected_root_relative=_relative_sparse_path(metadata.selected_root_absolute),
        sparse_path_absolute=metadata.sparse_path_absolute,
        sparse_path_relative=_relative_sparse_path(metadata.sparse_path_absolute),
        source_format=metadata.source_format,
        source_fingerprint="" if is_mesh else metadata.source_fingerprint,
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


def _validated_embedded_identity(model_data: Any, model_id: str) -> tuple[str, int, str, int]:
    """프록시 UUID로 실제 내장 Mesh를 재해결해 저장할 식별자를 반환합니다."""

    asset_uuid = str(getattr(model_data, "embedded_asset_uuid", "")).strip()
    if not asset_uuid:
        raise ProjectPersistenceRuntimeError("OBJ 프록시에 내장 Mesh UUID가 없습니다.")
    revision = int(getattr(model_data, "geometry_revision", 0))
    try:
        mesh = find_embedded_obj_mesh(asset_uuid)
        validate_embedded_obj_mesh(
            mesh,
            expected_asset_uuid=asset_uuid,
            expected_model_id=model_id,
            expected_revision=revision,
        )
    except EmbeddedObjStoreError as error:
        raise ProjectPersistenceRuntimeError(str(error)) from error
    schema_version = int(mesh.get("schema_version", 0))
    if schema_version != int(getattr(model_data, "embedded_asset_schema_version", 0)):
        raise ProjectPersistenceRuntimeError("OBJ 프록시와 내장 Mesh schema가 다릅니다.")
    return asset_uuid, schema_version, str(mesh.name), revision


def _project_companion_snapshot(runtime: Any, model_id: str) -> ProjectCompanionSourceSnapshot | None:
    """연결 메시의 출처와 별도 내장 Mesh 식별자를 검증해 저장합니다."""

    repository = runtime.model_repository
    mesh = repository.companion_mesh_for_model_id(model_id)
    metadata = repository.companion_sources.get(model_id)
    if (mesh is None) != (metadata is None):
        raise ProjectPersistenceRuntimeError("연결 OBJ 형상과 출처 메타데이터가 일치하지 않습니다.")
    if metadata is None:
        return None
    if mesh.model_id != model_id:
        raise ProjectPersistenceRuntimeError("연결 OBJ의 소유 모델 ID가 일치하지 않습니다.")
    asset_uuid, schema_version, data_name_hint, revision = _validated_embedded_identity(
        mesh,
        model_id,
    )
    return ProjectCompanionSourceSnapshot(
        path_absolute=metadata.source_path_absolute,
        path_relative=_relative_sparse_path(metadata.source_path_absolute),
        source_fingerprint="",
        geometry_only=metadata.geometry_only,
        embedded_obj_asset_uuid=asset_uuid,
        embedded_obj_schema_version=schema_version,
        embedded_obj_data_name_hint=data_name_hint,
        embedded_obj_geometry_revision=revision,
    )


def _display_transform_from_axes(axes: ProjectAxesSnapshot) -> ModelDisplayTransform:
    """저장된 축·이동·회전·배율·중심으로 행렬을 재생성하고 최종 지문을 검증합니다."""

    try:
        transform = create_model_display_transform(
            axes.preset_id, axes.axis_forward, axes.axis_up,
            rotation_xyz_degrees=axes.rotation_xyz_degrees,
            translation_xyz=axes.translation_xyz,
            scale_xyz=axes.scale_xyz,
            pivot_source=axes.pivot_source,
        )
    except (TypeError, ValueError) as error:
        raise ProjectPersistenceRuntimeError(f"저장된 현재 축 설정이 올바르지 않습니다: {error}") from error
    if transform.fingerprint != axes.display_transform_fingerprint:
        raise ProjectPersistenceRuntimeError("저장된 현재 축 설정의 변환 지문이 재생성한 행렬과 다릅니다.")
    return transform


def _project_axes_snapshot(transform: ModelDisplayTransform) -> ProjectAxesSnapshot:
    """원본과 미리보기 상태를 읽지 않고 확정된 중심 기준 변환만 검증해 저장합니다."""

    axes = ProjectAxesSnapshot(
        preset_id=transform.preset_id,
        axis_forward=transform.forward_axis,
        axis_up=transform.up_axis,
        rotation_xyz_degrees=tuple(transform.rotation_xyz_degrees),
        display_transform_fingerprint=transform.fingerprint,
        translation_xyz=transform.translation_xyz,
        scale_xyz=transform.scale_xyz,
        pivot_source=transform.pivot_source,
    )
    restored = _display_transform_from_axes(axes)
    if not np.array_equal(restored.matrix_float64, transform.matrix_float64):
        raise ProjectPersistenceRuntimeError("현재 축 설정으로 저장할 표시 행렬을 재현할 수 없습니다.")
    return axes


def _project_model_snapshot(runtime: Any, record: ModelCatalogRecord) -> ProjectModelSnapshot:
    """확정 레코드의 원본 출처·현재 형상별 축·정렬 변환을 분리해 저장합니다."""

    repository = runtime.model_repository
    runtime_model = repository.models[record.model_id]
    alignment = repository.alignment_transform_for_model_id(record.model_id)
    embedded_identity = (
        _validated_embedded_identity(runtime_model, record.model_id)
        if str(getattr(runtime_model, "geometry_kind", "")).strip().upper()
        == GEOMETRY_KIND_TRIANGLE_MESH
        else ("", 0, "", 0)
    )
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
            show_points=record.show_points,
            show_surface=record.show_surface,
        ),
        alignment_matrix_float64=_matrix_rows(alignment.matrix_float64),
        companion_source=_project_companion_snapshot(runtime, record.model_id),
        current_axes=_project_axes_snapshot(repository.display_transform_for_model_id(record.model_id)),
        companion_axes=(
            _project_axes_snapshot(repository.companion_display_transform_for_model_id(record.model_id))
            if repository.companion_mesh_for_model_id(record.model_id) is not None else None
        ),
        embedded_obj_asset_uuid=embedded_identity[0],
        embedded_obj_schema_version=embedded_identity[1],
        embedded_obj_data_name_hint=embedded_identity[2],
        embedded_obj_geometry_revision=embedded_identity[3],
    )


def _project_marker_snapshot(runtime: Any) -> ProjectMarkerSnapshot:
    """입력 중 상태를 제외한 완료 마커의 형상 출처·원본 요소·원본 좌표만 저장합니다."""

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
                geometry_source=pair.reference.geometry_source,
            ),
            moving=ProjectMarkerPointSnapshot(
                model_role=ROLE_MOVING,
                model_id=pair.moving.model_id,
                point3d_id=pair.moving.point3d_id,
                point_kind=pair.moving.point_kind,
                source_point_id=pair.moving.source_point_id,
                local_position=pair.moving.local_position,
                geometry_source=pair.moving.geometry_source,
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
            "obj 원본 파일",
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
    """파일 교체 전에 복원 타이머와 임시 상태를 폐기하고 세대 번호를 갱신합니다."""

    _clear_restore_timer(runtime, unregister=True)
    _rollback_restore_runtime(runtime)
    runtime.project_restore.clear()


def _rollback_restore_runtime(runtime: Any) -> None:
    """복원 임시 상태와 이번 복원에서 생성한 내장 Mesh만 함께 폐기합니다."""

    staging = runtime.project_restore.staging_runtime
    if staging is not None:
        job = staging.import_job
        if job is not None:
            job.request_cancel()
            job.step(staging)
        if runtime.import_job is job:
            runtime.clear_import_job()
    created_assets = tuple(runtime.project_restore.created_embedded_assets.items())
    for asset_uuid, model_id in created_assets:
        try:
            remove_embedded_obj_mesh(asset_uuid, expected_model_id=model_id)
        except Exception as error:
            retire_embedded_obj_mesh(asset_uuid, expected_model_id=model_id)
            _LOGGER.error(
                "event=restore_embedded_obj_remove_failed model_id=%s reason=%s",
                model_id,
                error,
            )
    runtime.project_restore.created_embedded_assets.clear()
    if staging is None:
        return
    staging.model_catalog.clear()
    staging.model_repository.clear()
    staging.marker_selection.clear()
    staging.point_clouds.clear()
    staging.gpu_batches.clear()
    staging.clear_import_job()
    runtime.project_restore.staging_runtime = None


def _track_restore_created_embedded_assets(
    runtime: Any,
    model: ProjectModelSnapshot,
) -> None:
    """구버전 원본 재가져오기로 이번 복원에서 새로 확정한 Mesh UUID만 기록합니다."""

    state = runtime.project_restore
    if _source_is_mesh(model) and not model.has_embedded_obj:
        restored = runtime.model_repository.models.get(model.model_id)
        asset_uuid = str(getattr(restored, "embedded_asset_uuid", "")).strip()
        if asset_uuid:
            state.created_embedded_assets[asset_uuid] = model.model_id
    if model.companion_source is not None and not model.has_embedded_companion_obj:
        companion = runtime.model_repository.companion_mesh_for_model_id(model.model_id)
        asset_uuid = str(getattr(companion, "embedded_asset_uuid", "")).strip()
        if asset_uuid:
            state.created_embedded_assets[asset_uuid] = model.model_id


def _new_restore_staging(runtime: Any) -> None:
    """공개 상태와 분리한 복원 전용 Runtime을 현재 복원 세대에 연결합니다."""

    _rollback_restore_runtime(runtime)
    staging = RuntimeState()
    staging.project_restore = runtime.project_restore
    runtime.project_restore.staging_runtime = staging


def _prepare_restored_gpu_state(runtime: Any) -> None:
    """화면이 있는 전역 Runtime의 확정 경계에서만 새 GPU 자원을 준비합니다."""

    if runtime is RUNTIME and not bpy.app.background:
        from ..viewport.renderer import prepare_restored_gpu_batches

        prepare_restored_gpu_batches(runtime)


def _commit_restore_staging(runtime: Any, staging: Any) -> None:
    """검증된 모델과 GPU를 함께 교체하고 준비 실패 시 기존 정상 상태를 되돌립니다."""

    field_names = (
        "model_repository", "model_catalog", "marker_selection", "marker_alignment",
        "layer_transform", "axis_settings", "icp_alignment", "merge_export", "obj_export", "undo_journal",
        "point_clouds", "gpu_batches",
    )
    previous = {name: getattr(runtime, name) for name in field_names}
    try:
        for field_name in field_names:
            setattr(runtime, field_name, getattr(staging, field_name))
        _prepare_restored_gpu_state(runtime)
    except Exception:
        for field_name, value in previous.items():
            setattr(runtime, field_name, value)
        raise
    runtime.clear_import_job()
    runtime.clear_data_layer_click()
    runtime.project_restore.staging_runtime = None


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


def _restore_error_solution(error: Exception | str) -> str:
    """외부 원본 오류와 내장 Data Block 손상에 맞는 복원 해결 문구를 반환합니다."""

    if _restore_error_needs_relink(error):
        return "같은 COLMAP 또는 구버전 OBJ 원본을 다시 지정한 뒤 재시도하십시오."
    return "내장 OBJ Data Block과 프로젝트 상태를 확인하거나 새 프로젝트를 시작하십시오."


def _embedded_proxy(
    *,
    asset_uuid: str,
    schema_version: int,
    model_id: str,
    display_name: str,
    source_path: str,
    geometry_revision: int,
    display_limit: int,
    geometry_only: bool,
) -> Any:
    """저장 식별자와 형상을 프록시 생성 시 한 번 검증하고 결정적 표시 표본을 복원합니다."""

    mesh = find_embedded_obj_mesh(asset_uuid)
    if int(schema_version) != int(mesh.get("schema_version", 0)):
        raise ProjectPersistenceRuntimeError("내장 OBJ schema가 저장 상태와 다릅니다.")
    proxy = build_triangle_proxy_from_embedded_mesh(
        mesh,
        model_id=model_id,
        display_name=display_name,
        source_path=source_path,
        geometry_only=geometry_only,
        expected_asset_uuid=asset_uuid,
        expected_revision=geometry_revision,
    )
    offsets = build_display_indices(
        proxy.registration_vertex_indices.astype(np.uint64, copy=False),
        max_points=display_limit,
    )
    return replace(
        proxy,
        display_vertex_indices=proxy.registration_vertex_indices[offsets],
    )


def _restore_embedded_main(runtime: Any, model: ProjectModelSnapshot) -> None:
    """독립 OBJ의 내장 원본에서 Repository와 Catalog를 외부 파일 접근 없이 복원합니다."""

    display = create_model_display_transform(
        model.source.axis_preset,
        model.source.axis_forward,
        model.source.axis_up,
    )
    if display.fingerprint != model.source.display_transform_fingerprint:
        raise ProjectPersistenceRuntimeError("저장된 Display Axis fingerprint가 현재 구현과 다릅니다.")
    proxy = _embedded_proxy(
        asset_uuid=model.embedded_obj_asset_uuid,
        schema_version=model.embedded_obj_schema_version,
        model_id=model.model_id,
        display_name=model.display_name,
        source_path=model.source.path_absolute,
        geometry_revision=model.embedded_obj_geometry_revision,
        display_limit=model.source.display_vertex_limit,
        geometry_only=False,
    )
    metadata = ImportedModelSourceMetadata(
        selected_root_absolute=model.source.selected_root_absolute,
        sparse_path_absolute=model.source.path_absolute,
        source_format="obj",
        source_fingerprint="",
        display_point_limit=model.source.display_vertex_limit,
        axis_preset=display.preset_id,
        axis_forward=display.forward_axis,
        axis_up=display.up_axis,
        display_transform_fingerprint=display.fingerprint,
        geometry_kind=GEOMETRY_KIND_TRIANGLE_MESH,
    )
    runtime.model_repository.add_model(proxy, display, metadata)
    runtime.model_catalog.add_imported_model(
        model.model_id,
        model.display_name,
        select_after_import=False,
        geometry_kind=GEOMETRY_KIND_TRIANGLE_MESH,
    )


def _restore_embedded_companion(runtime: Any, model: ProjectModelSnapshot) -> None:
    """COLMAP 소유 연결 OBJ를 별도 내장 UUID에서 형상 전용 프록시로 복원합니다."""

    source = model.companion_source
    if source is None or not source.embedded_obj_asset_uuid:
        return
    proxy = _embedded_proxy(
        asset_uuid=source.embedded_obj_asset_uuid,
        schema_version=source.embedded_obj_schema_version,
        model_id=model.model_id,
        display_name=model.display_name,
        source_path=source.path_absolute,
        geometry_revision=source.embedded_obj_geometry_revision,
        display_limit=model.source.display_vertex_limit,
        geometry_only=True,
    )
    metadata = CompanionMeshSourceMetadata(
        source_path_absolute=source.path_absolute,
        source_fingerprint="",
        geometry_only=True,
    )
    runtime.model_repository.add_companion_mesh(
        model.model_id,
        proxy,
        metadata,
        display_transform=companion_default_display_transform(
            runtime.model_repository.display_transform_for_model_id(model.model_id)
        ),
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


def _restore_dataset_root(runtime: Any, model: ProjectModelSnapshot, source_path: str) -> str:
    """실제 복원 sparse 위치와 저장된 상대 구조로 동일 데이터셋 루트를 구합니다."""

    if _source_is_mesh(model):
        return source_path
    from ..colmap_io import discover_sparse_model

    actual_sparse = discover_sparse_model(
        source_path, input_format=_source_input_format(model.source.source_format),
    ).source_path.resolve(strict=False)
    saved_root = Path(os.path.abspath(model.source.selected_root_absolute))
    saved_sparse = Path(os.path.abspath(model.source.sparse_path_absolute))
    try:
        sparse_suffix = saved_sparse.relative_to(saved_root)
    except ValueError as error:
        raise ProjectPersistenceRuntimeError("저장된 COLMAP 원본이 데이터셋 루트에 속하지 않습니다.") from error
    candidate = actual_sparse
    for _part in sparse_suffix.parts:
        candidate = candidate.parent
    if (candidate / sparse_suffix).resolve(strict=False) != actual_sparse or not candidate.is_dir():
        raise ProjectPersistenceRuntimeError("복원 COLMAP 폴더와 저장 데이터셋 구조가 일치하지 않습니다.")
    return str(candidate)


def _resolve_restore_companion(model: ProjectModelSnapshot, dataset_root: str) -> str:
    """같은 데이터셋의 저장된 연결 OBJ 한 파일만 경로·SHA-256으로 확인합니다."""

    source = model.companion_source
    if source is None:
        return ""
    from ..companion_discovery import resolve_companion_obj_path

    saved_path = Path(os.path.abspath(source.path_absolute))
    saved_root = Path(os.path.abspath(model.source.selected_root_absolute))
    if saved_path.parent != saved_root:
        raise ProjectPersistenceRuntimeError("저장된 연결 OBJ는 데이터셋 루트 바로 아래에 있어야 합니다.")
    root = Path(dataset_root).resolve(strict=False)
    try:
        candidate = resolve_companion_obj_path(root, root / saved_path.name)
    except (OSError, ValueError) as error:
        raise ProjectPersistenceRuntimeError("저장된 연결 OBJ 원본 파일이 없거나 안전한 루트 직하 파일이 아닙니다.") from error
    if candidate is None:
        raise ProjectPersistenceRuntimeError("저장된 연결 OBJ 원본 파일을 찾을 수 없습니다.")
    if _sha256_file_for_restore(candidate) != source.source_fingerprint:
        raise ProjectPersistenceRuntimeError("저장된 연결 OBJ source fingerprint와 현재 원본이 다릅니다.")
    return str(candidate)


def _preflight_restore_sources(runtime: Any, snapshot: ProjectStateSnapshot) -> None:
    """외부 출처와 내장 식별자를 먼저 검사하며 내장 형상은 복원 확정 전에 검사합니다."""

    for model in sorted(snapshot.models, key=lambda item: item.load_order):
        try:
            if _source_is_mesh(model) and model.has_embedded_obj:
                mesh = find_embedded_obj_mesh(model.embedded_obj_asset_uuid)
                validate_embedded_obj_identity(
                    mesh,
                    expected_asset_uuid=model.embedded_obj_asset_uuid,
                    expected_model_id=model.model_id,
                    expected_revision=model.embedded_obj_geometry_revision,
                )
                if int(mesh.get("schema_version", 0)) != model.embedded_obj_schema_version:
                    raise ProjectPersistenceRuntimeError("내장 OBJ schema가 저장 상태와 다릅니다.")
                continue
            source_path = _resolve_restore_source(runtime, model)
            dataset_root = _restore_dataset_root(runtime, model, source_path)
            if model.has_embedded_companion_obj:
                source = model.companion_source
                mesh = find_embedded_obj_mesh(source.embedded_obj_asset_uuid)
                validate_embedded_obj_identity(
                    mesh,
                    expected_asset_uuid=source.embedded_obj_asset_uuid,
                    expected_model_id=model.model_id,
                    expected_revision=source.embedded_obj_geometry_revision,
                )
                if int(mesh.get("schema_version", 0)) != source.embedded_obj_schema_version:
                    raise ProjectPersistenceRuntimeError("연결 내장 OBJ schema가 저장 상태와 다릅니다.")
            else:
                _resolve_restore_companion(model, dataset_root)
        except (OSError, ProjectPersistenceRuntimeError, EmbeddedObjStoreError) as error:
            raise _ProjectRestoreSourceError(model.model_id, str(error)) from error


def _start_restore_import(runtime: Any, model: ProjectModelSnapshot) -> bool:
    """내장 OBJ는 즉시 복원하고 나머지 출처는 기존 ImportJob을 시작합니다."""

    if _source_is_mesh(model) and model.has_embedded_obj:
        _restore_embedded_main(runtime, model)
        return True
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
        selected_root = _restore_dataset_root(runtime, model, source_path)
        companion_path = (
            "" if model.has_embedded_companion_obj
            else _resolve_restore_companion(model, selected_root)
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
                prepare_gpu=False,
            )
        else:
            from ..colmap_io import discover_sparse_model

            source_path = str(discover_sparse_model(
                source_path, input_format=_source_input_format(model.source.source_format),
            ).source_path)
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
                prepare_gpu=False,
                import_companion_obj=(
                    model.companion_source is not None
                    and not model.has_embedded_companion_obj
                ),
                companion_obj_path=companion_path,
                companion_expected_fingerprint=(
                    model.companion_source.source_fingerprint if model.companion_source is not None else ""
                ),
            )
    except (ImportJobError, MeshImportJobError, ValueError) as error:
        raise ProjectPersistenceRuntimeError(str(error)) from error
    runtime.import_job = job
    return False


def _validate_restored_import(runtime: Any, job: Any, model: ProjectModelSnapshot) -> None:
    """현재 축 적용 전에 최초 가져오기 축·출처와 연결 OBJ의 원본 지문을 확인합니다."""

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
    original_fields = (
        "axis_preset", "axis_forward", "axis_up", "display_transform_fingerprint",
        "source_format", "display_point_limit", "geometry_kind",
    )
    if any(getattr(metadata, field) != getattr(model.source, field) for field in original_fields):
        raise ProjectPersistenceRuntimeError("복원 ImportJob의 최초 가져오기 축 또는 원본 메타데이터가 저장 상태와 다릅니다.")
    companion = runtime.model_repository.companion_mesh_for_model_id(model.model_id)
    if model.companion_source is None:
        if companion is not None:
            raise ProjectPersistenceRuntimeError("저장되지 않은 연결 OBJ가 자동으로 추가되었습니다.")
    else:
        metadata = runtime.model_repository.companion_sources.get(model.model_id)
        if companion is None or metadata is None:
            raise ProjectPersistenceRuntimeError("복원 연결 OBJ가 저장 상태와 다릅니다.")
        if model.has_embedded_companion_obj:
            source = model.companion_source
            if (
                companion.embedded_asset_uuid != source.embedded_obj_asset_uuid
                or companion.embedded_asset_schema_version != source.embedded_obj_schema_version
                or companion.geometry_revision != source.embedded_obj_geometry_revision
                or metadata.source_fingerprint
            ):
                raise ProjectPersistenceRuntimeError("복원 연결 내장 OBJ 식별자가 저장 상태와 다릅니다.")
        elif not companion.embedded_asset_uuid or metadata.source_fingerprint:
            raise ProjectPersistenceRuntimeError("구버전 연결 OBJ를 내장 형식으로 전환하지 못했습니다.")


def _marker_point_lookup_key(point: ProjectMarkerPointSnapshot) -> tuple[object, ...]:
    """소유 모델의 주형상·연결 메시를 구분하는 원본 마커 조회 키를 만듭니다."""

    return (
        point.model_id,
        point.geometry_source,
        point.point_kind,
        point.source_point_id,
        point.local_position,
    )


def _marker_position_lookup(
    runtime: Any,
    snapshot: ProjectMarkerSnapshot,
) -> dict[tuple[object, ...], tuple[float, float, float]]:
    """마커 출처의 실제 원본 점·정점·삼각면을 검증해 원본 로컬 좌표를 복원합니다."""

    requested_points = {
        _marker_point_lookup_key(point): point
        for pair in snapshot.completed_pairs
        for point in (pair.reference, pair.moving)
    }
    lookup: dict[tuple[object, ...], tuple[float, float, float]] = {}
    maximum_point_id = int(np.iinfo(np.uint64).max)
    for key, point in requested_points.items():
        point_kind = point.point_kind
        point_id = point.source_point_id
        local_position = point.local_position
        if point_id < 0 or point_id > maximum_point_id:
            raise ProjectPersistenceRuntimeError("마커 원본 요소 ID가 uint64 범위를 벗어났습니다.")
        try:
            geometry = resolve_marker_geometry(runtime.model_repository, point.model_id, point.geometry_source)
            if geometry.geometry_source == MARKER_GEOMETRY_SOURCE_COMPANION and point_kind != MARKER_POINT_KIND_MESH_SURFACE:
                raise ValueError("연결 메시 마커는 삼각면 위치만 참조할 수 있습니다.")
            if point_kind == MARKER_POINT_KIND_MESH_SURFACE:
                if local_position is None:
                    raise ValueError("면 마커 원본 로컬 좌표가 비어 있습니다.")
                position = validated_mesh_surface_position(geometry.model_data, point_id, local_position)
            else:
                position = source_position_for_registration_id(geometry.model_data, point_kind, point_id)
                if local_position is not None and not np.allclose(
                    position,
                    np.asarray(local_position, dtype=np.float64),
                    rtol=0.0,
                    atol=1.0e-9,
                ):
                    raise ValueError("저장된 마커 좌표가 원본 점과 다릅니다.")
        except (KeyError, TypeError, ValueError) as error:
            if point_kind == MARKER_POINT_KIND_MESH_SURFACE:
                label = "연결 메시 삼각면 위치" if point.geometry_source == MARKER_GEOMETRY_SOURCE_COMPANION else "OBJ 삼각면 위치"
            elif point_kind == MARKER_POINT_KIND_MESH_VERTEX:
                label = "OBJ 원본 정점 ID"
            else:
                label = "COLMAP POINT3D_ID"
            raise ProjectPersistenceRuntimeError(f"마커 원본 {label}를 찾을 수 없습니다: {point_id}") from error
        if position.shape != (3,) or not np.isfinite(position).all():
            raise ProjectPersistenceRuntimeError("마커 원본 좌표가 유한하지 않습니다.")
        lookup[key] = tuple(float(value) for value in position)
    return lookup


def _restored_marker_snapshot(runtime: Any, snapshot: ProjectMarkerSnapshot) -> MarkerSelectionSnapshot:
    """원본 출처·요소·좌표를 검증한 완료 마커를 충돌 목록까지 유지해 복원합니다."""

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
                    local_position=position_lookup[_marker_point_lookup_key(pair.reference)],
                    geometry_source=pair.reference.geometry_source,
                ),
                moving=SelectedPoint(
                    model_role=ROLE_MOVING,
                    model_id=pair.moving.model_id,
                    point3d_id=pair.moving.point3d_id,
                    point_kind=pair.moving.point_kind,
                    source_point_id=pair.moving.source_point_id,
                    local_position=position_lookup[_marker_point_lookup_key(pair.moving)],
                    geometry_source=pair.moving.geometry_source,
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
    """원본 검증이 끝난 임시 상태에 형상별 현재 축·정렬·마커를 함께 적용합니다."""

    ordered_models = tuple(sorted(snapshot.models, key=lambda item: item.load_order))
    if tuple(runtime.model_repository.models) != tuple(model.model_id for model in ordered_models):
        raise ProjectPersistenceRuntimeError("복원 모델 순서 또는 모델 ID가 저장 상태와 다릅니다.")
    transforms = {
        model.model_id: Sim3Transform(matrix_float64=model.alignment_matrix_float64)
        for model in ordered_models
    }
    current_axes = {}
    companion_axes = {}
    for model in ordered_models:
        if model.current_axes is None:
            raise ProjectPersistenceRuntimeError("복원할 주모델의 현재 축 설정이 없습니다.")
        current_axes[model.model_id] = _display_transform_from_axes(model.current_axes)
        if model.companion_source is not None:
            if model.companion_axes is None:
                raise ProjectPersistenceRuntimeError("복원할 연결 메시의 현재 축 설정이 없습니다.")
            companion_axes[model.model_id] = _display_transform_from_axes(model.companion_axes)
        elif model.companion_axes is not None:
            raise ProjectPersistenceRuntimeError("연결 OBJ 없이 연결 메시 축을 복원할 수 없습니다.")
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
                show_points=model.catalog.show_points,
                show_surface=model.catalog.show_surface,
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
    for model_id, transform in current_axes.items():
        runtime.model_repository.set_display_transform(model_id, transform)
    for model_id, transform in companion_axes.items():
        runtime.model_repository.set_companion_display_transform(model_id, transform)
    for model_id, transform in transforms.items():
        runtime.model_repository.set_alignment_transform(model_id, transform)
    runtime.model_catalog.restore_snapshot(catalog_snapshot)
    runtime.marker_selection.restore_snapshot(marker_snapshot)
    runtime.marker_alignment.clear()
    runtime.layer_transform.clear(message="프로젝트 복원으로 레이어 변환 미리보기를 초기화했습니다.")
    runtime.axis_settings.clear()
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
    """같은 세대의 가져오기를 임시 상태에서 진행하고 전부 검증한 결과만 교체합니다."""

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
    staging = state.staging_runtime
    if staging is None:
        _set_restore_failure(
            runtime,
            _restore_issue("복원 임시 상태가 없습니다.", "현재 작업 상태를 유지했습니다.", "복원을 다시 시도하십시오."),
            needs_relink=False,
        )
        _clear_restore_timer(runtime, unregister=False, callback=callback)
        return None
    ordered_models = tuple(sorted(snapshot.models, key=lambda item: item.load_order))
    if state.current_model_index >= len(ordered_models):
        try:
            _apply_restored_snapshot(staging, snapshot)
            _commit_restore_staging(runtime, staging)
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
        migrated = bool(state.created_embedded_assets)
        state.created_embedded_assets.clear()
        state.phase = PROJECT_RESTORE_PHASE_IDLE
        state.snapshot = None
        state.current_model_index = 0
        state.issues.clear()
        state.last_error = ""
        state.last_message = "저장된 COLMAP Align 작업 상태를 복원했습니다."
        state.relink_paths.clear()
        runtime.project_persistence_dirty = relinked or migrated
        if relinked or migrated:
            reason = "원본 다시 연결 복원 완료" if relinked else "OBJ 내장 형식 전환 완료"
            notify_project_state_changed(reason, runtime=runtime)
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
    job = staging.import_job
    try:
        if job is None:
            completed_immediately = _start_restore_import(staging, model)
            runtime.import_job = staging.import_job
            if completed_immediately:
                state.current_model_index += 1
                request_redraw()
            return _RESTORE_TIMER_INTERVAL_SECONDS
        result = job.step(staging)
        if result == "RUNNING":
            return _RESTORE_TIMER_INTERVAL_SECONDS
        if job.stage == IMPORT_STAGE_COMPLETED:
            _track_restore_created_embedded_assets(staging, model)
            if model.has_embedded_companion_obj:
                _restore_embedded_companion(staging, model)
            _validate_restored_import(staging, job, model)
            staging.clear_import_job()
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
        needs_relink = _restore_error_needs_relink(error)
        _set_restore_failure(
            runtime,
            _restore_issue(
                str(error),
                "저장된 점군과 정렬 작업을 복원하지 않았습니다.",
                _restore_error_solution(error),
                model_id=getattr(error, "model_id", ""),
            ),
            needs_relink=needs_relink,
        )
        return False
    _new_restore_staging(runtime)
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
        needs_relink = _restore_error_needs_relink(error)
        _set_restore_failure(
            runtime,
            _restore_issue(
                str(error),
                "저장된 점군과 정렬 작업을 복원하지 않았습니다.",
                _restore_error_solution(error),
                model_id=getattr(error, "model_id", ""),
            ),
            needs_relink=needs_relink,
        )
        return False
    _new_restore_staging(runtime)
    _schedule_restore_timer(runtime)
    request_redraw()
    return True


def cancel_project_restore(runtime: Any = RUNTIME) -> bool:
    """복원 임시 상태를 취소하고 기존 정상 Runtime과 저장 Text를 유지합니다."""

    state = runtime.project_restore
    if state.phase not in {PROJECT_RESTORE_PHASE_RESTORING, PROJECT_RESTORE_PHASE_NEEDS_RELINK}:
        return False
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
    validate_embedded_obj_project_cleanup()
    try:
        retired = retire_active_project_state()
    except ProjectTextStoreError as error:
        raise ProjectPersistenceRuntimeError(str(error)) from error
    _clear_restore_timer(runtime, unregister=True)
    _rollback_restore_runtime(runtime)
    remove_all_embedded_obj_meshes()
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
