"""ICP Kabsch·강건 필터·다중 해상도·bounded scale refine을 Blender에서 검증합니다."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import sys
from unittest.mock import patch

import numpy as np


def _add_source_path() -> None:
    """애드온 소스 디렉터리를 Blender 테스트 모듈 경로에 추가합니다."""

    source_path = Path(__file__).resolve().parents[3]
    source_text = str(source_path)
    if source_text not in sys.path:
        sys.path.insert(0, source_text)


def _rotation_z(angle_degrees: float) -> np.ndarray:
    """known-transform fixture에 쓸 Z축 proper rotation을 반환합니다."""

    angle = np.radians(angle_degrees)
    return np.asarray(
        ((np.cos(angle), -np.sin(angle), 0.0), (np.sin(angle), np.cos(angle), 0.0), (0.0, 0.0, 1.0)),
        dtype=np.float64,
    )


def _source_points(count: int = 180) -> np.ndarray:
    """비공선·비대칭 synthetic P 점군을 결정적으로 생성합니다."""

    generator = np.random.default_rng(20260815)
    points = generator.normal(size=(count, 3))
    points[:, 0] *= 2.0
    points[:, 1] += points[:, 2] * 0.17
    return np.asarray(points, dtype=np.float64)


def _overlap_retry_clouds():
    """기본 voxel 공통 12개가 반 크기 voxel에서 34개로 분해되는 점군을 생성합니다."""

    from tools.align.alignment import IcpPointCloud

    common_positions: list[tuple[float, float, float]] = []
    for cell_index in range(12):
        offsets = ((0.125, 0.125, 0.125), (0.75, 0.125, 0.125))
        if cell_index < 10:
            offsets += ((0.125, 0.75, 0.125),)
        common_positions.extend(
            (2.0 * cell_index + offset_x, offset_y, offset_z)
            for offset_x, offset_y, offset_z in offsets
        )
    common = np.asarray(common_positions, dtype=np.float64)
    moving_positions = np.vstack(
        (
            common,
            np.asarray([(100.125 + index, 0.125, 0.125) for index in range(40)], dtype=np.float64),
        )
    )
    reference_positions = np.vstack(
        (
            common,
            np.asarray([(60.125 + index, 0.125, 0.125) for index in range(9)], dtype=np.float64),
        )
    )
    return (
        IcpPointCloud("P_retry", moving_positions, np.arange(moving_positions.shape[0], dtype=np.uint64) + 1),
        IcpPointCloud("A_retry", reference_positions, np.arange(reference_positions.shape[0], dtype=np.uint64) + 10_000),
    )


def _apply(matrix, points: np.ndarray) -> np.ndarray:
    """column-vector 4×4 행렬을 행 점 배열에 적용합니다."""

    return points @ matrix[:3, :3].T + matrix[:3, 3]


def _snapshot(
    *,
    scale_refine: bool,
    initial_scale: float = 1.2,
    true_scale: float = 1.2,
    point_count: int = 180,
    minimum_correspondences: int = 30,
):
    """기본 고정 스케일과 선택적 scale refine 모두에 쓸 ICP 입력 snapshot을 생성합니다."""

    from tools.align.alignment import (
        IcpInputSnapshot,
        IcpLevelParameters,
        IcpParameters,
        IcpPointCloud,
        IcpPreset,
        IcpScaleMode,
        create_sim3_transform,
    )

    source = _source_points(point_count)
    true = create_sim3_transform(
        _rotation_z(13.0),
        true_scale,
        np.asarray((2.0, -1.5, 0.7), dtype=np.float64),
    )
    # Marker initial 값은 같은 scale 또는 작은 scale 오차만 갖고 ICP 수렴 범위 안에 둡니다.
    initial = create_sim3_transform(
        _rotation_z(8.0),
        initial_scale,
        np.asarray((1.85, -1.35, 0.65), dtype=np.float64),
    )
    reference = _apply(true.matrix_float64, source)
    ids = np.arange(source.shape[0], dtype=np.uint64) + np.uint64(100)
    parameters = IcpParameters(
        preset=IcpPreset.FAST,
        scale_mode=IcpScaleMode.REFINE if scale_refine else IcpScaleMode.LOCKED,
        levels=(
            IcpLevelParameters("coarse", 120, 160, 10, 0.90, 2),
            # 기존 Kabsch 회귀는 목적함수 전환과 분리해 명시적으로 point-to-point를 고정합니다.
            IcpLevelParameters(
                "fine",
                180,
                180,
                8,
                0.90,
                2,
                objective_kind="POINT_TO_POINT",
            ),
        ),
        minimum_correspondences=minimum_correspondences,
        overlap_minimum_common_cells=min(5, minimum_correspondences),
        overlap_minimum_moving_coverage=0.05,
        scale_refine_min_inliers=50,
        scale_refine_min_fitness=0.30,
        scale_refine_min_reciprocal=0.40,
        scale_refine_min_overlap_coverage=0.30,
    )
    return IcpInputSnapshot(
        moving_model_id="P",
        reference_model_ids=("A",),
        moving_cloud=IcpPointCloud("P", source, ids),
        reference_clouds=(IcpPointCloud("A", reference, ids + np.uint64(10000)),),
        initial_alignment=initial,
        marker_uniform_scale=initial.uniform_scale,
        marker_max_error=0.20,
        marker_rms=0.01,
        marker_reference_project=reference[:5],
        marker_moving_project=source[:5],
        parameters=parameters,
        marker_signature="synthetic-marker-signature",
        model_revision_snapshot=(("A", 1), ("P", 1)),
    ), true


def _brute_factory():
    """작은 synthetic fixture에 사용할 순수 NumPy NN index를 생성합니다."""

    from tools.align.alignment import BruteForceNearestNeighborIndex

    return BruteForceNearestNeighborIndex(query_chunk_size=31)


def test_kabsch_and_bounded_scale_solver_recover_known_transform() -> None:
    """rigid Kabsch와 제한 Umeyama scale이 known transform을 복원하는지 확인합니다."""

    from tools.align.alignment import solve_bounded_scale_delta, solve_rigid_delta

    moving = _source_points(80)
    rigid_target = moving @ _rotation_z(31.0).T + np.asarray((4.0, -2.0, 1.0))
    rigid = solve_rigid_delta(moving, rigid_target)
    assert np.allclose(_apply(rigid.matrix_float64, moving), rigid_target, rtol=0.0, atol=1.0e-9)
    scaled_target = 1.03 * (moving @ _rotation_z(-7.0).T) + np.asarray((-1.0, 2.0, 0.5))
    scaled = solve_bounded_scale_delta(moving, scaled_target, (0.99, 1.04))
    assert abs(scaled.uniform_scale - 1.03) <= 1.0e-9
    assert np.allclose(_apply(scaled.matrix_float64, moving), scaled_target, rtol=0.0, atol=1.0e-9)


def test_point_to_plane_solver_reduces_conditioned_surface_residual() -> None:
    """완전 rank의 비대칭 표면 대응점에서 point-to-plane 선형계가 잔차를 줄이는지 확인합니다."""

    from tools.align.alignment import solve_point_to_plane_delta

    generator = np.random.default_rng(20_260_816)
    reference = generator.normal(size=(96, 3)).astype(np.float64)
    normals = generator.normal(size=(96, 3)).astype(np.float64)
    normals /= np.linalg.norm(normals, axis=1, keepdims=True)
    moving = reference - np.asarray((0.03, -0.02, 0.015), dtype=np.float64)
    before = np.einsum("ij,ij->i", normals, moving - reference, optimize=True)
    delta, rank, condition, _plane_rmse = solve_point_to_plane_delta(
        moving,
        reference,
        normals,
        damping=1.0e-9,
        condition_limit=1.0e12,
        robust_kernel_scale=1.345,
    )
    after_positions = _apply(delta.matrix_float64, moving)
    after = np.einsum("ij,ij->i", normals, after_positions - reference, optimize=True)
    assert rank == 6
    assert np.isfinite(condition)
    assert np.sqrt(np.mean(np.square(after))) < np.sqrt(np.mean(np.square(before))) * 0.05


def test_kabsch_rank_tolerance_and_multiscale_are_scale_invariant() -> None:
    """1e-8·일반·거대 좌표 스케일의 비공선 대응점이 같은 rigid rank 판정을 받는지 확인합니다."""

    from tools.align.alignment import (
        IcpInputSnapshot,
        IcpLevelParameters,
        IcpParameters,
        IcpPointCloud,
        IcpPreset,
        IcpScaleMode,
        create_sim3_transform,
        solve_multiscale_icp,
        solve_rigid_delta,
    )

    rank_points = np.asarray(
        ((0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)),
        dtype=np.float64,
    )
    base_points = _source_points(64)
    for scale in (1.0e-8, 1.0, 1.0e8):
        expected = create_sim3_transform(
            _rotation_z(7.0),
            1.0,
            # 좌표 스케일만 바꾸고 회전의 공분산 rank 판정을 직접 비교합니다.
            np.zeros(3, dtype=np.float64),
        )
        moving_rank = rank_points * scale
        reference_rank = _apply(expected.matrix_float64, moving_rank)
        rigid = solve_rigid_delta(moving_rank, reference_rank)
        tolerance = max(scale * 1.0e-6, 1.0e-18)
        assert np.allclose(
            _apply(rigid.matrix_float64, moving_rank),
            reference_rank,
            rtol=1.0e-8,
            atol=tolerance,
        )

        moving = base_points * scale
        reference = _apply(expected.matrix_float64, moving)

        ids = np.arange(moving.shape[0], dtype=np.uint64) + np.uint64(40_000)
        parameters = IcpParameters(
            preset=IcpPreset.FAST,
            scale_mode=IcpScaleMode.LOCKED,
            levels=(
                IcpLevelParameters("coarse", 48, 64, 3, 0.90, 2),
                IcpLevelParameters("fine", 64, 64, 2, 0.90, 2),
            ),
            minimum_correspondences=30,
            overlap_minimum_common_cells=3,
            overlap_minimum_moving_coverage=0.01,
        )
        snapshot = IcpInputSnapshot(
            moving_model_id="P",
            reference_model_ids=("A",),
            moving_cloud=IcpPointCloud("P", moving, ids),
            reference_clouds=(IcpPointCloud("A", reference, ids + np.uint64(10_000)),),
            initial_alignment=expected,
            marker_uniform_scale=1.0,
            marker_max_error=scale * 0.01,
            marker_rms=scale * 0.001,
            marker_reference_project=reference[:5],
            marker_moving_project=moving[:5],
            parameters=parameters,
            marker_signature=f"scale-rank-{scale}",
            model_revision_snapshot=(("A", 1), ("P", 1)),
        )
        result = solve_multiscale_icp(snapshot, index_factory=_brute_factory)
        assert result.quality.status.value != "REJECT"
        assert np.allclose(
            _apply(result.final_alignment.matrix_float64, moving),
            reference,
            rtol=1.0e-8,
            atol=tolerance,
        )


def test_correspondence_filter_keeps_unique_targets_and_rejects_collinear_input() -> None:
    """고유 target 필터와 공선 Kabsch 퇴화 거부를 확인합니다."""

    from tools.align.alignment import (
        IcpDegeneracyError,
        NearestNeighborQueryResult,
        filter_correspondences,
        solve_rigid_delta,
    )

    result = NearestNeighborQueryResult(
        np.asarray((0, 0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18,
                    19, 20, 21, 22, 23, 24, 25, 26, 27, 28, 29), dtype=np.int64),
        np.linspace(0.001, 0.05, 31, dtype=np.float64),
    )
    filtered = filter_correspondences(
        result,
        maximum_distance=0.1,
        voxel_size=0.01,
        trim_fraction=1.0,
        minimum_correspondences=30,
    )
    assert filtered.unique_target_count == 30
    assert filtered.inlier_count == 30
    collinear = np.column_stack((np.arange(30, dtype=np.float64), np.zeros(30), np.zeros(30)))
    try:
        solve_rigid_delta(collinear, collinear)
    except IcpDegeneracyError:
        pass
    else:
        raise AssertionError("공선 correspondence를 Kabsch가 허용했습니다.")


def test_multiscale_locked_scale_recovers_rigid_delta_and_reports_stable_progress() -> None:
    """Scale Locked ICP가 marker scale을 보존하고 레벨 완료 안정 행렬만 보고하는지 확인합니다."""

    from tools.align.alignment import solve_multiscale_icp

    snapshot, true = _snapshot(scale_refine=False)
    progress = []
    result = solve_multiscale_icp(
        snapshot,
        progress_sink=progress.append,
        index_factory=_brute_factory,
    )
    assert abs(result.final_alignment.uniform_scale - snapshot.initial_alignment.uniform_scale) <= 1.0e-9
    assert np.allclose(result.final_alignment.matrix_float64, true.matrix_float64, rtol=0.0, atol=2.0e-3)
    assert result.quality.status.value in {"PASS", "REVIEW"}
    assert tuple(item.model_id for item in result.quality.per_reference_model) == ("A",)
    stable = [item for item in progress if item.stable_alignment_float64 is not None]
    # v2는 레벨 완료 전에도 검증된 best 후보만 stable alignment로 전달합니다.
    assert stable and any(item.level_complete for item in stable)


def test_hybrid_levels_emit_point_to_plane_metrics_and_fixed_candidate_distance() -> None:
    """coarse P2P 뒤 medium·fine P2Plane이 고정 평가 지표와 함께 실행되는지 확인합니다."""

    from tools.align.alignment import (
        IcpAlignmentResult,
        IcpLevelParameters,
        IcpObjectiveKind,
        IcpParameters,
        IcpPreset,
        IcpScaleMode,
        solve_multiscale_icp,
    )

    snapshot, _true = _snapshot(scale_refine=False)
    parameters = IcpParameters(
        preset=IcpPreset.FAST,
        scale_mode=IcpScaleMode.LOCKED,
        levels=(
            IcpLevelParameters("coarse", 120, 160, 5, 0.90, 2),
            IcpLevelParameters(
                "medium",
                160,
                180,
                5,
                0.90,
                2,
                normal_minimum_neighbors=6,
                normal_maximum_neighbors=24,
                normal_minimum_planarity=0.0,
                normal_maximum_curvature=1.0,
            ),
            IcpLevelParameters(
                "fine",
                180,
                180,
                5,
                0.90,
                2,
                normal_minimum_neighbors=6,
                normal_maximum_neighbors=24,
                normal_minimum_planarity=0.0,
                normal_maximum_curvature=1.0,
            ),
        ),
        minimum_correspondences=30,
        overlap_minimum_common_cells=5,
        overlap_minimum_moving_coverage=0.05,
    )
    progress = []
    result = solve_multiscale_icp(
        replace(snapshot, parameters=parameters, input_fingerprint=""),
        progress_sink=progress.append,
        index_factory=_brute_factory,
    )
    objectives = tuple(item.objective_kind for item in result.level_results)
    assert objectives[0] is IcpObjectiveKind.POINT_TO_POINT
    assert IcpObjectiveKind.POINT_TO_PLANE in objectives
    assert any(item.stage == "ICP_MEDIUM_POINT_TO_PLANE" for item in progress)
    assert result.best_candidate is not None
    assert result.best_candidate_metrics is not None
    assert np.isclose(
        result.best_candidate.maximum_correspondence_distance,
        result.best_candidate_metrics.maximum_correspondence_distance,
        rtol=0.0,
        atol=0.0,
    )
    assert result.best_candidate.evaluation_fingerprint == result.best_candidate_metrics.evaluation_fingerprint
    restored = IcpAlignmentResult.from_mapping(result.to_mapping())
    assert restored.best_candidate is not None
    assert restored.best_candidate.to_mapping() == result.best_candidate.to_mapping()


def test_normal_coverage_returns_review_but_point_to_plane_rank_fails() -> None:
    """노멀 부족은 coarse 최선 후보 REVIEW로, rank 부족은 typed 실패로 끝나는지 확인합니다."""

    from tools.align.alignment import (
        IcpDegeneracyError,
        IcpLevelParameters,
        IcpParameters,
        IcpPreset,
        IcpQualityStatus,
        IcpScaleMode,
        IcpSolverStopReason,
        solve_multiscale_icp,
    )

    snapshot, _true = _snapshot(scale_refine=False)
    coverage_parameters = IcpParameters(
        preset=IcpPreset.FAST,
        scale_mode=IcpScaleMode.LOCKED,
        levels=(
            IcpLevelParameters("coarse", 120, 160, 4, 0.90, 2),
            IcpLevelParameters(
                "medium",
                160,
                180,
                4,
                0.90,
                2,
                normal_minimum_neighbors=300,
                normal_maximum_neighbors=300,
            ),
        ),
        minimum_correspondences=30,
        overlap_minimum_common_cells=5,
        overlap_minimum_moving_coverage=0.05,
    )
    reviewed = solve_multiscale_icp(
        replace(snapshot, parameters=coverage_parameters, input_fingerprint=""),
        index_factory=_brute_factory,
    )
    assert reviewed.quality.status is IcpQualityStatus.REVIEW
    assert reviewed.solver_stop_reason is IcpSolverStopReason.NORMAL_COVERAGE_INSUFFICIENT
    assert len(reviewed.level_results) == 1

    rank_parameters = IcpParameters(
        preset=IcpPreset.FAST,
        scale_mode=IcpScaleMode.LOCKED,
        levels=(
            IcpLevelParameters("coarse", 120, 160, 4, 0.90, 2),
            IcpLevelParameters(
                "medium",
                160,
                180,
                4,
                0.90,
                2,
                normal_minimum_neighbors=6,
                normal_maximum_neighbors=24,
                normal_minimum_planarity=0.0,
                normal_maximum_curvature=1.0,
            ),
        ),
        minimum_correspondences=30,
        overlap_minimum_common_cells=5,
        overlap_minimum_moving_coverage=0.05,
    )
    with patch(
        "tools.align.alignment.icp_solver.solve_point_to_plane_delta",
        side_effect=IcpDegeneracyError("테스트용 rank 부족"),
    ):
        try:
            solve_multiscale_icp(
                replace(snapshot, parameters=rank_parameters, input_fingerprint=""),
                index_factory=_brute_factory,
            )
        except IcpDegeneracyError:
            pass
        else:
            raise AssertionError("point-to-plane rank 부족을 REVIEW 결과로 바꿨습니다.")


def test_normal_gate_only_correspondence_shortage_is_normal_coverage() -> None:
    """거리 대응점은 충분하지만 노멀 gate 뒤 MAD만 부족하면 노멀 부족으로 분류합니다."""

    from tools.align.alignment import (
        IcpNormalCoverageError,
        IcpObjectiveKind,
        NearestNeighborQueryResult,
        filter_hybrid_correspondences,
    )

    query_count = 40
    reference_indices = np.arange(query_count, dtype=np.int64)
    distances = np.full(query_count, 0.1, dtype=np.float64)
    distances[29] = 10.0
    moving_normals = np.tile(np.asarray((1.0, 0.0, 0.0)), (query_count, 1))
    reference_normals = moving_normals.copy()
    reference_normals[30:] = np.asarray((0.0, 1.0, 0.0))
    try:
        filter_hybrid_correspondences(
            NearestNeighborQueryResult(reference_indices, distances),
            maximum_distance=20.0,
            voxel_size=0.01,
            trim_fraction=1.0,
            minimum_correspondences=30,
            mad_multiplier=3.0,
            objective_kind=IcpObjectiveKind.POINT_TO_PLANE,
            moving_normals_float64=moving_normals,
            reference_normals_float64=reference_normals,
            maximum_angle_degrees=10.0,
        )
    except IcpNormalCoverageError:
        pass
    else:
        raise AssertionError("노멀 gate 때문에만 부족해진 대응점을 일반 기하 성공으로 처리했습니다.")


def test_initial_project_world_frame_keeps_final_delta_times_initial_contract() -> None:
    """worker가 initial-world memmap을 넘겨도 최종 결과가 delta × initial 계약을 유지하는지 확인합니다."""

    from tools.align.alignment import (
        IcpInputSnapshot,
        IcpMovingCloudFrame,
        IcpPointCloud,
        solve_multiscale_icp,
    )

    source_snapshot, true = _snapshot(scale_refine=False)
    initial_world = _apply(
        source_snapshot.initial_alignment.matrix_float64,
        source_snapshot.moving_cloud.positions_float64,
    )
    world_snapshot = IcpInputSnapshot(
        moving_model_id=source_snapshot.moving_model_id,
        reference_model_ids=source_snapshot.reference_model_ids,
        moving_cloud=IcpPointCloud(
            "P",
            initial_world,
            source_snapshot.moving_cloud.point_ids_uint64,
        ),
        reference_clouds=source_snapshot.reference_clouds,
        initial_alignment=source_snapshot.initial_alignment,
        marker_uniform_scale=source_snapshot.marker_uniform_scale,
        marker_max_error=source_snapshot.marker_max_error,
        marker_rms=source_snapshot.marker_rms,
        marker_reference_project=source_snapshot.marker_reference_project,
        marker_moving_project=source_snapshot.marker_moving_project,
        parameters=source_snapshot.parameters,
        marker_signature=source_snapshot.marker_signature,
        model_revision_snapshot=source_snapshot.model_revision_snapshot,
        moving_cloud_frame=IcpMovingCloudFrame.INITIAL_PROJECT_WORLD,
    )
    result = solve_multiscale_icp(world_snapshot, index_factory=_brute_factory)
    assert np.allclose(result.final_alignment.matrix_float64, true.matrix_float64, rtol=0.0, atol=2.0e-3)


def test_scale_refine_accepts_small_marker_scale_error() -> None:
    """고정 결과가 충분할 때만 후행 ±5% scale refinement가 small marker scale 오차를 개선하는지 확인합니다."""

    from tools.align.alignment import solve_multiscale_icp

    snapshot, true = _snapshot(scale_refine=True, initial_scale=1.17, true_scale=1.20)
    result = solve_multiscale_icp(snapshot, index_factory=_brute_factory)
    assert abs(result.final_alignment.uniform_scale - true.uniform_scale) <= 3.0e-3
    assert result.quality.scale_refine_status.value == "ACCEPTED"
    assert result.quality.scale_ratio <= 1.05 + 1.0e-9
    assert result.quality.scale_ratio >= 0.95 - 1.0e-9


def test_blender_default_solver_index_is_mathutils_kdtree() -> None:
    """Blender 5.2 worker 기본 최근접점 검색이 brute-force가 아닌 KD-tree인지 확인합니다."""

    from tools.align.alignment import MathutilsKdTreeIndex, default_nearest_neighbor_index

    index = default_nearest_neighbor_index()
    try:
        assert isinstance(index, MathutilsKdTreeIndex)
    finally:
        index.close()


def test_default_solver_index_fails_when_mathutils_kdtree_is_unavailable() -> None:
    """실행 worker는 KD-tree 부재 시 BruteForce로 숨은 대체하지 않는지 확인합니다."""

    import builtins

    from tools.align.alignment import IcpInputError, default_nearest_neighbor_index

    original_import = builtins.__import__

    def _reject_mathutils(name, globals=None, locals=None, fromlist=(), level=0):
        """이 테스트 범위에서만 KD-tree import 부재를 재현합니다."""

        if name in {"mathutils", "mathutils.kdtree"}:
            raise ImportError("테스트용 KD-tree 부재")
        return original_import(name, globals, locals, fromlist, level)

    with patch("builtins.__import__", side_effect=_reject_mathutils):
        try:
            default_nearest_neighbor_index()
        except IcpInputError as error:
            assert "KDTree" in str(error)
        else:
            raise AssertionError("KD-tree 부재에서 BruteForce 대체를 허용했습니다.")


def test_large_world_origin_rotation_uses_local_translation_metrics() -> None:
    """원점 1e6의 1도 회전과 local t=0이 이동 안전 한도·품질을 오판하지 않는지 확인합니다."""

    from tools.align.alignment import (
        IcpInputSnapshot,
        IcpLevelParameters,
        IcpParameters,
        IcpPointCloud,
        IcpPreset,
        IcpScaleMode,
        create_sim3_transform,
        identity_sim3_transform,
        solve_multiscale_icp,
    )
    from tools.align.alignment.icp_solver import _local_delta_to_world

    origin = np.asarray((1_000_000.0, -1_000_000.0, 500_000.0), dtype=np.float64)
    rotation = _rotation_z(1.0)
    # local t=0인 회전은 월드 행렬 translation 열만 크게 만들며 실제 local 이동은 없습니다.
    local_delta = create_sim3_transform(rotation, 1.0, np.zeros(3, dtype=np.float64))
    world_delta = _local_delta_to_world(local_delta, origin)
    assert np.linalg.norm(world_delta.translation_float64) > 10_000.0

    moving = _source_points(180) + origin[np.newaxis, :]
    true = create_sim3_transform(rotation, 1.0, origin - rotation @ origin)
    reference = _apply(true.matrix_float64, moving)
    ids = np.arange(moving.shape[0], dtype=np.uint64) + np.uint64(20_000)
    parameters = IcpParameters(
        preset=IcpPreset.FAST,
        scale_mode=IcpScaleMode.LOCKED,
        levels=(
            # 첫 Kabsch 회전에서 world translation 안전 한도가 오판되지 않는지를 격리합니다.
            IcpLevelParameters("coarse", 120, 160, 1, 0.90, 2),
            IcpLevelParameters(
                "fine",
                180,
                180,
                1,
                0.90,
                2,
                objective_kind="POINT_TO_POINT",
            ),
        ),
        overlap_minimum_common_cells=5,
        overlap_minimum_moving_coverage=0.05,
    )
    snapshot = IcpInputSnapshot(
        moving_model_id="P",
        reference_model_ids=("A",),
        moving_cloud=IcpPointCloud("P", moving, ids),
        reference_clouds=(IcpPointCloud("A", reference, ids + np.uint64(10_000)),),
        initial_alignment=identity_sim3_transform(),
        marker_uniform_scale=1.0,
        marker_max_error=0.20,
        marker_rms=0.01,
        marker_reference_project=reference[:5],
        marker_moving_project=moving[:5],
        parameters=parameters,
        marker_signature="large-world-origin-marker",
        model_revision_snapshot=(("A", 1), ("P", 1)),
    )
    result = solve_multiscale_icp(snapshot, index_factory=_brute_factory)
    assert np.allclose(result.final_alignment.matrix_float64, true.matrix_float64, rtol=0.0, atol=2.0e-3)
    assert result.quality.delta_translation < 1.0
    assert "추가 이동" not in " ".join(result.quality.review_reasons)


def test_rmse_noise_floor_ignores_exact_alignment_roundoff_but_keeps_real_divergence() -> None:
    """정확 정렬의 1e-15 흔들림은 수렴으로, voxel 규모 증가는 발산으로 판정하는지 확인합니다."""

    from tools.align.alignment.icp_solver import _is_significant_rmse_increase

    assert not _is_significant_rmse_increase(
        3.0e-15,
        4.5e-15,
        voxel_size=0.1,
        overlap_diagonal=10.0,
    )
    assert _is_significant_rmse_increase(
        0.01,
        0.02,
        voxel_size=0.1,
        overlap_diagonal=10.0,
    )


def test_overlap_crop_and_level_guards_use_actual_local_geometry() -> None:
    """원거리 A/P outlier가 crop 대각선·inlier 급감·trim 경계를 부풀리지 않는지 확인합니다."""

    from tools.align.alignment import AxisAlignedBounds, IcpPointCloud, bounds_from_cloud
    from tools.align.alignment.icp_solver import (
        _effective_trim_fraction,
        _has_excessive_level_inlier_drop,
        _reference_crop_bounds,
    )
    from tools.align.alignment import IcpLevelParameters

    near = np.asarray(
        ((0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)),
        dtype=np.float64,
    )
    reference = IcpPointCloud(
        "A",
        np.vstack((near, np.asarray(((10_000.0, 0.0, 0.0),), dtype=np.float64))),
        np.arange(5, dtype=np.uint64) + np.uint64(1_000),
    )
    moving = IcpPointCloud(
        "P",
        np.vstack((near, np.asarray(((-10_000.0, 0.0, 0.0),), dtype=np.float64))),
        np.arange(5, dtype=np.uint64) + np.uint64(2_000),
    )
    moving_bounds = AxisAlignedBounds(np.zeros(3), np.ones(3))
    candidate_bounds, selected_bounds = _reference_crop_bounds(moving_bounds, (reference,), 0.25)
    assert selected_bounds.maximum_float64[0] < 2.0
    moving_crop = selected_bounds.expanded(0.25)
    cropped_moving = bounds_from_cloud(moving, crop_bounds=moving_crop)
    assert cropped_moving.minimum_float64[0] >= -0.25
    assert candidate_bounds["A"][1][0] <= 1.25
    level = IcpLevelParameters("coarse", 30, 30, 2, 0.80)
    assert np.isclose(_effective_trim_fraction(level, 0.20), 0.40)
    assert np.isclose(_effective_trim_fraction(level, 0.95), 0.80)
    assert _has_excessive_level_inlier_drop(100, 19)
    assert not _has_excessive_level_inlier_drop(100, 20)


def test_level_inlier_drop_returns_review_preview_with_rms() -> None:
    """레벨 간 inlier 급감이 결과를 폐기하지 않고 RMS가 있는 REVIEW를 반환하는지 확인합니다."""

    from tools.align.alignment import IcpQualityStatus, IcpSolverStopReason, solve_multiscale_icp

    snapshot, _true = _snapshot(scale_refine=False)
    with patch(
        "tools.align.alignment.icp_solver._has_excessive_level_inlier_drop",
        return_value=True,
    ):
        reviewed = solve_multiscale_icp(snapshot, index_factory=_brute_factory)
    assert reviewed.quality.status is IcpQualityStatus.REVIEW
    assert reviewed.quality.user_review_required is True
    assert reviewed.solver_stop_reason is IcpSolverStopReason.LEVEL_INLIER_DROP
    assert reviewed.quality.rmse > 0.0
    assert reviewed.best_candidate_metrics is not None
    assert len(reviewed.level_results) == 2


def test_overlap_search_refines_voxel_only_for_real_signal() -> None:
    """기본 격자 신호만 최대 네 단계 finer voxel로 재평가하고 약한·무겹침 후보는 거부하는지 확인합니다."""

    from tools.align.alignment import IcpInputError, IcpPointCloud
    from tools.align.alignment.icp_solver import _find_overlap_candidates_with_refined_voxel

    moving, reference = _overlap_retry_clouds()
    candidates, voxel_size = _find_overlap_candidates_with_refined_voxel(
        moving,
        (reference,),
        maximum_distance=0.1,
        base_voxel_size=1.0,
        minimum_common_cells=32,
        minimum_moving_coverage=0.01,
        maximum_occupancy_cells=10_000,
        cancel_probe=None,
    )
    assert np.isclose(voxel_size, 0.5)
    assert candidates[0].common_cell_count == 34
    assert candidates[0].effective_required_common_cells == 32
    assert candidates[0].selected is True

    base_reference = IcpPointCloud(
        "A_base",
        moving.positions_float64,
        np.arange(moving.count, dtype=np.uint64) + 15_000,
    )
    multi_candidates, multi_voxel_size = _find_overlap_candidates_with_refined_voxel(
        moving,
        (base_reference, reference),
        maximum_distance=0.1,
        base_voxel_size=1.0,
        minimum_common_cells=32,
        minimum_moving_coverage=0.01,
        maximum_occupancy_cells=10_000,
        cancel_probe=None,
    )
    multi_by_id = {candidate.reference_model_id: candidate for candidate in multi_candidates}
    assert np.isclose(multi_voxel_size, 0.5)
    assert multi_by_id["A_base"].selected is True
    assert multi_by_id["A_retry"].common_cell_count == 34
    assert multi_by_id["A_retry"].selected is True

    one_common_positions = np.vstack(
        (
            np.asarray(((0.125, 0.125, 0.125),), dtype=np.float64),
            np.asarray([(100.125 + index, 0.125, 0.125) for index in range(51)], dtype=np.float64),
        )
    )
    one_common_moving = IcpPointCloud(
        "P_one",
        one_common_positions,
        np.arange(one_common_positions.shape[0], dtype=np.uint64) + 20_000,
    )
    one_common_reference = IcpPointCloud(
        "A_one",
        one_common_positions[:1],
        np.asarray((30_000,), dtype=np.uint64),
    )
    weak_candidates, weak_voxel_size = _find_overlap_candidates_with_refined_voxel(
        one_common_moving,
        (one_common_reference,),
        maximum_distance=0.1,
        base_voxel_size=1.0,
        minimum_common_cells=32,
        minimum_moving_coverage=0.01,
        maximum_occupancy_cells=10_000,
        cancel_probe=None,
    )
    assert np.isclose(weak_voxel_size, 1.0 / 16.0)
    assert weak_candidates[0].common_cell_count == 1
    assert weak_candidates[0].selected is False

    distant_reference = IcpPointCloud(
        "A_distant",
        np.asarray(((10_000.0, 0.125, 0.125),), dtype=np.float64),
        np.asarray((40_000,), dtype=np.uint64),
    )
    distant_candidates, distant_voxel_size = _find_overlap_candidates_with_refined_voxel(
        one_common_moving,
        (distant_reference,),
        maximum_distance=0.1,
        base_voxel_size=1.0,
        minimum_common_cells=32,
        minimum_moving_coverage=0.01,
        maximum_occupancy_cells=10_000,
        cancel_probe=None,
    )
    assert np.isclose(distant_voxel_size, 1.0)
    assert distant_candidates[0].aabb_intersects is False
    assert distant_candidates[0].selected is False

    try:
        _find_overlap_candidates_with_refined_voxel(
            moving,
            (reference,),
            maximum_distance=0.1,
            base_voxel_size=1.0,
            minimum_common_cells=32,
            minimum_moving_coverage=0.01,
            maximum_occupancy_cells=1,
            cancel_probe=None,
        )
    except IcpInputError:
        pass
    else:
        raise AssertionError("overlap 점유 cell 한도 초과를 숨겼습니다.")


def test_strict_numeric_public_boundaries_reject_bool_and_strings() -> None:
    """worker JSON 수치 경계가 bool·문자열의 암묵 변환을 허용하지 않는지 확인합니다."""

    from tools.align.alignment import IcpInputError, IcpLevelParameters, IcpParameters, NearestNeighborQueryResult
    from tools.align.alignment.icp_solver import filter_correspondences

    try:
        IcpLevelParameters("coarse", 30, 30, 3, True)
    except IcpInputError:
        pass
    else:
        raise AssertionError("bool trim_fraction을 ICP 레벨 설정이 허용했습니다.")
    try:
        IcpParameters(mad_multiplier="3.0")
    except IcpInputError:
        pass
    else:
        raise AssertionError("문자열 MAD 배수를 ICP 설정이 허용했습니다.")
    query = NearestNeighborQueryResult(
        np.arange(3, dtype=np.int64),
        np.asarray((0.01, 0.02, 0.03), dtype=np.float64),
    )
    try:
        filter_correspondences(
            query,
            maximum_distance=True,
            voxel_size=0.1,
            trim_fraction=1.0,
            minimum_correspondences=3,
        )
    except IcpInputError:
        pass
    else:
        raise AssertionError("bool 최대 대응 거리를 ICP 필터가 허용했습니다.")


def test_cancel_and_quality_reject_do_not_return_preview_result() -> None:
    """취소와 최종 REJECT가 성공 결과를 반환하지 않는 typed 오류인지 확인합니다."""

    from tools.align.alignment import IcpCancelledError, IcpQualityRejectedError, solve_multiscale_icp

    snapshot, _true = _snapshot(scale_refine=False)
    try:
        solve_multiscale_icp(snapshot, index_factory=_brute_factory, cancel_probe=lambda: True)
    except IcpCancelledError:
        pass
    else:
        raise AssertionError("ICP cancel이 성공 결과를 반환했습니다.")
    # 30개 미만 fixture는 solver 반복은 통과해도 마지막 품질에서 preview를 만들지 않아야 합니다.
    short_snapshot, _true = _snapshot(
        scale_refine=False,
        point_count=29,
        minimum_correspondences=3,
    )
    try:
        solve_multiscale_icp(short_snapshot, index_factory=_brute_factory)
    except IcpQualityRejectedError as error:
        assert error.quality is not None
        assert "최종 대응점이 30개 미만입니다." in error.quality.rejection_reasons
    else:
        raise AssertionError("최종 REJECT 품질의 ICP가 preview 성공 결과를 반환했습니다.")


def test_cancel_and_low_evidence_quality_returns_review_best_candidate() -> None:
    """취소는 오류로 끝내고 낮은 근거의 유효 후보는 REVIEW로 보존하는지 확인합니다."""

    from tools.align.alignment import (
        IcpCancelledError,
        IcpInputError,
        IcpParameters,
        IcpQualityStatus,
        solve_multiscale_icp,
    )

    snapshot, _true = _snapshot(scale_refine=False)
    try:
        solve_multiscale_icp(snapshot, index_factory=_brute_factory, cancel_probe=lambda: True)
    except IcpCancelledError:
        pass
    else:
        raise AssertionError("ICP 취소가 성공 결과를 반환했습니다.")
    short_snapshot, _true = _snapshot(
        scale_refine=False,
        point_count=29,
        minimum_correspondences=3,
    )
    reviewed = solve_multiscale_icp(short_snapshot, index_factory=_brute_factory)
    assert reviewed.quality.status is IcpQualityStatus.REVIEW
    assert reviewed.quality.user_review_required is True
    assert reviewed.best_candidate is not None
    assert "30개 미만" in " ".join(reviewed.quality.review_reasons)
    try:
        IcpParameters(best_candidate_enabled=False)
    except IcpInputError:
        pass
    else:
        raise AssertionError("최선 후보 보존 비활성화를 허용했습니다.")


def run_all() -> None:
    """ICP solver 순수 회귀를 Blender 프로세스에서 순서대로 실행합니다."""

    _add_source_path()
    test_kabsch_and_bounded_scale_solver_recover_known_transform()
    test_point_to_plane_solver_reduces_conditioned_surface_residual()
    test_kabsch_rank_tolerance_and_multiscale_are_scale_invariant()
    test_correspondence_filter_keeps_unique_targets_and_rejects_collinear_input()
    test_multiscale_locked_scale_recovers_rigid_delta_and_reports_stable_progress()
    test_hybrid_levels_emit_point_to_plane_metrics_and_fixed_candidate_distance()
    test_normal_coverage_returns_review_but_point_to_plane_rank_fails()
    test_normal_gate_only_correspondence_shortage_is_normal_coverage()
    test_initial_project_world_frame_keeps_final_delta_times_initial_contract()
    test_scale_refine_accepts_small_marker_scale_error()
    test_blender_default_solver_index_is_mathutils_kdtree()
    test_default_solver_index_fails_when_mathutils_kdtree_is_unavailable()
    test_large_world_origin_rotation_uses_local_translation_metrics()
    test_rmse_noise_floor_ignores_exact_alignment_roundoff_but_keeps_real_divergence()
    test_overlap_crop_and_level_guards_use_actual_local_geometry()
    test_level_inlier_drop_returns_review_preview_with_rms()
    test_overlap_search_refines_voxel_only_for_real_signal()
    test_strict_numeric_public_boundaries_reject_bool_and_strings()
    test_cancel_and_low_evidence_quality_returns_review_best_candidate()
    print("test_icp_solver: 통과")


if __name__ == "__main__":
    run_all()
