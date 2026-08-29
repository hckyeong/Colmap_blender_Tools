"""CPython 3.12 외부 ICP worker의 독립 진입점입니다.

이 파일은 Blender 또는 기존 Hybrid 타입을 import하지 않습니다. job 디렉터리의
JSON·NPY만 읽고, Open3D 또는 small_gicp 결과 JSON·NPZ를 원자적으로 기록합니다.
"""

from __future__ import annotations

import argparse
import importlib
from importlib.metadata import PackageNotFoundError, version as package_version
import json
import os
from pathlib import Path
import platform
import struct
import sys
import tempfile
from time import monotonic, sleep
from typing import Any, Mapping


def _ensure_addon_parent_on_path() -> None:
    """직접 실행된 optional worker가 설치 애드온 패키지를 import하게 합니다."""

    addon_parent = str(Path(__file__).resolve().parents[3])
    if addon_parent not in sys.path:
        sys.path.insert(0, addon_parent)


_ensure_addon_parent_on_path()


_MANIFEST_NAME = "manifest.json"
_PROGRESS_NAME = "progress.json"
_RESULT_NAME = "result.json"
_RESULT_ARRAY_NAME = "result.npz"
_ERROR_NAME = "error.json"
_CANCEL_NAME = "cancel.request"
_EXPECTED_PROTOCOL_VERSION = "icp-job-protocol-v1"
_EXPECTED_OPEN3D_VERSION = "0.19.0"
_EXPECTED_SMALL_GICP_VERSION = "1.0.1"
_ATOMIC_REPLACE_RETRY_SECONDS = 1.0
_ATOMIC_REPLACE_RETRY_INTERVAL_SECONDS = 0.01
_BACKEND_VERSIONS = {
    "OPEN3D_GICP": "open3d-gicp-v1",
    "SMALL_GICP": "small-gicp-v6",
    "OPEN3D_ROBUST_POINT_TO_PLANE": "open3d-robust-point-to-plane-v1",
}


class Open3dWorkerProtocolError(RuntimeError):
    """외부 worker의 job 파일·환경·결과 계약 위반을 나타냅니다."""


def _arguments_after_separator(arguments: list[str]) -> list[str]:
    """호출자가 붙인 `--` 앞의 호스트 인자를 안전하게 제외합니다."""

    try:
        separator = arguments.index("--")
    except ValueError:
        return arguments
    return arguments[separator + 1 :]


def parse_worker_arguments(arguments: list[str] | None = None) -> argparse.Namespace:
    """job 실행과 환경 검사만 허용하는 worker CLI를 해석합니다."""

    parser = argparse.ArgumentParser(prog="colmap_align_open3d_worker")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--job-dir")
    group.add_argument("--check-environment", action="store_true")
    parser.add_argument("--backend", default="OPEN3D_GICP", choices=tuple(_BACKEND_VERSIONS))
    raw = list(sys.argv[1:] if arguments is None else arguments)
    return parser.parse_args(_arguments_after_separator(raw))


def _load_solver_module(backend_id: str) -> Any:
    """선택 backend의 패키지 solver를 고정 경로에서 불러옵니다."""

    module_name = (
        "align.alignment.icp_small_gicp_solver"
        if backend_id == "SMALL_GICP"
        else "align.optional_backends.open3d.solver"
    )
    try:
        return importlib.import_module(module_name)
    except ImportError as error:
        raise Open3dWorkerProtocolError("외부 ICP solver 모듈을 불러오지 못했습니다.") from error


def _python_version() -> str:
    """worker Python 버전을 프로토콜 문자열로 반환합니다."""

    return f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"


def _platform_architecture() -> str:
    """worker 포인터 폭을 결과 검증용 표준 문자열로 반환합니다."""

    return "64bit" if struct.calcsize("P") == 8 else "32bit"


