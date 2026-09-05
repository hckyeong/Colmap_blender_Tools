"""연결 표면의 실제 광선 피킹·캐시 분리·형상 가시성과 마커 표시를 검증합니다."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np


def _add_test_paths():
    """실험 소스와 인접 회귀 자료를 Blender 모듈 경로에 연결합니다."""
    folder = Path(__file__).resolve().parent
    for value in (folder.parents[2], folder):
        if str(value) not in sys.path:
            sys.path.insert(0, str(value))


def test_companion_ray_uses_mesh_axis_and_cache_isolates_source():
    """메시 축으로 정확한 원본 교차점을 복구하며 같은 소유자 캐시를 슬롯별로 유지합니다."""
    from test_companion_marker_alignment import _runtime, _display_runtime, _transform
    from tools.align.viewport import picking, point_data

    runtime, *_ = _runtime()
    mesh = runtime.model_repository.companion_meshes["moving"]
    triangle = mesh.vertices_float64[mesh.triangles_uint32[0]]
    local = triangle.mean(axis=0)
    with _display_runtime(runtime):
        matrix = point_data.effective_model_matrix_for_id("moving", companion=True)
        position = _transform(matrix, local[np.newaxis, :])[0]
        normal = matrix[:3, :3] @ np.cross(triangle[1] - triangle[0], triangle[2] - triangle[0])
        normal /= np.linalg.norm(normal)
        origin, direction = position + normal * 5, -normal
        region = SimpleNamespace(width=800, height=600)
        region_3d = SimpleNamespace(view_matrix=np.eye(4), perspective_matrix=None)
        with patch.object(picking, "_view_ray", return_value=(origin, direction)):
            picked = picking.pick_nearest_point("MOVING", region, region_3d, (0, 0))
        assert picked is not None and picked.geometry_source == "COMPANION"
        assert picked.model_id == "moving" and picked.point_kind == "MESH_SURFACE"
        np.testing.assert_allclose(picked.position, local, atol=1e-10)
        np.testing.assert_allclose(picked.depth, 5, atol=1e-10)
        main_tree = picking._mesh_surface_bvh("moving", mesh, "MAIN")
        companion_tree = picking._mesh_surface_bvh("moving", mesh, "COMPANION")
        assert main_tree is not companion_tree
        changed = replace(mesh, geometry_revision=mesh.geometry_revision + 1)
        changed_tree = picking._mesh_surface_bvh("moving", changed, "COMPANION")
        assert changed_tree is not companion_tree
        assert picking._mesh_surface_bvh("moving", mesh, "MAIN") is main_tree
        assert len([key for key in picking._MESH_SURFACE_BVH_CACHE if key[0] == "moving"]) == 2
        picking.discard_mesh_surface_pick_cache_for_model("moving")
        assert not any(key[0] == "moving" for key in picking._MESH_SURFACE_BVH_CACHE)


def test_hidden_geometry_cannot_pick_or_render_its_markers():
    """형상 토글과 전체 숨김이 새 피킹·표시만 바꾸고 저장 마커는 유지하는지 확인합니다."""
    from test_companion_marker_alignment import _runtime, _display_runtime
    from tools.align.viewport import picking, renderer

    runtime, *_ = _runtime()
    snapshot = runtime.marker_selection.snapshot()
    with _display_runtime(runtime):
        assert len(renderer._marker_entries("MOVING")) == 4
        runtime.model_catalog.set_geometry_visibility("moving", show_points=True, show_surface=False)
        assert renderer._marker_entries("MOVING") == []
        runtime.model_catalog.set_geometry_visibility("moving", show_points=False, show_surface=True)
        assert len(renderer._marker_entries("MOVING")) == 4
        runtime.model_catalog.set_model_visibility("moving", False)
        assert renderer._marker_entries("MOVING") == []
        with patch.object(picking, "_pick_model_mesh_surface_from_ray") as surface_picker:
            assert picking.pick_nearest_point("MOVING", SimpleNamespace(), SimpleNamespace(), (0, 0)) is None
            surface_picker.assert_not_called()
    assert runtime.marker_selection.snapshot() == snapshot


def test_nearest_visible_kind_is_returned_before_role_validation():
    """앞쪽 점군·메시를 종류 잠금으로 건너뛰지 않고 공통 광선 깊이로 비교합니다."""
    from test_companion_display import _runtime_fixture
    from test_mesh_surface_picking import _mesh_model
    from tools.align.alignment import create_sim3_transform
    from tools.align.viewport import picking

    with _runtime_fixture() as runtime:
        runtime.model_repository.set_alignment_transform("moving", create_sim3_transform(np.eye(3), 1, np.zeros(3)))
        square = _mesh_model("standalone")
        vertices = square.vertices_float64 - np.asarray((0.5, 0.5, 5.0))
        runtime.model_repository.models["standalone"] = replace(
            square, vertices_float64=vertices, bounds_min=vertices.min(axis=0), bounds_max=vertices.max(axis=0),
        )
        runtime.model_catalog.set_geometry_visibility("moving", show_points=True, show_surface=False)
        region = SimpleNamespace(width=800, height=600)
        view = np.eye(4)
        view[2, 3] = -10
        region_3d = SimpleNamespace(view_matrix=view, perspective_matrix=None)
        origin, direction = np.asarray((0, 0, 10.0)), np.asarray((0, 0, -1.0))
        with patch.object(picking, "_view_ray", return_value=(origin, direction)):
            front_point = picking.pick_nearest_point_for_model_ids(("standalone", "moving"), region, region_3d, (0, 0), radius=0.1)
            assert front_point.point_kind == "COLMAP_POINT" and front_point.model_id == "moving"
            runtime.model_repository.set_alignment_transform("moving", create_sim3_transform(np.eye(3), 1, np.asarray((0, 0, -10.0))))
            front_surface = picking.pick_nearest_point_for_model_ids(("moving", "standalone"), region, region_3d, (0, 0), radius=0.1)
            assert front_surface.point_kind == "MESH_SURFACE" and front_surface.model_id == "standalone"
        picking.clear_mesh_surface_pick_cache()



def test_front_point_inside_same_cloud_blocks_mesh_role_pick():
    """한 점군의 앞·뒤 점 사이 메시가 있어도 클릭을 덮는 앞 점을 먼저 판정합니다."""
    from test_companion_display import _runtime_fixture
    from test_icp_runtime import _model
    from test_mesh_surface_picking import _mesh_model
    from tools.align.state import MarkerPair, SelectedPoint
    from tools.align.viewport import picking

    with _runtime_fixture() as runtime:
        runtime.session_active = True
        positions = np.asarray(((0.001, 0.0, -5.0), (0.0, 0.0, -20.0)))
        cloud = _model("reference", positions, revision=2)
        runtime.model_repository.models["reference"] = cloud
        square = _mesh_model("standalone")
        vertices = square.vertices_float64 - np.asarray((0.5, 0.5, 10.0))
        runtime.model_repository.models["standalone"] = replace(
            square, vertices_float64=vertices, bounds_min=vertices.min(axis=0), bounds_max=vertices.max(axis=0),
        )
        runtime.model_catalog.promote_model_for_icp_commit("standalone")
        runtime.model_catalog.set_geometry_visibility("reference", show_points=True, show_surface=False)
        runtime.marker_selection.completed_pairs = [MarkerPair(
            1,
            SelectedPoint("REFERENCE", 0, (0.2, 0.1, -10.0), "standalone", "MESH_SURFACE", 0),
            SelectedPoint("MOVING", 11, (0.0, 0.0, 0.0), "moving"),
        )]
        runtime.marker_selection.next_pair_id = 2
        assert runtime.marker_selection.begin_create_draft(runtime=runtime)
        assert runtime.marker_selection.role_geometry_kind("REFERENCE") == "TRIANGLE_MESH"
        region = SimpleNamespace(width=800, height=600)
        region_3d = SimpleNamespace(view_matrix=np.eye(4), perspective_matrix=np.eye(4))
        origin, direction = np.zeros(3), np.asarray((0.0, 0.0, -1.0))
        screen, depths, valid = picking._project_positions(region, region_3d, positions)
        np.testing.assert_allclose(screen, ((400.4, 300), (400, 300)), atol=1e-10)
        np.testing.assert_allclose(depths, (5, 20), atol=1e-10)
        assert valid.all()
        with patch.object(picking, "_view_ray", return_value=(origin, direction)):
            result = picking.pick_nearest_point("REFERENCE", region, region_3d, (400, 300))
        assert result is not None and result.model_id == "reference"
        assert result.point_kind == "COLMAP_POINT" and result.point3d_id == 101
        np.testing.assert_allclose(result.depth, 5, atol=1e-10)
        assert not runtime.marker_selection.can_select_draft_point(
            "REFERENCE", result.point3d_id, result.position, model_id=result.model_id,
            point_kind=result.point_kind, source_point_id=result.source_point_id,
            geometry_source=result.geometry_source,
        )
        picking.clear_mesh_surface_pick_cache()



def test_point_depth_priority_preserves_radius_and_equal_depth_distance():
    """깊이 우선 선택에서도 반경 제외·같은 깊이 화면 거리·광선 없는 기존 선택을 보존합니다."""
    from test_companion_display import _runtime_fixture
    from test_icp_runtime import _model
    from tools.align.viewport import picking

    with _runtime_fixture() as runtime:
        positions = np.asarray(((0.02, 0, -1), (0.002, 0, -5), (0.001, 0, -5), (0, 0, -20)), dtype=np.float64)
        runtime.model_repository.models["reference"] = _model("reference", positions, revision=2)
        region = SimpleNamespace(width=800, height=600)
        region_3d = SimpleNamespace(view_matrix=np.eye(4), perspective_matrix=np.eye(4))
        origin, direction = np.zeros(3), np.asarray((0.0, 0.0, -1.0))
        front = picking._pick_model_nearest_point("reference", region, region_3d, (400, 300), 1.0, origin, direction)
        assert front.point3d_id == 103 and front.depth == 5
        narrow = picking._pick_model_nearest_point("reference", region, region_3d, (400, 300), 0.09, origin, direction)
        assert narrow.point3d_id == 104 and narrow.depth == 20
        legacy = picking._pick_model_nearest_point("reference", region, region_3d, (400, 300), 1.0)
        assert legacy.point3d_id == 104 and legacy.depth == 20


def test_companion_missed_face_does_not_snap_to_owner_sparse_points():
    """연결 표면을 놓친 광선이 주 점군 피킹으로 전환되지 않는지 확인합니다."""
    from test_companion_marker_alignment import _runtime, _display_runtime
    from tools.align.viewport import picking

    runtime, *_ = _runtime()
    with _display_runtime(runtime), patch.object(picking, "_view_ray", return_value=(np.asarray((999, 999, 999.0)), np.asarray((0, 0, 1.0)))), patch.object(picking, "_pick_model_nearest_point") as point_picker:
        assert picking.pick_nearest_point("MOVING", SimpleNamespace(), SimpleNamespace(), (0, 0)) is None
        point_picker.assert_not_called()


def test_marker_draft_keeps_role_views_when_selection_contains_both_roles():
    """복수 선택을 보존한 마커 입력이 A 전체·선택 P의 역할별 표시를 유지합니다."""
    from test_companion_marker_alignment import _runtime, _display_runtime
    from tools.align.viewport import renderer

    runtime, *_ = _runtime(multiple_reference=True)
    runtime.marker_selection.completed_pairs.clear()
    runtime.model_catalog.selected_model_ids = ("reference", "moving")
    assert runtime.marker_selection.begin_create_draft()
    with _display_runtime(runtime):
        reference = renderer.point_cloud_draw_passes_for_role("REFERENCE")
        moving = renderer.point_cloud_draw_passes_for_role("MOVING")
    assert len(reference) == 1 and reference[0].model_ids == ("reference", "reference_second")
    assert len(moving) == 1 and moving[0].model_ids == ("moving",)
    assert runtime.model_catalog.selected_model_ids == ("reference", "moving")


def run_all():
    """실제 Blender BVH와 표시 경계의 연결 메시 피킹 회귀를 실행합니다."""
    _add_test_paths()
    for name, test in sorted(globals().copy().items()):
        if name.startswith("test_") and callable(test):
            test()
            print("통과:", name, flush=True)


if __name__ == "__main__":
    run_all()
