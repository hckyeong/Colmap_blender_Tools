"""중첩 등록 표본의 재현성·공간 분포·원본 순서 보존을 검사합니다."""

import unittest
from pathlib import Path
import sys

import numpy as np

# 격리된 내장 Python에서도 같은 폴더의 검증 도구를 명시적으로 불러옵니다.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from prepare_nested_icp_samples import spatial_nested_order


class NestedIcpSamplesTests(unittest.TestCase):
    """계산 엔진을 실행하지 않고 실험용 표본 선택만 검증합니다."""

    def test_reproducible_nested_rows_cover_spatial_groups(self):
        """공간순으로 정렬된 입력에서도 앞부분에 편향되지 않는 중첩 집합을 만듭니다."""
        coordinates = np.linspace(-1, 1, 16)
        points = np.stack(np.meshgrid(coordinates, coordinates, coordinates), axis=-1).reshape(-1, 3)
        order = spatial_nested_order(points, 1729)
        np.testing.assert_array_equal(order, spatial_nested_order(points, 1729))
        np.testing.assert_array_equal(np.sort(order), np.arange(len(points)))
        self.assertFalse(np.array_equal(order, spatial_nested_order(points, 1730)))
        previous = np.empty(0, dtype=np.int64)
        for count in (400, 1600, 4096):
            rows = np.sort(order[:count])
            self.assertTrue(np.isin(previous, rows).all())
            self.assertTrue(np.all(np.diff(rows) > 0))
            selected = points[rows]
            self.assertEqual(len(np.unique(selected > 0, axis=0)), 8)
            self.assertTrue(np.all(np.ptp(selected, axis=0) > 1.8))
            self.assertTrue(np.all(np.abs(selected.mean(axis=0) - points.mean(axis=0)) < 0.06))
            previous = rows
        np.testing.assert_array_equal(points[np.sort(order)], points)

    def test_duplicate_positions_keep_distinct_source_rows(self):
        """좌표가 중복되어도 원본 표본 ID를 병합하거나 임의 복제하지 않습니다."""
        points = np.zeros((127, 3))
        order = spatial_nested_order(points, 1729)
        np.testing.assert_array_equal(np.sort(order), np.arange(127))


if __name__ == "__main__":
    unittest.main()
