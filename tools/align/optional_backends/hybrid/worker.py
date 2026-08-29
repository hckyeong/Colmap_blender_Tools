"""숨은 Blender 프로세스에서 ICP 수치 계산만 실행하는 worker 진입점입니다."""

from __future__ import annotations

import argparse
from dataclasses import fields, is_dataclass
from enum import Enum
from pathlib import Path
import sys
from time import monotonic
from typing import Any, Mapping

import numpy as np


def _ensure_addon_parent_on_path() -> None:
    """직접 실행된 worker가 설치 애드온 패키지를 import할 수 있게 부모 경로를 추가합니다."""

    addon_parent = str(Path(__file__).resolve().parents[3])
    if addon_parent not in sys.path:
        sys.path.insert(0, addon_parent)


_ensure_addon_parent_on_path()

from align.alignment.icp_backend import (  # noqa: E402
    IcpJobProtocolError,
    read_array_file,
    read_job_manifest,
    resolve_job_path,
    validate_hybrid_result_metadata,
    write_json_atomically,
    write_result_arrays_atomically,
)
from align.alignment.types import Sim3Transform  # noqa: E402
from align.constants import (  # noqa: E402
    ICP_ALGORITHM_VERSION,
    ICP_BACKEND_ALGORITHM_VERSION_CUSTOM_HYBRID,
    ICP_BACKEND_ID_CUSTOM_HYBRID,
    ICP_JOB_PROTOCOL_VERSION,
    ICP_STAGE_BUILDING_UNIFORM_SAMPLES,
    ICP_STAGE_CANCELLED,
    ICP_STAGE_COARSE_POINT_TO_POINT,
    ICP_STAGE_COMPLETED_PASS,
    ICP_STAGE_COMPLETED_REVIEW,
    ICP_STAGE_ESTIMATING_NORMALS_COARSE,
    ICP_STAGE_ESTIMATING_NORMALS_FINE,
    ICP_STAGE_ESTIMATING_NORMALS_MEDIUM,
    ICP_STAGE_EVALUATING_BEST_CANDIDATE,
    ICP_STAGE_FAILED,
    ICP_STAGE_FINE_POINT_TO_PLANE,
    ICP_STAGE_MEDIUM_POINT_TO_PLANE,
    ICP_STAGE_PREPARING_INPUT,
    ICP_STAGE_SCALE_REFINEMENT,
    ICP_STAGE_SELECTING_OVERLAP,
)
from align.logging_utils import configure_logging, get_logger  # noqa: E402


_LOGGER = get_logger("alignment.icp_worker")
_HYBRID_PROGRESS_STAGES = frozenset(
    {
        ICP_STAGE_PREPARING_INPUT,
        ICP_STAGE_SELECTING_OVERLAP,
        ICP_STAGE_BUILDING_UNIFORM_SAMPLES,
        ICP_STAGE_ESTIMATING_NORMALS_COARSE,
        ICP_STAGE_COARSE_POINT_TO_POINT,
        ICP_STAGE_ESTIMATING_NORMALS_MEDIUM,
        ICP_STAGE_MEDIUM_POINT_TO_PLANE,
        ICP_STAGE_ESTIMATING_NORMALS_FINE,
        ICP_STAGE_FINE_POINT_TO_PLANE,
        ICP_STAGE_SCALE_REFINEMENT,
        ICP_STAGE_EVALUATING_BEST_CANDIDATE,
        ICP_STAGE_COMPLETED_PASS,
        ICP_STAGE_COMPLETED_REVIEW,
        ICP_STAGE_FAILED,
        ICP_STAGE_CANCELLED,
    }
)


def _custom_backend_identity() -> dict[str, str]:
    """기존 Hybrid worker가 기록할 backend 식별자를 반환합니다."""

    return {
        "backend_id": ICP_BACKEND_ID_CUSTOM_HYBRID,
        "backend_algorithm_version": ICP_BACKEND_ALGORITHM_VERSION_CUSTOM_HYBRID,
        "job_protocol_version": ICP_JOB_PROTOCOL_VERSION,
    }


_MAX_PROGRESS_INTEGER = int(np.iinfo(np.int64).max)


def _arguments_after_separator(arguments: list[str]) -> list[str]:
    """Blender 자체 인자 뒤 `--` 이후의 worker 인자만 반환합니다."""

    try:
        separator = arguments.index("--")
    except ValueError:
        return arguments
    return arguments[separator + 1 :]


def parse_worker_arguments(arguments: list[str] | None = None) -> argparse.Namespace:
    """worker가 허용하는 전용 작업 디렉터리 인자만 엄격하게 해석합니다."""

    parser = argparse.ArgumentParser(prog="colmap_align_icp_worker")
    parser.add_argument("--job-dir", required=True)
    parsed = _arguments_after_separator(list(sys.argv[1:] if arguments is None else arguments))
    return parser.parse_args(parsed)


