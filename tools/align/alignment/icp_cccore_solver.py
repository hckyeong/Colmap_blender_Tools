"""CCCoreLib 프로세스를 조정하고 독립 표면 거리로 정렬 후보를 평가합니다."""

from __future__ import annotations

from dataclasses import dataclass
import ctypes
import math
import hashlib
import json
import os
from pathlib import Path
import secrets
import subprocess
from time import monotonic, sleep
from typing import Any, Callable, Mapping

import numpy as np

if __package__:
    from . import icp_cccore_contract as contract
else:
    import icp_cccore_contract as contract

ProgressSink = Callable[[Mapping[str, Any]], None] | None
CancelProbe = Callable[[], bool] | None
QUALITY_POLICY = {
    "minimum_correspondence_count": 3,
    "minimum_relative_mean_rms_improvement": 0.001,
    "absolute_distance_tolerance_normalized": 1.0e-6,
    "maximum_p95_relative_increase": 0.05,
    "maximum_spatial_bin_relative_increase": 0.25,
    "minimum_spatial_bin_count": 10,
    "trimmed_fraction": 0.9,
    "selection_metric": "MEAN_DIRECTIONAL_RMS",
    "distance_units": "PROJECT_WORLD",
}


@dataclass(frozen=True, slots=True)
class CcCoreSolverResult:
    """CC 계산 결과와 별도로 저장할 최종 행렬을 보관합니다."""
    metadata: Mapping[str, Any]
    delta_4x4: np.ndarray
    final_alignment_4x4: np.ndarray

    def result_arrays(self) -> dict[str, np.ndarray]:
        """결과 NPZ에 기록할 두 개의 검증된 행렬을 반환합니다."""
        return {"delta_4x4": self.delta_4x4, "final_alignment_4x4": self.final_alignment_4x4}


@dataclass(frozen=True, slots=True)
class Normalization:
    """두 입력이 공유하는 원점 이동과 양의 길이 단위를 보관합니다."""
    origin: np.ndarray
    unit: float

    def apply(self, positions: np.ndarray) -> np.ndarray:
        """Project World 좌표를 공통 길이 단위로 정규화합니다."""
        return np.ascontiguousarray((positions - self.origin) / self.unit)

    def world_delta(self, normalized_delta: np.ndarray) -> np.ndarray:
        """공통 H의 켤레변환으로 정규화 결과를 Project World로 복원합니다."""
        result = np.array(normalized_delta, dtype=np.float64, copy=True)
        result[:3, 3] = (
            self.unit * normalized_delta[:3, 3]
            + self.origin
            - normalized_delta[:3, :3] @ self.origin
        )
        return contract.matrix_sim3(result, "CC Project World 결과")[0]

    def to_mapping(self) -> dict[str, Any]:
        """수치 정밀도 감사에 필요한 원점·단위와 좌표 계약을 반환합니다."""
        return {
            "origin_project_world": self.origin.tolist(),
            "unit_project_world": self.unit,
            "formula": "x_native = (x_project_world - origin) / unit",
            "delta_formula": "D_world = inverse(H) @ D_native @ H",
        }


def make_normalization(moving: np.ndarray, reference: np.ndarray) -> Normalization:
    """큰 전역 좌표에서도 두 형상이 같은 원점과 길이 단위를 사용하게 합니다."""
    minimum = np.minimum(np.min(moving, axis=0), np.min(reference, axis=0))
    maximum = np.maximum(np.max(moving, axis=0), np.max(reference, axis=0))
    origin = minimum + (maximum - minimum) * 0.5
    unit = max(float(np.linalg.norm(np.ptp(moving, axis=0))), float(np.linalg.norm(np.ptp(reference, axis=0))))
    if not math.isfinite(unit) or unit <= 0.0 or not np.isfinite(origin).all():
        raise contract.CcCoreInputError("CC 공통 정규화 원점이나 길이 단위가 올바르지 않습니다.")
    return Normalization(origin, unit)


def transform_positions(positions: np.ndarray, matrix: np.ndarray) -> np.ndarray:
    """열벡터 변환 계약에 따라 N×3 좌표 배열에 유사변환을 적용합니다."""
    return np.ascontiguousarray(positions @ matrix[:3, :3].T + matrix[:3, 3])


def inverse_surface_query(positions: np.ndarray, matrix: np.ndarray) -> tuple[np.ndarray, float]:
    """양의 유사변환을 역으로 적용한 표본과 원래 거리 복원용 균일 배율을 반환합니다."""
    sim3, scale = contract.matrix_sim3(matrix, "CC 역방향 표면 평가 변환")
    inverse = np.linalg.inv(sim3)
    contract.matrix_sim3(inverse, "CC 역방향 표면 평가 역변환")
    return transform_positions(positions, inverse), scale


def sample_triangle_surface(
    positions: np.ndarray, triangles: np.ndarray, count: int, seed: int
) -> np.ndarray:
    """삼각면 면적에 비례하는 결정적 균일 표면 표본을 생성합니다."""
    vertices = positions[triangles]
    doubled_area = np.linalg.norm(
        np.cross(vertices[:, 1] - vertices[:, 0], vertices[:, 2] - vertices[:, 0]), axis=1
    )
    total_area = float(np.sum(doubled_area))
    if not np.isfinite(doubled_area).all() or not math.isfinite(total_area) or total_area <= 0.0:
        raise contract.CcCoreInputError("CC 메시의 유효 표면 면적이 양수가 아닙니다.")
    rng = np.random.default_rng(seed)
    indices = rng.choice(triangles.shape[0], size=count, replace=True, p=doubled_area / total_area)
    selected = vertices[indices]
    radial = np.sqrt(rng.random(count))
    edge = rng.random(count)
    return np.ascontiguousarray(
        selected[:, 0] * (1.0 - radial[:, None])
        + selected[:, 1] * (radial * (1.0 - edge))[:, None]
        + selected[:, 2] * (radial * edge)[:, None]
    )


def fixed_evaluation_points(geometry: contract.CcCoreGeometry, count: int, seed: int) -> np.ndarray:
    """정렬용 표본과 다른 고정 시드로 원본 평가점만 한 번 선택합니다."""
    if geometry.triangles is not None:
        return sample_triangle_surface(geometry.positions, geometry.triangles, count, seed)
    if geometry.positions.shape[0] <= count:
        return geometry.positions
    indices = np.sort(np.random.default_rng(seed).choice(geometry.positions.shape[0], count, replace=False))
    return np.ascontiguousarray(geometry.positions[indices])


def delta_diagnostics(delta: np.ndarray, normalization: Normalization) -> dict[str, Any]:
    """전역 원점에 민감하지 않은 배율·회전·중심 이동 진단을 계산합니다."""
    matrix, scale = contract.matrix_sim3(delta, "CC 진단 변환")
    rotation = matrix[:3, :3] / scale
    sine = 0.5 * np.linalg.norm(np.array((
        rotation[2, 1] - rotation[1, 2],
        rotation[0, 2] - rotation[2, 0],
        rotation[1, 0] - rotation[0, 1],
    )))
    cosine = float(np.clip((np.trace(rotation) - 1.0) * 0.5, -1.0, 1.0))
    local_translation = matrix[:3, :3] @ normalization.origin + matrix[:3, 3] - normalization.origin
    distance = float(np.linalg.norm(local_translation))
    return {
        "additional_scale": scale,
        "rotation_degrees": float(np.degrees(np.arctan2(sine, cosine))),
        "translation_project_world": matrix[:3, 3].tolist(),
        "local_translation_project_world": local_translation.tolist(),
        "translation_distance": distance,
        "translation_distance_normalized": distance / normalization.unit,
    }


