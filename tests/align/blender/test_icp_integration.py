"""실제 COLMAP ImportJob 뒤 ICP 결과 미리보기와 버리기 경계를 검증합니다."""

from __future__ import annotations

from pathlib import Path
import sys
from tempfile import TemporaryDirectory

import numpy as np


def _add_test_paths() -> None:
    """애드온 소스와 기존 마커 통합 fixture 경로를 테스트 모듈 탐색 경로에 넣습니다."""
    blender_test_path = Path(__file__).resolve().parent
    source_path = blender_test_path.parents[2]
    for path in (source_path, blender_test_path):
        path_text = str(path)
        if path_text not in sys.path:
            sys.path.insert(0, path_text)


def _preview_result_for_input(runtime, input_data, *, quality_status: str = "REVIEW"):
    """worker 중복 실행 없이 PASS·REVIEW UI 검증용 형식화된 ICP 결과 fixture를 구성합니다."""
    from tools.align.alignment import (
        IcpRuntimeResult,
        compose_sim3_transforms,
        create_sim3_transform,
    )
    from tools.align.constants import ICP_ALGORITHM_VERSION

    marker_result = runtime.marker_alignment.result
    assert marker_result is not None
    angle_radians = float(np.deg2rad(1.8))
    rotation = np.asarray(
        (
            (np.cos(angle_radians), -np.sin(angle_radians), 0.0),
            (np.sin(angle_radians), np.cos(angle_radians), 0.0),
            (0.0, 0.0, 1.0),
        ),
        dtype=np.float64,
    )
    translation = np.asarray((0.015, -0.010, 0.005), dtype=np.float64)
    icp_delta = create_sim3_transform(
        rotation,
        1.0,
        translation,
    )
    final_alignment = compose_sim3_transforms(icp_delta, marker_result.preview_alignment)
    # 누적 marker preview alignment의 scale을 기준으로 결과 품질을 구성합니다.
    marker_scale = float(input_data.snapshot.marker_uniform_scale)
    baseline_metrics = {
        "point_to_point_rmse": 0.05680,
        "weighted_point_to_point_rmse": 0.05412,
        "residual_median": 0.04120,
        "residual_p90": 0.09130,
        "evaluation_correspondence_count": 4,
        "low_weight_correspondence_count": 1,
        "isolation_candidate_correspondence_count": 1,
        "source_isolation_candidate_count": 2,
        "target_isolation_candidate_count": 3,
        "distance_unit": "PROJECT_WORLD_UNIT",
        "point_to_plane_rmse": 0.02110,
        "fitness": 0.742,
        "reciprocal_ratio": 0.694,
        "inlier_count": 4,
        "query_count": 5,
        "maximum_correspondence_distance": 0.0840,
        "normal_valid_ratio": 0.913,
        "normal_angle_pass_ratio": 0.887,
        "evaluation_fingerprint": "fixture-evaluation-v2",
    }
    candidate_metrics = {
        "point_to_point_rmse": 0.05677,
        "weighted_point_to_point_rmse": 0.05234,
        "residual_median": 0.03912,
        "residual_p90": 0.08765,
        "evaluation_correspondence_count": 4,
        "low_weight_correspondence_count": 1,
        "isolation_candidate_correspondence_count": 1,
        "source_isolation_candidate_count": 2,
        "target_isolation_candidate_count": 3,
        "distance_unit": "PROJECT_WORLD_UNIT",
        "point_to_plane_rmse": 0.02092,
        "fitness": 0.756,
        "reciprocal_ratio": 0.701,
        "inlier_count": 4,
        "query_count": 5,
        "maximum_correspondence_distance": 0.0672,
        "normal_valid_ratio": 0.917,
        "normal_angle_pass_ratio": 0.901,
        "evaluation_fingerprint": "fixture-evaluation-v2",
    }
    density_sampling_metrics = {
        "shared_voxel_size": 0.0124,
        "macro_cell_divisions": 4,
        "macro_cell_count": 64,
        "occupied_macro_cell_count": 23,
        "minimum_samples_per_macro_cell": 3,
        "median_samples_per_macro_cell": 7.0,
        "maximum_samples_per_macro_cell": 18,
    }
    best_candidate = {
        "level_name": "medium",
        "iteration": 4,
        "objective_kind": "POINT_TO_PLANE",
        "point_to_point_rmse": candidate_metrics["point_to_point_rmse"],
        "point_to_plane_rmse": candidate_metrics["point_to_plane_rmse"],
        "fitness": candidate_metrics["fitness"],
        "inlier_count": candidate_metrics["inlier_count"],
        "reciprocal_ratio": candidate_metrics["reciprocal_ratio"],
        "normal_valid_ratio": candidate_metrics["normal_valid_ratio"],
        "normal_angle_pass_ratio": candidate_metrics["normal_angle_pass_ratio"],
        "maximum_correspondence_distance": candidate_metrics[
            "maximum_correspondence_distance"
        ],
        "correspondence_signature": "fixture-correspondence-v2",
        "evaluation_fingerprint": "fixture-evaluation-v2",
    }
    normalized_status = str(quality_status).strip().upper()
    assert normalized_status in {"PASS", "REVIEW"}
    return IcpRuntimeResult(
        moving_model_id=input_data.seed.moving_model_id,
        reference_model_ids=tuple(
            cloud.model_id for cloud in input_data.snapshot.reference_clouds
        ),
        final_alignment=final_alignment,
        icp_delta=icp_delta,
        quality={
            "status": normalized_status,
            "fitness": 0.742,
            "rmse": 0.0031,
            "inlier_count": 4,
            "delta_translation": float(np.linalg.norm(translation)),
            "delta_rotation_degrees": 1.8,
            "marker_uniform_scale": marker_scale,
            "final_uniform_scale": final_alignment.uniform_scale,
            "scale_ratio": final_alignment.uniform_scale / marker_scale,
            "scale_refine_status": "SKIPPED",
            "scale_refine_reason": "고정 스케일 결과를 유지했습니다.",
            "review_reasons": (
                (
                    "마커 결과에서 추가 회전 1.8°를 확인하십시오.",
                    "겹침 경계를 육안으로 검토하십시오.",
                )
                if normalized_status == "REVIEW"
                else ()
            ),
        },
        metadata={
            "source": "ui-integration-fixture",
            "baseline_metrics": baseline_metrics,
            "best_candidate_metrics": candidate_metrics,
            "density_sampling_metrics": density_sampling_metrics,
            "relative_improvement": 0.0085,
            "best_level_name": "medium",
            "best_iteration": 4,
            "solver_stop_reason": "CORRESPONDENCE_OSCILLATION",
            "point_to_plane_rmse": candidate_metrics["point_to_plane_rmse"],
            "normal_valid_ratio": candidate_metrics["normal_valid_ratio"],
            "normal_angle_pass_ratio": candidate_metrics["normal_angle_pass_ratio"],
            "user_review_required": normalized_status == "REVIEW",
            "best_candidate": best_candidate,
        },
        input_fingerprint=input_data.input_fingerprint,
        parameter_fingerprint=input_data.parameters.fingerprint,
        marker_signature=input_data.seed.marker_signature,
        model_revision_snapshot=input_data.snapshot.model_revision_snapshot,
        algorithm_version=ICP_ALGORITHM_VERSION,
    )


