"""외부 Blender ICP 작업의 파일 계약과 프로세스 생명주기를 관리합니다."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from math import isfinite
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from time import monotonic, sleep
from typing import Any, Mapping
from zipfile import BadZipFile

import numpy as np

from ..constants import (
    ICP_ALGORITHM_VERSION,
    ICP_JOB_PROTOCOL_VERSION,
    ICP_OPEN3D_REQUIRED_PYTHON_MAJOR,
    ICP_OPEN3D_REQUIRED_PYTHON_MINOR,
    ICP_OPEN3D_REQUIRED_VERSION,
    ICP_SMALL_GICP_REQUIRED_VERSION,
)
from ..logging_utils import get_logger
from .icp_solver_backend import (
    IcpSolverBackend,
    backend_algorithm_version,
    is_external_python_backend,
    is_open3d_backend,
    normalize_solver_backend,
)


_LOGGER = get_logger("alignment.icp_backend")
_MANIFEST_FILE_NAME = "manifest.json"
_PROGRESS_FILE_NAME = "progress.json"
_RESULT_FILE_NAME = "result.json"
_RESULT_ARRAY_FILE_NAME = "result.npz"
_ERROR_FILE_NAME = "error.json"
_CANCEL_FILE_NAME = "cancel.request"
_WORKER_LOG_FILE_NAME = "worker.log"
# Hybrid Point-to-Plane 입력은 이전 point-to-point worker와 호환되지 않습니다.
_JOB_MANIFEST_VERSION = 2
_ATOMIC_REPLACE_RETRY_SECONDS = 1.0
_ATOMIC_REPLACE_RETRY_INTERVAL_SECONDS = 0.01
_OPEN3D_ENVIRONMENT_TIMEOUT_SECONDS = 30.0
_OPEN3D_FULL_INPUT_POLICY = "FULL_OVERLAP_NO_DOWNSAMPLING"
_REQUIRED_HYBRID_RESULT_KEYS = frozenset(
    {
        "job_id",
        "input_fingerprint",
        "algorithm_version",
        "moving_model_id",
        "reference_model_ids",
        "quality",
        "level_results",
        "parameter_fingerprint",
        "model_revision_snapshot",
        "model_state_snapshot",
        "marker_signature",
        "best_candidate",
        "baseline_metrics",
        "best_candidate_metrics",
        "relative_improvement",
        "best_level_name",
        "best_iteration",
        "solver_stop_reason",
        "point_to_plane_rmse",
        "normal_valid_ratio",
        "normal_angle_pass_ratio",
        "density_sampling_metrics",
        "user_review_required",
    }
)
_REQUIRED_HYBRID_CANDIDATE_KEYS = frozenset(
    {
        "delta_4x4",
        "final_alignment_4x4",
        "level_name",
        "iteration",
        "objective_kind",
        "point_to_plane_rmse",
        "point_to_point_rmse",
        "fitness",
        "inlier_count",
        "reciprocal_ratio",
        "normal_valid_ratio",
        "normal_angle_pass_ratio",
        "maximum_correspondence_distance",
        "correspondence_signature",
        "evaluation_fingerprint",
    }
)
_REQUIRED_HYBRID_EVALUATION_KEYS = frozenset(
    {
        "point_to_point_rmse",
        "point_to_plane_rmse",
        "fitness",
        "reciprocal_ratio",
        "inlier_count",
        "query_count",
        "maximum_correspondence_distance",
        "normal_valid_ratio",
        "normal_angle_pass_ratio",
        "evaluation_fingerprint",
    }
)
_REQUIRED_HYBRID_DENSITY_KEYS = frozenset(
    {
        "shared_voxel_size",
        "macro_cell_divisions",
        "macro_cell_count",
        "occupied_macro_cell_count",
        "minimum_samples_per_macro_cell",
        "median_samples_per_macro_cell",
        "maximum_samples_per_macro_cell",
    }
)
_REQUIRED_OPEN3D_RESULT_KEYS = frozenset(
    {
        "job_id",
        "input_fingerprint",
        "algorithm_version",
        "backend_id",
        "backend_algorithm_version",
        "job_protocol_version",
        "moving_model_id",
        "reference_model_ids",
        "quality",
        "level_results",
        "parameter_fingerprint",
        "model_revision_snapshot",
        "model_state_snapshot",
        "marker_signature",
        "solver_stop_reason",
        "user_review_required",
        "full_input_audit",
        "worker_python_version",
        "worker_numpy_version",
        "open3d_version",
        "platform_architecture",
        "local_origin",
        "moving_diagonal",
        "delta_translation",
        "delta_rotation_degrees",
        "scale_mode",
        "scale_refine_status",
    }
)
_REQUIRED_SMALL_GICP_RESULT_KEYS = (
    _REQUIRED_OPEN3D_RESULT_KEYS.difference({"open3d_version"})
    | frozenset(
        {
            "small_gicp_version",
            "baseline_metrics",
            "best_candidate_metrics",
            "confidence_preprocessing",
        }
    )
)
_REQUIRED_SMALL_GICP_DISTRIBUTION_EVALUATION_KEYS = frozenset(
    {
        "point_to_point_rmse",
        "weighted_point_to_point_rmse",
        "residual_median",
        "residual_p90",
        "evaluation_correspondence_count",
        "low_weight_correspondence_count",
        "isolation_candidate_correspondence_count",
        "source_isolation_candidate_count",
        "target_isolation_candidate_count",
        "distance_unit",
    }
)
_REQUIRED_OPEN3D_FULL_INPUT_AUDIT_KEYS = frozenset(
    {
        "input_policy",
        "source_original_count",
        "target_original_count",
        "source_crop_count",
        "target_crop_count",
        "source_crop_point_ids_fingerprint",
        "target_crop_point_ids_fingerprint",
        "source_crop_positions_fingerprint",
        "target_crop_positions_fingerprint",
        "stage_input_fingerprints",
        "downsampling",
    }
)
_REQUIRED_OPEN3D_STAGE_AUDIT_KEYS = frozenset(
    {
        "source_positions_fingerprint",
        "target_positions_fingerprint",
        "source_point_ids_fingerprint",
        "target_point_ids_fingerprint",
    }
)
_REQUIRED_OPEN3D_DOWNSAMPLING_KEYS = frozenset(
    {
        "used",
        "voxel_down_sample",
        "point_budget_sampling",
    }
)
_REQUIRED_OPEN3D_RESULT_ARRAY_KEYS = frozenset(
    {
        "initial_alignment",
        "final_alignment",
        "icp_delta",
    }
)

# 이 문자열은 Blender가 import하지 않고 외부 CPython에만 전달하는 검사 코드입니다.
_OPEN3D_ENVIRONMENT_PROBE = r"""
import json
import platform
import struct
import sys

if platform.python_implementation() != "CPython":
    raise RuntimeError("CPython 실행 파일이 아닙니다.")
if sys.version_info[:2] != (3, 12):
    raise RuntimeError("Python 3.12가 필요합니다.")
if struct.calcsize("P") * 8 != 64:
    raise RuntimeError("64비트 Python이 필요합니다.")

import numpy as np
import open3d as o3d

if o3d.__version__ != "0.19.0":
    raise RuntimeError("open3d 0.19.0이 필요합니다.")

registration = o3d.pipelines.registration
required_names = (
    "registration_generalized_icp",
    "registration_icp",
    "TransformationEstimationForGeneralizedICP",
    "TransformationEstimationPointToPlane",
    "TukeyLoss",
    "ICPConvergenceCriteria",
)
missing = [name for name in required_names if not hasattr(registration, name)]
if missing:
    raise RuntimeError("필수 Open3D 등록 API가 없습니다: " + ", ".join(missing))

coordinates = np.asarray(
    [(float(x), float(y), float(z)) for x in range(3) for y in range(3) for z in range(3)],
    dtype=np.float64,
)
source = o3d.geometry.PointCloud()
target = o3d.geometry.PointCloud()
source.points = o3d.utility.Vector3dVector(coordinates)
target.points = o3d.utility.Vector3dVector(coordinates)
criteria = registration.ICPConvergenceCriteria(max_iteration=1)
gicp_result = registration.registration_generalized_icp(
    source,
    target,
    1.0,
    np.eye(4, dtype=np.float64),
    registration.TransformationEstimationForGeneralizedICP(),
    criteria,
)
if np.asarray(gicp_result.transformation, dtype=np.float64).shape != (4, 4):
    raise RuntimeError("GICP smoke 결과 행렬이 올바르지 않습니다.")
search = o3d.geometry.KDTreeSearchParamKNN(knn=8)
source.estimate_normals(search)
target.estimate_normals(search)
robust_result = registration.registration_icp(
    source,
    target,
    1.0,
    np.eye(4, dtype=np.float64),
    registration.TransformationEstimationPointToPlane(registration.TukeyLoss(1.0)),
    criteria,
)
if np.asarray(robust_result.transformation, dtype=np.float64).shape != (4, 4):
    raise RuntimeError("강건 Point-to-Plane smoke 결과 행렬이 올바르지 않습니다.")

print(json.dumps({
    "python_implementation": platform.python_implementation(),
    "worker_python_version": ".".join(str(part) for part in sys.version_info[:3]),
    "worker_numpy_version": np.__version__,
    "open3d_version": o3d.__version__,
    "platform_architecture": platform.architecture()[0],
}, sort_keys=True))
"""

# 이 문자열은 Blender가 import하지 않고 외부 CPython에만 전달하는 small_gicp 검사 코드입니다.
_SMALL_GICP_ENVIRONMENT_PROBE = r"""
import json
import platform
import struct
import sys
from importlib.metadata import version as package_version

if platform.python_implementation() != "CPython":
    raise RuntimeError("CPython 실행 파일이 아닙니다.")
if sys.version_info[:2] != (3, 12):
    raise RuntimeError("Python 3.12가 필요합니다.")
if struct.calcsize("P") * 8 != 64:
    raise RuntimeError("64비트 Python이 필요합니다.")

import numpy as np
import small_gicp

small_gicp_version = package_version("small-gicp")
if small_gicp_version != "1.0.1":
    raise RuntimeError("small_gicp 1.0.1이 필요합니다.")

