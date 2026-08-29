from __future__ import annotations

import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path

from tools.modifier.core.export_job import (
    ExportTransactionJob,
    _ReconstructionEqualityJob,
)
from tools.modifier.core.export_progress import ExportPhase, ExportProgressSnapshot
from tools.modifier.core.export_transaction import execute_export
from tools.modifier.core.export_types import ExportTransactionError
from tools.modifier.core.export_validation import validate_export_request
from tools.modifier.core.loader import load_reconstruction
from tools.modifier.core.model import (
    CameraRecord,
    ImageRecord,
    Point2DRecord,
    Reconstruction,
)
from tools.modifier.core.export_staging_jobs import (
    IncrementalWriteValidationJob,
    StagingReadJob,
    StagingWriteJob,
)
from tools.modifier.core.export_integrity_job import ReconstructionValidationJob
from tools.modifier.core.integrity import validate_reconstruction
from tools.modifier.core.text_io import read_model_text
from tests.modifier.fixture_utils import make_legacy_text, make_modern_binary


def _remove_points(model: Reconstruction) -> Reconstruction:
    removed_ids = set(model.points3d)
    images: dict[int, ImageRecord] = {}
    for image_id, image in model.images.items():
        images[image_id] = replace(
            image,
            points2d=tuple(
                Point2DRecord(
                    point.x,
                    point.y,
                    None if point.point3d_id in removed_ids else point.point3d_id,
                )
                for point in image.points2d
            ),
        )
    return Reconstruction(
        cameras=dict(model.cameras),
        images=images,
        points3d={},
        rigs=dict(model.rigs),
        frames=dict(model.frames),
    )


def _loaded_text(root: Path):
    model_directory, images_directory = make_legacy_text(root)
    return load_reconstruction(root, model_directory, images_directory, "TEXT")


class ExportProgressValueTests(unittest.TestCase):
    def test_snapshot_is_frozen_and_rejects_invalid_values(self):
        snapshot = ExportProgressSnapshot(
            phase=ExportPhase.CREATED,
            overall_fraction=0.0,
            phase_fraction=0.0,
            completed_units=0,
            total_units=0,
            message="Export 준비",
            elapsed_seconds=0.0,
            can_cancel=False,
        )
        self.assertEqual(snapshot.phase, ExportPhase.CREATED)
        with self.assertRaises((AttributeError, TypeError)):
            snapshot.message = "변경"  # type: ignore[misc]

        invalid_cases = (
            {"overall_fraction": -0.1},
            {"overall_fraction": 1.1},
            {"phase_fraction": -0.1},
            {"phase_fraction": 1.1},
            {"completed_units": -1},
            {"total_units": -1},
            {"completed_units": 2, "total_units": 1},
            {"elapsed_seconds": -0.1},
        )
        for changes in invalid_cases:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                replace(snapshot, **changes)


