from __future__ import annotations

import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from tests.modifier.fixture_utils import (
    make_legacy_binary,
    make_legacy_text,
    make_modern_binary,
    make_modern_text,
)
from tools.modifier.core.fingerprint import SourceFingerprintJob, source_fingerprint
from tools.modifier.core.import_job import ImportLoadJob, SCHEMA_VERSION
from tools.modifier.core.import_types import ImportPlan
from tools.modifier.core.integrity_job import ReconstructionValidationJob
from tools.modifier.core.layout import resolve_layout
from tools.modifier.core.loader import load_reconstruction
from tools.modifier.core.reconstruction_read_job import ReconstructionReadJob
from tools.modifier.core.runtime_restore_job import (
    RuntimeSourceRestoreJob,
    RuntimeSourceRestorePhase,
    RuntimeSourceRestorePlan,
)


def _plan_from_loaded(loaded) -> RuntimeSourceRestorePlan:
    """전체 Import 결과에서 동일 source 빠른 복원 plan을 구성합니다."""

    source = loaded.source
    return RuntimeSourceRestorePlan(
        dataset_root=source.dataset_root,
        model_directory=source.model_directory,
        images_directory=source.images_directory,
        input_format=source.input_format,
        expected_dataset_uuid=source.dataset_uuid,
        expected_source_fingerprint=source.source_fingerprint,
        expected_schema_version=source.schema_version,
    )


def _drain(job, budget_ms: float = 0.001):
    """증분 Job을 완료까지 실행하고 모든 진행 snapshot을 반환합니다."""

    snapshots = [job.progress]
    while not job.done:
        snapshots.append(job.advance(budget_ms))
    return snapshots


