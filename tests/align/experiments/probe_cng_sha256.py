"""제품을 수정하지 않고 시스템 SHA-256의 동일 바이트 지문과 선택적 실측을 검증합니다."""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
from pathlib import Path
import ssl
import statistics
import sys
from time import perf_counter

import numpy as np


ROOT = Path(__file__).resolve().parents[3]
BLOCK_ROWS = 65_536
SYSTEM32_SEARCH_FLAG = 0x00000800
ULONG_MAXIMUM = 0xFFFFFFFF
MICROSOFT_PROVIDER = "Microsoft Primitive Provider"


def check_status(status, operation):
    """각 윈도 암호화 호출의 실패 코드를 손실 없이 전달합니다."""
    if status != 0:
        raise OSError(f"시스템 SHA-256 {operation} 실패: 0x{int(status) & 0xFFFFFFFF:08x}")


class CngApi:
    """실제 시스템 디렉터리의 암호화 DLL만 읽고 정확한 윈도 호출 형식을 연결합니다."""

    def __init__(self):
        """신뢰한 시스템 경로에서 커널·암호화 API를 읽고 인자 자료형을 고정합니다."""
        if os.name != "nt":
            raise OSError("이 시스템 SHA-256 실험은 윈도에서만 실행할 수 있습니다.")
        kernel = ctypes.WinDLL("kernel32.dll", winmode=SYSTEM32_SEARCH_FLAG)
        get_directory = kernel.GetSystemDirectoryW
        get_directory.argtypes = (ctypes.c_wchar_p, ctypes.c_uint32)
        get_directory.restype = ctypes.c_uint32
        buffer = ctypes.create_unicode_buffer(32_768)
        count = get_directory(buffer, len(buffer))
        if not count or count >= len(buffer):
            raise OSError("신뢰할 시스템 디렉터리를 확인하지 못했습니다.")
        self.dll_path = (Path(buffer.value) / "bcrypt.dll").resolve(strict=True)
        self.dll = ctypes.WinDLL(str(self.dll_path), winmode=SYSTEM32_SEARCH_FLAG)
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


_API = None


def cng_api():
    """실험 프로세스 안에서 시스템 DLL 연결만 재사용하고 각 해시 핸들은 독립 생성합니다."""
    global _API
    if _API is None:
        _API = CngApi()
    return _API


