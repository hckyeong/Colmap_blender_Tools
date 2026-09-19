"""원본 면적 표본의 분포·겹침 판정·자원 수명을 합성 거리로 독립 검증합니다."""

from pathlib import Path
import errno
import sys
import tempfile
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools" / "align" / "alignment"))
from icp_cccore_contract import CcCoreCancelled, CcCoreGeometry, CcCoreInputError
from icp_cccore_overlap import build_surface_sampler


def rectangle(width=1., height=1., subdivisions=1):
    """같은 직사각형을 가로 방향으로 재삼각화한 원본 메시를 만듭니다."""
    vertices = np.array([(x, y, 0.) for x in np.linspace(0., width, subdivisions + 1)
                         for y in (0., height)])
    faces = np.array([(2*i, 2*i+2, 2*i+3) for i in range(subdivisions)]
                     + [(2*i, 2*i+3, 2*i+1) for i in range(subdivisions)], dtype=np.uint32)
    return CcCoreGeometry("TRIANGLE_MESH", vertices, faces, {})


class RecordedDistance:
    """단위 정사각형의 왼쪽 띠까지 거리를 기록하며 계산합니다."""

    def __init__(self, fraction=1.):
        """띠 폭과 거리 조회 이력을 초기화합니다."""
        self.fraction = fraction
        self.calls = []

    def __call__(self, points):
        """반경 여유 없이 지정한 띠만 합격시키는 정확한 평면 거리를 반환합니다."""
        self.calls.append(points.copy())
        return np.maximum(points[:, 0] - self.fraction, 0.)


