"""COLMAP Core Import 준비의 세부 진행률 계약을 검증합니다."""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from tools.projection_paint.core.colmap.camera_import_job import (
    ImportPreparationPhase,
    ImportStagingPreparationJob,
    ImportStagingRevalidationJob,
    prepare_import_staging,
)
from tools.projection_paint.core.colmap.model import Point3DRecord, TrackElement

from support import sample_images, write_image_files, write_layout


class ImportPreparationProgressTests(unittest.TestCase):
    """실제 Byte와 레코드 기반 진행률이 단조 증가하는지 검증합니다."""

    def test_progress_visits_detailed_phases_and_advances_within_point_read(self):
        """Point3D 읽기 내부에서도 중간 퍼센트가 여러 번 갱신되는지 확인합니다."""

        points = tuple(
            Point3DRecord(
                point3d_id=index + 1,
                xyz=(float(index), 0.0, 0.0),
                rgb=(index % 256, 64, 128),
                error=0.25,
                track=(TrackElement(2, index),),
            )
            for index in range(1_024)
        )
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            sparse = root / "sparse"
            images_root = root / "images"
            write_layout(sparse, "BINARY", points3d=points)
            write_image_files(images_root, sample_images())
            job = ImportStagingPreparationJob(
                sparse,
                images_root,
                input_format="BINARY",
            )
            snapshots = [job.progress]
            while not job.done:
                snapshots.append(job.advance(work_budget_ms=0.001))

        fractions = [snapshot.overall_fraction for snapshot in snapshots]
        phases = {snapshot.phase for snapshot in snapshots}
        point_updates = [
            snapshot
            for snapshot in snapshots
            if snapshot.phase is ImportPreparationPhase.READ_POINTS
            and 0 < snapshot.completed_units < snapshot.total_units
        ]
        self.assertEqual(sorted(fractions), fractions)
        self.assertEqual(1.0, fractions[-1])
        self.assertGreaterEqual(len(point_updates), 3)
        self.assertTrue(
            {
                ImportPreparationPhase.READ_CAMERAS,
                ImportPreparationPhase.READ_IMAGES,
                ImportPreparationPhase.READ_POINTS,
                ImportPreparationPhase.VALIDATE_CAMERAS,
                ImportPreparationPhase.VALIDATE_POINTS,
                ImportPreparationPhase.VALIDATE_IMAGE_PATHS,
                ImportPreparationPhase.FINGERPRINT,
                ImportPreparationPhase.BUILD_RESULT,
                ImportPreparationPhase.DONE,
            }.issubset(phases)
        )
        self.assertEqual(1_024, len(job.result.points))

    def test_revalidation_reports_the_same_detailed_monotonic_progress(self):
        """Commit 직전 재검증도 파일과 레코드 내부 진행률을 유지하는지 확인합니다."""

        points = tuple(
            Point3DRecord(
                point3d_id=index + 1,
                xyz=(float(index), 1.0, 2.0),
                rgb=(32, 64, 128),
                error=0.5,
                track=(TrackElement(2, index),),
            )
            for index in range(768)
        )
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            sparse = root / "sparse"
            images_root = root / "images"
            write_layout(sparse, "BINARY", points3d=points)
            write_image_files(images_root, sample_images())
            staging = prepare_import_staging(
                sparse,
                images_root,
                input_format="BINARY",
            )
            job = ImportStagingRevalidationJob(staging)
            snapshots = [job.progress]
            while not job.done:
                snapshots.append(job.advance(work_budget_ms=0.001))

        fractions = [snapshot.overall_fraction for snapshot in snapshots]
        point_updates = [
            snapshot
            for snapshot in snapshots
            if snapshot.phase is ImportPreparationPhase.READ_POINTS
            and 0 < snapshot.completed_units < snapshot.total_units
        ]
        self.assertEqual(sorted(fractions), fractions)
        self.assertEqual(1.0, fractions[-1])
        self.assertGreaterEqual(len(point_updates), 2)


if __name__ == "__main__":
    unittest.main()
