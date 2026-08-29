"""클래스, Scene 속성, 파일 handler와 GPU handler의 생명주기를 관리합니다."""

from __future__ import annotations

import bpy
from bpy.app.handlers import persistent
from bpy.props import PointerProperty

try:
    from ...shared.blender_runtime.workspace_factory import source_workspace_from_metadata
    from ...shared.blender_runtime.workspace_handoff import (
        WorkspaceHandoffAdapter,
        register_workspace_handoff_adapter,
        unregister_workspace_handoff_adapter,
        workspace_tool_id,
    )
except ImportError:
    from shared.blender_runtime.workspace_factory import source_workspace_from_metadata
    from shared.blender_runtime.workspace_handoff import (
        WorkspaceHandoffAdapter,
        register_workspace_handoff_adapter,
        unregister_workspace_handoff_adapter,
        workspace_tool_id,
    )

from .logging_utils import configure_logging, get_logger, shutdown_logging
from .operators import CLASSES as OPERATOR_CLASSES
from .operators.demo_data import cleanup_legacy_owned_demo_data
from .operators.import_model import cancel_active_import_job
from .operators.marker_pick import stop_marker_modal_timer
from .operators.workspace import (
    cancel_workspace_setup,
)
from .operators.view_framing import draw_frame_all_view_menu
from .properties import CLASSES as PROPERTY_CLASSES
from .properties import (
    COLMAPALIGN_PG_marker_align_log_list,
    COLMAPALIGN_PG_marker_pair_list,
    COLMAPALIGN_PG_settings,
    sync_marker_align_log,
    sync_marker_pair_list,
)
from .project_persistence.runtime import (
    ProjectPersistenceRuntimeError,
    cancel_project_restore_for_file_load,
    schedule_project_restore_if_available,
    sync_project_state_if_dirty,
)
from .session import (
    abort_session_entry,
    clear_session_for_file_load,
    request_session_exit,
    restore_session_for_unregister,
    session_target_window,
)
from .constants import SESSION_ACTIVE, SESSION_ENTERING, SESSION_EXITING, SESSION_INACTIVE
from .state import RUNTIME
from .ui import CLASSES as UI_CLASSES
from .viewport.registry import invalidate_registry
from .viewport.renderer import install_draw_handlers, remove_draw_handlers, request_redraw


CLASSES = PROPERTY_CLASSES + OPERATOR_CLASSES + UI_CLASSES
_REGISTERED = False
_MSGBUS_OWNER = object()
_LEGACY_CLEANUP_PENDING = False
_VIEW_MENU_REGISTERED = False
_ADDON_KEYMAPS: list[tuple[object, object]] = []
_LOGGER = get_logger("registration")


def _align_handoff_start(context) -> None:
    """Align의 기존 Workspace 구성 계약을 공용 전환 완료 뒤 실행합니다."""

    from .operators.workspace import configure_workspace

    configure_workspace(context, force_restore=False)


def _align_handoff_is_engaged(window) -> bool:
    """지정 Window에 Align 진입·활성·종료 세션이 남아 있는지 반환합니다."""

    target_window = session_target_window()
    return bool(
        RUNTIME.session.phase != SESSION_INACTIVE
        and target_window is not None
        and target_window.as_pointer() == window.as_pointer()
    )


def _align_handoff_request_exit(context, window) -> None:
    """Align 단계에 맞춰 진입 취소나 정식 세션 종료 또는 Source 복귀를 요청합니다."""

    phase = RUNTIME.session.phase
    if phase == SESSION_ACTIVE:
        if not request_session_exit(window):
            raise RuntimeError("활성 Align 세션 종료 요청이 거부되었습니다.")
        return
    if phase == SESSION_ENTERING:
        cancel_workspace_setup()
        abort_session_entry("다른 Workspace 전환으로 Align 진입을 취소했습니다.")
        return
    if phase == SESSION_EXITING:
        return
    current = getattr(window, "workspace", None)
    if workspace_tool_id(current) != "align":
        return
    source = source_workspace_from_metadata(current)
    if source is None:
        raise RuntimeError("Align Workspace의 원래 Source Workspace를 찾지 못했습니다.")
    window.workspace = source


def _align_handoff_is_settled(window) -> bool:
    """Align 세션과 활성 Workspace가 모두 대상 Window에서 해제됐는지 반환합니다."""

    return bool(
        RUNTIME.session.phase == SESSION_INACTIVE
        and workspace_tool_id(getattr(window, "workspace", None)) != "align"
    )


