from __future__ import annotations

import copy
import unittest
from unittest.mock import patch

from tools.modifier.core.edit_delta import EditStateToken, apply_forward
from tools.modifier.core.edit_history import point_operation_from_report
from tools.modifier.core.edit_journal import (
    EditCommand,
    EditJournal,
    capture_edit_command,
)
from tools.modifier.core.indexes import RelationshipIndexes, build_relationship_indexes
from tools.modifier.core.model import (
    CameraRecord,
    ImageRecord,
    Point2DRecord,
    Point3DRecord,
    Reconstruction,
    TrackElement,
)
from tools.modifier.core.removal import apply_point_removal_indexed, plan_point_removal


DATASET_UUID = "12345678-1234-5678-1234-567812345678"
FINGERPRINT = "b" * 64


def _camera() -> CameraRecord:
    """Journal 테스트용 Camera 레코드를 생성합니다."""

    return CameraRecord(1, 0, "SIMPLE_PINHOLE", 100, 80, (80.0, 50.0, 40.0))


def _model() -> Reconstruction:
    """서로 독립적으로 분기 제거할 Point 세 개를 생성합니다."""

    point_ids = (100, 200, 300)
    return Reconstruction(
        cameras={1: _camera()},
        images={
            10: ImageRecord(
                10,
                (1.0, 0.0, 0.0, 0.0),
                (0.0, 0.0, 0.0),
                1,
                "a.jpg",
                tuple(
                    Point2DRecord(float(index), float(index + 1), point_id)
                    for index, point_id in enumerate(point_ids)
                ),
            )
        },
        points3d={
            point_id: Point3DRecord(
                point_id,
                (float(index), 0.0, 0.0),
                (index * 10, index * 20, index * 30),
                0.1,
                (TrackElement(10, index),),
            )
            for index, point_id in enumerate(point_ids)
        },
    )


def _transition(
    before: Reconstruction,
    before_indexes: RelationshipIndexes,
    point_id: int,
    revision: int,
) -> tuple[
    Reconstruction,
    RelationshipIndexes,
    object,
    object,
    EditCommand,
]:
    """Point 하나의 외부 forward 결과와 캡처한 command를 생성합니다."""

    plan = plan_point_removal(
        before,
        before_indexes,
        [point_id],
        source_revision=revision,
    )
    after, after_indexes, report = apply_point_removal_indexed(
        before,
        plan,
        current_revision=revision,
        indexes=before_indexes,
    )
    operation = point_operation_from_report(
        report,
        DATASET_UUID,
        FINGERPRINT,
        revision,
    )
    command = capture_edit_command(
        before,
        before_indexes,
        after,
        after_indexes,
        plan,
        operation,
    )
    return after, after_indexes, plan, operation, command


def _mapping_identities(
    model: Reconstruction,
    indexes: RelationshipIndexes,
) -> tuple[int, ...]:
    """Journal 적용 대상 9개 dict의 identity를 반환합니다."""

    return tuple(
        id(mapping)
        for mapping in (
            model.cameras,
            model.images,
            model.points3d,
            model.rigs,
            model.frames,
            indexes.point_to_images,
            indexes.image_to_points,
            indexes.camera_to_images,
            indexes.image_to_frame,
        )
    )