def _enum_from_value(enum_type: Any, value: object) -> Any:
    """manifest 문자열을 core enum 이름 또는 enum 값으로 엄격하게 복원합니다."""

    normalized = str(value).strip().upper()
    try:
        return enum_type[normalized]
    except KeyError:
        pass
    for item in enum_type:
        if str(getattr(item, "value", "")).upper() == normalized:
            return item
    raise IcpJobProtocolError(f"지원하지 않는 ICP enum 값입니다: {value}")


def _parameters_from_manifest(value: object) -> Any:
    """메인 프로세스가 기록한 core IcpParameters JSON을 같은 타입으로 복원합니다."""

    if not isinstance(value, dict):
        raise IcpJobProtocolError("ICP manifest parameters가 객체가 아닙니다.")
    try:
        from align.alignment.icp_types import IcpParameters
    except ImportError as error:
        raise IcpJobProtocolError("ICP 수치 설정 타입을 worker에서 불러오지 못했습니다.") from error
    try:
        return IcpParameters.from_mapping(value)
    except (TypeError, ValueError) as error:
        raise IcpJobProtocolError("ICP manifest parameters를 복원하지 못했습니다.") from error


def _required_mapping_value(mapping: Mapping[str, Any], key: str) -> Any:
    """manifest의 필수 키를 빈 값 없이 반환합니다."""

    if key not in mapping:
        raise IcpJobProtocolError(f"ICP manifest에 필수 키가 없습니다: {key}")
    value = mapping[key]
    if value is None or (isinstance(value, str) and not value.strip()):
        raise IcpJobProtocolError(f"ICP manifest의 필수 값이 비어 있습니다: {key}")
    return value


def _strict_manifest_integer(value: object, *, field_name: str, minimum: int = 0) -> int:
    """manifest 정수를 bool·문자열·실수 암묵 변환 없이 검증합니다."""

    if isinstance(value, (bool, np.bool_)) or not isinstance(value, (int, np.integer)):
        raise IcpJobProtocolError(f"{field_name}은(는) bool이 아닌 정수여야 합니다.")
    try:
        normalized = int(value)
    except (OverflowError, TypeError, ValueError) as error:
        raise IcpJobProtocolError(f"{field_name}을(를) 정수로 변환할 수 없습니다.") from error
    if normalized < minimum:
        raise IcpJobProtocolError(f"{field_name}은(는) {minimum} 이상이어야 합니다.")
    return normalized


def _strict_manifest_float(value: object, *, field_name: str, minimum: float | None = None) -> float:
    """manifest 실수를 bool·문자열 암묵 변환 없이 유한값으로 검증합니다."""

    if isinstance(value, (bool, np.bool_)) or not isinstance(
        value,
        (int, float, np.integer, np.floating),
    ):
        raise IcpJobProtocolError(f"{field_name}은(는) 숫자여야 합니다.")
    try:
        normalized = float(value)
    except (OverflowError, TypeError, ValueError) as error:
        raise IcpJobProtocolError(f"{field_name}을(를) 수치로 변환할 수 없습니다.") from error
    if not np.isfinite(normalized):
        raise IcpJobProtocolError(f"{field_name}은(는) 유한해야 합니다.")
    if minimum is not None and normalized < minimum:
        raise IcpJobProtocolError(f"{field_name}은(는) {minimum} 이상이어야 합니다.")
    return normalized


def _cloud_from_manifest(
    job_directory: Path,
    entry: Mapping[str, Any],
) -> Any:
    """한 moving/reference cloud의 NPY 두 개를 검증해 core point cloud로 복원합니다."""

    try:
        from align.alignment.icp_types import IcpPointCloud
    except ImportError as error:
        raise IcpJobProtocolError("ICP point cloud 타입을 worker에서 불러오지 못했습니다.") from error
    model_id = str(_required_mapping_value(entry, "model_id")).strip()
    positions_file = str(_required_mapping_value(entry, "positions_file"))
    point_ids_file = str(_required_mapping_value(entry, "point_ids_file"))
    positions = read_array_file(
        job_directory,
        positions_file,
        dtype=np.dtype(np.float64),
        shape=(None, 3),
        mmap_mode="r",
    )
    point_ids = read_array_file(
        job_directory,
        point_ids_file,
        dtype=np.dtype(np.uint64),
        shape=(positions.shape[0],),
        mmap_mode="r",
    )
    return IcpPointCloud.from_verified_arrays(
        model_id=model_id,
        positions_float64=positions,
        point_ids_uint64=point_ids,
    )


