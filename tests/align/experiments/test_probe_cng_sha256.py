"""시스템 SHA-256의 동일 지문·읽기 전용 버퍼·실패 시 핸들 해제 경계를 검증합니다."""

from __future__ import annotations

import ctypes
import hashlib
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

import numpy as np


sys.path.insert(0, str(Path(__file__).resolve().parent))
import probe_cng_sha256 as probe


class _FakeApi:
    """지정된 윈도 호출만 실패시켜 후속 핸들 해제 순서를 관찰합니다."""

    def __init__(self, fail=None):
        """실패 위치와 호출 기록을 초기화합니다."""
        self.fail = fail
        self.calls = []

    def _result(self, operation):
        """현재 호출을 기록하고 선택한 위치에만 고정된 실패 코드를 반환합니다."""
        self.calls.append(operation)
        return -1 if self.fail == operation else 0

    def BCryptOpenAlgorithmProvider(self, result, algorithm, provider, flags):
        """시험 알고리즘 핸들을 생성한 뒤 제공자 열기 결과를 반환합니다."""
        result._obj.value = 11
        return self._result("open")

    def BCryptGetProperty(self, handle, name, output, count, returned, flags):
        """정상 SHA-256 출력 길이와 속성 반환 크기를 구성합니다."""
        output._obj.value = 32
        returned._obj.value = 4
        return self._result("property")

    def BCryptCreateHash(self, algorithm, result, memory, count, secret, secret_count, flags):
        """시험 해시 핸들을 생성한 뒤 해시 생성 결과를 반환합니다."""
        result._obj.value = 22
        return self._result("create")

    def BCryptHashData(self, handle, data, count, flags):
        """원본 포인터에 쓰지 않고 입력 추가의 성공 또는 실패만 반환합니다."""
        return self._result("update")

    def BCryptFinishHash(self, handle, output, count, flags):
        """시험 출력을 채워 최종화와 해제를 구별할 수 있게 합니다."""
        for index in range(count):
            output[index] = index
        return self._result("finish")

    def BCryptDestroyHash(self, handle):
        """해시 핸들의 해제 시도를 기록합니다."""
        return self._result("destroy")

    def BCryptCloseAlgorithmProvider(self, handle, flags):
        """제공자 핸들의 해제 시도를 기록합니다."""
        return self._result("close")


class CngResourceTests(unittest.TestCase):
    """실제 파일이나 대형 배열 없이 모든 오류 위치의 정리 계약을 확인합니다."""

    def test_constructor_failures_release_partial_handles(self):
        """제공자·속성·해시 생성 도중 실패해도 생성된 각 핸들의 해제를 시도합니다."""
        for operation in ("open", "property", "create"):
            with self.subTest(operation=operation):
                api = _FakeApi(operation)
                with self.assertRaises(OSError):
                    probe.CngSha256(api)
                self.assertEqual(api.calls[-1], "close")
                self.assertEqual(api.calls.count("close"), 1)
                self.assertEqual(api.calls.count("destroy"), int(operation == "create"))

    def test_hash_and_finalization_failures_release_both_handles(self):
        """입력 추가·최종화 실패가 해시와 제공자의 순서 있는 해제를 막지 않습니다."""
        for operation in ("update", "finish"):
            with self.subTest(operation=operation):
                api = _FakeApi(operation)
                with self.assertRaises(OSError):
                    with probe.CngSha256(api) as digest:
                        digest.update(b"abc")
                        digest.hexdigest()
                self.assertEqual(api.calls[-2:], ["destroy", "close"])

    def test_failed_destroy_does_not_skip_provider_close_or_repeat_the_destroy(self):
        """해시 해제가 실패해도 제공자 해제를 시도하고 같은 핸들을 다시 해제하지 않습니다."""
        api = _FakeApi("destroy")
        digest = probe.CngSha256(api)
        with self.assertRaisesRegex(OSError, "해시 해제"):
            digest.close()
        self.assertEqual(api.calls[-2:], ["destroy", "close"])
        digest.close()
        self.assertEqual(api.calls.count("destroy"), 1)
        self.assertEqual(api.calls.count("close"), 1)

    def test_original_error_survives_cleanup_failure(self):
        """정리 실패가 있더라도 원래 계산 예외 객체를 다른 오류로 덮어쓰지 않습니다."""
        error = ValueError("원래 계산 오류")
        api = _FakeApi("destroy")
        with self.assertRaises(ValueError) as captured:
            with probe.CngSha256(api):
                raise error
        self.assertIs(captured.exception, error)
        self.assertEqual(api.calls[-2:], ["destroy", "close"])

    def test_finished_or_closed_hash_rejects_reuse(self):
        """최종화·해제된 시험 상태의 잘못된 재사용을 명시적으로 거부합니다."""
        with probe.CngSha256(_FakeApi()) as digest:
            digest.hexdigest()
            with self.assertRaises(RuntimeError):
                digest.update(b"after")
            with self.assertRaises(RuntimeError):
                digest.hexdigest()
        with self.assertRaises(RuntimeError):
            digest.update(b"closed")

    def test_feed_uses_the_exact_existing_prefix_and_canonical_blocks(self):
        """자료형·형상 ASCII 접두사와 C 순서 표준 자료형 바이트가 기존 지문과 같습니다."""
        for array, dtype in ((np.arange(90, dtype=np.int64).reshape(10, 9)[:, ::3], "<f8"),
                             (np.arange(30, dtype="<u8").reshape(10, 3)[::-1], "<u4"),
                             (np.empty((0, 3), dtype=np.float64), "<f8")):
            digest = hashlib.sha256()
            digest.update(f"{dtype}:{array.shape}".encode("ascii"))
            for start in range(0, len(array), 4):
                block = np.ascontiguousarray(array[start:start + 4], dtype=np.dtype(dtype))
                digest.update(memoryview(block).cast("B"))
            self.assertEqual(probe.reference_array_fingerprint(array, dtype, block_rows=4), digest.hexdigest())