required_names = (
    "align",
    "PointCloud",
    "KdTree",
    "estimate_normals_covariances",
)
missing = [name for name in required_names if not hasattr(small_gicp, name)]
if missing:
    raise RuntimeError("필수 small_gicp API가 없습니다: " + ", ".join(missing))

coordinates = np.asarray(
    [(float(x), float(y), float(z)) for x in range(3) for y in range(3) for z in range(3)],
    dtype=np.float64,
)
target = small_gicp.PointCloud(coordinates)
source = small_gicp.PointCloud(coordinates)
target_tree = small_gicp.KdTree(target, num_threads=1)
source_tree = small_gicp.KdTree(source, num_threads=1)
small_gicp.estimate_normals_covariances(target, target_tree, num_neighbors=8, num_threads=1)
small_gicp.estimate_normals_covariances(source, source_tree, num_neighbors=8, num_threads=1)
result = small_gicp.align(
    target,
    source,
    target_tree,
    init_T_target_source=np.eye(4, dtype=np.float64),
    registration_type="GICP",
    max_correspondence_distance=1.0,
    num_threads=1,
    max_iterations=1,
)
transformation = np.asarray(result.T_target_source, dtype=np.float64)
if transformation.shape != (4, 4) or not np.isfinite(transformation).all():
    raise RuntimeError("small_gicp smoke 결과 행렬이 올바르지 않습니다.")

