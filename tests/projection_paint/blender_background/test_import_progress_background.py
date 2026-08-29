"""Blender Background에서 COLMAP Import 진행 세션과 정리를 검증합니다."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import sys
import unittest

import bpy


PROJECT_PARENT = Path(__file__).resolve().parents[3]
if str(PROJECT_PARENT) not in sys.path:
    sys.path.insert(0, str(PROJECT_PARENT))

from tools import projection_paint as addon
from tools.projection_paint.blender.camera_import import (
    CameraImportRequest,
    _advance_import_session,
    _import_progress_overlay_view,
    _progress_values,
)
from tools.projection_paint.blender.import_progress_session import (
    clear_import_progress_sessions,
    create_import_progress_session,
    import_progress_session_count,
    release_import_progress_session,
)
from shared.blender_runtime.scene_marker_session import (
    acquire_scene_marker_session,
    release_scene_marker_session,
)


def _request(dataset_uuid: str) -> CameraImportRequest:
    """진행 테스트에 필요한 Camera 두 장과 Point 하나를 구성합니다."""

    camera = SimpleNamespace(
        camera_id=7,
        model_name="PINHOLE",
        width=1920,
        height=1080,
        params=(1200.0, 1190.0, 960.0, 540.0),
    )
    images = tuple(
        SimpleNamespace(
            image_id=image_id,
            camera_id=7,
            qvec=(1.0, 0.0, 0.0, 0.0),
            tvec=(float(index), 0.0, 0.0),
            name=f"{image_id}.jpg",
        )
        for index, image_id in enumerate((10, 30))
    )
    point = SimpleNamespace(
        point3d_id=5,
        xyz=(1.0, 2.0, 3.0),
        rgb=(255, 128, 0),
        error=0.25,
    )
    return CameraImportRequest(
        dataset_uuid=dataset_uuid,
        dataset_revision=1,
        source_fingerprint=f"fingerprint-{dataset_uuid}",
        cameras={7: camera},
        images=images,
        points=(point,),
        frame_start=1,
        axis_preset="BLENDER",
    )


class ImportProgressBackgroundTest(unittest.TestCase):
    """진행률 단조 증가와 중단 시 Staging 정리를 검증합니다."""

    @classmethod
    def setUpClass(cls) -> None:
        """Extension을 테스트 Process에 한 번 등록합니다."""

        addon.register()

    @classmethod
    def tearDownClass(cls) -> None:
        """진행 세션을 비운 뒤 Extension을 해제합니다."""

        clear_import_progress_sessions()
        addon.unregister()

    def setUp(self) -> None:
        """각 테스트에 독립 Scene과 최소 UI 설정을 만듭니다."""

        self.scene = bpy.data.scenes.new(f"PP_PROGRESS_{self._testMethodName}")
        self.scene.projection_paint.dataset_root = "E:/dataset"
        self.scene.projection_paint.input_format = "AUTO"
        self.context = SimpleNamespace(scene=self.scene)

    def tearDown(self) -> None:
        """진행 세션과 테스트가 만든 애드온 소유 ID를 제거합니다."""

        clear_import_progress_sessions()
        release_scene_marker_session(self.scene, "projection_paint")
        if self.scene.name in bpy.data.scenes:
            bpy.data.scenes.remove(self.scene)
        for collection in tuple(bpy.data.collections):
            if collection.get("pp_owner") == "blender_projection_painting":
                bpy.data.collections.remove(collection)
        for obj in tuple(bpy.data.objects):
            if obj.get("pp_owner") == "blender_projection_painting":
                bpy.data.objects.remove(obj, do_unlink=True)
        for values in (
            bpy.data.cameras,
            bpy.data.meshes,
            bpy.data.node_groups,
            bpy.data.materials,
        ):
            for data in tuple(values):
                if (
                    data.get("pp_owner") == "blender_projection_painting"
                    and data.users == 0
                ):
                    values.remove(data)

    def test_progress_is_monotonic_and_session_is_released(self) -> None:
        """Import 단계가 단조 증가하고 성공 뒤 Registry가 비는지 확인합니다."""

        acquire_scene_marker_session(self.scene, "projection_paint")
        session = create_import_progress_session(
            self.scene,
            lambda _context: _request("progress-complete"),
        )
        fractions = [_progress_values(session)[1]]
        phases = [_progress_values(session)[0]]
        while session.stage != "DONE":
            _advance_import_session(self.context, session)
            fractions.append(_progress_values(session)[1])
            phases.append(_progress_values(session)[0])
        view = _import_progress_overlay_view(session)
        self.assertEqual("Import complete", view.phase)
        self.assertEqual(1.0, view.fraction)
        self.assertEqual(sorted(fractions), fractions)
        self.assertIn("Preparing Point3D buffers", phases)
        self.assertIn("Creating Point3D mesh", phases)
        self.assertIn("Writing Point3D colors", phases)
        self.assertGreaterEqual(len(set(fractions)), 10)
        self.assertEqual(2, session.result.camera_count)
        self.assertEqual(1, session.result.point_count)
        release_import_progress_session(session.job_id)
        self.assertEqual(0, import_progress_session_count())

    def test_clear_aborts_uncommitted_staging(self) -> None:
        """수명주기 중단이 Commit 전 Staging과 진행 Registry를 정리하는지 확인합니다."""

        session = create_import_progress_session(
            self.scene,
            lambda _context: _request("progress-abort"),
        )
        _advance_import_session(self.context, session)
        _advance_import_session(self.context, session)
        self.assertIsNotNone(session.job)
        clear_import_progress_sessions()
        self.assertEqual(0, import_progress_session_count())
        self.assertFalse(
            any(
                collection.get("pp_dataset_uuid") == "progress-abort"
                for collection in bpy.data.collections
            )
        )


if __name__ == "__main__":
    unittest.main(argv=[__file__], verbosity=2)
