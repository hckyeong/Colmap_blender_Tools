"""숨김 GUI의 실제 도구 진입 경로에서 세 도구의 N 패널 폭을 비교합니다."""

import importlib
import json
import os
from pathlib import Path
import sys
import time
import traceback

import bpy


ROOT = Path(__file__).resolve().parents[2]
REPORT = Path(os.environ.get("COLMAP_SIDEBAR_REPORT", str(ROOT / ".artifacts/sidebar-width.json")))
INSTALLED = os.environ.get("COLMAP_TOOLS_USE_INSTALLED") == "1"
if INSTALLED:
    import addon_utils
    addon_utils.enable("bl_ext.user_default.colmap_tools", default_set=False, persistent=False)
else:
    sys.path.insert(0, str(ROOT.parent))
    importlib.import_module(ROOT.name).register()

OPERATORS = (
    ("COLMAP Modifier", bpy.ops.colmap.open_modifier_workspace),
    ("Projection Paint", bpy.ops.projection_paint.open_workspace),
    ("COLMAP Align", bpy.ops.colmap_align.open_workspace),
)
RESULTS = []
INDEX = 0
STARTED = time.monotonic()
READY_AT = None


def advance():
    """도구 준비와 화면 갱신을 기다린 뒤 실제 표시 폭이 같은지 검사합니다."""
    global INDEX, STARTED, READY_AT
    try:
        if INDEX >= len(OPERATORS):
            widths = [item["width"] for item in RESULTS]
            assert min(widths) > 100, RESULTS
            assert max(widths) - min(widths) <= 2, RESULTS
            REPORT.parent.mkdir(parents=True, exist_ok=True)
            REPORT.write_text(json.dumps({"passed": True, "results": RESULTS}, indent=2), encoding="utf-8")
            bpy.ops.wm.quit_blender()
            return None
        name, operator = OPERATORS[INDEX]
        if READY_AT is None:
            assert operator("EXEC_DEFAULT") == {"FINISHED"}, name
            READY_AT = 0.0
            STARTED = time.monotonic()
            return 0.2
        assert time.monotonic() - STARTED < 30.0, f"작업공간 준비 시간 초과: {name}"
        window = bpy.context.window
        if window.workspace.name != name or window.workspace.get("colmap_tools_workspace_state") != "READY":
            return 0.2
        if READY_AT == 0.0:
            READY_AT = time.monotonic()
        if time.monotonic() - READY_AT < 1.0:
            return 0.2
        widths = [r.width for a in window.screen.areas if a.type == "VIEW_3D"
                  for r in a.regions if r.type == "UI" and a.spaces.active.show_region_ui]
        assert len(widths) == 1, (name, widths)
        RESULTS.append({"tool": name, "width": widths[0], "ui_scale": bpy.context.preferences.system.ui_scale})
        INDEX += 1
        READY_AT = None
        return 0.2
    except Exception:
        REPORT.parent.mkdir(parents=True, exist_ok=True)
        REPORT.write_text(json.dumps({"passed": False, "results": RESULTS, "error": traceback.format_exc()}, indent=2), encoding="utf-8")
        bpy.ops.wm.quit_blender()
        return None


bpy.context.preferences.view.show_splash = False
bpy.app.timers.register(advance, first_interval=1.0)