def _align_handoff_report_error(message: str) -> None:
    """비동기 전환 실패 원인을 Align 런타임 상태에 기록합니다."""

    RUNTIME.workspace_setup_error = str(message)
    RUNTIME.session.message = str(message)
    request_redraw()


_ALIGN_HANDOFF_ADAPTER = WorkspaceHandoffAdapter(
    tool_id="align",
    label="COLMAP Align",
    start=_align_handoff_start,
    is_engaged=_align_handoff_is_engaged,
    request_exit=_align_handoff_request_exit,
    is_settled=_align_handoff_is_settled,
    report_error=_align_handoff_report_error,
)


def _data_context_is_ready() -> bool:
    """런타임 점군과 이전 버전 데이터에 안전하게 접근할 수 있는지 확인합니다."""
    return bool(
        getattr(bpy.data, "objects", None) is not None
        and getattr(bpy.data, "collections", None) is not None
    )


def _cleanup_pending_legacy_data() -> bool:
    """현재 등록 단위에서 아직 처리하지 않은 legacy 소유 데이터만 한 번 정리합니다."""
    global _LEGACY_CLEANUP_PENDING
    if not _LEGACY_CLEANUP_PENDING or not _data_context_is_ready():
        return False
    cleanup_legacy_owned_demo_data()
    _LEGACY_CLEANUP_PENDING = False
    return True


def _initialize_runtime() -> float | None:
    """제한 컨텍스트가 끝난 뒤 UI를 준비하고 필요하면 저장 프로젝트 복원을 예약합니다."""
    if not _REGISTERED:
        return None
    if not _data_context_is_ready():
        return 0.1
    try:
        _cleanup_pending_legacy_data()
        install_draw_handlers()
        sync_marker_pair_list()
        sync_marker_align_log(())
        RUNTIME.workspace_setup_error = ""
        schedule_project_restore_if_available()
        _LOGGER.info("event=runtime_initialized model_count=%s", RUNTIME.model_catalog.model_count)
    except (AttributeError, RuntimeError) as error:
        RUNTIME.workspace_setup_error = f"런타임 초기화에 실패했습니다: {error}"
        _LOGGER.exception("event=runtime_initialization_failed")
    return None


def _schedule_runtime_initialization() -> None:
    """가능하면 즉시 초기화하고 제한 컨텍스트에서는 타이머로 연기합니다."""
    if _data_context_is_ready():
        _initialize_runtime()
    elif not bpy.app.timers.is_registered(_initialize_runtime):
        bpy.app.timers.register(_initialize_runtime, first_interval=0.1)


def _cancel_runtime_initialization() -> None:
    """등록 해제 전에 대기 중인 런타임 초기화 타이머를 제거합니다."""
    if bpy.app.timers.is_registered(_initialize_runtime):
        bpy.app.timers.unregister(_initialize_runtime)


def _append_handler_once(handler_list, callback) -> None:
    """동일 callback을 Blender handler 목록에 한 번만 추가합니다."""
    if callback not in handler_list:
        handler_list.append(callback)


def _remove_handler_if_present(handler_list, callback) -> None:
    """등록된 callback이 있을 때만 Blender handler 목록에서 제거합니다."""
    if callback in handler_list:
        handler_list.remove(callback)


def _prepend_view_menu_once() -> None:
    """A/P 전용 View 메뉴 callback을 중복 없이 등록합니다."""
    global _VIEW_MENU_REGISTERED
    if _VIEW_MENU_REGISTERED:
        return
    bpy.types.VIEW3D_MT_view.prepend(draw_frame_all_view_menu)
    _VIEW_MENU_REGISTERED = True


def _remove_view_menu_if_present() -> None:
    """등록된 A/P 전용 View 메뉴 callback을 예외 없이 제거합니다."""
    global _VIEW_MENU_REGISTERED
    try:
        bpy.types.VIEW3D_MT_view.remove(draw_frame_all_view_menu)
    except (AttributeError, RuntimeError, ValueError):
        pass
    _VIEW_MENU_REGISTERED = False


