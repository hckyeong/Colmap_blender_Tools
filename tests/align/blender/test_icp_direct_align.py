"""마커 미리보기 없이 현재 P 위치에서 시작하는 직접 ICP 계약을 검증합니다."""

from __future__ import annotations

from pathlib import Path
import sys

import numpy as np


def _add_test_paths() -> None:
    """애드온 소스와 기존 ICP fixture 경로를 import 목록에 추가합니다."""

    test_path = Path(__file__).resolve().parent
    source_path = test_path.parents[2]
    for path in (source_path, test_path):
        path_text = str(path)
        if path_text not in sys.path:
            sys.path.insert(0, path_text)


def _runtime_with_current_alignment(
    *,
    include_far_reference: bool = False,
    selected_reference_model_id: str = "reference",
):
    """수정 키 A/P 선택과 비Identity P 확정 정렬만 있는 Runtime을 만듭니다."""

    from tools.align.alignment import (
        cancel_marker_preview,
        create_sim3_transform,
    )
    from test_icp_runtime import _runtime_with_marker_preview

    runtime, _marker_result = _runtime_with_marker_preview(
        include_far_reference=include_far_reference
    )
    cancel_marker_preview(runtime)
    runtime.marker_selection.completed_pairs.clear()
    assert runtime.model_catalog.select_model(
        "moving",
        has_marker_draft=False,
        completed_pair_count=0,
    )
    assert runtime.model_catalog.toggle_alignment_selection(
        selected_reference_model_id,
        has_marker_draft=False,
        completed_pair_count=0,
    )
    angle = np.deg2rad(7.0)
    rotation = np.asarray(
        (
            (np.cos(angle), -np.sin(angle), 0.0),
            (np.sin(angle), np.cos(angle), 0.0),
            (0.0, 0.0, 1.0),
        ),
        dtype=np.float64,
    )
    alignment = create_sim3_transform(
        rotation,
        1.25,
        np.asarray((1.5, -0.75, 0.25), dtype=np.float64),
    )
    runtime.model_repository.set_alignment_transform("moving", alignment)
    return runtime, alignment


def test_direct_seed_uses_current_alignment_without_fake_markers() -> None:
    """직접 seed가 현재 P 변환을 보존하고 가짜 마커 좌표를 만들지 않는지 확인합니다."""

    from tools.align.alignment import (
        create_icp_input_snapshot,
        create_icp_seed,
        icp_seed_is_current,
        identity_sim3_transform,
    )

    runtime, alignment = _runtime_with_current_alignment()
    seed = create_icp_seed(runtime)
    assert seed.initialization_mode == "CURRENT_ALIGNMENT"
    assert seed.uses_marker_preview is False
    assert seed.marker_result is None
    np.testing.assert_allclose(seed.initial_alignment_float64, alignment.matrix_float64)
    np.testing.assert_allclose(seed.base_committed_alignment_float64, alignment.matrix_float64)
    assert icp_seed_is_current(runtime, seed) is True

    input_data = create_icp_input_snapshot(runtime)
    assert input_data.reference_model_ids == ("reference",)
    assert input_data.seed.initialization_mode == "CURRENT_ALIGNMENT"
    assert input_data.snapshot is not None
    assert input_data.snapshot.marker_reference_project is None
    assert input_data.snapshot.marker_moving_project is None
    assert input_data.snapshot.marker_max_error == 0.0
    assert input_data.snapshot.marker_rms == 0.0
    assert input_data.snapshot.marker_signature == seed.marker_signature

    runtime.model_repository.set_alignment_transform("moving", identity_sim3_transform())
    assert icp_seed_is_current(runtime, seed) is False


