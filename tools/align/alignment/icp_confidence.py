"""ICP 결과 평가용 점 분포 신뢰도와 가중 RMS 계산을 제공합니다."""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from typing import Any

import numpy as np


DEFAULT_CONFIDENCE_NEIGHBORS = 24
DEFAULT_ISOLATION_Z_THRESHOLD = 3.5
LOW_WEIGHT_THRESHOLD = 0.5
DISTANCE_UNIT_PROJECT_WORLD = "PROJECT_WORLD_UNIT"
_MAD_NORMALIZATION = 1.4826
_DENSITY_WEIGHT_FLOOR = 0.25
_ISOLATION_WEIGHT_FLOOR = 0.05
_ISOLATION_TRANSITION_WIDTH = 0.75


class IcpConfidenceError(ValueError):
    """점 분포 신뢰도 입력 또는 평가값 계약 위반을 나타냅니다."""


def _readonly_array(
    value: object,
    *,
    dtype: np.dtype[Any],
    shape: tuple[int | None, ...],
    field_name: str,
) -> np.ndarray:
    """신뢰도 배열을 유한한 읽기 전용 C 연속 배열로 고정합니다."""

    try:
        array = np.ascontiguousarray(value, dtype=dtype)
    except (OverflowError, TypeError, ValueError) as error:
        raise IcpConfidenceError(f"{field_name} 배열을 읽지 못했습니다.") from error
    if array.ndim != len(shape) or any(
        expected is not None and actual != expected
        for actual, expected in zip(array.shape, shape, strict=True)
    ):
        raise IcpConfidenceError(f"{field_name} 배열 형상이 올바르지 않습니다.")
    if np.issubdtype(array.dtype, np.floating) and not np.isfinite(array).all():
        raise IcpConfidenceError(f"{field_name} 배열에 유한하지 않은 값이 있습니다.")
    array.setflags(write=False)
    return array


@dataclass(frozen=True, slots=True)
class PointDistributionConfidence:
    """한 원본 tie point 그룹의 불변 점별 신뢰도 계산 결과를 보관합니다."""

    weights_float64: np.ndarray
    isolation_candidates_bool: np.ndarray
    neighbor_count: int
    log_distance_median: float
    log_distance_mad: float

    def __post_init__(self) -> None:
        """가중치·고립 후보·robust 통계의 유한 범위를 검증합니다."""

        weights = _readonly_array(
            self.weights_float64,
            dtype=np.dtype(np.float64),
            shape=(None,),
            field_name="점 분포 신뢰도",
        )
        candidates = _readonly_array(
            self.isolation_candidates_bool,
            dtype=np.dtype(np.bool_),
            shape=(weights.shape[0],),
            field_name="고립 후보",
        )
        if weights.shape[0] < 1 or np.any(weights <= 0.0) or np.any(weights > 1.0):
            raise IcpConfidenceError("점 분포 신뢰도는 0 초과 1 이하이어야 합니다.")
        neighbor_count = int(self.neighbor_count)
        if neighbor_count < 0 or neighbor_count > max(weights.shape[0] - 1, 0):
            raise IcpConfidenceError("점 분포 신뢰도의 이웃 수가 올바르지 않습니다.")
        median = float(self.log_distance_median)
        mad = float(self.log_distance_mad)
        if not isfinite(median) or not isfinite(mad) or mad < 0.0:
            raise IcpConfidenceError("점 분포 신뢰도의 robust 통계가 올바르지 않습니다.")
        object.__setattr__(self, "weights_float64", weights)
        object.__setattr__(self, "isolation_candidates_bool", candidates)
        object.__setattr__(self, "neighbor_count", neighbor_count)
        object.__setattr__(self, "log_distance_median", median)
        object.__setattr__(self, "log_distance_mad", mad)

    @property
    def point_count(self) -> int:
        """캐시된 원본 tie point 수를 반환합니다."""

        return int(self.weights_float64.shape[0])

    @property
    def isolation_candidate_count(self) -> int:
        """robust z-score가 기준을 넘은 고립 후보 수를 반환합니다."""

        return int(np.count_nonzero(self.isolation_candidates_bool))


