"""페인트 대상 외 오브젝트 숨김과 안전한 원래 표시 상태 복구를 검증합니다."""

from pathlib import Path
import sys
import tempfile
import unittest

import bpy


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools import projection_paint as addon
from tools.projection_paint.blender.dataset_view_layer import (
    ensure_projection_dataset_view_layer,
    hide_other_paint_objects,
    restore_other_paint_objects,
)
from tools.projection_paint.blender.ownership import tag_owned


def new_object(scene, name, kind="MESH"):
    """표시 검사에 필요한 최소 오브젝트를 장면에 연결합니다."""
    if kind == "CAMERA":
        data = bpy.data.cameras.new(name + "Data")
    elif kind == "LIGHT":
        data = bpy.data.lights.new(name + "Data", type="POINT")
    elif kind == "MESH":
        data = bpy.data.meshes.new(name + "Data")
    else:
        data = None
    obj = bpy.data.objects.new(name, data)
    scene.collection.objects.link(obj)
    return obj


class PaintObjectVisibilityTests(unittest.TestCase):
    """다른 오브젝트와 카메라 배경 및 사용자 표시 선택의 경계를 검사합니다."""

    def setUp(self):
        """독립 페인트 대상과 일반 메시가 있는 빈 장면을 준비합니다."""
        bpy.ops.wm.read_factory_settings(use_empty=True)
        addon.register()
        self.scene = bpy.context.scene
        self.layer = bpy.context.view_layer
        self.scene.projection_paint.dataset_uuid = "paint-isolation-test"
        self.target = new_object(self.scene, "Target")
        self.other = new_object(self.scene, "Other")
        self.layer.update()

    def tearDown(self):
        """표시 상태를 복구한 뒤 검사 확장의 등록을 해제합니다."""
        restore_other_paint_objects(bpy.context.scene)
        addon.unregister()

    def test_isolates_other_types_and_preserves_background_cameras(self):
        """메시·조명·빈 오브젝트·일반 카메라는 숨기고 현재 관리 카메라는 보존합니다."""
        light = new_object(self.scene, "Light", "LIGHT")
        empty = new_object(self.scene, "Empty", "EMPTY")
        user_camera = new_object(self.scene, "UserCamera", "CAMERA")
        current = new_object(self.scene, "CurrentCamera", "CAMERA")
        following = new_object(self.scene, "FollowingCamera", "CAMERA")
        foreign = new_object(self.scene, "ForeignCamera", "CAMERA")
        for obj in (current, following):
            tag_owned(obj, "CAMERA_OBJECT", dataset_uuid="paint-isolation-test")
        tag_owned(foreign, "CAMERA_OBJECT", dataset_uuid="other-dataset")
        self.scene.camera = current
        self.layer.update()
        hide_other_paint_objects(self.scene, self.layer, self.target)
        self.assertFalse(self.target.hide_get())
        for obj in (self.other, light, empty, user_camera, foreign):
            self.assertTrue(obj.hide_get(), obj.name)
            self.assertFalse(obj.hide_viewport)
            self.assertFalse(obj.hide_render)
        self.assertFalse(current.hide_get())
        self.assertFalse(following.hide_get())
        restore_other_paint_objects(self.scene)
        for obj in (self.other, light, empty, user_camera, foreign):
            self.assertFalse(obj.hide_get(), obj.name)

    def test_preserves_camera_without_dataset(self):
        """단독 OBJ 페인트에서도 현재 카메라 배경 연결을 보존합니다."""
        self.scene.projection_paint.dataset_uuid = ""
        camera = new_object(self.scene, "Camera", "CAMERA")
        self.scene.camera = camera
        self.layer.update()
        hide_other_paint_objects(self.scene, self.layer, self.target)
        self.assertFalse(camera.hide_get())
        self.assertTrue(self.other.hide_get())

    def test_restores_initial_hidden_state(self):
        """진입 전부터 숨긴 오브젝트는 종료 뒤에도 숨김 상태를 유지합니다."""
        self.other.hide_set(True)
        hide_other_paint_objects(self.scene, self.layer, self.target)
        restore_other_paint_objects(self.scene)
        self.assertTrue(self.other.hide_get())

    def test_preserves_user_unhide(self):
        """페인트 중 사용자가 다시 표시한 오브젝트는 종료 때 덮어쓰지 않습니다."""
        self.other.hide_set(True)
        hide_other_paint_objects(self.scene, self.layer, self.target)
        self.other.hide_set(False)
        restore_other_paint_objects(self.scene)
        self.assertFalse(self.other.hide_get())

    def test_restores_renamed_object_and_skips_deleted_replacement(self):
        """이름 변경은 실제 참조로 복구하고 삭제 후 같은 이름의 새 오브젝트는 건드리지 않습니다."""
        deleted = new_object(self.scene, "Deleted")
        self.layer.update()
        hide_other_paint_objects(self.scene, self.layer, self.target)
        self.other.name = "Renamed"
        bpy.data.objects.remove(deleted, do_unlink=True)
        replacement = new_object(self.scene, "Deleted")
        self.layer.update()
        replacement.hide_set(True)
        restore_other_paint_objects(self.scene)
        self.assertFalse(self.other.hide_get())
        self.assertTrue(replacement.hide_get())

    def test_other_view_layer_is_unchanged(self):
        """페인트하지 않는 일반 뷰 레이어의 오브젝트 표시는 변경하지 않습니다."""
        other_layer = self.scene.view_layers.new("OtherLayer")
        other_layer.update()
        hide_other_paint_objects(self.scene, self.layer, self.target)
        self.assertTrue(self.other.hide_get(view_layer=self.layer))
        self.assertFalse(self.other.hide_get(view_layer=other_layer))
        restore_other_paint_objects(self.scene)
        self.assertFalse(self.other.hide_get(view_layer=other_layer))

    def test_repeat_does_not_overwrite_original_state(self):
        """반복 준비에서 원래 보임 상태를 숨김 상태로 덮어쓰지 않습니다."""
        hide_other_paint_objects(self.scene, self.layer, self.target)
        count = len(self.scene.projection_paint.paint_object_visibility)
        hide_other_paint_objects(self.scene, self.layer, self.target)
        self.assertEqual(len(self.scene.projection_paint.paint_object_visibility), count)
        restore_other_paint_objects(self.scene)
        self.assertFalse(self.other.hide_get())
        self.assertFalse(self.scene.projection_paint.paint_object_visibility_active)

    def test_restores_renamed_managed_view_layer(self):
        """관리 뷰 레이어 이름이 바뀌어도 소유 UUID로 원래 표시를 복구합니다."""
        layer = ensure_projection_dataset_view_layer(self.scene)
        layer.update()
        hide_other_paint_objects(self.scene, layer, self.target)
        layer.name = "RenamedPaintLayer"
        restore_other_paint_objects(self.scene)
        self.assertFalse(self.other.hide_get(view_layer=layer))

    def test_saved_object_references_restore_after_reload(self):
        """파일 저장·재로드 뒤 실제 오브젝트 참조와 표시 복구 기록이 유지됩니다."""
        hide_other_paint_objects(self.scene, self.layer, self.target)
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "visibility.blend")
            bpy.ops.wm.save_as_mainfile(filepath=path, check_existing=False)
            bpy.ops.wm.open_mainfile(filepath=path)
            scene = bpy.context.scene
            self.assertTrue(scene.projection_paint.paint_object_visibility_active)
            self.assertTrue(scene.objects["Other"].hide_get())
            restore_other_paint_objects(scene)
            self.assertFalse(scene.objects["Other"].hide_get())
            self.assertEqual(len(scene.projection_paint.paint_object_visibility), 0)


if __name__ == "__main__":
    unittest.main(argv=[__file__], verbosity=2)
