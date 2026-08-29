"""Blender Global Undo 경계에서 COLMAP 임시 상태를 안전하게 재생성합니다."""

from __future__ import annotations

from dataclasses import dataclass

import bpy
from bpy.app.handlers import persistent


@dataclass(frozen=True, slots=True)
class COLMAPUndoProtectionSnapshot:
    """Undo 전 보존하는 Blender ID 비소유 탐색 값입니다."""

    window_pointer: int
    scene_name: str
    dataset_uuid: str
    source_fingerprint: str
    current_image_id: int | None
    current_index: int
    current_frame: int
    timeline_active: bool


@dataclass(frozen=True, slots=True)
class _PendingReconcile:
    generation: int
    snapshot: COLMAPUndoProtectionSnapshot


_SNAPSHOTS: dict[int, COLMAPUndoProtectionSnapshot] = {}
_PENDING_RECONCILE: _PendingReconcile | None = None
_RECONCILE_GENERATION = 0
_RECONCILE_RUNNING = False
_RECOVERY_REQUIRED_DATASETS: set[str] = set()


def _pointer(value) -> int:
    try:
        return int(value.as_pointer()) if value is not None else 0
    except (AttributeError, ReferenceError, RuntimeError, TypeError):
        return 0


def _window_key(context) -> int:
    return _pointer(getattr(context, "window", None))


def _parse_image_id(value) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _capture_navigation_snapshot(context) -> COLMAPUndoProtectionSnapshot:
    """유효한 Undo 직전 Context에서 순수 값만 복사합니다."""

    scene = getattr(context, "scene", None)
    settings = getattr(scene, "colmap_import", None)
    scene_pointer = _pointer(scene)
    timeline_active = False
    if scene_pointer:
        try:
            from .camera_review_session import _SESSIONS as review_sessions

            session = review_sessions.get(scene_pointer)
            timeline_active = bool(
                session is not None and session.timeline_active
            )
        except (AttributeError, ReferenceError, RuntimeError, TypeError):
            timeline_active = False

    return COLMAPUndoProtectionSnapshot(
        window_pointer=_window_key(context),
        scene_name=str(getattr(scene, "name", "") or ""),
        dataset_uuid=str(getattr(settings, "dataset_uuid", "") or ""),
        source_fingerprint=str(
            getattr(settings, "source_fingerprint", "") or ""
        ),
        current_image_id=_parse_image_id(
            getattr(settings, "review_current_image_id", "")
        ),
        current_index=int(
            getattr(settings, "review_current_index", -1) or 0
        ),
        current_frame=int(getattr(scene, "frame_current", 0) or 0),
        timeline_active=timeline_active,
    )


def abandon_transient_state_for_global_undo() -> None:
    """이전 Blender ID를 읽거나 정리하지 않고 모든 임시 소유권을 버립니다."""

    from .camera_filter_queue import clear_pending_runtime
    from .camera_review_session import (
        abandon_review_sessions_for_global_undo,
    )
    from .edit_coordinator import abandon_edit_sessions_for_global_undo
    from .export_progress_session import (
        abandon_export_progress_sessions_for_global_undo,
    )
    from .import_progress_session import (
        abandon_import_progress_sessions_for_global_undo,
    )
    from .runtime_state import discard_runtime_for_global_undo
    from .selection import clear_snapshots

    abandon_review_sessions_for_global_undo()
    abandon_edit_sessions_for_global_undo()
    abandon_import_progress_sessions_for_global_undo()
    abandon_export_progress_sessions_for_global_undo()
    clear_pending_runtime()
    clear_snapshots()

    discard_runtime_for_global_undo()


@persistent
def capture_undo_protection_snapshot(*_args) -> None:
    """Undo·Redo 직전 탐색 값 캡처 후 stale ID Registry를 abandon합니다."""

    context = bpy.context
    snapshot = _capture_navigation_snapshot(context)
    _SNAPSHOTS[_window_key(context)] = snapshot
    abandon_transient_state_for_global_undo()


def current_undo_protection_snapshot() -> COLMAPUndoProtectionSnapshot | None:
    """현재 Window에서 마지막 Undo 전 탐색 Snapshot을 반환합니다."""

    return _SNAPSHOTS.get(_window_key(bpy.context))