def mean_knn_distances_with_blender(
    positions_float64: np.ndarray,
    *,
    k: int = DEFAULT_CONFIDENCE_NEIGHBORS,
) -> tuple[np.ndarray, int]:
    """중심·스케일 정규화 뒤 Blender KDTree로 k-NN 평균 거리를 계산합니다."""

    positions = _readonly_array(
        positions_float64,
        dtype=np.dtype(np.float64),
        shape=(None, 3),
        field_name="점 분포 원본 좌표",
    )
    if positions.shape[0] < 1:
        raise IcpConfidenceError("점 분포 신뢰도에는 한 개 이상의 원본점이 필요합니다.")
    requested_k = int(k)
    if requested_k < 1:
        raise IcpConfidenceError("점 분포 신뢰도의 k는 1 이상이어야 합니다.")
    neighbor_count = min(requested_k, max(int(positions.shape[0]) - 1, 0))
    if neighbor_count == 0:
        return np.zeros(positions.shape[0], dtype=np.float64), 0
    center = np.mean(positions, axis=0, dtype=np.float64)
    centered = positions - center[np.newaxis, :]
    normalization_scale = float(np.max(np.linalg.norm(centered, axis=1)))
    if not isfinite(normalization_scale) or normalization_scale <= np.finfo(np.float64).eps:
        return np.zeros(positions.shape[0], dtype=np.float64), neighbor_count
    normalized_positions = np.ascontiguousarray(
        centered / normalization_scale,
        dtype=np.float64,
    )
    try:
        from mathutils.kdtree import KDTree
    except ImportError as error:
        raise IcpConfidenceError("점 분포 신뢰도 계산에는 Blender KDTree가 필요합니다.") from error
    tree = KDTree(int(normalized_positions.shape[0]))
    for index, position in enumerate(normalized_positions):
        tree.insert(tuple(float(component) for component in position), index)
    tree.balance()
    means = np.empty(normalized_positions.shape[0], dtype=np.float64)
    query_count = neighbor_count + 1
    for index, position in enumerate(normalized_positions):
        distances = [
            float(distance)
            for _coordinate, neighbor_index, distance in tree.find_n(
                tuple(float(component) for component in position),
                query_count,
            )
            if int(neighbor_index) != index
        ]
        if len(distances) < neighbor_count:
            raise IcpConfidenceError("Blender KDTree가 필요한 이웃 수를 반환하지 못했습니다.")
        means[index] = float(np.mean(distances[:neighbor_count], dtype=np.float64))
    if not np.isfinite(means).all() or np.any(means < 0.0):
        raise IcpConfidenceError("점 분포 k-NN 평균 거리가 올바르지 않습니다.")
    return means, neighbor_count


