"""ICP backend가 공유하는 제한형 균일 스케일 보정 수치 함수를 제공합니다."""

from __future__ import annotations

from math import isfinite

import numpy as np


class BoundedScaleRefineError(ValueError):
    """제한형 Sim(3) 계산의 입력·퇴화·수치 오류를 나타냅니다."""


def _positions(value: object, field_name: str) -> np.ndarray:
    """대응점 입력을 유한한 C-contiguous float64 N×3 배열로 정규화합니다."""

    try:
        positions = np.asarray(value, dtype=np.float64)
    except (OverflowError, TypeError, ValueError) as error:
        raise BoundedScaleRefineError(f"{field_name} 배열을 읽지 못했습니다.") from error
    if positions.ndim != 2 or positions.shape[1:] != (3,):
        raise BoundedScaleRefineError(f"{field_name} 배열은 N×3이어야 합니다.")
    if not np.isfinite(positions).all():
        raise BoundedScaleRefineError(f"{field_name} 배열에 유한하지 않은 값이 있습니다.")
    return np.ascontiguousarray(positions, dtype=np.float64)


def solve_bounded_scale_matrix(
    moving_positions_float64: object,
    reference_positions_float64: object,
    scale_limits: tuple[float, float],
) -> np.ndarray:
    """대응점 사이의 proper Sim(3)을 주어진 양의 스케일 범위 안에서 계산합니다."""

    moving = _positions(moving_positions_float64, "스케일 보정 P 대응점")
    reference = _positions(reference_positions_float64, "스케일 보정 A 대응점")
    if moving.shape != reference.shape or moving.shape[0] < 3:
        raise BoundedScaleRefineError("스케일 보정에는 같은 개수의 대응점 3개 이상이 필요합니다.")
    if not isinstance(scale_limits, tuple) or len(scale_limits) != 2:
        raise BoundedScaleRefineError("스케일 보정에는 (최소, 최대) 범위가 필요합니다.")
    try:
        lower, upper = float(scale_limits[0]), float(scale_limits[1])
    except (OverflowError, TypeError, ValueError) as error:
        raise BoundedScaleRefineError("스케일 보정 범위를 수치로 읽지 못했습니다.") from error
    if not isfinite(lower) or not isfinite(upper) or lower <= 0.0 or upper < lower:
        raise BoundedScaleRefineError("스케일 보정 범위는 유한한 양수이며 최소값이 최대값 이하여야 합니다.")

    moving_mean = np.mean(moving, axis=0)
    reference_mean = np.mean(reference, axis=0)
    moving_centered = moving - moving_mean
    reference_centered = reference - reference_mean
    covariance = moving_centered.T @ reference_centered
    try:
        left, singular_values, right_t = np.linalg.svd(covariance, full_matrices=False)
    except np.linalg.LinAlgError as error:
        raise BoundedScaleRefineError("스케일 보정 대응점 SVD에 실패했습니다.") from error
    if not np.isfinite(singular_values).all():
        raise BoundedScaleRefineError("스케일 보정 singular value가 유한하지 않습니다.")
    largest = float(singular_values[0])
    rank_tolerance = max(largest * 1.0e-12, np.finfo(np.float64).tiny)
    if int(np.count_nonzero(singular_values > rank_tolerance)) < 2:
        raise BoundedScaleRefineError("스케일 보정 대응점이 공선이어서 변환을 결정할 수 없습니다.")

    correction = np.identity(3, dtype=np.float64)
    raw_rotation = right_t.T @ left.T
    correction[2, 2] = 1.0 if np.linalg.det(raw_rotation) >= 0.0 else -1.0
    rotation = right_t.T @ correction @ left.T
    signed_singular_values = singular_values * np.diag(correction)
    variance = float(np.mean(np.sum(np.square(moving_centered), axis=1)))
    if not isfinite(variance) or variance <= np.finfo(np.float64).eps:
        raise BoundedScaleRefineError("스케일 보정 P 대응점 분산이 너무 작습니다.")
    raw_scale = float(np.sum(signed_singular_values) / (moving.shape[0] * variance))
    if not isfinite(raw_scale) or raw_scale <= 0.0:
        raise BoundedScaleRefineError("스케일 보정 결과가 양의 유한한 값이 아닙니다.")
    scale = float(np.clip(raw_scale, lower, upper))
    translation = reference_mean - scale * (rotation @ moving_mean)
    matrix = np.identity(4, dtype=np.float64)
    matrix[:3, :3] = scale * rotation
    matrix[:3, 3] = translation
    return np.ascontiguousarray(matrix, dtype=np.float64)


def solve_bounded_uniform_scale_matrix(
    moving_positions_float64: object,
    reference_positions_float64: object,
    scale_limits: tuple[float, float],
) -> np.ndarray:
    """회전 없이 대응점 중심 기준의 균일 스케일과 평행이동만 계산합니다."""

    moving = _positions(moving_positions_float64, "스케일 보정 P 대응점")
    reference = _positions(reference_positions_float64, "스케일 보정 A 대응점")
    if moving.shape != reference.shape or moving.shape[0] < 3:
        raise BoundedScaleRefineError("스케일 보정에는 같은 개수의 대응점 3개 이상이 필요합니다.")
    if not isinstance(scale_limits, tuple) or len(scale_limits) != 2:
        raise BoundedScaleRefineError("스케일 보정에는 (최소, 최대) 범위가 필요합니다.")
    try:
        lower, upper = float(scale_limits[0]), float(scale_limits[1])
    except (OverflowError, TypeError, ValueError) as error:
        raise BoundedScaleRefineError("스케일 보정 범위를 수치로 읽지 못했습니다.") from error
    if not isfinite(lower) or not isfinite(upper) or lower <= 0.0 or upper < lower:
        raise BoundedScaleRefineError("스케일 보정 범위는 유한한 양수이며 최소값이 최대값 이하여야 합니다.")

    moving_mean = np.mean(moving, axis=0)
    reference_mean = np.mean(reference, axis=0)
    moving_centered = moving - moving_mean
    reference_centered = reference - reference_mean
    denominator = float(np.sum(np.square(moving_centered)))
    if not isfinite(denominator) or denominator <= np.finfo(np.float64).eps:
        raise BoundedScaleRefineError("스케일 보정 P 대응점 분산이 너무 작습니다.")
    numerator = float(np.sum(moving_centered * reference_centered))
    raw_scale = numerator / denominator
    if not isfinite(raw_scale) or raw_scale <= 0.0:
        raise BoundedScaleRefineError("스케일 보정 결과가 양의 유한한 값이 아닙니다.")
    scale = float(np.clip(raw_scale, lower, upper))
    translation = reference_mean - scale * moving_mean
    matrix = np.identity(4, dtype=np.float64)
    matrix[:3, :3] *= scale
    matrix[:3, 3] = translation
    return np.ascontiguousarray(matrix, dtype=np.float64)


__all__ = (
    "BoundedScaleRefineError",
    "solve_bounded_scale_matrix",
    "solve_bounded_uniform_scale_matrix",
)
