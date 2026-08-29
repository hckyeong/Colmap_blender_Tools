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
from tools.projection_paint.blender.ownership import tag_owned
from tools.projection_paint.blender.point_scene import (
    hide_dataset_points,
    restore_dataset_points,
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


if __name__ == "__main__":
    unittest.main(argv=[__file__], verbosity=2)
