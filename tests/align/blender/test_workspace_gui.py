"""Blender GUI에서 듀얼 뷰, 드래프트 Undo와 UIList 선택을 검증합니다."""

from __future__ import annotations

import sys
import traceback
from pathlib import Path

import bpy
from bpy_extras import view3d_utils
from mathutils import Euler, Quaternion


_NAVIGATION_PROBE_OPERATOR_ID = "colmap_align_test.space_navigation_probe"
_NAVIGATION_PROBE_REGISTERED = False
_NAVIGATION_PROBE_MODAL_RUNNING = False
_NAVIGATION_PROBE_INVOKED = 0
_NAVIGATION_PROBE_RECEIVED = 0
_NAVIGATION_PROBE_RELEASE_CONSUMED = 0


class COLMAPALIGN_OT_space_navigation_probe(bpy.types.Operator):
    """테스트 중 상위 탐색 모달의 LMB 탐색과 Space RELEASE 소비를 재현합니다."""

    bl_idname = _NAVIGATION_PROBE_OPERATOR_ID
    bl_label = "COLMAP Align 테스트 탐색 프로브"
    bl_options = {"INTERNAL"}

    @classmethod
    def poll(cls, context):
        """실제 3D Viewport Window Region에서만 테스트 탐색을 실행합니다."""
        return bool(context.area and context.area.type == "VIEW_3D" and context.region and context.region.type == "WINDOW")

    def invoke(self, context, _event):
        """Space PRESS 직후 상위 탐색 모달을 시작해 marker의 RELEASE 누락을 재현합니다."""
        global _NAVIGATION_PROBE_INVOKED, _NAVIGATION_PROBE_MODAL_RUNNING
        _NAVIGATION_PROBE_INVOKED += 1
        _NAVIGATION_PROBE_MODAL_RUNNING = True
        context.window_manager.modal_handler_add(self)
        return {"RUNNING_MODAL"}

    def modal(self, context, event):
        """LMB는 회전으로 소비하고 Space RELEASE는 marker에 넘기지 않은 채 종료합니다."""
        global _NAVIGATION_PROBE_MODAL_RUNNING, _NAVIGATION_PROBE_RECEIVED, _NAVIGATION_PROBE_RELEASE_CONSUMED
        if event.type == "SPACE" and event.value == "RELEASE":
            _NAVIGATION_PROBE_RELEASE_CONSUMED += 1
            _NAVIGATION_PROBE_MODAL_RUNNING = False
            return {"FINISHED"}
        if event.type != "LEFTMOUSE" or event.value != "PRESS":
            return {"PASS_THROUGH"}
        region_3d = context.space_data.region_3d
        rotation = Quaternion((0.0, 0.0, 1.0), 0.15)
        region_3d.view_rotation = rotation @ region_3d.view_rotation
        _NAVIGATION_PROBE_RECEIVED += 1
        context.area.tag_redraw()
        return {"RUNNING_MODAL"}

    def cancel(self, context):
        """Blender가 테스트 탐색 모달을 취소했을 때 실행 중 플래그를 정리합니다."""
        global _NAVIGATION_PROBE_MODAL_RUNNING
        del context
        _NAVIGATION_PROBE_MODAL_RUNNING = False


def _start_navigation_probe(window: bpy.types.Window, area: bpy.types.Area) -> None:
    """event_simulate가 addon keymap을 호출하지 않아 Space PASS 직후 상위 모달을 명시 시작합니다."""
    global _NAVIGATION_PROBE_REGISTERED
    if not _NAVIGATION_PROBE_REGISTERED:
        bpy.utils.register_class(COLMAPALIGN_OT_space_navigation_probe)
        _NAVIGATION_PROBE_REGISTERED = True
    region = next(region for region in area.regions if region.type == "WINDOW")
    with bpy.context.temp_override(window=window, screen=window.screen, area=area, region=region):
        result = bpy.ops.colmap_align_test.space_navigation_probe("INVOKE_DEFAULT")
    assert "RUNNING_MODAL" in result, "상위 탐색 모달을 시작하지 못했습니다."


def _navigation_probe_is_modal(window: bpy.types.Window) -> bool:
    """테스트 탐색 모달이 Space RELEASE를 소비하기 전까지 실행 중인지 확인합니다."""
    del window
    return _NAVIGATION_PROBE_MODAL_RUNNING


def _remove_navigation_probe() -> bool:
    """종료된 테스트 탐색 오퍼레이터 클래스를 해제해 런타임 등록을 남기지 않습니다."""
    global _NAVIGATION_PROBE_REGISTERED
    if any(_navigation_probe_is_modal(window) for window in bpy.context.window_manager.windows):
        return False
    if _NAVIGATION_PROBE_REGISTERED:
        try:
            bpy.utils.unregister_class(COLMAPALIGN_OT_space_navigation_probe)
        except RuntimeError:
            pass
    _NAVIGATION_PROBE_REGISTERED = False
    return True


def _add_source_path() -> None:
    """애드온 소스 디렉터리를 테스트 모듈 탐색 경로에 추가합니다."""
    source_path = Path(__file__).resolve().parents[3]
    source_text = str(source_path)
    if source_text not in sys.path:
        sys.path.insert(0, source_text)


def _windows() -> list[bpy.types.Window]:
    """현재 Blender 프로세스의 Window 목록을 반환합니다."""
    return list(bpy.context.window_manager.windows)


def _main_window() -> bpy.types.Window:
    """COLMAP Align 전용 작업공간을 표시하는 메인 Window를 반환합니다."""
    from tools.align.operators.workspace import find_main_window

    window = find_main_window(bpy.context.window_manager)
    assert window is not None, "COLMAP Align 메인 Window를 찾지 못했습니다."
    return window


def _invoke_in_area(
    window: bpy.types.Window,
    area: bpy.types.Area,
    operator_id: str,
    invocation: str = "EXEC_DEFAULT",
    **properties,
) -> set[str]:
    """지정 Window·Area의 WINDOW Region에서 애드온 오퍼레이터를 실행합니다."""
    region = next(region for region in area.regions if region.type == "WINDOW")
    namespace, name = operator_id.split(".", 1)
    operator = getattr(getattr(bpy.ops, namespace), name)
    with bpy.context.temp_override(
        window=window,
        screen=window.screen,
        area=area,
        region=region,
    ):
        return operator(invocation, **properties)