print(json.dumps({
    "python_implementation": platform.python_implementation(),
    "worker_python_version": ".".join(str(part) for part in sys.version_info[:3]),
    "worker_numpy_version": np.__version__,
    "small_gicp_version": small_gicp_version,
    "platform_architecture": platform.architecture()[0],
}, sort_keys=True))
"""


class IcpBackendError(RuntimeError):
    """ICP 외부 작업의 준비·통신·프로세스 오류를 구분합니다."""


class IcpJobProtocolError(IcpBackendError):
    """작업 파일의 경로·형식·상태 계약 위반을 나타냅니다."""


@dataclass(frozen=True, slots=True)
class IcpJobFiles:
    """한 ICP 작업 디렉터리 안에서 허용하는 고정 파일 이름을 보관합니다."""

    job_directory: Path

    @property
    def manifest_path(self) -> Path:
        """worker 입력 manifest의 절대 경로를 반환합니다."""

        return self.job_directory / _MANIFEST_FILE_NAME

    @property
    def progress_path(self) -> Path:
        """worker 진행률 파일의 절대 경로를 반환합니다."""

        return self.job_directory / _PROGRESS_FILE_NAME

    @property
    def result_path(self) -> Path:
        """worker 결과 메타데이터 파일의 절대 경로를 반환합니다."""

        return self.job_directory / _RESULT_FILE_NAME

    @property
    def result_array_path(self) -> Path:
        """worker 결과 배열 파일의 절대 경로를 반환합니다."""

        return self.job_directory / _RESULT_ARRAY_FILE_NAME

    @property
    def error_path(self) -> Path:
        """worker 오류 메타데이터 파일의 절대 경로를 반환합니다."""

        return self.job_directory / _ERROR_FILE_NAME

    @property
    def cancel_path(self) -> Path:
        """메인 프로세스가 만드는 취소 sentinel 경로를 반환합니다."""

        return self.job_directory / _CANCEL_FILE_NAME

    @property
    def worker_log_path(self) -> Path:
        """worker 표준 출력과 오류를 보관할 진단 로그 경로를 반환합니다."""

        return self.job_directory / _WORKER_LOG_FILE_NAME


@dataclass(slots=True)
class IcpJobHandle:
    """실행 중이거나 막 끝난 한 외부 ICP 작업의 소유 참조를 보관합니다."""

    job_id: str
    files: IcpJobFiles
    process: subprocess.Popen[Any]
    input_fingerprint: str
    backend_id: IcpSolverBackend | str = IcpSolverBackend.CUSTOM_HYBRID.value
    backend_algorithm_version: str | None = None
    started_at: float = field(default_factory=monotonic)
    cancel_requested_at: float | None = None
    _worker_log_handle: Any = None
    cleaned: bool = False
    diagnostics_retained: bool = False

    def __post_init__(self) -> None:
        """handle에 기록할 backend 식별자가 고정 계약과 맞는지 검증합니다."""

        try:
            backend = normalize_solver_backend(self.backend_id)
        except ValueError as error:
            raise IcpJobProtocolError("ICP worker handle backend ID가 올바르지 않습니다.") from error
        expected_algorithm_version = backend_algorithm_version(backend)
        if self.backend_algorithm_version is not None and (
            str(self.backend_algorithm_version).strip() != expected_algorithm_version
        ):
            raise IcpJobProtocolError("ICP worker handle backend 알고리즘 버전이 올바르지 않습니다.")
        self.backend_id = backend.value
        self.backend_algorithm_version = expected_algorithm_version

    @property
    def is_running(self) -> bool:
        """worker 프로세스가 아직 종료되지 않았는지 반환합니다."""

        return self.process.poll() is None

    @property
    def return_code(self) -> int | None:
        """worker가 종료했을 때만 종료 코드를 반환합니다."""

        return self.process.poll()


def _normalized_job_directory(job_directory: Path | str) -> Path:
    """존재하는 전용 작업 디렉터리를 절대 경로로 검증합니다."""

    resolved = Path(job_directory).resolve(strict=True)
    if not resolved.is_dir():
        raise IcpJobProtocolError("ICP 작업 경로가 디렉터리가 아닙니다.")
    return resolved


def resolve_job_path(job_directory: Path | str, relative_name: str) -> Path:
    """상대 파일명이 작업 디렉터리 밖으로 벗어나지 않게 해석합니다."""

    root = _normalized_job_directory(job_directory)
    name = str(relative_name).strip()
    if not name or Path(name).is_absolute():
        raise IcpJobProtocolError("ICP 작업 파일 경로는 비어 있지 않은 상대 경로여야 합니다.")
    candidate = (root / name).resolve(strict=False)
    try:
        candidate.relative_to(root)
    except ValueError as error:
        raise IcpJobProtocolError("ICP 작업 파일 경로가 작업 디렉터리를 벗어났습니다.") from error
    return candidate


def create_icp_job_directory() -> IcpJobFiles:
    """운영체제 임시 경로 아래에 전용 ICP 작업 디렉터리를 만듭니다."""

    directory = Path(tempfile.mkdtemp(prefix="colmap_align_icp_"))
    return IcpJobFiles(job_directory=directory)


def cleanup_unstarted_job_directory(files: IcpJobFiles) -> None:
    """worker를 아직 시작하지 못한 작업 폴더만 안전하게 제거합니다."""

    directory = _normalized_job_directory(files.job_directory)
    try:
        shutil.rmtree(directory)
    except OSError as error:
        raise IcpBackendError("시작하지 못한 ICP 작업 임시 디렉터리를 정리하지 못했습니다.") from error


def _write_bytes_atomically(target_path: Path, payload: bytes) -> None:
    """중간 파일을 같은 디렉터리에 쓴 뒤 원자 교체합니다."""

    target = resolve_job_path(target_path.parent, target_path.name)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=str(target.parent),
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        _replace_atomically(temporary, target)
    except BaseException:
        try:
            temporary.unlink(missing_ok=True)
        finally:
            raise


def write_json_atomically(target_path: Path, payload: Mapping[str, Any]) -> None:
    """JSON 객체를 UTF-8 원자 파일로 기록합니다."""

    try:
        encoded = json.dumps(
            dict(payload),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise IcpJobProtocolError("ICP 작업 JSON에 기록할 수 없는 값이 있습니다.") from error
    _write_bytes_atomically(target_path, encoded)


def read_json_file(target_path: Path) -> dict[str, Any]:
    """Windows 공유 충돌을 제한 재시도하고 완성된 JSON 객체만 반환합니다."""

    deadline = monotonic() + _ATOMIC_REPLACE_RETRY_SECONDS
    while True:
        try:
            raw = target_path.read_text(encoding="utf-8")
            break
        except UnicodeDecodeError as error:
            raise IcpJobProtocolError(
                f"ICP 작업 JSON을 읽지 못했습니다: {target_path.name}"
            ) from error
        except OSError as error:
            if monotonic() >= deadline:
                raise IcpJobProtocolError(
                    f"ICP 작업 JSON을 읽지 못했습니다: {target_path.name}"
                ) from error
            sleep(_ATOMIC_REPLACE_RETRY_INTERVAL_SECONDS)
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as error:
        raise IcpJobProtocolError(f"ICP 작업 JSON을 읽지 못했습니다: {target_path.name}") from error
    if not isinstance(value, dict):
        raise IcpJobProtocolError(f"ICP 작업 JSON 최상위 값이 객체가 아닙니다: {target_path.name}")
    _validate_finite_json_numbers(value, field_name=target_path.name)
    return value


def _validate_finite_json_numbers(value: Any, *, field_name: str) -> None:
    """외부 JSON의 모든 부동소수점 값이 유한한지 재귀 검증합니다."""

    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not isfinite(value):
            raise IcpJobProtocolError(f"ICP 작업 JSON에 유한하지 않은 수치가 있습니다: {field_name}")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _validate_finite_json_numbers(item, field_name=f"{field_name}[{index}]")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            _validate_finite_json_numbers(item, field_name=f"{field_name}.{key}")
        return
    raise IcpJobProtocolError(f"ICP 작업 JSON에 지원하지 않는 값이 있습니다: {field_name}")


def _close_memmap(array: np.ndarray) -> None:
    """원자 NPY 기록 뒤 열린 numpy memmap 핸들을 명시적으로 닫습니다."""

    candidate: object | None = array
    while candidate is not None:
        mapping = getattr(candidate, "_mmap", None)
        if mapping is not None:
            mapping.flush()
            mapping.close()
            return
        candidate = getattr(candidate, "base", None)


def close_array_memmap(array: np.ndarray) -> None:
    """입력 검증이 끝난 read-only NPY memmap 핸들을 명시적으로 닫습니다."""

    _close_memmap(array)


def _temporary_npy_path(target_path: Path) -> tuple[Path, Path]:
    """작업 폴더 안에서만 원자 NPY 교체용 임시 경로를 만듭니다."""

    target = resolve_job_path(target_path.parent, target_path.name)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=str(target.parent),
    )
    os.close(descriptor)
    return target, Path(temporary_name)


def _replace_atomically(temporary: Path, target: Path) -> None:
    """Windows의 짧은 읽기 공유 충돌만 제한 시간 동안 재시도해 원자 교체합니다."""

    deadline = monotonic() + _ATOMIC_REPLACE_RETRY_SECONDS
    while True:
        try:
            os.replace(temporary, target)
            return
        except PermissionError:
            if monotonic() >= deadline:
                raise
            sleep(_ATOMIC_REPLACE_RETRY_INTERVAL_SECONDS)


def _chunk_size(value: int) -> int:
    """main process의 고정 메모리 경계를 위한 양의 NPY chunk 크기를 검증합니다."""

    if isinstance(value, bool) or not isinstance(value, (int, np.integer)):
        raise IcpJobProtocolError("ICP NPY chunk 크기는 bool이 아닌 정수여야 합니다.")
    size = int(value)
    if size < 1:
        raise IcpJobProtocolError("ICP NPY chunk 크기는 1 이상이어야 합니다.")
    return size


def write_transformed_positions_atomically(
    target_path: Path,
    positions: object,
    transform: object,
    *,
    chunk_size: int = 65_536,
) -> None:
    """원본을 바꾸지 않고 Project World 좌표를 고정 크기 chunk로 NPY에 기록합니다."""

    source = np.asarray(positions)
    matrix = np.asarray(transform, dtype=np.float64)
    if source.ndim != 2 or source.shape[1] != 3:
        raise IcpJobProtocolError("ICP 원본 점 좌표는 N×3 배열이어야 합니다.")
    if matrix.shape != (4, 4) or not np.isfinite(matrix).all():
        raise IcpJobProtocolError("ICP 점 좌표 변환 행렬이 올바르지 않습니다.")
    size = _chunk_size(chunk_size)
    target, temporary = _temporary_npy_path(target_path)
    output: np.ndarray | None = None
    try:
        output = np.lib.format.open_memmap(
            temporary,
            mode="w+",
            dtype=np.float64,
            shape=source.shape,
            fortran_order=False,
        )
        for start in range(0, source.shape[0], size):
            stop = min(start + size, source.shape[0])
            source_chunk = np.asarray(source[start:stop], dtype=np.float64)
            if not np.isfinite(source_chunk).all():
                raise IcpJobProtocolError("ICP 원본 점 좌표에 유한하지 않은 값이 있습니다.")
            destination_chunk = output[start:stop]
            np.matmul(source_chunk, matrix[:3, :3].T, out=destination_chunk)
            np.add(destination_chunk, matrix[:3, 3], out=destination_chunk)
            if not np.isfinite(destination_chunk).all():
                raise IcpJobProtocolError("ICP Project World 좌표에 유한하지 않은 값이 있습니다.")
        _close_memmap(output)
        output = None
        _replace_atomically(temporary, target)
    except BaseException:
        if output is not None:
            _close_memmap(output)
        try:
            temporary.unlink(missing_ok=True)
        finally:
            raise


def write_point_ids_in_chunks_atomically(
    target_path: Path,
    point_ids: object,
    *,
    chunk_size: int = 65_536,
) -> None:
    """POINT3D_ID를 음수 변환 없이 고정 크기 chunk로 uint64 NPY에 기록합니다."""

    source = np.asarray(point_ids)
    if source.ndim != 1 or source.dtype.kind not in {"u", "i"}:
        raise IcpJobProtocolError("ICP POINT3D_ID는 1차원 정수 배열이어야 합니다.")
    size = _chunk_size(chunk_size)
    target, temporary = _temporary_npy_path(target_path)
    output: np.ndarray | None = None
    try:
        output = np.lib.format.open_memmap(
            temporary,
            mode="w+",
            dtype=np.uint64,
            shape=source.shape,
            fortran_order=False,
        )
        for start in range(0, source.shape[0], size):
            stop = min(start + size, source.shape[0])
            source_chunk = source[start:stop]
            if source.dtype.kind == "i" and np.any(source_chunk < 0):
                raise IcpJobProtocolError("ICP POINT3D_ID에 음수 값이 있습니다.")
            # 전체 고유성은 검증을 마친 COLMAP 저장소 입력 계약으로 보장하고,
            # 이 경계에서는 고정 chunk 안의 명백한 중복을 즉시 거부합니다.
            if np.unique(source_chunk).size != source_chunk.size:
                raise IcpJobProtocolError("ICP POINT3D_ID에 중복 값이 있습니다.")
            output[start:stop] = source_chunk
        _close_memmap(output)
        output = None
        _replace_atomically(temporary, target)
    except BaseException:
        if output is not None:
            _close_memmap(output)
        try:
            temporary.unlink(missing_ok=True)
        finally:
            raise


def write_array_atomically(
    target_path: Path,
    value: object,
    *,
    dtype: np.dtype[Any],
    shape: tuple[int | None, ...],
) -> None:
    """dtype·형상·유한값을 검증한 NPY 배열을 원자적으로 기록합니다."""

    array = np.ascontiguousarray(value, dtype=dtype)
    if array.ndim != len(shape):
        raise IcpJobProtocolError(f"ICP 배열 차원이 올바르지 않습니다: {target_path.name}")
    for actual, expected in zip(array.shape, shape, strict=True):
        if expected is not None and actual != expected:
            raise IcpJobProtocolError(f"ICP 배열 형상이 올바르지 않습니다: {target_path.name}")
    if np.issubdtype(array.dtype, np.floating) and not np.isfinite(array).all():
        raise IcpJobProtocolError(f"ICP 배열에 유한하지 않은 값이 있습니다: {target_path.name}")
    target = resolve_job_path(target_path.parent, target_path.name)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=str(target.parent),
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            np.save(stream, array, allow_pickle=False)
            stream.flush()
            os.fsync(stream.fileno())
        _replace_atomically(temporary, target)
    except BaseException:
        try:
            temporary.unlink(missing_ok=True)
        finally:
            raise


def read_array_file(
    job_directory: Path | str,
    relative_name: str,
    *,
    dtype: np.dtype[Any],
    shape: tuple[int | None, ...],
    mmap_mode: str | None = None,
) -> np.ndarray:
    """pickle 없이 NPY 배열을 읽고 선택적으로 읽기 전용 memmap을 유지합니다."""

    path = resolve_job_path(job_directory, relative_name)
    try:
        value = np.load(path, allow_pickle=False, mmap_mode=mmap_mode)
    except (OSError, ValueError) as error:
        raise IcpJobProtocolError(f"ICP 배열을 읽지 못했습니다: {relative_name}") from error
    array = value if mmap_mode is not None else np.asarray(value)
    if array.dtype != np.dtype(dtype):
        raise IcpJobProtocolError(f"ICP 배열 dtype이 올바르지 않습니다: {relative_name}")
    if array.ndim != len(shape):
        raise IcpJobProtocolError(f"ICP 배열 차원이 올바르지 않습니다: {relative_name}")
    for actual, expected in zip(array.shape, shape, strict=True):
        if expected is not None and actual != expected:
            raise IcpJobProtocolError(f"ICP 배열 형상이 올바르지 않습니다: {relative_name}")
    if np.issubdtype(array.dtype, np.floating) and not np.isfinite(array).all():
        raise IcpJobProtocolError(f"ICP 배열에 유한하지 않은 값이 있습니다: {relative_name}")
    if mmap_mode is not None:
        if not array.flags.c_contiguous or array.flags.writeable:
            raise IcpJobProtocolError(f"ICP memmap 배열 상태가 올바르지 않습니다: {relative_name}")
        return array
    return np.ascontiguousarray(array)


def write_result_arrays_atomically(target_path: Path, arrays: Mapping[str, object]) -> None:
    """ICP 결과의 작은 수치 배열을 pickle 없는 NPZ로 원자 기록합니다."""

    target = resolve_job_path(target_path.parent, target_path.name)
    normalized: dict[str, np.ndarray] = {}
    for key, value in arrays.items():
        normalized_key = str(key).strip()
        if not normalized_key or normalized_key.startswith("_"):
            raise IcpJobProtocolError("ICP 결과 배열 이름이 올바르지 않습니다.")
        array = np.ascontiguousarray(value, dtype=np.float64)
        if not np.isfinite(array).all():
            raise IcpJobProtocolError("ICP 결과 배열에 유한하지 않은 값이 있습니다.")
        normalized[normalized_key] = array
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=str(target.parent),
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            np.savez_compressed(stream, **normalized)
            stream.flush()
            os.fsync(stream.fileno())
        _replace_atomically(temporary, target)
    except BaseException:
        try:
            temporary.unlink(missing_ok=True)
        finally:
            raise


def read_result_arrays(target_path: Path) -> dict[str, np.ndarray]:
    """allow_pickle=False로 worker 결과 NPZ를 읽어 유한 배열만 반환합니다."""

    try:
        with np.load(target_path, allow_pickle=False) as loaded:
            arrays = {name: np.ascontiguousarray(loaded[name]) for name in loaded.files}
    except (BadZipFile, EOFError, OSError, ValueError) as error:
        raise IcpJobProtocolError("ICP 결과 배열을 읽지 못했습니다.") from error
    for name, value in arrays.items():
        if value.dtype != np.dtype(np.float64) or not np.isfinite(value).all():
            raise IcpJobProtocolError(f"ICP 결과 배열이 올바르지 않습니다: {name}")
    return arrays


def build_manifest(
    *,
    job_id: str,
    input_fingerprint: str,
    payload: Mapping[str, Any],
    backend_id: IcpSolverBackend | str | None = None,
) -> dict[str, Any]:
    """고정 작업·backend 식별자와 caller 입력을 포함한 manifest를 만듭니다."""

    if not isinstance(payload, Mapping):
        raise IcpJobProtocolError("ICP manifest payload는 사전이어야 합니다.")
    normalized_job_id = str(job_id).strip()
    fingerprint = str(input_fingerprint).strip()
    if not normalized_job_id or not fingerprint:
        raise IcpJobProtocolError("ICP 작업 식별자 또는 입력 fingerprint가 비어 있습니다.")
    reserved_keys = {"manifest_version", "job_id", "input_fingerprint"}
    payload_mapping = dict(payload)
    overwritten = reserved_keys.intersection(payload_mapping)
    if overwritten:
        joined = ", ".join(sorted(overwritten))
        raise IcpJobProtocolError(f"ICP manifest 예약 키를 덮어쓸 수 없습니다: {joined}")
    payload_backend = payload_mapping.get("backend_id")
    if backend_id is not None and payload_backend is not None:
        try:
            requested_backend = normalize_solver_backend(backend_id)
            payload_backend_value = normalize_solver_backend(payload_backend)
        except ValueError as error:
            raise IcpJobProtocolError("ICP manifest backend ID가 올바르지 않습니다.") from error
        if requested_backend is not payload_backend_value:
            raise IcpJobProtocolError("ICP manifest backend ID가 호출 인자와 다릅니다.")
    candidate_backend = (
        backend_id
        if backend_id is not None
        else payload_backend
        if payload_backend is not None
        else IcpSolverBackend.CUSTOM_HYBRID
    )
    try:
        backend = normalize_solver_backend(candidate_backend)
    except ValueError as error:
        raise IcpJobProtocolError("ICP manifest backend ID가 올바르지 않습니다.") from error
    algorithm_version = str(payload_mapping.get("algorithm_version", "")).strip()
    if algorithm_version != ICP_ALGORITHM_VERSION:
        raise IcpJobProtocolError("ICP manifest 알고리즘 버전이 현재 worker와 다릅니다.")
    expected_backend_algorithm_version = backend_algorithm_version(backend)
    supplied_backend_algorithm_version = payload_mapping.get("backend_algorithm_version")
    if supplied_backend_algorithm_version is not None and (
        str(supplied_backend_algorithm_version).strip() != expected_backend_algorithm_version
    ):
        raise IcpJobProtocolError("ICP manifest backend 알고리즘 버전이 현재 구현과 다릅니다.")
    supplied_protocol_version = payload_mapping.get("job_protocol_version")
    if supplied_protocol_version is not None and (
        str(supplied_protocol_version).strip() != ICP_JOB_PROTOCOL_VERSION
    ):
        raise IcpJobProtocolError("ICP manifest 작업 프로토콜 버전이 현재 구현과 다릅니다.")
    manifest = {
        "manifest_version": _JOB_MANIFEST_VERSION,
        "job_id": normalized_job_id,
        "input_fingerprint": fingerprint,
    }
    manifest.update(payload_mapping)
    manifest["backend_id"] = backend.value
    manifest["backend_algorithm_version"] = expected_backend_algorithm_version
    manifest["job_protocol_version"] = ICP_JOB_PROTOCOL_VERSION
    return manifest


def write_job_manifest(files: IcpJobFiles, manifest: Mapping[str, Any]) -> None:
    """생성한 작업 manifest를 worker 시작 전에 원자 기록합니다."""

    write_json_atomically(files.manifest_path, manifest)


def read_job_manifest(job_directory: Path | str) -> dict[str, Any]:
    """worker가 사용할 manifest의 버전·기본 식별자를 검증합니다."""

    root = _normalized_job_directory(job_directory)
    manifest = read_json_file(resolve_job_path(root, _MANIFEST_FILE_NAME))
    if manifest.get("manifest_version") != _JOB_MANIFEST_VERSION:
        raise IcpJobProtocolError("지원하지 않는 ICP 작업 manifest 버전입니다.")
    if not str(manifest.get("job_id", "")).strip():
        raise IcpJobProtocolError("ICP 작업 manifest의 job_id가 비어 있습니다.")
    if not str(manifest.get("input_fingerprint", "")).strip():
        raise IcpJobProtocolError("ICP 작업 manifest의 input fingerprint가 비어 있습니다.")
    if str(manifest.get("algorithm_version", "")).strip() != ICP_ALGORITHM_VERSION:
        raise IcpJobProtocolError("ICP 작업 manifest의 알고리즘 버전이 현재 worker와 다릅니다.")
    backend = _backend_from_manifest(manifest)
    # 이미 배포된 Custom Hybrid job은 새 식별 필드가 없을 수 있어 메모리에서만 보완합니다.
    manifest.setdefault("backend_id", backend.value)
    manifest.setdefault("backend_algorithm_version", backend_algorithm_version(backend))
    manifest.setdefault("job_protocol_version", ICP_JOB_PROTOCOL_VERSION)
    return manifest


def _backend_from_manifest(manifest: Mapping[str, Any]) -> IcpSolverBackend:
    """manifest의 backend 식별자가 기존·신규 작업 계약에 맞는지 검증합니다."""

    backend_keys = {
        "backend_id",
        "backend_algorithm_version",
        "job_protocol_version",
    }
    has_backend_identity = any(key in manifest for key in backend_keys)
    backend_value = manifest.get("backend_id", IcpSolverBackend.CUSTOM_HYBRID.value)
    backend = _normalize_backend_or_protocol_error(backend_value, "ICP manifest backend ID")
    _validate_backend_identity(
        manifest,
        backend,
        payload_name="ICP 작업 manifest",
        require_backend_identity=is_open3d_backend(backend) or has_backend_identity,
    )
    return backend


def _normalize_backend_or_protocol_error(
    value: IcpSolverBackend | str,
    field_name: str,
) -> IcpSolverBackend:
    """외부 backend 식별자를 protocol 오류 문맥의 enum으로 변환합니다."""

    try:
        return normalize_solver_backend(value)
    except ValueError as error:
        raise IcpJobProtocolError(f"{field_name}가 올바르지 않습니다.") from error


def _validate_backend_identity(
    payload: Mapping[str, Any],
    backend: IcpSolverBackend,
    *,
    payload_name: str,
    require_backend_identity: bool,
) -> None:
    """backend별 worker payload의 버전·식별자 일치 여부를 검증합니다."""

    if str(payload.get("algorithm_version", "")).strip() != ICP_ALGORITHM_VERSION:
        raise IcpJobProtocolError(f"{payload_name} 알고리즘 버전이 현재 구현과 다릅니다.")
    identity_keys = (
        "backend_id",
        "backend_algorithm_version",
        "job_protocol_version",
    )
    has_identity = any(key in payload for key in identity_keys)
    if not require_backend_identity and not has_identity:
        return
    missing = [key for key in identity_keys if key not in payload]
    if missing:
        joined = ", ".join(missing)
        raise IcpJobProtocolError(f"{payload_name} backend 식별자에 필수 키가 없습니다: {joined}")
    actual_backend = _normalize_backend_or_protocol_error(
        payload["backend_id"],
        f"{payload_name} backend ID",
    )
    if actual_backend is not backend:
        raise IcpJobProtocolError(f"{payload_name} backend ID가 현재 작업과 다릅니다.")
    if str(payload["backend_algorithm_version"]).strip() != backend_algorithm_version(backend):
        raise IcpJobProtocolError(f"{payload_name} backend 알고리즘 버전이 현재 구현과 다릅니다.")
    if str(payload["job_protocol_version"]).strip() != ICP_JOB_PROTOCOL_VERSION:
        raise IcpJobProtocolError(f"{payload_name} 작업 프로토콜 버전이 현재 구현과 다릅니다.")


def _worker_script_path() -> Path:
    """Full 빌드에 포함된 Hybrid worker 진입 스크립트를 반환합니다."""

    script = (
        Path(__file__).resolve(strict=False).parent.parent
        / "optional_backends"
        / "hybrid"
        / "worker.py"
    )
    if not script.is_file():
        raise IcpBackendError("현재 배포판에는 Hybrid ICP backend가 포함되지 않았습니다.")
    return script


def _external_worker_script_path(backend: IcpSolverBackend) -> Path:
    """선택한 외부 backend의 독립 worker 진입 스크립트를 반환합니다."""

    if backend is IcpSolverBackend.SMALL_GICP:
        script = Path(__file__).with_name("icp_small_gicp_worker.py").resolve(strict=False)
        missing_message = "small_gicp ICP worker 스크립트를 찾을 수 없습니다."
    else:
        script = (
            Path(__file__).resolve(strict=False).parent.parent
            / "optional_backends"
            / "open3d"
            / "worker.py"
        )
        missing_message = "현재 배포판에는 Open3D ICP backend가 포함되지 않았습니다."
    if not script.is_file():
        raise IcpBackendError(missing_message)
    return script


def _validated_blender_binary(binary_path: str | os.PathLike[str]) -> Path:
    """외부 worker에 넘길 현재 Blender 실행 파일 경로를 검증합니다."""

    binary = Path(binary_path).expanduser().resolve(strict=False)
    if not binary.is_file():
        raise IcpBackendError("ICP worker를 실행할 Blender 실행 파일을 찾을 수 없습니다.")
    return binary


def _validated_external_python_binary(python_path: str | os.PathLike[str]) -> Path:
    """외부 ICP 전용 CPython 실행 파일 경로를 검증합니다."""

    binary = Path(python_path).expanduser().resolve(strict=False)
    if not binary.is_file():
        raise IcpBackendError("외부 ICP worker를 실행할 CPython 실행 파일을 찾을 수 없습니다.")
    return binary


def validate_open3d_environment(
    python_path: str | os.PathLike[str],
) -> dict[str, str]:
    """외부 CPython 프로세스로만 Open3D 0.19.0 실행 환경을 검사합니다."""

    binary = _validated_external_python_binary(python_path)
    environment = os.environ.copy()
    environment["PYTHONNOUSERSITE"] = "1"
    command = [str(binary), "-I", "-c", _OPEN3D_ENVIRONMENT_PROBE]
    try:
        completed = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            env=environment,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_OPEN3D_ENVIRONMENT_TIMEOUT_SECONDS,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired as error:
        raise IcpBackendError("Open3D 환경 검사 시간이 초과되었습니다.") from error
    except OSError as error:
        raise IcpBackendError("Open3D 환경 검사 프로세스를 시작하지 못했습니다.") from error
    if completed.returncode != 0:
        diagnostic = _environment_probe_diagnostic(completed)
        raise IcpBackendError(f"Open3D 환경 검사가 실패했습니다: {diagnostic}")
    try:
        payload = json.loads(completed.stdout)
    except (TypeError, json.JSONDecodeError) as error:
        raise IcpBackendError("Open3D 환경 검사 결과 형식이 올바르지 않습니다.") from error
    if not isinstance(payload, dict):
        raise IcpBackendError("Open3D 환경 검사 결과가 객체가 아닙니다.")
    return _validated_open3d_environment_payload(payload)


def _environment_probe_diagnostic(completed: subprocess.CompletedProcess[str]) -> str:
    """환경 검사 실패의 마지막 안전 진단 문구만 반환합니다."""

    stderr = str(getattr(completed, "stderr", "") or "").strip()
    stdout = str(getattr(completed, "stdout", "") or "").strip()
    raw = stderr or stdout or "원인을 기록하지 않았습니다."
    line = raw.splitlines()[-1].strip()
    return line[:400] or "원인을 기록하지 않았습니다."


def _validated_open3d_environment_payload(payload: Mapping[str, Any]) -> dict[str, str]:
    """외부 검사 프로세스가 반환한 버전·아키텍처 정보를 엄격히 검증합니다."""

    required_keys = (
        "python_implementation",
        "worker_python_version",
        "worker_numpy_version",
        "open3d_version",
        "platform_architecture",
    )
    missing = [key for key in required_keys if key not in payload]
    if missing:
        joined = ", ".join(missing)
        raise IcpBackendError(f"Open3D 환경 검사 결과에 필수 키가 없습니다: {joined}")
    normalized = {
        key: str(payload[key]).strip()
        for key in required_keys
    }
    if any(not value for value in normalized.values()):
        raise IcpBackendError("Open3D 환경 검사 결과에 빈 값이 있습니다.")
    if normalized["python_implementation"] != "CPython":
        raise IcpBackendError("Open3D worker는 CPython이어야 합니다.")
    _validate_external_python_version(normalized["worker_python_version"])
    if normalized["open3d_version"] != ICP_OPEN3D_REQUIRED_VERSION:
        raise IcpBackendError(
            f"Open3D 버전은 {ICP_OPEN3D_REQUIRED_VERSION}이어야 합니다."
        )
    if not _is_x64_architecture(normalized["platform_architecture"]):
        raise IcpBackendError("Open3D worker는 64비트 Python이어야 합니다.")
    return normalized


def validate_small_gicp_environment(
    python_path: str | os.PathLike[str],
) -> dict[str, str]:
    """외부 CPython 프로세스로만 small_gicp 1.0.1 실행 환경을 검사합니다."""

    binary = _validated_external_python_binary(python_path)
    environment = os.environ.copy()
    environment["PYTHONNOUSERSITE"] = "1"
    command = [str(binary), "-I", "-c", _SMALL_GICP_ENVIRONMENT_PROBE]
    try:
        completed = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            env=environment,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_OPEN3D_ENVIRONMENT_TIMEOUT_SECONDS,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired as error:
        raise IcpBackendError("small_gicp 환경 검사 시간이 초과되었습니다.") from error
    except OSError as error:
        raise IcpBackendError("small_gicp 환경 검사 프로세스를 시작하지 못했습니다.") from error
    if completed.returncode != 0:
        diagnostic = _environment_probe_diagnostic(completed)
        raise IcpBackendError(f"small_gicp 환경 검사가 실패했습니다: {diagnostic}")
    try:
        payload = json.loads(completed.stdout)
    except (TypeError, json.JSONDecodeError) as error:
        raise IcpBackendError("small_gicp 환경 검사 결과 형식이 올바르지 않습니다.") from error
    if not isinstance(payload, dict):
        raise IcpBackendError("small_gicp 환경 검사 결과가 객체가 아닙니다.")
    return _validated_small_gicp_environment_payload(payload)


def _validated_small_gicp_environment_payload(payload: Mapping[str, Any]) -> dict[str, str]:
    """small_gicp 검사 프로세스가 반환한 버전·아키텍처 정보를 엄격히 검증합니다."""

    required_keys = (
        "python_implementation",
        "worker_python_version",
        "worker_numpy_version",
        "small_gicp_version",
        "platform_architecture",
    )
    missing = [key for key in required_keys if key not in payload]
    if missing:
        raise IcpBackendError(
            "small_gicp 환경 검사 결과에 필수 키가 없습니다: " + ", ".join(missing)
        )
    normalized = {key: str(payload[key]).strip() for key in required_keys}
    if any(not value for value in normalized.values()):
        raise IcpBackendError("small_gicp 환경 검사 결과에 빈 값이 있습니다.")
    if normalized["python_implementation"] != "CPython":
        raise IcpBackendError("small_gicp worker는 CPython이어야 합니다.")
    _validate_external_python_version(normalized["worker_python_version"])
    if normalized["small_gicp_version"] != ICP_SMALL_GICP_REQUIRED_VERSION:
        raise IcpBackendError(
            f"small_gicp 버전은 {ICP_SMALL_GICP_REQUIRED_VERSION}이어야 합니다."
        )
    if not _is_x64_architecture(normalized["platform_architecture"]):
        raise IcpBackendError("small_gicp worker는 64비트 Python이어야 합니다.")
    return normalized


def _validate_external_python_version(version: str) -> None:
    """외부 ICP worker의 Python 버전이 요구한 3.12.x인지 검증합니다."""

    parts = version.split(".")
    if len(parts) < 3:
        raise IcpBackendError("외부 ICP worker Python 버전 형식이 올바르지 않습니다.")
    try:
        major, minor, patch = (int(parts[index]) for index in range(3))
    except ValueError as error:
        raise IcpBackendError("외부 ICP worker Python 버전 형식이 올바르지 않습니다.") from error
    if patch < 0 or (major, minor) != (
        ICP_OPEN3D_REQUIRED_PYTHON_MAJOR,
        ICP_OPEN3D_REQUIRED_PYTHON_MINOR,
    ):
        raise IcpBackendError("외부 ICP worker는 CPython 3.12.x여야 합니다.")


def _is_x64_architecture(value: str) -> bool:
    """운영체제마다 다른 x64 아키텍처 표기를 허용 목록으로 판정합니다."""

    normalized = value.strip().lower().replace("-", "").replace("_", "")
    return normalized in {"64bit", "x64", "amd64"}


def launch_icp_worker(
    files: IcpJobFiles,
    *,
    blender_binary_path: str | os.PathLike[str],
    input_fingerprint: str,
) -> IcpJobHandle:
    """shell 없이 현재 Blender 실행 파일로 숨은 ICP worker를 시작합니다."""

    root = _normalized_job_directory(files.job_directory)
    if not files.manifest_path.is_file():
        raise IcpBackendError("ICP worker 시작 전에 manifest가 작성되지 않았습니다.")
    manifest = read_job_manifest(root)
    backend = _backend_from_manifest(manifest)
    if backend is not IcpSolverBackend.CUSTOM_HYBRID:
        raise IcpJobProtocolError("Custom Hybrid launcher에는 Custom Hybrid manifest만 사용할 수 있습니다.")
    if str(manifest["job_id"]) != root.name:
        raise IcpJobProtocolError("ICP worker manifest job_id가 작업 디렉터리 이름과 다릅니다.")
    manifest_fingerprint = str(manifest["input_fingerprint"])
    expected_fingerprint = str(input_fingerprint).strip()
    if manifest_fingerprint != expected_fingerprint:
        raise IcpJobProtocolError("ICP worker 입력 fingerprint가 manifest와 다릅니다.")
    binary = _validated_blender_binary(blender_binary_path)
    worker_script = _worker_script_path()
    log_stream = files.worker_log_path.open("w", encoding="utf-8", newline="\n")
    command = [
        str(binary),
        "--background",
        "--factory-startup",
        "--python",
        str(worker_script),
        "--",
        "--job-dir",
        str(root),
    ]
    environment = os.environ.copy()
    environment["PYTHONNOUSERSITE"] = "1"
    try:
        process = subprocess.Popen(
            command,
            cwd=str(root),
            stdin=subprocess.DEVNULL,
            stdout=log_stream,
            stderr=subprocess.STDOUT,
            shell=False,
            env=environment,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except OSError as error:
        log_stream.close()
        raise IcpBackendError("ICP worker 프로세스를 시작하지 못했습니다.") from error
    handle = IcpJobHandle(
        job_id=str(manifest["job_id"]),
        files=files,
        process=process,
        input_fingerprint=manifest_fingerprint,
        backend_id=backend.value,
        backend_algorithm_version=backend_algorithm_version(backend),
        _worker_log_handle=log_stream,
    )
    _LOGGER.info(
        "event=icp_worker_started job_id=%s process_id=%s input_fingerprint=%s",
        handle.job_id,
        process.pid,
        handle.input_fingerprint,
    )
    return handle


def launch_open3d_worker(
    files: IcpJobFiles,
    python_binary_path: str | os.PathLike[str],
    input_fingerprint: str,
) -> IcpJobHandle:
    """검증된 외부 CPython으로 Open3D 또는 small_gicp worker를 시작합니다."""

    root = _normalized_job_directory(files.job_directory)
    if not files.manifest_path.is_file():
        raise IcpBackendError("외부 ICP worker 시작 전에 manifest가 작성되지 않았습니다.")
    manifest = read_job_manifest(root)
    backend = _backend_from_manifest(manifest)
    if not is_external_python_backend(backend):
        raise IcpJobProtocolError("외부 ICP launcher에는 외부 CPython backend manifest만 사용할 수 있습니다.")
    if str(manifest["job_id"]) != root.name:
        raise IcpJobProtocolError("외부 ICP worker manifest job_id가 작업 디렉터리 이름과 다릅니다.")
    manifest_fingerprint = str(manifest["input_fingerprint"])
    expected_fingerprint = str(input_fingerprint).strip()
    if manifest_fingerprint != expected_fingerprint:
        raise IcpJobProtocolError("외부 ICP worker 입력 fingerprint가 manifest와 다릅니다.")
    binary = _validated_external_python_binary(python_binary_path)
    if backend is IcpSolverBackend.SMALL_GICP:
        validate_small_gicp_environment(binary)
    else:
        validate_open3d_environment(binary)
    worker_script = _external_worker_script_path(backend)
    log_stream = files.worker_log_path.open("w", encoding="utf-8", newline="\n")
    command = [
        str(binary),
        "-I",
        str(worker_script),
        "--job-dir",
        str(root),
    ]
    environment = os.environ.copy()
    environment["PYTHONNOUSERSITE"] = "1"
    try:
        process = subprocess.Popen(
            command,
            cwd=str(root),
            stdin=subprocess.DEVNULL,
            stdout=log_stream,
            stderr=subprocess.STDOUT,
            shell=False,
            env=environment,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except OSError as error:
        log_stream.close()
        raise IcpBackendError("외부 ICP worker 프로세스를 시작하지 못했습니다.") from error
    handle = IcpJobHandle(
        job_id=str(manifest["job_id"]),
        files=files,
        process=process,
        input_fingerprint=manifest_fingerprint,
        backend_id=backend.value,
        backend_algorithm_version=backend_algorithm_version(backend),
        _worker_log_handle=log_stream,
    )
    _LOGGER.info(
        "event=external_icp_worker_started job_id=%s process_id=%s backend_id=%s input_fingerprint=%s",
        handle.job_id,
        process.pid,
        handle.backend_id,
        handle.input_fingerprint,
    )
    return handle


def read_job_progress(handle: IcpJobHandle) -> dict[str, Any] | None:
    """존재하는 원자 진행률 파일만 읽고 아직 없으면 None을 반환합니다."""

    path = handle.files.progress_path
    if not path.is_file():
        return None
    payload = read_json_file(path)
    if handle.backend_id == IcpSolverBackend.CCCORELIB.value:
        from .icp_cccore_runtime import validate_cccore_payload
        validate_cccore_payload(payload, handle)
        return payload
    _validate_worker_payload_identity(payload, handle, "진행률")
    return payload


def read_job_error(handle: IcpJobHandle) -> dict[str, Any] | None:
    """구조화 오류를 읽고 CC 오류의 작업·입력 식별자를 검증합니다."""

    path = handle.files.error_path
    if not path.is_file():
        return None
    payload = read_json_file(path)
    if handle.backend_id == IcpSolverBackend.CCCORELIB.value:
        from .icp_cccore_runtime import validate_cccore_payload
        validate_cccore_payload(payload, handle)
    return payload


def read_job_result(handle: IcpJobHandle) -> tuple[dict[str, Any], dict[str, np.ndarray]] | None:
    """메타데이터와 배열 결과가 모두 기록된 경우에만 함께 읽습니다."""

    if handle.backend_id == IcpSolverBackend.CCCORELIB.value:
        from .icp_cccore_runtime import read_cccore_result
        return read_cccore_result(handle)
    if not handle.files.result_path.is_file() or not handle.files.result_array_path.is_file():
        return None
    metadata = read_json_file(handle.files.result_path)
    _validate_worker_payload_identity(metadata, handle, "결과")
    validate_result_metadata_for_backend(metadata, handle.backend_id)
    arrays = read_result_arrays(handle.files.result_array_path)
    if is_external_python_backend(handle.backend_id):
        _validate_external_result_arrays(arrays)
    return metadata, arrays


def _validate_worker_payload_identity(
    payload: Mapping[str, Any],
    handle: IcpJobHandle,
    payload_name: str,
) -> None:
    """worker progress·result JSON의 job·입력·backend 식별자를 검증합니다."""

    if str(payload.get("job_id", "")).strip() != handle.job_id:
        raise IcpJobProtocolError(f"ICP worker {payload_name} job_id가 현재 작업과 다릅니다.")
    if str(payload.get("input_fingerprint", "")).strip() != handle.input_fingerprint:
        raise IcpJobProtocolError(f"ICP worker {payload_name} 입력 fingerprint가 현재 작업과 다릅니다.")
    backend = _normalize_backend_or_protocol_error(handle.backend_id, "ICP worker handle backend ID")
    _validate_backend_identity(
        payload,
        backend,
        payload_name=f"ICP worker {payload_name}",
        # 기존 Custom worker 결과는 새 식별 필드가 없으므로 이 경로만 호환 유지합니다.
        require_backend_identity=is_external_python_backend(backend),
    )


def _require_mapping_keys(
    value: object,
    *,
    field_name: str,
    required_keys: frozenset[str],
) -> Mapping[str, Any]:
    """Hybrid 결과의 중첩 JSON 객체와 필수 키 집합을 검증합니다."""

    if not isinstance(value, Mapping):
        raise IcpJobProtocolError(f"ICP 결과 {field_name}은(는) 객체여야 합니다.")
    missing = required_keys.difference(value)
    if missing:
        joined = ", ".join(sorted(missing))
        raise IcpJobProtocolError(f"ICP 결과 {field_name}에 필수 키가 없습니다: {joined}")
    return value


def validate_hybrid_result_metadata(metadata: Mapping[str, Any]) -> None:
    """v2 결과가 UI에 필요한 Hybrid 진단값을 빠짐없이 담았는지 검증합니다."""

    missing = _REQUIRED_HYBRID_RESULT_KEYS.difference(metadata)
    if missing:
        joined = ", ".join(sorted(missing))
        raise IcpJobProtocolError(f"ICP Hybrid 결과에 필수 키가 없습니다: {joined}")
    quality = _require_mapping_keys(
        metadata["quality"],
        field_name="quality",
        required_keys=frozenset({"status"}),
    )
    status = str(quality["status"]).strip().upper()
    if status not in {"PASS", "REVIEW"}:
        raise IcpJobProtocolError("ICP Hybrid 결과 품질은 PASS 또는 REVIEW여야 합니다.")
    _require_mapping_keys(
        metadata["best_candidate"],
        field_name="best_candidate",
        required_keys=_REQUIRED_HYBRID_CANDIDATE_KEYS,
    )
    _require_mapping_keys(
        metadata["baseline_metrics"],
        field_name="baseline_metrics",
        required_keys=_REQUIRED_HYBRID_EVALUATION_KEYS,
    )
    _require_mapping_keys(
        metadata["best_candidate_metrics"],
        field_name="best_candidate_metrics",
        required_keys=_REQUIRED_HYBRID_EVALUATION_KEYS,
    )
    _require_mapping_keys(
        metadata["density_sampling_metrics"],
        field_name="density_sampling_metrics",
        required_keys=_REQUIRED_HYBRID_DENSITY_KEYS,
    )


def validate_result_metadata_for_backend(
    metadata: Mapping[str, Any],
    backend_id: IcpSolverBackend | str,
) -> None:
    """선택 backend에 맞는 결과 스키마와 식별자만 엄격하게 검증합니다."""

    if not isinstance(metadata, Mapping):
        raise IcpJobProtocolError("ICP 결과 메타데이터는 객체여야 합니다.")
    backend = _normalize_backend_or_protocol_error(backend_id, "ICP 결과 backend ID")
    if backend is IcpSolverBackend.CUSTOM_HYBRID:
        _validate_backend_identity(
            metadata,
            backend,
            payload_name="ICP Hybrid 결과",
            # 배포된 Custom worker의 기존 결과 필드를 그대로 허용합니다.
            require_backend_identity=False,
        )
        validate_hybrid_result_metadata(metadata)
        return
    if backend is IcpSolverBackend.SMALL_GICP:
        validate_small_gicp_result_metadata(metadata)
        return
    validate_open3d_result_metadata(metadata, backend)


def validate_open3d_result_metadata(
    metadata: Mapping[str, Any],
    backend_id: IcpSolverBackend | str,
) -> None:
    """전체점 Open3D worker 결과의 backend·환경·감사 정보를 검증합니다."""

    if not isinstance(metadata, Mapping):
        raise IcpJobProtocolError("Open3D ICP 결과 메타데이터는 객체여야 합니다.")
    backend = _normalize_backend_or_protocol_error(backend_id, "Open3D ICP 결과 backend ID")
    if not is_open3d_backend(backend):
        raise IcpJobProtocolError("Open3D 결과 검증에는 Open3D backend가 필요합니다.")
    _validate_backend_identity(
        metadata,
        backend,
        payload_name="Open3D ICP 결과",
        require_backend_identity=True,
    )
    missing = _REQUIRED_OPEN3D_RESULT_KEYS.difference(metadata)
    if missing:
        joined = ", ".join(sorted(missing))
        raise IcpJobProtocolError(f"Open3D ICP 결과에 필수 키가 없습니다: {joined}")
    for key in (
        "job_id",
        "input_fingerprint",
        "moving_model_id",
        "parameter_fingerprint",
        "marker_signature",
        "solver_stop_reason",
        "worker_numpy_version",
    ):
        _require_nonempty_result_string(metadata[key], key)
    _validate_open3d_result_environment(metadata)
    _validate_external_result_model_identity(metadata)
    _validate_external_result_quality(metadata)
    _validate_external_result_safety_metrics(metadata)
    _validate_external_full_input_audit(metadata["full_input_audit"])


def validate_small_gicp_result_metadata(metadata: Mapping[str, Any]) -> None:
    """전체점 small_gicp 결과의 backend·분포 평가·감사 정보를 검증합니다."""

    if not isinstance(metadata, Mapping):
        raise IcpJobProtocolError("small_gicp 결과 메타데이터는 객체여야 합니다.")
    backend = IcpSolverBackend.SMALL_GICP
    _validate_backend_identity(
        metadata,
        backend,
        payload_name="small_gicp 결과",
        require_backend_identity=True,
    )
    missing = _REQUIRED_SMALL_GICP_RESULT_KEYS.difference(metadata)
    if missing:
        joined = ", ".join(sorted(missing))
        raise IcpJobProtocolError(f"small_gicp 결과에 필수 키가 없습니다: {joined}")
    for key in (
        "job_id",
        "input_fingerprint",
        "moving_model_id",
        "parameter_fingerprint",
        "marker_signature",
        "solver_stop_reason",
        "worker_numpy_version",
    ):
        _require_nonempty_result_string(metadata[key], key)
    _validate_small_gicp_result_environment(metadata)
    _validate_external_result_model_identity(metadata)
    _validate_external_result_quality(metadata)
    _validate_small_gicp_scale_result(metadata)
    _validate_small_gicp_distribution_result(metadata)
    _validate_external_result_safety_metrics(metadata)
    _validate_external_full_input_audit(metadata["full_input_audit"])


def _validate_small_gicp_distribution_result(metadata: Mapping[str, Any]) -> None:
    """Small GICP raw·가중 RMS와 원본 신뢰도 전처리 계약을 검증합니다."""

    for metrics_name in ("baseline_metrics", "best_candidate_metrics"):
        metrics = _require_mapping_keys(
            metadata[metrics_name],
            field_name=metrics_name,
            required_keys=_REQUIRED_SMALL_GICP_DISTRIBUTION_EVALUATION_KEYS,
        )
        if metrics["distance_unit"] != "PROJECT_WORLD_UNIT":
            raise IcpJobProtocolError("small_gicp RMS 거리 단위가 Project World 계약과 다릅니다.")
        correspondence_count = metrics["evaluation_correspondence_count"]
        if isinstance(correspondence_count, bool) or not isinstance(
            correspondence_count,
            (int, np.integer),
        ) or int(correspondence_count) < 0:
            raise IcpJobProtocolError("small_gicp RMS 대응점 수가 올바르지 않습니다.")
        for count_name in (
            "low_weight_correspondence_count",
            "isolation_candidate_correspondence_count",
            "source_isolation_candidate_count",
            "target_isolation_candidate_count",
        ):
            value = metrics[count_name]
            if isinstance(value, bool) or not isinstance(value, (int, np.integer)) or int(value) < 0:
                raise IcpJobProtocolError(f"small_gicp {count_name} 값이 올바르지 않습니다.")
        for metric_name in (
            "point_to_point_rmse",
            "weighted_point_to_point_rmse",
            "residual_median",
            "residual_p90",
        ):
            value = metrics[metric_name]
            if value is None and int(correspondence_count) == 0:
                continue
            _require_finite_result_number(
                value,
                f"{metrics_name}.{metric_name}",
                minimum=0.0,
            )
    preprocessing = metadata["confidence_preprocessing"]
    if not isinstance(preprocessing, Mapping) or preprocessing.get("enabled") is not True:
        raise IcpJobProtocolError("small_gicp 점 분포 신뢰도 전처리 감사 정보가 올바르지 않습니다.")
    if preprocessing.get("k") != 24 or not isinstance(preprocessing.get("models"), list):
        raise IcpJobProtocolError("small_gicp 점 분포 신뢰도 k 또는 모델 감사 정보가 다릅니다.")


def _require_nonempty_result_string(value: object, field_name: str) -> str:
    """외부 결과의 필수 문자열이 비어 있지 않은지 검증합니다."""

    if not isinstance(value, str) or not value.strip():
        raise IcpJobProtocolError(f"외부 ICP 결과 {field_name}이(가) 비어 있거나 문자열이 아닙니다.")
    return value.strip()


def _validate_open3d_result_environment(metadata: Mapping[str, Any]) -> None:
    """결과가 실행 당시의 고정 Open3D worker 환경을 기록했는지 검증합니다."""

    python_version = _require_nonempty_result_string(
        metadata["worker_python_version"],
        "worker_python_version",
    )
    try:
        _validate_external_python_version(python_version)
    except IcpBackendError as error:
        raise IcpJobProtocolError(str(error)) from error
    if _require_nonempty_result_string(metadata["open3d_version"], "open3d_version") != ICP_OPEN3D_REQUIRED_VERSION:
        raise IcpJobProtocolError(
            f"Open3D ICP 결과 버전은 {ICP_OPEN3D_REQUIRED_VERSION}이어야 합니다."
        )
    architecture = _require_nonempty_result_string(
        metadata["platform_architecture"],
        "platform_architecture",
    )
    if not _is_x64_architecture(architecture):
        raise IcpJobProtocolError("Open3D ICP 결과 worker 아키텍처가 x64가 아닙니다.")


def _validate_small_gicp_result_environment(metadata: Mapping[str, Any]) -> None:
    """결과가 실행 당시의 고정 small_gicp worker 환경을 기록했는지 검증합니다."""

    python_version = _require_nonempty_result_string(
        metadata["worker_python_version"],
        "worker_python_version",
    )
    try:
        _validate_external_python_version(python_version)
    except IcpBackendError as error:
        raise IcpJobProtocolError(str(error)) from error
    installed_version = _require_nonempty_result_string(
        metadata["small_gicp_version"],
        "small_gicp_version",
    )
    if installed_version != ICP_SMALL_GICP_REQUIRED_VERSION:
        raise IcpJobProtocolError(
            f"small_gicp 결과 버전은 {ICP_SMALL_GICP_REQUIRED_VERSION}이어야 합니다."
        )
    architecture = _require_nonempty_result_string(
        metadata["platform_architecture"],
        "platform_architecture",
    )
    if not _is_x64_architecture(architecture):
        raise IcpJobProtocolError("small_gicp 결과 worker 아키텍처가 x64가 아닙니다.")


def _validate_external_result_model_identity(metadata: Mapping[str, Any]) -> None:
    """외부 ICP 결과의 모델 목록·snapshot 목록이 구조화된 JSON인지 검증합니다."""

    reference_model_ids = metadata["reference_model_ids"]
    if not isinstance(reference_model_ids, list) or not reference_model_ids:
        raise IcpJobProtocolError("외부 ICP 결과 기준 모델 ID 목록이 올바르지 않습니다.")
    if any(not isinstance(model_id, str) or not model_id.strip() for model_id in reference_model_ids):
        raise IcpJobProtocolError("외부 ICP 결과 기준 모델 ID가 올바르지 않습니다.")
    for key in ("model_revision_snapshot", "model_state_snapshot"):
        if not isinstance(metadata[key], list):
            raise IcpJobProtocolError(f"외부 ICP 결과 {key}이(가) 목록이 아닙니다.")
    level_results = metadata["level_results"]
    if not isinstance(level_results, list) or not level_results:
        raise IcpJobProtocolError("외부 ICP 결과 level_results가 비어 있거나 목록이 아닙니다.")
    if any(not isinstance(result, Mapping) for result in level_results):
        raise IcpJobProtocolError("외부 ICP 결과 level_results 항목이 객체가 아닙니다.")


def _validate_external_result_quality(metadata: Mapping[str, Any]) -> None:
    """외부 ICP 결과의 품질 상태와 사용자 검토 플래그 일치 여부를 검증합니다."""

    quality = _require_mapping_keys(
        metadata["quality"],
        field_name="quality",
        required_keys=frozenset({"status"}),
    )
    status = str(quality["status"]).strip().upper()
    if status not in {"PASS", "REVIEW"}:
        raise IcpJobProtocolError("외부 ICP 결과 품질은 PASS 또는 REVIEW여야 합니다.")
    review_required = metadata["user_review_required"]
    if not isinstance(review_required, bool):
        raise IcpJobProtocolError("외부 ICP 결과 user_review_required는 bool이어야 합니다.")
    if review_required is not (status == "REVIEW"):
        raise IcpJobProtocolError("외부 ICP 품질 상태와 사용자 검토 플래그가 다릅니다.")


def _validate_small_gicp_scale_result(metadata: Mapping[str, Any]) -> None:
    """Small GICP 결과의 scale mode·상태·품질 진단이 서로 일치하는지 검증합니다."""

    scale_mode = str(metadata["scale_mode"]).strip().upper()
    scale_status = str(metadata["scale_refine_status"]).strip().upper()
    if scale_mode == "LOCKED":
        allowed_statuses = {"NOT_REQUESTED"}
    elif scale_mode == "REFINE":
        allowed_statuses = {"SKIPPED", "REJECTED", "ACCEPTED"}
    else:
        raise IcpJobProtocolError("small_gicp 결과 scale mode가 올바르지 않습니다.")
    if scale_status not in allowed_statuses:
        raise IcpJobProtocolError("small_gicp 결과 Scale Refine 상태가 mode와 일치하지 않습니다.")
    quality = metadata["quality"]
    if not isinstance(quality, Mapping):
        raise IcpJobProtocolError("small_gicp 결과 품질 형식이 올바르지 않습니다.")
    if str(quality.get("scale_refine_status", "")).strip().upper() != scale_status:
        raise IcpJobProtocolError("small_gicp 결과와 품질의 Scale Refine 상태가 다릅니다.")
    if not isinstance(quality.get("scale_refine_reason"), str):
        raise IcpJobProtocolError("small_gicp Scale Refine 사유는 문자열이어야 합니다.")


def _validate_external_result_safety_metrics(metadata: Mapping[str, Any]) -> None:
    """runtime이 delta 안전 한도를 재검증할 수 있는 기초 수치를 검증합니다."""

    try:
        local_origin = np.asarray(metadata["local_origin"], dtype=np.float64)
    except (TypeError, ValueError, OverflowError) as error:
        raise IcpJobProtocolError("외부 ICP 결과 local_origin이 수치 배열이 아닙니다.") from error
    if local_origin.shape != (3,) or not np.isfinite(local_origin).all():
        raise IcpJobProtocolError("외부 ICP 결과 local_origin이 올바르지 않습니다.")
    _require_finite_result_number(metadata["moving_diagonal"], "moving_diagonal", minimum=0.0, exclusive=True)
    _require_finite_result_number(metadata["delta_translation"], "delta_translation", minimum=0.0)
    _require_finite_result_number(
        metadata["delta_rotation_degrees"],
        "delta_rotation_degrees",
        minimum=0.0,
        maximum=180.0,
    )


def _require_finite_result_number(
    value: object,
    field_name: str,
    *,
    minimum: float,
    maximum: float | None = None,
    exclusive: bool = False,
) -> float:
    """외부 결과의 안전 수치가 유한 범위 안인지 검증합니다."""

    if isinstance(value, bool):
        raise IcpJobProtocolError(f"외부 ICP 결과 {field_name}은(는) 숫자여야 합니다.")
    try:
        numeric = float(value)
    except (TypeError, ValueError, OverflowError) as error:
        raise IcpJobProtocolError(f"외부 ICP 결과 {field_name}은(는) 숫자여야 합니다.") from error
    if not isfinite(numeric):
        raise IcpJobProtocolError(f"외부 ICP 결과 {field_name}에 유한하지 않은 값이 있습니다.")
    below_minimum = numeric <= minimum if exclusive else numeric < minimum
    if below_minimum:
        comparator = "초과" if exclusive else "이상"
        raise IcpJobProtocolError(f"외부 ICP 결과 {field_name}은(는) {minimum} {comparator}이어야 합니다.")
    if maximum is not None and numeric > maximum:
        raise IcpJobProtocolError(f"외부 ICP 결과 {field_name}은(는) {maximum} 이하여야 합니다.")
    return numeric


def _validate_external_full_input_audit(value: object) -> None:
    """전체 overlap 점군이 모든 외부 ICP 단계에 그대로 쓰였는지 검증합니다."""

    audit = _require_mapping_keys(
        value,
        field_name="full_input_audit",
        required_keys=_REQUIRED_OPEN3D_FULL_INPUT_AUDIT_KEYS,
    )
    if audit["input_policy"] != _OPEN3D_FULL_INPUT_POLICY:
        raise IcpJobProtocolError("외부 ICP 결과 입력 정책이 전체 overlap 점군 계약과 다릅니다.")
    source_original_count = _require_positive_audit_count(audit["source_original_count"], "source_original_count")
    target_original_count = _require_positive_audit_count(audit["target_original_count"], "target_original_count")
    source_crop_count = _require_positive_audit_count(audit["source_crop_count"], "source_crop_count")
    target_crop_count = _require_positive_audit_count(audit["target_crop_count"], "target_crop_count")
    if source_crop_count > source_original_count or target_crop_count > target_original_count:
        raise IcpJobProtocolError("외부 ICP 결과 overlap crop 점 수가 원본 점 수를 넘습니다.")
    source_positions = _require_nonempty_result_string(
        audit["source_crop_positions_fingerprint"],
        "full_input_audit.source_crop_positions_fingerprint",
    )
    target_positions = _require_nonempty_result_string(
        audit["target_crop_positions_fingerprint"],
        "full_input_audit.target_crop_positions_fingerprint",
    )
    source_ids = _require_nonempty_result_string(
        audit["source_crop_point_ids_fingerprint"],
        "full_input_audit.source_crop_point_ids_fingerprint",
    )
    target_ids = _require_nonempty_result_string(
        audit["target_crop_point_ids_fingerprint"],
        "full_input_audit.target_crop_point_ids_fingerprint",
    )
    _validate_external_stage_input_fingerprints(
        audit["stage_input_fingerprints"],
        source_positions=source_positions,
        target_positions=target_positions,
        source_ids=source_ids,
        target_ids=target_ids,
    )
    downsampling = _require_mapping_keys(
        audit["downsampling"],
        field_name="full_input_audit.downsampling",
        required_keys=_REQUIRED_OPEN3D_DOWNSAMPLING_KEYS,
    )
    if any(downsampling[key] is not False for key in _REQUIRED_OPEN3D_DOWNSAMPLING_KEYS):
        raise IcpJobProtocolError("외부 ICP 결과에 금지된 downsampling 처리가 기록되었습니다.")


def _require_positive_audit_count(value: object, field_name: str) -> int:
    """전체점 감사 점 수가 bool이 아닌 양의 정수인지 검증합니다."""

    if isinstance(value, bool) or not isinstance(value, (int, np.integer)):
        raise IcpJobProtocolError(f"외부 ICP 결과 {field_name}은(는) 양의 정수여야 합니다.")
    count = int(value)
    if count < 1:
        raise IcpJobProtocolError(f"외부 ICP 결과 {field_name}은(는) 1 이상이어야 합니다.")
    return count


def _validate_external_stage_input_fingerprints(
    value: object,
    *,
    source_positions: str,
    target_positions: str,
    source_ids: str,
    target_ids: str,
) -> None:
    """모든 외부 ICP 단계가 같은 전체 overlap 입력 fingerprint를 썼는지 검증합니다."""

    if not isinstance(value, Mapping) or not value:
        raise IcpJobProtocolError("외부 ICP 결과 단계별 입력 fingerprint가 비어 있거나 객체가 아닙니다.")
    expected = {
        "source_positions_fingerprint": source_positions,
        "target_positions_fingerprint": target_positions,
        "source_point_ids_fingerprint": source_ids,
        "target_point_ids_fingerprint": target_ids,
    }
    for stage_name, stage_value in value.items():
        if not isinstance(stage_name, str) or not stage_name.strip():
            raise IcpJobProtocolError("외부 ICP 결과 단계 이름이 올바르지 않습니다.")
        stage = _require_mapping_keys(
            stage_value,
            field_name=f"full_input_audit.stage_input_fingerprints.{stage_name}",
            required_keys=_REQUIRED_OPEN3D_STAGE_AUDIT_KEYS,
        )
        for key, expected_value in expected.items():
            actual_value = _require_nonempty_result_string(stage[key], f"{stage_name}.{key}")
            if actual_value != expected_value:
                raise IcpJobProtocolError("외부 ICP 단계별 입력 fingerprint가 전체 overlap 입력과 다릅니다.")


def _validate_external_result_arrays(arrays: Mapping[str, np.ndarray]) -> None:
    """외부 ICP 결과 NPZ가 고정 세 개의 4×4 float64 행렬만 담는지 검증합니다."""

    if frozenset(arrays) != _REQUIRED_OPEN3D_RESULT_ARRAY_KEYS:
        raise IcpJobProtocolError("외부 ICP 결과 NPZ 행렬 항목이 고정 계약과 다릅니다.")
    for key in _REQUIRED_OPEN3D_RESULT_ARRAY_KEYS:
        matrix = arrays[key]
        if matrix.dtype != np.dtype(np.float64) or matrix.shape != (4, 4):
            raise IcpJobProtocolError(f"외부 ICP 결과 {key} 행렬이 올바르지 않습니다.")


def request_job_cancel(handle: IcpJobHandle) -> bool:
    """한 번만 취소 sentinel을 원자 생성하고 요청 시각을 기록합니다."""

    if handle.cleaned or not handle.is_running:
        return False
    if handle.cancel_requested_at is not None:
        return False
    _write_bytes_atomically(handle.files.cancel_path, b"cancel\n")
    handle.cancel_requested_at = monotonic()
    _LOGGER.info("event=icp_job_cancel_requested job_id=%s", handle.job_id)
    return True


def terminate_job_worker(handle: IcpJobHandle) -> bool:
    """grace 기간을 넘긴 해당 worker 프로세스만 종료합니다."""

    if handle.cleaned or not handle.is_running:
        return False
    try:
        handle.process.terminate()
    except OSError as error:
        raise IcpBackendError("ICP worker 프로세스를 종료하지 못했습니다.") from error
    _LOGGER.warning("event=icp_worker_terminated job_id=%s", handle.job_id)
    return True


def kill_job_worker(handle: IcpJobHandle) -> bool:
    """terminate grace 이후에도 남은 해당 worker만 강제 종료합니다."""

    if handle.cleaned or not handle.is_running:
        return False
    try:
        handle.process.kill()
    except OSError as error:
        raise IcpBackendError("ICP worker 프로세스를 강제 종료하지 못했습니다.") from error
    _LOGGER.error("event=icp_worker_killed job_id=%s", handle.job_id)
    return True


def wait_for_job_worker(handle: IcpJobHandle, *, timeout_seconds: float | None = None) -> int | None:
    """worker 종료를 기다리고 프로세스 handle을 회수한 뒤 종료 코드를 반환합니다."""

    if handle.cleaned:
        return handle.return_code
    try:
        return handle.process.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        return None
    except OSError as error:
        raise IcpBackendError("ICP worker 종료 상태를 기다리지 못했습니다.") from error


def close_job_log(handle: IcpJobHandle) -> None:
    """worker 종료 뒤 메인 프로세스가 보유한 로그 파일 handle을 닫습니다."""

    stream = handle._worker_log_handle
    if stream is None:
        return
    try:
        stream.close()
    finally:
        handle._worker_log_handle = None


def cleanup_job_directory(handle: IcpJobHandle, *, keep_failure_diagnostics: bool) -> None:
    """종료된 작업의 전용 디렉터리만 명시적으로 정리합니다."""

    if handle.cleaned:
        return
    if handle.is_running:
        raise IcpBackendError("실행 중인 ICP worker의 작업 디렉터리는 정리할 수 없습니다.")
    close_job_log(handle)
    if keep_failure_diagnostics:
        handle.diagnostics_retained = True
        return
    try:
        shutil.rmtree(handle.files.job_directory)
    except OSError as error:
        raise IcpBackendError("ICP 작업 임시 디렉터리를 정리하지 못했습니다.") from error
    handle.diagnostics_retained = False
    handle.cleaned = True


__all__ = (
    "IcpBackendError",
    "IcpJobFiles",
    "IcpJobHandle",
    "IcpJobProtocolError",
    "IcpSolverBackend",
    "build_manifest",
    "cleanup_job_directory",
    "cleanup_unstarted_job_directory",
    "close_array_memmap",
    "close_job_log",
    "create_icp_job_directory",
    "launch_icp_worker",
    "launch_open3d_worker",
    "kill_job_worker",
    "read_array_file",
    "read_job_error",
    "read_job_manifest",
    "read_job_progress",
    "read_job_result",
    "read_json_file",
    "read_result_arrays",
    "request_job_cancel",
    "resolve_job_path",
    "terminate_job_worker",
    "validate_hybrid_result_metadata",
    "validate_open3d_environment",
    "validate_open3d_result_metadata",
    "validate_result_metadata_for_backend",
    "validate_small_gicp_environment",
    "validate_small_gicp_result_metadata",
    "wait_for_job_worker",
    "write_array_atomically",
    "write_job_manifest",
    "write_json_atomically",
    "write_point_ids_in_chunks_atomically",
    "write_result_arrays_atomically",
    "write_transformed_positions_atomically",
)