def confidence_from_mean_neighbor_distances(
    mean_distances_float64: np.ndarray,
    *,
    neighbor_count: int,
    isolation_z_threshold: float = DEFAULT_ISOLATION_Z_THRESHOLD,
) -> PointDistributionConfidence:
    """k-NN 평균 거리에서 밀도 균형과 연속 고립 신뢰도를 계산합니다."""

    distances = _readonly_array(
        mean_distances_float64,
        dtype=np.dtype(np.float64),
        shape=(None,),
        field_name="k-NN 평균 거리",
    )
    if distances.shape[0] < 1 or np.any(distances < 0.0):
        raise IcpConfidenceError("k-NN 평균 거리는 비어 있지 않은 0 이상 배열이어야 합니다.")
    normalized_neighbor_count = int(neighbor_count)
    if normalized_neighbor_count < 0 or normalized_neighbor_count > max(distances.shape[0] - 1, 0):
        raise IcpConfidenceError("k-NN 평균 거리의 이웃 수가 올바르지 않습니다.")
    threshold = float(isolation_z_threshold)
    if not isfinite(threshold) or threshold <= 0.0:
        raise IcpConfidenceError("고립 후보 robust z-score 기준은 양수여야 합니다.")
    if normalized_neighbor_count == 0:
        return PointDistributionConfidence(
            weights_float64=np.ones(distances.shape[0], dtype=np.float64),
            isolation_candidates_bool=np.zeros(distances.shape[0], dtype=bool),
            neighbor_count=0,
            log_distance_median=0.0,
            log_distance_mad=0.0,
        )
    positive = distances[distances > 0.0]
    distance_floor = (
        max(float(np.min(positive)) * 1.0e-12, np.finfo(np.float64).tiny)
        if positive.size
        else np.finfo(np.float64).tiny
    )
    safe_distances = np.maximum(distances, distance_floor)
    log_distances = np.log(safe_distances)
    log_median = float(np.median(log_distances))
    log_mad = float(np.median(np.abs(log_distances - log_median)))
    robust_scale = _MAD_NORMALIZATION * log_mad
    if robust_scale > np.finfo(np.float64).eps * max(1.0, abs(log_median)):
        robust_z = (log_distances - log_median) / robust_scale
        isolation_candidates = robust_z > threshold
        transition = np.clip(
            (robust_z - threshold) / _ISOLATION_TRANSITION_WIDTH,
            -60.0,
            60.0,
        )
        isolation_weights = _ISOLATION_WEIGHT_FLOOR + (
            1.0 - _ISOLATION_WEIGHT_FLOOR
        ) / (1.0 + np.exp(transition))
    else:
        isolation_candidates = np.zeros(distances.shape[0], dtype=bool)
        isolation_weights = np.ones(distances.shape[0], dtype=np.float64)
    median_distance = float(np.exp(log_median))
    if median_distance <= np.finfo(np.float64).tiny:
        density_weights = np.ones(distances.shape[0], dtype=np.float64)
    else:
        density_weights = np.clip(
            safe_distances / median_distance,
            _DENSITY_WEIGHT_FLOOR,
            1.0,
        )
    weights = np.clip(
        density_weights * isolation_weights,
        _DENSITY_WEIGHT_FLOOR * _ISOLATION_WEIGHT_FLOOR,
        1.0,
    )
    return PointDistributionConfidence(
        weights_float64=weights,
        isolation_candidates_bool=isolation_candidates,
        neighbor_count=normalized_neighbor_count,
        log_distance_median=log_median,
        log_distance_mad=log_mad,
    )


def compute_point_distribution_confidence(
    positions_float64: np.ndarray,
    *,
    k: int = DEFAULT_CONFIDENCE_NEIGHBORS,
) -> PointDistributionConfidence:
    """원본 tie point 좌표에서 k-NN과 점별 분포 신뢰도를 한 번 계산합니다."""

    mean_distances, neighbor_count = mean_knn_distances_with_blender(
        positions_float64,
        k=k,
    )
    return confidence_from_mean_neighbor_distances(
        mean_distances,
        neighbor_count=neighbor_count,
    )


