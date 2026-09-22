"""실제 Blender에서 미터 눈금의 소유권·단위·복원·선택 격리를 검사합니다."""

import sys
import tempfile
from pathlib import Path

import bpy

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from tools.modifier.blender import meter_grid_guide as guide
from tools.modifier.blender.managed_scene import is_managed
from tools.modifier.blender.modifier_data_visibility import (
    activate_modifier_data_view_layer, restore_modifier_meter_visibility,
)


def run():
    """빈 Scene 진입과 단위·파일 복원 경계를 검사하고 사용자 자원 보존을 확인합니다."""
    scene = bpy.context.scene
    foreign = bpy.data.collections.new(guide.GUIDE_COLLECTION_NAME)
    scene.collection.children.link(foreign)
    guide.register_meter_grid_guide(restore_modifier_meter_visibility)
    activate_modifier_data_view_layer(bpy.context.window)
    collection = guide.iter_meter_guide_collections(scene)[0]
    assert collection != foreign
    assert len(collection.objects) == 21
    assert sum(obj.type == "FONT" for obj in collection.objects) == 20
    assert sum(obj.type == "CURVE" for obj in collection.objects) == 1
    assert all(not is_managed(obj) for obj in collection.objects)
    assert all(obj.parent is None and obj.hide_select and obj.hide_render and obj.show_in_front for obj in collection.objects)
    assert all(obj.visible_get() for obj in collection.objects)
    bpy.ops.object.select_all(action="SELECT")
    assert all(not obj.select_get() for obj in collection.objects)
    original_ids = {obj.as_pointer() for obj in collection.objects}
    activate_modifier_data_view_layer(bpy.context.window)
    assert original_ids == {obj.as_pointer() for obj in collection.objects}
    for layer in scene.view_layers:
        excluded = layer.layer_collection.children[collection.name].exclude
        assert excluded == (layer != bpy.context.view_layer)
    for system, scale, unit in (("METRIC", 1.0, 1.0), ("METRIC", 0.01, 100.0), ("IMPERIAL", 0.01, 100.0), ("NONE", 0.01, 1.0)):
        scene.unit_settings.system = system
        scene.unit_settings.scale_length = scale
        guide._units_changed(scene)
        label = next(obj for obj in collection.objects if obj.get(guide.GUIDE_ROLE_KEY) == "X_10")
        assert abs(label.location.x - 10 * unit) < 0.001
        assert abs(label.data.size - 0.15 * unit) < 0.001
        assert label.data.body == "10 m"
    missing = next(obj for obj in collection.objects if obj.type == "FONT")
    bpy.data.objects.remove(missing, do_unlink=True)
    guide.restore_meter_grid_guides()
    assert len(collection.objects) == 21
    assert sum(guide.is_meter_guide(curve) for curve in bpy.data.curves) == 21
    other_scene = bpy.data.scenes.new("미터 눈금 독립 Scene")
    other_scene.collection.children.link(collection)
    other_collection = guide.ensure_meter_grid_guide(other_scene)
    assert other_collection != collection
    assert other_collection[guide.GUIDE_UUID_KEY] != collection[guide.GUIDE_UUID_KEY]
    assert len(other_collection.objects) == 21
    other_scene.unit_settings.system = "METRIC"
    other_scene.unit_settings.scale_length = 0.01
    guide._units_changed(other_scene)
    original_label = next(obj for obj in collection.objects if obj.get(guide.GUIDE_ROLE_KEY) == "X_10")
    assert original_label.location.x == 10
    with tempfile.TemporaryDirectory() as directory:
        path = str(Path(directory) / "meter.blend")
        bpy.ops.wm.save_as_mainfile(filepath=path)
        bpy.ops.wm.open_mainfile(filepath=path)
        scene = bpy.context.scene
        collection = guide.iter_meter_guide_collections(scene)[0]
        assert len(collection.objects) == 21
        assert all(not is_managed(obj) for obj in collection.objects)
        guide.restore_meter_grid_guides()
        assert len(guide.iter_meter_guide_collections(scene)) == 1
    guide.unregister_meter_grid_guide()
    assert not any(guide.is_meter_guide(obj) for obj in bpy.data.objects)
    assert bpy.data.collections.get(guide.GUIDE_COLLECTION_NAME) is not None
    assert guide.restore_meter_grid_guides not in bpy.app.handlers.load_post
    assert guide._RESTORE_VISIBILITY is None
    guide.register_meter_grid_guide(restore_modifier_meter_visibility)
    guide.restore_meter_grid_guides()
    assert len(guide.iter_meter_guide_collections(scene)[0].objects) == 21
    guide.unregister_meter_grid_guide()
    guide.unregister_meter_grid_guide()
    print("미터 눈금 생성·단위·선택·격리·파일 복원·해제 검사 통과")


run()
