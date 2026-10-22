"""원본 배열 전체의 기존 SHA-256 지문을 시스템 구현으로 동일하게 계산합니다."""

from __future__ import annotations

import ctypes
import hashlib
import os
from pathlib import Path
from typing import Any

import numpy as np


_BLOCK_ROWS = 65_536
_SYSTEM32_SEARCH_FLAG = 0x00000800
_ULONG_MAXIMUM = 0xFFFFFFFF
_MICROSOFT_PROVIDER = "Microsoft Primitive Provider"


class ArrayHashError(RuntimeError):
    """전체 원본 지문을 계산할 시스템 구현이 실패했을 때 발생합니다."""


def _check_status(status: int, operation: str) -> None:
    """시스템 암호화 호출의 실패 코드를 보존하여 계산 실패로 전달합니다."""
    if status != 0:
        raise ArrayHashError(f"시스템 SHA-256 {operation} 실패: 0x{int(status) & 0xFFFFFFFF:08x}")


class _CngApi:
    """시스템 디렉터리의 암호화 DLL과 정확한 윈도 호출 형식을 연결합니다."""

    def __init__(self) -> None:
        """신뢰한 시스템 경로의 DLL만 읽고 각 암호화 함수의 인자·반환 자료형을 고정합니다."""
        if os.name != "nt":
            raise ArrayHashError("시스템 SHA-256 제공자는 윈도에서만 사용할 수 있습니다.")
        try:
            kernel = ctypes.WinDLL("kernel32.dll", winmode=_SYSTEM32_SEARCH_FLAG)
            get_directory = kernel.GetSystemDirectoryW
            get_directory.argtypes = (ctypes.c_wchar_p, ctypes.c_uint32)
            get_directory.restype = ctypes.c_uint32
            buffer = ctypes.create_unicode_buffer(32_768)
            count = get_directory(buffer, len(buffer))
            if not count or count >= len(buffer):
                raise ArrayHashError("시스템 SHA-256의 신뢰할 시스템 디렉터리를 확인하지 못했습니다.")
            self.dll_path = (Path(buffer.value) / "bcrypt.dll").resolve(strict=True)
            self.dll = ctypes.WinDLL(str(self.dll_path), winmode=_SYSTEM32_SEARCH_FLAG)
            signatures = {
                "BCryptOpenAlgorithmProvider": (ctypes.POINTER(ctypes.c_void_p), ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32),
                "BCryptGetProperty": (ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32), ctypes.c_uint32),
                "BCryptCreateHash": (ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32),
                "BCryptHashData": (ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32),
                "BCryptFinishHash": (ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32),
                "BCryptDestroyHash": (ctypes.c_void_p,),
                "BCryptCloseAlgorithmProvider": (ctypes.c_void_p, ctypes.c_uint32),
            }
            for name, arguments in signatures.items():
                function = getattr(self.dll, name)
                function.argtypes = arguments
                function.restype = ctypes.c_int32
                setattr(self, name, function)
        except (OSError, AttributeError) as error:
            raise ArrayHashError(f"시스템 SHA-256 제공자를 읽지 못했습니다. {error}") from error


_API: _CngApi | None = None


def _cng_api() -> _CngApi:
    """DLL 연결만 재사용하며 각 전체 원본 해시의 상태와 핸들은 독립 생성합니다."""
    global _API
    if _API is None:
        _API = _CngApi()
    return _API


