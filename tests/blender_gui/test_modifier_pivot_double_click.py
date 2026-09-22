"""실제 뷰포트와 제어된 클릭 간격으로 Modifier 기즈모·회전 중심 이동을 검증합니다."""

import importlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import traceback

import bpy
from bpy_extras import view3d_utils
from mathutils import Vector

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tests.modifier.fixture_utils import make_legacy_text_multi_point

INSTALLED = os.environ.get('COLMAP_TOOLS_USE_INSTALLED') == '1'
PACKAGE = 'bl_ext.user_default.colmap_tools' if INSTALLED else ROOT.name
if INSTALLED:
    import addon_utils
    addon_utils.enable(PACKAGE, default_set=False, persistent=False)
else:
    sys.path.insert(0, str(ROOT.parent))
    importlib.import_module(PACKAGE).register()
gizmo = importlib.import_module(f'{PACKAGE}.tools.modifier.blender.dataset_transform_gizmo')
REPORT = Path(os.environ.get('COLMAP_PIVOT_REPORT', str(ROOT / '.artifacts/modifier-pivot.json')))
DIRECTORY = tempfile.TemporaryDirectory(prefix='pivot-', dir=ROOT / '.artifacts')
DATASET = Path(DIRECTORY.name) / 'dataset'
make_legacy_text_multi_point(DATASET)
STEP = 0
START = time.monotonic()
TARGET = None
MOUSE = None
REVISION = None

def advance():
    """진입·투영·연속 클릭 뒤 피벗과 화면 회전 중심 및 편집 이력을 검사합니다."""
    global STEP, TARGET, MOUSE, REVISION
    try:
        assert time.monotonic() - START < 30, '피벗 검사 시간 초과'
        window = bpy.context.window
        if STEP == 0:
            bpy.ops.colmap.open_modifier_workspace('EXEC_DEFAULT')
        elif STEP == 1:
            if window.workspace.get('colmap_tools_workspace_state') != 'READY':
                return 0.2
            bpy.context.scene.colmap_import.dataset_root = str(DATASET)
            assert bpy.ops.colmap.import_model('EXEC_DEFAULT') == {'FINISHED'}
            state = gizmo.domain_for_scene(bpy.context.scene, required=True).dataset_state
            REVISION = state.edit_revision
            point = state.current_reconstruction.points3d[4294967303]
            TARGET = Vector(gizmo.transform_point(gizmo.compose_transforms(gizmo._display(bpy.context.scene), state.dataset_transform), point.xyz))
            area = next(a for a in window.screen.areas if a.type == 'VIEW_3D')
            area.spaces.active.region_3d.view_location = TARGET + Vector((1, 0, 0))
            area.spaces.active.region_3d.view_distance = 8
            area.tag_redraw()
        else:
            area = next(a for a in window.screen.areas if a.type == 'VIEW_3D')
            region = next(r for r in area.regions if r.type == 'WINDOW')
            view = area.spaces.active.region_3d
            if STEP == 2:
                p = view3d_utils.location_3d_to_region_2d(region, view, TARGET)
                assert p is not None
                MOUSE = (round(p.x + region.x), round(p.y + region.y))
                window.event_simulate(type='MOUSEMOVE', value='NOTHING', x=MOUSE[0], y=MOUSE[1])
            elif STEP == 3:
                from types import SimpleNamespace
                from unittest.mock import patch
                event = SimpleNamespace(type='LEFTMOUSE', value='PRESS', mouse_x=MOUSE[0], mouse_y=MOUSE[1])
                # 실제 뷰 문맥에서 시간만 고정해 연속 클릭 판정과 점 투영을 함께 검사합니다.
                with bpy.context.temp_override(area=area, region=region):
                    with patch.object(gizmo, 'monotonic', side_effect=(10.0, 10.1)):
                        assert not gizmo.route_dataset_transform_input(bpy.context, event)
                        assert gizmo.route_dataset_transform_input(bpy.context, event)
                STEP = 6
            else:
                assert (gizmo._pivot(bpy.context.scene) - TARGET).length < 1e-5, '기즈모 피벗이 이동하지 않았습니다.'
                assert (view.view_location - TARGET).length < 1e-5, '화면 회전 중심이 이동하지 않았습니다.'
                assert gizmo.domain_for_scene(bpy.context.scene, required=True).dataset_state.edit_revision == REVISION
                REPORT.write_text(json.dumps({'passed':True, 'pivot':list(TARGET), 'revision_unchanged':True}), encoding='utf-8')
                DIRECTORY.cleanup()
                bpy.ops.wm.quit_blender()
                return None
        STEP += 1
        return 0.08 if STEP >= 3 else 0.5
    except Exception:
        REPORT.write_text(json.dumps({'passed':False, 'step':STEP, 'mouse':MOUSE, 'error':traceback.format_exc()}, default=str), encoding='utf-8')
        DIRECTORY.cleanup()
        bpy.ops.wm.quit_blender()
        return None


bpy.context.preferences.view.show_splash = False
bpy.app.timers.register(advance, first_interval=1.0)
