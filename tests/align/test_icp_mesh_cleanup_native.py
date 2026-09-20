"""실제 메시 검사 라이브러리와 기존 넘파이 구현의 판정·순서·취소 계약을 비교합니다."""

import hashlib
import os
from pathlib import Path
import sys
import unittest

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools/align/alignment"))
import icp_cccore_contract as contract
from icp_cccore_solver import native_build_identity
from icp_mesh_cleanup import prepare_mesh_alignment_geometry
from icp_mesh_cleanup_native import NativeMeshCleaner

WORKER = Path(os.environ.get(
    "COLMAP_CCCORE_WORKER", str(ROOT / ".cccore-runtime/cccore/colmap_cccore_worker.exe"),
)).resolve()


def runtime_identity():
    """기본적으로 배포 검증을 수행하고 명시한 빌드 시험에서만 실제 파일 해시를 구성합니다."""
    if os.environ.get("COLMAP_MESH_CLEANUP_STAGING") == "1":
        library = WORKER.with_name("colmap_mesh_cleanup.dll")
        return {"mesh_cleanup_abi": 1,
                "binary_sha256": {library.name: hashlib.sha256(library.read_bytes()).hexdigest()}}
    return native_build_identity(WORKER)


def no_cancellation():
    """취소하지 않는 정상 실행의 확인 지점을 제공합니다."""
    return None


def independent_faces(corners):
    """각 면에 독립 정점을 부여하여 판정 결과와 원래 면 순서를 함께 추적합니다."""
    positions = np.asarray(corners, dtype=np.float64).reshape(-1, 3)
    return positions, np.arange(len(positions), dtype=np.uint32).reshape(-1, 3)


