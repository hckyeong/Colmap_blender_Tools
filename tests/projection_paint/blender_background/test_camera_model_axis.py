"""Blender Background에서 Model Axis 적용의 멱등성을 검증합니다."""

from __future__ import annotations

from pathlib import Path
import sys
import unittest

import bpy
from mathutils import Matrix


PROJECT_PARENT = Path(__file__).resolve().parents[3]
if str(PROJECT_PARENT) not in sys.path:
    sys.path.insert(0, str(PROJECT_PARENT))

from tools import projection_paint as addon
from tools.projection_paint.blender.model_import import apply_model_axis


class ModelAxisTest(unittest.TestCase):
    """Model Axis Root와 중복 실행 방지 계약을 검증합니다."""

    @classmethod
    def setUpClass(cls) -> None:
        """Extension을 테스트 Process에 한 번 등록합니다."""

        addon.register()

    @classmethod
    def tearDownClass(cls) -> None:
        """Extension을 테스트 Process에서 해제합니다."""

        addon.unregister()

    def setUp(self) -> None:
        """독립 Scene과 Mesh Object를 생성합니다."""

        self.scene = bpy.data.scenes.new(f"PP_AXIS_{self._testMethodName}")
        mesh = bpy.data.meshes.new("PP_AXIS_MESH")
        self.obj = bpy.data.objects.new("PP_AXIS_OBJECT", mesh)
        self.scene.collection.objects.link(self.obj)
        self.obj.matrix_world = Matrix.Translation((1.0, 2.0, 3.0))

    def tearDown(self) -> None:
        """테스트 Scene과 남은 Object·Mesh를 제거합니다."""

        if self.scene.name in bpy.data.scenes:
            bpy.data.scenes.remove(self.scene)
        for obj in tuple(bpy.data.objects):
            if obj.name.startswith("PP_AXIS_") or obj.get("pp_record_kind") == "MODEL_AXIS_ROOT":
                bpy.data.objects.remove(obj, do_unlink=True)
        for mesh in tuple(bpy.data.meshes):
            if mesh.name.startswith("PP_AXIS_") and mesh.users == 0:
                bpy.data.meshes.remove(mesh)

    def test_apply_once_and_repeat_is_no_op(self) -> None:
        """같은 Revision의 두 번째 축 적용이 Transform을 중복하지 않는지 확인합니다."""

        first = apply_model_axis(
            self.scene,
            (self.obj,),
            preset="BLENDER",
            dataset_uuid="axis-dataset",
            axis_revision="1",
        )
        matrix_after_first = self.obj.matrix_world.copy()
        second = apply_model_axis(
            self.scene,
            (self.obj,),
            preset="BLENDER",
            dataset_uuid="axis-dataset",
            axis_revision="1",
        )
        self.assertFalse(first.no_op)
        self.assertTrue(second.no_op)
        self.assertEqual(first.root, second.root)
        self.assertEqual(matrix_after_first, self.obj.matrix_world)


if __name__ == "__main__":
    unittest.main(argv=[__file__], verbosity=2)
