"""참고용 입력의 표본 상한·전체 검증·재파싱 없는 변경 감지를 검증합니다."""

from dataclasses import replace
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import numpy as np

from shared.colmap_io import ColmapIOError, create_incremental_sparse_model_reader, discover_sparse_model, validate_model_structure
from shared.colmap_io.reference_preview import ColmapReferencePreview
from tools.projection_paint.core.colmap.camera_import_job import ImportStagingPreparationJob, revalidate_import_staging
from tools.projection_paint.core.colmap.model import Point3DRecord, TrackElement
from support import sample_images, write_image_files, write_images_text, write_layout, write_points3d_binary, write_points3d_text


def reference_points(count=37):
    """큰 비연속 식별자와 원본 전체 경계·관측을 가진 자료를 만듭니다."""
    return tuple(Point3DRecord((1 << 63) + i * 17 + 1, (float(i), -float(i), 2.0), (12, 64, 255), 0.5, (TrackElement(2, i),)) for i in range(count))


def read_model(root, format_name, points, limit):
    """같은 공통 파서의 전체 또는 참고 보관 정책을 끝까지 실행합니다."""
    write_layout(root, format_name, points3d=points)
    if not points:
        if format_name == "BINARY":
            write_points3d_binary(root / "points3D.bin", ())
        else:
            write_points3d_text(root / "points3D.txt", ())
    reader = create_incremental_sparse_model_reader(discover_sparse_model(root), preview_point_limit=limit)
    try:
        while not reader.is_completed:
            reader.step(max_records=4096)
        return reader.model
    finally:
        reader.close()