@unittest.skipUnless(os.name == "nt", "윈도 시스템 암호화 API가 필요합니다.")
class NativeCngCorrectnessTests(unittest.TestCase):
    """실제 시스템 SHA-256과 기존 파이썬 구현의 동일 출력과 원본 보존을 확인합니다."""

    def test_small_byte_and_array_vectors_match(self):
        """빈 입력·알려진 벡터·빈 배열·비연속 배열의 전체 SHA-256이 일치합니다."""
        self.assertTrue(probe.small_correctness_checks()["same_sha256"])

    def test_system32_dll_is_loaded_using_an_absolute_path_and_system_search_flags(self):
        """실제 시스템 디렉터리의 절대 DLL 경로와 제한된 검색 플래그만 사용합니다."""
        actual_loader = ctypes.WinDLL
        with patch.object(ctypes, "WinDLL", wraps=actual_loader) as loader:
            api = probe.CngApi()
        self.assertTrue(api.dll_path.is_absolute())
        self.assertEqual(api.dll_path.name.lower(), "bcrypt.dll")
        self.assertEqual(api.dll_path.parent.name.lower(), "system32")
        self.assertEqual(loader.call_args_list[-1].args[0], str(api.dll_path))
        self.assertTrue(all(call.kwargs["winmode"] == probe.SYSTEM32_SEARCH_FLAG for call in loader.call_args_list))

    def test_readonly_immutable_and_noncontiguous_arrays_preserve_source(self):
        """불변 바이트 원본과 비연속 정수 배열을 읽어도 원본 내용과 쓰기 권한이 변하지 않습니다."""
        original = np.arange(90, dtype=np.float64).reshape(30, 3)
        readonly = np.frombuffer(original.tobytes(), dtype=np.float64).reshape(original.shape)
        arrays = ((readonly, "<f8"), (original[:, ::-1], "<f8"),
                  (np.arange(60, dtype=np.int64).reshape(20, 3)[::2], "<u4"))
        for array, dtype in arrays:
            before, writable = array.copy(), array.flags.writeable
            self.assertEqual(probe.cng_array_fingerprint(array, dtype, block_rows=4),
                             probe.reference_array_fingerprint(array, dtype, block_rows=4))
            np.testing.assert_array_equal(array, before)
            self.assertEqual(array.flags.writeable, writable)

    @unittest.skipUnless(os.environ.get("COLMAP_CNG_LARGE_TESTS") == "1", "백만 점 검사는 별도 실측 창에서만 실행합니다.")
    def test_more_than_one_million_points_and_real_block_boundaries(self):
        """백만 점을 넘는 배열과 실제 블록 경계 전후에서도 전체 지문이 완전히 일치합니다."""
        positions = np.arange(1_000_003 * 3, dtype=np.float64).reshape(-1, 3)
        for count in (65_535, 65_536, 65_537, 1_000_003):
            values = positions[:count]
            self.assertEqual(probe.cng_array_fingerprint(values, "<f8"), probe.reference_array_fingerprint(values, "<f8"))


if __name__ == "__main__":
    unittest.main()
