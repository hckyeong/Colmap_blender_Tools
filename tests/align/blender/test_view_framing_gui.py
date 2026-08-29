"""실제 Blender GUI에서 A/P 독립 Frame All과 Home·등록 계약을 검증합니다."""

from __future__ import annotations

import sys
import traceback
from pathlib import Path

import bpy


_STAGE = "OPEN"
_REGISTERED = False
_SCENE_IDENTIFIERS_BEFORE = ((), ())
_KEYMAP_SIGNATURES_BEFORE = ()
_SCENE_IDENTIFIERS_BEFORE_FRAME = ((), ())
_KEYMAP_SIGNATURES_BEFORE_FRAME = ()
_REFERENCE_BEFORE_HOME = None
_MOVING_BEFORE_HOME = None
_REFERENCE_EXPECTED_HOME = None
_MOVING_EXPECTED_HOME = None


def _add_source_path() -> None:
    """애드온 소스 디렉터리를 테스트 모듈 탐색 경로에 추가합니다."""
    source_path = Path(__file__).resolve().parents[3]
    source_text = str(source_path)
    if source_text not in sys.path:
        sys.path.insert(0, source_text)


def _snapshot(area):
    """Area RegionView3D의 프레이밍 전후 비교용 네 가지 시점 속성을 복사합니다."""
    region_3d = area.spaces.active.region_3d
    return (
        tuple(float(value) for value in region_3d.view_location),
        float(region_3d.view_distance),
        tuple(float(value) for value in region_3d.view_rotation),
        region_3d.view_perspective,
    )


def _assert_view_equal(before, after) -> None:
    """A/P 상대 시점 보존을 수치 오차 안에서 확인합니다."""
    assert before[2] == after[2]
    assert before[3] == after[3]
    assert before[0] == after[0]
    assert before[1] == after[1]


def _scene_data_identifiers() -> tuple[tuple[tuple[str, int], ...], tuple[tuple[str, int], ...]]:
    """프레이밍 전후 Scene Object·Collection의 이름과 RNA 식별자를 반환합니다."""
    return (
        tuple(sorted((item.name, int(item.as_pointer())) for item in bpy.data.objects)),
        tuple(sorted((item.name, int(item.as_pointer())) for item in bpy.data.collections)),
    )


def _keymap_item_signature(item) -> tuple:
    """KeyMapItem의 동작·수정자·속성 값을 비교 가능한 서명으로 만듭니다."""
    properties = getattr(item, "properties", None)
    property_values = ()
    if properties is not None:
        property_values = tuple(
            (prop.identifier, repr(getattr(properties, prop.identifier)))
            for prop in properties.bl_rna.properties
            if prop.identifier != "rna_type"
        )
    return (
        item.idname,
        item.type,
        item.value,
        getattr(item, "map_type", ""),
        getattr(item, "propvalue", ""),
        bool(getattr(item, "any", False)),
        bool(getattr(item, "shift", False)),
        bool(getattr(item, "ctrl", False)),
        bool(getattr(item, "alt", False)),
        bool(getattr(item, "oskey", False)),
        getattr(item, "key_modifier", ""),
        getattr(item, "direction", ""),
        bool(getattr(item, "repeat", False)),
        property_values,
    )


def _keymap_signatures() -> tuple:
    """사용자·애드온 KeyConfig의 모든 KeyMapItem 서명을 반환합니다."""
    window_manager = bpy.context.window_manager
    signatures = []
    for keyconfig_name in ("user", "addon"):
        keyconfig = getattr(window_manager.keyconfigs, keyconfig_name, None)
        keymap_signatures = ()
        if keyconfig is not None:
            keymap_signatures = tuple(
                (
                    keymap.name,
                    keymap.space_type,
                    keymap.region_type,
                    bool(getattr(keymap, "is_modal", False)),
                    tuple(_keymap_item_signature(item) for item in keymap.keymap_items),
                )
                for keymap in keyconfig.keymaps
            )
        signatures.append((keyconfig_name, keymap_signatures))
    return tuple(signatures)


