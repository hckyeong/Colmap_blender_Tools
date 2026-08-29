from __future__ import annotations

import tempfile
import unittest
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

from tools.modifier.core.import_job import ImportLoadJob
from tools.modifier.core.import_types import (
    ImportPhase,
    ImportPlan,
    ImportProgressSnapshot,
)
from tools.modifier.core.loader import load_reconstruction
from tests.modifier.fixture_utils import (
    make_legacy_binary,
    make_legacy_text,
    make_modern_binary,
    make_modern_text,
)


class ImportProgressContractTests(unittest.TestCase):
    def test_snapshot_is_frozen_and_rejects_invalid_values(self):
        snapshot = ImportProgressSnapshot(
            phase=ImportPhase.CREATED,
            overall_fraction=0.0,
            phase_fraction=0.0,
            completed_units=0,
            total_units=0,
            message="준비",
            elapsed_seconds=0.0,
            can_cancel=False,
        )
        with self.assertRaises(FrozenInstanceError):
            snapshot.message = "변경"  # type: ignore[misc]

        for changes in (
            {"overall_fraction": -0.1},
            {"overall_fraction": 1.1},
            {"phase_fraction": -0.1},
            {"phase_fraction": 1.1},
            {"completed_units": -1},
            {"total_units": -1},
            {"completed_units": 2, "total_units": 1},
            {"message": ""},
            {"elapsed_seconds": -1.0},
            {"can_cancel": 0},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                replace(snapshot, **changes)

    def test_plan_normalizes_paths_and_validates_format(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan = ImportPlan(
                dataset_root=root,
                sparse_directory="",
                images_directory=None,
                input_format="text",
            )
            self.assertEqual(plan.dataset_root, root.resolve())
            self.assertIsNone(plan.sparse_directory)
            self.assertEqual(plan.input_format, "TEXT")
            self.assertEqual(plan.camera_frustum_size, 1.0)

        with self.assertRaisesRegex(ValueError, "Input Format"):
            ImportPlan(dataset_root=".", input_format="INVALID")
        with self.assertRaisesRegex(ValueError, "Camera Frustum Size"):
            ImportPlan(dataset_root=".", camera_frustum_size=0.009)


class ImportLoadJobTests(unittest.TestCase):
    def _drain(
        self,
        job: ImportLoadJob,
        *,
        budget_ms: float = 0.001,
    ) -> list[ImportProgressSnapshot]:
        snapshots = [job.progress]
        while not job.done:
            snapshots.append(job.advance(budget_ms))
        return snapshots

    def test_job_progress_is_monotonic_and_visits_all_core_phases(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            make_legacy_text(root)
            job = ImportLoadJob(ImportPlan(dataset_root=root, input_format="AUTO"))
            snapshots = self._drain(job)

            fractions = [snapshot.overall_fraction for snapshot in snapshots]
            self.assertEqual(fractions, sorted(fractions))
            self.assertEqual(snapshots[-1].phase, ImportPhase.DONE)
            self.assertEqual(snapshots[-1].overall_fraction, 1.0)
            visited = {snapshot.phase for snapshot in snapshots}
            self.assertTrue(
                {
                    ImportPhase.DISCOVER,
                    ImportPhase.HASH_SOURCE_BEFORE,
                    ImportPhase.READ_MODEL,
                    ImportPhase.VALIDATE_MODEL,
                    ImportPhase.VALIDATE_IMAGES,
                    ImportPhase.HASH_SOURCE_AFTER,
                    ImportPhase.BUILD_RESULT,
                    ImportPhase.DONE,
                }.issubset(visited)
            )
            self.assertEqual(job.result.source.input_format, "text")
            self.assertEqual(
                job.resolved_sparse_directory,
                (root / "sparse" / "0").resolve(),
            )
            self.assertEqual(
                job.resolved_images_directory,
                (root / "images").resolve(),
            )

    def test_incremental_and_sync_loader_results_match_all_layouts(self):
        fixtures = (
            ("legacy_text", make_legacy_text, "TEXT"),
            ("legacy_binary", make_legacy_binary, "BINARY"),
            ("modern_text", make_modern_text, "TEXT"),
            ("modern_binary", make_modern_binary, "BINARY"),
        )
        for label, factory, input_format in fixtures:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                sparse, images = factory(root)
                dataset_uuid = f"fixture-{label}"
                expected = load_reconstruction(
                    root,
                    sparse,
                    images,
                    input_format,
                    dataset_uuid,
                )
                job = ImportLoadJob(
                    ImportPlan(
                        dataset_root=root,
                        sparse_directory=sparse,
                        images_directory=images,
                        input_format=input_format,
                        matching_dataset_uuid=dataset_uuid,
                        matching_model_directory=sparse,
                    )
                )
                self._drain(job)

                self.assertEqual(job.result, expected)
                self.assertEqual(
                    job.result.validation_token.cameras,
                    expected.validation_token.cameras,
                )
                self.assertEqual(
                    job.result.image_path_manifest,
                    expected.image_path_manifest,
                )

    def test_discovered_path_controls_existing_dataset_uuid_reuse(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sparse, _images = make_legacy_text(root)
            matching = ImportLoadJob(
                ImportPlan(
                    dataset_root=root,
                    matching_dataset_uuid="existing",
                    matching_model_directory=sparse,
                )
            )
            self._drain(matching)
            self.assertEqual(matching.result.source.dataset_uuid, "existing")

            different = ImportLoadJob(
                ImportPlan(
                    dataset_root=root,
                    matching_dataset_uuid="existing",
                    matching_model_directory=root / "sparse" / "1",
                )
            )
            self._drain(different)
            self.assertNotEqual(different.result.source.dataset_uuid, "existing")

    def test_nonstandard_image_candidates_are_validated_after_model_read(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sparse, images = make_legacy_text(root)
            model_directory = root / "model"
            sparse.rename(model_directory)
            valid_images = root / "captures" / "images"
            valid_images.parent.mkdir()
            images.rename(valid_images)
            invalid_images = root / "wrong" / "images"
            invalid_images.mkdir(parents=True)

            job = ImportLoadJob(ImportPlan(dataset_root=root, input_format="TEXT"))
            snapshots = self._drain(job)

            self.assertEqual(
                job.result.source.model_directory,
                model_directory.resolve(),
            )
            self.assertEqual(
                job.result.source.images_directory,
                valid_images.resolve(),
            )
            self.assertGreater(
                sum(
                    snapshot.phase is ImportPhase.VALIDATE_IMAGES
                    for snapshot in snapshots
                ),
                1,
            )

    def test_source_mutation_after_validation_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sparse, images = make_legacy_text(root)
            job = ImportLoadJob(
                ImportPlan(
                    dataset_root=root,
                    sparse_directory=sparse,
                    images_directory=images,
                    input_format="TEXT",
                )
            )
            while job.progress.phase is not ImportPhase.HASH_SOURCE_AFTER:
                job.advance(0.001)
            with (sparse / "cameras.txt").open("a", encoding="utf-8") as stream:
                stream.write("\n# source changed\n")

            with self.assertRaisesRegex(ValueError, "source 파일이 변경"):
                while not job.done:
                    job.advance(0.001)
            self.assertTrue(job.failed)

    def test_abort_closes_active_fingerprint_stream(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sparse, images = make_legacy_binary(root)
            # 첫 fingerprint tick 뒤에도 stream이 열린 상태를 확실히 만들기 위해
            # 유효성 검사 전에 읽는 source 파일 하나를 2 MiB 이상으로 확장합니다.
            with (sparse / "cameras.bin").open("ab") as stream:
                stream.write(b"\0" * (2 * 1024 * 1024))
            job = ImportLoadJob(
                ImportPlan(
                    dataset_root=root,
                    sparse_directory=sparse,
                    images_directory=images,
                    input_format="BINARY",
                )
            )
            while job.progress.phase is not ImportPhase.HASH_SOURCE_BEFORE:
                job.advance(0.001)
            job.advance(0.001)
            fingerprint_job = job._fingerprint_job
            self.assertIsNotNone(fingerprint_job)
            file_job = fingerprint_job._file_job
            self.assertIsNotNone(file_job)
            self.assertIsNotNone(file_job._stream)

            job.abort()

            self.assertTrue(job.failed)
            self.assertIsNone(file_job._stream)

    def test_failure_closes_active_parser(self):
        class FailingParser:
            def __init__(self):
                self.closed = False

            def advance(self, _work_budget_ms):
                raise ValueError("parser failure")

            def close(self):
                self.closed = True

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sparse, images = make_legacy_text(root)
            job = ImportLoadJob(
                ImportPlan(
                    dataset_root=root,
                    sparse_directory=sparse,
                    images_directory=images,
                    input_format="TEXT",
                )
            )
            while job.progress.phase is not ImportPhase.READ_MODEL:
                job.advance(0.001)
            failing = FailingParser()
            job._read_job = failing

            with self.assertRaisesRegex(ValueError, "parser failure"):
                job.advance(0.001)

            self.assertTrue(failing.closed)
            self.assertTrue(job.failed)

    def test_invalid_budget_does_not_start_work(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            make_legacy_text(root)
            job = ImportLoadJob(ImportPlan(dataset_root=root))
            for budget in (0.0, -1.0, float("nan"), True):
                with self.subTest(budget=budget), self.assertRaises(ValueError):
                    job.advance(budget)
            self.assertEqual(job.progress.phase, ImportPhase.CREATED)


if __name__ == "__main__":
    unittest.main()
