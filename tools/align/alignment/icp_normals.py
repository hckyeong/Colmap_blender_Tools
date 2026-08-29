"""worker ICP 샘플의 PCA 노멀과 신뢰도 지표를 계산합니다."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from math import cos, isfinite, pi
from typing import Callable

import numpy as np

from .icp_index import NearestNeighborIndex
from .icp_types import IcpCancelledError, IcpInputError
from .types import Sim3Transform


CancelProbe = Callable[[], bool] | None


def _freeze_float64(value: object, shape: tuple[int | None, ...], field_name: str) -> np.ndarray:
    """노멀 계산 결과에 쓸 유한 float64 배열을 읽기 전용으로 고정합니다."""

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


def _strict_positive_int(value: object, field_name: str) -> int:
    """bool·문자열 변환 없이 양의 정수 설정만 허용합니다."""

    if isinstance(value, bool) or not isinstance(value, (int, np.integer)):
        raise IcpInputError(f"{field_name}은 bool이 아닌 정수여야 합니다.")
    normalized = int(value)
    if normalized < 1:
        raise IcpInputError(f"{field_name}은 1 이상이어야 합니다.")
    return normalized


def _strict_finite_float(value: object, field_name: str) -> float:
    """bool·문자열 변환 없이 유한 실수 설정만 허용합니다."""

    if isinstance(value, bool) or not isinstance(value, (int, float, np.integer, np.floating)):
        raise IcpInputError(f"{field_name}은 bool·문자열이 아닌 실수여야 합니다.")
    normalized = float(value)
    if not isfinite(normalized):
        raise IcpInputError(f"{field_name}은 유한해야 합니다.")
    return normalized


def _raise_if_cancelled(cancel_probe: CancelProbe) -> None:
    """노멀 추정 block 경계에서 취소 요청을 공통 오류로 전달합니다."""

    if cancel_probe is not None and bool(cancel_probe()):
        raise IcpCancelledError("사용자가 ICP 노멀 추정 취소를 요청했습니다.")


@dataclass(frozen=True, slots=True)
class IcpNormalEstimate:
    """샘플별 PCA 노멀·유효도·평면성·곡률을 함께 보관합니다."""

    normals_float64: np.ndarray
    valid_mask: np.ndarray
    neighbor_counts_int64: np.ndarray
    planarity_float64: np.ndarray
    curvature_float64: np.ndarray

    def __post_init__(self) -> None:
        """노멀 결과 배열의 길이·범위·읽기 전용 상태를 검증합니다."""

        normals = _freeze_float64(self.normals_float64, (None, 3), "ICP PCA 노멀")
        valid_source = np.asarray(self.valid_mask)
        if valid_source.dtype != np.dtype(bool):
            raise IcpInputError("ICP 노멀 유효 마스크는 bool 배열이어야 합니다.")
        valid = np.array(valid_source, dtype=bool, order="C", copy=True)
        counts_source = np.asarray(self.neighbor_counts_int64)
        if counts_source.dtype.kind not in {"u", "i"}:
            raise IcpInputError("ICP 노멀 이웃 수는 정수 배열이어야 합니다.")
        counts = np.array(counts_source, dtype=np.int64, order="C", copy=True)
        planarity = _freeze_float64(self.planarity_float64, (normals.shape[0],), "ICP 노멀 평면성")
        curvature = _freeze_float64(self.curvature_float64, (normals.shape[0],), "ICP 노멀 곡률")
        if (
            valid.shape != (normals.shape[0],)
            or counts.shape != (normals.shape[0],)
            or np.any(counts < 0)
            or np.any(planarity < 0.0)
            or np.any(planarity > 1.0)
            or np.any(curvature < 0.0)
            or np.any(curvature > 1.0)
            or np.any(~np.isfinite(normals))
        ):
            raise IcpInputError("ICP PCA 노멀 결과 범위가 올바르지 않습니다.")
        norms = np.linalg.norm(normals, axis=1)
        if np.any(valid & ~np.isclose(norms, 1.0, rtol=0.0, atol=1.0e-8)):
            raise IcpInputError("유효한 ICP PCA 노멀은 단위 벡터여야 합니다.")
        if np.any(~valid & (norms > 0.0)):
            raise IcpInputError("무효 ICP PCA 노멀은 영 벡터여야 합니다.")
        valid.setflags(write=False)
        counts.setflags(write=False)
        object.__setattr__(self, "normals_float64", normals)
        object.__setattr__(self, "valid_mask", valid)
        object.__setattr__(self, "neighbor_counts_int64", counts)
        object.__setattr__(self, "planarity_float64", planarity)
        object.__setattr__(self, "curvature_float64", curvature)

    @property
    def valid_count(self) -> int:
        """point-to-plane에 쓸 수 있는 유효 노멀 수를 반환합니다."""

        return int(np.count_nonzero(self.valid_mask))

    @property
    def valid_ratio(self) -> float:
        """샘플에서 유효 노멀이 차지하는 비율을 반환합니다."""

        return float(self.valid_count / max(self.valid_mask.size, 1))


def _canonical_normal(normal: np.ndarray) -> np.ndarray:
    """PCA 부호 모호성을 결정적 가장 큰 성분 규칙으로 고정합니다."""

    maximum_axis = int(np.argmax(np.abs(normal)))
    return -normal if normal[maximum_axis] < 0.0 else normal


def estimate_sample_normals(
    positions_float64: object,
    *,
    index: NearestNeighborIndex,
    radius: float,
    minimum_neighbors: int,
    maximum_neighbors: int,
    minimum_planarity: float,
    maximum_curvature: float,
    query_block_size: int = 4_096,
    cancel_probe: CancelProbe = None,
) -> IcpNormalEstimate:
    """반경 KD-tree 이웃의 PCA로 노멀과 신뢰도를 계산합니다."""

    positions = _freeze_float64(positions_float64, (None, 3), "ICP 노멀 샘플 점")
    if positions.shape[0] == 0 or not isinstance(index, NearestNeighborIndex):
        raise IcpInputError("ICP 노멀 추정에는 비어 있지 않은 index와 샘플이 필요합니다.")
    normalized_radius = _strict_finite_float(radius, "ICP 노멀 반경")
    minimum = _strict_positive_int(minimum_neighbors, "ICP 노멀 최소 이웃 수")
    maximum = _strict_positive_int(maximum_neighbors, "ICP 노멀 최대 이웃 수")
    block_size = _strict_positive_int(query_block_size, "ICP 노멀 query block 크기")
    minimum_planarity_value = _strict_finite_float(minimum_planarity, "ICP 노멀 최소 평면성")
    maximum_curvature_value = _strict_finite_float(maximum_curvature, "ICP 노멀 최대 곡률")
    if (
        normalized_radius <= 0.0
        or minimum < 3
        or maximum < minimum
        or not 0.0 <= minimum_planarity_value <= 1.0
        or not 0.0 <= maximum_curvature_value <= 1.0
    ):
        raise IcpInputError("ICP 노멀 설정 범위가 올바르지 않습니다.")
    normals = np.zeros((positions.shape[0], 3), dtype=np.float64)
    valid = np.zeros(positions.shape[0], dtype=bool)
    counts = np.zeros(positions.shape[0], dtype=np.int64)
    planarity = np.zeros(positions.shape[0], dtype=np.float64)
    curvature = np.zeros(positions.shape[0], dtype=np.float64)
    for start in range(0, positions.shape[0], block_size):
        _raise_if_cancelled(cancel_probe)
        stop = min(start + block_size, positions.shape[0])
        neighbors = index.query_radius(
            positions[start:stop],
            radius=normalized_radius,
            maximum_neighbors=maximum,
            cancel_probe=cancel_probe,
        )
        if neighbors.query_count != stop - start:
            raise IcpInputError("ICP 노멀 이웃 결과 길이가 query block과 다릅니다.")
        for local_index, neighbor_indices in enumerate(neighbors.indices_by_query):
            point_index = start + local_index
            counts[point_index] = neighbor_indices.size
            if neighbor_indices.size < minimum:
                continue
            neighborhood = positions[neighbor_indices]
            centered = neighborhood - np.mean(neighborhood, axis=0)
            covariance = centered.T @ centered / float(neighborhood.shape[0])
            if not np.isfinite(covariance).all():
                continue
            try:
                eigenvalues, eigenvectors = np.linalg.eigh(covariance)
            except np.linalg.LinAlgError as error:
                raise IcpInputError("ICP PCA 노멀 고유분해에 실패했습니다.") from error
            if not np.isfinite(eigenvalues).all() or not np.isfinite(eigenvectors).all():
                continue
            total_variance = float(np.sum(eigenvalues))
            scale = max(float(eigenvalues[-1]), np.finfo(np.float64).tiny)
            rank_tolerance = max(scale * 1.0e-12, np.finfo(np.float64).tiny)
            if total_variance <= rank_tolerance or eigenvalues[1] <= rank_tolerance:
                continue
            local_planarity = float((eigenvalues[1] - eigenvalues[0]) / scale)
            local_curvature = float(eigenvalues[0] / total_variance)
            planarity[point_index] = local_planarity
            curvature[point_index] = local_curvature
            if local_planarity < minimum_planarity_value or local_curvature > maximum_curvature_value:
                continue
            normal = np.asarray(eigenvectors[:, 0], dtype=np.float64)
            normal_length = float(np.linalg.norm(normal))
            if not isfinite(normal_length) or normal_length <= np.finfo(np.float64).eps:
                continue
            normals[point_index] = _canonical_normal(normal / normal_length)
            valid[point_index] = True
    _raise_if_cancelled(cancel_probe)
    return IcpNormalEstimate(normals, valid, counts, planarity, curvature)


def transform_normals(normals_float64: object, transform: Sim3Transform) -> np.ndarray:
    """Sim(3)의 회전 성분만 적용해 노멀의 길이와 방향을 유지합니다."""

    normals = _freeze_float64(normals_float64, (None, 3), "ICP 변환 전 노멀")
    if not isinstance(transform, Sim3Transform):
        raise IcpInputError("ICP 노멀 변환에는 Sim3Transform이 필요합니다.")
    transformed = normals @ transform.rotation_float64.T
    lengths = np.linalg.norm(transformed, axis=1)
    nonzero = lengths > np.finfo(np.float64).eps
    transformed[nonzero] /= lengths[nonzero, np.newaxis]
    transformed[~nonzero] = 0.0
    return _freeze_float64(transformed, (normals.shape[0], 3), "ICP 변환된 노멀")


def normal_angle_mask(
    moving_normals_float64: object,
    reference_normals_float64: object,
    *,
    maximum_angle_degrees: float,
    use_absolute_dot: bool = True,
) -> np.ndarray:
    """부호 독립 dot 기준으로 point-to-plane 대응점 각도 gate를 계산합니다."""

    moving = _freeze_float64(moving_normals_float64, (None, 3), "ICP P 노멀")
    reference = _freeze_float64(reference_normals_float64, (moving.shape[0], 3), "ICP A 노멀")
    angle = _strict_finite_float(maximum_angle_degrees, "ICP 노멀 최대 각도")
    if type(use_absolute_dot) is not bool or not 0.0 < angle <= 90.0:
        raise IcpInputError("ICP 노멀 각도 gate 설정이 올바르지 않습니다.")
    moving_lengths = np.linalg.norm(moving, axis=1)
    reference_lengths = np.linalg.norm(reference, axis=1)
    valid = (moving_lengths > np.finfo(np.float64).eps) & (reference_lengths > np.finfo(np.float64).eps)
    normalized_dot = np.zeros(moving.shape[0], dtype=np.float64)
    normalized_dot[valid] = np.einsum(
        "ij,ij->i",
        moving[valid] / moving_lengths[valid, np.newaxis],
        reference[valid] / reference_lengths[valid, np.newaxis],
        optimize=True,
    )
    if use_absolute_dot:
        normalized_dot = np.abs(normalized_dot)
    threshold = cos(angle * pi / 180.0)
    mask = valid & (np.clip(normalized_dot, -1.0, 1.0) >= threshold)
    mask.setflags(write=False)
    return mask


def normal_cache_key(
    *,
    model_id: str,
    model_revision: int,
    sample_fingerprint: str,
    voxel_size: float,
    radius: float,
    minimum_neighbors: int,
    maximum_neighbors: int,
    minimum_planarity: float,
    maximum_curvature: float,
) -> str:
    """동일 worker job의 재사용 가능한 PCA 노멀 cache key를 생성합니다."""

    normalized_model_id = str(model_id).strip()
    normalized_fingerprint = str(sample_fingerprint).strip()
    if not normalized_model_id or not normalized_fingerprint:
        raise IcpInputError("ICP 노멀 cache key의 모델 ID 또는 표본 지문이 비어 있습니다.")
    if isinstance(model_revision, bool) or not isinstance(model_revision, (int, np.integer)):
        raise IcpInputError("ICP 노멀 모델 revision은 bool이 아닌 정수여야 합니다.")
    normalized_revision = int(model_revision)
    if normalized_revision < 0:
        raise IcpInputError("ICP 노멀 모델 revision은 0 이상이어야 합니다.")
    payload = {
        "model_id": normalized_model_id,
        "model_revision": normalized_revision,
        "sample_fingerprint": normalized_fingerprint,
        "voxel_size": _strict_finite_float(voxel_size, "ICP 노멀 voxel 크기"),
        "radius": _strict_finite_float(radius, "ICP 노멀 반경"),
        "minimum_neighbors": _strict_positive_int(minimum_neighbors, "ICP 노멀 최소 이웃 수"),
        "maximum_neighbors": _strict_positive_int(maximum_neighbors, "ICP 노멀 최대 이웃 수"),
        "minimum_planarity": _strict_finite_float(minimum_planarity, "ICP 노멀 최소 평면성"),
        "maximum_curvature": _strict_finite_float(maximum_curvature, "ICP 노멀 최대 곡률"),
    }
    return hashlib.blake2b(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8"),
        digest_size=20,
    ).hexdigest()


__all__ = (
    "IcpNormalEstimate",
    "estimate_sample_normals",
    "normal_angle_mask",
    "normal_cache_key",
    "transform_normals",
)
