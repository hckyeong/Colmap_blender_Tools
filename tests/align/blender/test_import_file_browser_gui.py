"""실제 Blender File Browser의 COLMAP 가져오기 축 옵션과 취소 경계를 검증합니다."""

from __future__ import annotations

from pathlib import Path
import sys
import traceback

import bpy


_STAGE = "PREPARE"
_WAIT_COUNT = 0
_SETTINGS_SNAPSHOT: tuple[str, str, int, str, str, str] | None = None
_MAIN_WINDOW = None


def _add_source_path() -> None:
    """애드온 소스 디렉터리를 Blender 테스트 모듈 경로에 추가합니다."""
    source_path = Path(__file__).resolve().parents[3]
    source_text = str(source_path)
    if source_text not in sys.path:
        sys.path.insert(0, source_text)


def _main_window():
    """현재 테스트의 단일 Blender 메인 창을 반환합니다."""
    global _MAIN_WINDOW
    if _MAIN_WINDOW is not None:
        return _MAIN_WINDOW
    windows = tuple(bpy.context.window_manager.windows)
    assert len(windows) == 1
    _MAIN_WINDOW = windows[0]
    return _MAIN_WINDOW


def _file_browser_context():
    """별도 임시 창을 포함해 현재 열린 File Browser Area와 Window를 찾습니다."""
    matches = tuple(
        (window, area)
        for window in bpy.context.window_manager.windows
        for area in window.screen.areas
        if area.type == "FILE_BROWSER"
    )
    return matches[0] if len(matches) == 1 else None


def _view3d_override(window) -> dict[str, object]:
    """COLMAP Align P 뷰의 오퍼레이터 실행용 context override를 만듭니다."""
    area = next(area for area in window.screen.areas if area.type == "VIEW_3D")
    region = next(region for region in area.regions if region.type == "WINDOW")
    return {
        "window": window,
        "screen": window.screen,
        "area": area,
        "region": region,
        "scene": window.scene,
    }


def _settings_snapshot(window) -> tuple[str, str, int, str, str, str]:
    """File Browser 확인 전후에 비교할 Scene 가져오기 기본값을 반환합니다."""
    settings = window.scene.colmap_align_settings
    return (
        settings.import_last_directory,
        settings.import_input_format,
        settings.import_display_point_limit,
        settings.import_axis_preset,
        settings.import_axis_forward,
        settings.import_axis_up,
    )


def _finish(succeeded: bool) -> None:
    """애드온을 정리하고 결과 마커를 출력한 뒤 Blender를 종료합니다."""
    try:
        from tools.align import registration

        registration.unregister()
    finally:
        print(f"IMPORT_FILE_BROWSER_GUI_TEST_RESULT={'PASS' if succeeded else 'FAIL'}")
        bpy.ops.wm.quit_blender()


def _run_step() -> float | None:
    """작업공간 진입, File Browser 검사, 취소 검증을 순서대로 수행합니다."""
    global _STAGE, _WAIT_COUNT, _SETTINGS_SNAPSHOT
    try:
        from tools.align.operators.workspace import _process_sidebar_activation
        from tools.align.session import is_session_active_for_window
        from tools.align.state import RUNTIME

        if RUNTIME.workspace_setup_task is not None:
            return 0.05
        if RUNTIME.workspace_setup_error:
            raise AssertionError(RUNTIME.workspace_setup_error)

        if _STAGE == "PREPARE":
            assert bpy.ops.colmap_align.import_model.poll() is False
            assert "FINISHED" in bpy.ops.colmap_align.open_workspace()
            _STAGE = "WAIT_WORKSPACE"
            return 0.05

        window = _main_window()
        if _STAGE == "WAIT_WORKSPACE":
            if bpy.app.timers.is_registered(_process_sidebar_activation):
                return 0.05
            assert is_session_active_for_window(window) is True
            with bpy.context.temp_override(**_view3d_override(window)):
                assert bpy.ops.colmap_align.import_model.poll() is True
                result = bpy.ops.colmap_align.import_model("INVOKE_DEFAULT")
            assert "RUNNING_MODAL" in result
            _SETTINGS_SNAPSHOT = _settings_snapshot(window)
            _STAGE = "WAIT_FILE_BROWSER"
            _WAIT_COUNT = 0
            return 0.05

        if _STAGE == "WAIT_FILE_BROWSER":
            _WAIT_COUNT += 1
            assert _WAIT_COUNT < 200, "COLMAP File Browser가 제한 시간 안에 열리지 않았습니다."
            file_context = _file_browser_context()
            if file_context is None:
                return 0.05
            file_window, area = file_context
            active_operator = area.spaces.active.active_operator
            assert active_operator is not None
            assert active_operator.bl_rna.identifier == "COLMAP_ALIGN_OT_import_model", (
                active_operator.bl_idname,
                active_operator.bl_rna.identifier,
            )
            assert active_operator.input_format == "AUTO"
            assert active_operator.display_point_limit == 250_000
            assert active_operator.axis_preset == "REALITY_CAPTURE"
            assert (active_operator.axis_forward, active_operator.axis_up) == ("Z", "-Y")
            assert active_operator.filter_folder is True
            assert any(region.type == "TOOL_PROPS" for region in area.regions)
            assert _SETTINGS_SNAPSHOT is not None
            assert _settings_snapshot(window) == _SETTINGS_SNAPSHOT
            active_operator.display_point_limit = 400_000
            assert active_operator.display_point_limit == 400_000
            assert _settings_snapshot(window) == _SETTINGS_SNAPSHOT
            file_window.event_simulate(type="ESC", value="PRESS")
            file_window.event_simulate(type="ESC", value="RELEASE")
            _STAGE = "WAIT_CANCEL"
            _WAIT_COUNT = 0
            return 0.05

        if _STAGE == "WAIT_CANCEL":
            _WAIT_COUNT += 1
            assert _WAIT_COUNT < 200, "COLMAP File Browser가 제한 시간 안에 닫히지 않았습니다."
            if _file_browser_context() is not None:
                return 0.05
            assert _SETTINGS_SNAPSHOT is not None
            assert _settings_snapshot(window) == _SETTINGS_SNAPSHOT
            assert RUNTIME.import_job is None
            assert RUNTIME.model_catalog.model_count == 0
            assert RUNTIME.model_repository.models == {}
            assert RUNTIME.model_repository.display_transforms == {}
            _finish(True)
            return None

        raise AssertionError(f"알 수 없는 File Browser GUI 테스트 단계입니다: {_STAGE}")
    except Exception:
        traceback.print_exc()
        _finish(False)
        return None


def _start() -> None:
    """소스 애드온을 등록하고 File Browser GUI 테스트 타이머를 시작합니다."""
    _add_source_path()
    from tools.align import registration

    registration.unregister()
    registration.register()
    bpy.app.timers.register(_run_step, first_interval=0.1)


_start()