def _matrix_from_manifest(value: object, *, field_name: str) -> Sim3Transform:
    """manifest의 4×4 float64 행렬을 검증된 Sim3Transform으로 복원합니다."""

    try:
        matrix = np.asarray(value, dtype=np.float64)
    except (OverflowError, TypeError, ValueError) as error:
        raise IcpJobProtocolError(f"{field_name} 행렬을 수치 행렬로 변환할 수 없습니다.") from error
    if matrix.shape != (4, 4) or not np.isfinite(matrix).all():
        raise IcpJobProtocolError(f"{field_name} 행렬이 올바르지 않습니다.")
    try:
        return Sim3Transform(matrix_float64=matrix)
    except (TypeError, ValueError) as error:
        raise IcpJobProtocolError(f"{field_name} Sim(3) 행렬이 올바르지 않습니다.") from error


def _marker_points_from_manifest(value: object, *, field_name: str) -> np.ndarray | None:
    """선택적으로 전달된 대표 marker 점 배열을 유한 float64로 복원합니다."""

    if value is None:
        return None
    try:
        points = np.asarray(value, dtype=np.float64)
    except (OverflowError, TypeError, ValueError) as error:
        raise IcpJobProtocolError(f"{field_name} marker 좌표를 수치 배열로 변환할 수 없습니다.") from error
    if points.ndim != 2 or points.shape[1] != 3 or not np.isfinite(points).all():
        raise IcpJobProtocolError(f"{field_name} marker 좌표가 올바르지 않습니다.")
    return np.ascontiguousarray(points, dtype=np.float64)