class ExportTransactionJobTests(unittest.TestCase):
    def test_incremental_job_progress_is_monotonic_and_matches_sync_output(self):
        with tempfile.TemporaryDirectory() as incremental_directory:
            incremental_root = Path(incremental_directory)
            loaded = _loaded_text(incremental_root)
            edited = _remove_points(loaded.reconstruction)
            plan = validate_export_request(loaded.source, edited, backup_enabled=False)
            job = ExportTransactionJob(plan, edited)

            self.assertEqual(job.progress.phase, ExportPhase.CREATED)
            self.assertFalse(job.done)
            self.assertFalse(job.failed)
            with self.assertRaises(RuntimeError):
                _ = job.result

            snapshots: list[ExportProgressSnapshot] = [job.progress]
            while not job.done:
                snapshots.append(job.advance(work_budget_ms=25.0))

            fractions = [snapshot.overall_fraction for snapshot in snapshots]
            self.assertEqual(fractions, sorted(fractions))
            self.assertEqual(snapshots[-1].phase, ExportPhase.DONE)
            self.assertEqual(snapshots[-1].overall_fraction, 1.0)
            self.assertEqual(read_model_text(loaded.source.model_directory, False), edited)
            self.assertIs(job.result.loaded.reconstruction, edited)
            self.assertIsNone(job.error)
            with self.assertRaises(RuntimeError):
                job.advance()

        with tempfile.TemporaryDirectory() as sync_directory:
            sync_root = Path(sync_directory)
            sync_loaded = _loaded_text(sync_root)
            sync_edited = _remove_points(sync_loaded.reconstruction)
            sync_result = execute_export(
                validate_export_request(
                    sync_loaded.source,
                    sync_edited,
                    backup_enabled=False,
                ),
                sync_edited,
            )
            self.assertEqual(job.result.loaded.reconstruction, sync_result.loaded.reconstruction)
            self.assertEqual(
                tuple(path.name for path in job.result.replaced_files),
                tuple(path.name for path in sync_result.replaced_files),
            )

    def test_binary_modern_backup_job_completes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model_directory, images_directory = make_modern_binary(root)
            loaded = load_reconstruction(
                root,
                model_directory,
                images_directory,
                "BINARY",
            )
            edited = _remove_points(loaded.reconstruction)
            job = ExportTransactionJob(
                validate_export_request(loaded.source, edited, backup_enabled=True),
                edited,
            )

            while not job.done:
                job.advance()

            self.assertIsNotNone(job.result.backup_snapshot)
            self.assertEqual(job.progress.phase, ExportPhase.DONE)

    def test_failure_is_raised_and_preserved_then_advance_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            loaded = _loaded_text(root)
            edited = _remove_points(loaded.reconstruction)
            plan = validate_export_request(loaded.source, edited, backup_enabled=False)
            stale_plan = replace(
                plan,
                runtime_token=replace(
                    plan.runtime_token,
                    edit_revision=1,
                    index_revision=0,
                ),
            )
            job = ExportTransactionJob(stale_plan, edited)

            # CREATED에서 PREPARE로 전환한 뒤 실제 사전검사에서 실패합니다.
            self.assertEqual(job.advance().phase, ExportPhase.PREPARE)
            with self.assertRaises(ExportTransactionError) as caught:
                job.advance()

            self.assertTrue(job.failed)
            self.assertFalse(job.done)
            self.assertIs(job.error, caught.exception)
            self.assertEqual(job.progress.phase, ExportPhase.FAILED)
            self.assertEqual(caught.exception.stage, "validation")
            with self.assertRaises(RuntimeError):
                job.advance()
            with self.assertRaises(RuntimeError):
                _ = job.result

    def test_invalid_budget_is_rejected_without_advancing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            loaded = _loaded_text(root)
            job = ExportTransactionJob(
                validate_export_request(
                    loaded.source,
                    loaded.reconstruction,
                    backup_enabled=False,
                ),
                loaded.reconstruction,
            )
            for value in (0.0, -1.0, float("inf"), float("nan")):
                with self.subTest(value=value), self.assertRaises(ValueError):
                    job.advance(value)
            self.assertEqual(job.progress.phase, ExportPhase.CREATED)

    def test_abort_before_commit_cleans_transaction_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            loaded = _loaded_text(root)
            job = ExportTransactionJob(
                validate_export_request(
                    loaded.source,
                    loaded.reconstruction,
                    backup_enabled=False,
                ),
                loaded.reconstruction,
            )
            job.advance()  # PREPARE 진입
            job.advance()  # transaction 경로 생성
            self.assertTrue(
                tuple(root.glob(".blender_colmap_export_*")),
                "강제 종료 검증 전에 transaction 경로가 생성되어야 합니다.",
            )

            job.abort()
            job.abort()

            self.assertTrue(job.failed)
            self.assertIsNotNone(job.error)
            self.assertEqual(job.error.stage, "abort")
            self.assertFalse(tuple(root.glob(".blender_colmap_export_*")))
            with self.assertRaises(RuntimeError):
                job.advance()

    def test_abort_after_commit_rolls_back_original_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            loaded = _loaded_text(root)
            original = loaded.reconstruction
            edited = _remove_points(original)
            job = ExportTransactionJob(
                validate_export_request(
                    loaded.source,
                    edited,
                    backup_enabled=False,
                ),
                edited,
            )

            while job.progress.phase is not ExportPhase.BUILD_RESULT:
                job.advance()
            self.assertEqual(
                read_model_text(loaded.source.model_directory, False),
                edited,
            )

            job.abort()

            self.assertTrue(job.failed)
            self.assertFalse(job.error.source_changed)
            self.assertTrue(job.error.rollback_attempted)
            self.assertTrue(job.error.rollback_succeeded)
            self.assertEqual(
                read_model_text(loaded.source.model_directory, False),
                original,
            )

    def test_runtime_count_change_before_commit_is_rejected_before_replace(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            loaded = _loaded_text(root)
            edited = _remove_points(loaded.reconstruction)
            job = ExportTransactionJob(
                validate_export_request(
                    loaded.source,
                    edited,
                    backup_enabled=False,
                ),
                edited,
            )
            while job.progress.phase is not ExportPhase.COMMIT:
                job.advance(2.0)
            original_source = read_model_text(
                loaded.source.model_directory,
                modern=False,
            )
            first_image = next(iter(edited.images.values()))
            edited.images[99] = replace(first_image, image_id=99)

            with self.assertRaises(ExportTransactionError) as caught:
                job.advance(2.0)

            self.assertEqual(caught.exception.stage, "commit")
            self.assertFalse(caught.exception.replaced_files)
            self.assertFalse(caught.exception.source_changed)
            self.assertEqual(
                read_model_text(loaded.source.model_directory, modern=False),
                original_source,
            )


class ExportChunkJobTests(unittest.TestCase):
    @staticmethod
    def _large_observation_model(count: int = 100_000) -> Reconstruction:
        camera = CameraRecord(
            camera_id=1,
            model_id=1,
            model_name="PINHOLE",
            width=1920,
            height=1080,
            params=(1000.0, 1000.0, 960.0, 540.0),
        )
        image = ImageRecord(
            image_id=1,
            qvec=(1.0, 0.0, 0.0, 0.0),
            tvec=(0.0, 0.0, 0.0),
            camera_id=1,
            name="large.jpg",
            points2d=tuple(
                Point2DRecord(float(index), float(index), None)
                for index in range(count)
            ),
        )
        return Reconstruction(cameras={1: camera}, images={1: image})

    def test_binary_writer_and_parser_yield_inside_large_point2d_block(self):
        model = self._large_observation_model()
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            writer = StagingWriteJob(
                output,
                model,
                input_format="binary",
                modern=False,
            )
            durations: list[float] = []
            calls = 0
            while not writer.done:
                started = time.perf_counter()
                writer.advance(2.0)
                durations.append(time.perf_counter() - started)
                calls += 1

            self.assertGreater(calls, 5)
            self.assertLess(max(durations), 0.5)

            parser = StagingReadJob(
                output,
                input_format="binary",
                modern=False,
            )
            parser_calls = 0
            parser_durations: list[float] = []
            while not parser.done:
                started = time.perf_counter()
                parser.advance(2.0)
                parser_durations.append(time.perf_counter() - started)
                parser_calls += 1

            self.assertGreater(parser_calls, 5)
            self.assertLess(max(parser_durations), 0.5)
            self.assertEqual(parser.result, model)

    def test_incremental_integrity_matches_existing_validator(self):
        model = self._large_observation_model(count=10_000)
        expected = validate_reconstruction(model)
        job = ReconstructionValidationJob(model)
        calls = 0
        while not job.done:
            job.advance(0.001)
            calls += 1

        self.assertGreater(calls, 1)
        self.assertEqual(job.result, expected)

    def test_text_writer_yields_inside_point2d_line_and_parser_yields_by_record(self):
        model = self._large_observation_model(count=50_000)
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            writer = StagingWriteJob(
                output,
                model,
                input_format="text",
                modern=False,
            )
            writer_calls = 0
            while not writer.done:
                writer.advance(2.0)
                writer_calls += 1
            self.assertGreater(writer_calls, 5)

            parser = StagingReadJob(
                output,
                input_format="text",
                modern=False,
            )
            parser_calls = 0
            while not parser.done:
                parser.advance(2.0)
                parser_calls += 1
            self.assertGreaterEqual(parser_calls, 3)
            self.assertEqual(parser.result, model)

    def test_writer_field_preflight_obeys_budget_on_large_point2d_block(self):
        model = self._large_observation_model()
        job = IncrementalWriteValidationJob(model, modern=False)
        durations: list[float] = []
        calls = 0
        while not job.done:
            started = time.perf_counter()
            job.advance(2.0)
            durations.append(time.perf_counter() - started)
            calls += 1

        self.assertGreater(calls, 5)
        self.assertLess(max(durations), 0.05)

    def test_equality_yields_inside_point2d_and_detects_late_mismatch(self):
        staging = self._large_observation_model()
        expected = self._large_observation_model()
        image = expected.images[1]
        changed_points = list(image.points2d)
        changed_points[-1] = replace(changed_points[-1], x=-1.0)
        expected.images[1] = replace(image, points2d=tuple(changed_points))
        job = _ReconstructionEqualityJob(staging, expected)
        calls = 0

        with self.assertRaises(ValueError):
            while not job.done:
                job.advance(0.001)
                calls += 1

        self.assertGreater(calls, 1)


if __name__ == "__main__":
    unittest.main()
