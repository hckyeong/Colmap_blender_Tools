from __future__ import annotations

import unittest
import uuid
from dataclasses import replace
from unittest.mock import patch

from tools.modifier.core.edit_replay import history_hash, replay_history
from tools.modifier.core.edits import (
    apply_image_observation_removal,
    apply_point_removal,
    apply_removal,
    deserialize_history,
    image_operation_from_report,
    operation_from_report,
    plan_image_observation_removal,
    plan_point_removal,
    plan_removal,
    point_operation_from_report,
    serialize_history,
)
from tools.modifier.core.indexes import build_relationship_indexes
from tools.modifier.core.integrity import validate_reconstruction
from tools.modifier.core.model import (
    CameraRecord,
    DataIdRecord,
    FrameRecord,
    ImageRecord,
    Point2DRecord,
    Point3DRecord,
    Reconstruction,
    RigRecord,
    SensorRecord,
    TrackElement,
)


def _camera(camera_id: int) -> CameraRecord:
    return CameraRecord(camera_id, 0, "SIMPLE_PINHOLE", 100, 80, (80.0, 50.0, 40.0))


def _legacy_model() -> Reconstruction:
    large_id = 2**63 + 7
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
                    Point2DRecord(1.0, 2.0, large_id),
                    Point2DRecord(3.0, 4.0, 20),
                    Point2DRecord(5.0, 6.0, 30),
                ),
            ),
            20: ImageRecord(
                20,
                (1.0, 0.0, 0.0, 0.0),
                (1.0, 0.0, 0.0),
                1,
                "b.jpg",
                (
                    Point2DRecord(7.0, 8.0, large_id),
                    Point2DRecord(9.0, 10.0, 20),
                    Point2DRecord(11.0, 12.0, 30),
                ),
            ),
            30: ImageRecord(
                30,
                (1.0, 0.0, 0.0, 0.0),
                (2.0, 0.0, 0.0),
                2,
                "c.jpg",
                (Point2DRecord(13.0, 14.0, 20),),
            ),
        },
        points3d={
            large_id: Point3DRecord(
                large_id,
                (0.0, 0.0, 0.0),
                (255, 0, 0),
                0.1,
                (TrackElement(10, 0), TrackElement(20, 0)),
            ),
            20: Point3DRecord(
                20,
                (1.0, 0.0, 0.0),
                (0, 255, 0),
                0.2,
                (TrackElement(10, 1), TrackElement(20, 1), TrackElement(30, 0)),
            ),
            30: Point3DRecord(
                30,
                (2.0, 0.0, 0.0),
                (0, 0, 255),
                0.3,
                (TrackElement(10, 2), TrackElement(20, 2)),
            ),
        },
    )


