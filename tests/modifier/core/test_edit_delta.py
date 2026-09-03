from __future__ import annotations

import copy
import unittest
from dataclasses import FrozenInstanceError
from unittest.mock import patch

from tools.modifier.core import edit_delta as edit_delta_module
from tools.modifier.core.edit_delta import (
    EditStateToken,
    apply_forward,
    apply_inverse,
    capture_edit_delta,
)
from tools.modifier.core.edit_history import (
    image_operation_from_report,
    operation_from_report,
    point_operation_from_report,
)
from tools.modifier.core.indexes import RelationshipIndexes, build_relationship_indexes
from tools.modifier.core.legacy_removal import apply_removal, plan_removal
from tools.modifier.core.model import (
    CameraRecord,
    DataIdRecord,
    FrameRecord,
    ImageRecord,
    Point2DRecord,
    Point3DRecord,
    Reconstruction,
    RigRecord,
    TrackElement,
)
from tools.modifier.core.removal import (
    apply_image_observation_removal_indexed,
    apply_point_removal_indexed,
    plan_image_observation_removal,
    plan_point_removal,
)


DATASET_UUID = "12345678-1234-5678-1234-567812345678"
FINGERPRINT = "a" * 64


def _camera(camera_id: int) -> CameraRecord:
    """테스트용 SIMPLE_PINHOLE Camera를 생성합니다."""

    return CameraRecord(camera_id, 0, "SIMPLE_PINHOLE", 100, 80, (80.0, 50.0, 40.0))


def _model() -> Reconstruction:
    """Point와 Image 제거가 모두 가능한 작은 Reconstruction을 생성합니다."""

    return Reconstruction(
        cameras={1: _camera(1), 2: _camera(2)},
        images={
            10: ImageRecord(
                10,
                (1.0, 0.0, 0.0, 0.0),
                (0.0, 0.0, 0.0),
                1,
                "a.jpg",
                (
                    Point2DRecord(1.0, 2.0, 100),
                    Point2DRecord(3.0, 4.0, 200),
                ),
            ),
            20: ImageRecord(
                20,
                (1.0, 0.0, 0.0, 0.0),
                (1.0, 0.0, 0.0),
                2,
                "b.jpg",
                (
                    Point2DRecord(5.0, 6.0, 100),
                    Point2DRecord(7.0, 8.0, 200),
                ),
            ),
        },
        points3d={
            100: Point3DRecord(
                100,
                (0.0, 0.0, 0.0),
                (255, 0, 0),
                0.1,
                (TrackElement(10, 0), TrackElement(20, 0)),
            ),
            200: Point3DRecord(
                200,
                (1.0, 0.0, 0.0),
                (0, 255, 0),
                0.2,
                (TrackElement(10, 1), TrackElement(20, 1)),
            ),
        },
        rigs={
            1: RigRecord(1, 0, 1, ()),
            2: RigRecord(2, 0, 2, ()),
        },
        frames={
            1: FrameRecord(
                1,
                1,
                (1.0, 0.0, 0.0, 0.0),
                (0.0, 0.0, 0.0),
                (DataIdRecord(0, 1, 10),),
            ),
            2: FrameRecord(
                2,
                2,
                (1.0, 0.0, 0.0, 0.0),
                (1.0, 0.0, 0.0),
                (DataIdRecord(0, 2, 20),),
            ),
        },
    )


def _mapping_identities(
    model: Reconstruction,
    indexes: RelationshipIndexes,
) -> tuple[int, ...]:
    """Reconstruction 5개와 관계 인덱스 4개 dict identity를 반환합니다."""

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


