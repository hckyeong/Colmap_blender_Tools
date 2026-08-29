"""ICP 최종 수치 지표와 PASS·REVIEW·REJECT 품질 판정을 제공합니다."""

from __future__ import annotations

from math import isfinite
from typing import Mapping, Sequence

import numpy as np

from .icp_index import NearestNeighborIndex
from .icp_types import (
    IcpDensitySamplingMetrics,
    IcpEvaluationMetrics,
    IcpInputError,
    IcpPerReferenceMetrics,
    IcpQualityMetrics,
    IcpQualityStatus,
    IcpScaleMode,
    IcpScaleRefineStatus,
    IcpSolverStopReason,
    _strict_exact_bool,
    _strict_finite_float,
    _strict_nonnegative_int,
)
from .types import Sim3Transform


def _freeze_float64(value: object, shape: tuple[int | None, ...], field_name: str) -> np.ndarray:
    """품질 계산에 쓸 유한 float64 배열을 복사해 읽기 전용으로 고정합니다."""

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
    """품질 계산에 쓸 int64 배열을 복사해 읽기 전용으로 고정합니다."""

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


def transform_points(transform: Sim3Transform, positions_float64: object) -> np.ndarray:
    """품질 marker drift용으로 Sim(3)을 N×3 점에 적용한 새 배열을 반환합니다."""

    if not isinstance(transform, Sim3Transform):
        raise IcpInputError("ICP 품질 점 변환에는 Sim3Transform이 필요합니다.")
    positions = _freeze_float64(positions_float64, (None, 3), "ICP 품질 변환 점")
    transformed = positions @ transform.matrix_float64[:3, :3].T + transform.matrix_float64[:3, 3]
    return _freeze_float64(transformed, (positions.shape[0], 3), "ICP 품질 변환 결과")


def rotation_angle_degrees(rotation_float64: object) -> float:
    """proper rotation 행렬의 최소 회전각을 도 단위로 반환합니다."""

    rotation = _freeze_float64(rotation_float64, (3, 3), "ICP 회전 행렬")
    if not np.allclose(rotation.T @ rotation, np.identity(3), rtol=0.0, atol=1.0e-8):
        raise IcpInputError("ICP 회전 행렬은 직교 행렬이어야 합니다.")
    determinant = float(np.linalg.det(rotation))
    if not np.isclose(determinant, 1.0, rtol=0.0, atol=1.0e-8):
        raise IcpInputError("ICP 회전 행렬식은 +1이어야 합니다.")
    cosine = np.clip((float(np.trace(rotation)) - 1.0) * 0.5, -1.0, 1.0)
    return float(np.degrees(np.arccos(cosine)))


def delta_from_initial(
    initial_alignment: Sim3Transform,
    final_alignment: Sim3Transform,
) -> Sim3Transform:
    """`final × inverse(initial)` 순서의 ICP 추가 delta Sim(3)을 계산합니다."""

    if not isinstance(initial_alignment, Sim3Transform) or not isinstance(final_alignment, Sim3Transform):
        raise IcpInputError("ICP 추가 delta에는 initial/final Sim3Transform이 필요합니다.")
    return Sim3Transform(
        matrix_float64=final_alignment.matrix_float64 @ initial_alignment.inverse_float64
    )


def _local_delta_translation_magnitude(
    delta: Sim3Transform,
    local_origin_float64: object | None,
) -> float:
    """월드 원점 보정 translation 대신 ICP local-frame의 실제 추가 이동량을 계산합니다."""

    if local_origin_float64 is None:
        return float(np.linalg.norm(delta.translation_float64))
    origin = _freeze_float64(local_origin_float64, (3,), "ICP 품질 local origin")
    local_translation = (
        delta.translation_float64
        + delta.uniform_scale * (delta.rotation_float64 @ origin)
        - origin
    )
    return float(np.linalg.norm(local_translation))