def _register_addon_keymaps() -> None:
    """A/P 뷰포트 포인트 연속 클릭용 3D View 애드온 키맵을 한 번 등록합니다."""

    if _ADDON_KEYMAPS:
        return
    window_manager = getattr(bpy.context, "window_manager", None)
    keyconfigs = getattr(window_manager, "keyconfigs", None)
    addon_keyconfig = getattr(keyconfigs, "addon", None)
    if addon_keyconfig is None:
        return
    keymap = addon_keyconfig.keymaps.new(
        name="3D View",
        space_type="VIEW_3D",
        region_type="WINDOW",
    )
    item = keymap.keymap_items.new(
        "colmap_align.set_layer_transform_pivot",
        "LEFTMOUSE",
        "PRESS",
        head=True,
    )
    _ADDON_KEYMAPS.append((keymap, item))


def _unregister_addon_keymaps() -> None:
    """등록했던 더블클릭 키맵 항목만 제거하고 다른 애드온 키맵은 유지합니다."""

    for keymap, item in reversed(_ADDON_KEYMAPS):
        try:
            keymap.keymap_items.remove(item)
        except (ReferenceError, RuntimeError, ValueError):
            pass
    _ADDON_KEYMAPS.clear()


def _on_workspace_changed() -> None:
    """작업공간 변경 시 화면 Registry만 갱신하고 세션 종료는 공용 조정기에 맡깁니다."""
    invalidate_registry()
    # 자유로운 탭 이동을 위해 기존 Align Workspace 강제 복귀는 사용하지 않습니다.
    # schedule_workspace_recovery()
    request_redraw()


def _subscribe_workspace_changes() -> None:
    """Window의 작업공간 변경 알림을 중복 없이 구독합니다."""
    bpy.msgbus.clear_by_owner(_MSGBUS_OWNER)
    bpy.msgbus.subscribe_rna(
        key=(bpy.types.Window, "workspace"),
        owner=_MSGBUS_OWNER,
        args=(),
        notify=_on_workspace_changed,
        options={"PERSISTENT"},
    )


@persistent
def _on_load_pre(_unused) -> None:
    """파일 교체 전에 draw handler와 만료 예정 포인터 및 모달 상태를 정리합니다."""
    clear_session_for_file_load()
    cancel_active_import_job()
    stop_marker_modal_timer()
    RUNTIME.clear_marker_command()
    RUNTIME.modal_active = False
    RUNTIME.marker_selection.clear()
    RUNTIME.model_catalog.clear()
    RUNTIME.model_repository.clear()
    sync_marker_pair_list()
    cancel_workspace_setup()
    remove_draw_handlers()
    invalidate_registry()
    RUNTIME.main_window_ref = None
    RUNTIME.point_clouds.clear()
    RUNTIME.gpu_batches.clear()
    _LOGGER.info("event=file_load_pre_runtime_cleared")


@persistent
def _on_save_pre(_unused) -> None:
    """Blender 저장 직전에 dirty 확정 상태만 Text snapshot으로 동기화합니다."""

    try:
        sync_project_state_if_dirty()
    except ProjectPersistenceRuntimeError:
        _LOGGER.exception("event=project_state_sync_failed phase=save_pre")
        raise


@persistent
def _on_load_post(_unused) -> None:
    """새 파일 UI를 준비한 뒤 Text owner가 있으면 비동기 프로젝트 복원을 예약합니다."""
    global _LEGACY_CLEANUP_PENDING
    if not _data_context_is_ready():
        return
    cleanup_legacy_owned_demo_data()
    _LEGACY_CLEANUP_PENDING = False
    install_draw_handlers()
    sync_marker_pair_list()
    sync_marker_align_log(())
    _subscribe_workspace_changes()
    invalidate_registry()
    request_redraw()
    schedule_project_restore_if_available()
    _LOGGER.info("event=file_load_post_runtime_initialized")


