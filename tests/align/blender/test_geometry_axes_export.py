"""형상별 추가 회전의 COLMAP 출력·카메라 투영·희소 정렬 입력 경계를 검증합니다."""

from __future__ import annotations

from dataclasses import fields, replace
from pathlib import Path
import sys
from tempfile import TemporaryDirectory, gettempdir
from unittest.mock import patch

import numpy as np


def _add_test_paths() -> None:
    """소스와 기존 정렬·출력 검증 자료를 Blender 탐색 경로에 추가합니다."""
    for path in (Path(__file__).resolve().parents[3], Path(__file__).resolve().parent):
        if str(path) not in sys.path:
            sys.path.insert(0, str(path))


def _rotation(axis: str, degrees: float) -> np.ndarray:
    """추가 회전의 독립적인 기대값에 사용할 배정밀도 기본 회전 행렬을 만듭니다."""
    first, second = {"X": (1, 2), "Y": (2, 0), "Z": (0, 1)}[axis]
    cosine, sine = np.cos(np.deg2rad(degrees)), np.sin(np.deg2rad(degrees))
    matrix = np.identity(3)
    matrix[first, first] = matrix[second, second] = cosine
    matrix[first, second], matrix[second, first] = -sine, sine
    return matrix


def _display(rotation, *, companion=False):
    """점군·연결 메시 원본 축에 지정한 세 축 추가 회전을 적용합니다."""
    from tools.align.display_axes import (
        DEFAULT_AXIS_PRESET, RC_OBJ_AXIS_PRESET, create_model_display_transform,
    )

    return create_model_display_transform(
        RC_OBJ_AXIS_PRESET if companion else DEFAULT_AXIS_PRESET,
        "Y" if companion else "Z", "Z" if companion else "-Y",
        rotation_xyz_degrees=rotation,
    )


def _transform_positions(matrix, positions) -> np.ndarray:
    """제품 좌표 변환과 독립적으로 동차 좌표의 기대값을 계산합니다."""
    homogeneous = np.column_stack((positions, np.ones(len(positions))))
    result = homogeneous @ matrix.T
    return result[:, :3] / result[:, 3, np.newaxis]


def _with_camera(model):
    """기존 COLMAP 카메라 자료에 희소 점별 유효 관측과 비영 카메라 자세를 연결합니다."""
    from test_merge_export_transforms import _legacy_model
    from tools.align.colmap_io.projection import project_world_point
    from tools.align.colmap_io.types import ImageTable
    from tools.align.merge_export.transforms import rotation_translation_to_pose

    legacy = _legacy_model()
    camera = legacy.cameras[3]
    pose = rotation_translation_to_pose(
        _rotation("Y", 7.0) @ _rotation("Z", 12.0), np.asarray((-0.3, 0.6, 8.0)),
    )
    image = replace(legacy.images.image_records[9], name=f"{model.model_id}/image.jpg", cam_from_world=pose)
    count = len(model.points.positions)
    observations = np.asarray([
        project_world_point(camera, pose, point) for point in model.points.positions
    ])
    points = replace(
        model.points,
        track_offsets=np.arange(count + 1, dtype=np.uint64),
        track_image_ids=np.full(count, image.image_id, dtype=np.uint64),
        track_point2d_indices=np.arange(count, dtype=np.uint32),
    )
    return replace(
        model, cameras=legacy.cameras, points=points,
        images=ImageTable(
            image_records={image.image_id: image},
            point2d_offsets=np.asarray((0, count), dtype=np.uint64),
            point2d_xy=observations, point2d_point3d_ids=points.point_ids,
        ),
    )


def _runtime():
    """기존 직접 정렬 자료에 카메라·출처·연결 메시와 서로 다른 세 축 회전을 구성합니다."""
    from test_companion_display import _mesh
    from test_icp_direct_align import _runtime_with_current_alignment
    from tools.align.model_repository import ImportedModelSourceMetadata, CompanionMeshSourceMetadata

    runtime, _alignment = _runtime_with_current_alignment()
    repository = runtime.model_repository
    for model_id, main_angles, mesh_angles in (
        ("reference", (-13.0, 21.0, 29.0), (7.0, -17.0, 11.0)),
        ("moving", (17.0, -26.0, 38.0), (-19.0, 14.0, 23.0)),
    ):
        model = _with_camera(repository.models[model_id])
        repository.models[model_id] = model
        imported = repository.display_transforms[model_id]
        repository.source_metadata[model_id] = ImportedModelSourceMetadata(
            selected_root_absolute=str(model.source_path),
            sparse_path_absolute=str(model.source_path), source_format=model.source_format,
            source_fingerprint="a" * 64, display_point_limit=250_000,
            axis_preset=imported.preset_id, axis_forward=imported.forward_axis,
            axis_up=imported.up_axis, display_transform_fingerprint=imported.fingerprint,
        )
        mesh = _mesh(model_id)
        repository.add_companion_mesh(
            model_id, mesh, CompanionMeshSourceMetadata(str(mesh.source_path), "b" * 64),
            _display(mesh_angles, companion=True),
        )
        repository.set_display_transform(model_id, _display(main_angles))
    return runtime


