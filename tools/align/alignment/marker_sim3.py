"""NumPy만 사용해 마커 대응점의 P→A Sim(3)을 계산합니다."""

from __future__ import annotations

from itertools import combinations
from math import isfinite

import numpy as np

from ..constants import (
    MARKER_ALIGNMENT_RELATIVE_DEGENERACY_EPS,
    MARKER_ALIGNMENT_TIE_TOLERANCE,
)
from .types import (
    MarkerAlignmentInput,
    MarkerAlignmentResult,
    MarkerDegeneracyError,
    MarkerInputError,
    MarkerNumericalError,
    MarkerResidualMetrics,
    RepresentativeTriangle,
    Sim3Transform,
    compose_sim3_transforms,
    create_sim3_transform,
    transform_positions,
)


def _as_marker_array(value: object, field_name: str) -> np.ndarray:
    """마커 점 배열을 유한한 연속 float64 N×3 배열로 검증해 복사합니다."""

    points = np.ascontiguousarray(value, dtype=np.float64)
    if points.ndim != 2 or points.shape[1] != 3:
        raise MarkerInputError(f"{field_name} 마커는 N×3 float64 배열이어야 합니다.")
    if not np.isfinite(points).all():
        raise MarkerInputError(f"{field_name} 마커에 유한하지 않은 좌표가 있습니다.")
    return points


def validate_marker_alignment_arrays(
    reference: object,
    moving: object,
) -> tuple[np.ndarray, np.ndarray]:
    """A/P 마커 배열의 shape·개수·유한성만 상태 변경 없이 검증합니다."""

    reference_points = _as_marker_array(reference, "A")
    moving_points = _as_marker_array(moving, "P")
    if reference_points.shape != moving_points.shape:
        raise MarkerInputError("A와 P 마커 배열의 형상이 서로 다릅니다.")
    if reference_points.shape[0] < 3:
        raise MarkerInputError("마커 정렬에는 완성된 대응쌍이 최소 3개 필요합니다.")
    return reference_points, moving_points


def _center_and_normalize(points: np.ndarray, role_label: str) -> tuple[np.ndarray, np.ndarray, float]:
    """점 집합을 중심 이동·RMS 반경 정규화하고 퇴화 분포를 거부합니다."""

    center = np.mean(points, axis=0, dtype=np.float64)
    centered = points - center[np.newaxis, :]
    squared_radius = float(np.mean(np.einsum("ij,ij->i", centered, centered)))
    radius = float(np.sqrt(squared_radius))
    coordinate_scale = max(float(np.max(np.abs(points))), 1.0)
    if not isfinite(radius) or radius <= coordinate_scale * np.finfo(np.float64).eps:
        raise MarkerDegeneracyError(f"{role_label} 마커가 한 위치에 겹쳐 있어 정렬할 수 없습니다.")
    normalized = centered / radius
    rank = int(
        np.linalg.matrix_rank(
            normalized,
            tol=MARKER_ALIGNMENT_RELATIVE_DEGENERACY_EPS,
        )
    )
    if rank < 2:
        raise MarkerDegeneracyError(
            f"{role_label} 마커가 거의 일직선에 있어 Sim(3) 정렬을 계산할 수 없습니다."
        )
    return center, normalized, radius


def _validate_scale(scale: float) -> float:
    """증감률을 제한하지 않고 유한한 양의 마커 균일 스케일만 허용합니다."""

    numeric_scale = float(scale)
    if not isfinite(numeric_scale) or numeric_scale <= 0.0:
        raise MarkerNumericalError("계산한 Sim(3) 균일 스케일이 양수가 아닙니다.")
    return numeric_scale


