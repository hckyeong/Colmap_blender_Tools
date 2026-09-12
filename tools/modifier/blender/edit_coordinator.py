"""편집 세션과 권위 Scene 동기화 경계를 조정합니다."""

from __future__ import annotations

import bpy
from bpy.app.handlers import persistent

from .camera_edit_session import force_end_camera_mode
from .point_edit_session import (
    _PENDING_RESTORES,
    _PROGRAMMATIC_ENDS,
    _SESSIONS,
    _restore_point_display_visibility,
    _session_matches_state,
    _set_point_display_edit_visibility,
    clear_point_edit_sessions,
    abandon_point_edit_sessions_for_global_undo,
    force_end_point_mode,
)
from .runtime_state import cached_runtime, ensure_runtime, loaded_for_current
from .scene_bridge import build_staging, cleanup_staging, commit_staging
from .selection import (
    PointMeshIntegrityError,
    SelectionSnapshot,
    clear_snapshot,
    collect_selected_point_ids_from_object_mesh,
    lock_managed_point_objects,
    managed_point_object,
    observing_images_for_points,
    set_snapshot,
)


def synchronize_scene(context, state, settings) -> tuple[str, ...]:
    """편집된 Reconstruction을 staging 방식으로 관리 Scene에 반영합니다."""
    staged = None
    try:
        force_end_point_mode(context, state.dataset_uuid)
        force_end_camera_mode(context, state.dataset_uuid)
        staged = build_staging(
            loaded_for_current(state),
            point_size=settings.point_size,
            forward_axis=settings.axis_forward,
            up_axis=settings.axis_up,
            camera_frustum_size=settings.camera_frustum_size,
        )
        warnings = commit_staging(context.scene, staged)
        staged = None
        from .axis_display import apply_dataset_transform_preview
        apply_dataset_transform_preview(context.scene, state.dataset_transform)
        force_end_point_mode(context, state.dataset_uuid)
        force_end_camera_mode(context, state.dataset_uuid)
        return warnings
    finally:
        try:
            if staged is not None:
                cleanup_staging(staged)
        finally:
            force_end_point_mode(context, state.dataset_uuid)
            force_end_camera_mode(context, state.dataset_uuid)


def restore_authoritative_point_scene(context, state, message: str) -> tuple[str, ...]:
    """손상된 표시 Mesh를 현재 권위 Reconstruction에서 다시 구성합니다."""
    settings = context.scene.colmap_import
    force_end_point_mode(context, state.dataset_uuid)
    warnings = synchronize_scene(context, state, settings)
    force_end_point_mode(context, state.dataset_uuid)
    clear_snapshot(state.dataset_uuid)
    settings.selected_point_count = 0
    settings.observing_camera_count = 0
    settings.camera_target_count = 0
    settings.point_selection_status = message
    return warnings


def _restore_pending_point_meshes():
    """depsgraph handler 밖에서 손상된 표시 Mesh를 권위 모델로 복원합니다."""
    pending_items = tuple(_PENDING_RESTORES.items())
    for scene_pointer, (session, message) in pending_items:
        scene = next(
            (item for item in bpy.data.scenes if item.as_pointer() == scene_pointer),
            None,
        )
        if scene is None:
            _PENDING_RESTORES.pop(scene_pointer, None)
            continue
        settings = getattr(scene, "colmap_import", None)
        try:
            state = cached_runtime(session.dataset_uuid)
            if state is None:
                state = ensure_runtime(scene)
            if state.dataset_uuid != session.dataset_uuid:
                raise ValueError("복원 대상 데이터셋이 현재 Runtime과 다릅니다.")
            with bpy.context.temp_override(
                scene=scene,
                view_layer=scene.view_layers[0],
            ):
                restore_authoritative_point_scene(bpy.context, state, message)
        except Exception as exc:
            if settings is not None:
                settings.point_selection_status = f"표시 Mesh 지연 복원 실패: {exc}"
        finally:
            _PENDING_RESTORES.pop(scene_pointer, None)
    return None


def _queue_authoritative_restore(
    scene,
    session,
    message: str,
) -> None:
    _PENDING_RESTORES[scene.as_pointer()] = (session, message)
    if not bpy.app.timers.is_registered(_restore_pending_point_meshes):
        bpy.app.timers.register(_restore_pending_point_meshes, first_interval=0.0)


