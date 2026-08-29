"""small_gicp 1.0.1 GICP의 외부 CPython 수치 경계를 제공합니다.

입력 준비·감사·결과 계약은 기존 전체점 외부 ICP 경계를 재사용하지만,
정렬과 최근접점 평가는 Open3D 없이 small_gicp와 NumPy만 사용합니다.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version as package_version
import os
from time import monotonic
from typing import Any, Mapping

import numpy as np

from .icp_external_contract import (
    FULL_INPUT_POLICY,
    Open3dIcpCancelled,
    Open3dIcpEnvironmentError,
    Open3dIcpError,
    Open3dIcpInputError,
    Open3dIcpNumericalError,
    Open3dDistanceStage,
    Open3dPointCloud,
    Open3dSolverInput,
    Open3dSolverResult,
    SMALL_GICP_ALGORITHM_VERSION,
    SMALL_GICP_BACKEND_ID,
    _array_fingerprint,
    _canonical_fingerprint,
    _derive_capture_distance,
    _derive_stages_and_normal_policy,
    _diagonal,
    _full_input_audit,
    _inverse_rigid,
    _is_stable_delta,
    _local_delta_to_world,
    _prepare_full_overlap_clouds,
    _raise_if_cancelled,
    _rotation_angle_degrees,
    _strict_finite_float,
    _strict_positive_int,
    _transform_positions,
    _uniform_scale_and_rotation,
    _validate_rigid_matrix,
    analyze_external_icp_delta,
    decompose_sim3_matrix,
)
from .icp_scale_refine import (
    BoundedScaleRefineError,
    solve_bounded_uniform_scale_matrix,
)
from .icp_confidence import weighted_residual_metrics


SMALL_GICP_VERSION = "1.0.1"


def _lazy_small_gicp() -> Any:
    """외부 worker에서만 고정 버전 small_gicp API를 지연 import합니다."""

    try:
        import small_gicp
    except ImportError as error:
        raise Open3dIcpEnvironmentError("small_gicp 1.0.1을 import하지 못했습니다.") from error
    try:
        installed_version = package_version("small-gicp")
    except PackageNotFoundError as error:
        raise Open3dIcpEnvironmentError("small_gicp 배포 버전을 확인하지 못했습니다.") from error
    if installed_version != SMALL_GICP_VERSION:
        raise Open3dIcpEnvironmentError(
            f"small_gicp 버전이 {SMALL_GICP_VERSION}이 아닙니다: {installed_version}"
        )
    required = ("align", "PointCloud", "KdTree", "estimate_normals_covariances")
    if any(not hasattr(small_gicp, name) for name in required):
        raise Open3dIcpEnvironmentError("small_gicp GICP 필수 API가 완전하지 않습니다.")
    return small_gicp


def _worker_thread_count() -> int:
    """한 작업이 시스템을 독점하지 않도록 small_gicp 스레드 수를 제한합니다."""

    return max(1, min(4, int(os.cpu_count() or 1)))


def _prepared_small_gicp_cloud(
    small_gicp: Any,
    positions: np.ndarray,
    *,
    normal_neighbors: int,
    num_threads: int,
) -> tuple[Any, Any, np.ndarray]:
    """전체점 PointCloud·KdTree·정규화 normal을 한 번만 구성합니다."""

    cloud = small_gicp.PointCloud(np.ascontiguousarray(positions, dtype=np.float64))
    tree = small_gicp.KdTree(cloud, num_threads=num_threads)
    neighbor_count = min(max(5, int(normal_neighbors)), int(positions.shape[0]))
    small_gicp.estimate_normals_covariances(
        cloud,
        tree,
        num_neighbors=neighbor_count,
        num_threads=num_threads,
    )
    normals_h = np.asarray(cloud.normals(), dtype=np.float64)
    covariances = np.asarray(cloud.covs(), dtype=np.float64)
    if normals_h.shape != (positions.shape[0], 4):
        raise Open3dIcpNumericalError("small_gicp normal 배열 형상이 올바르지 않습니다.")
    if covariances.shape != (positions.shape[0], 4, 4):
        raise Open3dIcpNumericalError("small_gicp covariance 배열 형상이 올바르지 않습니다.")
    normals = np.ascontiguousarray(normals_h[:, :3], dtype=np.float64)
    lengths = np.linalg.norm(normals, axis=1)
    valid = np.isfinite(normals).all(axis=1) & (lengths > np.finfo(np.float64).eps)
    if not np.all(valid):
        raise Open3dIcpNumericalError("small_gicp이 유효하지 않은 normal을 만들었습니다.")
    normals /= lengths[:, np.newaxis]
    return cloud, tree, normals


def _nearest_pairs(
    tree: Any,
    queries: np.ndarray,
    maximum_distance: float,
    *,
    num_threads: int,
) -> np.ndarray:
    """small_gicp KdTree 결과를 거리 제한된 source·target index 쌍으로 정규화합니다."""

    indices_raw, squared_raw = tree.batch_nearest_neighbor_search(
        np.ascontiguousarray(queries, dtype=np.float64),
        num_threads,
    )
    indices = np.asarray(indices_raw, dtype=np.int64)
    squared = np.asarray(squared_raw, dtype=np.float64)
    if indices.shape != (queries.shape[0],) or squared.shape != indices.shape:
        raise Open3dIcpNumericalError("small_gicp 최근접점 결과 형상이 올바르지 않습니다.")
    valid = (
        (indices >= 0)
        & np.isfinite(squared)
        & (squared <= float(maximum_distance) ** 2)
    )
    source_indices = np.flatnonzero(valid).astype(np.int64, copy=False)
    if source_indices.size == 0:
        return np.empty((0, 2), dtype=np.int64)
    return np.ascontiguousarray(
        np.column_stack((source_indices, indices[valid])),
        dtype=np.int64,
    )


def _correspondence_signature(pairs: np.ndarray) -> str:
    """source 순으로 정렬된 small_gicp 대응 index 쌍의 지문을 반환합니다."""

    return _array_fingerprint(np.ascontiguousarray(pairs, dtype=np.int64))


def _scale_mode(solver_input: Open3dSolverInput) -> str:
    """외부 작업 설정의 scale mode를 비교 가능한 문자열로 반환합니다."""

    return str(solver_input.parameters.get("scale_mode", "")).strip().upper()


def _validate_similarity_delta(
    matrix: np.ndarray,
    solver_input: Open3dSolverInput,
    field_name: str,
) -> np.ndarray:
    """Small GICP delta를 locked 또는 제한형 Sim(3) 계약에 맞게 검증합니다."""

    try:
        normalized = np.ascontiguousarray(matrix, dtype=np.float64)
    except (OverflowError, TypeError, ValueError) as error:
        raise Open3dIcpNumericalError(f"{field_name} 행렬을 읽지 못했습니다.") from error
    if (
        normalized.shape != (4, 4)
        or not np.isfinite(normalized).all()
        or not np.allclose(
            normalized[3],
            np.asarray((0.0, 0.0, 0.0, 1.0), dtype=np.float64),
            rtol=0.0,
            atol=1.0e-10,
        )
    ):
        raise Open3dIcpNumericalError(f"{field_name} 행렬이 유한한 affine 4×4가 아닙니다.")
    scale, _rotation = _uniform_scale_and_rotation(normalized, field_name)
    mode = _scale_mode(solver_input)
    if mode == "LOCKED":
        if not np.isclose(scale, 1.0, rtol=0.0, atol=1.0e-8):
            raise Open3dIcpNumericalError(f"{field_name}에 허용하지 않는 scale 변화가 있습니다.")
        return normalized
    maximum_change = _strict_finite_float(
        solver_input.parameters.get("scale_refine_max_relative_change", 0.05),
        "small_gicp 전체 스케일 보정 한도",
        minimum=0.0,
    )
    if mode != "REFINE" or not 1.0 - maximum_change - 1.0e-9 <= scale <= 1.0 + maximum_change + 1.0e-9:
        raise Open3dIcpNumericalError(f"{field_name}의 scale이 제한형 보정 범위를 벗어났습니다.")
    return normalized


def _local_similarity_delta_to_world(
    delta_local: np.ndarray,
    local_origin: np.ndarray,
    solver_input: Open3dSolverInput,
) -> np.ndarray:
    """local-origin Small GICP Sim(3) delta를 Project World delta로 변환합니다."""

    delta = _validate_similarity_delta(delta_local, solver_input, "small_gicp local delta")
    decomposition = decompose_sim3_matrix(delta, field_name="small_gicp local delta")
    world = np.array(delta, dtype=np.float64, order="C", copy=True)
    world[:3, 3] = (
        decomposition.translation_float64
        + local_origin
        - decomposition.uniform_scale * (decomposition.rotation_float64 @ local_origin)
    )
    return _validate_similarity_delta(world, solver_input, "small_gicp Project World delta")


def _reciprocal_pairs(
    small_gicp: Any,
    source_positions: np.ndarray,
    target_positions: np.ndarray,
    target_tree: Any,
    maximum_distance: float,
    *,
    num_threads: int,
) -> np.ndarray:
    """양방향 최근접 관계가 일치하는 source·target 대응점만 반환합니다."""

    forward = _nearest_pairs(
        target_tree,
        source_positions,
        maximum_distance,
        num_threads=num_threads,
    )
    if forward.shape[0] == 0:
        return forward
    source_cloud = small_gicp.PointCloud(np.ascontiguousarray(source_positions, dtype=np.float64))
    source_tree = small_gicp.KdTree(source_cloud, num_threads=num_threads)
    reverse = _nearest_pairs(
        source_tree,
        target_positions,
        maximum_distance,
        num_threads=num_threads,
    )
    target_count = int(target_positions.shape[0])
    forward_codes = forward[:, 0] * target_count + forward[:, 1]
    reverse_codes = reverse[:, 1] * target_count + reverse[:, 0]
    return np.ascontiguousarray(
        forward[np.isin(forward_codes, reverse_codes, assume_unique=False)],
        dtype=np.int64,
    )


def _scale_refine_skip_reason(
    solver_input: Open3dSolverInput,
    fixed_metrics: Mapping[str, Any],
    full_input_audit: Mapping[str, Any],
) -> str | None:
    """현재 정렬이 제한형 스케일 보정의 입력 품질 조건을 만족하는지 확인합니다."""

    parameters = solver_input.parameters
    minimum_inliers = _strict_positive_int(
        parameters.get("scale_refine_min_inliers", 100),
        "small_gicp 스케일 보정 최소 inlier",
    )
    if int(fixed_metrics["inlier_count"]) < minimum_inliers:
        return "현재 정렬의 inlier 수가 스케일 보정 최소 조건보다 적습니다."
    minimum_fitness = _strict_finite_float(
        parameters.get("scale_refine_min_fitness", 0.30),
        "small_gicp 스케일 보정 최소 fitness",
        minimum=0.0,
    )
    if float(fixed_metrics["fitness"]) < minimum_fitness:
        return "현재 정렬의 Fitness가 스케일 보정 최소 조건보다 낮습니다."
    minimum_reciprocal = _strict_finite_float(
        parameters.get("scale_refine_min_reciprocal", 0.40),
        "small_gicp 스케일 보정 최소 reciprocal",
        minimum=0.0,
    )
    reciprocal_ratio = fixed_metrics.get("reciprocal_ratio")
    if reciprocal_ratio is None or float(reciprocal_ratio) < minimum_reciprocal:
        return "현재 정렬의 reciprocal 대응 비율이 스케일 보정 최소 조건보다 낮습니다."
    source_coverage = float(full_input_audit["source_crop_count"]) / max(
        int(full_input_audit["source_original_count"]),
        1,
    )
    target_coverage = float(full_input_audit["target_crop_count"]) / max(
        int(full_input_audit["target_original_count"]),
        1,
    )
    minimum_coverage = _strict_finite_float(
        parameters.get("scale_refine_min_overlap_coverage", 0.30),
        "small_gicp 스케일 보정 최소 overlap coverage",
        minimum=0.0,
    )
    if min(source_coverage, target_coverage) < minimum_coverage:
        return "전체점 overlap coverage가 스케일 보정 최소 조건보다 낮습니다."
    return None


def _evaluate_on_full_crop(
    small_gicp: Any,
    source_positions: np.ndarray,
    target_positions: np.ndarray,
    target_tree: Any,
    target_normals: np.ndarray,
    source_confidence_weights: np.ndarray,
    target_confidence_weights: np.ndarray,
    source_isolation_candidates: np.ndarray,
    target_isolation_candidates: np.ndarray,
    transform: np.ndarray,
    maximum_distance: float,
    *,
    minimum_correspondences: int,
    require_minimum: bool,
    evaluation_fingerprint: str,
    num_threads: int,
) -> dict[str, Any]:
    """같은 전체 crop·거리·원본 분포 신뢰도로 후보 품질을 계산합니다."""

    transformed = _transform_positions(source_positions, transform)
    pairs = _nearest_pairs(
        target_tree,
        transformed,
        maximum_distance,
        num_threads=num_threads,
    )
    correspondence_count = int(pairs.shape[0])
    if require_minimum and correspondence_count < minimum_correspondences:
        raise Open3dIcpNumericalError("small_gicp 전체점 평가 대응점 수가 최소 기준보다 적습니다.")
    if correspondence_count:
        residual_vectors = transformed[pairs[:, 0]] - target_positions[pairs[:, 1]]
        distances = np.linalg.norm(residual_vectors, axis=1)
        point_rmse: float | None = float(np.sqrt(np.mean(np.square(distances))))
        distribution_metrics = weighted_residual_metrics(
            distances,
            pairs,
            source_confidence_weights,
            target_confidence_weights,
            source_isolation_candidates,
            target_isolation_candidates,
        )
        transformed_cloud = small_gicp.PointCloud(transformed)
        transformed_tree = small_gicp.KdTree(transformed_cloud, num_threads=num_threads)
        reverse_pairs_raw = _nearest_pairs(
            transformed_tree,
            target_positions,
            maximum_distance,
            num_threads=num_threads,
        )
        target_count = int(target_positions.shape[0])
        forward_codes = pairs[:, 0] * target_count + pairs[:, 1]
        reverse_codes = reverse_pairs_raw[:, 1] * target_count + reverse_pairs_raw[:, 0]
        reciprocal_count = int(
            np.intersect1d(forward_codes, reverse_codes, assume_unique=False).size
        )
        reciprocal_ratio: float | None = reciprocal_count / correspondence_count
        normals = target_normals[pairs[:, 1]]
        plane_residuals = np.einsum("ij,ij->i", residual_vectors, normals)
        point_to_plane_rmse: float | None = float(
            np.sqrt(np.mean(np.square(plane_residuals)))
        )
        normal_valid_ratio: float | None = 1.0
    else:
        point_rmse = None
        distribution_metrics = weighted_residual_metrics(
            np.empty(0, dtype=np.float64),
            pairs,
            source_confidence_weights,
            target_confidence_weights,
            source_isolation_candidates,
            target_isolation_candidates,
        )
        reciprocal_count = 0
        reciprocal_ratio = None
        point_to_plane_rmse = None
        normal_valid_ratio = None
    return {
        **distribution_metrics,
        "point_to_point_rmse": point_rmse,
        "point_to_plane_rmse": point_to_plane_rmse,
        "fitness": correspondence_count / max(int(source_positions.shape[0]), 1),
        "small_gicp_inlier_rmse": point_rmse,
        "inlier_count": correspondence_count,
        "query_count": int(source_positions.shape[0]),
        "reciprocal_count": reciprocal_count,
        "reciprocal_ratio": reciprocal_ratio,
        "normal_valid_ratio": normal_valid_ratio,
        "maximum_correspondence_distance": float(maximum_distance),
        "correspondence_signature": _correspondence_signature(pairs),
        "evaluation_fingerprint": evaluation_fingerprint,
        "valid": bool(correspondence_count >= minimum_correspondences),
    }


def _run_bounded_scale_refinement(
    solver_input: Open3dSolverInput,
    small_gicp: Any,
    source_positions: np.ndarray,
    target_positions: np.ndarray,
    target_tree: Any,
    target_normals: np.ndarray,
    source_confidence_weights: np.ndarray,
    target_confidence_weights: np.ndarray,
    source_isolation_candidates: np.ndarray,
    target_isolation_candidates: np.ndarray,
    fixed_delta: np.ndarray,
    fixed_metrics: Mapping[str, Any],
    full_input_audit: Mapping[str, Any],
    maximum_distance: float,
    *,
    minimum_correspondences: int,
    evaluation_fingerprint: str,
    local_origin: np.ndarray,
    num_threads: int,
    progress_sink: Any,
    cancel_probe: Any,
) -> tuple[np.ndarray, Mapping[str, Any], str, str, int]:
    """현재 정렬에서 회전 없는 제한형 scale 후보를 만들고 사용자 검토용으로 유지합니다."""

    if _scale_mode(solver_input) != "REFINE":
        return fixed_delta, fixed_metrics, "NOT_REQUESTED", "", 0
    skip_reason = _scale_refine_skip_reason(solver_input, fixed_metrics, full_input_audit)
    if skip_reason is not None:
        return fixed_delta, fixed_metrics, "SKIPPED", skip_reason, 0
    parameters = solver_input.parameters
    maximum_change = _strict_finite_float(
        parameters.get("scale_refine_max_relative_change", 0.05),
        "small_gicp 전체 스케일 보정 한도",
        minimum=0.0,
    )
    maximum_step = _strict_finite_float(
        parameters.get("scale_refine_max_step", 0.005),
        "small_gicp 1회 스케일 보정 한도",
        minimum=np.finfo(np.float64).eps,
    )
    maximum_iterations = _strict_positive_int(
        parameters.get("scale_refine_max_iterations", 5),
        "small_gicp 스케일 보정 반복",
    )
    minimum_scale_pairs = _strict_positive_int(
        parameters.get("scale_refine_min_inliers", 100),
        "small_gicp 스케일 보정 최소 inlier",
    )
    current_delta = np.array(fixed_delta, dtype=np.float64, order="C", copy=True)
    completed_iterations = 0
    for iteration in range(1, maximum_iterations + 1):
        _raise_if_cancelled(cancel_probe)
        transformed = _transform_positions(source_positions, current_delta)
        pairs = _reciprocal_pairs(
            small_gicp,
            transformed,
            target_positions,
            target_tree,
            maximum_distance,
            num_threads=num_threads,
        )
        if pairs.shape[0] < minimum_scale_pairs:
            reason = "reciprocal 대응점 수가 스케일 보정 최소 조건보다 적습니다."
            if completed_iterations == 0:
                return fixed_delta, fixed_metrics, "SKIPPED", reason, 0
            break
        current_scale, _rotation = _uniform_scale_and_rotation(
            current_delta,
            "small_gicp scale refinement delta",
        )
        lower_step = max(1.0 - maximum_step, (1.0 - maximum_change) / current_scale)
        upper_step = min(1.0 + maximum_step, (1.0 + maximum_change) / current_scale)
        if lower_step > upper_step + 1.0e-12:
            break
        try:
            step = solve_bounded_uniform_scale_matrix(
                transformed[pairs[:, 0]],
                target_positions[pairs[:, 1]],
                (lower_step, upper_step),
            )
        except BoundedScaleRefineError as error:
            if completed_iterations == 0:
                return fixed_delta, fixed_metrics, "SKIPPED", str(error), 0
            break
        current_delta = _validate_similarity_delta(
            step @ current_delta,
            solver_input,
            "small_gicp scale refinement 누적 delta",
        )
        completed_iterations = iteration
        _emit_progress(
            progress_sink,
            solver_input,
            stage="SMALL_GICP_SCALE_REFINEMENT",
            progress=min(0.96, 0.90 + 0.06 * iteration / maximum_iterations),
            message=f"Refine 균일 스케일 보정 반복 {iteration}/{maximum_iterations}",
            level_name="scale",
            iteration=iteration,
            max_iterations=maximum_iterations,
            maximum_correspondence_distance=maximum_distance,
            correspondence_count=int(pairs.shape[0]),
            source_point_count=int(source_positions.shape[0]),
            target_point_count=int(target_positions.shape[0]),
            stable_delta=_local_similarity_delta_to_world(
                current_delta,
                local_origin,
                solver_input,
            ),
        )
    if completed_iterations == 0:
        return fixed_delta, fixed_metrics, "SKIPPED", "스케일 보정 범위가 안전 경계에 도달했습니다.", 0
    refined_metrics = _evaluate_on_full_crop(
        small_gicp,
        source_positions,
        target_positions,
        target_tree,
        target_normals,
        source_confidence_weights,
        target_confidence_weights,
        source_isolation_candidates,
        target_isolation_candidates,
        current_delta,
        maximum_distance,
        minimum_correspondences=minimum_correspondences,
        require_minimum=True,
        evaluation_fingerprint=evaluation_fingerprint,
        num_threads=num_threads,
    )
    fixed_rmse = float(fixed_metrics["point_to_point_rmse"])
    refined_rmse = float(refined_metrics["point_to_point_rmse"])
    rmse_improvement = (fixed_rmse - refined_rmse) / max(
        fixed_rmse,
        np.finfo(np.float64).eps,
    )
    minimum_improvement = _strict_finite_float(
        parameters.get("scale_refine_min_rmse_improvement", 0.02),
        "small_gicp 스케일 보정 최소 RMSE 개선율",
        minimum=0.0,
    )
    if rmse_improvement < minimum_improvement:
        return (
            current_delta,
            refined_metrics,
            "ACCEPTED",
            "RMS 개선 조건을 충족하지 못했지만 비교를 위해 스케일 보정 후보를 유지했습니다.",
            completed_iterations,
        )
    return (
        current_delta,
        refined_metrics,
        "ACCEPTED",
        f"현재 정렬 대비 RMS를 {rmse_improvement * 100.0:.2f}% 개선했습니다.",
        completed_iterations,
    )


def _run_final_gicp_polish(
    solver_input: Open3dSolverInput,
    small_gicp: Any,
    source_positions: np.ndarray,
    target_positions: np.ndarray,
    target_cloud: Any,
    target_tree: Any,
    target_normals: np.ndarray,
    source_confidence_weights: np.ndarray,
    target_confidence_weights: np.ndarray,
    source_isolation_candidates: np.ndarray,
    target_isolation_candidates: np.ndarray,
    scaled_delta: np.ndarray,
    scaled_metrics: Mapping[str, Any],
    maximum_distance: float,
    *,
    minimum_correspondences: int,
    evaluation_fingerprint: str,
    normal_neighbors: int,
    num_threads: int,
    max_iterations: int,
    progress_sink: Any,
    cancel_probe: Any,
    local_origin: np.ndarray,
) -> tuple[np.ndarray, Mapping[str, Any], Mapping[str, Any]]:
    """첫 GICP 결과를 초기값으로 두 번째 fine Small GICP 강체 정렬을 실행합니다."""

    _raise_if_cancelled(cancel_probe)
    scaled_source = _transform_positions(source_positions, scaled_delta)
    scaled_source_cloud, _scaled_tree, _scaled_normals = _prepared_small_gicp_cloud(
        small_gicp,
        scaled_source,
        normal_neighbors=normal_neighbors,
        num_threads=num_threads,
    )
    registration_result = small_gicp.align(
        target_cloud,
        scaled_source_cloud,
        target_tree,
        init_T_target_source=np.identity(4, dtype=np.float64),
        registration_type="GICP",
        max_correspondence_distance=float(maximum_distance),
        num_threads=num_threads,
        max_iterations=max_iterations,
    )
    _raise_if_cancelled(cancel_probe)
    rigid_polish = _validate_rigid_matrix(
        np.asarray(registration_result.T_target_source, dtype=np.float64),
        "small_gicp second rigid pass",
    )
    polished_delta = _validate_similarity_delta(
        rigid_polish @ scaled_delta,
        solver_input,
        "small_gicp second pass delta",
    )
    polished_metrics = _evaluate_on_full_crop(
        small_gicp,
        source_positions,
        target_positions,
        target_tree,
        target_normals,
        source_confidence_weights,
        target_confidence_weights,
        source_isolation_candidates,
        target_isolation_candidates,
        polished_delta,
        maximum_distance,
        minimum_correspondences=minimum_correspondences,
        require_minimum=True,
        evaluation_fingerprint=evaluation_fingerprint,
        num_threads=num_threads,
    )
    scaled_rmse = float(scaled_metrics["point_to_point_rmse"])
    polished_rmse = float(polished_metrics["point_to_point_rmse"])
    scaled_reciprocal = float(scaled_metrics.get("reciprocal_ratio") or 0.0)
    polished_reciprocal = float(polished_metrics.get("reciprocal_ratio") or 0.0)
    accepted = (
        polished_rmse <= scaled_rmse + 1.0e-12
        and float(polished_metrics["fitness"]) + 0.02 >= float(scaled_metrics["fitness"])
        and polished_reciprocal + 1.0e-12 >= scaled_reciprocal
    )
    chosen_delta = polished_delta if accepted else scaled_delta
    chosen_metrics = polished_metrics if accepted else scaled_metrics
    _emit_progress(
        progress_sink,
        solver_input,
        stage="SMALL_GICP_SECOND_GICP_FINE_DISTANCE",
        progress=0.98,
        message="두 번째 Small GICP 강체 정렬을 완료했습니다.",
        level_name="second_fine",
        iteration=int(registration_result.iterations),
        max_iterations=max_iterations,
        level_complete=True,
        maximum_correspondence_distance=maximum_distance,
        correspondence_count=int(registration_result.num_inliers),
        source_point_count=int(source_positions.shape[0]),
        target_point_count=int(target_positions.shape[0]),
        stable_delta=_local_similarity_delta_to_world(
            chosen_delta,
            local_origin,
            solver_input,
        ),
    )
    level_result = {
        "level_name": "second_fine",
        "maximum_correspondence_distance": float(maximum_distance),
        "max_iterations": int(max_iterations),
        "iterations": int(registration_result.iterations),
        "native_iterations": int(registration_result.iterations),
        "iteration_batch_size": int(max_iterations),
        "batch_count": 1,
        "converged": bool(registration_result.converged),
        "stop_reason": "ACCEPTED" if accepted else "REJECTED_NO_IMPROVEMENT",
        "small_gicp_error": float(registration_result.error),
        "small_gicp_inlier_count": int(registration_result.num_inliers),
        "source_point_count": int(source_positions.shape[0]),
        "target_point_count": int(target_positions.shape[0]),
    }
    return chosen_delta, chosen_metrics, level_result


def _quality_mapping(
    baseline: Mapping[str, Any],
    candidate: Mapping[str, Any],
    *,
    scale_refine_status: str = "NOT_REQUESTED",
    scale_refine_reason: str = "",
) -> dict[str, Any]:
    """초기 정렬 대비 후보 품질과 제한형 scale 보정 진단을 함께 반환합니다."""

    reasons: list[str] = []
    baseline_rmse = baseline.get("point_to_point_rmse")
    candidate_rmse = candidate.get("point_to_point_rmse")
    if not bool(candidate.get("valid")) or candidate_rmse is None:
        raise Open3dIcpNumericalError("small_gicp 최종 후보의 전체점 평가가 성립하지 않습니다.")
    if not bool(baseline.get("valid")) or baseline_rmse is None:
        reasons.append("초기 정렬 baseline의 전체점 대응점이 최소 기준보다 적습니다.")
    elif float(candidate_rmse) >= float(baseline_rmse):
        reasons.append("small_gicp 후보가 초기 정렬 baseline RMSE를 개선하지 못했습니다.")
    status = "PASS" if not reasons else "REVIEW"
    return {
        "status": status,
        "review_reasons": reasons,
        "user_review_required": bool(reasons),
        "scale_refine_status": str(scale_refine_status),
        "scale_refine_reason": str(scale_refine_reason),
    }


def _emit_progress(
    sink: Any,
    job: Open3dSolverInput,
    *,
    stage: str,
    progress: float,
    message: str,
    level_name: str = "",
    iteration: int = 0,
    max_iterations: int = 0,
    level_complete: bool = False,
    maximum_correspondence_distance: float | None = None,
    correspondence_count: int | None = None,
    point_to_point_rmse: float | None = None,
    normal_valid_count: int | None = None,
    normal_neighbors: int | None = None,
    source_point_count: int | None = None,
    target_point_count: int | None = None,
    stable_delta: np.ndarray | None = None,
) -> None:
    """small_gicp 진행률에 backend·버전·전체점 식별자를 함께 기록합니다."""

    if sink is None:
        return
    payload: dict[str, Any] = {
        "job_id": job.job_id,
        "input_fingerprint": job.input_fingerprint,
        "algorithm_version": job.algorithm_version,
        "backend_id": job.backend_id,
        "backend_algorithm_version": job.backend_algorithm_version,
        "job_protocol_version": job.job_protocol_version,
        "small_gicp_version": SMALL_GICP_VERSION,
        "stage": stage,
        "level_name": level_name,
        "iteration": int(iteration),
        "max_iterations": int(max_iterations),
        "progress": float(min(1.0, max(0.0, progress))),
        "message": message,
        "level_complete": bool(level_complete),
    }
    optional_values = {
        "maximum_correspondence_distance": maximum_correspondence_distance,
        "correspondence_count": correspondence_count,
        "point_to_point_rmse": point_to_point_rmse,
        "normal_valid_count": normal_valid_count,
        "normal_neighbors": normal_neighbors,
        "source_point_count": source_point_count,
        "target_point_count": target_point_count,
        "moving_sample_count": source_point_count,
        "reference_sample_count": target_point_count,
    }
    for name, value in optional_values.items():
        if value is not None:
            payload[name] = value
    if stable_delta is not None:
        delta = _validate_similarity_delta(
            stable_delta,
            job,
            "small_gicp stable delta",
        )
        payload["stable_alignment_4x4"] = (delta @ job.initial_alignment).tolist()
    sink(payload)


def solve_small_gicp(
    solver_input: Open3dSolverInput,
    *,
    progress_sink: Any = None,
    cancel_probe: Any = None,
) -> Open3dSolverResult:
    """Small GICP 강체 2회를 실행하고 Refine 선택 시 그 사이에 균일 스케일을 보정합니다."""

    if solver_input.backend_id != SMALL_GICP_BACKEND_ID:
        raise Open3dIcpInputError("small_gicp solver에는 SMALL_GICP backend 입력이 필요합니다.")
    if solver_input.backend_algorithm_version != SMALL_GICP_ALGORITHM_VERSION:
        raise Open3dIcpInputError("small_gicp backend 알고리즘 버전이 올바르지 않습니다.")
    started_at = monotonic()
    small_gicp = _lazy_small_gicp()
    _raise_if_cancelled(cancel_probe)
    _emit_progress(
        progress_sink,
        solver_input,
        stage="SMALL_GICP_PREPARE",
        progress=0.0,
        message=(
            "Refine 이동·회전·제한형 스케일 보정 정책을 검증했습니다."
            if _scale_mode(solver_input) == "REFINE"
            else "small_gicp 입력과 locked scale 정책을 검증했습니다."
        ),
    )
    capture_distance = _derive_capture_distance(solver_input)
    prepared = _prepare_full_overlap_clouds(solver_input, capture_distance)
    minimum_correspondences = _strict_positive_int(
        solver_input.parameters.get("minimum_correspondences", 30),
        "small_gicp 최소 대응점 수",
    )
    if (
        prepared.source_positions.shape[0] < minimum_correspondences
        or prepared.target_positions.shape[0] < minimum_correspondences
    ):
        raise Open3dIcpInputError("small_gicp 전체 overlap crop 점 수가 최소 대응점보다 적습니다.")
    stages, normal_radius, normal_max_nn, _unused_tukey, batch_size = (
        _derive_stages_and_normal_policy(solver_input, prepared, capture_distance)
    )
    scale_refine_requested = _scale_mode(solver_input) == "REFINE"
    source_minimum, source_maximum = prepared.source_bounds
    target_minimum, target_maximum = prepared.target_bounds
    local_origin = np.ascontiguousarray(
        (np.minimum(source_minimum, target_minimum) + np.maximum(source_maximum, target_maximum))
        * 0.5,
        dtype=np.float64,
    )
    moving_diagonal = _diagonal(source_minimum, source_maximum)
    local_source = np.ascontiguousarray(
        prepared.source_positions - local_origin[np.newaxis, :],
        dtype=np.float64,
    )
    local_target = np.ascontiguousarray(
        prepared.target_positions - local_origin[np.newaxis, :],
        dtype=np.float64,
    )
    audit_stages = (
        (
            *stages,
            Open3dDistanceStage(
                name="scale_refine",
                maximum_correspondence_distance=stages[-1].maximum_correspondence_distance,
                max_iterations=_strict_positive_int(
                    solver_input.parameters.get("scale_refine_max_iterations", 5),
                    "small_gicp 스케일 보정 반복",
                ),
            ),
        )
        if scale_refine_requested
        else stages
    )
    full_input_audit = _full_input_audit(prepared, audit_stages)
    num_threads = _worker_thread_count()
    normal_neighbors = min(max(5, normal_max_nn), local_source.shape[0], local_target.shape[0])
    _emit_progress(
        progress_sink,
        solver_input,
        stage="SMALL_GICP_BUILD_FULL_CLOUD",
        progress=0.08,
        message="downsampling 없이 전체 overlap 점군의 covariance를 계산합니다.",
        normal_neighbors=normal_neighbors,
        source_point_count=int(local_source.shape[0]),
        target_point_count=int(local_target.shape[0]),
    )
    source_cloud, _source_tree, source_normals = _prepared_small_gicp_cloud(
        small_gicp,
        local_source,
        normal_neighbors=normal_neighbors,
        num_threads=num_threads,
    )
    target_cloud, target_tree, target_normals = _prepared_small_gicp_cloud(
        small_gicp,
        local_target,
        normal_neighbors=normal_neighbors,
        num_threads=num_threads,
    )
    normal_audit: dict[str, Any] = {
        "normal_policy": "SMALL_GICP_KNN_COVARIANCE",
        "normal_radius": float(normal_radius),
        "normal_maximum_neighbors": int(normal_neighbors),
        "source_normal_valid_count": int(source_normals.shape[0]),
        "target_normal_valid_count": int(target_normals.shape[0]),
        "target_normal_valid_ratio": 1.0,
    }
    current_delta_local = np.identity(4, dtype=np.float64)
    level_results: list[Mapping[str, Any]] = []
    gicp_stages = stages
    total_iterations = sum(stage.max_iterations for stage in gicp_stages)
    completed_iterations = 0
    max_rotation_degrees = _strict_finite_float(
        solver_input.parameters.get("max_rotation_degrees", 30.0),
        "small_gicp 최대 회전 안전 한도",
        minimum=np.finfo(np.float64).eps,
    )
    translation_limit = moving_diagonal * _strict_finite_float(
        solver_input.parameters.get("max_translation_fraction", 0.25),
        "small_gicp 최대 이동 비율",
        minimum=np.finfo(np.float64).eps,
    )
    for stage in gicp_stages:
        _raise_if_cancelled(cancel_probe)
        stage_iterations = 0
        batch_count = 0
        converged = False
        last_result: Any | None = None
        while stage_iterations < stage.max_iterations:
            _raise_if_cancelled(cancel_probe)
            current_batch = min(batch_size, stage.max_iterations - stage_iterations)
            previous_delta = current_delta_local
            registration_result = small_gicp.align(
                target_cloud,
                source_cloud,
                target_tree,
                init_T_target_source=current_delta_local,
                registration_type="GICP",
                max_correspondence_distance=float(stage.maximum_correspondence_distance),
                num_threads=num_threads,
                max_iterations=current_batch,
            )
            _raise_if_cancelled(cancel_probe)
            current_delta_local = _validate_rigid_matrix(
                np.asarray(registration_result.T_target_source, dtype=np.float64),
                "small_gicp registration delta",
            )
            step = _validate_rigid_matrix(
                current_delta_local @ _inverse_rigid(previous_delta),
                "small_gicp batch step",
            )
            step_translation = float(np.linalg.norm(step[:3, 3]))
            step_rotation = _rotation_angle_degrees(step[:3, :3])
            if step_translation > translation_limit:
                raise Open3dIcpNumericalError("small_gicp 한 batch 이동이 안전 한도를 초과했습니다.")
            if step_rotation > max_rotation_degrees:
                raise Open3dIcpNumericalError("small_gicp 한 batch 회전이 안전 한도를 초과했습니다.")
            stage_iterations += current_batch
            completed_iterations += current_batch
            batch_count += 1
            last_result = registration_result
            _emit_progress(
                progress_sink,
                solver_input,
                stage=f"SMALL_GICP_GICP_{stage.name.upper()}_DISTANCE",
                progress=0.15 + 0.72 * (completed_iterations / max(total_iterations, 1)),
                message=f"small_gicp {stage.name} 거리 단계 batch {batch_count}",
                level_name=stage.name,
                iteration=stage_iterations,
                max_iterations=stage.max_iterations,
                maximum_correspondence_distance=stage.maximum_correspondence_distance,
                correspondence_count=int(registration_result.num_inliers),
                normal_valid_count=int(target_normals.shape[0]),
                normal_neighbors=normal_neighbors,
                source_point_count=int(local_source.shape[0]),
                target_point_count=int(local_target.shape[0]),
                stable_delta=_local_delta_to_world(current_delta_local, local_origin),
            )
            if bool(registration_result.converged) or _is_stable_delta(
                previous_delta,
                current_delta_local,
                stage.maximum_correspondence_distance,
            ):
                converged = True
                break
        if last_result is None:
            raise Open3dIcpNumericalError("small_gicp 거리 단계가 결과를 만들지 못했습니다.")
        level_results.append(
            {
                "level_name": stage.name,
                "maximum_correspondence_distance": float(stage.maximum_correspondence_distance),
                "max_iterations": int(stage.max_iterations),
                "iterations": int(stage_iterations),
                "native_iterations": int(last_result.iterations),
                "iteration_batch_size": int(batch_size),
                "batch_count": int(batch_count),
                "converged": bool(converged),
                "stop_reason": "CONVERGED" if converged else "ITERATION_LIMIT",
                "small_gicp_error": float(last_result.error),
                "small_gicp_inlier_count": int(last_result.num_inliers),
                "source_point_count": int(local_source.shape[0]),
                "target_point_count": int(local_target.shape[0]),
                "source_positions_fingerprint": full_input_audit[
                    "source_crop_positions_fingerprint"
                ],
                "source_point_ids_fingerprint": full_input_audit[
                    "source_crop_point_ids_fingerprint"
                ],
                "target_positions_fingerprint": full_input_audit[
                    "target_crop_positions_fingerprint"
                ],
                "target_point_ids_fingerprint": full_input_audit[
                    "target_crop_point_ids_fingerprint"
                ],
            }
        )
        _emit_progress(
            progress_sink,
            solver_input,
            stage=f"SMALL_GICP_GICP_{stage.name.upper()}_DISTANCE",
            progress=0.15 + 0.72 * (completed_iterations / max(total_iterations, 1)),
            message=f"small_gicp {stage.name} 거리 단계를 완료했습니다.",
            level_name=stage.name,
            iteration=stage_iterations,
            max_iterations=stage.max_iterations,
            level_complete=True,
            maximum_correspondence_distance=stage.maximum_correspondence_distance,
            correspondence_count=int(last_result.num_inliers),
            normal_valid_count=int(target_normals.shape[0]),
            normal_neighbors=normal_neighbors,
            source_point_count=int(local_source.shape[0]),
            target_point_count=int(local_target.shape[0]),
            stable_delta=_local_delta_to_world(current_delta_local, local_origin),
        )
    _raise_if_cancelled(cancel_probe)
    evaluation_distance = stages[-1].maximum_correspondence_distance
    evaluation_fingerprint = _canonical_fingerprint(
        {
            "input_policy": FULL_INPUT_POLICY,
            "source_positions": full_input_audit["source_crop_positions_fingerprint"],
            "source_point_ids": full_input_audit["source_crop_point_ids_fingerprint"],
            "target_positions": full_input_audit["target_crop_positions_fingerprint"],
            "target_point_ids": full_input_audit["target_crop_point_ids_fingerprint"],
            "source_confidence_weights": full_input_audit[
                "source_confidence_weights_fingerprint"
            ],
            "target_confidence_weights": full_input_audit[
                "target_confidence_weights_fingerprint"
            ],
            "maximum_correspondence_distance": evaluation_distance,
        }
    )
    _emit_progress(
        progress_sink,
        solver_input,
        stage="SMALL_GICP_EVALUATE",
        progress=0.90,
        message="초기 정렬과 small_gicp 후보를 같은 전체점 crop에서 평가합니다.",
        maximum_correspondence_distance=evaluation_distance,
        normal_valid_count=int(target_normals.shape[0]),
        normal_neighbors=normal_neighbors,
        source_point_count=int(local_source.shape[0]),
        target_point_count=int(local_target.shape[0]),
    )
    baseline = _evaluate_on_full_crop(
        small_gicp,
        local_source,
        local_target,
        target_tree,
        target_normals,
        prepared.source_confidence_weights,
        prepared.target_confidence_weights,
        prepared.source_isolation_candidates,
        prepared.target_isolation_candidates,
        np.identity(4, dtype=np.float64),
        evaluation_distance,
        minimum_correspondences=minimum_correspondences,
        require_minimum=False,
        evaluation_fingerprint=evaluation_fingerprint,
        num_threads=num_threads,
    )
    candidate = _evaluate_on_full_crop(
        small_gicp,
        local_source,
        local_target,
        target_tree,
        target_normals,
        prepared.source_confidence_weights,
        prepared.target_confidence_weights,
        prepared.source_isolation_candidates,
        prepared.target_isolation_candidates,
        current_delta_local,
        evaluation_distance,
        minimum_correspondences=minimum_correspondences,
        require_minimum=True,
        evaluation_fingerprint=evaluation_fingerprint,
        num_threads=num_threads,
    )
    current_delta_local, candidate, scale_refine_status, scale_refine_reason, scale_iterations = (
        _run_bounded_scale_refinement(
            solver_input,
            small_gicp,
            local_source,
            local_target,
            target_tree,
            target_normals,
            prepared.source_confidence_weights,
            prepared.target_confidence_weights,
            prepared.source_isolation_candidates,
            prepared.target_isolation_candidates,
            current_delta_local,
            candidate,
            full_input_audit,
            evaluation_distance,
            minimum_correspondences=minimum_correspondences,
            evaluation_fingerprint=evaluation_fingerprint,
            local_origin=local_origin,
            num_threads=num_threads,
            progress_sink=progress_sink,
            cancel_probe=cancel_probe,
        )
    )
    if scale_refine_requested:
        level_results.append(
            {
                "level_name": "scale_refine",
                "maximum_correspondence_distance": float(evaluation_distance),
                "max_iterations": int(
                    solver_input.parameters.get("scale_refine_max_iterations", 5)
                ),
                "iterations": int(scale_iterations),
                "native_iterations": int(scale_iterations),
                "iteration_batch_size": 1,
                "batch_count": int(scale_iterations),
                "converged": scale_refine_status == "ACCEPTED",
                "stop_reason": scale_refine_status,
                "small_gicp_error": float(candidate["point_to_point_rmse"]),
                "small_gicp_inlier_count": int(candidate["inlier_count"]),
                "source_point_count": int(local_source.shape[0]),
                "target_point_count": int(local_target.shape[0]),
                "source_positions_fingerprint": full_input_audit[
                    "source_crop_positions_fingerprint"
                ],
                "source_point_ids_fingerprint": full_input_audit[
                    "source_crop_point_ids_fingerprint"
                ],
                "target_positions_fingerprint": full_input_audit[
                    "target_crop_positions_fingerprint"
                ],
                "target_point_ids_fingerprint": full_input_audit[
                    "target_crop_point_ids_fingerprint"
                ],
            }
        )
    current_delta_local, candidate, final_polish_result = _run_final_gicp_polish(
        solver_input,
        small_gicp,
        local_source,
        local_target,
        target_cloud,
        target_tree,
        target_normals,
        prepared.source_confidence_weights,
        prepared.target_confidence_weights,
        prepared.source_isolation_candidates,
        prepared.target_isolation_candidates,
        current_delta_local,
        candidate,
        evaluation_distance,
        minimum_correspondences=minimum_correspondences,
        evaluation_fingerprint=evaluation_fingerprint,
        normal_neighbors=normal_neighbors,
        num_threads=num_threads,
        max_iterations=stages[-1].max_iterations,
        progress_sink=progress_sink,
        cancel_probe=cancel_probe,
        local_origin=local_origin,
    )
    final_polish_result = {
        **dict(final_polish_result),
        "source_positions_fingerprint": full_input_audit[
            "source_crop_positions_fingerprint"
        ],
        "source_point_ids_fingerprint": full_input_audit[
            "source_crop_point_ids_fingerprint"
        ],
        "target_positions_fingerprint": full_input_audit[
            "target_crop_positions_fingerprint"
        ],
        "target_point_ids_fingerprint": full_input_audit[
            "target_crop_point_ids_fingerprint"
        ],
    }
    level_results.append(final_polish_result)
    icp_delta = _local_similarity_delta_to_world(
        current_delta_local,
        local_origin,
        solver_input,
    )
    delta_diagnostics = analyze_external_icp_delta(
        icp_delta,
        local_origin,
        field_name="small_gicp 최종 Project World delta",
    )
    relative_scale = delta_diagnostics.uniform_scale
    delta_translation = delta_diagnostics.translation_distance
    delta_rotation_degrees = delta_diagnostics.rotation_degrees
    if delta_translation > translation_limit:
        raise Open3dIcpNumericalError("small_gicp 최종 이동이 안전 한도를 초과했습니다.")
    if delta_rotation_degrees > max_rotation_degrees:
        raise Open3dIcpNumericalError("small_gicp 최종 회전이 안전 한도를 초과했습니다.")
    final_alignment = np.ascontiguousarray(
        icp_delta @ solver_input.initial_alignment,
        dtype=np.float64,
    )
    final_scale, _final_rotation = _uniform_scale_and_rotation(
        final_alignment,
        "small_gicp final alignment",
    )
    if _scale_mode(solver_input) == "LOCKED":
        if not np.isclose(
            final_scale,
            solver_input.marker_uniform_scale,
            rtol=0.0,
            atol=1.0e-9 * max(1.0, solver_input.marker_uniform_scale),
        ):
            raise Open3dIcpNumericalError("small_gicp final alignment가 초기 scale을 변경했습니다.")
    else:
        maximum_scale_change = _strict_finite_float(
            solver_input.parameters.get("scale_refine_max_relative_change", 0.05),
            "small_gicp 전체 스케일 보정 한도",
            minimum=0.0,
        )
        scale_ratio = final_scale / solver_input.marker_uniform_scale
        if not 1.0 - maximum_scale_change - 1.0e-9 <= scale_ratio <= 1.0 + maximum_scale_change + 1.0e-9:
            raise Open3dIcpNumericalError("small_gicp final alignment scale이 보정 범위를 벗어났습니다.")
    quality = _quality_mapping(
        baseline,
        candidate,
        scale_refine_status=scale_refine_status,
        scale_refine_reason=scale_refine_reason,
    )
    execution_audit = {
        "small_gicp_api": (
            "PREPROCESSED_POINT_CLOUD_GICP_WITH_BOUNDED_SCALE_REFINE"
            if scale_refine_requested
            else "PREPROCESSED_POINT_CLOUD_GICP"
        ),
        "small_gicp_version": SMALL_GICP_VERSION,
        "num_threads": int(num_threads),
        "initial_registration_transform": "IDENTITY",
        "final_composition": "icp_delta @ initial_alignment",
        "pipeline": [
            "CURRENT_OR_MARKER_ALIGNMENT",
            "SMALL_GICP_RIGID_PASS_1",
            *(["BOUNDED_UNIFORM_SCALE_REFINE"] if scale_refine_requested else []),
            "SMALL_GICP_RIGID_PASS_2",
        ],
        "distance_stages": [
            {
                "name": stage.name,
                "maximum_correspondence_distance": float(
                    stage.maximum_correspondence_distance
                ),
                "max_iterations": int(stage.max_iterations),
            }
            for stage in gicp_stages
        ],
        "iteration_batch_size": int(batch_size),
        "evaluation_maximum_correspondence_distance": float(evaluation_distance),
        "scale_refine": {
            "status": scale_refine_status,
            "reason": scale_refine_reason,
            "iterations": int(scale_iterations),
            "relative_scale": float(relative_scale),
            "maximum_relative_change": float(
                solver_input.parameters.get("scale_refine_max_relative_change", 0.05)
            ),
            "maximum_step": float(
                solver_input.parameters.get("scale_refine_max_step", 0.005)
            ),
            "second_gicp_ran": final_polish_result is not None,
        },
        "motion_limits": {
            "max_rotation_degrees": float(max_rotation_degrees),
            "max_translation": float(translation_limit),
        },
    }
    return Open3dSolverResult(
        solver_input=solver_input,
        initial_alignment=np.array(
            solver_input.initial_alignment,
            dtype=np.float64,
            order="C",
            copy=True,
        ),
        final_alignment=final_alignment,
        icp_delta=icp_delta,
        level_results=tuple(level_results),
        baseline_metrics=baseline,
        candidate_metrics=candidate,
        quality=quality,
        full_input_audit=full_input_audit,
        local_origin=local_origin,
        moving_diagonal=moving_diagonal,
        delta_translation=delta_translation,
        delta_rotation_degrees=delta_rotation_degrees,
        normal_audit=normal_audit,
        execution_audit=execution_audit,
        elapsed_seconds=monotonic() - started_at,
    )


__all__ = (
    "Open3dIcpCancelled",
    "Open3dIcpEnvironmentError",
    "Open3dIcpError",
    "Open3dIcpInputError",
    "Open3dIcpNumericalError",
    "Open3dPointCloud",
    "Open3dSolverInput",
    "Open3dSolverResult",
    "SMALL_GICP_VERSION",
    "solve_small_gicp",
)
