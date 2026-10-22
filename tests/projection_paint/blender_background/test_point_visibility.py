"""Blender Background에서 Paint 진입용 Point 표시 숨김과 복구를 검증합니다."""

from __future__ import annotations

from pathlib import Path
import sys
import unittest

import bpy


PROJECT_PARENT = Path(__file__).resolve().parents[3]
if str(PROJECT_PARENT) not in sys.path:
    sys.path.insert(0, str(PROJECT_PARENT))

from tools import projection_paint as addon
from tools.projection_paint.blender.ownership import owned_uuid_of, tag_owned
from tools.projection_paint.blender.point_scene import (
    hide_dataset_points,
    restore_dataset_points,
)
from tools.projection_paint.blender.dataset_view_layer import (
    hide_paint_dataset_references,
    restore_paint_dataset_references,
)


class PointVisibilityTests(unittest.TestCase):
    """관리 Point의 눈 표시만 변경하고 사용자 변경은 보존하는지 검사합니다."""

    def setUp(self) -> None:
        """빈 Scene에 현재 Dataset 소유 Point Object를 만듭니다."""
        bpy.ops.wm.read_factory_settings(use_empty=True)
        addon.register()
        self.scene = bpy.context.scene
        self.view_layer = bpy.context.view_layer
        self.scene.projection_paint.dataset_uuid = "dataset-point-visibility"
        mesh = bpy.data.meshes.new("POINT_TEST_MESH")
        self.point = bpy.data.objects.new("POINT_TEST", mesh)
        tag_owned(
            self.point,
            "POINT_MESH",
            dataset_uuid="dataset-point-visibility",
        )
        self.scene.collection.objects.link(self.point)
        self.view_layer.update()
        self.assertEqual(
            "dataset-point-visibility",
            self.scene.projection_paint.dataset_uuid,
        )
        self.assertEqual("POINT_MESH", self.point.get("pp_record_kind"))

    def tearDown(self) -> None:
        """테스트 Extension 등록을 해제합니다."""
        addon.unregister()

    def test_hide_and_restore_visible_point(self) -> None:
        """보이던 Point는 숨긴 뒤 원래 보임 상태로 복구합니다."""
        records = hide_dataset_points(self.scene, self.view_layer)
        self.assertEqual(1, len(records))
        self.assertTrue(self.point.hide_get(view_layer=self.view_layer))
        restore_dataset_points(self.scene, self.view_layer, records)
        self.assertFalse(self.point.hide_get(view_layer=self.view_layer))

    def test_restore_preserves_user_visibility_change(self) -> None:
        """Paint 중 사용자가 다시 켠 Point 눈 표시는 종료 때 덮어쓰지 않습니다."""
        records = hide_dataset_points(self.scene, self.view_layer)
        self.point.hide_set(False, view_layer=self.view_layer)
        restore_dataset_points(self.scene, self.view_layer, records)
        self.assertFalse(self.point.hide_get(view_layer=self.view_layer))

    def test_restore_preserves_original_hidden_state(self) -> None:
        """진입 전부터 숨겨진 Point는 종료 뒤에도 숨김 상태를 유지합니다."""
        self.point.hide_set(True, view_layer=self.view_layer)
        records = hide_dataset_points(self.scene, self.view_layer)
        restore_dataset_points(self.scene, self.view_layer, records)
        self.assertTrue(self.point.hide_get(view_layer=self.view_layer))

    def test_reference_visibility_restores_the_recorded_view_layer(self) -> None:
        """카메라 배경을 유지하고 다른 View Layer로 이동해도 점군의 이전 표시를 복구합니다."""
        camera = bpy.data.objects.new("PP_REFERENCE_CAMERA", bpy.data.cameras.new("PP_REFERENCE_DATA"))
        tag_owned(camera, "CAMERA_OBJECT", dataset_uuid="dataset-point-visibility")
        self.scene.collection.objects.link(camera)
        self.scene.camera = camera
        user_camera = bpy.data.objects.new("USER_CAMERA", bpy.data.cameras.new("USER_CAMERA_DATA"))
        self.scene.collection.objects.link(user_camera)
        paint_layer = self.scene.view_layers.new("PaintReferenceTest")
        self.point.hide_set(True, view_layer=paint_layer)
        records = hide_paint_dataset_references(self.scene, paint_layer)
        self.assertEqual(len(records), 1)
        self.assertFalse(camera.hide_get(view_layer=paint_layer))
        self.assertFalse(camera.hide_get(view_layer=self.view_layer))
        self.assertFalse(user_camera.hide_get(view_layer=paint_layer))
        self.assertIs(self.scene.camera, camera)
        restore_paint_dataset_references(self.scene, records)
        self.assertFalse(camera.hide_get(view_layer=paint_layer))
        self.assertTrue(self.point.hide_get(view_layer=paint_layer))

    def test_previous_camera_hiding_record_restores_visibility(self) -> None:
        """이전 설치본이 저장한 카메라 숨김 기록도 정식 복구 경로로 처리합니다."""
        camera = bpy.data.objects.new("PREVIOUS_CAMERA", bpy.data.cameras.new("PREVIOUS_CAMERA_DATA"))
        tag_owned(camera, "CAMERA_OBJECT", dataset_uuid="dataset-point-visibility")
        self.scene.collection.objects.link(camera)
        self.view_layer.update()
        camera.hide_set(True, view_layer=self.view_layer)
        record = {
            "uuid": owned_uuid_of(camera),
            "kind": "CAMERA_OBJECT",
            "dataset_uuid": "dataset-point-visibility",
            "view_layer": self.view_layer.name,
            "original_hidden": False,
            "applied_hidden": True,
        }
        restore_paint_dataset_references(self.scene, (record,))
        self.assertFalse(camera.hide_get(view_layer=self.view_layer))

    def test_reference_restore_preserves_user_changes_and_other_datasets(self) -> None:
        """사용자가 다시 켠 참고 표시와 다른 데이터셋의 카메라는 변경하지 않습니다."""
        camera = bpy.data.objects.new("OTHER_CAMERA", bpy.data.cameras.new("OTHER_CAMERA_DATA"))
        tag_owned(camera, "CAMERA_OBJECT", dataset_uuid="other-dataset")
        self.scene.collection.objects.link(camera)
        self.view_layer.update()
        records = hide_paint_dataset_references(self.scene, self.view_layer)
        self.assertEqual(len(records), 1)
        self.assertFalse(camera.hide_get(view_layer=self.view_layer))
        self.point.hide_set(False, view_layer=self.view_layer)
        restore_paint_dataset_references(self.scene, records)
        self.assertFalse(self.point.hide_get(view_layer=self.view_layer))


if __name__ == "__main__":
    unittest.main(argv=[__file__], verbosity=2)