def _simulate_shortcut(window: bpy.types.Window, event_type: str, **modifiers) -> None:
    """공개 Window.event_simulate로 세션 입력 가드 단축키를 보냅니다."""
    assert bpy.app.use_event_simulate is True, "--enable-event-simulate가 필요합니다."
    window.event_simulate(type=event_type, value="PRESS", **modifiers)
    window.event_simulate(type=event_type, value="RELEASE", **modifiers)


def _simulate_viewport_mouse_event(
    window: bpy.types.Window,
    area: bpy.types.Area,
    event_type: str,
) -> None:
    """3D 뷰 Window Region 중앙에 지정 마우스 입력을 실제 이벤트로 보냅니다."""

    assert bpy.app.use_event_simulate is True, "--enable-event-simulate가 필요합니다."
    region = next(region for region in area.regions if region.type == "WINDOW")
    x = region.x + region.width // 2
    y = region.y + region.height // 2
    window.event_simulate(type=event_type, value="PRESS", x=x, y=y)
    window.event_simulate(type=event_type, value="RELEASE", x=x, y=y)


def _simulate_point_click(window: bpy.types.Window, area: bpy.types.Area, position) -> None:
    """투영한 점의 실제 화면 좌표에 LMB 이벤트를 보내 모달 피킹을 검증합니다."""
    x, y = _projected_window_coordinate(area, position)
    window.event_simulate(type="LEFTMOUSE", value="PRESS", x=x, y=y)
    window.event_simulate(type="LEFTMOUSE", value="RELEASE", x=x, y=y)


def _projected_window_coordinate(area: bpy.types.Area, position) -> tuple[int, int]:
    """3차원 점을 Window 이벤트에 사용할 화면 좌표로 투영합니다."""
    region = next(region for region in area.regions if region.type == "WINDOW")
    projected = view3d_utils.location_3d_to_region_2d(
        region,
        area.spaces.active.region_3d,
        position,
        default=None,
    )
    assert projected is not None, "실제 LMB 검증용 점을 뷰포트에 투영하지 못했습니다."
    return round(region.x + projected.x), round(region.y + projected.y)


def _simulate_empty_viewport_click(window: bpy.types.Window, area: bpy.types.Area, role: str) -> None:
    """피킹 반경에 점이 없는 Window Region 위치로 실제 LMB 이벤트를 보냅니다."""
    from tools.align.viewport.picking import pick_nearest_point

    region = next(region for region in area.regions if region.type == "WINDOW")
    inset = 24
    candidates = (
        (inset, inset),
        (max(inset, region.width - inset), inset),
        (inset, max(inset, region.height - inset)),
        (max(inset, region.width - inset), max(inset, region.height - inset)),
    )
    for local_x, local_y in candidates:
        if pick_nearest_point(role, region, area.spaces.active.region_3d, (local_x, local_y)) is None:
            window.event_simulate(
                type="LEFTMOUSE",
                value="PRESS",
                x=region.x + local_x,
                y=region.y + local_y,
            )
            window.event_simulate(
                type="LEFTMOUSE",
                value="RELEASE",
                x=region.x + local_x,
                y=region.y + local_y,
            )
            return
    raise AssertionError("빈 공간 LMB 회귀 검사에 사용할 뷰포트 좌표를 찾지 못했습니다.")


def _simulate_space_navigation_click(window: bpy.types.Window, area: bpy.types.Area, position) -> None:
    """Space PASS 뒤 상위 모달이 LMB·RELEASE를 소비하는 탐색 순서를 실제 이벤트로 재현합니다."""
    x, y = _projected_window_coordinate(area, position)
    window.event_simulate(type="SPACE", value="PRESS")
    _start_navigation_probe(window, area)
    window.event_simulate(type="LEFTMOUSE", value="PRESS", x=x, y=y)
    window.event_simulate(type="MOUSEMOVE", value="NOTHING", x=x + 16, y=y + 16)
    window.event_simulate(type="LEFTMOUSE", value="RELEASE", x=x + 16, y=y + 16)
    window.event_simulate(type="SPACE", value="RELEASE")