def input_snapshot_from_manifest(job_directory: Path) -> tuple[dict[str, Any], Any]:
    """worker manifest와 NPY 입력을 core `IcpInputSnapshot`으로 완전히 복원합니다."""

    manifest = read_job_manifest(job_directory)
    if str(manifest["job_id"]) != job_directory.resolve(strict=True).name:
        raise IcpJobProtocolError("ICP manifest job_id가 작업 디렉터리 이름과 다릅니다.")
    try:
        from align.alignment.icp_types import ICP_ALGORITHM_VERSION
    except ImportError as error:
        raise IcpJobProtocolError("ICP 알고리즘 버전을 worker에서 불러오지 못했습니다.") from error
    if str(manifest.get("algorithm_version", "")) != ICP_ALGORITHM_VERSION:
        raise IcpJobProtocolError("ICP manifest 알고리즘 버전이 현재 worker와 다릅니다.")
    arrays = _required_mapping_value(manifest, "arrays")
    if not isinstance(arrays, dict):
        raise IcpJobProtocolError("ICP manifest arrays가 객체가 아닙니다.")
    moving_entry = _required_mapping_value(arrays, "moving")
    reference_entries = _required_mapping_value(arrays, "references")
    if not isinstance(moving_entry, dict) or not isinstance(reference_entries, list) or not reference_entries:
        raise IcpJobProtocolError("ICP manifest cloud 배열 항목이 올바르지 않습니다.")
    moving_cloud = _cloud_from_manifest(job_directory, moving_entry)
    reference_clouds = tuple(
        _cloud_from_manifest(job_directory, entry)
        for entry in reference_entries
        if isinstance(entry, dict)
    )
    if len(reference_clouds) != len(reference_entries):
        raise IcpJobProtocolError("ICP manifest reference cloud 항목이 올바르지 않습니다.")
    raw_reference_model_ids = _required_mapping_value(manifest, "reference_model_ids")
    if not isinstance(raw_reference_model_ids, list):
        raise IcpJobProtocolError("ICP manifest reference_model_ids가 목록이 아닙니다.")
    if any(not isinstance(item, str) for item in raw_reference_model_ids):
        raise IcpJobProtocolError("ICP manifest reference_model_ids는 문자열 목록이어야 합니다.")
    reference_model_ids = tuple(item.strip() for item in raw_reference_model_ids)
    if not reference_model_ids or any(not item for item in reference_model_ids):
        raise IcpJobProtocolError("ICP manifest reference_model_ids가 올바르지 않습니다.")
    if reference_model_ids != tuple(cloud.model_id for cloud in reference_clouds):
        raise IcpJobProtocolError("ICP manifest reference model ID와 배열 순서가 다릅니다.")
    revisions_value = _required_mapping_value(manifest, "model_revision_snapshot")
    if not isinstance(revisions_value, list):
        raise IcpJobProtocolError("ICP manifest model revision snapshot이 올바르지 않습니다.")
    revisions_list: list[tuple[str, int]] = []
    for index, item in enumerate(revisions_value):
        if not isinstance(item, list) or len(item) != 2 or not isinstance(item[0], str):
            raise IcpJobProtocolError("ICP manifest model revision 항목이 올바르지 않습니다.")
        model_id = item[0].strip()
        if not model_id:
            raise IcpJobProtocolError("ICP manifest model revision 모델 ID가 비어 있습니다.")
        revisions_list.append(
            (
                model_id,
                _strict_manifest_integer(
                    item[1],
                    field_name=f"ICP manifest model revision[{index}]",
                    minimum=0,
                ),
            )
        )
    revisions = tuple(revisions_list)
    try:
        from align.alignment.icp_types import IcpInputSnapshot
    except ImportError as error:
        raise IcpJobProtocolError("ICP input snapshot 타입을 worker에서 불러오지 못했습니다.") from error
    moving_model_id = _required_mapping_value(manifest, "moving_model_id")
    marker_signature = _required_mapping_value(manifest, "marker_signature")
    moving_cloud_frame = _required_mapping_value(manifest, "moving_cloud_frame")
    if not all(
        isinstance(value, str)
        for value in (moving_model_id, marker_signature, moving_cloud_frame)
    ):
        raise IcpJobProtocolError("ICP manifest 모델·marker·좌표계 식별자는 문자열이어야 합니다.")
    marker_uniform_scale = _strict_manifest_float(
        _required_mapping_value(manifest, "marker_uniform_scale"),
        field_name="ICP marker uniform scale",
        minimum=0.0,
    )
    if marker_uniform_scale <= 0.0:
        raise IcpJobProtocolError("ICP marker uniform scale은 0보다 커야 합니다.")
    snapshot = IcpInputSnapshot(
        moving_model_id=moving_model_id,
        reference_model_ids=reference_model_ids,
        moving_cloud=moving_cloud,
        reference_clouds=reference_clouds,
        initial_alignment=_matrix_from_manifest(
            _required_mapping_value(manifest, "initial_alignment"),
            field_name="ICP 초기 정렬",
        ),
        parameters=_parameters_from_manifest(_required_mapping_value(manifest, "parameters")),
        marker_uniform_scale=marker_uniform_scale,
        marker_max_error=_strict_manifest_float(
            _required_mapping_value(manifest, "marker_max_error"),
            field_name="ICP marker 최대 오차",
            minimum=0.0,
        ),
        marker_rms=_strict_manifest_float(
            _required_mapping_value(manifest, "marker_rms"),
            field_name="ICP marker RMS",
            minimum=0.0,
        ),
        marker_reference_project=_marker_points_from_manifest(
            manifest.get("marker_reference_project"),
            field_name="A",
        ),
        marker_moving_project=_marker_points_from_manifest(
            manifest.get("marker_moving_project"),
            field_name="P",
        ),
        marker_signature=marker_signature,
        model_revision_snapshot=revisions,
        input_fingerprint="",
        algorithm_version=str(_required_mapping_value(manifest, "algorithm_version")),
        moving_cloud_frame=moving_cloud_frame,
    )
    if snapshot.input_fingerprint != str(_required_mapping_value(manifest, "input_fingerprint")):
        raise IcpJobProtocolError("ICP manifest input fingerprint가 복원한 snapshot과 다릅니다.")
    return manifest, snapshot


def _json_value(value: Any) -> Any:
    """core 결과 값을 결과 JSON에 쓸 수 있는 기본 타입으로 재귀 변환합니다."""

    if isinstance(value, Enum):
        return value.value
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, np.generic):
        return value.item()
    if is_dataclass(value):
        return {item.name: _json_value(getattr(value, item.name)) for item in fields(value)}
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise IcpJobProtocolError("ICP 결과에 JSON으로 기록할 수 없는 값이 있습니다.")