class PointSelectionEditTests(unittest.TestCase):
    DATASET_UUID = str(uuid.UUID("12345678-1234-5678-1234-567812345678"))
    FINGERPRINT = "a" * 64

    def test_reverse_index_and_removal_preserve_surviving_track(self):
        model = _legacy_model()
        indexes = build_relationship_indexes(model)
        selected = 2**63 + 7
        self.assertEqual(indexes.point_to_images[selected], frozenset({10, 20}))

        plan = plan_removal(model, indexes, [selected], source_revision=0)
        self.assertEqual(plan.observing_image_ids, (10, 20))
        self.assertEqual(plan.affected_point3d_ids, (20, 30, selected))
        self.assertEqual(plan.derived_orphan_point3d_ids, (30,))
        result, result_indexes, report = apply_removal(
            model, plan, current_revision=0
        )

        self.assertEqual(set(result.images), {30})
        self.assertEqual(set(result.points3d), {20})
        self.assertEqual(result.points3d[20].track, (TrackElement(30, 0),))
        self.assertEqual(result.images[30].points2d[0].point3d_id, 20)
        self.assertEqual(result_indexes.point_to_images[20], frozenset({30}))
        self.assertEqual(set(result.cameras), {2})
        self.assertEqual(report.after_counts.points3d, 1)
        self.assertEqual(len(model.images), 3, "입력 Reconstruction은 변경되면 안 됩니다.")

    def test_apply_reuses_unaffected_records_and_builds_result_index_once(self):
        model = _legacy_model()
        model.cameras[3] = _camera(3)
        model.cameras[99] = _camera(99)
        model.images[40] = ImageRecord(
            40,
            (1.0, 0.0, 0.0, 0.0),
            (3.0, 0.0, 0.0),
            3,
            "d.jpg",
            (Point2DRecord(15.0, 16.0, 40),),
        )
        model.points3d[40] = Point3DRecord(
            40,
            (3.0, 0.0, 0.0),
            (10, 20, 30),
            0.4,
            (TrackElement(40, 0),),
        )
        indexes = build_relationship_indexes(model)
        plan = plan_removal(
            model,
            indexes,
            [2**63 + 7],
            source_revision=0,
        )
        self.assertNotIn(40, plan.affected_point3d_ids)

        with patch(
            "tools.modifier.core.legacy_removal.build_relationship_indexes",
            wraps=build_relationship_indexes,
        ) as result_index_builder:
            result, _, _ = apply_removal(model, plan, current_revision=0)

        self.assertEqual(
            result_index_builder.call_count,
            1,
            "삭제 결과의 전체 검증과 인덱스 생성은 한 번이어야 합니다.",
        )
        self.assertIs(result.points3d[40], model.points3d[40])
        self.assertIs(result.images[30], model.images[30])
        self.assertIs(result.images[40], model.images[40])
        self.assertIs(result.images[40].points2d, model.images[40].points2d)
        self.assertIsNot(result.points3d[20], model.points3d[20])
        self.assertNotIn(
            99,
            result.cameras,
            "삭제 후 전체 미사용 Camera 정리라는 기존 결과가 바뀌었습니다.",
        )

    def test_overlapping_selected_points_do_not_duplicate_images(self):
        model = _legacy_model()
        plan = plan_removal(
            model,
            build_relationship_indexes(model),
            [2**63 + 7, 30],
            source_revision=0,
        )
        self.assertEqual(plan.observing_image_ids, (10, 20))
        self.assertEqual(plan.derived_orphan_point3d_ids, ())

    def test_shared_camera_record_is_retained_when_one_image_survives(self):
        model = _legacy_model()
        point = model.points3d[30]
        model.points3d[30] = Point3DRecord(
            point.point3d_id,
            point.xyz,
            point.rgb,
            point.error,
            (TrackElement(10, 2),),
        )
        image = model.images[20]
        points2d = list(image.points2d)
        points2d[2] = Point2DRecord(points2d[2].x, points2d[2].y, None)
        model.images[20] = ImageRecord(
            image.image_id,
            image.qvec,
            image.tvec,
            image.camera_id,
            image.name,
            tuple(points2d),
        )
        plan = plan_removal(
            model,
            build_relationship_indexes(model),
            [30],
            source_revision=0,
        )
        self.assertEqual(plan.observing_image_ids, (10,))
        self.assertNotIn(1, plan.removed_camera_ids)

    def test_trivial_rig_is_cleaned(self):
        model = Reconstruction(
            cameras={1: _camera(1), 2: _camera(2)},
            images={
                1: ImageRecord(
                    1,
                    (1.0, 0.0, 0.0, 0.0),
                    (0.0, 0.0, 0.0),
                    1,
                    "a.jpg",
                    (Point2DRecord(1.0, 1.0, 1),),
                )
            },
            points3d={
                1: Point3DRecord(
                    1,
                    (0.0, 0.0, 0.0),
                    (1, 2, 3),
                    0.1,
                    (TrackElement(1, 0),),
                )
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
                    (DataIdRecord(0, 1, 1),),
                )
            },
        )
        plan = plan_removal(
            model,
            build_relationship_indexes(model),
            [1],
            source_revision=0,
        )
        result, _, report = apply_removal(model, plan, current_revision=0)
        self.assertFalse(result.frames)
        self.assertFalse(result.rigs)
        self.assertFalse(result.cameras)
        self.assertEqual(report.removed_frame_ids, (1,))
        self.assertEqual(report.removed_rig_ids, (1, 2))

    def test_real_multi_sensor_rig_is_rejected_before_change(self):
        model = Reconstruction(
            cameras={1: _camera(1)},
            images={
                1: ImageRecord(
                    1,
                    (1.0, 0.0, 0.0, 0.0),
                    (0.0, 0.0, 0.0),
                    1,
                    "a.jpg",
                    (Point2DRecord(1.0, 1.0, 1),),
                )
            },
            points3d={
                1: Point3DRecord(
                    1,
                    (0.0, 0.0, 0.0),
                    (1, 2, 3),
                    0.1,
                    (TrackElement(1, 0),),
                )
            },
            rigs={
                1: RigRecord(
                    1,
                    0,
                    1,
                    (SensorRecord(1, 7, True, (1.0, 0.0, 0.0, 0.0), (0.0, 0.0, 0.0)),),
                )
            },
            frames={
                1: FrameRecord(
                    1,
                    1,
                    (1.0, 0.0, 0.0, 0.0),
                    (0.0, 0.0, 0.0),
                    (DataIdRecord(0, 1, 1), DataIdRecord(1, 7, 2)),
                )
            },
        )
        with self.assertRaisesRegex(ValueError, "다중 센서"):
            plan_removal(
                model,
                build_relationship_indexes(model),
                [1],
                source_revision=0,
            )
        self.assertIn(1, model.images)

    def test_history_roundtrip_and_replay(self):
        model = _legacy_model()
        plan = plan_removal(
            model,
            build_relationship_indexes(model),
            [2**63 + 7],
            source_revision=0,
        )
        expected, _, report = apply_removal(model, plan, current_revision=0)
        operation = operation_from_report(
            report, self.DATASET_UUID, self.FINGERPRINT, 0
        )
        serialized = serialize_history((operation,))
        restored = deserialize_history(serialized)
        replayed, _ = replay_history(
            model, restored, self.DATASET_UUID, self.FINGERPRINT
        )
        self.assertEqual(replayed, expected)
        self.assertEqual(history_hash(restored), history_hash(deserialize_history(serialized)))

    def test_replay_rejects_source_mismatch(self):
        model = _legacy_model()
        plan = plan_removal(
            model,
            build_relationship_indexes(model),
            [2**63 + 7],
            source_revision=0,
        )
        _, _, report = apply_removal(model, plan, current_revision=0)
        operation = operation_from_report(
            report, self.DATASET_UUID, self.FINGERPRINT, 0
        )
        with self.assertRaisesRegex(ValueError, "fingerprint"):
            replay_history(model, (operation,), self.DATASET_UUID, "b" * 64)

    def test_integrity_rejects_dict_key_record_id_mismatch(self):
        model = _legacy_model()
        model.points3d[999] = model.points3d.pop(30)
        with self.assertRaisesRegex(ValueError, "dict key"):
            validate_reconstruction(model)

    def test_integrity_rejects_empty_track_point(self):
        model = _legacy_model()
        point = model.points3d[30]
        model.points3d[30] = Point3DRecord(
            point.point3d_id, point.xyz, point.rgb, point.error, ()
        )
        for image_id in (10, 20):
            image = model.images[image_id]
            points2d = list(image.points2d)
            points2d[2] = Point2DRecord(points2d[2].x, points2d[2].y, None)
            model.images[image_id] = ImageRecord(
                image.image_id,
                image.qvec,
                image.tvec,
                image.camera_id,
                image.name,
                tuple(points2d),
            )
        with self.assertRaisesRegex(ValueError, "track이 비어"):
            build_relationship_indexes(model)

    def test_apply_rejects_stale_removal_plan_revision(self):
        model = _legacy_model()
        plan = plan_removal(
            model,
            build_relationship_indexes(model),
            [2**63 + 7],
            source_revision=0,
        )
        with self.assertRaisesRegex(ValueError, "revision"):
            apply_removal(model, plan, current_revision=1)

    def test_unused_nontrivial_rig_is_preserved(self):
        model = Reconstruction(
            cameras={1: _camera(1), 2: _camera(2)},
            images={
                1: ImageRecord(
                    1,
                    (1.0, 0.0, 0.0, 0.0),
                    (0.0, 0.0, 0.0),
                    1,
                    "a.jpg",
                    (Point2DRecord(1.0, 1.0, 1),),
                )
            },
            points3d={
                1: Point3DRecord(
                    1,
                    (0.0, 0.0, 0.0),
                    (1, 2, 3),
                    0.1,
                    (TrackElement(1, 0),),
                )
            },
            rigs={
                1: RigRecord(1, 0, 1, ()),
                2: RigRecord(
                    2,
                    0,
                    2,
                    (
                        SensorRecord(
                            1,
                            7,
                            True,
                            (1.0, 0.0, 0.0, 0.0),
                            (0.0, 0.0, 0.0),
                        ),
                    ),
                ),
            },
            frames={
                1: FrameRecord(
                    1,
                    1,
                    (1.0, 0.0, 0.0, 0.0),
                    (0.0, 0.0, 0.0),
                    (DataIdRecord(0, 1, 1),),
                ),
                2: FrameRecord(
                    2,
                    2,
                    (1.0, 0.0, 0.0, 0.0),
                    (0.0, 0.0, 0.0),
                    (),
                ),
            },
        )
        plan = plan_removal(
            model,
            build_relationship_indexes(model),
            [1],
            source_revision=0,
        )
        result, _, _ = apply_removal(model, plan, current_revision=0)
        self.assertIn(2, result.rigs)
        self.assertIn(2, result.cameras)
        self.assertIn(2, result.frames)

    def test_history_json_rejects_non_integer_ids_and_invalid_uuid(self):
        model = _legacy_model()
        plan = plan_removal(
            model,
            build_relationship_indexes(model),
            [2**63 + 7],
            source_revision=0,
        )
        _, _, report = apply_removal(model, plan, current_revision=0)
        operation = operation_from_report(
            report, self.DATASET_UUID, self.FINGERPRINT, 0
        )
        raw = serialize_history((operation,))
        with self.assertRaisesRegex(ValueError, "정수 ID"):
            deserialize_history(raw.replace(str(2**63 + 7), '"bad"', 1))
        with self.assertRaisesRegex(ValueError, "UUID"):
            deserialize_history(raw.replace(self.DATASET_UUID, "not-a-uuid"))

    def test_two_operations_replay_in_order_and_reject_tampered_result(self):
        model = Reconstruction(
            cameras={1: _camera(1), 2: _camera(2)},
            images={
                1: ImageRecord(
                    1,
                    (1.0, 0.0, 0.0, 0.0),
                    (0.0, 0.0, 0.0),
                    1,
                    "a.jpg",
                    (Point2DRecord(1.0, 1.0, 1),),
                ),
                2: ImageRecord(
                    2,
                    (1.0, 0.0, 0.0, 0.0),
                    (1.0, 0.0, 0.0),
                    2,
                    "b.jpg",
                    (Point2DRecord(2.0, 2.0, 2),),
                ),
            },
            points3d={
                1: Point3DRecord(
                    1,
                    (0.0, 0.0, 0.0),
                    (1, 2, 3),
                    0.1,
                    (TrackElement(1, 0),),
                ),
                2: Point3DRecord(
                    2,
                    (1.0, 0.0, 0.0),
                    (4, 5, 6),
                    0.2,
                    (TrackElement(2, 0),),
                ),
            },
        )
        first_plan = plan_removal(
            model,
            build_relationship_indexes(model),
            [1],
            source_revision=0,
        )
        after_first, after_first_indexes, first_report = apply_removal(
            model,
            first_plan,
            current_revision=0,
        )
        first_operation = operation_from_report(
            first_report,
            self.DATASET_UUID,
            self.FINGERPRINT,
            0,
        )
        second_plan = plan_removal(
            after_first,
            after_first_indexes,
            [2],
            source_revision=1,
        )
        expected, _, second_report = apply_removal(
            after_first,
            second_plan,
            current_revision=1,
        )
        second_operation = operation_from_report(
            second_report,
            self.DATASET_UUID,
            self.FINGERPRINT,
            1,
        )

        replayed, _ = replay_history(
            model,
            (first_operation, second_operation),
            self.DATASET_UUID,
            self.FINGERPRINT,
        )
        self.assertEqual(replayed, expected)

        tampered_second = replace(second_operation, removed_image_ids=(999,))
        with self.assertRaisesRegex(ValueError, "저장 결과"):
            replay_history(
                model,
                (first_operation, tampered_second),
                self.DATASET_UUID,
                self.FINGERPRINT,
            )

        with (
            patch(
                "tools.modifier.core.edit_replay.build_relationship_indexes",
                wraps=build_relationship_indexes,
            ) as baseline_index_builder,
            patch(
                "tools.modifier.core.legacy_removal.build_relationship_indexes",
                wraps=build_relationship_indexes,
            ) as result_index_builder,
        ):
            replayed_again, replayed_indexes = replay_history(
                model,
                (first_operation, second_operation),
                self.DATASET_UUID,
                self.FINGERPRINT,
            )
        self.assertEqual(replayed_again, expected)
        self.assertEqual(
            baseline_index_builder.call_count,
            1,
            "Replay baseline 인덱스는 한 번만 생성해야 합니다.",
        )
        self.assertEqual(
            result_index_builder.call_count,
            2,
            "Replay operation마다 결과 인덱스를 한 번만 생성해야 합니다.",
        )
        self.assertEqual(replayed_indexes.point_to_images, {})


