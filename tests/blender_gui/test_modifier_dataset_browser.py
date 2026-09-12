"""파일 선택창의 취소와 Import 확정 후 실제 가져오기 완료를 검증합니다."""

import importlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import traceback

import bpy

ROOT = Path(__file__).resolve().parents[2]
REPORT = Path(os.environ.get("COLMAP_BROWSER_REPORT", str(ROOT / ".artifacts/dataset-browser.json")))
if os.environ.get("COLMAP_TOOLS_USE_INSTALLED") == "1":
    import addon_utils
    addon_utils.enable("bl_ext.user_default.colmap_tools", default_set=False, persistent=False)
else:
    sys.path.insert(0, str(ROOT.parent))
    importlib.import_module(ROOT.name).register()

SCENE = bpy.context.scene
SETTINGS = SCENE.colmap_import
ORIGINAL = (SETTINGS.dataset_root, SETTINGS.axis_preset, SETTINGS.axis_forward, SETTINGS.axis_up)
DIRECTORY = tempfile.TemporaryDirectory(prefix="browser-dataset-", dir=ROOT / ".artifacts")
sys.path.insert(0, str(ROOT))
from tests.modifier.fixture_utils import make_legacy_text
make_legacy_text(Path(DIRECTORY.name))
image = bpy.data.images.new("가져오기 검사 이미지", width=1000, height=800)
image.filepath_raw = str(Path(DIRECTORY.name) / "images/frame 01.jpg")
image.file_format = "JPEG"
image.save()
bpy.data.images.remove(image)
PACKAGE = "bl_ext.user_default.colmap_tools" if os.environ.get("COLMAP_TOOLS_USE_INSTALLED") == "1" else ROOT.name
progress = importlib.import_module(f"{PACKAGE}.tools.modifier.blender.import_progress_session")
STEP = 0
START = time.monotonic()


def file_browser():
    """실제 열린 파일 탐색기와 해당 실행 문맥을 찾습니다."""
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.type == "FILE_BROWSER" and area.spaces.active.active_operator is not None:
                return window, area
    return None


def advance():
    """취소 시 설정 보존과 Import 한 번으로 점군·카메라 생성 완료를 검사합니다."""
    global STEP
    try:
        assert time.monotonic() - START < 30, "파일 탐색기 검사 시간 초과"
        if STEP in (0, 3):
            if bpy.context.window.workspace.get("colmap_tools_workspace_state") != "READY":
                return 0.2
            bpy.ops.colmap.select_dataset_root("INVOKE_DEFAULT")
        elif STEP in (1, 4):
            browser = file_browser()
            if browser is None:
                return 0.2
            window, area = browser
            operator = area.spaces.active.active_operator
            assert operator.bl_idname == "COLMAP_OT_select_dataset_root"
            operator.axis_preset = "CUSTOM"
            operator.axis_forward = "X"
            operator.axis_up = "Z"
            operator.directory = DIRECTORY.name + os.sep
            area.spaces.active.params.directory = os.fsencode(DIRECTORY.name + os.sep)
            assert (SETTINGS.dataset_root, SETTINGS.axis_preset, SETTINGS.axis_forward, SETTINGS.axis_up) == ORIGINAL
            with bpy.context.temp_override(window=window, area=area):
                if STEP == 1:
                    bpy.ops.file.cancel()
                else:
                    bpy.ops.file.execute()
        elif STEP == 2:
            if file_browser() is not None:
                return 0.2
            assert (SETTINGS.dataset_root, SETTINGS.axis_preset, SETTINGS.axis_forward, SETTINGS.axis_up) == ORIGINAL
        else:
            if file_browser() is not None:
                return 0.2
            assert Path(SETTINGS.dataset_root) == Path(DIRECTORY.name), SETTINGS.dataset_root
            assert (SETTINGS.axis_preset, SETTINGS.axis_forward, SETTINGS.axis_up) == ("CUSTOM", "X", "Z")
            assert SETTINGS.status_kind != "ERROR", SETTINGS.status
            if progress.active_import_progress_session() is not None:
                return 0.2
            assert int(SETTINGS.point3d_count) == 1 and int(SETTINGS.camera_count) == 1, SETTINGS.status
            REPORT.write_text(json.dumps({"passed": True, "cancel_preserved_scene": True, "confirmed_axes": [SETTINGS.axis_forward, SETTINGS.axis_up], "imported_points": SETTINGS.point3d_count, "imported_cameras": SETTINGS.camera_count}), encoding="utf-8")
            DIRECTORY.cleanup()
            bpy.ops.wm.quit_blender()
            return None
        STEP += 1
        return 0.5
    except Exception:
        REPORT.write_text(json.dumps({"passed": False, "step": STEP, "error": traceback.format_exc()}), encoding="utf-8")
        DIRECTORY.cleanup()
        bpy.ops.wm.quit_blender()
        return None


bpy.context.preferences.view.show_splash = False
bpy.context.preferences.view.filebrowser_display_type = "SCREEN"
bpy.ops.colmap.open_modifier_workspace("EXEC_DEFAULT")
bpy.app.timers.register(advance, first_interval=1.0)