def _progress_payload(
    progress: Any,
    *,
    job_id: str,
    input_fingerprint: str,
    moving_model_id: str,
) -> dict[str, Any]:
    """core progress snapshot을 원자 progress.json 형식으로 변환합니다."""

    stage = str(getattr(progress, "stage", "")).strip()
    if stage not in _HYBRID_PROGRESS_STAGES:
        raise IcpJobProtocolError("ICP progress 단계가 Hybrid 프로토콜에 없습니다.")
    iteration = _strict_progress_integer(
        getattr(progress, "iteration", 0),
        field_name="ICP progress iteration",
    )
    max_iterations = _strict_progress_integer(
        getattr(progress, "max_iterations", 0),
        field_name="ICP progress 최대 iteration",
    )
    if max_iterations and iteration > max_iterations:
        raise IcpJobProtocolError("ICP progress iteration이 최대 반복 횟수를 넘었습니다.")
    fraction = _strict_progress_fraction(getattr(progress, "progress", 0.0))
    level_complete = _strict_progress_bool(
        getattr(progress, "level_complete", False),
        field_name="ICP progress level_complete",
    )
    payload = {
        "job_id": job_id,
        "input_fingerprint": input_fingerprint,
        "moving_model_id": moving_model_id,
        "algorithm_version": ICP_ALGORITHM_VERSION,
        "stage": stage,
        "level_name": str(getattr(progress, "level_name", "")).strip(),
        "iteration": iteration,
        "max_iterations": max_iterations,
        "progress": fraction,
        "message": str(getattr(progress, "message", "")).strip(),
        "level_complete": level_complete,
    }
    payload.update(_custom_backend_identity())
    _append_runtime_progress_metrics(progress, payload)
    stable_alignment = getattr(progress, "stable_alignment_float64", None)
    if stable_alignment is not None:
        try:
            matrix = np.asarray(stable_alignment, dtype=np.float64)
        except (OverflowError, TypeError, ValueError) as error:
            raise IcpJobProtocolError("ICP progress stable alignment를 수치 행렬로 변환할 수 없습니다.") from error
        if matrix.shape != (4, 4) or not np.isfinite(matrix).all():
            raise IcpJobProtocolError("ICP progress stable alignment가 올바르지 않습니다.")
        # 검증된 best candidate만 core가 stable_alignment_4x4로 전달한다는 공개 계약을 유지합니다.
        payload["stable_alignment_4x4"] = matrix.tolist()
    return payload


def _strict_progress_integer(value: object, *, field_name: str) -> int:
    """worker progress의 bool·문자열 없는 음이 아닌 정수를 검증합니다."""

    if isinstance(value, (bool, np.bool_)) or not isinstance(value, (int, np.integer)):
        raise IcpJobProtocolError(f"{field_name}은(는) bool이 아닌 정수여야 합니다.")
    try:
        result = int(value)
    except (OverflowError, TypeError, ValueError) as error:
        raise IcpJobProtocolError(f"{field_name}을(를) 정수로 변환할 수 없습니다.") from error
    if result < 0 or result > _MAX_PROGRESS_INTEGER:
        raise IcpJobProtocolError(f"{field_name}은(는) 0 이상 범위의 정수여야 합니다.")
    return result


def _strict_progress_fraction(value: object) -> float:
    """worker progress의 유한한 0 이상 1 이하 진행률을 검증합니다."""

    if isinstance(value, (bool, np.bool_)) or not isinstance(
        value,
        (int, float, np.integer, np.floating),
    ):
        raise IcpJobProtocolError("ICP progress는 숫자여야 합니다.")
    try:
        result = float(value)
    except (OverflowError, TypeError, ValueError) as error:
        raise IcpJobProtocolError("ICP progress를 수치로 변환할 수 없습니다.") from error
    if not np.isfinite(result) or not 0.0 <= result <= 1.0:
        raise IcpJobProtocolError("ICP progress는 유한한 0 이상 1 이하여야 합니다.")
    return result


def _strict_progress_bool(value: object, *, field_name: str) -> bool:
    """worker progress의 JSON bool 필드를 문자열 변환 없이 검증합니다."""

    if not isinstance(value, (bool, np.bool_)):
        raise IcpJobProtocolError(f"{field_name}은(는) bool이어야 합니다.")
    return bool(value)


def _optional_progress_metric_float(value: object, *, field_name: str) -> float | None:
    """선택 progress 실수를 유한한 0 이상 값으로 검증합니다."""

    if value is None:
        return None
    if isinstance(value, (bool, np.bool_)) or not isinstance(
        value,
        (int, float, np.integer, np.floating),
    ):
        raise IcpJobProtocolError(f"{field_name}은(는) 숫자 또는 null이어야 합니다.")
    try:
        normalized = float(value)
    except (OverflowError, TypeError, ValueError) as error:
        raise IcpJobProtocolError(f"{field_name}을(를) 수치로 변환할 수 없습니다.") from error
    if not np.isfinite(normalized) or normalized < 0.0:
        raise IcpJobProtocolError(f"{field_name}은(는) 유한한 0 이상 값이어야 합니다.")
    return normalized


def _optional_progress_metric_integer(value: object, *, field_name: str) -> int | None:
    """선택 progress 개수를 bool·문자열 없이 0 이상 정수로 검증합니다."""

    if value is None:
        return None
    return _strict_progress_integer(value, field_name=field_name)


def _optional_progress_objective(value: object) -> str | None:
    """core 목적함수를 IPC 공개 문자열로 엄격하게 정규화합니다."""

    if value is None:
        return None
    normalized = str(getattr(value, "value", value)).strip().upper()
    if normalized not in {"POINT_TO_POINT", "POINT_TO_PLANE"}:
        raise IcpJobProtocolError("ICP progress 목적함수가 올바르지 않습니다.")
    return normalized