def _source_snapshot(runtime):
    """원본 모델·출처 객체와 모든 점·관측·카메라 배열의 원시 바이트를 보관합니다."""
    repository = runtime.model_repository
    objects = {
        name: dict(getattr(repository, name))
        for name in ("models", "source_metadata", "companion_meshes", "companion_sources")
    }
    arrays = {}
    for model_id, model in repository.models.items():
        for table_name in ("points", "images"):
            table = getattr(model, table_name)
            for field in fields(table):
                value = getattr(table, field.name)
                if isinstance(value, np.ndarray):
                    arrays[(model_id, table_name, field.name)] = value.tobytes()
        for image_id, image in model.images.image_records.items():
            arrays[(model_id, "pose", image_id)] = image.cam_from_world.tobytes()
        for camera_id, camera in model.cameras.items():
            arrays[(model_id, "camera", camera_id)] = camera.params_float64.tobytes()
    for model_id, mesh in repository.companion_meshes.items():
        arrays[(model_id, "mesh", "vertices")] = mesh.vertices_float64.tobytes()
        arrays[(model_id, "mesh", "triangles")] = mesh.triangles_uint32.tobytes()
    return objects, arrays, dict(repository.source_path_index)


def _assert_source_unchanged(runtime, before) -> None:
    """축 변경과 내보내기가 최초 원본 객체·바이트·경로 인덱스를 변경하지 않았는지 확인합니다."""
    objects, arrays, source_paths = _source_snapshot(runtime)
    before_objects, before_arrays, before_paths = before
    assert arrays == before_arrays and source_paths == before_paths
    for name, mapping in before_objects.items():
        assert set(objects[name]) == set(mapping)
        assert all(objects[name][key] is value for key, value in mapping.items())


def _export_snapshot(runtime, *, restore_import_axes=True):
    """두 COLMAP을 실제 출력 대상 선택 경로에 전달하고 정렬용 그룹 상태는 복원합니다."""
    from tools.align.operators import export_model

    catalog = runtime.model_catalog
    before = catalog.snapshot()
    try:
        catalog.promote_model_for_icp_commit("moving")
        with patch.object(export_model, "RUNTIME", runtime):
            snapshot = export_model._build_merge_export_snapshot(
                Path(gettempdir()) / "geometry_axes_export_snapshot", restore_import_axes=restore_import_axes,
            )
    finally:
        catalog.restore_snapshot(before)
    assert tuple(source.model_id for source in snapshot.source_models) == ("reference", "moving")
    assert snapshot.excluded_model_ids == ()
    return snapshot


def _baked_models(runtime, snapshot):
    """실제 출력 작업 처리기의 원본 굽기 경로로 점·카메라·프레임을 생성합니다."""
    from tools.align.merge_export.preflight import LoadedMergeSource
    from tools.align.merge_export.worker import _bake_source

    anchor = next(source for source in snapshot.source_models if source.model_id == snapshot.anchor_model_id)
    return {
        source.model_id: _bake_source(
            LoadedMergeSource(index, source, runtime.model_repository.models[source.model_id]),
            anchor.display_matrix_float64, restore_import_axes=snapshot.restore_import_axes,
        ).model
        for index, source in enumerate(snapshot.source_models)
    }


def _expected_output_matrix(snapshot, source):
    """출력 정책에 따라 앵커 원본 축 또는 프로젝트 좌표의 기대 행렬을 계산합니다."""
    project_matrix = source.alignment_matrix_float64 @ source.display_matrix_float64
    if not snapshot.restore_import_axes:
        return project_matrix
    anchor = next(item for item in snapshot.source_models if item.model_id == snapshot.anchor_model_id)
    return np.linalg.inv(anchor.display_matrix_float64) @ project_matrix


