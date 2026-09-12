"""데이터셋 변환과 삭제의 혼합 이력·영속 복원 계약을 검증합니다."""

import tempfile
import unittest
import uuid
from dataclasses import replace
from pathlib import Path

from tests.modifier.core.test_edit_journal import _model, _transition, DATASET_UUID, FINGERPRINT
from tests.modifier.core.test_runtime_state_fast_restore import _scene_for, _drain
from tests.modifier.fixture_utils import make_legacy_text
from tools.modifier.blender import runtime_state
from tools.modifier.core.dataset_transform import IDENTITY_TRANSFORM
from tools.modifier.core.dataset_transform import transformed_reconstruction
from tools.modifier.core.domain_persistence import make_domain_payload, encode_domain_payload, decode_domain_payload
from tools.modifier.core.edit_delta import EditStateToken
from tools.modifier.core.edit_history import transform_operation, dataset_transform_from_history, serialize_history, deserialize_history
from tools.modifier.core.edit_journal import EditJournal, create_transform_command
from tools.modifier.core.indexes import build_relationship_indexes
from tools.modifier.core.loader import load_reconstruction
from tools.modifier.core.model import LoadedReconstruction


TRANSLATION = ((1.0, 0.0, 0.0, 4.0), (0.0, 1.0, 0.0, -2.0),
               (0.0, 0.0, 1.0, 3.0), (0.0, 0.0, 0.0, 1.0))


class TransformHistoryTests(unittest.TestCase):
    """행렬만 보관하는 명령이 기존 삭제 명령과 같은 경계를 따르는지 확인합니다."""

    def tearDown(self):
        """각 검증 뒤 런타임 도메인과 복원 작업을 정리합니다."""
        runtime_state.clear_runtime()

    def test_mixed_remove_transform_remove_undo_redo_reset(self):
        """삭제·변환·삭제를 모든 방향으로 이동해 형상과 누적 행렬을 확인합니다."""
        model = _model()
        indexes = build_relationship_indexes(model)
        journal = EditJournal(DATASET_UUID, FINGERPRINT)
        model, indexes, _plan, _report, command = _transition(model, indexes, 100, 0)
        journal.push(command)
        points = model.points3d
        record = model.points3d[200]
        operation = transform_operation(IDENTITY_TRANSFORM, TRANSLATION, DATASET_UUID, FINGERPRINT, 1)
        journal.push(create_transform_command(operation))
        self.assertIs(model.points3d, points)
        self.assertIs(model.points3d[200], record)
        model, indexes, _plan, _report, command = _transition(model, indexes, 200, 2)
        journal.push(command)
        token = EditStateToken(DATASET_UUID, FINGERPRINT, 3, 3)
        token = journal.undo(model, indexes, token)
        self.assertEqual(set(model.points3d), {200, 300})
        self.assertEqual(dataset_transform_from_history(journal.history), TRANSLATION)
        token = journal.undo(model, indexes, token)
        self.assertEqual(dataset_transform_from_history(journal.history), IDENTITY_TRANSFORM)
        token = journal.redo(model, indexes, token)
        token = journal.redo(model, indexes, token)
        token = journal.reset(model, indexes, token)
        self.assertEqual(set(model.points3d), {100, 200, 300})
        self.assertEqual(dataset_transform_from_history(journal.history), IDENTITY_TRANSFORM)
        token = journal.undo(model, indexes, token)
        self.assertEqual(set(model.points3d), {300})
        self.assertEqual(dataset_transform_from_history(journal.history), TRANSLATION)
        token = journal.redo(model, indexes, token)
        self.assertEqual(token.edit_revision, 0)
        self.assertEqual(set(model.points3d), {100, 200, 300})

    def test_persistence_and_both_restore_paths(self):
        """확정 변환을 저장하고 동기·증분 복원 뒤 전용 실행 취소를 확인합니다."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sparse, images = make_legacy_text(root)
            loaded = load_reconstruction(root, sparse, images, "TEXT", str(uuid.uuid4()))
            operation = transform_operation(IDENTITY_TRANSFORM, TRANSLATION,
                                            loaded.source.dataset_uuid,
                                            loaded.source.source_fingerprint, 0)
            operations = (operation,)
            payload = make_domain_payload(loaded.source, operations, domain_uuid=str(uuid.uuid4()), domain_generation=1)
            self.assertEqual(decode_domain_payload(encode_domain_payload(payload)), payload)
            self.assertEqual(deserialize_history(serialize_history(operations)), operations)
            state, journal = runtime_state._make_state_and_journal(loaded, operations)
            self.assertEqual(state.dataset_transform, TRANSLATION)
            self.assertIs(state.current_reconstruction, loaded.reconstruction)
            self.assertEqual(journal.undo_count, 1)
            scene = _scene_for(loaded, operations)
            job = runtime_state.RuntimeRestoreJob(scene, runtime_state.runtime_restore_token(scene))
            _drain(job)
            self.assertEqual(job.result.dataset_transform, TRANSLATION)
            self.assertEqual(job._journal.undo_count, 1)
            token = EditStateToken(state.dataset_uuid, state.source.source_fingerprint, 1, 1)
            journal.undo(state.current_reconstruction, state.indexes, token)
            restored = runtime_state.state_with_current(state, state.current_reconstruction, state.indexes, journal.history)
            self.assertEqual(restored.dataset_transform, IDENTITY_TRANSFORM)

    def test_transform_chain_rejects_wrong_before(self):
        """유효한 개별 행렬이어도 이력 연결이 끊기면 추가를 거부합니다."""
        operation = transform_operation(IDENTITY_TRANSFORM, TRANSLATION, DATASET_UUID, FINGERPRINT, 0)
        journal = EditJournal(DATASET_UUID, FINGERPRINT)
        journal.push(create_transform_command(operation))
        disconnected = replace(operation, previous_revision=1, resulting_revision=2)
        with self.assertRaises(ValueError):
            journal.push(create_transform_command(disconnected))
        self.assertEqual(journal.undo_count, 1)

    def test_export_rebase_and_cache_fallback(self):
        """캐시 설치가 변환 명령을 캡처하고 출력 성공은 변환된 새 기준선을 채택합니다."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sparse, images = make_legacy_text(root)
            loaded = load_reconstruction(root, sparse, images, "TEXT", str(uuid.uuid4()))
            state, journal = runtime_state._make_state_and_journal(loaded, ())
            runtime_state.install_state(state, journal=journal)
            operation = transform_operation(IDENTITY_TRANSFORM, TRANSLATION,
                                            state.dataset_uuid, state.source.source_fingerprint, 0)
            changed = runtime_state.state_with_current(state, state.current_reconstruction, state.indexes, (operation,))
            domain = runtime_state.install_state(changed)
            self.assertEqual(domain.journal.undo_count, 1)
            self.assertIs(changed.point_id_order, state.point_id_order)
            transformed = transformed_reconstruction(changed.current_reconstruction, TRANSLATION)
            exported = LoadedReconstruction(transformed, loaded.source, ())
            rebased = runtime_state.state_from_successful_export(changed, exported)
            self.assertIs(rebased.current_reconstruction, transformed)
            self.assertIs(rebased.baseline_reconstruction, transformed)
            self.assertIs(rebased.indexes, changed.indexes)
            self.assertEqual(rebased.operations, ())
            self.assertEqual(rebased.dataset_transform, IDENTITY_TRANSFORM)
            domain.dataset_state = replace(changed, dataset_transform=IDENTITY_TRANSFORM)
            with self.assertRaises(ValueError):
                domain.assert_integrity()
