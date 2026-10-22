"""분리된 실제 Blender 화면에서 후발 레이어·작업공간의 미터 눈금 격리를 검사합니다."""

import argparse
import json
import sys
from pathlib import Path

import bpy

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.modifier.blender import meter_grid_guide as guide
from tools.modifier.blender.modifier_data_visibility import (
    activate_modifier_data_view_layer,
    restore_modifier_meter_visibility,
    synchronize_modifier_meter_visibility,
)
from tools.projection_paint.blender.dataset_view_layer import ensure_projection_dataset_view_layer

parser = argparse.ArgumentParser()
parser.add_argument("--output", type=Path, required=True)
args = parser.parse_args(sys.argv[sys.argv.index("--") + 1:])
state = {"stage": "start", "checks": []}


def assert_hidden(layer):
    """Modifier 외 레이어에서 눈금만 제외되고 같은 이름의 사용자 컬렉션은 유지되는지 확인합니다."""
    collection = state["collection"]
    assert layer.layer_collection.children[collection.name].exclude
    assert all(not obj.visible_get(view_layer=layer) for obj in collection.objects)
    assert not layer.layer_collection.children[state["foreign"].name].exclude
    assert state["object_ids"] == {obj.as_pointer() for obj in collection.objects}


def finish(success, error=""):
    """화면 검사 결과를 저장하고 현재 검사 프로세스만 종료합니다."""
    result = {"success": success, "error": error, "checks": state["checks"]}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    guide.unregister_meter_grid_guide()
    bpy.ops.wm.quit_blender()


def advance():
    """실제 알림 처리 사이에 레이어·작업공간을 전환하고 격리 규칙을 검사합니다."""
    try:
        scene, window = bpy.context.scene, bpy.context.window
        stage = state["stage"]
        if stage == "start":
            foreign = bpy.data.collections.new(guide.GUIDE_COLLECTION_NAME)
            scene.collection.children.link(foreign)
            guide.register_meter_grid_guide(restore_modifier_meter_visibility, synchronize_modifier_meter_visibility)
            modifier = activate_modifier_data_view_layer(window)
            collection = guide.iter_meter_guide_collections(scene)[0]
            state.update(foreign=foreign, modifier=modifier, collection=collection,
                         object_ids={obj.as_pointer() for obj in collection.objects})
            state["painting"] = ensure_projection_dataset_view_layer(scene)
            window.view_layer = state["painting"]
            state["stage"] = "painting"
        elif stage == "painting":
            assert_hidden(state["painting"])
            state["checks"].append("후발 Painting 레이어 자동 격리")
            state["ordinary"] = scene.view_layers.new("후발 일반 표시 레이어")
            window.view_layer = state["ordinary"]
            state["stage"] = "ordinary"
        elif stage == "ordinary":
            assert_hidden(state["ordinary"])
            state["checks"].append("후발 일반 레이어 자동 격리")
            window.view_layer = state["modifier"]
            state["stage"] = "modifier"
        elif stage == "modifier":
            collection = state["collection"]
            assert all(obj.visible_get(view_layer=state["modifier"]) for obj in collection.objects)
            assert state["object_ids"] == {obj.as_pointer() for obj in collection.objects}
            state["checks"].append("Modifier 복귀 시 21개 눈금 유지")
            window.view_layer = state["painting"]
            state["stage"] = "return"
        elif stage == "return":
            assert_hidden(state["painting"])
            state["checks"].append("반복 Painting 전환 시 격리 유지")
            state["painting"].layer_collection.children[state["collection"].name].exclude = False
            window.workspace = next(workspace for workspace in bpy.data.workspaces if workspace != window.workspace)
            state["stage"] = "workspace"
        else:
            assert_hidden(state["painting"])
            state["checks"].append("작업공간 전환 알림으로 눈금 격리 복구")
            finish(True)
            return None
        return 0.25
    except Exception as exc:
        finish(False, str(exc))
        return None


assert not bpy.app.background, "실제 화면 모드에서 실행해야 합니다."
bpy.app.timers.register(advance, first_interval=0.5)
