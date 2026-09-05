"""연결 메시와 점군 마커 조합·원본 불변·승인 종속성 계약을 검증합니다."""

from __future__ import annotations

from contextlib import ExitStack, contextmanager
from dataclasses import replace
from pathlib import Path
import sys
from unittest.mock import patch

import numpy as np


def _add_test_paths():
    """실험 소스와 공통 Blender 회귀 자료를 모듈 경로에 연결합니다."""
    folder = Path(__file__).resolve().parent
    for value in (folder.parents[2], folder):
        if str(value) not in sys.path:
            sys.path.insert(0, str(value))


def _transform(matrix, positions):
    """동차 행렬로 원본 좌표 배열을 새 좌표 배열로 변환합니다."""
    return positions @ matrix[:3, :3].T + matrix[:3, 3]


def _rotation(degrees):
    """회복 대상에서 합성 순서가 드러나는 Z 회전 행렬을 만듭니다."""
    radians = np.deg2rad(degrees)
    return np.asarray(((np.cos(radians), -np.sin(radians), 0), (np.sin(radians), np.cos(radians), 0), (0, 0, 1)))


def _mesh(model_id, vertices):
    """앞쪽 네 등록점을 포함하는 두 원본 삼각면을 구성합니다."""
    from tools.align.mesh_io.types import TriangleMeshModelData

    return TriangleMeshModelData(
        model_id=model_id, display_name=model_id, source_path=Path.cwd() / (model_id + ".obj"),
        vertices_float64=vertices,
        triangles_uint32=np.asarray(((0, 1, 2), (0, 2, 3)), dtype=np.uint32),
        registration_vertex_indices=np.arange(len(vertices), dtype=np.uint32),
        display_vertex_indices=np.arange(len(vertices), dtype=np.uint32),
        bounds_min=vertices.min(axis=0), bounds_max=vertices.max(axis=0),
    )


def _runtime(reference_mesh=True, moving_mesh=True, *, multiple_reference=False, delta_scale=1.7):
    """서로 다른 주 축·연결 축·소유 정렬에서 알려진 대응점과 네 마커를 구성합니다."""
    from test_icp_runtime import _model, _moving_positions
    from test_geometry_axes_export import _with_camera
    from tools.align.alignment import create_sim3_transform
    from tools.align.display_axes import create_model_display_transform
    from tools.align.model_repository import CompanionMeshSourceMetadata
    from tools.align.state import MarkerPair, RuntimeState, SelectedPoint

    runtime = RuntimeState()
    runtime.session_active = True
    delta = create_sim3_transform(_rotation(39), delta_scale, np.asarray((4.5, -3.2, 2.7)))
    moving_world = _moving_positions(24)
    reference_world = _transform(delta.matrix_float64, moving_world)
    model_ids = ("reference", "reference_second", "moving") if multiple_reference else ("reference", "moving")
    for index, model_id in enumerate(model_ids):
        main_axis = create_model_display_transform("CUSTOM", "Z", "-Y", rotation_xyz_degrees=(13 + index, -22, 37))
        mesh_axis = create_model_display_transform("CUSTOM", "X", "Z", rotation_xyz_degrees=(-19, 31 + index, 8))
        alignment = create_sim3_transform(_rotation(-17 + index * 13), 0.85 + index * 0.23, np.asarray((2 - index, -3 + index, 0.7)))
        target_world = moving_world if model_id == "moving" else reference_world
        main_local = _transform(np.linalg.inv(alignment.matrix_float64 @ main_axis.matrix_float64), target_world)
        mesh_local = _transform(np.linalg.inv(alignment.matrix_float64 @ mesh_axis.matrix_float64), target_world)
        model = _with_camera(_model(model_id, main_local, revision=index + 3))
        runtime.model_repository.add_model(model, main_axis)
        runtime.model_repository.set_alignment_transform(model_id, alignment)
        runtime.model_catalog.add_imported_model(model_id, model_id, select_after_import=model_id == "moving")
        if model_id == "reference_second":
            runtime.model_catalog.promote_model_for_icp_commit(model_id)
        mesh = _mesh(model_id, mesh_local)
        runtime.model_repository.add_companion_mesh(
            model_id, mesh, CompanionMeshSourceMetadata(str(mesh.source_path), str(index + 1) * 64), mesh_axis,
        )
        is_mesh = moving_mesh if model_id == "moving" else reference_mesh
        runtime.model_catalog.set_geometry_visibility(model_id, show_points=not is_mesh, show_surface=is_mesh)
    pairs = []
    for index in range(4):
        points = []
        reference_id = "reference_second" if multiple_reference and index % 2 else "reference"
        for role, model_id, is_mesh in (("REFERENCE", reference_id, reference_mesh), ("MOVING", "moving", moving_mesh)):
            if is_mesh:
                source = runtime.model_repository.companion_meshes[model_id].vertices_float64[index]
                identifier = 0 if index < 3 else 1
                kind, slot = "MESH_SURFACE", "COMPANION"
            else:
                source = runtime.model_repository.models[model_id].points.positions[index]
                identifier = int(runtime.model_repository.models[model_id].points.point_ids[index])
                kind, slot = "COLMAP_POINT", "MAIN"
            points.append(SelectedPoint(role, identifier, tuple(source), model_id, kind, identifier, slot))
        pairs.append(MarkerPair(index + 1, *points))
    runtime.marker_selection.completed_pairs = pairs
    runtime.marker_selection.next_pair_id = 5
    return runtime, delta, reference_world[:4], moving_world[:4]