def _small_gicp_job_input(runtime):
    """외부 실행 없이 실제 Small GICP 입력 NPY·신뢰도·지문을 준비하고 메모리로 읽습니다."""
    from tools.align.alignment.icp_backend import IcpJobFiles
    from tools.align.alignment.icp_runtime import _prepare_icp_job_input
    from tools.align.alignment.icp_solver_backend import backend_algorithm_version
    from tools.align.constants import ICP_ALIGNMENT_PHASE_PREPARING

    state = runtime.icp_alignment
    previous_phase = state.phase
    try:
        state.phase = ICP_ALIGNMENT_PHASE_PREPARING
        with TemporaryDirectory(prefix="geometry_axes_small_gicp_") as temporary:
            files = IcpJobFiles(Path(temporary))
            data, entries = _prepare_icp_job_input(
                runtime, files, "BALANCED", False, "SMALL_GICP", backend_algorithm_version("SMALL_GICP"),
            )
            arrays = {
                (entry["model_id"], key): np.load(files.job_directory / filename, allow_pickle=False)
                for entry in (entries["moving"], *entries["references"])
                for key, filename in entry.items()
                if key.endswith("_file")
            }
    finally:
        state.phase = previous_phase
    assert data.solver_backend == "SMALL_GICP" and data.moving_cloud_frame == "INITIAL_PROJECT_WORLD"
    assert "small_gicp" not in sys.modules
    return data, arrays


def _assert_same_sparse_input(first, second) -> None:
    """연결 메시 축 변경 전후 희소 정렬 입력의 지문·점·ID·모델 범위가 같은지 확인합니다."""
    assert first.input_fingerprint == second.input_fingerprint
    assert first.model_state_snapshot == second.model_state_snapshot
    assert first.reference_model_ids == second.reference_model_ids == ("reference",)
    np.testing.assert_array_equal(first.moving_positions, second.moving_positions)
    np.testing.assert_array_equal(first.moving_point_ids, second.moving_point_ids)
    np.testing.assert_array_equal(first.reference_positions[0], second.reference_positions[0])
    np.testing.assert_array_equal(first.reference_point_ids[0], second.reference_point_ids[0])


def test_xyz_rotation_order_and_relative_mesh_axis_contract() -> None:
    """비영 XYZ 회전이 Rz·Ry·Rx 순서를 따르고 메시 상대 축이 역주축·메시축인지를 확인합니다."""
    angles = (23.0, -31.0, 47.0)
    transform = _display(angles)
    base = _display((0.0, 0.0, 0.0))
    expected_q = _rotation("Z", angles[2]) @ _rotation("Y", angles[1]) @ _rotation("X", angles[0])
    np.testing.assert_allclose(transform.matrix_float64[:3, :3], expected_q @ base.matrix_float64[:3, :3], rtol=0.0, atol=1e-14)
    reverse_q = _rotation("X", angles[0]) @ _rotation("Y", angles[1]) @ _rotation("Z", angles[2])
    assert not np.allclose(expected_q, reverse_q)
    mesh_transform = _display((-14.0, 22.0, -37.0), companion=True)
    relative = transform.inverse_float64 @ mesh_transform.matrix_float64
    vertices = np.asarray(((1.7, -2.6, 3.9), (-4.1, 0.8, 2.2)))
    np.testing.assert_allclose(
        _transform_positions(transform.matrix_float64, _transform_positions(relative, vertices)),
        _transform_positions(mesh_transform.matrix_float64, vertices), rtol=0.0, atol=2e-14,
    )


