"""Blender Background에서 Camera World Transform과 KDB 후보 선택을 검증합니다."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import sys
import unittest

import bpy
from mathutils import Matrix, Vector


PROJECT_PARENT = Path(__file__).resolve().parents[3]
if str(PROJECT_PARENT) not in sys.path:
    sys.path.insert(0, str(PROJECT_PARENT))

from tools.projection_paint.blender.camera_scene import CameraManifestEntry
from tools.projection_paint.blender.camera_spatial import (
    ViewportPose,
    build_camera_kdb_index,
    camera_kdb_index_is_current,
    select_best_camera,
)


def _flat_identity() -> tuple[float, ...]:
    """Camera Manifest 테스트에 사용할 4×4 단위 행렬을 반환합니다."""

    return tuple(float(Matrix.Identity(4)[row][column]) for row in range(4) for column in range(4))


class CameraSpatialTests(unittest.TestCase):
    """Camera KDB Adapter가 World 좌표와 방향 정책을 지키는지 검증합니다."""

    def setUp(self) -> None:
        """독립 Scene과 모든 Camera가 공유할 Axis Root를 생성합니다."""

        bpy.ops.wm.read_factory_settings(use_empty=True)
        self.scene = bpy.context.scene
        self.root = bpy.data.objects.new("PP_KDB_ROOT", None)
        self.scene.collection.objects.link(self.root)
        self.cameras: list[object] = []

    def tearDown(self) -> None:
        """테스트에서 생성한 Camera Object와 Data를 제거합니다."""

        for camera in tuple(self.cameras):
            data = camera.data
            bpy.data.objects.remove(camera, do_unlink=True)
            if data.users == 0:
                bpy.data.cameras.remove(data)
        if self.root.name in bpy.data.objects:
            bpy.data.objects.remove(self.root, do_unlink=True)

    def _camera(self, image_id: int, position, direction) -> tuple[object, CameraManifestEntry]:
        """지정 World 전 단계 위치와 촬영 방향의 관리 Camera Fixture를 생성합니다."""

        data = bpy.data.cameras.new(f"PP_KDB_DATA_{image_id}")
        camera = bpy.data.objects.new(f"PP_KDB_CAMERA_{image_id}", data)
        self.scene.collection.objects.link(camera)
        camera.parent = self.root
        camera.matrix_parent_inverse = Matrix.Identity(4)
        rotation = Vector(direction).normalized().to_track_quat("-Z", "Y").to_matrix().to_4x4()
        camera.matrix_basis = Matrix.Translation(position) @ rotation
        self.cameras.append(camera)
        entry = CameraManifestEntry(
            image_id=image_id,
            camera_id=image_id,
            frame=image_id,
            image_name=f"{image_id}.png",
            camera_uuid=f"camera-{image_id}",
            marker_name=f"marker-{image_id}",
            matrix_local=_flat_identity(),
            qvec=(1.0, 0.0, 0.0, 0.0),
            tvec=(0.0, 0.0, 0.0),
        )
        return camera, entry

    def _runtime(self, fixtures) -> SimpleNamespace:
        """Camera Fixture를 Adapter가 요구하는 최소 Runtime 형태로 묶습니다."""

        bpy.context.view_layer.update()
        entries = tuple(entry for _camera, entry in fixtures)
        return SimpleNamespace(
            manifest=SimpleNamespace(
                dataset_uuid="kdb-test",
                dataset_revision=3,
                entries=entries,
            ),
            objects={entry.camera_uuid: camera for camera, entry in fixtures},
        )

    def test_axis_root_world_transform_is_indexed_and_invalidated(self) -> None:
        """Axis Root가 적용된 World 위치를 검색하고 Root 변경 시 Index를 무효화합니다."""

        self.root.matrix_world = Matrix.Translation((10.0, -2.0, 4.0))
        fixtures = (
            self._camera(10, (1.0, 0.0, 0.0), (0.0, 0.0, -1.0)),
            self._camera(20, (5.0, 0.0, 0.0), (0.0, 0.0, -1.0)),
        )
        runtime = self._runtime(fixtures)
        index = build_camera_kdb_index(runtime)
        pose = ViewportPose(
            origin=tuple(float(value) for value in fixtures[0][0].matrix_world.translation),
            direction=(0.0, 0.0, -1.0),
        )
        self.assertEqual(select_best_camera(index, pose).entry.image_id, 10)
        self.assertTrue(camera_kdb_index_is_current(index, runtime))
        self.root.matrix_world = Matrix.Translation((11.0, -2.0, 4.0))
        self.assertFalse(camera_kdb_index_is_current(index, runtime))

    def test_same_position_prefers_matching_camera_direction(self) -> None:
        """동일 위치 Camera 중 현재 시선과 촬영 방향이 일치하는 항목을 선택합니다."""

        fixtures = (
            self._camera(10, (0.0, 0.0, 0.0), (1.0, 0.0, 0.0)),
            self._camera(20, (0.0, 0.0, 0.0), (-1.0, 0.0, 0.0)),
        )
        candidate = select_best_camera(
            build_camera_kdb_index(self._runtime(fixtures)),
            ViewportPose(origin=(0.0, 0.0, 0.0), direction=(-1.0, 0.0, 0.0)),
        )
        self.assertEqual(candidate.entry.image_id, 20)
        self.assertAlmostEqual(candidate.alignment, 1.0, places=6)

    def test_distance_gate_rejects_far_aligned_camera(self) -> None:
        """방향이 일치해도 최근접 거리 Gate 밖의 Camera를 선택하지 않습니다."""

        fixtures = (
            self._camera(10, (1.0, 0.0, 0.0), (-1.0, 0.0, 0.0)),
            self._camera(20, (3.0, 0.0, 0.0), (1.0, 0.0, 0.0)),
        )
        candidate = select_best_camera(
            build_camera_kdb_index(self._runtime(fixtures)),
            ViewportPose(origin=(0.0, 0.0, 0.0), direction=(1.0, 0.0, 0.0)),
        )
        self.assertEqual(candidate.entry.image_id, 10)


if __name__ == "__main__":
    unittest.main(argv=[__file__], verbosity=2)
