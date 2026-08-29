"""ICP AABB·점유 그리드 자동 겹침 후보를 Blender 환경에서 검증합니다."""

from __future__ import annotations

from pathlib import Path
import sys

import numpy as np


def _add_source_path() -> None:
    """애드온 소스 디렉터리를 Blender 테스트 모듈 경로에 추가합니다."""

    source_path = Path(__file__).resolve().parents[3]
    source_text = str(source_path)
    if source_text not in sys.path:
        sys.path.insert(0, source_text)


def _grid_cloud(model_id: str, offset: tuple[float, float, float]):
    """점유 cell overlap을 명확히 검토할 3차원 격자 A 점군을 생성합니다."""

    from tools.align.alignment import IcpPointCloud

    coordinates = np.asarray(
        [
            (float(x) + offset[0], float(y) + offset[1], float(z) + offset[2])
            for x in range(5)
            for y in range(5)
            for z in range(3)
        ],
        dtype=np.float64,
    )
    ids = np.arange(coordinates.shape[0], dtype=np.uint64) + np.uint64(1000)
    return IcpPointCloud(model_id, coordinates, ids)


def _sparse_line_cloud(model_id: str, x_coordinates: np.ndarray, *, y_coordinate: float = 0.0):
    """점유 cell 수를 정확히 제어할 수 있는 희소 선형 점군을 생성합니다."""

    from tools.align.alignment import IcpPointCloud

    x_values = np.asarray(x_coordinates, dtype=np.float64)
    positions = np.column_stack(
        (
            x_values,
            np.full(x_values.shape[0], y_coordinate, dtype=np.float64),
            np.zeros(x_values.shape[0], dtype=np.float64),
        )
    )
    point_ids = np.arange(1, x_values.shape[0] + 1, dtype=np.uint64)
    return IcpPointCloud(model_id, positions, point_ids)


def _coarse_to_fine_overlap_clouds():
    """기본 1.0 voxel의 공통 12개가 0.5 voxel에서 34개로 분해되는 A/P 점군을 생성합니다."""

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
    moving_only = np.asarray(
        [(100.125 + index, 0.125, 0.125) for index in range(40)],
        dtype=np.float64,
    )
    reference_only = np.asarray(
        [(60.125 + index, 0.125, 0.125) for index in range(9)],
        dtype=np.float64,
    )
    moving_positions = np.vstack((common, moving_only))
    reference_positions = np.vstack((common, reference_only))
    return (
        IcpPointCloud("P", moving_positions, np.arange(moving_positions.shape[0], dtype=np.uint64) + 1),
        IcpPointCloud("A", reference_positions, np.arange(reference_positions.shape[0], dtype=np.uint64) + 10_000),
    )


def test_overlap_selects_only_intersecting_reference_model() -> None:
    """AABB와 점유 grid 조건을 만족한 A 모델만 selected인지 확인합니다."""

    from tools.align.alignment import find_overlap_candidates, select_overlap_clouds

    moving = _grid_cloud("P", (0.0, 0.0, 0.0))
    overlapping = _grid_cloud("A_overlap", (0.08, -0.04, 0.02))
    distant = _grid_cloud("A_distant", (100.0, 0.0, 0.0))
    candidates = find_overlap_candidates(
        moving.positions_float64,
        (overlapping, distant),
        maximum_distance=0.2,
        occupancy_voxel_size=1.0,
        minimum_common_cells=10,
        minimum_moving_coverage=0.2,
    )
    by_id = {candidate.reference_model_id: candidate for candidate in candidates}
    assert by_id["A_overlap"].selected is True
    assert by_id["A_overlap"].common_cell_count >= 10
    assert by_id["A_distant"].selected is False
    assert by_id["A_distant"].aabb_intersects is False
    selected = select_overlap_clouds(candidates, (distant, overlapping))
    assert tuple(cloud.model_id for cloud in selected) == ("A_overlap",)


