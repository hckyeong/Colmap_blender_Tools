"""CCCoreLib 외부 정렬 작업의 순수 Python 파일·배열 계약입니다."""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
import os
from pathlib import Path, PureWindowsPath
import tempfile
from time import monotonic, sleep
from typing import Any, Mapping

import numpy as np

CC_BACKEND_ID = "CCCORELIB"
CC_ALGORITHM_VERSION = "cccore-icp-v4"
CC_JOB_PROTOCOL_VERSION = "cccore-job-v4"
CC_NATIVE_PROTOCOL_VERSION = "cccore-native-v4"
CC_LEGACY_NATIVE_PROTOCOL_VERSION = "cccore-native-v2"
CC_THREAD_BACKEND = "oneTBB"
CC_TBB_VERSION = "2022.0.0"
CC_MAX_THREADS = 1024
CC_DEFAULT_MAX_ITERATIONS = 200
CC_DEFAULT_SCALE_REFINE_MAX_ITERATIONS = 1000
CC_MAX_PREPARING_OPERATIONS = 2048
CC_NATIVE_OPERATION_SEQUENCES = {
    "POINT_TO_POINT": ("distance", "distance", "register", "distance", "distance", "register", "distance"),
    "POINT_TO_MESH": ("distance", "distance", "register", "distance", "distance", "register", "distance"),
    "MESH_TO_POINT": ("distance", "distance", "register", "distance", "distance", "register", "distance"),
    "MESH_TO_MESH": (
        "distance", "distance", "distance", "register", "distance",
        "distance", "distance", "register", "distance", "distance",
    ),
}
CC_NATIVE_OPERATION_STAGES = {
    "POINT_TO_POINT": (
        "BASELINE", "RIGID", "RIGID", "RIGID_EVALUATION",
        "SCALE_REFINE", "SCALE_REFINE", "REFINE_EVALUATION",
    ),
    "POINT_TO_MESH": (
        "BASELINE", "RIGID", "RIGID", "RIGID_EVALUATION", "SCALE_REFINE", "SCALE_REFINE", "REFINE_EVALUATION",
    ),
    "MESH_TO_POINT": (
        "BASELINE", "RIGID", "RIGID", "RIGID_EVALUATION", "SCALE_REFINE", "SCALE_REFINE", "REFINE_EVALUATION",
    ),
    "MESH_TO_MESH": (
        "BASELINE", "BASELINE", "RIGID", "RIGID", "RIGID_EVALUATION",
        "RIGID_EVALUATION", "SCALE_REFINE", "SCALE_REFINE", "REFINE_EVALUATION", "REFINE_EVALUATION",
    ),
}
CC_NATIVE_TIMING_FIELDS = (
    "binary_read_seconds", "conversion_validation_seconds", "mesh_load_seconds",
    "octree_seconds", "mesh_grid_seconds", "kernel_seconds", "serialization_seconds",
)
CC_NATIVE_COUNTER_FIELDS = (
    "geometry_load_count", "geometry_cache_hits", "octree_build_count", "octree_cache_hits",
    "mesh_grid_build_count", "mesh_grid_cache_hits",
)
CC_MIN_SCALE = 0.9
CC_MAX_SCALE = 1.1
CC_OPTIMIZER_SEED = 1729
CC_EVALUATION_SEED = 104729
GEOMETRY_KINDS = ("COLMAP_POINT_CLOUD", "TRIANGLE_MESH")
GEOMETRY_MODES = {
    ("COLMAP_POINT_CLOUD", "COLMAP_POINT_CLOUD"): "POINT_TO_POINT",
    ("COLMAP_POINT_CLOUD", "TRIANGLE_MESH"): "POINT_TO_MESH",
    ("TRIANGLE_MESH", "COLMAP_POINT_CLOUD"): "MESH_TO_POINT",
    ("TRIANGLE_MESH", "TRIANGLE_MESH"): "MESH_TO_MESH",
}


class CcCoreError(RuntimeError):
    """CC 정렬 작업의 기본 오류입니다."""


class CcCoreInputError(CcCoreError):
    """작업 파일이나 입력 형상이 계약을 벗어났습니다."""


class CcCoreNumericalError(CcCoreError):
    """정렬 결과나 거리 계산이 수치 계약을 벗어났습니다."""


class CcCoreEnvironmentError(CcCoreError):
    """CC 계산 실행 파일 또는 프로세스 환경이 올바르지 않습니다."""


class CcCoreCancelled(CcCoreError):
    """사용자가 CC 정렬 작업의 취소를 요청했습니다."""


def job_path(directory: Path | str, relative_name: str) -> Path:
    """작업 경계를 벗어나는 절대·상위·심볼릭 링크 경로를 거부합니다."""
    root = Path(directory).resolve(strict=True)
    if not isinstance(relative_name, str) or not relative_name.strip():
        raise CcCoreInputError("작업 파일명은 비어 있지 않은 문자열이어야 합니다.")
    name = relative_name.strip()
    windows_name = PureWindowsPath(name)
    if Path(name).is_absolute() or windows_name.drive or windows_name.root:
        raise CcCoreInputError("작업 파일명은 상대 경로여야 합니다.")
    if ".." in windows_name.parts or ":" in name:
        raise CcCoreInputError("작업 파일명에 상위 경로나 대체 스트림을 사용할 수 없습니다.")
    target = (root / name).resolve(strict=False)
    if not target.is_relative_to(root) or target == root:
        raise CcCoreInputError("작업 파일이 전용 디렉터리 경계를 벗어났습니다.")
    return target