def test_imported_marker_preview_uses_icp_result_non_destructively_and_discards() -> None:
    """실제 ImportJob 모델에서 ICP final은 preview만 바꾸고 버리면 marker 위치로 돌아오는지 검증합니다."""
    from marker_alignment_test_support import (
        add_three_completed_marker_pairs,
        import_known_sim3_models,
    )
    from tools.align.alignment import (
        begin_marker_preview,
        create_icp_input_snapshot,
        discard_icp_preview,
        effective_alignment_for_model,
    )
    from tools.align.constants import ICP_ALIGNMENT_PHASE_IDLE, ICP_ALIGNMENT_PHASE_PREVIEW
    from tools.align.state import RuntimeState

    with TemporaryDirectory(prefix="colmap_align_icp_integration_") as directory:
        runtime = RuntimeState()
        runtime.session_active = True
        fixture = import_known_sim3_models(runtime, Path(directory))
        add_three_completed_marker_pairs(runtime, fixture)
        marker_result = begin_marker_preview(runtime)
        committed_before = runtime.model_repository.alignment_transform_for_model_id(
            fixture.moving_model_id
        )
        input_data = create_icp_input_snapshot(
            runtime,
            preset="BALANCED",
            scale_refine_enabled=False,
        )
        result = _preview_result_for_input(runtime, input_data)
        assert result.quality["marker_uniform_scale"] == input_data.snapshot.marker_uniform_scale
        state = runtime.icp_alignment
        state.phase = ICP_ALIGNMENT_PHASE_PREVIEW
        state.input_data = input_data
        state.result = result
        state.working_alignment = result.final_alignment

        assert effective_alignment_for_model(runtime, fixture.moving_model_id).fingerprint == (
            result.final_alignment.fingerprint
        )
        assert runtime.model_repository.alignment_transform_for_model_id(
            fixture.moving_model_id
        ).fingerprint == committed_before.fingerprint

        discard_icp_preview(runtime)

        assert state.phase == ICP_ALIGNMENT_PHASE_IDLE
        assert state.result is None
        assert runtime.marker_alignment.is_active is True
        assert runtime.marker_alignment.result is marker_result
        assert effective_alignment_for_model(runtime, fixture.moving_model_id).fingerprint == (
            marker_result.preview_alignment.fingerprint
        )
        assert runtime.model_repository.alignment_transform_for_model_id(
            fixture.moving_model_id
        ).fingerprint == committed_before.fingerprint


