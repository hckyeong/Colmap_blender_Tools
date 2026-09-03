"""파일 로드와 Blender Undo/Redo 뒤 재생성 가능한 임시 상태를 폐기합니다."""

from __future__ import annotations

import bpy
from bpy.app.handlers import persistent

from .axis_display import migrate_loaded_axis_presets
from .camera_review_session import clear_review_sessions
from .camera_filter_queue import (
    PENDING_STATUS_STALE,
    clear_pending_filters,
    clear_pending_runtime,
    has_pending_filter_data,
    release_pending_overlay,
    restore_pending_filters,
)
from .camera_edit_session import clear_camera_edit_sessions
from .edit_coordinator import clear_edit_sessions
from .export_progress_session import clear_export_progress_sessions
from .import_progress_session import clear_import_progress_sessions
from .managed_scene import iter_managed_roots
from .modifier_data_visibility import synchronize_modifier_data_visibility
from .modifier_session import SESSION, SESSION_ACTIVE, is_session_active_for_window
from .modifier_input_guard import (
    rearm_modifier_input_guard,
    stop_modifier_input_guard,
)
from .modifier_persistence_handlers import (
    register_modifier_persistence_handlers,
    restore_domain_mirror_from_active,
    unregister_modifier_persistence_handlers,
)
from .runtime_state import (
    clear_runtime,
    request_runtime_restore,
    runtime_restore_job,
    write_domain_settings,
    write_history_settings,
    write_source_settings,
)
from .selection import clear_snapshots
from .undo_domain_coordinator import (
    capture_undo_protection_snapshot,
    clear_undo_domain_state,
    current_undo_protection_snapshot,
    schedule_undo_domain_reconcile,
)


_PENDING_RESTORE_SCHEDULED = False
_LOAD_RESTORE_GENERATION = 0
_VISIBILITY_SYNC_SCHEDULED = False


def _synchronize_visibility_after_state_change() -> None:
    """Load·Undo·Redo 뒤 관리 데이터를 전용 View Layer로 다시 격리합니다."""

    global _VISIBILITY_SYNC_SCHEDULED
    _VISIBILITY_SYNC_SCHEDULED = False
    for scene in tuple(getattr(bpy.data, "scenes", ())):
        if not tuple(iter_managed_roots(scene)):
            continue
        try:
            synchronize_modifier_data_visibility(
                scene,
                create_view_layer=True,
            )
        except (AttributeError, ReferenceError, RuntimeError, TypeError, ValueError) as error:
            settings = getattr(scene, "colmap_import", None)
            if settings is not None:
                settings.status = f"Modifier 데이터 View Layer 복구 실패: {error}"
                settings.status_kind = "ERROR"
    return None


def _schedule_visibility_synchronization() -> None:
    """Modifier 데이터 View Layer 동기화 Timer를 중복 없이 예약합니다."""

    global _VISIBILITY_SYNC_SCHEDULED
    if _VISIBILITY_SYNC_SCHEDULED:
        return
    scenes = tuple(getattr(bpy.data, "scenes", ()))
    if not any(tuple(iter_managed_roots(scene)) for scene in scenes):
        return
    _VISIBILITY_SYNC_SCHEDULED = True
    if not bpy.app.timers.is_registered(_synchronize_visibility_after_state_change):
        bpy.app.timers.register(
            _synchronize_visibility_after_state_change,
            first_interval=0.0,
        )


def _restore_pending_after_state_change():
    """Blender 상태 복구가 끝난 다음 저장 Pending 표시를 안전하게 재생성합니다."""
    global _PENDING_RESTORE_SCHEDULED
    _PENDING_RESTORE_SCHEDULED = False
    context = bpy.context
    scene = getattr(context, "scene", None)
    settings = getattr(scene, "colmap_import", None)
    if settings is None or not has_pending_filter_data(settings):
        return None
    try:
        from .camera_review_session import ensure_review_session
        from .runtime_state import ensure_runtime

        state = ensure_runtime(scene)
        session = ensure_review_session(context)
        restore_pending_filters(context, state=state, session=session)
    except Exception as exc:
        settings.review_pending_filter_status = PENDING_STATUS_STALE
        settings.review_status = (
            f"저장된 Pending Camera Filter 복원 실패: {exc}. Clear만 허용됩니다."
        )
    return None