@contextmanager
def _display_runtime(runtime):
    """시험 런타임을 실제 피킹·표시 조회 경계에만 연결합니다."""
    from tools.align.viewport import picking, point_data, renderer

    with ExitStack() as stack:
        for module in (picking, point_data, renderer):
            stack.enter_context(patch.object(module, "RUNTIME", runtime))
        yield


def _expect_error(operation):
    """원본·종류·슬롯이 잘못된 입력이 정렬 오류로 거절되는지 확인합니다."""
    from tools.align.alignment.types import MarkerInputError

    try:
        operation()
    except MarkerInputError:
        return
    raise AssertionError("잘못된 마커 입력이 허용되었습니다.")



def _finish_external_worker_result(runtime, input_data):
    """종료한 외부 작업의 실제 JSON·NPZ 프로토콜을 통과시켜 결과 미리보기를 만듭니다."""
    from test_icp_runtime import _ExitedProcess, _valid_progress_payload
    from test_icp_small_gicp_backend import _external_result_metadata
    from tools.align.alignment import finish_icp_job
    from tools.align.alignment.icp_backend import (
        IcpJobHandle, create_icp_job_directory, cleanup_job_directory,
        write_json_atomically, write_result_arrays_atomically,
    )
    from tools.align.alignment.icp_external_contract import analyze_external_icp_delta
    from tools.align.alignment.icp_solver_backend import backend_algorithm_version
    from tools.align.constants import ICP_ALIGNMENT_PHASE_RUNNING

    input_data = replace(input_data, solver_backend="SMALL_GICP", backend_algorithm_version=backend_algorithm_version("SMALL_GICP"))
    files = create_icp_job_directory()
    handle = IcpJobHandle(
        job_id=files.job_directory.name, files=files, process=_ExitedProcess(),
        input_fingerprint=input_data.input_fingerprint, backend_id="SMALL_GICP",
    )
    delta = np.eye(4)
    diagnostics = analyze_external_icp_delta(delta, np.zeros(3), field_name="연결 메시 승인 회귀")
    metadata = _external_result_metadata(input_data, handle.job_id, diagnostics)
    try:
        write_json_atomically(files.result_path, metadata)
        write_result_arrays_atomically(files.result_array_path, {
            "initial_alignment": input_data.seed.initial_alignment_float64,
            "final_alignment": input_data.seed.initial_alignment_float64,
            "icp_delta": delta,
        })
        write_json_atomically(files.progress_path, {
            **_valid_progress_payload(handle, input_data),
            "stage": "SMALL_GICP_COMPLETED_PASS", "progress": 1.0, "level_complete": True,
            "backend_id": "SMALL_GICP",
            "backend_algorithm_version": input_data.backend_algorithm_version,
            "job_protocol_version": "icp-job-protocol-v1",
            "stable_alignment_4x4": input_data.seed.initial_alignment_float64.tolist(),
        })
        runtime.icp_alignment.phase = ICP_ALIGNMENT_PHASE_RUNNING
        runtime.icp_alignment.job_handle = handle
        runtime.icp_alignment.input_data = input_data
        result = finish_icp_job(runtime)
        assert not files.job_directory.exists()
        return result
    finally:
        if files.job_directory.exists():
            cleanup_job_directory(handle, keep_failure_diagnostics=False)