def _append_runtime_progress_metrics(progress: Any, payload: dict[str, Any]) -> None:
    """core iteration 지표를 계획서의 단일 IPC 필드명으로 기록합니다."""

    objective_kind = _optional_progress_objective(
        getattr(progress, "objective_kind", None)
    )
    if objective_kind is not None:
        payload["objective_kind"] = objective_kind
    optional_float_fields = (
        (
            "maximum_correspondence_distance",
            "maximum_correspondence_distance",
            "ICP progress 대응 반경",
        ),
        ("point_to_point_rmse", "point_to_point_rmse", "ICP progress point RMSE"),
        ("point_to_plane_rmse", "point_to_plane_rmse", "ICP progress plane RMSE"),
        ("normal_radius", "normal_radius", "ICP progress 실제 노멀 반경"),
    )
    for payload_name, source_name, field_name in optional_float_fields:
        value = _optional_progress_metric_float(
            getattr(progress, source_name, None),
            field_name=field_name,
        )
        if value is not None:
            payload[payload_name] = value
    optional_integer_fields = (
        ("correspondence_count", "correspondence_count", "ICP progress 대응점 수"),
        ("normal_valid_count", "normal_valid_count", "ICP progress 유효 노멀 수"),
        (
            "normal_angle_pass_count",
            "normal_angle_pass_count",
            "ICP progress 노멀 각도 통과 수",
        ),
        ("reference_sample_count", "reference_sample_count", "ICP progress 기준 표본 수"),
        ("moving_sample_count", "moving_sample_count", "ICP progress 이동 표본 수"),
    )
    for payload_name, source_name, field_name in optional_integer_fields:
        value = _optional_progress_metric_integer(
            getattr(progress, source_name, None),
            field_name=field_name,
        )
        if value is not None:
            payload[payload_name] = value
    normal_valid_count = payload.get("normal_valid_count")
    angle_pass_count = payload.get("normal_angle_pass_count")
    if (
        normal_valid_count is not None
        and angle_pass_count is not None
        and angle_pass_count > normal_valid_count
    ):
        raise IcpJobProtocolError("ICP progress 노멀 각도 통과 수가 유효 노멀 수를 넘습니다.")


def _result_metadata(result: Any, manifest: Mapping[str, Any]) -> dict[str, Any]:
    """core 결과의 행렬 외 메타데이터를 main 검증용 JSON으로 만듭니다."""

    if not hasattr(result, "to_mapping"):
        raise IcpJobProtocolError("ICP solver 결과에 직렬화 API가 없습니다.")
    serialized = result.to_mapping()
    if not isinstance(serialized, dict):
        raise IcpJobProtocolError("ICP solver 결과 직렬화 형식이 올바르지 않습니다.")
    metadata = dict(serialized)
    matrix_keys = (
        "initial_alignment_4x4",
        "final_alignment_4x4",
        "icp_delta_4x4",
    )
    if any(key not in metadata for key in matrix_keys):
        raise IcpJobProtocolError("ICP solver 결과에 필수 변환 행렬이 없습니다.")
    for key in matrix_keys:
        metadata.pop(key)
    metadata["job_id"] = str(manifest["job_id"])
    metadata["model_state_snapshot"] = list(manifest.get("model_state_snapshot", ()))
    metadata.update(_custom_backend_identity())
    required = (
        "input_fingerprint",
        "parameter_fingerprint",
        "marker_signature",
        "moving_model_id",
        "algorithm_version",
    )
    if any(not str(metadata[name]).strip() for name in required):
        raise IcpJobProtocolError("ICP 결과 식별자 메타데이터가 비어 있습니다.")
    if metadata["input_fingerprint"] != str(manifest["input_fingerprint"]):
        raise IcpJobProtocolError("ICP 결과 input fingerprint가 manifest와 다릅니다.")
    if metadata["marker_signature"] != str(manifest["marker_signature"]):
        raise IcpJobProtocolError("ICP 결과 marker signature가 manifest와 다릅니다.")
    if metadata["moving_model_id"] != str(manifest["moving_model_id"]):
        raise IcpJobProtocolError("ICP 결과 moving model ID가 manifest와 다릅니다.")
    if metadata["algorithm_version"] != ICP_ALGORITHM_VERSION:
        raise IcpJobProtocolError("ICP 결과 알고리즘 버전이 Hybrid worker와 다릅니다.")
    if metadata["algorithm_version"] != str(manifest["algorithm_version"]):
        raise IcpJobProtocolError("ICP 결과 알고리즘 버전이 manifest와 다릅니다.")
    if metadata.get("model_revision_snapshot") != manifest.get("model_revision_snapshot"):
        raise IcpJobProtocolError("ICP 결과 모델 revision snapshot이 manifest와 다릅니다.")
    expected_parameters = _parameters_from_manifest(manifest.get("parameters"))
    if metadata["parameter_fingerprint"] != expected_parameters.fingerprint:
        raise IcpJobProtocolError("ICP 결과 parameter fingerprint가 manifest와 다릅니다.")
    validate_hybrid_result_metadata(metadata)
    return metadata