def register() -> None:
    """클래스와 속성 및 생명주기 handler를 순서대로 한 번만 등록합니다."""
    global _LEGACY_CLEANUP_PENDING, _REGISTERED
    if _REGISTERED:
        return
    configure_logging()
    _LOGGER.info("event=addon_register_started")
    _LEGACY_CLEANUP_PENDING = True
    _cleanup_pending_legacy_data()
    registered_classes: list[type] = []
    try:
        for cls in CLASSES:
            bpy.utils.register_class(cls)
            registered_classes.append(cls)
        bpy.types.Scene.colmap_align_settings = PointerProperty(type=COLMAPALIGN_PG_settings)
        bpy.types.WindowManager.colmap_align_marker_pairs = PointerProperty(
            type=COLMAPALIGN_PG_marker_pair_list
        )
        bpy.types.WindowManager.colmap_align_marker_align_log = PointerProperty(
            type=COLMAPALIGN_PG_marker_align_log_list
        )
        _append_handler_once(bpy.app.handlers.load_pre, _on_load_pre)
        _append_handler_once(bpy.app.handlers.load_post, _on_load_post)
        _append_handler_once(bpy.app.handlers.save_pre, _on_save_pre)
        _prepend_view_menu_once()
        _register_addon_keymaps()
        _subscribe_workspace_changes()
        register_workspace_handoff_adapter(_ALIGN_HANDOFF_ADAPTER)
        _REGISTERED = True
        _schedule_runtime_initialization()
        _LOGGER.info("event=addon_register_completed")
    except Exception:
        _REGISTERED = False
        _cancel_runtime_initialization()
        _cleanup_pending_legacy_data()
        remove_draw_handlers()
        _remove_view_menu_if_present()
        _unregister_addon_keymaps()
        unregister_workspace_handoff_adapter("align")
        bpy.msgbus.clear_by_owner(_MSGBUS_OWNER)
        _remove_handler_if_present(bpy.app.handlers.load_post, _on_load_post)
        _remove_handler_if_present(bpy.app.handlers.load_pre, _on_load_pre)
        _remove_handler_if_present(bpy.app.handlers.save_pre, _on_save_pre)
        if hasattr(bpy.types.Scene, "colmap_align_settings"):
            del bpy.types.Scene.colmap_align_settings
        if hasattr(bpy.types.WindowManager, "colmap_align_marker_pairs"):
            del bpy.types.WindowManager.colmap_align_marker_pairs
        if hasattr(bpy.types.WindowManager, "colmap_align_marker_align_log"):
            del bpy.types.WindowManager.colmap_align_marker_align_log
        for cls in reversed(registered_classes):
            try:
                bpy.utils.unregister_class(cls)
            except RuntimeError:
                pass
        _LOGGER.exception("event=addon_register_failed")
        shutdown_logging()
        raise


def unregister() -> None:
    """모달·handler·런타임 참조·속성·클래스를 등록 역순으로 모두 해제합니다."""
    global _REGISTERED
    was_registered = _REGISTERED
    if was_registered:
        try:
            sync_project_state_if_dirty()
        except ProjectPersistenceRuntimeError:
            _LOGGER.exception("event=project_state_sync_failed phase=unregister")
            raise
    _REGISTERED = False
    _LOGGER.info("event=addon_unregister_started")
    _cancel_runtime_initialization()
    cancel_project_restore_for_file_load()
    _cleanup_pending_legacy_data()
    cancel_active_import_job()
    stop_marker_modal_timer()
    restore_session_for_unregister()
    RUNTIME.clear_marker_command()
    RUNTIME.modal_active = False
    RUNTIME.marker_selection.clear()
    cancel_workspace_setup()
    remove_draw_handlers()
    _remove_view_menu_if_present()
    _unregister_addon_keymaps()
    unregister_workspace_handoff_adapter("align")
    bpy.msgbus.clear_by_owner(_MSGBUS_OWNER)
    _remove_handler_if_present(bpy.app.handlers.load_post, _on_load_post)
    _remove_handler_if_present(bpy.app.handlers.load_pre, _on_load_pre)
    _remove_handler_if_present(bpy.app.handlers.save_pre, _on_save_pre)
    if hasattr(bpy.types.Scene, "colmap_align_settings"):
        del bpy.types.Scene.colmap_align_settings
    if hasattr(bpy.types.WindowManager, "colmap_align_marker_pairs"):
        del bpy.types.WindowManager.colmap_align_marker_pairs
    if hasattr(bpy.types.WindowManager, "colmap_align_marker_align_log"):
        del bpy.types.WindowManager.colmap_align_marker_align_log
    if was_registered:
        for cls in reversed(CLASSES):
            try:
                bpy.utils.unregister_class(cls)
            except RuntimeError:
                pass
    RUNTIME.clear_transient()
    _LOGGER.info("event=addon_unregister_completed")
    shutdown_logging()


def is_registered() -> bool:
    """애드온 클래스 생명주기가 현재 등록 상태인지 반환합니다."""
    return _REGISTERED