def test_four_combinations_recover_sim3_and_keep_all_sources():
    """네 메시·점군 조합에서 선택 축 순서·공통 정렬 합성·원본 불변성을 검증합니다."""
    from test_geometry_axes_export import _source_snapshot, _assert_source_unchanged
    from tools.align.alignment import begin_marker_preview, create_marker_alignment_input
    from tools.align.viewport import renderer, point_data

    for reference_mesh in (False, True):
        for moving_mesh in (False, True):
            runtime, delta, expected_reference, expected_moving = _runtime(reference_mesh, moving_mesh)
            repository = runtime.model_repository
            source_before = _source_snapshot(runtime)
            base = repository.alignment_transform_for_model_id("moving").matrix_float64.copy()
            input_data = create_marker_alignment_input(runtime)
            np.testing.assert_allclose(input_data.reference_project, expected_reference, atol=1e-11)
            np.testing.assert_allclose(input_data.moving_project, expected_moving, atol=1e-11)
            with _display_runtime(runtime):
                for pair in runtime.marker_selection.completed_pairs:
                    for point in (pair.reference, pair.moving):
                        expected = input_data.reference_project[pair.pair_id - 1] if point.model_role == "REFERENCE" else input_data.moving_project[pair.pair_id - 1]
                        np.testing.assert_allclose(renderer._display_marker_position(point.model_id, point.local_position, point.geometry_source), expected, atol=1e-11)
                result = begin_marker_preview(runtime)
                np.testing.assert_allclose(result.preview_alignment.matrix_float64, delta.matrix_float64 @ base, atol=1e-10)
                for companion in (False, True):
                    selected_axis = repository.companion_display_transform_for_model_id("moving") if companion else repository.display_transform_for_model_id("moving")
                    np.testing.assert_allclose(point_data.effective_model_matrix_for_id("moving", companion=companion), delta.matrix_float64 @ base @ selected_axis.matrix_float64, atol=1e-10)
            assert result.metrics.rms < 1e-10
            _assert_source_unchanged(runtime, source_before)


def test_multiple_reference_owners_and_rigid_mode():
    """여러 A 소유자의 서로 다른 축·정렬을 조합하고 강체 모드도 회복합니다."""
    from tools.align.alignment import begin_marker_preview, create_marker_alignment_input

    for scale in (1.0, 1.7):
        runtime, delta, reference, moving = _runtime(multiple_reference=True, delta_scale=scale)
        base = runtime.model_repository.alignment_transform_for_model_id("moving")
        current = create_marker_alignment_input(runtime)
        assert current.reference_model_ids == ("reference", "reference_second")
        np.testing.assert_allclose(current.reference_project, reference, atol=1e-11)
        np.testing.assert_allclose(current.moving_project, moving, atol=1e-11)
        result = begin_marker_preview(runtime, use_uniform_scale=scale != 1.0)
        np.testing.assert_allclose(result.preview_alignment.matrix_float64, delta.matrix_float64 @ base.matrix_float64, atol=1e-10)


def test_source_validation_and_role_conflict_are_final_input_guards():
    """연결 표면 누락·바뀐 면 위치·혼합 역할을 최종 계산 경계에서도 거절합니다."""
    from tools.align.alignment import create_marker_alignment_input
    from tools.align.state import SelectedPoint

    runtime, *_ = _runtime()
    first = runtime.marker_selection.completed_pairs[0]
    reference_model = runtime.model_repository.models["reference"]
    raw_point = reference_model.points.positions[0]
    cloud = SelectedPoint("REFERENCE", 101, tuple(raw_point), "reference")
    runtime.marker_selection.completed_pairs[0] = replace(first, reference=cloud)
    _expect_error(lambda: create_marker_alignment_input(runtime))
    runtime.marker_selection.completed_pairs[0] = replace(first, reference=replace(first.reference, local_position=tuple(np.asarray(first.reference.local_position) + 99)))
    _expect_error(lambda: create_marker_alignment_input(runtime))
    runtime.marker_selection.completed_pairs[0] = first
    runtime.model_repository.companion_meshes.pop("reference")
    _expect_error(lambda: create_marker_alignment_input(runtime))