class RedesignedRemovalTests(unittest.TestCase):
    DATASET_UUID = str(uuid.UUID("12345678-1234-5678-1234-567812345678"))
    FINGERPRINT = "a" * 64

    def test_point_removal_keeps_all_images_and_cameras(self):
        model = _legacy_model()
        selected = 2**63 + 7
        plan = plan_point_removal(
            model,
            build_relationship_indexes(model),
            [selected],
            source_revision=0,
        )
        self.assertEqual(plan.affected_image_ids, (10, 20))
        self.assertEqual(plan.cleared_observation_count, 2)

        with patch(
            "tools.modifier.core.removal.build_relationship_indexes",
            wraps=build_relationship_indexes,
        ) as result_index_builder:
            result, result_indexes, report = apply_point_removal(
                model,
                plan,
                current_revision=0,
            )

        self.assertEqual(result_index_builder.call_count, 1)
        self.assertEqual(set(result.images), {10, 20, 30})
        self.assertEqual(set(result.cameras), {1, 2})
        self.assertEqual(set(result.points3d), {20, 30})
        self.assertIsNone(result.images[10].points2d[0].point3d_id)
        self.assertIsNone(result.images[20].points2d[0].point3d_id)
        self.assertEqual(result.images[10].points2d[0].x, 1.0)
        self.assertEqual(result.images[10].points2d[0].y, 2.0)
        self.assertIs(result.images[30], model.images[30])
        self.assertIs(result.images[30].points2d, model.images[30].points2d)
        self.assertEqual(result_indexes.image_to_points[10], frozenset({20, 30}))
        self.assertEqual(report.cleared_observation_count, 2)
        self.assertIn(selected, model.points3d, "입력 Reconstruction은 변경되면 안 됩니다.")

    def test_point_removal_rebuilds_each_affected_image_once(self):
        model = _legacy_model()
        plan = plan_point_removal(
            model,
            build_relationship_indexes(model),
            [2**63 + 7, 30],
            source_revision=0,
        )
        result, _, report = apply_point_removal(model, plan, current_revision=0)
        self.assertEqual(report.cleared_observation_count, 4)
        self.assertIsNone(result.images[10].points2d[0].point3d_id)
        self.assertIsNone(result.images[10].points2d[2].point3d_id)
        self.assertIsNone(result.images[20].points2d[0].point3d_id)
        self.assertIsNone(result.images[20].points2d[2].point3d_id)

    def test_image_removal_deletes_observed_points_globally(self):
        model = _legacy_model()
        plan = plan_image_observation_removal(
            model,
            build_relationship_indexes(model),
            [10],
            source_revision=0,
        )
        self.assertEqual(
            plan.observed_point3d_ids,
            (20, 30, 2**63 + 7),
        )
        self.assertEqual(plan.affected_surviving_image_ids, (20, 30))
        self.assertEqual(plan.cleared_surviving_observation_count, 4)

        result, result_indexes, report = apply_image_observation_removal(
            model,
            plan,
            current_revision=0,
        )

        self.assertEqual(set(result.images), {20, 30})
        self.assertFalse(result.points3d)
        self.assertEqual(set(result.cameras), {1, 2})
        self.assertTrue(
            all(point.point3d_id is None for point in result.images[20].points2d)
        )
        self.assertIsNone(result.images[30].points2d[0].point3d_id)
        self.assertEqual(result_indexes.image_to_points[20], frozenset())
        self.assertEqual(report.cleared_surviving_observation_count, 4)
        self.assertEqual(len(model.points3d), 3)

    def test_image_removal_reuses_unaffected_image_and_builds_index_once(self):
        model = _legacy_model()
        model.cameras[3] = _camera(3)
        model.images[40] = ImageRecord(
            40,
            (1.0, 0.0, 0.0, 0.0),
            (3.0, 0.0, 0.0),
            3,
            "d.jpg",
            (),
        )
        plan = plan_image_observation_removal(
            model,
            build_relationship_indexes(model),
            [10],
            source_revision=0,
        )
        with patch(
            "tools.modifier.core.removal.build_relationship_indexes",
            wraps=build_relationship_indexes,
        ) as result_index_builder:
            result, _, _ = apply_image_observation_removal(
                model,
                plan,
                current_revision=0,
            )
        self.assertEqual(result_index_builder.call_count, 1)
        self.assertIs(result.images[40], model.images[40])
        self.assertIs(result.images[40].points2d, model.images[40].points2d)

    def test_schema_two_and_mixed_schema_replay(self):
        model = _legacy_model()
        legacy_plan = plan_removal(
            model,
            build_relationship_indexes(model),
            [2**63 + 7],
            source_revision=0,
        )
        after_legacy, after_legacy_indexes, legacy_report = apply_removal(
            model,
            legacy_plan,
            current_revision=0,
        )
        legacy_operation = operation_from_report(
            legacy_report,
            self.DATASET_UUID,
            self.FINGERPRINT,
            0,
        )
        point_plan = plan_point_removal(
            after_legacy,
            after_legacy_indexes,
            [20],
            source_revision=1,
        )
        expected, _, point_report = apply_point_removal(
            after_legacy,
            point_plan,
            current_revision=1,
        )
        point_operation = point_operation_from_report(
            point_report,
            self.DATASET_UUID,
            self.FINGERPRINT,
            1,
        )
        serialized = serialize_history((legacy_operation, point_operation))
        restored = deserialize_history(serialized)
        replayed, _ = replay_history(
            model,
            restored,
            self.DATASET_UUID,
            self.FINGERPRINT,
        )
        self.assertEqual(replayed, expected)
        self.assertEqual(restored[0].schema_version, 1)
        self.assertEqual(restored[1].schema_version, 2)

    def test_schema_two_image_result_tamper_is_rejected(self):
        model = _legacy_model()
        plan = plan_image_observation_removal(
            model,
            build_relationship_indexes(model),
            [10],
            source_revision=0,
        )
        _, _, report = apply_image_observation_removal(
            model,
            plan,
            current_revision=0,
        )
        operation = image_operation_from_report(
            report,
            self.DATASET_UUID,
            self.FINGERPRINT,
            0,
        )
        tampered = replace(operation, removed_point3d_ids=(20,))
        with self.assertRaisesRegex(ValueError, "저장 결과"):
            replay_history(
                model,
                (tampered,),
                self.DATASET_UUID,
                self.FINGERPRINT,
            )

    def test_schema_two_stale_plans_are_rejected(self):
        model = _legacy_model()
        indexes = build_relationship_indexes(model)
        point_plan = plan_point_removal(model, indexes, [20], source_revision=0)
        image_plan = plan_image_observation_removal(model, indexes, [10], source_revision=0)
        with self.assertRaisesRegex(ValueError, "revision"):
            apply_point_removal(model, point_plan, current_revision=1)
        with self.assertRaisesRegex(ValueError, "revision"):
            apply_image_observation_removal(model, image_plan, current_revision=1)


if __name__ == "__main__":
    unittest.main()