def _schedule_pending_restore() -> None:
    global _PENDING_RESTORE_SCHEDULED
    settings = getattr(getattr(bpy.context, "scene", None), "colmap_import", None)
    if (
        settings is None
        or not has_pending_filter_data(settings)
        or _PENDING_RESTORE_SCHEDULED
    ):
        return
    _PENDING_RESTORE_SCHEDULED = True
    bpy.app.timers.register(
        _restore_pending_after_state_change,
        first_interval=0.0,
    )


@persistent
def invalidate_transient_state(
    *_args,
    schedule_pending_restore: bool = True,
) -> None:
    """파일 Load 경계에서 Blender ID 참조와 모든 프로세스 Runtime을 정리합니다."""

    stop_modifier_input_guard()
    clear_import_progress_sessions()
    clear_export_progress_sessions()
    warnings = release_pending_overlay(bpy.context)
    clear_review_sessions()
    clear_pending_runtime()
    clear_edit_sessions(bpy.context)
    clear_camera_edit_sessions(bpy.context)
    clear_runtime()
    clear_snapshots()
    clear_undo_domain_state()
    settings = getattr(
        getattr(bpy.context, "scene", None),
        "colmap_import",
        None,
    )
    if settings is not None:
        if warnings:
            settings.warning_summary = "\n".join(warnings)
        if not has_pending_filter_data(settings):
            restore_pending_filters(bpy.context)
    if schedule_pending_restore:
        _schedule_pending_restore()


@persistent
def reconcile_after_undo_redo(*_args) -> None:
    """활성 Modifier Workspace에서만 Undo 결과를 즉시 재생성합니다."""

    window = getattr(bpy.context, "window", None)
    if SESSION.phase != SESSION_ACTIVE or not is_session_active_for_window(window):
        return
    snapshot = current_undo_protection_snapshot()
    schedule_undo_domain_reconcile(snapshot)
    _schedule_visibility_synchronization()


@persistent
def invalidate_loaded_state(*_args) -> None:
    """새 `.blend` load에서 Runtime을 비우고 ACTIVE Payload 자동 복원을 예약합니다."""

    global _PENDING_RESTORE_SCHEDULED
    migrate_loaded_axis_presets(bpy.context)
    if bpy.app.timers.is_registered(_restore_pending_after_state_change):
        bpy.app.timers.unregister(_restore_pending_after_state_change)
    _PENDING_RESTORE_SCHEDULED = False
    invalidate_transient_state(schedule_pending_restore=False)
    try:
        clear_pending_filters(bpy.context)
    except (AttributeError, ReferenceError, RuntimeError, TypeError, ValueError):
        pass
    scene = getattr(bpy.context, "scene", None)
    if scene is not None and restore_domain_mirror_from_active(scene):
        _schedule_load_restore()
    _schedule_visibility_synchronization()


