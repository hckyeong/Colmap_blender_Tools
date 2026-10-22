"""점 기반 강체·Sim(3) 정렬의 독립 작업·네이티브 파일 계약을 정의합니다."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
import re
from pathlib import Path, PureWindowsPath
import tempfile
from time import monotonic, sleep
from typing import Any, Mapping

import numpy as np

BACKEND_ID = "POINT_KD_ICP"
ALGORITHM_VERSION = "point-kd-icp-v3"
JOB_PROTOCOL_VERSION = "point-kd-job-v1"
NATIVE_PROTOCOL_VERSION = "point-kd-native-v1"
PROGRESS_STAGES = frozenset({"PREPARING", "RIGID", "SIM3", "FINALIZING", "COMPLETED", "FAILED", "CANCELLED"})


class PointKdError(ValueError):
    """점 기반 정렬의 입력·출력 계약 오류를 나타냅니다."""


class PointKdCancelled(PointKdError):
    """결과 공개 전에 취소된 점 기반 작업을 나타냅니다."""


def plain(value: Any) -> Any:
    """불변 감사 객체를 JSON에서 지원하는 기본 값으로 변환합니다."""
    if hasattr(value, "to_mapping"):
        return plain(value.to_mapping())
    if isinstance(value, Mapping):
        return {str(key): plain(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [plain(item) for item in value]
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, np.generic):
        return value.item()
    return value


def fingerprint(value: Any) -> str:
    """매개변수와 작은 감사 기록의 결정적인 UTF-8 지문을 계산합니다."""
    encoded = json.dumps(plain(value), ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.blake2b(encoded, digest_size=20).hexdigest()


def number(value: Any, name: str, *, minimum: float = 0.0) -> float:
    """유한한 실수와 하한을 검사하여 정규화합니다."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PointKdError(f"{name} 값은 유한한 수여야 합니다.")
    result = float(value)
    if not np.isfinite(result) or result < minimum:
        raise PointKdError(f"{name} 값이 허용 범위를 벗어났습니다.")
    return result


def integer(value: Any, name: str, *, minimum: int = 0) -> int:
    """정수의 형식과 하한을 검사합니다."""
    if type(value) is not int or value < minimum:
        raise PointKdError(f"{name} 값은 {minimum} 이상의 정수여야 합니다.")
    return value


def matrix(value: Any, *, rigid: bool = False, similarity: bool = False) -> np.ndarray:
    """유한 아핀 행렬과 요청된 강체 또는 양수 균일 배율 변환을 검증합니다."""
    result = np.asarray(value, dtype=np.float64)
    if (result.shape != (4, 4) or not np.isfinite(result).all()
            or not np.allclose(result[3], (0, 0, 0, 1), rtol=0, atol=1e-12)):
        raise PointKdError("정렬 변환은 유한한 4×4 아핀 행렬이어야 합니다.")
    if rigid and (not np.allclose(result[:3, :3].T @ result[:3, :3], np.eye(3), rtol=0, atol=1e-8)
                  or not np.isclose(np.linalg.det(result[:3, :3]), 1.0, rtol=0, atol=1e-8)):
        raise PointKdError("점 기반 정렬의 추가 변환에는 배율·반사·전단이 허용되지 않습니다.")
    if similarity:
        scale = uniform_scale(result)
        rotation = result[:3, :3] / scale
        if (not np.allclose(rotation.T @ rotation, np.eye(3), rtol=0, atol=1e-8)
                or not np.isclose(np.linalg.det(rotation), 1.0, rtol=0, atol=1e-8)):
            raise PointKdError("Sim(3) 정렬에는 양수 균일 배율만 허용되며 반사·전단은 금지됩니다.")
    return result


def uniform_scale(value: Any) -> float:
    """유한한 변환의 양수 균일 배율 후보를 행렬식에서 계산합니다."""
    determinant = float(np.linalg.det(np.asarray(value, dtype=np.float64)[:3, :3]))
    if not np.isfinite(determinant) or determinant <= 0:
        raise PointKdError("정렬 배율은 유한한 양수여야 합니다.")
    return float(np.cbrt(determinant))