def test_imported_pass_and_review_results_are_both_approvable() -> None:
    """실제 ImportJob 모델에서 PASS·REVIEW가 모두 동일한 원자 승인 경계를 통과하는지 검증합니다."""
    from marker_alignment_test_support import (
        add_three_completed_marker_pairs,
        import_known_sim3_models,
    )
    from tools.align.alignment import (
        approve_icp_result,
        begin_marker_preview,
        create_icp_input_snapshot,
        is_icp_result_approvable,
    )
    from tools.align.constants import ICP_ALIGNMENT_PHASE_PREVIEW, MODEL_STATUS_REGISTERED
    from tools.align.state import RuntimeState

    for quality_status in ("PASS", "REVIEW"):
        with TemporaryDirectory(prefix="colmap_align_icp_approval_") as directory:
            runtime = RuntimeState()
            runtime.session_active = True
            fixture = import_known_sim3_models(runtime, Path(directory))
            add_three_completed_marker_pairs(runtime, fixture)
            begin_marker_preview(runtime)
            input_data = create_icp_input_snapshot(
                runtime,
                preset="BALANCED",
                scale_refine_enabled=False,
            )
            result = _preview_result_for_input(
                runtime,
                input_data,
                quality_status=quality_status,
            )
            state = runtime.icp_alignment
            state.phase = ICP_ALIGNMENT_PHASE_PREVIEW
            state.input_data = input_data
            state.result = result
            state.working_alignment = result.final_alignment

            assert is_icp_result_approvable(result) is True
            commit = approve_icp_result(runtime)

            assert commit.after.alignment.fingerprint == result.final_alignment.fingerprint
            assert runtime.model_repository.alignment_transform_for_model_id(
                fixture.moving_model_id
            ).fingerprint == result.final_alignment.fingerprint
            assert runtime.model_catalog.models[fixture.moving_model_id].registration_status == MODEL_STATUS_REGISTERED
            assert runtime.marker_selection.completed_pairs == []
            assert runtime.marker_alignment.is_active is False


def run_all() -> None:
    """실제 가져오기 기반 ICP 결과 미리보기 통합 검사를 실행합니다."""
    _add_test_paths()
    test_imported_marker_preview_uses_icp_result_non_destructively_and_discards()
    test_imported_pass_and_review_results_are_both_approvable()
    print("test_icp_integration: 통과")


if __name__ == "__main__":
    run_all()