def marker_drift_rms(
    final_alignment: Sim3Transform,
    marker_reference_project: object | None,
    marker_moving_project: object | None,
) -> float:
    """최종 transform 뒤 marker A/P 거리의 RMS drift를 계산하고 입력 없으면 0을 반환합니다."""

    if marker_reference_project is None and marker_moving_project is None:
        return 0.0
    if marker_reference_project is None or marker_moving_project is None:
        raise IcpInputError("ICP marker drift에는 A/P 마커가 함께 필요합니다.")
    reference = _freeze_float64(marker_reference_project, (None, 3), "ICP A 마커")
    moving = _freeze_float64(marker_moving_project, (reference.shape[0], 3), "ICP P 마커")
    if reference.shape[0] < 3:
        raise IcpInputError("ICP marker drift에는 마커 3개 이상이 필요합니다.")
    residuals = np.linalg.norm(transform_points(final_alignment, moving) - reference, axis=1)
    return float(np.sqrt(np.mean(np.square(residuals))))


def per_reference_metrics(
    reference_model_ids: Sequence[str],
    reference_model_indices: object,
    *,
    overlap_moving_coverage_by_model: Mapping[str, float] | None = None,
) -> tuple[IcpPerReferenceMetrics, ...]:
    """최종 inlier 기여도와 후보 단계의 모델별 overlap coverage를 함께 계산합니다."""

    model_ids = tuple(str(model_id).strip() for model_id in reference_model_ids)
    if not model_ids or any(not model_id for model_id in model_ids) or len(set(model_ids)) != len(model_ids):
        raise IcpInputError("ICP 기준 모델 ID가 올바르지 않습니다.")
    indices = _freeze_int64(reference_model_indices, (None,), "ICP 기준 모델 인덱스")
    if np.any(indices < 0) or np.any(indices >= len(model_ids)):
        raise IcpInputError("ICP 기준 모델 인덱스 범위가 올바르지 않습니다.")
    total = int(indices.size)
    return tuple(
        IcpPerReferenceMetrics(
            model_id=model_id,
            correspondence_count=int(np.count_nonzero(indices == index)),
            fraction=(float(np.count_nonzero(indices == index)) / total if total else 0.0),
            overlap_moving_coverage=(
                _strict_finite_float(
                    overlap_moving_coverage_by_model.get(model_id, 0.0),
                    "ICP 기준 모델 overlap coverage",
                )
                if overlap_moving_coverage_by_model is not None
                else 0.0
            ),
        )
        for index, model_id in enumerate(model_ids)
    )


def reciprocal_match_ratio(
    moving_positions_float64: object,
    reference_positions_float64: object,
    moving_indices: object,
    reference_indices: object,
    index: NearestNeighborIndex,
    *,
    maximum_distance: float,
    cancel_probe: object = None,
) -> float:
    """최종 P→A correspondence가 A→P 역검색에서도 같은 쌍인지 비율을 계산합니다."""

    maximum = _strict_finite_float(maximum_distance, "reciprocal 최대 대응 거리")
    if maximum <= 0.0:
        raise IcpInputError("reciprocal 최대 대응 거리는 0보다 커야 합니다.")
    moving = _freeze_float64(moving_positions_float64, (None, 3), "reciprocal P 좌표")
    reference = _freeze_float64(reference_positions_float64, (None, 3), "reciprocal A 좌표")
    source_indices = _freeze_int64(moving_indices, (None,), "reciprocal P 인덱스")
    target_indices = _freeze_int64(
        reference_indices,
        (source_indices.shape[0],),
        "reciprocal A 인덱스",
    )
    if source_indices.size == 0:
        return 0.0
    if (
        np.any(source_indices < 0)
        or np.any(source_indices >= moving.shape[0])
        or np.any(target_indices < 0)
        or np.any(target_indices >= reference.shape[0])
    ):
        raise IcpInputError("reciprocal correspondence 인덱스 범위가 올바르지 않습니다.")
    try:
        index.build(moving, cancel_probe=cancel_probe)
        reverse = index.query(
            reference[target_indices],
            maximum_distance=maximum,
            cancel_probe=cancel_probe,
        )
    finally:
        index.close()
    matches = reverse.reference_indices_int64 == source_indices
    return float(np.count_nonzero(matches) / source_indices.size)