class NativeMeshCleanupTests(unittest.TestCase):
    """같은 입력을 두 구현에 전달하여 값·자료형·원본 보존과 오류를 비교합니다."""

    @classmethod
    def setUpClass(cls):
        """실제 런타임을 검증하고 단일 스레드와 다중 스레드 검사기를 준비합니다."""
        if not WORKER.is_file():
            raise AssertionError(f"네이티브 워커가 없습니다: {WORKER}")
        cls.identity = runtime_identity()
        cls.cleaners = {
            threads: NativeMeshCleaner(WORKER, cls.identity, max_threads=threads,
                                       check_cancelled=no_cancellation)
            for threads in (1, 4)
        }

    def compare(self, positions, triangles, *, native_precision=False):
        """두 구현의 정확한 배열·순서·자료형 또는 동일한 오류 메시지를 검사합니다."""
        original_positions, original_triangles = positions.copy(), triangles.copy()
        try:
            expected = prepare_mesh_alignment_geometry(
                positions, triangles, native_precision=native_precision)
        except ValueError as expected_error:
            for threads, cleaner in self.cleaners.items():
                with self.subTest(threads=threads, native_precision=native_precision):
                    with self.assertRaises(ValueError) as actual_error:
                        cleaner(positions, triangles, native_precision=native_precision)
                    self.assertEqual(str(actual_error.exception), str(expected_error))
        else:
            for threads, cleaner in self.cleaners.items():
                with self.subTest(threads=threads, native_precision=native_precision):
                    actual = cleaner(positions, triangles, native_precision=native_precision)
                    for wanted, found in zip(expected, actual):
                        np.testing.assert_array_equal(found, wanted)
                        self.assertEqual(found.dtype, wanted.dtype)
        np.testing.assert_array_equal(positions, original_positions)
        np.testing.assert_array_equal(triangles, original_triangles)

    def test_normal_mixed_all_degenerate_and_unreferenced(self):
        """정상·혼합·전부 퇴화와 미참조 정점의 제거 및 재색인을 확인합니다."""
        points = np.array([[99., 99., 99.], [0., 0., 0.], [1., 0., 0.],
                           [0., 1., 0.], [2., 0., 0.], [-99., -99., -99.]])
        for faces in ([[1, 2, 3]], [[1, 2, 4], [3, 2, 1], [1, 1, 3], [1, 3, 2]],
                      [[1, 2, 4], [1, 1, 3]], []):
            for precision in (False, True):
                self.compare(points, np.array(faces, dtype=np.uint32).reshape(-1, 3),
                             native_precision=precision)

    def test_signed_indices_and_nonfinite_unused_points(self):
        """정수 축소 전 범위 오류와 미참조 비유한 좌표의 오류 우선순위를 보존합니다."""
        points = np.array([[0., 0., 0.], [1., 0., 0.], [0., 1., 0.], [5., 5., 5.]])
        for index in (-1, 4, 2**32, 2**63 - 1):
            self.compare(points, np.array([[0, 1, index]], dtype=np.int64))
        for bad in (np.nan, np.inf, -np.inf):
            bad_points = points.copy()
            bad_points[-1, 2] = bad
            for dtype in (np.uint32, np.int64):
                for faces in ([[0, 1, 2]], [[0, 1, 4]]):
                    self.compare(bad_points, np.array(faces, dtype=dtype))

    def test_overflow_float32_collapse_and_tiny_coordinates(self):
        """변 차이·단정도 범위 초과와 단정도 붕괴 및 극소 좌표의 판정을 비교합니다."""
        maximum = np.finfo(np.float64).max
        cases = [
            [[-maximum, 0., 0.], [maximum, 0., 0.], [0., 1., 0.]],
            [[1e40, 0., 0.], [1e40, 1., 0.], [1e40, 0., 1.]],
            [[1e8, 0., 0.], [1e8 + 1., 0., 0.], [1e8, 1., 0.]],
            [[0., 0., 0.], [1e-300, 0., 0.], [0., 1e-300, 0.]],
            [[0., 0., 0.], [np.nextafter(0., 1.), 0., 0.],
             [0., np.nextafter(0., 1.), 0.]],
        ]
        for case in cases:
            for precision in (False, True):
                self.compare(np.asarray(case), np.array([[0, 1, 2]], dtype=np.uint32),
                             native_precision=precision)

    def test_threshold_nextafter_neighbors(self):
        """두 임계값의 바로 아래·동일·바로 위 및 단정도 인접 표현값을 비교합니다."""
        exact = 32. * np.finfo(np.float64).eps
        native = np.sqrt(1e-16)
        for precision, threshold in ((False, exact), (True, native)):
            heights = [np.nextafter(threshold, 0.), threshold, np.nextafter(threshold, np.inf)]
            if precision:
                rounded = np.float32(threshold)
                heights.extend([float(np.nextafter(rounded, np.float32(0.))), float(rounded),
                                float(np.nextafter(rounded, np.float32(np.inf)))])
            corners = [[[0., 0., 0.], [1., 0., 0.], [1., height, 0.]] for height in heights]
            # 기준면을 더해 모든 경계 표본이 탈락하더라도 남은 면의 순서를 검사합니다.
            corners.append([[0., 0., 0.], [1., 0., 0.], [0., 1., 0.]])
            self.compare(*independent_faces(corners), native_precision=precision)

    def test_random_batch_boundaries_and_multiple_batches(self):
        """고정 난수의 퇴화 혼합 면을 묶음 경계 전후와 여러 묶음에서 비교합니다."""
        rng = np.random.default_rng(738201)
        for count in (65_535, 65_536, 65_537, 131_089):
            corners = rng.normal(size=(count, 3, 3))
            corners[::7, 2] = corners[::7, 0]
            points, faces = independent_faces(corners)
            # 앞뒤 미참조 정점이 있을 때도 입력 순서에 따라 재색인되어야 합니다.
            points = np.vstack(([17., 18., 19.], points, [-17., -18., -19.]))
            faces += 1
            for precision in (False, True):
                self.compare(points, faces, native_precision=precision)

    def test_error_precedence_across_batches(self):
        """뒤 묶음 인덱스 오류가 앞 묶음 변 차이 오류를 가리지 않는지 검사합니다."""
        maximum = np.finfo(np.float64).max
        points = np.array([[-maximum, 0., 0.], [maximum, 0., 0.], [0., 1., 0.]])
        faces = np.tile(np.array([[0, 1, 2]], dtype=np.int64), (65_537, 1))
        faces[-1, -1] = 2**32
        self.compare(points, faces)

    def test_readonly_all_valid_returns_original_arrays(self):
        """모든 면·정점이 유효한 읽기 전용 입력의 원래 배열 객체를 반환합니다."""
        for dtype in (np.float32, np.float64, np.int64):
            points = np.array([[0, 0, 0], [1, 0, 0], [0, 1, 0]], dtype=dtype)
            for index_dtype in (np.uint32, np.int64):
                faces = np.array([[0, 1, 2]], dtype=index_dtype)
                points.flags.writeable = False
                faces.flags.writeable = False
                for cleaner in self.cleaners.values():
                    for precision in (False, True):
                        actual = cleaner(points, faces, native_precision=precision)
                        self.assertIs(actual[0], points)
                        self.assertIs(actual[1], faces)
                        self.assertFalse(points.flags.writeable)
                        self.assertFalse(faces.flags.writeable)

    def test_noncontiguous_arrays(self):
        """비연속 배열과 뒤집힌 면의 값 및 원본 객체 보존을 검사합니다."""
        points = np.array([[0., 0., 0., 7.], [1., 0., 0., 7.], [0., 1., 0., 7.]])[:, :3]
        faces = np.array([[0, 1, 2]], dtype=np.int64)[:, ::-1]
        for precision in (False, True):
            self.compare(points, faces, native_precision=precision)

    def test_cancel_callback_preserves_exception(self):
        """호출 중 콜백에서 발생한 취소 예외 객체가 그대로 다시 전달되는지 확인합니다."""
        points = np.array([[0., 0., 0.], [1., 0., 0.], [0., 1., 0.]])
        faces = np.tile(np.array([[0, 1, 2]], dtype=np.uint32), (131_073, 1))
        for threads in (1, 4):
            for trigger in (1, 2, 3, 4):
                error = contract.CcCoreCancelled("검사 중 취소")
                calls = [0]

                def cancel():
                    """지정한 확인 횟수에서 동일한 취소 예외 객체를 발생시킵니다."""
                    calls[0] += 1
                    if calls[0] == trigger:
                        raise error

                cleaner = NativeMeshCleaner(WORKER, self.identity, max_threads=threads,
                                            check_cancelled=cancel)
                with self.assertRaises(contract.CcCoreCancelled) as caught:
                    cleaner(points, faces)
                self.assertIs(caught.exception, error)
                self.assertEqual(calls[0], trigger)

    def test_loader_rejects_wrong_hash_and_abi(self):
        """빌드 시험에서도 실제 해시와 인터페이스 버전 검증을 건너뛰지 않는지 확인합니다."""
        for identity in (
            {**self.identity, "mesh_cleanup_abi": 0},
            {**self.identity, "binary_sha256": {"colmap_mesh_cleanup.dll": "0" * 64}},
        ):
            with self.assertRaises(contract.CcCoreEnvironmentError):
                NativeMeshCleaner(WORKER, identity, max_threads=1, check_cancelled=no_cancellation)


if __name__ == "__main__":
    unittest.main()