def test_scale_refine_seed_uses_current_alignment_without_markers() -> None:
    """Scale Refine 입력이 마커 없이 현재 P 확정 정렬을 초기값으로 사용하는지 확인합니다."""

    from tools.align.alignment import (
        create_icp_input_snapshot,
        create_icp_seed,
        icp_seed_is_current,
    )

    runtime, alignment = _runtime_with_current_alignment()
    seed = create_icp_seed(runtime)
    assert seed.initialization_mode == "CURRENT_ALIGNMENT"
    assert seed.uses_marker_preview is False
    assert seed.marker_result is None
    np.testing.assert_allclose(seed.initial_alignment_float64, alignment.matrix_float64)
    assert runtime.marker_alignment.is_active is False
    assert icp_seed_is_current(runtime, seed) is True

    input_data = create_icp_input_snapshot(
        runtime,
        scale_refine_enabled=True,
    )
    assert input_data.seed.initialization_mode == "CURRENT_ALIGNMENT"
    assert input_data.seed.marker_result is None
    assert input_data.snapshot is not None
    assert input_data.snapshot.marker_reference_project is None
    assert input_data.snapshot.marker_moving_project is None
    assert runtime.marker_alignment.is_active is False


def test_direct_open3d_capture_uses_geometry_scale() -> None:
    """마커 없는 Open3D 입력이 점 간격만큼 좁은 대응 거리로 축소되지 않는지 확인합니다."""

    from tools.align.alignment.icp_open3d_solver import (
        OPEN3D_GICP_ALGORITHM_VERSION,
        OPEN3D_GICP_BACKEND_ID,
        OPEN3D_JOB_PROTOCOL_VERSION,
        Open3dPointCloud,
        Open3dSolverInput,
        _derive_capture_distance,
    )

    coordinates = np.linspace(0.0, 10.0, 11, dtype=np.float64)
    positions = np.asarray(
        [(x, y, z) for x in coordinates for y in coordinates for z in (0.0, 10.0)],
        dtype=np.float64,
    )
    point_ids = np.arange(1, positions.shape[0] + 1, dtype=np.uint64)
    source = Open3dPointCloud("moving", positions, point_ids)
    target = Open3dPointCloud("reference", positions.copy(), point_ids.copy())
    solver_input = Open3dSolverInput(
        job_id="direct-align-test",
        input_fingerprint="direct-align-input",
        algorithm_version="hybrid-point-to-plane-icp-v2",
        backend_id=OPEN3D_GICP_BACKEND_ID,
        backend_algorithm_version=OPEN3D_GICP_ALGORITHM_VERSION,
        job_protocol_version=OPEN3D_JOB_PROTOCOL_VERSION,
        moving_cloud_frame="INITIAL_PROJECT_WORLD",
        source=source,
        references=(target,),
        initial_alignment=np.identity(4, dtype=np.float64),
        marker_uniform_scale=1.0,
        marker_max_error=0.0,
        parameters={"scale_mode": "LOCKED", "scale_refine_enabled": False},
        marker_signature="direct-alignment-signature",
    )
    geometry_diagonal = float(np.linalg.norm(np.ptp(positions, axis=0)))
    capture_distance = _derive_capture_distance(solver_input)
    assert capture_distance >= 0.05 * geometry_diagonal - 1.0e-12
    assert capture_distance <= 0.25 * geometry_diagonal + 1.0e-12


def test_direct_seed_uses_the_full_a_group_regardless_of_selected_a() -> None:
    """선택한 A 조합·숨김과 무관하게 직접 Align이 A 그룹 전체를 기준으로 쓰는지 확인합니다."""

    from tools.align.alignment import (
        create_icp_input_snapshot,
        create_icp_seed,
        icp_seed_is_current,
    )

    runtime, _alignment = _runtime_with_current_alignment(include_far_reference=True)
    seed = create_icp_seed(runtime)
    input_data = create_icp_input_snapshot(runtime)
    expected_reference_ids = ("reference", "reference_far")
    assert seed.reference_model_ids == expected_reference_ids
    assert input_data.reference_model_ids == expected_reference_ids
    assert len(input_data.reference_positions) == len(expected_reference_ids)

    assert runtime.model_catalog.set_model_visibility("reference_far", False)
    assert icp_seed_is_current(runtime, seed) is True

    assert runtime.model_catalog.toggle_alignment_selection(
        "reference",
        has_marker_draft=False,
        completed_pair_count=0,
    )
    assert runtime.model_catalog.toggle_alignment_selection(
        "reference_far",
        has_marker_draft=False,
        completed_pair_count=0,
    )
    assert runtime.model_catalog.selected_inactive_model_id == "moving"
    assert runtime.model_catalog.selected_reference_model_id == "reference_far"
    assert icp_seed_is_current(runtime, seed) is True
    changed_seed = create_icp_seed(runtime)
    changed_input = create_icp_input_snapshot(runtime)
    assert changed_seed.reference_model_ids == expected_reference_ids
    assert changed_input.reference_model_ids == expected_reference_ids
    assert changed_input.input_fingerprint == input_data.input_fingerprint