class ReferenceImportTests(unittest.TestCase):
    """선택되지 않은 자료도 검사하고 원본과 표본의 의미를 구별하는지 확인합니다."""

    def test_default_reader_keeps_all_points_and_observations(self):
        """공통 API의 기본 입력이 표본으로 제한되지 않는지 확인합니다."""
        with TemporaryDirectory() as directory:
            for format_name in ("TEXT", "BINARY"):
                model = read_model(Path(directory) / format_name, format_name, reference_points(), None)
                self.assertNotIsInstance(model, ColmapReferencePreview)
                self.assertEqual(37, model.points.count)
                self.assertEqual(37, len(model.points.track_image_ids))
                self.assertEqual(6, len(model.images.point2d_xy))
                validate_model_structure(model)

    def test_preview_preserves_cameras_bounds_and_large_ids(self):
        """참고 표본이 전체 카메라·원본 경계를 유지하고 관측 배열을 보관하지 않습니다."""
        with TemporaryDirectory() as directory:
            samples = []
            for format_name in ("TEXT", "BINARY"):
                points = reference_points() if format_name == "TEXT" else tuple(reversed(reference_points()))
                model = read_model(Path(directory) / format_name, format_name, points, 7)
                self.assertIsInstance(model, ColmapReferencePreview)
                self.assertEqual((37, 7, 37, 6), (model.source_point_count, model.points.count, model.source_track_count, model.source_observation_count))
                self.assertEqual((2, 3), (len(model.cameras), len(model.images.image_records)))
                self.assertEqual((0, 0), (len(model.points.track_image_ids), len(model.images.point2d_xy)))
                np.testing.assert_array_equal(model.bounds_min, (0, -36, 2))
                np.testing.assert_array_equal(model.bounds_max, (36, 0, 2))
                self.assertFalse(model.points.point_ids.flags.writeable)
                self.assertTrue(np.all(model.points.point_ids > (1 << 63)))
                with self.assertRaises(TypeError):
                    validate_model_structure(model)
                samples.append(model.points.point_ids)
            np.testing.assert_array_equal(*samples)

    def test_empty_and_below_limit_keep_valid_points(self):
        """빈 점군과 상한 이하 자료의 기존 입력 동작을 유지합니다."""
        with TemporaryDirectory() as directory:
            for count in (0, 7, 8):
                model = read_model(Path(directory) / str(count), "BINARY", reference_points(count), 7)
                self.assertEqual(min(count, 7), model.points.count)
                self.assertEqual(count, model.source_point_count)

    def test_invalid_point_limit_is_rejected(self):
        """잘못된 설정으로 상한이 무효화되거나 무제한 입력이 되지 않도록 합니다."""
        with TemporaryDirectory() as directory:
            for limit in (0, -1, True, 1.5, 100_000_001):
                with self.assertRaises(ValueError):
                    read_model(Path(directory), "TEXT", (), limit)

    def test_twenty_thousand_limit_across_blocks_and_duplicate_outside_sample(self):
        """기본 상한과 블록 경계를 넘는 표본 선택·원본 중복 검사를 확인합니다."""
        with TemporaryDirectory() as directory:
            root = Path(directory)
            points = reference_points(20_001)
            model = read_model(root, "BINARY", points, 20_000)
            self.assertEqual((20_001, 20_000), (model.source_point_count, model.points.count))
            reverse = read_model(root, "BINARY", tuple(reversed(points)), 20_000)
            np.testing.assert_array_equal(model.points.point_ids, reverse.points.point_ids)
            with self.assertRaises(ColmapIOError):
                read_model(root, "BINARY", points + (points[0],), 1)

    def test_discarded_point_errors_and_duplicate_ids_are_rejected(self):
        """대표점 밖의 수치 오류와 전체 식별자 중복도 거부합니다."""
        with TemporaryDirectory() as directory:
            normal = reference_points()
            variants = (
                normal + (normal[-1],),
                normal[:-1] + (replace(normal[-1], error=-1.0),),
                normal[:-1] + (replace(normal[-1], xyz=(float("nan"), 0, 0)),),
                normal[:-1] + (replace(normal[-1], track=(TrackElement(0, 0),)),),
            )
            for format_name in ("TEXT", "BINARY"):
                for points in variants:
                    with self.subTest(format_name=format_name, last=points[-1]):
                        with self.assertRaises(ColmapIOError):
                            read_model(Path(directory), format_name, points, 1)

    def test_discarded_image_observation_is_still_validated(self):
        """표시하지 않는 이미지 관측의 비유한 좌표를 거부합니다."""
        with TemporaryDirectory() as directory:
            root = Path(directory)
            write_layout(root, "TEXT", points3d=reference_points())
            write_images_text(root / "images.txt", sample_images(), point_line="nan 0 -1")
            reader = create_incremental_sparse_model_reader(discover_sparse_model(root), preview_point_limit=1)
            with self.assertRaises(ColmapIOError):
                while not reader.is_completed:
                    reader.step()
            self.assertFalse(reader.has_open_file)

    def test_revalidation_hash_detects_same_size_and_timestamp_change(self):
        """크기와 수정 시간을 복원한 파일 내용 변경도 재파싱 없이 감지합니다."""
        with TemporaryDirectory() as directory:
            root = Path(directory)
            sparse, images = root / "sparse", root / "images"
            write_layout(sparse, "TEXT", points3d=reference_points())
            write_image_files(images, sample_images())
            staging = ImportStagingPreparationJob(sparse, images, point_limit=7).run_to_completion()
            with patch("tools.projection_paint.core.colmap.camera_import_job.shared_dataset_steps", side_effect=AssertionError("재파싱 호출")):
                revalidate_import_staging(staging)
                source = sparse / "points3D.txt"
                before = source.stat()
                original = source.read_bytes()
                source.write_bytes(original.replace(b"0.5", b"0.6", 1))
                os.utime(source, ns=(before.st_atime_ns, before.st_mtime_ns))
                self.assertEqual(before.st_size, source.stat().st_size)
                with self.assertRaisesRegex(Exception, "내용"):
                    revalidate_import_staging(staging)


if __name__ == "__main__":
    unittest.main()
