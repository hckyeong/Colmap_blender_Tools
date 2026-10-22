"""공통 점 배열의 64비트 식별자와 지연 순회 계약을 검증합니다."""
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np

from shared.colmap_io import PointTable
from tools.projection_paint.core.colmap.shared_reader import PointRecordMapping
from tools.projection_paint.core.colmap.validation import validate_point3d_records_steps
from tools.projection_paint.core.colmap.fingerprint import semantic_fingerprint
from tools.projection_paint.core.colmap.model import DEFAULT_LIMITS


def point_mapping():
    """부호 경계에 인접하고 원본 순서가 섞인 점 배열을 만듭니다."""
    ids = np.array([(1 << 63) - 1, (1 << 63) - 2, (1 << 64) - 2, 1 << 63], dtype=np.uint64)
    count = len(ids)
    table = PointTable(
        point_ids=ids,
        positions=np.zeros((count, 3), dtype=np.float64),
        colors=np.zeros((count, 3), dtype=np.uint8),
        errors=np.zeros(count, dtype=np.float64),
        track_offsets=np.zeros(count + 1, dtype=np.uint64),
        track_image_ids=np.zeros(0, dtype=np.uint64),
        track_point2d_indices=np.zeros(0, dtype=np.uint32),
        display_indices=np.arange(count, dtype=np.uint32),
    )
    return PointRecordMapping(table)


class SharedPointViewTests(unittest.TestCase):
    """대형 점 보기의 정밀도와 첫 증분 단계 비용을 검증합니다."""

    def test_uint64_boundary_keys_remain_exact(self):
        """2의 63승 인접 식별자가 실수 변환 없이 정확히 조회됩니다."""
        mapping = point_mapping()
        for key in mapping.table.point_ids:
            self.assertEqual(int(key), mapping[int(key)].point3d_id)
            self.assertEqual(int(key), mapping[key].point3d_id)
        for key in (-1, 1 << 64, float(1 << 63), (1 << 63) - 3):
            with self.assertRaises(KeyError):
                mapping[key]

    def test_first_validation_yield_does_not_iterate_points(self):
        """첫 진행률은 점 레코드 전체 변환 없이 배열 트랙 총량으로 계산됩니다."""
        mapping = point_mapping()
        with patch.object(mapping, "values", side_effect=AssertionError("전체 점 순회")):
            progress = validate_point3d_records_steps(mapping, DEFAULT_LIMITS)
            self.assertEqual((0, len(mapping)), next(progress))
            progress.close()

    def test_fingerprint_uses_sorted_values_without_key_list(self):
        """배열 지문 계산은 키 목록 복사 없이 정렬된 값 보기를 순회합니다."""
        mapping = point_mapping()
        dataset = SimpleNamespace(layout=SimpleNamespace(input_format="binary"), cameras={}, images={}, points3d=mapping)
        with patch.object(PointRecordMapping, "__iter__", side_effect=AssertionError("키 목록 복사")):
            fingerprint = semantic_fingerprint(dataset)
        self.assertEqual(64, len(fingerprint))


if __name__ == "__main__":
    unittest.main()
