"""Load·Undo·Redo·Save와 Scene Event를 안전한 수명주기 경계로 연결합니다."""

from __future__ import annotations

from typing import Any

import bpy

from .lifecycle import lifecycle_registry


_LAZY_RECOVERY_SCENES: set[int] = set()


def _pointer(value: Any) -> int:
    """유효한 Blender RNA 포인터를 반환하고 실패하면 0을 반환합니다."""

    try:
        return int(value.as_pointer()) if value is not None else 0
    except (AttributeError, ReferenceError, RuntimeError, TypeError):
        return 0


def _projection_workspace_active(scene: Any) -> bool:
    """현재 Window 중 하나가 Scene의 Projection Paint Workspace를 표시하는지 반환합니다."""

    from .paint_workspace import PAINT_WORKSPACE_SPEC
    try:
        from ....shared.blender_runtime.workspace_factory import workspace_matches_spec
    except ImportError:
        from shared.blender_runtime.workspace_factory import workspace_matches_spec

    windows = tuple(getattr(getattr(bpy.context, "window_manager", None), "windows", ()))
    return any(
        getattr(window, "scene", None) is scene
        and workspace_matches_spec(
            getattr(window, "workspace", None),
            PAINT_WORKSPACE_SPEC,
            require_ready=False,
        )
        for window in windows
    )


def _settings(scene: Any) -> Any | None:
    """Scene의 Projection Painting 설정을 예외 없이 반환합니다."""

    try:
        return getattr(scene, "projection_paint", None)
    except (AttributeError, ReferenceError, RuntimeError):
        return None


def _set_status(scene: Any, message: str, kind: str = "ERROR") -> None:
    """Handler 오류 상태를 Scene UI에 안전하게 기록합니다."""

    settings = _settings(scene)
    if settings is None:
        return
    try:
        settings.status = str(message)
        settings.status_kind = str(kind)
        if kind == "ERROR" and hasattr(settings, "paint_state"):
            settings.paint_state = "ERROR"
    except (AttributeError, ReferenceError, RuntimeError, TypeError):
        return


def _clear_transient_caches() -> None:
    """Blender 권위 데이터를 유지한 채 Import와 Overlay Runtime을 비웁니다."""
    native_bake_pending = False

    try:
        from .import_progress_session import clear_import_progress_sessions

        clear_import_progress_sessions()
    except (ImportError, AttributeError):
        pass
    try:
        from .texture_export import (
            clear_texture_export_jobs,
            texture_export_native_bake_pending,
        )

        clear_texture_export_jobs()
        native_bake_pending = bool(
            native_bake_pending or texture_export_native_bake_pending()
        )
    except (ImportError, AttributeError):
        pass
    try:
        from .paint_progress_session import clear_paint_progress_sessions

        clear_paint_progress_sessions()
    except (ImportError, AttributeError):
        pass
    try:
        from .composite_layer import (
            clear_composite_create_jobs,
            composite_native_bake_pending,
        )

        clear_composite_create_jobs()
        native_bake_pending = bool(
            native_bake_pending or composite_native_bake_pending()
        )
    except (ImportError, AttributeError):
        pass
    if not native_bake_pending:
        try:
            from .composite_progress import clear_composite_progress_sessions

            clear_composite_progress_sessions()
        except (ImportError, AttributeError):
            pass
    try:
        from .overlay_durability import clear_runtime_registry

        clear_runtime_registry()
    except (ImportError, AttributeError):
        pass
    try:
        from .camera_review import clear_camera_review_runtime

        clear_camera_review_runtime()
    except (ImportError, AttributeError):
        pass


def lifecycle_pre_handler(*_args: Any, reason: str) -> None:
    """Load·Undo·Redo 전에 Callback을 멈추고 Blender Paint 데이터는 유지합니다."""
    _clear_transient_caches()
    lifecycle_registry().barrier(reason)


