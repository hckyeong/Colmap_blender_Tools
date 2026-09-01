"""Modifier Reconstruction의 값 Snapshot 변환 정확성을 검증합니다."""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tools.modifier.blender.projection_snapshot_provider import (
    build_projection_snapshot,
    projection_snapshot_is_current,
)
from tools.modifier.core.model import CameraRecord, ImageRecord, Point3DRecord, Reconstruction


class ProjectionSnapshotProviderTests(unittest.TestCase):
    """비연속 ID·intrinsics·pose·경로·point 전달을 확인합니다."""

    def test_provider_preserves_reconstruction_values(self) -> None:
        """확정 Reconstruction의 PINHOLE·SIMPLE_PINHOLE 값을 그대로 복사합니다."""

        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            image_a = root / "a.png"
            image_b = root / "b.png"
            reconstruction = Reconstruction(
                cameras={
                    2: CameraRecord(2, 0, "SIMPLE_PINHOLE", 800, 600, (500.0, 400.0, 300.0)),
                    19: CameraRecord(19, 1, "PINHOLE", 1200, 900, (700.0, 701.0, 600.0, 450.0)),
                },
                images={
                    4: ImageRecord(4, (1.0, 0.0, 0.0, 0.0), (3.0, 2.0, 1.0), 19, "a.png", ()),
                    27: ImageRecord(27, (0.5, 0.5, 0.5, 0.5), (-1.0, 0.0, 2.0), 2, "b.png", ()),
                },
                points3d={
                    101: Point3DRecord(101, (1.0, 2.0, 3.0), (10, 20, 30), 0.25, ()),
                },
            )
            entries = {
                4: SimpleNamespace(resolved_image_path=image_a),
                27: SimpleNamespace(resolved_image_path=image_b),
            }
            state = SimpleNamespace(
                dataset_uuid="modifier-dataset",
                edit_revision=6,
                current_reconstruction=reconstruction,
                source=SimpleNamespace(
                    source_fingerprint="source-fingerprint",
                    model_directory=root,
                    images_directory=root,
                ),
                image_path_manifest=SimpleNamespace(entries=entries),
            )
            settings = SimpleNamespace(
                dataset_uuid="modifier-dataset",
                edit_revision=6,
                source_fingerprint="source-fingerprint",
                axis_preset="BLENDER",
                axis_forward="Y",
                axis_up="Z",
                point_size=0.125,
                camera_frustum_size=2.5,
            )
            scene = SimpleNamespace(colmap_import=settings)
            with patch(
                "tools.modifier.blender.projection_snapshot_provider.ensure_runtime",
                return_value=state,
            ):
                snapshot = build_projection_snapshot(scene)
                self.assertTrue(projection_snapshot_is_current(scene, snapshot))
                settings.edit_revision += 1
                self.assertFalse(projection_snapshot_is_current(scene, snapshot))

        self.assertEqual(tuple(item.camera_id for item in snapshot.cameras), (2, 19))
        self.assertEqual(tuple(item.image_id for item in snapshot.images), (4, 27))
        self.assertEqual(snapshot.images[0].qvec, (1.0, 0.0, 0.0, 0.0))
        self.assertEqual(snapshot.images[0].tvec, (3.0, 2.0, 1.0))
        self.assertEqual(snapshot.images[0].image_path, str(image_a))
        self.assertEqual(snapshot.points3d[0].xyz, (1.0, 2.0, 3.0))
        self.assertEqual(snapshot.display_defaults.point_size, 0.125)


if __name__ == "__main__":
    unittest.main()
