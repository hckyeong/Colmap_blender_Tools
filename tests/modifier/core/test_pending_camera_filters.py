"""Camera Review 지연 필터의 불변 상태와 JSON 계약을 검증합니다."""

from __future__ import annotations

import json
import unittest
from dataclasses import FrozenInstanceError

from tools.modifier.core.indexes import RelationshipIndexes
from tools.modifier.core.pending_camera_filters import (
    PendingCameraFilterToken,
    PendingCameraFilters,
    add_pending_image,
    deserialize_pending_filters,
    pending_filters_from_image_ids,
    remove_pending_image,
    serialize_pending_filters,
    validate_pending_token,
)


DATASET_UUID = "12345678-1234-5678-1234-567812345678"
SOURCE_FINGERPRINT = "a" * 64
HISTORY_HASH = "b" * 64


def _token() -> PendingCameraFilterToken:
    return PendingCameraFilterToken(
        dataset_uuid=DATASET_UUID,
        source_fingerprint=SOURCE_FINGERPRINT,
        edit_revision=3,
        index_revision=3,
        history_hash=HISTORY_HASH,
    )


def _indexes() -> RelationshipIndexes:
    return RelationshipIndexes(
        point_to_images={
            100: frozenset((2, 5)),
            200: frozenset((5,)),
            300: frozenset((9,)),
        },
        image_to_points={
            2: frozenset((100,)),
            5: frozenset((100, 200)),
            9: frozenset((300,)),
        },
        camera_to_images={1: frozenset((2, 5, 9))},
        image_to_frame={},
    )


class PendingCameraFilterTests(unittest.TestCase):
    def test_first_add_and_shared_point_union_are_deterministic(self):
        pending = PendingCameraFilters(
            token=_token(),
            image_ids=(),
            preview_point3d_ids=frozenset(),
        )

        first = add_pending_image(pending, 5, _indexes())
        second = add_pending_image(first, 2, _indexes())

        self.assertEqual(first.image_ids, (5,))
        self.assertEqual(first.preview_point3d_ids, frozenset((100, 200)))
        self.assertEqual(second.image_ids, (2, 5))
        self.assertEqual(second.preview_point3d_ids, frozenset((100, 200)))

    def test_duplicate_add_returns_the_same_immutable_state(self):
        first = add_pending_image(
            PendingCameraFilters(_token(), (), frozenset()),
            5,
            _indexes(),
        )

        duplicate = add_pending_image(first, 5, _indexes())

        self.assertIs(duplicate, first)
        with self.assertRaises(FrozenInstanceError):
            first.image_ids = ()

    def test_remove_recalculates_remaining_point_union(self):
        indexes = _indexes()
        pending = pending_filters_from_image_ids(_token(), (2, 5), indexes)

        updated = remove_pending_image(pending, 5, indexes)

        self.assertEqual(updated.image_ids, (2,))
        self.assertEqual(
            updated.preview_point3d_ids,
            indexes.image_to_points[2],
        )
        self.assertIs(remove_pending_image(updated, 9, indexes), updated)

    def test_rebuild_from_image_ids_recomputes_preview_points(self):
        pending = pending_filters_from_image_ids(
            _token(),
            (2, 9),
            _indexes(),
        )

        self.assertEqual(pending.image_ids, (2, 9))
        self.assertEqual(pending.preview_point3d_ids, frozenset((100, 300)))

    def test_canonical_json_only_persists_token_and_image_ids(self):
        pending = pending_filters_from_image_ids(
            _token(),
            (2, 9),
            _indexes(),
        )

        serialized = serialize_pending_filters(pending)

        self.assertEqual(
            serialized,
            (
                '{"dataset_uuid":"12345678-1234-5678-1234-567812345678",'
                '"edit_revision":3,"history_hash":"'
                + HISTORY_HASH
                + '","image_ids":[2,9],"index_revision":3,'
                '"schema_version":1,"source_fingerprint":"'
                + SOURCE_FINGERPRINT
                + '"}'
            ),
        )
        self.assertNotIn("preview_point3d_ids", serialized)
        restored = deserialize_pending_filters(serialized, _indexes())
        self.assertEqual(restored, pending)

    def test_empty_string_represents_no_pending_filters(self):
        self.assertIsNone(deserialize_pending_filters("", _indexes()))
        self.assertIsNone(deserialize_pending_filters("{}", _indexes()))

    def test_schema_types_duplicates_and_negative_ids_are_rejected(self):
        valid = json.loads(
            serialize_pending_filters(
                pending_filters_from_image_ids(_token(), (2, 9), _indexes())
            )
        )
        invalid_values = []
        for field, value in (
            ("schema_version", 2),
            ("schema_version", True),
            ("dataset_uuid", "not-a-uuid"),
            ("source_fingerprint", "x"),
            ("history_hash", "y"),
            ("edit_revision", True),
            ("index_revision", -1),
            ("image_ids", [2, 2]),
            ("image_ids", [9, 2]),
            ("image_ids", [-1]),
            ("image_ids", ["2"]),
        ):
            damaged = dict(valid)
            damaged[field] = value
            invalid_values.append(damaged)
        extra = dict(valid)
        extra["unexpected"] = True
        invalid_values.append(extra)

        for damaged in invalid_values:
            with self.subTest(damaged=damaged):
                with self.assertRaises(ValueError):
                    deserialize_pending_filters(
                        json.dumps(damaged),
                        _indexes(),
                    )

    def test_missing_image_id_and_token_mismatch_are_rejected(self):
        with self.assertRaises(ValueError):
            pending_filters_from_image_ids(_token(), (7,), _indexes())

        with self.assertRaises(ValueError):
            validate_pending_token(
                _token(),
                dataset_uuid=DATASET_UUID,
                source_fingerprint=SOURCE_FINGERPRINT,
                edit_revision=4,
                index_revision=4,
                history_hash=HISTORY_HASH,
            )


if __name__ == "__main__":
    unittest.main()