def weighted_residual_metrics(
    residuals_float64: np.ndarray,
    pairs_int64: np.ndarray,
    source_weights_float64: np.ndarray,
    target_weights_float64: np.ndarray,
    source_isolation_candidates_bool: np.ndarray,
    target_isolation_candidates_bool: np.ndarray,
) -> dict[str, float | int | str | None]:
    """고정 대응점 잔차에 양쪽 신뢰도의 최솟값을 적용한 평가 지표를 반환합니다."""

    residuals = _readonly_array(
        residuals_float64,
        dtype=np.dtype(np.float64),
        shape=(None,),
        field_name="ICP 평가 잔차",
    )
    pairs = _readonly_array(
        pairs_int64,
        dtype=np.dtype(np.int64),
        shape=(residuals.shape[0], 2),
        field_name="ICP 평가 대응점",
    )
    source_weights = _readonly_array(
        source_weights_float64,
        dtype=np.dtype(np.float64),
        shape=(None,),
        field_name="ICP source 신뢰도",
    )
    target_weights = _readonly_array(
        target_weights_float64,
        dtype=np.dtype(np.float64),
        shape=(None,),
        field_name="ICP target 신뢰도",
    )
    source_candidates = _readonly_array(
        source_isolation_candidates_bool,
        dtype=np.dtype(np.bool_),
        shape=(source_weights.shape[0],),
        field_name="ICP source 고립 후보",
    )
    target_candidates = _readonly_array(
        target_isolation_candidates_bool,
        dtype=np.dtype(np.bool_),
        shape=(target_weights.shape[0],),
        field_name="ICP target 고립 후보",
    )
    if np.any(residuals < 0.0):
        raise IcpConfidenceError("ICP 평가 잔차 거리는 0 이상이어야 합니다.")
    if (
        np.any(source_weights <= 0.0)
        or np.any(source_weights > 1.0)
        or np.any(target_weights <= 0.0)
        or np.any(target_weights > 1.0)
    ):
        raise IcpConfidenceError("ICP 평가 신뢰도는 0 초과 1 이하이어야 합니다.")
    if residuals.size == 0:
        return {
            "weighted_point_to_point_rmse": None,
            "residual_median": None,
            "residual_p90": None,
            "evaluation_correspondence_count": 0,
            "low_weight_correspondence_count": 0,
            "isolation_candidate_correspondence_count": 0,
            "source_isolation_candidate_count": int(np.count_nonzero(source_candidates)),
            "target_isolation_candidate_count": int(np.count_nonzero(target_candidates)),
            "distance_unit": DISTANCE_UNIT_PROJECT_WORLD,
        }
    if (
        np.any(pairs[:, 0] < 0)
        or np.any(pairs[:, 0] >= source_weights.shape[0])
        or np.any(pairs[:, 1] < 0)
        or np.any(pairs[:, 1] >= target_weights.shape[0])
    ):
        raise IcpConfidenceError("ICP 평가 대응점 index가 신뢰도 배열 범위를 벗어났습니다.")
    pair_weights = np.minimum(
        source_weights[pairs[:, 0]],
        target_weights[pairs[:, 1]],
    )
    weight_sum = float(np.sum(pair_weights, dtype=np.float64))
    if not isfinite(weight_sum) or weight_sum <= 0.0:
        raise IcpConfidenceError("ICP 평가 대응점 신뢰도 합이 양수가 아닙니다.")
    weighted_rmse = float(
        np.sqrt(
            np.sum(pair_weights * np.square(residuals), dtype=np.float64)
            / weight_sum
        )
    )
    pair_candidates = (
        source_candidates[pairs[:, 0]] | target_candidates[pairs[:, 1]]
    )
    return {
        "weighted_point_to_point_rmse": weighted_rmse,
        "residual_median": float(np.median(residuals)),
        "residual_p90": float(np.percentile(residuals, 90.0)),
        "evaluation_correspondence_count": int(residuals.shape[0]),
        "low_weight_correspondence_count": int(
            np.count_nonzero(pair_weights < LOW_WEIGHT_THRESHOLD)
        ),
        "isolation_candidate_correspondence_count": int(np.count_nonzero(pair_candidates)),
        "source_isolation_candidate_count": int(np.count_nonzero(source_candidates)),
        "target_isolation_candidate_count": int(np.count_nonzero(target_candidates)),
        "distance_unit": DISTANCE_UNIT_PROJECT_WORLD,
    }


__all__ = (
    "DEFAULT_CONFIDENCE_NEIGHBORS",
    "DEFAULT_ISOLATION_Z_THRESHOLD",
    "DISTANCE_UNIT_PROJECT_WORLD",
    "IcpConfidenceError",
    "LOW_WEIGHT_THRESHOLD",
    "PointDistributionConfidence",
    "compute_point_distribution_confidence",
    "confidence_from_mean_neighbor_distances",
    "mean_knn_distances_with_blender",
    "weighted_residual_metrics",
)