def evaluate_icp_quality(
    *,
    residuals_float64: object,
    query_count: int,
    reciprocal_ratio: float,
    overlap_ratio: float,
    initial_alignment: Sim3Transform,
    final_alignment: Sim3Transform,
    marker_uniform_scale: float,
    scale_mode: IcpScaleMode | str,
    scale_refine_status: IcpScaleRefineStatus | str,
    scale_refine_reason: str,
    fine_voxel_size: float,
    overlap_diagonal: float,
    total_iterations: int,
    elapsed_seconds: float,
    per_reference_model: Sequence[IcpPerReferenceMetrics] = (),
    marker_reference_project: object | None = None,
    marker_moving_project: object | None = None,
    marker_rms: float = 0.0,
    local_origin_float64: object | None = None,
    scale_boundary_reached: bool = False,
    baseline_metrics: IcpEvaluationMetrics | None = None,
    best_candidate_metrics: IcpEvaluationMetrics | None = None,
    solver_stop_reason: IcpSolverStopReason | str = IcpSolverStopReason.NONE,
    point_to_plane_rmse: float = 0.0,
    normal_valid_ratio: float = 1.0,
    normal_angle_pass_ratio: float = 1.0,
    density_sampling_metrics: IcpDensitySamplingMetrics | None = None,
) -> IcpQualityMetrics:
    """계획서 임계값으로 최종 ICP 품질을 PASS·REVIEW·REJECT로 분류합니다."""

    residuals = _freeze_float64(residuals_float64, (None,), "ICP 최종 잔차")
    normalized_query_count = _strict_nonnegative_int(query_count, "ICP 품질 query 수")
    reciprocal = _strict_finite_float(reciprocal_ratio, "ICP reciprocal 비율")
    overlap = _strict_finite_float(overlap_ratio, "ICP overlap 비율")
    voxel = _strict_finite_float(fine_voxel_size, "ICP fine voxel 크기")
    diagonal = _strict_finite_float(overlap_diagonal, "ICP overlap 대각선")
    marker_scale = _strict_finite_float(marker_uniform_scale, "마커 균일 스케일")
    marker_rms = _strict_finite_float(marker_rms, "마커 RMS")
    elapsed = _strict_finite_float(elapsed_seconds, "ICP 경과 시간")
    normalized_total_iterations = _strict_nonnegative_int(total_iterations, "ICP 품질 총 반복 수")
    normalized_scale_boundary_reached = _strict_exact_bool(
        scale_boundary_reached,
        "ICP 스케일 안전 경계 도달 여부",
    )
    if residuals.size == 0 or normalized_query_count < 1:
        raise IcpInputError("ICP 품질 평가에는 잔차와 query가 하나 이상 필요합니다.")
    if residuals.size > normalized_query_count:
        raise IcpInputError("ICP 품질 inlier 수가 query 수보다 클 수 없습니다.")
    if (
        not 0.0 <= reciprocal <= 1.0
        or not 0.0 <= overlap <= 1.0
        or voxel <= 0.0
        or diagonal <= 0.0
        or marker_scale <= 0.0
        or marker_rms < 0.0
        or elapsed < 0.0
    ):
        raise IcpInputError("ICP 품질 평가 입력 범위가 올바르지 않습니다.")
    try:
        normalized_scale_mode = scale_mode if isinstance(scale_mode, IcpScaleMode) else IcpScaleMode(str(scale_mode))
        normalized_refine_status = (
            scale_refine_status
            if isinstance(scale_refine_status, IcpScaleRefineStatus)
            else IcpScaleRefineStatus(str(scale_refine_status))
        )
    except ValueError as error:
        raise IcpInputError("ICP 품질 scale 모드가 올바르지 않습니다.") from error
    if not isinstance(initial_alignment, Sim3Transform) or not isinstance(final_alignment, Sim3Transform):
        raise IcpInputError("ICP 품질 평가에는 initial/final Sim3Transform이 필요합니다.")
    rmse = float(np.sqrt(np.mean(np.square(residuals))))
    median = float(np.median(residuals))
    p90 = float(np.quantile(residuals, 0.90))
    fitness = float(residuals.size / normalized_query_count)
    delta = delta_from_initial(initial_alignment, final_alignment)
    # 큰 Project World 오프셋에서 회전 보정에 따라 커지는 행렬 translation을 이동량으로 쓰지 않습니다.
    translation = _local_delta_translation_magnitude(delta, local_origin_float64)
    rotation = rotation_angle_degrees(delta.rotation_float64)
    final_scale = float(final_alignment.uniform_scale)
    scale_ratio = final_scale / marker_scale
    drift = marker_drift_rms(final_alignment, marker_reference_project, marker_moving_project)
    per_reference = tuple(per_reference_model)
    if not all(isinstance(item, IcpPerReferenceMetrics) for item in per_reference):
        raise IcpInputError("ICP 기준 모델별 품질 형식이 올바르지 않습니다.")
    try:
        normalized_stop_reason = (
            solver_stop_reason
            if isinstance(solver_stop_reason, IcpSolverStopReason)
            else IcpSolverStopReason(str(solver_stop_reason))
        )
    except ValueError as error:
        raise IcpInputError("ICP 품질 solver 종료 사유가 올바르지 않습니다.") from error
    plane_rmse = _strict_finite_float(point_to_plane_rmse, "ICP 품질 plane RMSE")
    normal_valid = _strict_finite_float(normal_valid_ratio, "ICP 품질 노멀 유효 비율")
    normal_angle = _strict_finite_float(normal_angle_pass_ratio, "ICP 품질 노멀 각도 통과 비율")
    if plane_rmse < 0.0 or not 0.0 <= normal_valid <= 1.0 or not 0.0 <= normal_angle <= 1.0:
        raise IcpInputError("ICP 품질 노멀·plane 지표 범위가 올바르지 않습니다.")
    if baseline_metrics is not None and not isinstance(baseline_metrics, IcpEvaluationMetrics):
        raise IcpInputError("ICP 품질 baseline 지표 형식이 올바르지 않습니다.")
    if best_candidate_metrics is not None and not isinstance(best_candidate_metrics, IcpEvaluationMetrics):
        raise IcpInputError("ICP 품질 candidate 지표 형식이 올바르지 않습니다.")
    if (baseline_metrics is None) != (best_candidate_metrics is None):
        raise IcpInputError("ICP 품질 baseline과 candidate 지표는 함께 필요합니다.")
    if density_sampling_metrics is not None and not isinstance(density_sampling_metrics, IcpDensitySamplingMetrics):
        raise IcpInputError("ICP 품질 밀도 표본 지표 형식이 올바르지 않습니다.")
    relative_improvement = 0.0
    if baseline_metrics is not None and best_candidate_metrics is not None:
        if baseline_metrics.evaluation_fingerprint != best_candidate_metrics.evaluation_fingerprint:
            raise IcpInputError("ICP baseline과 candidate 평가 표본 지문이 다릅니다.")
        baseline_objective = (
            baseline_metrics.point_to_plane_rmse
            if baseline_metrics.point_to_plane_rmse > 0.0
            else baseline_metrics.point_to_point_rmse
        )
        candidate_objective = (
            best_candidate_metrics.point_to_plane_rmse
            if best_candidate_metrics.point_to_plane_rmse > 0.0
            else best_candidate_metrics.point_to_point_rmse
        )
        relative_improvement = (baseline_objective - candidate_objective) / max(
            baseline_objective,
            np.finfo(np.float64).eps,
        )
    reject_reasons: list[str] = []
    review_reasons: list[str] = []
    if residuals.size < 30:
        review_reasons.append("최종 대응점이 30개 미만이어서 결과 비교가 필요합니다.")
    if fitness < 0.05:
        review_reasons.append("최종 Fitness가 5% 미만이어서 결과 비교가 필요합니다.")
    if normalized_scale_mode is IcpScaleMode.LOCKED and not np.isclose(
        final_scale,
        marker_scale,
        rtol=0.0,
        atol=1.0e-8 * max(1.0, marker_scale),
    ):
        reject_reasons.append("Scale Locked 결과에서 marker 스케일이 변경되었습니다.")
    if not 0.95 - 1.0e-9 <= scale_ratio <= 1.05 + 1.0e-9:
        reject_reasons.append("최종 스케일이 marker 기준 ±5% 범위를 벗어났습니다.")
    if fitness < 0.20:
        review_reasons.append("Fitness가 20% 미만입니다.")
    if reciprocal < 0.30:
        review_reasons.append("reciprocal correspondence 비율이 30% 미만입니다.")
    if rmse / voxel > 3.0:
        review_reasons.append("fine voxel 기준 정규화 RMSE가 큽니다.")
    if rotation > 15.0:
        review_reasons.append("마커 결과 이후 추가 회전이 15도를 초과합니다.")
    if translation > diagonal * 0.10:
        review_reasons.append("마커 결과 이후 추가 이동이 overlap 대각선의 10%를 초과합니다.")
    drift_limit = max(marker_rms * 3.0, voxel * 2.0)
    if drift > drift_limit:
        review_reasons.append("최종 marker drift가 허용 기준을 초과합니다.")
    if normalized_scale_boundary_reached or np.isclose(abs(scale_ratio - 1.0), 0.05, rtol=0.0, atol=1.0e-8):
        review_reasons.append("스케일 보정이 ±5% 안전 경계에 도달했습니다.")
    if abs(scale_ratio - 1.0) > 0.02:
        review_reasons.append("스케일 보정량이 ±2%를 초과합니다.")
    if normalized_stop_reason is IcpSolverStopReason.OBJECTIVE_INCREASE:
        review_reasons.append("목적함수 증가를 감지해 검증된 최선 후보로 복원했습니다.")
    elif normalized_stop_reason is IcpSolverStopReason.CORRESPONDENCE_OSCILLATION:
        review_reasons.append("대응점 집합 진동을 감지해 검증된 최선 후보로 복원했습니다.")
    elif normalized_stop_reason is IcpSolverStopReason.NORMAL_COVERAGE_INSUFFICIENT:
        review_reasons.append("다음 레벨의 유효 노멀이 부족해 이전 최선 후보를 유지했습니다.")
    elif normalized_stop_reason is IcpSolverStopReason.LEVEL_INLIER_DROP:
        review_reasons.append("레벨 전환 후 대응점이 급감해 현재 후보와 RMS를 사용자 검토용으로 보존했습니다.")
    elif normalized_stop_reason is IcpSolverStopReason.ITERATION_LIMIT:
        review_reasons.append("반복 한도에서 최선 후보를 반환합니다.")
    if relative_improvement <= 0.0 and baseline_metrics is not None:
        review_reasons.append("마커 기준 대비 유의미한 ICP 개선이 확인되지 않았습니다.")
    if len(per_reference) > 1 and max(item.fraction for item in per_reference) > 0.95:
        review_reasons.append("대응점이 한 기준 A 모델에 과도하게 집중됩니다.")
    status = (
        IcpQualityStatus.REJECT
        if reject_reasons
        else IcpQualityStatus.REVIEW
        if review_reasons
        else IcpQualityStatus.PASS
    )
    return IcpQualityMetrics(
        status=status,
        fitness=fitness,
        rmse=rmse,
        normalized_rmse_voxel=rmse / voxel,
        normalized_rmse_diagonal=rmse / diagonal,
        median_residual=median,
        p90_residual=p90,
        overlap_ratio=overlap,
        reciprocal_ratio=reciprocal,
        inlier_count=int(residuals.size),
        query_count=normalized_query_count,
        total_iterations=normalized_total_iterations,
        delta_translation=translation,
        delta_rotation_degrees=rotation,
        marker_uniform_scale=marker_scale,
        final_uniform_scale=final_scale,
        scale_ratio=scale_ratio,
        scale_refine_status=normalized_refine_status,
        scale_refine_reason=str(scale_refine_reason).strip(),
        marker_drift_rms=drift,
        elapsed_seconds=elapsed,
        per_reference_model=per_reference,
        review_reasons=tuple(review_reasons),
        rejection_reasons=tuple(reject_reasons),
        baseline_metrics=baseline_metrics,
        best_candidate_metrics=best_candidate_metrics,
        relative_improvement=relative_improvement,
        best_level_name="",
        best_iteration=0,
        solver_stop_reason=normalized_stop_reason,
        point_to_plane_rmse=plane_rmse,
        normal_valid_ratio=normal_valid,
        normal_angle_pass_ratio=normal_angle,
        density_sampling_metrics=density_sampling_metrics,
        user_review_required=bool(review_reasons),
    )


__all__ = (
    "delta_from_initial",
    "evaluate_icp_quality",
    "marker_drift_rms",
    "per_reference_metrics",
    "reciprocal_match_ratio",
    "rotation_angle_degrees",
    "transform_points",
)
