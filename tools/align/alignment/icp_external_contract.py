"""small_gicp와 선택형 외부 ICP가 공유하는 전체점 입력·결과 계약입니다.

이 모듈은 Open3D를 import하거나 Open3D 수치 계산을 실행하지 않습니다.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from math import atan2, degrees, isfinite
from typing import Any, Callable, Mapping, Sequence

import numpy as np

OPEN3D_GICP_BACKEND_ID = "OPEN3D_GICP"

OPEN3D_ROBUST_POINT_TO_PLANE_BACKEND_ID = "OPEN3D_ROBUST_POINT_TO_PLANE"

SMALL_GICP_BACKEND_ID = "SMALL_GICP"

OPEN3D_JOB_PROTOCOL_VERSION = "icp-job-protocol-v1"

OPEN3D_GICP_ALGORITHM_VERSION = "open3d-gicp-v1"

OPEN3D_ROBUST_POINT_TO_PLANE_ALGORITHM_VERSION = "open3d-robust-point-to-plane-v1"

SMALL_GICP_ALGORITHM_VERSION = "small-gicp-v6"

FULL_INPUT_POLICY = "FULL_OVERLAP_NO_DOWNSAMPLING"

CancelProbe = Callable[[], bool] | None

class Open3dIcpError(RuntimeError):
    """Open3D 독립 solver 경계의 기본 오류입니다."""

class Open3dIcpInputError(Open3dIcpError):
    """입력 배열·manifest 정책·행렬 계약이 잘못됐을 때 발생합니다."""

class Open3dIcpEnvironmentError(Open3dIcpError):
    """요구한 Open3D legacy API 또는 버전을 찾지 못했을 때 발생합니다."""

class Open3dIcpCancelled(Open3dIcpError):
    """native batch 사이에서 취소 sentinel이 확인됐을 때 발생합니다."""

class Open3dIcpNumericalError(Open3dIcpError):
    """Open3D 결과가 rigid·안전 이동 수치 계약을 벗어났을 때 발생합니다."""

@dataclass(frozen=True, slots=True)
class Sim3MatrixDecomposition:
    """검증된 Sim(3) 행렬의 공통 scale·회전·이동 진단값입니다."""

    uniform_scale: float
    rotation_float64: np.ndarray
    translation_float64: np.ndarray
    rotation_degrees: float

@dataclass(frozen=True, slots=True)
class ExternalIcpDeltaDiagnostics:
    """최종 Project World ICP delta에서 복원한 공통 local-frame 진단값입니다."""

    uniform_scale: float
    rotation_float64: np.ndarray
    project_translation_float64: np.ndarray
    local_origin_float64: np.ndarray
    local_translation_float64: np.ndarray
    translation_distance: float
    rotation_degrees: float

def _canonical_fingerprint(value: object) -> str:
    """작은 JSON 값에 대한 결정적 BLAKE2b 지문을 반환합니다."""

    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise Open3dIcpInputError("Open3D JSON 지문 입력을 직렬화하지 못했습니다.") from error
    return hashlib.blake2b(encoded, digest_size=20).hexdigest()

def _array_fingerprint(array: np.ndarray) -> str:
    """대형 C 배열을 복사하지 않고 일정 chunk로 해시합니다."""

    if not isinstance(array, np.ndarray) or not array.flags.c_contiguous:
        raise Open3dIcpInputError("Open3D 배열 지문 입력은 C-contiguous ndarray여야 합니다.")
    digest = hashlib.blake2b(digest_size=20)
    digest.update(array.dtype.str.encode("ascii"))
    digest.update(repr(array.shape).encode("ascii"))
    try:
        bytes_view = memoryview(array).cast("B")
    except TypeError as error:
        raise Open3dIcpInputError("Open3D 배열 byte view를 만들지 못했습니다.") from error
    chunk_size = 1_048_576
    for start in range(0, len(bytes_view), chunk_size):
        digest.update(bytes_view[start : start + chunk_size])
    return digest.hexdigest()

def _strict_text(value: object, field_name: str) -> str:
    """비어 있지 않은 식별자 문자열만 반환합니다."""

    normalized = str(value).strip()
    if not normalized:
        raise Open3dIcpInputError(f"{field_name}이(가) 비어 있습니다.")
    return normalized

def _strict_positive_int(value: object, field_name: str) -> int:
    """bool·실수·문자열을 허용하지 않는 양의 정수를 반환합니다."""

    if isinstance(value, (bool, np.bool_)) or not isinstance(value, (int, np.integer)):
        raise Open3dIcpInputError(f"{field_name}은(는) 양의 정수여야 합니다.")
    normalized = int(value)
    if normalized < 1:
        raise Open3dIcpInputError(f"{field_name}은(는) 1 이상이어야 합니다.")
    return normalized

def _strict_finite_float(
    value: object,
    field_name: str,
    *,
    minimum: float | None = None,
) -> float:
    """bool·문자열을 허용하지 않는 유한 실수를 반환합니다."""

    if isinstance(value, (bool, np.bool_)) or not isinstance(
        value,
        (int, float, np.integer, np.floating),
    ):
        raise Open3dIcpInputError(f"{field_name}은(는) 유한 실수여야 합니다.")
    normalized = float(value)
    if not isfinite(normalized) or (minimum is not None and normalized < minimum):
        raise Open3dIcpInputError(f"{field_name} 범위가 올바르지 않습니다.")
    return normalized

def _float64_positions(value: object, field_name: str) -> np.ndarray:
    """유한한 C-contiguous float64 N×3 좌표를 검증합니다."""

    if not isinstance(value, np.ndarray) or value.dtype != np.dtype(np.float64):
        raise Open3dIcpInputError(f"{field_name} 좌표는 float64 ndarray여야 합니다.")
    positions = value if value.flags.c_contiguous else np.ascontiguousarray(value)
    if positions.ndim != 2 or positions.shape[1] != 3 or positions.shape[0] < 1:
        raise Open3dIcpInputError(f"{field_name} 좌표 형상이 올바르지 않습니다.")
    if not np.isfinite(positions).all():
        raise Open3dIcpInputError(f"{field_name} 좌표에 유한하지 않은 값이 있습니다.")
    return positions

def _uint64_ids(value: object, expected_count: int, field_name: str) -> np.ndarray:
    """C-contiguous uint64 POINT3D_ID 배열의 길이와 고유성을 검증합니다."""

    if not isinstance(value, np.ndarray) or value.dtype != np.dtype(np.uint64):
        raise Open3dIcpInputError(f"{field_name} POINT3D_ID는 uint64 ndarray여야 합니다.")
    point_ids = value if value.flags.c_contiguous else np.ascontiguousarray(value)
    if point_ids.ndim != 1 or point_ids.shape[0] != expected_count:
        raise Open3dIcpInputError(f"{field_name} POINT3D_ID 길이가 좌표와 다릅니다.")
    if np.unique(point_ids).size != point_ids.size:
        raise Open3dIcpInputError(f"{field_name} POINT3D_ID에 중복이 있습니다.")
    return point_ids

def _matrix4(value: object, field_name: str) -> np.ndarray:
    """유한한 float64 4×4 행렬을 C 배열로 검증합니다."""

    try:
        matrix = np.asarray(value, dtype=np.float64)
    except (TypeError, ValueError, OverflowError) as error:
        raise Open3dIcpInputError(f"{field_name} 행렬을 읽지 못했습니다.") from error
    matrix = np.ascontiguousarray(matrix, dtype=np.float64)
    if matrix.shape != (4, 4) or not np.isfinite(matrix).all():
        raise Open3dIcpInputError(f"{field_name} 행렬은 유한한 4×4여야 합니다.")
    if not np.allclose(matrix[3], np.array((0.0, 0.0, 0.0, 1.0)), rtol=0.0, atol=1.0e-10):
        raise Open3dIcpInputError(f"{field_name} 행렬 마지막 행이 동차 좌표 계약과 다릅니다.")
    return matrix

def _uniform_scale_and_rotation(matrix: np.ndarray, field_name: str) -> tuple[float, np.ndarray]:
    """proper Sim(3) 3×3 블록의 균일 scale과 회전을 검증합니다."""

    linear = matrix[:3, :3]
    try:
        singular_values = np.linalg.svd(linear, compute_uv=False)
    except np.linalg.LinAlgError as error:
        raise Open3dIcpNumericalError(f"{field_name} 선형 블록 SVD에 실패했습니다.") from error
    scale = float(np.mean(singular_values))
    tolerance = 1.0e-8 * max(1.0, scale)
    if scale <= 0.0 or not np.allclose(singular_values, scale, rtol=0.0, atol=tolerance):
        raise Open3dIcpNumericalError(f"{field_name}에 균일하지 않은 scale이 있습니다.")
    rotation = linear / scale
    if not np.allclose(rotation.T @ rotation, np.identity(3), rtol=0.0, atol=1.0e-8):
        raise Open3dIcpNumericalError(f"{field_name} 회전이 직교 행렬이 아닙니다.")
    determinant = float(np.linalg.det(rotation))
    if not np.isclose(determinant, 1.0, rtol=0.0, atol=1.0e-8):
        raise Open3dIcpNumericalError(f"{field_name}에 반사 또는 잘못된 회전이 있습니다.")
    return scale, rotation

def _validate_rigid_matrix(matrix: np.ndarray, field_name: str) -> np.ndarray:
    """scale·반사 없는 SE(3) 행렬만 허용합니다."""

    normalized = _matrix4(matrix, field_name)
    scale, _rotation = _uniform_scale_and_rotation(normalized, field_name)
    if not np.isclose(scale, 1.0, rtol=0.0, atol=1.0e-8):
        raise Open3dIcpNumericalError(f"{field_name} delta에 scale 변화가 있습니다.")
    return normalized

def _rotation_angle_degrees(rotation: np.ndarray) -> float:
    """proper rotation의 0~180도 최소 회전각을 안정적인 atan2로 계산합니다."""

    sine_vector = 0.5 * np.array(
        (
            rotation[2, 1] - rotation[1, 2],
            rotation[0, 2] - rotation[2, 0],
            rotation[1, 0] - rotation[0, 1],
        ),
        dtype=np.float64,
    )
    sine = float(np.linalg.norm(sine_vector))
    cosine = float(np.clip((np.trace(rotation) - 1.0) * 0.5, -1.0, 1.0))
    return float(degrees(atan2(sine, cosine)))

def decompose_sim3_matrix(value: object, *, field_name: str) -> Sim3MatrixDecomposition:
    """Sim(3) 행렬을 공통 SVD 경로로 검증·분해하고 회전각을 계산합니다."""

    matrix = _matrix4(value, field_name)
    scale, rotation = _uniform_scale_and_rotation(matrix, field_name)
    return Sim3MatrixDecomposition(
        uniform_scale=scale,
        rotation_float64=np.ascontiguousarray(rotation, dtype=np.float64),
        translation_float64=np.ascontiguousarray(matrix[:3, 3], dtype=np.float64),
        rotation_degrees=_rotation_angle_degrees(rotation),
    )

def analyze_external_icp_delta(
    value: object,
    local_origin: object,
    *,
    field_name: str = "외부 ICP delta",
) -> ExternalIcpDeltaDiagnostics:
    """최종 Project World delta 하나에서 scale·회전·local 이동량을 함께 계산합니다."""

    try:
        origin = np.asarray(local_origin, dtype=np.float64)
    except (OverflowError, TypeError, ValueError) as error:
        raise Open3dIcpInputError(f"{field_name} local origin을 읽지 못했습니다.") from error
    origin = np.ascontiguousarray(origin, dtype=np.float64)
    if origin.shape != (3,) or not np.isfinite(origin).all():
        raise Open3dIcpInputError(f"{field_name} local origin은 유한한 3차원 벡터여야 합니다.")
    decomposition = decompose_sim3_matrix(value, field_name=field_name)
    local_translation = (
        decomposition.translation_float64
        + decomposition.uniform_scale * (decomposition.rotation_float64 @ origin)
        - origin
    )
    return ExternalIcpDeltaDiagnostics(
        uniform_scale=decomposition.uniform_scale,
        rotation_float64=decomposition.rotation_float64,
        project_translation_float64=decomposition.translation_float64,
        local_origin_float64=origin,
        local_translation_float64=np.ascontiguousarray(local_translation, dtype=np.float64),
        translation_distance=float(np.linalg.norm(local_translation)),
        rotation_degrees=decomposition.rotation_degrees,
    )

def _inverse_rigid(matrix: np.ndarray) -> np.ndarray:
    """검증된 rigid 행렬의 수치적으로 단순한 역행렬을 만듭니다."""

    inverse = np.identity(4, dtype=np.float64)
    rotation = matrix[:3, :3]
    inverse[:3, :3] = rotation.T
    inverse[:3, 3] = -(rotation.T @ matrix[:3, 3])
    return inverse

def _local_delta_to_world(delta_local: np.ndarray, local_origin: np.ndarray) -> np.ndarray:
    """local-origin Open3D rigid delta를 Project World delta로 되돌립니다."""

    delta = _validate_rigid_matrix(delta_local, "Open3D local delta")
    decomposition = decompose_sim3_matrix(delta, field_name="Open3D local delta")
    world = np.array(delta, dtype=np.float64, order="C", copy=True)
    world[:3, 3] = (
        decomposition.translation_float64
        + local_origin
        - decomposition.uniform_scale * (decomposition.rotation_float64 @ local_origin)
    )
    return _validate_rigid_matrix(world, "Open3D Project World delta")

def _transform_positions(positions: np.ndarray, transform: np.ndarray) -> np.ndarray:
    """column-vector 4×4 계약으로 N×3 좌표에 변환을 적용합니다."""

    return np.ascontiguousarray(
        positions @ transform[:3, :3].T + transform[:3, 3],
        dtype=np.float64,
    )

def _bounds(positions: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """유한 점군의 정확한 AABB 최소·최대를 반환합니다."""

    return (
        np.min(positions, axis=0).astype(np.float64, copy=False),
        np.max(positions, axis=0).astype(np.float64, copy=False),
    )

def _diagonal(minimum: np.ndarray, maximum: np.ndarray) -> float:
    """AABB 대각선 길이를 유한 양수로 검증해 반환합니다."""

    value = float(np.linalg.norm(maximum - minimum))
    if not isfinite(value) or value <= np.finfo(np.float64).eps:
        raise Open3dIcpInputError("Open3D 점군 AABB 대각선이 양수가 아닙니다.")
    return value

def _nominal_spacing(positions: np.ndarray) -> float:
    """전체점 AABB와 점 수에서 통계용 대표 간격을 계산합니다."""

    minimum, maximum = _bounds(positions)
    diagonal = _diagonal(minimum, maximum)
    spacing = diagonal / max(float(positions.shape[0]) ** (1.0 / 3.0), 1.0)
    if not isfinite(spacing) or spacing <= 0.0:
        raise Open3dIcpInputError("Open3D 전체점 대표 간격을 계산하지 못했습니다.")
    return spacing

def _raise_if_cancelled(cancel_probe: CancelProbe) -> None:
    """Open3D native 호출 직전·직후의 취소 요청을 확인합니다."""

    if cancel_probe is not None and bool(cancel_probe()):
        raise Open3dIcpCancelled("사용자가 Open3D ICP 작업 취소를 요청했습니다.")

@dataclass(frozen=True, slots=True)
class Open3dPointCloud:
    """한 P 또는 A 모델의 원본 Project World 전체점 입력을 보관합니다."""

    model_id: str
    positions_float64: np.ndarray
    point_ids_uint64: np.ndarray
    confidence_weights_float64: np.ndarray | None = None
    isolation_candidates_bool: np.ndarray | None = None

    def __post_init__(self) -> None:
        """점군 ID·좌표·POINT3D_ID·선택 평가 신뢰도의 불변식을 검증합니다."""

        model_id = _strict_text(self.model_id, "Open3D 점군 모델 ID")
        positions = _float64_positions(self.positions_float64, model_id)
        point_ids = _uint64_ids(self.point_ids_uint64, positions.shape[0], model_id)
        confidence_weights = self.confidence_weights_float64
        isolation_candidates = self.isolation_candidates_bool
        if (confidence_weights is None) != (isolation_candidates is None):
            raise Open3dIcpInputError("점 분포 신뢰도와 고립 후보 배열은 함께 있어야 합니다.")
        if confidence_weights is not None and isolation_candidates is not None:
            confidence_weights = np.ascontiguousarray(confidence_weights, dtype=np.float64)
            isolation_candidates = np.ascontiguousarray(isolation_candidates, dtype=np.bool_)
            if (
                confidence_weights.shape != (positions.shape[0],)
                or isolation_candidates.shape != (positions.shape[0],)
                or not np.isfinite(confidence_weights).all()
                or np.any(confidence_weights <= 0.0)
                or np.any(confidence_weights > 1.0)
            ):
                raise Open3dIcpInputError("점 분포 신뢰도 또는 고립 후보 배열이 올바르지 않습니다.")
            confidence_weights.setflags(write=False)
            isolation_candidates.setflags(write=False)
        object.__setattr__(self, "model_id", model_id)
        object.__setattr__(self, "positions_float64", positions)
        object.__setattr__(self, "point_ids_uint64", point_ids)
        object.__setattr__(self, "confidence_weights_float64", confidence_weights)
        object.__setattr__(self, "isolation_candidates_bool", isolation_candidates)

    @property
    def count(self) -> int:
        """점군의 전체 원본 점 수를 반환합니다."""

        return int(self.positions_float64.shape[0])

    @property
    def positions_fingerprint(self) -> str:
        """원본 좌표 배열의 결정적 지문을 반환합니다."""

        return _array_fingerprint(self.positions_float64)

    @property
    def point_ids_fingerprint(self) -> str:
        """원본 POINT3D_ID 배열의 결정적 지문을 반환합니다."""

        return _array_fingerprint(self.point_ids_uint64)

@dataclass(frozen=True, slots=True)
class Open3dSolverInput:
    """Open3D worker가 Hybrid 타입 없이 소비하는 독립 입력 계약입니다."""

    job_id: str
    input_fingerprint: str
    algorithm_version: str
    backend_id: str
    backend_algorithm_version: str
    job_protocol_version: str
    moving_cloud_frame: str
    source: Open3dPointCloud
    references: tuple[Open3dPointCloud, ...]
    initial_alignment: np.ndarray
    marker_uniform_scale: float
    marker_max_error: float
    parameters: Mapping[str, Any]
    marker_signature: str = ""
    model_revision_snapshot: Sequence[object] = ()
    model_state_snapshot: Sequence[object] = ()
    marker_reference_project: np.ndarray | None = None
    marker_moving_project: np.ndarray | None = None
    confidence_preprocessing: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        """Project World·backend scale·Small GICP 평가 신뢰도를 검증합니다."""

        job_id = _strict_text(self.job_id, "Open3D job_id")
        input_fingerprint = _strict_text(self.input_fingerprint, "Open3D input fingerprint")
        algorithm_version = _strict_text(self.algorithm_version, "Open3D algorithm_version")
        backend_id = _strict_text(self.backend_id, "Open3D backend ID").upper()
        algorithm = _strict_text(self.backend_algorithm_version, "Open3D backend algorithm version")
        protocol = _strict_text(self.job_protocol_version, "Open3D job protocol version")
        if protocol != OPEN3D_JOB_PROTOCOL_VERSION:
            raise Open3dIcpInputError("지원하지 않는 Open3D job protocol 버전입니다.")
        expected_algorithms = {
            OPEN3D_GICP_BACKEND_ID: OPEN3D_GICP_ALGORITHM_VERSION,
            OPEN3D_ROBUST_POINT_TO_PLANE_BACKEND_ID: OPEN3D_ROBUST_POINT_TO_PLANE_ALGORITHM_VERSION,
            SMALL_GICP_BACKEND_ID: SMALL_GICP_ALGORITHM_VERSION,
        }
        if backend_id not in expected_algorithms:
            raise Open3dIcpInputError("지원하지 않는 Open3D backend ID입니다.")
        if algorithm != expected_algorithms[backend_id]:
            raise Open3dIcpInputError("Open3D backend 알고리즘 버전이 backend ID와 다릅니다.")
        frame = _strict_text(self.moving_cloud_frame, "Open3D moving cloud frame").upper()
        if frame != "INITIAL_PROJECT_WORLD":
            raise Open3dIcpInputError("Open3D source는 marker 적용 INITIAL_PROJECT_WORLD여야 합니다.")
        if not isinstance(self.source, Open3dPointCloud):
            raise Open3dIcpInputError("Open3D source 점군 형식이 올바르지 않습니다.")
        references = tuple(self.references)
        if not references or not all(isinstance(item, Open3dPointCloud) for item in references):
            raise Open3dIcpInputError("Open3D target A 점군이 하나 이상 필요합니다.")
        reference_ids = tuple(item.model_id for item in references)
        if len(set(reference_ids)) != len(reference_ids) or self.source.model_id in reference_ids:
            raise Open3dIcpInputError("Open3D P/A 모델 ID가 중복되었거나 섞였습니다.")
        initial_alignment = _matrix4(self.initial_alignment, "Open3D marker initial alignment")
        marker_scale, _marker_rotation = _uniform_scale_and_rotation(
            initial_alignment,
            "Open3D marker initial alignment",
        )
        marker_uniform_scale = _strict_finite_float(
            self.marker_uniform_scale,
            "Open3D marker uniform scale",
            minimum=np.finfo(np.float64).eps,
        )
        if not np.isclose(marker_scale, marker_uniform_scale, rtol=0.0, atol=1.0e-9 * max(1.0, marker_scale)):
            raise Open3dIcpInputError("Open3D marker 행렬 scale과 manifest scale이 다릅니다.")
        marker_max_error = _strict_finite_float(
            self.marker_max_error,
            "Open3D marker 최대 오차",
            minimum=0.0,
        )
        if not isinstance(self.parameters, Mapping):
            raise Open3dIcpInputError("Open3D parameters는 JSON 객체여야 합니다.")
        parameters = dict(self.parameters)
        scale_mode = str(parameters.get("scale_mode", "")).strip().upper()
        scale_refine_enabled = parameters.get("scale_refine_enabled")
        if scale_mode not in {"LOCKED", "REFINE"} or not isinstance(
            scale_refine_enabled,
            bool,
        ):
            raise Open3dIcpInputError("외부 ICP scale 설정 형식이 올바르지 않습니다.")
        if scale_refine_enabled is not (scale_mode == "REFINE"):
            raise Open3dIcpInputError("외부 ICP scale mode와 보정 활성값이 일치하지 않습니다.")
        if backend_id != SMALL_GICP_BACKEND_ID and scale_refine_enabled:
            raise Open3dIcpInputError("Open3D backend에서는 Scale Refine를 사용할 수 없습니다.")
        if backend_id == SMALL_GICP_BACKEND_ID and (
            self.source.confidence_weights_float64 is None
            or any(item.confidence_weights_float64 is None for item in references)
        ):
            raise Open3dIcpInputError("small_gicp 평가에는 원본 점 분포 신뢰도 입력이 필요합니다.")
        confidence_preprocessing = self.confidence_preprocessing
        if confidence_preprocessing is not None and not isinstance(
            confidence_preprocessing,
            Mapping,
        ):
            raise Open3dIcpInputError("점 분포 신뢰도 전처리 감사 정보가 객체가 아닙니다.")
        reference_markers = self.marker_reference_project
        moving_markers = self.marker_moving_project
        if (reference_markers is None) != (moving_markers is None):
            raise Open3dIcpInputError("Open3D marker 검증점은 A/P가 함께 있어야 합니다.")
        if reference_markers is not None and moving_markers is not None:
            reference_markers = _float64_positions(reference_markers, "Open3D A marker")
            moving_markers = _float64_positions(moving_markers, "Open3D P marker")
            if reference_markers.shape != moving_markers.shape:
                raise Open3dIcpInputError("Open3D A/P marker 배열 형상이 다릅니다.")
        object.__setattr__(self, "job_id", job_id)
        object.__setattr__(self, "input_fingerprint", input_fingerprint)
        object.__setattr__(self, "algorithm_version", algorithm_version)
        object.__setattr__(self, "backend_id", backend_id)
        object.__setattr__(self, "backend_algorithm_version", algorithm)
        object.__setattr__(self, "job_protocol_version", protocol)
        object.__setattr__(self, "moving_cloud_frame", frame)
        object.__setattr__(self, "references", references)
        object.__setattr__(self, "initial_alignment", initial_alignment)
        object.__setattr__(self, "marker_uniform_scale", marker_uniform_scale)
        object.__setattr__(self, "marker_max_error", marker_max_error)
        object.__setattr__(self, "parameters", parameters)
        object.__setattr__(self, "marker_reference_project", reference_markers)
        object.__setattr__(self, "marker_moving_project", moving_markers)
        object.__setattr__(
            self,
            "confidence_preprocessing",
            dict(confidence_preprocessing or {}),
        )

    @property
    def parameter_fingerprint(self) -> str:
        """기존 parameters mapping과 같은 방식의 재현성 지문을 반환합니다."""

        return _canonical_fingerprint(self.parameters)

@dataclass(frozen=True, slots=True)
class Open3dDistanceStage:
    """같은 전체 overlap 점군에 적용할 한 대응 거리 단계입니다."""

    name: str
    maximum_correspondence_distance: float
    max_iterations: int
    relative_fitness: float = 1.0e-6
    relative_rmse: float = 1.0e-6

    def __post_init__(self) -> None:
        """거리 감소 단계의 이름·반복·수렴 범위를 검증합니다."""

        name = _strict_text(self.name, "Open3D 거리 단계 이름").lower()
        distance = _strict_finite_float(
            self.maximum_correspondence_distance,
            "Open3D 최대 대응 거리",
            minimum=np.finfo(np.float64).eps,
        )
        max_iterations = _strict_positive_int(self.max_iterations, "Open3D 최대 반복")
        relative_fitness = _strict_finite_float(
            self.relative_fitness,
            "Open3D relative fitness",
            minimum=0.0,
        )
        relative_rmse = _strict_finite_float(
            self.relative_rmse,
            "Open3D relative RMSE",
            minimum=0.0,
        )
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "maximum_correspondence_distance", distance)
        object.__setattr__(self, "max_iterations", max_iterations)
        object.__setattr__(self, "relative_fitness", relative_fitness)
        object.__setattr__(self, "relative_rmse", relative_rmse)

@dataclass(frozen=True, slots=True)
class _PreparedClouds:
    """한 번만 확정한 전체 overlap crop 배열과 감사 정보를 보관합니다."""

    source_positions: np.ndarray
    source_point_ids: np.ndarray
    source_confidence_weights: np.ndarray
    source_isolation_candidates: np.ndarray
    target_positions: np.ndarray
    target_point_ids: np.ndarray
    target_confidence_weights: np.ndarray
    target_isolation_candidates: np.ndarray
    target_model_ids: tuple[str, ...]
    source_bounds: tuple[np.ndarray, np.ndarray]
    target_bounds: tuple[np.ndarray, np.ndarray]
    crop_margin: float
    source_original_count: int
    target_original_count: int
    source_original_positions_fingerprint: str
    source_original_point_ids_fingerprint: str
    target_original_positions_fingerprint: str
    target_original_point_ids_fingerprint: str

@dataclass(frozen=True, slots=True)
class Open3dSolverResult:
    """Open3D 전용 metadata와 NPZ용 세 행렬을 함께 보관합니다."""

    solver_input: Open3dSolverInput
    initial_alignment: np.ndarray
    final_alignment: np.ndarray
    icp_delta: np.ndarray
    level_results: tuple[Mapping[str, Any], ...]
    baseline_metrics: Mapping[str, Any]
    candidate_metrics: Mapping[str, Any]
    quality: Mapping[str, Any]
    full_input_audit: Mapping[str, Any]
    local_origin: np.ndarray
    moving_diagonal: float
    delta_translation: float
    delta_rotation_degrees: float
    normal_audit: Mapping[str, Any]
    execution_audit: Mapping[str, Any]
    elapsed_seconds: float

    def result_arrays(self) -> dict[str, np.ndarray]:
        """원자 NPZ에 기록할 정확한 세 float64 4×4 행렬을 반환합니다."""

        return {
            "initial_alignment": np.array(self.initial_alignment, dtype=np.float64, order="C", copy=True),
            "final_alignment": np.array(self.final_alignment, dtype=np.float64, order="C", copy=True),
            "icp_delta": np.array(self.icp_delta, dtype=np.float64, order="C", copy=True),
        }

    def to_metadata(self, *, environment: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """행렬을 제외한 외부 ICP 결과와 평가 전처리 감사를 JSON으로 반환합니다."""

        source = self.solver_input.source
        scale_mode = str(self.solver_input.parameters["scale_mode"]).strip().upper()
        scale_refine_status = str(
            self.quality.get(
                "scale_refine_status",
                "NOT_REQUESTED" if scale_mode == "LOCKED" else "SKIPPED",
            )
        ).strip().upper()
        metadata: dict[str, Any] = {
            "job_id": self.solver_input.job_id,
            "input_fingerprint": self.solver_input.input_fingerprint,
            "parameter_fingerprint": self.solver_input.parameter_fingerprint,
            "algorithm_version": self.solver_input.algorithm_version,
            "backend_id": self.solver_input.backend_id,
            "backend_algorithm_version": self.solver_input.backend_algorithm_version,
            "job_protocol_version": self.solver_input.job_protocol_version,
            "moving_model_id": source.model_id,
            "reference_model_ids": list(self.full_input_audit["target_crop_model_ids"]),
            "moving_cloud_frame": self.solver_input.moving_cloud_frame,
            "marker_signature": str(self.solver_input.marker_signature),
            "model_revision_snapshot": [list(item) if isinstance(item, tuple) else item for item in self.solver_input.model_revision_snapshot],
            "model_state_snapshot": [list(item) if isinstance(item, tuple) else item for item in self.solver_input.model_state_snapshot],
            "matrix_arrays": {
                "initial_alignment": "initial_alignment",
                "final_alignment": "final_alignment",
                "icp_delta": "icp_delta",
            },
            "scale_mode": scale_mode,
            "scale_refine_status": scale_refine_status,
            "level_results": [dict(item) for item in self.level_results],
            "solver_stop_reason": str(self.level_results[-1]["stop_reason"]),
            "user_review_required": bool(self.quality["user_review_required"]),
            "baseline_metrics": dict(self.baseline_metrics),
            "best_candidate_metrics": dict(self.candidate_metrics),
            "best_candidate": {
                "level_name": str(self.level_results[-1]["level_name"]),
                "iteration": int(self.level_results[-1]["iterations"]),
                "maximum_correspondence_distance": float(
                    self.candidate_metrics["maximum_correspondence_distance"]
                ),
                "point_to_point_rmse": self.candidate_metrics["point_to_point_rmse"],
                "point_to_plane_rmse": self.candidate_metrics["point_to_plane_rmse"],
                "fitness": self.candidate_metrics["fitness"],
                "inlier_count": self.candidate_metrics["inlier_count"],
                "evaluation_fingerprint": self.candidate_metrics["evaluation_fingerprint"],
            },
            "best_level_name": str(self.level_results[-1]["level_name"]),
            "best_iteration": int(self.level_results[-1]["iterations"]),
            "quality": dict(self.quality),
            "full_input_audit": dict(self.full_input_audit),
            # 현재 UI가 전환 중인 경량 필드도 같은 정본을 가리키게 남깁니다.
            "input_policy": FULL_INPUT_POLICY,
            "overlap_point_counts": {
                "moving": int(self.full_input_audit["source_crop_count"]),
                "reference": int(self.full_input_audit["target_crop_count"]),
                "source_original": int(self.full_input_audit["source_original_count"]),
                "target_original": int(self.full_input_audit["target_original_count"]),
                "source_crop": int(self.full_input_audit["source_crop_count"]),
                "target_crop": int(self.full_input_audit["target_crop_count"]),
            },
            "local_origin": self.local_origin.tolist(),
            "moving_diagonal": float(self.moving_diagonal),
            "delta_translation": float(self.delta_translation),
            "delta_rotation_degrees": float(self.delta_rotation_degrees),
            "normal_audit": dict(self.normal_audit),
            "execution_audit": dict(self.execution_audit),
            "confidence_preprocessing": dict(
                self.solver_input.confidence_preprocessing or {}
            ),
            "elapsed_seconds": float(self.elapsed_seconds),
        }
        if environment is not None:
            metadata.update(dict(environment))
        return metadata

def _scoped_cloud_fingerprint(clouds: Sequence[Open3dPointCloud], *, positions: bool) -> str:
    """여러 A 모델의 ID 충돌을 피하도록 모델 ID 범위를 포함해 해시합니다."""

    return _canonical_fingerprint(
        [
            {
                "model_id": cloud.model_id,
                "fingerprint": cloud.positions_fingerprint if positions else cloud.point_ids_fingerprint,
            }
            for cloud in clouds
        ]
    )

def _derive_capture_distance(job: Open3dSolverInput) -> float:
    """초기화 방식과 전체 P/A 통계로 최초 overlap capture 거리를 계산합니다."""

    source_minimum, source_maximum = _bounds(job.source.positions_float64)
    source_diagonal = _diagonal(source_minimum, source_maximum)
    target_positions = np.concatenate([item.positions_float64 for item in job.references], axis=0)
    target_minimum, target_maximum = _bounds(target_positions)
    target_diagonal = _diagonal(target_minimum, target_maximum)
    geometry_scale = min(source_diagonal, target_diagonal)
    spacing = max(
        _nominal_spacing(job.source.positions_float64),
        _nominal_spacing(target_positions),
    )
    if job.marker_reference_project is None:
        # 직접 Align은 마커 오차가 없으므로 대략 맞춘 현재 위치 주변을 형상 크기로 포착합니다.
        distance = min(0.25 * geometry_scale, max(0.05 * geometry_scale, 3.0 * spacing))
    else:
        distance = min(0.25 * geometry_scale, max(2.0 * job.marker_max_error, 3.0 * spacing))
    if not isfinite(distance) or distance <= np.finfo(np.float64).eps:
        raise Open3dIcpInputError("Open3D 최초 대응 거리를 계산하지 못했습니다.")
    return float(distance)

def _within_expanded_bounds(
    positions: np.ndarray,
    minimum: np.ndarray,
    maximum: np.ndarray,
    margin: float,
) -> np.ndarray:
    """전체점 AABB crop에 포함되는 위치 mask를 반환합니다."""

    return np.all(
        (positions >= (minimum - margin)[np.newaxis, :])
        & (positions <= (maximum + margin)[np.newaxis, :]),
        axis=1,
    )

def _prepare_full_overlap_clouds(job: Open3dSolverInput, capture_distance: float) -> _PreparedClouds:
    """AABB 겹침 원본점과 같은 index의 평가 신뢰도를 한 번만 고정 crop합니다."""

    source = job.source
    references = job.references
    source_weights_full = (
        source.confidence_weights_float64
        if source.confidence_weights_float64 is not None
        else np.ones(source.count, dtype=np.float64)
    )
    source_candidates_full = (
        source.isolation_candidates_bool
        if source.isolation_candidates_bool is not None
        else np.zeros(source.count, dtype=bool)
    )
    source_minimum, source_maximum = _bounds(source.positions_float64)
    source_mask = np.zeros(source.count, dtype=bool)
    target_position_parts: list[np.ndarray] = []
    target_id_parts: list[np.ndarray] = []
    target_weight_parts: list[np.ndarray] = []
    target_candidate_parts: list[np.ndarray] = []
    target_model_ids: list[str] = []
    for reference in references:
        reference_minimum, reference_maximum = _bounds(reference.positions_float64)
        source_mask |= _within_expanded_bounds(
            source.positions_float64,
            reference_minimum,
            reference_maximum,
            capture_distance,
        )
        reference_mask = _within_expanded_bounds(
            reference.positions_float64,
            source_minimum,
            source_maximum,
            capture_distance,
        )
        if not np.any(reference_mask):
            continue
        target_position_parts.append(
            np.ascontiguousarray(reference.positions_float64[reference_mask], dtype=np.float64)
        )
        target_id_parts.append(
            np.ascontiguousarray(reference.point_ids_uint64[reference_mask], dtype=np.uint64)
        )
        reference_weights = (
            reference.confidence_weights_float64
            if reference.confidence_weights_float64 is not None
            else np.ones(reference.count, dtype=np.float64)
        )
        reference_candidates = (
            reference.isolation_candidates_bool
            if reference.isolation_candidates_bool is not None
            else np.zeros(reference.count, dtype=bool)
        )
        target_weight_parts.append(
            np.ascontiguousarray(reference_weights[reference_mask], dtype=np.float64)
        )
        target_candidate_parts.append(
            np.ascontiguousarray(reference_candidates[reference_mask], dtype=np.bool_)
        )
        target_model_ids.append(reference.model_id)
    if not np.any(source_mask) or not target_position_parts:
        raise Open3dIcpInputError("Open3D 전체점 overlap crop에 P/A 공통 영역이 없습니다.")
    source_positions = np.ascontiguousarray(source.positions_float64[source_mask], dtype=np.float64)
    source_point_ids = np.ascontiguousarray(source.point_ids_uint64[source_mask], dtype=np.uint64)
    source_confidence_weights = np.ascontiguousarray(
        source_weights_full[source_mask],
        dtype=np.float64,
    )
    source_isolation_candidates = np.ascontiguousarray(
        source_candidates_full[source_mask],
        dtype=np.bool_,
    )
    target_positions = np.ascontiguousarray(np.concatenate(target_position_parts, axis=0), dtype=np.float64)
    target_point_ids = np.ascontiguousarray(np.concatenate(target_id_parts, axis=0), dtype=np.uint64)
    target_confidence_weights = np.ascontiguousarray(
        np.concatenate(target_weight_parts, axis=0),
        dtype=np.float64,
    )
    target_isolation_candidates = np.ascontiguousarray(
        np.concatenate(target_candidate_parts, axis=0),
        dtype=np.bool_,
    )
    source_bounds = _bounds(source_positions)
    target_bounds = _bounds(target_positions)
    return _PreparedClouds(
        source_positions=source_positions,
        source_point_ids=source_point_ids,
        source_confidence_weights=source_confidence_weights,
        source_isolation_candidates=source_isolation_candidates,
        target_positions=target_positions,
        target_point_ids=target_point_ids,
        target_confidence_weights=target_confidence_weights,
        target_isolation_candidates=target_isolation_candidates,
        target_model_ids=tuple(target_model_ids),
        source_bounds=source_bounds,
        target_bounds=target_bounds,
        crop_margin=capture_distance,
        source_original_count=source.count,
        target_original_count=sum(item.count for item in references),
        source_original_positions_fingerprint=source.positions_fingerprint,
        source_original_point_ids_fingerprint=source.point_ids_fingerprint,
        target_original_positions_fingerprint=_scoped_cloud_fingerprint(references, positions=True),
        target_original_point_ids_fingerprint=_scoped_cloud_fingerprint(references, positions=False),
    )

def _parameter_levels(parameters: Mapping[str, Any]) -> tuple[tuple[str, int, float], ...]:
    """기존 parameters의 반복 수·노멀 반경 배수만 읽고 점 예산은 사용하지 않습니다."""

    raw_levels = parameters.get("levels")
    if not isinstance(raw_levels, list) or not raw_levels:
        raise Open3dIcpInputError("Open3D parameters에 레벨 목록이 없습니다.")
    levels: list[tuple[str, int, float]] = []
    names: set[str] = set()
    supported_names = {"coarse", "medium", "fine"}
    for index, raw in enumerate(raw_levels):
        if not isinstance(raw, Mapping):
            raise Open3dIcpInputError(f"Open3D parameters levels[{index}]가 객체가 아닙니다.")
        name = _strict_text(raw.get("name"), f"Open3D levels[{index}] 이름").lower()
        if name not in supported_names:
            raise Open3dIcpInputError("Open3D 거리 단계는 coarse·medium·fine만 지원합니다.")
        if name in names:
            raise Open3dIcpInputError("Open3D 거리 단계 이름이 중복됩니다.")
        names.add(name)
        iterations = _strict_positive_int(raw.get("max_iterations"), f"Open3D levels[{index}] 반복")
        multiplier = _strict_finite_float(
            raw.get("normal_radius_multiplier", 3.0),
            f"Open3D levels[{index}] 노멀 반경 배수",
            minimum=np.finfo(np.float64).eps,
        )
        levels.append((name, iterations, multiplier))
    return tuple(levels)

def _parameter_normal_max_nn(parameters: Mapping[str, Any]) -> int:
    """기존 레벨 파라미터의 검증된 최대 노멀 이웃 수를 반환합니다."""

    raw_levels = parameters.get("levels")
    assert isinstance(raw_levels, list)
    return max(
        _strict_positive_int(
            raw.get("normal_maximum_neighbors", 28),
            f"Open3D levels[{index}] 노멀 최대 이웃",
        )
        for index, raw in enumerate(raw_levels)
        if isinstance(raw, Mapping)
    )

def _derive_stages_and_normal_policy(
    job: Open3dSolverInput,
    prepared: _PreparedClouds,
    capture_distance: float,
) -> tuple[tuple[Open3dDistanceStage, ...], float, int, float, int]:
    """동일 crop에 쓸 거리 감소 단계와 target normal·Tukey 정책을 계산합니다."""

    levels = _parameter_levels(job.parameters)
    stages = tuple(
        Open3dDistanceStage(
            name=name,
            maximum_correspondence_distance=capture_distance * (0.5 ** index),
            max_iterations=iterations,
        )
        for index, (name, iterations, _multiplier) in enumerate(levels)
    )
    if any(
        next_stage.maximum_correspondence_distance >= stage.maximum_correspondence_distance
        for stage, next_stage in zip(stages, stages[1:], strict=False)
    ):
        raise Open3dIcpInputError("Open3D 대응 거리 단계가 엄격히 감소하지 않습니다.")
    target_spacing = _nominal_spacing(prepared.target_positions)
    normal_multiplier = max(multiplier for _name, _iterations, multiplier in levels)
    normal_radius = max(target_spacing * normal_multiplier, np.finfo(np.float64).eps)
    normal_max_nn = _parameter_normal_max_nn(job.parameters)
    robust_scale = _strict_finite_float(
        job.parameters.get("robust_kernel_scale", 1.345),
        "Open3D 기존 robust kernel scale",
        minimum=np.finfo(np.float64).eps,
    )
    tukey_loss_k = robust_scale * max(target_spacing, stages[-1].maximum_correspondence_distance)
    if not isfinite(tukey_loss_k) or tukey_loss_k <= np.finfo(np.float64).eps:
        raise Open3dIcpInputError("Open3D TukeyLoss scale을 계산하지 못했습니다.")
    batch_size = min(4, max(stage.max_iterations for stage in stages))
    return stages, float(normal_radius), normal_max_nn, float(tukey_loss_k), batch_size

def _is_stable_delta(previous: np.ndarray, current: np.ndarray, distance: float) -> bool:
    """batch 전후 rigid 변화가 좌표 스케일 noise floor 아래인지 판정합니다."""

    step = _validate_rigid_matrix(current @ _inverse_rigid(previous), "Open3D batch step")
    translation = float(np.linalg.norm(step[:3, 3]))
    rotation = _rotation_angle_degrees(step[:3, :3])
    return translation <= max(distance * 1.0e-7, 1.0e-12) and rotation <= 1.0e-7

def _full_input_audit(
    prepared: _PreparedClouds,
    stages: Sequence[Open3dDistanceStage],
) -> dict[str, Any]:
    """전체 overlap crop과 평가 신뢰도가 모든 단계에서 동일함을 증명합니다."""

    source_crop_positions_fingerprint = _array_fingerprint(prepared.source_positions)
    source_crop_point_ids_fingerprint = _array_fingerprint(prepared.source_point_ids)
    target_crop_positions_fingerprint = _array_fingerprint(prepared.target_positions)
    source_confidence_fingerprint = _array_fingerprint(
        prepared.source_confidence_weights
    )
    target_confidence_fingerprint = _array_fingerprint(
        prepared.target_confidence_weights
    )
    # 여러 A 모델의 ID 범위가 합쳐진 순서를 포함해 지문을 만듭니다.
    target_crop_point_ids_fingerprint = _canonical_fingerprint(
        {
            "target_model_ids": prepared.target_model_ids,
            "array": _array_fingerprint(prepared.target_point_ids),
        }
    )
    stage_inputs = {
        stage.name: {
            "source_positions_fingerprint": source_crop_positions_fingerprint,
            "source_point_ids_fingerprint": source_crop_point_ids_fingerprint,
            "target_positions_fingerprint": target_crop_positions_fingerprint,
            "target_point_ids_fingerprint": target_crop_point_ids_fingerprint,
            "source_point_count": int(prepared.source_positions.shape[0]),
            "target_point_count": int(prepared.target_positions.shape[0]),
        }
        for stage in stages
    }
    return {
        "input_policy": FULL_INPUT_POLICY,
        "source_original_count": int(prepared.source_original_count),
        "target_original_count": int(prepared.target_original_count),
        "source_crop_count": int(prepared.source_positions.shape[0]),
        "target_crop_count": int(prepared.target_positions.shape[0]),
        "source_original_positions_fingerprint": prepared.source_original_positions_fingerprint,
        "source_original_point_ids_fingerprint": prepared.source_original_point_ids_fingerprint,
        "target_original_positions_fingerprint": prepared.target_original_positions_fingerprint,
        "target_original_point_ids_fingerprint": prepared.target_original_point_ids_fingerprint,
        "source_crop_positions_fingerprint": source_crop_positions_fingerprint,
        "source_crop_point_ids_fingerprint": source_crop_point_ids_fingerprint,
        "target_crop_positions_fingerprint": target_crop_positions_fingerprint,
        "target_crop_point_ids_fingerprint": target_crop_point_ids_fingerprint,
        "source_confidence_weights_fingerprint": source_confidence_fingerprint,
        "target_confidence_weights_fingerprint": target_confidence_fingerprint,
        "source_isolation_candidate_count": int(
            np.count_nonzero(prepared.source_isolation_candidates)
        ),
        "target_isolation_candidate_count": int(
            np.count_nonzero(prepared.target_isolation_candidates)
        ),
        "target_crop_model_ids": list(prepared.target_model_ids),
        "crop_margin": float(prepared.crop_margin),
        "source_crop_bounds": {
            "minimum": prepared.source_bounds[0].tolist(),
            "maximum": prepared.source_bounds[1].tolist(),
        },
        "target_crop_bounds": {
            "minimum": prepared.target_bounds[0].tolist(),
            "maximum": prepared.target_bounds[1].tolist(),
        },
        "stage_input_fingerprints": stage_inputs,
        "downsampling": {
            "used": False,
            "voxel_down_sample": False,
            "point_budget_sampling": False,
        },
    }
__all__ = (
    'CancelProbe',
    'ExternalIcpDeltaDiagnostics',
    'FULL_INPUT_POLICY',
    'OPEN3D_GICP_ALGORITHM_VERSION',
    'OPEN3D_GICP_BACKEND_ID',
    'OPEN3D_JOB_PROTOCOL_VERSION',
    'OPEN3D_ROBUST_POINT_TO_PLANE_ALGORITHM_VERSION',
    'OPEN3D_ROBUST_POINT_TO_PLANE_BACKEND_ID',
    'Open3dDistanceStage',
    'Open3dIcpCancelled',
    'Open3dIcpEnvironmentError',
    'Open3dIcpError',
    'Open3dIcpInputError',
    'Open3dIcpNumericalError',
    'Open3dPointCloud',
    'Open3dSolverInput',
    'Open3dSolverResult',
    'SMALL_GICP_ALGORITHM_VERSION',
    'SMALL_GICP_BACKEND_ID',
    'Sim3MatrixDecomposition',
    '_PreparedClouds',
    '_array_fingerprint',
    '_bounds',
    '_canonical_fingerprint',
    '_derive_capture_distance',
    '_derive_stages_and_normal_policy',
    '_diagonal',
    '_float64_positions',
    '_full_input_audit',
    '_inverse_rigid',
    '_is_stable_delta',
    '_local_delta_to_world',
    '_matrix4',
    '_nominal_spacing',
    '_parameter_levels',
    '_parameter_normal_max_nn',
    '_prepare_full_overlap_clouds',
    '_raise_if_cancelled',
    '_rotation_angle_degrees',
    '_scoped_cloud_fingerprint',
    '_strict_finite_float',
    '_strict_positive_int',
    '_strict_text',
    '_transform_positions',
    '_uint64_ids',
    '_uniform_scale_and_rotation',
    '_validate_rigid_matrix',
    '_within_expanded_bounds',
    'analyze_external_icp_delta',
    'decompose_sim3_matrix',
)