def test_actual_colmap_bake_roundtrip_preserves_projection_and_anchor_axes() -> None:
    """실제 출력 굽기·텍스트 왕복에서 점·카메라 투영과 앵커 원본 좌표 계약을 확인합니다."""
    from tools.align.colmap_io import load_sparse_model
    from tools.align.colmap_io.projection import project_world_point
    from tools.align.colmap_io.text_writer import write_text_model
    from tools.align.merge_export.transforms import camera_center_from_pose

    runtime = _runtime()
    original = _source_snapshot(runtime)
    with TemporaryDirectory(prefix="geometry_axes_export_") as temporary:
        for restore in (False, True):
            snapshot = _export_snapshot(runtime, restore_import_axes=restore)
            baked = _baked_models(runtime, snapshot)
            for source in snapshot.source_models:
                model_id = source.model_id
                model = runtime.model_repository.models[model_id]
                expected_matrix = _expected_output_matrix(snapshot, source)
                np.testing.assert_allclose(baked[model_id].points.positions, _transform_positions(expected_matrix, model.points.positions), rtol=0.0, atol=2e-13)
                if restore and source.is_anchor:
                    np.testing.assert_allclose(expected_matrix, np.identity(4), rtol=0.0, atol=2e-14)
                    np.testing.assert_allclose(baked[model_id].points.positions, model.points.positions, rtol=0.0, atol=2e-13)
                folder = Path(temporary) / str(restore) / model_id
                written = write_text_model(baked[model_id], folder)
                assert len(written) == 5
                loaded = load_sparse_model(folder)
                assert len(loaded.points.point_ids) == len(model.points.point_ids)
                assert len(loaded.images.image_records) == len(model.images.image_records) and len(loaded.cameras) == len(model.cameras)
                np.testing.assert_allclose(loaded.points.positions, baked[model_id].points.positions, rtol=0.0, atol=2e-13)
                camera = model.cameras[3]
                original_pose = model.images.image_records[9].cam_from_world
                exported_pose = loaded.images.image_records[9].cam_from_world
                np.testing.assert_array_equal(loaded.cameras[3].params_float64, camera.params_float64)
                expected_center = _transform_positions(expected_matrix, camera_center_from_pose(original_pose)[np.newaxis, :])[0]
                np.testing.assert_allclose(camera_center_from_pose(exported_pose), expected_center, rtol=0.0, atol=2e-12)
                for source_point, exported_point in zip(model.points.positions, loaded.points.positions, strict=True):
                    np.testing.assert_allclose(
                        project_world_point(camera, original_pose, source_point),
                        project_world_point(loaded.cameras[3], exported_pose, exported_point),
                        rtol=0.0, atol=2e-10,
                    )
                if restore and source.is_anchor:
                    np.testing.assert_allclose(exported_pose, original_pose, rtol=0.0, atol=2e-13)
    _assert_source_unchanged(runtime, original)


def test_companion_axis_changes_leave_sparse_input_and_colmap_output_unchanged() -> None:
    """메시 축만 바꾸면 희소 정렬 지문·값과 COLMAP 출력 행렬·대상 수·좌표가 유지되는지 확인합니다."""
    from tools.align.alignment import create_icp_input_snapshot

    runtime = _runtime()
    original = _source_snapshot(runtime)
    first_input = create_icp_input_snapshot(runtime)
    first_job, first_arrays = _small_gicp_job_input(runtime)
    first_exports = {restore: _export_snapshot(runtime, restore_import_axes=restore) for restore in (False, True)}
    first_baked = {restore: _baked_models(runtime, snapshot) for restore, snapshot in first_exports.items()}
    for model_id, angles in (("reference", (43.0, -8.0, 61.0)), ("moving", (-52.0, 33.0, -16.0))):
        runtime.model_repository.set_companion_display_transform(model_id, _display(angles, companion=True))
    second_input = create_icp_input_snapshot(runtime)
    _assert_same_sparse_input(first_input, second_input)
    second_job, second_arrays = _small_gicp_job_input(runtime)
    assert first_job.input_fingerprint == second_job.input_fingerprint
    assert first_job.model_state_snapshot == second_job.model_state_snapshot
    assert set(first_arrays) == set(second_arrays)
    for key in first_arrays:
        np.testing.assert_array_equal(first_arrays[key], second_arrays[key])
    for model_id in ("reference", "moving"):
        repository = runtime.model_repository
        expected = repository.alignment_transforms[model_id].matrix_float64 @ repository.display_transforms[model_id].matrix_float64
        np.testing.assert_allclose(
            second_arrays[(model_id, "positions_file")],
            _transform_positions(expected, repository.models[model_id].points.positions), rtol=0.0, atol=2e-13,
        )
    assert len(second_input.moving_positions) == len(runtime.model_repository.models["moving"].points.point_ids)
    assert len(second_input.reference_positions[0]) == len(runtime.model_repository.models["reference"].points.point_ids)
    for restore in (False, True):
        first, second = first_exports[restore], _export_snapshot(runtime, restore_import_axes=restore)
        assert first.runtime_fingerprint == second.runtime_fingerprint
        assert len(first.source_models) == len(second.source_models) == 2
        second_baked = _baked_models(runtime, second)
        for before, after in zip(first.source_models, second.source_models, strict=True):
            assert before.model_id == after.model_id
            assert before.expected_source_fingerprint == after.expected_source_fingerprint
            np.testing.assert_array_equal(before.display_matrix_float64, after.display_matrix_float64)
            np.testing.assert_array_equal(_expected_output_matrix(first, before), _expected_output_matrix(second, after))
            np.testing.assert_array_equal(first_baked[restore][before.model_id].points.positions, second_baked[after.model_id].points.positions)
            np.testing.assert_array_equal(first_baked[restore][before.model_id].images.image_records[9].cam_from_world, second_baked[after.model_id].images.image_records[9].cam_from_world)
    _assert_source_unchanged(runtime, original)
    assert "small_gicp" not in sys.modules


