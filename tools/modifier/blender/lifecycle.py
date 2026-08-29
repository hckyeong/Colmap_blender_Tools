"""Extension 클래스와 Scene 속성을 대칭적으로 등록하고 해제합니다."""

import bpy
from bpy.props import PointerProperty

try:
    from ....shared.blender_runtime.workspace_factory import source_workspace_from_metadata
    from ....shared.blender_runtime.workspace_handoff import (
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

from .axis_display import (
    cancel_axis_preset_migration,
    schedule_axis_preset_migration,
)
from .delete_guard import (
    CLASSES as DELETE_GUARD_CLASSES,
    register_delete_guard_keymaps,
    unregister_delete_guard_keymaps,
)
from .camera_edit_session import clear_camera_edit_sessions
from .camera_review_keymap import (
    register_camera_review_keymap,
    unregister_camera_review_keymap,
)
from .camera_review_timeline import (
    detach_modifier_timeline_markers_for_scenes,
    modifier_marker_session_owned,
    modifier_review_markers_attached,
    release_modifier_marker_session,
    register_frame_change_handler,
    unregister_frame_change_handler,
)
from .edit_coordinator import (
    clear_edit_sessions,
    register_edit_mode_handler,
    unregister_edit_mode_handler,
)
from .export_progress_session import clear_export_progress_sessions
from .import_progress_session import clear_import_progress_sessions
from .operators_import import CLASSES as OPERATOR_CLASSES
from .operators_export import CLASSES as EXPORT_OPERATOR_CLASSES
from .operators_export_progress import CLASSES as EXPORT_PROGRESS_OPERATOR_CLASSES
from .operators_camera_review import CLASSES as CAMERA_REVIEW_OPERATOR_CLASSES
from .operators_camera_edit_mode import CLASSES as CAMERA_EDIT_MODE_OPERATOR_CLASSES
from .operators_point_edit_mode import CLASSES as POINT_EDIT_MODE_OPERATOR_CLASSES
from .operators_point_selection import CLASSES as POINT_SELECTION_OPERATOR_CLASSES
from .operators_modifier_workspace import CLASSES as MODIFIER_WORKSPACE_OPERATOR_CLASSES
from .panels import CLASSES as PANEL_CLASSES
from .point_display import register_point_scale_sync, unregister_point_scale_sync
from .properties import CLASSES as PROPERTY_CLASSES
from .properties import COLMAP_PG_import_settings
from .state_handlers import register_state_handlers, unregister_state_handlers
from .status_display import CLASSES as STATUS_UI_CLASSES
from .status_display import (
    clear_status_display_runtime,
    initialize_status_display,
)
from .modifier_workspace import (
    ModifierWorkspaceError,
    cancel_workspace_tasks,
    configure_modifier_workspace,
    exit_modifier_workspace,
    register_modifier_workspace_runtime,
    request_redraw,
    unregister_modifier_workspace_runtime,
)
from .modifier_session import (
    SESSION,
    SESSION_ACTIVE,
    SESSION_ENTERING,
    SESSION_EXITING,
    SESSION_INACTIVE,
    abort_session_entry,
    session_target_window,
)

CLASSES = (
    *PROPERTY_CLASSES,
    *STATUS_UI_CLASSES,
    *OPERATOR_CLASSES,
    *POINT_EDIT_MODE_OPERATOR_CLASSES,
    *CAMERA_EDIT_MODE_OPERATOR_CLASSES,
    *POINT_SELECTION_OPERATOR_CLASSES,
    *CAMERA_REVIEW_OPERATOR_CLASSES,
    *EXPORT_PROGRESS_OPERATOR_CLASSES,
    *EXPORT_OPERATOR_CLASSES,
    *DELETE_GUARD_CLASSES,
    *MODIFIER_WORKSPACE_OPERATOR_CLASSES,
    *PANEL_CLASSES,
)

_REGISTERED = False


def _modifier_handoff_start(context) -> None:
    """Modifier의 기존 Workspace 구성 계약을 공용 전환 완료 뒤 실행합니다."""

    configure_modifier_workspace(context, force_restore=False)


def _modifier_handoff_is_engaged(window) -> bool:
    """지정 Window에 Modifier 진입·활성·종료 세션이 남아 있는지 반환합니다."""

    target_window = session_target_window()
    return bool(
        SESSION.phase != SESSION_INACTIVE
        and target_window is not None
        and target_window.as_pointer() == window.as_pointer()
    )


def _modifier_handoff_request_exit(context, window) -> None:
    """Modifier 단계에 맞춰 진입 취소나 정식 종료 또는 Source 복귀를 실행합니다."""

    if SESSION.phase == SESSION_ACTIVE:
        exit_modifier_workspace(context)
        return
    if SESSION.phase == SESSION_ENTERING:
        cancel_workspace_tasks()
        abort_session_entry("다른 Workspace 전환으로 Modifier 진입을 취소했습니다.")
        return
    if SESSION.phase == SESSION_EXITING:
        return
    current = getattr(window, "workspace", None)
    if workspace_tool_id(current) != "modifier":
        return
    source = source_workspace_from_metadata(current)
    if source is None:
        raise ModifierWorkspaceError(
            "Modifier Workspace의 원래 Source Workspace를 찾지 못했습니다."
        )
    window.workspace = source


def _modifier_handoff_is_settled(window) -> bool:
    """Modifier 세션과 활성 Workspace가 모두 대상 Window에서 해제됐는지 반환합니다."""

    scene = getattr(window, "scene", None)
    return bool(
        SESSION.phase == SESSION_INACTIVE
        and workspace_tool_id(getattr(window, "workspace", None)) != "modifier"
        and (
            scene is None
            or (
                not modifier_marker_session_owned(scene)
                and not modifier_review_markers_attached(scene)
            )
        )
    )


def _modifier_handoff_report_error(message: str) -> None:
    """비동기 전환 실패 원인을 Modifier 세션과 Scene 상태에 기록합니다."""

    SESSION.status_message = str(message)
    settings = getattr(getattr(bpy.context, "scene", None), "colmap_import", None)
    if settings is not None:
        settings.status = str(message)
        settings.status_kind = "ERROR"
    request_redraw()


def _detach_modifier_markers_before_unregister() -> None:
    """Runtime 해제 전에 모든 Scene의 검증된 Marker를 분리하고 Modifier lease를 반납합니다."""

    scenes = tuple(getattr(bpy.data, "scenes", ()))
    detach_modifier_timeline_markers_for_scenes(scenes)
    for scene in scenes:
        if modifier_marker_session_owned(scene):
            release_modifier_marker_session(scene)


_MODIFIER_HANDOFF_ADAPTER = WorkspaceHandoffAdapter(
    tool_id="modifier",
    label="COLMAP Modifier",
    start=_modifier_handoff_start,
    is_engaged=_modifier_handoff_is_engaged,
    request_exit=_modifier_handoff_request_exit,
    is_settled=_modifier_handoff_is_settled,
    report_error=_modifier_handoff_report_error,
)


def register():
    """Extension 구성요소와 Modifier Workspace 생명주기를 등록합니다."""

    global _REGISTERED
    if _REGISTERED:
        return
    registered_classes: list[type] = []
    property_registered = False
    point_scale_registered = False
    edit_handler_registered = False
    state_handlers_registered = False
    frame_handler_registered = False
    camera_review_keymap_registered = False
    delete_keymaps_registered = False
    workspace_runtime_registered = False
    try:
        for cls in CLASSES:
            bpy.utils.register_class(cls)
            registered_classes.append(cls)
        bpy.types.Scene.colmap_import = PointerProperty(type=COLMAP_PG_import_settings)
        property_registered = True
        schedule_axis_preset_migration()
        initialize_status_display(bpy.context)
        register_point_scale_sync()
        point_scale_registered = True
        register_edit_mode_handler()
        edit_handler_registered = True
        register_state_handlers()
        state_handlers_registered = True
        register_frame_change_handler()
        frame_handler_registered = True
        register_camera_review_keymap()
        camera_review_keymap_registered = True
        register_delete_guard_keymaps()
        delete_keymaps_registered = True
        workspace_runtime_registered = True
        register_modifier_workspace_runtime()
        register_workspace_handoff_adapter(_MODIFIER_HANDOFF_ADAPTER)
        _REGISTERED = True
    except Exception:
        unregister_workspace_handoff_adapter("modifier")
        if workspace_runtime_registered:
            unregister_modifier_workspace_runtime()
        if delete_keymaps_registered:
            unregister_delete_guard_keymaps()
        if camera_review_keymap_registered:
            unregister_camera_review_keymap()
        if frame_handler_registered:
            unregister_frame_change_handler()
        if state_handlers_registered:
            unregister_state_handlers()
        if edit_handler_registered:
            unregister_edit_mode_handler()
        if point_scale_registered:
            unregister_point_scale_sync()
        cancel_axis_preset_migration()
        clear_status_display_runtime()
        if property_registered and hasattr(bpy.types.Scene, "colmap_import"):
            del bpy.types.Scene.colmap_import
        for cls in reversed(registered_classes):
            try:
                bpy.utils.unregister_class(cls)
            except RuntimeError:
                pass
        raise


def unregister():
    """Modifier Workspace를 복귀시킨 뒤 구성요소를 역순으로 해제합니다."""

    global _REGISTERED
    if not _REGISTERED:
        return
    _detach_modifier_markers_before_unregister()
    _REGISTERED = False
    from .camera_filter_queue import (
        clear_pending_runtime,
        release_pending_overlay,
    )
    from .camera_review_session import clear_review_sessions
    from .runtime_state import clear_runtime
    from .selection import clear_snapshots
    unregister_workspace_handoff_adapter("modifier")
    unregister_modifier_workspace_runtime()
    cancel_axis_preset_migration()
    unregister_delete_guard_keymaps()
    unregister_camera_review_keymap()
    clear_import_progress_sessions()
    clear_export_progress_sessions()
    clear_status_display_runtime()
    unregister_frame_change_handler()
    unregister_state_handlers()
    unregister_edit_mode_handler()
    release_pending_overlay(bpy.context)
    clear_review_sessions()
    clear_pending_runtime()
    clear_edit_sessions(bpy.context)
    clear_camera_edit_sessions(bpy.context)
    clear_snapshots()
    clear_runtime()
    unregister_point_scale_sync()
    if hasattr(bpy.types.Scene, "colmap_import"):
        del bpy.types.Scene.colmap_import
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)
