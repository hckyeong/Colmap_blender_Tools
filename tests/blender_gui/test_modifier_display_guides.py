"""실제 Modifier 작업공간에서 표시 크기·미터 가이드와 단위 변경 알림을 검증합니다."""

import importlib
import json
import os
from math import hypot
from pathlib import Path
import sys
import tempfile
import time
import traceback

import bpy

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tests.modifier.fixture_utils import make_legacy_text_multi_point

INSTALLED = os.environ.get("COLMAP_TOOLS_USE_INSTALLED") == "1"
PACKAGE = "bl_ext.user_default.colmap_tools" if INSTALLED else ROOT.name
if INSTALLED:
    import addon_utils
    addon_utils.enable(PACKAGE, default_set=False, persistent=False)
else:
    sys.path.insert(0, str(ROOT.parent))
    importlib.import_module(PACKAGE).register()

WORKFLOWS = importlib.import_module(f"{PACKAGE}.tools.modifier.blender.edit_workflows")
RUNTIME = importlib.import_module(f"{PACKAGE}.tools.modifier.blender.modifier_runtime_domain")
OWNERSHIP = importlib.import_module(f"{PACKAGE}.tools.modifier.blender.ownership")
REPORT = Path(os.environ.get("COLMAP_DISPLAY_GUIDE_REPORT", str(ROOT / ".artifacts/modifier-display-guides.json")))
DIRECTORY = tempfile.TemporaryDirectory(prefix="display-guide-", dir=ROOT / ".artifacts")
DATASET = Path(DIRECTORY.name) / "dataset"
make_legacy_text_multi_point(DATASET)
START = time.monotonic()
STEP = 0
GUIDE_POSITIONS = {}


def managed_objects(kind):
    """현재 데이터셋의 관리 표시 오브젝트만 반환합니다."""
    dataset_uuid = bpy.context.scene.colmap_import.dataset_uuid
    return [obj for obj in bpy.context.scene.objects
            if obj.get(OWNERSHIP.OWNER_KEY) == OWNERSHIP.OWNER_TOKEN
            and obj.get(OWNERSHIP.RECORD_KIND_KEY) == kind
            and obj.get(OWNERSHIP.DATASET_UUID_KEY) == dataset_uuid]


def assert_display_size():
    """카메라 월드 배율과 실제 평가된 점의 월드 반지름을 확인합니다."""
    bpy.context.view_layer.update()
    cameras = managed_objects("CAMERA")
    assert cameras
    for obj in cameras:
        assert all(abs(value - 1.0) < 1e-5 for value in obj.matrix_world.to_scale())
        assert abs(obj.data.display_size - bpy.context.scene.colmap_import.camera_frustum_size) < 1e-5
    for obj in managed_objects("POINT_MESH"):
        scale = hypot(*(obj.matrix_world[i][0] for i in range(3)))
        evaluated = obj.evaluated_get(bpy.context.evaluated_depsgraph_get())
        geometry = evaluated.evaluated_geometry()
        assert geometry.pointcloud is not None
        assert len(geometry.pointcloud.points) == len(obj.data.vertices)
        assert all(abs(point.radius * scale - bpy.context.scene.colmap_import.point_size) < 1e-6
                   for point in geometry.pointcloud.points)


def advance():
    """첫 진입부터 배율·되돌리기·단위 변경까지 실제 이벤트 루프로 확인합니다."""
    global STEP, GUIDE_POSITIONS
    try:
        assert time.monotonic() - START < 90, "표시·가이드 GUI 검사 시간 초과"
        scene = bpy.context.scene
        if STEP == 0:
            bpy.ops.colmap.open_modifier_workspace("EXEC_DEFAULT")
        elif STEP == 1:
            if bpy.context.window.workspace.get("colmap_tools_workspace_state") != "READY":
                return 0.2
            fonts = [obj for obj in scene.objects if obj.type == "FONT"]
            assert len(fonts) == 20, "첫 진입의 미터 텍스트 수가 다릅니다."
            assert sorted(obj.data.body for obj in fonts) == sorted([f"{n} m" for n in range(1, 11)] * 2)
            assert all(obj.visible_get(view_layer=bpy.context.view_layer) for obj in fonts)
            assert all(obj.hide_select and obj.hide_render and obj.parent is None for obj in fonts)
            GUIDE_POSITIONS = {obj.name: tuple(obj.location) for obj in fonts}
            scene.colmap_import.dataset_root = str(DATASET)
            assert bpy.ops.colmap.import_model("EXEC_DEFAULT") == {"FINISHED"}
            scene.colmap_import.point_size = 0.03
            scene.colmap_import.camera_frustum_size = 0.75
            matrix = ((10., 0., 0., 2.), (0., 10., 0., 3.), (0., 0., 10., 4.), (0., 0., 0., 1.))
            WORKFLOWS.commit_dataset_transform(bpy.context, matrix)
            assert_display_size()
            assert all(tuple(scene.objects[name].location) == xyz for name, xyz in GUIDE_POSITIONS.items())
            assert abs(scene.colmap_import.point_size - 0.03) < 1e-6
            assert RUNTIME.domain_for_scene(scene).dataset_state.edit_revision == 1
            WORKFLOWS.undo_last_edit(bpy.context)
            assert_display_size()
            WORKFLOWS.redo_last_edit(bpy.context)
            assert_display_size()
            area = next(area for area in bpy.context.window.screen.areas if area.type == "VIEW_3D")
            region = next(region for region in area.regions if region.type == "WINDOW")
            with bpy.context.temp_override(area=area, region=region):
                bpy.ops.object.select_all(action="SELECT")
                assert not any(obj.type == "FONT" for obj in bpy.context.selected_objects)
                bpy.ops.object.select_all(action="DESELECT")
            scene.unit_settings.system = "METRIC"
            scene.unit_settings.scale_length = 0.01
        elif STEP == 2:
            for name, old in GUIDE_POSITIONS.items():
                actual = scene.objects[name].location
                assert all(abs(actual[i] - old[i] * 100) < 1e-3 for i in range(3)), "단위 변경 후 가이드 위치가 다릅니다."
            assert_display_size()
            REPORT.write_text(json.dumps({"passed": True, "installed": INSTALLED, "text_count": 20,
                                          "scale": 10, "unit_scale": 0.01, "undo_redo": True,
                                          "selection_excluded": True}, ensure_ascii=False), encoding="utf-8")
            DIRECTORY.cleanup()
            bpy.ops.wm.quit_blender()
            return None
        STEP += 1
        return 1.0
    except Exception:
        REPORT.write_text(json.dumps({"passed": False, "step": STEP, "error": traceback.format_exc()},
                                     ensure_ascii=False), encoding="utf-8")
        DIRECTORY.cleanup()
        bpy.ops.wm.quit_blender()
        return None


bpy.context.preferences.view.show_splash = False
bpy.app.timers.register(advance, first_interval=1.0)
