from __future__ import annotations

import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tests.modifier.fixture_utils import make_legacy_text
from tools.modifier.blender import runtime_state
from tools.modifier.core.edit_history import (
    image_operation_from_report,
    point_operation_from_report,
)
from tools.modifier.core.edit_replay import history_hash, replay_history
from tools.modifier.core.edits import serialize_history
from tools.modifier.core.loader import load_reconstruction
from tools.modifier.core.removal import (
    apply_image_observation_removal,
    apply_point_removal,
    plan_image_observation_removal,
    plan_point_removal,
)


class _FakeScene:
    """RuntimeRestoreJob에 필요한 최소 Scene pointer와 설정을 제공합니다."""

    def __init__(self, settings) -> None:
        """고정 포인터 대신 객체 identity를 사용하는 테스트 Scene을 만듭니다."""

        self.colmap_import = settings

    def as_pointer(self) -> int:
        """Runtime Registry key로 사용할 안정적인 객체 identity를 반환합니다."""

        return id(self)


def _operations_for(loaded, kind: str):
    """Fixture 기준선에서 Point 또는 Image 제거 이력 한 건을 생성합니다."""

    model, indexes = replay_history(
        loaded.reconstruction,
        (),
        loaded.source.dataset_uuid,
        loaded.source.source_fingerprint,
        validation_token=loaded.validation_token,
    )
    if kind == "POINT":
        plan = plan_point_removal(
            model,
            indexes,
            [next(iter(model.points3d))],
            source_revision=0,
        )
        _current, _indexes, report = apply_point_removal(model, plan, 0)
        operation = point_operation_from_report(
            report,
            loaded.source.dataset_uuid,
            loaded.source.source_fingerprint,
            0,
        )
    elif kind == "IMAGE":
        plan = plan_image_observation_removal(
            model,
            indexes,
            [next(iter(model.images))],
            source_revision=0,
        )
        _current, _indexes, report = apply_image_observation_removal(model, plan, 0)
        operation = image_operation_from_report(
            report,
            loaded.source.dataset_uuid,
            loaded.source.source_fingerprint,
            0,
        )
    else:
        return ()
    return (operation,)


def _scene_for(loaded, operations) -> _FakeScene:
    """전체 Import 결과와 저장 이력을 Runtime 복원용 Scene 설정으로 변환합니다."""

    source = loaded.source
    return _FakeScene(
        SimpleNamespace(
            dataset_uuid=source.dataset_uuid,
            edit_revision=len(operations),
            history_hash=history_hash(operations),
            source_fingerprint=source.source_fingerprint,
            active_dataset_root=str(source.dataset_root),
            active_model_directory=str(source.model_directory),
            active_images_directory=str(source.images_directory),
            active_input_format=source.input_format,
            schema_version=str(source.schema_version),
            edit_history_json=serialize_history(operations),
            domain_uuid="11111111-1111-4111-8111-111111111111",
            domain_generation=3,
            domain_saved_generation=3,
            domain_persistence_dirty=False,
            domain_restore_error="",
        )
    )


def _drain(job) -> None:
    """Blender Runtime 복원 Job을 작은 Tick으로 완료까지 실행합니다."""

    while not job.done:
        job.advance(0.001)