def job_path(root: Path, relative: Any) -> Path:
    """작업 디렉터리 안의 상대 파일만 허용하고 경로 이탈을 차단합니다."""
    if not isinstance(relative, str) or not relative.strip():
        raise PointKdError("작업 파일은 비어 있지 않은 상대 경로여야 합니다.")
    windows = PureWindowsPath(relative)
    if Path(relative).is_absolute() or windows.drive or windows.root or ":" in relative or ".." in windows.parts:
        raise PointKdError("작업 파일에 절대 경로·상위 경로·대체 스트림을 사용할 수 없습니다.")
    resolved_root = root.resolve(strict=True)
    target = (resolved_root / relative).resolve()
    if not target.is_relative_to(resolved_root) or target == resolved_root:
        raise PointKdError("작업 파일 경로가 허용 디렉터리를 벗어났습니다.")
    return target


def _open_json_shared(path: Path) -> Any:
    """Windows 원자 파일 교체를 방해하지 않는 삭제 공유 읽기 핸들을 엽니다."""
    if os.name != "nt":
        return path.open("r", encoding="utf-8")
    import ctypes
    from ctypes import wintypes
    import msvcrt
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                   wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    handle = kernel.CreateFileW(str(path), 0x80000000, 7, None, 3, 0x80, None)
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


def replace_atomic(temporary: Path, target: Path) -> None:
    """Windows에서 관찰자의 일시적인 파일 공유 충돌만 제한 재시도합니다."""
    deadline = monotonic() + 1.0
    while True:
        try:
            os.replace(temporary, target)
            return
        except PermissionError:
            if monotonic() >= deadline:
                raise
            sleep(.01)


def read_json(path: Path) -> dict[str, Any]:
    """UTF-8 JSON 객체만 읽으며 비정상 수치와 중복 키를 거부합니다."""
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        """중복 필드로 식별자를 덮어쓰는 JSON을 거부합니다."""
        result = {}
        for key, value in items:
            if key in result:
                raise PointKdError(f"JSON 필드가 중복되었습니다: {key}")
            result[key] = value
        return result
    def invalid_constant(value: str) -> None:
        """표준 JSON이 허용하지 않는 비유한 상수를 거부합니다."""
        raise PointKdError(f"유효하지 않은 JSON 수치입니다: {value}")
    deadline = monotonic() + .24
    while True:
        try:
            with _open_json_shared(path) as stream:
                value = json.load(stream, object_pairs_hook=pairs, parse_constant=invalid_constant)
            break
        except OSError as error:
            if os.name == "nt" and getattr(error, "winerror", None) in (5, 32, 33) and monotonic() < deadline:
                sleep(.005)
                continue
            raise PointKdError(f"작업 JSON을 읽지 못했습니다: {path.name}") from error
        except (UnicodeError, ValueError) as error:
            raise PointKdError(f"작업 JSON을 읽지 못했습니다: {path.name}") from error
    if not isinstance(value, dict):
        raise PointKdError("작업 JSON은 객체여야 합니다.")
    return value