def test_companion_axis_revision_fingerprint_and_slot_invalidate_preview():
    """미리보기 후 메시 축·형상·출처·마커 슬롯 변경이 오래된 결과를 차단합니다."""
    from tools.align.alignment import begin_marker_preview, preview_is_current
    from tools.align.display_axes import create_model_display_transform

    for change in ("axis", "revision", "fingerprint", "slot"):
        runtime, *_ = _runtime()
        begin_marker_preview(runtime)
        repository = runtime.model_repository
        if change == "axis":
            repository.set_companion_display_transform("moving", create_model_display_transform("CUSTOM", "Y", "Z"))
        elif change == "revision":
            mesh = repository.companion_meshes["moving"]
            repository.companion_meshes["moving"] = replace(mesh, geometry_revision=mesh.geometry_revision + 1)
        elif change == "fingerprint":
            repository.companion_sources["moving"] = replace(repository.companion_sources["moving"], source_fingerprint="f" * 64)
        else:
            pair = runtime.marker_selection.completed_pairs[0]
            runtime.marker_selection.completed_pairs[0] = replace(pair, moving=replace(pair.moving, geometry_source="MAIN"))
        assert not preview_is_current(runtime)
        assert runtime.marker_alignment.result is None


def test_marker_seed_approval_records_dependencies_and_keeps_common_alignment():
    """메시 마커 초기값의 승인·마커 정리·종속 출처·공통 정렬과 원본 불변성을 검증합니다."""
    from test_geometry_axes_export import _source_snapshot, _assert_source_unchanged
    from tools.align.alignment import begin_marker_preview, create_icp_input_snapshot, approve_icp_result, restore_icp_commit_snapshot
    runtime, delta, *_ = _runtime(multiple_reference=True)
    before = _source_snapshot(runtime)
    base = runtime.model_repository.alignment_transform_for_model_id("moving")
    begin_marker_preview(runtime)
    input_data = create_icp_input_snapshot(runtime)
    expected_dependencies = (("moving", "COMPANION"), ("reference", "COMPANION"), ("reference_second", "COMPANION"))
    assert input_data.seed.marker_geometry_dependencies == expected_dependencies
    result = _finish_external_worker_result(runtime, input_data)
    assert result.solver_backend == "SMALL_GICP"
    commit = approve_icp_result(runtime)
    assert runtime.marker_selection.completed_pairs == []
    provenance = runtime.model_catalog.models["moving"].alignment_provenance
    assert provenance.marker_geometry_dependencies == expected_dependencies
    np.testing.assert_allclose(runtime.model_repository.alignment_transform_for_model_id("moving").matrix_float64, delta.matrix_float64 @ base.matrix_float64, atol=1e-10)
    _assert_source_unchanged(runtime, before)
    restore_icp_commit_snapshot(runtime, commit.before)
    assert len(runtime.marker_selection.completed_pairs) == 4
    restore_icp_commit_snapshot(runtime, commit.after)
    assert runtime.model_catalog.models["moving"].alignment_provenance is provenance
    assert runtime.marker_selection.completed_pairs == []



def test_companion_change_after_worker_finish_blocks_approval():
    """외부 결과가 끝난 뒤 연결 표면이 바뀌면 승인 직전에도 이전 입력을 차단합니다."""
    from tools.align.alignment import begin_marker_preview, create_icp_input_snapshot, approve_icp_result
    from tools.align.alignment.icp_runtime import IcpRuntimeStaleError

    runtime, *_ = _runtime()
    base = runtime.model_repository.alignment_transform_for_model_id("moving")
    begin_marker_preview(runtime)
    _finish_external_worker_result(runtime, create_icp_input_snapshot(runtime))
    mesh = runtime.model_repository.companion_meshes["moving"]
    runtime.model_repository.companion_meshes["moving"] = replace(mesh, geometry_revision=mesh.geometry_revision + 1)
    try:
        approve_icp_result(runtime)
    except IcpRuntimeStaleError:
        pass
    else:
        raise AssertionError("교체된 연결 메시의 이전 계산 결과가 승인되었습니다.")
    assert runtime.model_repository.alignment_transform_for_model_id("moving") is base
    assert len(runtime.marker_selection.completed_pairs) == 4


def test_direct_sparse_seed_ignores_unused_mesh_markers_and_mesh_axis():
    """남아 있는 메시 마커를 사용하지 않은 직접 정렬 초기값은 희소 형상만 참조합니다."""
    from tools.align.alignment import create_icp_seed, create_icp_input_snapshot, icp_seed_is_current
    from tools.align.display_axes import create_model_display_transform

    runtime, *_ = _runtime()
    runtime.model_catalog.selected_model_ids = ("reference", "moving")
    seed = create_icp_seed(runtime)
    assert seed.initialization_mode == "CURRENT_ALIGNMENT"
    assert seed.marker_geometry_dependencies == ()
    first_input = create_icp_input_snapshot(runtime)
    runtime.model_repository.set_companion_display_transform("moving", create_model_display_transform("CUSTOM", "Y", "Z"))
    assert icp_seed_is_current(runtime, seed)
    second_input = create_icp_input_snapshot(runtime)
    np.testing.assert_array_equal(first_input.moving_positions, second_input.moving_positions)
    assert first_input.input_fingerprint == second_input.input_fingerprint



