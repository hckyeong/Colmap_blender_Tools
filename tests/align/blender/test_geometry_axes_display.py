"""형상별 축·추가 회전과 공통 정렬의 표시·피킹·화면 맞춤을 검증합니다."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import Mock, patch

from mathutils import Matrix, Vector
import numpy as np


def _add_source_path() -> None:
    """소스와 인접 회귀 자료를 Blender 모듈 탐색 경로에 추가합니다."""
    for path in (Path(__file__).resolve().parents[3], Path(__file__).resolve().parent):
        if str(path) not in sys.path:
            sys.path.insert(0, str(path))


def _axis_transform(*, mesh: bool = False, rotation=(0.0, 0.0, 0.0)):
    """점군과 연결 메시의 서로 다른 원본 축에 추가 회전을 구성합니다."""
    from tools.align.display_axes import (
        DEFAULT_AXIS_PRESET, RC_OBJ_AXIS_PRESET, create_model_display_transform,
    )

    return create_model_display_transform(
        RC_OBJ_AXIS_PRESET if mesh else DEFAULT_AXIS_PRESET,
        "Y" if mesh else "Z",
        "Z" if mesh else "-Y",
        rotation_xyz_degrees=rotation,
    )


def _rotation_matrix(angle: float, axis: str) -> np.ndarray:
    """엄격한 정렬 입력 정밀도를 만족하는 배정밀도 회전 행렬을 생성합니다."""
    first, second = {"X": (1, 2), "Y": (2, 0), "Z": (0, 1)}[axis]
    matrix = np.identity(3, dtype=np.float64)
    cosine, sine = np.cos(angle), np.sin(angle)
    matrix[first, first] = matrix[second, second] = cosine
    matrix[first, second], matrix[second, first] = -sine, sine
    return matrix


@contextmanager
def _geometry_runtime():
    """기존 연결 모델 자료에 점군·메시 독립 축과 공통 회전·배율·이동을 적용합니다."""
    from test_companion_display import _runtime_fixture
    from tools.align.alignment import create_sim3_transform

    with _runtime_fixture() as runtime:
        repository = runtime.model_repository
        repository.set_display_transform("moving", _axis_transform(rotation=(17.0, -23.0, 31.0)))
        repository.set_companion_display_transform(
            "moving", _axis_transform(mesh=True, rotation=(-11.0, 19.0, 7.0)),
        )
        repository.set_display_transform("standalone", _axis_transform(rotation=(8.0, 13.0, -21.0)))
        alignment = create_sim3_transform(
            _rotation_matrix(0.38, "Z"),
            2.5,
            np.asarray((4.0, -7.0, 3.0)),
        )
        repository.set_alignment_transform("moving", alignment)
        repository.set_alignment_transform("standalone", alignment)
        yield runtime


def _transformed(matrix, positions) -> np.ndarray:
    """독립적인 동차 좌표 계산으로 기대 프로젝트 좌표를 구합니다."""
    values = np.asarray(positions, dtype=np.float64)
    homogeneous = np.column_stack((values, np.ones(len(values))))
    result = homogeneous @ np.asarray(matrix, dtype=np.float64).T
    return result[:, :3] / result[:, 3, np.newaxis]


def _expected_bounds(matrix, model):
    """원본 경계의 여덟 모서리를 변환한 기대 경계를 계산합니다."""
    corners = np.asarray([
        (x, y, z)
        for x in (model.bounds_min[0], model.bounds_max[0])
        for y in (model.bounds_min[1], model.bounds_max[1])
        for z in (model.bounds_min[2], model.bounds_max[2])
    ])
    values = _transformed(matrix, corners)
    return values.min(axis=0), values.max(axis=0)


def _expected_matrix(runtime, *, companion=False, model_id="moving"):
    """확정된 형상별 원본 축에 소유 모델의 공통 정렬을 합성합니다."""
    repository = runtime.model_repository
    transforms = repository.companion_display_transforms if companion else repository.display_transforms
    return repository.alignment_transforms[model_id].matrix_float64 @ transforms[model_id].matrix_float64


def test_geometry_matrices_positions_normals_and_markers() -> None:
    """형상별 위치·법선과 완료 마커가 올바른 축 뒤에 같은 정렬을 적용하는지 확인합니다."""
    from tools.align.viewport import point_data, renderer

    with _geometry_runtime() as runtime:
        point_matrix = _expected_matrix(runtime)
        mesh_matrix = _expected_matrix(runtime, companion=True)
        assert not np.allclose(point_matrix, mesh_matrix)
        source = np.asarray(((0.3, -0.8, 1.1), (2.0, 3.0, -4.0)))
        np.testing.assert_allclose(point_data.effective_positions_for_model_id("moving", source), _transformed(point_matrix, source))
        np.testing.assert_allclose(point_data.effective_positions_for_model_id("moving", source, companion=True), _transformed(mesh_matrix, source))
        np.testing.assert_allclose(renderer._display_marker_position("moving", source[0]), _transformed(point_matrix, source[:1])[0])
        np.testing.assert_allclose(renderer._model_view_projection_matrix(Matrix.Identity(4), "moving"), point_matrix, atol=2e-6)
        view = Matrix.Rotation(0.19, 4, "X")
        projection = Matrix.Diagonal((1.3, 0.9, 0.7, 1.0))
        mvp, normal = renderer._mesh_draw_matrices(projection, view, "moving", companion=True)
        model_view = np.asarray(view) @ mesh_matrix
        np.testing.assert_allclose(mvp, np.asarray(projection) @ model_view, atol=2e-6)
        np.testing.assert_allclose(normal, np.linalg.inv(model_view[:3, :3]).T, atol=2e-6)
        standalone, _normal = renderer._mesh_draw_matrices(Matrix.Identity(4), Matrix.Identity(4), "standalone")
        np.testing.assert_allclose(standalone, _expected_matrix(runtime, model_id="standalone"), atol=2e-6)


def _new_gpu_batch(*_args, **_kwargs):
    """원본 배치 생성 호출마다 구별 가능한 GPU 대체 객체를 만듭니다."""
    return Mock()


def test_changing_one_geometry_keeps_other_matrix_and_raw_gpu_batches() -> None:
    """한쪽 축을 바꿔도 다른 형상 행렬과 원본 배열·GPU 배치를 재생성하지 않는지 확인합니다."""
    from tools.align.viewport import point_data, renderer

    with _geometry_runtime() as runtime, patch.object(renderer, "_point_shader", return_value=Mock()), patch.object(
        renderer, "_mesh_shader", return_value=Mock(),
    ), patch.object(renderer, "batch_for_shader", side_effect=_new_gpu_batch) as factory:
        renderer.ensure_gpu_batches()
        repository = runtime.model_repository
        points = repository.models["moving"].points.positions
        vertices = repository.companion_meshes["moving"].vertices_float64
        original_points, original_vertices = points.copy(), vertices.copy()
        cached = dict(runtime.gpu_batches)
        count = factory.call_count
        main_before = point_data.effective_model_matrix_for_id("moving").copy()
        repository.set_companion_display_transform("moving", _axis_transform(mesh=True, rotation=(90.0, 5.0, 0.0)))
        np.testing.assert_array_equal(point_data.effective_model_matrix_for_id("moving"), main_before)
        surface_before = point_data.effective_model_matrix_for_id("moving", companion=True).copy()
        repository.set_display_transform("moving", _axis_transform(rotation=(0.0, 45.0, 0.0)))
        np.testing.assert_array_equal(point_data.effective_model_matrix_for_id("moving", companion=True), surface_before)
        renderer.ensure_gpu_batches()
        assert factory.call_count == count
        assert set(runtime.gpu_batches) == set(cached)
        assert all(runtime.gpu_batches[key] is batch for key, batch in cached.items())
        assert repository.models["moving"].points.positions is points
        assert repository.companion_meshes["moving"].vertices_float64 is vertices
        np.testing.assert_array_equal(points, original_points)
        np.testing.assert_array_equal(vertices, original_vertices)


def test_draw_submits_surface_axis_and_primary_point_axis() -> None:
    """형상을 차례로 선택해 연결 면·텍스처는 메시 축, 점 배치는 주 축을 받는지 확인합니다."""
    from tools.align.constants import ROLE_MOVING
    from tools.align.viewport import renderer

    with _geometry_runtime() as runtime:
        mesh = runtime.model_repository.companion_meshes["moving"]
        model = runtime.model_repository.models["moving"]
        point_batch, solid_batch, textured_batch = Mock(), Mock(), Mock()
        texture_path = Path("fixture.png")
        runtime.gpu_batches[renderer._batch_key(model)] = point_batch
        runtime.gpu_batches[renderer._mesh_batch_key(mesh)] = renderer._MeshSurfaceBatch(
            solid_batch,
            (renderer._TexturedMeshBatch(textured_batch, texture_path),),
        )
        resource = renderer._SharedTextureResource(object(), object(), False, {"moving"})
        point_shader, solid_shader, texture_shader = Mock(), Mock(), Mock()
        gpu = SimpleNamespace(
            state=Mock(),
            matrix=SimpleNamespace(
                get_projection_matrix=Mock(return_value=Matrix.Identity(4)),
                get_model_view_matrix=Mock(return_value=Matrix.Identity(4)),
            ),
        )
        settings = SimpleNamespace(use_colmap_rgb=True, point_size=3.0)
        bpy = SimpleNamespace(context=SimpleNamespace(scene=SimpleNamespace(colmap_align_settings=settings)))
        with patch.object(renderer, "gpu", gpu), patch.object(renderer, "bpy", bpy), patch.object(
            renderer, "_point_shader", return_value=point_shader,
        ), patch.object(renderer, "_mesh_shader", return_value=solid_shader), patch.object(
            renderer, "_mesh_texture_shader", return_value=texture_shader,
        ), patch.object(renderer, "_MESH_TEXTURE_RESOURCES", {texture_path: resource}):
            runtime.model_catalog.set_geometry_visibility("moving", show_surface=True)
            renderer._draw_model_meshes(("moving",), ROLE_MOVING)
            runtime.model_catalog.set_geometry_visibility("moving", show_points=True)
            renderer._draw_model_point_clouds(("moving",), ROLE_MOVING)
        for shader, expected in ((point_shader, _expected_matrix(runtime)), (solid_shader, _expected_matrix(runtime, companion=True)), (texture_shader, _expected_matrix(runtime, companion=True))):
            matrices = [call.args[1] for call in shader.uniform_float.call_args_list if call.args[0] == "ModelViewProjectionMatrix"]
            assert len(matrices) == 1
            np.testing.assert_allclose(matrices[0], expected, atol=2e-6)
        point_batch.draw.assert_called_once_with(point_shader)
        solid_batch.draw.assert_called_once_with(solid_shader)
        textured_batch.draw.assert_called_once_with(texture_shader)


def test_point_picking_uses_main_axis_and_keeps_source_coordinates() -> None:
    """메시 축과 무관하게 점군의 현재 표시 위치를 집고 원본 좌표·ID를 보존하는지 확인합니다."""
    from tools.align.viewport import picking

    with _geometry_runtime() as runtime:
        points = runtime.model_repository.models["moving"].points
        source_index = 2
        shown = _transformed(_expected_matrix(runtime), points.positions)[source_index]
        view = np.identity(4)
        view[2, 3] = -100.0
        result = picking._pick_model_nearest_point(
            "moving", SimpleNamespace(width=800, height=600),
            SimpleNamespace(view_matrix=view, perspective_matrix=None),
            tuple(shown[:2]), 1e-8,
        )
        assert result is not None
        assert result.point3d_id == int(points.point_ids[source_index])
        np.testing.assert_array_equal(result.position, points.positions[source_index])


def test_independent_obj_ray_pick_reuses_source_bvh_after_axis_change() -> None:
    """독립 OBJ 주 축 변경 후에도 원본 BVH를 재사용하며 정확한 원본 면 좌표를 집는지 확인합니다."""
    from tools.align.viewport import picking

    with _geometry_runtime() as runtime:
        repository = runtime.model_repository
        model = repository.models["standalone"]
        local_position = np.asarray((10.4, 0.6, 0.0))
        cached_tree = picking._mesh_surface_bvh("standalone", model)
        try:
            for rotation in ((8.0, 13.0, -21.0), (41.0, -38.0, 12.0)):
                repository.set_display_transform("standalone", _axis_transform(rotation=rotation))
                matrix = _expected_matrix(runtime, model_id="standalone")
                shown = _transformed(matrix, local_position[np.newaxis, :])[0]
                normal = np.linalg.inv(matrix[:3, :3]).T @ np.asarray((0.0, 0.0, 1.0))
                normal /= np.linalg.norm(normal)
                result = picking._pick_model_mesh_surface_from_ray(
                    "standalone", model, SimpleNamespace(), SimpleNamespace(),
                    shown + normal * 5.0, -normal,
                )
                assert result is not None
                np.testing.assert_allclose(result.position, local_position, atol=1e-11)
                assert abs(result.depth - 5.0) < 1e-11
                assert picking._mesh_surface_bvh("standalone", model) is cached_tree
        finally:
            picking.clear_mesh_surface_pick_cache()


def test_frame_bounds_and_view_center_combine_each_geometry_axis() -> None:
    """독립 축을 적용한 선택 형상만 화면 경계와 중심 계산에 사용하는지 확인합니다."""
    from tools.align.constants import ROLE_MOVING
    from tools.align.operators import workspace

    with _geometry_runtime() as runtime:
        repository = runtime.model_repository
        point_bounds = _expected_bounds(_expected_matrix(runtime), repository.models["moving"])
        mesh_bounds = _expected_bounds(_expected_matrix(runtime, companion=True), repository.companion_meshes["moving"])
        for points, surface, expected in (
            (True, False, point_bounds),
            (False, True, mesh_bounds),
        ):
            runtime.model_catalog.set_geometry_visibility("moving", show_points=points, show_surface=surface)
            actual = workspace._bounds_for_model_ids(["moving"])
            if expected is None:
                assert actual is None
            else:
                np.testing.assert_allclose(actual, expected, atol=3e-6)
        runtime.model_catalog.set_model_visibility("moving", False)
        assert workspace._bounds_for_model_ids(["moving"]) is None
        runtime.model_catalog.set_model_visibility("moving", True)
        runtime.model_catalog.set_geometry_visibility("moving", show_surface=True)
        region_3d = SimpleNamespace(view_location=Vector(), view_distance=1.0)
        area = SimpleNamespace(
            regions=[SimpleNamespace(type="WINDOW", width=800, height=600)],
            spaces=SimpleNamespace(active=SimpleNamespace(lens=50.0, clip_end=1000.0, region_3d=region_3d)),
            tag_redraw=Mock(),
        )
        with patch.object(workspace, "resolve_area", return_value=area):
            assert workspace.fit_role_view(object(), ROLE_MOVING, model_ids=("moving",))
        expected_center = (mesh_bounds[0] + mesh_bounds[1]) * 0.5
        np.testing.assert_allclose(region_3d.view_location, expected_center, atol=3e-6)
        area.tag_redraw.assert_called_once()


def test_dictionary_fixture_fallback_and_missing_real_axis() -> None:
    """최소 사전 자료의 기존 축·항등 호환과 실제 COLMAP 축 누락 오류를 보존하는지 확인합니다."""
    from tools.align.display_axes import identity_display_transform
    from tools.align.model_repository import ModelRepositoryError
    from tools.align.viewport import point_data

    transform = _axis_transform(rotation=(10.0, 20.0, 30.0))
    model = SimpleNamespace(positions=np.zeros((1, 3)))
    runtime = SimpleNamespace(model_repository=SimpleNamespace(models={"fixture": model}, display_transforms={"fixture": transform}))
    with patch.object(point_data, "RUNTIME", runtime):
        assert point_data.display_transform_for_model_id("fixture") is transform
        runtime.model_repository.display_transforms.clear()
        np.testing.assert_array_equal(point_data.display_transform_for_model_id("fixture").matrix_float64, identity_display_transform().matrix_float64)
    with _geometry_runtime() as runtime:
        runtime.model_repository.display_transforms.pop("moving")
        try:
            point_data.display_transform_for_model_id("moving")
        except ModelRepositoryError:
            pass
        else:
            raise AssertionError("실제 COLMAP의 누락된 주 축을 항등으로 대체했습니다.")


def test_temporary_axis_preview_drives_display_pick_and_bounds_without_commit() -> None:
    """축 미리보기 중 두 형상을 경계에 포함하고 취소 때 확정 축과 선택 표시로 복귀합니다."""
    from tools.align.axis_settings import begin_axis_edit, update_axis_edit, cancel_axis_edit
    from tools.align.viewport import point_data, renderer, picking
    from tools.align.operators import workspace

    with _geometry_runtime() as runtime:
        runtime.session.session_active = True
        repository = runtime.model_repository
        main_before = repository.display_transforms["moving"]
        mesh_before = repository.companion_display_transforms["moving"]
        other_before = point_data.effective_model_matrix_for_id("reference").copy()
        main_preview = _axis_transform(rotation=(-35.0, 51.0, 16.0))
        mesh_preview = _axis_transform(mesh=True, rotation=(72.0, 11.0, -28.0))
        token = begin_axis_edit(runtime, "moving")
        try:
            update_axis_edit(runtime, token, main_transform=main_preview, companion_transform=mesh_preview)
            common = repository.alignment_transforms["moving"].matrix_float64
            main_matrix = common @ main_preview.matrix_float64
            mesh_matrix = common @ mesh_preview.matrix_float64
            np.testing.assert_allclose(point_data.effective_model_matrix_for_id("moving"), main_matrix)
            np.testing.assert_allclose(point_data.effective_model_matrix_for_id("moving", companion=True), mesh_matrix)
            np.testing.assert_array_equal(point_data.effective_model_matrix_for_id("reference"), other_before)
            assert repository.display_transforms["moving"] is main_before
            assert repository.companion_display_transforms["moving"] is mesh_before
            model, mesh = repository.models["moving"], repository.companion_meshes["moving"]
            source = model.points.positions[2]
            shown = _transformed(main_matrix, source[np.newaxis, :])[0]
            np.testing.assert_allclose(renderer._display_marker_position("moving", source), shown)
            view = np.identity(4)
            view[2, 3] = -100.0
            result = picking._pick_model_nearest_point(
                "moving", SimpleNamespace(width=800, height=600),
                SimpleNamespace(view_matrix=view, perspective_matrix=None), tuple(shown[:2]), 1e-8,
            )
            assert result is not None and result.point3d_id == int(model.points.point_ids[2])
            point_bounds = _expected_bounds(main_matrix, model)
            mesh_bounds = _expected_bounds(mesh_matrix, mesh)
            combined_bounds = (np.minimum(point_bounds[0], mesh_bounds[0]), np.maximum(point_bounds[1], mesh_bounds[1]))
            np.testing.assert_allclose(workspace._bounds_for_model_ids(["moving"]), combined_bounds, atol=3e-6)
            runtime.model_catalog.set_geometry_visibility("moving", show_surface=True)
            np.testing.assert_allclose(workspace._bounds_for_model_ids(["moving"]), combined_bounds, atol=3e-6)
            update_axis_edit(runtime, token, main_transform=main_preview, companion_transform=mesh_preview, preview_enabled=False)
            np.testing.assert_allclose(point_data.effective_model_matrix_for_id("moving"), _expected_matrix(runtime))
            np.testing.assert_allclose(point_data.effective_model_matrix_for_id("moving", companion=True), _expected_matrix(runtime, companion=True))
            update_axis_edit(runtime, token, main_transform=main_preview, companion_transform=mesh_before)
            np.testing.assert_allclose(point_data.effective_model_matrix_for_id("moving", companion=True), _expected_matrix(runtime, companion=True))
        finally:
            cancel_axis_edit(runtime, token)
        np.testing.assert_allclose(point_data.effective_model_matrix_for_id("moving"), _expected_matrix(runtime))
        np.testing.assert_allclose(point_data.effective_model_matrix_for_id("moving", companion=True), _expected_matrix(runtime, companion=True))
        restored_mesh_bounds = _expected_bounds(_expected_matrix(runtime, companion=True), mesh)
        np.testing.assert_allclose(workspace._bounds_for_model_ids(["moving"]), restored_mesh_bounds, atol=3e-6)


def test_common_alignment_preview_keeps_geometry_axes_independent() -> None:
    """공통 정렬 미리보기에서도 점군과 연결 표면이 각자의 축을 정확히 한 번 사용하는지 확인합니다."""
    from tools.align.alignment import create_sim3_transform
    from tools.align.constants import MARKER_ALIGNMENT_PHASE_PREVIEW
    from tools.align.viewport import point_data, renderer

    with _geometry_runtime() as runtime:
        preview = create_sim3_transform(
            _rotation_matrix(-0.43, "Y"), 1.7, np.asarray((-2.0, 6.0, 11.0)),
        )
        runtime.marker_alignment.phase = MARKER_ALIGNMENT_PHASE_PREVIEW
        runtime.marker_alignment.result = SimpleNamespace(moving_model_id="moving", preview_alignment=preview)
        for companion in (False, True):
            transforms = runtime.model_repository.companion_display_transforms if companion else runtime.model_repository.display_transforms
            expected = preview.matrix_float64 @ transforms["moving"].matrix_float64
            np.testing.assert_allclose(point_data.effective_model_matrix_for_id("moving", companion=companion), expected)
            actual, _normal = renderer._mesh_draw_matrices(Matrix.Identity(4), Matrix.Identity(4), "moving", companion=companion)
            np.testing.assert_allclose(actual, expected, atol=2e-6)


def run_all() -> None:
    """형상별 표시 축 소비 경계와 기존 피킹 호환 회귀를 실행합니다."""
    _add_source_path()
    for name, function in tuple(globals().items()):
        if name.startswith("test_") and callable(function):
            function()
    print("형상별 축 표시·피킹·경계 검증 통과")


if __name__ == "__main__":
    run_all()