def process_pending_load_restore():
    """ACTIVE Payload의 Source·Operation을 UI Tick 예산으로 자동 복원합니다."""

    scene = getattr(bpy.context, "scene", None)
    settings = getattr(scene, "colmap_import", None)
    if scene is None or settings is None:
        return None
    try:
        job = runtime_restore_job(scene)
        if job is None:
            job = request_runtime_restore(scene)
        progress = job.advance(work_budget_ms=20.0)
        settings.status_kind = "IMPORTING" if not job.done else "SUCCESS"
        settings.status = str(progress.message)
        if not job.done:
            return 0.01
        state = job.result
        if state is None:
            raise RuntimeError("자동 복원된 Modifier Runtime 결과가 없습니다.")
        from .edit_coordinator import synchronize_scene
        from .modifier_runtime_domain import domain_for_scene

        domain = domain_for_scene(scene, required=True)
        write_source_settings(settings, state)
        write_history_settings(settings, state)
        write_domain_settings(settings, domain)
        settings.camera_count = str(len(state.current_reconstruction.cameras))
        settings.image_count = str(len(state.current_reconstruction.images))
        settings.point3d_count = str(len(state.current_reconstruction.points3d))
        settings.point_edit_mode_active = False
        settings.camera_edit_mode_active = False
        settings.remove_target = "NONE"
        settings.selected_point_count = 0
        settings.observing_camera_count = 0
        settings.derived_orphan_count = 0
        settings.camera_target_count = 0
        warnings = synchronize_scene(bpy.context, state, settings)
        if warnings:
            settings.warning_summary = "\n".join(warnings)
        settings.domain_restore_error = ""
        settings.status_kind = "SUCCESS"
        settings.status = "저장된 Modifier Runtime 자동 복원을 완료했습니다."
        if SESSION.phase == SESSION_ACTIVE:
            rearm_modifier_input_guard(getattr(bpy.context, "window", None))
        _schedule_visibility_synchronization()
        return None
    except Exception as exc:
        settings.domain_restore_error = str(exc)
        settings.domain_persistence_dirty = False
        settings.status_kind = "ERROR"
        settings.status = (
            "저장된 Modifier Runtime 자동 복원에 실패했습니다. "
            f"Dataset을 다시 Import하십시오: {exc}"
        )
        return None


def _schedule_load_restore() -> None:
    """파일 Load 자동 Runtime 복원 Timer를 중복 없이 예약합니다."""

    global _LOAD_RESTORE_GENERATION

    _LOAD_RESTORE_GENERATION += 1
    if bpy.app.timers.is_registered(process_pending_load_restore):
        bpy.app.timers.unregister(process_pending_load_restore)
    bpy.app.timers.register(process_pending_load_restore, first_interval=0.0)


def register_state_handlers() -> None:
    """Modifier 상태 복구 Handler와 기존 데이터 가시성 동기화를 등록합니다."""

    if invalidate_loaded_state not in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.append(invalidate_loaded_state)
    register_modifier_persistence_handlers()
    for handlers in (
        bpy.app.handlers.undo_pre,
        bpy.app.handlers.redo_pre,
    ):
        if capture_undo_protection_snapshot not in handlers:
            handlers.append(capture_undo_protection_snapshot)
    for handlers in (
        bpy.app.handlers.undo_post,
        bpy.app.handlers.redo_post,
    ):
        if reconcile_after_undo_redo not in handlers:
            handlers.append(reconcile_after_undo_redo)
    _schedule_visibility_synchronization()


def unregister_state_handlers() -> None:
    """Modifier 상태 복구 Handler와 예약된 가시성 동기화를 해제합니다."""

    global _PENDING_RESTORE_SCHEDULED
    global _LOAD_RESTORE_GENERATION
    global _VISIBILITY_SYNC_SCHEDULED
    if invalidate_loaded_state in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(invalidate_loaded_state)
    unregister_modifier_persistence_handlers()
    for handlers in (
        bpy.app.handlers.undo_pre,
        bpy.app.handlers.redo_pre,
    ):
        if capture_undo_protection_snapshot in handlers:
            handlers.remove(capture_undo_protection_snapshot)
    for handlers in (
        bpy.app.handlers.undo_post,
        bpy.app.handlers.redo_post,
    ):
        if reconcile_after_undo_redo in handlers:
            handlers.remove(reconcile_after_undo_redo)
    if bpy.app.timers.is_registered(_restore_pending_after_state_change):
        bpy.app.timers.unregister(_restore_pending_after_state_change)
    if bpy.app.timers.is_registered(process_pending_load_restore):
        bpy.app.timers.unregister(process_pending_load_restore)
    if bpy.app.timers.is_registered(_synchronize_visibility_after_state_change):
        bpy.app.timers.unregister(_synchronize_visibility_after_state_change)
    _PENDING_RESTORE_SCHEDULED = False
    _LOAD_RESTORE_GENERATION += 1
    _VISIBILITY_SYNC_SCHEDULED = False
    clear_undo_domain_state()
