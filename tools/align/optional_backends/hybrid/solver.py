"""다중 해상도 point-to-point ICP와 제한된 후행 스케일 보정을 구현합니다."""

from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
from math import isfinite
from time import perf_counter
from typing import Callable, Sequence

import numpy as np

from ...alignment.icp_scale_refine import (
    BoundedScaleRefineError,
    solve_bounded_scale_matrix,
)

from ...constants import (
    ICP_STAGE_BUILDING_UNIFORM_SAMPLES,
    ICP_STAGE_COARSE_POINT_TO_POINT,
    ICP_STAGE_COMPLETED_PASS,
    ICP_STAGE_COMPLETED_REVIEW,
    ICP_STAGE_ESTIMATING_NORMALS_COARSE,
    ICP_STAGE_ESTIMATING_NORMALS_FINE,
    ICP_STAGE_ESTIMATING_NORMALS_MEDIUM,
    ICP_STAGE_EVALUATING_BEST_CANDIDATE,
    ICP_STAGE_FINE_POINT_TO_PLANE,
    ICP_STAGE_MEDIUM_POINT_TO_PLANE,
    ICP_STAGE_PREPARING_INPUT,
    ICP_STAGE_SCALE_REFINEMENT,
    ICP_STAGE_SELECTING_OVERLAP,
)
from ...alignment.icp_index import (
    CancelProbe,
    NearestNeighborIndex,
    NearestNeighborQueryResult,
    default_nearest_neighbor_index,
)
from ...alignment.icp_overlap import (
    AxisAlignedBounds,
    IcpOverlapCandidate,
    bounds_from_cloud,
    bounds_intersection,
    combined_moving_overlap_coverage,
    find_overlap_candidates,
    select_overlap_clouds,
)
from ...alignment.icp_quality import (
    evaluate_icp_quality,
    per_reference_metrics,
    reciprocal_match_ratio,
    rotation_angle_degrees,
)
from ...alignment.icp_sampling import (
    VoxelSample,
    build_uniform_density_sample,
    choose_shared_voxel_size,
    allocate_multi_reference_quotas,
)
from ...alignment.icp_normals import (
    IcpNormalEstimate,
    estimate_sample_normals,
    normal_angle_mask,
    transform_normals,
)
from ...alignment.icp_types import (
    IcpAlignmentResult,
    IcpBestCandidate,
    IcpCancelledError,
    IcpDegeneracyError,
    IcpError,
    IcpInputError,
    IcpInputSnapshot,
    IcpEvaluationMetrics,
    IcpDensitySamplingMetrics,
    IcpIterationMetrics,
    IcpLevelParameters,
    IcpLevelResult,
    IcpMovingCloudFrame,
    IcpNormalCoverageError,
    IcpObjectiveKind,
    IcpNumericalError,
    IcpOverlapError,
    IcpParameters,
    IcpPointCloud,
    IcpProgressSnapshot,
    IcpQualityMetrics,
    IcpQualityRejectedError,
    IcpScaleMode,
    IcpScaleRefineStatus,
    IcpSolverStopReason,
    _strict_finite_float,
)
from ...alignment.types import (
    Sim3Transform,
    compose_sim3_transforms,
    create_sim3_transform,
    identity_sim3_transform,
)


ProgressSink = Callable[[IcpProgressSnapshot], None] | None
IndexFactory = Callable[[], NearestNeighborIndex] | Callable[..., NearestNeighborIndex] | None
_MAX_OVERLAP_VOXEL_REFINEMENT_STEPS = 4


def _freeze_float64(value: object, shape: tuple[int | None, ...], field_name: str) -> np.ndarray:
    """solver 내부에서 사용할 유한 float64 배열을 복사해 읽기 전용으로 고정합니다."""

    array = np.array(value, dtype=np.float64, order="C", copy=True)
    if array.ndim != len(shape) or any(
        expected is not None and actual != expected
        for actual, expected in zip(array.shape, shape, strict=True)
    ):
        raise IcpInputError(f"{field_name} 배열 형상이 올바르지 않습니다.")
    if not np.isfinite(array).all():
        raise IcpInputError(f"{field_name} 배열에 유한하지 않은 값이 있습니다.")
    array.setflags(write=False)
    return array


def _freeze_int64(value: object, shape: tuple[int | None, ...], field_name: str) -> np.ndarray:
    """solver correspondence 인덱스를 읽기 전용 int64 배열로 고정합니다."""

    source = np.asarray(value)
    if source.dtype.kind not in {"u", "i"}:
        raise IcpInputError(f"{field_name} 배열은 정수 dtype이어야 합니다.")
    array = np.array(source, dtype=np.int64, order="C", copy=True)
    if array.ndim != len(shape) or any(
        expected is not None and actual != expected
        for actual, expected in zip(array.shape, shape, strict=True)
    ):
        raise IcpInputError(f"{field_name} 배열 형상이 올바르지 않습니다.")
    array.setflags(write=False)
    return array


def _raise_if_cancelled(cancel_probe: CancelProbe) -> None:
    """수치 단계 경계에서 worker 취소 요청을 공통 오류로 전달합니다."""

    if cancel_probe is not None and bool(cancel_probe()):
        raise IcpCancelledError("사용자가 ICP 계산 취소를 요청했습니다.")


def _emit_progress(progress_sink: ProgressSink, progress: IcpProgressSnapshot) -> None:
    """등록된 progress sink가 있을 때만 검증된 진행 snapshot을 전달합니다."""

    if progress_sink is not None:
        progress_sink(progress)


def apply_sim3_transform(transform: Sim3Transform, positions_float64: object) -> np.ndarray:
    """column-vector Sim(3)을 N×3 행 점 배열에 적용한 새 float64 배열을 반환합니다."""

    if not isinstance(transform, Sim3Transform):
        raise IcpInputError("ICP 점 변환에는 Sim3Transform이 필요합니다.")
    positions = _freeze_float64(positions_float64, (None, 3), "ICP 변환 점")
    transformed = positions @ transform.matrix_float64[:3, :3].T + transform.matrix_float64[:3, 3]
    return _freeze_float64(transformed, (positions.shape[0], 3), "ICP 변환 결과")