def _terminal_stage_for_result(result: Any) -> tuple[str, str]:
    """유효한 PASS·REVIEW 결과를 각 terminal progress 단계와 안내문으로 변환합니다."""

    quality = getattr(result, "quality", None)
    status = str(getattr(getattr(quality, "status", None), "value", getattr(quality, "status", ""))).upper()
    if status == "PASS":
        return ICP_STAGE_COMPLETED_PASS, "ICP 정렬을 완료했습니다."
    if status == "REVIEW":
        return (
            ICP_STAGE_COMPLETED_REVIEW,
            "ICP 개선이 없거나 미미합니다. 결과를 비교한 뒤 승인 여부를 결정하십시오.",
        )
    raise IcpJobProtocolError("Hybrid ICP 결과 품질은 PASS 또는 REVIEW여야 합니다.")


def _cancel_probe(job_directory: Path) -> bool:
    """cancel sentinel 존재 여부를 worker solver가 반복 경계에서 조회하게 합니다."""

    return resolve_job_path(job_directory, "cancel.request").is_file()


def _quality_rejection_payload(error: Exception, manifest: Mapping[str, Any]) -> dict[str, Any]:
    """품질 거부를 계산 crash와 구분하는 구조화 worker 오류 payload를 만듭니다."""

    quality = getattr(error, "quality", None)
    quality_mapping = quality.to_mapping() if hasattr(quality, "to_mapping") else None
    reasons = getattr(error, "rejection_reasons", ())
    if not reasons:
        reasons = getattr(error, "review_reasons", ())
    if not reasons and isinstance(quality_mapping, dict):
        reasons = quality_mapping.get("rejection_reasons", ())
    if not reasons and isinstance(quality_mapping, dict):
        reasons = quality_mapping.get("review_reasons", ())
    normalized_reasons = [str(reason).strip() for reason in reasons if str(reason).strip()]
    payload: dict[str, Any] = {
        "job_id": str(manifest["job_id"]),
        "input_fingerprint": str(manifest["input_fingerprint"]),
        "status": "QUALITY_REJECTED",
        "message": str(error) or "ICP 품질 기준을 충족하지 못했습니다.",
        # rejection_reasons는 품질 거부 전용 계약이고 review_reasons는 구버전 호환용입니다.
        "rejection_reasons": normalized_reasons,
        "review_reasons": normalized_reasons,
    }
    payload.update(_custom_backend_identity())
    if isinstance(quality_mapping, dict):
        payload["quality"] = quality_mapping
    return payload


