"""제품 원본 지문의 플랫폼 분기·기존 SHA-256 호환·시스템 자원 정리를 검증합니다."""

from __future__ import annotations

import builtins
import ctypes
import hashlib
import importlib.util
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

import numpy as np


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.align.alignment import icp_array_hash as hashing


def _reference(array, dtype):
    """제품 코드와 독립적으로 접두사 및 표준 순서의 전체 바이트를 직접 해시합니다."""
    digest = hashlib.sha256(f"{dtype}:{array.shape}".encode("ascii"))
    digest.update(np.asarray(array, dtype=np.dtype(dtype), order="C").tobytes(order="C"))
    return digest.hexdigest()


class _FakeApi:
    """선택한 시스템 함수의 실패와 부분 생성 핸들의 정리 순서를 관찰합니다."""

    def __init__(self, failure=None, *, digest_length=32):
        """실패 함수·출력 길이·호출 기록을 초기화합니다."""
        self.failure = failure
        self.digest_length = digest_length
        self.calls = []

    def __getattr__(self, name):
        """윈도 함수 이름별로 제어 가능한 반환값과 최소 출력 인자를 제공합니다."""
        if not name.startswith("BCrypt"):
            raise AttributeError(name)

        def call(*arguments):
            """핸들·길이·출력을 구성하고 요청한 지점에서만 오류를 발생시킵니다."""
            self.calls.append(name)
            if name == "BCryptOpenAlgorithmProvider":
                arguments[0]._obj.value = 11
            elif name == "BCryptCreateHash":
                arguments[1]._obj.value = 22
            elif name == "BCryptGetProperty":
                arguments[2]._obj.value = self.digest_length
                arguments[4]._obj.value = 4
            elif name == "BCryptFinishHash":
                for index in range(arguments[2]):
                    arguments[1][index] = index
            return -1 if name == self.failure else 0

        return call