def schedule_undo_domain_reconcile(
    snapshot: COLMAPUndoProtectionSnapshot | None = None,
) -> bool:
    """가장 최신 Global Undo·Redo 결과 재생성을 다음 Tick에 예약합니다."""

    global _PENDING_RECONCILE
    global _RECONCILE_GENERATION
    if snapshot is None:
        snapshot = current_undo_protection_snapshot()
    if snapshot is None:
        return False
    _RECONCILE_GENERATION += 1
    _PENDING_RECONCILE = _PendingReconcile(
        generation=_RECONCILE_GENERATION,
        snapshot=snapshot,
    )
    if not bpy.app.timers.is_registered(process_pending_undo_reconcile):
        bpy.app.timers.register(
            process_pending_undo_reconcile,
            first_interval=0.0,
        )
    return True


def _resolve_window(snapshot: COLMAPUndoProtectionSnapshot):
    if not snapshot.window_pointer:
        return None
    try:
        windows = bpy.context.window_manager.windows
    except (AttributeError, ReferenceError, RuntimeError):
        return None
    return next(
        (
            window
            for window in windows
            if _pointer(window) == snapshot.window_pointer
        ),
        None,
    )


def _resolve_scene(snapshot: COLMAPUndoProtectionSnapshot, window):
    if window is not None:
        try:
            return window.scene
        except (AttributeError, ReferenceError, RuntimeError):
            return None
    scene = getattr(bpy.context, "scene", None)
    if scene is not None:
        return scene
    if snapshot.scene_name:
        return bpy.data.scenes.get(snapshot.scene_name)
    return None


def _normalize_restored_modes(settings) -> None:
    """Undo 후 재개하지 않는 임시 편집 모드를 비활성 상태로 맞춥니다."""

    settings.point_edit_mode_active = False
    settings.camera_edit_mode_active = False
    settings.remove_target = "NONE"
    settings.selected_point_count = 0
    settings.observing_camera_count = 0
    settings.derived_orphan_count = 0
    settings.camera_target_count = 0


def _restore_review(context, state, snapshot) -> None:
    """복원된 Runtime과 새 Blender ID로 Review·Timeline을 재생성합니다."""

    from .camera_filter_queue import restore_pending_filters
    from .camera_review_session import (
        ensure_review_session,
        update_review_background_from_settings,
    )
    from .camera_review_timeline import (
        build_or_refresh_timeline,
        sync_playhead_to_current,
    )

    settings = context.scene.colmap_import
    restored_image_id = _parse_image_id(settings.review_current_image_id)
    if (
        restored_image_id is None
        and snapshot.dataset_uuid == state.dataset_uuid
        and snapshot.current_image_id in state.current_reconstruction.images
    ):
        settings.review_current_image_id = (
            str(snapshot.current_image_id)
            if snapshot.current_image_id is not None
            else ""
        )
        settings.review_current_index = snapshot.current_index

    session = ensure_review_session(context, state=state)
    marker_manifest = str(settings.review_marker_manifest_json or "").strip()
    restored_timeline_active = marker_manifest not in ("", "[]", "{}")
    if (
        restored_timeline_active
        or (
            snapshot.dataset_uuid == state.dataset_uuid
            and snapshot.timeline_active
        )
    ):
        build_or_refresh_timeline(context, session, allow_empty=True)
        if session.current_image_id is not None:
            sync_playhead_to_current(session)
    else:
        session.timeline_active = False
        session.timeline_refresh_enabled = False
        session.timeline_manifest_signature = ()

    update_review_background_from_settings(settings, context)
    restore_pending_filters(context, state=state, session=session)


def _perform_reconcile(context, snapshot) -> None:
    """복원된 Scene history를 유일한 권위로 Runtime과 표시 상태를 만듭니다."""

    scene = context.scene
    settings = getattr(scene, "colmap_import", None)
    if settings is None:
        return

    _normalize_restored_modes(settings)
    if not settings.dataset_uuid or not settings.active_model_directory:
        settings.review_status = (
            "Blender Undo/Redo 완료: 활성 COLMAP 데이터셋이 없습니다."
        )
        return

    from .edit_coordinator import synchronize_scene
    from .runtime_state import ensure_runtime

    state = ensure_runtime(scene)
    if (
        state.dataset_uuid != settings.dataset_uuid
        or state.source.source_fingerprint != settings.source_fingerprint
        or state.edit_revision != settings.edit_revision
        or state.index_revision != state.edit_revision
        or state.history_hash != settings.history_hash
    ):
        raise ValueError(
            "복원된 Scene과 재생성한 COLMAP Runtime token이 일치하지 않습니다."
        )

    warnings = synchronize_scene(context, state, settings)
    context.view_layer.update()
    _restore_review(context, state, snapshot)
    settings.review_status = "Blender Undo/Redo 뒤 COLMAP 상태를 재생성했습니다."
    if warnings:
        settings.warning_summary = "\n".join(warnings)
    _RECOVERY_REQUIRED_DATASETS.discard(state.dataset_uuid)