class _CngSha256:
    """해시와 알고리즘 핸들을 독립 소유하고 오류가 나도 순서대로 정리합니다."""

    def __init__(self, api: Any = None) -> None:
        """마이크로소프트 SHA-256 제공자와 시스템이 메모리를 소유하는 계산 상태를 엽니다."""
        self.algorithm = ctypes.c_void_p()
        self.hash = ctypes.c_void_p()
        self.finished = False
        self.api = _cng_api() if api is None else api
        try:
            _check_status(self.api.BCryptOpenAlgorithmProvider(
                ctypes.byref(self.algorithm), "SHA256", _MICROSOFT_PROVIDER, 0), "제공자 열기")
            length, returned = ctypes.c_uint32(), ctypes.c_uint32()
            _check_status(self.api.BCryptGetProperty(self.algorithm, "HashDigestLength", ctypes.byref(length),
                ctypes.sizeof(length), ctypes.byref(returned), 0), "출력 길이 조회")
            if length.value != 32 or returned.value != ctypes.sizeof(length):
                raise ArrayHashError("시스템 SHA-256 출력 길이가 32바이트가 아닙니다.")
            _check_status(self.api.BCryptCreateHash(
                self.algorithm, ctypes.byref(self.hash), None, 0, None, 0, 0), "해시 생성")
        except BaseException:
            self.close(suppress_errors=True)
            raise

    def __enter__(self) -> _CngSha256:
        """생성한 해시 상태를 제한된 수명의 계산 구간에 연결합니다."""
        return self

    def __exit__(self, error_type: Any, error: Any, traceback: Any) -> None:
        """원래 오류를 유지하면서 해시와 제공자 핸들을 모두 해제합니다."""
        self.close(suppress_errors=error_type is not None)

    def update(self, data: bytes | np.ndarray) -> None:
        """살아 있는 바이트·연속 수치 배열의 읽기 포인터로 원본 복사 없이 내용을 추가합니다."""
        if not self.hash.value or self.finished:
            raise ArrayHashError("종료된 시스템 SHA-256 상태에 입력을 추가할 수 없습니다.")
        if isinstance(data, bytes):
            pointer = ctypes.cast(ctypes.c_char_p(data), ctypes.c_void_p).value
            size = len(data)
        elif isinstance(data, np.ndarray) and data.flags.c_contiguous and not data.dtype.hasobject:
            pointer, size = int(data.ctypes.data), int(data.nbytes)
        else:
            raise TypeError("시스템 SHA-256은 바이트 또는 연속된 수치 배열만 받습니다.")
        # 호출 내내 입력 객체를 보관하여 읽기 전용 포인터의 원본 수명을 유지합니다.
        for start in range(0, size, _ULONG_MAXIMUM):
            count = min(size - start, _ULONG_MAXIMUM)
            _check_status(self.api.BCryptHashData(self.hash, ctypes.c_void_p(pointer + start), count, 0), "입력 추가")

    def hexdigest(self) -> str:
        """한 번 최종화한 표준 SHA-256 32바이트를 소문자 16진수로 반환합니다."""
        if not self.hash.value or self.finished:
            raise ArrayHashError("종료된 시스템 SHA-256 상태를 다시 최종화할 수 없습니다.")
        output = (ctypes.c_ubyte * 32)()
        _check_status(self.api.BCryptFinishHash(self.hash, output, len(output), 0), "최종화")
        self.finished = True
        return bytes(output).hex()

    def close(self, *, suppress_errors: bool = False) -> None:
        """어느 해제 호출이 실패해도 다른 핸들의 해제를 시도하고 중복 해제를 막습니다."""
        failure = None
        if self.hash.value:
            handle, self.hash = self.hash, ctypes.c_void_p()
            try:
                _check_status(self.api.BCryptDestroyHash(handle), "해시 해제")
            except Exception as error:
                failure = error
        if self.algorithm.value:
            handle, self.algorithm = self.algorithm, ctypes.c_void_p()
            try:
                _check_status(self.api.BCryptCloseAlgorithmProvider(handle, 0), "제공자 해제")
            except Exception as error:
                if failure is None:
                    failure = error
        if failure is not None and not suppress_errors:
            raise failure

    def __del__(self) -> None:
        """생성 중 오류나 문맥 누락이 있더라도 남은 소유 핸들의 마지막 해제를 시도합니다."""
        try:
            self.close(suppress_errors=True)
        except Exception:
            pass


def array_sha256_fingerprint(array: np.ndarray, dtype: str, *, block_rows: int = _BLOCK_ROWS) -> str:
    """기존 접두사·표준 블록 전체를 윈도 시스템 SHA-256 또는 다른 플랫폼의 기존 구현으로 해시합니다."""
    if type(block_rows) is not int or block_rows <= 0:
        raise ValueError("원본 지문 블록의 행 수는 양의 정수여야 합니다.")
    prefix = f"{dtype}:{array.shape}".encode("ascii")
    if os.name == "nt":
        with _CngSha256() as digest:
            digest.update(prefix)
            for start in range(0, len(array), block_rows):
                block = np.ascontiguousarray(array[start:start + block_rows], dtype=np.dtype(dtype))
                digest.update(block)
            return digest.hexdigest()
    digest = hashlib.sha256()
    digest.update(prefix)
    for start in range(0, len(array), block_rows):
        block = np.ascontiguousarray(array[start:start + block_rows], dtype=np.dtype(dtype))
        digest.update(memoryview(block).cast("B"))
    return digest.hexdigest()