class SurfaceSamplingTests(unittest.TestCase):
    """거리 색인과 독립적인 면적 표본 및 제한된 거리 배치를 검증합니다."""

    def test_unequal_face_areas(self):
        """면 개수가 아닌 실제 넓이 비율 1대 9로 삼각형을 선택합니다."""
        positions = np.array([[0., 0., 0.], [1., 0., 0.], [0., 1., 0.],
                              [10., 0., 0.], [13., 0., 0.], [10., 3., 0.]])
        geometry = CcCoreGeometry("TRIANGLE_MESH", positions, np.array([[0, 1, 2], [3, 4, 5]]), {})
        with TemporaryDirectory() as directory:
            with build_surface_sampler(geometry, (positions.min(0), positions.max(0)), .01, Path(directory)) as sampler:
                points = sampler.sample_full(60000, 1729)
                self.assertAlmostEqual(float(np.mean(points[:, 0] < 5.)), .1, delta=.005)

    def test_ambiguous_one_percent_interval(self):
        """최대 탐색에서도 신뢰구간이 정확히 1퍼센트를 걸치면 판정 불가로 거부합니다."""
        visited = [0]

        def query(points):
            """누적 제안 백 개마다 한 개를 합격시켜 경계 통계를 고정합니다."""
            indices = np.arange(visited[0], visited[0] + len(points))
            visited[0] += len(points)
            return np.where(indices % 100 == 0, 0., 1.)

        with TemporaryDirectory() as directory:
            with build_surface_sampler(rectangle(), (np.zeros(3), np.ones(3)), .01, Path(directory)) as sampler:
                with self.assertRaises(CcCoreInputError):
                    sampler.probe(query, valid_radius=.01, minimum_fraction=.01, seed=1729)
                self.assertEqual(visited[0], 131072)

    def test_safe_bounds_preserve_original_area_denominator(self):
        """경계 밖 큰 면을 제외해도 겹침 비율의 분모와 전체 표본은 원본을 사용합니다."""
        positions = np.array([[0., 0., 0.], [1., 0., 0.], [0., 1., 0.],
                              [10., 0., 0.], [13., 0., 0.], [10., 3., 0.]])
        geometry = CcCoreGeometry("TRIANGLE_MESH", positions, np.array([[0, 1, 2], [3, 4, 5]]), {})

        def query(points):
            """첫 삼각형 평면 패치와 멀리 떨어진 원본 면을 구분합니다."""
            return np.maximum(points[:, 0] - 1., 0.)

        with TemporaryDirectory() as directory:
            with build_surface_sampler(geometry, (np.zeros(3), np.array([1., 1., 0.])), .01, Path(directory)) as sampler:
                self.assertAlmostEqual(sampler.metadata["original_area"], 5.)
                self.assertAlmostEqual(sampler.metadata["eligible_area"], .5)
                self.assertEqual(sampler.metadata["index_bytes"], 32)
                diagnostic = sampler.probe(query, valid_radius=.01, minimum_fraction=.01, seed=1729)
                self.assertAlmostEqual(diagnostic["estimated_overlap_fraction"], .1)
                self.assertGreater(np.mean(sampler.sample_full(10000, 11)[:, 0] > 5.), .88)
                samples = sampler.sample(1000, 12, query, radius=.01, max_proposals=200000)
                self.assertTrue(np.all(samples.source_indices == 0))

    def test_uniform_area_and_retriangulation(self):
        """재삼각화와 얇은 형상에서도 표본의 면적 분포와 분산을 보존합니다."""
        for height in (1., .0001):
            for subdivisions in (1, 37):
                geometry = rectangle(height=height, subdivisions=subdivisions)
                with self.subTest(height=height, subdivisions=subdivisions), TemporaryDirectory() as directory:
                    with build_surface_sampler(geometry, (np.zeros(3), np.ones(3)), .01, Path(directory)) as sampler:
                        points = sampler.sample_full(60000, 1729)
                        scaled = points[:, :2] / np.array([1., height])
                        np.testing.assert_allclose(scaled.mean(axis=0), [.5, .5], atol=.006)
                        np.testing.assert_allclose(scaled.var(axis=0), [1/12, 1/12], atol=.002)
                        histogram, _ = np.histogram(scaled[:, 0], bins=10, range=(0., 1.))
                        self.assertLess(np.max(np.abs(histogram / len(points) - .1)), .005)
                    self.assertEqual(list(Path(directory).iterdir()), [])

    def test_large_face_interior_and_repeated_rejection(self):
        """정점이 멀리 있는 큰 면 내부에서 추가 제안으로 목표 표본을 확보합니다."""
        geometry = CcCoreGeometry("TRIANGLE_MESH", np.array([[-10., -10., 0.], [30., -10., 0.], [-10., 20., 0.]]),
                                  np.array([[0, 1, 2]], dtype=np.uint32), {})
        original = geometry.positions.copy()
        calls = []

        def query(points):
            """큰 삼각형 내부의 단위 원까지 정확한 거리를 기록합니다."""
            calls.append(len(points))
            return np.linalg.norm(points, axis=1)

        with TemporaryDirectory() as directory:
            with build_surface_sampler(geometry, (np.zeros(3), np.zeros(3)), 1., Path(directory)) as sampler:
                self.assertEqual(calls, [])
                samples = sampler.sample(1000, 7, query, radius=1., max_proposals=600000)
                self.assertEqual(samples.positions.shape, (1000, 3))
                self.assertTrue(np.all(samples.distances <= 1.))
                self.assertTrue(np.all(samples.source_indices == 0))
                self.assertGreater(len(calls), 1)
                self.assertLessEqual(max(calls), 8192)
                self.assertEqual(len(np.unique(samples.positions, axis=0)), 1000)
            self.assertEqual(list(Path(directory).iterdir()), [])
        np.testing.assert_array_equal(geometry.positions, original)

    def test_probe_overlap_and_independent_samples(self):
        """충분한 겹침은 통과하고 탐색·평가 시드가 서로 다른 좌표를 만듭니다."""
        for fraction in (.05, .2, 1.):
            with self.subTest(fraction=fraction), TemporaryDirectory() as directory:
                query = RecordedDistance(fraction)
                with build_surface_sampler(rectangle(), (np.zeros(3), np.ones(3)), 1e-9, Path(directory)) as sampler:
                    diagnostic = sampler.probe(query, valid_radius=1e-9, minimum_fraction=.01, seed=1729)
                    self.assertAlmostEqual(diagnostic["estimated_overlap_fraction"], fraction, delta=.02)
                    probe_points = np.concatenate(query.calls)
                    first = sampler.sample(1000, 1730, query, radius=1e-9, max_proposals=200000)
                    second = sampler.sample(1000, 1731, query, radius=1e-9, max_proposals=200000)
                    again = sampler.sample(1000, 1730, query, radius=1e-9, max_proposals=200000)
                    self.assertEqual(first.fingerprint, again.fingerprint)
                    self.assertNotEqual(first.fingerprint, second.fingerprint)
                    self.assertFalse(np.any(np.all(probe_points == first.positions[0], axis=1)))
                    self.assertTrue(np.all(first.positions[:, 0] <= fraction + 1e-9))
                    self.assertLessEqual(max(map(len, query.calls)), 8192)

    def test_below_threshold_and_no_overlap(self):
        """겹침이 없거나 최소 비율보다 명백히 작은 경우를 거부합니다."""
        for fraction in (0., .005):
            with self.subTest(fraction=fraction), TemporaryDirectory() as directory:
                with build_surface_sampler(rectangle(), (np.zeros(3), np.ones(3)), 1e-12, Path(directory)) as sampler:
                    with self.assertRaises(CcCoreInputError):
                        sampler.probe(RecordedDistance(fraction), valid_radius=1e-12, minimum_fraction=.01, seed=1729)

    def test_query_failure_and_nan_cleanup(self):
        """거리 조회 예외와 비유한 거리에서 중단하고 메모리 매핑 파일을 정리합니다."""
        for failure in (False, True):
            with self.subTest(failure=failure), TemporaryDirectory() as directory:
                def query(points):
                    """검증용 오류 또는 비유한 거리 배열을 반환합니다."""
                    if failure:
                        raise RuntimeError("검증용 거리 실패")
                    return np.full(len(points), np.nan)

                with self.assertRaises((RuntimeError, CcCoreInputError)):
                    with build_surface_sampler(rectangle(), (np.zeros(3), np.ones(3)), .01, Path(directory)) as sampler:
                        sampler.sample(100, 7, query, radius=.01, max_proposals=20000)
                self.assertEqual(list(Path(directory).iterdir()), [])

    def test_cancel_and_source_immutability(self):
        """표본 생성 도중 취소가 전파되고 원본과 호출자 파일은 보존됩니다."""
        geometry = rectangle(subdivisions=13)
        original_positions, original_faces = geometry.positions.copy(), geometry.triangles.copy()
        cancelled = [False]
        with TemporaryDirectory() as directory:
            keep = Path(directory) / "보존.txt"
            keep.write_text("호출자 파일", encoding="utf-8")
            with self.assertRaises(CcCoreCancelled):
                with build_surface_sampler(geometry, (np.zeros(3), np.ones(3)), .01, Path(directory),
                                           cancel_probe=lambda: cancelled[0]) as sampler:
                    cancelled[0] = True
                    sampler.sample_full(100, 1729)
            self.assertEqual(list(Path(directory).iterdir()), [keep])
        np.testing.assert_array_equal(geometry.positions, original_positions)
        np.testing.assert_array_equal(geometry.triangles, original_faces)

    def test_partial_cleanup_failure_retries_only_remaining_file(self):
        """한 파일 삭제가 실패해도 다른 색인을 정리하고 남은 파일을 재시도합니다."""
        with TemporaryDirectory() as directory:
            sampler = build_surface_sampler(rectangle(), (np.zeros(3), np.ones(3)), .01, Path(directory))
            paths = list(Path(directory).iterdir())
            self.assertEqual(len(paths), 2)
            unlink = Path.unlink

            def fail_one(path, *args, **kwargs):
                """첫 번째 색인에만 일시적인 파일 삭제 오류를 주입합니다."""
                if path == paths[0]:
                    raise PermissionError("검증용 삭제 실패")
                return unlink(path, *args, **kwargs)

            with patch.object(Path, "unlink", fail_one):
                with self.assertRaises(CcCoreInputError):
                    sampler.close()
            self.assertEqual(list(Path(directory).iterdir()), [paths[0]])
            sampler.close()
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_cleanup_failure_preserves_original_exception(self):
        """정리까지 실패하는 경우 최초 계산 예외의 동일성과 부가 설명을 보존합니다."""
        with TemporaryDirectory() as directory:
            sampler = build_surface_sampler(rectangle(), (np.zeros(3), np.ones(3)), .01, Path(directory))
            original = RuntimeError("검증용 최초 계산 오류")
            with patch.object(Path, "unlink", side_effect=PermissionError("검증용 삭제 실패")):
                with self.assertRaises(RuntimeError) as captured:
                    with sampler:
                        raise original
            self.assertIs(captured.exception, original)
            self.assertTrue(original.__notes__)
            sampler.close()
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_strict_seed_and_radius_validation(self):
        """불리언과 음수·비정수 시드 및 불리언 반경을 입력 오류로 거부합니다."""
        with TemporaryDirectory() as directory:
            with build_surface_sampler(rectangle(), (np.zeros(3), np.ones(3)), .01, Path(directory)) as sampler:
                for seed in (True, np.bool_(False), -1, 1.5, "7"):
                    with self.subTest(seed=seed), self.assertRaises(CcCoreInputError):
                        sampler.sample_full(10, seed)
                for radius in (True, np.bool_(True)):
                    with self.subTest(radius=radius), self.assertRaises(CcCoreInputError):
                        sampler.sample(10, 7, RecordedDistance(), radius=radius, max_proposals=100)
                for fraction in (True, np.bool_(True), "0.01"):
                    with self.subTest(fraction=fraction), self.assertRaises(CcCoreInputError):
                        sampler.probe(RecordedDistance(), valid_radius=.01, minimum_fraction=fraction, seed=7)

    def test_second_index_disk_failure_cleans_first_resource(self):
        """두 번째 색인의 생성·확장 실패에서 최초 운영체제 오류와 선행 자원 정리를 확인합니다."""
        for failure_stage in ("create", "expand"):
            with self.subTest(stage=failure_stage), TemporaryDirectory() as directory:
                keep = Path(directory) / "보존.txt"
                keep.write_text("호출자 파일", encoding="utf-8")
                original_error = OSError(errno.ENOSPC, "검증용 디스크 공간 부족")
                original_create, original_map = tempfile.mkstemp, np.memmap
                opened = []
                created_count = [0]

                def create_index(*args, **kwargs):
                    """두 번째 임시 파일 생성 단계에 디스크 부족 오류를 주입합니다."""
                    created_count[0] += 1
                    if failure_stage == "create" and created_count[0] == 2:
                        raise original_error
                    return original_create(*args, **kwargs)

                def map_index(*args, **kwargs):
                    """첫 실제 매핑은 기록하고 두 번째 파일 확장 단계만 실패시킵니다."""
                    if failure_stage == "expand" and len(opened) == 1:
                        raise original_error
                    mapped = original_map(*args, **kwargs)
                    opened.append(mapped)
                    return mapped

                with patch.object(tempfile, "mkstemp", create_index), patch.object(np, "memmap", map_index):
                    with self.assertRaises(CcCoreInputError) as captured:
                        build_surface_sampler(rectangle(), (np.zeros(3), np.ones(3)), .01, Path(directory))
                self.assertIs(captured.exception.__cause__, original_error)
                self.assertEqual(len(opened), 1)
                self.assertTrue(opened[0]._mmap.closed)
                self.assertEqual(list(Path(directory).iterdir()), [keep])

    def test_unrepresentable_surface_area_is_rejected(self):
        """양수 면적의 누적 소실과 유한 좌표의 면적 오버플로를 조용히 편향시키지 않습니다."""
        for edge in (1e9, 1e200):
            with self.subTest(edge=edge), TemporaryDirectory() as directory:
                positions = np.array([[0., 0., 0.], [edge, 0., 0.], [0., edge, 0.],
                                      [0., 0., 1.], [1., 0., 1.], [0., 1., 1.]])
                geometry = CcCoreGeometry("TRIANGLE_MESH", positions, np.array([[0, 1, 2], [3, 4, 5]]), {})
                for batch_size in (1, 8192):
                    with self.subTest(batch_size=batch_size), np.errstate(over="ignore", invalid="ignore"):
                        with self.assertRaises(CcCoreInputError):
                            build_surface_sampler(geometry, (positions.min(0), positions.max(0)), .01,
                                                  Path(directory), batch_size=batch_size)
                    self.assertEqual(list(Path(directory).iterdir()), [])

    def test_seed_reproducibility_across_batch_sizes(self):
        """묶음 크기를 바꿔도 같은 시드의 전체 표본과 거절 후 합격 순서가 같습니다."""
        geometry = rectangle(subdivisions=8)
        results = []
        for batch_size in (13, 8192):
            with TemporaryDirectory() as directory:
                with build_surface_sampler(geometry, (np.zeros(3), np.ones(3)), 1e-9, Path(directory),
                                           batch_size=batch_size) as sampler:
                    full = sampler.sample_full(3000, 1729)
                    accepted = sampler.sample(1000, 1730, RecordedDistance(.2), radius=1e-9,
                                              max_proposals=200000)
                    results.append((full, accepted))
        np.testing.assert_array_equal(results[0][0], results[1][0])
        np.testing.assert_array_equal(results[0][1].positions, results[1][1].positions)
        np.testing.assert_array_equal(results[0][1].source_indices, results[1][1].source_indices)
        np.testing.assert_array_equal(results[0][1].distances, results[1][1].distances)
        self.assertEqual(results[0][1].fingerprint, results[1][1].fingerprint)


if __name__ == "__main__":
    unittest.main()