def process_pending_undo_reconcile():
    """예약된 최신 Undo·Redo 결과만 새 Blender ID로 재생성합니다."""

    global _PENDING_RECONCILE
    global _RECONCILE_RUNNING
    pending = _PENDING_RECONCILE
    _PENDING_RECONCILE = None
    if pending is None:
        return None
    if pending.generation != _RECONCILE_GENERATION:
        return 0.0 if _PENDING_RECONCILE is not None else None

    snapshot = pending.snapshot
    window = _resolve_window(snapshot)
    scene = _resolve_scene(snapshot, window)
    if scene is None:
        if snapshot.dataset_uuid:
            _RECOVERY_REQUIRED_DATASETS.add(snapshot.dataset_uuid)
        return None

    _RECONCILE_RUNNING = True
    try:
        view_layer = scene.view_layers[0]
        if window is None:
            with bpy.context.temp_override(
                scene=scene,
                view_layer=view_layer,
            ):
                _perform_reconcile(bpy.context, snapshot)
        else:
            with bpy.context.temp_override(
                window=window,
                scene=scene,
                view_layer=view_layer,
            ):
                _perform_reconcile(bpy.context, snapshot)
    except Exception as exc:
        settings = getattr(scene, "colmap_import", None)
        dataset_uuid = str(getattr(settings, "dataset_uuid", "") or "")
        if dataset_uuid:
            _RECOVERY_REQUIRED_DATASETS.add(dataset_uuid)
        if settings is not None:
            settings.review_status = (
                "COLMAP Undo 복구에 실패했습니다. 다시 Import해야 합니다: "
                f"{exc}"
            )
    finally:
        _RECONCILE_RUNNING = False
    return 0.0 if _PENDING_RECONCILE is not None else None


def undo_domain_mutation_blocked(context) -> bool:
    """재생성 중이거나 실패한 Dataset의 추가 편집을 차단합니다."""

    settings = getattr(getattr(context, "scene", None), "colmap_import", None)
    dataset_uuid = str(getattr(settings, "dataset_uuid", "") or "")
    return bool(
        _RECONCILE_RUNNING
        or _PENDING_RECONCILE is not None
        or (dataset_uuid and dataset_uuid in _RECOVERY_REQUIRED_DATASETS)
    )


def undo_domain_reconcile_pending() -> bool:
    return bool(_RECONCILE_RUNNING or _PENDING_RECONCILE is not None)


def undo_domain_recovery_required(context) -> bool:
    settings = getattr(getattr(context, "scene", None), "colmap_import", None)
    dataset_uuid = str(getattr(settings, "dataset_uuid", "") or "")
    return bool(dataset_uuid and dataset_uuid in _RECOVERY_REQUIRED_DATASETS)


def pending_reconcile_generation() -> int:
    return _RECONCILE_GENERATION


def clear_undo_domain_state(*_args) -> None:
    """load·새 Import·unregister에서 Snapshot과 예약 작업을 폐기합니다."""

    global _PENDING_RECONCILE
    global _RECONCILE_GENERATION
    global _RECONCILE_RUNNING
    _SNAPSHOTS.clear()
    _PENDING_RECONCILE = None
    _RECONCILE_GENERATION = 0
    _RECONCILE_RUNNING = False
    _RECOVERY_REQUIRED_DATASETS.clear()
    if bpy.app.timers.is_registered(process_pending_undo_reconcile):
        bpy.app.timers.unregister(process_pending_undo_reconcile)


__all__ = (
    "COLMAPUndoProtectionSnapshot",
    "abandon_transient_state_for_global_undo",
    "capture_undo_protection_snapshot",
    "clear_undo_domain_state",
    "current_undo_protection_snapshot",
    "pending_reconcile_generation",
    "process_pending_undo_reconcile",
    "schedule_undo_domain_reconcile",
    "undo_domain_mutation_blocked",
    "undo_domain_reconcile_pending",
    "undo_domain_recovery_required",
)