def solve_similarity_transform(
    reference: object,
    moving: object,
    *,
    use_uniform_scale: bool = True,
) -> Sim3Transform:
    """모든 P 마커를 A Project World로 맞추는 proper Sim(3) 또는 강체 delta를 계산합니다."""

    reference_points, moving_points = validate_marker_alignment_arrays(reference, moving)
    reference_center, reference_normalized, _reference_radius = _center_and_normalize(
        reference_points,
        "A",
    )
    moving_center, moving_normalized, moving_radius = _center_and_normalize(
        moving_points,
        "P",
    )
    count = float(reference_points.shape[0])
    covariance = (moving_normalized.T @ reference_normalized) / count
    if not np.isfinite(covariance).all():
        raise MarkerNumericalError("마커 Sim(3) 공분산에 유한하지 않은 값이 있습니다.")
    try:
        left_vectors, singular_values, right_vectors_transposed = np.linalg.svd(
            covariance,
            full_matrices=False,
        )
    except np.linalg.LinAlgError as error:
        raise MarkerNumericalError("마커 Sim(3) SVD 계산에 실패했습니다.") from error
    if (
        singular_values.shape != (3,)
        or not np.isfinite(singular_values).all()
        or not np.isfinite(left_vectors).all()
        or not np.isfinite(right_vectors_transposed).all()
    ):
        raise MarkerNumericalError("마커 Sim(3) SVD 결과가 유효하지 않습니다.")

    right_vectors = right_vectors_transposed.T
    correction = np.identity(3, dtype=np.float64)
    uncorrected_rotation = right_vectors @ left_vectors.T
    if float(np.linalg.det(uncorrected_rotation)) < 0.0:
        correction[-1, -1] = -1.0
    rotation = right_vectors @ correction @ left_vectors.T
    rotation_determinant = float(np.linalg.det(rotation))
    if not np.isfinite(rotation).all() or not np.isclose(
        rotation_determinant,
        1.0,
        rtol=0.0,
        atol=1.0e-10,
    ):
        raise MarkerNumericalError("마커 Sim(3) 회전이 proper rotation이 아닙니다.")

    if bool(use_uniform_scale):
        # 정규화 좌표에서의 SVD scale을 원래 P/A 반경 비율로 되돌립니다.
        normalized_scale = float(np.sum(singular_values * np.diag(correction)))
        scale = _validate_scale(normalized_scale * (_reference_radius / moving_radius))
    else:
        scale = 1.0
    translation = reference_center - scale * (rotation @ moving_center)
    if not np.isfinite(translation).all():
        raise MarkerNumericalError("마커 Sim(3) 이동 결과에 유한하지 않은 값이 있습니다.")
    try:
        return create_sim3_transform(rotation, scale, translation)
    except (TypeError, ValueError, np.linalg.LinAlgError) as error:
        raise MarkerNumericalError("마커 Sim(3) 행렬을 만들지 못했습니다.") from error


def compute_marker_residual_metrics(
    reference: object,
    transformed_moving: object,
    *,
    uniform_scale: float,
    rotation_determinant: float,
) -> MarkerResidualMetrics:
    """변환된 P와 A 전체 마커의 점별 거리 잔차와 요약 지표를 계산합니다."""

    reference_points, transformed_points = validate_marker_alignment_arrays(
        reference,
        transformed_moving,
    )
    residuals = np.linalg.norm(reference_points - transformed_points, axis=1)
    if not np.isfinite(residuals).all():
        raise MarkerNumericalError("마커 잔차 계산 결과에 유한하지 않은 값이 있습니다.")
    _center, _normalized, reference_radius = _center_and_normalize(reference_points, "A")
    rms = float(np.sqrt(np.mean(np.square(residuals))))
    return MarkerResidualMetrics(
        count=int(residuals.shape[0]),
        rms=rms,
        median=float(np.median(residuals)),
        maximum=float(np.max(residuals)),
        normalized_rms=rms / reference_radius,
        residuals_float64=residuals,
        uniform_scale=float(uniform_scale),
        rotation_determinant=float(rotation_determinant),
    )


def _line_distance(points: np.ndarray, first_index: int, second_index: int, index: int) -> float:
    """정규화 좌표에서 한 점과 두 점 직선 사이의 유한 거리를 계산합니다."""

    segment = points[second_index] - points[first_index]
    segment_length = float(np.linalg.norm(segment))
    if segment_length <= np.finfo(np.float64).eps:
        raise MarkerDegeneracyError("대표 삼각형의 첫 두 마커가 서로 겹칩니다.")
    distance = float(
        np.linalg.norm(np.cross(segment, points[index] - points[first_index]))
        / segment_length
    )
    if not isfinite(distance):
        raise MarkerNumericalError("대표 삼각형 거리 계산 결과가 유효하지 않습니다.")
    return distance


