"""ICP 품질 지표의 PASS·REVIEW·REJECT 경계를 Blender 환경에서 검증합니다."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import sys

import numpy as np


def _add_source_path() -> None:
    """애드온 소스 디렉터리를 Blender 테스트 모듈 경로에 추가합니다."""

    source_path = Path(__file__).resolve().parents[3]
    source_text = str(source_path)
    if source_text not in sys.path:
        sys.path.insert(0, source_text)


def _quality(**overrides):
    """품질 상태 경계 테스트에 쓸 일관된 기본 입력을 생성합니다."""

    from tools.align.alignment import (
        IcpPerReferenceMetrics,
        IcpScaleMode,
        IcpScaleRefineStatus,
        create_sim3_transform,
        evaluate_icp_quality,
    )

    identity = create_sim3_transform(np.identity(3), 1.0, np.zeros(3))
    residuals = np.full(100, 0.01, dtype=np.float64)
    arguments = {
        "residuals_float64": residuals,
        "query_count": 120,
        "reciprocal_ratio": 0.9,
        "overlap_ratio": 0.8,
        "initial_alignment": identity,
        "final_alignment": identity,
        "marker_uniform_scale": 1.0,
        "scale_mode": IcpScaleMode.LOCKED,
        "scale_refine_status": IcpScaleRefineStatus.NOT_REQUESTED,
        "scale_refine_reason": "",
        "fine_voxel_size": 0.1,
        "overlap_diagonal": 10.0,
        "total_iterations": 8,
        "elapsed_seconds": 0.1,
        "per_reference_model": (
            IcpPerReferenceMetrics("A", 100, 1.0),
        ),
    }
    arguments.update(overrides)
    return evaluate_icp_quality(**arguments)


def test_quality_pass_review_and_reject_boundaries() -> None:
    """충분한 대응점은 PASS, 불확실한 유효 결과는 REVIEW, 스케일 위반은 REJECT인지 확인합니다."""

    from tools.align.alignment import IcpQualityStatus

    assert _quality().status is IcpQualityStatus.PASS
    reviewed = _quality(reciprocal_ratio=0.2)
    assert reviewed.status is IcpQualityStatus.REVIEW
    assert "reciprocal" in " ".join(reviewed.review_reasons)
    low_evidence = _quality(
        residuals_float64=np.full(29, 0.01),
        query_count=120,
        per_reference_model=(),
    )
    assert low_evidence.status is IcpQualityStatus.REVIEW
    assert low_evidence.user_review_required is True
    assert "30개 미만" in " ".join(low_evidence.review_reasons)


def test_scale_lock_and_scale_boundary_are_classified() -> None:
    """Locked scale 변경은 REJECT, ±5% 경계 보정은 REVIEW인지 확인합니다."""

    from tools.align.alignment import (
        IcpQualityStatus,
        IcpScaleMode,
        IcpScaleRefineStatus,
        create_sim3_transform,
    )

    changed = create_sim3_transform(np.identity(3), 1.02, np.zeros(3))
    locked = _quality(final_alignment=changed)
    assert locked.status is IcpQualityStatus.REJECT
    refined = _quality(
        final_alignment=create_sim3_transform(np.identity(3), 1.05, np.zeros(3)),
        scale_mode=IcpScaleMode.REFINE,
        scale_refine_status=IcpScaleRefineStatus.ACCEPTED,
        scale_boundary_reached=True,
    )
    assert refined.status is IcpQualityStatus.REVIEW
    assert "스케일" in " ".join(refined.review_reasons)


def test_marker_drift_and_reference_metrics_are_strict() -> None:
    """marker drift 경고와 다중 A count·fraction 무결성 검증을 확인합니다."""

    from tools.align.alignment import IcpInputError, IcpPerReferenceMetrics, create_sim3_transform

    marker = np.asarray(((0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0)), dtype=np.float64)
    drifted = _quality(
        final_alignment=create_sim3_transform(np.identity(3), 1.0, np.asarray((2.0, 0.0, 0.0))),
        marker_reference_project=marker,
        marker_moving_project=marker,
        marker_rms=0.01,
    )
    assert "marker drift" in " ".join(drifted.review_reasons)
    try:
        _quality(
            per_reference_model=(
                IcpPerReferenceMetrics("A", 99, 0.99),
                IcpPerReferenceMetrics("B", 1, 0.02),
            )
        )
    except IcpInputError:
        pass
    else:
        raise AssertionError("기준 모델별 count/fraction 불일치를 허용했습니다.")


def test_quality_numeric_inputs_reject_bool_and_string_coercion() -> None:
    """품질 계산의 공개 실수 인자가 bool·문자열을 암묵 변환하지 않는지 확인합니다."""

    from tools.align.alignment import IcpInputError

    for field, value in (("reciprocal_ratio", True), ("fine_voxel_size", "0.1")):
        try:
            _quality(**{field: value})
        except IcpInputError:
            pass
        else:
            raise AssertionError(f"품질 계산이 {field}의 암묵 변환을 허용했습니다.")


def test_reciprocal_distance_rejects_bool_and_string_coercion() -> None:
    """reciprocal 검색 거리도 공개 수치 경계에서 암묵 변환하지 않는지 확인합니다."""

    from tools.align.alignment import (
        BruteForceNearestNeighborIndex,
        IcpInputError,
        reciprocal_match_ratio,
    )

    points = np.asarray(((0.0, 0.0, 0.0),), dtype=np.float64)
    indices = np.asarray((0,), dtype=np.int64)
    for maximum_distance in (True, "0.1"):
        try:
            reciprocal_match_ratio(
                points,
                points,
                indices,
                indices,
                BruteForceNearestNeighborIndex(),
                maximum_distance=maximum_distance,
            )
        except IcpInputError:
            pass
        else:
            raise AssertionError("reciprocal 최대 대응 거리의 암묵 변환을 허용했습니다.")


def _valid_worker_result_mapping() -> dict[str, object]:
    """외부 결과 역직렬화 경계 테스트용 정상 result.json 사전을 생성합니다."""

    from tools.align.alignment import (
        IcpAlignmentResult,
        IcpIterationMetrics,
        IcpLevelResult,
        create_sim3_transform,
    )

    identity = create_sim3_transform(np.identity(3), 1.0, np.zeros(3))
    iteration = IcpIterationMetrics(
        level_name="fine",
        iteration=1,
        query_count=100,
        maximum_distance_count=100,
        unique_target_count=100,
        inlier_count=100,
        fitness=1.0,
        rmse=0.01,
        median_residual=0.01,
        p90_residual=0.01,
        delta_translation=0.0,
        delta_rotation_degrees=0.0,
        elapsed_seconds=0.01,
        correspondence_signature="strict-worker-boundary",
        effective_trim_fraction=0.8,
    )
    level = IcpLevelResult(
        level_name="fine",
        voxel_size=0.1,
        maximum_correspondence_distance=0.3,
        moving_sample_count=100,
        reference_sample_count=100,
        iterations=(iteration,),
        converged=True,
        effective_trim_fraction=0.8,
    )
    result = IcpAlignmentResult(
        moving_model_id="P",
        reference_model_ids=("A",),
        initial_alignment=identity,
        final_alignment=identity,
        icp_delta=identity,
        quality=_quality(),
        level_results=(level,),
        input_fingerprint="strict-input-fingerprint",
        parameter_fingerprint="strict-parameter-fingerprint",
        model_revision_snapshot=(("A", 7), ("P", 4)),
        marker_signature="strict-marker-signature",
    )
    return result.to_mapping()


def _expect_worker_error(factory, mapping: dict[str, object], description: str) -> None:
    """손상된 worker 사전이 공용 typed 오류로 중단되는지 검증합니다."""

    from tools.align.alignment import IcpWorkerError

    try:
        factory(mapping)
    except IcpWorkerError:
        return
    raise AssertionError(f"{description}이 worker 결과 경계에서 거부되지 않았습니다.")


def test_worker_mapping_rejects_implicit_numeric_and_bool_coercion() -> None:
    """progress·반복·레벨·품질 결과가 암묵 수치·bool 변환을 허용하지 않는지 확인합니다."""

    from tools.align.alignment import (
        IcpIterationMetrics,
        IcpLevelResult,
        IcpProgressSnapshot,
        IcpQualityMetrics,
    )

    progress = IcpProgressSnapshot(
        stage="ICP_FINE",
        level_name="fine",
        iteration=1,
        max_iterations=2,
        progress=0.5,
        level_complete=False,
    ).to_mapping()
    for field, invalid_values in (
        ("iteration", (1.0e999, "1", True)),
        ("progress", (float("nan"), float("inf"), "0.5", True)),
        ("level_complete", (1, "false", np.bool_(False))),
    ):
        for invalid_value in invalid_values:
            malformed = deepcopy(progress)
            malformed[field] = invalid_value
            _expect_worker_error(
                IcpProgressSnapshot.from_mapping,
                malformed,
                f"progress.{field}={invalid_value!r}",
            )

    result = _valid_worker_result_mapping()
    iteration = deepcopy(result["level_results"][0]["iterations"][0])
    for field, invalid_value in (
        ("inlier_count", "100"),
        ("query_count", True),
        ("rmse", float("inf")),
        ("fitness", "1.0"),
    ):
        malformed = deepcopy(iteration)
        malformed[field] = invalid_value
        _expect_worker_error(
            IcpIterationMetrics.from_mapping,
            malformed,
            f"iteration.{field}={invalid_value!r}",
        )

    level = deepcopy(result["level_results"][0])
    for field, invalid_value in (
        ("moving_sample_count", 1.0e999),
        ("reference_sample_count", "100"),
        ("converged", 1),
    ):
        malformed = deepcopy(level)
        malformed[field] = invalid_value
        _expect_worker_error(
            IcpLevelResult.from_mapping,
            malformed,
            f"level.{field}={invalid_value!r}",
        )

    quality = deepcopy(result["quality"])
    for field, invalid_value in (
        ("inlier_count", float("inf")),
        ("query_count", "120"),
        ("total_iterations", True),
        ("rmse", float("nan")),
        ("overlap_ratio", "0.8"),
    ):
        malformed = deepcopy(quality)
        malformed[field] = invalid_value
        _expect_worker_error(
            IcpQualityMetrics.from_mapping,
            malformed,
            f"quality.{field}={invalid_value!r}",
        )


def test_alignment_result_mapping_rejects_invalid_revision_values_and_roundtrips() -> None:
    """결과 revision은 엄격히 복원하고 정상 worker 사전은 손실 없이 왕복하는지 확인합니다."""

    from tools.align.alignment import IcpAlignmentResult

    mapping = _valid_worker_result_mapping()
    restored = IcpAlignmentResult.from_mapping(deepcopy(mapping))
    assert restored.to_mapping() == mapping

    for invalid_revision in (float("inf"), float("nan"), "7", True):
        malformed = deepcopy(mapping)
        malformed["model_revision_snapshot"][0][1] = invalid_revision
        _expect_worker_error(
            IcpAlignmentResult.from_mapping,
            malformed,
            f"result.model_revision_snapshot={invalid_revision!r}",
        )


def test_fixed_evaluation_metrics_and_stop_reason_are_preserved_for_review() -> None:
    """같은 평가 표본의 baseline/candidate와 증가 종료 사유가 REVIEW로 보존되는지 확인합니다."""

    from tools.align.alignment import (
        IcpEvaluationMetrics,
        IcpQualityStatus,
        IcpSolverStopReason,
    )

    baseline = IcpEvaluationMetrics(
        point_to_point_rmse=0.20,
        point_to_plane_rmse=0.10,
        fitness=0.80,
        reciprocal_ratio=0.70,
        inlier_count=96,
        query_count=120,
        maximum_correspondence_distance=0.50,
        normal_valid_ratio=0.90,
        normal_angle_pass_ratio=0.85,
        evaluation_fingerprint="fixed-evaluation-a",
    )
    candidate = IcpEvaluationMetrics(
        point_to_point_rmse=0.08,
        point_to_plane_rmse=0.04,
        fitness=0.80,
        reciprocal_ratio=0.70,
        inlier_count=96,
        query_count=120,
        maximum_correspondence_distance=0.50,
        normal_valid_ratio=0.90,
        normal_angle_pass_ratio=0.85,
        evaluation_fingerprint="fixed-evaluation-a",
    )
    reviewed = _quality(
        baseline_metrics=baseline,
        best_candidate_metrics=candidate,
        point_to_plane_rmse=candidate.point_to_plane_rmse,
        normal_valid_ratio=candidate.normal_valid_ratio,
        normal_angle_pass_ratio=candidate.normal_angle_pass_ratio,
        solver_stop_reason=IcpSolverStopReason.OBJECTIVE_INCREASE,
    )
    assert reviewed.status is IcpQualityStatus.REVIEW
    assert reviewed.relative_improvement > 0.0
    assert reviewed.baseline_metrics == baseline
    assert reviewed.best_candidate_metrics == candidate
    assert reviewed.solver_stop_reason is IcpSolverStopReason.OBJECTIVE_INCREASE
    assert "목적함수 증가" in " ".join(reviewed.review_reasons)

    inlier_drop_reviewed = _quality(
        baseline_metrics=baseline,
        best_candidate_metrics=candidate,
        point_to_plane_rmse=candidate.point_to_plane_rmse,
        normal_valid_ratio=candidate.normal_valid_ratio,
        normal_angle_pass_ratio=candidate.normal_angle_pass_ratio,
        solver_stop_reason=IcpSolverStopReason.LEVEL_INLIER_DROP,
    )
    assert inlier_drop_reviewed.status is IcpQualityStatus.REVIEW
    assert inlier_drop_reviewed.user_review_required is True
    assert inlier_drop_reviewed.rmse > 0.0
    assert inlier_drop_reviewed.point_to_plane_rmse == candidate.point_to_plane_rmse
    assert "RMS" in " ".join(inlier_drop_reviewed.review_reasons)


def run_all() -> None:
    """ICP 품질 순수 회귀를 Blender 프로세스에서 순서대로 실행합니다."""

    _add_source_path()
    test_quality_pass_review_and_reject_boundaries()
    test_scale_lock_and_scale_boundary_are_classified()
    test_marker_drift_and_reference_metrics_are_strict()
    test_quality_numeric_inputs_reject_bool_and_string_coercion()
    test_reciprocal_distance_rejects_bool_and_string_coercion()
    test_worker_mapping_rejects_implicit_numeric_and_bool_coercion()
    test_alignment_result_mapping_rejects_invalid_revision_values_and_roundtrips()
    test_fixed_evaluation_metrics_and_stop_reason_are_preserved_for_review()
    print("test_icp_quality: 통과")


if __name__ == "__main__":
    run_all()