def run_worker(job_directory: Path) -> None:
    """한 worker job의 입력 복원·ICP 계산·원자 결과 기록을 순서대로 수행합니다."""

    try:
        from align.optional_backends.hybrid.solver import solve_multiscale_icp
        from align.alignment.icp_types import (
            IcpCancelledError,
            IcpQualityRejectedError,
        )
    except ImportError as error:
        raise IcpJobProtocolError("ICP 수치 solver를 worker에서 불러오지 못했습니다.") from error
    manifest, snapshot = input_snapshot_from_manifest(job_directory)
    started_at = monotonic()

    def progress_sink(progress: Any) -> None:
        """solver 진행 이벤트를 progress.json에 원자 기록합니다."""

        write_json_atomically(
            resolve_job_path(job_directory, "progress.json"),
            _progress_payload(
                progress,
                job_id=str(manifest["job_id"]),
                input_fingerprint=str(manifest["input_fingerprint"]),
                moving_model_id=str(manifest["moving_model_id"]),
            ),
        )

    progress_sink(
        type(
            "PrepareProgress",
            (),
            {
                "stage": ICP_STAGE_PREPARING_INPUT,
                "level_name": "",
                "iteration": 0,
                "max_iterations": 0,
                "progress": 0.0,
                "message": "ICP 입력을 검증했습니다.",
                "level_complete": False,
                "stable_alignment_float64": None,
            },
        )()
    )
    try:
        result = solve_multiscale_icp(
            snapshot,
            progress_sink=progress_sink,
            cancel_probe=lambda: _cancel_probe(job_directory),
        )
    except IcpCancelledError:
        write_json_atomically(
            resolve_job_path(job_directory, "error.json"),
            {
                "job_id": str(manifest["job_id"]),
                "input_fingerprint": str(manifest["input_fingerprint"]),
                "status": "CANCELLED",
                "message": "ICP 작업을 취소했습니다.",
            },
        )
        progress_sink(
            type(
                "CancelledProgress",
                (),
                {
                    "stage": ICP_STAGE_CANCELLED,
                    "level_name": "",
                    "iteration": 0,
                    "max_iterations": 0,
                    "progress": 1.0,
                    "message": "ICP 작업을 취소했습니다.",
                    "level_complete": False,
                    "stable_alignment_float64": None,
                },
            )()
        )
        return
    except IcpQualityRejectedError as error:
        write_json_atomically(
            resolve_job_path(job_directory, "error.json"),
            _quality_rejection_payload(error, manifest),
        )
        progress_sink(
            type(
                "QualityRejectedProgress",
                (),
                {
                    "stage": ICP_STAGE_FAILED,
                    "level_name": "",
                    "iteration": 0,
                    "max_iterations": 0,
                    "progress": 1.0,
                    "message": "ICP 품질 기준을 충족하지 못했습니다.",
                    "level_complete": False,
                    "stable_alignment_float64": None,
                },
            )()
        )
        return
    metadata = _result_metadata(result, manifest)
    metadata["elapsed_seconds"] = monotonic() - started_at
    initial_alignment = getattr(result, "initial_alignment", None)
    final_alignment = getattr(result, "final_alignment", None)
    icp_delta = getattr(result, "icp_delta", None)
    if not all(
        isinstance(alignment, Sim3Transform)
        for alignment in (initial_alignment, final_alignment, icp_delta)
    ):
        raise IcpJobProtocolError("ICP 결과 transform 형식이 올바르지 않습니다.")
    if not np.allclose(
        initial_alignment.matrix_float64,
        snapshot.initial_alignment.matrix_float64,
        rtol=0.0,
        atol=1.0e-8,
    ):
        raise IcpJobProtocolError("ICP 결과 초기 alignment가 worker 입력과 다릅니다.")
    terminal_stage, terminal_message = _terminal_stage_for_result(result)
    progress_sink(
        type(
            "EvaluateBestCandidateProgress",
            (),
            {
                "stage": ICP_STAGE_EVALUATING_BEST_CANDIDATE,
                "level_name": "",
                "iteration": 0,
                "max_iterations": 0,
                "progress": 0.99,
                "message": "최선 ICP 후보를 평가하고 결과를 기록합니다.",
                "level_complete": True,
                "stable_alignment_float64": final_alignment.matrix_float64,
            },
        )()
    )
    write_result_arrays_atomically(
        resolve_job_path(job_directory, "result.npz"),
        {
            "initial_alignment": initial_alignment.matrix_float64,
            "final_alignment": final_alignment.matrix_float64,
            "icp_delta": icp_delta.matrix_float64,
        },
    )
    write_json_atomically(resolve_job_path(job_directory, "result.json"), metadata)
    progress_sink(
        type(
            "CompletedProgress",
            (),
            {
                "stage": terminal_stage,
                "level_name": "",
                "iteration": 0,
                "max_iterations": 0,
                "progress": 1.0,
                "message": terminal_message,
                "level_complete": True,
                "stable_alignment_float64": final_alignment.matrix_float64,
            },
        )()
    )


def _write_worker_failure(job_directory: Path | None, error: Exception) -> None:
    """확보된 작업 폴더가 있을 때만 worker 실패를 원자 error.json에 남깁니다."""

    if job_directory is None:
        return
    try:
        manifest = read_job_manifest(job_directory)
        payload = {
            "job_id": str(manifest["job_id"]),
            "input_fingerprint": str(manifest["input_fingerprint"]),
            "status": "FAILED",
            "error_type": type(error).__name__,
            "message": str(error),
        }
        payload.update(_custom_backend_identity())
        write_json_atomically(
            resolve_job_path(job_directory, "error.json"),
            payload,
        )
    except (IcpJobProtocolError, OSError, ValueError):
        return


def main(arguments: list[str] | None = None) -> int:
    """worker 인자를 해석하고 오류를 구조화 파일과 로그에 남긴 뒤 종료 코드를 반환합니다."""

    configure_logging()
    job_directory: Path | None = None
    try:
        namespace = parse_worker_arguments(arguments)
        job_directory = Path(namespace.job_dir).resolve(strict=True)
        if not job_directory.is_dir():
            raise IcpJobProtocolError("ICP worker 작업 경로가 디렉터리가 아닙니다.")
        run_worker(job_directory)
    except Exception as error:
        _write_worker_failure(job_directory, error)
        _LOGGER.exception("event=icp_worker_failed reason=%s", error)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
