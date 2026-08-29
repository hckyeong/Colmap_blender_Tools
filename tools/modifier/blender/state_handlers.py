"""파일 로드와 Blender Undo/Redo 뒤 재생성 가능한 임시 상태를 폐기합니다."""

from __future__ import annotations

from dataclasses import dataclass

import bpy
from bpy.app.handlers import persistent

from .axis_display import migrate_loaded_axis_presets
from .camera_review_session import clear_review_sessions
from .camera_filter_queue import (
    PENDING_STATUS_STALE,
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
from .runtime_state import clear_runtime
from .selection import clear_snapshots
from .undo_domain_coordinator import (
    capture_undo_protection_snapshot,
    clear_undo_domain_state,
    current_undo_protection_snapshot,
    schedule_undo_domain_reconcile,
)


_PENDING_RESTORE_SCHEDULED = False
_LOAD_RESTORE_GENERATION = 0
_LOAD_RESTORE_SCHEDULED = False
_VISIBILITY_SYNC_SCHEDULED = False


@dataclass(frozen=True)
class _PendingLoadRestore:
    """연속 파일 로드에서 최신 Scene만 복원하기 위한 저장 token입니다."""

    generation: int
    filepath: str
    scene_pointer: int
    dataset_uuid: str
    model_directory: str


_PENDING_LOAD_RESTORE: _PendingLoadRestore | None = None


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
    """Undo가 복원한 Scene을 다음 Tick에서 새 Runtime으로 재생성합니다."""

    snapshot = current_undo_protection_snapshot()
    schedule_undo_domain_reconcile(snapshot)
    _schedule_visibility_synchronization()


@persistent
def invalidate_loaded_state(*_args) -> None:
    """새 `.blend` load에서 비영속 History와 모든 임시 Runtime을 폐기합니다."""

    global _PENDING_RESTORE_SCHEDULED
    migrate_loaded_axis_presets(bpy.context)
    if bpy.app.timers.is_registered(_restore_pending_after_state_change):
        bpy.app.timers.unregister(_restore_pending_after_state_change)
    _PENDING_RESTORE_SCHEDULED = False
    invalidate_transient_state(schedule_pending_restore=False)
    _schedule_visibility_synchronization()
    _schedule_load_restore()


def _load_restore_jobs_running() -> bool:
    """로드 복원이 다른 장기 작업의 Blender 상태와 겹치는지 확인합니다."""

    from .export_progress_session import active_export_progress_session
    from .import_progress_session import active_import_progress_session

    return bool(
        active_import_progress_session() is not None
        or active_export_progress_session() is not None
    )


def _pending_load_restore_is_current(
    pending: _PendingLoadRestore,
    scene,
    settings,
) -> bool:
    """Timer가 예약된 파일과 현재 활성 Scene을 다시 검증합니다."""

    try:
        return bool(
            pending.generation == _LOAD_RESTORE_GENERATION
            and str(bpy.data.filepath or "") == pending.filepath
            and scene.as_pointer() == pending.scene_pointer
            and settings.dataset_uuid == pending.dataset_uuid
            and settings.active_model_directory == pending.model_directory
        )
    except (AttributeError, ReferenceError, RuntimeError):
        return False


def process_pending_load_restore():
    """로드 다음 Tick에서 저장 Camera Review와 Timeline Runtime을 재결합합니다."""

    global _LOAD_RESTORE_SCHEDULED
    global _PENDING_LOAD_RESTORE

    pending = _PENDING_LOAD_RESTORE
    if pending is None:
        _LOAD_RESTORE_SCHEDULED = False
        return None
    context = bpy.context
    scene = getattr(context, "scene", None)
    settings = getattr(scene, "colmap_import", None)
    if (
        scene is None
        or settings is None
        or not _pending_load_restore_is_current(pending, scene, settings)
    ):
        if pending is _PENDING_LOAD_RESTORE:
            _PENDING_LOAD_RESTORE = None
            _LOAD_RESTORE_SCHEDULED = False
        return None
    if _load_restore_jobs_running():
        return 0.1

    _PENDING_LOAD_RESTORE = None
    _LOAD_RESTORE_SCHEDULED = False
    if not settings.dataset_uuid or not settings.active_model_directory:
        return None

    try:
        from .camera_review_session import (
            ensure_review_session,
            update_review_background_from_settings,
        )
        from .camera_review_timeline import (
            attach_review_timeline_markers,
            detach_review_timeline_markers,
            modifier_marker_session_owned,
        )
        from .runtime_state import ensure_runtime

        state = ensure_runtime(scene)
        session = ensure_review_session(context, state=state)
        restore_pending_filters(context, state=state, session=session)
        if settings.review_pending_filter_status == PENDING_STATUS_STALE:
            return None

        marker_session_active = bool(
            SESSION.phase == SESSION_ACTIVE
            and is_session_active_for_window(getattr(context, "window", None))
            and modifier_marker_session_owned(scene)
        )
        marker_count = (
            attach_review_timeline_markers(session)
            if marker_session_active
            else 0
        )
        if not marker_session_active:
            detach_review_timeline_markers(session)

        update_review_background_from_settings(settings, context)
        if marker_count:
            settings.review_status = (
                f"저장된 Camera Review Timeline {marker_count}개를 "
                f"복원했습니다. 현재 IMAGE_ID {session.current_image_id}."
            )
        else:
            settings.review_status = (
                "저장된 Camera Review를 복원했습니다. "
                "비활성 Modifier의 Timeline Marker는 분리된 상태입니다."
            )
    except Exception as exc:
        settings.review_status = (
            "저장된 COLMAP Camera Review를 복원하지 못했습니다: "
            f"{exc}. source와 Timeline을 확인한 뒤 "
            "Build / Refresh Timeline을 실행하십시오."
        )
    return None


def _schedule_load_restore() -> None:
    """현재 로드 generation의 복원 Timer를 정확히 하나 예약합니다."""

    global _LOAD_RESTORE_GENERATION
    global _LOAD_RESTORE_SCHEDULED
    global _PENDING_LOAD_RESTORE

    _LOAD_RESTORE_GENERATION += 1
    scene = getattr(bpy.context, "scene", None)
    settings = getattr(scene, "colmap_import", None)
    if scene is None or settings is None:
        _PENDING_LOAD_RESTORE = None
        _LOAD_RESTORE_SCHEDULED = False
        return
    _PENDING_LOAD_RESTORE = _PendingLoadRestore(
        generation=_LOAD_RESTORE_GENERATION,
        filepath=str(bpy.data.filepath or ""),
        scene_pointer=scene.as_pointer(),
        dataset_uuid=str(settings.dataset_uuid or ""),
        model_directory=str(settings.active_model_directory or ""),
    )
    _LOAD_RESTORE_SCHEDULED = True
    if not bpy.app.timers.is_registered(process_pending_load_restore):
        bpy.app.timers.register(
            process_pending_load_restore,
            first_interval=0.0,
        )


def register_state_handlers() -> None:
    """Modifier 상태 복구 Handler와 기존 데이터 가시성 동기화를 등록합니다."""

    if invalidate_loaded_state not in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.append(invalidate_loaded_state)
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
    global _LOAD_RESTORE_SCHEDULED
    global _PENDING_LOAD_RESTORE
    global _VISIBILITY_SYNC_SCHEDULED
    if invalidate_loaded_state in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(invalidate_loaded_state)
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
    _LOAD_RESTORE_SCHEDULED = False
    _PENDING_LOAD_RESTORE = None
    _VISIBILITY_SYNC_SCHEDULED = False
    clear_undo_domain_state()
