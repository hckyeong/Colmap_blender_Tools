"""검증된 네이티브 라이브러리로 솔버 입력 메시의 유효면 마스크를 계산합니다."""

from __future__ import annotations

import ctypes
import hashlib
from pathlib import Path
from typing import Any, Callable, Mapping

import numpy as np

if __package__:
    from . import icp_cccore_contract as contract
else:
    import icp_cccore_contract as contract


_CANCEL_CALLBACK = ctypes.CFUNCTYPE(ctypes.c_int)
_BLOCK_SIZE = 65_536


class NativeMeshCleaner:
    """작업에서 검증한 라이브러리와 취소 함수를 유지해 네 번의 메시 검사를 수행합니다."""

    def __init__(
        self, executable: Path, runtime_manifest: Mapping[str, Any], *,
        max_threads: int, check_cancelled: Callable[[], None],
    ) -> None:
        """실행 파일 옆 라이브러리의 해시와 인터페이스 버전을 확인하고 함수를 연결합니다."""
        executable = Path(executable)
        if not executable.is_absolute():
            raise contract.CcCoreEnvironmentError("CC 메시 검사 실행 파일의 절대 경로가 필요합니다.")
        self.path = executable.with_name("colmap_mesh_cleanup.dll")
        if type(runtime_manifest.get("mesh_cleanup_abi")) is not int or runtime_manifest["mesh_cleanup_abi"] != 1:
            raise contract.CcCoreEnvironmentError("CC 메시 검사 라이브러리의 인터페이스 버전이 올바르지 않습니다.")
        hashes = runtime_manifest.get("binary_sha256")
        expected = hashes.get(self.path.name) if isinstance(hashes, Mapping) else None
        if not isinstance(expected, str) or len(expected) != 64:
            raise contract.CcCoreEnvironmentError("CC 메시 검사 라이브러리의 검증된 해시가 없습니다.")
        try:
            digest = hashlib.sha256()
            with self.path.open("rb") as stream:
                for block in iter(lambda: stream.read(1_048_576), b""):
                    digest.update(block)
            if digest.hexdigest() != expected:
                raise contract.CcCoreEnvironmentError("CC 메시 검사 라이브러리의 해시가 변경되었습니다.")
            self._library = ctypes.CDLL(str(self.path))
            abi = self._library.colmap_mesh_cleanup_abi
            abi.argtypes = []
            abi.restype = ctypes.c_uint32
            if abi() != 1:
                raise contract.CcCoreEnvironmentError("CC 메시 검사 라이브러리의 인터페이스 버전이 일치하지 않습니다.")
            self._cleanup = self._library.colmap_mesh_cleanup
            self._cleanup.argtypes = [
                ctypes.POINTER(ctypes.c_double), ctypes.c_uint64,
                ctypes.POINTER(ctypes.c_uint32), ctypes.c_uint64,
                ctypes.c_uint32, ctypes.c_uint32,
                ctypes.POINTER(ctypes.c_uint8), ctypes.POINTER(ctypes.c_uint8), _CANCEL_CALLBACK,
            ]
            self._cleanup.restype = ctypes.c_int32
        except (OSError, AttributeError) as error:
            raise contract.CcCoreEnvironmentError("CC 메시 검사 라이브러리를 불러오지 못했습니다.") from error
        if isinstance(max_threads, bool) or not isinstance(max_threads, int) or not 1 <= max_threads <= np.iinfo(np.uint32).max:
            raise contract.CcCoreEnvironmentError("CC 메시 검사 스레드 수가 올바르지 않습니다.")
        self.max_threads = max_threads
        self.check_cancelled = check_cancelled

    def __call__(
        self, positions: np.ndarray, triangles: np.ndarray, *, native_precision: bool = False,
    ) -> tuple[np.ndarray, np.ndarray]:
        """필요한 배열만 변환하고 네이티브 마스크에 따라 원본 순서를 보존해 압축합니다."""
        positions = np.asarray(positions)
        triangles = np.asarray(triangles)
        if (positions.ndim != 2 or positions.shape[1:] != (3,)
                or positions.dtype.kind not in "fiu" or len(positions) > np.iinfo(np.uint32).max):
            raise ValueError("정렬 메시 좌표 형식이 올바르지 않습니다.")
        if (triangles.ndim != 2 or triangles.shape[1:] != (3,)
                or triangles.dtype.kind not in "iu"):
            raise ValueError("정렬 메시 삼각면 인덱스 형식이 올바르지 않습니다.")
        self.check_cancelled()
        # 범위 밖 정수는 도달 불가능한 인덱스로 표시해 네이티브 오류 검사 순서를 보존합니다.
        if triangles.dtype != np.dtype(np.uint32):
            native_triangles = np.empty(triangles.shape, dtype=np.uint32)
            for start in range(0, len(triangles), _BLOCK_SIZE):
                self.check_cancelled()
                faces = triangles[start:start + _BLOCK_SIZE]
                invalid = (faces < 0) | (faces >= len(positions))
                target = native_triangles[start:start + _BLOCK_SIZE]
                if np.any(invalid):
                    target[invalid] = np.iinfo(np.uint32).max
                    target[~invalid] = faces[~invalid]
                else:
                    target[:] = faces
        else:
            native_triangles = np.ascontiguousarray(triangles)
        native_positions = np.ascontiguousarray(positions, dtype=np.float64)
        valid = np.zeros(len(triangles), dtype=bool)
        used = np.zeros(len(positions), dtype=bool)
        cancellation: list[BaseException] = []

        def cancel() -> int:
            """네이티브 호출 경계를 넘기지 않고 취소 예외를 보관합니다."""
            if cancellation:
                return 1
            try:
                self.check_cancelled()
            except BaseException as error:
                cancellation.append(error)
                return 1
            return 0

        callback = _CANCEL_CALLBACK(cancel)
        status = self._cleanup(
            native_positions.ctypes.data_as(ctypes.POINTER(ctypes.c_double)), len(positions),
            native_triangles.ctypes.data_as(ctypes.POINTER(ctypes.c_uint32)), len(triangles),
            int(native_precision), self.max_threads,
            valid.ctypes.data_as(ctypes.POINTER(ctypes.c_uint8)),
            used.ctypes.data_as(ctypes.POINTER(ctypes.c_uint8)), callback,
        )
        if cancellation:
            raise cancellation[0]
        errors = {
            1: "정렬 메시 좌표 또는 삼각면 인덱스 형식이 올바르지 않습니다.",
            2: "정렬 메시 좌표에 유한하지 않은 값이 있습니다.",
            3: "정렬 메시 삼각면 인덱스가 정점 범위를 벗어났습니다.",
            4: "정렬 메시 좌표가 단정도 유한 범위를 벗어났습니다.",
            5: "정렬 메시 변의 좌표 차이가 유한 범위를 벗어났습니다.",
        }
        if status in errors:
            raise ValueError(errors[status])
        if status == 6:
            raise contract.CcCoreCancelled("CC 메시 검사가 취소되었습니다.")
        if status != 0:
            raise contract.CcCoreEnvironmentError(f"CC 메시 검사 라이브러리에서 내부 오류가 발생했습니다: {status}")
        self.check_cancelled()
        if not np.any(valid):
            raise ValueError("정렬 메시의 모든 삼각면이 퇴화하여 유효한 표면이 없습니다.")
        kept_faces = triangles if np.all(valid) else triangles[valid]
        if np.all(used):
            return positions, kept_faces
        mapping = np.empty(len(positions), dtype=np.uint32)
        mapping[used] = np.arange(np.count_nonzero(used), dtype=np.uint32)
        remapped = np.empty(kept_faces.shape, dtype=np.uint32)
        for start in range(0, len(kept_faces), _BLOCK_SIZE):
            self.check_cancelled()
            remapped[start:start + _BLOCK_SIZE] = mapping[kept_faces[start:start + _BLOCK_SIZE]]
        return positions[used], remapped