def select_representative_triangle(
    reference: object,
    moving: object,
    pair_ids: tuple[int, ...] | list[int],
) -> RepresentativeTriangle:
    """A/P 양쪽에 넓게 분포한 세 마커를 결정적 O(N²) 근사로 선택합니다."""

    reference_points, moving_points = validate_marker_alignment_arrays(reference, moving)
    normalized_pair_ids = tuple(int(pair_id) for pair_id in pair_ids)
    if len(normalized_pair_ids) != reference_points.shape[0]:
        raise MarkerInputError("대표 삼각형 pair_id 개수가 마커 수와 다릅니다.")
    if any(pair_id <= 0 for pair_id in normalized_pair_ids) or len(set(normalized_pair_ids)) != len(
        normalized_pair_ids
    ):
        raise MarkerInputError("대표 삼각형 pair_id는 서로 다른 양수여야 합니다.")

    _reference_center, normalized_reference, _reference_radius = _center_and_normalize(
        reference_points,
        "A",
    )
    _moving_center, normalized_moving, _moving_radius = _center_and_normalize(
        moving_points,
        "P",
    )
    ordered_indices = tuple(sorted(range(len(normalized_pair_ids)), key=lambda index: normalized_pair_ids[index]))

    best_pair: tuple[int, int] | None = None
    best_pair_score = -np.inf
    best_pair_ids: tuple[int, int] | None = None
    for first_index, second_index in combinations(ordered_indices, 2):
        reference_distance = float(
            np.linalg.norm(normalized_reference[first_index] - normalized_reference[second_index])
        )
        moving_distance = float(
            np.linalg.norm(normalized_moving[first_index] - normalized_moving[second_index])
        )
        score = min(reference_distance, moving_distance)
        candidate_ids = tuple(sorted((normalized_pair_ids[first_index], normalized_pair_ids[second_index])))
        if (
            score > best_pair_score + MARKER_ALIGNMENT_TIE_TOLERANCE
            or (
                abs(score - best_pair_score) <= MARKER_ALIGNMENT_TIE_TOLERANCE
                and (best_pair_ids is None or candidate_ids < best_pair_ids)
            )
        ):
            best_pair = (first_index, second_index)
            best_pair_score = score
            best_pair_ids = candidate_ids
    if best_pair is None or best_pair_score <= MARKER_ALIGNMENT_RELATIVE_DEGENERACY_EPS:
        raise MarkerDegeneracyError("대표 삼각형의 첫 두 마커가 충분히 떨어져 있지 않습니다.")

    first_index, second_index = best_pair
    best_third_index: int | None = None
    best_third_score = -np.inf
    best_third_pair_id: int | None = None
    for candidate_index in ordered_indices:
        if candidate_index in best_pair:
            continue
        reference_distance = _line_distance(
            normalized_reference,
            first_index,
            second_index,
            candidate_index,
        )
        moving_distance = _line_distance(
            normalized_moving,
            first_index,
            second_index,
            candidate_index,
        )
        score = min(reference_distance, moving_distance)
        candidate_pair_id = normalized_pair_ids[candidate_index]
        if (
            score > best_third_score + MARKER_ALIGNMENT_TIE_TOLERANCE
            or (
                abs(score - best_third_score) <= MARKER_ALIGNMENT_TIE_TOLERANCE
                and (best_third_pair_id is None or candidate_pair_id < best_third_pair_id)
            )
        ):
            best_third_index = candidate_index
            best_third_score = score
            best_third_pair_id = candidate_pair_id
    if (
        best_third_index is None
        or best_third_score <= MARKER_ALIGNMENT_RELATIVE_DEGENERACY_EPS
    ):
        raise MarkerDegeneracyError(
            "마커가 거의 일직선에 있어 대표 삼각형을 만들 수 없습니다."
        )

    selected_indices = tuple(
        sorted(
            (first_index, second_index, best_third_index),
            key=lambda index: normalized_pair_ids[index],
        )
    )
    selected_pair_ids = tuple(normalized_pair_ids[index] for index in selected_indices)
    return RepresentativeTriangle(
        pair_ids=selected_pair_ids,
        input_indices=selected_indices,
        reference_vertices=reference_points[list(selected_indices)],
        moving_vertices=moving_points[list(selected_indices)],
    )


def build_marker_alignment_result(
    input_data: MarkerAlignmentInput,
    base_alignment: Sim3Transform,
    *,
    use_uniform_scale: bool = True,
) -> MarkerAlignmentResult:
    """스케일 사용 설정과 불변 입력으로 비파괴 marker preview 결과를 생성합니다."""

    if not isinstance(input_data, MarkerAlignmentInput):
        raise TypeError("마커 정렬 결과에는 MarkerAlignmentInput이 필요합니다.")
    if not isinstance(base_alignment, Sim3Transform):
        raise TypeError("마커 정렬 결과에는 확정 Sim(3) transform이 필요합니다.")
    delta_transform = solve_similarity_transform(
        input_data.reference_project,
        input_data.moving_project,
        use_uniform_scale=use_uniform_scale,
    )
    transformed_moving = transform_positions(delta_transform, input_data.moving_project)
    metrics = compute_marker_residual_metrics(
        input_data.reference_project,
        transformed_moving,
        uniform_scale=delta_transform.uniform_scale,
        rotation_determinant=float(np.linalg.det(delta_transform.rotation_float64)),
    )
    representative_triangle = select_representative_triangle(
        input_data.reference_project,
        input_data.moving_project,
        input_data.pair_ids,
    )
    preview_alignment = compose_sim3_transforms(delta_transform, base_alignment)
    return MarkerAlignmentResult(
        moving_model_id=input_data.moving_model_id,
        base_alignment=base_alignment,
        delta_transform=delta_transform,
        preview_alignment=preview_alignment,
        metrics=metrics,
        representative_triangle=representative_triangle,
        marker_signature=input_data.marker_signature,
        source_revision_snapshot=input_data.source_revision_snapshot,
    )


__all__ = (
    "build_marker_alignment_result",
    "compute_marker_residual_metrics",
    "select_representative_triangle",
    "solve_similarity_transform",
    "validate_marker_alignment_arrays",
)
