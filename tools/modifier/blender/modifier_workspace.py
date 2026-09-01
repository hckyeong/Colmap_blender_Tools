"""COLMAP Modifier 전용 Workspace의 생성·복구·종료를 관리합니다."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import bpy
from bpy.app.handlers import persistent

try:
    from ....shared.blender_runtime.workspace_factory import (
        WorkspaceFactoryError,
        append_seed_workspace,
        delete_owned_workspace,
        mark_workspace_failed,
        mark_workspace_ready,
        screen_matches_workspace,
        workspace_matches_spec,
    )
except ImportError:
    from shared.blender_runtime.workspace_factory import (
        WorkspaceFactoryError,
        append_seed_workspace,
        delete_owned_workspace,
        mark_workspace_failed,
        mark_workspace_ready,
        screen_matches_workspace,
        workspace_matches_spec,
    )

from .modifier_session import (
    CONTROL_PANEL_CATEGORY,
    MAIN_SCREEN_OWNER_KEY,
    SESSION,
    SESSION_ACTIVE,
    SESSION_ENTERING,
    SESSION_INACTIVE,
    WORKSPACE_NAME,
    WORKSPACE_SOURCE_KEY,
    abort_session_entry,
    activate_session,
    begin_session_entry,
    begin_session_exit,
    bind_modifier_workspace,
    complete_session_exit,
    is_legacy_modifier_workspace,
    is_owned_modifier_workspace,
    is_session_active_for_window,
    is_session_owned_by_window,
    restore_source_workspace,
    session_modifier_workspace,
    session_source_workspace,
    session_target_window,
    window_is_live,
)
from .modifier_data_visibility import activate_modifier_data_view_layer
from .modifier_viewport import (
    hide_default_startup_objects,
    normalize_modifier_viewport,
    restore_default_startup_objects,
    set_modifier_perspective_view,
)
from .modifier_workspace_contract import MODIFIER_WORKSPACE_SPEC


SETUP_INTERVAL_SECONDS = 0.05
LAYOUT_READY_LIMIT = 20
SIDEBAR_ACTIVATION_LIMIT = 12
AREA_EDGE_GAP_TOLERANCE = 5
TIMELINE_HEIGHT_FACTOR = 0.2


class ModifierWorkspaceError(RuntimeError):
    """사용자 Workspace를 수정하지 않고 전환을 중단할 때 사용합니다."""


@dataclass
class WorkspaceSetupTask:
    """UI 이벤트 사이에서 전용 Screen 구성을 한 단계씩 진행합니다."""

    main_window: Any
    phase: str
    generation: int
    source_screen_pointer: int
    wait_for_screen_change: bool
    created_workspace: bool = False
    force_restore: bool = False
    screen_wait_attempts: int = 0


@dataclass
class RuntimeRestoreTask:
    """활성 Modifier Workspace에 연결할 증분 Runtime 복원 상태를 보관합니다."""

    window: Any
    area: Any
    scene_pointer: int
    job: Any


_SETUP_TASK: WorkspaceSetupTask | None = None
_RUNTIME_RESTORE_TASK: RuntimeRestoreTask | None = None
_RECOVERY_ATTEMPTS = 0
_SIDEBAR_ATTEMPTS = 0
_MSGBUS_OWNER = object()


def _pointer(value: Any) -> int:
    """Blender RNA 객체의 유효한 런타임 포인터를 반환합니다."""

    if value is None:
        return 0
    try:
        return int(value.as_pointer())
    except (AttributeError, ReferenceError, RuntimeError):
        return 0


def _set_status(message: str, *, error: bool = False) -> None:
    """현재 Scene의 상태 패널에 Workspace 결과를 기록합니다."""

    scene = getattr(bpy.context, "scene", None)
    settings = getattr(scene, "colmap_import", None)
    if settings is None:
        return
    settings.status = str(message)
    settings.status_kind = "ERROR" if error else "SUCCESS"


def request_redraw() -> None:
    """열려 있는 모든 Area에 안전한 다시 그리기를 요청합니다."""

    manager = getattr(bpy.context, "window_manager", None)
    if manager is None:
        return
    for window in manager.windows:
        try:
            for area in window.screen.areas:
                area.tag_redraw()
        except (AttributeError, ReferenceError, RuntimeError):
            continue


def _screen_pointer(window: Any) -> int:
    """Window에 연결된 현재 Screen 포인터를 반환합니다."""

    return _pointer(getattr(window, "screen", None))


def _mark_main_screen(window: Any) -> None:
    """현재 Screen을 COLMAP Modifier 소유 Control Screen으로 표시합니다."""

    window.screen[MAIN_SCREEN_OWNER_KEY] = True


def find_main_window(window_manager: Any, preferred: Any = None):
    """애드온 소유 Workspace와 Screen을 표시하는 Window를 찾습니다."""

    candidates = list(getattr(window_manager, "windows", ()))
    for window in (preferred, session_target_window(), *candidates):
        if window is None or not window_is_live(window):
            continue
        try:
            if (
                is_owned_modifier_workspace(window.workspace)
                and window.screen.get(MAIN_SCREEN_OWNER_KEY, False)
            ):
                return window
        except (AttributeError, ReferenceError, RuntimeError):
            continue
    return None


def _ensure_dedicated_workspace(context: Any):
    """현행 Workspace를 재사용하거나 독립 Seed에서 새 Workspace를 Append합니다."""

    window = getattr(context, "window", None)
    if window is None:
        raise ModifierWorkspaceError("COLMAP Modifier로 전환할 현재 Window가 없습니다.")
    source_workspace = window.workspace
    manager = getattr(context, "window_manager", None)
    windows = tuple(getattr(manager, "windows", ()))
    all_owned = [
        workspace
        for workspace in bpy.data.workspaces
        if workspace_matches_spec(workspace, MODIFIER_WORKSPACE_SPEC, require_ready=False)
    ]
    current_owned = [
        workspace
        for workspace in all_owned
        if workspace_matches_spec(workspace, MODIFIER_WORKSPACE_SPEC, require_ready=True)
    ]
    current_owned_pointers = {_pointer(workspace) for workspace in current_owned}
    stale_owned = [
        workspace
        for workspace in all_owned
        if _pointer(workspace) not in current_owned_pointers
    ]
    if len(current_owned) > 1:
        raise ModifierWorkspaceError("현행 COLMAP Modifier Workspace가 둘 이상 존재합니다.")
    if current_owned and any(
        _pointer(candidate) != _pointer(window)
        and _pointer(getattr(candidate, "workspace", None)) == _pointer(current_owned[0])
        for candidate in windows
    ):
        raise ModifierWorkspaceError(
            "현행 COLMAP Modifier Workspace를 다른 Blender Window가 사용 중입니다."
        )
    for stale_workspace in stale_owned:
        if any(
            _pointer(candidate) != _pointer(window)
            and _pointer(getattr(candidate, "workspace", None)) == _pointer(stale_workspace)
            for candidate in windows
        ):
            raise ModifierWorkspaceError(
                "미완료 COLMAP Modifier Workspace를 다른 Blender Window가 사용 중이므로 정리할 수 없습니다."
            )

    legacy_workspaces = [
        workspace for workspace in bpy.data.workspaces if is_legacy_modifier_workspace(workspace)
    ]
    for legacy_workspace in legacy_workspaces:
        other_window = next(
            (
                candidate
                for candidate in windows
                if _pointer(candidate) != _pointer(window)
                and _pointer(getattr(candidate, "workspace", None)) == _pointer(legacy_workspace)
            ),
            None,
        )
        if other_window is not None:
            raise ModifierWorkspaceError(
                "Legacy COLMAP Modifier Workspace를 다른 Blender Window가 사용 중이므로 교체할 수 없습니다."
            )

    stale_owned_pointers = {_pointer(workspace) for workspace in stale_owned}
    if _pointer(source_workspace) in stale_owned_pointers:
        restored_source = session_source_workspace()
        if restored_source is None:
            raise ModifierWorkspaceError(
                "미완료 COLMAP Modifier Workspace의 원래 Workspace를 찾을 수 없습니다."
            )
        window.workspace = restored_source
        source_workspace = restored_source
    elif is_legacy_modifier_workspace(source_workspace):
        source_name = str(source_workspace.get(WORKSPACE_SOURCE_KEY, "") or "")
        restored_source = bpy.data.workspaces.get(source_name)
        if restored_source is None or is_legacy_modifier_workspace(restored_source):
            raise ModifierWorkspaceError(
                "Legacy COLMAP Modifier Workspace의 원래 Workspace를 찾을 수 없습니다."
            )
        window.workspace = restored_source
        source_workspace = restored_source

    for stale_workspace in stale_owned:
        if not delete_owned_workspace(
            context,
            window,
            stale_workspace,
            MODIFIER_WORKSPACE_SPEC,
            restore_workspace=source_workspace,
        ):
            raise ModifierWorkspaceError(
                "미완료 COLMAP Modifier Workspace를 안전하게 정리하지 못했습니다."
            )

    for legacy_workspace in legacy_workspaces:
        _delete_legacy_workspace(context, window, legacy_workspace, source_workspace)

    if current_owned:
        workspace = current_owned[0]
        window.workspace = workspace
        return workspace, window, False

    try:
        workspace = append_seed_workspace(
            context,
            window,
            MODIFIER_WORKSPACE_SPEC,
            source_workspace,
        )
    except WorkspaceFactoryError as error:
        raise ModifierWorkspaceError(str(error)) from error
    return workspace, window, True


def _delete_legacy_workspace(
    context: Any,
    window: Any,
    legacy_workspace: Any,
    source_workspace: Any,
) -> None:
    """다른 창이 쓰지 않는 Legacy Workspace를 Operator로 삭제하고 Source로 복귀합니다."""

    legacy_pointer = _pointer(legacy_workspace)
    try:
        window.workspace = legacy_workspace
        with context.temp_override(window=window):
            result = bpy.ops.workspace.delete()
        deleted = "FINISHED" in result and not any(
            _pointer(candidate) == legacy_pointer for candidate in bpy.data.workspaces
        )
        if not deleted:
            raise ModifierWorkspaceError("Legacy Workspace 삭제가 완료되지 않았습니다.")
    except (AttributeError, ReferenceError, RuntimeError) as error:
        raise ModifierWorkspaceError(
            f"Legacy COLMAP Modifier Workspace 교체에 실패했습니다: {error}"
        ) from error
    finally:
        try:
            window.workspace = source_workspace
        except (AttributeError, ReferenceError, RuntimeError):
            pass


def _areas_are_adjacent(first: Any, second: Any) -> bool:
    """두 Area가 결합 가능한 전체 경계를 공유하는지 확인합니다."""

    first_right = first.x + first.width
    second_right = second.x + second.width
    first_top = first.y + first.height
    second_top = second.y + second.height
    vertical_touch = (
        abs(first_right - second.x) <= AREA_EDGE_GAP_TOLERANCE
        or abs(second_right - first.x) <= AREA_EDGE_GAP_TOLERANCE
    )
    same_vertical_span = (
        abs(first.y - second.y) <= 1 and abs(first.height - second.height) <= 2
    )
    horizontal_touch = (
        abs(first_top - second.y) <= AREA_EDGE_GAP_TOLERANCE
        or abs(second_top - first.y) <= AREA_EDGE_GAP_TOLERANCE
    )
    same_horizontal_span = (
        abs(first.x - second.x) <= 1 and abs(first.width - second.width) <= 2
    )
    return (vertical_touch and same_vertical_span) or (
        horizontal_touch and same_horizontal_span
    )


def _area_center(area: Any) -> tuple[int, int]:
    """Area Join에 사용할 내부 중심 좌표를 계산합니다."""

    return (
        area.x + max(area.width // 2, 1),
        area.y + max(area.height // 2, 1),
    )


def _join_candidate_pairs(areas: list[Any]) -> tuple[tuple[Any, Any], ...]:
    """인접 Area를 우선하는 결합 후보 순서를 반환합니다."""

    pairs = tuple(
        (source, target)
        for source in sorted(areas, key=lambda item: item.width * item.height)
        for target in areas
        if source != target
    )
    return tuple(sorted(pairs, key=lambda pair: 0 if _areas_are_adjacent(*pair) else 1))


def _join_one_area(context: Any, window: Any) -> bool:
    """애드온 소유 Screen에서 Area 한 쌍을 결합합니다."""

    if not window.screen.get(MAIN_SCREEN_OWNER_KEY, False):
        raise ModifierWorkspaceError("사용자 소유 Screen의 Area는 결합할 수 없습니다.")
    areas = list(window.screen.areas)
    if len(areas) <= 1:
        return False
    for source, target in _join_candidate_pairs(areas):
        before_count = len(window.screen.areas)
        try:
            with context.temp_override(window=window, screen=window.screen, area=source):
                bpy.ops.screen.area_join(
                    source_xy=_area_center(source),
                    target_xy=_area_center(target),
                )
        except RuntimeError:
            continue
        if len(window.screen.areas) == before_count - 1:
            return True
    summary = ", ".join(
        f"{area.type}({area.x},{area.y},{area.width},{area.height})" for area in areas
    )
    raise ModifierWorkspaceError(f"결합 가능한 Area를 찾지 못했습니다: {summary}")


def _workspace_layout_areas(window: Any) -> tuple[Any, Any] | None:
    """상단 3D View와 하단 Timeline으로 구성된 정상 Layout을 반환합니다."""

    areas = list(window.screen.areas)
    view_areas = [area for area in areas if area.type == "VIEW_3D"]
    timeline_areas = [
        area
        for area in areas
        if area.type == "DOPESHEET_EDITOR"
        and getattr(area, "ui_type", "") == "TIMELINE"
    ]
    if len(areas) != 2 or len(view_areas) != 1 or len(timeline_areas) != 1:
        return None
    view_area = view_areas[0]
    timeline_area = timeline_areas[0]
    if view_area.y <= timeline_area.y:
        return None
    return view_area, timeline_area


def _control_area(window: Any):
    """정상 Layout의 상단 VIEW_3D Control Area를 반환합니다."""

    layout_areas = _workspace_layout_areas(window)
    return layout_areas[0] if layout_areas is not None else None


def _split_timeline_area(context: Any, window: Any) -> None:
    """단일 소유 Area를 상단 3D View와 하단 Timeline으로 분할합니다."""

    if not window.screen.get(MAIN_SCREEN_OWNER_KEY, False):
        raise ModifierWorkspaceError("사용자 소유 Screen을 분할할 수 없습니다.")
    areas = list(window.screen.areas)
    if len(areas) != 1:
        raise ModifierWorkspaceError("Timeline 분할 전 Area가 하나가 아닙니다.")
    source_area = areas[0]
    with context.temp_override(window=window, screen=window.screen, area=source_area):
        result = bpy.ops.screen.area_split(
            direction="HORIZONTAL",
            factor=TIMELINE_HEIGHT_FACTOR,
        )
    if "FINISHED" not in result or len(window.screen.areas) != 2:
        raise ModifierWorkspaceError("하단 Timeline Area 생성에 실패했습니다.")
    refreshed_areas = list(window.screen.areas)
    timeline_area = min(refreshed_areas, key=lambda area: area.y)
    control_area = max(refreshed_areas, key=lambda area: area.y)
    timeline_area.type = "DOPESHEET_EDITOR"
    timeline_area.ui_type = "TIMELINE"
    control_area.type = "VIEW_3D"


def _window_region(area: Any):
    """3D View에서 Operator Context에 사용할 WINDOW Region을 찾습니다."""

    return next((region for region in area.regions if region.type == "WINDOW"), None)


def configure_sidebar(window: Any) -> bool:
    """Control View Sidebar를 열고 COLMAP Modifier Category를 활성화합니다."""

    area = _control_area(window)
    if area is None:
        raise ModifierWorkspaceError("COLMAP Modifier Control View가 준비되지 않았습니다.")
    space = area.spaces.active
    space.show_region_ui = True
    ui_region = next((region for region in area.regions if region.type == "UI"), None)
    if ui_region is None:
        raise ModifierWorkspaceError("Control View의 Sidebar Region을 찾지 못했습니다.")
    try:
        if ui_region.is_property_readonly("active_panel_category"):
            return False
        ui_region.active_panel_category = CONTROL_PANEL_CATEGORY
        area.tag_redraw()
        return ui_region.active_panel_category == CONTROL_PANEL_CATEGORY
    except (AttributeError, ReferenceError, RuntimeError, TypeError):
        return False


def _rehydrate_viewport_runtime(window: Any, area: Any, state: Any) -> tuple[str, ...]:
    """현재 Control View에 Review와 Pending 파생 상태를 다시 연결합니다."""

    region = _window_region(area)
    if region is None:
        raise ModifierWorkspaceError("Control View의 WINDOW Region을 찾지 못했습니다.")
    warnings: list[str] = []
    with bpy.context.temp_override(
        window=window,
        screen=window.screen,
        area=area,
        region=region,
    ):
        settings = getattr(bpy.context.scene, "colmap_import", None)
        if settings is None or not settings.dataset_uuid or not settings.active_model_directory:
            return ()
        from .camera_filter_queue import restore_pending_filters
        from .camera_review_session import (
            ensure_review_session,
            update_review_background_from_settings,
        )

        review_session = ensure_review_session(bpy.context, state=state)
        restore_pending_filters(
            bpy.context,
            state=state,
            session=review_session,
        )
        update_review_background_from_settings(settings, bpy.context)
        if settings.warning_summary:
            warnings.extend(settings.warning_summary.splitlines())
    return tuple(warnings)


def _record_runtime_restore_error(scene: Any, error: Exception) -> None:
    """Runtime 오류를 경고와 상태에 기록하되 활성 Workspace는 그대로 유지합니다."""

    settings = getattr(scene, "colmap_import", None)
    if settings is None:
        return
    warning = f"Viewport Runtime 복원을 건너뜁니다: {error}"
    existing = tuple(
        line.strip() for line in str(settings.warning_summary or "").splitlines() if line.strip()
    )
    settings.warning_summary = "\n".join(dict.fromkeys((*existing, warning)))
    settings.status = warning
    settings.status_kind = "ERROR"


def _process_runtime_restore() -> float | None:
    """활성 Workspace의 Runtime을 전진시키고 완료 뒤 파생 UI를 한 번 연결합니다."""

    global _RUNTIME_RESTORE_TASK
    task = _RUNTIME_RESTORE_TASK
    if task is None:
        return None
    window = task.window
    scene = getattr(window, "scene", None)
    if (
        scene is None
        or _pointer(scene) != task.scene_pointer
        or SESSION.phase != SESSION_ACTIVE
        or not is_session_active_for_window(window)
    ):
        _RUNTIME_RESTORE_TASK = None
        return None
    settings = getattr(scene, "colmap_import", None)
    try:
        if task.job.failed:
            raise task.job.error or ModifierWorkspaceError(
                "Modifier Runtime 복원이 완료 전에 중단되었습니다."
            )
        if not task.job.done:
            progress = task.job.advance(20.0)
            settings.status = (
                f"Modifier Runtime 복원 중: {float(progress.fraction) * 100.0:.1f}% · "
                f"{progress.message}"
            )
            settings.status_kind = "IMPORTING"
            request_redraw()
            return SETUP_INTERVAL_SECONDS
        state = task.job.result
        if state is None:
            raise ModifierWorkspaceError("완료된 Modifier Runtime 복원 결과가 없습니다.")
        _rehydrate_viewport_runtime(window, task.area, state)
        _activate_modifier_marker_session(window)
        settings.status = "COLMAP Modifier Runtime과 Review 연결이 준비되었습니다."
        settings.status_kind = "SUCCESS"
    except (
        AttributeError,
        ReferenceError,
        RuntimeError,
        TypeError,
        ValueError,
        ModifierWorkspaceError,
    ) as error:
        _record_runtime_restore_error(scene, error)
        try:
            from .camera_review_timeline import release_modifier_marker_session

            release_modifier_marker_session(scene)
        except (AttributeError, ReferenceError, RuntimeError, TypeError, ValueError):
            pass
    _RUNTIME_RESTORE_TASK = None
    request_redraw()
    return None


def _schedule_runtime_restore(window: Any, area: Any) -> None:
    """Runtime이 필요한 Modifier Workspace에서 공유 복원 Job Timer를 예약합니다."""

    global _RUNTIME_RESTORE_TASK
    scene = window.scene
    settings = getattr(scene, "colmap_import", None)
    if settings is None or not settings.dataset_uuid or not settings.active_model_directory:
        _activate_modifier_marker_session(window)
        _set_status("COLMAP Modifier Workspace가 준비되었습니다.")
        return
    from .runtime_state import cached_runtime, cached_runtime_matches_scene, request_runtime_restore

    if cached_runtime_matches_scene(scene):
        state = cached_runtime(str(settings.dataset_uuid))
        if state is None:
            raise ModifierWorkspaceError("Modifier Runtime Cache를 찾지 못했습니다.")
        _rehydrate_viewport_runtime(window, area, state)
        _activate_modifier_marker_session(window)
        _set_status("COLMAP Modifier Runtime과 Review 연결이 준비되었습니다.")
        return
    job = request_runtime_restore(scene)
    _RUNTIME_RESTORE_TASK = RuntimeRestoreTask(window, area, _pointer(scene), job)
    settings.status = "Modifier Runtime 복원 중: 0.0%"
    settings.status_kind = "IMPORTING"
    if not bpy.app.timers.is_registered(_process_runtime_restore):
        bpy.app.timers.register(
            _process_runtime_restore,
            first_interval=SETUP_INTERVAL_SECONDS,
        )


def _complete_workspace_activation(window: Any) -> None:
    """Control View를 먼저 활성화하고 필요한 Runtime 복원을 별도 Timer로 시작합니다."""

    area = _control_area(window)
    if area is None:
        raise ModifierWorkspaceError("COLMAP Modifier Control View 구성이 완료되지 않았습니다.")
    workspace = session_modifier_workspace()
    if workspace is None or not screen_matches_workspace(window.screen, workspace):
        raise ModifierWorkspaceError("Modifier Workspace와 활성 Screen의 소유 UUID가 일치하지 않습니다.")
    activate_modifier_data_view_layer(window)
    hide_default_startup_objects(window)
    set_modifier_perspective_view(area)
    normalize_modifier_viewport(window, area)
    if not configure_sidebar(window):
        schedule_sidebar_activation()
    mark_workspace_ready(workspace, window.screen, MODIFIER_WORKSPACE_SPEC)
    activate_session(window, area)
    try:
        _schedule_runtime_restore(window, area)
    except (AttributeError, ReferenceError, RuntimeError, TypeError, ValueError) as error:
        _record_runtime_restore_error(window.scene, error)
    request_redraw()


def _abort_workspace_setup(message: str, task: WorkspaceSetupTask | None) -> None:
    """실패 Workspace를 표시하고 Source 복귀와 신규 ID 정리를 수행합니다."""

    target_window = session_target_window()
    workspace = session_modifier_workspace()
    source_workspace = session_source_workspace()
    if target_window is not None:
        restore_default_startup_objects(target_window)
    if workspace is not None:
        try:
            mark_workspace_failed(workspace)
        except (AttributeError, ReferenceError, RuntimeError, WorkspaceFactoryError):
            pass
    restore_source_workspace()
    if task is not None and task.created_workspace and workspace is not None:
        try:
            delete_owned_workspace(
                bpy.context,
                target_window,
                workspace,
                MODIFIER_WORKSPACE_SPEC,
                restore_workspace=source_workspace,
            )
        except (AttributeError, ReferenceError, RuntimeError, WorkspaceFactoryError):
            pass
    abort_session_entry(message)


def _process_workspace_setup() -> float | None:
    """Timer Tick마다 Screen 구성의 한 단계만 수행합니다."""

    global _SETUP_TASK
    task = _SETUP_TASK
    if task is None:
        return None
    window = task.main_window
    try:
        if task.generation != SESSION.generation:
            raise ModifierWorkspaceError("이전 세대의 Workspace 구성 작업입니다.")
        if not window_is_live(window):
            raise ModifierWorkspaceError("Workspace 구성 중 대상 Window가 닫혔습니다.")
        if not is_owned_modifier_workspace(window.workspace):
            raise ModifierWorkspaceError("Workspace 구성 중 전용 Workspace가 변경되었습니다.")

        if task.phase == "WAIT_SCREEN":
            if (
                task.wait_for_screen_change
                and _screen_pointer(window) == task.source_screen_pointer
            ):
                task.screen_wait_attempts += 1
                if task.screen_wait_attempts < LAYOUT_READY_LIMIT:
                    return SETUP_INTERVAL_SECONDS
                raise ModifierWorkspaceError("전용 Workspace Screen 전환이 완료되지 않았습니다.")
            _mark_main_screen(window)
            if _control_area(window) is not None and not task.force_restore:
                task.phase = "FINALIZE"
            else:
                task.phase = "COLLAPSE"
            return SETUP_INTERVAL_SECONDS

        _mark_main_screen(window)
        if task.phase == "COLLAPSE":
            if _join_one_area(bpy.context, window):
                return SETUP_INTERVAL_SECONDS
            task.phase = "PREPARE"
            return SETUP_INTERVAL_SECONDS

        if task.phase == "PREPARE":
            area = window.screen.areas[0]
            area.type = "VIEW_3D"
            _mark_main_screen(window)
            task.phase = "SPLIT_TIMELINE"
            return SETUP_INTERVAL_SECONDS

        if task.phase == "SPLIT_TIMELINE":
            _split_timeline_area(bpy.context, window)
            task.phase = "FINALIZE"
            return SETUP_INTERVAL_SECONDS

        if task.phase == "FINALIZE":
            _SETUP_TASK = None
            _complete_workspace_activation(window)
            return None

        raise ModifierWorkspaceError(f"알 수 없는 Workspace 구성 단계입니다: {task.phase}")
    except (
        AttributeError,
        ReferenceError,
        RuntimeError,
        ValueError,
        ModifierWorkspaceError,
        WorkspaceFactoryError,
    ) as error:
        _SETUP_TASK = None
        message = f"COLMAP Modifier Workspace 구성 실패: {error}"
        _abort_workspace_setup(message, task)
        _set_status(message, error=True)
        request_redraw()
        return None


def _process_sidebar_activation() -> float | None:
    """UI Region 생성 뒤 Sidebar Category 활성화를 제한 횟수 재시도합니다."""

    global _SIDEBAR_ATTEMPTS
    if SESSION.phase != SESSION_ACTIVE:
        _SIDEBAR_ATTEMPTS = 0
        return None
    window = session_target_window()
    if window is None:
        _SIDEBAR_ATTEMPTS = 0
        return None
    try:
        if configure_sidebar(window):
            _SIDEBAR_ATTEMPTS = 0
            return None
    except ModifierWorkspaceError:
        pass
    _SIDEBAR_ATTEMPTS += 1
    if _SIDEBAR_ATTEMPTS >= SIDEBAR_ACTIVATION_LIMIT:
        _SIDEBAR_ATTEMPTS = 0
        return None
    return 0.1


def schedule_sidebar_activation() -> None:
    """Sidebar Category 활성화 Timer를 중복 없이 등록합니다."""

    global _SIDEBAR_ATTEMPTS
    if bpy.app.timers.is_registered(_process_sidebar_activation):
        return
    _SIDEBAR_ATTEMPTS = 0
    bpy.app.timers.register(_process_sidebar_activation, first_interval=0.1)


def configure_modifier_workspace(context: Any, *, force_restore: bool = False) -> bool:
    """현재 Window를 전용 Workspace로 전환하거나 기존 구성을 재사용합니다."""

    global _SETUP_TASK
    manager = getattr(context, "window_manager", None)
    window = getattr(context, "window", None)
    if manager is None or window is None:
        raise ModifierWorkspaceError("Blender Window Context가 없습니다.")
    if _SETUP_TASK is not None:
        raise ModifierWorkspaceError("COLMAP Modifier Workspace를 이미 구성 중입니다.")

    was_active = SESSION.phase == SESSION_ACTIVE
    if was_active:
        if not is_session_active_for_window(window):
            raise ModifierWorkspaceError("다른 Blender Window에서 COLMAP Modifier가 실행 중입니다.")
        area = _control_area(window)
        if area is not None and not force_restore:
            normalize_modifier_viewport(window, area)
            configure_sidebar(window)
            return True
    elif SESSION.phase != SESSION_INACTIVE:
        raise ModifierWorkspaceError("이전 COLMAP Modifier Session 정리가 끝나지 않았습니다.")

    source_screen_pointer = _screen_pointer(window)
    source_workspace_pointer = _pointer(window.workspace)
    workspace = None
    created_workspace = False
    if not was_active:
        begin_session_entry(window)
    try:
        workspace, main_window, created_workspace = _ensure_dedicated_workspace(context)
        if not was_active:
            bind_modifier_workspace(workspace)
        _SETUP_TASK = WorkspaceSetupTask(
            main_window=main_window,
            phase="WAIT_SCREEN",
            generation=SESSION.generation,
            source_screen_pointer=source_screen_pointer,
            wait_for_screen_change=(
                _pointer(workspace) != source_workspace_pointer
            ),
            created_workspace=created_workspace,
            force_restore=force_restore,
        )
        if not bpy.app.timers.is_registered(_process_workspace_setup):
            bpy.app.timers.register(
                _process_workspace_setup,
                first_interval=SETUP_INTERVAL_SECONDS,
            )
        return False
    except (
        AttributeError,
        ReferenceError,
        RuntimeError,
        ModifierWorkspaceError,
        WorkspaceFactoryError,
    ) as error:
        if not was_active:
            if workspace is not None and created_workspace:
                _abort_workspace_setup(
                    str(error),
                    WorkspaceSetupTask(
                        main_window=window,
                        phase="ABORT",
                        generation=SESSION.generation,
                        source_screen_pointer=source_screen_pointer,
                        wait_for_screen_change=False,
                        created_workspace=True,
                    ),
                )
            else:
                abort_session_entry(str(error))
        raise ModifierWorkspaceError(str(error)) from error


def _exit_block_reason(context: Any) -> str | None:
    """Workspace 종료 전에 완료되어야 하는 장기 작업을 검사합니다."""

    from .camera_filter_queue import PENDING_STATUS_APPLYING
    from .export_progress_session import active_export_progress_session
    from .import_progress_session import active_import_progress_session

    settings = context.scene.colmap_import
    if active_import_progress_session() is not None:
        return "Import가 진행 중입니다. 완료 후 종료하십시오."
    if active_export_progress_session() is not None or settings.export_running:
        return "Export가 진행 중입니다. 완료 후 종료하십시오."
    if settings.review_pending_filter_status == PENDING_STATUS_APPLYING:
        return "Pending Camera Filter를 적용 중입니다. 완료 후 종료하십시오."
    return None


def _finish_active_edit_modes(context: Any) -> None:
    """Workspace 전환 전에 Point와 Camera Edit Mode를 기존 Operator로 종료합니다."""

    settings = context.scene.colmap_import
    if settings.point_edit_mode_active:
        result = bpy.ops.colmap.toggle_edit_point_mode()
        if "FINISHED" not in result or settings.point_edit_mode_active:
            raise ModifierWorkspaceError("Point Edit Mode를 안전하게 종료하지 못했습니다.")
    if settings.camera_edit_mode_active:
        result = bpy.ops.colmap.toggle_edit_camera_mode()
        if "FINISHED" not in result or settings.camera_edit_mode_active:
            raise ModifierWorkspaceError("Camera Edit Mode를 안전하게 종료하지 못했습니다.")


def _release_viewport_runtime(context: Any) -> tuple[str, ...]:
    """Scene 권위 상태를 보존하고 Viewport에 묶인 임시 표시만 해제합니다."""

    from .camera_filter_queue import release_pending_overlay
    from .camera_review_session import current_review_session
    from .camera_review_viewport import restore_session_views

    warnings = list(release_pending_overlay(context))
    review_session = current_review_session(context)
    if review_session is not None:
        warnings.extend(restore_session_views(review_session))
    return tuple(warnings)


def _activate_modifier_marker_session(window: Any) -> int:
    """ACTIVE Workspace에서 Marker 사용권을 얻고 Manifest Marker를 연결합니다."""

    from .camera_review_session import current_review_session
    from .camera_review_timeline import (
        acquire_modifier_marker_session,
        attach_review_timeline_markers,
    )

    scene = window.scene
    acquire_modifier_marker_session(scene, window=window)
    with bpy.context.temp_override(window=window, screen=window.screen):
        review_session = current_review_session(bpy.context)
    if review_session is None or review_session.scene is not scene:
        return 0
    return attach_review_timeline_markers(review_session)


def _deactivate_modifier_marker_session(context: Any) -> int:
    """Source 복귀 전에 Modifier Marker를 분리하고 Scene 사용권을 반납합니다."""

    from .camera_review_session import current_review_session
    from .camera_review_timeline import (
        detach_modifier_timeline_markers_from_scene,
        detach_review_timeline_markers,
        refresh_timeline_manifest_revision,
        release_modifier_marker_session,
    )

    scene = context.scene
    review_session = current_review_session(context)
    removed_count = 0
    if review_session is not None and review_session.scene is scene:
        refresh_timeline_manifest_revision(review_session)
        removed_count = detach_review_timeline_markers(review_session)
    else:
        removed_count = detach_modifier_timeline_markers_from_scene(scene)
    release_modifier_marker_session(scene)
    return removed_count


def exit_modifier_workspace(context: Any) -> tuple[str, ...]:
    """Viewport Runtime을 정리하고 진입 전 Workspace로 복귀합니다."""

    if (
        SESSION.phase != SESSION_ACTIVE
        or not is_session_owned_by_window(getattr(context, "window", None))
    ):
        raise ModifierWorkspaceError("종료할 COLMAP Modifier Session이 없습니다.")
    block_reason = _exit_block_reason(context)
    if block_reason:
        raise ModifierWorkspaceError(block_reason)
    if session_source_workspace() is None:
        raise ModifierWorkspaceError("진입 전 Workspace를 찾을 수 없어 종료할 수 없습니다.")

    _finish_active_edit_modes(context)
    _deactivate_modifier_marker_session(context)
    warnings = _release_viewport_runtime(context)
    begin_session_exit()
    if not restore_source_workspace():
        SESSION.phase = SESSION_ACTIVE
        SESSION.exit_requested = False
        try:
            _activate_modifier_marker_session(context.window)
        except Exception as marker_error:
            SESSION.status_message = (
                f"{SESSION.status_message}; Marker Session 복구 실패: {marker_error}"
            )
        raise ModifierWorkspaceError(
            SESSION.status_message or "진입 전 Workspace 복귀에 실패했습니다."
        )
    warnings = (*warnings, *restore_default_startup_objects(context.window))
    complete_session_exit()
    if warnings:
        context.scene.colmap_import.warning_summary = "\n".join(warnings)
    _set_status("COLMAP Modifier를 종료하고 이전 Workspace로 돌아왔습니다.")
    request_redraw()
    return warnings


def _process_workspace_recovery() -> float | None:
    """수동 Workspace 이탈 뒤 Session 대상 Window만 전용 Workspace로 복구합니다."""

    global _RECOVERY_ATTEMPTS
    if SESSION.phase != SESSION_ACTIVE:
        SESSION.recovery_pending = False
        _RECOVERY_ATTEMPTS = 0
        return None
    window = session_target_window()
    workspace = session_modifier_workspace()
    if window is None or workspace is None:
        SESSION.recovery_pending = False
        _RECOVERY_ATTEMPTS = 0
        _set_status("COLMAP Modifier Workspace 복구 대상을 찾지 못했습니다.", error=True)
        return None
    try:
        if _pointer(window.workspace) != _pointer(workspace):
            window.workspace = workspace
            _RECOVERY_ATTEMPTS += 1
            if _RECOVERY_ATTEMPTS < LAYOUT_READY_LIMIT:
                return SETUP_INTERVAL_SECONDS
            raise ModifierWorkspaceError("Workspace 전환 완료를 확인하지 못했습니다.")
        _mark_main_screen(window)
        area = _control_area(window)
        if area is None:
            with bpy.context.temp_override(window=window, screen=window.screen):
                configure_modifier_workspace(bpy.context, force_restore=True)
            return None
        SESSION.bind_control_area(window, area)
        configure_sidebar(window)
        SESSION.recovery_pending = False
        _RECOVERY_ATTEMPTS = 0
        _set_status("Workspace 변경을 감지해 COLMAP Modifier로 복귀했습니다.")
        request_redraw()
        return None
    except (AttributeError, ReferenceError, RuntimeError, ModifierWorkspaceError) as error:
        SESSION.recovery_pending = False
        _RECOVERY_ATTEMPTS = 0
        _set_status(f"COLMAP Modifier Workspace 복구 실패: {error}", error=True)
        return None


def schedule_workspace_recovery() -> None:
    """Workspace 변경 Callback에서 실제 복구를 다음 UI Tick으로 예약합니다."""

    if SESSION.phase != SESSION_ACTIVE or SESSION.exit_requested:
        return
    window = session_target_window()
    workspace = session_modifier_workspace()
    if window is None or workspace is None:
        return
    try:
        if _pointer(window.workspace) == _pointer(workspace):
            return
    except (AttributeError, ReferenceError, RuntimeError):
        return
    SESSION.recovery_pending = True
    if not bpy.app.timers.is_registered(_process_workspace_recovery):
        bpy.app.timers.register(_process_workspace_recovery, first_interval=0.0)


def _on_workspace_changed() -> None:
    """Workspace 변경 시 다시 그리지 않고 세션 종료는 공용 조정기에 맡깁니다."""

    # 자유로운 탭 이동을 위해 기존 Modifier Workspace 강제 복귀는 사용하지 않습니다.
    # schedule_workspace_recovery()
    request_redraw()


def _subscribe_workspace_changes() -> None:
    """Window Workspace 변경 Message Bus를 중복 없이 등록합니다."""

    bpy.msgbus.clear_by_owner(_MSGBUS_OWNER)
    bpy.msgbus.subscribe_rna(
        key=(bpy.types.Window, "workspace"),
        owner=_MSGBUS_OWNER,
        args=(),
        notify=_on_workspace_changed,
        options={"PERSISTENT"},
    )


def cancel_workspace_tasks() -> None:
    """Workspace Setup, Runtime 복원 UI, Sidebar와 Recovery Timer를 모두 취소합니다."""

    global _RECOVERY_ATTEMPTS, _RUNTIME_RESTORE_TASK, _SETUP_TASK, _SIDEBAR_ATTEMPTS
    for callback in (
        _process_workspace_setup,
        _process_sidebar_activation,
        _process_workspace_recovery,
        _process_runtime_restore,
        _restore_session_after_load,
    ):
        if bpy.app.timers.is_registered(callback):
            bpy.app.timers.unregister(callback)
    _SETUP_TASK = None
    _RUNTIME_RESTORE_TASK = None
    _RECOVERY_ATTEMPTS = 0
    _SIDEBAR_ATTEMPTS = 0


@persistent
def clear_modifier_session_before_load(*_args) -> None:
    """파일 교체 전에 이전 Window와 Workspace 참조를 폐기합니다."""

    cancel_workspace_tasks()
    window = session_target_window()
    scene = getattr(window, "scene", None)
    if scene is not None:
        from .camera_review_timeline import release_modifier_marker_session

        release_modifier_marker_session(scene)
    SESSION.clear()


def _restore_session_after_load() -> float | None:
    """로드된 소유 Workspace를 현재 Blender ID로 다시 Session에 연결합니다."""

    if SESSION.phase != SESSION_INACTIVE:
        return None
    manager = getattr(bpy.context, "window_manager", None)
    if manager is None:
        return None
    window = next(
        (
            candidate
            for candidate in manager.windows
            if workspace_matches_spec(
                getattr(candidate, "workspace", None),
                MODIFIER_WORKSPACE_SPEC,
                require_ready=True,
            )
        ),
        None,
    )
    if window is None:
        return None
    try:
        with bpy.context.temp_override(window=window, screen=window.screen):
            begin_session_entry(window)
            bind_modifier_workspace(window.workspace)
            global _SETUP_TASK
            _SETUP_TASK = WorkspaceSetupTask(
                main_window=window,
                phase="WAIT_SCREEN",
                generation=SESSION.generation,
                source_screen_pointer=0,
                wait_for_screen_change=False,
                created_workspace=False,
            )
            if not bpy.app.timers.is_registered(_process_workspace_setup):
                bpy.app.timers.register(
                    _process_workspace_setup,
                    first_interval=SETUP_INTERVAL_SECONDS,
                )
    except (AttributeError, ReferenceError, RuntimeError, ModifierWorkspaceError) as error:
        abort_session_entry(str(error))
        _set_status(f"COLMAP Modifier Session 복원 실패: {error}", error=True)
    return None


@persistent
def restore_modifier_session_after_load(*_args) -> None:
    """파일 로드 후 Message Bus와 소유 Workspace Session 복원을 예약합니다."""

    _subscribe_workspace_changes()
    if not bpy.app.timers.is_registered(_restore_session_after_load):
        bpy.app.timers.register(_restore_session_after_load, first_interval=0.0)


def register_modifier_workspace_runtime() -> None:
    """Workspace 감시와 파일 로드 생명주기를 등록합니다."""

    if clear_modifier_session_before_load not in bpy.app.handlers.load_pre:
        bpy.app.handlers.load_pre.append(clear_modifier_session_before_load)
    if restore_modifier_session_after_load not in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.append(restore_modifier_session_after_load)
    _subscribe_workspace_changes()
    if not bpy.app.timers.is_registered(_restore_session_after_load):
        bpy.app.timers.register(_restore_session_after_load, first_interval=0.0)


def unregister_modifier_workspace_runtime() -> None:
    """전용 Workspace를 벗어난 뒤 Handler, Message Bus와 Timer를 해제합니다."""

    cancel_workspace_tasks()
    bpy.msgbus.clear_by_owner(_MSGBUS_OWNER)
    if clear_modifier_session_before_load in bpy.app.handlers.load_pre:
        bpy.app.handlers.load_pre.remove(clear_modifier_session_before_load)
    if restore_modifier_session_after_load in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(restore_modifier_session_after_load)
    if SESSION.phase != SESSION_INACTIVE:
        window = session_target_window()
        if window is not None:
            with bpy.context.temp_override(window=window, screen=window.screen):
                _deactivate_modifier_marker_session(bpy.context)
        restore_source_workspace()
        if window is not None:
            restore_default_startup_objects(window)
    SESSION.clear()
    request_redraw()


__all__ = [
    "AREA_EDGE_GAP_TOLERANCE",
    "LAYOUT_READY_LIMIT",
    "ModifierWorkspaceError",
    "MODIFIER_WORKSPACE_SPEC",
    "SETUP_INTERVAL_SECONDS",
    "SIDEBAR_ACTIVATION_LIMIT",
    "TIMELINE_HEIGHT_FACTOR",
    "WorkspaceSetupTask",
    "cancel_workspace_tasks",
    "configure_modifier_workspace",
    "configure_sidebar",
    "exit_modifier_workspace",
    "find_main_window",
    "register_modifier_workspace_runtime",
    "request_redraw",
    "restore_modifier_session_after_load",
    "schedule_workspace_recovery",
    "unregister_modifier_workspace_runtime",
]
