"""격리 Blender 창에서 축 팝업의 실제 RNA 갱신·초기화·Esc·적용을 검증합니다."""

from pathlib import Path
import sys
import traceback
from unittest.mock import patch

import bpy


_STAGE = "START"
_RUNTIME = None
_FIXTURE = None
_SESSION_PATCH = None
_BASELINE = None
_SELECTED = None
_TOKEN = ""


def _add_test_paths() -> None:
    """실험 소스와 축 UI fixture를 GUI 프로세스의 모듈 경로에 추가합니다."""
    test_dir = Path(__file__).resolve().parent
    for path in (test_dir.parents[2], test_dir):
        if str(path) not in sys.path:
            sys.path.insert(0, str(path))


def _key(event_type: str) -> None:
    """실제 창 이벤트로 팝업 취소 또는 적용 키를 전달합니다."""
    window = bpy.context.window_manager.windows[0]
    window.event_simulate(type=event_type, value="PRESS")
    window.event_simulate(type=event_type, value="RELEASE")


def _open(model_id: str) -> str:
    """현재 실제 창에서 네이티브 속성 대화상자를 열고 편집 토큰을 반환합니다."""
    window = bpy.context.window_manager.windows[0]
    area = next(item for item in window.screen.areas if item.type == "VIEW_3D")
    region = next(item for item in area.regions if item.type == "WINDOW")
    window.event_simulate(type="MOUSEMOVE", value="NOTHING", x=region.x + region.width // 2, y=region.y + region.height // 2)
    with bpy.context.temp_override(window=window, area=area, region=region):
        assert bpy.ops.colmap_align.edit_geometry_axes("INVOKE_DEFAULT", model_id=model_id) == {"RUNNING_MODAL"}
    return _RUNTIME.axis_settings.token


def _capture(name: str) -> None:
    """완전히 그려진 팝업을 저장소 내부 검증 폴더에 저장해 육안 검증에 사용합니다."""
    output_dir = Path(__file__).resolve().parents[3] / "build" / "verification" / "axis-transform-20260921" / "gui"
    output_dir.mkdir(parents=True, exist_ok=True)
    window = bpy.context.window_manager.windows[0]
    with bpy.context.temp_override(window=window):
        bpy.ops.wm.redraw_timer(type="DRAW_WIN_SWAP", iterations=1)
        bpy.ops.screen.screenshot(filepath=str(output_dir / name))


def _finish(passed: bool) -> None:
    """이 테스트가 등록한 클래스와 fixture만 정리하고 전용 Blender 프로세스를 종료합니다."""
    global _FIXTURE, _SESSION_PATCH
    from tools.align.operators import axis_settings
    try:
        if _FIXTURE is not None:
            _FIXTURE.__exit__(None, None, None)
            _FIXTURE = None
        if _SESSION_PATCH is not None:
            _SESSION_PATCH.stop()
            _SESSION_PATCH = None
        for cls in reversed(axis_settings.CLASSES):
            if getattr(cls, "is_registered", False):
                bpy.utils.unregister_class(cls)
    except Exception:
        traceback.print_exc()
        passed = False
    print(f"GEOMETRY_AXES_GUI_TEST_RESULT={'PASS' if passed else 'FAIL'}", flush=True)
    bpy.ops.wm.quit_blender()


def _step():
    """실제 팝업에서 축·이동·비율 잠금·원점 이동·취소·적용을 순차 검사합니다."""
    global _STAGE, _RUNTIME, _FIXTURE, _SESSION_PATCH, _BASELINE, _SELECTED, _TOKEN
    from tools.align.operators import axis_settings
    try:
        if _STAGE == "START":
            _key("ESC")
            _STAGE = "SETUP"
            return 0.3
        if _STAGE == "SETUP":
            from test_geometry_axes_ui import _fixture
            assert bpy.app.use_event_simulate
            redraw = axis_settings.request_redraw
            _FIXTURE = _fixture()
            _RUNTIME, _context = _FIXTURE.__enter__()
            axis_settings.request_redraw = redraw
            window = bpy.context.window_manager.windows[0]
            _RUNTIME.session.target_window_pointer = window.as_pointer()
            _RUNTIME.session.align_workspace_pointer = window.workspace.as_pointer()
            _SESSION_PATCH = patch.object(axis_settings, "is_session_active_for_window", return_value=True)
            _SESSION_PATCH.start()
            for cls in axis_settings.CLASSES:
                bpy.utils.register_class(cls)
            _BASELINE = _RUNTIME.model_repository.display_transforms["moving"]
            _SELECTED = _RUNTIME.model_catalog.selected_model_ids
            _TOKEN = _open("moving")
            _STAGE = "EDIT_CANCEL"
            return 0.35
        if _STAGE == "EDIT_CANCEL":
            dialog = axis_settings._OPEN_DIALOGS[_TOKEN]
            dialog.main_scale = (2.0, 3.0, 4.0)
            assert tuple(dialog.main_scale) == (2.0, 3.0, 4.0)
            dialog.main_scale_locked = True
            assert tuple(dialog.main_scale) == (2.0, 3.0, 4.0)
            dialog.main_scale[1] = 6.0
            assert tuple(dialog.main_scale) == (4.0, 6.0, 8.0)
            assert _RUNTIME.axis_settings.main_transform.scale_xyz == (4.0, 6.0, 8.0)
            dialog.main_translation = (1.0, 2.0, 3.0)
            assert _RUNTIME.axis_settings.main_transform.translation_xyz == (1.0, 2.0, 3.0)
            assert bpy.ops.colmap_align.center_geometry_at_origin(edit_token=_TOKEN, component="MAIN") == {"FINISHED"}
            assert not dialog.error_message
            dialog.main_rotation = (12.0, 24.0, 36.0)
            dialog.companion_rotation = (0.0, 0.0, 45.0)
            assert _RUNTIME.axis_settings.main_transform.rotation_xyz_degrees == (12.0, 24.0, 36.0)
            assert _RUNTIME.model_repository.display_transforms["moving"] is _BASELINE
            assert bpy.ops.colmap_align.reset_geometry_axes(edit_token=_TOKEN, component="COMPANION") == {"FINISHED"}
            assert tuple(dialog.companion_rotation) == (0.0, 0.0, 0.0)
            assert tuple(dialog.main_rotation) == (12.0, 24.0, 36.0)
            dialog.main_forward = "X"
            assert dialog.main_preset == "CUSTOM"
            dialog.main_up = "-X"
            assert dialog.error_message
            dialog.main_up = "Z"
            assert not dialog.error_message
            _STAGE = "ESC_CANCEL"
            return 0.3
        if _STAGE == "ESC_CANCEL":
            assert _RUNTIME.axis_settings.is_active
            assert _TOKEN in axis_settings._OPEN_DIALOGS
            _capture("popup-companion.png")
            _key("ESC")
            _STAGE = "VERIFY_CANCEL"
            return 0.35
        if _STAGE == "VERIFY_CANCEL":
            assert not _RUNTIME.axis_settings.is_active
            assert not axis_settings._OPEN_DIALOGS
            assert _RUNTIME.model_repository.display_transforms["moving"] is _BASELINE
            assert _RUNTIME.model_catalog.selected_model_ids == _SELECTED
            axis_settings.notify_project_state_changed.assert_not_called()
            _TOKEN = _open("moving")
            _STAGE = "EDIT_APPLY"
            return 0.35
        if _STAGE == "EDIT_APPLY":
            dialog = axis_settings._OPEN_DIALOGS[_TOKEN]
            dialog.main_rotation = (0.0, 15.0, 0.0)
            dialog.companion_rotation = (0.0, 0.0, 25.0)
            assert bpy.ops.colmap_align.reset_geometry_axes(edit_token=_TOKEN, component="BOTH") == {"FINISHED"}
            assert dialog.main_preset == "REALITY_CAPTURE" and dialog.companion_preset == "REALITY_CAPTURE_OBJ"
            dialog.main_rotation = (0.0, 15.0, 0.0)
            dialog.companion_rotation = (0.0, 0.0, 25.0)
            dialog.preview_enabled = False
            assert not _RUNTIME.axis_settings.preview_enabled
            dialog.preview_enabled = True
            _STAGE = "CONFIRM_APPLY"
            return 0.25
        if _STAGE == "CONFIRM_APPLY":
            _key("RET")
            _STAGE = "VERIFY_APPLY"
            return 0.35
        if _STAGE == "VERIFY_APPLY":
            assert not _RUNTIME.axis_settings.is_active
            assert _RUNTIME.model_repository.display_transforms["moving"].rotation_xyz_degrees == (0.0, 15.0, 0.0)
            assert _RUNTIME.model_repository.companion_display_transforms["moving"].rotation_xyz_degrees == (0.0, 0.0, 25.0)
            assert _RUNTIME.model_catalog.selected_model_ids == _SELECTED
            axis_settings.notify_project_state_changed.assert_called_once()
            _TOKEN = _open("standalone")
            _STAGE = "VERIFY_STANDALONE"
            return 0.35
        if _STAGE == "VERIFY_STANDALONE":
            dialog = axis_settings._OPEN_DIALOGS[_TOKEN]
            assert dialog.is_standalone and not dialog.has_companion
            _capture("popup-standalone.png")
            assert bpy.ops.colmap_align.reset_geometry_axes(edit_token=_TOKEN, component="BOTH") == {"FINISHED"}
            assert dialog.main_preset == "REALITY_CAPTURE_OBJ"
            _key("ESC")
            _STAGE = "DONE"
            return 0.35
        if _STAGE == "DONE":
            assert not _RUNTIME.axis_settings.is_active
            _TOKEN = _open("moving")
            _STAGE = "LEAVE_WORKSPACE"
            return 0.3
        if _STAGE == "LEAVE_WORKSPACE":
            dialog = axis_settings._OPEN_DIALOGS[_TOKEN]
            dialog.main_rotation = (65.0, 0.0, 0.0)
            window = bpy.context.window_manager.windows[0]
            window.workspace = next(workspace for workspace in bpy.data.workspaces if workspace != window.workspace)
            _STAGE = "VERIFY_LEAVE_WORKSPACE"
            return 0.4
        if _STAGE == "VERIFY_LEAVE_WORKSPACE":
            assert not _RUNTIME.axis_settings.is_active
            assert not axis_settings._OPEN_DIALOGS
            assert not bpy.app.timers.is_registered(axis_settings._watch_axis_edit_dialogs)
            assert _RUNTIME.model_repository.display_transforms["moving"].rotation_xyz_degrees == (0.0, 15.0, 0.0)
            _finish(True)
            return None
        raise AssertionError(f"알 수 없는 축 팝업 GUI 단계입니다: {_STAGE}")
    except Exception:
        traceback.print_exc()
        _finish(False)
        return None


def run() -> None:
    """전용 GUI 프로세스에서만 축 팝업 실제 입력 검사를 예약합니다."""
    _add_test_paths()
    if bpy.app.background:
        raise RuntimeError("축 팝업 검사는 전용 GUI Blender 창이 필요합니다.")
    bpy.app.timers.register(_step, first_interval=0.2)


if __name__ == "__main__":
    run()