def _validate_correspondence_geometry(
    moving_positions: np.ndarray,
    reference_positions: np.ndarray,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Kabsch·Umeyama에 필요한 centered spread와 rank를 검증합니다."""

    if moving_positions.shape != reference_positions.shape or moving_positions.shape[0] < 3:
        raise IcpDegeneracyError("ICP 변환에는 같은 개수의 대응점 3개 이상이 필요합니다.")
    moving_mean = np.mean(moving_positions, axis=0)
    reference_mean = np.mean(reference_positions, axis=0)
    moving_centered = moving_positions - moving_mean
    reference_centered = reference_positions - reference_mean
    moving_spread = float(np.linalg.norm(moving_centered))
    reference_spread = float(np.linalg.norm(reference_centered))
    tolerance = max(moving_spread, reference_spread, 1.0) * 1.0e-12
    if moving_spread <= tolerance or reference_spread <= tolerance:
        raise IcpDegeneracyError("ICP 대응점의 중심화 spread가 너무 작습니다.")
    covariance = moving_centered.T @ reference_centered
    try:
        covariance_singular_values = np.linalg.svd(covariance, compute_uv=False)
    except np.linalg.LinAlgError as error:
        raise IcpNumericalError("ICP 대응점 공분산 rank 계산에 실패했습니다.") from error
    if not np.isfinite(covariance_singular_values).all():
        raise IcpNumericalError("ICP 대응점 공분산 singular value가 유한하지 않습니다.")
    largest_singular_value = float(covariance_singular_values[0])
    # 공분산 singular value는 길이² 단위이므로 길이 단위 spread tolerance를 재사용하면 안 됩니다.
    rank_tolerance = max(largest_singular_value * 1.0e-12, np.finfo(np.float64).tiny)
    rank = int(np.count_nonzero(covariance_singular_values > rank_tolerance))
    if rank < 2:
        raise IcpDegeneracyError("ICP 대응점이 공선이어서 rigid 변환을 결정할 수 없습니다.")
    return moving_mean, reference_mean, moving_centered, reference_centered


def _proper_rotation(moving_centered: np.ndarray, reference_centered: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """공분산 SVD에서 반사를 제거한 proper rotation과 singular values를 계산합니다."""

    covariance = moving_centered.T @ reference_centered
    try:
        left, singular_values, right_t = np.linalg.svd(covariance, full_matrices=False)
    except np.linalg.LinAlgError as error:
        raise IcpNumericalError("ICP Kabsch SVD 계산에 실패했습니다.") from error
    if not np.isfinite(singular_values).all():
        raise IcpNumericalError("ICP Kabsch singular value가 유한하지 않습니다.")
    correction = np.identity(3, dtype=np.float64)
    raw_rotation = right_t.T @ left.T
    correction[2, 2] = 1.0 if np.linalg.det(raw_rotation) >= 0.0 else -1.0
    rotation = right_t.T @ correction @ left.T
    if not np.allclose(rotation.T @ rotation, np.identity(3), rtol=0.0, atol=1.0e-8):
        raise IcpNumericalError("ICP Kabsch 회전이 직교 행렬이 아닙니다.")
    if not np.isclose(float(np.linalg.det(rotation)), 1.0, rtol=0.0, atol=1.0e-8):
        raise IcpNumericalError("ICP Kabsch 회전 행렬식이 +1이 아닙니다.")
    return rotation, singular_values * np.diag(correction)


def solve_rigid_delta(moving_positions_float64: object, reference_positions_float64: object) -> Sim3Transform:
    """현재 P 대응점에서 A 대응점으로 가는 scale=1 proper rigid Kabsch delta를 계산합니다."""

    moving = _freeze_float64(moving_positions_float64, (None, 3), "ICP rigid P 대응점")
    reference = _freeze_float64(
        reference_positions_float64,
        (moving.shape[0], 3),
        "ICP rigid A 대응점",
    )
    moving_mean, reference_mean, moving_centered, reference_centered = _validate_correspondence_geometry(
        moving,
        reference,
    )
    rotation, _signed_singular_values = _proper_rotation(moving_centered, reference_centered)
    translation = reference_mean - rotation @ moving_mean
    return create_sim3_transform(rotation, 1.0, translation)


def _rotation_from_axis_angle(axis_angle_float64: np.ndarray) -> np.ndarray:
    """작은 회전 벡터를 Rodrigues 공식으로 3×3 proper rotation으로 변환합니다."""

    vector = _freeze_float64(axis_angle_float64, (3,), "ICP point-to-plane 회전 벡터")
    angle = float(np.linalg.norm(vector))
    if angle <= np.finfo(np.float64).eps:
        skew = np.array(
            (
                (0.0, -vector[2], vector[1]),
                (vector[2], 0.0, -vector[0]),
                (-vector[1], vector[0], 0.0),
            ),
            dtype=np.float64,
        )
        return np.identity(3, dtype=np.float64) + skew
    axis = vector / angle
    skew = np.array(
        (
            (0.0, -axis[2], axis[1]),
            (axis[2], 0.0, -axis[0]),
            (-axis[1], axis[0], 0.0),
        ),
        dtype=np.float64,
    )
    return np.identity(3, dtype=np.float64) + np.sin(angle) * skew + (1.0 - np.cos(angle)) * (skew @ skew)


def solve_point_to_plane_delta(
    moving_positions_float64: object,
    reference_positions_float64: object,
    reference_normals_float64: object,
    *,
    damping: float,
    condition_limit: float,
    robust_kernel_scale: float,
) -> tuple[Sim3Transform, int, float, float]:
    """local-origin point-to-plane 6자유도 가중 최소제곱 rigid delta를 풉니다."""

    moving = _freeze_float64(moving_positions_float64, (None, 3), "ICP plane P 대응점")
    reference = _freeze_float64(reference_positions_float64, (moving.shape[0], 3), "ICP plane A 대응점")
    normals = _freeze_float64(reference_normals_float64, (moving.shape[0], 3), "ICP plane A 노멀")
    if moving.shape[0] < 6:
        raise IcpDegeneracyError("point-to-plane 선형계에는 대응점 6개 이상이 필요합니다.")
    normalized_damping = _strict_finite_float(damping, "ICP point-to-plane damping")
    normalized_condition_limit = _strict_finite_float(condition_limit, "ICP point-to-plane condition 한도")
    normalized_kernel_scale = _strict_finite_float(robust_kernel_scale, "ICP point-to-plane 강건 커널 배수")
    if normalized_damping < 0.0 or normalized_condition_limit <= 1.0 or normalized_kernel_scale <= 0.0:
        raise IcpInputError("point-to-plane 선형계 설정 범위가 올바르지 않습니다.")
    normal_lengths = np.linalg.norm(normals, axis=1)
    if np.any(normal_lengths <= np.finfo(np.float64).eps):
        raise IcpDegeneracyError("point-to-plane 대응점에 무효 노멀이 있습니다.")
    unit_normals = normals / normal_lengths[:, np.newaxis]
    residuals = np.einsum("ij,ij->i", unit_normals, moving - reference, optimize=True)
    if not np.isfinite(residuals).all():
        raise IcpNumericalError("point-to-plane 잔차가 유한하지 않습니다.")
    median = float(np.median(residuals))
    mad = float(np.median(np.abs(residuals - median)))
    robust_scale = max(1.4826 * mad * normalized_kernel_scale, np.finfo(np.float64).eps)
    normalized_abs = np.abs(residuals) / robust_scale
    weights = np.ones(residuals.shape[0], dtype=np.float64)
    large = normalized_abs > 1.0
    weights[large] = 1.0 / normalized_abs[large]
    jacobian = np.concatenate((np.cross(moving, unit_normals), unit_normals), axis=1)
    normal_matrix = jacobian.T @ (weights[:, np.newaxis] * jacobian)
    right_hand = -(jacobian.T @ (weights * residuals))
    try:
        singular_values = np.linalg.svd(normal_matrix, compute_uv=False)
    except np.linalg.LinAlgError as error:
        raise IcpNumericalError("point-to-plane 선형계 SVD에 실패했습니다.") from error
    if not np.isfinite(singular_values).all() or singular_values.size != 6:
        raise IcpNumericalError("point-to-plane 선형계 singular value가 올바르지 않습니다.")
    largest = float(singular_values[0])
    tolerance = max(largest * 1.0e-12, np.finfo(np.float64).tiny)
    rank = int(np.count_nonzero(singular_values > tolerance))
    smallest = float(singular_values[-1])
    condition = float(largest / max(smallest, np.finfo(np.float64).tiny))
    if rank < 6:
        raise IcpDegeneracyError("point-to-plane 선형계 rank가 6보다 작습니다.")
    if not isfinite(condition) or condition > normalized_condition_limit:
        raise IcpDegeneracyError("point-to-plane 선형계 condition number가 안전 한도를 넘었습니다.")
    try:
        increment = np.linalg.solve(normal_matrix + normalized_damping * np.identity(6), right_hand)
    except np.linalg.LinAlgError as error:
        raise IcpNumericalError("point-to-plane 선형계 해법 계산에 실패했습니다.") from error
    if not np.isfinite(increment).all():
        raise IcpNumericalError("point-to-plane 선형계 해가 유한하지 않습니다.")
    rotation = _rotation_from_axis_angle(increment[:3])
    delta = create_sim3_transform(rotation, 1.0, increment[3:])
    plane_rmse = float(np.sqrt(np.mean(np.square(residuals))))
    return delta, rank, condition, plane_rmse


def solve_bounded_scale_delta(
    moving_positions_float64: object,
    reference_positions_float64: object,
    scale_limits: tuple[float, float],
) -> Sim3Transform:
    """공통 Umeyama형 수치 경계로 제한된 proper Sim(3) delta를 계산합니다."""

    try:
        matrix = solve_bounded_scale_matrix(
            moving_positions_float64,
            reference_positions_float64,
            scale_limits,
        )
    except BoundedScaleRefineError as error:
        raise IcpNumericalError(str(error)) from error
    return Sim3Transform(matrix_float64=matrix)


@dataclass(frozen=True, slots=True)
class FilteredCorrespondences:
    """고유 target·MAD·trim을 통과한 한 ICP 반복의 대응점 인덱스입니다."""

    moving_indices_int64: np.ndarray
    reference_indices_int64: np.ndarray
    distances_float64: np.ndarray
    query_count: int
    maximum_distance_count: int
    unique_target_count: int
    mad_gate: float
    trim_fraction: float
    correspondence_signature: str
    normal_valid_count: int = 0
    normal_angle_pass_count: int = 0

    def __post_init__(self) -> None:
        """필터 단계별 count와 inlier 배열의 일관성을 검증합니다."""

        moving = _freeze_int64(self.moving_indices_int64, (None,), "ICP P 대응 인덱스")
        reference = _freeze_int64(
            self.reference_indices_int64,
            (moving.shape[0],),
            "ICP A 대응 인덱스",
        )
        distances = _freeze_float64(
            self.distances_float64,
            (moving.shape[0],),
            "ICP 대응 거리",
        )
        if isinstance(self.query_count, bool) or not isinstance(self.query_count, (int, np.integer)):
            raise IcpInputError("ICP correspondence query 수는 bool이 아닌 정수여야 합니다.")
        if isinstance(self.maximum_distance_count, bool) or not isinstance(
            self.maximum_distance_count,
            (int, np.integer),
        ):
            raise IcpInputError("ICP correspondence 최대 거리 수는 bool이 아닌 정수여야 합니다.")
        if isinstance(self.unique_target_count, bool) or not isinstance(
            self.unique_target_count,
            (int, np.integer),
        ):
            raise IcpInputError("ICP correspondence 고유 target 수는 bool이 아닌 정수여야 합니다.")
        if isinstance(self.normal_valid_count, bool) or not isinstance(
            self.normal_valid_count,
            (int, np.integer),
        ):
            raise IcpInputError("ICP correspondence 유효 노멀 수는 bool이 아닌 정수여야 합니다.")
        if isinstance(self.normal_angle_pass_count, bool) or not isinstance(
            self.normal_angle_pass_count,
            (int, np.integer),
        ):
            raise IcpInputError("ICP correspondence 노멀 각도 통과 수는 bool이 아닌 정수여야 합니다.")
        query_count = int(self.query_count)
        maximum_count = int(self.maximum_distance_count)
        unique_count = int(self.unique_target_count)
        gate = _strict_finite_float(self.mad_gate, "ICP MAD gate")
        trim = _strict_finite_float(self.trim_fraction, "ICP trim 비율")
        normal_valid_count = int(self.normal_valid_count)
        normal_angle_pass_count = int(self.normal_angle_pass_count)
        if (
            moving.size == 0
            or np.any(moving < 0)
            or np.any(reference < 0)
            or np.any(distances < 0.0)
            or query_count < 1
            or maximum_count < moving.size
            or unique_count < moving.size
            or maximum_count > query_count
            or unique_count > maximum_count
            or gate <= 0.0
            or not 0.0 < trim <= 1.0
            or normal_valid_count < 0
            or normal_angle_pass_count < 0
            or normal_valid_count > query_count
            or normal_angle_pass_count > normal_valid_count
        ):
            raise IcpInputError("ICP correspondence 필터 결과가 올바르지 않습니다.")
        object.__setattr__(self, "moving_indices_int64", moving)
        object.__setattr__(self, "reference_indices_int64", reference)
        object.__setattr__(self, "distances_float64", distances)
        object.__setattr__(self, "query_count", query_count)
        object.__setattr__(self, "maximum_distance_count", maximum_count)
        object.__setattr__(self, "unique_target_count", unique_count)
        object.__setattr__(self, "mad_gate", gate)
        object.__setattr__(self, "trim_fraction", trim)
        object.__setattr__(self, "correspondence_signature", str(self.correspondence_signature).strip())
        object.__setattr__(self, "normal_valid_count", normal_valid_count)
        object.__setattr__(self, "normal_angle_pass_count", normal_angle_pass_count)

    @property
    def inlier_count(self) -> int:
        """Kabsch에 전달할 최종 inlier 수를 반환합니다."""

        return int(self.moving_indices_int64.size)


def _correspondence_signature(moving_indices: np.ndarray, reference_indices: np.ndarray) -> str:
    """두 인덱스 배열에서 반복 수렴 판정용 결정적 서명을 생성합니다."""

    digest = hashlib.blake2b(digest_size=16)
    digest.update(np.ascontiguousarray(moving_indices, dtype=np.dtype("<i8")).tobytes())
    digest.update(np.ascontiguousarray(reference_indices, dtype=np.dtype("<i8")).tobytes())
    return digest.hexdigest()


def filter_correspondences(
    query_result: NearestNeighborQueryResult,
    *,
    maximum_distance: float,
    voxel_size: float,
    trim_fraction: float,
    minimum_correspondences: int = 30,
    mad_multiplier: float = 3.0,
) -> FilteredCorrespondences:
    """최대 거리→고유 target→MAD→trim 순서로 ICP correspondence를 강건하게 필터링합니다."""

    if not isinstance(query_result, NearestNeighborQueryResult):
        raise IcpInputError("ICP correspondence에는 NearestNeighborQueryResult가 필요합니다.")
    maximum = _strict_finite_float(maximum_distance, "ICP 최대 대응 거리")
    voxel = _strict_finite_float(voxel_size, "ICP voxel 크기")
    trim = _strict_finite_float(trim_fraction, "ICP trim 비율")
    if isinstance(minimum_correspondences, bool) or not isinstance(minimum_correspondences, (int, np.integer)):
        raise IcpInputError("ICP 최소 대응점 수는 bool이 아닌 정수여야 합니다.")
    required = int(minimum_correspondences)
    multiplier = _strict_finite_float(mad_multiplier, "ICP MAD 배수")
    if (
        maximum <= 0.0
        or voxel <= 0.0
        or not 0.0 < trim <= 1.0
        or required < 3
        or multiplier <= 0.0
    ):
        raise IcpInputError("ICP correspondence 필터 설정이 올바르지 않습니다.")
    raw_indices = query_result.reference_indices_int64
    raw_distances = query_result.distances_float64
    valid = (raw_indices >= 0) & np.isfinite(raw_distances) & (raw_distances <= maximum)
    candidate_moving = np.flatnonzero(valid).astype(np.int64, copy=False)
    maximum_count = int(candidate_moving.size)
    if maximum_count < required:
        raise IcpDegeneracyError("최대 거리 gate 뒤 ICP 대응점이 부족합니다.")
    candidate_reference = raw_indices[candidate_moving]
    candidate_distances = raw_distances[candidate_moving]
    # target 인덱스를 첫 키로, 거리·P 인덱스를 tie-break로 둬 한 A 점만 남깁니다.
    target_order = np.lexsort((candidate_moving, candidate_distances, candidate_reference))
    ordered_targets = candidate_reference[target_order]
    first = np.empty(target_order.shape[0], dtype=bool)
    first[0] = True
    first[1:] = ordered_targets[1:] != ordered_targets[:-1]
    unique_order = target_order[first]
    unique_moving = candidate_moving[unique_order]
    unique_reference = candidate_reference[unique_order]
    unique_distances = candidate_distances[unique_order]
    unique_count = int(unique_moving.size)
    if unique_count < required:
        raise IcpDegeneracyError("고유 target 필터 뒤 ICP 대응점이 부족합니다.")
    median = float(np.median(unique_distances))
    mad = float(np.median(np.abs(unique_distances - median)))
    robust_sigma = max(1.4826 * mad, voxel)
    mad_gate = min(maximum, median + multiplier * robust_sigma)
    mad_mask = unique_distances <= mad_gate
    mad_moving = unique_moving[mad_mask]
    mad_reference = unique_reference[mad_mask]
    mad_distances = unique_distances[mad_mask]
    if mad_moving.size < required:
        raise IcpDegeneracyError("MAD gate 뒤 ICP 대응점이 부족합니다.")
    residual_order = np.lexsort((mad_moving, mad_reference, mad_distances))
    keep_count = max(required, int(np.ceil(mad_moving.size * trim)))
    if keep_count > mad_moving.size:
        raise IcpDegeneracyError("trim 비율 뒤 ICP 대응점이 부족합니다.")
    kept = residual_order[:keep_count]
    moving = mad_moving[kept]
    reference = mad_reference[kept]
    distances = mad_distances[kept]
    return FilteredCorrespondences(
        moving_indices_int64=moving,
        reference_indices_int64=reference,
        distances_float64=distances,
        query_count=query_result.query_count,
        maximum_distance_count=maximum_count,
        unique_target_count=unique_count,
        mad_gate=mad_gate,
        trim_fraction=trim,
        correspondence_signature=_correspondence_signature(moving, reference),
    )


def filter_hybrid_correspondences(
    query_result: NearestNeighborQueryResult,
    *,
    maximum_distance: float,
    voxel_size: float,
    trim_fraction: float,
    minimum_correspondences: int,
    mad_multiplier: float,
    objective_kind: IcpObjectiveKind,
    moving_normals_float64: np.ndarray | None = None,
    reference_normals_float64: np.ndarray | None = None,
    maximum_angle_degrees: float = 90.0,
    use_absolute_dot: bool = True,
) -> FilteredCorrespondences:
    """point-to-plane에서는 거리 뒤 노멀 유효·각도 gate를 추가로 적용합니다."""

    if not isinstance(objective_kind, IcpObjectiveKind):
        raise IcpInputError("ICP 혼합 correspondence 목적함수가 올바르지 않습니다.")
    if objective_kind is IcpObjectiveKind.POINT_TO_POINT:
        correspondences = filter_correspondences(
            query_result,
            maximum_distance=maximum_distance,
            voxel_size=voxel_size,
            trim_fraction=trim_fraction,
            minimum_correspondences=minimum_correspondences,
            mad_multiplier=mad_multiplier,
        )
        return replace(
            correspondences,
            normal_valid_count=correspondences.maximum_distance_count,
            normal_angle_pass_count=correspondences.maximum_distance_count,
        )
    if moving_normals_float64 is None or reference_normals_float64 is None:
        raise IcpInputError("point-to-plane correspondence에는 양쪽 노멀이 필요합니다.")
    moving_normals = _freeze_float64(moving_normals_float64, (query_result.query_count, 3), "ICP P 노멀")
    reference_normals = _freeze_float64(reference_normals_float64, (None, 3), "ICP A 노멀")
    raw_indices = query_result.reference_indices_int64
    raw_distances = query_result.distances_float64
    maximum = _strict_finite_float(maximum_distance, "ICP 최대 대응 거리")
    distance_mask = (raw_indices >= 0) & np.isfinite(raw_distances) & (raw_distances <= maximum)
    valid_indices = np.flatnonzero(distance_mask)
    valid_targets = raw_indices[valid_indices]
    valid_normal_mask = np.zeros(query_result.query_count, dtype=bool)
    if valid_indices.size:
        if np.any(valid_targets < 0) or np.any(valid_targets >= reference_normals.shape[0]):
            raise IcpInputError("ICP 노멀 대응 target 인덱스 범위가 올바르지 않습니다.")
        moving_lengths = np.linalg.norm(moving_normals[valid_indices], axis=1)
        reference_lengths = np.linalg.norm(reference_normals[valid_targets], axis=1)
        valid_normal_mask[valid_indices] = (
            (moving_lengths > np.finfo(np.float64).eps)
            & (reference_lengths > np.finfo(np.float64).eps)
        )
    normal_valid_count = int(np.count_nonzero(valid_normal_mask))
    angle_mask = np.zeros(query_result.query_count, dtype=bool)
    if normal_valid_count:
        indices = np.flatnonzero(valid_normal_mask)
        angle_mask[indices] = normal_angle_mask(
            moving_normals[indices],
            reference_normals[raw_indices[indices]],
            maximum_angle_degrees=maximum_angle_degrees,
            use_absolute_dot=use_absolute_dot,
        )
    normal_angle_pass_count = int(np.count_nonzero(angle_mask))
    if normal_valid_count < minimum_correspondences:
        raise IcpNormalCoverageError("유효 PCA 노멀 대응점이 point-to-plane 최소 수보다 적습니다.")
    if normal_angle_pass_count < minimum_correspondences:
        raise IcpNormalCoverageError("노멀 각도 gate를 통과한 대응점이 point-to-plane 최소 수보다 적습니다.")
    filtered_indices = raw_indices.copy()
    filtered_distances = raw_distances.copy()
    filtered_indices[~angle_mask] = -1
    filtered_distances[~angle_mask] = np.inf
    try:
        correspondences = filter_correspondences(
            NearestNeighborQueryResult(filtered_indices, filtered_distances),
            maximum_distance=maximum,
            voxel_size=voxel_size,
            trim_fraction=trim_fraction,
            minimum_correspondences=minimum_correspondences,
            mad_multiplier=mad_multiplier,
        )
    except IcpDegeneracyError as filtered_error:
        # 원본 거리 대응점이 충분한데 노멀 gate 뒤에만 부족해진 경우는
        # 기하 퇴화가 아니라 다음 point-to-plane 레벨의 노멀 근거 부족입니다.
        filter_correspondences(
            query_result,
            maximum_distance=maximum,
            voxel_size=voxel_size,
            trim_fraction=trim_fraction,
            minimum_correspondences=minimum_correspondences,
            mad_multiplier=mad_multiplier,
        )
        raise IcpNormalCoverageError(
            "노멀 gate 뒤 강건 필터를 통과한 point-to-plane 대응점이 부족합니다."
        ) from filtered_error
    return replace(
        correspondences,
        # point-to-plane의 최대 거리 통계는 노멀 gate 전 후보 수를 유지해야
        # 노멀 유효·각도 통과 비율이 같은 분모에서 해석됩니다.
        maximum_distance_count=int(valid_indices.size),
        normal_valid_count=normal_valid_count,
        normal_angle_pass_count=normal_angle_pass_count,
    )


def _local_delta_to_world(delta_local: Sim3Transform, local_origin: np.ndarray) -> Sim3Transform:
    """local-origin 좌표계에서 푼 delta를 같은 효과의 Project World Sim(3)으로 바꿉니다."""

    origin = _freeze_float64(local_origin, (3,), "ICP local origin")
    scale = float(delta_local.uniform_scale)
    rotation = delta_local.rotation_float64
    translation = origin + delta_local.translation_float64 - scale * (rotation @ origin)
    return create_sim3_transform(rotation, scale, translation)


def _new_index(index_factory: IndexFactory, query_chunk_size: int) -> NearestNeighborIndex:
    """테스트 index factory 또는 실제 Blender KD-tree 구현체를 한 개 생성합니다."""

    if index_factory is None:
        return default_nearest_neighbor_index(query_chunk_size=query_chunk_size)
    try:
        index = index_factory(query_chunk_size=query_chunk_size)
    except TypeError:
        index = index_factory()
    if not isinstance(index, NearestNeighborIndex):
        raise IcpInputError("ICP index factory가 NearestNeighborIndex 구현체를 반환하지 않았습니다.")
    return index


def _level_progress(
    level_index: int,
    level_count: int,
    iteration: int,
    maximum_iterations: int,
) -> float:
    """해상도·반복 번호를 0.10~0.90 범위의 단조 worker 진행률로 변환합니다."""

    level_fraction = (iteration / max(maximum_iterations, 1))
    return min(0.90, 0.10 + 0.80 * ((level_index + level_fraction) / level_count))


def _normal_estimation_stage(level: IcpLevelParameters) -> str:
    """레벨 이름에 맞는 고정 PCA 노멀 진행 단계를 반환합니다."""

    if level.name == "coarse":
        return ICP_STAGE_ESTIMATING_NORMALS_COARSE
    if level.name == "fine":
        return ICP_STAGE_ESTIMATING_NORMALS_FINE
    return ICP_STAGE_ESTIMATING_NORMALS_MEDIUM


def _iteration_stage(level: IcpLevelParameters) -> str:
    """목적함수와 레벨에 맞는 고정 ICP 반복 진행 단계를 반환합니다."""

    if level.objective_kind is IcpObjectiveKind.POINT_TO_POINT:
        return ICP_STAGE_COARSE_POINT_TO_POINT
    if level.name == "fine":
        return ICP_STAGE_FINE_POINT_TO_PLANE
    return ICP_STAGE_MEDIUM_POINT_TO_PLANE


def _initial_overlap_distance(diagonal: float, voxel_size: float, marker_max_error: float) -> float:
    """marker 오차와 coarse voxel을 함께 반영한 첫 최대 대응 거리를 계산합니다."""

    if not isfinite(diagonal) or diagonal <= 0.0:
        raise IcpInputError("ICP overlap 대각선이 유한한 양수가 아닙니다.")
    distance = min(0.25 * diagonal, max(3.0 * voxel_size, 2.0 * marker_max_error))
    if not isfinite(distance) or distance <= 0.0:
        raise IcpInputError("ICP 첫 최대 대응 거리를 계산하지 못했습니다.")
    return distance


def _refinable_overlap_model_ids(
    candidates: Sequence[IcpOverlapCandidate],
    minimum_moving_coverage: float,
) -> frozenset[str]:
    """기본 격자 신호는 있으나 고정 공통 cell gate를 못 넘은 A 모델 ID를 반환합니다."""

    coverage = _strict_finite_float(minimum_moving_coverage, "최소 moving coverage")
    if coverage > 1.0:
        raise IcpInputError("최소 moving coverage는 1을 넘을 수 없습니다.")
    return frozenset(
        candidate.reference_model_id
        for candidate in candidates
        if not candidate.selected
        and candidate.aabb_intersects
        and candidate.common_cell_count > 0
        and candidate.moving_coverage >= coverage
    )


def _merge_overlap_candidates_with_previous_selection(
    candidates: Sequence[IcpOverlapCandidate],
    selected_by_model_id: dict[str, IcpOverlapCandidate],
) -> tuple[IcpOverlapCandidate, ...]:
    """이전 해상도에서 통과한 A 후보를 보존해 다중 A 합집합 선택을 유지합니다."""

    return tuple(
        selected_by_model_id.get(candidate.reference_model_id, candidate)
        for candidate in candidates
    )


def _selected_overlap_candidates_by_model_id(
    candidates: Sequence[IcpOverlapCandidate],
) -> dict[str, IcpOverlapCandidate]:
    """현재 격자에서 통과한 A 후보를 모델 ID별로 결정적으로 보관합니다."""

    return {
        candidate.reference_model_id: candidate
        for candidate in candidates
        if candidate.selected
    }


def _find_overlap_candidates_with_refined_voxel(
    moving_cloud: IcpPointCloud,
    reference_clouds: Sequence[IcpPointCloud],
    *,
    maximum_distance: float,
    base_voxel_size: float,
    minimum_common_cells: int,
    minimum_moving_coverage: float,
    maximum_occupancy_cells: int,
    cancel_probe: CancelProbe,
) -> tuple[tuple[IcpOverlapCandidate, ...], float]:
    """기본 신호 A만 최대 네 단계 finer voxel로 재평가해 다중 A 후보 합집합을 반환합니다."""

    base_voxel = _strict_finite_float(
        base_voxel_size,
        "ICP overlap 기본 복셀 크기",
    )
    if base_voxel <= 0.0:
        raise IcpInputError("ICP overlap 기본 복셀 크기는 양수여야 합니다.")
    candidates = find_overlap_candidates(
        moving_cloud,
        reference_clouds,
        maximum_distance=maximum_distance,
        occupancy_voxel_size=base_voxel,
        minimum_common_cells=minimum_common_cells,
        minimum_moving_coverage=minimum_moving_coverage,
        maximum_occupancy_cells=maximum_occupancy_cells,
        cancel_probe=cancel_probe,
    )
    selected_by_model_id = _selected_overlap_candidates_by_model_id(candidates)
    pending_model_ids = _refinable_overlap_model_ids(candidates, minimum_moving_coverage)
    if not pending_model_ids:
        return candidates, base_voxel

    voxel_size = base_voxel
    for _step in range(_MAX_OVERLAP_VOXEL_REFINEMENT_STEPS):
        _raise_if_cancelled(cancel_probe)
        voxel_size *= 0.5
        candidates = find_overlap_candidates(
            moving_cloud,
            reference_clouds,
            maximum_distance=maximum_distance,
            occupancy_voxel_size=voxel_size,
            minimum_common_cells=minimum_common_cells,
            minimum_moving_coverage=minimum_moving_coverage,
            maximum_occupancy_cells=maximum_occupancy_cells,
            cancel_probe=cancel_probe,
        )
        selected_by_model_id.update(_selected_overlap_candidates_by_model_id(candidates))
        pending_model_ids -= set(selected_by_model_id)
        if not pending_model_ids:
            return _merge_overlap_candidates_with_previous_selection(
                candidates,
                selected_by_model_id,
            ), voxel_size
    return _merge_overlap_candidates_with_previous_selection(
        candidates,
        selected_by_model_id,
    ), voxel_size


def _is_significant_rmse_increase(
    previous_rmse: float,
    current_rmse: float,
    *,
    voxel_size: float,
    overlap_diagonal: float,
) -> bool:
    """복셀·좌표 스케일 noise floor를 넘는 실제 RMSE 증가만 발산 후보로 판정합니다."""

    previous = float(previous_rmse)
    current = float(current_rmse)
    voxel = float(voxel_size)
    diagonal = float(overlap_diagonal)
    if not all(isfinite(value) and value >= 0.0 for value in (previous, current)) or voxel <= 0.0 or diagonal <= 0.0:
        raise IcpInputError("ICP RMSE 증가 판정 입력 범위가 올바르지 않습니다.")
    coordinate_scale = max(diagonal, voxel, 1.0)
    tolerance = max(voxel * 1.0e-12, 64.0 * np.finfo(np.float64).eps * coordinate_scale)
    return current > previous + max(previous * 1.0e-5, tolerance)


def _effective_trim_fraction(level: IcpLevelParameters, overlap_ratio: float) -> float:
    """점유 grid의 합성 P coverage를 반영하되 trim을 40% 아래로 내리지 않습니다."""

    if not isinstance(level, IcpLevelParameters):
        raise IcpInputError("ICP 실제 trim 계산에는 IcpLevelParameters가 필요합니다.")
    coverage = float(overlap_ratio)
    if not isfinite(coverage) or not 0.0 <= coverage <= 1.0:
        raise IcpInputError("ICP 합성 overlap coverage 범위가 올바르지 않습니다.")
    return max(0.40, min(level.trim_fraction, coverage))


def _has_excessive_level_inlier_drop(previous_count: int, current_count: int) -> bool:
    """다음 레벨 inlier가 직전 레벨의 20% 미만으로 급감했는지 반환합니다."""

    if isinstance(previous_count, bool) or isinstance(current_count, bool):
        raise IcpInputError("ICP 레벨 inlier 수는 정수여야 합니다.")
    if not isinstance(previous_count, (int, np.integer)) or not isinstance(current_count, (int, np.integer)):
        raise IcpInputError("ICP 레벨 inlier 수는 정수여야 합니다.")
    previous = int(previous_count)
    current = int(current_count)
    if previous < 0 or current < 0:
        raise IcpInputError("ICP 레벨 inlier 수는 음수일 수 없습니다.")
    return current < 0.20 * previous


def _reference_crop_bounds(
    moving_bounds: AxisAlignedBounds,
    reference_clouds: Sequence[IcpPointCloud],
    maximum_distance: float,
) -> tuple[dict[str, tuple[np.ndarray, np.ndarray]], AxisAlignedBounds]:
    """선택된 A마다 실제 P crop 안의 AABB만 계산해 원거리 outlier 확장을 막습니다."""

    candidate_bounds: dict[str, tuple[np.ndarray, np.ndarray]] = {}
    selected_bounds: list[AxisAlignedBounds] = []
    moving_expanded = moving_bounds.expanded(maximum_distance)
    for cloud in reference_clouds:
        reference_bounds = bounds_from_cloud(cloud)
        intersection = bounds_intersection(moving_expanded, reference_bounds)
        if intersection is None:
            continue
        try:
            cropped_bounds = bounds_from_cloud(cloud, crop_bounds=moving_expanded)
        except IcpInputError:
            continue
        candidate_bounds[cloud.model_id] = (
            moving_expanded.minimum_float64,
            moving_expanded.maximum_float64,
        )
        selected_bounds.append(cropped_bounds)
    if not candidate_bounds or not selected_bounds:
        raise IcpOverlapError("자동 ICP 겹침 후보 안에 사용할 A 점이 없습니다.")
    union_minimum = np.min(np.asarray([item.minimum_float64 for item in selected_bounds]), axis=0)
    union_maximum = np.max(np.asarray([item.maximum_float64 for item in selected_bounds]), axis=0)
    return candidate_bounds, AxisAlignedBounds(union_minimum, union_maximum)


def _combine_reference_samples(
    samples: Sequence[VoxelSample],
    model_ids: Sequence[str],
) -> tuple[np.ndarray, np.ndarray]:
    """모델별 A 샘플을 임시 연속 배열로 합치고 reference_model_index를 보존합니다."""

    normalized_samples = tuple(samples)
    normalized_ids = tuple(model_ids)
    if len(normalized_samples) != len(normalized_ids) or not normalized_samples:
        raise IcpInputError("ICP 다중 A 샘플과 모델 ID 길이가 올바르지 않습니다.")
    positions = np.concatenate([sample.positions_float64 for sample in normalized_samples], axis=0)
    model_indices = np.concatenate(
        [np.full(sample.count, index, dtype=np.int64) for index, sample in enumerate(normalized_samples)],
        axis=0,
    )
    return _freeze_float64(positions, (None, 3), "ICP 합성 A 샘플"), _freeze_int64(
        model_indices,
        (positions.shape[0],),
        "ICP 합성 A 모델 인덱스",
    )


@dataclass(frozen=True, slots=True)
class _LevelSamples:
    """한 해상도 레벨의 P/A 대표점과 multi-A 모델 인덱스를 보관합니다."""

    moving: VoxelSample
    reference_positions_float64: np.ndarray
    reference_model_indices_int64: np.ndarray
    reference_model_ids: tuple[str, ...]
    voxel_size: float
    moving_normals: IcpNormalEstimate
    reference_normals_float64: np.ndarray
    reference_normal_valid_mask: np.ndarray
    density_sampling_metrics: IcpDensitySamplingMetrics
    evaluation_moving_indices_int64: np.ndarray
    evaluation_fingerprint: str


def _build_level_samples(
    *,
    moving_initial_cloud: IcpPointCloud,
    selected_reference_clouds: Sequence[IcpPointCloud],
    overlap_cell_counts: dict[str, int],
    moving_candidate_bounds: tuple[np.ndarray, np.ndarray],
    reference_candidate_bounds: dict[str, tuple[np.ndarray, np.ndarray]],
    level: IcpLevelParameters,
    common_origin: np.ndarray,
    index_factory: IndexFactory,
    cancel_probe: CancelProbe,
) -> _LevelSamples:
    """공통 voxel·macro quota·레벨별 PCA 노멀을 한 worker 표본으로 준비합니다."""

    if not selected_reference_clouds:
        raise IcpInputError("ICP 레벨 샘플에는 선택된 A 점군이 필요합니다.")
    reference_minimum = np.min(
        np.asarray([bounds[0] for bounds in reference_candidate_bounds.values()]),
        axis=0,
    )
    reference_maximum = np.max(
        np.asarray([bounds[1] for bounds in reference_candidate_bounds.values()]),
        axis=0,
    )
    overlap_bounds = (
        np.minimum(moving_candidate_bounds[0], reference_minimum),
        np.maximum(moving_candidate_bounds[1], reference_maximum),
    )
    shared_voxel = choose_shared_voxel_size(
        moving_initial_cloud,
        selected_reference_clouds,
        moving_budget=level.moving_budget,
        reference_budget=level.reference_budget,
        origin_float64=common_origin,
        moving_candidate_bounds_float64=moving_candidate_bounds,
        reference_candidate_bounds_by_model=reference_candidate_bounds,
        cancel_probe=cancel_probe,
    )
    moving_sample = build_uniform_density_sample(
        moving_initial_cloud,
        level.moving_budget,
        shared_voxel_size=shared_voxel,
        shared_origin_float64=common_origin,
        overlap_bounds_float64=overlap_bounds,
        macro_cell_divisions=level.macro_cell_divisions,
        candidate_bounds_float64=moving_candidate_bounds,
        cancel_probe=cancel_probe,
    )
    reference_counts = {cloud.model_id: cloud.count for cloud in selected_reference_clouds}
    quotas = allocate_multi_reference_quotas(
        reference_counts,
        overlap_cell_counts,
        level.reference_budget,
    )
    reference_samples: list[VoxelSample] = []
    model_ids: list[str] = []
    for cloud in selected_reference_clouds:
        quota = quotas.get(cloud.model_id, 0)
        if quota <= 0:
            continue
        _raise_if_cancelled(cancel_probe)
        reference_samples.append(
            build_uniform_density_sample(
                cloud,
                quota,
                shared_voxel_size=shared_voxel,
                shared_origin_float64=common_origin,
                overlap_bounds_float64=overlap_bounds,
                macro_cell_divisions=level.macro_cell_divisions,
                candidate_bounds_float64=reference_candidate_bounds.get(cloud.model_id),
                cancel_probe=cancel_probe,
            )
        )
        model_ids.append(cloud.model_id)
    positions, model_indices = _combine_reference_samples(reference_samples, model_ids)
    normal_radius = level.normal_radius_multiplier * shared_voxel
    moving_index = _new_index(index_factory, 2_048)
    try:
        moving_local = moving_sample.positions_float64 - common_origin[np.newaxis, :]
        moving_index.build(moving_local, cancel_probe=cancel_probe)
        moving_normals = estimate_sample_normals(
            moving_local,
            index=moving_index,
            radius=normal_radius,
            minimum_neighbors=level.normal_minimum_neighbors,
            maximum_neighbors=level.normal_maximum_neighbors,
            minimum_planarity=level.normal_minimum_planarity,
            maximum_curvature=level.normal_maximum_curvature,
            cancel_probe=cancel_probe,
        )
    finally:
        moving_index.close()
    reference_normal_arrays: list[np.ndarray] = []
    reference_valid_arrays: list[np.ndarray] = []
    for sample in reference_samples:
        _raise_if_cancelled(cancel_probe)
        reference_index = _new_index(index_factory, 2_048)
        try:
            reference_local = sample.positions_float64 - common_origin[np.newaxis, :]
            reference_index.build(reference_local, cancel_probe=cancel_probe)
            estimate = estimate_sample_normals(
                reference_local,
                index=reference_index,
                radius=normal_radius,
                minimum_neighbors=level.normal_minimum_neighbors,
                maximum_neighbors=level.normal_maximum_neighbors,
                minimum_planarity=level.normal_minimum_planarity,
                maximum_curvature=level.normal_maximum_curvature,
                cancel_probe=cancel_probe,
            )
            reference_normal_arrays.append(estimate.normals_float64)
            reference_valid_arrays.append(estimate.valid_mask)
        finally:
            reference_index.close()
    reference_normals = _freeze_float64(
        np.concatenate(reference_normal_arrays, axis=0),
        (positions.shape[0], 3),
        "ICP A PCA 노멀",
    )
    reference_valid = np.ascontiguousarray(np.concatenate(reference_valid_arrays, axis=0), dtype=bool)
    reference_valid.setflags(write=False)
    macro_scaled = (moving_sample.positions_float64 - overlap_bounds[0][np.newaxis, :]) / np.maximum(
        overlap_bounds[1] - overlap_bounds[0],
        np.finfo(np.float64).eps,
    )
    macro_keys = np.floor(np.clip(macro_scaled, 0.0, 1.0 - np.finfo(np.float64).eps) * level.macro_cell_divisions).astype(np.int64)
    _keys, macro_counts = np.unique(macro_keys, axis=0, return_counts=True)
    density_metrics = IcpDensitySamplingMetrics(
        shared_voxel_size=shared_voxel,
        macro_cell_divisions=level.macro_cell_divisions,
        macro_cell_count=level.macro_cell_divisions**3,
        occupied_macro_cell_count=int(macro_counts.size),
        minimum_samples_per_macro_cell=int(np.min(macro_counts)),
        median_samples_per_macro_cell=float(np.median(macro_counts)),
        maximum_samples_per_macro_cell=int(np.max(macro_counts)),
    )
    evaluation_count = min(level.evaluation_budget, moving_sample.count)
    evaluation_order = np.argsort(moving_sample.point_ids_uint64, kind="stable")[:evaluation_count]
    evaluation_indices = np.ascontiguousarray(evaluation_order, dtype=np.int64)
    evaluation_indices.setflags(write=False)
    evaluation_digest = hashlib.blake2b(digest_size=16)
    evaluation_digest.update(np.ascontiguousarray(moving_sample.point_ids_uint64[evaluation_indices]).tobytes())
    evaluation_digest.update(np.ascontiguousarray(positions).tobytes())
    # 같은 좌표라도 다중 A의 모델 기여 순서가 다르면 다른 고정 평가 표본으로 구분합니다.
    evaluation_digest.update(np.ascontiguousarray(model_indices, dtype=np.int64).tobytes())
    evaluation_digest.update("\x1f".join(model_ids).encode("utf-8"))
    return _LevelSamples(
        moving=moving_sample,
        reference_positions_float64=positions,
        reference_model_indices_int64=model_indices,
        reference_model_ids=tuple(model_ids),
        voxel_size=float(shared_voxel),
        moving_normals=moving_normals,
        reference_normals_float64=reference_normals,
        reference_normal_valid_mask=reference_valid,
        density_sampling_metrics=density_metrics,
        evaluation_moving_indices_int64=evaluation_indices,
        evaluation_fingerprint=evaluation_digest.hexdigest(),
    )


def evaluate_candidate_on_fixed_sample(
    *,
    current_delta: Sim3Transform,
    samples: _LevelSamples,
    reference_local: np.ndarray,
    local_origin: np.ndarray,
    index: NearestNeighborIndex,
    maximum_distance: float,
    level: IcpLevelParameters,
    overlap_ratio: float,
    parameters: IcpParameters,
    cancel_probe: CancelProbe,
) -> tuple[IcpEvaluationMetrics, FilteredCorrespondences]:
    """고정 P 평가 표본에서 후보의 비교 가능한 point·plane 지표를 계산합니다."""

    evaluation_indices = samples.evaluation_moving_indices_int64
    working_world = apply_sim3_transform(
        current_delta,
        samples.moving.positions_float64[evaluation_indices],
    )
    working_local = working_world - local_origin[np.newaxis, :]
    query = index.query(
        working_local,
        maximum_distance=maximum_distance,
        cancel_probe=cancel_probe,
    )
    transformed_normals = transform_normals(
        samples.moving_normals.normals_float64[evaluation_indices],
        current_delta,
    )
    minimum_correspondences = min(parameters.minimum_correspondences, evaluation_indices.size)
    if level.objective_kind is IcpObjectiveKind.POINT_TO_PLANE:
        minimum_correspondences = max(6, minimum_correspondences)
    correspondences = filter_hybrid_correspondences(
        query,
        maximum_distance=maximum_distance,
        voxel_size=samples.voxel_size,
        trim_fraction=_effective_trim_fraction(level, overlap_ratio),
        minimum_correspondences=minimum_correspondences,
        mad_multiplier=parameters.mad_multiplier,
        objective_kind=level.objective_kind,
        moving_normals_float64=transformed_normals,
        reference_normals_float64=samples.reference_normals_float64,
        maximum_angle_degrees=level.normal_maximum_angle_degrees,
        use_absolute_dot=parameters.normal_angle_uses_absolute_dot,
    )
    point_rmse = float(np.sqrt(np.mean(np.square(correspondences.distances_float64))))
    plane_rmse = 0.0
    if level.objective_kind is IcpObjectiveKind.POINT_TO_PLANE:
        source = working_local[correspondences.moving_indices_int64]
        target = reference_local[correspondences.reference_indices_int64]
        normals = samples.reference_normals_float64[correspondences.reference_indices_int64]
        residuals = np.einsum("ij,ij->i", normals, source - target, optimize=True)
        plane_rmse = float(np.sqrt(np.mean(np.square(residuals))))
    evaluation = IcpEvaluationMetrics(
        point_to_point_rmse=point_rmse,
        point_to_plane_rmse=plane_rmse,
        fitness=correspondences.inlier_count / max(correspondences.query_count, 1),
        reciprocal_ratio=0.0,
        inlier_count=correspondences.inlier_count,
        query_count=correspondences.query_count,
        maximum_correspondence_distance=maximum_distance,
        normal_valid_ratio=(
            correspondences.normal_valid_count / max(correspondences.maximum_distance_count, 1)
        ),
        normal_angle_pass_ratio=(
            correspondences.normal_angle_pass_count / max(correspondences.normal_valid_count, 1)
        ),
        evaluation_fingerprint=samples.evaluation_fingerprint,
    )
    return evaluation, correspondences


def _evaluate_fixed_candidate(
    *,
    current_delta: Sim3Transform,
    samples: _LevelSamples,
    level: IcpLevelParameters,
    local_origin: np.ndarray,
    maximum_distance: float,
    overlap_ratio: float,
    parameters: IcpParameters,
    index_factory: IndexFactory,
    cancel_probe: CancelProbe,
) -> tuple[IcpEvaluationMetrics, FilteredCorrespondences]:
    """최종 품질 비교용 고정 표본 평가 index를 열고 안전하게 닫습니다."""

    index = _new_index(index_factory, parameters.query_chunk_size)
    reference_local = samples.reference_positions_float64 - local_origin[np.newaxis, :]
    try:
        index.build(reference_local, cancel_probe=cancel_probe)
        return evaluate_candidate_on_fixed_sample(
            current_delta=current_delta,
            samples=samples,
            reference_local=reference_local,
            local_origin=local_origin,
            index=index,
            maximum_distance=maximum_distance,
            level=level,
            overlap_ratio=overlap_ratio,
            parameters=parameters,
            cancel_probe=cancel_probe,
        )
    finally:
        index.close()


def choose_better_candidate(
    candidate: IcpBestCandidate,
    best: IcpBestCandidate | None,
    *,
    voxel_size: float,
) -> bool:
    """같은 고정 평가 표본의 후보를 목적함수·진단 지표 순으로 비교합니다."""

    if not isinstance(candidate, IcpBestCandidate):
        raise IcpInputError("ICP 후보 비교에는 IcpBestCandidate가 필요합니다.")
    if best is None:
        return True
    if not isinstance(best, IcpBestCandidate):
        raise IcpInputError("ICP 최선 후보 형식이 올바르지 않습니다.")
    if candidate.evaluation_fingerprint != best.evaluation_fingerprint:
        # 레벨이 바뀐 경우에는 다음 단계가 기존 최선에서 출발하므로 새 평가 표본의 첫 유효 후보를 채택합니다.
        return True
    primary_candidate = (
        candidate.point_to_plane_rmse
        if candidate.objective_kind is IcpObjectiveKind.POINT_TO_PLANE
        else candidate.point_to_point_rmse
    )
    primary_best = (
        best.point_to_plane_rmse
        if best.objective_kind is IcpObjectiveKind.POINT_TO_PLANE
        else best.point_to_point_rmse
    )
    tolerance = max(float(voxel_size) * 1.0e-12, np.finfo(np.float64).eps * 64.0)
    if primary_candidate < primary_best - tolerance:
        return True
    if abs(primary_candidate - primary_best) <= tolerance:
        if candidate.fitness > best.fitness + 1.0e-12:
            return True
        if np.isclose(candidate.fitness, best.fitness, rtol=0.0, atol=1.0e-12):
            if candidate.inlier_count > best.inlier_count:
                return True
    return False


def next_correspondence_radius(
    current_radius: float,
    *,
    best_p90_residual: float,
    voxel_size: float,
    level: IcpLevelParameters,
) -> float:
    """최선 후보 residual과 voxel 하한을 이용해 다음 반복 반경을 점진 축소합니다."""

    current = _strict_finite_float(current_radius, "ICP 현재 대응 반경")
    p90 = _strict_finite_float(best_p90_residual, "ICP 최선 P90 residual")
    voxel = _strict_finite_float(voxel_size, "ICP voxel 크기")
    lower = level.radius_minimum_voxel_multiplier * voxel
    target = max(lower, 1.5 * p90)
    return float(max(lower, min(current * level.radius_shrink_factor, target)))


def _run_level(
    *,
    level: IcpLevelParameters,
    level_index: int,
    level_count: int,
    samples: _LevelSamples,
    current_delta: Sim3Transform,
    initial_alignment: Sim3Transform,
    local_origin: np.ndarray,
    maximum_distance: float,
    overlap_diagonal: float,
    overlap_ratio: float,
    parameters: IcpParameters,
    index_factory: IndexFactory,
    progress_sink: ProgressSink,
    cancel_probe: CancelProbe,
) -> tuple[Sim3Transform, IcpLevelResult, FilteredCorrespondences, IcpBestCandidate]:
    """한 혼합 ICP 레벨에서 best 후보·반경 축소·REVIEW 종료 사유를 관리합니다."""

    _raise_if_cancelled(cancel_probe)
    effective_trim_fraction = _effective_trim_fraction(level, overlap_ratio)
    if (
        level.objective_kind is IcpObjectiveKind.POINT_TO_PLANE
        and (
            samples.moving_normals.valid_count < max(6, parameters.minimum_correspondences)
            or int(np.count_nonzero(samples.reference_normal_valid_mask)) < max(6, parameters.minimum_correspondences)
        )
    ):
        raise IcpNormalCoverageError("point-to-plane 노멀 coverage가 최소 대응점 수보다 적습니다.")
    index = _new_index(index_factory, parameters.query_chunk_size)
    reference_local = samples.reference_positions_float64 - local_origin[np.newaxis, :]
    iterations: list[IcpIterationMetrics] = []
    current_radius = maximum_distance
    best_candidate: IcpBestCandidate | None = None
    best_correspondences: FilteredCorrespondences | None = None
    previous_objective: float | None = None
    previous_signature = ""
    stable_count = 0
    increasing_count = 0
    converged = False
    stop_reason = IcpSolverStopReason.ITERATION_LIMIT
    started_at = perf_counter()
    try:
        index.build(reference_local, cancel_probe=cancel_probe)
        for iteration in range(1, level.max_iterations + 1):
            _raise_if_cancelled(cancel_probe)
            try:
                working_world = apply_sim3_transform(current_delta, samples.moving.positions_float64)
                working_local = working_world - local_origin[np.newaxis, :]
                query = index.query(
                    working_local,
                    maximum_distance=current_radius,
                    cancel_probe=cancel_probe,
                )
                transformed_normals = transform_normals(samples.moving_normals.normals_float64, current_delta)
                correspondences = filter_hybrid_correspondences(
                    query,
                    maximum_distance=current_radius,
                    voxel_size=samples.voxel_size,
                    trim_fraction=effective_trim_fraction,
                    minimum_correspondences=parameters.minimum_correspondences,
                    mad_multiplier=parameters.mad_multiplier,
                    objective_kind=level.objective_kind,
                    moving_normals_float64=transformed_normals,
                    reference_normals_float64=samples.reference_normals_float64,
                    maximum_angle_degrees=level.normal_maximum_angle_degrees,
                    use_absolute_dot=parameters.normal_angle_uses_absolute_dot,
                )
                source = working_local[correspondences.moving_indices_int64]
                target = reference_local[correspondences.reference_indices_int64]
                if level.objective_kind is IcpObjectiveKind.POINT_TO_POINT:
                    delta_local = solve_rigid_delta(source, target)
                    linear_rank = 3
                    linear_condition = 0.0
                else:
                    delta_local, linear_rank, linear_condition, _plane = solve_point_to_plane_delta(
                        source,
                        target,
                        samples.reference_normals_float64[correspondences.reference_indices_int64],
                        damping=parameters.point_to_plane_damping,
                        condition_limit=parameters.normal_condition_limit,
                        robust_kernel_scale=parameters.robust_kernel_scale,
                    )
                delta_world = _local_delta_to_world(delta_local, local_origin)
                translation_step = float(np.linalg.norm(delta_local.translation_float64))
                rotation_step = rotation_angle_degrees(delta_local.rotation_float64)
                if translation_step > overlap_diagonal * parameters.max_translation_fraction:
                    raise IcpNumericalError("한 ICP 반복의 local 추가 이동이 안전 한도를 초과했습니다.")
                if rotation_step > parameters.max_rotation_degrees:
                    raise IcpNumericalError("한 ICP 반복의 추가 회전이 안전 한도를 초과했습니다.")
                candidate_delta = compose_sim3_transforms(delta_world, current_delta)
                if not np.isclose(candidate_delta.uniform_scale, 1.0, rtol=0.0, atol=1.0e-9):
                    raise IcpNumericalError("고정 스케일 ICP delta에 scale 변화가 생겼습니다.")
                evaluation, evaluation_correspondences = evaluate_candidate_on_fixed_sample(
                    current_delta=candidate_delta,
                    samples=samples,
                    reference_local=reference_local,
                    local_origin=local_origin,
                    index=index,
                    maximum_distance=maximum_distance,
                    level=level,
                    overlap_ratio=overlap_ratio,
                    parameters=parameters,
                    cancel_probe=cancel_probe,
                )
            except IcpNormalCoverageError:
                if best_candidate is None:
                    raise
                stop_reason = IcpSolverStopReason.NORMAL_COVERAGE_INSUFFICIENT
                break
            candidate = IcpBestCandidate(
                delta_transform=candidate_delta,
                final_alignment=compose_sim3_transforms(candidate_delta, initial_alignment),
                level_name=level.name,
                iteration=iteration,
                objective_kind=level.objective_kind,
                point_to_plane_rmse=evaluation.point_to_plane_rmse,
                point_to_point_rmse=evaluation.point_to_point_rmse,
                fitness=evaluation.fitness,
                inlier_count=evaluation.inlier_count,
                reciprocal_ratio=evaluation.reciprocal_ratio,
                normal_valid_ratio=evaluation.normal_valid_ratio,
                normal_angle_pass_ratio=evaluation.normal_angle_pass_ratio,
                maximum_correspondence_distance=evaluation.maximum_correspondence_distance,
                correspondence_signature=evaluation_correspondences.correspondence_signature,
                evaluation_fingerprint=evaluation.evaluation_fingerprint,
            )
            best_updated = choose_better_candidate(
                candidate,
                best_candidate,
                voxel_size=samples.voxel_size,
            )
            if best_updated:
                best_candidate = candidate
                best_correspondences = evaluation_correspondences
            objective = (
                evaluation.point_to_plane_rmse
                if level.objective_kind is IcpObjectiveKind.POINT_TO_PLANE
                else evaluation.point_to_point_rmse
            )
            metrics = IcpIterationMetrics(
                level_name=level.name,
                iteration=iteration,
                query_count=correspondences.query_count,
                maximum_distance_count=correspondences.maximum_distance_count,
                unique_target_count=correspondences.unique_target_count,
                inlier_count=correspondences.inlier_count,
                fitness=correspondences.inlier_count / correspondences.query_count,
                rmse=evaluation.point_to_point_rmse,
                median_residual=float(np.median(correspondences.distances_float64)),
                p90_residual=float(np.quantile(correspondences.distances_float64, 0.90)),
                delta_translation=translation_step,
                delta_rotation_degrees=rotation_step,
                elapsed_seconds=perf_counter() - started_at,
                correspondence_signature=correspondences.correspondence_signature,
                effective_trim_fraction=effective_trim_fraction,
                objective_kind=level.objective_kind,
                point_to_plane_rmse=evaluation.point_to_plane_rmse,
                point_to_point_rmse=evaluation.point_to_point_rmse,
                normal_valid_count=correspondences.normal_valid_count,
                normal_angle_pass_count=correspondences.normal_angle_pass_count,
                normal_angle_pass_ratio=(
                    correspondences.normal_angle_pass_count / max(correspondences.normal_valid_count, 1)
                ),
                maximum_correspondence_distance=current_radius,
                linear_system_rank=linear_rank,
                linear_system_condition=linear_condition,
                best_candidate_updated=best_updated,
            )
            iterations.append(metrics)
            stable_alignment = best_candidate.final_alignment.matrix_float64 if best_updated else None
            stage = _iteration_stage(level)
            _emit_progress(
                progress_sink,
                IcpProgressSnapshot(
                    stage=stage,
                    level_name=level.name,
                    iteration=iteration,
                    max_iterations=level.max_iterations,
                    progress=_level_progress(level_index, level_count, iteration, level.max_iterations),
                    message=(
                        f"{level.name} {level.objective_kind.value} · 반복 {iteration}/{level.max_iterations} · "
                        f"대응점 {correspondences.inlier_count} · Point RMSE {evaluation.point_to_point_rmse:.6g}"
                    ),
                    stable_alignment_float64=stable_alignment,
                    objective_kind=level.objective_kind,
                    maximum_correspondence_distance=current_radius,
                    correspondence_count=correspondences.inlier_count,
                    point_to_point_rmse=evaluation.point_to_point_rmse,
                    point_to_plane_rmse=evaluation.point_to_plane_rmse,
                    normal_valid_count=correspondences.normal_valid_count,
                    normal_angle_pass_count=correspondences.normal_angle_pass_count,
                    normal_angle_pass_ratio=(
                        correspondences.normal_angle_pass_count
                        / max(correspondences.normal_valid_count, 1)
                    ),
                    normal_radius=level.normal_radius_multiplier * samples.voxel_size,
                    moving_sample_count=samples.moving.count,
                    reference_sample_count=samples.reference_positions_float64.shape[0],
                ),
            )
            if previous_objective is not None:
                relative_change = abs(previous_objective - objective) / max(previous_objective, np.finfo(np.float64).eps)
                noise_floor = max(samples.voxel_size * 1.0e-12, 64.0 * np.finfo(np.float64).eps * max(overlap_diagonal, 1.0))
                if relative_change <= 1.0e-5 and translation_step <= max(samples.voxel_size * 1.0e-3, noise_floor):
                    stable_count += 1
                else:
                    stable_count = 0
                if _is_significant_rmse_increase(
                    previous_objective,
                    objective,
                    voxel_size=samples.voxel_size,
                    overlap_diagonal=overlap_diagonal,
                ):
                    increasing_count += 1
                else:
                    increasing_count = 0
                if increasing_count >= 3:
                    stop_reason = IcpSolverStopReason.OBJECTIVE_INCREASE
                    iterations[-1] = replace(iterations[-1], stop_reason=stop_reason)
                    break
            if previous_signature == correspondences.correspondence_signature:
                converged = True
                stop_reason = IcpSolverStopReason.CONVERGED
                iterations[-1] = replace(iterations[-1], stop_reason=stop_reason)
                break
            if stable_count >= level.convergence_required:
                converged = True
                stop_reason = IcpSolverStopReason.CONVERGED
                iterations[-1] = replace(iterations[-1], stop_reason=stop_reason)
                break
            current_delta = candidate_delta
            previous_objective = objective
            previous_signature = correspondences.correspondence_signature
            if best_updated and iteration >= 3:
                current_radius = next_correspondence_radius(
                    current_radius,
                    best_p90_residual=float(np.quantile(correspondences.distances_float64, 0.90)),
                    voxel_size=samples.voxel_size,
                    level=level,
                )
    finally:
        index.close()
    if best_candidate is None or best_correspondences is None:
        raise IcpDegeneracyError("ICP 해상도 레벨에서 수치적으로 유효한 후보를 만들지 못했습니다.")
    if iterations and stop_reason is IcpSolverStopReason.ITERATION_LIMIT:
        iterations[-1] = replace(iterations[-1], stop_reason=stop_reason)
    level_result = IcpLevelResult(
        level_name=level.name,
        voxel_size=samples.voxel_size,
        maximum_correspondence_distance=maximum_distance,
        moving_sample_count=samples.moving.count,
        reference_sample_count=samples.reference_positions_float64.shape[0],
        iterations=tuple(iterations),
        converged=converged,
        effective_trim_fraction=effective_trim_fraction,
        start_maximum_correspondence_distance=maximum_distance,
        end_maximum_correspondence_distance=current_radius,
        best_iteration=best_candidate.iteration,
        stop_reason=stop_reason,
        objective_kind=level.objective_kind,
        normal_radius=level.normal_radius_multiplier * samples.voxel_size,
        moving_normal_valid_count=samples.moving_normals.valid_count,
        reference_normal_valid_count=int(np.count_nonzero(samples.reference_normal_valid_mask)),
        normal_angle_pass_ratio=best_candidate.normal_angle_pass_ratio,
    )
    return best_candidate.delta_transform, level_result, best_correspondences, best_candidate


def _final_correspondences(
    *,
    current_delta: Sim3Transform,
    samples: _LevelSamples,
    local_origin: np.ndarray,
    maximum_distance: float,
    level: IcpLevelParameters,
    overlap_ratio: float,
    parameters: IcpParameters,
    index_factory: IndexFactory,
    cancel_probe: CancelProbe,
) -> tuple[np.ndarray, FilteredCorrespondences]:
    """최종 transform 뒤 같은 fine sample에서 품질 계산용 correspondence를 다시 만듭니다."""

    index = _new_index(index_factory, parameters.query_chunk_size)
    reference_local = samples.reference_positions_float64 - local_origin[np.newaxis, :]
    try:
        index.build(reference_local, cancel_probe=cancel_probe)
        working_world = apply_sim3_transform(current_delta, samples.moving.positions_float64)
        query = index.query(
            working_world - local_origin[np.newaxis, :],
            maximum_distance=maximum_distance,
            cancel_probe=cancel_probe,
        )
        correspondences = filter_correspondences(
            query,
            maximum_distance=maximum_distance,
            voxel_size=samples.voxel_size,
            trim_fraction=_effective_trim_fraction(level, overlap_ratio),
            minimum_correspondences=parameters.minimum_correspondences,
            mad_multiplier=parameters.mad_multiplier,
        )
        return working_world, correspondences
    finally:
        index.close()


def _quality_for_current(
    *,
    snapshot: IcpInputSnapshot,
    current_delta: Sim3Transform,
    samples: _LevelSamples,
    level: IcpLevelParameters,
    local_origin: np.ndarray,
    maximum_distance: float,
    overlap_diagonal: float,
    overlap_ratio: float,
    overlap_coverage_by_model: dict[str, float],
    total_iterations: int,
    elapsed_seconds: float,
    scale_refine_status: IcpScaleRefineStatus,
    scale_refine_reason: str,
    index_factory: IndexFactory,
    cancel_probe: CancelProbe,
    scale_boundary_reached: bool = False,
    baseline_metrics: IcpEvaluationMetrics | None = None,
    best_candidate_metrics: IcpEvaluationMetrics | None = None,
    solver_stop_reason: IcpSolverStopReason = IcpSolverStopReason.NONE,
    density_sampling_metrics: IcpDensitySamplingMetrics | None = None,
) -> tuple[Sim3Transform, np.ndarray, FilteredCorrespondences, IcpQualityMetrics]:
    """현재 rigid/scale delta의 final transform과 reciprocal 포함 품질 지표를 계산합니다."""

    working_world, correspondences = _final_correspondences(
        current_delta=current_delta,
        samples=samples,
        local_origin=local_origin,
        maximum_distance=maximum_distance,
        level=level,
        overlap_ratio=overlap_ratio,
        parameters=snapshot.parameters,
        index_factory=index_factory,
        cancel_probe=cancel_probe,
    )
    reference_indices = correspondences.reference_indices_int64
    moving_indices = correspondences.moving_indices_int64
    reciprocal_index = _new_index(index_factory, snapshot.parameters.query_chunk_size)
    try:
        reciprocal = reciprocal_match_ratio(
            working_world,
            samples.reference_positions_float64,
            moving_indices,
            reference_indices,
            reciprocal_index,
            maximum_distance=maximum_distance,
            cancel_probe=cancel_probe,
        )
    finally:
        reciprocal_index.close()
    final_alignment = compose_sim3_transforms(current_delta, snapshot.initial_alignment)
    model_indices = samples.reference_model_indices_int64[reference_indices]
    quality = evaluate_icp_quality(
        residuals_float64=correspondences.distances_float64,
        query_count=correspondences.query_count,
        reciprocal_ratio=reciprocal,
        overlap_ratio=overlap_ratio,
        initial_alignment=snapshot.initial_alignment,
        final_alignment=final_alignment,
        marker_uniform_scale=snapshot.marker_uniform_scale,
        scale_mode=snapshot.parameters.scale_mode,
        scale_refine_status=scale_refine_status,
        scale_refine_reason=scale_refine_reason,
        fine_voxel_size=samples.voxel_size,
        overlap_diagonal=overlap_diagonal,
        total_iterations=total_iterations,
        elapsed_seconds=elapsed_seconds,
        per_reference_model=per_reference_metrics(
            samples.reference_model_ids,
            model_indices,
            overlap_moving_coverage_by_model=overlap_coverage_by_model,
        ),
        marker_reference_project=snapshot.marker_reference_project,
        marker_moving_project=snapshot.marker_moving_project,
        marker_rms=snapshot.marker_rms,
        local_origin_float64=local_origin,
        scale_boundary_reached=scale_boundary_reached,
        baseline_metrics=baseline_metrics,
        best_candidate_metrics=best_candidate_metrics,
        solver_stop_reason=solver_stop_reason,
        point_to_plane_rmse=(
            best_candidate_metrics.point_to_plane_rmse
            if best_candidate_metrics is not None
            else 0.0
        ),
        normal_valid_ratio=(
            best_candidate_metrics.normal_valid_ratio
            if best_candidate_metrics is not None
            else 1.0
        ),
        normal_angle_pass_ratio=(
            best_candidate_metrics.normal_angle_pass_ratio
            if best_candidate_metrics is not None
            else 1.0
        ),
        density_sampling_metrics=density_sampling_metrics,
    )
    return final_alignment, working_world, correspondences, quality


def _scale_refine_eligible(snapshot: IcpInputSnapshot, quality: IcpQualityMetrics, overlap_ratio: float) -> str | None:
    """후행 스케일 보정을 건너뛸 명시 사유를 반환하고 가능하면 None을 반환합니다."""

    parameters = snapshot.parameters
    if quality.inlier_count < parameters.scale_refine_min_inliers:
        return "최종 inlier 수가 스케일 보정 최소 조건보다 적습니다."
    if quality.fitness < parameters.scale_refine_min_fitness:
        return "최종 Fitness가 스케일 보정 최소 조건보다 낮습니다."
    if quality.reciprocal_ratio < parameters.scale_refine_min_reciprocal:
        return "reciprocal correspondence 비율이 스케일 보정 최소 조건보다 낮습니다."
    if overlap_ratio < parameters.scale_refine_min_overlap_coverage:
        return "자동 overlap coverage가 스케일 보정 최소 조건보다 낮습니다."
    return None


def _run_scale_refinement(
    *,
    snapshot: IcpInputSnapshot,
    fixed_delta: Sim3Transform,
    fixed_quality: IcpQualityMetrics,
    fine_samples: _LevelSamples,
    fine_level: IcpLevelParameters,
    local_origin: np.ndarray,
    maximum_distance: float,
    overlap_diagonal: float,
    overlap_ratio: float,
    overlap_coverage_by_model: dict[str, float],
    total_iterations: int,
    started_at: float,
    index_factory: IndexFactory,
    progress_sink: ProgressSink,
    cancel_probe: CancelProbe,
) -> tuple[Sim3Transform, IcpQualityMetrics]:
    """rigid 수렴 뒤 ±5%·±0.5% 제한 내에서만 scale refinement 후보를 비교합니다."""

    skip_reason = _scale_refine_eligible(snapshot, fixed_quality, overlap_ratio)
    if skip_reason is not None:
        return fixed_delta, replace(
            fixed_quality,
            scale_refine_status=IcpScaleRefineStatus.SKIPPED,
            scale_refine_reason=skip_reason,
        )
    parameters = snapshot.parameters
    current_delta = fixed_delta
    current_alignment = compose_sim3_transforms(current_delta, snapshot.initial_alignment)
    final_correspondences: FilteredCorrespondences | None = None
    for iteration in range(1, parameters.scale_refine_max_iterations + 1):
        _raise_if_cancelled(cancel_probe)
        working_world, correspondences = _final_correspondences(
            current_delta=current_delta,
            samples=fine_samples,
            local_origin=local_origin,
            maximum_distance=maximum_distance,
            level=fine_level,
            overlap_ratio=overlap_ratio,
            parameters=parameters,
            index_factory=index_factory,
            cancel_probe=cancel_probe,
        )
        source_local = working_world[correspondences.moving_indices_int64] - local_origin[np.newaxis, :]
        target_local = (
            fine_samples.reference_positions_float64[correspondences.reference_indices_int64]
            - local_origin[np.newaxis, :]
        )
        marker_scale = snapshot.marker_uniform_scale
        current_scale = current_alignment.uniform_scale
        lower_total = marker_scale * (1.0 - parameters.scale_refine_max_relative_change)
        upper_total = marker_scale * (1.0 + parameters.scale_refine_max_relative_change)
        lower_step = max(1.0 - parameters.scale_refine_max_step, lower_total / current_scale)
        upper_step = min(1.0 + parameters.scale_refine_max_step, upper_total / current_scale)
        if lower_step > upper_step + 1.0e-12:
            break
        delta_local = solve_bounded_scale_delta(source_local, target_local, (lower_step, upper_step))
        delta_world = _local_delta_to_world(delta_local, local_origin)
        current_delta = compose_sim3_transforms(delta_world, current_delta)
        current_alignment = compose_sim3_transforms(current_delta, snapshot.initial_alignment)
        final_correspondences = correspondences
        _emit_progress(
            progress_sink,
            IcpProgressSnapshot(
                stage=ICP_STAGE_SCALE_REFINEMENT,
                level_name="fine",
                iteration=iteration,
                max_iterations=parameters.scale_refine_max_iterations,
                progress=min(0.97, 0.90 + 0.07 * iteration / parameters.scale_refine_max_iterations),
                message=f"Scale Refine · 반복 {iteration}/{parameters.scale_refine_max_iterations}",
            ),
        )
    if final_correspondences is None:
        return fixed_delta, replace(
            fixed_quality,
            scale_refine_status=IcpScaleRefineStatus.SKIPPED,
            scale_refine_reason="스케일 보정 범위가 이미 안전 경계에 도달했습니다.",
        )
    # 마지막 scale 고정 상태에서 rigid polish를 한 번만 수행합니다.
    working_world, correspondences = _final_correspondences(
        current_delta=current_delta,
        samples=fine_samples,
        local_origin=local_origin,
        maximum_distance=maximum_distance,
        level=fine_level,
        overlap_ratio=overlap_ratio,
        parameters=parameters,
        index_factory=index_factory,
        cancel_probe=cancel_probe,
    )
    polish_local = solve_rigid_delta(
        working_world[correspondences.moving_indices_int64] - local_origin[np.newaxis, :],
        fine_samples.reference_positions_float64[correspondences.reference_indices_int64]
        - local_origin[np.newaxis, :],
    )
    current_delta = compose_sim3_transforms(_local_delta_to_world(polish_local, local_origin), current_delta)
    current_alignment = compose_sim3_transforms(current_delta, snapshot.initial_alignment)
    scale_boundary_reached = bool(
        np.isclose(
            abs(current_alignment.uniform_scale / snapshot.marker_uniform_scale - 1.0),
            parameters.scale_refine_max_relative_change,
            rtol=0.0,
            atol=1.0e-8,
        )
    )
    refined_final, _working, _corr, refined_quality = _quality_for_current(
        snapshot=snapshot,
        current_delta=current_delta,
        samples=fine_samples,
        level=fine_level,
        local_origin=local_origin,
        maximum_distance=maximum_distance,
        overlap_diagonal=overlap_diagonal,
        overlap_ratio=overlap_ratio,
        overlap_coverage_by_model=overlap_coverage_by_model,
        total_iterations=total_iterations,
        elapsed_seconds=perf_counter() - started_at,
        scale_refine_status=IcpScaleRefineStatus.ACCEPTED,
        scale_refine_reason="고정 스케일 결과 뒤 제한된 스케일 보정을 실행했습니다.",
        index_factory=index_factory,
        cancel_probe=cancel_probe,
        scale_boundary_reached=scale_boundary_reached,
    )
    rmse_improvement = (fixed_quality.rmse - refined_quality.rmse) / max(
        fixed_quality.rmse,
        np.finfo(np.float64).eps,
    )
    accepted = (
        refined_quality.status.value != "REJECT"
        and rmse_improvement >= parameters.scale_refine_min_rmse_improvement
        and refined_quality.fitness + 0.02 >= fixed_quality.fitness
        and refined_quality.reciprocal_ratio + 1.0e-12 >= fixed_quality.reciprocal_ratio
    )
    if accepted:
        return current_delta, refined_quality
    return fixed_delta, replace(
        fixed_quality,
        scale_refine_status=IcpScaleRefineStatus.REJECTED,
        scale_refine_reason="스케일 보정 후보가 fixed 결과 채택 조건을 만족하지 못했습니다.",
    )


def solve_multiscale_icp(
    snapshot: IcpInputSnapshot,
    *,
    progress_sink: ProgressSink = None,
    cancel_probe: CancelProbe = None,
    index_factory: IndexFactory = None,
) -> IcpAlignmentResult:
    """Marker Align 초기값부터 다중 해상도 ICP와 선택적 bounded scale refine을 실행합니다."""

    if not isinstance(snapshot, IcpInputSnapshot):
        raise IcpInputError("ICP 실행에는 IcpInputSnapshot이 필요합니다.")
    started_at = perf_counter()
    _emit_progress(
        progress_sink,
        IcpProgressSnapshot(
            ICP_STAGE_PREPARING_INPUT,
            progress=0.0,
            message="ICP 입력과 좌표계를 검증하고 있습니다.",
        ),
    )
    _raise_if_cancelled(cancel_probe)
    _emit_progress(
        progress_sink,
        IcpProgressSnapshot(
            ICP_STAGE_SELECTING_OVERLAP,
            progress=0.02,
            message="자동 겹침 후보를 찾고 있습니다.",
        ),
    )
    if snapshot.moving_cloud_frame is IcpMovingCloudFrame.INITIAL_PROJECT_WORLD:
        # worker가 initial alignment까지 적용한 NPY를 전달한 경우 원본 전체를 다시 복사하지 않습니다.
        moving_initial_cloud = snapshot.moving_cloud
    else:
        moving_initial_positions = apply_sim3_transform(
            snapshot.initial_alignment,
            snapshot.moving_cloud.positions_float64,
        )
        moving_initial_cloud = IcpPointCloud(
            model_id=snapshot.moving_model_id,
            positions_float64=moving_initial_positions,
            point_ids_uint64=snapshot.moving_cloud.point_ids_uint64,
        )
    moving_bounds = bounds_from_cloud(moving_initial_cloud, cancel_probe=cancel_probe)
    initial_diagonal = moving_bounds.diagonal
    if initial_diagonal <= np.finfo(np.float64).eps:
        raise IcpDegeneracyError("ICP 이동 P 점군의 경계 대각선이 0입니다.")
    coarse_level = snapshot.parameters.levels[0]
    preliminary_voxel = max(initial_diagonal / max(coarse_level.moving_budget, 1) ** (1.0 / 3.0), 1.0e-9)
    preliminary_distance = _initial_overlap_distance(
        initial_diagonal,
        preliminary_voxel,
        snapshot.marker_max_error,
    )
    overlap_occupancy_limit = max(
        coarse_level.moving_budget,
        coarse_level.reference_budget,
    ) * 2
    candidates, overlap_occupancy_voxel = _find_overlap_candidates_with_refined_voxel(
        moving_initial_cloud,
        snapshot.reference_clouds,
        maximum_distance=preliminary_distance,
        base_voxel_size=preliminary_voxel,
        minimum_common_cells=snapshot.parameters.overlap_minimum_common_cells,
        minimum_moving_coverage=snapshot.parameters.overlap_minimum_moving_coverage,
        maximum_occupancy_cells=overlap_occupancy_limit,
        cancel_probe=cancel_probe,
    )
    try:
        selected_reference_clouds = select_overlap_clouds(candidates, snapshot.reference_clouds)
    except IcpInputError as error:
        raise IcpOverlapError(str(error)) from error
    overlap_coverage_by_model = {
        candidate.reference_model_id: candidate.moving_coverage
        for candidate in candidates
        if candidate.selected
    }
    overlap_cell_counts = {
        candidate.reference_model_id: candidate.common_cell_count
        for candidate in candidates
        if candidate.selected
    }
    reference_candidate_bounds, selected_reference_bounds = _reference_crop_bounds(
        moving_bounds,
        selected_reference_clouds,
        preliminary_distance,
    )
    moving_candidate_bounds = selected_reference_bounds.expanded(preliminary_distance)
    try:
        moving_cropped_bounds = bounds_from_cloud(
            moving_initial_cloud,
            crop_bounds=moving_candidate_bounds,
            cancel_probe=cancel_probe,
        )
    except IcpInputError as error:
        raise IcpOverlapError("자동 ICP 겹침 후보 안에 사용할 P 점이 없습니다.")
    common_origin = np.minimum(
        moving_cropped_bounds.minimum_float64,
        selected_reference_bounds.minimum_float64,
    )
    overlap_diagonal = AxisAlignedBounds(
        np.minimum(moving_cropped_bounds.minimum_float64, selected_reference_bounds.minimum_float64),
        np.maximum(moving_cropped_bounds.maximum_float64, selected_reference_bounds.maximum_float64),
    ).diagonal
    if overlap_diagonal <= np.finfo(np.float64).eps:
        raise IcpDegeneracyError("ICP overlap 경계 대각선이 0입니다.")
    reference_overlap_bounds = {
        model_id: AxisAlignedBounds(minimum, maximum)
        for model_id, (minimum, maximum) in reference_candidate_bounds.items()
    }
    try:
        overlap_ratio = combined_moving_overlap_coverage(
            moving_initial_cloud,
            selected_reference_clouds,
            moving_crop_bounds=moving_bounds,
            reference_crop_bounds_by_model=reference_overlap_bounds,
            occupancy_voxel_size=overlap_occupancy_voxel,
            origin_float64=common_origin,
            maximum_occupancy_cells=overlap_occupancy_limit,
            cancel_probe=cancel_probe,
        )
    except IcpInputError as error:
        raise IcpOverlapError("선택 A 모델의 합성 overlap coverage를 계산하지 못했습니다.") from error
    current_delta = identity_sim3_transform()
    level_results: list[IcpLevelResult] = []
    fine_samples: _LevelSamples | None = None
    fine_level: IcpLevelParameters | None = None
    fine_distance = preliminary_distance
    previous_p90: float | None = None
    previous_level_inlier_count: int | None = None
    latest_best_candidate: IcpBestCandidate | None = None
    solver_stop_reason = IcpSolverStopReason.NONE
    for level_index, level in enumerate(snapshot.parameters.levels):
        _raise_if_cancelled(cancel_probe)
        _emit_progress(
            progress_sink,
            IcpProgressSnapshot(
                stage=ICP_STAGE_BUILDING_UNIFORM_SAMPLES,
                level_name=level.name,
                progress=_level_progress(level_index, len(snapshot.parameters.levels), 0, level.max_iterations),
                message=f"{level.name} ICP 샘플을 준비하고 있습니다.",
            ),
        )
        _emit_progress(
            progress_sink,
            IcpProgressSnapshot(
                stage=_normal_estimation_stage(level),
                level_name=level.name,
                progress=_level_progress(level_index, len(snapshot.parameters.levels), 0, level.max_iterations),
                message=f"{level.name} ICP PCA 노멀을 추정하고 있습니다.",
            ),
        )
        samples = _build_level_samples(
            moving_initial_cloud=moving_initial_cloud,
            selected_reference_clouds=selected_reference_clouds,
            overlap_cell_counts=overlap_cell_counts,
            moving_candidate_bounds=(
                moving_candidate_bounds.minimum_float64,
                moving_candidate_bounds.maximum_float64,
            ),
            reference_candidate_bounds=reference_candidate_bounds,
            level=level,
            common_origin=common_origin,
            index_factory=index_factory,
            cancel_probe=cancel_probe,
        )
        maximum_distance = (
            _initial_overlap_distance(overlap_diagonal, samples.voxel_size, snapshot.marker_max_error)
            if previous_p90 is None
            else max(2.5 * samples.voxel_size, 1.5 * previous_p90)
        )
        try:
            current_delta, level_result, last_correspondences, level_best_candidate = _run_level(
                level=level,
                level_index=level_index,
                level_count=len(snapshot.parameters.levels),
                samples=samples,
                current_delta=current_delta,
                initial_alignment=snapshot.initial_alignment,
                local_origin=common_origin,
                maximum_distance=maximum_distance,
                overlap_diagonal=overlap_diagonal,
                overlap_ratio=overlap_ratio,
                parameters=snapshot.parameters,
                index_factory=index_factory,
                progress_sink=progress_sink,
                cancel_probe=cancel_probe,
            )
        except IcpNormalCoverageError:
            if not level_results:
                raise
            solver_stop_reason = IcpSolverStopReason.NORMAL_COVERAGE_INSUFFICIENT
            break
        level_inlier_drop = (
            previous_level_inlier_count is not None
            and _has_excessive_level_inlier_drop(
                previous_level_inlier_count,
                last_correspondences.inlier_count,
            )
        )
        level_results.append(level_result)
        solver_stop_reason = (
            IcpSolverStopReason.LEVEL_INLIER_DROP
            if level_inlier_drop
            else level_result.stop_reason
        )
        previous_level_inlier_count = last_correspondences.inlier_count
        previous_p90 = float(np.quantile(last_correspondences.distances_float64, 0.90))
        fine_samples = samples
        fine_level = level
        fine_distance = maximum_distance
        latest_best_candidate = level_best_candidate
        _emit_progress(
            progress_sink,
            IcpProgressSnapshot(
                stage=_iteration_stage(level),
                level_name=level.name,
                iteration=len(level_result.iterations),
                max_iterations=level.max_iterations,
                progress=_level_progress(level_index + 1, len(snapshot.parameters.levels), 0, level.max_iterations),
                message=(
                    f"{level.name} 레벨에서 대응점이 급감해 현재 후보를 검토용으로 보존했습니다."
                    if level_inlier_drop
                    else f"{level.name} ICP 레벨을 완료했습니다."
                ),
                level_complete=True,
                stable_alignment_float64=compose_sim3_transforms(
                    current_delta,
                    snapshot.initial_alignment,
                ).matrix_float64,
            ),
        )
        if level_inlier_drop:
            break
    if fine_samples is None or fine_level is None:
        raise IcpDegeneracyError("ICP 해상도 레벨을 실행하지 못했습니다.")
    _emit_progress(
        progress_sink,
        IcpProgressSnapshot(
            ICP_STAGE_EVALUATING_BEST_CANDIDATE,
            progress=0.92,
            message="ICP 최종 품질을 평가하고 있습니다.",
        ),
    )
    fixed_evaluation_distance = max(preliminary_distance, fine_distance)
    baseline_metrics, _baseline_correspondences = _evaluate_fixed_candidate(
        current_delta=identity_sim3_transform(),
        samples=fine_samples,
        level=fine_level,
        local_origin=common_origin,
        maximum_distance=fixed_evaluation_distance,
        overlap_ratio=overlap_ratio,
        parameters=snapshot.parameters,
        index_factory=index_factory,
        cancel_probe=cancel_probe,
    )
    best_candidate_metrics, _candidate_correspondences = _evaluate_fixed_candidate(
        current_delta=current_delta,
        samples=fine_samples,
        level=fine_level,
        local_origin=common_origin,
        maximum_distance=fixed_evaluation_distance,
        overlap_ratio=overlap_ratio,
        parameters=snapshot.parameters,
        index_factory=index_factory,
        cancel_probe=cancel_probe,
    )
    fixed_final, _working, _correspondences, fixed_quality = _quality_for_current(
        snapshot=snapshot,
        current_delta=current_delta,
        samples=fine_samples,
        level=fine_level,
        local_origin=common_origin,
        maximum_distance=fixed_evaluation_distance,
        overlap_diagonal=overlap_diagonal,
        overlap_ratio=overlap_ratio,
        overlap_coverage_by_model=overlap_coverage_by_model,
        total_iterations=sum(len(item.iterations) for item in level_results),
        elapsed_seconds=perf_counter() - started_at,
        scale_refine_status=IcpScaleRefineStatus.NOT_REQUESTED,
        scale_refine_reason="Scale Refine을 요청하지 않았습니다.",
        index_factory=index_factory,
        cancel_probe=cancel_probe,
        baseline_metrics=baseline_metrics,
        best_candidate_metrics=best_candidate_metrics,
        solver_stop_reason=solver_stop_reason,
        density_sampling_metrics=fine_samples.density_sampling_metrics,
    )
    fixed_quality = replace(
        fixed_quality,
        best_level_name=(latest_best_candidate.level_name if latest_best_candidate is not None else fine_level.name),
        best_iteration=(latest_best_candidate.iteration if latest_best_candidate is not None else 0),
        solver_stop_reason=solver_stop_reason,
    )
    if fixed_quality.status.value == "REJECT":
        reason = "; ".join(fixed_quality.rejection_reasons) or "최종 ICP 품질이 미리보기 기준에 미달했습니다."
        raise IcpQualityRejectedError(reason, fixed_quality)
    chosen_delta = current_delta
    chosen_quality = fixed_quality
    if snapshot.parameters.scale_mode is IcpScaleMode.REFINE:
        chosen_delta, chosen_quality = _run_scale_refinement(
            snapshot=snapshot,
            fixed_delta=current_delta,
            fixed_quality=fixed_quality,
            fine_samples=fine_samples,
            fine_level=fine_level,
            local_origin=common_origin,
            maximum_distance=fine_distance,
            overlap_diagonal=overlap_diagonal,
            overlap_ratio=overlap_ratio,
            overlap_coverage_by_model=overlap_coverage_by_model,
            total_iterations=sum(len(item.iterations) for item in level_results),
            started_at=started_at,
            index_factory=index_factory,
            progress_sink=progress_sink,
            cancel_probe=cancel_probe,
        )
    chosen_metrics, _chosen_evaluation_correspondences = _evaluate_fixed_candidate(
        current_delta=chosen_delta,
        samples=fine_samples,
        level=fine_level,
        local_origin=common_origin,
        maximum_distance=fixed_evaluation_distance,
        overlap_ratio=overlap_ratio,
        parameters=snapshot.parameters,
        index_factory=index_factory,
        cancel_probe=cancel_probe,
    )
    final_alignment, _final_working, _final_correspondences, chosen_quality = _quality_for_current(
        snapshot=snapshot,
        current_delta=chosen_delta,
        samples=fine_samples,
        level=fine_level,
        local_origin=common_origin,
        maximum_distance=fixed_evaluation_distance,
        overlap_diagonal=overlap_diagonal,
        overlap_ratio=overlap_ratio,
        overlap_coverage_by_model=overlap_coverage_by_model,
        total_iterations=sum(len(item.iterations) for item in level_results),
        elapsed_seconds=perf_counter() - started_at,
        scale_refine_status=chosen_quality.scale_refine_status,
        scale_refine_reason=chosen_quality.scale_refine_reason,
        index_factory=index_factory,
        cancel_probe=cancel_probe,
        baseline_metrics=baseline_metrics,
        best_candidate_metrics=chosen_metrics,
        solver_stop_reason=solver_stop_reason,
        density_sampling_metrics=fine_samples.density_sampling_metrics,
    )
    chosen_quality = replace(
        chosen_quality,
        best_level_name=(latest_best_candidate.level_name if latest_best_candidate is not None else fine_level.name),
        best_iteration=(latest_best_candidate.iteration if latest_best_candidate is not None else 0),
        solver_stop_reason=solver_stop_reason,
    )
    if chosen_quality.status.value == "REJECT":
        reason = "; ".join(chosen_quality.rejection_reasons) or "최종 ICP 품질이 미리보기 기준에 미달했습니다."
        raise IcpQualityRejectedError(reason, chosen_quality)
    final_best_candidate = IcpBestCandidate(
        delta_transform=chosen_delta,
        final_alignment=final_alignment,
        level_name=(latest_best_candidate.level_name if latest_best_candidate is not None else fine_level.name),
        iteration=(latest_best_candidate.iteration if latest_best_candidate is not None else 0),
        objective_kind=fine_level.objective_kind,
        point_to_plane_rmse=chosen_metrics.point_to_plane_rmse,
        point_to_point_rmse=chosen_metrics.point_to_point_rmse,
        fitness=chosen_metrics.fitness,
        inlier_count=chosen_metrics.inlier_count,
        reciprocal_ratio=chosen_quality.reciprocal_ratio,
        normal_valid_ratio=chosen_metrics.normal_valid_ratio,
        normal_angle_pass_ratio=chosen_metrics.normal_angle_pass_ratio,
        maximum_correspondence_distance=fixed_evaluation_distance,
        correspondence_signature=_chosen_evaluation_correspondences.correspondence_signature,
        evaluation_fingerprint=chosen_metrics.evaluation_fingerprint,
    )
    _emit_progress(
        progress_sink,
        IcpProgressSnapshot(
            (
                ICP_STAGE_COMPLETED_PASS
                if chosen_quality.status.value == "PASS"
                else ICP_STAGE_COMPLETED_REVIEW
            ),
            progress=1.0,
            message=(
                "ICP 최종 품질 평가를 완료했습니다."
                if chosen_quality.status.value == "PASS"
                else "ICP 최선 후보를 REVIEW 상태로 준비했습니다."
            ),
            level_complete=True,
            stable_alignment_float64=final_alignment.matrix_float64,
        ),
    )
    return IcpAlignmentResult(
        moving_model_id=snapshot.moving_model_id,
        reference_model_ids=fine_samples.reference_model_ids,
        initial_alignment=snapshot.initial_alignment,
        final_alignment=final_alignment,
        icp_delta=chosen_delta,
        quality=chosen_quality,
        level_results=tuple(level_results),
        input_fingerprint=snapshot.input_fingerprint,
        parameter_fingerprint=snapshot.parameters.fingerprint,
        model_revision_snapshot=snapshot.model_revision_snapshot,
        marker_signature=snapshot.marker_signature,
        algorithm_version=snapshot.algorithm_version,
        best_candidate=final_best_candidate,
        baseline_metrics=baseline_metrics,
        best_candidate_metrics=chosen_metrics,
        relative_improvement=chosen_quality.relative_improvement,
        best_level_name=final_best_candidate.level_name,
        best_iteration=final_best_candidate.iteration,
        solver_stop_reason=solver_stop_reason,
        point_to_plane_rmse=chosen_metrics.point_to_plane_rmse,
        normal_valid_ratio=chosen_metrics.normal_valid_ratio,
        normal_angle_pass_ratio=chosen_metrics.normal_angle_pass_ratio,
        density_sampling_metrics=fine_samples.density_sampling_metrics,
        user_review_required=chosen_quality.user_review_required,
    )


def run_icp(
    snapshot: IcpInputSnapshot,
    progress_sink: ProgressSink = None,
    cancel_probe: CancelProbe = None,
) -> IcpAlignmentResult:
    """계획서 공개명 호환을 위해 기본 index로 multiscale ICP를 실행합니다."""

    return solve_multiscale_icp(
        snapshot,
        progress_sink=progress_sink,
        cancel_probe=cancel_probe,
    )


__all__ = (
    "FilteredCorrespondences",
    "apply_sim3_transform",
    "choose_better_candidate",
    "evaluate_candidate_on_fixed_sample",
    "filter_correspondences",
    "filter_hybrid_correspondences",
    "next_correspondence_radius",
    "run_icp",
    "solve_bounded_scale_delta",
    "solve_multiscale_icp",
    "solve_point_to_plane_delta",
    "solve_rigid_delta",
)