def file_sha256(path: Path) -> str:
    """실행 파일 바이트의 SHA-256 지문을 제한된 메모리로 계산합니다."""
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1048576), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_array_atomic(path: Path, values: np.ndarray) -> None:
    """고정폭 배열을 임시 파일에 동기화한 뒤 작업 경로에 원자적으로 공개합니다."""
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    try:
        with temporary.open("xb") as stream:
            values.tofile(stream)
            stream.flush()
            os.fsync(stream.fileno())
        contract._replace_atomic(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _manifest_sha256(value: Any, field_name: str) -> str:
    """Manifest의 SHA-256 문자열을 검증하고 소문자로 정규화합니다."""
    if not isinstance(value, str) or len(value) != 64 or any(
        character not in "0123456789abcdef" for character in value.lower()
    ):
        raise contract.CcCoreEnvironmentError(f"CC Runtime Manifest의 {field_name} 해시가 올바르지 않습니다.")
    return value.lower()


def native_build_identity(executable: Path) -> dict[str, Any]:
    """네이티브 실행 파일·DLL·Manifest의 고정 v3 배포 지문을 엄격히 검증합니다."""
    executable = Path(executable)
    if not executable.is_absolute() or not executable.is_file():
        raise contract.CcCoreEnvironmentError("CC 네이티브 실행 파일의 절대 경로가 올바르지 않습니다.")
    executable = executable.resolve(strict=True)
    if executable.name.casefold() != "colmap_cccore_worker.exe":
        raise contract.CcCoreEnvironmentError("CC 네이티브 실행 파일 이름이 고정 배포 계약과 다릅니다.")
    manifest_path = executable.with_name("runtime_manifest.json")
    if not manifest_path.is_file():
        raise contract.CcCoreEnvironmentError("CC Runtime Manifest가 실행 파일 옆에 없습니다.")
    try:
        manifest = contract.read_json_object(manifest_path)
    except contract.CcCoreError as error:
        raise contract.CcCoreEnvironmentError("CC Runtime Manifest를 읽지 못했습니다.") from error
    required_fields = {
        "protocol_version", "platform", "core_commit", "core_patch", "dependencies",
        "build_options", "thread_backend", "tbb_version", "requested_max_threads",
        "effective_max_threads", "hardware_thread_count", "distance_multithread_support",
        "octree_multithread_support", "coordinate_type", "matrix_scalar_type",
        "binary_sha256", "self_test",
    }
    missing_fields = sorted(required_fields.difference(manifest))
    if missing_fields:
        raise contract.CcCoreEnvironmentError(
            f"CC Runtime Manifest의 필수 필드가 없습니다: {', '.join(missing_fields)}"
        )
    if manifest.get("protocol_version") != contract.CC_NATIVE_PROTOCOL_VERSION:
        raise contract.CcCoreEnvironmentError("CC Runtime Manifest가 v3 네이티브 계약이 아닙니다.")
    if manifest.get("platform") != "windows-x64":
        raise contract.CcCoreEnvironmentError("CC Runtime Manifest의 플랫폼이 windows-x64가 아닙니다.")
    if manifest.get("coordinate_type") != "float32" or manifest.get("matrix_scalar_type") != "float64":
        raise contract.CcCoreEnvironmentError("CC Runtime Manifest의 좌표 또는 행렬 수치 형식이 고정 계약과 다릅니다.")
    options = manifest.get("build_options")
    required_options = {
        "CCCORELIB_SHARED": True,
        "CCCORELIB_USE_QT_CONCURRENT": False,
        "CCCORELIB_USE_TBB": True,
        "CCCORELIB_USE_CGAL": False,
    }
    if not isinstance(options, Mapping) or any(options.get(key) is not value for key, value in required_options.items()):
        raise contract.CcCoreEnvironmentError("CC Runtime Manifest의 CCCoreLib oneTBB 빌드 옵션이 올바르지 않습니다.")
    dependencies = manifest.get("dependencies")
    tbb_dependency = dependencies.get("tbb") if isinstance(dependencies, Mapping) else None
    if not isinstance(tbb_dependency, Mapping) or tbb_dependency.get("version") != contract.CC_TBB_VERSION:
        raise contract.CcCoreEnvironmentError("CC Runtime Manifest의 oneTBB 고정 의존성이 올바르지 않습니다.")
    manifest_identity = contract.validate_native_execution_metadata(
        {**manifest, "elapsed_seconds": 0.0},
        requested_max_threads=manifest.get("requested_max_threads"),
    )
    self_test = manifest.get("self_test")
    if (
        not isinstance(self_test, Mapping)
        or self_test.get("protocol_version") != contract.CC_NATIVE_PROTOCOL_VERSION
        or self_test.get("status") != "completed"
        or self_test.get("self_test") != "passed"
        or self_test.get("coordinate_type") != "float32"
        or self_test.get("matrix_scalar_type") != "float64"
    ):
        raise contract.CcCoreEnvironmentError("CC Runtime Manifest의 자체 검사 결과가 고정 v3 계약과 다릅니다.")
    expected_self_test_libraries = {
        "library_path": "CCCoreLib.dll",
        "tbb_library_path": "tbb12.dll",
    }
    if any(self_test.get(name) != expected for name, expected in expected_self_test_libraries.items()):
        raise contract.CcCoreEnvironmentError("CC Runtime Manifest 자체 검사의 DLL 상대 식별자가 고정 계약과 다릅니다.")
    contract.validate_native_execution_metadata(
        self_test,
        requested_max_threads=manifest_identity["requested_max_threads"],
        expected_identity=manifest_identity,
    )
    required_paths = {
        "colmap_cccore_worker.exe": executable,
        "CCCoreLib.dll": executable.with_name("CCCoreLib.dll"),
        "tbb12.dll": executable.with_name("tbb12.dll"),
    }
    binaries = manifest.get("binary_sha256")
    if not isinstance(binaries, Mapping):
        raise contract.CcCoreEnvironmentError("CC Runtime Manifest에 바이너리 해시 목록이 없습니다.")
    missing_binaries = sorted(set(required_paths).difference(binaries))
    if missing_binaries:
        raise contract.CcCoreEnvironmentError(
            f"CC Runtime Manifest의 필수 바이너리 해시가 없습니다: {', '.join(missing_binaries)}"
        )
    verified_hashes: dict[str, str] = {}
    for filename, declared_hash in binaries.items():
        if not isinstance(filename, str) or Path(filename).name != filename or "/" in filename or "\\" in filename:
            raise contract.CcCoreEnvironmentError("CC Runtime Manifest의 바이너리 파일명이 올바르지 않습니다.")
        binary_path = executable.with_name(filename)
        if not binary_path.is_file():
            raise contract.CcCoreEnvironmentError(f"CC 런타임 필수 바이너리가 실행 파일 옆에 없습니다: {filename}")
        expected_hash = _manifest_sha256(declared_hash, filename)
        actual_hash = file_sha256(binary_path)
        if actual_hash != expected_hash:
            raise contract.CcCoreEnvironmentError(f"CC 런타임 바이너리 해시가 Manifest와 다릅니다: {filename}")
        verified_hashes[filename] = actual_hash
    result: dict[str, Any] = {
        "runtime_manifest_sha256": file_sha256(manifest_path),
        "native_worker_sha256": verified_hashes["colmap_cccore_worker.exe"],
        "cccore_dll_sha256": verified_hashes["CCCoreLib.dll"],
        "tbb_dll_sha256": verified_hashes["tbb12.dll"],
        "binary_sha256": verified_hashes,
    }
    for key in (
        "protocol_version", "core_commit", "core_patch", "coordinate_type",
        "matrix_scalar_type", "build_type", "compiler", "architecture", "platform",
        "core_patch_sha256", "concurrency_patch", "concurrency_patch_sha256",
        "patched_registration_sha256", "build_options", "requested_max_threads",
        "effective_max_threads", "hardware_thread_count", "worker_source_sha256",
        "cmake_source_sha256", "source_archive_sha256", "thread_backend", "tbb_version",
        "distance_multithread_support", "octree_multithread_support",
    ):
        if key in manifest:
            result[key] = manifest[key]
    return result


def normalize_native_sim3(value: np.ndarray) -> tuple[np.ndarray, float]:
    """확인된 수치 허용 범위 안에서 가장 가까운 양의 유사변환으로 투영합니다."""
    matrix, scale = contract.matrix_sim3(value, "CC 수치 정규화 입력")
    left, _singular, right = np.linalg.svd(matrix[:3, :3])
    rotation = left @ right
    if np.linalg.det(rotation) <= 0.0:
        raise contract.CcCoreNumericalError("CC 수치 정규화에 반사 회전을 사용할 수 없습니다.")
    projected = np.array(matrix, dtype=np.float64, copy=True)
    projected[:3, :3] = scale * rotation
    correction = float(np.linalg.norm(projected[:3, :3] - matrix[:3, :3], ord="fro"))
    if correction > 5.0e-6 * max(1.0, scale):
        raise contract.CcCoreNumericalError("CC 유사변환 보정량이 코어 수치 허용 범위를 벗어났습니다.")
    contract.matrix_sim3(projected, "CC 수치 정규화 결과", tolerance=1.0e-10)
    return projected, correction


class _ChildLifetime:
    """Windows 부모 프로세스가 종료되어도 네이티브 자식이 남지 않게 합니다."""

    def __init__(self, process: subprocess.Popen) -> None:
        """자식 프로세스를 종료 연동 작업 객체에 등록합니다."""
        self.handle = None
        if os.name != "nt":
            return
        from ctypes import wintypes

        class BasicLimits(ctypes.Structure):
            """Windows 기본 작업 한계 구조의 정확한 메모리 배열입니다."""
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class IoCounters(ctypes.Structure):
            """Windows 작업 객체의 입출력 계수 배열입니다."""
            _fields_ = [(name, ctypes.c_uint64) for name in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
            )]

        class ExtendedLimits(ctypes.Structure):
            """자식 종료 연동 플래그를 설정할 확장 작업 한계입니다."""
            _fields_ = [
                ("BasicLimitInformation", BasicLimits), ("IoInfo", IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
        kernel.CreateJobObjectW.restype = wintypes.HANDLE
        kernel.SetInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
        kernel.SetInformationJobObject.restype = wintypes.BOOL
        kernel.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
        kernel.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel.CloseHandle.restype = wintypes.BOOL
        handle = kernel.CreateJobObjectW(None, None)
        if not handle:
            raise contract.CcCoreEnvironmentError("CC 자식 프로세스 수명 객체를 만들지 못했습니다.")
        limits = ExtendedLimits()
        limits.BasicLimitInformation.LimitFlags = 0x00002000
        if not kernel.SetInformationJobObject(handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            kernel.CloseHandle(handle)
            raise contract.CcCoreEnvironmentError("CC 자식 프로세스 자동 종료를 설정하지 못했습니다.")
        if not kernel.AssignProcessToJobObject(handle, wintypes.HANDLE(int(process._handle))):
            kernel.CloseHandle(handle)
            raise contract.CcCoreEnvironmentError("CC 자식 프로세스를 종료 연동 객체에 등록하지 못했습니다.")
        self.handle = handle
        self.kernel = kernel

    def close(self) -> None:
        """작업 핸들을 닫아 남아 있는 자식 프로세스를 정리합니다."""
        if self.handle is not None:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


def _stop_child(process: subprocess.Popen) -> None:
    """취소된 네이티브 자식을 종료하고 운영체제 자원 회수까지 기다립니다."""
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
    else:
        process.wait()


class NativeRunner:
    """Align 작업 하나에 네이티브 세션 하나만 열어 모든 논리 연산을 실행합니다."""

    def __init__(
        self, directory: Path, executable: Path | str, parameters: Mapping[str, Any],
        cancel_probe: CancelProbe = None, progress_sink: ProgressSink = None,
        session_identity: Mapping[str, Any] | None = None,
    ) -> None:
        """실행 파일과 작업 식별자를 검증하고 아직 시작하지 않은 세션을 구성합니다."""
        self.directory = Path(directory).resolve(strict=True)
        self.executable = Path(executable)
        if not self.executable.is_absolute() or not self.executable.is_file():
            raise contract.CcCoreEnvironmentError("CC 네이티브 실행 파일의 절대 경로가 올바르지 않습니다.")
        self.executable = self.executable.resolve(strict=True)
        self.runtime_manifest = native_build_identity(self.executable)
        self.native_worker_sha256 = self.runtime_manifest["native_worker_sha256"]
        self.parameters = parameters
        self.cancel_probe = cancel_probe
        self.progress_sink = progress_sink
        self.counter = 0
        self.core_commit = None
        self.native_identity: dict[str, Any] | None = None
        self.logical_operations: list[dict[str, Any]] = []
        self.native_calls = self.logical_operations
        self.physical_native_calls: list[dict[str, Any]] = []
        self.last_call_reference: tuple[int, int] | None = None
        self.files: dict[str, Mapping[str, Any]] = {}
        self.native_root = contract.job_path(self.directory, "native")
        self.native_root.mkdir(exist_ok=True)
        identity = dict(session_identity or {})
        self.job_id = identity.get("job_id", self.directory.name)
        self.input_fingerprint = identity.get("input_fingerprint", "standalone-native-session")
        for name, value in (("작업 ID", self.job_id), ("입력 지문", self.input_fingerprint)):
            if not isinstance(value, str) or not value or len(value) > 128 or any(
                character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for character in value
            ):
                raise contract.CcCoreInputError(f"CC 네이티브 세션의 {name}가 안전한 식별자가 아닙니다.")
        self.session_id = secrets.token_hex(16)
        self.process: subprocess.Popen | None = None
        self.lifetime: _ChildLifetime | None = None
        self.native_process_count = 0
        self.process_startup_seconds = 0.0
        self.session_elapsed_seconds = 0.0
        self.session_exit_reason = ""
        self.session_statistics: dict[str, int] | None = None
        self.geometry_statistics: list[dict[str, Any]] = []
        self.closed = False

    def __enter__(self) -> "NativeRunner":
        """문맥 진입 시 단일 네이티브 세션을 시작합니다."""
        self.start()
        return self

    def __exit__(self, error_type: Any, error: Any, traceback: Any) -> None:
        """정상 완료는 close 명령으로, 오류는 즉시 종료로 네이티브 자원을 회수합니다."""
        if error_type is None:
            self.close()
        else:
            self.abort()

    def check_cancelled(self) -> None:
        """파일과 외부 취소 신호를 동일한 작업 취소로 처리합니다."""
        if contract.job_path(self.directory, "cancel.request").is_file() or (
            self.cancel_probe is not None and self.cancel_probe()
        ):
            raise contract.CcCoreCancelled("CC 정렬 작업을 취소했습니다.")

    def _session_payload(self) -> dict[str, Any]:
        """모든 세션 파일에 공통으로 사용하는 불변 식별자를 반환합니다."""
        return {
            "protocol_version": contract.CC_NATIVE_PROTOCOL_VERSION,
            "session_id": self.session_id,
            "job_id": self.job_id,
            "input_fingerprint": self.input_fingerprint,
        }

    def _validate_runtime_unchanged(self, message: str) -> None:
        """실행 파일·DLL·Manifest가 세션 시작 지문과 같은지 다시 검사합니다."""
        if native_build_identity(self.executable) != self.runtime_manifest:
            raise contract.CcCoreEnvironmentError(message)

    def start(self) -> None:
        """세션 요청을 원자 기록하고 ready 응답까지 단일 자식 프로세스를 확인합니다."""
        if self.process is not None:
            return
        if self.closed:
            raise contract.CcCoreEnvironmentError("종료한 CC 네이티브 세션은 다시 시작할 수 없습니다.")
        self.check_cancelled()
        self._validate_runtime_unchanged("CC 세션 시작 전에 실행 파일이나 라이브러리 배포 정보가 변경되었습니다.")
        request_path = contract.job_path(self.directory, "native-session-request.json")
        ready_path = contract.job_path(self.directory, "native/session-ready.json")
        result_path = contract.job_path(self.directory, "native/session-result.json")
        for path in (request_path, ready_path, result_path):
            if path.exists():
                raise contract.CcCoreInputError("CC 네이티브 세션 파일이 작업 시작 전에 이미 존재합니다.")
        request = {
            **self._session_payload(),
            "parameters": {"max_threads": self.parameters["max_threads"]},
            "ready_file": "native/session-ready.json",
            "result_file": "native/session-result.json",
            "cancel_file": "cancel.request",
            "command_directory": "native",
        }
        contract.write_json_atomic(request_path, request)
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        started = monotonic()
        process = subprocess.Popen(
            [str(self.executable), "--session", str(request_path)],
            cwd=self.directory, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, creationflags=flags,
        )
        self.process = process
        self.native_process_count = 1
        try:
            self.lifetime = _ChildLifetime(process)
            deadline = monotonic() + 30.0
            while not ready_path.is_file():
                self.check_cancelled()
                failure = self._session_failure()
                self._raise_for_session_failure(
                    failure, context="CC 네이티브 세션 시작에 실패했습니다",
                )
                if failure is not None:
                    raise contract.CcCoreEnvironmentError(
                        f"CC 네이티브 세션 시작에 실패했습니다: {failure.get('message', failure.get('status'))}"
                    )
                if process.poll() is not None:
                    failure = self._session_failure()
                    self._raise_for_session_failure(
                        failure, context="CC 네이티브 세션 시작에 실패했습니다",
                    )
                    raise contract.CcCoreEnvironmentError(
                        f"CC 네이티브 세션이 ready 전에 종료되었습니다. 종료 코드: {process.returncode}"
                    )
                if monotonic() >= deadline:
                    raise contract.CcCoreEnvironmentError("CC 네이티브 세션 준비 시간이 30초를 넘었습니다.")
                sleep(0.01)
            ready = contract.read_json_object(ready_path)
            contract.validate_native_session_identity(
                ready, session_id=self.session_id, job_id=self.job_id,
                input_fingerprint=self.input_fingerprint,
            )
            if ready.get("status") != "ready" or ready.get("process_id") != process.pid:
                raise contract.CcCoreEnvironmentError("CC 네이티브 세션 ready 응답이 현재 자식 프로세스와 다릅니다.")
            native_metadata = contract.validate_native_execution_metadata(
                ready, requested_max_threads=self.parameters["max_threads"],
            )
            self._accept_native_identity(native_metadata)
            failure = self._session_failure()
            self._raise_for_session_failure(
                failure, context="CC 네이티브 세션 시작에 실패했습니다",
            )
            if failure is not None:
                raise contract.CcCoreEnvironmentError(
                    f"CC 네이티브 세션이 ready와 함께 종료되었습니다: {failure.get('status')}"
                )
            if process.poll() is not None:
                failure = self._session_failure()
                self._raise_for_session_failure(
                    failure, context="CC 네이티브 세션 시작에 실패했습니다",
                )
                raise contract.CcCoreEnvironmentError(
                    f"CC 네이티브 세션이 ready 직후 종료되었습니다. 종료 코드: {process.returncode}"
                )
            self.process_startup_seconds = float(monotonic() - started)
        except Exception:
            self.abort()
            raise

    def _accept_native_identity(self, native_metadata: Mapping[str, Any]) -> None:
        """세션 전체 코어·패치·스레드 식별자가 Manifest와 같은지 고정합니다."""
        if self.runtime_manifest.get("core_commit") not in (None, native_metadata["core_commit"]):
            raise contract.CcCoreEnvironmentError("CC 결과의 코어 커밋이 Runtime Manifest와 다릅니다.")
        if self.runtime_manifest.get("core_patch") not in (None, native_metadata["core_patch"]):
            raise contract.CcCoreEnvironmentError("CC 결과의 코어 패치가 Runtime Manifest와 다릅니다.")
        if self.runtime_manifest.get("tbb_version") not in (None, native_metadata["tbb_version"]):
            raise contract.CcCoreEnvironmentError("CC 결과의 oneTBB 버전이 Runtime Manifest와 다릅니다.")
        identity = {key: value for key, value in native_metadata.items() if key != "elapsed_seconds"}
        if self.native_identity is None:
            self.native_identity = identity
        elif identity != self.native_identity:
            raise contract.CcCoreEnvironmentError("CC 세션 중 네이티브 실행 식별자가 변경되었습니다.")
        self.core_commit = native_metadata["core_commit"]

    def geometry(
        self, label: str, positions: np.ndarray, triangles: np.ndarray | None = None,
        *, cache_scope: str = "SESSION",
    ) -> Mapping[str, Any]:
        """입력 배열과 세션·명령 수명 범위를 최초 한 번만 전용 바이너리로 기록합니다."""
        self.check_cancelled()
        if not isinstance(label, str) or not label or any(
            character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for character in label
        ):
            raise contract.CcCoreInputError("CC 지오메트리 이름은 영문·숫자·하이픈·밑줄만 사용할 수 있습니다.")
        if cache_scope not in {"SESSION", "COMMAND"}:
            raise contract.CcCoreInputError("CC 지오메트리 캐시 범위가 올바르지 않습니다.")
        if label in self.files:
            if self.files[label].get("cache_scope") != cache_scope:
                raise contract.CcCoreInputError("같은 CC 지오메트리 ID의 캐시 범위를 변경할 수 없습니다.")
            return self.files[label]
        name = f"native/{label}-positions.bin"
        position_values = np.ascontiguousarray(positions, dtype="<f8")
        _write_array_atomic(contract.job_path(self.directory, name), position_values)
        entry: dict[str, Any] = {
            "geometry_id": label,
            "positions_file": name,
            "point_count": int(position_values.shape[0]),
            "cache_scope": cache_scope,
        }
        if triangles is not None:
            triangle_name = f"native/{label}-triangles.bin"
            triangle_values = np.ascontiguousarray(triangles, dtype="<u4")
            _write_array_atomic(contract.job_path(self.directory, triangle_name), triangle_values)
            entry.update({"triangles_file": triangle_name, "triangle_count": int(triangle_values.shape[0])})
        self.files[label] = entry
        return entry

    def _command_paths(self, sequence: int) -> tuple[Path, Path, Path, str]:
        """단조 순번에 대응하는 요청·응답·진행·거리 파일 경로를 반환합니다."""
        stem = f"native/command-{sequence:04d}"
        return (
            contract.job_path(self.directory, f"{stem}.request.json"),
            contract.job_path(self.directory, f"{stem}.response.json"),
            contract.job_path(self.directory, f"{stem}.progress.json"),
            f"{stem}.distances.bin",
        )

    def _session_failure(self) -> Mapping[str, Any] | None:
        """워커가 세션 전체 실패를 원자 기록했으면 식별자를 확인해 반환합니다."""
        path = contract.job_path(self.directory, "native/session-result.json")
        if not path.is_file():
            return None
        payload = contract.read_json_object(path)
        contract.validate_native_session_identity(
            payload, session_id=self.session_id, job_id=self.job_id,
            input_fingerprint=self.input_fingerprint,
        )
        return payload

    def _raise_for_session_failure(self, failure: Mapping[str, Any] | None, *, context: str) -> None:
        """검증된 세션 종료 상태에서 취소와 실제 오류만 일관되게 발생시킵니다."""
        if failure is None or failure.get("status") in {"closed", "completed"}:
            return
        if failure.get("status") == "cancelled":
            raise contract.CcCoreCancelled("CC 네이티브 세션을 취소했습니다.")
        raise contract.CcCoreEnvironmentError(
            f"{context}: {failure.get('message', failure.get('status'))}"
        )

    def _wait_for_response(
        self, response_path: Path, progress_path: Path, *, sequence: int,
        operation: str, stage: str, allow_cancel: bool = True,
    ) -> dict[str, Any]:
        """현재 순번 응답을 기다리며 진행률·취소·충돌을 같은 세션 경계에서 처리합니다."""
        process = self.process
        if process is None:
            raise contract.CcCoreEnvironmentError("CC 네이티브 세션 프로세스가 시작되지 않았습니다.")
        last_progress = None
        cancelled_at = None
        while not response_path.is_file():
            cancelled = contract.job_path(self.directory, "cancel.request").is_file() or (
                self.cancel_probe is not None and self.cancel_probe()
            )
            if cancelled and allow_cancel:
                if cancelled_at is None:
                    cancelled_at = monotonic()
                    contract.job_path(self.directory, "cancel.request").touch(exist_ok=True)
                elif monotonic() - cancelled_at >= 2.0:
                    self.abort()
                    raise contract.CcCoreCancelled("CC 정렬 작업을 취소했습니다.")
            failure = self._session_failure()
            self._raise_for_session_failure(failure, context="CC 네이티브 세션이 실패했습니다")
            if progress_path.is_file() and self.progress_sink is not None:
                native_progress = contract.read_json_object(progress_path)
                contract.validate_native_session_identity(
                    native_progress, session_id=self.session_id, job_id=self.job_id,
                    input_fingerprint=self.input_fingerprint, sequence=sequence,
                )
                if native_progress.get("operation") != operation or native_progress.get("stage") != stage:
                    raise contract.CcCoreEnvironmentError("CC 네이티브 진행률의 연산 또는 단계가 현재 명령과 다릅니다.")
                if native_progress != last_progress:
                    self.progress_sink({"native": native_progress})
                    last_progress = native_progress
            if process.poll() is not None:
                # 종료 직전 공개된 취소 결과를 다시 읽어 프로세스 코드와의 관찰 경쟁을 닫습니다.
                failure = self._session_failure()
                self._raise_for_session_failure(failure, context="CC 네이티브 세션이 실패했습니다")
                if response_path.is_file():
                    break
                raise contract.CcCoreEnvironmentError(
                    f"CC 네이티브 세션이 응답 전에 종료되었습니다. 종료 코드: {process.returncode}"
                )
            sleep(0.01)
        if cancelled_at is not None:
            raise contract.CcCoreCancelled("CC 정렬 작업을 취소했습니다.")
        return contract.read_json_object(response_path)

    def call(
        self, operation: str, data: Mapping[str, Any], model: Mapping[str, Any],
        *, adjust_scale: bool = False, reverse: bool = False, stage: str = "PREPARING",
        evaluation_key_sha256: str = "",
    ) -> dict[str, Any]:
        """단일 세션에 다음 순번 명령을 기록하고 응답·거리 파일을 엄격히 검증합니다."""
        self.check_cancelled()
        self._validate_runtime_unchanged("CC 호출 전에 실행 파일이나 라이브러리 배포 정보가 변경되었습니다.")
        self.start()
        if operation not in {"register", "distance"}:
            raise contract.CcCoreInputError("CC 네이티브 명령 종류가 올바르지 않습니다.")
        if evaluation_key_sha256 and (
            operation != "distance"
            or len(evaluation_key_sha256) != 64
            or any(character not in "0123456789abcdef" for character in evaluation_key_sha256)
        ):
            raise contract.CcCoreInputError("CC 평가 캐시 키 해시가 거리 연산 계약과 다릅니다.")
        self.counter += 1
        number = self.counter
        request_path, response_path, progress_path, distance_name = self._command_paths(number)
        for path in (request_path, response_path, progress_path, contract.job_path(self.directory, distance_name)):
            if path.exists():
                raise contract.CcCoreInputError("CC 네이티브 명령 파일이 순번 실행 전에 이미 존재합니다.")
        sampling_limit = max(int(data["point_count"]), int(model["point_count"]) if "triangles_file" not in model else 1)
        parameters = {
            "adjust_scale": adjust_scale,
            "min_scale": (1.0 / contract.CC_MAX_SCALE if reverse else contract.CC_MIN_SCALE) if adjust_scale else 1.0,
            "max_scale": (1.0 / contract.CC_MIN_SCALE if reverse else contract.CC_MAX_SCALE) if adjust_scale else 1.0,
            "overlap_ratio": self.parameters["overlap_ratio"],
            "max_iterations": self.parameters["max_iterations"],
            "sampling_limit": sampling_limit,
            "max_threads": self.parameters["max_threads"],
        }
        request = {
            **self._session_payload(), "sequence": number, "operation": operation, "stage": stage,
            "data": dict(data), "model": dict(model), "parameters": parameters,
            "response_file": f"native/command-{number:04d}.response.json",
            "progress_file": f"native/command-{number:04d}.progress.json",
            "distances_file": distance_name,
        }
        roundtrip_started = monotonic()
        contract.write_json_atomic(request_path, request)
        result = self._wait_for_response(
            response_path, progress_path, sequence=number, operation=operation, stage=stage,
        )
        contract.validate_native_session_identity(
            result, session_id=self.session_id, job_id=self.job_id,
            input_fingerprint=self.input_fingerprint, sequence=number,
        )
        if result.get("operation") != operation or result.get("stage") != stage:
            raise contract.CcCoreEnvironmentError("CC 네이티브 응답의 연산 또는 단계가 현재 명령과 다릅니다.")
        if result.get("status") == "cancelled":
            raise contract.CcCoreCancelled("CC 네이티브 계산을 취소했습니다.")
        if result.get("status") != "completed":
            raise contract.CcCoreNumericalError(
                f"CC {operation} 계산에 실패했습니다: {result.get('message', result.get('error', result.get('status')))}"
            )
        native_metadata = contract.validate_native_execution_metadata(
            result,
            requested_max_threads=parameters["max_threads"],
            expected_identity=self.native_identity,
        )
        self._accept_native_identity(native_metadata)
        timings = contract.validate_native_timings(result.get("timings"))
        operation_statistics = result.get("operation_statistics")
        if not isinstance(operation_statistics, Mapping) or set(operation_statistics) != set(contract.CC_NATIVE_COUNTER_FIELDS):
            raise contract.CcCoreEnvironmentError("CC 네이티브 명령 통계가 고정 계약과 다릅니다.")
        counters = {
            name: contract.nonnegative_integer(operation_statistics.get(name), f"CC 호출 {name}")
            for name in contract.CC_NATIVE_COUNTER_FIELDS
        }
        data_geometry_id = data.get("geometry_id")
        model_geometry_id = model.get("geometry_id")
        if result.get("data_geometry_id") != data_geometry_id or result.get("model_geometry_id") != model_geometry_id:
            raise contract.CcCoreEnvironmentError("CC 네이티브 응답의 지오메트리 ID가 현재 명령과 다릅니다.")
        logical_index = len(self.logical_operations) + 1
        physical_summary = {
            "operation": operation,
            "index": number,
            "sequence": number,
            "logical_index": logical_index,
            "stage": stage,
            "execution_source": "native_session",
            "evaluation_key_sha256": evaluation_key_sha256,
            "elapsed_seconds": native_metadata["elapsed_seconds"],
            "roundtrip_seconds": 0.0,
            "protocol_overhead_seconds": 0.0,
            "requested_max_threads": native_metadata["requested_max_threads"],
            "effective_max_threads": native_metadata["effective_max_threads"],
            "data_geometry_id": data_geometry_id,
            "model_geometry_id": model_geometry_id,
            "timings": timings,
            **counters,
        }
        if operation == "distance":
            count = contract.positive_integer(result.get("count", result.get("point_count")), "CC 거리 수")
            if count != data["point_count"]:
                raise contract.CcCoreNumericalError("CC 거리 개수가 평가점 수와 다릅니다.")
            distances_path = contract.job_path(self.directory, result.get("distances_file"))
            if distances_path != contract.job_path(self.directory, distance_name):
                raise contract.CcCoreEnvironmentError("CC 거리 응답 경로가 현재 명령 순번과 다릅니다.")
            if distances_path.stat().st_size != int(count) * 8:
                raise contract.CcCoreNumericalError("CC 거리 바이너리 크기가 평가점 수와 다릅니다.")
            distances = np.fromfile(distances_path, dtype="<f8")
            if not np.isfinite(distances).all() or np.any(distances < 0.0):
                raise contract.CcCoreNumericalError("CC 평가 거리에 음수나 유한하지 않은 값이 있습니다.")
            result["distances"] = distances
        else:
            native_matrix, scale = contract.matrix_sim3(result.get("matrix_4x4"), "CC 네이티브 결과")
            matrix, correction = normalize_native_sim3(native_matrix)
            result["native_matrix_4x4"] = native_matrix.tolist()
            result["sim3_projection_frobenius"] = correction
            declared_scale = result.get("scale")
            if not isinstance(declared_scale, (int, float)) or not math.isfinite(declared_scale):
                raise contract.CcCoreNumericalError("CC 네이티브 결과 배율이 올바르지 않습니다.")
            if not np.isclose(scale, declared_scale, atol=5.0e-6, rtol=0.0):
                raise contract.CcCoreNumericalError("CC 네이티브 행렬과 보고 배율이 다릅니다.")
            tolerance = 5.0e-6
            if scale < parameters["min_scale"] - tolerance or scale > parameters["max_scale"] + tolerance:
                raise contract.CcCoreNumericalError("CC 네이티브 배율이 추가 배율 범위를 벗어났습니다.")
            count = contract.positive_integer(result.get("final_point_count"), "CC 최종 대응 수")
            if count < QUALITY_POLICY["minimum_correspondence_count"] or count > data["point_count"]:
                raise contract.CcCoreNumericalError("CC 정렬의 유효 대응 수가 부족하거나 입력보다 큽니다.")
            rms = result.get("final_rms")
            if isinstance(rms, bool) or not isinstance(rms, (int, float)) or not math.isfinite(rms) or rms < 0.0:
                raise contract.CcCoreNumericalError("CC 최종 RMS가 올바르지 않습니다.")
            result["matrix_4x4"] = matrix
        roundtrip_seconds = float(monotonic() - roundtrip_started)
        physical_summary["roundtrip_seconds"] = roundtrip_seconds
        physical_summary["protocol_overhead_seconds"] = max(
            0.0, roundtrip_seconds - native_metadata["elapsed_seconds"],
        )
        self.physical_native_calls.append(physical_summary)
        self.logical_operations.append({
            **{key: value for key, value in physical_summary.items() if key not in {"index", "logical_index"}},
            "index": logical_index,
            "sequence": logical_index,
            "physical_sequence": number,
            "source_sequence": number,
            "source_logical_index": logical_index,
        })
        self.last_call_reference = (logical_index, number)
        return result

    def record_cached_distance(
        self, *, stage: str, data_geometry_id: str, model_geometry_id: str,
        evaluation_key_sha256: str, source_logical_index: int, source_sequence: int,
    ) -> None:
        """비트 동일 평가의 원본 물리 명령을 가리키는 논리 거리 연산을 기록합니다."""
        if (
            len(evaluation_key_sha256) != 64
            or any(character not in "0123456789abcdef" for character in evaluation_key_sha256)
        ):
            raise contract.CcCoreEnvironmentError("CC 평가 캐시 적중 키가 올바르지 않습니다.")
        if not 1 <= source_sequence <= len(self.physical_native_calls):
            raise contract.CcCoreEnvironmentError("CC 평가 캐시가 존재하지 않는 물리 명령을 참조합니다.")
        source = self.physical_native_calls[source_sequence - 1]
        if (
            source["operation"] != "distance"
            or source["evaluation_key_sha256"] != evaluation_key_sha256
            or source["logical_index"] != source_logical_index
        ):
            raise contract.CcCoreEnvironmentError("CC 평가 캐시 원본의 연산·키·논리 순번이 다릅니다.")
        logical_index = len(self.logical_operations) + 1
        self.logical_operations.append({
            "operation": "distance",
            "index": logical_index,
            "sequence": logical_index,
            "physical_sequence": None,
            "source_sequence": source_sequence,
            "source_logical_index": source_logical_index,
            "stage": stage,
            "execution_source": "exact_evaluation_cache",
            "evaluation_key_sha256": evaluation_key_sha256,
            "elapsed_seconds": 0.0,
            "roundtrip_seconds": source["roundtrip_seconds"],
            "protocol_overhead_seconds": source["protocol_overhead_seconds"],
            "requested_max_threads": source["requested_max_threads"],
            "effective_max_threads": source["effective_max_threads"],
            "data_geometry_id": data_geometry_id,
            "model_geometry_id": model_geometry_id,
            "timings": {name: 0.0 for name in contract.CC_NATIVE_TIMING_FIELDS},
            **{name: 0 for name in contract.CC_NATIVE_COUNTER_FIELDS},
        })

    def close(self) -> None:
        """마지막 순번 close 명령과 세션 요약을 확인한 뒤 정상 종료를 확정합니다."""
        if self.closed:
            return
        if self.process is None:
            self.closed = True
            return
        process = self.process
        if process.poll() is not None:
            try:
                failure = self._session_failure()
            finally:
                self.abort()
            self._raise_for_session_failure(failure, context="CC 네이티브 세션이 close 전에 실패했습니다")
            raise contract.CcCoreEnvironmentError("CC 네이티브 세션이 close 전에 종료되었습니다.")
        sequence = self.counter + 1
        request_path, response_path, progress_path, _distance_name = self._command_paths(sequence)
        for path in (request_path, response_path, progress_path):
            if path.exists():
                self.abort()
                raise contract.CcCoreInputError("CC 네이티브 close 파일이 순번 실행 전에 이미 존재합니다.")
        request = {
            **self._session_payload(), "sequence": sequence, "operation": "close", "stage": "CLOSE",
            "response_file": f"native/command-{sequence:04d}.response.json",
            "progress_file": f"native/command-{sequence:04d}.progress.json",
        }
        try:
            contract.write_json_atomic(request_path, request)
            response = self._wait_for_response(
                response_path, progress_path, sequence=sequence,
                operation="close", stage="CLOSE",
            )
            contract.validate_native_session_identity(
                response, session_id=self.session_id, job_id=self.job_id,
                input_fingerprint=self.input_fingerprint, sequence=sequence,
            )
            if response.get("status") != "closed" or response.get("operation") != "close":
                raise contract.CcCoreEnvironmentError("CC 네이티브 세션 close 응답이 올바르지 않습니다.")
            native_metadata = contract.validate_native_execution_metadata(
                response, requested_max_threads=self.parameters["max_threads"],
                expected_identity=self.native_identity,
            )
            self._accept_native_identity(native_metadata)
            self.session_statistics = contract.validate_native_session_statistics(response.get("statistics"))
            self.session_elapsed_seconds = contract.finite_nonnegative_number(
                response.get("session_elapsed_seconds"), "CC 네이티브 세션 시간",
            )
            self.session_exit_reason = response.get("session_exit_reason", "")
            if self.session_exit_reason != "closed":
                raise contract.CcCoreEnvironmentError("CC 네이티브 세션 종료 사유가 정상 close가 아닙니다.")
            geometry_statistics = contract.validate_native_geometry_statistics(response.get("geometry_statistics"))
            self.geometry_statistics = [dict(item) for item in geometry_statistics]
            contract.validate_native_session_aggregates(
                self.session_statistics, geometry_statistics, self.physical_native_calls,
            )
            try:
                process.wait(timeout=2.0)
            except subprocess.TimeoutExpired as error:
                self.abort()
                raise contract.CcCoreEnvironmentError("CC 네이티브 세션이 close 뒤 2초 안에 종료되지 않았습니다.") from error
            if process.returncode != 0:
                raise contract.CcCoreEnvironmentError(f"CC 네이티브 세션 종료 코드가 0이 아닙니다: {process.returncode}")
            session_result = self._session_failure()
            if session_result is None or session_result.get("status") != "closed":
                raise contract.CcCoreEnvironmentError("CC 네이티브 세션의 최종 종료 파일이 없습니다.")
            self._validate_runtime_unchanged("CC 세션 중 실행 파일이나 라이브러리 배포 정보가 변경되었습니다.")
            self.closed = True
        except Exception:
            self.abort()
            raise
        finally:
            if self.lifetime is not None:
                self.lifetime.close()
                self.lifetime = None

    def abort(self) -> None:
        """오류·취소 시 남은 단일 자식 프로세스를 즉시 종료하고 Job Object를 닫습니다."""
        process = self.process
        if process is not None:
            _stop_child(process)
        if self.lifetime is not None:
            self.lifetime.close()
            self.lifetime = None
        self.closed = True


def _distance_summary(distances: np.ndarray, unit: float) -> dict[str, Any]:
    """유한 거리 배열에서 전체·중앙·꼬리·90퍼센트 절단 오차를 계산합니다."""
    if distances.size < QUALITY_POLICY["minimum_correspondence_count"]:
        raise contract.CcCoreNumericalError("CC 평가에 사용할 유효 거리가 부족합니다.")
    ordered = np.sort(distances)
    trimmed_count = max(1, int(math.ceil(ordered.size * QUALITY_POLICY["trimmed_fraction"])))
    values = {
        "rms": float(np.sqrt(np.mean(distances * distances))),
        "median": float(np.median(distances)),
        "p95": float(np.percentile(distances, 95.0)),
        "trimmed_rms_90": float(np.sqrt(np.mean(ordered[:trimmed_count] ** 2))),
    }
    return {
        "count": int(distances.size), "trimmed_count": trimmed_count,
        **{key: value * unit for key, value in values.items()},
        **{f"{key}_normalized": value for key, value in values.items()},
    }


def _spatial_bins(points: np.ndarray, distances: np.ndarray, unit: float) -> list[dict[str, Any]]:
    """원본 표본의 고정 8개 공간 구간별 RMS와 개수를 계산합니다."""
    center = np.median(points, axis=0)
    labels = np.sum((points >= center).astype(np.int32) * np.array((1, 2, 4)), axis=1)
    output = []
    for label in range(8):
        selected = distances[labels == label]
        if selected.size:
            rms = float(np.sqrt(np.mean(selected * selected)))
            output.append({"bin": label, "count": int(selected.size), "rms": rms * unit, "rms_normalized": rms})
    return output


def prepare_overlap_policy(
    runner: NativeRunner, moving: contract.CcCoreGeometry, reference: contract.CcCoreGeometry, reverse: bool,
) -> dict[str, Any]:
    """실제 등록 방향과 원본 형상 규모·점 간격으로 고정 대응 반경을 정합니다."""
    data = reference if reverse else moving
    model = moving if reverse else reference
    data_diagonal = float(np.linalg.norm(np.ptp(data.positions, axis=0)))
    spacing = 0.0
    spacing_count = 0
    if model.triangles is None:
        total = model.positions.shape[0]
        spacing_count = min(2048, max(1, total // 2))
        indices = np.sort(np.random.default_rng(contract.CC_EVALUATION_SEED + 2).choice(total, spacing_count, replace=False))
        remaining = np.ones(total, dtype=bool)
        remaining[indices] = False
        data_entry = runner.geometry("spacing-query", model.positions[indices], cache_scope="COMMAND")
        model_entry = runner.geometry("spacing-model", model.positions[remaining], cache_scope="COMMAND")
        distances = runner.call("distance", data_entry, model_entry, stage="PREPARING")["distances"]
        spacing = float(np.median(distances))
    radius = min(data_diagonal * 0.05, max(data_diagonal * 0.01, spacing * 3.0))
    if not math.isfinite(radius) or radius <= 0.0:
        raise contract.CcCoreInputError("CC 유효 대응 거리 기준을 계산하지 못했습니다.")
    return {
        **QUALITY_POLICY,
        "primary_direction": "reference_to_moving" if reverse else "moving_to_reference",
        "data_diagonal_normalized": data_diagonal,
        "model_point_spacing_normalized": spacing,
        "point_spacing_probe_count": spacing_count,
        "correspondence_radius_normalized": radius,
        "correspondence_radius_formula": "min(0.05 * data_diagonal, max(0.01 * data_diagonal, 3 * median_model_spacing))",
        "minimum_valid_correspondence_ratio": 0.2,
        "minimum_pass_correspondence_ratio": runner.parameters["overlap_ratio"] * 0.85,
        "minimum_spatial_span_ratio": 0.25,
        "minimum_covered_spatial_bins": 3,
        "minimum_bin_valid_ratio": 0.2,
        "policy_basis": "원본 등록 데이터 크기의 1퍼센트를 기본 반경으로 사용하고 점 모델의 실제 간격을 보완하되 5퍼센트로 제한합니다.",
    }


def correspondence_coverage(
    points: np.ndarray, distances: np.ndarray, policy: Mapping[str, Any],
) -> dict[str, Any]:
    """등록 데이터 방향의 고정 반경 대응 수와 공간 분포를 평가합니다."""
    valid = distances <= policy["correspondence_radius_normalized"]
    count = int(np.count_nonzero(valid))
    ratio = count / int(points.shape[0])
    center = np.median(points, axis=0)
    labels = np.sum((points >= center).astype(np.int32) * np.array((1, 2, 4)), axis=1)
    occupied = 0
    covered = 0
    bins = []
    for label in range(8):
        mask = labels == label
        total_count = int(np.count_nonzero(mask))
        if not total_count:
            continue
        occupied += 1
        valid_count = int(np.count_nonzero(mask & valid))
        fraction = valid_count / total_count
        if valid_count >= 1 and fraction >= policy["minimum_bin_valid_ratio"]:
            covered += 1
        bins.append({"bin": label, "count": total_count, "valid_count": valid_count, "valid_ratio": fraction})
    all_centered = points - np.mean(points, axis=0)
    full_variance = np.linalg.eigvalsh(all_centered.T @ all_centered / points.shape[0])
    active = full_variance > max(float(full_variance[-1]) * 1.0e-8, np.finfo(float).tiny)
    span_ratios = np.zeros(int(np.count_nonzero(active)), dtype=np.float64)
    if count >= 3:
        subset = points[valid]
        subset_centered = subset - np.mean(subset, axis=0)
        variance = np.maximum(0.0, np.linalg.eigvalsh(subset_centered.T @ subset_centered / count))
        span_ratios = np.sqrt(variance[active] / full_variance[active])
    return {
        "valid_count": count, "count": int(points.shape[0]), "valid_ratio": ratio,
        "covered_spatial_bins": covered, "occupied_spatial_bins": occupied,
        "spatial_span_ratios": span_ratios.tolist(), "bins": bins,
    }


def assess_correspondence_coverage(metrics: Mapping[str, Any], policy: Mapping[str, Any]) -> list[str]:
    """대응 부족이나 퇴화된 접촉은 실패시키고 제한된 겹침은 검토로 표시합니다."""
    coverage = metrics["correspondence_coverage"]
    if coverage["valid_count"] < policy["minimum_correspondence_count"]:
        raise contract.CcCoreNumericalError("CC 독립 평가에서 가까운 유효 대응점이 부족합니다.")
    if coverage["valid_ratio"] < policy["minimum_valid_correspondence_ratio"]:
        raise contract.CcCoreNumericalError("CC 독립 평가에서 실제로 겹치는 데이터 비율이 부족합니다.")
    required_bins = min(policy["minimum_covered_spatial_bins"], coverage["occupied_spatial_bins"])
    if coverage["covered_spatial_bins"] < required_bins or any(
        value < policy["minimum_spatial_span_ratio"] for value in coverage["spatial_span_ratios"]
    ):
        raise contract.CcCoreNumericalError("CC 대응점이 작은 영역이나 퇴화된 접촉면에 집중되어 있습니다.")
    if coverage["valid_ratio"] < policy["minimum_pass_correspondence_ratio"]:
        return ["유효 대응 비율이 요청한 겹침 비율의 자동 승인 기준보다 낮습니다."]
    return []


class ExactEvaluationCache:
    """작업 내 불변 평가 계약과 비트 동일 후보 행렬에만 거리 배열을 재사용합니다."""

    def __init__(
        self, context: Mapping[str, Any], moving_samples: np.ndarray, reference_samples: np.ndarray,
    ) -> None:
        """직렬화한 정책과 원본 표본 바이트를 캐시의 불변 범위로 고정합니다."""
        self._context_json = json.dumps(
            context, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
        ).encode("utf-8")
        self._moving_samples = np.ascontiguousarray(moving_samples, dtype="<f8").tobytes(order="C")
        self._reference_samples = np.ascontiguousarray(reference_samples, dtype="<f8").tobytes(order="C")
        self._entries: dict[tuple[bytes, bytes, bytes, str, bytes], tuple[np.ndarray, int, int]] = {}
        self.hits = 0
        self.misses = 0

    def _key(self, direction: str, matrix: np.ndarray) -> tuple[tuple[bytes, bytes, bytes, str, bytes], str]:
        """평가 방향과 검증된 유사변환의 원시 float64 바이트로 완전 키를 만듭니다."""
        if direction not in {"moving_to_reference", "reference_to_moving"}:
            raise contract.CcCoreInputError("CC 평가 캐시 방향이 올바르지 않습니다.")
        sim3, _scale = contract.matrix_sim3(matrix, "CC 평가 캐시 후보 행렬")
        matrix_bytes = np.ascontiguousarray(sim3, dtype="<f8").tobytes(order="C")
        key = (
            self._context_json, self._moving_samples, self._reference_samples, direction, matrix_bytes,
        )
        digest = hashlib.sha256()
        for value in (*key[:3], direction.encode("ascii"), matrix_bytes):
            digest.update(len(value).to_bytes(8, "little", signed=False))
            digest.update(value)
        return key, digest.hexdigest()

    def lookup(
        self, direction: str, matrix: np.ndarray,
    ) -> tuple[np.ndarray | None, str, int | None, int | None]:
        """완전 키가 같을 때만 읽기 전용 거리와 원본 논리·물리 순번을 반환합니다."""
        key, digest = self._key(direction, matrix)
        entry = self._entries.get(key)
        if entry is None:
            self.misses += 1
            return None, digest, None, None
        self.hits += 1
        distances, source_logical_index, source_sequence = entry
        return distances, digest, source_logical_index, source_sequence

    def store(
        self, direction: str, matrix: np.ndarray, distances: np.ndarray,
        *, source_logical_index: int, source_sequence: int,
    ) -> None:
        """물리 거리 명령이 성공한 뒤 원시 거리와 출처 순번을 비트 동일 키에 보관합니다."""
        key, _digest = self._key(direction, matrix)
        if key in self._entries:
            raise contract.CcCoreEnvironmentError("CC 평가 캐시의 같은 완전 키를 중복 저장할 수 없습니다.")
        values = np.array(distances, dtype="<f8", order="C", copy=True)
        if values.ndim != 1 or not np.isfinite(values).all() or np.any(values < 0.0):
            raise contract.CcCoreNumericalError("CC 평가 캐시에 유효하지 않은 거리 배열을 저장할 수 없습니다.")
        values.setflags(write=False)
        self._entries[key] = (values, source_logical_index, source_sequence)

    def statistics(self) -> dict[str, int | str]:
        """정확 후보 캐시의 범위와 적중·미스·항목 수를 결과용으로 반환합니다."""
        return {
            "policy": "BIT_EXACT_MATRIX_AND_EVALUATION_CONTRACT",
            "entry_count": len(self._entries),
            "hit_count": self.hits,
            "miss_count": self.misses,
        }


def evaluate_candidate(
    runner: NativeRunner, label: str, delta: np.ndarray,
    moving: contract.CcCoreGeometry, reference: contract.CcCoreGeometry,
    moving_samples: np.ndarray, reference_samples: np.ndarray, normalization: Normalization,
    overlap_policy: Mapping[str, Any] | None = None, *, stage: str,
    evaluation_cache: ExactEvaluationCache | None = None,
) -> dict[str, Any]:
    """동일 원본 표본의 실제 점·삼각면 거리를 후보마다 독립 계산합니다."""
    directions: dict[str, Any] = {}
    arrays = []
    coverage = None
    # 기본 평가는 이동→기준이며 메시·메시만 양방향, 메시·점은 역방향입니다.
    if not (moving.triangles is not None and reference.triangles is None):
        query_label = f"{label}-moving-evaluation"
        model_label = "reference-model"
        cached = None
        evaluation_key = ""
        source_logical_index = source_sequence = None
        if evaluation_cache is not None:
            cached, evaluation_key, source_logical_index, source_sequence = evaluation_cache.lookup(
                "moving_to_reference", delta,
            )
        if cached is not None:
            runner.record_cached_distance(
                stage=stage, data_geometry_id=query_label, model_geometry_id=model_label,
                evaluation_key_sha256=evaluation_key,
                source_logical_index=int(source_logical_index), source_sequence=int(source_sequence),
            )
            distances = cached
        else:
            query = runner.geometry(
                query_label, transform_positions(moving_samples, delta), cache_scope="COMMAND",
            )
            model = runner.geometry(model_label, reference.positions, reference.triangles)
            distances = runner.call(
                "distance", query, model, stage=stage, evaluation_key_sha256=evaluation_key,
            )["distances"]
            if evaluation_cache is not None:
                if runner.last_call_reference is None:
                    raise contract.CcCoreEnvironmentError("CC 평가 캐시 원본 명령 순번이 없습니다.")
                source_logical_index, source_sequence = runner.last_call_reference
                evaluation_cache.store(
                    "moving_to_reference", delta, distances,
                    source_logical_index=source_logical_index, source_sequence=source_sequence,
                )
        summary = _distance_summary(distances, normalization.unit)
        summary["spatial_bins"] = _spatial_bins(moving_samples, distances, normalization.unit)
        directions["moving_to_reference"] = summary
        if overlap_policy is not None and overlap_policy["primary_direction"] == "moving_to_reference":
            coverage = correspondence_coverage(moving_samples, distances, overlap_policy)
        arrays.append(distances)
    if moving.triangles is not None:
        query_label = f"{label}-reference-inverse-evaluation"
        model_label = "moving-model"
        cached = None
        evaluation_key = ""
        source_logical_index = source_sequence = None
        if evaluation_cache is not None:
            cached, evaluation_key, source_logical_index, source_sequence = evaluation_cache.lookup(
                "reference_to_moving", delta,
            )
        if cached is not None:
            runner.record_cached_distance(
                stage=stage, data_geometry_id=query_label, model_geometry_id=model_label,
                evaluation_key_sha256=evaluation_key,
                source_logical_index=int(source_logical_index), source_sequence=int(source_sequence),
            )
            distances = cached
        else:
            inverse_query, scale = inverse_surface_query(reference_samples, delta)
            query = runner.geometry(query_label, inverse_query, cache_scope="COMMAND")
            model = runner.geometry(model_label, moving.positions, moving.triangles)
            distances = np.ascontiguousarray(
                runner.call(
                    "distance", query, model, stage=stage, evaluation_key_sha256=evaluation_key,
                )["distances"] * scale,
                dtype=np.float64,
            )
            if evaluation_cache is not None:
                if runner.last_call_reference is None:
                    raise contract.CcCoreEnvironmentError("CC 평가 캐시 원본 명령 순번이 없습니다.")
                source_logical_index, source_sequence = runner.last_call_reference
                evaluation_cache.store(
                    "reference_to_moving", delta, distances,
                    source_logical_index=source_logical_index, source_sequence=source_sequence,
                )
        summary = _distance_summary(distances, normalization.unit)
        summary["spatial_bins"] = _spatial_bins(reference_samples, distances, normalization.unit)
        directions["reference_to_moving"] = summary
        if overlap_policy is not None and overlap_policy["primary_direction"] == "reference_to_moving":
            coverage = correspondence_coverage(reference_samples, distances, overlap_policy)
        arrays.append(distances)
    result = _distance_summary(np.concatenate(arrays), normalization.unit)
    result["mean_rms"] = float(np.mean([entry["rms"] for entry in directions.values()]))
    result["mean_rms_normalized"] = float(np.mean([entry["rms_normalized"] for entry in directions.values()]))
    result["directions"] = directions
    result["spatial_bins"] = {name: item["spatial_bins"] for name, item in directions.items()}
    if coverage is not None:
        result["correspondence_coverage"] = coverage
    return result


def select_candidate(
    metrics: Mapping[str, Mapping[str, Any]], diagnostics: Mapping[str, Any]
) -> tuple[str, str, list[str], float, str]:
    """평균 방향 RMS로 후보를 고르고 꼬리·공간 악화와 배율 경계를 검토 대상으로 표시합니다."""
    best = min(("baseline", "rigid", "scale_refine"), key=lambda key: metrics[key]["mean_rms_normalized"])
    baseline = metrics["baseline"]
    selected = metrics[best]
    initial_rms = baseline["mean_rms_normalized"]
    final_rms = selected["mean_rms_normalized"]
    tolerance = QUALITY_POLICY["absolute_distance_tolerance_normalized"]
    improvement = (initial_rms - final_rms) / max(initial_rms, np.finfo(np.float64).eps)
    reasons = []
    if initial_rms - final_rms <= tolerance or improvement < QUALITY_POLICY["minimum_relative_mean_rms_improvement"]:
        reasons.append("독립 평가의 평균 RMS가 충분히 감소하지 않았습니다.")
    if metrics["scale_refine"]["mean_rms_normalized"] > metrics["rigid"]["mean_rms_normalized"] + tolerance:
        reasons.append("배율 보정 결과가 크기 고정 결과보다 나빠 더 좋은 기존 후보를 유지했습니다.")
    for direction, selected_direction in selected["directions"].items():
        baseline_direction = baseline["directions"][direction]
        if selected_direction["p95_normalized"] > baseline_direction["p95_normalized"] * (
            1.0 + QUALITY_POLICY["maximum_p95_relative_increase"]
        ) + tolerance:
            direction_label = "이동→기준" if direction == "moving_to_reference" else "기준→이동"
            reasons.append(f"{direction_label} 평가의 상위 5퍼센트 거리 오차가 증가했습니다.")
        previous_bins = {entry["bin"]: entry for entry in baseline_direction["spatial_bins"]}
        for current in selected_direction["spatial_bins"]:
            previous = previous_bins[current["bin"]]
            if current["count"] >= QUALITY_POLICY["minimum_spatial_bin_count"] and current["rms_normalized"] > (
                previous["rms_normalized"] * (1.0 + QUALITY_POLICY["maximum_spatial_bin_relative_increase"]) + tolerance
            ):
                direction_label = "이동→기준" if direction == "moving_to_reference" else "기준→이동"
                reasons.append(f"{direction_label}의 공간 구간 {current['bin']}에서 거리 오차가 증가했습니다.")
    scale = diagnostics["additional_scale"]
    if min(abs(scale - contract.CC_MIN_SCALE), abs(scale - contract.CC_MAX_SCALE)) <= 1.0e-5:
        reasons.append("추가 배율이 허용 범위의 경계에 도달했습니다.")
    stop_reason = {
        "baseline": "NO_MEAN_RMS_IMPROVEMENT", "rigid": "RIGID_RETAINED", "scale_refine": "SCALE_REFINED",
    }[best]
    return best, "REVIEW" if reasons else "PASS", reasons, float(improvement), stop_reason


def solve_cccore_job(
    job_directory: Path | str, native_worker: Path | str,
    progress_sink: ProgressSink = None, cancel_probe: CancelProbe = None,
) -> CcCoreSolverResult:
    """고정 배율 정렬과 ±10퍼센트 배율 보정을 실행하고 독립 평가로 최종 후보를 선택합니다."""
    root = Path(job_directory).resolve(strict=True)
    manifest = contract.validate_manifest(contract.read_json_object(contract.job_path(root, "manifest.json")), root)
    parameters = contract.resolve_parameters(manifest["parameters"])
    identity = contract.identity_payload(manifest)
    started = monotonic()
    current_event: dict[str, Any] = {}

    def progress(stage: str, fraction: float, message: str) -> None:
        """계산 단계와 한국어 진행 설명에 동일한 작업 식별자를 붙입니다."""
        current_event.clear()
        current_event.update({
            **identity, "stage": stage, "state": "RUNNING", "progress": fraction,
            "message": message, "elapsed_seconds": float(monotonic() - started),
        })
        if progress_sink is not None:
            progress_sink(dict(current_event))

    def native_progress(event: Mapping[str, Any]) -> None:
        """네이티브 진행률을 현재 외부 작업 단계의 보조 진단으로 전달합니다."""
        if progress_sink is not None:
            progress_sink({**current_event, **event, "elapsed_seconds": float(monotonic() - started)})

    runner = NativeRunner(
        root, native_worker, parameters, cancel_probe, native_progress,
        session_identity=manifest,
    )

    def execute_session() -> CcCoreSolverResult:
        """준비부터 결과 메타데이터 확정까지 하나의 네이티브 세션 안에서 실행합니다."""
        runner.start()
        progress("PREPARING", 0.02, "CC 정렬 입력과 독립 평가 표본을 준비합니다.")
        runner.check_cancelled()
        moving_world = contract.load_geometry(root, manifest["geometry"]["moving"])
        reference_world = contract.load_geometry(root, manifest["geometry"]["reference"])
        normalization = make_normalization(moving_world.positions, reference_world.positions)
        moving = contract.CcCoreGeometry(
            moving_world.kind, normalization.apply(moving_world.positions), moving_world.triangles, moving_world.provenance
        )
        reference = contract.CcCoreGeometry(
            reference_world.kind, normalization.apply(reference_world.positions), reference_world.triangles, reference_world.provenance
        )
        mode = contract.GEOMETRY_MODES[(moving.kind, reference.kind)]
        reverse = mode == "MESH_TO_POINT"
        overlap_policy = prepare_overlap_policy(runner, moving, reference, reverse)
        seed = contract.CC_EVALUATION_SEED
        moving_samples = fixed_evaluation_points(moving, parameters["evaluation_sample_count"], seed)
        reference_samples = fixed_evaluation_points(reference, parameters["evaluation_sample_count"], seed + 1)
        evaluation_cache = ExactEvaluationCache({
            "algorithm_version": contract.CC_ALGORITHM_VERSION,
            "native_worker_sha256": runner.native_worker_sha256,
            "job_id": manifest["job_id"],
            "input_fingerprint": manifest["input_fingerprint"],
            "geometry": manifest["geometry"],
            "geometry_mode": mode,
            "normalization": normalization.to_mapping(),
            "parameters": parameters,
            "quality_policy": overlap_policy,
            "optimizer_seed": contract.CC_OPTIMIZER_SEED,
            "evaluation_seed": contract.CC_EVALUATION_SEED,
        }, moving_samples, reference_samples)
        deltas = {"baseline": np.identity(4, dtype=np.float64)}
        metrics = {}
        progress("BASELINE", 0.08, "정렬 전 원본 표본의 표면 거리를 평가합니다.")
        metrics["baseline"] = evaluate_candidate(
            runner, "baseline", deltas["baseline"], moving, reference, moving_samples, reference_samples,
            normalization, overlap_policy, stage="BASELINE", evaluation_cache=evaluation_cache,
        )
        if reverse:
            data_positions = reference.positions
            model_geometry = moving
        else:
            data_positions = (
                sample_triangle_surface(
                    moving.positions, moving.triangles, parameters["mesh_sample_count"],
                    contract.CC_OPTIMIZER_SEED,
                ) if moving.triangles is not None else moving.positions
            )
            model_geometry = reference
        data = runner.geometry("registration-data", data_positions, cache_scope="COMMAND")
        model_label = "moving-model" if reverse else "reference-model"
        model = runner.geometry(model_label, model_geometry.positions, model_geometry.triangles)
        progress("RIGID", 0.20, "크기를 고정한 CC 정밀 정렬을 계산합니다.")
        rigid = runner.call("register", data, model, adjust_scale=False, reverse=reverse, stage="RIGID")
        rigid_native = rigid["matrix_4x4"]
        deltas["rigid"] = np.linalg.inv(rigid_native) if reverse else rigid_native
        progress("RIGID_EVALUATION", 0.43, "크기 고정 결과를 원본 표본으로 평가합니다.")
        metrics["rigid"] = evaluate_candidate(
            runner, "rigid", deltas["rigid"], moving, reference, moving_samples, reference_samples,
            normalization, overlap_policy, stage="RIGID_EVALUATION", evaluation_cache=evaluation_cache,
        )
        # 두 번째 등록은 첫 번째 결과를 좌표에 반영하고 같은 원본 모델을 유지합니다.
        refined_data = runner.geometry(
            "registration-rigid-data", transform_positions(data_positions, rigid_native), cache_scope="COMMAND",
        )
        progress("SCALE_REFINE", 0.57, "추가 배율 ±10퍼센트 안에서 CC 정렬을 정밀 보정합니다.")
        refined = runner.call(
            "register", refined_data, model, adjust_scale=True, reverse=reverse, stage="SCALE_REFINE",
        )
        refined_native = refined["matrix_4x4"] @ rigid_native
        deltas["scale_refine"] = np.linalg.inv(refined_native) if reverse else refined_native
        for name in ("rigid", "scale_refine"):
            _matrix, scale = contract.matrix_sim3(deltas[name], f"CC {name} 최종 변화량")
            if not contract.CC_MIN_SCALE - 5.0e-6 <= scale <= contract.CC_MAX_SCALE + 5.0e-6:
                raise contract.CcCoreNumericalError("CC 이동 모델의 최종 추가 배율이 ±10퍼센트를 벗어났습니다.")
        progress("REFINE_EVALUATION", 0.78, "배율 보정 결과를 같은 원본 표본으로 평가합니다.")
        metrics["scale_refine"] = evaluate_candidate(
            runner, "scale-refine", deltas["scale_refine"], moving, reference, moving_samples,
            reference_samples, normalization, overlap_policy, stage="REFINE_EVALUATION",
            evaluation_cache=evaluation_cache,
        )
        runner.check_cancelled()
        runner.close()
        worlds = {key: normalization.world_delta(value) for key, value in deltas.items()}
        best_name = min(metrics, key=lambda key: metrics[key]["mean_rms_normalized"])
        diagnostics = delta_diagnostics(worlds[best_name], normalization)
        diagnostics["rigid_sim3_projection_frobenius"] = rigid["sim3_projection_frobenius"]
        diagnostics["scale_refine_sim3_projection_frobenius"] = refined["sim3_projection_frobenius"]
        if file_sha256(runner.executable) != runner.native_worker_sha256 or native_build_identity(runner.executable) != runner.runtime_manifest:
            raise contract.CcCoreEnvironmentError("계산 중 CC 실행 파일이나 라이브러리 배포 정보가 변경되었습니다.")
        if runner.native_identity is None or runner.session_statistics is None:
            raise contract.CcCoreEnvironmentError("CC 계산에서 검증된 네이티브 세션 정보가 없습니다.")
        validated_native_calls = contract.validate_native_call_summaries(
            runner.logical_operations,
            geometry_mode=mode,
            requested_max_threads=runner.native_identity["requested_max_threads"],
            effective_max_threads=runner.native_identity["effective_max_threads"],
        )
        validated_physical_calls = contract.validate_physical_native_call_summaries(
            runner.physical_native_calls, validated_native_calls,
            requested_max_threads=runner.native_identity["requested_max_threads"],
            effective_max_threads=runner.native_identity["effective_max_threads"],
        )
        contract.validate_native_session_aggregates(
            runner.session_statistics, runner.geometry_statistics, validated_physical_calls,
        )
        evaluation_cache_statistics = contract.validate_exact_evaluation_cache_statistics(
            evaluation_cache.statistics(), validated_native_calls,
        )
        best_name, quality, reasons, improvement, stop_reason = select_candidate(metrics, diagnostics)
        coverage_reasons = assess_correspondence_coverage(metrics[best_name], overlap_policy)
        reasons.extend(coverage_reasons)
        if reasons:
            quality = "REVIEW"
        delta = worlds[best_name]
        initial, _initial_scale = contract.matrix_sim3(manifest["initial_alignment_4x4"], "CC 시작 정렬")
        final, _final_scale = contract.matrix_sim3(delta @ initial, "CC 최종 정렬")
        progress("FINALIZING", 0.96, "독립 거리 평가와 최종 비파괴 정렬 결과를 확정합니다.")

        def stage_metadata(value: Mapping[str, Any], stage_name: str) -> dict[str, Any]:
            """네이티브 정렬 진단을 독립 평가 지표와 구분해 기록합니다."""
            return {
                **{key: item for key, item in value.items() if key != "matrix_4x4"},
                "stage": stage_name, "matrix_4x4": value["matrix_4x4"].tolist(),
                "final_rms_project_world": float(value["final_rms"]) * normalization.unit,
                "data_point_count": data["point_count"], "sampling_limit": max(
                    data["point_count"], model["point_count"] if model_geometry.triangles is None else 1
                ),
            }

        logical_operations = [dict(item) for item in validated_native_calls]
        physical_native_calls = [dict(item) for item in validated_physical_calls]
        total_alignment_seconds = float(monotonic() - started)
        physical_roundtrip_seconds = math.fsum(
            item["roundtrip_seconds"] for item in validated_physical_calls
        )
        protocol_overhead_seconds = math.fsum(
            item["protocol_overhead_seconds"] for item in validated_physical_calls
        )
        session_protocol_statistics = contract.validate_session_protocol_statistics({
            "physical_roundtrip_seconds": physical_roundtrip_seconds,
            "protocol_overhead_seconds": protocol_overhead_seconds,
            "protocol_overhead_ratio_of_roundtrip": (
                protocol_overhead_seconds / physical_roundtrip_seconds
                if physical_roundtrip_seconds > 0.0 else 0.0
            ),
            "protocol_overhead_fraction_of_total_alignment": (
                protocol_overhead_seconds / total_alignment_seconds
                if total_alignment_seconds > 0.0 else 0.0
            ),
        }, validated_physical_calls, total_alignment_seconds=total_alignment_seconds)
        metadata: dict[str, Any] = {
            **identity, "status": "SUCCEEDED",
            **{key: manifest[key] for key in (
                "reference_model_ids", "parameter_fingerprint", "model_revision_snapshot",
                "model_state_snapshot", "marker_signature", "initial_alignment_4x4",
            )},
            "geometry_mode": mode, "reverse_registration": reverse, "geometry_frame": "PROJECT_WORLD",
            "registration_direction": "REFERENCE_TO_MOVING_THEN_INVERT" if reverse else "MOVING_TO_REFERENCE",
            "geometry_provenance": manifest["geometry"].get("provenance", {}),
            "geometry_input_fingerprint": manifest["geometry"].get("input_fingerprint", ""),
            "core_commit": runner.native_identity["core_commit"],
            "core_patch": runner.native_identity["core_patch"],
            "native_worker_sha256": runner.native_worker_sha256,
            "native_build_identity": runner.runtime_manifest,
            **{key: runner.native_identity[key] for key in (
                "thread_backend", "tbb_version", "requested_max_threads",
                "effective_max_threads", "hardware_thread_count",
                "distance_multithread_support", "octree_multithread_support",
            )},
            "native_process_count": runner.native_process_count,
            "logical_operations": logical_operations,
            "native_calls": logical_operations,
            "physical_native_calls": physical_native_calls,
            "physical_native_command_count": len(physical_native_calls),
            "exact_evaluation_cache": evaluation_cache_statistics,
            "process_startup_seconds": runner.process_startup_seconds,
            "session_elapsed_seconds": runner.session_elapsed_seconds,
            "session_exit_reason": runner.session_exit_reason,
            "geometry_statistics": runner.geometry_statistics,
            "session_statistics": dict(runner.session_statistics),
            **runner.session_statistics,
            "session_protocol_statistics": session_protocol_statistics,
            **session_protocol_statistics,
            "normalization": normalization.to_mapping(),
            "parameters": parameters, "quality_policy": overlap_policy,
            "quality": quality, "user_review_required": quality == "REVIEW", "review_reasons": reasons,
            "baseline_metrics": metrics["baseline"], "best_candidate_metrics": metrics[best_name],
            "candidate_metrics": metrics, "relative_improvement": improvement,
            "best_stage": best_name, "solver_stop_reason": stop_reason,
            "diagnostics": diagnostics,
            "registration_stages": [stage_metadata(rigid, "rigid"), stage_metadata(refined, "scale_refine")],
            "elapsed_seconds": total_alignment_seconds,
        }
        return CcCoreSolverResult(metadata, delta, final)

    try:
        return execute_session()
    except BaseException:
        runner.abort()
        raise