def test_primary_axis_changes_refresh_sparse_input_and_colmap_output() -> None:
    """COLMAP 축 변경은 희소 입력·출력만 갱신하고 앵커 복원·원본·출처를 유지하는지 확인합니다."""
    from tools.align.alignment import create_icp_input_snapshot

    runtime = _runtime()
    original = _source_snapshot(runtime)
    before_input = create_icp_input_snapshot(runtime)
    before_job, before_arrays = _small_gicp_job_input(runtime)
    before_export = _export_snapshot(runtime)
    before_baked = _baked_models(runtime, before_export)
    runtime.model_repository.set_display_transform("moving", _display((-42.0, 37.0, -18.0)))
    moving_input = create_icp_input_snapshot(runtime)
    moving_job, moving_arrays = _small_gicp_job_input(runtime)
    assert before_job.input_fingerprint != moving_job.input_fingerprint
    assert not np.allclose(before_arrays[("moving", "positions_file")], moving_arrays[("moving", "positions_file")])
    np.testing.assert_array_equal(before_arrays[("reference", "positions_file")], moving_arrays[("reference", "positions_file")])
    moving_export = _export_snapshot(runtime)
    moving_baked = _baked_models(runtime, moving_export)
    assert before_input.input_fingerprint != moving_input.input_fingerprint
    assert not np.allclose(before_input.moving_positions, moving_input.moving_positions)
    np.testing.assert_array_equal(before_input.reference_positions[0], moving_input.reference_positions[0])
    np.testing.assert_array_equal(before_input.moving_point_ids, moving_input.moving_point_ids)
    assert before_export.runtime_fingerprint != moving_export.runtime_fingerprint
    assert not np.allclose(
        _expected_output_matrix(before_export, before_export.source_models[1]),
        _expected_output_matrix(moving_export, moving_export.source_models[1]),
    )
    assert len(before_export.source_models) == len(moving_export.source_models) == 2
    assert not np.allclose(before_baked["moving"].points.positions, moving_baked["moving"].points.positions)
    np.testing.assert_array_equal(before_baked["reference"].points.positions, moving_baked["reference"].points.positions)
    runtime.model_repository.set_display_transform("reference", _display((39.0, -24.0, 56.0)))
    anchor_input = create_icp_input_snapshot(runtime)
    anchor_job, anchor_arrays = _small_gicp_job_input(runtime)
    assert moving_job.input_fingerprint != anchor_job.input_fingerprint
    assert not np.allclose(moving_arrays[("reference", "positions_file")], anchor_arrays[("reference", "positions_file")])
    np.testing.assert_array_equal(moving_arrays[("moving", "positions_file")], anchor_arrays[("moving", "positions_file")])
    anchor_export = _export_snapshot(runtime)
    anchor_baked = _baked_models(runtime, anchor_export)
    assert moving_input.input_fingerprint != anchor_input.input_fingerprint
    assert not np.allclose(moving_input.reference_positions[0], anchor_input.reference_positions[0])
    np.testing.assert_array_equal(moving_input.moving_positions, anchor_input.moving_positions)
    assert moving_export.runtime_fingerprint != anchor_export.runtime_fingerprint
    assert len(anchor_export.source_models) == 2
    assert not np.allclose(
        _expected_output_matrix(moving_export, moving_export.source_models[1]),
        _expected_output_matrix(anchor_export, anchor_export.source_models[1]),
    )
    assert not np.allclose(moving_baked["moving"].points.positions, anchor_baked["moving"].points.positions)
    np.testing.assert_allclose(anchor_baked["reference"].points.positions, runtime.model_repository.models["reference"].points.positions, rtol=0.0, atol=2e-13)
    _assert_source_unchanged(runtime, original)
    assert "small_gicp" not in sys.modules


def run_all() -> None:
    """추가 회전의 실제 출력·희소 입력 검사를 네이티브 정렬 모듈 없이 실행합니다."""
    _add_test_paths()
    assert "small_gicp" not in sys.modules
    for name, function in tuple(globals().items()):
        if name.startswith("test_") and callable(function):
            function()
    assert "small_gicp" not in sys.modules
    print("형상별 축 COLMAP 출력·투영·희소 입력 검증 통과")


if __name__ == "__main__":
    run_all()
