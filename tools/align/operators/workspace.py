"""현재 Blender 창의 전용 COLMAP Align 듀얼 뷰 작업공간을 관리합니다."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
import math
from typing import Any

import bpy
from bpy.props import EnumProperty
from mathutils import Quaternion, Vector

try:
    from ....shared.blender_runtime.workspace_factory import (
        WorkspaceFactoryError,
        append_seed_workspace,
        delete_owned_workspace,
        mark_workspace_failed,
        mark_workspace_ready,
        pointer,
        screen_matches_workspace,
        workspace_matches_spec,
    )
    from ....shared.blender_runtime.workspace_handoff import (
        ACTIVATION_DEFERRED,
        WorkspaceHandoffError,
        request_workspace_activation,
    )
except ImportError:  # Source Tree를 최상위 tools 패키지로 실행하는 기존 테스트를 지원합니다.
    from shared.blender_runtime.workspace_factory import (
        WorkspaceFactoryError,
        append_seed_workspace,
        delete_owned_workspace,
        mark_workspace_failed,
        mark_workspace_ready,
        pointer,
        screen_matches_workspace,
        workspace_matches_spec,
    )
    from shared.blender_runtime.workspace_handoff import (
        ACTIVATION_DEFERRED,
        WorkspaceHandoffError,
        request_workspace_activation,
    )

from ..alignment import LayerTransformError, MarkerAlignmentError
from ..constants import (
    CONTROL_PANEL_CATEGORY,
    DEFAULT_VIEW_AZIMUTH_DEGREES,
    DEFAULT_VIEW_ELEVATION_DEGREES,
    MAIN_SCREEN_OWNER_KEY,
    ROLE_MOVING,
    ROLE_REFERENCE,
    SESSION_INACTIVE,
    WORKSPACE_OWNER_KEY,
    WORKSPACE_SOURCE_KEY,
    VIEW_PRESET_VERSION,
    VIEW_PRESET_VERSION_KEY,
)
from ..model_repository import ModelRepositoryError
from ..session import (
    abort_session_entry,
    activate_session,
    begin_session_entry,
    bind_align_workspace,
    is_session_active_for_window,
    request_session_exit,
    session_source_workspace,
)
from ..logging_utils import get_logger
from ..state import RUNTIME
from ..viewport.point_data import (
    companion_mesh_data_for_id,
    effective_bounds_for_model_id,
    model_geometry_is_visible,
)
from ..viewport.registry import (
    classify_areas,
    invalidate_registry,
    refresh_registry,
    resolve_area,
)
from ..viewport.renderer import (
    ensure_gpu_batches,
    install_draw_handlers,
    render_model_ids_for_role,
    request_redraw,
)
from ..workspace_contract import ALIGN_WORKSPACE_SPEC


_LOGGER = get_logger("operators.workspace")


class WorkspaceConfigurationError(RuntimeError):
    """사용자 소유 화면을 더 수정하지 않고 구성을 중단할 때 사용합니다."""


@dataclass
class WorkspaceSetupTask:
    """UI 이벤트 사이에 전용 메인 작업공간 구성을 한 단계씩 이어갑니다."""

    main_window: Any
    workspace: Any
    source_workspace: Any
    phase: str
    reset_view_preset: bool = False
    force_view_preset: bool = False
    layout_wait_attempts: int = 0
    source_screen_pointer: int = 0
    wait_for_screen_change: bool = False
    screen_wait_attempts: int = 0


@dataclass(frozen=True)
class RoleViewState:
    """작업공간 왕복 전에 보관할 한 A/P 뷰의 사용자 시점입니다."""

    location: tuple[float, float, float]
    distance: float
    rotation: tuple[float, float, float, float]
    perspective: str


_SIDEBAR_ACTIVATION_LIMIT = 12
_LAYOUT_READY_LIMIT = 12
# 4K·고배율 UI에서는 서로 맞닿은 Blender Area 경계 좌표가 5픽셀까지 벌어집니다.
_AREA_EDGE_GAP_TOLERANCE = 5
_sidebar_activation_attempts = 0


def _window_is_live(window_manager: Any, window: Any) -> bool:
    """저장한 Window 참조가 현재 WindowManager에 남아 있는지 확인합니다."""
    if window is None:
        return False
    try:
        pointer = window.as_pointer()
    except (AttributeError, ReferenceError):
        return False
    return any(candidate.as_pointer() == pointer for candidate in window_manager.windows)


def find_main_window(window_manager: Any, preferred: Any = None) -> Any | None:
    """현행 Align Tool ID·UUID·Schema와 Screen 소유권이 일치하는 Window를 찾습니다."""
    candidates = list(window_manager.windows)
    for candidate in (preferred, RUNTIME.main_window_ref):
        if (
            _window_is_live(window_manager, candidate)
            and workspace_matches_spec(candidate.workspace, ALIGN_WORKSPACE_SPEC)
            and screen_matches_workspace(candidate.screen, candidate.workspace)
        ):
            return candidate
    for window in candidates:
        if (
            workspace_matches_spec(window.workspace, ALIGN_WORKSPACE_SPEC)
            and screen_matches_workspace(window.screen, window.workspace)
        ):
            return window
    return None


def _mark_main_screen(window: Any) -> None:
    """공통 UUID 소유권이 확인된 Screen만 Align 메인 화면으로 표시합니다."""
    if not workspace_matches_spec(window.workspace, ALIGN_WORKSPACE_SPEC, require_ready=False):
        raise WorkspaceConfigurationError("Align 소유 Workspace가 아닌 화면을 표시할 수 없습니다.")
    if not screen_matches_workspace(window.screen, window.workspace):
        raise WorkspaceConfigurationError("Align Workspace UUID와 Screen 소유권이 일치하지 않습니다.")
    window.screen[MAIN_SCREEN_OWNER_KEY] = True
    RUNTIME.main_window_ref = window


def _screen_pointer(window: Any) -> int:
    """유효한 Window Screen의 런타임 포인터를 반환합니다."""
    try:
        return window.screen.as_pointer()
    except (AttributeError, ReferenceError):
        return 0


def capture_role_view_states(window: Any) -> bool:
    """작업공간 왕복 전에 A/P의 사용자 시점을 Screen별 런타임 캐시에 저장합니다."""
    screen_pointer = _screen_pointer(window)
    if not screen_pointer or not refresh_registry(window):
        return False
    snapshots: dict[str, RoleViewState] = {}
    for role in (ROLE_REFERENCE, ROLE_MOVING):
        area = resolve_area(window, role)
        if area is None:
            return False
        region_3d = area.spaces.active.region_3d
        snapshots[role] = RoleViewState(
            location=tuple(float(value) for value in region_3d.view_location),
            distance=float(region_3d.view_distance),
            rotation=tuple(float(value) for value in region_3d.view_rotation),
            perspective=region_3d.view_perspective,
        )
    RUNTIME.role_view_states[screen_pointer] = snapshots
    return True


def restore_role_view_states(window: Any) -> bool:
    """같은 전용 Screen으로 돌아올 때 저장한 사용자 A/P 시점을 복원합니다."""
    screen_pointer = _screen_pointer(window)
    snapshots = RUNTIME.role_view_states.get(screen_pointer)
    if not snapshots or not refresh_registry(window):
        return False
    for role in (ROLE_REFERENCE, ROLE_MOVING):
        snapshot = snapshots.get(role)
        area = resolve_area(window, role)
        if snapshot is None or area is None:
            return False
        region_3d = area.spaces.active.region_3d
        region_3d.view_location = Vector(snapshot.location)
        region_3d.view_distance = snapshot.distance
        region_3d.view_rotation = Quaternion(snapshot.rotation)
        region_3d.view_perspective = snapshot.perspective
        area.tag_redraw()
    return True


def _workspace_is_used_by_other_window(window_manager: Any, workspace: Any, window: Any) -> bool:
    """현재 대상 외의 Window가 지정 Workspace를 사용하는지 포인터로 판정합니다."""
    workspace_pointer = pointer(workspace)
    window_pointer = pointer(window)
    return any(
        pointer(candidate) != window_pointer
        and pointer(getattr(candidate, "workspace", None)) == workspace_pointer
        for candidate in window_manager.windows
    )


def _delete_legacy_workspace(
    context: Any,
    window: Any,
    workspace: Any,
    source_workspace: Any,
) -> None:
    """다른 Window가 쓰지 않는 구버전 Align Workspace만 Source 복귀를 보장하며 삭제합니다."""
    if _workspace_is_used_by_other_window(context.window_manager, workspace, window):
        raise WorkspaceConfigurationError(
            "구버전 COLMAP Align 작업공간을 다른 Blender Window가 사용 중입니다. "
            "해당 Window를 닫거나 다른 작업공간으로 전환한 뒤 다시 실행하십시오."
        )
    try:
        window.workspace = workspace
        with context.temp_override(window=window):
            result = bpy.ops.workspace.delete()
    except (AttributeError, ReferenceError, RuntimeError, TypeError) as error:
        raise WorkspaceConfigurationError("구버전 COLMAP Align 작업공간을 안전하게 교체하지 못했습니다.") from error
    finally:
        if pointer(source_workspace):
            try:
                window.workspace = source_workspace
            except (AttributeError, ReferenceError, RuntimeError, TypeError):
                pass
    if "FINISHED" not in result or any(pointer(item) == pointer(workspace) for item in bpy.data.workspaces):
        raise WorkspaceConfigurationError("구버전 COLMAP Align 작업공간을 안전하게 교체하지 못했습니다.")


def _remove_replaceable_workspaces(
    context: Any,
    target_window: Any,
    source_workspace: Any,
) -> None:
    """FAILED·CREATING 현행 공간과 Legacy Align 공간만 사용 여부를 검사해 교체합니다."""
    non_ready = [
        workspace
        for workspace in bpy.data.workspaces
        if workspace_matches_spec(workspace, ALIGN_WORKSPACE_SPEC, require_ready=False)
        and not workspace_matches_spec(workspace, ALIGN_WORKSPACE_SPEC)
    ]
    for workspace in non_ready:
        if _workspace_is_used_by_other_window(context.window_manager, workspace, target_window):
            raise WorkspaceConfigurationError(
                "준비가 완료되지 않은 COLMAP Align 작업공간을 다른 Blender Window가 사용 중입니다."
            )
        if not delete_owned_workspace(
            context,
            target_window,
            workspace,
            ALIGN_WORKSPACE_SPEC,
            restore_workspace=source_workspace,
        ):
            raise WorkspaceConfigurationError("COLMAP Align 소유 작업공간을 안전하게 교체하지 못했습니다.")

    legacy_workspaces = [
        workspace
        for workspace in bpy.data.workspaces
        if workspace.get(WORKSPACE_OWNER_KEY, False)
        and not workspace_matches_spec(workspace, ALIGN_WORKSPACE_SPEC, require_ready=False)
    ]
    for workspace in legacy_workspaces:
        _delete_legacy_workspace(context, target_window, workspace, source_workspace)


def _ensure_dedicated_workspace(
    context: Any,
    source_workspace: Any,
) -> tuple[bpy.types.WorkSpace, Any]:
    """현재 Window를 검증된 Align Seed Workspace로 전환하거나 READY 공간을 재사용합니다."""
    window_manager = context.window_manager
    target_window = context.window
    if target_window is None:
        raise WorkspaceConfigurationError("전용 작업공간으로 전환할 현재 Window가 없습니다.")

    _remove_replaceable_workspaces(context, target_window, source_workspace)
    ready_workspaces = [
        workspace
        for workspace in bpy.data.workspaces
        if workspace_matches_spec(workspace, ALIGN_WORKSPACE_SPEC)
    ]
    if len(ready_workspaces) > 1:
        raise WorkspaceConfigurationError("현행 COLMAP Align 소유 작업공간이 여러 개이므로 안전한 대상을 결정할 수 없습니다.")
    if ready_workspaces:
        workspace = ready_workspaces[0]
        if _workspace_is_used_by_other_window(window_manager, workspace, target_window):
            raise WorkspaceConfigurationError(
                "COLMAP Align 작업공간을 다른 Blender Window가 사용 중입니다. "
                "해당 Window에서 세션을 종료한 뒤 다시 실행하십시오."
            )
        target_window.workspace = workspace
        return workspace, target_window

    if pointer(target_window.workspace) != pointer(source_workspace):
        target_window.workspace = source_workspace
    try:
        workspace = append_seed_workspace(
            context,
            target_window,
            ALIGN_WORKSPACE_SPEC,
            source_workspace,
        )
    except WorkspaceFactoryError as error:
        raise WorkspaceConfigurationError(str(error)) from error
    # Legacy 호출부는 유지하되 현행 판정은 공통 메타데이터만 사용합니다.
    workspace[WORKSPACE_OWNER_KEY] = True
    workspace[WORKSPACE_SOURCE_KEY] = source_workspace.name
    return workspace, target_window


def _areas_are_adjacent(first: Any, second: Any) -> bool:
    """두 Area가 전체 가로나 세로 경계를 공유해 조인 가능한지 확인합니다."""
    first_right = first.x + first.width
    second_right = second.x + second.width
    first_top = first.y + first.height
    second_top = second.y + second.height
    vertical_touch = (
        abs(first_right - second.x) <= _AREA_EDGE_GAP_TOLERANCE
        or abs(second_right - first.x) <= _AREA_EDGE_GAP_TOLERANCE
    )
    same_vertical_span = abs(first.y - second.y) <= 1 and abs(first.height - second.height) <= 2
    horizontal_touch = (
        abs(first_top - second.y) <= _AREA_EDGE_GAP_TOLERANCE
        or abs(second_top - first.y) <= _AREA_EDGE_GAP_TOLERANCE
    )
    same_horizontal_span = abs(first.x - second.x) <= 1 and abs(first.width - second.width) <= 2
    return (vertical_touch and same_vertical_span) or (horizontal_touch and same_horizontal_span)


def _area_center(area: Any) -> tuple[int, int]:
    """Area 조인 오퍼레이터에 넘길 내부 중심 좌표를 계산합니다."""
    return (area.x + max(area.width // 2, 1), area.y + max(area.height // 2, 1))


def _join_area_pair(context: Any, window: Any, source: Any, target: Any) -> bool:
    """두 Area 결합을 시도하고 실제 Area 개수 감소 여부만 성공으로 판정합니다."""
    before_count = len(window.screen.areas)
    try:
        with context.temp_override(window=window, screen=window.screen, area=source):
            result = bpy.ops.screen.area_join(
                source_xy=_area_center(source),
                target_xy=_area_center(target),
            )
    except RuntimeError as error:
        raise WorkspaceConfigurationError(
            f"{source.type}와 {target.type} Area 결합에 실패했습니다: {error}"
        ) from error
    after_count = len(window.screen.areas)
    if after_count == before_count - 1:
        return True
    if after_count == before_count and ("FINISHED" in result or "CANCELLED" in result):
        return False
    raise WorkspaceConfigurationError(
        f"{source.type}와 {target.type} Area 결합 결과가 유효하지 않습니다."
    )


def _join_candidate_pairs(areas: list[Any]) -> list[tuple[Any, Any]]:
    """RNA 좌표상 인접한 쌍을 우선하고 나머지는 Blender 내부 위상 판정 후보로 둡니다."""
    pairs = [
        (source, target)
        for source in sorted(areas, key=lambda item: item.width * item.height)
        for target in areas
        if source != target
    ]
    return sorted(pairs, key=lambda pair: 0 if _areas_are_adjacent(*pair) else 1)


def _join_failure_summary(areas: list[Any]) -> str:
    """결합 실패 진단에 사용할 현재 Area 좌표 요약을 만듭니다."""
    return ", ".join(
        f"{area.type}({area.x},{area.y},{area.width},{area.height})" for area in areas
    )


def _split_area(context: Any, window: Any, area: Any, factor: float) -> None:
    """명시적인 메인 Window·Screen·Area 컨텍스트에서 수직 분할합니다."""
    before_count = len(window.screen.areas)
    with context.temp_override(window=window, screen=window.screen, area=area):
        result = bpy.ops.screen.area_split(direction="VERTICAL", factor=factor)
    if "FINISHED" not in result or len(window.screen.areas) != before_count + 1:
        raise WorkspaceConfigurationError("메인 작업공간의 Area 분할에 실패했습니다.")


def _set_main_editor_types(window: Any) -> tuple[Any, Any]:
    """메인 창의 두 Area를 x좌표 기준 A와 P VIEW_3D로 확정합니다."""
    areas = sorted(window.screen.areas, key=lambda area: area.x)
    if len(areas) != 2:
        raise WorkspaceConfigurationError("메인 작업 창의 Area 수가 2개가 아닙니다.")
    reference_area, moving_area = areas
    reference_area.type = "VIEW_3D"
    moving_area.type = "VIEW_3D"
    _mark_main_screen(window)
    return reference_area, moving_area


def hide_scene_objects_in_align_viewports(window: Any) -> None:
    """A/P 전용 뷰에서만 모든 Blender Scene 오브젝트 타입 표시를 끕니다."""

    if not workspace_matches_spec(window.workspace, ALIGN_WORKSPACE_SPEC, require_ready=False):
        raise WorkspaceConfigurationError("오브젝트 필터는 Align 소유 Workspace에서만 변경할 수 있습니다.")
    if not screen_matches_workspace(window.screen, window.workspace):
        raise WorkspaceConfigurationError("오브젝트 필터는 Align UUID 소유 Screen에서만 변경할 수 있습니다.")
    if not refresh_registry(window):
        raise WorkspaceConfigurationError("Scene 오브젝트를 숨길 A/P 뷰를 등록하지 못했습니다.")
    for role in (ROLE_REFERENCE, ROLE_MOVING):
        area = resolve_area(window, role)
        if area is None or area.type != "VIEW_3D":
            raise WorkspaceConfigurationError("Scene 오브젝트를 숨길 A/P 뷰를 찾지 못했습니다.")
        space = area.spaces.active
        filter_names = tuple(
            prop.identifier
            for prop in space.bl_rna.properties
            if prop.identifier.startswith("show_object_viewport_")
        )
        if not filter_names:
            raise WorkspaceConfigurationError("현재 Blender에서 뷰포트 오브젝트 표시 필터를 찾지 못했습니다.")
        for filter_name in filter_names:
            setattr(space, filter_name, False)
        area.tag_redraw()


def configure_moving_sidebar(window: Any) -> bool:
    """오른쪽 P 뷰의 Sidebar를 열고 쓰기 가능할 때만 전용 탭을 선택합니다."""
    if not refresh_registry(window):
        raise WorkspaceConfigurationError("오른쪽 P 뷰를 등록하지 못했습니다.")
    moving_area = resolve_area(window, ROLE_MOVING)
    if moving_area is None:
        raise WorkspaceConfigurationError("오른쪽 P 뷰를 찾지 못했습니다.")
    moving_space = moving_area.spaces.active
    moving_space.show_region_ui = True
    ui_region = next(
        (region for region in moving_area.regions if region.type == "UI"),
        None,
    )
    if ui_region is None:
        raise WorkspaceConfigurationError("오른쪽 P 뷰의 Sidebar Region을 찾지 못했습니다.")
    category_active = activate_sidebar_category(ui_region)
    moving_area.tag_redraw()
    return category_active


def activate_sidebar_category(ui_region: Any) -> bool:
    """Blender RNA가 쓰기를 허용할 때만 Sidebar 활성 카테고리를 설정합니다."""
    try:
        if ui_region.is_property_readonly("active_panel_category"):
            return False
        ui_region.active_panel_category = CONTROL_PANEL_CATEGORY
        return ui_region.active_panel_category == CONTROL_PANEL_CATEGORY
    except (AttributeError, ReferenceError, TypeError):
        return False


def _process_sidebar_activation() -> float | None:
    """UI 그림 이후 전용 Sidebar 탭 선택을 제한 횟수만 다시 시도합니다."""
    global _sidebar_activation_attempts
    if not RUNTIME.session_active:
        _sidebar_activation_attempts = 0
        return None
    main_window = find_main_window(bpy.context.window_manager)
    if main_window is None:
        _sidebar_activation_attempts = 0
        return None
    try:
        if configure_moving_sidebar(main_window):
            _sidebar_activation_attempts = 0
            return None
    except (ReferenceError, RuntimeError, WorkspaceConfigurationError):
        pass
    _sidebar_activation_attempts += 1
    if _sidebar_activation_attempts >= _SIDEBAR_ACTIVATION_LIMIT:
        _sidebar_activation_attempts = 0
        return None
    return 0.1


def schedule_sidebar_activation() -> None:
    """세션을 막지 않는 Sidebar 탭 활성화 재시도를 한 번만 예약합니다."""
    global _sidebar_activation_attempts
    if bpy.app.timers.is_registered(_process_sidebar_activation):
        return
    _sidebar_activation_attempts = 0
    bpy.app.timers.register(_process_sidebar_activation, first_interval=0.1)


def _complete_session_activation(main_window: Any) -> None:
    """듀얼 뷰·Registry·Sidebar 검증 후 Workspace를 READY로 확정하고 세션을 활성화합니다."""
    if not refresh_registry(main_window):
        raise WorkspaceConfigurationError("READY 확정 전 Align A/P Registry를 검증하지 못했습니다.")
    sidebar_category_active = configure_moving_sidebar(main_window)
    moving_area = resolve_area(main_window, ROLE_MOVING)
    ui_region = (
        next((region for region in moving_area.regions if region.type == "UI"), None)
        if moving_area is not None
        else None
    )
    if (
        moving_area is None
        or ui_region is None
        or not moving_area.spaces.active.show_region_ui
    ):
        raise WorkspaceConfigurationError("READY 확정 전 P 뷰 Sidebar Region을 준비하지 못했습니다.")
    try:
        mark_workspace_ready(main_window.workspace, main_window.screen, ALIGN_WORKSPACE_SPEC)
    except WorkspaceFactoryError as error:
        raise WorkspaceConfigurationError(str(error)) from error
    RUNTIME.workspace_setup_task = None
    RUNTIME.workspace_setup_error = ""
    activate_session(main_window)
    # Blender가 첫 Draw 전 Sidebar Category 쓰기를 막으면 READY 후 기존 제한 재시도로 선택합니다.
    if not sidebar_category_active:
        schedule_sidebar_activation()
    request_redraw()


def _finish_main_configuration(
    window: Any,
    reset_view_preset: bool,
    force_view_preset: bool = False,
) -> None:
    """메인 듀얼 뷰의 역할·Scene 숨김·GPU 표시와 버전별 기본 시점을 확정합니다."""
    _set_main_editor_types(window)
    if not refresh_registry(window):
        raise WorkspaceConfigurationError("구성한 메인 Area의 A/P 역할을 등록하지 못했습니다.")
    hide_scene_objects_in_align_viewports(window)
    ensure_gpu_batches()
    install_draw_handlers()
    initialize_default_role_views(
        window,
        force=reset_view_preset or force_view_preset,
    )
    if not reset_view_preset and not force_view_preset:
        restore_role_view_states(window)
    request_redraw()


def _next_joinable_pair(areas: list[Any]) -> tuple[Any, Any] | None:
    """현재 좌표가 갱신된 Area 중 결합 가능한 가장 작은 쌍을 선택합니다."""
    return next(
        (
            (source, target)
            for source in sorted(areas, key=lambda item: item.width * item.height)
            for target in areas
            if source != target and _areas_are_adjacent(source, target)
        ),
        None,
    )


def _collapse_one_area(context: Any, window: Any) -> bool:
    """대상 애드온 Screen의 Area를 한 쌍 결합하고 남은 작업 여부를 반환합니다."""
    if (
        not workspace_matches_spec(window.workspace, ALIGN_WORKSPACE_SPEC, require_ready=False)
        or not screen_matches_workspace(window.screen, window.workspace)
        or not window.screen.get(MAIN_SCREEN_OWNER_KEY, False)
    ):
        raise WorkspaceConfigurationError("사용자 소유 Screen은 결합할 수 없습니다.")
    areas = list(window.screen.areas)
    if len(areas) <= 1:
        return False
    for pair in _join_candidate_pairs(areas):
        if _join_area_pair(context, window, *pair):
            invalidate_registry()
            return True
    raise WorkspaceConfigurationError(
        f"결합 가능한 Area가 없습니다: {_join_failure_summary(areas)}"
    )


def _process_workspace_setup() -> float | None:
    """현재 창의 전용 듀얼 뷰를 UI 이벤트 사이에 한 단계씩 구성합니다."""
    task = RUNTIME.workspace_setup_task
    if task is None:
        return None
    main_window = task.main_window
    try:
        if not _window_is_live(bpy.context.window_manager, main_window):
            raise WorkspaceConfigurationError("메인 작업 창이 구성 도중 닫혔습니다.")
        if pointer(main_window.workspace) != pointer(task.workspace):
            if task.phase == "WAIT_WORKSPACE_SCREEN":
                task.screen_wait_attempts += 1
                if task.screen_wait_attempts < _LAYOUT_READY_LIMIT:
                    return 0.05
                raise WorkspaceConfigurationError("Seed Append 후 Align Workspace 활성 전환이 완료되지 않았습니다.")
            raise WorkspaceConfigurationError("전용 작업공간이 구성 도중 변경되었습니다.")
        if not workspace_matches_spec(main_window.workspace, ALIGN_WORKSPACE_SPEC, require_ready=False):
            raise WorkspaceConfigurationError("현행 Align Tool ID·UUID·Schema가 일치하지 않습니다.")
        if not screen_matches_workspace(main_window.screen, main_window.workspace):
            raise WorkspaceConfigurationError("Align Workspace와 Screen UUID 소유권이 일치하지 않습니다.")
        if task.phase == "WAIT_WORKSPACE_SCREEN":
            screen_pointer = _screen_pointer(main_window)
            if task.wait_for_screen_change and screen_pointer == task.source_screen_pointer:
                task.screen_wait_attempts += 1
                if task.screen_wait_attempts < _LAYOUT_READY_LIMIT:
                    return 0.05
                raise WorkspaceConfigurationError(
                    "전용 작업공간 Screen 전환이 완료되지 않아 사용자 Screen을 수정하지 않습니다."
                )
            screen_was_owned = bool(main_window.screen.get(MAIN_SCREEN_OWNER_KEY, False))
            _mark_main_screen(main_window)
            layout_is_ready = classify_areas(main_window.screen.areas) is not None
            if task.force_view_preset:
                task.reset_view_preset = True
                task.phase = "MAIN_COLLAPSE"
                return 0.05
            if layout_is_ready:
                _set_main_editor_types(main_window)
                if refresh_registry(main_window):
                    _finish_main_configuration(
                        main_window,
                        reset_view_preset=False,
                    )
                    task.phase = "FINALIZE"
                    return 0.05
                task.phase = "MAIN_PREPARE"
                return 0.05
            if screen_was_owned:
                task.phase = "WAIT_LAYOUT_READY"
                return 0.05
            task.reset_view_preset = True
            task.phase = "MAIN_COLLAPSE"
            return 0.05

        _mark_main_screen(main_window)

        if task.phase == "WAIT_LAYOUT_READY":
            layout_is_ready = classify_areas(main_window.screen.areas) is not None
            if layout_is_ready:
                _set_main_editor_types(main_window)
                if refresh_registry(main_window):
                    _finish_main_configuration(
                        main_window,
                        reset_view_preset=False,
                    )
                    task.phase = "FINALIZE"
                    return 0.05
                task.phase = "MAIN_PREPARE"
                return 0.05
            task.layout_wait_attempts += 1
            if task.layout_wait_attempts < _LAYOUT_READY_LIMIT:
                return 0.05
            task.reset_view_preset = True
            task.phase = "MAIN_COLLAPSE"
            return 0.05

        if task.phase == "MAIN_COLLAPSE":
            if _collapse_one_area(bpy.context, main_window):
                return 0.05
            task.phase = "MAIN_SPLIT"
            return 0.05

        if task.phase == "MAIN_SPLIT":
            base_area = main_window.screen.areas[0]
            base_area.type = "VIEW_3D"
            _mark_main_screen(main_window)
            _split_area(bpy.context, main_window, base_area, 0.5)
            invalidate_registry()
            task.phase = "MAIN_PREPARE"
            return 0.05

        if task.phase == "MAIN_PREPARE":
            _set_main_editor_types(main_window)
            if not refresh_registry(main_window):
                raise WorkspaceConfigurationError("메인 듀얼 뷰 역할을 등록하지 못했습니다.")
            _finish_main_configuration(
                main_window,
                task.reset_view_preset,
                task.force_view_preset,
            )
            task.phase = "FINALIZE"
            return 0.05

        if task.phase == "FINALIZE":
            _complete_session_activation(main_window)
            return None

        raise WorkspaceConfigurationError(f"알 수 없는 작업공간 구성 단계입니다: {task.phase}")
    except (ReferenceError, RuntimeError, WorkspaceConfigurationError) as error:
        RUNTIME.workspace_setup_error = str(error)
        RUNTIME.workspace_setup_task = None
        try:
            mark_workspace_failed(task.workspace)
        except (ReferenceError, RuntimeError, WorkspaceFactoryError):
            pass
        abort_session_entry(str(error))
        delete_owned_workspace(
            bpy.context,
            main_window,
            task.workspace,
            ALIGN_WORKSPACE_SPEC,
            restore_workspace=task.source_workspace,
        )
        invalidate_registry()
        request_redraw()
        return None


def cancel_workspace_setup() -> None:
    """등록 해제 시 예약된 구성 타이머와 단기 작업 상태를 제거합니다."""
    global _sidebar_activation_attempts
    if bpy.app.timers.is_registered(_process_workspace_setup):
        bpy.app.timers.unregister(_process_workspace_setup)
    if bpy.app.timers.is_registered(_process_sidebar_activation):
        bpy.app.timers.unregister(_process_sidebar_activation)
    _sidebar_activation_attempts = 0
    RUNTIME.workspace_setup_task = None


def ensure_object_mode_before_align(context: Any) -> bool:
    """기존 도구 종료 뒤 현재 활성 Object를 Object Mode로 안전하게 전환합니다."""

    current_mode = str(getattr(context, "mode", "OBJECT"))
    if current_mode == "OBJECT":
        return False
    window = getattr(context, "window", None)
    if window is None or pointer(window) == 0:
        raise WorkspaceConfigurationError(
            f"{current_mode}에서 Object Mode로 전환할 Blender Window가 없습니다."
        )
    screen = getattr(window, "screen", None)
    scene = getattr(window, "scene", None)
    view_layer = getattr(window, "view_layer", None)
    if screen is None or scene is None or view_layer is None:
        raise WorkspaceConfigurationError(
            f"{current_mode}에서 Object Mode로 전환할 화면 컨텍스트가 없습니다."
        )
    active_object = getattr(getattr(view_layer, "objects", None), "active", None)
    if active_object is None:
        raise WorkspaceConfigurationError(
            f"{current_mode}에서 Object Mode로 전환할 활성 오브젝트가 없습니다."
        )

    context_area = getattr(context, "area", None)
    live_areas = tuple(getattr(screen, "areas", ()))
    area = next(
        (
            candidate
            for candidate in live_areas
            if candidate.type == "VIEW_3D"
            and (
                context_area is None
                or pointer(candidate) == pointer(context_area)
            )
        ),
        None,
    )
    if area is None:
        area = next(
            (candidate for candidate in live_areas if candidate.type == "VIEW_3D"),
            None,
        )
    override = {
        "window": window,
        "screen": screen,
        "scene": scene,
        "view_layer": view_layer,
        "active_object": active_object,
        "object": active_object,
    }
    if area is not None:
        region = next(
            (candidate for candidate in area.regions if candidate.type == "WINDOW"),
            None,
        )
        override["area"] = area
        if region is not None:
            override["region"] = region
    try:
        with context.temp_override(**override):
            if not bpy.ops.object.mode_set.poll():
                raise WorkspaceConfigurationError(
                    f"{current_mode}에서 Object Mode 전환을 실행할 수 없습니다."
                )
            result = bpy.ops.object.mode_set(mode="OBJECT")
    except WorkspaceConfigurationError:
        raise
    except (AttributeError, ReferenceError, RuntimeError, TypeError, ValueError) as error:
        raise WorkspaceConfigurationError(
            f"{current_mode}에서 Object Mode로 전환하지 못했습니다: {error}"
        ) from error
    if "FINISHED" not in result or str(getattr(active_object, "mode", "")) != "OBJECT":
        raise WorkspaceConfigurationError(
            f"{current_mode}에서 Object Mode 전환이 완료되지 않았습니다."
        )
    return True


def configure_workspace(context: Any, force_restore: bool = False) -> bool:
    """현재 창을 전용 듀얼 뷰 작업공간으로 전환하거나 멱등적으로 재사용합니다."""
    if context.window_manager is None:
        raise WorkspaceConfigurationError("Blender WindowManager가 없습니다.")
    if RUNTIME.workspace_setup_task is not None:
        return False
    ensure_object_mode_before_align(context)
    is_active_session = RUNTIME.session.session_active
    if is_active_session and not RUNTIME.session.is_target_window(context.window):
        raise WorkspaceConfigurationError("다른 Blender Window에서 COLMAP Align 세션이 실행 중입니다.")
    if not is_active_session and RUNTIME.session.phase != SESSION_INACTIVE:
        raise WorkspaceConfigurationError("COLMAP Align 세션 정리가 끝난 뒤 다시 열 수 있습니다.")
    if is_active_session and not is_session_active_for_window(context.window):
        raise WorkspaceConfigurationError("COLMAP Align 세션의 작업공간 복구가 진행 중입니다.")

    source_screen_pointer = _screen_pointer(context.window)
    source_workspace_pointer = context.window.workspace.as_pointer()
    if not is_active_session:
        begin_session_entry(context.window)
    source_workspace = session_source_workspace()
    if source_workspace is None:
        if not is_active_session or force_restore:
            abort_session_entry("원래 사용자 작업공간을 찾지 못했습니다.")
        raise WorkspaceConfigurationError("원래 사용자 작업공간을 찾지 못했습니다.")
    workspace = None
    try:
        RUNTIME.workspace_setup_error = ""
        workspace, main_window = _ensure_dedicated_workspace(context, source_workspace)
        if not workspace_matches_spec(workspace, ALIGN_WORKSPACE_SPEC, require_ready=False):
            raise WorkspaceConfigurationError("현행 Align 소유 작업공간만 구성할 수 있습니다.")
        if not is_active_session:
            bind_align_workspace(workspace)
        RUNTIME.workspace_setup_task = WorkspaceSetupTask(
            main_window=main_window,
            workspace=workspace,
            source_workspace=source_workspace,
            phase="WAIT_WORKSPACE_SCREEN",
            force_view_preset=force_restore,
            source_screen_pointer=source_screen_pointer,
            wait_for_screen_change=(workspace.as_pointer() != source_workspace_pointer),
        )
        if not bpy.app.timers.is_registered(_process_workspace_setup):
            bpy.app.timers.register(_process_workspace_setup, first_interval=0.05)
        return False
    except (ReferenceError, RuntimeError, WorkspaceConfigurationError) as error:
        if workspace is not None:
            try:
                mark_workspace_failed(workspace)
            except (ReferenceError, RuntimeError, WorkspaceFactoryError):
                pass
        if not is_active_session or force_restore:
            abort_session_entry(str(error))
        if workspace is not None:
            delete_owned_workspace(
                context,
                context.window,
                workspace,
                ALIGN_WORKSPACE_SPEC,
                restore_workspace=source_workspace,
            )
        raise WorkspaceConfigurationError(str(error)) from error


def _bounds_for_model_ids(model_ids: list[str]) -> tuple[Vector, Vector] | None:
    """보이는 각 형상의 축과 공통 소유 모델 정렬을 적용해 전체 경계를 합칩니다."""
    normalized_model_ids = [str(model_id).strip() for model_id in model_ids if str(model_id).strip()]
    if not normalized_model_ids:
        return None
    repository_models = [
        RUNTIME.model_repository.models.get(model_id)
        for model_id in normalized_model_ids
    ]
    if any(model is None for model in repository_models) and any(
        model is not None for model in repository_models
    ):
        missing_ids = [
            model_id
            for model_id, model in zip(normalized_model_ids, repository_models, strict=True)
            if model is None
        ]
        raise WorkspaceConfigurationError(
            "화면 맞춤 대상 모델을 저장소에서 찾을 수 없습니다: "
            + ", ".join(missing_ids)
        )
    real_models = [model for model in repository_models if model is not None]
    if real_models:
        project_bounds = []
        for model_id, model in zip(normalized_model_ids, repository_models, strict=True):
            if model is None:
                continue
            model_id = str(getattr(model, "model_id", model_id))
            try:
                kind = getattr(model, "geometry_kind", "COLMAP_POINT_CLOUD")
                geometries = [(model, False)] if model_geometry_is_visible(model_id, kind) else []
                if kind != "TRIANGLE_MESH" and model_geometry_is_visible(model_id, "TRIANGLE_MESH"):
                    companion = companion_mesh_data_for_id(model_id)
                    if companion is not None:
                        geometries.append((companion, True))
                for geometry, is_companion in geometries:
                    project_bounds.append(
                        effective_bounds_for_model_id(
                            model_id,
                            geometry.bounds_min,
                            geometry.bounds_max,
                            **({"companion": True} if is_companion else {}),
                        )
                    )
            except (
                AttributeError,
                LayerTransformError,
                MarkerAlignmentError,
                ModelRepositoryError,
                ValueError,
                TypeError,
            ) as error:
                raise WorkspaceConfigurationError(
                    f"화면 맞춤에 필요한 유효 좌표 변환을 준비하지 못했습니다: {model_id} ({error})"
                ) from error
        if not project_bounds:
            return None
        minimum = Vector(
            min(float(bounds[0][index]) for bounds in project_bounds)
            for index in range(3)
        )
        maximum = Vector(
            max(float(bounds[1][index]) for bounds in project_bounds)
            for index in range(3)
        )
        return minimum, maximum
    clouds = [RUNTIME.point_clouds.get(model_id) for model_id in normalized_model_ids]
    if any(cloud is None for cloud in clouds):
        raise WorkspaceConfigurationError(
            "화면 맞춤 대상 모델의 저장된 원본 경계를 찾을 수 없습니다."
        )
    positions = [
        position
        for model_id, cloud in zip(normalized_model_ids, clouds, strict=True)
        if model_geometry_is_visible(model_id, "COLMAP_POINT_CLOUD")
        for position in cloud.positions
    ]
    if not positions:
        return None
    minimum = Vector((min(point[index] for point in positions) for index in range(3)))
    maximum = Vector((max(point[index] for point in positions) for index in range(3)))
    return minimum, maximum


def _view_distance_for_bounds(area: Any, radius: float) -> float:
    """뷰포트 렌즈와 종횡비에서 바운딩 구가 잘리지 않는 원근 거리를 계산합니다."""

    window_region = next(
        (region for region in area.regions if region.type == "WINDOW"),
        None,
    )
    if window_region is None:
        raise WorkspaceConfigurationError("화면 맞춤에 필요한 3D 뷰 영역을 찾을 수 없습니다.")
    width = max(float(window_region.width), 1.0)
    height = max(float(window_region.height), 1.0)
    lens = max(float(area.spaces.active.lens), 1.0e-6)
    half_horizontal = math.atan(16.0 / lens)
    half_vertical = math.atan(math.tan(half_horizontal) * height / width)
    limiting_half_angle = max(min(half_horizontal, half_vertical), math.radians(1.0))
    return max(float(radius) / math.sin(limiting_half_angle) * 1.05, 0.1)


def _fit_area_to_bounds(area: Any, bounds: tuple[Vector, Vector]) -> None:
    """지정 Area의 시점 중심·거리·원거리 클리핑을 Project World 경계에 맞춥니다."""
    try:
        minimum = Vector(bounds[0])
        maximum = Vector(bounds[1])
    except (IndexError, TypeError, ValueError) as error:
        raise WorkspaceConfigurationError("화면 맞춤 경계 형식이 올바르지 않습니다.") from error
    if (
        len(minimum) != 3
        or len(maximum) != 3
        or not all(math.isfinite(float(value)) for value in minimum)
        or not all(math.isfinite(float(value)) for value in maximum)
        or any(minimum[index] > maximum[index] for index in range(3))
    ):
        raise WorkspaceConfigurationError("화면 맞춤 경계가 유효한 최소·최대 좌표가 아닙니다.")
    center = (minimum + maximum) * 0.5
    radius = max((maximum - minimum).length * 0.5, 0.1)
    view_distance = _view_distance_for_bounds(area, radius)
    space = area.spaces.active
    required_clip_end = (view_distance + radius) * 1.05
    if float(space.clip_end) < required_clip_end:
        space.clip_end = required_clip_end
    region_3d = space.region_3d
    region_3d.view_location = center
    region_3d.view_distance = view_distance
    area.tag_redraw()


def fit_role_view(
    window: Any,
    role: str,
    *,
    model_ids: Sequence[str] | None = None,
) -> bool:
    """대상 Area를 지정 모델 또는 현재 역할 모델 경계에 맞춥니다."""

    area = resolve_area(window, role)
    target_model_ids: list[str]
    if model_ids is not None:
        target_model_ids = [str(model_id) for model_id in model_ids]
    elif role in {ROLE_REFERENCE, ROLE_MOVING}:
        target_model_ids = list(render_model_ids_for_role(role))
    else:
        target_model_ids = []
    if area is None:
        raise WorkspaceConfigurationError("화면에 맞출 역할 Area를 찾을 수 없습니다.")
    bounds = _bounds_for_model_ids(target_model_ids)
    if bounds is None:
        _LOGGER.info("event=fit_role_view_skipped role=%s reason=empty_model_set", role)
        return False
    _fit_area_to_bounds(area, bounds)
    return True


def _capture_area_view_state(area: Any) -> RoleViewState:
    """지정 3D Area의 RegionView3D 시점을 불변 snapshot으로 복사합니다."""
    region_3d = area.spaces.active.region_3d
    return RoleViewState(
        location=tuple(float(value) for value in region_3d.view_location),
        distance=float(region_3d.view_distance),
        rotation=tuple(float(value) for value in region_3d.view_rotation),
        perspective=region_3d.view_perspective,
    )


def _restore_area_view_state(area: Any, snapshot: RoleViewState) -> None:
    """보관한 RegionView3D snapshot을 지정 3D Area에 복원합니다."""
    region_3d = area.spaces.active.region_3d
    region_3d.view_location = Vector(snapshot.location)
    region_3d.view_distance = snapshot.distance
    region_3d.view_rotation = Quaternion(snapshot.rotation)
    region_3d.view_perspective = snapshot.perspective
    area.tag_redraw()


def capture_moving_view_snapshot(window: Any) -> RoleViewState:
    """marker preview 시작 전 P 뷰의 사용자 시점을 별도 snapshot으로 보관합니다."""
    if not refresh_registry(window):
        raise WorkspaceConfigurationError("P 뷰 시점을 보관할 듀얼 뷰 구성이 유효하지 않습니다.")
    area = resolve_area(window, ROLE_MOVING)
    if area is None:
        raise WorkspaceConfigurationError("P 뷰 시점을 보관할 Area를 찾을 수 없습니다.")
    return _capture_area_view_state(area)


def restore_moving_view_snapshot(window: Any, snapshot: RoleViewState | None) -> bool:
    """marker preview 취소 뒤 보관한 P 뷰 시점을 최선으로 복원합니다."""
    if snapshot is None or not refresh_registry(window):
        return False
    area = resolve_area(window, ROLE_MOVING)
    if area is None:
        return False
    _restore_area_view_state(area, snapshot)
    return True


def _default_view_rotation():
    """Z-up 기준 방위각 45도·내려다보는 고도 45도의 시점 회전을 만듭니다."""
    azimuth = math.radians(DEFAULT_VIEW_AZIMUTH_DEGREES)
    elevation = math.radians(DEFAULT_VIEW_ELEVATION_DEGREES)
    view_direction = Vector(
        (
            -math.cos(elevation) * math.cos(azimuth),
            -math.cos(elevation) * math.sin(azimuth),
            -math.sin(elevation),
        )
    )
    return view_direction.to_track_quat("-Z", "Y")


def initialize_default_role_view(window: Any, role: str) -> None:
    """한 역할 뷰를 빈 상태도 허용하는 원근 45도 기본 시점으로 초기화합니다."""
    fit_role_view(window, role)
    area = resolve_area(window, role)
    if area is None:
        raise WorkspaceConfigurationError("기본 시점을 적용할 듀얼 Area가 없습니다.")
    region_3d = area.spaces.active.region_3d
    region_3d.view_perspective = "PERSP"
    region_3d.view_rotation = _default_view_rotation()
    area.tag_redraw()


def initialize_default_role_views(window: Any, *, force: bool = False) -> bool:
    """새 전용 Screen 또는 강제 복구에서만 A/P 기본 시점을 한 번 적용합니다."""
    screen = window.screen
    if not force and screen.get(VIEW_PRESET_VERSION_KEY) == VIEW_PRESET_VERSION:
        return False
    initialize_default_role_view(window, ROLE_REFERENCE)
    initialize_default_role_view(window, ROLE_MOVING)
    screen[VIEW_PRESET_VERSION_KEY] = VIEW_PRESET_VERSION
    return True


class COLMAPALIGN_OT_open_workspace(bpy.types.Operator):
    """현재 Blender 창을 COLMAP Align 전용 듀얼 뷰로 전환합니다."""

    bl_idname = "colmap_align.open_workspace"
    bl_label = "COLMAP Align 열기"
    bl_description = "현재 창을 A/P 듀얼 뷰로 바꾸고 오른쪽 N 패널을 엽니다"
    bl_options = {"REGISTER"}

    def execute(self, context):
        """다른 COLMAP 도구 종료 뒤 Align Workspace 구성을 시작합니다."""
        try:
            result = request_workspace_activation(context, ALIGN_WORKSPACE_SPEC.tool_id)
        except (WorkspaceConfigurationError, WorkspaceHandoffError) as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        self.report(
            {"INFO"},
            (
                "현재 COLMAP Workspace를 종료한 뒤 Align을 준비합니다."
                if result == ACTIVATION_DEFERRED
                else "COLMAP Align 작업공간을 준비합니다."
            ),
        )
        return {"FINISHED"}


class COLMAPALIGN_OT_restore_workspace(bpy.types.Operator):
    """애드온 소유 메인 Screen만 목표 듀얼 뷰 구성으로 다시 만듭니다."""

    bl_idname = "colmap_align.restore_workspace"
    bl_label = "작업공간 복원"
    bl_description = "애드온 전용 작업공간을 A/P 듀얼 뷰로 복원합니다"
    bl_options = {"INTERNAL"}

    def execute(self, context):
        """사용자 소유 Screen은 건드리지 않고 애드온 Screen만 강제로 재구성합니다."""
        try:
            configure_workspace(context, force_restore=True)
        except WorkspaceConfigurationError as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        return {"FINISHED"}


class COLMAPALIGN_OT_exit_session(bpy.types.Operator):
    """COLMAP Align 세션을 끝내고 진입 전 사용자 WorkSpace로 복귀합니다."""

    bl_idname = "colmap_align.exit_session"
    bl_label = "COLMAP Align 종료"
    bl_description = "세션 Undo·Redo를 지우고 원래 Blender 작업공간으로 돌아갑니다"
    bl_options = {"REGISTER"}

    @classmethod
    def poll(cls, context):
        """현재 N 패널이 세션 대상 Window에 있을 때만 종료를 허용합니다."""
        return is_session_active_for_window(context.window)

    def execute(self, context):
        """모달 정리가 끝난 뒤 안전하게 WorkSpace를 복귀하도록 비동기 종료를 요청합니다."""
        if not request_session_exit(context.window):
            self.report({"ERROR"}, "종료할 COLMAP Align 세션이 없습니다.")
            return {"CANCELLED"}
        self.report({"INFO"}, "COLMAP Align 세션을 종료하고 원래 작업공간으로 돌아갑니다.")
        return {"FINISHED"}


class COLMAPALIGN_OT_fit_view(bpy.types.Operator):
    """선택한 메인 Area의 시점만 해당 임시 점군에 맞춥니다."""

    bl_idname = "colmap_align.fit_view"
    bl_label = "역할 화면 맞춤"
    bl_description = "현재 역할 뷰의 보이는 레이어 전체를 화면에 맞춥니다 (Home)"
    bl_options = {"REGISTER"}

    role: EnumProperty(
        name="역할",
        items=(
            (ROLE_REFERENCE, "활성 레이어", "활성 레이어 점군 화면 맞춤"),
            (ROLE_MOVING, "선택 레이어", "선택 레이어 점군 화면 맞춤"),
        ),
    )

    def execute(self, context):
        """등록된 메인 Window의 역할 Area만 찾아 RegionView3D를 변경합니다."""
        main_window = find_main_window(context.window_manager)
        try:
            fitted = fit_role_view(main_window, self.role)
        except WorkspaceConfigurationError as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        if not fitted:
            role_label = "A" if self.role == ROLE_REFERENCE else "P"
            self.report(
                {"WARNING"},
                f"{role_label} 뷰에 화면 맞춤할 보이는 레이어가 없습니다.",
            )
            return {"CANCELLED"}
        return {"FINISHED"}


CLASSES = (
    COLMAPALIGN_OT_open_workspace,
    COLMAPALIGN_OT_restore_workspace,
    COLMAPALIGN_OT_exit_session,
    COLMAPALIGN_OT_fit_view,
)
