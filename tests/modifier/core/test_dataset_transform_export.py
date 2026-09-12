"""변환을 적용한 내보내기의 재입력과 출처 보존을 검증합니다."""

from dataclasses import replace
from pathlib import Path
import tempfile
import unittest

from tests.modifier.fixture_utils import make_modern_binary, make_modern_text
from tools.modifier.core.dataset_transform import IDENTITY_TRANSFORM
from tools.modifier.core.export_job import ExportTransactionJob
from tools.modifier.core.export_progress import ExportPhase
from tools.modifier.core.export_transaction import execute_export
from tools.modifier.core.export_types import ExportTransactionError
from tools.modifier.core.export_validation import validate_export_request
from tools.modifier.core.loader import load_reconstruction
from tools.modifier.core.model import Point2DRecord, TrackElement


TRANSFORM = ((0., -2., 0., 4.), (2., 0., 0., -3.), (0., 0., 2., 7.), (0., 0., 0., 1.))


class DatasetTransformExportTests(unittest.TestCase):
    """변환 출력이 저장 형식과 반복 실행에 관계없이 보존되는지 검사합니다."""

    def test_modern_roundtrip_and_second_identity_export(self):
        """텍스트와 바이너리에서 변환이 한 번 적용되고 재출력 시 누적되지 않습니다."""
        for output_format, fixture in (('TEXT', make_modern_text), ('BINARY', make_modern_binary)):
            with self.subTest(output_format=output_format), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                model_directory, images_directory = fixture(root)
                loaded = load_reconstruction(root, model_directory, images_directory, output_format)
                source = loaded.reconstruction
                old_images, old_points = dict(source.images), dict(source.points3d)
                old_rigs, old_frames = dict(source.rigs), dict(source.frames)
                result = execute_export(validate_export_request(loaded.source, source, backup_enabled=False), source, dataset_transform=TRANSFORM)
                reread = load_reconstruction(root, model_directory, images_directory, output_format).reconstruction
                self.assertEqual(reread, result.loaded.reconstruction)
                for key, point in old_points.items():
                    x, y, z = point.xyz
                    self.assertEqual(reread.points3d[key].xyz, (4.-2.*y, 2.*x-3., 2.*z+7.))
                    self.assertEqual(reread.points3d[key].track, point.track)
                for key, image in old_images.items():
                    self.assertEqual(reread.images[key].points2d, image.points2d)
                    self.assertNotEqual(reread.images[key].tvec, image.tvec)
                for key, rig in old_rigs.items():
                    for old, new in zip(rig.sensors, reread.rigs[key].sensors):
                        self.assertEqual(new.qvec, old.qvec)
                        if old.has_pose:
                            self.assertEqual(new.tvec, tuple(2.*v for v in old.tvec))
                for key, frame in old_frames.items():
                    self.assertNotEqual(reread.frames[key].tvec, frame.tvec)
                self.assertEqual(source.images, old_images)
                self.assertEqual(source.points3d, old_points)
                self.assertEqual(source.rigs, old_rigs)
                self.assertEqual(source.frames, old_frames)
                current = result.loaded
                second = execute_export(validate_export_request(current.source, current.reconstruction, backup_enabled=False), current.reconstruction, dataset_transform=IDENTITY_TRANSFORM)
                self.assertEqual(second.loaded.reconstruction, reread)
                self.assertEqual(load_reconstruction(root, model_directory, images_directory, output_format).reconstruction, reread)

    def test_prepare_yields_for_large_point_set(self):
        """대규모 점 변환이 준비 단계의 여러 호출로 분리됩니다."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model_directory, images_directory = make_modern_text(root)
            loaded = load_reconstruction(root, model_directory, images_directory, 'TEXT')
            original = loaded.reconstruction
            sample = next(iter(original.points3d.values()))
            points = dict(original.points3d)
            image = next(iter(original.images.values()))
            observations = list(image.points2d)
            for i in range(100, 4300):
                points[i] = replace(sample, point3d_id=i, track=(TrackElement(image.image_id, len(observations)),))
                observations.append(Point2DRecord(10., 20., i))
            images = dict(original.images)
            images[image.image_id] = replace(image, points2d=tuple(observations))
            source = replace(original, points3d=points, images=images)
            job = ExportTransactionJob(validate_export_request(loaded.source, source, backup_enabled=False), source, dataset_transform=TRANSFORM)
            job.advance()
            self.assertEqual(job.progress.phase, ExportPhase.PREPARE)
            job.advance(work_budget_ms=.001)
            self.assertEqual(job.progress.phase, ExportPhase.PREPARE)
            self.assertGreater(job.progress.completed_units, 0)
            self.assertLess(job.progress.completed_units, job.progress.total_units)
            fractions = [job.progress.overall_fraction]
            while not job.done:
                fractions.append(job.advance(work_budget_ms=10.).overall_fraction)
            self.assertEqual(fractions, sorted(fractions))
            self.assertEqual(len(job.result.loaded.reconstruction.points3d), len(points))
            self.assertEqual(source.points3d[100].xyz, sample.xyz)

    def test_overflow_failure_preserves_source_bytes_and_records(self):
        """실수 범위를 넘는 변환 실패는 원본 파일과 메모리 레코드를 보존합니다."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model_directory, images_directory = make_modern_text(root)
            loaded = load_reconstruction(root, model_directory, images_directory, 'TEXT')
            source = loaded.reconstruction
            before = {path.name: path.read_bytes() for path in model_directory.iterdir() if path.is_file()}
            points = dict(source.points3d)
            matrix = ((1e308, 0., 0., 0.), (0., 1e308, 0., 0.), (0., 0., 1e308, 0.), (0., 0., 0., 1.))
            job = ExportTransactionJob(validate_export_request(loaded.source, source, backup_enabled=False), source, dataset_transform=matrix)
            with self.assertRaises(ExportTransactionError):
                while not job.done:
                    job.advance()
            self.assertTrue(job.failed)
            self.assertFalse(job.error.source_changed)
            self.assertEqual(source.points3d, points)
            self.assertEqual({path.name: path.read_bytes() for path in model_directory.iterdir() if path.is_file()}, before)


if __name__ == '__main__':
    unittest.main()