class _NoIterationDict(dict):
    """영향 key 조회 외의 전체 mapping 순회를 테스트에서 차단합니다."""

    def __iter__(self):
        """전체 key 순회 시 즉시 실패합니다."""

        raise AssertionError("Delta 캡처에서 전체 mapping 순회를 호출했습니다.")

    def keys(self):
        """전체 key view 요청 시 즉시 실패합니다."""

        raise AssertionError("Delta 캡처에서 전체 mapping key 조회를 호출했습니다.")

    def items(self):
        """전체 item view 요청 시 즉시 실패합니다."""

        raise AssertionError("Delta 캡처에서 전체 mapping item 조회를 호출했습니다.")

    def values(self):
        """전체 value view 요청 시 즉시 실패합니다."""

        raise AssertionError("Delta 캡처에서 전체 mapping value 조회를 호출했습니다.")


class EditDeltaTests(unittest.TestCase):
    def test_point_delta_inverse_forward_preserves_all_mapping_identities(self):
        """Point Delta의 undo·redo 결과와 9개 dict identity를 검증합니다."""

        before = _model()
        before_indexes = build_relationship_indexes(before)
        plan = plan_point_removal(before, before_indexes, [100], source_revision=0)
        after, after_indexes, report = apply_point_removal_indexed(
            before,
            plan,
            current_revision=0,
            indexes=before_indexes,
        )
        operation = point_operation_from_report(report, DATASET_UUID, FINGERPRINT, 0)
        delta = capture_edit_delta(
            before,
            before_indexes,
            after,
            after_indexes,
            plan,
            operation,
        )
        expected_before = copy.deepcopy(before)
        expected_before_indexes = copy.deepcopy(before_indexes)
        expected_after = copy.deepcopy(after)
        expected_after_indexes = copy.deepcopy(after_indexes)
        identities = _mapping_identities(after, after_indexes)

        token = apply_inverse(
            after,
            after_indexes,
            delta,
            EditStateToken(DATASET_UUID, FINGERPRINT, 1, 1),
        )
        self.assertEqual(after, expected_before)
        self.assertEqual(after_indexes, expected_before_indexes)
        self.assertEqual(_mapping_identities(after, after_indexes), identities)

        token = apply_forward(after, after_indexes, delta, token)
        self.assertEqual(after, expected_after)
        self.assertEqual(after_indexes, expected_after_indexes)
        self.assertEqual(_mapping_identities(after, after_indexes), identities)
        self.assertEqual((token.edit_revision, token.index_revision), (1, 1))

    def test_image_delta_restores_removed_containers_and_indexes(self):
        """Image Delta가 Camera·Rig·Frame과 모든 영향 인덱스를 복원합니다."""

        before = _model()
        before_indexes = build_relationship_indexes(before)
        plan = plan_image_observation_removal(
            before,
            before_indexes,
            [10],
            source_revision=0,
        )
        after, after_indexes, report = apply_image_observation_removal_indexed(
            before,
            plan,
            current_revision=0,
            indexes=before_indexes,
        )
        operation = image_operation_from_report(report, DATASET_UUID, FINGERPRINT, 0)
        delta = capture_edit_delta(
            before,
            before_indexes,
            after,
            after_indexes,
            plan,
            operation,
        )
        self.assertEqual(tuple(entry.key for entry in delta.cameras), (1,))
        self.assertEqual(tuple(entry.key for entry in delta.rigs), (1,))
        self.assertEqual(tuple(entry.key for entry in delta.frames), (1,))
        self.assertEqual(tuple(entry.key for entry in delta.camera_to_images), (1,))
        self.assertEqual(tuple(entry.key for entry in delta.image_to_frame), (10,))
        expected_before = copy.deepcopy(before)
        expected_before_indexes = copy.deepcopy(before_indexes)
        expected_after = copy.deepcopy(after)
        expected_after_indexes = copy.deepcopy(after_indexes)
        identities = _mapping_identities(after, after_indexes)

        token = apply_inverse(
            after,
            after_indexes,
            delta,
            EditStateToken(DATASET_UUID, FINGERPRINT, 1, 1),
        )
        self.assertEqual(after, expected_before)
        self.assertEqual(after_indexes, expected_before_indexes)
        self.assertEqual(_mapping_identities(after, after_indexes), identities)
        apply_forward(after, after_indexes, delta, token)
        self.assertEqual(after, expected_after)
        self.assertEqual(after_indexes, expected_after_indexes)

    def test_capture_uses_only_plan_impact_keys(self):
        """Delta 캡처가 Reconstruction과 인덱스 전체 mapping을 순회하지 않습니다."""

        before = _model()
        before_indexes = build_relationship_indexes(before)
        plan = plan_point_removal(before, before_indexes, [100], source_revision=0)
        after, after_indexes, report = apply_point_removal_indexed(
            before,
            plan,
            current_revision=0,
            indexes=before_indexes,
        )
        operation = point_operation_from_report(report, DATASET_UUID, FINGERPRINT, 0)
        guarded_before = Reconstruction(
            cameras=_NoIterationDict(before.cameras),
            images=_NoIterationDict(before.images),
            points3d=_NoIterationDict(before.points3d),
            rigs=_NoIterationDict(before.rigs),
            frames=_NoIterationDict(before.frames),
        )
        guarded_after = Reconstruction(
            cameras=_NoIterationDict(after.cameras),
            images=_NoIterationDict(after.images),
            points3d=_NoIterationDict(after.points3d),
            rigs=_NoIterationDict(after.rigs),
            frames=_NoIterationDict(after.frames),
        )
        guarded_before_indexes = RelationshipIndexes(
            _NoIterationDict(before_indexes.point_to_images),
            _NoIterationDict(before_indexes.image_to_points),
            _NoIterationDict(before_indexes.camera_to_images),
            _NoIterationDict(before_indexes.image_to_frame),
        )
        guarded_after_indexes = RelationshipIndexes(
            _NoIterationDict(after_indexes.point_to_images),
            _NoIterationDict(after_indexes.image_to_points),
            _NoIterationDict(after_indexes.camera_to_images),
            _NoIterationDict(after_indexes.image_to_frame),
        )
        delta = capture_edit_delta(
            guarded_before,
            guarded_before_indexes,
            guarded_after,
            guarded_after_indexes,
            plan,
            operation,
        )
        self.assertEqual(tuple(entry.key for entry in delta.points3d), (100,))

    def test_token_revision_and_late_entry_mismatch_leave_state_unchanged(self):
        """Token 또는 뒤쪽 index entry 불일치가 부분 적용 없이 실패합니다."""

        before = _model()
        before_indexes = build_relationship_indexes(before)
        plan = plan_point_removal(before, before_indexes, [100], source_revision=0)
        after, after_indexes, report = apply_point_removal_indexed(
            before,
            plan,
            current_revision=0,
            indexes=before_indexes,
        )
        operation = point_operation_from_report(report, DATASET_UUID, FINGERPRINT, 0)
        delta = capture_edit_delta(
            before,
            before_indexes,
            after,
            after_indexes,
            plan,
            operation,
        )
        state = copy.deepcopy((after, after_indexes))
        with self.assertRaisesRegex(ValueError, "revision"):
            apply_inverse(
                after,
                after_indexes,
                delta,
                EditStateToken(DATASET_UUID, FINGERPRINT, 0, 0),
            )
        self.assertEqual((after, after_indexes), state)

        after_indexes.image_to_points[20] = frozenset({999})
        stale_state = copy.deepcopy((after, after_indexes))
        with self.assertRaisesRegex(ValueError, "기대 값"):
            apply_inverse(
                after,
                after_indexes,
                delta,
                EditStateToken(DATASET_UUID, FINGERPRINT, 1, 1),
            )
        self.assertEqual((after, after_indexes), stale_state)

    def test_warm_apply_never_calls_full_rebuild_or_history_replay(self):
        """warm 양방향 적용에서 전체 인덱스 구축과 history replay를 호출하지 않습니다."""

        before = _model()
        before_indexes = build_relationship_indexes(before)
        plan = plan_point_removal(before, before_indexes, [100], source_revision=0)
        after, after_indexes, report = apply_point_removal_indexed(
            before,
            plan,
            current_revision=0,
            indexes=before_indexes,
        )
        operation = point_operation_from_report(report, DATASET_UUID, FINGERPRINT, 0)
        delta = capture_edit_delta(
            before,
            before_indexes,
            after,
            after_indexes,
            plan,
            operation,
        )
        with (
            patch(
                "tools.modifier.core.indexes.build_relationship_indexes",
                side_effect=AssertionError("전체 인덱스 구축 금지"),
            ),
            patch(
                "tools.modifier.core.edit_replay.replay_history",
                side_effect=AssertionError("전체 history replay 금지"),
            ),
        ):
            token = apply_inverse(
                after,
                after_indexes,
                delta,
                EditStateToken(DATASET_UUID, FINGERPRINT, 1, 1),
            )
            apply_forward(after, after_indexes, delta, token)

    def test_commit_exception_rolls_back_already_applied_entries(self):
        """commit 도중 예외가 발생해도 앞서 적용한 영향 entry를 모두 복구합니다."""

        before = _model()
        before_indexes = build_relationship_indexes(before)
        plan = plan_point_removal(before, before_indexes, [100], source_revision=0)
        after, after_indexes, report = apply_point_removal_indexed(
            before,
            plan,
            current_revision=0,
            indexes=before_indexes,
        )
        operation = point_operation_from_report(report, DATASET_UUID, FINGERPRINT, 0)
        delta = capture_edit_delta(
            before,
            before_indexes,
            after,
            after_indexes,
            plan,
            operation,
        )
        expected = copy.deepcopy((after, after_indexes))
        original_set = edit_delta_module._set_entry_value
        call_count = 0

        def fail_once_after_write(mapping, key, value):
            """세 번째 쓰기 직후 한 번만 예외를 발생시킵니다."""

            nonlocal call_count
            call_count += 1
            original_set(mapping, key, value)
            if call_count == 3:
                raise RuntimeError("의도한 commit 중간 실패")

        with patch.object(
            edit_delta_module,
            "_set_entry_value",
            side_effect=fail_once_after_write,
        ):
            with self.assertRaisesRegex(RuntimeError, "commit 중간 실패"):
                apply_inverse(
                    after,
                    after_indexes,
                    delta,
                    EditStateToken(DATASET_UUID, FINGERPRINT, 1, 1),
                )
        self.assertEqual((after, after_indexes), expected)

    def test_delta_is_frozen_and_legacy_operation_is_rejected(self):
        """Delta 불변성과 schema 1 신규 command 캡처 거부를 확인합니다."""

        before = _model()
        before_indexes = build_relationship_indexes(before)
        point_plan = plan_point_removal(before, before_indexes, [100], source_revision=0)
        after, after_indexes, point_report = apply_point_removal_indexed(
            before,
            point_plan,
            current_revision=0,
            indexes=before_indexes,
        )
        point_operation = point_operation_from_report(
            point_report,
            DATASET_UUID,
            FINGERPRINT,
            0,
        )
        delta = capture_edit_delta(
            before,
            before_indexes,
            after,
            after_indexes,
            point_plan,
            point_operation,
        )
        with self.assertRaises(FrozenInstanceError):
            delta.previous_revision = 3  # type: ignore[misc]

        legacy_plan = plan_removal(before, before_indexes, [100], source_revision=0)
        _legacy_after, _legacy_indexes, legacy_report = apply_removal(
            before,
            legacy_plan,
            current_revision=0,
        )
        legacy_operation = operation_from_report(
            legacy_report,
            DATASET_UUID,
            FINGERPRINT,
            0,
        )
        with self.assertRaisesRegex(ValueError, "Legacy schema 1"):
            capture_edit_delta(
                before,
                before_indexes,
                after,
                after_indexes,
                point_plan,
                legacy_operation,  # type: ignore[arg-type]
            )


if __name__ == "__main__":
    unittest.main()
