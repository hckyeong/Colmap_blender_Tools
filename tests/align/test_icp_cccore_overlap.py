"""정확한 합성 거리로 겹침 후보 보존·표본 예산·결정성을 검증합니다."""

from pathlib import Path
import sys
import unittest

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools" / "align" / "alignment"))
from icp_cccore_contract import CcCoreCancelled, CcCoreGeometry, CcCoreInputError
from icp_cccore_overlap import build_overlap_domain, sample_overlap_domain


def point_distance(points):
    """원점 대상의 정확한 거리를 반환합니다."""
    return np.linalg.norm(points, axis=1)


def large_face():
    """모든 정점과 중심은 멀지만 원점을 덮는 큰 면을 만듭니다."""
    return CcCoreGeometry("TRIANGLE_MESH", np.array([[-10., -10., 0.], [30., -10., 0.], [-10., 20., 0.]]),
                          np.array([[0, 1, 2]], dtype=np.uint32), {"model_id": "원본"})


class OverlapTests(unittest.TestCase):
    """원본 출처와 합격 영역의 핵심 계약을 검사합니다."""

    def test_large_face_and_fifty_thousand(self):
        """큰 경계 면을 보존하고 반경 안의 서로 다른 5만 점을 확보합니다."""
        geometry = large_face()
        original = geometry.positions.copy()
        self.assertGreater(float(point_distance(geometry.positions.mean(axis=0)[None])[0]), 1.)
        self.assertTrue(np.all(point_distance(geometry.positions) > 1.))
        domain = build_overlap_domain(geometry, (np.zeros(3), np.zeros(3)), 1., point_distance)
        first = sample_overlap_domain(domain, 50000, 1729, point_distance)
        second = sample_overlap_domain(domain, 50000, 1729, point_distance)
        self.assertEqual(first.positions.shape, (50000, 3))
        self.assertEqual(len(np.unique(first.positions, axis=0)), 50000)
        self.assertTrue(np.all(first.distances <= 1))
        self.assertTrue(np.all(first.source_indices == 0))
        self.assertEqual(first.fingerprint, second.fingerprint)
        self.assertLess(domain.metadata["candidate_area_fraction"], .03)
        np.testing.assert_array_equal(geometry.positions, original)

    def test_narrow_independent_evaluation(self):
        """별도 시드의 좁은 평가 표본도 같은 후보의 실제 거리로 제한합니다."""
        domain = build_overlap_domain(large_face(), (np.zeros(3), np.zeros(3)), 1., point_distance)
        samples = sample_overlap_domain(domain, 1000, 7, point_distance, radius=.5)
        self.assertTrue(np.all(samples.distances <= .5))

    def test_point_uniqueness_and_spatial_selection(self):
        """점군은 원본 점을 복제하지 않고 공간 구간을 고려해 결정적으로 선택합니다."""
        points = np.array([(x, y, 0.) for x in range(10) for y in range(10)], dtype=float)
        geometry = CcCoreGeometry("COLMAP_POINT_CLOUD", points, None, {})
        domain = build_overlap_domain(geometry, (points.min(axis=0), points.max(axis=0)), 20., point_distance)
        all_samples = sample_overlap_domain(domain, 50000, 10, point_distance)
        chosen = sample_overlap_domain(domain, 20, 10, point_distance)
        again = sample_overlap_domain(domain, 20, 10, point_distance)
        self.assertEqual(len(all_samples.positions), len(points))
        self.assertEqual(len(np.unique(chosen.source_indices)), 20)
        self.assertEqual(chosen.fingerprint, again.fingerprint)
        np.testing.assert_array_equal(chosen.positions, points[chosen.source_indices])
        self.assertGreaterEqual(chosen.metadata["sample_spatial_distribution"]["occupied_spatial_cells"], 16)

    def test_no_candidate_cancel_and_limits(self):
        """빈 후보·취소·잘못된 예산과 표본 부족을 명확히 중단합니다."""
        with self.assertRaises(CcCoreCancelled):
            build_overlap_domain(large_face(), (np.zeros(3), np.zeros(3)), 1., point_distance, cancel_probe=lambda: True)
        with self.assertRaises(CcCoreInputError):
            build_overlap_domain(large_face(), (np.full(3, 100.), np.full(3, 101.)), 1., point_distance)
        with self.assertRaises(CcCoreInputError):
            build_overlap_domain(large_face(), (np.zeros(3), np.zeros(3)), 1., point_distance, batch_size=0)
        domain = build_overlap_domain(large_face(), (np.zeros(3), np.zeros(3)), 1., point_distance,
                                      max_subdivision_depth=0, max_candidate_patches=1)
        self.assertEqual(domain.metadata["limited_mixed_patch_count"], 1)
        with self.assertRaises(CcCoreInputError):
            sample_overlap_domain(domain, 500, 123, point_distance, max_proposals_factor=1)
        with self.assertRaises(CcCoreInputError):
            sample_overlap_domain(domain, 500, 123, point_distance, radius=2.)

    def test_distance_contract(self):
        """잘못된 거리 배열을 후보 판단에 사용하지 못하게 합니다."""
        with self.assertRaises(CcCoreInputError):
            build_overlap_domain(large_face(), (np.zeros(3), np.zeros(3)), 1., lambda points: np.full(len(points), np.nan))

    def test_area_weighting_source_indices_and_patch_cap(self):
        """면적 비례 표본의 원본 면 ID와 추가 후보 조각 상한을 검사합니다."""
        points = np.array([[0., 0., 0.], [1., 0., 0.], [0., 1., 0.],
                           [3., 0., 0.], [5., 0., 0.], [3., 2., 0.]])
        geometry = CcCoreGeometry("TRIANGLE_MESH", points, np.array([[0, 1, 2], [3, 4, 5]]), {})
        bounds = (points.min(axis=0), points.max(axis=0))
        domain = build_overlap_domain(geometry, bounds, 100., point_distance)
        result = sample_overlap_domain(domain, 6000, 6, point_distance)
        self.assertAlmostEqual(float(np.mean(result.source_indices == 1)), .8, delta=.025)
        self.assertEqual(domain.metadata["candidate_area_fraction"], 1.)
        limited = build_overlap_domain(geometry, bounds, 100., point_distance, max_candidate_patches=1)
        self.assertEqual(limited.metadata["candidate_source_face_count"], 2)

    def test_original_face_count_exceeds_subdivision_budget(self):
        """25만 개가 넘는 원본 면도 추가 사분할 예산과 독립적으로 처리합니다."""
        geometry = large_face()
        many = CcCoreGeometry(geometry.kind, geometry.positions, np.tile(geometry.triangles, (250001, 1)), {})
        domain = build_overlap_domain(many, (np.zeros(3), np.zeros(3)), 1., point_distance,
                                      max_candidate_patches=1)
        self.assertEqual(domain.metadata["candidate_source_face_count"], 250001)
        self.assertEqual(domain.metadata["additional_subdivision_patch_count"], 0)

    def test_duplicate_point_coordinates_keep_first_source(self):
        """중복 좌표 5만 개를 저장해도 최초 원본 ID의 실제 고유 점만 채택합니다."""
        unique = np.array([[1., 1., 0.], [0., 0., 0.], [1., 0., 0.], [0., 1., 0.]])
        points = np.tile(unique, (12500, 1))
        geometry = CcCoreGeometry("COLMAP_POINT_CLOUD", points, None, {})
        domain = build_overlap_domain(geometry, (np.zeros(3), np.ones(3)), 2., point_distance)
        result = sample_overlap_domain(domain, 50000, 77, point_distance)
        again = sample_overlap_domain(domain, 50000, 77, point_distance)
        np.testing.assert_array_equal(result.source_indices, np.arange(4))
        np.testing.assert_array_equal(result.positions, unique)
        np.testing.assert_array_equal(result.distances, point_distance(unique))
        self.assertEqual(result.fingerprint, again.fingerprint)
        self.assertEqual(result.metadata["sample_count"], 4)
        self.assertEqual(result.metadata["candidate_point_count"], 4)
        self.assertEqual(result.metadata["original_duplicate_point_count"], 49996)
        self.assertEqual(result.metadata["candidate_fraction"], 1.)
        self.assertEqual(sum(result.metadata["spatial_cell_counts"]), 4)


if __name__ == "__main__":
    unittest.main()
