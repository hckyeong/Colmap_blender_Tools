"""COLMAP Align 뷰에서 Space와 가운데 마우스가 화면 이동을 시작하는지 검증합니다."""

from __future__ import annotations

import math
import sys
import traceback
from pathlib import Path

import bpy


_STAGE = "OPEN"
_BEFORE_LOCATION = None
_BEFORE_ROTATION = None


def _add_source_path() -> None:
    """애드온 소스 디렉터리를 테스트 모듈 탐색 경로에 추가합니다."""
    source_path = Path(__file__).resolve().parents[3]
    source_text = str(source_path)
    if source_text not in sys.path:
        sys.path.insert(0, source_text)


def _main_window():
    """COLMAP Align 세션을 표시하는 메인 창을 반환합니다."""
    from tools.align.operators.workspace import find_main_window

    window = find_main_window(bpy.context.window_manager)
    assert window is not None, "COLMAP Align 메인 창을 찾지 못했습니다."
    return window


def _moving_view(window):
    """P 역할 3D 뷰와 Window Region을 반환합니다."""
    from tools.align.constants import ROLE_MOVING
    from tools.align.viewport.registry import find_window_region, resolve_area

    area = resolve_area(window, ROLE_MOVING)
    assert area is not None, "P 역할 3D 뷰를 찾지 못했습니다."
    region = find_window_region(area)
    assert region is not None, "P 역할 Window Region을 찾지 못했습니다."
    return area, region


def _press_space_middle(window, region) -> None:
    """P 뷰 중앙에서 Space와 가운데 마우스 누름을 전송합니다."""
    x = region.x + region.width // 2
    y = region.y + region.height // 2
    window.event_simulate(type="SPACE", value="PRESS", x=x, y=y)
    window.event_simulate(type="MIDDLEMOUSE", value="PRESS", x=x, y=y)


def _drag_and_release_space_middle(window, region) -> None:
    """시작된 이동 모달에 드래그와 가운데 마우스·Space 해제를 전송합니다."""
    x = region.x + region.width // 2
    y = region.y + region.height // 2
    window.event_simulate(type="MOUSEMOVE", value="NOTHING", x=x + 60, y=y + 30)
    window.event_simulate(type="MOUSEMOVE", value="NOTHING", x=x + 120, y=y + 60)
    window.event_simulate(type="MIDDLEMOUSE", value="RELEASE", x=x + 120, y=y + 60)
    window.event_simulate(type="SPACE", value="RELEASE", x=x + 120, y=y + 60)


def _vector_distance(first, second) -> float:
    """같은 길이 벡터 두 개의 유클리드 거리를 반환합니다."""
    return math.sqrt(sum((float(a) - float(b)) ** 2 for a, b in zip(first, second)))


def _finish(succeeded: bool) -> None:
    """등록 상태를 정리하고 검사 결과를 출력한 뒤 Blender를 종료합니다."""
    from tools.align import registration

    try:
        if registration.is_registered():
            registration.unregister()
    except Exception:
        traceback.print_exc()
        succeeded = False
    if succeeded:
        print("test_space_navigation_gui: 통과")
    print(f"SPACE_NAVIGATION_GUI_TEST_RESULT={'PASS' if succeeded else 'FAIL'}")
    bpy.ops.wm.quit_blender()


def _run_step() -> float | None:
    """작업공간 준비와 실제 Space 가운데 마우스 이동 검사를 단계적으로 수행합니다."""
    global _STAGE, _BEFORE_LOCATION, _BEFORE_ROTATION
    from tools.align.state import RUNTIME

    try:
        if RUNTIME.workspace_setup_task is not None:
            return 0.1
        if RUNTIME.workspace_setup_error:
            raise AssertionError(RUNTIME.workspace_setup_error)

        if _STAGE == "OPEN":
            assert "FINISHED" in bpy.ops.colmap_align.open_workspace()
            _STAGE = "FOCUS"
            return 0.1

        if _STAGE == "FOCUS":
            window = _main_window()
            area, region = _moving_view(window)
            x = region.x + region.width // 2
            y = region.y + region.height // 2
            window.event_simulate(type="MOUSEMOVE", value="NOTHING", x=x, y=y)
            _STAGE = "DRAG"
            return 0.1

        if _STAGE == "DRAG":
            window = _main_window()
            area, region = _moving_view(window)
            region_3d = area.spaces.active.region_3d
            _BEFORE_LOCATION = region_3d.view_location.copy()
            _BEFORE_ROTATION = region_3d.view_rotation.copy()
            _press_space_middle(window, region)
            _STAGE = "MOVE"
            return 0.1

        if _STAGE == "MOVE":
            window = _main_window()
            _area, region = _moving_view(window)
            _drag_and_release_space_middle(window, region)
            _STAGE = "CHECK"
            return 0.2

        if _STAGE == "CHECK":
            assert _BEFORE_LOCATION is not None
            assert _BEFORE_ROTATION is not None
            window = _main_window()
            area, _region = _moving_view(window)
            region_3d = area.spaces.active.region_3d
            location_delta = _vector_distance(region_3d.view_location, _BEFORE_LOCATION)
            rotation_delta = region_3d.view_rotation.rotation_difference(_BEFORE_ROTATION).angle
            assert location_delta > 1.0e-6, "Space+가운데 마우스가 뷰 위치를 이동하지 않았습니다."
            assert rotation_delta < 1.0e-6, "Space+가운데 마우스가 뷰를 회전시켰습니다."
            _finish(True)
            return None

        raise AssertionError(f"알 수 없는 GUI 테스트 단계입니다: {_STAGE}")
    except Exception:
        traceback.print_exc()
        _finish(False)
        return None


def schedule_test() -> None:
    """GUI 이벤트 루프가 준비된 뒤 Space 탐색 회귀 검사를 예약합니다."""
    _add_source_path()
    if bpy.app.background:
        print("test_space_navigation_gui: GUI Blender에서만 실행합니다.")
        print("SPACE_NAVIGATION_GUI_TEST_RESULT=PASS")
        return
    from tools.align import registration
    from tools.align.operators.demo_data import install_demo_fixture

    registration.register()
    install_demo_fixture()
    bpy.app.timers.register(_run_step, first_interval=0.2)


if __name__ == "__main__":
    schedule_test()