def load_pre_handler(*args: Any) -> None:
    """파일 교체 전에 이전 파일의 Runtime 참조를 무효화합니다."""

    lifecycle_pre_handler(*args, reason="load_pre")


def undo_pre_handler(*args: Any) -> None:
    """Undo 전에 지연 작업과 직접 RNA 참조를 무효화합니다."""

    lifecycle_pre_handler(*args, reason="undo_pre")


def redo_pre_handler(*args: Any) -> None:
    """Redo 전에 지연 작업과 직접 RNA 참조를 무효화합니다."""

    lifecycle_pre_handler(*args, reason="redo_pre")


def _rebuild_transient_state(scene: Any, reason: str) -> None:
    """현재 Blender Pointer와 Manifest에서 Overlay·Camera·Paint Cache를 재구축합니다."""

    try:
        from .composite_layer import recover_interrupted_recomposite

        recover_interrupted_recomposite(scene)
    except (ImportError, AttributeError):
        pass
    except RuntimeError as exc:
        _set_status(scene, f"Composite 다시 생성 복구 실패: {exc}")

    try:
        from .overlay_durability import rebuild_overlay_registry

        rebuild_overlay_registry()
    except (ImportError, AttributeError):
        pass
    except RuntimeError as exc:
        _set_status(scene, f"Overlay 상태 복구 실패: {exc}")
    lifecycle_registry().rebuild(scene, reason)


def _rebuild_paint_runtime(scene: Any, reason: str, _epoch: int) -> None:
    """수명주기 완료 뒤 Paint 상태 Reconcile을 Main Thread에 예약합니다."""
    if scene is None:
        return
    settings = _settings(scene)
    if settings is None:
        return
    from .paint_runtime import schedule_paint_reconcile

    schedule_paint_reconcile(scene, reason)


def load_post_handler(*_args: Any) -> None:
    """새 파일의 Manifest 기준으로 Runtime을 안전하게 재구축합니다."""

    register_runtime_handlers()
    scene = getattr(bpy.context, "scene", None)
    _rebuild_transient_state(scene, "load_post")


def undo_post_handler(scene: Any | None = None, *_args: Any) -> None:
    """활성 PP Workspace만 즉시 복구하고 비활성 Scene은 지연 상태로 둡니다."""

    target = scene or getattr(bpy.context, "scene", None)
    if target is None:
        return
    if not _projection_workspace_active(target):
        _LAZY_RECOVERY_SCENES.add(_pointer(target))
        return
    _LAZY_RECOVERY_SCENES.discard(_pointer(target))
    _rebuild_transient_state(target, "undo_post")


def redo_post_handler(scene: Any | None = None, *_args: Any) -> None:
    """활성 PP Workspace만 즉시 복구하고 비활성 Scene은 지연 상태로 둡니다."""

    target = scene or getattr(bpy.context, "scene", None)
    if target is None:
        return
    if not _projection_workspace_active(target):
        _LAZY_RECOVERY_SCENES.add(_pointer(target))
        return
    _LAZY_RECOVERY_SCENES.discard(_pointer(target))
    _rebuild_transient_state(target, "redo_post")


def recover_lazy_runtime(scene: Any, reason: str = "workspace_enter") -> None:
    """PP 재진입 시 미뤄 둔 Runtime을 현재 Blender ID에서 한 번만 복구합니다."""

    pointer = _pointer(scene)
    if pointer not in _LAZY_RECOVERY_SCENES and not lifecycle_registry().suspended:
        return
    _LAZY_RECOVERY_SCENES.discard(pointer)
    _rebuild_transient_state(scene, reason)


def depsgraph_update_handler(scene: Any, depsgraph: Any) -> None:
    """변경된 관리 Camera 식별자만 Runtime Dirty Queue에 기록합니다."""

    if lifecycle_registry().suspended:
        return
    from .camera_scene import collect_dirty_camera_ids

    collect_dirty_camera_ids(scene, depsgraph)