class ArrayHashContractTests(unittest.TestCase):
    """전체 바이트 지문과 실패 경로가 플랫폼이나 구현 교체 때문에 달라지지 않는지 확인합니다."""

    def test_non_windows_uses_the_existing_hashlib_implementation(self):
        """윈도 외의 플랫폼에서는 시스템 API를 열지 않고 기존 SHA-256으로 계산합니다."""
        values = np.arange(90, dtype=np.int64).reshape(10, 9)[:, ::3]
        expected = _reference(values, "<f8")
        with patch.object(hashing.os, "name", "posix"):
            with patch.object(hashing, "_CngSha256", side_effect=AssertionError("시스템 API 호출 금지")):
                self.assertEqual(hashing.array_sha256_fingerprint(values, "<f8", block_rows=4), expected)

    def test_windows_failure_never_silently_falls_back_to_hashlib(self):
        """윈도 제공자 실패가 원본 검사 생략이나 다른 구현의 묵시적 재시도로 바뀌지 않습니다."""
        error = hashing.ArrayHashError("시스템 제공자 실패")
        with patch.object(hashing.os, "name", "nt"):
            with patch.object(hashing, "_CngSha256", side_effect=error):
                with patch.object(hashing.hashlib, "sha256", side_effect=AssertionError("묵시적 재시도 금지")):
                    with self.assertRaises(hashing.ArrayHashError) as captured:
                        hashing.array_sha256_fingerprint(np.eye(3), "<f8")
        self.assertIs(captured.exception, error)

    def test_constructor_errors_release_every_partially_created_handle(self):
        """제공자·속성·해시 생성의 모든 실패 지점에서 보유한 핸들을 해제합니다."""
        for failure in ("BCryptOpenAlgorithmProvider", "BCryptGetProperty", "BCryptCreateHash"):
            with self.subTest(failure=failure):
                api = _FakeApi(failure)
                with self.assertRaises(hashing.ArrayHashError):
                    hashing._CngSha256(api)
                self.assertEqual(api.calls[-1], "BCryptCloseAlgorithmProvider")
                self.assertEqual(api.calls.count("BCryptCloseAlgorithmProvider"), 1)
                self.assertEqual(api.calls.count("BCryptDestroyHash"), int(failure == "BCryptCreateHash"))

    def test_wrong_digest_size_is_rejected_and_provider_is_closed(self):
        """잘못된 SHA-256 출력 길이를 거부하고 열린 제공자를 해제합니다."""
        api = _FakeApi(digest_length=16)
        with self.assertRaisesRegex(hashing.ArrayHashError, "32바이트"):
            hashing._CngSha256(api)
        self.assertEqual(api.calls[-1], "BCryptCloseAlgorithmProvider")
        self.assertNotIn("BCryptCreateHash", api.calls)

    def test_update_or_finish_errors_close_both_handles_and_preserve_the_error(self):
        """입력 추가·최종화 실패 뒤에도 두 핸들을 해제하고 원래 시스템 오류를 유지합니다."""
        for failure in ("BCryptHashData", "BCryptFinishHash"):
            with self.subTest(failure=failure):
                api = _FakeApi(failure)
                with self.assertRaises(hashing.ArrayHashError):
                    with hashing._CngSha256(api) as digest:
                        digest.update(b"abc")
                        digest.hexdigest()
                self.assertEqual(api.calls[-2:], ["BCryptDestroyHash", "BCryptCloseAlgorithmProvider"])

    def test_cleanup_failure_attempts_all_handles_without_double_release(self):
        """각 해제 함수가 실패해도 나머지 자원을 정리하고 같은 핸들을 다시 해제하지 않습니다."""
        for failure in ("BCryptDestroyHash", "BCryptCloseAlgorithmProvider"):
            with self.subTest(failure=failure):
                api = _FakeApi(failure)
                digest = hashing._CngSha256(api)
                with self.assertRaises(hashing.ArrayHashError):
                    digest.close()
                digest.close()
                self.assertEqual(api.calls[-2:], ["BCryptDestroyHash", "BCryptCloseAlgorithmProvider"])
                self.assertEqual(api.calls.count("BCryptDestroyHash"), 1)
                self.assertEqual(api.calls.count("BCryptCloseAlgorithmProvider"), 1)

    def test_original_exception_is_preserved_if_cleanup_also_fails(self):
        """자원 정리 실패가 먼저 발생한 원래 계산 예외 객체를 덮어쓰지 않습니다."""
        original = ValueError("원래 형상 오류")
        api = _FakeApi("BCryptDestroyHash")
        with self.assertRaises(ValueError) as captured:
            with hashing._CngSha256(api):
                raise original
        self.assertIs(captured.exception, original)
        self.assertEqual(api.calls[-2:], ["BCryptDestroyHash", "BCryptCloseAlgorithmProvider"])

    def test_block_rows_must_be_a_positive_integer(self):
        """무효한 블록 크기를 시스템 호출 전에 거부합니다."""
        for count in (0, -1, True, 1.5):
            with self.assertRaises(ValueError):
                hashing.array_sha256_fingerprint(np.eye(3), "<f8", block_rows=count)

    def test_module_has_no_blender_dependency(self):
        """독립 파일 로딩 중 블렌더 모듈이나 전용 실행 환경을 요구하지 않습니다."""
        original = builtins.__import__

        def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
            """순수 배열 해시가 블렌더 모듈을 요청하면 검사를 실패시킵니다."""
            if name == "bpy" or name.startswith(("bpy.", "bpy_extras")):
                raise AssertionError("블렌더 의존성 유입")
            return original(name, globals, locals, fromlist, level)

        spec = importlib.util.spec_from_file_location("_independent_array_hash", hashing.__file__)
        module = importlib.util.module_from_spec(spec)
        with patch.object(builtins, "__import__", side_effect=guarded_import):
            spec.loader.exec_module(module)
        self.assertIsNotNone(module.array_sha256_fingerprint)


