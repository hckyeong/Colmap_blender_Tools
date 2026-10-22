"""Frame·Axis·Fingerprint와 Scene 외부 Staging Payload를 검증합니다."""

from __future__ import annotations

import tempfile
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path

from tools.projection_paint.core.axis import axes_for_preset, axis_matrix
from tools.projection_paint.core.colmap.camera_import_job import (
    load_camera_image_dataset,
    prepare_import_staging,
    revalidate_import_staging,
)
from tools.projection_paint.core.colmap.errors import ColmapError
from tools.projection_paint.core.colmap.fingerprint import semantic_fingerprint
from tools.projection_paint.core.colmap.frame_mapping import build_frame_mapping
from tools.projection_paint.core.colmap.layout import resolve_layout

from support import (
    sample_cameras,
    sample_images,
    sample_points3d,
    write_image_files,
    write_layout,
)


class StagingAxisFingerprintTests(unittest.TestCase):
    """순수 변환과 Import 준비 Payload의 결정성을 검증합니다."""

    def test_frame_mapping_uses_sorted_image_ids(self) -> None:
        """불연속 IMAGE_ID를 오름차순 연속 Frame에 배치합니다."""
        mapping = build_frame_mapping(sample_images(), frame_start=7)
        self.assertEqual(
            dict(mapping.frame_to_image),
            {7: 2, 8: 10, 9: 105},
        )
        self.assertEqual(
            dict(mapping.image_to_frame),
            {2: 7, 10: 8, 105: 9},
        )
        with self.assertRaises(TypeError):
            mapping.frame_to_image[7] = 999

    def test_frame_mapping_rejects_range_overflow(self) -> None:
        """연속 Frame 끝이 허용 범위를 넘으면 거부합니다."""
        with self.assertRaisesRegex(ColmapError, "허용 범위"):
            build_frame_mapping(sample_images(), frame_start=1_048_574)

    def test_axis_presets_and_matrix_are_pure(self) -> None:
        """축 프리셋과 RC 변환 행렬을 Blender 없이 계산합니다."""
        self.assertEqual(axes_for_preset("REALITY_CAPTURE"), ("Z", "-Y"))
        self.assertEqual(axes_for_preset("BLENDER"), ("Y", "Z"))
        self.assertEqual(
            axes_for_preset(
                "CUSTOM",
                custom_forward="-X",
                custom_up="Z",
            ),
            ("-X", "Z"),
        )
        self.assertEqual(
            axis_matrix("Y", "Z"),
            (
                (1.0, 0.0, 0.0, 0.0),
                (0.0, 1.0, 0.0, 0.0),
                (0.0, 0.0, 1.0, 0.0),
                (0.0, 0.0, 0.0, 1.0),
            ),
        )
        rc = axis_matrix("Z", "-Y")
        self.assertEqual((rc[0][2], rc[1][2], rc[2][2]), (0.0, 1.0, 0.0))
        self.assertEqual((rc[0][1], rc[1][1], rc[2][1]), (0.0, 0.0, -1.0))
        with self.assertRaises(ValueError):
            axis_matrix("X", "-X")

    def test_fingerprint_is_mapping_order_independent(self) -> None:
        """Record Mapping 삽입 순서가 달라도 Fingerprint는 같습니다."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_layout(root / "a", "BINARY")
            write_layout(
                root / "b",
                "BINARY",
                reversed(sample_cameras()),
                reversed(sample_images()),
            )
            first = load_camera_image_dataset(resolve_layout(root / "a"))
            second = load_camera_image_dataset(resolve_layout(root / "b"))
            self.assertEqual(
                semantic_fingerprint(first),
                semantic_fingerprint(second),
            )

    def test_fingerprint_changes_with_semantic_pose(self) -> None:
        """Image Pose가 바뀌면 Typed Fingerprint도 바뀝니다."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_layout(root / "a", "BINARY")
            changed = list(sample_images())
            original = changed[0]
            changed[0] = type(original)(
                original.image_id,
                original.camera_id,
                original.qvec,
                (99.0, *original.tvec[1:]),
                original.image_name,
            )
            write_layout(root / "b", "BINARY", sample_cameras(), changed)
            first = load_camera_image_dataset(resolve_layout(root / "a"))
            second = load_camera_image_dataset(resolve_layout(root / "b"))
            self.assertNotEqual(
                semantic_fingerprint(first),
                semantic_fingerprint(second),
            )

    def test_prepare_staging_is_complete_and_immutable(self) -> None:
        """Staging Payload가 공유 Camera Data와 연속 Marker를 완성합니다."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sparse = root / "sparse" / "0"
            images_root = root / "images"
            write_layout(sparse, "BINARY")
            write_image_files(images_root, sample_images())
            payload = prepare_import_staging(
                sparse,
                images_root,
                frame_start=4,
            )
            self.assertEqual(len(payload.camera_data), 2)
            self.assertEqual(len(payload.camera_objects), 3)
            self.assertEqual(len(payload.points), len(sample_points3d()))
            self.assertEqual(
                tuple((item.frame, item.image_id) for item in payload.markers),
                ((4, 2), (5, 10), (6, 105)),
            )
            normalized = next(
                item for item in payload.camera_objects if item.image_id == 10
            )
            self.assertEqual(normalized.qvec, (1.0, 0.0, 0.0, 0.0))
            self.assertEqual(len(payload.source_fingerprint), 64)
            with self.assertRaises(FrozenInstanceError):
                payload.source_fingerprint = "changed"
            revalidate_import_staging(payload)

    def test_revalidation_detects_source_change(self) -> None:
        """Staging 뒤 Camera Source 변경을 Commit 직전 검증에서 감지합니다."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sparse = root / "sparse"
            images_root = root / "images"
            write_layout(sparse, "TEXT")
            write_image_files(images_root, sample_images())
            payload = prepare_import_staging(
                sparse,
                images_root,
                input_format="TEXT",
            )
            with (sparse / "cameras.txt").open("a", encoding="utf-8") as stream:
                stream.write("# changed\n")
            with self.assertRaisesRegex(ColmapError, "식별자|내용|Identity|Fingerprint"):
                revalidate_import_staging(payload)


if __name__ == "__main__":
    unittest.main()
