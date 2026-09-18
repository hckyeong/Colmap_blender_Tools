"""네이티브 면 검색의 안정 삼각형 ID와 표시 정점 전용 피킹을 검증합니다."""

from dataclasses import replace
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

import bpy
import numpy as np

for path in (Path(__file__).resolve().parents[3], Path(__file__).resolve().parent):
    sys.path.insert(0, str(path))

from tools.align.viewport import picking, point_data
from tools.align.state import RUNTIME


def test_embedded_polygons_preserve_proxy_triangle_ids_and_coordinates() -> None:
    """혼합 원본 면의 모든 프록시 삼각형을 정확한 ID·좌표로 선택하고 변환 후 캐시를 재사용합니다."""
    from test_embedded_obj import _write_fixture
    from tools.align.alignment.types import Sim3Transform
    from tools.align.geometry_access import validated_mesh_surface_position
    from tools.align.mesh_importer import run_mesh_import_job
    from tools.align.mesh_io.embedded_obj_store import remove_embedded_obj_mesh

    RUNTIME.clear_transient()
    with TemporaryDirectory() as temporary:
        job = run_mesh_import_job(RUNTIME, _write_fixture(Path(temporary)), prepare_gpu=False)
        assert job.stage == "COMPLETED", job.error_message
        model = job.staged_model
        before = tuple(tuple(item.as_pointer() for item in collection) for collection in (
            bpy.data.scenes, bpy.data.objects, bpy.data.meshes,
        ))
        try:
            tree = None
            for translation in ((0.0, 0.0, 0.0), (3.0, 4.0, 5.0)):
                alignment = np.eye(4)
                alignment[:3, 3] = translation
                RUNTIME.model_repository.set_alignment_transform(model.model_id, Sim3Transform(matrix_float64=alignment))
                matrix = point_data.effective_model_matrix_for_id(model.model_id)
                for index, indices in enumerate(model.triangles_uint32):
                    triangle = model.vertices_float64[indices]
                    local_position = triangle.mean(axis=0)
                    normal = np.cross(triangle[1] - triangle[0], triangle[2] - triangle[0])
                    normal /= np.linalg.norm(normal)
                    origin = (matrix @ np.append(local_position + normal, 1.0))[:3]
                    direction = matrix[:3, :3] @ -normal
                    direction /= np.linalg.norm(direction)
                    with patch.object(picking, "_view_ray", return_value=(origin, direction)):
                        result = picking.pick_nearest_point_for_model_ids(
                            (model.model_id,), SimpleNamespace(), SimpleNamespace(), (0, 0),
                        )
                    assert result is not None and result.point_kind == "MESH_SURFACE"
                    assert result.source_point_id == index, (result.source_point_id, index)
                    np.testing.assert_allclose(result.position, local_position, rtol=0, atol=1e-12)
                    validated_mesh_surface_position(model, index, result.position)
                    cached = picking._mesh_surface_bvh(model.model_id, model)
                    if tree is None:
                        tree = cached
                    assert cached is tree
            assert before == tuple(tuple(item.as_pointer() for item in collection) for collection in (
                bpy.data.scenes, bpy.data.objects, bpy.data.meshes,
            ))
        finally:
            RUNTIME.clear_transient()
            remove_embedded_obj_mesh(model.embedded_asset_uuid, expected_model_id=model.model_id)


def test_mesh_vertex_pick_uses_original_ids_without_full_point_arrays() -> None:
    """메시 정점 선택이 전체 ID·색상 배열 없이 원본 인덱스와 깊이 우선순위를 유지합니다."""
    from test_mesh_surface_picking import _mesh_model
    from tools.align.display_axes import identity_display_transform

    RUNTIME.clear_transient()
    original = _mesh_model("vertex-only")
    vertices = np.asarray(((0.02, 0, -1), (0.002, 0, -5), (0.001, 0, -5), (0, 0, -20)))
    model = replace(original, vertices_float64=vertices, bounds_min=vertices.min(axis=0), bounds_max=vertices.max(axis=0))
    RUNTIME.model_repository.add_model(model, identity_display_transform())
    region = SimpleNamespace(width=800, height=600)
    view = SimpleNamespace(view_matrix=np.eye(4), perspective_matrix=np.eye(4))
    origin, direction = np.zeros(3), np.asarray((0.0, 0.0, -1.0))
    try:
        with patch.object(picking, "point_arrays", side_effect=AssertionError("전체 점 배열을 만들었습니다.")):
            result = picking._pick_model_nearest_point(model.model_id, region, view, (400, 300), 1.0, origin, direction)
            assert result.point_kind == "MESH_VERTEX" and result.source_point_id == 2
            assert result.depth == 5.0
            result = picking._pick_model_nearest_point(model.model_id, region, view, (400, 300), 0.09, origin, direction)
            assert result.source_point_id == 3 and result.depth == 20.0
            result = picking._pick_model_nearest_point(model.model_id, region, view, (400, 300), 1.0)
            assert result.source_point_id == 3
    finally:
        RUNTIME.clear_transient()


def test_polygon_lookup_handles_nonmonotonic_proxy_order() -> None:
    """비연속 Polygon 매핑도 삼각형 ID를 재번호화하지 않고 원래 순서로 조회합니다."""
    from test_mesh_surface_picking import _mesh_model

    model = _mesh_model("lookup")
    model = replace(model, embedded_asset_uuid="a" * 32, embedded_asset_schema_version=1,
                    embedded_data_name_hint="검사", triangle_polygon_indices_uint32=np.asarray((1, 0)))
    with patch.object(picking, "build_embedded_pick_bvh", return_value=object()):
        tree = picking._mesh_surface_bvh(model.model_id, model)
    assert list(tree.triangle_candidates(0)) == [1]
    assert list(tree.triangle_candidates(1)) == [0]
    picking.clear_mesh_surface_pick_cache()


def test_native_failure_does_not_fall_back_or_cache_partial_tree() -> None:
    """내장 원본 검증 실패를 삼각면 재구축으로 우회하지 않고 캐시를 비워 둡니다."""
    from test_mesh_surface_picking import _mesh_model
    from tools.align.model_repository import ModelRepositoryError

    model = replace(_mesh_model("failed"), embedded_asset_uuid="b" * 32,
                    embedded_asset_schema_version=1, embedded_data_name_hint="검사")
    with patch.object(picking, "build_embedded_pick_bvh", side_effect=RuntimeError("원본 불일치")), patch.object(picking, "BVHTree") as factory:
        try:
            picking._mesh_surface_bvh(model.model_id, model)
        except ModelRepositoryError as error:
            assert "원본 불일치" in str(error)
        else:
            raise AssertionError("검색 구조 생성 실패가 전달되지 않았습니다.")
        factory.FromPolygons.assert_not_called()
    assert not picking._MESH_SURFACE_BVH_CACHE


def run_all() -> None:
    """네이티브 피킹과 정점 접근 최적화의 동작 보존을 검사합니다."""
    test_embedded_polygons_preserve_proxy_triangle_ids_and_coordinates()
    test_mesh_vertex_pick_uses_original_ids_without_full_point_arrays()
    test_polygon_lookup_handles_nonmonotonic_proxy_order()
    test_native_failure_does_not_fall_back_or_cache_partial_tree()
    print("피킹 최적화 4개 검사 통과")


if __name__ == "__main__":
    run_all()