def test_marker_labels_project_the_selected_geometry_and_preview():
    """점군·메시 네 조합에서 글자와 원이 같은 실제 축·정렬 좌표를 화면에 투영합니다."""
    from types import SimpleNamespace
    from mathutils import Matrix
    from tools.align.alignment import begin_marker_preview
    from tools.align.viewport import labels, renderer

    region = SimpleNamespace(width=800, height=600)
    region_3d = SimpleNamespace(perspective_matrix=Matrix.Diagonal((0.01, 0.01, 0.01, 1.0)))
    context = SimpleNamespace(region=region, space_data=SimpleNamespace(region_3d=region_3d))
    for reference_mesh in (False, True):
        for moving_mesh in (False, True):
            runtime, _delta, reference_world, moving_world = _runtime(reference_mesh, moving_mesh)
            with _display_runtime(runtime), patch.object(labels, "RUNTIME", runtime), patch.object(labels, "bpy", SimpleNamespace(context=context)):
                for preview in (False, True):
                    if preview:
                        begin_marker_preview(runtime)
                    for role in ("REFERENCE", "MOVING"):
                        expected = reference_world if role == "REFERENCE" or preview else moving_world
                        prefix = "A" if role == "REFERENCE" else "P"
                        with patch.object(labels, "_project", wraps=labels._project) as project, patch.object(labels, "draw_text") as draw:
                            labels.draw_viewport_labels(role)
                        assert project.call_count == 4
                        marker_text = {call.args[0]: call.args for call in draw.call_args_list if call.args[0] in {f"{prefix}{index + 1}" for index in range(4)}}
                        assert len(marker_text) == 4
                        circles = renderer._marker_entries(role)
                        for index, projected_call in enumerate(project.call_args_list):
                            np.testing.assert_allclose(projected_call.args[2], expected[index], atol=1e-10)
                            circle = circles[index]
                            np.testing.assert_allclose(renderer._display_marker_position(circle[3], circle[0], circle[4]), expected[index], atol=1e-10)
                            text = marker_text[f"{prefix}{index + 1}"]
                            np.testing.assert_allclose(text[1:3], (409 + 4 * expected[index, 0], 307 + 3 * expected[index, 1]), atol=2e-4)


def test_marker_labels_hide_with_their_geometry_in_completed_and_edit_states():
    """확정·편집 후보 글자를 원 마커와 함께 숨기고 기존 출처·편집 강조를 유지합니다."""
    from tools.align.viewport import labels, renderer

    for use_mesh in (False, True):
        runtime, *_ = _runtime(moving_mesh=use_mesh)
        with _display_runtime(runtime), patch.object(labels, "RUNTIME", runtime):
            for editing in (False, True):
                runtime.model_catalog.set_geometry_visibility("moving", show_points=not use_mesh, show_surface=use_mesh)
                if editing:
                    assert runtime.marker_selection.begin_edit_draft(1)
                entries = labels._marker_labels("MOVING")
                assert len(entries) == len(renderer._marker_entries("MOVING")) == 4
                assert all(entry[-1] == ("COMPANION" if use_mesh else "MAIN") for entry in entries)
                if editing:
                    assert sum(entry[0] == "P1" for entry in entries) == 1
                    assert entries[-1][0] == "P1" and entries[-1][3]
                runtime.model_catalog.set_geometry_visibility("moving", show_points=use_mesh, show_surface=not use_mesh)
                assert labels._marker_labels("MOVING") == []
                assert renderer._marker_entries("MOVING") == []
                runtime.model_catalog.set_geometry_visibility("moving", show_points=not use_mesh, show_surface=use_mesh)
                record = runtime.model_catalog.models["moving"]
                runtime.model_catalog.models["moving"] = replace(record, is_visible=False)
                assert labels._marker_labels("MOVING") == []
                assert renderer._marker_entries("MOVING") == []
                runtime.model_catalog.models["moving"] = record


def run_all():
    """연결 메시 마커의 계산·승인 회귀를 실제 Blender에서 실행합니다."""
    _add_test_paths()
    for name, test in sorted(globals().copy().items()):
        if name.startswith("test_") and callable(test):
            test()
            print("통과:", name, flush=True)


if __name__ == "__main__":
    run_all()