def _open3d_environment_payload(*, run_smoke_test: bool) -> dict[str, Any]:
    """CPython·NumPy·Open3D legacy CPU API와 선택 smoke test를 검사합니다."""

    payload: dict[str, Any] = {
        "python_implementation": platform.python_implementation(),
        "worker_python_version": _python_version(),
        "worker_numpy_version": "",
        "open3d_version": "",
        "platform_architecture": _platform_architecture(),
        "open3d_api": "legacy_cpu",
        "gicp_api_available": False,
        "robust_point_to_plane_api_available": False,
        "synthetic_gicp_smoke": {"passed": False, "message": "실행하지 않았습니다."},
        "errors": [],
    }
    errors: list[str] = payload["errors"]
    if payload["python_implementation"] != "CPython":
        errors.append("Open3D worker는 CPython 실행 파일이어야 합니다.")
    if sys.version_info[:2] != (3, 12):
        errors.append("Open3D worker는 CPython 3.12.x여야 합니다.")
    if _platform_architecture() != "64bit":
        errors.append("Open3D worker는 64비트 프로세스여야 합니다.")
    try:
        import numpy as np
    except ImportError:
        errors.append("NumPy를 import하지 못했습니다.")
        payload["ok"] = False
        return payload
    payload["worker_numpy_version"] = str(np.__version__)
    try:
        import open3d as o3d
    except ImportError:
        errors.append("Open3D를 import하지 못했습니다.")
        payload["ok"] = False
        return payload
    payload["open3d_version"] = str(getattr(o3d, "__version__", ""))
    if payload["open3d_version"] != _EXPECTED_OPEN3D_VERSION:
        errors.append(f"Open3D 버전은 {_EXPECTED_OPEN3D_VERSION}이어야 합니다.")
    registration = getattr(getattr(o3d, "pipelines", None), "registration", None)
    gicp_required = (
        "registration_generalized_icp",
        "TransformationEstimationForGeneralizedICP",
        "ICPConvergenceCriteria",
    )
    robust_required = (
        "registration_icp",
        "TransformationEstimationPointToPlane",
        "TukeyLoss",
    )
    payload["gicp_api_available"] = bool(
        registration is not None and all(hasattr(registration, name) for name in gicp_required)
    )
    payload["robust_point_to_plane_api_available"] = bool(
        registration is not None and all(hasattr(registration, name) for name in robust_required)
    )
    if not payload["gicp_api_available"]:
        errors.append("Open3D legacy GICP API가 완전하지 않습니다.")
    if not payload["robust_point_to_plane_api_available"]:
        errors.append("Open3D legacy Tukey Point-to-Plane API가 완전하지 않습니다.")
    if run_smoke_test and not errors:
        try:
            # 3×3×3 비평면 점군은 GICP 내부 KNN covariance 경로의 최소 smoke 입력입니다.
            grid = np.stack(
                np.meshgrid(
                    np.arange(3, dtype=np.float64),
                    np.arange(3, dtype=np.float64),
                    np.arange(3, dtype=np.float64),
                    indexing="ij",
                ),
                axis=-1,
            ).reshape(-1, 3)
            source = o3d.geometry.PointCloud()
            target = o3d.geometry.PointCloud()
            source.points = o3d.utility.Vector3dVector(grid)
            target.points = o3d.utility.Vector3dVector(grid + np.array((0.01, -0.02, 0.03)))
            result = registration.registration_generalized_icp(
                source,
                target,
                1.0,
                np.identity(4, dtype=np.float64),
                registration.TransformationEstimationForGeneralizedICP(),
                registration.ICPConvergenceCriteria(max_iteration=1),
            )
            transformation = np.asarray(result.transformation, dtype=np.float64)
            if transformation.shape != (4, 4) or not np.isfinite(transformation).all():
                raise RuntimeError("synthetic GICP가 유한한 4×4 변환을 반환하지 않았습니다.")
            payload["synthetic_gicp_smoke"] = {"passed": True, "message": "통과"}
        except Exception as error:  # 외부 라이브러리 오류를 환경 진단으로만 정규화합니다.
            errors.append(f"synthetic GICP smoke 실패: {error}")
            payload["synthetic_gicp_smoke"] = {"passed": False, "message": str(error)}
    elif not run_smoke_test:
        payload["synthetic_gicp_smoke"] = {"passed": None, "message": "worker 실행 시 생략"}
    payload["ok"] = not errors
    return payload


