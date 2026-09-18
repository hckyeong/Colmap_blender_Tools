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


class PlaneDistanceRecorder:
    """수평면까지의 실제 거리와 조회 좌표를 함께 기록합니다."""

    def __init__(self):
        """각 거리 조회의 독립적인 좌표 기록을 준비합니다."""
        self.calls = []

    def __call__(self, points):
        """좌표를 기록하고 수평면까지의 비음수 거리를 반환합니다."""
        self.calls.append(points.copy())
        return np.abs(points[:, 2])


def mesh_from_patches(patches):
    """주어진 순서의 삼각형마다 독립적인 원본 면 ID를 부여합니다."""
    positions = np.asarray(patches, dtype=float).reshape(-1, 3)
    return CcCoreGeometry("TRIANGLE_MESH", positions,
                          np.arange(len(positions)).reshape(-1, 3), {})


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
        for value in (np.nan, np.inf, -1.):
            with self.subTest(value=value), self.assertRaises(CcCoreInputError):
                build_overlap_domain(large_face(), (np.zeros(3), np.zeros(3)), 1.,
                                     lambda points: np.full(len(points), value))
        with self.assertRaises(CcCoreInputError):
            build_overlap_domain(large_face(), (np.zeros(3), np.zeros(3)), 1.,
                                 lambda points: np.zeros((len(points), 1)))

    def test_initial_inside_outside_and_mixed_distance_calls(self):
        """최초 중심 조회 한 번으로 내부·외부·분할 제한 면을 분류합니다."""
        small = np.array([[0., 0., 0.], [.5, 0., 0.], [0., .5, 0.]])
        cases = ((small, False, 0), (small + [0., 0., 3.], True, 0),
                 (small * 4, False, 1))
        for patch, rejected, limited in cases:
            with self.subTest(rejected=rejected, limited=limited):
                query = PlaneDistanceRecorder()
                geometry = mesh_from_patches([patch])
                if rejected:
                    with self.assertRaises(CcCoreInputError):
                        build_overlap_domain(geometry, (np.full(3, -10.), np.full(3, 10.)),
                                             1., query, max_subdivision_depth=0)
                else:
                    domain = build_overlap_domain(geometry, (np.full(3, -10.), np.full(3, 10.)),
                                                  1., query, max_subdivision_depth=0)
                    np.testing.assert_array_equal(domain.patches, [patch])
                    self.assertEqual(domain.metadata["limited_mixed_patch_count"], limited)
                self.assertEqual(len(query.calls), 1)
                np.testing.assert_array_equal(query.calls[0], patch.mean(axis=0)[None])

    def test_batch_boundary_bbox_filter_and_original_face_ids(self):
        """8192면 경계의 경계상자 탈락과 중복 좌표에도 원본 ID를 유지합니다."""
        small = np.array([[0., 0., 0.], [.5, 0., 0.], [0., .5, 0.]])
        patches = np.tile(small, (8194, 1, 1))
        patches[[0, 8191, 8192]] += [100., 0., 0.]
        patches[[1, 8193]] *= 4
        patches[4] += [0., 0., 3.]
        geometry = mesh_from_patches(patches)
        query = PlaneDistanceRecorder()
        domain = build_overlap_domain(geometry, (np.zeros(3), np.array([1., 1., 10.])),
                                      1., query, max_candidate_patches=1)
        expected_ids = np.r_[2, 3, np.arange(5, 8191), 1, 8193]
        queried_ids = np.r_[np.arange(1, 8191), 8193]
        self.assertEqual([len(call) for call in query.calls], [8190, 1])
        np.testing.assert_array_equal(domain.source_indices, expected_ids)
        np.testing.assert_array_equal(domain.patches, patches[expected_ids])
        np.testing.assert_array_equal(np.concatenate(query.calls), patches[queried_ids].mean(axis=1))
        self.assertEqual(domain.metadata["initial_candidate_face_count"], 8190)
        self.assertEqual(domain.metadata["limited_mixed_patch_count"], 2)

    def test_child_distance_contract(self):
        """재사용 이후의 사분할 조회도 잘못된 거리와 배열 형태를 거부합니다."""
        for invalid in (np.nan, np.inf, -1., "잘못된 형태"):
            calls = 0

            def query(points):
                """첫 조회는 실제 거리로 처리하고 후속 조회에 계약 위반을 주입합니다."""
                nonlocal calls
                calls += 1
                if calls == 1:
                    return np.abs(points[:, 2])
                if isinstance(invalid, str):
                    return np.zeros((len(points), 1))
                return np.full(len(points), invalid)

            with self.subTest(invalid=invalid), self.assertRaises(CcCoreInputError):
                build_overlap_domain(large_face(), (np.zeros(3), np.zeros(3)), 1., query)
            self.assertEqual(calls, 2)

    def test_child_queries_patch_order_and_sample_coordinates(self):
        """분할 중심과 실제 표본은 재조회하고 내부 면 우선 순서와 좌표를 보존합니다."""
        patches = np.array([[[0., 0., 0.], [2., 0., 0.], [0., 2., 0.]],
                            [[4., 0., 0.], [4.5, 0., 0.], [4., .5, 0.]],
                            [[0., 0., 3.], [.5, 0., 3.], [0., .5, 3.]],
                            [[4., 0., 0.], [4.5, 0., 0.], [4., .5, 0.]]])
        children = np.array([[[0., 0., 0.], [1., 0., 0.], [0., 1., 0.]],
                             [[1., 0., 0.], [2., 0., 0.], [1., 1., 0.]],
                             [[0., 1., 0.], [1., 1., 0.], [0., 2., 0.]],
                             [[1., 0., 0.], [1., 1., 0.], [0., 1., 0.]]])
        query = PlaneDistanceRecorder()
        domain = build_overlap_domain(mesh_from_patches(patches),
                                      (np.full(3, -10.), np.full(3, 10.)), 1., query,
                                      max_subdivision_depth=1)
        self.assertEqual(len(query.calls), 2)
        np.testing.assert_array_equal(query.calls[0], patches.mean(axis=1))
        np.testing.assert_array_equal(query.calls[1], children.mean(axis=1))
        np.testing.assert_array_equal(domain.patches, np.concatenate((patches[[1, 3]], children)))
        np.testing.assert_array_equal(domain.source_indices, [1, 3, 0, 0, 0, 0])
        np.testing.assert_array_equal(domain.patch_areas, [.125, .125, .5, .5, .5, .5])
        self.assertEqual(domain.metadata["additional_subdivision_patch_count"], 3)
        self.assertEqual(domain.metadata["limited_mixed_patch_count"], 0)
        samples = sample_overlap_domain(domain, 8, 3, query)
        # 고정 시드의 기존 표본을 상수로 보관해 표본 생성 코드를 복제하지 않습니다.
        expected = np.array([[4.04740428202112, .06773658263867599, 0.],
                             [.09682100212174646, .32020579484493966, 0.],
                             [.47876827603246896, .9683135002296065, 0.],
                             [.08860750646667119, 1.0060839569840314, 0.],
                             [4.049225344787795, .2731494076042086, 0.],
                             [1.160993360496545, .3255936539934958, 0.],
                             [1.4678300717978807, .16880784894194834, 0.],
                             [.5803594345734728, .25862155926797703, 0.]])
        np.testing.assert_allclose(samples.positions, expected, rtol=0, atol=1e-14)
        np.testing.assert_array_equal(samples.source_indices, [3, 0, 0, 0, 3, 0, 0, 0])
        np.testing.assert_array_equal(samples.distances, np.zeros(8))
        self.assertEqual([len(call) for call in query.calls], [4, 4, 160])
        np.testing.assert_array_equal(query.calls[-1][:8], samples.positions)

    def test_global_split_cap_keeps_later_mixed_faces(self):
        """전역 추가 예산은 첫 혼합 면에만 사용하고 나머지 원본 면을 보존합니다."""
        patch = np.array([[0., 0., 0.], [4., 0., 0.], [0., 4., 0.]])
        query = PlaneDistanceRecorder()
        domain = build_overlap_domain(mesh_from_patches([patch, patch]),
                                      (np.zeros(3), np.full(3, 4.)), 1., query,
                                      max_subdivision_depth=4, max_candidate_patches=3)
        self.assertEqual([len(call) for call in query.calls], [2, 4])
        np.testing.assert_array_equal(domain.source_indices, [1, 0, 0, 0, 0])
        np.testing.assert_array_equal(domain.patches[0], patch)
        self.assertEqual(domain.metadata["additional_subdivision_patch_count"], 3)
        self.assertEqual(domain.metadata["limited_mixed_patch_count"], 5)

    def test_cancel_between_initial_query_and_classification(self):
        """최초 조회 직후와 첫 분류 경계에서 취소를 계속 확인합니다."""
        for cancel_after in (1, 2):
            query = PlaneDistanceRecorder()
            checks_after_query = 0

            def cancel():
                """최초 조회가 끝난 뒤 지정한 취소 확인 지점에서 중단합니다."""
                nonlocal checks_after_query
                if query.calls:
                    checks_after_query += 1
                return checks_after_query >= cancel_after

            with self.subTest(cancel_after=cancel_after), self.assertRaises(CcCoreCancelled):
                build_overlap_domain(large_face(), (np.zeros(3), np.zeros(3)), 1., query,
                                     cancel_probe=cancel)
            self.assertEqual(len(query.calls), 1)

    def test_cancel_after_child_distance_query(self):
        """사분할 중심의 후속 조회가 끝난 직후에도 취소를 전달합니다."""
        query = PlaneDistanceRecorder()
        with self.assertRaises(CcCoreCancelled):
            build_overlap_domain(large_face(), (np.zeros(3), np.zeros(3)), 1., query,
                                 cancel_probe=lambda: len(query.calls) >= 2)
        self.assertEqual([len(call) for call in query.calls], [1, 4])

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