def test_transformed_bounds_handles_rotation_and_expansion() -> None:
    """AABB 여덟 모서리 변환과 닫힌 경계 교집합을 정확히 계산하는지 확인합니다."""

    from tools.align.alignment import (
        AxisAlignedBounds,
        bounds_intersect,
        bounds_intersection,
        create_sim3_transform,
    )
    from tools.align.alignment.icp_overlap import transform_bounds

    source = AxisAlignedBounds(np.asarray((0.0, 0.0, 0.0)), np.asarray((2.0, 1.0, 1.0)))
    rotation = np.asarray(((0.0, -1.0, 0.0), (1.0, 0.0, 0.0), (0.0, 0.0, 1.0)))
    transformed = transform_bounds(
        source,
        create_sim3_transform(rotation, 1.0, np.asarray((5.0, 3.0, 0.0))),
    )
    assert np.allclose(transformed.minimum_float64, np.asarray((4.0, 3.0, 0.0)))
    assert np.allclose(transformed.maximum_float64, np.asarray((5.0, 5.0, 1.0)))
    touching = AxisAlignedBounds(np.asarray((5.0, 5.0, 1.0)), np.asarray((6.0, 6.0, 2.0)))
    assert bounds_intersect(transformed, touching) is True
    assert bounds_intersection(transformed, touching) is not None


def test_overlap_candidate_checks_cancel_between_reference_models() -> None:
    """다중 A 후보 평가가 모델 경계 취소 요청을 ICP 취소 오류로 전달하는지 확인합니다."""

    from tools.align.alignment import IcpCancelledError, find_overlap_candidates

    moving = _grid_cloud("P", (0.0, 0.0, 0.0))
    references = (_grid_cloud("A1", (0.0, 0.0, 0.0)), _grid_cloud("A2", (0.0, 0.0, 0.0)))
    probe_count = {"value": 0}

    def cancel_after_first_probe() -> bool:
        """첫 모델 이후 호출을 취소로 바꾸는 테스트 callback입니다."""

        probe_count["value"] += 1
        return probe_count["value"] > 1

    try:
        find_overlap_candidates(
            moving.positions_float64,
            references,
            maximum_distance=0.2,
            occupancy_voxel_size=1.0,
            minimum_common_cells=5,
            minimum_moving_coverage=0.1,
            cancel_probe=cancel_after_first_probe,
        )
    except IcpCancelledError:
        pass
    else:
        raise AssertionError("다중 A overlap 후보가 취소 요청을 전달하지 않았습니다.")


def test_combined_multi_reference_coverage_uses_union_not_per_model_maximum() -> None:
    """서로 다른 P 영역을 덮는 두 A의 합성 coverage가 각 후보 최대값보다 큰지 확인합니다."""

    from tools.align.alignment import (
        AxisAlignedBounds,
        IcpPointCloud,
        find_overlap_candidates,
    )
    from tools.align.alignment.icp_overlap import combined_moving_overlap_coverage

    moving_positions = np.column_stack(
        (np.arange(10, dtype=np.float64), np.zeros(10), np.zeros(10))
    )
    moving = IcpPointCloud("P", moving_positions, np.arange(10, dtype=np.uint64))
    left = IcpPointCloud("A_left", moving_positions[:5], np.arange(100, 105, dtype=np.uint64))
    right = IcpPointCloud("A_right", moving_positions[5:], np.arange(200, 205, dtype=np.uint64))
    candidates = find_overlap_candidates(
        moving,
        (left, right),
        maximum_distance=0.01,
        occupancy_voxel_size=0.5,
        minimum_common_cells=3,
        minimum_moving_coverage=0.1,
    )
    by_id = {item.reference_model_id: item for item in candidates}
    assert np.isclose(by_id["A_left"].moving_coverage, 0.5)
    assert np.isclose(by_id["A_right"].moving_coverage, 0.5)
    coverage = combined_moving_overlap_coverage(
        moving,
        (left, right),
        moving_crop_bounds=AxisAlignedBounds(
            np.asarray((0.0, 0.0, 0.0)),
            np.asarray((9.0, 0.0, 0.0)),
        ),
        reference_crop_bounds_by_model={
            "A_left": AxisAlignedBounds(np.asarray((0.0, 0.0, 0.0)), np.asarray((4.0, 0.0, 0.0))),
            "A_right": AxisAlignedBounds(np.asarray((5.0, 0.0, 0.0)), np.asarray((9.0, 0.0, 0.0))),
        },
        occupancy_voxel_size=0.5,
        origin_float64=np.zeros(3, dtype=np.float64),
    )
    assert np.isclose(coverage, 1.0)


