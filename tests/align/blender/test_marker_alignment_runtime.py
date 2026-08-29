"""실제 COLMAP Runtime snapshot을 사용한 marker preview 수명주기를 검증합니다."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import sys

import numpy as np


def _add_source_path() -> None:
    """애드온 소스 디렉터리를 Blender 테스트 모듈 경로에 추가합니다."""

    source_path = Path(__file__).resolve().parents[3]
    source_text = str(source_path)
    if source_text not in sys.path:
        sys.path.insert(0, source_text)


def _point_table(positions: np.ndarray):
    """테스트할 Source Local 좌표에서 최소 유효 COLMAP PointTable을 생성합니다."""

    from tools.align.colmap_io.types import PointTable

    count = int(positions.shape[0])
    return PointTable(
        point_ids=np.arange(101, 101 + count, dtype=np.uint64),
        positions=positions,
        colors=np.full((count, 3), 128, dtype=np.uint8),
        errors=np.zeros(count, dtype=np.float64),
        track_offsets=np.zeros(count + 1, dtype=np.uint64),
        track_image_ids=np.empty(0, dtype=np.uint64),
        track_point2d_indices=np.empty(0, dtype=np.uint32),
        display_indices=np.arange(count, dtype=np.uint32),
    )


def _model(model_id: str, positions: np.ndarray, revision: int):
    """Repository 등록에 필요한 최소 실제 ColmapModelData를 생성합니다."""

    from tools.align.colmap_io.types import ColmapModelData, ImageTable

    points = _point_table(positions)
    images = ImageTable(
        image_records={},
        point2d_offsets=np.asarray((0,), dtype=np.uint64),
        point2d_xy=np.empty((0, 2), dtype=np.float64),
        point2d_point3d_ids=np.empty(0, dtype=np.uint64),
    )
    return ColmapModelData(
        model_id=model_id,
        display_name=model_id,
        source_path=Path.cwd() / "marker_runtime_fixture" / model_id,
        source_format="text",
        cameras={},
        images=images,
        rigs={},
        frames={},
        points=points,
        bounds_min=np.min(positions, axis=0),
        bounds_max=np.max(positions, axis=0),
        geometry_revision=revision,
    )


def _rotation_z_90() -> np.ndarray:
    """runtime fixture의 알려진 P→A 회전을 반환합니다."""

    return np.asarray(
        ((0.0, -1.0, 0.0), (1.0, 0.0, 0.0), (0.0, 0.0, 1.0)),
        dtype=np.float64,
    )


def _create_runtime_with_three_pairs():
    """A/P 실제 모델·카탈로그·완성 마커 3쌍을 가진 독립 Runtime을 생성합니다."""

    from tools.align.constants import ROLE_MOVING, ROLE_REFERENCE
    from tools.align.display_axes import identity_display_transform
    from tools.align.state import MarkerPair, RuntimeState, SelectedPoint

    moving_positions = np.asarray(
        ((0.0, 0.0, 0.0), (2.0, 0.0, 1.0), (0.0, 3.0, -1.0), (1.0, 1.0, 2.0)),
        dtype=np.float64,
    )
    reference_positions = 1.5 * (moving_positions @ _rotation_z_90().T) + np.asarray(
        (4.0, -3.0, 2.0),
        dtype=np.float64,
    )
    runtime = RuntimeState()
    runtime.session_active = True
    reference_model = _model("reference", reference_positions, revision=7)
    moving_model = _model("moving", moving_positions, revision=11)
    runtime.model_repository.add_model(reference_model, identity_display_transform())
    runtime.model_repository.add_model(moving_model, identity_display_transform())
    runtime.model_catalog.add_imported_model(
        "reference",
        "reference",
        select_after_import=False,
    )
    runtime.model_catalog.add_imported_model(
        "moving",
        "moving",
        select_after_import=True,
    )
    reference_ids = reference_model.points.point_ids
    moving_ids = moving_model.points.point_ids
    runtime.marker_selection.completed_pairs = [
        MarkerPair(
            pair_id=index + 1,
            reference=SelectedPoint(
                ROLE_REFERENCE,
                int(reference_ids[index]),
                tuple(float(value) for value in reference_positions[index]),
                model_id="reference",
            ),
            moving=SelectedPoint(
                ROLE_MOVING,
                int(moving_ids[index]),
                tuple(float(value) for value in moving_positions[index]),
                model_id="moving",
            ),
        )
        for index in range(3)
    ]
    return runtime, reference_positions, moving_positions


def test_preview_is_non_destructive_and_cancel_restores_snapshot() -> None:
    """marker preview가 Repository와 마커 Undo를 바꾸지 않고 취소 가능한지 확인합니다."""

    from tools.align.alignment import (
        begin_marker_preview,
        cancel_marker_preview,
        effective_alignment_for_model,
        set_marker_preview_view_snapshot,
        transform_positions,
    )
    from tools.align.constants import MARKER_ALIGNMENT_PHASE_IDLE, MARKER_ALIGNMENT_PHASE_PREVIEW

    runtime, reference_positions, moving_positions = _create_runtime_with_three_pairs()
    original_alignment = runtime.model_repository.alignment_transform_for_model_id("moving")
    original_marker_pairs = tuple(runtime.marker_selection.completed_pairs)
    original_undo_count = runtime.undo_journal.undo_count
    result = begin_marker_preview(runtime)
    assert runtime.marker_alignment.phase == MARKER_ALIGNMENT_PHASE_PREVIEW
    assert runtime.marker_alignment.result is result
    assert runtime.model_repository.alignment_transform_for_model_id("moving") is original_alignment
    assert tuple(runtime.marker_selection.completed_pairs) == original_marker_pairs
    assert runtime.undo_journal.undo_count == original_undo_count
    assert effective_alignment_for_model(runtime, "moving") is result.preview_alignment
    assert effective_alignment_for_model(runtime, "reference") is runtime.model_repository.alignment_transform_for_model_id(
        "reference"
    )
    transformed_moving_triangle = transform_positions(
        result.delta_transform,
        result.representative_triangle.moving_vertices,
    )
    assert np.allclose(
        transformed_moving_triangle,
        result.representative_triangle.reference_vertices,
        rtol=0.0,
        atol=1.0e-9,
    )
    assert np.allclose(
        transform_positions(result.delta_transform, moving_positions[:3]),
        reference_positions[:3],
        rtol=0.0,
        atol=1.0e-9,
    )
    view_snapshot = {"view_distance": 17.0}
    set_marker_preview_view_snapshot(runtime, view_snapshot)
    assert cancel_marker_preview(runtime) is view_snapshot
    assert runtime.marker_alignment.phase == MARKER_ALIGNMENT_PHASE_IDLE
    assert runtime.marker_alignment.result is None
    assert effective_alignment_for_model(runtime, "moving") is original_alignment
    assert tuple(runtime.marker_selection.completed_pairs) == original_marker_pairs
    assert runtime.undo_journal.undo_count == original_undo_count


def test_preview_can_disable_marker_uniform_scale() -> None:
    """마커 미리보기가 사용자 선택에 따라 기존 scale을 유지하고 강체 delta만 적용하는지 확인합니다."""

    from tools.align.alignment import begin_marker_preview, cancel_marker_preview

    runtime, _reference_positions, _moving_positions = _create_runtime_with_three_pairs()
    base_alignment = runtime.model_repository.alignment_transform_for_model_id("moving")
    result = begin_marker_preview(runtime, use_uniform_scale=False)

    assert np.isclose(result.delta_transform.uniform_scale, 1.0, rtol=0.0, atol=1.0e-12)
    assert np.isclose(
        result.preview_alignment.uniform_scale,
        base_alignment.uniform_scale,
        rtol=0.0,
        atol=1.0e-12,
    )
    assert np.isclose(result.metrics.uniform_scale, 1.0, rtol=0.0, atol=1.0e-12)
    assert result.metrics.rms > 0.0
    cancel_marker_preview(runtime)


def test_stale_marker_and_icp_start_failure_do_not_commit_alignment() -> None:
    """stale 입력과 ICP 시작 실패가 marker preview·확정 alignment를 보존하는지 확인합니다."""

    from tools.align.alignment import (
        IcpRuntimeError,
        begin_icp_job,
        begin_marker_preview,
        create_icp_seed,
        preview_is_current,
    )
    from tools.align.constants import (
        ICP_ALIGNMENT_PHASE_FAILED,
        MARKER_ALIGNMENT_PHASE_IDLE,
        MARKER_ALIGNMENT_PHASE_PREVIEW,
    )
    from tools.align.state import SelectedPoint

    runtime, _reference_positions, _moving_positions = _create_runtime_with_three_pairs()
    original_alignment = runtime.model_repository.alignment_transform_for_model_id("moving")
    result = begin_marker_preview(runtime)
    assert runtime.marker_alignment.phase == MARKER_ALIGNMENT_PHASE_PREVIEW
    seed = create_icp_seed(runtime)
    assert seed.moving_model_id == "moving"
    assert np.array_equal(seed.initial_alignment_float64, result.preview_alignment.matrix_float64)
    missing_blender = Path(__file__).resolve().parent / "missing_icp_worker_blender.exe"
    assert not missing_blender.exists()
    try:
        begin_icp_job(
            runtime,
            preset="BALANCED",
            scale_refine_enabled=False,
            blender_binary_path=str(missing_blender),
        )
    except IcpRuntimeError as error:
        assert str(error)
    else:
        raise AssertionError("없는 Blender worker 경로로 ICP 작업이 시작되었습니다.")
    assert runtime.icp_alignment.phase == ICP_ALIGNMENT_PHASE_FAILED
    assert runtime.icp_alignment.job_handle is None
    assert runtime.icp_alignment.result is None
    assert runtime.icp_alignment.last_error
    assert runtime.marker_alignment.result is result
    assert runtime.model_repository.alignment_transform_for_model_id("moving") is original_alignment

    first_pair = runtime.marker_selection.completed_pairs[0]
    runtime.marker_selection.completed_pairs[0] = replace(
        first_pair,
        reference=SelectedPoint(
            first_pair.reference.model_role,
            first_pair.reference.point3d_id,
            (999.0, 999.0, 999.0),
            model_id=first_pair.reference.model_id,
        ),
    )
    assert preview_is_current(runtime) is False
    assert runtime.marker_alignment.phase == MARKER_ALIGNMENT_PHASE_IDLE
    assert runtime.model_repository.alignment_transform_for_model_id("moving") is original_alignment


def test_invalid_marker_input_leaves_preview_idle_and_repository_identity_lifecycle_is_complete() -> None:
    """입력 실패 원자성과 Repository alignment 등록·삭제·clear 경계를 확인합니다."""

    from tools.align.alignment import MarkerInputError, begin_marker_preview
    from tools.align.constants import MARKER_ALIGNMENT_PHASE_IDLE

    runtime, _reference_positions, _moving_positions = _create_runtime_with_three_pairs()
    runtime.marker_selection.completed_pairs = runtime.marker_selection.completed_pairs[:2]
    original_alignment = runtime.model_repository.alignment_transform_for_model_id("moving")
    try:
        begin_marker_preview(runtime)
    except MarkerInputError:
        pass
    else:
        raise AssertionError("두 마커 쌍으로 preview가 생성되었습니다.")
    assert runtime.marker_alignment.phase == MARKER_ALIGNMENT_PHASE_IDLE
    assert runtime.marker_alignment.result is None
    assert runtime.model_repository.alignment_transform_for_model_id("moving") is original_alignment

    runtime, _reference_positions, _moving_positions = _create_runtime_with_three_pairs()
    original_alignment = runtime.model_repository.alignment_transform_for_model_id("moving")
    first_pair = runtime.marker_selection.completed_pairs[0]
    runtime.marker_selection.completed_pairs[0] = replace(
        first_pair,
        moving=replace(first_pair.moving, point3d_id=None),
    )
    try:
        begin_marker_preview(runtime)
    except MarkerInputError:
        pass
    else:
        raise AssertionError("None POINT3D_ID로 preview가 생성되었습니다.")
    assert runtime.marker_alignment.phase == MARKER_ALIGNMENT_PHASE_IDLE
    assert runtime.marker_alignment.result is None
    assert runtime.model_repository.alignment_transform_for_model_id("moving") is original_alignment

    removed = runtime.model_repository.remove_model("moving")
    assert removed is not None
    assert "moving" not in runtime.model_repository.alignment_transforms
    runtime.model_repository.clear()
    assert runtime.model_repository.models == {}
    assert runtime.model_repository.display_transforms == {}
    assert runtime.model_repository.alignment_transforms == {}


def run_all() -> None:
    """marker preview Runtime 회귀를 Blender 프로세스에서 순서대로 실행합니다."""

    _add_source_path()
    test_preview_is_non_destructive_and_cancel_restores_snapshot()
    test_preview_can_disable_marker_uniform_scale()
    test_stale_marker_and_icp_start_failure_do_not_commit_alignment()
    test_invalid_marker_input_leaves_preview_idle_and_repository_identity_lifecycle_is_complete()
    print("test_marker_alignment_runtime: 통과")


if __name__ == "__main__":
    run_all()
