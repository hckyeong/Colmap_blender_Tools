"""연결 OBJ의 단일 형상 표시·캐시·피킹·화면 맞춤 계약을 검증합니다."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
from mathutils import Matrix


def _add_source_path() -> None:
    """실험 소스를 Blender 테스트 모듈 탐색 경로에 추가합니다."""
    source = Path(__file__).resolve().parents[3]
    if str(source) not in sys.path:
        sys.path.insert(0, str(source))


def _colmap(model_id: str):
    """연결 표면과 동일 샘플 수를 갖는 실제 COLMAP 테스트 모델을 만듭니다."""
    from tools.align.colmap_io.types import ColmapModelData, ImageTable, PointTable

    positions = np.asarray(((0, 0, 0), (1, 0, 0), (0, 1, 0)), dtype=np.float64)
    return ColmapModelData(
        model_id=model_id,
        display_name=model_id,
        source_path=Path.cwd() / model_id,
        source_format="text",
        cameras={},
        images=ImageTable(
            image_records={},
            point2d_offsets=np.zeros(1, dtype=np.uint64),
            point2d_xy=np.empty((0, 2), dtype=np.float64),
            point2d_point3d_ids=np.empty(0, dtype=np.uint64),
        ),
        rigs={}, frames={},
        points=PointTable(
            point_ids=np.arange(11, 14, dtype=np.uint64),
            positions=positions,
            colors=np.full((3, 3), 255, dtype=np.uint8),
            errors=np.zeros(3, dtype=np.float64),
            track_offsets=np.zeros(4, dtype=np.uint64),
            track_image_ids=np.empty(0, dtype=np.uint64),
            track_point2d_indices=np.empty(0, dtype=np.uint32),
            display_indices=np.arange(3, dtype=np.uint32),
        ),
        bounds_min=positions.min(axis=0), bounds_max=positions.max(axis=0),
    )


def _mesh(model_id: str):
    """점군 바깥에 배치된 연결·독립 표면 테스트 형상을 만듭니다."""
    from tools.align.mesh_io.types import TriangleMeshModelData

    vertices = np.asarray(((10, 0, 0), (12, 0, 0), (10, 2, 0)), dtype=np.float64)
    return TriangleMeshModelData(
        model_id=model_id, display_name=model_id,
        source_path=Path.cwd() / (model_id + ".obj"),
        vertices_float64=vertices,
        triangles_uint32=np.asarray(((0, 1, 2),), dtype=np.uint32),
        registration_vertex_indices=np.arange(3, dtype=np.uint32),
        display_vertex_indices=np.arange(3, dtype=np.uint32),
        bounds_min=vertices.min(axis=0), bounds_max=vertices.max(axis=0),
    )


@contextmanager
def _runtime_fixture():
    """두 COLMAP 소유자와 독립 OBJ를 격리 런타임에 구성합니다."""
    from contextlib import ExitStack
    from tools.align import state
    from tools.align.alignment import create_sim3_transform
    from tools.align.display_axes import identity_display_transform
    from tools.align.model_repository import CompanionMeshSourceMetadata
    from tools.align.operators import workspace, model_group
    from tools.align.viewport import renderer, picking, point_data

    runtime = state.RuntimeState()
    for model_id in ("reference", "moving"):
        model = _colmap(model_id)
        mesh = _mesh(model_id)
        runtime.model_repository.add_model(model, identity_display_transform())
        runtime.model_catalog.add_imported_model(model_id, model_id, select_after_import=True)
        runtime.model_repository.add_companion_mesh(
            model_id, mesh,
            CompanionMeshSourceMetadata(str(mesh.source_path), "a" * 64),
        )
    standalone = _mesh("standalone")
    runtime.model_repository.add_model(standalone, identity_display_transform())
    runtime.model_catalog.add_imported_model(
        "standalone", "standalone", select_after_import=False, geometry_kind="TRIANGLE_MESH",
    )
    runtime.model_repository.set_alignment_transform(
        "moving", create_sim3_transform(np.identity(3), 2.0, np.asarray((4, 5, 6))),
    )
    with ExitStack() as stack:
        for module in (renderer, picking, point_data, workspace, model_group):
            stack.enter_context(patch.object(module, "RUNTIME", runtime))
        yield runtime


def test_visibility_keeps_selection_alignment_and_group_undo() -> None:
    """단일 형상 선택·눈·그룹 되돌리기가 선택과 정렬을 변경하지 않는지 확인합니다."""
    from tools.align.viewport import point_data

    with _runtime_fixture() as runtime:
        catalog = runtime.model_catalog
        before = catalog.snapshot()
        alignment = runtime.model_repository.alignment_transforms["moving"]
        for points, surface in ((False, True), (True, False)):
            assert catalog.set_geometry_visibility("moving", show_points=points, show_surface=surface)
            assert point_data.model_geometry_is_visible("moving", "COLMAP_POINT_CLOUD") == points
            assert point_data.model_geometry_is_visible("moving", "TRIANGLE_MESH") == surface
            assert catalog.selected_model_ids == before.selected_model_ids
            assert catalog.models["moving"].registration_status == before.records[1].registration_status
            assert runtime.model_repository.alignment_transforms["moving"] is alignment
        catalog.set_geometry_visibility("moving", show_points=False, show_surface=True)
        catalog.set_model_visibility("moving", False)
        assert not point_data.model_geometry_is_visible("moving", "TRIANGLE_MESH")
        assert not point_data.model_geometry_is_visible("moving", "COLMAP_POINT_CLOUD")
        catalog.set_model_visibility("moving", True)
        catalog.restore_group_snapshot(before)
        assert not catalog.models["moving"].show_points
        assert catalog.models["moving"].show_surface
        catalog.restore_snapshot(before)
        assert catalog.models["moving"].show_points
        assert not catalog.models["moving"].show_surface
        assert not catalog.set_geometry_visibility("missing", show_points=False)


def test_gpu_point_and_surface_caches_coexist_and_lazy_prepare() -> None:
    """공유 위치 배치를 포함한 점·면 캐시가 공존하고 표시 변경 때 재생성되지 않는지 확인합니다."""
    from tools.align.viewport import renderer
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from test_mesh_normal_reuse import _capture_shared_batches

    with _runtime_fixture() as runtime:
        with patch.object(renderer, "_point_shader", return_value=object()), patch.object(
            renderer, "_mesh_shader", return_value=object(),
        ), patch.object(renderer, "batch_for_shader", side_effect=lambda *args, **kwargs: object()) as factory, patch.object(
            renderer, "_shared_mesh_batches", side_effect=_capture_shared_batches(factory),
        ):
            model = runtime.model_repository.models["moving"]
            mesh = runtime.model_repository.companion_meshes["moving"]
            points = renderer.prepare_gpu_batch_for_model("moving", model)
            renderer.prepare_gpu_batch_for_model("moving", mesh)
            assert renderer._batch_key(model) != renderer._batch_key(mesh)
            assert renderer._prepared_batch_for_model("moving") is points
            surface = runtime.gpu_batches[renderer._mesh_batch_key(mesh)]
            assert surface.solid_batch is not None and not surface.textured_batches
            renderer.ensure_gpu_batches()
            count = factory.call_count
            keys = dict(runtime.gpu_batches)
            runtime.model_catalog.set_model_visibility("moving", False)
            renderer.ensure_gpu_batches()
            assert factory.call_count == count
            assert runtime.gpu_batches == keys
            changed = replace(model, geometry_revision=model.geometry_revision + 1)
            renderer.prepare_gpu_batch_for_model("moving", changed)
            assert runtime.gpu_batches[renderer._mesh_batch_key(mesh)] is surface
            assert len([key for key in runtime.gpu_batches if key[0] == "moving"]) == 3
            renderer.discard_gpu_batch_for_model("moving")
            assert all(key[0] != "moving" for key in runtime.gpu_batches)
            assert any(key[0] == "reference" for key in runtime.gpu_batches)


def test_draw_submissions_apply_toggles_in_both_views_and_preview() -> None:
    """각 레이어의 단일 형상 선택이 일반·복수 선택·미리보기 제출에 적용되는지 확인합니다."""
    from tools.align.constants import ROLE_MOVING, ROLE_REFERENCE, MARKER_ALIGNMENT_PHASE_PREVIEW
    from tools.align.viewport import renderer

    with _runtime_fixture() as runtime, patch.object(renderer, "_draw_model_point_clouds") as points, patch.object(
        renderer, "_draw_model_meshes",
    ) as meshes:
        runtime.model_catalog.set_geometry_visibility("moving", show_points=False, show_surface=True)
        renderer._draw_point_clouds(ROLE_MOVING)
        renderer._draw_meshes(ROLE_MOVING)
        assert points.call_count == 0
        assert meshes.call_args.args == (("moving",), ROLE_MOVING)
        runtime.marker_alignment.phase = MARKER_ALIGNMENT_PHASE_PREVIEW
        runtime.marker_alignment.result = SimpleNamespace(moving_model_id="moving")
        points.reset_mock()
        meshes.reset_mock()
        renderer._draw_point_clouds(ROLE_MOVING)
        renderer._draw_meshes(ROLE_MOVING)
        assert points.call_args.args == (("reference",), ROLE_REFERENCE)
        assert points.call_args.kwargs["opacity"] == 1.0
        assert points.call_args.kwargs["depth_write"] is True
        assert points.call_args.kwargs["depth_test"] is None
        assert meshes.call_count == 1 and meshes.call_args.args[0] == ("moving",)
        assert meshes.call_args.kwargs["opacity"] == 1.0
        assert meshes.call_args.kwargs["depth_write"] is True
        assert meshes.call_args.kwargs["depth_test"] is None
        runtime.model_catalog.set_geometry_visibility("reference", show_surface=True)
        points.reset_mock()
        meshes.reset_mock()
        renderer._draw_point_clouds(ROLE_MOVING)
        renderer._draw_meshes(ROLE_MOVING)
        assert points.call_count == 0
        assert [call.args[0] for call in meshes.call_args_list] == [("moving",), ("reference",)]
        assert all(call.kwargs["opacity"] == 1.0 for call in meshes.call_args_list)
        assert all(call.kwargs["depth_write"] is True for call in meshes.call_args_list)
        assert all(call.kwargs["depth_test"] is None for call in meshes.call_args_list)
        runtime.model_catalog.set_geometry_visibility("reference", show_points=True)
        runtime.marker_alignment.clear()
        runtime.model_catalog.toggle_alignment_selection("reference", has_marker_draft=False, completed_pair_count=0)
        for role in (ROLE_REFERENCE, ROLE_MOVING):
            points.reset_mock()
            meshes.reset_mock()
            renderer._draw_point_clouds(role)
            renderer._draw_meshes(role)
            assert points.call_args.args == (("reference",), ROLE_REFERENCE)
            assert meshes.call_args.args == (("moving",), ROLE_MOVING)
        runtime.model_catalog.set_model_visibility("moving", False)
        meshes.reset_mock()
        renderer._draw_meshes(ROLE_MOVING)
        assert meshes.call_count == 0


def test_surface_and_points_share_owner_effective_preview_transform() -> None:
    """연결 표면과 점군의 행렬이 소유자 확정·미리보기 변환과 정확히 같은지 확인합니다."""
    from tools.align.alignment import create_sim3_transform
    from tools.align.constants import MARKER_ALIGNMENT_PHASE_PREVIEW
    from tools.align.viewport import renderer, point_data

    with _runtime_fixture() as runtime:
        for preview in (False, True):
            if preview:
                runtime.marker_alignment.phase = MARKER_ALIGNMENT_PHASE_PREVIEW
                runtime.marker_alignment.result = SimpleNamespace(
                    moving_model_id="moving",
                    preview_alignment=create_sim3_transform(np.identity(3), 3.0, np.asarray((7, 8, 9))),
                )
            expected = point_data.effective_model_matrix_for_id("moving")
            point_mvp = renderer._model_view_projection_matrix(Matrix.Identity(4), "moving")
            surface_mvp, _normal = renderer._mesh_draw_matrices(Matrix.Identity(4), Matrix.Identity(4), "moving")
            np.testing.assert_allclose(point_mvp, expected)
            np.testing.assert_allclose(surface_mvp, expected)
            assert point_data.surface_model_data_for_id("moving").model_id == "moving"


def test_hidden_points_are_not_picked_and_existing_markers_remain() -> None:
    """메시 표시 중 숨긴 점군은 피킹·마커 표시에서 제외하지만 저장 마커는 보존합니다."""
    from tools.align.constants import ROLE_MOVING
    from tools.align.state import SelectedPoint
    from tools.align.viewport import picking, renderer

    with _runtime_fixture() as runtime, patch.object(picking, "_pick_model_nearest_point", return_value=None) as nearest, patch.object(
        picking, "_pick_model_mesh_surface_from_ray", return_value=None,
    ):
        runtime.model_catalog.set_geometry_visibility("moving", show_surface=True)
        assert picking.marker_source_model_ids(ROLE_MOVING) == ("moving",)
        assert picking.marker_pick_model_ids(ROLE_MOVING) == ("moving",)
        region = SimpleNamespace(width=800, height=600)
        region_3d = SimpleNamespace(view_matrix=np.identity(4), perspective_matrix=np.identity(4))
        assert picking.pick_nearest_point_for_model_ids(("moving",), region, region_3d, (400, 300)) is None
        assert nearest.call_count == 0
        runtime.marker_selection.completed_pairs = [SimpleNamespace(
            pair_id=1,
            moving=SelectedPoint(model_role=ROLE_MOVING, point3d_id=11, model_id="moving", local_position=(0, 0, 0)),
        )]
        assert len(renderer._marker_entries(ROLE_MOVING)) == 0
        assert len(runtime.marker_selection.completed_pairs) == 1
        runtime.model_catalog.set_geometry_visibility("moving", show_points=True)
        assert len(renderer._marker_entries(ROLE_MOVING)) == 1
        runtime.model_catalog.select_model("standalone", has_marker_draft=False, completed_pair_count=0)
        assert picking.marker_pick_model_ids(ROLE_MOVING) == ("standalone",)
        runtime.model_catalog.set_model_visibility("standalone", False)
        assert picking.marker_pick_model_ids(ROLE_MOVING) == ()


def test_frame_bounds_use_only_visible_geometry_with_owner_transform() -> None:
    """선택한 형상 경계만 화면 맞춤에 사용하고 눈을 끄면 경계에서도 제외합니다."""
    from tools.align.operators import workspace
    from tools.align.viewport import point_data

    with _runtime_fixture() as runtime:
        model = runtime.model_repository.models["moving"]
        mesh = runtime.model_repository.companion_meshes["moving"]
        point_bounds = point_data.effective_bounds_for_model_id("moving", model.bounds_min, model.bounds_max)
        mesh_bounds = point_data.effective_bounds_for_model_id("moving", mesh.bounds_min, mesh.bounds_max, companion=True)
        for points, surface, expected in (
            (True, False, point_bounds),
            (False, True, mesh_bounds),
        ):
            runtime.model_catalog.set_geometry_visibility("moving", show_points=points, show_surface=surface)
            result = workspace._bounds_for_model_ids(["moving"])
            np.testing.assert_allclose(result[0], expected[0])
            np.testing.assert_allclose(result[1], expected[1])
        runtime.model_catalog.set_model_visibility("moving", False)
        assert workspace._bounds_for_model_ids(["moving"]) is None
        runtime.model_catalog.set_model_visibility("moving", True)
        result = workspace._bounds_for_model_ids(["moving"])
        np.testing.assert_allclose(result[0], mesh_bounds[0])
        np.testing.assert_allclose(result[1], mesh_bounds[1])


def test_geometry_operator_changes_only_display_during_preview() -> None:
    """미리보기 중 형상 선택이 정렬 결과·선택·GPU 객체를 그대로 보존하는지 확인합니다."""
    from tools.align.constants import MARKER_ALIGNMENT_PHASE_PREVIEW
    from tools.align.operators import model_group

    with _runtime_fixture() as runtime, patch.object(model_group, "is_session_active_for_window", return_value=True), patch.object(
        model_group, "restore_input_block_reason", return_value=None,
    ), patch.object(model_group, "_report_status"), patch.object(model_group, "request_redraw") as redraw, patch.object(
        model_group, "notify_project_state_changed",
    ) as notify:
        runtime.model_catalog.set_geometry_visibility("moving", show_surface=True)
        preview = SimpleNamespace(moving_model_id="moving")
        runtime.marker_alignment.phase = MARKER_ALIGNMENT_PHASE_PREVIEW
        runtime.marker_alignment.result = preview
        runtime.gpu_batches[("moving", 1)] = cached = object()
        selection = runtime.model_catalog.selected_model_ids
        operator = model_group.COLMAPALIGN_OT_toggle_data_layer_geometry
        assert operator.poll(SimpleNamespace(window=object()))
        result = operator.execute(SimpleNamespace(model_id="moving", geometry_kind="COLMAP_POINT_CLOUD"), None)
        assert result == {"FINISHED"}
        assert runtime.model_catalog.models["moving"].show_points
        assert not runtime.model_catalog.models["moving"].show_surface
        assert runtime.marker_alignment.result is preview
        assert runtime.model_catalog.selected_model_ids == selection
        assert runtime.gpu_batches[("moving", 1)] is cached
        assert redraw.call_count == notify.call_count == 1
        runtime.model_repository.companion_meshes.pop("moving")
        assert operator.execute(SimpleNamespace(model_id="moving", geometry_kind="TRIANGLE_MESH"), None) == {"CANCELLED"}
        assert redraw.call_count == notify.call_count == 1


def test_restore_gpu_failure_preserves_previous_batches_and_textures() -> None:
    """복원 GPU 준비 실패가 기존 캐시·이미지 소유권을 보존하고 새 자원만 정리하는지 확인합니다."""
    from unittest.mock import Mock
    from tools.align.viewport import renderer

    with _runtime_fixture() as runtime:
        previous_batches = {("old", 1): object()}
        runtime.gpu_batches = previous_batches
        old_image = SimpleNamespace(users=0)
        new_image = SimpleNamespace(users=0)
        old_path = Path.cwd() / "old.png"
        new_path = Path.cwd() / "new.png"
        old_resource = renderer._SharedTextureResource(old_image, object(), True, {"old"})
        previous_resources = {old_path: old_resource}
        remove_image = Mock()
        calls = []

        def prepare_then_fail(model_id, _model):
            """첫 모델의 임시 자원을 만든 뒤 두 번째 모델에서 GPU 실패를 재현합니다."""
            calls.append(model_id)
            if len(calls) > 1:
                raise RuntimeError("GPU 준비 실패 재현")
            runtime.gpu_batches[(model_id, 1)] = object()
            renderer._MESH_TEXTURE_RESOURCES[new_path] = renderer._SharedTextureResource(
                new_image, object(), True, {model_id},
            )

        with patch.object(renderer, "_MESH_TEXTURE_RESOURCES", previous_resources), patch.object(
            renderer, "bpy", SimpleNamespace(data=SimpleNamespace(images=SimpleNamespace(remove=remove_image))),
        ), patch.object(renderer, "prepare_gpu_batch_for_model", side_effect=prepare_then_fail):
            try:
                renderer.prepare_restored_gpu_batches(runtime)
            except RuntimeError as error:
                assert "준비 실패" in str(error)
            else:
                raise AssertionError("복원 GPU 실패를 삼켰습니다.")
            assert runtime.gpu_batches is previous_batches
            assert renderer._MESH_TEXTURE_RESOURCES is previous_resources
            assert previous_resources[old_path] is old_resource
            assert old_resource.owned_image and old_resource.model_ids == {"old"}
            assert remove_image.call_args.args == (new_image,)


def test_restore_gpu_success_transfers_reused_image_ownership() -> None:
    """GPU 교체 성공 시 재사용 이미지 소유권을 옮기고 사용하지 않는 이미지만 정리합니다."""
    from unittest.mock import Mock
    from tools.align.viewport import renderer

    with _runtime_fixture() as runtime:
        previous_batches = {("old", 1): object()}
        runtime.gpu_batches = previous_batches
        image = SimpleNamespace(users=0)
        obsolete_image = SimpleNamespace(users=0)
        path = Path.cwd() / "shared.png"
        obsolete_path = Path.cwd() / "obsolete.png"
        previous_resources = {
            path: renderer._SharedTextureResource(image, object(), True, {"old"}),
            obsolete_path: renderer._SharedTextureResource(obsolete_image, object(), True, {"old"}),
        }
        remove_image = Mock()
        prepared_ids = []

        def prepare_with_shared_image(model_id, model):
            """새 배치를 추가하고 첫 모델에서 기존 Blender 이미지를 재사용합니다."""
            prepared_ids.append(model_id)
            runtime.gpu_batches[(model_id, renderer.geometry_kind_for_model(model))] = object()
            if path not in renderer._MESH_TEXTURE_RESOURCES:
                renderer._MESH_TEXTURE_RESOURCES[path] = renderer._SharedTextureResource(
                    image, object(), False, {model_id},
                )

        with patch.object(renderer, "_MESH_TEXTURE_RESOURCES", previous_resources), patch.object(
            renderer, "bpy", SimpleNamespace(data=SimpleNamespace(images=SimpleNamespace(remove=remove_image))),
        ), patch.object(renderer, "prepare_gpu_batch_for_model", side_effect=prepare_with_shared_image):
            renderer.prepare_restored_gpu_batches(runtime)
            assert runtime.gpu_batches is not previous_batches
            assert prepared_ids == ["reference", "reference", "moving", "moving", "standalone"]
            assert renderer._MESH_TEXTURE_RESOURCES is previous_resources
            assert renderer._MESH_TEXTURE_RESOURCES[path].owned_image
            assert set(renderer._MESH_TEXTURE_RESOURCES) == {path}
            assert remove_image.call_count == 1
            assert remove_image.call_args.args == (obsolete_image,)


def run_all() -> None:
    """연결 표시와 기존 동작 보존에 필요한 회귀 검사를 실행합니다."""
    _add_source_path()
    for name, function in tuple(globals().items()):
        if name.startswith("test_") and callable(function):
            function()
    print("test_companion_display: 통과")


if __name__ == "__main__":
    run_all()