def frame_change_handler(scene: Any, *_args: Any) -> None:
    """활성 Camera 세션의 Frame만 예약하고 비활성 pending은 조용히 폐기합니다."""

    if lifecycle_registry().suspended:
        return
    from .camera_scene import (
        cancel_projection_frame_requests,
        projection_camera_session_is_active,
        queue_frame_change,
        runtime_for_scene,
    )

    runtime = runtime_for_scene(scene)
    if runtime is None:
        return
    if not projection_camera_session_is_active(
        scene,
        token=runtime.camera_session_token,
    ):
        cancel_projection_frame_requests(scene)
        return

    queue_frame_change(scene)


def save_pre_handler(*_args: Any) -> None:
    """저장 직전 모든 Dirty 관리 Overlay를 Pack 또는 Sidecar로 보호합니다."""

    scene = getattr(bpy.context, "scene", None)
    try:
        from .overlay_durability import pack_registered_overlays

        pack_registered_overlays(scene=scene)
    except Exception as exc:
        settings = _settings(scene)
        if settings is not None:
            try:
                settings.durability_state = "DATA_AT_RISK"
                settings.recovery_available = bool(settings.recovery_path)
            except (AttributeError, ReferenceError, RuntimeError, TypeError):
                pass
        _set_status(scene, f"Overlay 저장 내구성 확보 실패: {exc}")
        raise


def save_post_handler(*_args: Any) -> None:
    """저장 완료 뒤 관리 Overlay의 Pack 계약을 재검증합니다."""

    scene = getattr(bpy.context, "scene", None)
    from .overlay_durability import verify_registered_overlays

    verify_registered_overlays()
    settings = _settings(scene)
    if settings is not None and settings.durability_state != "DATA_AT_RISK":
        settings.durability_state = "DURABLE"


def save_post_fail_handler(*_args: Any) -> None:
    """Blender 저장 실패 뒤 Dirty와 Recovery 상태를 유지합니다."""

    scene = getattr(bpy.context, "scene", None)
    settings = _settings(scene)
    if settings is None:
        return
    if settings.durability_state != "DATA_AT_RISK":
        settings.durability_state = "DIRTY"
    _set_status(scene, "Blender 파일 저장이 실패했습니다.", "ERROR")


def _record_native_bake_outcome(outcome: str, *args: Any) -> None:
    """Blender 5.2 Bake 신호를 Composite와 Texture Export 소비자 양쪽에 기록합니다."""
    if not args:
        return
    handler_scene = getattr(bpy.context, "scene", None) if len(args) == 1 else None
    try:
        from .composite_layer import mark_native_bake_outcome

        if handler_scene is not None:
            mark_native_bake_outcome(handler_scene, args[0], outcome)
        elif len(args) == 1:
            mark_native_bake_outcome(args[0], outcome)
        else:
            mark_native_bake_outcome(args[0], args[1], outcome)
    except (ImportError, AttributeError, ReferenceError, RuntimeError, TypeError):
        pass
    try:
        from .texture_export import mark_texture_export_native_bake_outcome

        if handler_scene is not None:
            mark_texture_export_native_bake_outcome(handler_scene, args[0], outcome)
        elif len(args) == 1:
            mark_texture_export_native_bake_outcome(args[0], outcome)
        else:
            mark_texture_export_native_bake_outcome(args[0], args[1], outcome)
    except (ImportError, AttributeError, ReferenceError, RuntimeError, TypeError):
        pass


def object_bake_pre_handler(*args: Any) -> None:
    """Native Bake 시작 Handler에서 일치하는 소비자 Job에 RUNNING 신호만 기록합니다."""
    _record_native_bake_outcome("RUNNING", *args)


def object_bake_complete_handler(*args: Any) -> None:
    """Native Bake 완료 Handler에서 일치하는 소비자 Job에 COMPLETE 신호만 기록합니다."""
    _record_native_bake_outcome("COMPLETE", *args)