class RuntimeStateFastRestoreTests(unittest.TestCase):
    def tearDown(self) -> None:
        """각 테스트 뒤 공유 Runtime과 진행 Job Registry를 함께 비웁니다."""

        runtime_state.clear_runtime()

    def test_no_edit_point_removal_and_image_removal_match_full_replay(self):
        """무편집·Point 제거·Image 제거의 빠른 복원 결과가 전체 replay와 같습니다."""

        for kind in ("NONE", "POINT", "IMAGE"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                sparse, images = make_legacy_text(root)
                loaded = load_reconstruction(
                    root,
                    sparse,
                    images,
                    "TEXT",
                    str(uuid.uuid4()),
                )
                operations = _operations_for(loaded, kind)
                expected_model, expected_indexes = replay_history(
                    loaded.reconstruction,
                    operations,
                    loaded.source.dataset_uuid,
                    loaded.source.source_fingerprint,
                    validation_token=loaded.validation_token,
                )
                scene = _scene_for(loaded, operations)
                job = runtime_state.RuntimeRestoreJob(
                    scene,
                    runtime_state.runtime_restore_token(scene),
                )
                _drain(job)

                self.assertEqual(job.result.current_reconstruction, expected_model)
                self.assertEqual(job.result.indexes, expected_indexes)
                self.assertEqual(job.result.operations, operations)
                self.assertEqual(job.result.edit_revision, len(operations))
                self.assertEqual(
                    job.result.point_id_order,
                    tuple(sorted(expected_model.points3d)),
                )
                self.assertEqual(
                    job.result.image_path_manifest,
                    loaded.image_path_manifest,
                )
                runtime_state.clear_runtime()

    def test_source_mismatch_never_replays_history_or_installs_runtime(self):
        """source 불일치 실패는 저장 이력을 재생하거나 전역 Runtime을 설치하지 않습니다."""

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
            operations = _operations_for(loaded, "POINT")
            scene = _scene_for(loaded, operations)
            with (sparse / "cameras.txt").open("a", encoding="utf-8") as stream:
                stream.write("\n# changed\n")
            with (
                patch.object(
                    runtime_state,
                    "replay_operation_indexed",
                    side_effect=AssertionError("source 불일치 뒤 History를 재생했습니다."),
                ) as replay_call,
                patch.object(
                    runtime_state,
                    "install_state",
                    side_effect=AssertionError("source 불일치 뒤 Runtime을 설치했습니다."),
                ) as install_call,
            ):
                job = runtime_state.RuntimeRestoreJob(
                    scene,
                    runtime_state.runtime_restore_token(scene),
                )
                with self.assertRaisesRegex(ValueError, "마지막 Import 또는 Export"):
                    _drain(job)

            self.assertEqual(replay_call.call_count, 0)
            self.assertEqual(install_call.call_count, 0)
            self.assertIsNone(runtime_state.cached_runtime(loaded.source.dataset_uuid))

    def test_completed_cold_restore_makes_warm_reentry_a_no_read_cache_hit(self):
        """COLD 복원 완료 뒤 같은 Scene의 WARM 재진입은 원본을 다시 읽지 않습니다."""

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
            scene = _scene_for(loaded, ())
            cold_job = runtime_state.request_runtime_restore(scene)
            _drain(cold_job)
            self.assertTrue(runtime_state.cached_runtime_matches_scene(scene))

            with patch.object(
                runtime_state,
                "RuntimeSourceRestoreJob",
                side_effect=AssertionError("WARM 재진입이 원본 Restore Job을 만들었습니다."),
            ) as restore_class:
                warm_job = runtime_state.request_runtime_restore(scene)
            self.assertIs(warm_job, cold_job)
            self.assertTrue(warm_job.done)
            self.assertEqual(restore_class.call_count, 0)

    def test_runtime_progress_uses_fast_restore_stage_labels(self):
        """Runtime 전체 진행 문구가 빠른 복원·관계·이력·Point 순서를 구분합니다."""

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
            scene = _scene_for(loaded, _operations_for(loaded, "POINT"))
            job = runtime_state.RuntimeRestoreJob(
                scene,
                runtime_state.runtime_restore_token(scene),
            )
            messages = {job.progress.message}
            while not job.done:
                messages.add(job.advance(0.001).message)

            self.assertTrue(
                {
                    "저장된 원본 확인 중",
                    "COLMAP 데이터 읽기 중",
                    "이미지 경로 확인 중",
                    "편집 관계 준비 중",
                    "저장된 편집 내용 적용 중",
                    "Point3D 순서 준비 중",
                }.issubset(messages)
            )
            self.assertFalse(any("무결성 검사" in message for message in messages))


if __name__ == "__main__":
    unittest.main()