class EditJournalTests(unittest.TestCase):
    def test_reset_is_single_session_command_and_restores_individual_history(self):
        """Reset·Undo·Redo가 전체 재생 없이 같은 Session의 Command 경계를 복원합니다."""

        baseline = _model()
        baseline_indexes = build_relationship_indexes(baseline)
        after_one, indexes_one, _plan_one, operation_one, command_one = _transition(
            baseline,
            baseline_indexes,
            100,
            0,
        )
        after_two, indexes_two, _plan_two, operation_two, command_two = _transition(
            after_one,
            indexes_one,
            200,
            1,
        )
        expected_baseline = copy.deepcopy((baseline, baseline_indexes))
        expected_edited = copy.deepcopy((after_two, indexes_two))
        journal = EditJournal(DATASET_UUID, FINGERPRINT)
        journal.push(command_one)
        journal.push(command_two)
        token = EditStateToken(DATASET_UUID, FINGERPRINT, 2, 2)

        token = journal.reset(after_two, indexes_two, token)
        self.assertIsNotNone(token)
        self.assertEqual((after_two, indexes_two), expected_baseline)
        self.assertEqual(journal.history, ())
        self.assertEqual((journal.undo_count, journal.redo_count), (1, 0))

        token = journal.undo(after_two, indexes_two, token)
        self.assertEqual((after_two, indexes_two), expected_edited)
        self.assertEqual(journal.history, (operation_one, operation_two))
        self.assertEqual((journal.undo_count, journal.redo_count), (2, 1))

        token = journal.redo(after_two, indexes_two, token)
        self.assertEqual((after_two, indexes_two), expected_baseline)
        self.assertEqual(journal.history, ())
        self.assertEqual((journal.undo_count, journal.redo_count), (1, 0))

    def test_checkpoint_restores_stack_metadata_after_failed_outer_transaction(self):
        """외부 표시 Commit 실패를 가정해 Push 이전 Journal 경계를 복구합니다."""

        baseline = _model()
        baseline_indexes = build_relationship_indexes(baseline)
        _after, _indexes, _plan, _operation, command = _transition(
            baseline,
            baseline_indexes,
            100,
            0,
        )
        journal = EditJournal(DATASET_UUID, FINGERPRINT)
        checkpoint = journal.checkpoint()
        journal.push(command)
        journal.restore_checkpoint(checkpoint)
        self.assertEqual(journal.history, ())
        self.assertEqual(journal.revision, 0)
        self.assertFalse(journal.poll_undo())
        self.assertFalse(journal.poll_redo())

    def test_cold_append_then_warm_undo_redo_keeps_history_and_identity(self):
        """cold 재구축 뒤 warm undo·redo가 history·상태·dict identity를 동기화합니다."""

        baseline = _model()
        baseline_indexes = build_relationship_indexes(baseline)
        after_one, indexes_one, plan_one, operation_one, _command_one = _transition(
            baseline,
            baseline_indexes,
            100,
            0,
        )
        after_two, indexes_two, plan_two, operation_two, _command_two = _transition(
            after_one,
            indexes_one,
            200,
            1,
        )
        journal = EditJournal(DATASET_UUID, FINGERPRINT)
        journal.append_rebuilt(
            before_reconstruction=baseline,
            before_indexes=baseline_indexes,
            after_reconstruction=after_one,
            after_indexes=indexes_one,
            plan=plan_one,
            operation=operation_one,
        )
        journal.append_rebuilt(
            before_reconstruction=after_one,
            before_indexes=indexes_one,
            after_reconstruction=after_two,
            after_indexes=indexes_two,
            plan=plan_two,
            operation=operation_two,
        )
        expected_baseline = copy.deepcopy((baseline, baseline_indexes))
        expected_one = copy.deepcopy((after_one, indexes_one))
        expected_two = copy.deepcopy((after_two, indexes_two))
        identities = _mapping_identities(after_two, indexes_two)
        token = EditStateToken(DATASET_UUID, FINGERPRINT, 2, 2)

        with (
            patch(
                "tools.modifier.core.indexes.build_relationship_indexes",
                side_effect=AssertionError("warm undo에서 전체 인덱스 구축 금지"),
            ),
            patch(
                "tools.modifier.core.edit_replay.replay_history",
                side_effect=AssertionError("warm undo에서 전체 history replay 금지"),
            ),
        ):
            token = journal.undo(after_two, indexes_two, token)
            self.assertIsNotNone(token)
            self.assertEqual((after_two, indexes_two), expected_one)
            self.assertEqual(journal.history, (operation_one,))
            token = journal.undo(after_two, indexes_two, token)
            self.assertIsNotNone(token)
            self.assertEqual((after_two, indexes_two), expected_baseline)
            self.assertEqual(journal.history, ())
            token = journal.redo(after_two, indexes_two, token)
            self.assertIsNotNone(token)
            self.assertEqual((after_two, indexes_two), expected_one)
            token = journal.redo(after_two, indexes_two, token)
            self.assertIsNotNone(token)

        self.assertEqual((after_two, indexes_two), expected_two)
        self.assertEqual(journal.history, (operation_one, operation_two))
        self.assertEqual(_mapping_identities(after_two, indexes_two), identities)

    def test_new_branch_discards_redo_and_tracks_branch_history(self):
        """Undo 뒤 새 command를 push하면 이전 redo 분기를 폐기합니다."""

        baseline = _model()
        baseline_indexes = build_relationship_indexes(baseline)
        after_one, indexes_one, _plan_one, operation_one, command_one = _transition(
            baseline,
            baseline_indexes,
            100,
            0,
        )
        after_two, indexes_two, _plan_two, operation_two, command_two = _transition(
            after_one,
            indexes_one,
            200,
            1,
        )
        journal = EditJournal(DATASET_UUID, FINGERPRINT)
        journal.push(command_one)
        journal.push(command_two)
        token = journal.undo(
            after_two,
            indexes_two,
            EditStateToken(DATASET_UUID, FINGERPRINT, 2, 2),
        )
        self.assertIsNotNone(token)
        self.assertTrue(journal.poll_redo())

        branch_after, branch_indexes, _plan, branch_operation, branch_command = _transition(
            after_two,
            indexes_two,
            300,
            1,
        )
        token = apply_forward(after_two, indexes_two, branch_command.delta, token)
        journal.push(branch_command)
        self.assertFalse(journal.poll_redo())
        self.assertEqual(journal.redo_count, 0)
        self.assertEqual(journal.history, (operation_one, branch_operation))
        self.assertEqual((after_two, indexes_two), (branch_after, branch_indexes))
        self.assertNotEqual(journal.history[-1], operation_two)

    def test_empty_poll_barrier_and_clear_have_explicit_boundaries(self):
        """빈 poll, barrier의 history 보존과 clear의 전체 초기화를 검증합니다."""

        baseline = _model()
        indexes = build_relationship_indexes(baseline)
        journal = EditJournal(DATASET_UUID, FINGERPRINT)
        token = EditStateToken(DATASET_UUID, FINGERPRINT, 0, 0)
        self.assertFalse(journal.poll_undo())
        self.assertFalse(journal.poll_redo())
        self.assertIsNone(journal.undo(baseline, indexes, token))
        self.assertIsNone(journal.redo(baseline, indexes, token))

        _after, _after_indexes, _plan, operation, command = _transition(
            baseline,
            indexes,
            100,
            0,
        )
        journal.push(command)
        journal.barrier()
        self.assertEqual(journal.history, (operation,))
        self.assertEqual(journal.revision, 1)
        self.assertFalse(journal.poll_undo())
        self.assertFalse(journal.poll_redo())

        journal.clear()
        self.assertEqual(journal.history, ())
        self.assertEqual(journal.revision, 0)
        self.assertEqual(journal.undo_count, 0)
        self.assertEqual(journal.redo_count, 0)

    def test_failed_undo_keeps_model_and_journal_stacks_unchanged(self):
        """영향 index 불일치로 Undo가 실패하면 모델과 Journal 상태를 유지합니다."""

        baseline = _model()
        baseline_indexes = build_relationship_indexes(baseline)
        after, after_indexes, _plan, operation, command = _transition(
            baseline,
            baseline_indexes,
            100,
            0,
        )
        journal = EditJournal(DATASET_UUID, FINGERPRINT)
        journal.push(command)
        after_indexes.image_to_points[10] = frozenset({999})
        stale_state = copy.deepcopy((after, after_indexes))

        with self.assertRaisesRegex(ValueError, "기대 값"):
            journal.undo(
                after,
                after_indexes,
                EditStateToken(DATASET_UUID, FINGERPRINT, 1, 1),
            )
        self.assertEqual((after, after_indexes), stale_state)
        self.assertEqual(journal.history, (operation,))
        self.assertEqual(journal.undo_count, 1)
        self.assertEqual(journal.redo_count, 0)

    def test_revision_or_source_token_mismatch_is_fail_closed(self):
        """Journal과 다른 revision·Source token은 stack 변경 전에 거부합니다."""

        baseline = _model()
        baseline_indexes = build_relationship_indexes(baseline)
        after, after_indexes, _plan, operation, command = _transition(
            baseline,
            baseline_indexes,
            100,
            0,
        )
        journal = EditJournal(DATASET_UUID, FINGERPRINT)
        journal.push(command)
        unchanged = copy.deepcopy((after, after_indexes))

        with self.assertRaisesRegex(ValueError, "revision"):
            journal.undo(
                after,
                after_indexes,
                EditStateToken(DATASET_UUID, FINGERPRINT, 0, 0),
            )
        with self.assertRaisesRegex(ValueError, "source fingerprint"):
            journal.undo(
                after,
                after_indexes,
                EditStateToken(DATASET_UUID, "c" * 64, 1, 1),
            )
        self.assertEqual((after, after_indexes), unchanged)
        self.assertEqual(journal.history, (operation,))
        self.assertEqual((journal.undo_count, journal.redo_count), (1, 0))


if __name__ == "__main__":
    unittest.main()