class RuntimeSourceRestoreJobTests(unittest.TestCase):
    def test_explicit_import_still_runs_full_reconstruction_validation(self):
        """명시적 Import가 기존 Reconstruction 전체 검사를 계속 실행합니다."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            make_legacy_text(root)
            with patch(
                "tools.modifier.core.import_job.ReconstructionValidationJob",
                side_effect=ReconstructionValidationJob,
            ) as validation_class:
                _drain(ImportLoadJob(ImportPlan(dataset_root=root)))
            self.assertEqual(validation_class.call_count, 1)

    def test_fast_restore_hashes_once_and_skips_reconstruction_validation(self):
        """동일 fingerprint 복원이 전체 해시 1회와 무결성 검사 0회를 보장합니다."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sparse, images = make_legacy_text(root)
            loaded = load_reconstruction(
                root,
                sparse,
                images,
                "TEXT",
                str(uuid.uuid4()),
            )
            from tools.modifier.core import runtime_restore_job as restore_module

            original_fingerprint = restore_module.source_fingerprint
            with (
                patch.object(
                    restore_module,
                    "source_fingerprint",
                    wraps=original_fingerprint,
                ) as fingerprint_call,
                patch(
                    "tools.modifier.core.integrity_job.ReconstructionValidationJob",
                    side_effect=AssertionError("빠른 복원이 전체 무결성 검사를 실행했습니다."),
                ) as validation_class,
            ):
                job = RuntimeSourceRestoreJob(_plan_from_loaded(loaded))
                snapshots = _drain(job)

            self.assertEqual(fingerprint_call.call_count, 1)
            self.assertEqual(validation_class.call_count, 0)
            self.assertFalse(
                any("Reconstruction 무결성 검사" in item.message for item in snapshots)
            )

    def test_fast_restore_matches_full_load_for_all_formats_and_layouts(self):
        """TXT·BIN과 legacy·modern에서 전체 Import와 값·Manifest가 같습니다."""

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
                loaded = load_reconstruction(
                    root,
                    sparse,
                    images,
                    input_format,
                    str(uuid.uuid4()),
                )
                job = RuntimeSourceRestoreJob(_plan_from_loaded(loaded))
                _drain(job)
                restored = job.result

                self.assertEqual(restored.reconstruction, loaded.reconstruction)
                self.assertEqual(restored.source, loaded.source)
                self.assertEqual(restored.image_path_manifest, loaded.image_path_manifest)
                self.assertIsNotNone(restored.validation_token)
                self.assertIs(
                    restored.validation_token.reconstruction,
                    restored.reconstruction,
                )
                for name in ("cameras", "images", "points3d", "rigs", "frames"):
                    self.assertEqual(
                        getattr(restored.validation_token, name),
                        getattr(loaded.validation_token, name),
                    )

    def test_fingerprint_result_contract_and_signature_snapshot_are_available(self):
        """기존 동기 문자열 계약을 유지하며 증분 Job은 파일 서명을 제공합니다."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sparse, _images = make_modern_text(root)
            layout = resolve_layout(sparse, "TEXT")
            expected = source_fingerprint(layout)
            self.assertIsInstance(expected, str)

            job = source_fingerprint(layout, incremental=True)
            self.assertIsInstance(job, SourceFingerprintJob)
            while not job.done:
                job.advance(0.001)
            self.assertEqual(job.result, expected)
            self.assertEqual(
                tuple(item.relative_path for item in job.signature_snapshot),
                tuple(path.name for path in layout.selected_files),
            )
            self.assertTrue(all(item.size >= 0 for item in job.signature_snapshot))

    def test_fingerprint_mismatch_fails_before_parse(self):
        """저장 fingerprint 불일치는 Reconstruction Parse 전에 재Import를 요구합니다."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sparse, images = make_legacy_text(root)
            loaded = load_reconstruction(
                root,
                sparse,
                images,
                "TEXT",
                str(uuid.uuid4()),
            )
            with (sparse / "cameras.txt").open("a", encoding="utf-8") as stream:
                stream.write("\n# changed\n")
            with patch(
                "tools.modifier.core.runtime_restore_job.ReconstructionReadJob",
                side_effect=AssertionError("fingerprint 불일치 뒤 Parse를 시작했습니다."),
            ) as read_job_class:
                job = RuntimeSourceRestoreJob(_plan_from_loaded(loaded))
                with self.assertRaisesRegex(
                    ValueError,
                    "마지막 Import 또는 Export 이후 변경",
                ):
                    _drain(job)
            self.assertEqual(read_job_class.call_count, 0)
            self.assertTrue(job.failed)

    def test_signature_change_during_parse_is_detected_without_second_hash(self):
        """첫 해시 뒤 Parse 중 파일 서명이 바뀌면 결과 구성 전에 실패합니다."""

        class MutatingReadJob:
            """Parser 완료 직후 source를 변경해 Parse 경합을 재현합니다."""

            def __init__(self, model_directory, *, input_format, modern):
                """실제 Parser와 변경할 source 파일을 함께 보관합니다."""

                self._inner = ReconstructionReadJob(
                    model_directory,
                    input_format=input_format,
                    modern=modern,
                )
                self._path = Path(model_directory) / "cameras.txt"
                self._mutated = False

            @property
            def done(self):
                """내부 Parser의 완료 상태를 전달합니다."""

                return self._inner.done

            @property
            def result(self):
                """내부 Parser의 Reconstruction 결과를 전달합니다."""

                return self._inner.result

            @property
            def completed_units(self):
                """내부 Parser의 완료 작업량을 전달합니다."""

                return self._inner.completed_units

            @property
            def total_units(self):
                """내부 Parser의 전체 작업량을 전달합니다."""

                return self._inner.total_units

            def advance(self, work_budget_ms):
                """내부 Parse 완료 직후 source 파일 서명을 한 번 변경합니다."""

                self._inner.advance(work_budget_ms)
                if self._inner.done and not self._mutated:
                    with self._path.open("a", encoding="utf-8") as stream:
                        stream.write("\n# changed during parse\n")
                    self._mutated = True

            def close(self):
                """내부 Parser의 열린 stream을 닫습니다."""

                self._inner.close()

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sparse, images = make_legacy_text(root)
            loaded = load_reconstruction(
                root,
                sparse,
                images,
                "TEXT",
                str(uuid.uuid4()),
            )
            with patch(
                "tools.modifier.core.runtime_restore_job.ReconstructionReadJob",
                MutatingReadJob,
            ):
                job = RuntimeSourceRestoreJob(_plan_from_loaded(loaded))
                with self.assertRaisesRegex(ValueError, "읽는 동안 원본 파일이 변경"):
                    _drain(job)
            self.assertTrue(job.failed)

    def test_missing_fingerprint_and_stale_schema_require_explicit_reimport(self):
        """신뢰 token이 없거나 구 schema이면 자동 보정 없이 생성 단계에서 거부합니다."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sparse, images = make_legacy_text(root)
            with self.assertRaisesRegex(ValueError, "fingerprint"):
                RuntimeSourceRestorePlan(
                    dataset_root=root,
                    model_directory=sparse,
                    images_directory=images,
                    input_format="TEXT",
                    expected_dataset_uuid=str(uuid.uuid4()),
                    expected_source_fingerprint="",
                    expected_schema_version=SCHEMA_VERSION,
                )
            with self.assertRaisesRegex(ValueError, "현재 신뢰 기준"):
                RuntimeSourceRestoreJob(
                    RuntimeSourceRestorePlan(
                        dataset_root=root,
                        model_directory=sparse,
                        images_directory=images,
                        input_format="TEXT",
                        expected_dataset_uuid=str(uuid.uuid4()),
                        expected_source_fingerprint="a" * 64,
                        expected_schema_version=SCHEMA_VERSION + 1,
                    )
                )

    def test_progress_uses_restore_specific_messages(self):
        """빠른 복원은 Import 무결성 검사와 구분되는 사용자 진행 문구만 표시합니다."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sparse, images = make_legacy_text(root)
            loaded = load_reconstruction(
                root,
                sparse,
                images,
                "TEXT",
                str(uuid.uuid4()),
            )
            snapshots = _drain(RuntimeSourceRestoreJob(_plan_from_loaded(loaded)))
            messages = {item.message for item in snapshots}
            self.assertIn("저장된 원본 확인 중", messages)
            self.assertIn("COLMAP 데이터 읽기 중", messages)
            self.assertIn("이미지 경로 확인 중", messages)
            self.assertEqual(snapshots[-1].phase, RuntimeSourceRestorePhase.DONE)
            self.assertFalse(any("무결성 검사" in message for message in messages))


if __name__ == "__main__":
    unittest.main()