def write_json(path: Path, payload: Mapping[str, Any]) -> None:
    """완성된 UTF-8 JSON을 원자적으로 공개합니다."""
    content = json.dumps(plain(payload), ensure_ascii=True, sort_keys=True, allow_nan=False, separators=(",", ":"))
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        replace_atomic(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def identity(manifest: Mapping[str, Any]) -> dict[str, Any]:
    """출력 종류에 관계없이 같은 작업·엔진 식별자를 반환합니다."""
    return {"job_id": manifest.get("job_id", ""), "input_fingerprint": manifest.get("input_fingerprint", ""),
            "moving_model_id": manifest.get("moving_model_id", ""), "backend_id": BACKEND_ID,
            "algorithm_version": ALGORITHM_VERSION, "backend_algorithm_version": ALGORITHM_VERSION,
            "job_protocol_version": JOB_PROTOCOL_VERSION}


@dataclass(frozen=True, slots=True)
class PointKdRuntimeParameters:
    """전체점 정렬 모드와 상대 변화·선택적 배율 종료 조건을 정의합니다."""

    max_threads: int = 1
    max_iterations: int = 30
    trim_fraction: float = 0.05
    normal_angle_degrees: float = 45.0
    relative_rms_tolerance: float = 0.0010000000474974513
    translation_tolerance: float = 0.0010000000474974513
    rotation_tolerance: float = 0.0010000000474974513
    maximum_correspondence_distance: float | None = None
    scale_mode: str = "FIXED"
    scale_tolerance: float = 1e-6

    def __post_init__(self) -> None:
        """실행 설정과 명시적으로 선택한 배율 보정 모드의 범위를 검사합니다."""
        if not 1 <= integer(self.max_threads, "CPU 스레드", minimum=1) <= 1024:
            raise PointKdError("CPU 스레드는 1024 이하여야 합니다.")
        if not 1 <= integer(self.max_iterations, "최대 반복", minimum=1) <= 30:
            raise PointKdError("최대 반복은 30 이하여야 합니다.")
        if not 0 <= number(self.trim_fraction, "꼬리 제거 비율") < 1:
            raise PointKdError("꼬리 제거 비율은 1 미만이어야 합니다.")
        if not 0 <= number(self.normal_angle_degrees, "법선 각도") <= 90:
            raise PointKdError("법선 각도는 90도 이하여야 합니다.")
        for name in ("relative_rms_tolerance", "translation_tolerance", "rotation_tolerance"):
            number(getattr(self, name), name)
        if self.maximum_correspondence_distance is not None and number(self.maximum_correspondence_distance, "최대 대응 거리") <= 0:
            raise PointKdError("최대 대응 거리는 양수여야 합니다.")
        if self.scale_mode not in {"FIXED", "SIM3"}:
            raise PointKdError("점 기반 정렬 배율 모드는 FIXED 또는 SIM3여야 합니다.")
        if not 0 < number(self.scale_tolerance, "배율 수렴 허용치") <= 1:
            raise PointKdError("배율 수렴 허용치는 0 초과 1 이하여야 합니다.")

    def to_mapping(self) -> dict[str, Any]:
        """배율 모드를 포함하여 네이티브가 사용할 명시적 매개변수를 반환합니다."""
        return {name: getattr(self, name) for name in self.__dataclass_fields__ if getattr(self, name) is not None}

    @property
    def fingerprint(self) -> str:
        """알고리즘·배율 모드·계산 매개변수의 지문을 반환합니다."""
        return fingerprint({"algorithm": ALGORITHM_VERSION, "parameters": self.to_mapping()})


def validate_input_buffer(entry: Mapping[str, Any], field: str, byte_count: int, root: Path) -> str:
    """파일 또는 작업 전용 공유 버퍼 하나만 허용하고 이름·크기 경계를 검사합니다."""
    file_key, mapping_key = field + '_file', field + '_mapping'
    if (file_key in entry) == (mapping_key in entry):
        raise PointKdError('입력 배열은 파일 또는 공유 메모리 하나만 지정해야 합니다.')
    if mapping_key in entry:
        descriptor = entry[mapping_key]
        if (os.name != 'nt' or not isinstance(descriptor, Mapping)
                or set(descriptor) != {'name', 'byte_count'}
                or not isinstance(descriptor.get('name'), str)
                or re.fullmatch(r'Local\\ColmapPointKd_[0-9a-f]{32}', descriptor['name']) is None
                or type(descriptor.get('byte_count')) is not int or descriptor['byte_count'] != byte_count):
            raise PointKdError('공유 입력의 이름 또는 바이트 크기가 올바르지 않습니다.')
        return 'WIN32_SHARED_MEMORY_V1'
    path = job_path(root, entry[file_key])
    if not path.is_file() or path.stat().st_size != byte_count:
        raise PointKdError('입력 바이너리 크기가 원소 수와 다릅니다.')
    return 'FILES_V1'


def validate_manifest(value: Mapping[str, Any], root: Path) -> dict[str, Any]:
    """엔진·크기와 작업별 입력 또는 불변 로컬 원본·변환 전달 계약을 검사합니다."""
    for name, expected in identity(value).items():
        if value.get(name) != expected or (name in {"job_id", "input_fingerprint", "moving_model_id"} and not expected):
            raise PointKdError(f"작업의 {name} 식별자가 올바르지 않습니다.")
    if value.get("manifest_version") != 1:
        raise PointKdError("점 기반 작업 manifest 버전이 올바르지 않습니다.")
    initial = matrix(value.get("initial_alignment_4x4"))
    if np.linalg.det(initial[:3, :3]) <= 0:
        raise PointKdError("초기 정렬 배율은 양수여야 합니다.")
    geometry = value.get("geometry")
    if not isinstance(geometry, Mapping):
        raise PointKdError("선택 형상 정보가 없습니다.")
    transports = set()
    for role in ("moving", "reference"):
        entry = geometry.get(role)
        if not isinstance(entry, Mapping) or entry.get("kind") not in {"COLMAP_POINT_CLOUD", "TRIANGLE_MESH"}:
            raise PointKdError("선택 형상 종류가 올바르지 않습니다.")
        count = integer(entry.get("point_count"), "전체 유효점 수", minimum=3)
        if 'parts' in entry:
            validate_local_parts(entry, root)
            transports.add('WIN32_SHARED_LOCAL_V1')
            continue
        transports.add(validate_input_buffer(entry, 'positions', count * 24, root))
        if entry["kind"] == "TRIANGLE_MESH":
            count = integer(entry.get("triangle_count"), "유효 삼각면 수", minimum=1)
            transports.add(validate_input_buffer(entry, 'triangles', count * 12, root))
    if transports != {value.get('input_transport', 'FILES_V1')}:
        raise PointKdError('작업의 입력 전달 방식과 배열 저장 방식이 다릅니다.')
    if not isinstance(value.get("parameters"), Mapping):
        raise PointKdError("강체 정렬 매개변수가 없습니다.")
    parameters = PointKdRuntimeParameters(**value["parameters"])
    if value.get("parameter_fingerprint") != parameters.fingerprint:
        raise PointKdError("매개변수 지문이 입력 설정과 다릅니다.")
    number(value.get("marker_initial_rms"), "마커 초기 RMS")
    if not isinstance(value.get("reference_model_ids"), list) or not value["reference_model_ids"]:
        raise PointKdError("기준 모델 목록이 없습니다.")
    return dict(value)


def validate_local_parts(entry: Mapping[str, Any], root: Path) -> None:
    """로컬 원본 묶음의 배열 설명자·합계·가역 변환을 작은 메타데이터로 검증합니다."""
    parts = entry['parts']
    if not isinstance(parts, list) or not parts or any(k.endswith(('_file', '_mapping')) for k in entry):
        raise PointKdError('로컬 형상 목록이 없거나 작업별 배열과 혼합되었습니다.')
    points = faces = 0
    mesh = entry['kind'] == 'TRIANGLE_MESH'
    for part in parts:
        if not isinstance(part, Mapping):
            raise PointKdError('로컬 형상 설명자가 올바르지 않습니다.')
        count = integer(part.get('point_count'), '로컬 점 수', minimum=3)
        points += count
        if validate_input_buffer(part, 'positions', count * 24, root) != 'WIN32_SHARED_MEMORY_V1':
            raise PointKdError('로컬 원본에는 공유 배열만 허용됩니다.')
        transform = matrix(part.get('transform_4x4'))
        if not np.array_equal(transform[3], (0., 0., 0., 1.)) or np.linalg.slogdet(transform[:3, :3])[0] <= 0:
            raise PointKdError('로컬 형상 변환은 양수 배율의 가역 아핀 행렬이어야 합니다.')
        if mesh:
            count = integer(part.get('triangle_count'), '로컬 삼각형 수', minimum=1)
            faces += count
            if validate_input_buffer(part, 'triangles', count * 12, root) != 'WIN32_SHARED_MEMORY_V1':
                raise PointKdError('로컬 삼각형에는 공유 배열만 허용됩니다.')
        elif any(k.startswith('triangles_') or k == 'triangle_count' for k in part):
            raise PointKdError('점군에는 삼각형 배열을 지정할 수 없습니다.')
    if points != entry['point_count'] or (mesh and (faces != entry.get('triangle_count') or points > np.iinfo(np.uint32).max)):
        raise PointKdError('로컬 형상 목록의 원소 수 합계가 입력과 다릅니다.')


def registration_direction(manifest: Mapping[str, Any]) -> tuple[str, str, str]:
    """혼합 입력은 점군을 질의하고 같은 종류는 작은 쪽을 질의하며 동률에는 P를 씁니다."""
    moving = manifest["geometry"]["moving"]
    reference = manifest["geometry"]["reference"]
    if moving["kind"] != reference["kind"]:
        source = "moving" if moving["kind"] == "COLMAP_POINT_CLOUD" else "reference"
    else:
        source = "moving" if moving["point_count"] <= reference["point_count"] else "reference"
    target = "reference" if source == "moving" else "moving"
    method = "POINT_TO_POINT" if moving["kind"] == reference["kind"] == "COLMAP_POINT_CLOUD" else "POINT_TO_PLANE"
    return source, target, method


def moving_delta(native_delta: Any, reverse: bool, scale_mode: str = "FIXED") -> np.ndarray:
    """선택한 변환 모드를 검증하고 역질의 배율까지 원래 이동 P 방향으로 바꿉니다."""
    if scale_mode not in {"FIXED", "SIM3"}:
        raise PointKdError("알 수 없는 배율 모드입니다.")
    checks = {"rigid": scale_mode == "FIXED", "similarity": scale_mode == "SIM3"}
    delta = matrix(native_delta, **checks)
    return matrix(np.linalg.inv(delta) if reverse else delta, **checks)


def validate_native_result(payload: Mapping[str, Any], request: Mapping[str, Any]) -> dict[str, Any]:
    """네이티브 결과의 출처·요청 배율 모드·변환·전체점 수·진단값을 검증합니다."""
    if not isinstance(payload, Mapping):
        raise PointKdError("네이티브 결과는 JSON 객체여야 합니다.")
    if payload.get("protocol_version") != NATIVE_PROTOCOL_VERSION or payload.get("engine") != "POINT_KD":
        raise PointKdError("점 기반 네이티브 결과 식별자가 올바르지 않습니다.")
    status = payload.get("status")
    if status not in {"SUCCESS", "REVIEW", "FAIL", "CANCELLED"}:
        raise PointKdError("네이티브 결과 상태가 올바르지 않습니다.")
    if status in {"FAIL", "CANCELLED"}:
        error_type = PointKdCancelled if status == "CANCELLED" else PointKdError
        raise error_type(str(payload.get("message") or payload.get("termination_reason", "네이티브 정렬에 실패했습니다.")))
    scale_mode = request["parameters"].get("scale_mode", "FIXED")
    if payload.get("scale_mode", "FIXED") != scale_mode:
        raise PointKdError("네이티브 결과의 배율 모드가 요청과 다릅니다.")
    delta = moving_delta(payload.get("delta_4x4"), False, scale_mode)
    if scale_mode == "SIM3" and not np.isclose(number(payload.get("additional_scale"), "네이티브 추가 배율"),
                                              uniform_scale(delta), rtol=1e-10, atol=0):
        raise PointKdError("네이티브 추가 배율이 결과 행렬과 다릅니다.")
    for key, role in (("source_count", "source"), ("target_count", "target")):
        if integer(payload.get(key), key, minimum=3) != request[role]["point_count"]:
            raise PointKdError("네이티브가 사용한 전체점 수가 입력과 다릅니다.")
    for key in ("iterations", "correspondence_count", "rejected_distance_count", "rejected_normal_count", "trimmed_count"):
        integer(payload.get(key), key)
    if (payload["iterations"] > request["parameters"]["max_iterations"]
            or not 3 <= payload["correspondence_count"] <= request["source"]["point_count"]):
        raise PointKdError("네이티브 반복 또는 유효 대응점 수가 올바르지 않습니다.")
    for key in ("initial_rms", "final_rms"):
        number(payload.get(key), key)
    if not isinstance(payload.get("termination_reason"), str) or not payload["termination_reason"]:
        raise PointKdError("네이티브 종료 사유가 없습니다.")
    if not isinstance(payload.get("normal_policy"), str) or not payload["normal_policy"]:
        raise PointKdError("네이티브 법선 정책이 없습니다.")
    timings = payload.get("timing_seconds")
    if not isinstance(timings, Mapping) or not timings:
        raise PointKdError("네이티브 구간별 시간이 없습니다.")
    for name, value in timings.items():
        number(value, f"구간 시간 {name}")
    for role in ("source", "target"):
        normals = payload.get("valid_normal_count")
        if not isinstance(normals, Mapping) or integer(normals.get(role), "유효 법선 수") > request[role]["point_count"]:
            raise PointKdError("유효 법선 수가 전체점 수를 벗어났습니다.")
    return dict(payload)
