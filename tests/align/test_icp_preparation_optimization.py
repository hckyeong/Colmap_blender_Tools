"""전처리의 판정·표본·경계·취소 계약이 반복 계산 제거 전과 같은지 검증합니다."""
from pathlib import Path
import os
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest import mock

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'tools/align/alignment'))
import icp_cccore_contract as contract
import icp_cccore_overlap as overlap
import icp_cccore_solver as solver


def original_candidate_mask(patches, areas, lower, upper, maximum, minimum):
    """변경 전 판정식을 독립 기준으로 유지하여 경계와 표본을 비교합니다."""
    return (areas > 0) & np.all(patches.max(axis=1) >= lower, axis=1) & np.all(patches.min(axis=1) <= upper, axis=1)


def cancel_runner(directory, probe=None):
    """네이티브 계산 없이 파일·콜백 취소 경계를 검사할 최소 러너를 만듭니다."""
    runner = object.__new__(solver.NativeRunner)
    runner.directory = Path(directory).resolve(strict=True)
    runner._cancel_path = runner.directory / 'cancel.request'
    runner.cancel_probe = probe
    return runner


class PreparationOptimizationTests(unittest.TestCase):
    """실제 입력과 별도로 퇴화·경계·취소 안전성의 회귀를 검증합니다."""

    def test_candidate_boundary_conditions_and_short_final_batch(self):
        """경계 접촉·바로 안팎·영 면적과 마지막 짧은 묶음의 판정을 보존합니다."""
        rng = np.random.default_rng(6543)
        patches = rng.uniform(-3, 3, (8195, 3, 3))
        patches[:8] = np.array([0., -0., 1., -1., np.nextafter(1., 0.),
                                np.nextafter(1., 2.), np.nextafter(-1., 0.),
                                np.nextafter(-1., -2.)])[:, None, None]
        areas = rng.choice([0., 1., 2.], len(patches))
        lower, upper = np.full(3, -1.), np.ones(3)
        buffers = np.empty(8192), np.empty(8192)
        for start in (0, 8192):
            values = patches[start:start + 8192]
            weights = areas[start:start + 8192]
            expected = original_candidate_mask(values, weights, lower, upper, *buffers)
            np.testing.assert_array_equal(overlap._candidate_face_mask(values, weights, lower, upper, *buffers), expected)

    def test_cumulative_tables_and_samples_match_original(self):
        """후보 탈락이 섞인 다중 묶음에서 누적표·표본·진단과 취소 확인 횟수를 보존합니다."""
        rng = np.random.default_rng(341)
        points = rng.uniform(-3, 3, (9001 * 3, 3))
        points[:300] += 10.
        faces = np.arange(len(points), dtype=np.uint32).reshape(-1, 3)
        geometry = contract.CcCoreGeometry('TRIANGLE_MESH', points, faces, {})
        bounds = points.min(axis=0), points.max(axis=0)
        captures = []
        optimized = overlap._candidate_face_mask
        for use_bounds, predicate in ((False, original_candidate_mask), (True, optimized)):
            probe = mock.Mock(return_value=False)
            with TemporaryDirectory() as directory, mock.patch.object(overlap, '_candidate_face_mask', predicate):
                with overlap.build_surface_sampler(
                    geometry, (np.full(3, -.5), np.full(3, .5)), .01, Path(directory),
                    cancel_probe=probe, source_bounds=bounds if use_bounds else None,
                ) as sampler:
                    sampled = sampler.sample(512, 27, lambda values: np.zeros(len(values)), radius=.01, max_proposals=4096)
                    captures.append((tuple(table.copy() for table in sampler._indices), sampler.sample_full(512, 31),
                                     sampled.positions, sampled.fingerprint, sampler.metadata, probe.call_count))
            self.assertFalse(Path(directory).exists())
        for first, second in zip(captures[0][:3], captures[1][:3]):
            np.testing.assert_array_equal(first, second)
        self.assertEqual(captures[0][3:], captures[1][3:])

    def test_reused_bounds_keep_coordinate_validation(self):
        """범위를 전달해도 비유한 정점과 잘못된 경계는 계속 거부합니다."""
        points = np.array([[0., 0., 0.], [1., 0., 0.], [0., 1., 0.], [np.nan, 0., 0.]])
        geometry = contract.CcCoreGeometry('TRIANGLE_MESH', points, np.array([[0, 1, 2]]), {})
        for bounds in ((np.zeros(3), np.ones(3)), (np.ones(3), np.zeros(3)), np.zeros((2, 2))):
            with self.subTest(bounds=bounds), TemporaryDirectory() as directory:
                with self.assertRaises(contract.CcCoreInputError):
                    overlap.build_surface_sampler(geometry, (np.zeros(3), np.ones(3)), .01, Path(directory), source_bounds=bounds)
                self.assertEqual(list(Path(directory).iterdir()), [])

    def test_normalization_matches_original_formula(self):
        """큰 전역 좌표와 서로 다른 형상 범위에서도 원점과 길이 단위를 정확 보존합니다."""
        rng = np.random.default_rng(781)
        for offset in (0., 1e12, -1e12):
            moving = rng.normal(size=(8193, 3)) + offset
            reference = rng.normal(size=(9351, 3)) * 2 + offset
            minimum = np.minimum(np.min(moving, axis=0), np.min(reference, axis=0))
            maximum = np.maximum(np.max(moving, axis=0), np.max(reference, axis=0))
            expected_unit = max(float(np.linalg.norm(np.ptp(moving, axis=0))), float(np.linalg.norm(np.ptp(reference, axis=0))))
            result = solver.make_normalization(moving, reference)
            np.testing.assert_array_equal(result.origin, minimum + (maximum - minimum) * .5)
            self.assertEqual(result.unit, expected_unit)

    def test_final_cleaned_bounds_match_policy_without_hint(self):
        """퇴화면 전용 먼 정점을 제거한 뒤의 범위가 양방향 반경과 검색 범위를 보존합니다."""
        points = np.array([[0., 0., 0.], [1., 0., 0.], [0., 1., 0.], [1e8, 1e8, 1e8]])
        geometry = contract.CcCoreGeometry('TRIANGLE_MESH', points, np.array([[0, 1, 2], [3, 3, 3]]), {})
        moving = solver._clean_mesh_geometry(geometry, '시험')
        reference = contract.CcCoreGeometry('TRIANGLE_MESH', moving.positions + .002, moving.triangles, {})
        bounds = tuple((item.positions.min(axis=0), item.positions.max(axis=0)) for item in (moving, reference))
        runner = mock.Mock(parameters=contract.resolve_parameters({}))
        for reverse in (False, True):
            expected = solver.prepare_overlap_policy(runner, moving, reference, reverse)
            actual = solver.prepare_overlap_policy(runner, moving, reference, reverse, geometry_bounds=bounds)
            self.assertEqual(actual, expected)
        self.assertEqual(solver.make_preparing_search_domain(moving, reference),
                         solver.make_preparing_search_domain(moving, reference, geometry_bounds=bounds))

    def test_absent_cancel_avoids_resolve_and_preserves_callback(self):
        """파일 부재 때 반복 경로 해석 없이 매번 외부 취소 콜백을 확인합니다."""
        with TemporaryDirectory() as directory:
            probe = mock.Mock(return_value=False)
            runner = cancel_runner(directory, probe)
            with mock.patch.object(contract, 'job_path', wraps=contract.job_path) as resolve:
                for _ in range(100):
                    runner.check_cancelled()
                self.assertEqual(resolve.call_count, 0)
            self.assertEqual(probe.call_count, 100)
            probe.return_value = True
            with self.assertRaises(contract.CcCoreCancelled):
                runner.check_cancelled()

    def test_cancel_creation_removal_and_directory(self):
        """파일 생성·제거를 즉시 반영하고 디렉터리를 취소 파일로 오인하지 않습니다."""
        with TemporaryDirectory() as directory:
            probe = mock.Mock(return_value=False)
            runner = cancel_runner(directory, probe)
            runner.check_cancelled()
            runner._cancel_path.touch()
            with mock.patch.object(contract, 'job_path', wraps=contract.job_path) as resolve:
                with self.assertRaises(contract.CcCoreCancelled):
                    runner.check_cancelled()
                resolve.assert_called_once_with(runner.directory, 'cancel.request')
            self.assertEqual(probe.call_count, 1)
            runner._cancel_path.unlink()
            runner.check_cancelled()
            runner._cancel_path.mkdir()
            runner.check_cancelled()

    def test_missing_job_directory_still_fails(self):
        """작업 디렉터리 소실을 정상적인 파일 부재로 숨기지 않습니다."""
        with TemporaryDirectory() as directory:
            runner = cancel_runner(directory)
        with self.assertRaises(FileNotFoundError):
            runner.check_cancelled()

    def test_existing_cancel_entry_keeps_path_rejection(self):
        """기존 경로 검증의 거부가 취소 최적화 뒤에도 그대로 전파됩니다."""
        with TemporaryDirectory() as directory:
            runner = cancel_runner(directory)
            runner._cancel_path.touch()
            with mock.patch.object(contract, 'job_path', side_effect=contract.CcCoreInputError('외부 경로')):
                with self.assertRaises(contract.CcCoreInputError):
                    runner.check_cancelled()

    def test_real_cancel_links_preserve_job_boundary(self):
        """실제 내부·외부·깨진 링크에서 기존 작업 경계 검증을 유지합니다."""
        with TemporaryDirectory() as directory, TemporaryDirectory() as outside:
            runner = cancel_runner(directory)
            inside = runner.directory / 'inside'
            inside.touch()
            external = Path(outside) / 'outside'
            external.touch()
            try:
                os.symlink(inside, runner._cancel_path)
            except OSError as error:
                self.skipTest(f'이 환경에서 심볼릭 링크를 만들 수 없습니다: {error}')
            with self.assertRaises(contract.CcCoreCancelled):
                runner.check_cancelled()
            runner._cancel_path.unlink()
            for target in (external, Path(outside) / 'missing'):
                os.symlink(target, runner._cancel_path)
                with self.assertRaises(contract.CcCoreInputError):
                    runner.check_cancelled()
                runner._cancel_path.unlink()
            os.symlink(runner.directory / 'missing', runner._cancel_path)
            runner.check_cancelled()


if __name__ == '__main__':
    unittest.main()
