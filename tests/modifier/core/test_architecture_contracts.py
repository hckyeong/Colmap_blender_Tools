"""리팩토링 중 공개 Core 편집 API의 호환성을 고정합니다."""

from __future__ import annotations

import inspect
import unittest

from tools.modifier.core import (
    edit_history,
    edit_types,
    edits,
    legacy_removal,
    model,
    removal,
)
from tools.modifier.core import indexes


class CoreEditArchitectureContractTests(unittest.TestCase):
    def test_edits_facade_reexports_concrete_module_objects(self):
        expected_origins = {
            "LEGACY_EDIT_SCHEMA_VERSION": edit_types,
            "EDIT_SCHEMA_VERSION": edit_types,
            "REMOVE_SELECTED_OPERATION": edit_types,
            "REMOVE_POINTS_OPERATION": edit_types,
            "REMOVE_IMAGES_OPERATION": edit_types,
            "ModelCounts": edit_types,
            "RemovalPlan": edit_types,
            "RemovalReport": edit_types,
            "PointRemovalPlan": edit_types,
            "PointRemovalReport": edit_types,
            "ImageObservationRemovalPlan": edit_types,
            "ImageObservationRemovalReport": edit_types,
            "LegacyEditOperation": edit_types,
            "EditOperation": edit_types,
            "AnyEditOperation": edit_types,
            "ImageRecord": model,
            "Point2DRecord": model,
            "Point3DRecord": model,
            "RelationshipIndexes": indexes,
            "build_relationship_indexes": indexes,
            "Reconstruction": model,
            "plan_point_removal": removal,
            "apply_point_removal": removal,
            "plan_image_observation_removal": removal,
            "apply_image_observation_removal": removal,
            "plan_removal": legacy_removal,
            "apply_removal": legacy_removal,
            "point_operation_from_report": edit_history,
            "image_operation_from_report": edit_history,
            "operation_from_report": edit_history,
            "operation_to_dict": edit_history,
            "operation_from_dict": edit_history,
            "serialize_history": edit_history,
            "deserialize_history": edit_history,
        }
        for name, origin in expected_origins.items():
            with self.subTest(name=name):
                self.assertIs(getattr(edits, name), getattr(origin, name))

    def test_public_removal_function_signatures_are_preserved(self):
        expected = {
            "plan_point_removal": (
                "(model: 'Reconstruction', indexes: 'RelationshipIndexes', "
                "selected_point_ids: 'Iterable[int]', source_revision: 'int') "
                "-> 'PointRemovalPlan'"
            ),
            "apply_point_removal": (
                "(model: 'Reconstruction', plan: 'PointRemovalPlan', "
                "current_revision: 'int') -> "
                "'tuple[Reconstruction, RelationshipIndexes, PointRemovalReport]'"
            ),
            "plan_image_observation_removal": (
                "(model: 'Reconstruction', indexes: 'RelationshipIndexes', "
                "selected_image_ids: 'Iterable[int]', source_revision: 'int') "
                "-> 'ImageObservationRemovalPlan'"
            ),
            "apply_image_observation_removal": (
                "(model: 'Reconstruction', plan: 'ImageObservationRemovalPlan', "
                "current_revision: 'int') -> "
                "'tuple[Reconstruction, RelationshipIndexes, "
                "ImageObservationRemovalReport]'"
            ),
            "plan_removal": (
                "(model: 'Reconstruction', indexes: 'RelationshipIndexes', "
                "selected_point_ids: 'Iterable[int]', source_revision: 'int') "
                "-> 'RemovalPlan'"
            ),
            "apply_removal": (
                "(model: 'Reconstruction', removal_plan: 'RemovalPlan', "
                "current_revision: 'int') -> "
                "'tuple[Reconstruction, RelationshipIndexes, RemovalReport]'"
            ),
            "point_operation_from_report": (
                "(report: 'PointRemovalReport', dataset_uuid: 'str', "
                "source_fingerprint: 'str', previous_revision: 'int') "
                "-> 'EditOperation'"
            ),
            "image_operation_from_report": (
                "(report: 'ImageObservationRemovalReport', dataset_uuid: 'str', "
                "source_fingerprint: 'str', previous_revision: 'int') "
                "-> 'EditOperation'"
            ),
            "operation_from_report": (
                "(report: 'RemovalReport', dataset_uuid: 'str', "
                "source_fingerprint: 'str', previous_revision: 'int') "
                "-> 'LegacyEditOperation'"
            ),
            "operation_to_dict": (
                "(operation: 'AnyEditOperation') -> 'dict[str, Any]'"
            ),
            "operation_from_dict": "(value: 'dict[str, Any]') -> 'AnyEditOperation'",
            "serialize_history": (
                "(operations: 'Iterable[AnyEditOperation]') -> 'str'"
            ),
            "deserialize_history": (
                "(value: 'str') -> 'tuple[AnyEditOperation, ...]'"
            ),
        }
        for name, signature in expected.items():
            with self.subTest(name=name):
                self.assertEqual(str(inspect.signature(getattr(edits, name))), signature)

    def test_edit_operation_repr_equality_and_canonical_json_are_preserved(self):
        operation = edits.EditOperation(
            schema_version=2,
            operation_id="12345678-1234-5678-1234-567812345678",
            operation_type="remove_selected_points",
            dataset_uuid="87654321-4321-8765-4321-876543218765",
            source_fingerprint="a" * 64,
            requested_point3d_ids=(3, 9),
            requested_image_ids=(),
            removed_point3d_ids=(3, 9),
            removed_image_ids=(),
            removed_camera_ids=(),
            removed_frame_ids=(),
            removed_rig_ids=(),
            previous_revision=4,
            resulting_revision=5,
            created_at_utc="2026-01-02T03:04:05+00:00",
        )
        self.assertEqual(operation, edits.EditOperation(**operation.__dict__))
        self.assertEqual(
            repr(operation),
            "EditOperation(schema_version=2, "
            "operation_id='12345678-1234-5678-1234-567812345678', "
            "operation_type='remove_selected_points', "
            "dataset_uuid='87654321-4321-8765-4321-876543218765', "
            f"source_fingerprint='{'a' * 64}', "
            "requested_point3d_ids=(3, 9), requested_image_ids=(), "
            "removed_point3d_ids=(3, 9), removed_image_ids=(), "
            "removed_camera_ids=(), removed_frame_ids=(), removed_rig_ids=(), "
            "previous_revision=4, resulting_revision=5, "
            "created_at_utc='2026-01-02T03:04:05+00:00')",
        )
        self.assertEqual(
            edits.serialize_history((operation,)),
            '[{"created_at_utc":"2026-01-02T03:04:05+00:00",'
            '"dataset_uuid":"87654321-4321-8765-4321-876543218765",'
            '"operation_id":"12345678-1234-5678-1234-567812345678",'
            '"operation_type":"remove_selected_points","previous_revision":4,'
            '"removed_camera_ids":[],"removed_frame_ids":[],"removed_image_ids":[],'
            '"removed_point3d_ids":[3,9],"removed_rig_ids":[],'
            '"requested_image_ids":[],"requested_point3d_ids":[3,9],'
            '"resulting_revision":5,"schema_version":2,'
            f'"source_fingerprint":"{"a" * 64}"}}]',
        )


if __name__ == "__main__":
    unittest.main()