def _open_json_shared(path: Path):
    """Windows 원자 교체와 공존하도록 삭제 공유를 허용한 읽기 핸들을 엽니다."""
    if os.name != "nt":
        return path.open("r", encoding="utf-8")
    import ctypes
    from ctypes import wintypes
    import msvcrt
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = (
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    )
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    handle = kernel.CreateFileW(str(path), 0x80000000, 0x00000007, None, 3, 0x80, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        descriptor = msvcrt.open_osfhandle(int(handle), os.O_RDONLY)
    except Exception:
        kernel.CloseHandle(handle)
        raise
    try:
        return os.fdopen(descriptor, "r", encoding="utf-8")
    except Exception:
        os.close(descriptor)
        raise


def read_json_object(path: Path | str) -> dict[str, Any]:
    """원자 교체 중 일시적 공유 충돌만 재시도하여 유한한 UTF-8 JSON 객체를 읽습니다."""
    def reject_constant(value: str) -> None:
        """JSON의 비표준 무한값을 거부합니다."""
        raise CcCoreInputError(f"JSON에 유한하지 않은 수가 있습니다: {value}")
    deadline = monotonic() + 0.24
    while True:
        try:
            with _open_json_shared(Path(path)) as stream:
                value = json.load(stream, parse_constant=reject_constant)
            break
        except OSError as error:
            if os.name == "nt" and getattr(error, "winerror", None) in (5, 32, 33) and monotonic() < deadline:
                sleep(0.005)
                continue
            raise CcCoreInputError(f"작업 JSON을 읽지 못했습니다: {Path(path).name}") from error
        except (UnicodeError, ValueError) as error:
            raise CcCoreInputError(f"작업 JSON을 읽지 못했습니다: {Path(path).name}") from error
    if not isinstance(value, dict):
        raise CcCoreInputError("작업 JSON 최상위 값은 객체여야 합니다.")
    return value


def _replace_atomic(temporary: Path, target: Path) -> None:
    """Windows 읽기 핸들의 일시적 충돌만 제한 시간 동안 재시도합니다."""
    deadline = monotonic() + 1.0
    while True:
        try:
            os.replace(temporary, target)
            return
        except PermissionError:
            if monotonic() >= deadline:
                raise
            sleep(0.01)


def write_json_atomic(path: Path | str, payload: Mapping[str, Any]) -> None:
    """완성된 UTF-8 JSON만 원자 교체하여 작업 관찰자에게 공개합니다."""
    target = Path(path)
    content = json.dumps(payload, ensure_ascii=False, allow_nan=False, sort_keys=True)
    descriptor, name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        _replace_atomic(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def write_arrays_atomic(path: Path | str, arrays: Mapping[str, np.ndarray]) -> None:
    """최종 행렬 NPZ를 임시 파일에서 완성한 다음 원자적으로 공개합니다."""
    target = Path(path)
    descriptor, name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            np.savez(stream, **arrays)
            stream.flush()
            os.fsync(stream.fileno())
        _replace_atomic(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def positive_integer(value: Any, field_name: str) -> int:
    """불리언이나 실수를 정수로 조용히 바꾸지 않고 양의 정수를 검증합니다."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise CcCoreInputError(f"{field_name}은 양의 정수여야 합니다.")
    return value


def nonnegative_integer(value: Any, field_name: str) -> int:
    """불리언과 음수를 제외한 0 이상의 정수를 검증합니다."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise CcCoreEnvironmentError(f"{field_name}은 0 이상의 정수여야 합니다.")
    return value


def finite_nonnegative_number(value: Any, field_name: str) -> float:
    """불리언과 비유한 값을 제외한 0 이상의 실수를 검증합니다."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CcCoreEnvironmentError(f"{field_name}은 0 이상의 유한한 수여야 합니다.")
    result = float(value)
    if not math.isfinite(result) or result < 0.0:
        raise CcCoreEnvironmentError(f"{field_name}은 0 이상의 유한한 수여야 합니다.")
    return result


def validate_native_execution_metadata(
    payload: Mapping[str, Any],
    *,
    requested_max_threads: Any,
    expected_identity: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """네이티브 호출의 oneTBB 스레드 수와 코어 식별 계약을 엄격히 검증합니다."""
    if not isinstance(payload, Mapping):
        raise CcCoreEnvironmentError("CC 네이티브 실행 정보가 객체가 아닙니다.")
    try:
        requested = positive_integer(requested_max_threads, "CC 요청 스레드 수")
        reported_requested = positive_integer(payload.get("requested_max_threads"), "CC 보고 요청 스레드 수")
        effective = positive_integer(payload.get("effective_max_threads"), "CC 실제 스레드 수")
        hardware = positive_integer(payload.get("hardware_thread_count"), "CC 하드웨어 스레드 수")
    except CcCoreInputError as error:
        raise CcCoreEnvironmentError(str(error)) from error
    if requested > CC_MAX_THREADS or reported_requested > CC_MAX_THREADS:
        raise CcCoreEnvironmentError(f"CC 요청 스레드 수는 {CC_MAX_THREADS} 이하여야 합니다.")
    if reported_requested != requested:
        raise CcCoreEnvironmentError("CC 네이티브 결과의 요청 스레드 수가 작업 요청과 다릅니다.")
    if effective != min(requested, hardware):
        raise CcCoreEnvironmentError("CC 네이티브 결과의 실제 스레드 수가 요청·하드웨어 상한과 다릅니다.")
    if payload.get("thread_backend") != CC_THREAD_BACKEND:
        raise CcCoreEnvironmentError("CC 네이티브 결과가 oneTBB 병렬 백엔드를 보고하지 않았습니다.")
    if payload.get("tbb_version") != CC_TBB_VERSION:
        raise CcCoreEnvironmentError("CC 네이티브 결과의 oneTBB 버전이 고정 계약과 다릅니다.")
    for name in ("distance_multithread_support", "octree_multithread_support"):
        if payload.get(name) is not True:
            raise CcCoreEnvironmentError(f"CC 네이티브 결과의 {name} 지원이 활성 상태가 아닙니다.")
    commit = payload.get("core_commit")
    if not isinstance(commit, str) or len(commit) != 40 or any(character not in "0123456789abcdef" for character in commit.lower()):
        raise CcCoreEnvironmentError("CC 네이티브 결과의 코어 커밋 식별자가 올바르지 않습니다.")
    patch = payload.get("core_patch")
    if not isinstance(patch, str) or not patch.strip():
        raise CcCoreEnvironmentError("CC 네이티브 결과의 코어 패치 식별자가 없습니다.")
    elapsed = finite_nonnegative_number(payload.get("elapsed_seconds"), "CC 네이티브 실행 시간")
    identity = {
        "thread_backend": CC_THREAD_BACKEND,
        "tbb_version": CC_TBB_VERSION,
        "requested_max_threads": requested,
        "effective_max_threads": effective,
        "hardware_thread_count": hardware,
        "distance_multithread_support": True,
        "octree_multithread_support": True,
        "core_commit": commit,
        "core_patch": patch.strip(),
    }
    if expected_identity is not None:
        for name, value in identity.items():
            if expected_identity.get(name) != value:
                raise CcCoreEnvironmentError(f"CC 순차 호출의 {name} 값이 첫 호출과 다릅니다.")
    return {**identity, "elapsed_seconds": elapsed}


def validate_native_session_identity(
    payload: Mapping[str, Any], *, session_id: str, job_id: str,
    input_fingerprint: str, sequence: int | None = None,
) -> None:
    """세션 응답의 프로토콜·세션·작업 식별자와 선택적 순번을 검증합니다."""
    if not isinstance(payload, Mapping):
        raise CcCoreEnvironmentError("CC 네이티브 세션 응답이 객체가 아닙니다.")
    expected = {
        "protocol_version": CC_NATIVE_PROTOCOL_VERSION,
        "session_id": session_id,
        "job_id": job_id,
        "input_fingerprint": input_fingerprint,
    }
    if sequence is not None:
        expected["sequence"] = sequence
    for name, value in expected.items():
        if payload.get(name) != value:
            raise CcCoreEnvironmentError(f"CC 네이티브 세션 응답의 {name} 값이 현재 요청과 다릅니다.")


def validate_native_timings(value: Any) -> dict[str, float]:
    """명령별 적재·가속구조·커널·직렬화 시간을 고정 필드로 검증합니다."""
    if not isinstance(value, Mapping) or set(value) != set(CC_NATIVE_TIMING_FIELDS):
        raise CcCoreEnvironmentError("CC 네이티브 명령의 단계별 시간 정보가 고정 계약과 다릅니다.")
    return {
        name: finite_nonnegative_number(value.get(name), f"CC {name}")
        for name in CC_NATIVE_TIMING_FIELDS
    }


def validate_native_session_statistics(value: Any) -> dict[str, int]:
    """세션 전체 캐시·가속구조 횟수와 메모리 최고값을 검증합니다."""
    expected_keys = set(CC_NATIVE_COUNTER_FIELDS) | {
        "peak_working_set_bytes", "cache_peak_estimated_bytes", "cache_resident_estimated_bytes",
    }
    if not isinstance(value, Mapping) or set(value) != expected_keys:
        raise CcCoreEnvironmentError("CC 네이티브 세션 통계가 고정 계약과 다릅니다.")
    return {
        name: nonnegative_integer(value.get(name), f"CC {name}")
        for name in sorted(expected_keys)
    }


def validate_native_geometry_statistics(value: Any) -> tuple[dict[str, Any], ...]:
    """세션 종료의 geometry별 적재·캐시·메모리 통계를 엄격히 검증합니다."""
    expected_keys = {
        "geometry_id", "point_count", "triangle_count", "load_count", "cache_hits",
        "binary_read_seconds", "conversion_validation_seconds", "mesh_load_seconds", "estimated_bytes",
        "cache_scope", "released_after_command",
    }
    if not isinstance(value, list) or not value:
        raise CcCoreEnvironmentError("CC 네이티브 지오메트리 통계가 비어 있거나 목록이 아닙니다.")
    result = []
    identifiers = set()
    for item in value:
        if not isinstance(item, Mapping) or set(item) != expected_keys:
            raise CcCoreEnvironmentError("CC 네이티브 지오메트리 통계 항목이 고정 계약과 다릅니다.")
        identifier = item.get("geometry_id")
        if not isinstance(identifier, str) or not identifier.strip() or identifier in identifiers:
            raise CcCoreEnvironmentError("CC 네이티브 지오메트리 통계 ID가 없거나 중복됩니다.")
        identifiers.add(identifier)
        try:
            point_count = positive_integer(item.get("point_count"), "CC 지오메트리 좌표 수")
        except CcCoreInputError as error:
            raise CcCoreEnvironmentError(str(error)) from error
        triangle_count = nonnegative_integer(item.get("triangle_count"), "CC 지오메트리 삼각면 수")
        load_count = nonnegative_integer(item.get("load_count"), "CC 지오메트리 적재 수")
        if load_count != 1:
            raise CcCoreEnvironmentError("CC 캐시의 각 지오메트리는 정확히 한 번 적재되어야 합니다.")
        cache_scope = item.get("cache_scope")
        released = item.get("released_after_command")
        if cache_scope not in {"SESSION", "COMMAND"} or type(released) is not bool:
            raise CcCoreEnvironmentError("CC 지오메트리 캐시 범위 또는 해제 상태가 올바르지 않습니다.")
        if released is not (cache_scope == "COMMAND"):
            raise CcCoreEnvironmentError("CC 지오메트리의 명령 종료 해제 상태가 캐시 범위와 다릅니다.")
        result.append({
            "geometry_id": identifier,
            "point_count": point_count,
            "triangle_count": triangle_count,
            "load_count": load_count,
            "cache_hits": nonnegative_integer(item.get("cache_hits"), "CC 지오메트리 캐시 적중 수"),
            "binary_read_seconds": finite_nonnegative_number(item.get("binary_read_seconds"), "CC 지오메트리 읽기 시간"),
            "conversion_validation_seconds": finite_nonnegative_number(
                item.get("conversion_validation_seconds"), "CC 지오메트리 변환·검증 시간",
            ),
            "mesh_load_seconds": finite_nonnegative_number(item.get("mesh_load_seconds"), "CC 메시 적재 시간"),
            "estimated_bytes": nonnegative_integer(item.get("estimated_bytes"), "CC 지오메트리 추정 바이트"),
            "cache_scope": cache_scope,
            "released_after_command": released,
        })
    return tuple(result)


def validate_native_session_aggregates(
    session_statistics: Any,
    geometry_statistics: Any,
    physical_operations: Any,
) -> None:
    """세션 합계가 지오메트리 수명과 실제 네이티브 명령 합계에 맞는지 검증합니다."""
    session = validate_native_session_statistics(session_statistics)
    geometries = validate_native_geometry_statistics(list(geometry_statistics))
    if not isinstance(physical_operations, (list, tuple)) or not physical_operations:
        raise CcCoreEnvironmentError("CC 실제 네이티브 명령 통계가 비어 있거나 목록이 아닙니다.")
    physical_totals = {name: 0 for name in CC_NATIVE_COUNTER_FIELDS}
    for operation in physical_operations:
        if not isinstance(operation, Mapping):
            raise CcCoreEnvironmentError("CC 실제 네이티브 명령 통계 항목이 객체가 아닙니다.")
        for name in CC_NATIVE_COUNTER_FIELDS:
            physical_totals[name] += nonnegative_integer(
                operation.get(name), f"CC 실제 명령 합계 {name}",
            )
    geometry_loads = sum(item["load_count"] for item in geometries)
    geometry_hits = sum(item["cache_hits"] for item in geometries)
    if (
        session["geometry_load_count"] != geometry_loads
        or session["geometry_cache_hits"] != geometry_hits
    ):
        raise CcCoreEnvironmentError("CC 세션 지오메트리 합계가 항목별 적재·캐시 통계와 다릅니다.")
    resident = sum(
        item["estimated_bytes"]
        for item in geometries
        if not item["released_after_command"]
    )
    if session["cache_resident_estimated_bytes"] != resident:
        raise CcCoreEnvironmentError("CC 세션 종료 시 상주 캐시 바이트가 SESSION 지오메트리 합계와 다릅니다.")
    if session["cache_peak_estimated_bytes"] < resident:
        raise CcCoreEnvironmentError("CC 세션 캐시 최고 바이트가 종료 시 상주 바이트보다 작습니다.")
    for name, total in physical_totals.items():
        if session[name] != total:
            raise CcCoreEnvironmentError(f"CC 세션 {name} 합계가 실제 네이티브 명령 합계와 다릅니다.")


def _validate_evaluation_key_sha256(value: Any, *, required: bool) -> str:
    """평가 캐시 키 해시는 빈 문자열 또는 소문자 SHA-256만 허용합니다."""
    if not isinstance(value, str) or (required and not value):
        raise CcCoreEnvironmentError("CC 평가 캐시 키 해시가 없습니다.")
    if value and (len(value) != 64 or any(character not in "0123456789abcdef" for character in value)):
        raise CcCoreEnvironmentError("CC 평가 캐시 키 해시가 소문자 SHA-256이 아닙니다.")
    return value


def validate_native_call_summaries(
    value: Any,
    *,
    geometry_mode: str,
    requested_max_threads: int,
    effective_max_threads: int,
) -> tuple[dict[str, Any], ...]:
    """유한한 후보 준비 뒤 고정 등록·평가 순서와 세션·캐시 연결을 검증합니다."""
    if not isinstance(value, list) or not value:
        raise CcCoreEnvironmentError("CC 최종 결과에 논리 연산 요약이 없습니다.")
    expected_keys = {
        "operation", "index", "sequence", "physical_sequence", "source_sequence",
        "source_logical_index", "stage", "execution_source", "evaluation_key_sha256",
        "elapsed_seconds", "roundtrip_seconds", "protocol_overhead_seconds",
        "requested_max_threads", "effective_max_threads",
        "data_geometry_id", "model_geometry_id", "timings", *CC_NATIVE_COUNTER_FIELDS,
    }
    expected_operations = CC_NATIVE_OPERATION_SEQUENCES.get(geometry_mode)
    expected_stages = CC_NATIVE_OPERATION_STAGES.get(geometry_mode)
    if expected_operations is None or expected_stages is None:
        raise CcCoreEnvironmentError("CC 논리 연산 요약의 형상 모드가 올바르지 않습니다.")
    preparing_count = 0
    for item in value:
        if not isinstance(item, Mapping) or item.get("stage") != "PREPARING":
            break
        preparing_count += 1
    if not 1 <= preparing_count <= CC_MAX_PREPARING_OPERATIONS:
        raise CcCoreEnvironmentError("CC 후보 준비 연산 수가 1~2048 범위를 벗어났습니다.")
    expected_operations = ("distance",) * preparing_count + expected_operations
    expected_stages = ("PREPARING",) * preparing_count + expected_stages
    if len(value) != len(expected_operations):
        raise CcCoreEnvironmentError("CC 논리 연산 요약의 작업 순서 또는 수가 형상 모드 계약과 다릅니다.")
    summaries = []
    next_physical_sequence = 1
    for expected_index, item in enumerate(value, start=1):
        if not isinstance(item, Mapping) or set(item) != expected_keys:
            raise CcCoreEnvironmentError("CC 논리 연산 요약 항목이 고정 계약과 다릅니다.")
        operation = item.get("operation")
        source = item.get("execution_source")
        if operation not in {"register", "distance"} or source not in {
            "native_session", "exact_evaluation_cache",
        }:
            raise CcCoreEnvironmentError("CC 논리 연산의 종류 또는 실행 출처가 올바르지 않습니다.")
        try:
            index = positive_integer(item.get("index"), "CC 논리 연산 순번")
            sequence = positive_integer(item.get("sequence"), "CC 논리 표시 순번")
            source_sequence = positive_integer(item.get("source_sequence"), "CC 캐시 원본 물리 순번")
            source_logical_index = positive_integer(
                item.get("source_logical_index"), "CC 캐시 원본 논리 순번",
            )
            requested = positive_integer(item.get("requested_max_threads"), "CC 호출 요청 스레드 수")
            effective = positive_integer(item.get("effective_max_threads"), "CC 호출 실제 스레드 수")
        except CcCoreInputError as error:
            raise CcCoreEnvironmentError(str(error)) from error
        if index != expected_index or sequence != expected_index:
            raise CcCoreEnvironmentError("CC 논리 연산 순번이 연속적이지 않습니다.")
        if operation != expected_operations[expected_index - 1] or item.get("stage") != expected_stages[expected_index - 1]:
            raise CcCoreEnvironmentError("CC 논리 연산의 작업 순서 또는 단계가 형상 모드 계약과 다릅니다.")
        if requested != requested_max_threads or effective != effective_max_threads:
            raise CcCoreEnvironmentError("CC 논리 연산의 요청·실제 스레드 수가 최종 결과와 다릅니다.")
        for name in ("data_geometry_id", "model_geometry_id"):
            if not isinstance(item.get(name), str) or not item[name].strip():
                raise CcCoreEnvironmentError(f"CC 논리 연산의 {name} 값이 없습니다.")
        physical_sequence = item.get("physical_sequence")
        requires_evaluation_key = operation == "distance" and item.get("stage") in {
            "BASELINE", "RIGID_EVALUATION", "REFINE_EVALUATION",
        }
        evaluation_key = _validate_evaluation_key_sha256(
            item.get("evaluation_key_sha256"), required=requires_evaluation_key,
        )
        if not requires_evaluation_key and evaluation_key:
            raise CcCoreEnvironmentError("CC 준비·등록 연산에는 평가 캐시 키를 기록할 수 없습니다.")
        elapsed = finite_nonnegative_number(item.get("elapsed_seconds"), "CC 논리 연산 시간")
        roundtrip = finite_nonnegative_number(item.get("roundtrip_seconds"), "CC 논리 연산 왕복 시간")
        protocol_overhead = finite_nonnegative_number(
            item.get("protocol_overhead_seconds"), "CC 논리 연산 프로토콜 오버헤드",
        )
        timings = validate_native_timings(item.get("timings"))
        counters = {
            name: nonnegative_integer(item.get(name), f"CC 논리 연산 {name}")
            for name in CC_NATIVE_COUNTER_FIELDS
        }
        if source == "native_session":
            try:
                physical_sequence = positive_integer(physical_sequence, "CC 실제 네이티브 명령 순번")
            except CcCoreInputError as error:
                raise CcCoreEnvironmentError(str(error)) from error
            if (
                physical_sequence != next_physical_sequence
                or source_sequence != physical_sequence
                or source_logical_index != expected_index
            ):
                raise CcCoreEnvironmentError("CC 실제 네이티브 명령 연결 순번이 올바르지 않습니다.")
            if roundtrip < elapsed and not math.isclose(
                roundtrip, elapsed, rel_tol=1.0e-9, abs_tol=1.0e-9,
            ):
                raise CcCoreEnvironmentError("CC 실제 논리 연산의 왕복 시간이 네이티브 실행 시간보다 짧습니다.")
            expected_overhead = max(0.0, roundtrip - elapsed)
            if not math.isclose(protocol_overhead, expected_overhead, rel_tol=1.0e-12, abs_tol=1.0e-12):
                raise CcCoreEnvironmentError("CC 실제 논리 연산의 프로토콜 오버헤드가 왕복·네이티브 시간과 다릅니다.")
            next_physical_sequence += 1
        else:
            if not requires_evaluation_key or physical_sequence is not None:
                raise CcCoreEnvironmentError("CC 정확 평가 캐시는 물리 명령 없는 거리 연산이어야 합니다.")
            if source_logical_index >= expected_index or source_sequence >= next_physical_sequence:
                raise CcCoreEnvironmentError("CC 정확 평가 캐시가 앞선 물리 거리 명령을 참조하지 않습니다.")
            if elapsed != 0.0 or any(timings.values()) or any(counters.values()):
                raise CcCoreEnvironmentError("CC 정확 평가 캐시 적중에 네이티브 시간 또는 작업 횟수가 기록되었습니다.")
        summaries.append({
            "operation": operation,
            "index": index,
            "sequence": sequence,
            "physical_sequence": physical_sequence,
            "source_sequence": source_sequence,
            "source_logical_index": source_logical_index,
            "stage": item["stage"],
            "execution_source": source,
            "evaluation_key_sha256": evaluation_key,
            "elapsed_seconds": elapsed,
            "roundtrip_seconds": roundtrip,
            "protocol_overhead_seconds": protocol_overhead,
            "requested_max_threads": requested,
            "effective_max_threads": effective,
            "data_geometry_id": item["data_geometry_id"],
            "model_geometry_id": item["model_geometry_id"],
            "timings": timings,
            **counters,
        })
    for item in summaries:
        if item["execution_source"] != "exact_evaluation_cache":
            continue
        source = summaries[item["source_logical_index"] - 1]
        if (
            source["execution_source"] != "native_session"
            or source["operation"] != "distance"
            or source["physical_sequence"] != item["source_sequence"]
            or source["evaluation_key_sha256"] != item["evaluation_key_sha256"]
            or source["model_geometry_id"] != item["model_geometry_id"]
            or source["roundtrip_seconds"] != item["roundtrip_seconds"]
            or source["protocol_overhead_seconds"] != item["protocol_overhead_seconds"]
        ):
            raise CcCoreEnvironmentError("CC 정확 평가 캐시의 원본 거리 명령 또는 완전 키가 다릅니다.")
    return tuple(summaries)


def validate_physical_native_call_summaries(
    value: Any,
    logical_operations: tuple[dict[str, Any], ...],
    *,
    requested_max_threads: int,
    effective_max_threads: int,
) -> tuple[dict[str, Any], ...]:
    """실제 세션 명령 목록을 연속 순번과 대응 논리 연산에 맞춰 엄격히 검증합니다."""
    if not isinstance(value, list) or not value:
        raise CcCoreEnvironmentError("CC 최종 결과에 실제 네이티브 명령 요약이 없습니다.")
    expected_keys = {
        "operation", "index", "sequence", "logical_index", "stage", "execution_source",
        "evaluation_key_sha256", "elapsed_seconds", "roundtrip_seconds",
        "protocol_overhead_seconds", "requested_max_threads", "effective_max_threads",
        "data_geometry_id", "model_geometry_id", "timings", *CC_NATIVE_COUNTER_FIELDS,
    }
    native_logical = [item for item in logical_operations if item["execution_source"] == "native_session"]
    if len(value) != len(native_logical):
        raise CcCoreEnvironmentError("CC 실제 네이티브 명령 수가 논리 연산 연결과 다릅니다.")
    summaries = []
    for expected_sequence, (item, logical) in enumerate(zip(value, native_logical), start=1):
        if not isinstance(item, Mapping) or set(item) != expected_keys:
            raise CcCoreEnvironmentError("CC 실제 네이티브 명령 요약 항목이 고정 계약과 다릅니다.")
        try:
            index = positive_integer(item.get("index"), "CC 실제 명령 목록 순번")
            sequence = positive_integer(item.get("sequence"), "CC 실제 네이티브 명령 순번")
            logical_index = positive_integer(item.get("logical_index"), "CC 실제 명령의 논리 순번")
            requested = positive_integer(item.get("requested_max_threads"), "CC 실제 명령 요청 스레드 수")
            effective = positive_integer(item.get("effective_max_threads"), "CC 실제 명령 실제 스레드 수")
        except CcCoreInputError as error:
            raise CcCoreEnvironmentError(str(error)) from error
        if index != expected_sequence or sequence != expected_sequence or logical_index != logical["index"]:
            raise CcCoreEnvironmentError("CC 실제 네이티브 명령 또는 논리 연결 순번이 연속적이지 않습니다.")
        if requested != requested_max_threads or effective != effective_max_threads:
            raise CcCoreEnvironmentError("CC 실제 네이티브 명령의 스레드 수가 최종 결과와 다릅니다.")
        evaluation_key = _validate_evaluation_key_sha256(item.get("evaluation_key_sha256"), required=False)
        timings = validate_native_timings(item.get("timings"))
        counters = {
            name: nonnegative_integer(item.get(name), f"CC 실제 명령 {name}")
            for name in CC_NATIVE_COUNTER_FIELDS
        }
        comparable = (
            "operation", "stage", "evaluation_key_sha256", "elapsed_seconds",
            "roundtrip_seconds", "protocol_overhead_seconds",
            "requested_max_threads", "effective_max_threads", "data_geometry_id", "model_geometry_id",
        )
        if item.get("execution_source") != "native_session" or any(item.get(name) != logical[name] for name in comparable):
            raise CcCoreEnvironmentError("CC 실제 네이티브 명령이 연결된 논리 연산과 다릅니다.")
        if timings != logical["timings"] or any(counters[name] != logical[name] for name in CC_NATIVE_COUNTER_FIELDS):
            raise CcCoreEnvironmentError("CC 실제 네이티브 명령의 시간·통계가 연결된 논리 연산과 다릅니다.")
        elapsed = finite_nonnegative_number(item.get("elapsed_seconds"), "CC 실제 명령 시간")
        roundtrip = finite_nonnegative_number(item.get("roundtrip_seconds"), "CC 실제 명령 왕복 시간")
        protocol_overhead = finite_nonnegative_number(
            item.get("protocol_overhead_seconds"), "CC 실제 명령 프로토콜 오버헤드",
        )
        if roundtrip < elapsed and not math.isclose(
            roundtrip, elapsed, rel_tol=1.0e-9, abs_tol=1.0e-9,
        ):
            raise CcCoreEnvironmentError("CC 실제 명령의 왕복 시간이 네이티브 실행 시간보다 짧습니다.")
        expected_overhead = max(0.0, roundtrip - elapsed)
        if not math.isclose(protocol_overhead, expected_overhead, rel_tol=1.0e-12, abs_tol=1.0e-12):
            raise CcCoreEnvironmentError("CC 실제 명령의 프로토콜 오버헤드가 왕복·네이티브 시간과 다릅니다.")
        summaries.append({
            "operation": item["operation"], "index": index, "sequence": sequence,
            "logical_index": logical_index, "stage": item["stage"],
            "execution_source": "native_session", "evaluation_key_sha256": evaluation_key,
            "elapsed_seconds": elapsed, "roundtrip_seconds": roundtrip,
            "protocol_overhead_seconds": protocol_overhead,
            "requested_max_threads": requested, "effective_max_threads": effective,
            "data_geometry_id": item["data_geometry_id"], "model_geometry_id": item["model_geometry_id"],
            "timings": timings, **counters,
        })
    return tuple(summaries)


def validate_session_protocol_statistics(
    value: Any, physical_operations: tuple[dict[str, Any], ...], *, total_alignment_seconds: Any,
) -> dict[str, float]:
    """실제 명령 왕복 합계와 전체 정렬 대비 프로토콜 오버헤드를 교차 검증합니다."""
    expected_keys = {
        "physical_roundtrip_seconds", "protocol_overhead_seconds",
        "protocol_overhead_ratio_of_roundtrip", "protocol_overhead_fraction_of_total_alignment",
    }
    if not isinstance(value, Mapping) or set(value) != expected_keys:
        raise CcCoreEnvironmentError("CC 세션 프로토콜 통계가 고정 계약과 다릅니다.")
    total_alignment = finite_nonnegative_number(total_alignment_seconds, "CC 전체 정렬 시간")
    validated = {
        name: finite_nonnegative_number(value.get(name), f"CC 세션 프로토콜 {name}")
        for name in expected_keys
    }
    if not isinstance(physical_operations, (list, tuple)) or not physical_operations:
        raise CcCoreEnvironmentError("CC 세션 프로토콜 통계에 실제 네이티브 명령이 없습니다.")
    roundtrips = []
    overheads = []
    for item in physical_operations:
        if not isinstance(item, Mapping):
            raise CcCoreEnvironmentError("CC 세션 프로토콜 통계의 실제 명령이 객체가 아닙니다.")
        roundtrips.append(finite_nonnegative_number(item.get("roundtrip_seconds"), "CC 실제 명령 왕복 시간"))
        overheads.append(finite_nonnegative_number(
            item.get("protocol_overhead_seconds"), "CC 실제 명령 프로토콜 오버헤드",
        ))
    roundtrip_total = math.fsum(roundtrips)
    overhead_total = math.fsum(overheads)
    if total_alignment <= 0.0:
        raise CcCoreEnvironmentError("CC 실제 명령이 있는 결과의 전체 정렬 시간이 0입니다.")
    if roundtrip_total > total_alignment and not math.isclose(
        roundtrip_total, total_alignment, rel_tol=1.0e-9, abs_tol=1.0e-9,
    ):
        raise CcCoreEnvironmentError("CC 실제 명령 왕복 합계가 전체 정렬 시간보다 큽니다.")
    ratio = overhead_total / roundtrip_total if roundtrip_total > 0.0 else 0.0
    total_fraction = overhead_total / total_alignment if total_alignment > 0.0 else 0.0
    expected = {
        "physical_roundtrip_seconds": roundtrip_total,
        "protocol_overhead_seconds": overhead_total,
        "protocol_overhead_ratio_of_roundtrip": ratio,
        "protocol_overhead_fraction_of_total_alignment": total_fraction,
    }
    if validated["protocol_overhead_ratio_of_roundtrip"] > 1.0:
        raise CcCoreEnvironmentError("CC 프로토콜 왕복 대비 오버헤드 비율이 0~1 범위를 벗어났습니다.")
    if validated["protocol_overhead_fraction_of_total_alignment"] > 1.0:
        raise CcCoreEnvironmentError("CC 전체 정렬 대비 프로토콜 오버헤드 비율이 0~1 범위를 벗어났습니다.")
    if any(
        not math.isclose(validated[name], expected_value, rel_tol=1.0e-12, abs_tol=1.0e-12)
        for name, expected_value in expected.items()
    ):
        raise CcCoreEnvironmentError("CC 세션 프로토콜 합계 또는 비율이 실제 네이티브 명령과 다릅니다.")
    return validated


def validate_exact_evaluation_cache_statistics(
    value: Any, logical_operations: tuple[dict[str, Any], ...],
) -> dict[str, Any]:
    """정확 후보 캐시 합계를 논리 연산의 실제 적중·미스와 교차 검증합니다."""
    expected_keys = {"policy", "entry_count", "hit_count", "miss_count"}
    if not isinstance(value, Mapping) or set(value) != expected_keys:
        raise CcCoreEnvironmentError("CC 정확 평가 캐시 통계가 고정 계약과 다릅니다.")
    if value.get("policy") != "BIT_EXACT_MATRIX_AND_EVALUATION_CONTRACT":
        raise CcCoreEnvironmentError("CC 정확 평가 캐시 정책이 고정 계약과 다릅니다.")
    entries = nonnegative_integer(value.get("entry_count"), "CC 평가 캐시 항목 수")
    hits = nonnegative_integer(value.get("hit_count"), "CC 평가 캐시 적중 수")
    misses = nonnegative_integer(value.get("miss_count"), "CC 평가 캐시 미스 수")
    expected_hits = sum(
        item["execution_source"] == "exact_evaluation_cache" for item in logical_operations
    )
    expected_misses = sum(
        item["execution_source"] == "native_session" and bool(item["evaluation_key_sha256"])
        for item in logical_operations
    )
    if hits != expected_hits or misses != expected_misses or entries != misses:
        raise CcCoreEnvironmentError("CC 정확 평가 캐시 통계가 논리 연산의 적중·미스와 다릅니다.")
    return {
        "policy": value["policy"], "entry_count": entries,
        "hit_count": hits, "miss_count": misses,
    }


def matrix_sim3(value: Any, field_name: str, *, tolerance: float = 5.0e-6) -> tuple[np.ndarray, float]:
    """반사·전단·비균일 배율을 거부하고 양의 유사변환을 검증합니다."""
    try:
        matrix = np.asarray(value, dtype=np.float64)
    except (TypeError, ValueError, OverflowError) as error:
        raise CcCoreNumericalError(f"{field_name} 행렬을 읽지 못했습니다.") from error
    if matrix.shape != (4, 4) or not np.isfinite(matrix).all():
        raise CcCoreNumericalError(f"{field_name}은 유한한 4×4 행렬이어야 합니다.")
    if not np.allclose(matrix[3], (0.0, 0.0, 0.0, 1.0), atol=1.0e-10, rtol=0.0):
        raise CcCoreNumericalError(f"{field_name}의 동차 좌표 행이 올바르지 않습니다.")
    singular_values = np.linalg.svd(matrix[:3, :3], compute_uv=False)
    scale = float(np.mean(singular_values))
    if scale <= 0.0 or not np.allclose(singular_values, scale, atol=tolerance * scale, rtol=0.0):
        raise CcCoreNumericalError(f"{field_name}에 비균일 배율이나 특이행렬이 있습니다.")
    rotation = matrix[:3, :3] / scale
    if not np.isclose(np.linalg.det(rotation), 1.0, atol=tolerance, rtol=0.0):
        raise CcCoreNumericalError(f"{field_name}에 반사가 포함되어 있습니다.")
    return np.ascontiguousarray(matrix), scale


def identity_payload(manifest: Mapping[str, Any]) -> dict[str, Any]:
    """진행률·오류·결과에 같은 작업 식별자를 포함합니다."""
    return {
        "job_id": manifest.get("job_id", ""),
        "input_fingerprint": manifest.get("input_fingerprint", ""),
        "moving_model_id": manifest.get("moving_model_id", ""),
        "backend_id": CC_BACKEND_ID,
        "algorithm_version": CC_ALGORITHM_VERSION,
        "backend_algorithm_version": CC_ALGORITHM_VERSION,
        "job_protocol_version": CC_JOB_PROTOCOL_VERSION,
    }


def resolve_parameters(value: Mapping[str, Any]) -> dict[str, Any]:
    """후보 우선 표본·자동 겹침 추정 정책과 명시적 성능 옵션을 검증합니다."""
    if not isinstance(value, Mapping):
        raise CcCoreInputError("CC 작업 매개변수는 객체여야 합니다.")
    fixed_policy = {
        "overlap_mode": "ESTIMATED_ON_CANDIDATES",
        "filter_out_farthest_points": True,
        "candidate_probe_batch_size": 8192,
        "candidate_max_depth": 8,
        "candidate_max_patch_points": 250000,
        "candidate_proposal_limit": 20,
        "validation_distance_diagonal_fraction": 0.01,
        "validation_distance_spacing_factor": 3.0,
        "validation_distance_marker_factor": 2.0,
        "candidate_distance_factor": 2.0,
        "validation_distance_max_diagonal_fraction": 0.05,
    }
    for name, expected in fixed_policy.items():
        actual = value.get(f"cc_{name}", expected)
        if type(actual) is not type(expected) or actual != expected:
            raise CcCoreInputError(f"CC {name} 값이 후보 선별 고정 정책과 다릅니다.")
    values = {
        **fixed_policy,
        "max_iterations": positive_integer(value.get("cc_max_iterations", CC_DEFAULT_MAX_ITERATIONS), "CC 최대 반복 수"),
        "scale_refine_max_iterations": positive_integer(
            value.get("cc_scale_refine_max_iterations", value.get("cc_max_iterations", CC_DEFAULT_SCALE_REFINE_MAX_ITERATIONS)),
            "CC 배율 보정 최대 반복 수",
        ),
        "mesh_sample_count": positive_integer(value.get("cc_mesh_sample_count", 50000), "CC 메시 표본 수"),
        "point_sample_count": positive_integer(value.get("cc_point_sample_count", 50000), "CC 점군 표본 수"),
        "evaluation_sample_count": positive_integer(value.get("cc_evaluation_sample_count", 30000), "CC 평가 표본 수"),
        "max_threads": positive_integer(value.get("cc_max_threads", 1), "CC 스레드 수"),
        "min_scale": CC_MIN_SCALE,
        "max_scale": CC_MAX_SCALE,
        "optimizer_seed": CC_OPTIMIZER_SEED,
        "evaluation_seed": CC_EVALUATION_SEED,
        "point_input_policy": "OVERLAP_CANDIDATES_SPATIAL_SAMPLE",
        "mesh_sampling_policy": "OVERLAP_PATCHES_AREA_WEIGHTED_FIXED_SEED",
        "automatic_overlap_search": True,
    }
    if any(values[name] < 3 for name in ("mesh_sample_count", "point_sample_count", "evaluation_sample_count")):
        raise CcCoreInputError("정렬·평가 표본은 각각 최소 3개이어야 합니다.")
    if values["max_threads"] > CC_MAX_THREADS:
        raise CcCoreInputError(f"CC 스레드 수는 {CC_MAX_THREADS} 이하여야 합니다.")
    return values


def validate_manifest(manifest: Mapping[str, Any], job_directory: Path | str) -> dict[str, Any]:
    """독립 CC 작업의 식별자·형상·초기 변환 및 파일 경계를 검증합니다."""
    if not isinstance(manifest, Mapping):
        raise CcCoreInputError("CC 작업 요청은 객체여야 합니다.")
    root = Path(job_directory).resolve(strict=True)
    expected = {
        "backend_id": CC_BACKEND_ID,
        "algorithm_version": CC_ALGORITHM_VERSION,
        "backend_algorithm_version": CC_ALGORITHM_VERSION,
        "job_protocol_version": CC_JOB_PROTOCOL_VERSION,
    }
    for key, wanted in expected.items():
        if manifest.get(key) != wanted:
            raise CcCoreInputError(f"CC 작업의 {key} 값이 지원 계약과 다릅니다.")
    for key in ("job_id", "input_fingerprint", "moving_model_id", "parameter_fingerprint"):
        if not isinstance(manifest.get(key), str) or not manifest[key].strip():
            raise CcCoreInputError(f"CC 작업에 {key} 식별자가 없습니다.")
    if manifest["job_id"] != root.name:
        raise CcCoreInputError("CC 작업 ID와 작업 디렉터리 이름이 다릅니다.")
    reference_ids = manifest.get("reference_model_ids")
    if not isinstance(reference_ids, list) or not reference_ids or any(
        not isinstance(item, str) or not item.strip() for item in reference_ids
    ):
        raise CcCoreInputError("CC 기준 모델 ID 목록이 올바르지 않습니다.")
    if len(set(reference_ids)) != len(reference_ids) or manifest["moving_model_id"] in reference_ids:
        raise CcCoreInputError("CC 이동·기준 모델 ID가 중복됩니다.")
    for key in ("model_revision_snapshot", "model_state_snapshot"):
        if not isinstance(manifest.get(key), list):
            raise CcCoreInputError(f"CC 작업에 {key} 목록이 없습니다.")
    if not isinstance(manifest.get("marker_signature"), str):
        raise CcCoreInputError("CC 마커 서명은 문자열이어야 합니다.")
    matrix_sim3(manifest.get("initial_alignment_4x4"), "CC 시작 정렬", tolerance=1.0e-8)
    geometry = manifest.get("geometry")
    if not isinstance(geometry, dict):
        raise CcCoreInputError("CC 작업 형상 객체가 없습니다.")
    kinds = []
    for role in ("moving", "reference"):
        entry = geometry.get(role)
        if not isinstance(entry, dict) or entry.get("kind") not in GEOMETRY_KINDS:
            raise CcCoreInputError(f"CC {role} 형상 종류가 올바르지 않습니다.")
        kinds.append(entry["kind"])
        job_path(root, entry.get("positions_file"))
        if positive_integer(entry.get("point_count"), "CC 좌표 수") < 3:
            raise CcCoreInputError("CC 입력 형상에는 최소 3개 좌표가 필요합니다.")
        if entry["kind"] == "TRIANGLE_MESH":
            job_path(root, entry.get("triangles_file"))
            positive_integer(entry.get("triangle_count"), "CC 삼각면 수")
        elif entry.get("triangles_file") is not None or entry.get("triangle_count", 0) != 0:
            raise CcCoreInputError("CC 점군 입력에 삼각면을 붙일 수 없습니다.")
    mode = GEOMETRY_MODES[tuple(kinds)]
    if manifest.get("geometry_mode", mode) != mode:
        raise CcCoreInputError("CC 형상 모드와 실제 입력 종류가 다릅니다.")
    if manifest.get("reverse_registration", mode == "MESH_TO_POINT") is not (mode == "MESH_TO_POINT"):
        raise CcCoreInputError("CC 역방향 정렬 플래그와 형상 모드가 다릅니다.")
    resolve_parameters(manifest.get("parameters"))
    return dict(manifest)


@dataclass(frozen=True, slots=True)
class CcCoreGeometry:
    """독립 작업에서 읽은 Project World 좌표와 삼각면을 보관합니다."""
    kind: str
    positions: np.ndarray
    triangles: np.ndarray | None
    provenance: Mapping[str, Any]


def load_geometry(directory: Path, entry: Mapping[str, Any]) -> CcCoreGeometry:
    """파일·좌표·인덱스를 검증하며 메시의 비퇴화 검사는 계산용 면 정제 단계에 맡깁니다."""
    count = positive_integer(entry["point_count"], "CC 좌표 수")
    path = job_path(directory, entry["positions_file"])
    if path.stat().st_size != count * 3 * 8:
        raise CcCoreInputError("CC 좌표 바이너리 크기가 점 수와 다릅니다.")
    positions = np.fromfile(path, dtype="<f8").reshape(count, 3)
    if not np.isfinite(positions).all():
        raise CcCoreInputError("CC 좌표에 유한하지 않은 값이 있습니다.")
    if entry["kind"] == "COLMAP_POINT_CLOUD" and np.linalg.matrix_rank(positions - positions[0]) < 2:
        raise CcCoreInputError("CC 좌표가 한 점이나 직선으로 퇴화했습니다.")
    triangles = None
    if entry["kind"] == "TRIANGLE_MESH":
        triangle_count = positive_integer(entry["triangle_count"], "CC 삼각면 수")
        path = job_path(directory, entry["triangles_file"])
        if path.stat().st_size != triangle_count * 3 * 4:
            raise CcCoreInputError("CC 삼각면 바이너리 크기가 면 수와 다릅니다.")
        triangles = np.fromfile(path, dtype="<u4").reshape(triangle_count, 3)
        if np.max(triangles) >= count:
            raise CcCoreInputError("CC 삼각면이 존재하지 않는 꼭짓점을 참조합니다.")
        triangles.setflags(write=False)
    positions.setflags(write=False)
    return CcCoreGeometry(entry["kind"], positions, triangles, entry.get("provenance", {}))