def test_direct_seed_preserves_full_a_catalog_order() -> None:
    """A 그룹 전체 seed와 기준 점군이 화면의 카탈로그 순서를 보존하는지 확인합니다."""

    from tools.align.alignment import create_icp_input_snapshot, create_icp_seed

    runtime, _alignment = _runtime_with_current_alignment(include_far_reference=True)
    runtime.model_catalog.model_order = ["reference_far", "reference", "moving"]

    seed = create_icp_seed(runtime)
    input_data = create_icp_input_snapshot(runtime)

    assert seed.reference_model_ids == ("reference_far", "reference")
    assert input_data.reference_model_ids == ("reference_far", "reference")
    assert tuple(cloud.model_id for cloud in input_data.snapshot.reference_clouds) == (
        "reference_far",
        "reference",
    )


def test_marker_preview_seed_uses_the_full_a_group() -> None:
    """마커 Sim(3) 뒤에도 ICP seed와 입력이 숨김 A를 포함한 전체 A 그룹을 쓰는지 확인합니다."""

    from tools.align.alignment import create_icp_input_snapshot, create_icp_seed
    from test_icp_runtime import _runtime_with_marker_preview

    runtime, _marker_result = _runtime_with_marker_preview(include_far_reference=True)
    assert runtime.model_catalog.set_model_visibility("reference_far", False)
    expected_reference_ids = ("reference", "reference_far")

    seed = create_icp_seed(runtime)
    input_data = create_icp_input_snapshot(runtime)

    assert seed.uses_marker_preview is True
    assert seed.reference_model_ids == expected_reference_ids
    assert input_data.reference_model_ids == expected_reference_ids
    assert len(input_data.reference_positions) == len(expected_reference_ids)


def test_direct_manifest_omits_marker_coordinates() -> None:
    """직접 Align 작업 manifest가 가짜 마커 좌표 없이 현재 P 변환을 전달하는지 확인합니다."""

    from tools.align.alignment import create_icp_input_snapshot
    from tools.align.alignment.icp_backend import (
        cleanup_unstarted_job_directory,
        create_icp_job_directory,
    )
    from tools.align.alignment.icp_runtime import _manifest_for_input

    runtime, alignment = _runtime_with_current_alignment()
    input_data = create_icp_input_snapshot(runtime)
    files = create_icp_job_directory()
    try:
        manifest = _manifest_for_input(files, input_data)
        assert manifest["marker_reference_project"] is None
        assert manifest["marker_moving_project"] is None
        assert manifest["marker_max_error"] == 0.0
        assert manifest["marker_rms"] == 0.0
        assert manifest["marker_uniform_scale"] == alignment.uniform_scale
        np.testing.assert_allclose(manifest["initial_alignment"], alignment.matrix_float64)
        assert (files.job_directory / "moving_positions.npy").is_file()
        assert (files.job_directory / "moving_point_ids.npy").is_file()
    finally:
        cleanup_unstarted_job_directory(files)