class CngSha256:
    """알고리즘·해시 핸들의 독립 소유권과 오류 시 정리를 보장하는 시험용 SHA-256입니다."""

    def __init__(self, api=None):
        """마이크로소프트 SHA-256 제공자와 시스템이 메모리를 소유하는 해시 상태를 엽니다."""
        self.algorithm = ctypes.c_void_p()
        self.hash = ctypes.c_void_p()
        self.finished = False
        self.api = cng_api() if api is None else api
        try:
            check_status(self.api.BCryptOpenAlgorithmProvider(ctypes.byref(self.algorithm), "SHA256", MICROSOFT_PROVIDER, 0), "제공자 열기")
            length, returned = ctypes.c_uint32(), ctypes.c_uint32()
            check_status(self.api.BCryptGetProperty(self.algorithm, "HashDigestLength", ctypes.byref(length),
                ctypes.sizeof(length), ctypes.byref(returned), 0), "출력 길이 조회")
            if length.value != 32 or returned.value != ctypes.sizeof(length):
                raise OSError("시스템 SHA-256 출력 길이가 32바이트가 아닙니다.")
            check_status(self.api.BCryptCreateHash(self.algorithm, ctypes.byref(self.hash), None, 0, None, 0, 0), "해시 생성")
        except BaseException:
            self.close(suppress_errors=True)
            raise

    def __enter__(self):
        """생성된 해시 상태를 제한된 수명의 계산 구간에 연결합니다."""
        return self

    def __exit__(self, error_type, error, traceback):
        """원래 계산 오류를 보존하면서 해시와 제공자 핸들을 모두 해제합니다."""
        self.close(suppress_errors=error_type is not None)

    def update(self, data):
        """살아 있는 바이트 또는 연속 배열의 읽기 포인터로 원본 복사 없이 내용을 추가합니다."""
        if not self.hash.value or self.finished:
            raise RuntimeError("종료된 시스템 SHA-256 상태에 입력을 추가할 수 없습니다.")
        if isinstance(data, bytes):
            pointer = ctypes.cast(ctypes.c_char_p(data), ctypes.c_void_p).value
            size = len(data)
        elif isinstance(data, np.ndarray) and data.flags.c_contiguous and not data.dtype.hasobject:
            pointer, size = int(data.ctypes.data), int(data.nbytes)
        else:
            raise TypeError("시스템 SHA-256은 바이트 또는 연속된 수치 배열만 받습니다.")
        # 입력 객체를 호출 내내 보관하여 읽기 전용 포인터의 원본 수명을 유지합니다.
        for start in range(0, size, ULONG_MAXIMUM):
            count = min(size - start, ULONG_MAXIMUM)
            check_status(self.api.BCryptHashData(self.hash, ctypes.c_void_p(pointer + start), count, 0), "입력 추가")

    def hexdigest(self):
        """한 번의 최종화로 표준 SHA-256 32바이트를 소문자 16진수로 반환합니다."""
        if not self.hash.value or self.finished:
            raise RuntimeError("종료된 시스템 SHA-256 상태를 다시 최종화할 수 없습니다.")
        output = (ctypes.c_ubyte * 32)()
        check_status(self.api.BCryptFinishHash(self.hash, output, len(output), 0), "최종화")
        self.finished = True
        return bytes(output).hex()

    def close(self, *, suppress_errors=False):
        """어느 해제 호출이 실패해도 다른 핸들의 해제를 시도하고 중복 해제를 막습니다."""
        failure = None
        if self.hash.value:
            handle, self.hash = self.hash, ctypes.c_void_p()
            try:
                check_status(self.api.BCryptDestroyHash(handle), "해시 해제")
            except Exception as error:
                failure = error
        if self.algorithm.value:
            handle, self.algorithm = self.algorithm, ctypes.c_void_p()
            try:
                check_status(self.api.BCryptCloseAlgorithmProvider(handle, 0), "제공자 해제")
            except Exception as error:
                if failure is None:
                    failure = error
        if failure is not None and not suppress_errors:
            raise failure

    def __del__(self):
        """생성 중 오류나 문맥 누락이 있어도 소유한 핸들의 마지막 해제를 시도합니다."""
        try:
            self.close(suppress_errors=True)
        except Exception:
            pass


def feed_array(digest, array, dtype, *, block_rows=BLOCK_ROWS):
    """기존 형상 지문과 같은 자료형·형상 접두사 및 표준 블록 순서를 유지합니다."""
    if type(block_rows) is not int or block_rows <= 0:
        raise ValueError("해시 블록의 행 수는 양의 정수여야 합니다.")
    digest.update(f"{dtype}:{array.shape}".encode("ascii"))
    for start in range(0, len(array), block_rows):
        block = np.ascontiguousarray(array[start:start + block_rows], dtype=np.dtype(dtype))
        digest.update(block)


def reference_array_fingerprint(array, dtype, *, block_rows=BLOCK_ROWS):
    """현재 제품의 SHA-256 접두사와 블록 바이트를 그대로 계산합니다."""
    digest = hashlib.sha256()
    feed_array(digest, array, dtype, block_rows=block_rows)
    return digest.hexdigest()


def cng_array_fingerprint(array, dtype, *, block_rows=BLOCK_ROWS):
    """기존과 동일한 모든 바이트를 시스템 SHA-256으로 계산하고 핸들을 해제합니다."""
    with CngSha256() as digest:
        feed_array(digest, array, dtype, block_rows=block_rows)
        return digest.hexdigest()


def small_correctness_checks():
    """벤치마크 없이 작은 알려진 벡터와 빈 배열·비연속 배열의 동일 지문만 확인합니다."""
    for content in (b"", b"abc", bytes(range(256))):
        with CngSha256() as digest:
            digest.update(content)
            if digest.hexdigest() != hashlib.sha256(content).hexdigest():
                raise AssertionError("시스템 SHA-256의 표준 바이트 지문이 다릅니다.")
    arrays = ((np.empty((0, 3), dtype=np.float64), "<f8"),
              (np.arange(96, dtype=np.float64).reshape(8, 12)[:, ::4], "<f8"),
              (np.arange(24, dtype=np.uint32).reshape(8, 3)[::-1], "<u4"))
    for array, dtype in arrays:
        if reference_array_fingerprint(array, dtype) != cng_array_fingerprint(array, dtype):
            raise AssertionError("시스템 SHA-256의 형상 지문이 기존 구현과 다릅니다.")
    return {"byte_vectors": 3, "array_vectors": len(arrays), "same_sha256": True}