def _small_gicp_environment_payload(*, run_smoke_test: bool) -> dict[str, Any]:
    """CPython·NumPy·small_gicp GICP API와 선택 smoke test를 검사합니다."""

    payload: dict[str, Any] = {
        "python_implementation": platform.python_implementation(),
        "worker_python_version": _python_version(),
        "worker_numpy_version": "",
        "open3d_version": "",
        "small_gicp_version": "",
        "platform_architecture": _platform_architecture(),
        "small_gicp_api": "preprocessed_point_cloud_gicp",
        "gicp_api_available": False,
        "synthetic_gicp_smoke": {"passed": False, "message": "실행하지 않았습니다."},
        "errors": [],
    }
    errors: list[str] = payload["errors"]
    if payload["python_implementation"] != "CPython":
        errors.append("small_gicp worker는 CPython 실행 파일이어야 합니다.")
    if sys.version_info[:2] != (3, 12):
        errors.append("small_gicp worker는 CPython 3.12.x여야 합니다.")
    if _platform_architecture() != "64bit":
        errors.append("small_gicp worker는 64비트 프로세스여야 합니다.")
    try:
        import numpy as np
    except ImportError:
        errors.append("NumPy를 import하지 못했습니다.")
        payload["ok"] = False
        return payload
    payload["worker_numpy_version"] = str(np.__version__)
    try:
        import small_gicp
    except ImportError:
        errors.append("small_gicp를 import하지 못했습니다.")
        payload["ok"] = False
        return payload
    try:
        payload["small_gicp_version"] = package_version("small-gicp")
    except PackageNotFoundError:
        errors.append("small_gicp 배포 버전을 확인하지 못했습니다.")
    if payload["small_gicp_version"] != _EXPECTED_SMALL_GICP_VERSION:
        errors.append(f"small_gicp 버전은 {_EXPECTED_SMALL_GICP_VERSION}이어야 합니다.")
    required = ("align", "PointCloud", "KdTree", "estimate_normals_covariances")
    payload["gicp_api_available"] = all(hasattr(small_gicp, name) for name in required)
    if not payload["gicp_api_available"]:
        errors.append("small_gicp GICP API가 완전하지 않습니다.")
    if run_smoke_test and not errors:
        try:
            grid = np.stack(
                np.meshgrid(
                    np.arange(3, dtype=np.float64),
                    np.arange(3, dtype=np.float64),
                    np.arange(3, dtype=np.float64),
                    indexing="ij",
                ),
                axis=-1,
            ).reshape(-1, 3)
            target = small_gicp.PointCloud(grid)
            source = small_gicp.PointCloud(grid)
            target_tree = small_gicp.KdTree(target, num_threads=1)
            source_tree = small_gicp.KdTree(source, num_threads=1)
            small_gicp.estimate_normals_covariances(
                target,
                target_tree,
                num_neighbors=8,
                num_threads=1,
            )
            small_gicp.estimate_normals_covariances(
                source,
                source_tree,
                num_neighbors=8,
                num_threads=1,
            )
            result = small_gicp.align(
                target,
                source,
                target_tree,
                init_T_target_source=np.identity(4, dtype=np.float64),
                registration_type="GICP",
                max_correspondence_distance=1.0,
                num_threads=1,
                max_iterations=1,
            )
            transformation = np.asarray(result.T_target_source, dtype=np.float64)
            if transformation.shape != (4, 4) or not np.isfinite(transformation).all():
                raise RuntimeError("synthetic GICP가 유한한 4×4 변환을 반환하지 않았습니다.")
            payload["synthetic_gicp_smoke"] = {"passed": True, "message": "통과"}
        except Exception as error:  # 외부 라이브러리 오류를 환경 진단으로만 정규화합니다.
            errors.append(f"synthetic small_gicp smoke 실패: {error}")
            payload["synthetic_gicp_smoke"] = {"passed": False, "message": str(error)}
    elif not run_smoke_test:
        payload["synthetic_gicp_smoke"] = {"passed": None, "message": "worker 실행 시 생략"}
    payload["ok"] = not errors
    return payload