@persistent
def normalize_external_mode_change(scene, depsgraph) -> None:
    """외부 모드 종료를 감지하고 종료 경계에서 선택과 Mesh를 한 번 검증합니다."""
    del depsgraph
    context = bpy.context
    if scene is None or context.scene is not scene:
        return
    session = _SESSIONS.get(scene.as_pointer())
    if session is None:
        return
    if scene.as_pointer() in _PROGRAMMATIC_ENDS:
        return
    active = context.active_object
    if (
        active is not None
        and active.as_pointer() == session.point_object_pointer
        and active.data is not None
        and active.data.as_pointer() == session.point_mesh_pointer
        and active.mode == "EDIT"
    ):
        return
    _SESSIONS.pop(scene.as_pointer(), None)
    settings = getattr(scene, "colmap_import", None)
    try:
        state = cached_runtime(session.dataset_uuid)
        if state is None or not _session_matches_state(session, state):
            raise ValueError(
                "외부 모드 종료 시 Runtime 또는 revision이 Edit Session과 다릅니다."
            )
        point_object = managed_point_object(context, state.dataset_uuid)
        if (
            point_object.as_pointer() != session.point_object_pointer
            or point_object.data.as_pointer() != session.point_mesh_pointer
        ):
            raise PointMeshIntegrityError(
                "외부 모드 종료 시 Point Object 또는 Mesh가 교체되었습니다."
            )
        selected = collect_selected_point_ids_from_object_mesh(
            context,
            state,
            allow_empty=True,
        )
        observing = (
            observing_images_for_points(state, selected)
            if selected
            else ()
        )
        if selected:
            set_snapshot(
                SelectionSnapshot(
                    dataset_uuid=state.dataset_uuid,
                    scene_pointer=context.scene.as_pointer(),
                    point_object_pointer=point_object.as_pointer(),
                    point_mesh_pointer=point_object.data.as_pointer(),
                    selected_point3d_ids=selected,
                    observing_image_ids=observing,
                    source_fingerprint=state.source.source_fingerprint,
                    edit_revision=state.edit_revision,
                    index_revision=state.index_revision,
                    history_hash=state.history_hash,
                )
            )
        else:
            clear_snapshot(state.dataset_uuid)
        _set_point_display_edit_visibility(point_object, True)
        lock_managed_point_objects(context, state.dataset_uuid)
        if settings is not None:
            settings.point_edit_mode_active = False
            settings.remove_target = "NONE"
            settings.selected_point_count = len(selected)
            settings.observing_camera_count = len(observing)
            settings.camera_target_count = 0
            settings.point_selection_status = (
                f"외부 모드 종료를 검증하고 Point 선택 {len(selected)}개를 보존했습니다."
            )
    except PointMeshIntegrityError as exc:
        _restore_point_display_visibility(context, session.dataset_uuid)
        lock_managed_point_objects(context, session.dataset_uuid)
        clear_snapshot(session.dataset_uuid)
        if settings is not None:
            settings.point_edit_mode_active = False
            settings.remove_target = "NONE"
            settings.selected_point_count = 0
            settings.observing_camera_count = 0
            settings.camera_target_count = 0
            settings.point_selection_status = (
                f"외부 모드 종료에서 표시 Mesh 변경을 감지했습니다: {exc}"
            )
        _queue_authoritative_restore(
            scene,
            session,
            f"표시 Mesh 변경을 거부하고 권위 모델로 복원했습니다: {exc}",
        )
    except Exception as exc:
        _restore_point_display_visibility(context, session.dataset_uuid)
        lock_managed_point_objects(context, session.dataset_uuid)
        clear_snapshot(session.dataset_uuid)
        if settings is not None:
            settings.point_edit_mode_active = False
            settings.remove_target = "NONE"
            settings.selected_point_count = 0
            settings.observing_camera_count = 0
            settings.camera_target_count = 0
            settings.point_selection_status = f"외부 모드 종료 검증 실패: {exc}"


def clear_edit_sessions(context=None) -> None:
    """Point 세션과 지연 복원 작업을 함께 폐기합니다."""
    clear_point_edit_sessions(context)
    _PENDING_RESTORES.clear()
    if bpy.app.timers.is_registered(_restore_pending_point_meshes):
        bpy.app.timers.unregister(_restore_pending_point_meshes)


def abandon_edit_sessions_for_global_undo() -> None:
    """Undo 전 Blender ID를 건드리지 않고 모든 편집 callback을 폐기합니다."""

    from .camera_edit_session import (
        abandon_camera_edit_sessions_for_global_undo,
    )

    abandon_point_edit_sessions_for_global_undo()
    abandon_camera_edit_sessions_for_global_undo()
    _PENDING_RESTORES.clear()
    if bpy.app.timers.is_registered(_restore_pending_point_meshes):
        bpy.app.timers.unregister(_restore_pending_point_meshes)


def register_edit_mode_handler() -> None:
    if normalize_external_mode_change not in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.append(normalize_external_mode_change)


def unregister_edit_mode_handler() -> None:
    if normalize_external_mode_change in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.remove(normalize_external_mode_change)
    _PENDING_RESTORES.clear()
    if bpy.app.timers.is_registered(_restore_pending_point_meshes):
        bpy.app.timers.unregister(_restore_pending_point_meshes)