def runtime_metadata():
    """실측 해석에 필요한 파이썬·OpenSSL·실제 해시 제공자·시스템 DLL 위치를 기록합니다."""
    sample = hashlib.sha256()
    return {"python_executable": sys.executable, "python_version": sys.version,
        "openssl_version": ssl.OPENSSL_VERSION, "hashlib_constructor_module": hashlib.sha256.__module__,
        "hashlib_type_module": type(sample).__module__, "hashlib_source": hashlib.__file__,
        "system_bcrypt_dll": str(cng_api().dll_path), "cng_provider": MICROSOFT_PROVIDER}


def benchmark_geometry(manifest_path, repeats):
    """명시적으로 허용한 때만 실제 네 배열을 같은 블록·해시로 반복 비교합니다."""
    if type(repeats) is not int or repeats < 1:
        raise ValueError("실측 반복 수는 양의 정수여야 합니다.")
    manifest_path = Path(manifest_path).resolve(strict=True)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    arrays, identities = {}, {}
    for role in ("moving", "reference"):
        for field, count_name, dtype in (("positions_file", "point_count", "<f8"),
                                        ("triangles_file", "triangle_count", "<u4")):
            path = (manifest_path.parent / manifest[role][field]).resolve(strict=True)
            if not path.is_relative_to(manifest_path.parent):
                raise ValueError("실측 원본 바이너리가 지정된 원본 폴더를 벗어났습니다.")
            count = manifest[role][count_name]
            if type(count) is not int or count < 1 or path.stat().st_size != count * 3 * np.dtype(dtype).itemsize:
                raise ValueError("실측 원본 바이너리 크기와 명세가 다릅니다.")
            name = role + "_" + field.removesuffix("_file")
            arrays[name] = (np.memmap(path, mode="r", dtype=dtype, shape=(count, 3)), dtype)
            identities[name] = {"path": str(path), "bytes": path.stat().st_size, "mtime_ns": path.stat().st_mtime_ns}
    methods = {"existing_sha256": reference_array_fingerprint, "system_cng_sha256": cng_array_fingerprint}
    runs, expected = [], {}
    for repeat in range(repeats + 1):
        order = tuple(methods) if repeat % 2 == 0 else tuple(reversed(methods))
        for method in order:
            began = perf_counter()
            row = {"method": method, "repeat": repeat, "warmup": repeat == 0, "arrays": {}}
            for name, (array, dtype) in arrays.items():
                start = perf_counter()
                fingerprint = methods[method](array, dtype)
                row["arrays"][name] = {"seconds": perf_counter() - start, "fingerprint": fingerprint,
                    "bytes": array.nbytes, "point_or_triangle_count": len(array)}
                if name not in expected:
                    expected[name] = fingerprint
                elif fingerprint != expected[name]:
                    raise AssertionError(f"실측 중 기존과 다른 SHA-256 지문을 반환했습니다: {method}/{name}")
            row["total_seconds"] = perf_counter() - began
            runs.append(row)
    for identity in identities.values():
        stat = Path(identity["path"]).stat()
        if (stat.st_size, stat.st_mtime_ns) != (identity["bytes"], identity["mtime_ns"]):
            raise ValueError("실측 중 읽기 전용 원본 파일이 변경되었습니다.")
    return {"scope": "EXPERIMENT_ONLY", "same_sha256": True, "prefix": "ASCII dtype:shape followed by C-order canonical bytes",
        "block_rows": BLOCK_ROWS, "source_files": identities, "runs": runs,
        "median_seconds": {name: statistics.median(row["total_seconds"] for row in runs if row["method"] == name and not row["warmup"])
                           for name in methods}}


def main():
    """기본 작은 검증만 실행하며 실제 원본 실측은 별도 선택한 경우에만 시작합니다."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--benchmark", action="store_true")
    parser.add_argument("--geometry", type=Path, default=ROOT / "build/verification/point-kd-20261021/baseline/geometry.json")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--output", type=Path)
    options = parser.parse_args()
    report = {"runtime": runtime_metadata(), "correctness": small_correctness_checks(), "benchmark_performed": options.benchmark}
    if options.benchmark:
        report["benchmark"] = benchmark_geometry(options.geometry, options.repeats)
    content = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False)
    if options.output is not None:
        options.output.parent.mkdir(parents=True, exist_ok=True)
        options.output.write_text(content, encoding="utf-8")
    print(content)


if __name__ == "__main__":
    main()