def _environment_payload(backend_id: str, *, run_smoke_test: bool) -> dict[str, Any]:
    """선택 외부 backend에 필요한 라이브러리만 검사합니다."""

    normalized = str(backend_id).strip().upper()
    if normalized == "SMALL_GICP":
        return _small_gicp_environment_payload(run_smoke_test=run_smoke_test)
    if normalized in {"OPEN3D_GICP", "OPEN3D_ROBUST_POINT_TO_PLANE"}:
        return _open3d_environment_payload(run_smoke_test=run_smoke_test)
    raise Open3dWorkerProtocolError("지원하지 않는 외부 ICP backend ID입니다.")


def check_open3d_environment() -> dict[str, Any]:
    """CLI 환경 검사에서 사용할 전체 synthetic GICP smoke 결과를 반환합니다."""

    return _environment_payload("OPEN3D_GICP", run_smoke_test=True)


def check_small_gicp_environment() -> dict[str, Any]:
    """CLI 환경 검사에서 사용할 small_gicp synthetic smoke 결과를 반환합니다."""

    return _environment_payload("SMALL_GICP", run_smoke_test=True)


def _resolve_job_directory(value: str | Path) -> Path:
    """존재하는 전용 job 디렉터리를 절대 경로로 제한합니다."""

    try:
        directory = Path(value).resolve(strict=True)
    except OSError as error:
        raise Open3dWorkerProtocolError("Open3D job 디렉터리를 찾을 수 없습니다.") from error
    if not directory.is_dir():
        raise Open3dWorkerProtocolError("Open3D job 경로가 디렉터리가 아닙니다.")
    return directory


def _job_file(job_directory: Path, relative_name: str) -> Path:
    """job 디렉터리 밖으로 벗어나는 입력·출력 파일명을 거부합니다."""

    name = str(relative_name).strip()
    if not name or Path(name).is_absolute():
        raise Open3dWorkerProtocolError("Open3D job 파일명은 비어 있지 않은 상대 경로여야 합니다.")
    candidate = (job_directory / name).resolve(strict=False)
    try:
        candidate.relative_to(job_directory)
    except ValueError as error:
        raise Open3dWorkerProtocolError("Open3D job 파일 경로가 job 디렉터리를 벗어났습니다.") from error
    return candidate


def _read_json(path: Path, *, field_name: str) -> dict[str, Any]:
    """UTF-8 JSON 객체만 읽어 worker 경계로 반환합니다."""

    try:
        with path.open("r", encoding="utf-8") as stream:
            value = json.load(stream)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise Open3dWorkerProtocolError(f"{field_name} JSON을 읽지 못했습니다.") from error
    if not isinstance(value, dict):
        raise Open3dWorkerProtocolError(f"{field_name} JSON은 객체여야 합니다.")
    return value


def _replace_atomically(temporary: Path, target: Path) -> None:
    """Windows의 짧은 파일 공유 충돌만 제한 시간 동안 재시도합니다."""

    deadline = monotonic() + _ATOMIC_REPLACE_RETRY_SECONDS
    while True:
        try:
            os.replace(temporary, target)
            return
        except PermissionError:
            if monotonic() >= deadline:
                raise
            sleep(_ATOMIC_REPLACE_RETRY_INTERVAL_SECONDS)