def object_bake_cancel_handler(*args: Any) -> None:
    """Native Bake 취소 Handler에서 일치하는 소비자 Job에 CANCELLED 신호만 기록합니다."""
    _record_native_bake_outcome("CANCELLED", *args)


def register_runtime_handlers() -> None:
    """파일 로드 때 제거되는 Scene Runtime Handler를 다시 등록합니다."""

    registry = lifecycle_registry()
    registry.register_handler(
        bpy.app.handlers.depsgraph_update_post,
        depsgraph_update_handler,
        component="camera_depsgraph",
        keep_after_load=False,
    )
    registry.register_handler(
        bpy.app.handlers.frame_change_post,
        frame_change_handler,
        component="camera_frame",
        keep_after_load=False,
    )


def register_state_handlers() -> None:
    """영속 Bootstrap Handler와 Camera Runtime Hook을 등록합니다."""

    registry = lifecycle_registry()
    persistent_handlers = (
        (bpy.app.handlers.load_pre, load_pre_handler, "load_pre"),
        (bpy.app.handlers.load_post, load_post_handler, "load_post"),
        (bpy.app.handlers.undo_pre, undo_pre_handler, "undo_pre"),
        (bpy.app.handlers.undo_post, undo_post_handler, "undo_post"),
        (bpy.app.handlers.redo_pre, redo_pre_handler, "redo_pre"),
        (bpy.app.handlers.redo_post, redo_post_handler, "redo_post"),
        (bpy.app.handlers.save_pre, save_pre_handler, "save_pre"),
        (bpy.app.handlers.save_post, save_post_handler, "save_post"),
        (bpy.app.handlers.save_post_fail, save_post_fail_handler, "save_post_fail"),
    )
    for handler_list, callback, component in persistent_handlers:
        registry.register_handler(
            handler_list,
            callback,
            component=component,
            keep_after_load=True,
        )
    for handler_list, callback, component in (
        (bpy.app.handlers.object_bake_pre, object_bake_pre_handler, "object_bake_pre"),
        (
            bpy.app.handlers.object_bake_complete,
            object_bake_complete_handler,
            "object_bake_complete",
        ),
        (
            bpy.app.handlers.object_bake_cancel,
            object_bake_cancel_handler,
            "object_bake_cancel",
        ),
    ):
        registry.register_handler(
            handler_list,
            callback,
            component=component,
            keep_after_load=True,
        )
    register_runtime_handlers()
    from .camera_scene import register_camera_runtime_hooks
    from .paint_runtime import pause_paint_runtimes

    register_camera_runtime_hooks()
    registry.add_barrier_hook(pause_paint_runtimes)
    registry.add_rebuild_hook(_rebuild_paint_runtime)


def unregister_state_handlers() -> None:
    """Native Bake 중에는 Handler를 유지하고 종료 뒤에만 Runtime Hook을 해제합니다."""

    try:
        from .camera_scene import unregister_camera_runtime_hooks
        from .composite_layer import (
            clear_composite_create_jobs,
            composite_native_bake_pending,
        )
        from .import_progress_session import clear_import_progress_sessions
        from .paint_progress_session import clear_paint_progress_sessions
        from .paint_runtime import pause_paint_runtimes
        from .texture_export import (
            clear_texture_export_jobs,
            texture_export_running,
        )

        clear_composite_create_jobs()
        clear_texture_export_jobs()
        if composite_native_bake_pending() or texture_export_running():
            return
        clear_import_progress_sessions()
        clear_paint_progress_sessions()
        unregister_camera_runtime_hooks()
        lifecycle_registry().remove_barrier_hook(pause_paint_runtimes)
        lifecycle_registry().remove_rebuild_hook(_rebuild_paint_runtime)
        _LAZY_RECOVERY_SCENES.clear()
    except (ImportError, AttributeError):
        pass
