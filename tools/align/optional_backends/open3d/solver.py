"""Open3D 0.19 legacy CPU ICP의 독립 수치 경계를 제공합니다.

이 모듈은 Blender·기존 Hybrid ICP 타입에 의존하지 않습니다. 외부 CPython
worker가 marker 적용 Project World P(source)와 확정 Project World A(target)를
직접 받아 rigid delta만 계산하도록 유지합니다.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from math import isfinite
from time import monotonic
from typing import Any, Callable, Mapping, Sequence

import numpy as np

from ...alignment.icp_external_contract import (
    CancelProbe,
    FULL_INPUT_POLICY,
    OPEN3D_GICP_ALGORITHM_VERSION,
    OPEN3D_GICP_BACKEND_ID,
    OPEN3D_JOB_PROTOCOL_VERSION,
    OPEN3D_ROBUST_POINT_TO_PLANE_ALGORITHM_VERSION,
    OPEN3D_ROBUST_POINT_TO_PLANE_BACKEND_ID,
    Open3dDistanceStage,
    Open3dIcpCancelled,
    Open3dIcpEnvironmentError,
    Open3dIcpError,
    Open3dIcpInputError,
    Open3dIcpNumericalError,
    Open3dPointCloud,
    Open3dSolverInput,
    Open3dSolverResult,
    SMALL_GICP_ALGORITHM_VERSION,
    SMALL_GICP_BACKEND_ID,
    _PreparedClouds,
    _array_fingerprint,
    _bounds,
    _canonical_fingerprint,
    _derive_capture_distance,
    _derive_stages_and_normal_policy,
    _diagonal,
    _float64_positions,
    _full_input_audit,
    _inverse_rigid,
    _is_stable_delta,
    _local_delta_to_world,
    _matrix4,
    _nominal_spacing,
    _parameter_levels,
    _parameter_normal_max_nn,
    _prepare_full_overlap_clouds,
    _raise_if_cancelled,
    _rotation_angle_degrees,
    _scoped_cloud_fingerprint,
    _strict_finite_float,
    _strict_positive_int,
    _strict_text,
    _transform_positions,
    _uint64_ids,
    _uniform_scale_and_rotation,
    _validate_rigid_matrix,
    _within_expanded_bounds,
    analyze_external_icp_delta,
)


OPEN3D_LEGACY_VERSION = "0.19.0"
"""검증 backend가 허용하는 Open3D legacy 버전입니다."""



ProgressSink = Callable[[Mapping[str, Any]], None] | None


















def _strict_bool(value: object, field_name: str) -> bool:
    """JSON bool 이외의 묵시적 참·거짓 변환을 막습니다."""

    if type(value) is not bool:
        raise Open3dIcpInputError(f"{field_name}은(는) 정확한 bool이어야 합니다.")
    return value
































def _lazy_open3d() -> Any:
    """외부 worker에서만 Open3D legacy 모듈을 지연 import합니다."""

    try:
        import open3d as o3d
    except ImportError as error:
        raise Open3dIcpEnvironmentError("Open3D 0.19.0을 import하지 못했습니다.") from error
    version = str(getattr(o3d, "__version__", ""))
    if version != OPEN3D_LEGACY_VERSION:
        raise Open3dIcpEnvironmentError(
            f"Open3D 버전이 {OPEN3D_LEGACY_VERSION}이 아닙니다: {version or 'unknown'}"
        )
    registration = getattr(getattr(o3d, "pipelines", None), "registration", None)
    required = (
        "registration_generalized_icp",
        "registration_icp",
        "TransformationEstimationForGeneralizedICP",
        "TransformationEstimationPointToPlane",
        "ICPConvergenceCriteria",
        "TukeyLoss",
        "evaluate_registration",
    )
    if registration is None or any(not hasattr(registration, name) for name in required):
        raise Open3dIcpEnvironmentError("Open3D 0.19 legacy registration API가 완전하지 않습니다.")
    return o3d


























def _make_legacy_point_cloud(o3d: Any, positions: np.ndarray) -> Any:
    """legacy CPU Vector3dVector로만 PointCloud를 구성합니다."""

    cloud = o3d.geometry.PointCloud()
    cloud.points = o3d.utility.Vector3dVector(np.ascontiguousarray(positions, dtype=np.float64))
    return cloud


def _estimate_target_normals(
    o3d: Any,
    target_cloud: Any,
    *,
    radius: float,
    maximum_neighbors: int,
    minimum_valid: int,
) -> tuple[np.ndarray, int, float]:
    """target 전체점에만 Open3D legacy 노멀을 추정하고 유효성을 검사합니다."""

    search = o3d.geometry.KDTreeSearchParamHybrid(
        radius=float(radius),
        max_nn=int(maximum_neighbors),
    )
    target_cloud.estimate_normals(search_param=search, fast_normal_computation=False)
    normals = np.ascontiguousarray(np.asarray(target_cloud.normals), dtype=np.float64)
    if normals.ndim != 2 or normals.shape[1] != 3:
        raise Open3dIcpNumericalError("Open3D target normal 배열 형상이 올바르지 않습니다.")
    lengths = np.linalg.norm(normals, axis=1)
    valid = np.isfinite(normals).all(axis=1) & (lengths > np.finfo(np.float64).eps)
    valid_count = int(np.count_nonzero(valid))
    if valid_count < minimum_valid:
        raise Open3dIcpNumericalError("Open3D target 유효 normal 수가 최소 대응점보다 적습니다.")
    normalized = np.array(normals, dtype=np.float64, order="C", copy=True)
    normalized[valid] /= lengths[valid, np.newaxis]
    ratio = valid_count / max(normals.shape[0], 1)
    return normalized, valid_count, float(ratio)


def _stage_progress_name(backend_id: str, stage_name: str) -> str:
    """backend와 거리 단계를 명시하는 worker 진행 단계 문자열을 만듭니다."""

    backend_name = "GICP" if backend_id == OPEN3D_GICP_BACKEND_ID else "ROBUST_POINT_TO_PLANE"
    return f"OPEN3D_{backend_name}_{stage_name.upper()}_DISTANCE"


def _emit_progress(
    sink: ProgressSink,
    job: Open3dSolverInput,
    *,
    open3d_version: str,
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
    point_to_plane_rmse: float | None = None,
    normal_valid_count: int | None = None,
    normal_radius: float | None = None,
    source_point_count: int | None = None,
    target_point_count: int | None = None,
    stable_delta: np.ndarray | None = None,
) -> None:
    """모든 Open3D progress에 backend·버전·전체점 식별자를 기록합니다."""

    if sink is None:
        return
    payload: dict[str, Any] = {
        "job_id": job.job_id,
        "input_fingerprint": job.input_fingerprint,
        "algorithm_version": job.algorithm_version,
        "backend_id": job.backend_id,
        "backend_algorithm_version": job.backend_algorithm_version,
        "job_protocol_version": job.job_protocol_version,
        "open3d_version": open3d_version,
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
        "point_to_plane_rmse": point_to_plane_rmse,
        "normal_valid_count": normal_valid_count,
        "normal_radius": normal_radius,
        "source_point_count": source_point_count,
        "target_point_count": target_point_count,
        # 기존 UI의 이름은 유지하되 실제로는 sample이 아닌 전체 crop 점 수입니다.
        "moving_sample_count": source_point_count,
        "reference_sample_count": target_point_count,
    }
    for name, value in optional_values.items():
        if value is not None:
            payload[name] = value
    if stable_delta is not None:
        delta = _validate_rigid_matrix(stable_delta, "Open3D stable delta")
        # runtime은 marker preview까지 합성된 안정 alignment만 소비합니다.
        payload["stable_alignment_4x4"] = (delta @ job.initial_alignment).tolist()
    sink(payload)


def _registration_call(
    o3d: Any,
    job: Open3dSolverInput,
    source_cloud: Any,
    target_cloud: Any,
    stage: Open3dDistanceStage,
    current_delta: np.ndarray,
    batch_iterations: int,
    tukey_loss_k: float | None,
) -> Any:
    """선택 backend에 맞는 Open3D legacy registration 한 batch를 실행합니다."""

    registration = o3d.pipelines.registration
    criteria = registration.ICPConvergenceCriteria(
        relative_fitness=stage.relative_fitness,
        relative_rmse=stage.relative_rmse,
        max_iteration=int(batch_iterations),
    )
    if job.backend_id == OPEN3D_GICP_BACKEND_ID:
        # GICP에는 normal·covariance를 미리 넣지 않아 Open3D의 regularized 경로를 보존합니다.
        estimation = registration.TransformationEstimationForGeneralizedICP()
        return registration.registration_generalized_icp(
            source_cloud,
            target_cloud,
            stage.maximum_correspondence_distance,
            current_delta,
            estimation,
            criteria,
        )
    if tukey_loss_k is None:
        raise Open3dIcpInputError("강건 Point-to-Plane에는 TukeyLoss scale이 필요합니다.")
    loss = registration.TukeyLoss(float(tukey_loss_k))
    estimation = registration.TransformationEstimationPointToPlane(loss)
    return registration.registration_icp(
        source_cloud,
        target_cloud,
        stage.maximum_correspondence_distance,
        current_delta,
        estimation,
        criteria,
    )




def _correspondence_pairs(registration_result: Any) -> np.ndarray:
    """Open3D의 비결정 순서 correspondence를 source·target 순으로 정렬합니다."""

    pairs = np.asarray(registration_result.correspondence_set, dtype=np.int64)
    if pairs.size == 0:
        return np.empty((0, 2), dtype=np.int64)
    if pairs.ndim != 2 or pairs.shape[1] != 2 or np.any(pairs < 0):
        raise Open3dIcpNumericalError("Open3D correspondence_set 형식이 올바르지 않습니다.")
    order = np.lexsort((pairs[:, 1], pairs[:, 0]))
    return np.ascontiguousarray(pairs[order], dtype=np.int64)


def _correspondence_signature(pairs: np.ndarray) -> str:
    """정렬한 correspondence index 쌍의 재현성 지문을 반환합니다."""

    return _array_fingerprint(np.ascontiguousarray(pairs, dtype=np.int64))


def _evaluate_on_full_crop(
    o3d: Any,
    source_cloud: Any,
    target_cloud: Any,
    source_positions: np.ndarray,
    target_positions: np.ndarray,
    target_normals: np.ndarray | None,
    transform: np.ndarray,
    maximum_distance: float,
    *,
    minimum_correspondences: int,
    require_minimum: bool,
    evaluation_fingerprint: str,
) -> dict[str, Any]:
    """같은 전체 crop P/A와 같은 평가 거리에서 품질 지표를 계산합니다."""

    registration = o3d.pipelines.registration
    evaluation = registration.evaluate_registration(
        source_cloud,
        target_cloud,
        maximum_distance,
        transform,
    )
    pairs = _correspondence_pairs(evaluation)
    correspondence_count = int(pairs.shape[0])
    if require_minimum and correspondence_count < minimum_correspondences:
        raise Open3dIcpNumericalError("Open3D 전체점 평가 대응점 수가 최소 기준보다 적습니다.")
    if correspondence_count:
        transformed = _transform_positions(source_positions, transform)
        residual_vectors = transformed[pairs[:, 0]] - target_positions[pairs[:, 1]]
        distances = np.linalg.norm(residual_vectors, axis=1)
        point_rmse: float | None = float(np.sqrt(np.mean(np.square(distances))))
        transformed_cloud = _make_legacy_point_cloud(o3d, transformed)
        reverse = registration.evaluate_registration(
            target_cloud,
            transformed_cloud,
            maximum_distance,
            np.identity(4, dtype=np.float64),
        )
        reverse_pairs = _correspondence_pairs(reverse)
        target_count = int(target_positions.shape[0])
        forward_codes = pairs[:, 0] * target_count + pairs[:, 1]
        reverse_codes = reverse_pairs[:, 1] * target_count + reverse_pairs[:, 0]
        reciprocal_count = int(np.intersect1d(forward_codes, reverse_codes, assume_unique=False).size)
        reciprocal_ratio: float | None = reciprocal_count / correspondence_count
        point_to_plane_rmse: float | None = None
        normal_valid_ratio: float | None = None
        if target_normals is not None:
            normals = target_normals[pairs[:, 1]]
            lengths = np.linalg.norm(normals, axis=1)
            valid = np.isfinite(normals).all(axis=1) & (lengths > np.finfo(np.float64).eps)
            normal_valid_ratio = float(np.count_nonzero(valid) / correspondence_count)
            if np.any(valid):
                plane_residuals = np.einsum("ij,ij->i", residual_vectors[valid], normals[valid])
                point_to_plane_rmse = float(np.sqrt(np.mean(np.square(plane_residuals))))
    else:
        point_rmse = None
        reciprocal_count = 0
        reciprocal_ratio = None
        point_to_plane_rmse = None
        normal_valid_ratio = None
    return {
        "point_to_point_rmse": point_rmse,
        "point_to_plane_rmse": point_to_plane_rmse,
        "fitness": float(evaluation.fitness),
        "open3d_inlier_rmse": float(evaluation.inlier_rmse),
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




def _quality_mapping(baseline: Mapping[str, Any], candidate: Mapping[str, Any]) -> dict[str, Any]:
    """초기 정렬 baseline 대비 전체점 후보의 PASS·REVIEW만 결정합니다."""

    reasons: list[str] = []
    baseline_rmse = baseline.get("point_to_point_rmse")
    candidate_rmse = candidate.get("point_to_point_rmse")
    if not bool(candidate.get("valid")) or candidate_rmse is None:
        raise Open3dIcpNumericalError("Open3D 최종 후보의 전체점 평가가 성립하지 않습니다.")
    if not bool(baseline.get("valid")) or baseline_rmse is None:
        reasons.append("초기 정렬 baseline의 전체점 대응점이 최소 기준보다 적습니다.")
    elif float(candidate_rmse) >= float(baseline_rmse):
        reasons.append("Open3D 후보가 초기 정렬 baseline point-to-point RMSE를 개선하지 못했습니다.")
    if candidate.get("point_to_plane_rmse") is None:
        reasons.append("공통 target normal 기반 point-to-plane RMSE를 계산하지 못했습니다.")
    status = "PASS" if not reasons else "REVIEW"
    return {
        "status": status,
        "review_reasons": reasons,
        "user_review_required": bool(reasons),
    }


def solve_open3d_icp(
    solver_input: Open3dSolverInput,
    *,
    progress_sink: ProgressSink = None,
    cancel_probe: CancelProbe = None,
) -> Open3dSolverResult:
    """전체 overlap 점군으로 Open3D GICP 또는 Tukey Point-to-Plane ICP를 실행합니다."""

    if not isinstance(solver_input, Open3dSolverInput):
        raise Open3dIcpInputError("Open3D solver 입력 형식이 올바르지 않습니다.")
    if solver_input.backend_id not in {
        OPEN3D_GICP_BACKEND_ID,
        OPEN3D_ROBUST_POINT_TO_PLANE_BACKEND_ID,
    }:
        raise Open3dIcpInputError("Open3D solver에는 Open3D backend 입력이 필요합니다.")
    started_at = monotonic()
    _raise_if_cancelled(cancel_probe)
    o3d = _lazy_open3d()
    open3d_version = str(o3d.__version__)
    _emit_progress(
        progress_sink,
        solver_input,
        open3d_version=open3d_version,
        stage="OPEN3D_PREPARE",
        progress=0.0,
        message="Open3D 입력과 locked scale 정책을 검증했습니다.",
    )
    capture_distance = _derive_capture_distance(solver_input)
    prepared = _prepare_full_overlap_clouds(solver_input, capture_distance)
    minimum_correspondences = _strict_positive_int(
        solver_input.parameters.get("minimum_correspondences", 30),
        "Open3D 최소 대응점 수",
    )
    if prepared.source_positions.shape[0] < minimum_correspondences or prepared.target_positions.shape[0] < minimum_correspondences:
        raise Open3dIcpInputError("Open3D 전체 overlap crop 점 수가 최소 대응점보다 적습니다.")
    stages, normal_radius, normal_max_nn, tukey_loss_k, batch_size = _derive_stages_and_normal_policy(
        solver_input,
        prepared,
        capture_distance,
    )
    source_minimum, source_maximum = prepared.source_bounds
    target_minimum, target_maximum = prepared.target_bounds
    local_origin = np.ascontiguousarray(
        (np.minimum(source_minimum, target_minimum) + np.maximum(source_maximum, target_maximum)) * 0.5,
        dtype=np.float64,
    )
    moving_diagonal = _diagonal(source_minimum, source_maximum)
    local_source = np.ascontiguousarray(prepared.source_positions - local_origin[np.newaxis, :], dtype=np.float64)
    local_target = np.ascontiguousarray(prepared.target_positions - local_origin[np.newaxis, :], dtype=np.float64)
    full_input_audit = _full_input_audit(prepared, stages)
    _emit_progress(
        progress_sink,
        solver_input,
        open3d_version=open3d_version,
        stage="OPEN3D_BUILD_FULL_CLOUD",
        progress=0.08,
        message="voxel·점 예산 없이 전체 overlap 점군을 한 번 고정했습니다.",
        source_point_count=int(local_source.shape[0]),
        target_point_count=int(local_target.shape[0]),
    )
    _raise_if_cancelled(cancel_probe)
    source_cloud = _make_legacy_point_cloud(o3d, local_source)
    target_cloud = _make_legacy_point_cloud(o3d, local_target)
    target_normals: np.ndarray | None = None
    normal_audit: dict[str, Any] = {
        "normal_radius": float(normal_radius),
        "normal_maximum_neighbors": int(normal_max_nn),
        "tukey_loss_k": float(tukey_loss_k),
        "source_normals_prepopulated": False,
        "target_normals_prepopulated_before_gicp": False,
    }
    if solver_input.backend_id == OPEN3D_GICP_BACKEND_ID:
        # GICP cloud에는 추정 normal/covariance를 넣지 않습니다.
        if bool(getattr(source_cloud, "has_normals")()) or bool(getattr(target_cloud, "has_normals")()):
            raise Open3dIcpNumericalError("GICP 입력에 사전 normal이 설정되었습니다.")
        has_covariances = getattr(source_cloud, "has_covariances", None)
        if callable(has_covariances) and (has_covariances() or target_cloud.has_covariances()):
            raise Open3dIcpNumericalError("GICP 입력에 사전 covariance가 설정되었습니다.")
        normal_audit["normal_policy"] = "OPEN3D_INTERNAL_REGULARIZED_COVARIANCE"
    else:
        _emit_progress(
            progress_sink,
            solver_input,
            open3d_version=open3d_version,
            stage="OPEN3D_BUILD_FULL_CLOUD",
            progress=0.12,
            message="강건 Point-to-Plane용 target 전체점 normal을 추정합니다.",
            normal_radius=normal_radius,
            source_point_count=int(local_source.shape[0]),
            target_point_count=int(local_target.shape[0]),
        )
        target_normals, valid_normals, valid_ratio = _estimate_target_normals(
            o3d,
            target_cloud,
            radius=normal_radius,
            maximum_neighbors=normal_max_nn,
            minimum_valid=minimum_correspondences,
        )
        normal_audit.update(
            {
                "normal_policy": "TARGET_ONLY_OPEN3D_ESTIMATE_NORMALS",
                "target_normal_valid_count": valid_normals,
                "target_normal_valid_ratio": valid_ratio,
            }
        )
    current_delta_local = np.identity(4, dtype=np.float64)
    level_results: list[Mapping[str, Any]] = []
    total_iterations = sum(stage.max_iterations for stage in stages)
    completed_iterations = 0
    max_rotation_degrees = _strict_finite_float(
        solver_input.parameters.get("max_rotation_degrees", 30.0),
        "Open3D 최대 회전 안전 한도",
        minimum=np.finfo(np.float64).eps,
    )
    translation_limit = moving_diagonal * _strict_finite_float(
        solver_input.parameters.get("max_translation_fraction", 0.25),
        "Open3D 최대 이동 비율",
        minimum=np.finfo(np.float64).eps,
    )
    for stage in stages:
        _raise_if_cancelled(cancel_probe)
        stage_iterations = 0
        batch_count = 0
        converged = False
        last_result: Any | None = None
        while stage_iterations < stage.max_iterations:
            _raise_if_cancelled(cancel_probe)
            current_batch = min(batch_size, stage.max_iterations - stage_iterations)
            previous_delta = current_delta_local
            registration_result = _registration_call(
                o3d,
                solver_input,
                source_cloud,
                target_cloud,
                stage,
                current_delta_local,
                current_batch,
                tukey_loss_k if solver_input.backend_id == OPEN3D_ROBUST_POINT_TO_PLANE_BACKEND_ID else None,
            )
            _raise_if_cancelled(cancel_probe)
            current_delta_local = _validate_rigid_matrix(
                np.asarray(registration_result.transformation, dtype=np.float64),
                "Open3D registration delta",
            )
            step = _validate_rigid_matrix(
                current_delta_local @ _inverse_rigid(previous_delta),
                "Open3D batch step",
            )
            step_translation = float(np.linalg.norm(step[:3, 3]))
            step_rotation = _rotation_angle_degrees(step[:3, :3])
            if step_translation > translation_limit:
                raise Open3dIcpNumericalError("Open3D 한 batch의 local 추가 이동이 안전 한도를 초과했습니다.")
            if step_rotation > max_rotation_degrees:
                raise Open3dIcpNumericalError("Open3D 한 batch의 추가 회전이 안전 한도를 초과했습니다.")
            stage_iterations += current_batch
            completed_iterations += current_batch
            batch_count += 1
            last_result = registration_result
            pairs = _correspondence_pairs(registration_result)
            _emit_progress(
                progress_sink,
                solver_input,
                open3d_version=open3d_version,
                stage=_stage_progress_name(solver_input.backend_id, stage.name),
                progress=0.15 + 0.72 * (completed_iterations / max(total_iterations, 1)),
                message=f"Open3D {stage.name} 거리 단계 batch {batch_count}",
                level_name=stage.name,
                iteration=stage_iterations,
                max_iterations=stage.max_iterations,
                maximum_correspondence_distance=stage.maximum_correspondence_distance,
                correspondence_count=int(pairs.shape[0]),
                point_to_point_rmse=float(registration_result.inlier_rmse),
                normal_valid_count=normal_audit.get("target_normal_valid_count"),
                normal_radius=normal_radius if solver_input.backend_id == OPEN3D_ROBUST_POINT_TO_PLANE_BACKEND_ID else None,
                source_point_count=int(local_source.shape[0]),
                target_point_count=int(local_target.shape[0]),
                stable_delta=_local_delta_to_world(current_delta_local, local_origin),
            )
            if _is_stable_delta(previous_delta, current_delta_local, stage.maximum_correspondence_distance):
                converged = True
                break
        if last_result is None:
            raise Open3dIcpNumericalError("Open3D 거리 단계가 registration 결과를 만들지 못했습니다.")
        level_results.append(
            {
                "level_name": stage.name,
                "maximum_correspondence_distance": float(stage.maximum_correspondence_distance),
                "max_iterations": int(stage.max_iterations),
                "iterations": int(stage_iterations),
                "iteration_batch_size": int(batch_size),
                "batch_count": int(batch_count),
                "converged": bool(converged),
                "stop_reason": "CONVERGED" if converged else "ITERATION_LIMIT",
                "open3d_fitness": float(last_result.fitness),
                "open3d_inlier_rmse": float(last_result.inlier_rmse),
                "open3d_correspondence_count": int(_correspondence_pairs(last_result).shape[0]),
                "source_point_count": int(local_source.shape[0]),
                "target_point_count": int(local_target.shape[0]),
                "source_positions_fingerprint": full_input_audit["source_crop_positions_fingerprint"],
                "source_point_ids_fingerprint": full_input_audit["source_crop_point_ids_fingerprint"],
                "target_positions_fingerprint": full_input_audit["target_crop_positions_fingerprint"],
                "target_point_ids_fingerprint": full_input_audit["target_crop_point_ids_fingerprint"],
            }
        )
        _emit_progress(
            progress_sink,
            solver_input,
            open3d_version=open3d_version,
            stage=_stage_progress_name(solver_input.backend_id, stage.name),
            progress=0.15 + 0.72 * (completed_iterations / max(total_iterations, 1)),
            message=f"Open3D {stage.name} 거리 단계를 완료했습니다.",
            level_name=stage.name,
            iteration=stage_iterations,
            max_iterations=stage.max_iterations,
            level_complete=True,
            maximum_correspondence_distance=stage.maximum_correspondence_distance,
            correspondence_count=int(_correspondence_pairs(last_result).shape[0]),
            point_to_point_rmse=float(last_result.inlier_rmse),
            normal_valid_count=normal_audit.get("target_normal_valid_count"),
            normal_radius=normal_radius if solver_input.backend_id == OPEN3D_ROBUST_POINT_TO_PLANE_BACKEND_ID else None,
            source_point_count=int(local_source.shape[0]),
            target_point_count=int(local_target.shape[0]),
            stable_delta=_local_delta_to_world(current_delta_local, local_origin),
        )
    _raise_if_cancelled(cancel_probe)
    # GICP 등록 cloud를 건드리지 않고, 공통 평가용 target normal만 별도 cloud에서 계산합니다.
    if target_normals is None:
        evaluation_target_cloud = _make_legacy_point_cloud(o3d, local_target)
        try:
            target_normals, valid_normals, valid_ratio = _estimate_target_normals(
                o3d,
                evaluation_target_cloud,
                radius=normal_radius,
                maximum_neighbors=normal_max_nn,
                minimum_valid=minimum_correspondences,
            )
            normal_audit.update(
                {
                    "evaluation_normal_policy": "SEPARATE_FULL_TARGET_EVALUATION",
                    "target_normal_valid_count": valid_normals,
                    "target_normal_valid_ratio": valid_ratio,
                }
            )
            evaluation_target_for_metrics = evaluation_target_cloud
        except Open3dIcpNumericalError as error:
            # GICP 정합 자체는 유효할 수 있으므로 point-to-plane 평가 불가를 REVIEW 사유로 보존합니다.
            normal_audit["evaluation_normal_error"] = str(error)
            target_normals = None
            evaluation_target_for_metrics = target_cloud
    else:
        evaluation_target_for_metrics = target_cloud
    evaluation_distance = stages[-1].maximum_correspondence_distance
    evaluation_fingerprint = _canonical_fingerprint(
        {
            "input_policy": FULL_INPUT_POLICY,
            "source_positions": full_input_audit["source_crop_positions_fingerprint"],
            "source_point_ids": full_input_audit["source_crop_point_ids_fingerprint"],
            "target_positions": full_input_audit["target_crop_positions_fingerprint"],
            "target_point_ids": full_input_audit["target_crop_point_ids_fingerprint"],
            "maximum_correspondence_distance": evaluation_distance,
        }
    )
    _emit_progress(
        progress_sink,
        solver_input,
        open3d_version=open3d_version,
        stage="OPEN3D_EVALUATE",
        progress=0.90,
        message="초기 정렬 baseline과 Open3D 후보를 같은 전체점 crop에서 평가합니다.",
        maximum_correspondence_distance=evaluation_distance,
        normal_valid_count=normal_audit.get("target_normal_valid_count"),
        normal_radius=normal_radius,
        source_point_count=int(local_source.shape[0]),
        target_point_count=int(local_target.shape[0]),
    )
    baseline = _evaluate_on_full_crop(
        o3d,
        source_cloud,
        evaluation_target_for_metrics,
        local_source,
        local_target,
        target_normals,
        np.identity(4, dtype=np.float64),
        evaluation_distance,
        minimum_correspondences=minimum_correspondences,
        require_minimum=False,
        evaluation_fingerprint=evaluation_fingerprint,
    )
    candidate = _evaluate_on_full_crop(
        o3d,
        source_cloud,
        evaluation_target_for_metrics,
        local_source,
        local_target,
        target_normals,
        current_delta_local,
        evaluation_distance,
        minimum_correspondences=minimum_correspondences,
        require_minimum=True,
        evaluation_fingerprint=evaluation_fingerprint,
    )
    icp_delta = _local_delta_to_world(current_delta_local, local_origin)
    delta_diagnostics = analyze_external_icp_delta(
        icp_delta,
        local_origin,
        field_name="Open3D 최종 Project World delta",
    )
    delta_translation = delta_diagnostics.translation_distance
    delta_rotation_degrees = delta_diagnostics.rotation_degrees
    if delta_translation > translation_limit:
        raise Open3dIcpNumericalError("Open3D 최종 local delta 이동이 안전 한도를 초과했습니다.")
    if delta_rotation_degrees > max_rotation_degrees:
        raise Open3dIcpNumericalError("Open3D 최종 delta 회전이 안전 한도를 초과했습니다.")
    final_alignment = np.ascontiguousarray(icp_delta @ solver_input.initial_alignment, dtype=np.float64)
    final_scale, _final_rotation = _uniform_scale_and_rotation(final_alignment, "Open3D final alignment")
    if not np.isclose(
        final_scale,
        solver_input.marker_uniform_scale,
        rtol=0.0,
        atol=1.0e-9 * max(1.0, solver_input.marker_uniform_scale),
    ):
        raise Open3dIcpNumericalError("Open3D final alignment가 초기 scale을 변경했습니다.")
    quality = _quality_mapping(baseline, candidate)
    execution_audit = {
        "open3d_api": "legacy_cpu",
        "initial_registration_transform": "IDENTITY",
        "final_composition": "icp_delta @ initial_alignment",
        "distance_stages": [
            {
                "name": stage.name,
                "maximum_correspondence_distance": float(stage.maximum_correspondence_distance),
                "max_iterations": int(stage.max_iterations),
                "relative_fitness": float(stage.relative_fitness),
                "relative_rmse": float(stage.relative_rmse),
            }
            for stage in stages
        ],
        "iteration_batch_size": int(batch_size),
        "evaluation_maximum_correspondence_distance": float(evaluation_distance),
        "motion_limits": {
            "max_rotation_degrees": float(max_rotation_degrees),
            "max_translation": float(translation_limit),
        },
    }
    return Open3dSolverResult(
        solver_input=solver_input,
        initial_alignment=np.array(solver_input.initial_alignment, dtype=np.float64, order="C", copy=True),
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
    "FULL_INPUT_POLICY",
    "OPEN3D_GICP_ALGORITHM_VERSION",
    "OPEN3D_GICP_BACKEND_ID",
    "OPEN3D_JOB_PROTOCOL_VERSION",
    "OPEN3D_LEGACY_VERSION",
    "OPEN3D_ROBUST_POINT_TO_PLANE_ALGORITHM_VERSION",
    "OPEN3D_ROBUST_POINT_TO_PLANE_BACKEND_ID",
    "SMALL_GICP_ALGORITHM_VERSION",
    "SMALL_GICP_BACKEND_ID",
    "Open3dDistanceStage",
    "Open3dIcpCancelled",
    "Open3dIcpEnvironmentError",
    "Open3dIcpError",
    "Open3dIcpInputError",
    "Open3dIcpNumericalError",
    "Open3dPointCloud",
    "Open3dSolverInput",
    "Open3dSolverResult",
    "solve_open3d_icp",
)