def _set_arbitrary_view(area, location, distance) -> None:
    """검증용 임의 위치·거리를 기록하고 회전·투영 모드는 그대로 둡니다."""
    region_3d = area.spaces.active.region_3d
    region_3d.view_location = location
    region_3d.view_distance = distance


def _assert_view_changed(before, after) -> None:
    """프레이밍 대상의 위치 또는 거리 중 하나가 실제로 바뀌었는지 확인합니다."""
    assert before[0] != after[0] or before[1] != after[1]


def _call_view_menu(window, area) -> None:
    """실제 A/P VIEW3D 컨텍스트에서 Blender View 메뉴를 호출합니다."""
    from tools.align.viewport.registry import find_window_region

    region = find_window_region(area)
    assert region is not None
    with bpy.context.temp_override(
        window=window,
        screen=window.screen,
        area=area,
        region=region,
    ):
        result = bpy.ops.wm.call_menu(name="VIEW3D_MT_view")
    assert result


def _assert_view_menu_callback_lifecycle(registration, window, area) -> None:
    """실제 View 메뉴 호출에서 중복·잔류와 최상단 Frame All 항목 계약을 검증합니다."""
    original_callback = registration.draw_frame_all_view_menu
    callback_calls = []
    recorded_draws = []

    class RecordedOperator:
        """메뉴 항목에 기록되는 역할 속성을 보관합니다."""

        role = ""

    class RecordingLayout:
        """Frame All callback이 그리는 항목 순서와 인수를 기록합니다."""

        def __init__(self):
            self.operations = []

        def operator(self, operator_id, *, text, icon):
            """메뉴 operator 호출 인수와 반환 속성을 기록합니다."""
            properties = RecordedOperator()
            self.operations.append(("operator", operator_id, text, icon, properties))
            return properties

        def separator(self):
            """메뉴 구분선의 상대 순서를 기록합니다."""
            self.operations.append(("separator",))

    class RecordingMenu:
        """실제 callback을 한 번 더 실행할 기록용 메뉴를 제공합니다."""

        def __init__(self):
            self.layout = RecordingLayout()

    def tracked_callback(menu, context):
        """실제 메뉴를 그린 뒤 같은 컨텍스트의 항목 계약을 기록합니다."""
        callback_calls.append(True)
        original_callback(menu, context)
        recording_menu = RecordingMenu()
        original_callback(recording_menu, context)
        recorded_draws.append(tuple(recording_menu.layout.operations))

    registration._remove_view_menu_if_present()
    registration.draw_frame_all_view_menu = tracked_callback
    try:
        registration._prepend_view_menu_once()
        registration._prepend_view_menu_once()
        _call_view_menu(window, area)
        assert callback_calls == [True]
        assert len(recorded_draws) == 1
        operations = recorded_draws[0]
        assert len(operations) == 2
        assert operations[0][:4] == (
            "operator",
            "colmap_align.fit_view",
            "A 그룹 전체 보기",
            "HOME",
        )
        assert operations[0][4].role == "REFERENCE"
        assert operations[1] == ("separator",)

        registration._remove_view_menu_if_present()
        callback_calls.clear()
        _call_view_menu(window, area)
        assert callback_calls == []
    finally:
        registration.draw_frame_all_view_menu = original_callback
        registration._prepend_view_menu_once()


