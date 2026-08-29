"""COLMAP Core Adapter와 Blender Camera Commit 경계를 통합 검증합니다."""

from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest

import bpy


PROJECT_PARENT = Path(__file__).resolve().parents[3]
if str(PROJECT_PARENT) not in sys.path:
    sys.path.insert(0, str(PROJECT_PARENT))

from tools import projection_paint as addon
from tools.projection_paint.blender.camera_import import (
    CameraImportJob,
    _advance_import_session,
    _progress_values,
)
from tools.projection_paint.blender.core_adapter import (
    build_camera_import_request,
    create_camera_import_request_job,
)
from tools.projection_paint.blender.import_progress_session import (
    create_import_progress_session,
    release_import_progress_session,
)
from tools.projection_paint.core.colmap.errors import ColmapError
from shared.blender_runtime.scene_marker_session import (
    acquire_scene_marker_session,
    release_scene_marker_session,
)


def _write_dataset(root: Path) -> tuple[Path, Path]:
    """불연속 IMAGE_ID 두 개의 최소 COLMAP Text Dataset을 작성합니다."""

    sparse = root / "sparse" / "0"
    images = root / "images"
    sparse.mkdir(parents=True)
    images.mkdir()
    (sparse / "cameras.txt").write_text(
        "# Camera list\n"
        "7 PINHOLE 64 32 40 40 32 16\n",
        encoding="utf-8",
    )
    (sparse / "images.txt").write_text(
        "# Image list\n"
        "30 1 0 0 0 1 0 0 7 b.jpg\n"
        "\n"
        "10 1 0 0 0 0 0 0 7 a.jpg\n"
        "\n",
        encoding="utf-8",
    )
    (sparse / "points3D.txt").write_text(
        "# Point3D list\n"
        "42 1 2 3 255 128 0 0.25 10 0 30 1\n",
        encoding="utf-8",
    )
    (images / "a.jpg").write_bytes(b"image-a")
    (images / "b.jpg").write_bytes(b"image-b")
    return sparse, images


class CoreAdapterIntegrationTests(unittest.TestCase):
    """Core Staging과 Blender Commit의 IMAGE_ID·경로 계약을 검증합니다."""

    def setUp(self) -> None:
        """빈 Blender 파일에 Extension을 등록합니다."""

        bpy.ops.wm.read_factory_settings(use_empty=True)
        addon.register()

    def tearDown(self) -> None:
        """각 테스트가 등록한 Extension과 Runtime을 해제합니다."""

        release_scene_marker_session(bpy.context.scene, "projection_paint")
        addon.unregister()

    def test_dataset_root_builds_atomic_camera_request(self) -> None:
        """Dataset Root가 연속 Frame과 영구 Image Path Manifest로 Commit됩니다."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _sparse, images = _write_dataset(root)
            settings = bpy.context.scene.projection_paint
            settings.dataset_root = str(root)
            settings.input_format = "TEXT"
            settings.camera_axis_preset = "BLENDER"
            settings.frame_start = 4
            request = build_camera_import_request(bpy.context)
            acquire_scene_marker_session(bpy.context.scene, "projection_paint")
            self.assertEqual({image.image_id for image in request.images}, {10, 30})
            self.assertEqual(set(request.image_paths), {10, 30})
            result = CameraImportJob(
                bpy.context.scene,
                request,
            ).run_to_completion()
            self.assertEqual(result.camera_count, 2)
            self.assertEqual(result.point_count, 1)
            self.assertEqual(bpy.context.scene.frame_start, 4)
            self.assertEqual(bpy.context.scene.frame_end, 5)
            self.assertEqual(
                Path(settings.sparse_directory),
                (root / "sparse" / "0").resolve(),
            )
            self.assertEqual(Path(settings.images_directory), images.resolve())
            paths = json.loads(settings.image_path_manifest_json)
            self.assertEqual(set(paths), {"10", "30"})
            self.assertTrue(all(Path(path).is_file() for path in paths.values()))
            point_objects = [
                obj
                for obj in bpy.context.scene.objects
                if obj.get("pp_record_kind") == "POINT_MESH"
            ]
            self.assertEqual(len(point_objects), 1)
            self.assertEqual(len(point_objects[0].data.vertices), 1)

    def test_source_change_before_commit_fails_closed(self) -> None:
        """Staging 뒤 Source Image 변경이 Scene 연결 전에 Import를 중단합니다."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _sparse, images = _write_dataset(root)
            settings = bpy.context.scene.projection_paint
            settings.dataset_root = str(root)
            settings.input_format = "TEXT"
            settings.camera_axis_preset = "BLENDER"
            request = build_camera_import_request(bpy.context)
            acquire_scene_marker_session(bpy.context.scene, "projection_paint")
            (images / "a.jpg").write_bytes(b"changed-source")
            with self.assertRaises(ColmapError):
                CameraImportJob(
                    bpy.context.scene,
                    request,
                ).run_to_completion()
            self.assertFalse(tuple(bpy.context.scene.timeline_markers))
            self.assertFalse(settings.dataset_uuid)

    def test_modal_session_reports_core_scene_and_revalidation_details(self) -> None:
        """Modal 상태기가 준비·Scene·재검증 세부 퍼센트를 모두 거치는지 확인합니다."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_dataset(root)
            settings = bpy.context.scene.projection_paint
            settings.dataset_root = str(root)
            settings.input_format = "TEXT"
            settings.camera_axis_preset = "BLENDER"
            session = create_import_progress_session(
                bpy.context.scene,
                create_camera_import_request_job,
            )
            acquire_scene_marker_session(bpy.context.scene, "projection_paint")
            stages = []
            fractions = []
            phases = []
            while session.stage != "DONE":
                stages.append(session.stage)
                phase, fraction, _completed, _total = _progress_values(session)
                phases.append(phase)
                fractions.append(fraction)
                _advance_import_session(bpy.context, session)
            stages.append(session.stage)
            phases.append(_progress_values(session)[0])
            fractions.append(_progress_values(session)[1])
            release_import_progress_session(session.job_id)

        self.assertEqual(sorted(fractions), fractions)
        self.assertEqual(1.0, fractions[-1])
        self.assertIn("REVALIDATE", stages)
        self.assertIn("Reading image pose records", phases)
        self.assertIn("Creating Point3D mesh", phases)
        self.assertTrue(any(phase.startswith("Rechecking:") for phase in phases))


if __name__ == "__main__":
    unittest.main(argv=[__file__], verbosity=2)