def _write_json_atomically(path: Path, payload: Mapping[str, Any]) -> None:
    """JSON을 같은 디렉터리 임시 파일 뒤 원자 교체로 기록합니다."""

    try:
        encoded = json.dumps(
            dict(payload),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise Open3dWorkerProtocolError("Open3D JSON 결과를 직렬화하지 못했습니다.") from error
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        _replace_atomically(temporary, path)
    except BaseException:
        try:
            temporary.unlink(missing_ok=True)
        finally:
            raise


def _write_result_arrays_atomically(path: Path, arrays: Mapping[str, Any]) -> None:
    """세 결과 행렬만 pickle 없이 압축 NPZ로 원자 기록합니다."""

    import numpy as np

    expected = {"initial_alignment", "final_alignment", "icp_delta"}
    if set(arrays) != expected:
        raise Open3dWorkerProtocolError("Open3D 결과 NPZ 행렬 키가 정확하지 않습니다.")
    normalized: dict[str, Any] = {}
    for name, value in arrays.items():
        matrix = np.ascontiguousarray(value, dtype=np.float64)
        if matrix.shape != (4, 4) or not np.isfinite(matrix).all():
            raise Open3dWorkerProtocolError(f"Open3D 결과 {name} 행렬이 유한한 4×4가 아닙니다.")
        normalized[name] = matrix
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            np.savez_compressed(stream, **normalized)
            stream.flush()
            os.fsync(stream.fileno())
        _replace_atomically(temporary, path)
    except BaseException:
        try:
            temporary.unlink(missing_ok=True)
        finally:
            raise


def _required_text(manifest: Mapping[str, Any], key: str) -> str:
    """manifest의 비어 있지 않은 식별자 문자열을 반환합니다."""

    if key not in manifest:
        raise Open3dWorkerProtocolError(f"Open3D manifest에 필수 키가 없습니다: {key}")
    value = str(manifest[key]).strip()
    if not value:
        raise Open3dWorkerProtocolError(f"Open3D manifest의 {key} 값이 비어 있습니다.")
    return value


def _validate_manifest_identity(manifest: Mapping[str, Any], job_directory: Path) -> None:
    """backend/protocol/core snapshot 식별자가 job 폴더와 일치하는지 검증합니다."""

    if _required_text(manifest, "job_id") != job_directory.name:
        raise Open3dWorkerProtocolError("Open3D manifest job_id가 job 디렉터리 이름과 다릅니다.")
    _required_text(manifest, "input_fingerprint")
    _required_text(manifest, "algorithm_version")
    backend_id = _required_text(manifest, "backend_id").upper()
    backend_version = _required_text(manifest, "backend_algorithm_version")
    if backend_id not in _BACKEND_VERSIONS:
        raise Open3dWorkerProtocolError("지원하지 않는 Open3D backend ID입니다.")
    if backend_version != _BACKEND_VERSIONS[backend_id]:
        raise Open3dWorkerProtocolError("Open3D backend 알고리즘 버전이 backend ID와 다릅니다.")
    if _required_text(manifest, "job_protocol_version") != _EXPECTED_PROTOCOL_VERSION:
        raise Open3dWorkerProtocolError("지원하지 않는 Open3D job protocol 버전입니다.")
    if _required_text(manifest, "moving_cloud_frame").upper() != "INITIAL_PROJECT_WORLD":
        raise Open3dWorkerProtocolError("Open3D source frame은 INITIAL_PROJECT_WORLD여야 합니다.")


def _read_array(
    job_directory: Path,
    filename: object,
    *,
    dtype: Any,
    shape: tuple[int | None, ...],
    field_name: str,
) -> Any:
    """pickle 없이 read-only NPY를 읽고 dtype·형상·유한성을 검증합니다."""

    import numpy as np

    path = _job_file(job_directory, str(filename))
    try:
        value = np.load(path, allow_pickle=False, mmap_mode="r")
    except (OSError, ValueError) as error:
        raise Open3dWorkerProtocolError(f"{field_name} NPY를 읽지 못했습니다.") from error
    if not isinstance(value, np.ndarray) or value.dtype != np.dtype(dtype):
        raise Open3dWorkerProtocolError(f"{field_name} NPY dtype이 올바르지 않습니다.")
    if value.ndim != len(shape) or any(
        expected is not None and actual != expected
        for actual, expected in zip(value.shape, shape, strict=True)
    ):
        raise Open3dWorkerProtocolError(f"{field_name} NPY 형상이 올바르지 않습니다.")
    if not value.flags.c_contiguous or value.flags.writeable:
        raise Open3dWorkerProtocolError(f"{field_name} NPY는 read-only C-contiguous여야 합니다.")
    if np.issubdtype(value.dtype, np.floating) and not np.isfinite(value).all():
        raise Open3dWorkerProtocolError(f"{field_name} NPY에 유한하지 않은 값이 있습니다.")
    return value


def _mapping_value(mapping: Mapping[str, Any], key: str, field_name: str) -> Mapping[str, Any]:
    """manifest 내부의 필수 객체 값을 반환합니다."""

    value = mapping.get(key)
    if not isinstance(value, Mapping):
        raise Open3dWorkerProtocolError(f"{field_name}은(는) 객체여야 합니다.")
    return value


def _list_value(mapping: Mapping[str, Any], key: str, field_name: str) -> list[Any]:
    """manifest 내부의 필수 배열 값을 반환합니다."""

    value = mapping.get(key)
    if not isinstance(value, list):
        raise Open3dWorkerProtocolError(f"{field_name}은(는) 배열이어야 합니다.")
    return value


def _optional_marker_points(value: object, field_name: str) -> Any | None:
    """선택 marker 검증점 배열을 유한 float64 N×3으로 복원합니다."""

    if value is None:
        return None
    import numpy as np

    try:
        points = np.ascontiguousarray(value, dtype=np.float64)
    except (TypeError, ValueError, OverflowError) as error:
        raise Open3dWorkerProtocolError(f"{field_name} marker 배열을 읽지 못했습니다.") from error
    if points.ndim != 2 or points.shape[1] != 3 or points.shape[0] < 3 or not np.isfinite(points).all():
        raise Open3dWorkerProtocolError(f"{field_name} marker 배열 형식이 올바르지 않습니다.")
    return points


def _solver_input_from_manifest(job_directory: Path, manifest: Mapping[str, Any], solver: Any) -> Any:
    """manifest와 NPY를 Open3D 독립 solver 입력으로 복원합니다."""

    import numpy as np

    _validate_manifest_identity(manifest, job_directory)
    arrays = _mapping_value(manifest, "arrays", "Open3D manifest arrays")
    moving = _mapping_value(arrays, "moving", "Open3D manifest moving")
    references = _list_value(arrays, "references", "Open3D manifest references")
    moving_model_id = _required_text(manifest, "moving_model_id")
    if _required_text(moving, "model_id") != moving_model_id:
        raise Open3dWorkerProtocolError("Open3D moving 모델 ID가 manifest와 arrays에서 다릅니다.")
    moving_positions = _read_array(
        job_directory,
        moving.get("positions_file"),
        dtype=np.float64,
        shape=(None, 3),
        field_name="Open3D moving positions",
    )
    moving_ids = _read_array(
        job_directory,
        moving.get("point_ids_file"),
        dtype=np.uint64,
        shape=(moving_positions.shape[0],),
        field_name="Open3D moving POINT3D_ID",
    )
    source = solver.Open3dPointCloud(
        model_id=moving_model_id,
        positions_float64=moving_positions,
        point_ids_uint64=moving_ids,
    )
    expected_reference_ids = _list_value(
        manifest,
        "reference_model_ids",
        "Open3D manifest reference_model_ids",
    )
    if len(references) != len(expected_reference_ids) or not references:
        raise Open3dWorkerProtocolError("Open3D A 모델 목록 길이가 arrays와 다릅니다.")
    clouds: list[Any] = []
    for index, (entry, expected_model_id) in enumerate(zip(references, expected_reference_ids, strict=True)):
        if not isinstance(entry, Mapping):
            raise Open3dWorkerProtocolError(f"Open3D references[{index}]가 객체가 아닙니다.")
        model_id = _required_text(entry, "model_id")
        if model_id != str(expected_model_id).strip() or not model_id:
            raise Open3dWorkerProtocolError("Open3D A 모델 ID 순서가 manifest와 arrays에서 다릅니다.")
        positions = _read_array(
            job_directory,
            entry.get("positions_file"),
            dtype=np.float64,
            shape=(None, 3),
            field_name=f"Open3D reference[{index}] positions",
        )
        point_ids = _read_array(
            job_directory,
            entry.get("point_ids_file"),
            dtype=np.uint64,
            shape=(positions.shape[0],),
            field_name=f"Open3D reference[{index}] POINT3D_ID",
        )
        clouds.append(
            solver.Open3dPointCloud(
                model_id=model_id,
                positions_float64=positions,
                point_ids_uint64=point_ids,
            )
        )
    parameters = _mapping_value(manifest, "parameters", "Open3D manifest parameters")
    initial_alignment = manifest.get("initial_alignment")
    marker_reference = _optional_marker_points(
        manifest.get("marker_reference_project"),
        "Open3D A",
    )
    marker_moving = _optional_marker_points(
        manifest.get("marker_moving_project"),
        "Open3D P",
    )
    try:
        marker_uniform_scale = float(manifest.get("marker_uniform_scale"))
        marker_max_error = float(manifest.get("marker_max_error"))
    except (TypeError, ValueError, OverflowError) as error:
        raise Open3dWorkerProtocolError("Open3D marker scale 또는 최대 오차가 수치가 아닙니다.") from error
    if not np.isfinite(marker_uniform_scale) or marker_uniform_scale <= 0.0:
        raise Open3dWorkerProtocolError("Open3D marker uniform scale이 양수가 아닙니다.")
    if not np.isfinite(marker_max_error) or marker_max_error < 0.0:
        raise Open3dWorkerProtocolError("Open3D marker 최대 오차가 0 이상 유한값이 아닙니다.")
    return solver.Open3dSolverInput(
        job_id=_required_text(manifest, "job_id"),
        input_fingerprint=_required_text(manifest, "input_fingerprint"),
        algorithm_version=_required_text(manifest, "algorithm_version"),
        backend_id=_required_text(manifest, "backend_id"),
        backend_algorithm_version=_required_text(manifest, "backend_algorithm_version"),
        job_protocol_version=_required_text(manifest, "job_protocol_version"),
        moving_cloud_frame=_required_text(manifest, "moving_cloud_frame"),
        source=source,
        references=tuple(clouds),
        initial_alignment=initial_alignment,
        marker_uniform_scale=marker_uniform_scale,
        marker_max_error=marker_max_error,
        parameters=parameters,
        marker_signature=_required_text(manifest, "marker_signature"),
        model_revision_snapshot=tuple(manifest.get("model_revision_snapshot", ())),
        model_state_snapshot=tuple(manifest.get("model_state_snapshot", ())),
        marker_reference_project=marker_reference,
        marker_moving_project=marker_moving,
    )


def _identity_payload(manifest: Mapping[str, Any]) -> dict[str, Any]:
    """오류·취소 progress에도 필요한 외부 ICP 식별자 필드를 만듭니다."""

    return {
        "job_id": str(manifest.get("job_id", "")),
        "input_fingerprint": str(manifest.get("input_fingerprint", "")),
        "algorithm_version": str(manifest.get("algorithm_version", "")),
        "backend_id": str(manifest.get("backend_id", "")),
        "backend_algorithm_version": str(manifest.get("backend_algorithm_version", "")),
        "job_protocol_version": str(manifest.get("job_protocol_version", "")),
    }


def _cancel_requested(job_directory: Path) -> bool:
    """main process가 만든 취소 sentinel 존재 여부를 반환합니다."""

    return _job_file(job_directory, _CANCEL_NAME).is_file()


def run_open3d_worker(job_directory: Path | str) -> None:
    """한 외부 ICP job을 복원·계산·원자 결과 기록 순서로 실행합니다."""

    root = _resolve_job_directory(job_directory)
    manifest = _read_json(_job_file(root, _MANIFEST_NAME), field_name="외부 ICP manifest")
    _validate_manifest_identity(manifest, root)
    backend_id = _required_text(manifest, "backend_id").upper()
    environment = _environment_payload(backend_id, run_smoke_test=False)
    if not bool(environment.get("ok")):
        joined = " ".join(str(item) for item in environment.get("errors", ()))
        raise Open3dWorkerProtocolError(f"외부 ICP worker 환경이 올바르지 않습니다. {joined}".strip())
    solver = _load_solver_module(backend_id)
    solver_input = _solver_input_from_manifest(root, manifest, solver)
    started_at = monotonic()

    def progress_sink(event: Mapping[str, Any]) -> None:
        """solver event에 worker 환경 audit을 합쳐 progress.json으로 원자 기록합니다."""

        payload = dict(event)
        payload.update(
            {
                "worker_python_version": environment["worker_python_version"],
                "worker_numpy_version": environment["worker_numpy_version"],
                "open3d_version": environment.get("open3d_version", ""),
                "small_gicp_version": environment.get("small_gicp_version", ""),
                "platform_architecture": environment["platform_architecture"],
            }
        )
        _write_json_atomically(_job_file(root, _PROGRESS_NAME), payload)

    try:
        solve = solver.solve_small_gicp if backend_id == "SMALL_GICP" else solver.solve_open3d_icp
        result = solve(
            solver_input,
            progress_sink=progress_sink,
            cancel_probe=lambda: _cancel_requested(root),
        )
    except solver.Open3dIcpCancelled:
        cancelled = _identity_payload(manifest)
        cancelled.update(
            {
                "status": "CANCELLED",
                "message": "외부 ICP 작업을 취소했습니다.",
                "worker_python_version": environment["worker_python_version"],
                "worker_numpy_version": environment["worker_numpy_version"],
                "open3d_version": environment.get("open3d_version", ""),
                "small_gicp_version": environment.get("small_gicp_version", ""),
                "platform_architecture": environment["platform_architecture"],
            }
        )
        _write_json_atomically(_job_file(root, _ERROR_NAME), cancelled)
        progress_sink(
            {
                **_identity_payload(manifest),
                "open3d_version": environment.get("open3d_version", ""),
                "small_gicp_version": environment.get("small_gicp_version", ""),
                "stage": "CANCELLED",
                "level_name": "",
                "iteration": 0,
                "max_iterations": 0,
                "progress": 1.0,
                "message": "외부 ICP 작업을 취소했습니다.",
                "level_complete": False,
            }
        )
        return
    metadata = result.to_metadata(environment=environment)
    metadata["elapsed_seconds"] = float(monotonic() - started_at)
    # NPZ를 먼저 확정하고 JSON을 마지막에 교체해 reader가 반쪽 결과를 보지 않게 합니다.
    _write_result_arrays_atomically(_job_file(root, _RESULT_ARRAY_NAME), result.result_arrays())
    _write_json_atomically(_job_file(root, _RESULT_NAME), metadata)
    terminal_prefix = "SMALL_GICP" if backend_id == "SMALL_GICP" else "OPEN3D"
    terminal_status = "PASS" if metadata["quality"]["status"] == "PASS" else "REVIEW"
    terminal = f"{terminal_prefix}_COMPLETED_{terminal_status}"
    progress_sink(
        {
            **_identity_payload(manifest),
            "open3d_version": environment.get("open3d_version", ""),
            "small_gicp_version": environment.get("small_gicp_version", ""),
            "stage": terminal,
            "level_name": "",
            "iteration": 0,
            "max_iterations": 0,
            "progress": 1.0,
            "message": "외부 ICP 결과를 기록했습니다.",
            "level_complete": True,
            "stable_alignment_4x4": result.result_arrays()["final_alignment"].tolist(),
        }
    )


def _write_worker_failure(job_directory: Path | None, error: Exception) -> None:
    """manifest를 읽을 수 있는 경우에만 구조화 FAILED 파일을 남깁니다."""

    if job_directory is None:
        return
    try:
        manifest = _read_json(_job_file(job_directory, _MANIFEST_NAME), field_name="외부 ICP manifest")
        payload = _identity_payload(manifest)
        payload.update(
            {
                "status": "FAILED",
                "error_type": type(error).__name__,
                "message": str(error) or "외부 ICP worker가 실패했습니다.",
            }
        )
        _write_json_atomically(_job_file(job_directory, _ERROR_NAME), payload)
    except (OSError, Open3dWorkerProtocolError):
        return


def main(arguments: list[str] | None = None) -> int:
    """CLI 인자를 처리하고 환경 JSON 또는 job 결과를 반환합니다."""

    job_directory: Path | None = None
    try:
        namespace = parse_worker_arguments(arguments)
        if bool(namespace.check_environment):
            payload = _environment_payload(namespace.backend, run_smoke_test=True)
            # 콘솔 코드페이지와 무관하게 환경 검사 JSON을 안전하게 전달합니다.
            print(json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")))
            return 0 if bool(payload.get("ok")) else 2
        job_directory = _resolve_job_directory(namespace.job_dir)
        run_open3d_worker(job_directory)
        return 0
    except Exception as error:
        _write_worker_failure(job_directory, error)
        print(f"외부 ICP worker 오류: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = (
    "Open3dWorkerProtocolError",
    "check_open3d_environment",
    "check_small_gicp_environment",
    "main",
    "parse_worker_arguments",
    "run_open3d_worker",
)