def test_overlap_keeps_common_cell_gate_and_finer_voxel_resolves_real_signal() -> None:
    """52/21/12 기본 격자는 거부하고 0.5 voxel의 공통 34개만 기존 32 gate를 통과하는지 확인합니다."""

    from tools.align.alignment.icp_overlap import evaluate_overlap_candidate

    moving, reference = _coarse_to_fine_overlap_clouds()
    base_candidate = evaluate_overlap_candidate(
        moving,
        reference,
        maximum_distance=0.1,
        occupancy_voxel_size=1.0,
        minimum_common_cells=32,
        minimum_moving_coverage=0.01,
    )
    assert base_candidate.moving_cell_count == 52
    assert base_candidate.reference_cell_count == 21
    assert base_candidate.common_cell_count == 12
    assert base_candidate.effective_required_common_cells == 32
    assert np.isclose(base_candidate.moving_coverage, 12.0 / 52.0)
    assert base_candidate.selected is False

    finer_candidate = evaluate_overlap_candidate(
        moving,
        reference,
        maximum_distance=0.1,
        occupancy_voxel_size=0.5,
        minimum_common_cells=32,
        minimum_moving_coverage=0.01,
    )
    assert finer_candidate.common_cell_count == 34
    assert finer_candidate.effective_required_common_cells == 32
    assert finer_candidate.selected is True

    coverage_rejected = evaluate_overlap_candidate(
        moving,
        reference,
        maximum_distance=0.1,
        occupancy_voxel_size=0.5,
        minimum_common_cells=1,
        minimum_moving_coverage=0.50,
    )
    assert coverage_rejected.common_cell_count >= coverage_rejected.effective_required_common_cells
    assert coverage_rejected.moving_coverage < 0.50
    assert coverage_rejected.selected is False


def test_overlap_rejects_zero_common_cells_and_keeps_large_grid_gate() -> None:
    """공통 cell 0은 거부하고 3,200개 이상 점유 격자에서는 구성값 32를 유지하는지 확인합니다."""

    from tools.align.alignment.icp_overlap import evaluate_overlap_candidate

    small_moving = _sparse_line_cloud(
        "P",
        np.concatenate((np.arange(12, dtype=np.float64), np.arange(100, 140, dtype=np.float64))),
    )
    no_common_reference = _sparse_line_cloud(
        "A_zero",
        np.arange(21, dtype=np.float64),
        y_coordinate=1.0,
    )
    no_common = evaluate_overlap_candidate(
        small_moving,
        no_common_reference,
        maximum_distance=2.0,
        occupancy_voxel_size=1.0,
        minimum_common_cells=32,
        minimum_moving_coverage=0.0,
    )
    assert no_common.common_cell_count == 0
    assert no_common.effective_required_common_cells == 32
    assert no_common.selected is False

    large_moving = _sparse_line_cloud(
        "P_large",
        np.concatenate((np.arange(40, dtype=np.float64), np.arange(100, 3261, dtype=np.float64))),
    )
    large_reference = _sparse_line_cloud("A_large", np.arange(40, dtype=np.float64))
    large = evaluate_overlap_candidate(
        large_moving,
        large_reference,
        maximum_distance=0.1,
        occupancy_voxel_size=1.0,
        minimum_common_cells=32,
        minimum_moving_coverage=0.01,
    )
    assert large.moving_cell_count == 3201
    assert large.common_cell_count == 40
    assert large.effective_required_common_cells == 32
    assert large.selected is True


def run_all() -> None:
    """ICP overlap 순수 회귀를 Blender 프로세스에서 순서대로 실행합니다."""

    _add_source_path()
    test_overlap_selects_only_intersecting_reference_model()
    test_transformed_bounds_handles_rotation_and_expansion()
    test_overlap_candidate_checks_cancel_between_reference_models()
    test_combined_multi_reference_coverage_uses_union_not_per_model_maximum()
    test_overlap_keeps_common_cell_gate_and_finer_voxel_resolves_real_signal()
    test_overlap_rejects_zero_common_cells_and_keeps_large_grid_gate()
    print("test_icp_overlap: 통과")


if __name__ == "__main__":
    run_all()