def _simulate_npanel_click(window: bpy.types.Window, area: bpy.types.Area) -> None:
    """오른쪽 데이터 레이어 뷰 N 패널의 빈 UI 영역에 실제 LMB 이벤트를 보냅니다."""
    region = next(region for region in area.regions if region.type == "UI")
    x = region.x + min(12, max(1, region.width // 2))
    y = region.y + min(12, max(1, region.height // 2))
    window.event_simulate(type="LEFTMOUSE", value="PRESS", x=x, y=y)
    window.event_simulate(type="LEFTMOUSE", value="RELEASE", x=x, y=y)


def _assert_npanel_excluded_from_viewport_picking(
    window: bpy.types.Window,
    area: bpy.types.Area,
) -> None:
    """겹친 N 패널 중앙 좌표가 P 뷰포트 피킹 경계에서 제외되는지 확인합니다."""

    from tools.align.viewport.picking import find_viewport_under_mouse

    region = next(region for region in area.regions if region.type == "UI")
    assert region.width > 1 and region.height > 1, "검증할 열린 N 패널 Region이 필요합니다."
    x = region.x + region.width // 2
    y = region.y + region.height // 2
    assert find_viewport_under_mouse(window, x, y) is None


def _marker_cloud(role: str):
    """현재 A/P 마커 엔진 역할에 연결된 모델 ID 점군을 반환합니다."""
    from tools.align.state import RUNTIME
    from tools.align.viewport.picking import marker_source_model_id

    model_id = marker_source_model_id(role)
    assert model_id is not None, "선택 비활성 레이어가 없어 마커 피킹 점군을 찾을 수 없습니다."
    cloud = RUNTIME.point_clouds.get(model_id)
    assert cloud is not None, "마커 피킹 대상 모델 점군을 찾을 수 없습니다."
    return cloud


def _pickable_model_point(
    window: bpy.types.Window,
    area: bpy.types.Area,
    role: str,
    model_id: str,
):
    """겹친 UI 밖에서 실제 다중 모델 피커가 지정 모델로 판정하는 점을 찾습니다."""
    from tools.align.state import RUNTIME
    from tools.align.viewport.picking import find_viewport_under_mouse, pick_nearest_point

    cloud = RUNTIME.point_clouds.get(model_id)
    assert cloud is not None, "지정한 A 그룹 모델 점군을 찾을 수 없습니다."
    region = next(region for region in area.regions if region.type == "WINDOW")
    region_3d = area.spaces.active.region_3d
    stride = max(1, len(cloud.positions) // 48)
    for position in cloud.positions[::stride]:
        projected = view3d_utils.location_3d_to_region_2d(
            region,
            region_3d,
            position,
            default=None,
        )
        if projected is None:
            continue
        window_x = round(region.x + projected.x)
        window_y = round(region.y + projected.y)
        if find_viewport_under_mouse(window, window_x, window_y) is None:
            continue
        picked = pick_nearest_point(
            role,
            region,
            region_3d,
            (projected.x, projected.y),
        )
        if picked is not None and picked.model_id == model_id:
            return picked
    raise AssertionError("지정한 A 그룹 레이어에서 실제 피킹 가능한 점을 찾지 못했습니다.")


def _create_completed_pair(reference_id: int, moving_id: int):
    """실제 모달과 같은 드래프트 범위로 확정 마커 쌍 하나를 만듭니다."""
    from tools.align.constants import MARKER_COMMAND_PAIR_CREATE, ROLE_MOVING, ROLE_REFERENCE
    from tools.align.properties import sync_marker_pair_list
    from tools.align.state import RUNTIME
    from tools.align.viewport.picking import marker_source_model_id

    state = RUNTIME.marker_selection
    journal = RUNTIME.undo_journal
    baseline = state.snapshot()
    assert state.begin_create_draft() is True
    assert journal.begin_scope(baseline) is True
    reference_cloud = _marker_cloud(ROLE_REFERENCE)
    moving_cloud = _marker_cloud(ROLE_MOVING)
    for role, cloud, point_id in (
        (ROLE_REFERENCE, reference_cloud, reference_id),
        (ROLE_MOVING, moving_cloud, moving_id),
    ):
        point_index = cloud.point_ids.index(point_id)
        before = state.snapshot()
        model_id = marker_source_model_id(role)
        assert model_id is not None
        assert state.select_draft_point(
            role,
            point_id,
            cloud.positions[point_index],
            model_id=model_id,
        ) is True
        assert journal.record_draft(before, state.snapshot(), "A점 선택" if role == ROLE_REFERENCE else "P점 선택")
    pair = state.commit_draft()
    assert pair is not None
    assert journal.commit_scope(state.snapshot(), "마커 쌍 추가", MARKER_COMMAND_PAIR_CREATE)
    sync_marker_pair_list(bpy.context.window_manager)
    return pair


def _assert_no_legacy_scene_data() -> None:
    """전용 작업공간 진입·종료 중 legacy Object·Collection이 생성되지 않는지 확인합니다."""
    from tools.align.constants import LEGACY_DEMO_COLLECTION_NAMES, LEGACY_DEMO_OBJECT_NAMES

    assert all(bpy.data.collections.get(name) is None for name in LEGACY_DEMO_COLLECTION_NAMES)
    assert all(bpy.data.objects.get(name) is None for name in LEGACY_DEMO_OBJECT_NAMES)


def _scene_data_snapshot() -> tuple[frozenset[tuple[str, int]], frozenset[tuple[str, int]]]:
    """현재 Scene Object·Collection의 이름과 포인터를 순서와 무관하게 기록합니다."""
    return (
        frozenset((object_data.name, object_data.as_pointer()) for object_data in bpy.data.objects),
        frozenset((collection.name, collection.as_pointer()) for collection in bpy.data.collections),
    )


def _viewport_object_filter_snapshot(screen: bpy.types.Screen) -> tuple[tuple[tuple[str, bool], ...], ...]:
    """한 Screen의 3D 뷰별 Scene 오브젝트 표시 필터 값을 결정적 순서로 기록합니다."""

    snapshots = []
    for area in sorted(screen.areas, key=lambda item: (item.x, item.y, item.width, item.height)):
        if area.type != "VIEW_3D":
            continue
        space = area.spaces.active
        snapshots.append(
            tuple(
                (prop.identifier, bool(getattr(space, prop.identifier)))
                for prop in space.bl_rna.properties
                if prop.identifier.startswith("show_object_viewport_")
            )
        )
    return tuple(snapshots)


def _assert_scene_data_snapshot_unchanged() -> None:
    """OPEN 직전 스냅샷과 비교해 애드온이 어떤 Scene 데이터도 변경하지 않았는지 확인합니다."""
    assert _SCENE_DATA_SNAPSHOT is not None, "OPEN 직전 Scene 데이터 스냅샷이 필요합니다."
    assert _scene_data_snapshot() == _SCENE_DATA_SNAPSHOT


def _assert_scene_objects_hidden(reference_area: bpy.types.Area, moving_area: bpy.types.Area) -> None:
    """기존 Scene 오브젝트가 A/P 전용 뷰에서 타입과 관계없이 숨겨졌는지 확인합니다."""

    sentinel = bpy.data.objects.get(_UNDO_SENTINEL_NAME)
    assert sentinel is not None, "Scene 오브젝트 숨김을 확인할 기존 Empty가 필요합니다."
    for area in (reference_area, moving_area):
        space = area.spaces.active
        filter_values = tuple(
            bool(getattr(space, prop.identifier))
            for prop in space.bl_rna.properties
            if prop.identifier.startswith("show_object_viewport_")
        )
        assert filter_values, "SpaceView3D 오브젝트 표시 필터를 찾지 못했습니다."
        assert not any(filter_values), "COLMAP Align 뷰에 Blender Scene 오브젝트 타입이 남아 있습니다."
        assert sentinel.visible_get(viewport=space) is False


def _assert_role_draw_visibility(
    window: bpy.types.Window,
    reference_area: bpy.types.Area,
    moving_area: bpy.types.Area,
) -> None:
    """Area 역할이 활성 전체와 선택 비활성 렌더 필터로 분리되는지 확인합니다."""
    from tools.align.constants import DEMO_MODEL_1_ID, DEMO_MODEL_2_ID, ROLE_MOVING, ROLE_REFERENCE
    from tools.align.state import RUNTIME
    from tools.align.viewport.registry import find_window_region
    from tools.align.viewport.renderer import _active_role, render_model_ids_for_role

    with bpy.context.temp_override(
        window=window,
        screen=window.screen,
        area=reference_area,
        region=find_window_region(reference_area),
    ):
        assert _active_role() == ROLE_REFERENCE
        assert render_model_ids_for_role(ROLE_REFERENCE) == (DEMO_MODEL_1_ID, DEMO_MODEL_2_ID)
    with bpy.context.temp_override(
        window=window,
        screen=window.screen,
        area=moving_area,
        region=find_window_region(moving_area),
    ):
        assert _active_role() == ROLE_MOVING
        assert render_model_ids_for_role(ROLE_MOVING) == ()
    assert RUNTIME.model_catalog.selected_inactive_model_id is None


def _assert_single_window_layout(expected_window_count: int, *, expect_default_view: bool = True) -> None:
    """단일 창의 A/P 역할·Scene 토글 렌더링·N 패널·세션을 확인합니다."""
    from shared.blender_runtime.workspace_factory import (
        screen_matches_workspace,
        workspace_matches_spec,
    )
    from tools.align.constants import (
        CONTROL_PANEL_CATEGORY,
        MAIN_SCREEN_OWNER_KEY,
        VIEW_PRESET_VERSION,
        VIEW_PRESET_VERSION_KEY,
    )
    from tools.align.operators.workspace import _default_view_rotation
    from tools.align.state import RUNTIME
    from tools.align.ui.panels import COLMAPALIGN_PT_main
    from tools.align.viewport.registry import classify_areas, find_window_region, refresh_registry
    from tools.align.workspace_contract import ALIGN_WORKSPACE_SPEC

    assert len(_windows()) == expected_window_count
    window = _main_window()
    assert workspace_matches_spec(window.workspace, ALIGN_WORKSPACE_SPEC)
    assert screen_matches_workspace(window.screen, window.workspace)
    assert window.screen.get(MAIN_SCREEN_OWNER_KEY, False)
    assert RUNTIME.session_active is True
    assert RUNTIME.session.phase == "ACTIVE"
    assert RUNTIME.session.input_guard_active is True
    _assert_no_legacy_scene_data()
    _assert_scene_data_snapshot_unchanged()
    reference_area, moving_area = classify_areas(window.screen.areas)
    assert len(window.screen.areas) == 2
    assert reference_area.x < moving_area.x
    assert moving_area.spaces.active.show_region_ui is True
    ui_region = next(region for region in moving_area.regions if region.type == "UI")
    if not ui_region.is_property_readonly("active_panel_category"):
        assert ui_region.active_panel_category == CONTROL_PANEL_CATEGORY
    assert refresh_registry(window) is True
    assert RUNTIME.viewport_registry.is_complete() is True
    _assert_scene_objects_hidden(reference_area, moving_area)
    _assert_role_draw_visibility(window, reference_area, moving_area)
    assert window.screen.get(VIEW_PRESET_VERSION_KEY) == VIEW_PRESET_VERSION
    expected_rotation = _default_view_rotation()
    for area in (reference_area, moving_area):
        region_3d = area.spaces.active.region_3d
        assert region_3d.view_perspective == "PERSP"
        if expect_default_view:
            assert region_3d.view_rotation.rotation_difference(expected_rotation).angle < 0.0001
    with bpy.context.temp_override(
        window=window,
        screen=window.screen,
        area=reference_area,
        region=find_window_region(reference_area),
    ):
        assert COLMAPALIGN_PT_main.poll(bpy.context) is False
    with bpy.context.temp_override(
        window=window,
        screen=window.screen,
        area=moving_area,
        region=find_window_region(moving_area),
    ):
        assert COLMAPALIGN_PT_main.poll(bpy.context) is True


def _assert_ui_list_selection_guard(first_pair, second_pair) -> None:
    """UIList 동기화 중 콜백이 활성 pair_id를 덮어쓰지 않는지 확인합니다."""
    from tools.align.properties import marker_pair_list, sync_marker_pair_list
    from tools.align.state import RUNTIME

    state = RUNTIME.marker_selection
    pair_list = marker_pair_list(bpy.context.window_manager)
    assert pair_list is not None
    state.set_active_pair_id(first_pair.pair_id)
    sync_marker_pair_list(bpy.context.window_manager)
    assert [item.pair_id for item in pair_list.items] == [first_pair.pair_id, second_pair.pair_id]
    assert pair_list.active_index == 0
    RUNTIME.ui_list_syncing = True
    try:
        pair_list.active_index = 1
    finally:
        RUNTIME.ui_list_syncing = False
    assert state.active_pair_id == first_pair.pair_id
    pair_list.active_index = -1
    assert state.active_pair_id is None
    pair_list.active_index = 1
    assert state.active_pair_id == second_pair.pair_id


def _assert_marker_pair_color_contract(first_pair, second_pair) -> None:
    """GUI Blender에서 7개 이상 쌍의 공통 색상과 활성 기하 강조를 확인합니다."""
    from tools.align.constants import ROLE_MOVING, ROLE_REFERENCE, marker_pair_color
    from tools.align.properties import sync_marker_pair_list
    from tools.align.state import RUNTIME
    from tools.align.viewport.labels import _marker_label_color
    from tools.align.viewport.renderer import _marker_circle_layers, _marker_entries

    pairs = [first_pair, second_pair]
    for point_id in range(4, 9):
        pairs.append(_create_completed_pair(point_id, point_id))

    assert [pair.pair_id for pair in pairs] == list(range(1, 8))
    colors = [marker_pair_color(pair.pair_id) for pair in pairs]
    assert len(set(colors)) == len(colors)
    assert colors[0] != colors[1]
    reference_entries = {entry[1]: entry for entry in _marker_entries(ROLE_REFERENCE)}
    moving_entries = {entry[1]: entry for entry in _marker_entries(ROLE_MOVING)}
    for pair, color in zip(pairs, colors):
        reference_entry = reference_entries[pair.pair_id]
        moving_entry = moving_entries[pair.pair_id]
        reference_outer, reference_inner = _marker_circle_layers(
            pair.pair_id,
            reference_entry[2],
        )
        moving_outer, moving_inner = _marker_circle_layers(
            pair.pair_id,
            moving_entry[2],
        )
        inactive_outer, inactive_inner = _marker_circle_layers(pair.pair_id, is_active=False)
        active_outer, active_inner = _marker_circle_layers(pair.pair_id, is_active=True)
        assert reference_outer[1] == reference_inner[1] == color
        assert moving_outer[1] == moving_inner[1] == color
        assert _marker_label_color(pair.pair_id) == color
        assert active_outer[1] == active_inner[1] == inactive_outer[1] == inactive_inner[1] == color
        assert active_outer[0] > inactive_outer[0]
        assert active_outer[2] > inactive_outer[2]

    RUNTIME.marker_selection.set_active_pair_id(second_pair.pair_id)
    sync_marker_pair_list(bpy.context.window_manager)


_BASE_WINDOW_COUNT = 0
_SOURCE_WORKSPACE = None
_SOURCE_SCREEN_POINTER = 0
_STAGE = "OPEN"
_CUSTOM_VIEW_ROTATION = None
_FIRST_PAIR = None
_SECOND_PAIR = None
_UNDO_SENTINEL_NAME = "COLMAP_ALIGN_SESSION_UNDO_SENTINEL"
_EMPTY_CLICK_SNAPSHOT = None
_SPACE_NAVIGATION_SNAPSHOT = None
_SPACE_NAVIGATION_ROTATION = None
_SPACE_NAVIGATION_INVOKE_COUNT = 0
_SPACE_NAVIGATION_PROBE_COUNT = 0
_SPACE_NAVIGATION_RELEASE_COUNT = 0
_RESELECT_SNAPSHOT = None
_NONBUTTON_EXIT_SNAPSHOT = None
_MOVING_PICK_POINT_ID = None
_SCENE_DATA_SNAPSHOT = None
_SOURCE_VIEWPORT_FILTER_SNAPSHOT = None


def _finish_gui_test(succeeded: bool) -> None:
    """애드온을 해제하고 GUI 검사 결과를 출력한 뒤 Blender를 종료합니다."""
    from tools.align import registration
    from tools.align.state import RUNTIME

    try:
        _remove_navigation_probe()
        if registration.is_registered():
            registration.unregister()
        if succeeded:
            assert len(_windows()) == _BASE_WINDOW_COUNT
            assert RUNTIME.draw_handlers == []
            assert RUNTIME.modal_active is False
            assert RUNTIME.viewport_registry.is_complete() is False
            assert RUNTIME.session_active is False
            assert RUNTIME.undo_journal.undo_count == 0
            assert RUNTIME.undo_journal.redo_count == 0
            _assert_scene_data_snapshot_unchanged()
    except Exception:
        traceback.print_exc()
        succeeded = False
    finally:
        if succeeded:
            print("test_workspace_gui: 통과")
        print(f"GUI_TEST_RESULT={'PASS' if succeeded else 'FAIL'}")
        bpy.ops.wm.quit_blender()


def _run_gui_test_step() -> float | None:
    """작업공간, 모달 Ctrl+Z, UIList, 편집·삭제와 종료를 단계적으로 검증합니다."""
    global _STAGE, _CUSTOM_VIEW_ROTATION, _FIRST_PAIR, _SECOND_PAIR
    global _EMPTY_CLICK_SNAPSHOT, _SPACE_NAVIGATION_SNAPSHOT, _SPACE_NAVIGATION_ROTATION
    global _SPACE_NAVIGATION_INVOKE_COUNT, _SPACE_NAVIGATION_PROBE_COUNT
    global _SPACE_NAVIGATION_RELEASE_COUNT, _RESELECT_SNAPSHOT
    global _NONBUTTON_EXIT_SNAPSHOT, _MOVING_PICK_POINT_ID
    global _SCENE_DATA_SNAPSHOT, _SOURCE_VIEWPORT_FILTER_SNAPSHOT
    from tools.align.constants import DEMO_MODEL_3_ID, ROLE_MOVING, ROLE_REFERENCE
    from tools.align.state import RUNTIME
    from tools.align.viewport.registry import classify_areas

    try:
        if RUNTIME.workspace_setup_task is not None:
            return 0.1
        if RUNTIME.workspace_setup_error:
            raise AssertionError(RUNTIME.workspace_setup_error)

        if _STAGE == "OPEN":
            _SCENE_DATA_SNAPSHOT = _scene_data_snapshot()
            result = bpy.ops.colmap_align.open_workspace()
            assert "FINISHED" in result
            _STAGE = "WAIT_OPEN"
            return 0.1

        if _STAGE == "WAIT_OPEN":
            from tools.align.operators.workspace import _process_sidebar_activation

            if bpy.app.timers.is_registered(_process_sidebar_activation):
                return 0.1
            _assert_single_window_layout(_BASE_WINDOW_COUNT)
            window = _main_window()
            _simulate_shortcut(window, "PAGE_UP", ctrl=True)
            _STAGE = "WAIT_PAGE_UP_BLOCK"
            return 0.1

        if _STAGE == "WAIT_PAGE_UP_BLOCK":
            window = _main_window()
            assert RUNTIME.session.is_align_workspace(window.workspace)
            assert "작업공간 순환" in RUNTIME.marker_selection.message
            _simulate_shortcut(window, "PAGE_DOWN", ctrl=True)
            _STAGE = "WAIT_PAGE_DOWN_BLOCK"
            return 0.1

        if _STAGE == "WAIT_PAGE_DOWN_BLOCK":
            window = _main_window()
            assert RUNTIME.session.is_align_workspace(window.workspace)
            assert "작업공간 순환" in RUNTIME.marker_selection.message
            window.workspace = _SOURCE_WORKSPACE
            _STAGE = "WAIT_WORKSPACE_RECOVERY"
            return 0.1

        if _STAGE == "WAIT_WORKSPACE_RECOVERY":
            from tools.align.operators.workspace import _process_sidebar_activation
            from tools.align.session import _recover_workspace_after_change

            window = _windows()[0]
            if not RUNTIME.session.is_align_workspace(window.workspace):
                return 0.1
            if bpy.app.timers.is_registered(_recover_workspace_after_change):
                return 0.1
            if bpy.app.timers.is_registered(_process_sidebar_activation):
                return 0.1
            assert RUNTIME.session.recovery_pending is False
            assert "작업공간 전환을 감지" in RUNTIME.session.message
            _assert_single_window_layout(_BASE_WINDOW_COUNT)
            window = _main_window()
            reference_area, moving_area = classify_areas(window.screen.areas)
            assert len(window.modal_operators) == 1
            result = _invoke_in_area(
                window,
                moving_area,
                "colmap_align.select_data_layer",
                model_id=DEMO_MODEL_3_ID,
            )
            assert "FINISHED" in result
            assert RUNTIME.model_catalog.selected_inactive_model_id == DEMO_MODEL_3_ID
            result = _invoke_in_area(
                window,
                moving_area,
                "colmap_align.add_marker_pair",
                "INVOKE_DEFAULT",
            )
            assert "FINISHED" in result
            assert RUNTIME.modal_active is True
            assert RUNTIME.marker_selection.has_draft is True
            assert RUNTIME.marker_selection.phase == "CAPTURING"
            _simulate_npanel_click(window, moving_area)
            _STAGE = "WAIT_NPANEL_UI_CLICK"
            return 0.1

        if _STAGE == "WAIT_NPANEL_UI_CLICK":
            assert RUNTIME.modal_active is True
            window = _main_window()
            _reference_area, moving_area = classify_areas(window.screen.areas)
            _assert_npanel_excluded_from_viewport_picking(window, moving_area)
            _NONBUTTON_EXIT_SNAPSHOT = (
                RUNTIME.marker_selection.snapshot(),
                RUNTIME.undo_journal.draft_undo_count,
                RUNTIME.undo_journal.draft_redo_count,
            )
            _simulate_viewport_mouse_event(window, moving_area, "RIGHTMOUSE")
            _STAGE = "WAIT_MODAL_RIGHTMOUSE"
            return 0.1

        if _STAGE == "WAIT_MODAL_RIGHTMOUSE":
            assert RUNTIME.modal_active is True
            assert _NONBUTTON_EXIT_SNAPSHOT is not None
            assert (
                RUNTIME.marker_selection.snapshot(),
                RUNTIME.undo_journal.draft_undo_count,
                RUNTIME.undo_journal.draft_redo_count,
            ) == _NONBUTTON_EXIT_SNAPSHOT
            window = _main_window()
            _simulate_shortcut(window, "ESC")
            _STAGE = "WAIT_MODAL_ESCAPE"
            return 0.1

        if _STAGE == "WAIT_MODAL_ESCAPE":
            assert RUNTIME.modal_active is True
            assert _NONBUTTON_EXIT_SNAPSHOT is not None
            assert (
                RUNTIME.marker_selection.snapshot(),
                RUNTIME.undo_journal.draft_undo_count,
                RUNTIME.undo_journal.draft_redo_count,
            ) == _NONBUTTON_EXIT_SNAPSHOT
            window = _main_window()
            _reference_area, moving_area = classify_areas(window.screen.areas)
            _EMPTY_CLICK_SNAPSHOT = (
                RUNTIME.marker_selection.snapshot(),
                RUNTIME.undo_journal.draft_undo_count,
                RUNTIME.undo_journal.draft_redo_count,
            )
            _simulate_empty_viewport_click(window, moving_area, ROLE_MOVING)
            _STAGE = "WAIT_EMPTY_VIEWPORT_CLICK"
            return 0.1

        if _STAGE == "WAIT_EMPTY_VIEWPORT_CLICK":
            assert RUNTIME.modal_active is True
            assert _EMPTY_CLICK_SNAPSHOT is not None
            assert (
                RUNTIME.marker_selection.snapshot(),
                RUNTIME.undo_journal.draft_undo_count,
                RUNTIME.undo_journal.draft_redo_count,
            ) == _EMPTY_CLICK_SNAPSHOT
            window = _main_window()
            _reference_area, moving_area = classify_areas(window.screen.areas)
            moving_cloud = _marker_cloud(ROLE_MOVING)
            _SPACE_NAVIGATION_SNAPSHOT = (
                RUNTIME.marker_selection.snapshot(),
                RUNTIME.undo_journal.draft_undo_count,
                RUNTIME.undo_journal.draft_redo_count,
            )
            _SPACE_NAVIGATION_ROTATION = moving_area.spaces.active.region_3d.view_rotation.copy()
            _SPACE_NAVIGATION_INVOKE_COUNT = _NAVIGATION_PROBE_INVOKED
            _SPACE_NAVIGATION_PROBE_COUNT = _NAVIGATION_PROBE_RECEIVED
            _SPACE_NAVIGATION_RELEASE_COUNT = _NAVIGATION_PROBE_RELEASE_CONSUMED
            _simulate_space_navigation_click(window, moving_area, moving_cloud.positions[0])
            _STAGE = "WAIT_SPACE_NAVIGATION_CLICK"
            return 0.1

        if _STAGE == "WAIT_SPACE_NAVIGATION_CLICK":
            assert RUNTIME.modal_active is True
            window = _main_window()
            assert _SPACE_NAVIGATION_SNAPSHOT is not None
            assert (
                RUNTIME.marker_selection.snapshot(),
                RUNTIME.undo_journal.draft_undo_count,
                RUNTIME.undo_journal.draft_redo_count,
            ) == _SPACE_NAVIGATION_SNAPSHOT
            assert _NAVIGATION_PROBE_INVOKED == _SPACE_NAVIGATION_INVOKE_COUNT + 1
            assert _NAVIGATION_PROBE_RECEIVED == _SPACE_NAVIGATION_PROBE_COUNT + 1
            assert _NAVIGATION_PROBE_RELEASE_CONSUMED == _SPACE_NAVIGATION_RELEASE_COUNT + 1
            assert _navigation_probe_is_modal(window) is False
            assert _SPACE_NAVIGATION_ROTATION is not None
            _reference_area, moving_area = classify_areas(window.screen.areas)
            current_rotation = moving_area.spaces.active.region_3d.view_rotation
            assert current_rotation.rotation_difference(_SPACE_NAVIGATION_ROTATION).angle > 0.01
            moving_model_id = RUNTIME.model_catalog.selected_inactive_model_id
            assert moving_model_id is not None
            moving_pick = _pickable_model_point(
                window,
                moving_area,
                ROLE_MOVING,
                moving_model_id,
            )
            _MOVING_PICK_POINT_ID = moving_pick.point3d_id
            _simulate_point_click(window, moving_area, moving_pick.position)
            _STAGE = "WAIT_MODAL_P_PICK"
            return 0.1

        if _STAGE == "WAIT_MODAL_P_PICK":
            window = _main_window()
            draft = RUNTIME.marker_selection.draft
            assert draft is not None and draft.moving is not None
            assert draft.moving.point3d_id == _MOVING_PICK_POINT_ID
            assert RUNTIME.undo_journal.draft_undo_count == 1
            assert _remove_navigation_probe() is True
            _RESELECT_SNAPSHOT = (
                RUNTIME.marker_selection.snapshot(),
                RUNTIME.undo_journal.draft_undo_count,
                RUNTIME.undo_journal.draft_redo_count,
            )
            _reference_area, moving_area = classify_areas(window.screen.areas)
            _simulate_point_click(window, moving_area, draft.moving.local_position)
            _STAGE = "WAIT_MODAL_RESELECT_REJECTION"
            return 0.1

        if _STAGE == "WAIT_MODAL_RESELECT_REJECTION":
            assert _RESELECT_SNAPSHOT is not None
            assert (
                RUNTIME.marker_selection.snapshot(),
                RUNTIME.undo_journal.draft_undo_count,
                RUNTIME.undo_journal.draft_redo_count,
            ) == _RESELECT_SNAPSHOT
            window = _main_window()
            _simulate_shortcut(window, "Z", ctrl=True)
            _STAGE = "WAIT_MODAL_DRAFT_UNDO"
            return 0.1

        if _STAGE == "WAIT_MODAL_DRAFT_UNDO":
            window = _main_window()
            assert RUNTIME.modal_active is True
            assert RUNTIME.marker_selection.draft is not None
            assert RUNTIME.marker_selection.draft.moving is None
            assert RUNTIME.undo_journal.draft_redo_count == 1
            assert bpy.data.objects.get(_UNDO_SENTINEL_NAME) is not None
            _simulate_shortcut(window, "Z", ctrl=True, shift=True)
            _STAGE = "WAIT_MODAL_DRAFT_REDO"
            return 0.1

        if _STAGE == "WAIT_MODAL_DRAFT_REDO":
            from tools.align.constants import DEMO_MODEL_2_ID

            window = _main_window()
            assert RUNTIME.marker_selection.draft is not None
            assert RUNTIME.marker_selection.draft.moving is not None
            reference_area, _moving_area = classify_areas(window.screen.areas)
            reference_pick = _pickable_model_point(
                window,
                reference_area,
                ROLE_REFERENCE,
                DEMO_MODEL_2_ID,
            )
            _simulate_point_click(window, reference_area, reference_pick.position)
            _STAGE = "WAIT_MODAL_A_PICK"
            return 0.1

        if _STAGE == "WAIT_MODAL_A_PICK":
            from tools.align.constants import DEMO_MODEL_2_ID

            window = _main_window()
            draft = RUNTIME.marker_selection.draft
            assert draft is not None and draft.reference is not None
            assert draft.reference.model_id == DEMO_MODEL_2_ID
            assert RUNTIME.marker_selection.completed_pairs == []
            assert RUNTIME.marker_selection.can_confirm_draft() is True
            _reference_area, moving_area = classify_areas(window.screen.areas)
            result = _invoke_in_area(window, moving_area, "colmap_align.confirm_marker_selection")
            assert "FINISHED" in result
            _STAGE = "WAIT_FIRST_CONFIRM"
            return 0.1

        if _STAGE == "WAIT_FIRST_CONFIRM":
            if RUNTIME.modal_active:
                return 0.1
            _FIRST_PAIR = RUNTIME.marker_selection.get_pair(1)
            assert _FIRST_PAIR is not None
            _SECOND_PAIR = _create_completed_pair(2, 2)
            _assert_ui_list_selection_guard(_FIRST_PAIR, _SECOND_PAIR)
            _assert_marker_pair_color_contract(_FIRST_PAIR, _SECOND_PAIR)
            window = _main_window()
            _reference_area, moving_area = classify_areas(window.screen.areas)
            result = _invoke_in_area(
                window,
                moving_area,
                "colmap_align.edit_marker_pair",
                "INVOKE_DEFAULT",
            )
            assert "FINISHED" in result
            assert RUNTIME.modal_active is True
            assert RUNTIME.marker_selection.draft is not None
            assert RUNTIME.marker_selection.draft.source_pair_id == _SECOND_PAIR.pair_id
            assert RUNTIME.marker_selection.draft.reference == _SECOND_PAIR.reference
            assert RUNTIME.marker_selection.draft.moving == _SECOND_PAIR.moving
            assert RUNTIME.marker_selection.can_confirm_draft() is False
            _STAGE = "EDIT_CHANGE_A"
            return 0.1

        if _STAGE == "EDIT_CHANGE_A":
            window = _main_window()
            state = RUNTIME.marker_selection
            reference_cloud = _marker_cloud(ROLE_REFERENCE)
            from tools.align.viewport.picking import marker_source_model_id

            reference_model_id = marker_source_model_id(ROLE_REFERENCE)
            assert reference_model_id is not None
            before = state.snapshot()
            assert state.select_draft_point(
                ROLE_REFERENCE,
                reference_cloud.point_ids[3],
                reference_cloud.positions[3],
                model_id=reference_model_id,
            )
            assert RUNTIME.undo_journal.record_draft(before, state.snapshot(), "A점 선택")
            assert state.can_confirm_draft() is True
            _STAGE = "EDIT_RETURN_ORIGINAL"
            return 0.1

        if _STAGE == "EDIT_RETURN_ORIGINAL":
            window = _main_window()
            state = RUNTIME.marker_selection
            assert _SECOND_PAIR is not None
            before = state.snapshot()
            assert state.select_draft_point(
                ROLE_REFERENCE,
                _SECOND_PAIR.reference.point3d_id,
                _SECOND_PAIR.reference.local_position,
                model_id=_SECOND_PAIR.reference.model_id,
            )
            assert RUNTIME.undo_journal.record_draft(before, state.snapshot(), "A점 선택")
            assert state.can_confirm_draft() is False
            _simulate_shortcut(window, "Z", ctrl=True)
            _STAGE = "WAIT_EDIT_RETURN_UNDO"
            return 0.1

        if _STAGE == "WAIT_EDIT_RETURN_UNDO":
            window = _main_window()
            assert RUNTIME.marker_selection.can_confirm_draft() is True
            _simulate_shortcut(window, "Z", ctrl=True, shift=True)
            _STAGE = "WAIT_EDIT_RETURN_REDO"
            return 0.1

        if _STAGE == "WAIT_EDIT_RETURN_REDO":
            window = _main_window()
            assert RUNTIME.marker_selection.can_confirm_draft() is False
            _simulate_shortcut(window, "Z", ctrl=True)
            _STAGE = "WAIT_EDIT_CHANGE_RESTORED"
            return 0.1

        if _STAGE == "WAIT_EDIT_CHANGE_RESTORED":
            window = _main_window()
            assert RUNTIME.marker_selection.can_confirm_draft() is True
            _reference_area, moving_area = classify_areas(window.screen.areas)
            result = _invoke_in_area(window, moving_area, "colmap_align.confirm_marker_selection")
            assert "FINISHED" in result
            _STAGE = "WAIT_EDIT_CONFIRM"
            return 0.1

        if _STAGE == "WAIT_EDIT_CONFIRM":
            if RUNTIME.modal_active:
                return 0.1
            edited = RUNTIME.marker_selection.get_pair(_SECOND_PAIR.pair_id)
            assert edited is not None and edited.reference.point3d_id == 3
            assert edited.moving == _SECOND_PAIR.moving
            window = _main_window()
            _reference_area, moving_area = classify_areas(window.screen.areas)
            result = _invoke_in_area(window, moving_area, "colmap_align.delete_marker_pair")
            assert "FINISHED" in result
            assert RUNTIME.marker_selection.get_pair(_SECOND_PAIR.pair_id) is None
            _simulate_shortcut(window, "Z", ctrl=True)
            _STAGE = "WAIT_DELETE_UNDO"
            return 0.1

        if _STAGE == "WAIT_DELETE_UNDO":
            restored = RUNTIME.marker_selection.get_pair(_SECOND_PAIR.pair_id)
            if restored is None:
                return 0.1
            assert restored.reference.point3d_id == 3
            window = _main_window()
            reference_area, _moving_area = classify_areas(window.screen.areas)
            _CUSTOM_VIEW_ROTATION = Euler((0.35, 0.45, 0.55)).to_quaternion()
            reference_area.spaces.active.region_3d.view_rotation = _CUSTOM_VIEW_ROTATION
            reference_area.tag_redraw()
            _STAGE = "WAIT_CUSTOM_VIEW_FLUSH"
            return 0.1

        if _STAGE == "WAIT_CUSTOM_VIEW_FLUSH":
            window = _main_window()
            reference_area, _moving_area = classify_areas(window.screen.areas)
            assert reference_area.spaces.active.region_3d.view_rotation.rotation_difference(
                _CUSTOM_VIEW_ROTATION
            ).angle < 0.0001
            result = _invoke_in_area(window, reference_area, "colmap_align.exit_session")
            assert "FINISHED" in result
            _STAGE = "WAIT_EXIT"
            return 0.1

        if _STAGE == "WAIT_EXIT":
            window = _windows()[0]
            if RUNTIME.session.phase != "INACTIVE":
                return 0.1
            assert window.workspace.as_pointer() == _SOURCE_WORKSPACE.as_pointer()
            assert window.screen.as_pointer() == _SOURCE_SCREEN_POINTER
            assert _viewport_object_filter_snapshot(window.screen) == _SOURCE_VIEWPORT_FILTER_SNAPSHOT
            assert RUNTIME.undo_journal.undo_count == 0
            assert RUNTIME.undo_journal.redo_count == 0
            _assert_no_legacy_scene_data()
            _assert_scene_data_snapshot_unchanged()
            result = bpy.ops.colmap_align.open_workspace()
            assert "FINISHED" in result
            _STAGE = "WAIT_REENTER"
            return 0.1

        if _STAGE == "WAIT_REENTER":
            from tools.align.properties import sync_marker_pair_list

            _assert_single_window_layout(_BASE_WINDOW_COUNT, expect_default_view=False)
            window = _main_window()
            reference_area, moving_area = classify_areas(window.screen.areas)
            rotation_error = reference_area.spaces.active.region_3d.view_rotation.rotation_difference(
                _CUSTOM_VIEW_ROTATION
            ).angle
            assert rotation_error < 0.0001, "재진입 뒤 사용자가 조정한 A 뷰 회전이 보존돼야 합니다."
            # 재진입 모달 정리는 선택 잠금과 무관하게 검사하려고 테스트 마커만 비웁니다.
            RUNTIME.marker_selection.clear()
            RUNTIME.undo_journal.clear()
            sync_marker_pair_list(bpy.context.window_manager)
            result = _invoke_in_area(
                window,
                moving_area,
                "colmap_align.select_data_layer",
                model_id=DEMO_MODEL_3_ID,
            )
            assert "FINISHED" in result
            assert RUNTIME.model_catalog.selected_inactive_model_id == DEMO_MODEL_3_ID
            result = _invoke_in_area(
                window,
                moving_area,
                "colmap_align.add_marker_pair",
                "INVOKE_DEFAULT",
            )
            assert "FINISHED" in result
            assert RUNTIME.modal_active is True
            assert RUNTIME.undo_journal.scope_active is True
            assert RUNTIME.marker_modal_timer is not None
            _STAGE = "UNREGISTER_ACTIVE_MODAL"
            return 0.1

        if _STAGE == "UNREGISTER_ACTIVE_MODAL":
            from tools.align import registration

            registration.unregister()
            _STAGE = "WAIT_UNREGISTER_ACTIVE_MODAL"
            return 0.1

        if _STAGE == "WAIT_UNREGISTER_ACTIVE_MODAL":
            window = _windows()[0]
            assert window.workspace.as_pointer() == _SOURCE_WORKSPACE.as_pointer()
            assert RUNTIME.modal_active is False
            assert RUNTIME.marker_modal_timer is None
            assert RUNTIME.undo_journal.scope_active is False
            assert RUNTIME.undo_journal.undo_count == 0
            assert RUNTIME.undo_journal.redo_count == 0
            assert RUNTIME.marker_selection.has_draft is False
            assert RUNTIME.session.phase == "INACTIVE"
            _assert_no_legacy_scene_data()
            _assert_scene_data_snapshot_unchanged()
            assert not any(
                operator.bl_idname.startswith("colmap_align.")
                for operator in window.modal_operators
            )
            _finish_gui_test(True)
            return None

        raise AssertionError(f"알 수 없는 GUI 테스트 단계입니다: {_STAGE}")
    except Exception:
        traceback.print_exc()
        _finish_gui_test(False)
    return None


def schedule_gui_test() -> None:
    """GUI 이벤트 루프 준비 뒤 단일 창 듀얼 뷰 회귀 검사를 예약합니다."""
    global _BASE_WINDOW_COUNT, _SOURCE_SCREEN_POINTER, _SOURCE_WORKSPACE
    global _SOURCE_VIEWPORT_FILTER_SNAPSHOT
    global _NAVIGATION_PROBE_INVOKED, _NAVIGATION_PROBE_RECEIVED, _NAVIGATION_PROBE_RELEASE_CONSUMED
    _add_source_path()
    if bpy.app.background:
        print("test_workspace_gui: GUI Blender에서만 실행합니다.")
        print("GUI_TEST_RESULT=PASS")
        return
    from tools.align import registration
    from tools.align.operators.demo_data import install_demo_fixture

    registration.register()
    install_demo_fixture()
    _assert_no_legacy_scene_data()
    _NAVIGATION_PROBE_INVOKED = 0
    _NAVIGATION_PROBE_RECEIVED = 0
    _NAVIGATION_PROBE_RELEASE_CONSUMED = 0
    bpy.ops.object.empty_add(type="PLAIN_AXES", location=(100.0, 100.0, 100.0))
    bpy.context.active_object.name = _UNDO_SENTINEL_NAME
    _BASE_WINDOW_COUNT = len(_windows())
    _SOURCE_SCREEN_POINTER = bpy.context.window.screen.as_pointer()
    _SOURCE_WORKSPACE = bpy.context.window.workspace
    _SOURCE_VIEWPORT_FILTER_SNAPSHOT = _viewport_object_filter_snapshot(bpy.context.window.screen)
    bpy.app.timers.register(_run_gui_test_step, first_interval=0.2)


if __name__ == "__main__":
    schedule_gui_test()