def test_direct_preview_discard_and_approval_keep_current_alignment_contract() -> None:
    """직접 결과의 버리기·승인이 현재 P 초기값과 기존 Undo 계약을 유지하는지 확인합니다."""

    from tools.align.alignment import (
        IcpRuntimeResult,
        Sim3Transform,
        approve_icp_result,
        create_icp_input_snapshot,
        discard_icp_preview,
        restore_icp_commit_snapshot,
    )
    from tools.align.constants import (
        ICP_ALIGNMENT_PHASE_PREVIEW,
        MODEL_STATUS_PENDING,
        MODEL_STATUS_REGISTERED,
    )

    runtime, initial_alignment = _runtime_with_current_alignment()
    input_data = create_icp_input_snapshot(runtime)
    angle = np.deg2rad(1.0)
    delta_matrix = np.identity(4, dtype=np.float64)
    delta_matrix[:3, :3] = np.asarray(
        (
            (np.cos(angle), -np.sin(angle), 0.0),
            (np.sin(angle), np.cos(angle), 0.0),
            (0.0, 0.0, 1.0),
        ),
        dtype=np.float64,
    )
    delta_matrix[:3, 3] = np.asarray((0.02, -0.01, 0.005), dtype=np.float64)
    delta = Sim3Transform(matrix_float64=delta_matrix)
    final_alignment = Sim3Transform(
        matrix_float64=delta.matrix_float64 @ initial_alignment.matrix_float64
    )
    result = IcpRuntimeResult(
        moving_model_id="moving",
        reference_model_ids=input_data.reference_model_ids,
        final_alignment=final_alignment,
        icp_delta=delta,
        quality={"status": "PASS"},
        metadata={"fixture": "direct-align"},
        input_fingerprint=input_data.input_fingerprint,
        parameter_fingerprint=input_data.parameters.fingerprint,
        marker_signature=input_data.seed.marker_signature,
        model_revision_snapshot=input_data.model_revision_snapshot,
        algorithm_version=input_data.algorithm_version,
    )

    state = runtime.icp_alignment
    state.phase = ICP_ALIGNMENT_PHASE_PREVIEW
    state.input_data = input_data
    state.result = result
    state.working_alignment = final_alignment
    discard_icp_preview(runtime)
    assert runtime.model_repository.alignment_transform_for_model_id(
        "moving"
    ).fingerprint == initial_alignment.fingerprint
    assert runtime.model_catalog.models["moving"].registration_status == MODEL_STATUS_PENDING

    state.phase = ICP_ALIGNMENT_PHASE_PREVIEW
    state.input_data = input_data
    state.result = result
    state.working_alignment = final_alignment
    commit = approve_icp_result(runtime)
    assert runtime.model_repository.alignment_transform_for_model_id(
        "moving"
    ).fingerprint == final_alignment.fingerprint
    assert runtime.model_catalog.models["moving"].registration_status == MODEL_STATUS_REGISTERED
    assert commit.before.marker_preview.result is None
    assert runtime.undo_journal.undo_committed(
        lambda snapshot, _kind: restore_icp_commit_snapshot(runtime, snapshot)
    ) is True
    assert runtime.model_repository.alignment_transform_for_model_id(
        "moving"
    ).fingerprint == initial_alignment.fingerprint
    assert runtime.model_catalog.models["moving"].registration_status == MODEL_STATUS_PENDING


def main() -> None:
    """직접 Align 회귀 테스트를 순서대로 실행합니다."""

    _add_test_paths()
    test_direct_seed_uses_current_alignment_without_fake_markers()
    test_scale_refine_seed_uses_current_alignment_without_markers()
    test_direct_open3d_capture_uses_geometry_scale()
    test_direct_seed_uses_the_full_a_group_regardless_of_selected_a()
    test_direct_seed_preserves_full_a_catalog_order()
    test_marker_preview_seed_uses_the_full_a_group()
    test_direct_manifest_omits_marker_coordinates()
    test_direct_preview_discard_and_approval_keep_current_alignment_contract()
    print("test_icp_direct_align: 통과")


if __name__ == "__main__":
    main()