def _queue_home_event(window, area, value: str) -> None:
    """마우스 아래 Area 좌표로 HOME 입력을 큐에 넣어 다음 타이머 단계에서 처리합니다."""
    from tools.align.viewport.registry import find_window_region

    region = find_window_region(area)
    assert region is not None
    x = int(region.x + region.width // 2)
    y = int(region.y + region.height // 2)
    try:
        window.event_simulate(type="HOME", value=value, x=x, y=y)
    except (AttributeError, RuntimeError, TypeError, ValueError) as error:
        raise AssertionError("실제 HOME event_simulate 호출에 실패했습니다.") from error


def _focus_viewport(window, area) -> None:
    """키 이벤트에 앞서 실제 마우스 이동으로 대상 A/P 뷰포트를 활성화합니다."""
    from tools.align.viewport.registry import find_window_region

    region = find_window_region(area)
    assert region is not None
    x = int(region.x + region.width // 2)
    y = int(region.y + region.height // 2)
    window.event_simulate(type="MOUSEMOVE", value="NOTHING", x=x, y=y)


def _fit_via_operator(window, area, role) -> tuple[float, float, float, float]:
    """메뉴가 호출하는 fit_view 오퍼레이터를 해당 Area override에서 실행합니다."""
    from tools.align.viewport.registry import find_window_region

    region = find_window_region(area)
    assert region is not None
    with bpy.context.temp_override(
        window=window,
        screen=window.screen,
        area=area,
        region=region,
    ):
        result = bpy.ops.colmap_align.fit_view("EXEC_DEFAULT", role=role)
    assert "FINISHED" in result
    return _snapshot(area)


def _run_gui_test_step() -> float | None:
    """작업공간 진입 뒤 A/P 독립 프레이밍·Home·operator·등록 해제를 단계별로 검증합니다."""
    global _STAGE, _REGISTERED
    global _SCENE_IDENTIFIERS_BEFORE_FRAME, _KEYMAP_SIGNATURES_BEFORE_FRAME
    global _REFERENCE_BEFORE_HOME, _MOVING_BEFORE_HOME
    global _REFERENCE_EXPECTED_HOME, _MOVING_EXPECTED_HOME
    try:
        from tools.align import registration
        from tools.align.constants import ROLE_MOVING, ROLE_REFERENCE
        from tools.align.operators import CLASSES
        from tools.align.operators.view_framing import (
            COLMAPALIGN_GGT_frame_all,
        )
        from tools.align.operators.workspace import find_main_window, fit_role_view
        from tools.align.state import RUNTIME
        from tools.align.viewport.registry import classify_areas, find_window_region
        from tools.align.viewport.renderer import render_model_ids_for_role

        if _STAGE == "OPEN":
            result = bpy.ops.colmap_align.open_workspace("EXEC_DEFAULT")
            assert "FINISHED" in result
            _STAGE = "WAIT_OPEN"
            return 0.1

        if _STAGE == "WAIT_OPEN":
            if RUNTIME.workspace_setup_task is not None:
                return 0.1
            assert not RUNTIME.workspace_setup_error, RUNTIME.workspace_setup_error
            window = find_main_window(bpy.context.window_manager)
            assert window is not None
            assert RUNTIME.session_active is True
            reference_area, moving_area = classify_areas(window.screen.areas)
            assert reference_area is not None and moving_area is not None
            with bpy.context.temp_override(
                window=window,
                screen=window.screen,
                area=reference_area,
                region=find_window_region(reference_area),
            ):
                assert COLMAPALIGN_GGT_frame_all.poll(bpy.context) is True
            with bpy.context.temp_override(
                window=window,
                screen=window.screen,
                area=moving_area,
                region=find_window_region(moving_area),
            ):
                assert COLMAPALIGN_GGT_frame_all.poll(bpy.context) is True
            assert COLMAPALIGN_GGT_frame_all.poll(type("Context", (), {"area": None, "region": None})()) is False
            _STAGE = "FIT"
            return 0.1

        if _STAGE == "FIT":
            window = find_main_window(bpy.context.window_manager)
            assert window is not None
            reference_area, moving_area = classify_areas(window.screen.areas)
            assert reference_area is not None and moving_area is not None
            assert COLMAPALIGN_GGT_frame_all in CLASSES
            assert registration._VIEW_MENU_REGISTERED is True

            _SCENE_IDENTIFIERS_BEFORE_FRAME = _scene_data_identifiers()
            _KEYMAP_SIGNATURES_BEFORE_FRAME = _keymap_signatures()

            assert RUNTIME.model_catalog.select_model(
                "demo_3",
                has_marker_draft=False,
                completed_pair_count=0,
            ) is True
            assert RUNTIME.model_catalog.toggle_alignment_selection(
                "demo_4",
                has_marker_draft=False,
                completed_pair_count=0,
            ) is True
            assert render_model_ids_for_role(ROLE_MOVING) == ("demo_3", "demo_4")

            _set_arbitrary_view(reference_area, (100.0, 120.0, 140.0), 1.0)
            _set_arbitrary_view(moving_area, (-100.0, -120.0, -140.0), 2.0)
            moving_before = _snapshot(moving_area)
            reference_before = _snapshot(reference_area)
            fit_role_view(window, ROLE_REFERENCE)
            _assert_view_changed(reference_before, _snapshot(reference_area))
            _assert_view_equal(moving_before, _snapshot(moving_area))

            reference_before = _snapshot(reference_area)
            moving_before = _snapshot(moving_area)
            fit_role_view(window, ROLE_MOVING)
            _assert_view_changed(moving_before, _snapshot(moving_area))
            _assert_view_equal(reference_before, _snapshot(reference_area))

            reference_region = find_window_region(reference_area)
            moving_region = find_window_region(moving_area)
            assert reference_region is not None and moving_region is not None
            _set_arbitrary_view(reference_area, (200.0, 220.0, 240.0), 1.0)
            _set_arbitrary_view(moving_area, (-200.0, -220.0, -240.0), 2.0)
            fit_role_view(window, ROLE_REFERENCE)
            _REFERENCE_EXPECTED_HOME = _snapshot(reference_area)
            _set_arbitrary_view(reference_area, (200.0, 220.0, 240.0), 1.0)
            fit_role_view(window, ROLE_MOVING)
            _MOVING_EXPECTED_HOME = _snapshot(moving_area)
            _set_arbitrary_view(moving_area, (-200.0, -220.0, -240.0), 2.0)
            _REFERENCE_BEFORE_HOME = _snapshot(reference_area)
            _MOVING_BEFORE_HOME = _snapshot(moving_area)
            _focus_viewport(window, reference_area)
            _STAGE = "QUEUE_REFERENCE_HOME"
            return 0.1

        if _STAGE == "QUEUE_REFERENCE_HOME":
            window = find_main_window(bpy.context.window_manager)
            assert window is not None
            reference_area, _moving_area = classify_areas(window.screen.areas)
            assert reference_area is not None
            _queue_home_event(window, reference_area, "PRESS")
            _STAGE = "VERIFY_REFERENCE_HOME"
            return 0.1

        if _STAGE == "VERIFY_REFERENCE_HOME":
            window = find_main_window(bpy.context.window_manager)
            assert window is not None
            reference_area, moving_area = classify_areas(window.screen.areas)
            assert reference_area is not None and moving_area is not None
            _assert_view_changed(_REFERENCE_BEFORE_HOME, _snapshot(reference_area))
            _assert_view_equal(_REFERENCE_EXPECTED_HOME, _snapshot(reference_area))
            _assert_view_equal(_MOVING_BEFORE_HOME, _snapshot(moving_area))
            _queue_home_event(window, reference_area, "RELEASE")
            _REFERENCE_BEFORE_HOME = _snapshot(reference_area)
            _MOVING_BEFORE_HOME = _snapshot(moving_area)
            _focus_viewport(window, moving_area)
            _STAGE = "QUEUE_MOVING_HOME"
            return 0.1

        if _STAGE == "QUEUE_MOVING_HOME":
            window = find_main_window(bpy.context.window_manager)
            assert window is not None
            _reference_area, moving_area = classify_areas(window.screen.areas)
            assert moving_area is not None
            _queue_home_event(window, moving_area, "PRESS")
            _STAGE = "VERIFY_MOVING_HOME"
            return 0.1

        if _STAGE == "VERIFY_MOVING_HOME":
            window = find_main_window(bpy.context.window_manager)
            assert window is not None
            reference_area, moving_area = classify_areas(window.screen.areas)
            assert reference_area is not None and moving_area is not None
            _assert_view_changed(_MOVING_BEFORE_HOME, _snapshot(moving_area))
            _assert_view_equal(_MOVING_EXPECTED_HOME, _snapshot(moving_area))
            _assert_view_equal(_REFERENCE_BEFORE_HOME, _snapshot(reference_area))
            _queue_home_event(window, moving_area, "RELEASE")
            _STAGE = "VERIFY_MENU_AND_OPERATOR"
            return 0.1

        if _STAGE == "VERIFY_MENU_AND_OPERATOR":
            window = find_main_window(bpy.context.window_manager)
            assert window is not None
            reference_area, moving_area = classify_areas(window.screen.areas)
            assert reference_area is not None and moving_area is not None
            _set_arbitrary_view(reference_area, (300.0, 320.0, 340.0), 3.0)
            menu_fit_a = _fit_via_operator(window, reference_area, ROLE_REFERENCE)
            _set_arbitrary_view(reference_area, (300.0, 320.0, 340.0), 3.0)
            fit_role_view(window, ROLE_REFERENCE)
            helper_fit_a = _snapshot(reference_area)
            assert menu_fit_a[:2] == helper_fit_a[:2]

            _set_arbitrary_view(moving_area, (-300.0, -320.0, -340.0), 3.0)
            menu_fit_p = _fit_via_operator(window, moving_area, ROLE_MOVING)
            _set_arbitrary_view(moving_area, (-300.0, -320.0, -340.0), 3.0)
            fit_role_view(window, ROLE_MOVING)
            helper_fit_p = _snapshot(moving_area)
            assert menu_fit_p[:2] == helper_fit_p[:2]
            _assert_view_menu_callback_lifecycle(registration, window, reference_area)
            assert _scene_data_identifiers() == _SCENE_IDENTIFIERS_BEFORE_FRAME
            assert _keymap_signatures() == _KEYMAP_SIGNATURES_BEFORE_FRAME
            _STAGE = "UNREGISTER"
            return 0.1

        if _STAGE == "UNREGISTER":
            registration.unregister()
            _REGISTERED = False
            _STAGE = "DONE"
            return 0.1

        if _STAGE == "DONE":
            assert RUNTIME.session_active is False
            assert registration._VIEW_MENU_REGISTERED is False
            assert _scene_data_identifiers() == _SCENE_IDENTIFIERS_BEFORE
            assert _keymap_signatures() == _KEYMAP_SIGNATURES_BEFORE
            assert COLMAPALIGN_GGT_frame_all.poll(
                type("Context", (), {"area": None, "region": None})()
            ) is False
            print("test_view_framing_gui: 통과")
            print("VIEW_FRAMING_GUI_TEST_RESULT=PASS")
            bpy.ops.wm.quit_blender()
            return None
    except Exception:
        traceback.print_exc()
        try:
            if _REGISTERED:
                from tools.align import registration

                registration.unregister()
        except Exception:
            traceback.print_exc()
        print("VIEW_FRAMING_GUI_TEST_RESULT=FAIL")
        bpy.ops.wm.quit_blender()
        return None
    return 0.1


def schedule_gui_test() -> None:
    """GUI 이벤트 루프에서 애드온 등록과 Frame All 검사를 예약합니다."""
    global _REGISTERED, _SCENE_IDENTIFIERS_BEFORE, _KEYMAP_SIGNATURES_BEFORE
    _add_source_path()
    if bpy.app.background:
        print("test_view_framing_gui: GUI Blender에서만 실행합니다.")
        print("VIEW_FRAMING_GUI_TEST_RESULT=SKIP")
        return
    from tools.align import registration
    from tools.align.operators.demo_data import install_demo_fixture

    _SCENE_IDENTIFIERS_BEFORE = _scene_data_identifiers()
    _KEYMAP_SIGNATURES_BEFORE = _keymap_signatures()
    registration.register()
    _REGISTERED = True
    install_demo_fixture()
    bpy.app.timers.register(_run_gui_test_step, first_interval=0.2)


if __name__ == "__main__":
    schedule_gui_test()
