"""내부 축 숨김이 하위 표시·변환·편집 선택을 보존하는지 실제 Blender로 검사합니다."""

import sys
import tempfile
from pathlib import Path

import bpy
from mathutils import Vector

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from tools import modifier
from tools.modifier.blender.axis_display import hide_dataset_axis_roots
from tools.modifier.blender.modifier_data_visibility import activate_modifier_data_view_layer
from tools.modifier.blender.modifier_runtime_domain import domain_for_scene
from tools.modifier.blender.camera_edit_session import start_camera_edit_mode, force_end_camera_mode
from tools.modifier.blender.point_edit_session import start_point_edit_mode, force_end_point_mode
from tools.modifier.blender.selection import collect_selected_image_ids, collect_selected_point_ids_from_object_mesh
from tools.modifier.blender.ownership import RECORD_KIND_KEY
from tests.modifier.fixture_utils import make_legacy_text_multi_point


def run():
    """신규 축 잠금과 구형 축 교정 후 카메라·점 선택을 검사합니다."""
    modifier.register()
    try:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'dataset'
            make_legacy_text_multi_point(path)
            scene = bpy.context.scene
            scene.colmap_import.dataset_root = str(path)
            assert bpy.ops.colmap.import_model() == {'FINISHED'}
            activate_modifier_data_view_layer(bpy.context.window)
            bpy.context.view_layer.update()
            roots = [obj for obj in scene.objects if obj.get(RECORD_KIND_KEY) == 'AXIS_ROOT']
            assert roots
            children = [obj for obj in scene.objects if obj.get(RECORD_KIND_KEY) in {'POINT_MESH', 'CAMERA'}]
            assert all(root.hide_viewport and root.hide_select for root in roots)
            assert all(obj.visible_get() for obj in children), '축 숨김으로 하위 표시가 사라졌습니다.'
            root = roots[0]
            child = next(obj for obj in children if obj.parent == root)
            before = child.matrix_world.translation.copy()
            root.location += Vector((2, 3, 4))
            bpy.context.view_layer.update()
            assert (child.matrix_world.translation - before - Vector((2, 3, 4))).length < 1e-5
            root.hide_viewport = False
            root.hide_select = False
            bpy.context.view_layer.update()
            root.select_set(True)
            bpy.context.view_layer.objects.active = root
            hide_dataset_axis_roots(scene)
            bpy.context.view_layer.update()
            assert root.hide_viewport and root.hide_select and not root.select_get()
            assert bpy.context.view_layer.objects.active != root
            assert all(obj.visible_get() for obj in children)
            state = domain_for_scene(scene, required=True).dataset_state
            start_camera_edit_mode(bpy.context, state)
            bpy.ops.object.select_all(action='SELECT')
            assert collect_selected_image_ids(bpy.context, state) == (7,)
            force_end_camera_mode(bpy.context)
            start_point_edit_mode(bpy.context, state)
            bpy.ops.mesh.select_all(action='SELECT')
            bpy.ops.object.mode_set(mode='OBJECT')
            assert len(collect_selected_point_ids_from_object_mesh(bpy.context, state)) == 3
            force_end_point_mode(bpy.context)
            print('보조 축 숨김·하위 변환·카메라 및 점 선택 검사 통과')
    finally:
        modifier.unregister()


run()