@unittest.skipUnless(os.name == "nt", "윈도 시스템 암호화 API가 필요합니다.")
class WindowsArrayHashTests(unittest.TestCase):
    """실제 윈도 시스템 제공자의 SHA-256이 기존 지문과 완전히 같은지 확인합니다."""

    def test_known_byte_vectors_and_single_finalization(self):
        """알려진 SHA-256 바이트 벡터와 최종화 이후 재사용 거부를 확인합니다."""
        for data in (b"", b"abc", bytes(range(256))):
            with hashing._CngSha256() as digest:
                digest.update(data)
                self.assertEqual(digest.hexdigest(), hashlib.sha256(data).hexdigest())
                with self.assertRaises(hashing.ArrayHashError):
                    digest.update(b"after")
                with self.assertRaises(hashing.ArrayHashError):
                    digest.hexdigest()

    def test_empty_strided_fortran_byte_order_and_readonly_arrays_match_the_original(self):
        """빈 배열·비연속·열 우선·바이트 순서·불변 원본에도 동일 지문과 쓰기 권한을 유지합니다."""
        values = np.arange(90, dtype=np.float64).reshape(30, 3)
        readonly = np.frombuffer(values.tobytes(), dtype=np.float64).reshape(values.shape)
        arrays = ((np.empty((0, 3)), "<f8"), (values[::3, ::-1], "<f8"),
                  (np.asfortranarray(values), "<f8"), (values.astype(">f8"), "<f8"),
                  (np.arange(90, dtype=np.int64).reshape(30, 3), "<u4"), (readonly, "<f8"))
        for array, dtype in arrays:
            with self.subTest(dtype=array.dtype, strides=array.strides):
                before, writable = array.copy(), array.flags.writeable
                self.assertEqual(hashing.array_sha256_fingerprint(array, dtype, block_rows=4), _reference(array, dtype))
                np.testing.assert_array_equal(array, before)
                self.assertEqual(array.flags.writeable, writable)

    def test_system32_absolute_dll_and_restricted_search_flags(self):
        """실제 시스템 디렉터리의 절대 DLL 경로만 제한된 검색 플래그로 읽습니다."""
        loader = ctypes.WinDLL
        with patch.object(ctypes, "WinDLL", wraps=loader) as calls:
            api = hashing._CngApi()
        self.assertTrue(api.dll_path.is_absolute())
        self.assertEqual(api.dll_path.parent.name.lower(), "system32")
        self.assertEqual(api.dll_path.name.lower(), "bcrypt.dll")
        self.assertEqual(calls.call_args.args[0], str(api.dll_path))
        self.assertTrue(all(call.kwargs["winmode"] == hashing._SYSTEM32_SEARCH_FLAG for call in calls.call_args_list))

    def test_tail_mutation_changes_the_full_array_fingerprint(self):
        """첫 블록 바깥 마지막 원본 좌표 변경도 전체 지문에 반드시 반영합니다."""
        array = np.arange(90, dtype=np.float64).reshape(30, 3)
        first = hashing.array_sha256_fingerprint(array, "<f8", block_rows=4)
        array[-1, -1] += .25
        second = hashing.array_sha256_fingerprint(array, "<f8", block_rows=4)
        self.assertNotEqual(first, second)
        self.assertEqual(second, _reference(array, "<f8"))

    @unittest.skipUnless(os.environ.get("COLMAP_CNG_LARGE_TESTS") == "1", "대형 원본 검사는 별도 검증 창에서만 실행합니다.")
    def test_more_than_one_million_points_and_original_block_boundaries(self):
        """백만 점 이상과 실제 65,536행 경계 전후의 전체 지문을 독립 구현과 비교합니다."""
        array = np.arange(1_000_003 * 3, dtype=np.float64).reshape(-1, 3)
        for count in (65_535, 65_536, 65_537, 1_000_003):
            values = array[:count]
            self.assertEqual(hashing.array_sha256_fingerprint(values, "<f8"), _reference(values, "<f8"))


if __name__ == "__main__":
    unittest.main()
