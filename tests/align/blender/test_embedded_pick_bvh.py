"""내장 원본 BVH의 Polygon ID·격리·예외 정리·프록시 일치를 검증합니다."""

from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import runpy
import sys

import bpy
import numpy as np
from mathutils import Vector

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from tools.align.mesh_io.embedded_obj_store import (
    EmbeddedObjStoreError,
    commit_embedded_obj_mesh_and_build_proxy,
    remove_embedded_obj_mesh,
)
from tools.align.mesh_io.obj_native_import import import_obj_to_staging
from tools.align.viewport import embedded_pick_bvh


def _snapshot(mesh):
    """사용자 문맥과 기존 데이터 블록·UV·원본 메시 상태를 기록합니다."""
    return (
        bpy.context.scene.as_pointer(), bpy.context.view_layer.as_pointer(),
        bpy.context.view_layer.objects.active,
        tuple(obj.as_pointer() for obj in bpy.context.selected_objects), bpy.context.mode,
        bpy.context.view_layer.active_layer_collection.as_pointer(),
        tuple(tuple(item.as_pointer() for item in collection) for collection in (
            bpy.data.scenes, bpy.data.objects, bpy.data.meshes, bpy.data.materials, bpy.data.images,
        )),
        tuple(tuple(item.uv) for item in mesh.uv_layers.active.data),
        tuple(poly.material_index for poly in mesh.polygons), mesh.use_fake_user, mesh.users,
    )


def main():
    """실제 삼각형·사각형·오목 오각형 OBJ에서 면 BVH와 실패 경계를 검증합니다."""
    fixture = runpy.run_path(str(ROOT / "tests/align/blender/test_embedded_obj.py"))
    with TemporaryDirectory() as temporary:
        path = fixture["_write_fixture"](Path(temporary))
        staging = import_obj_to_staging(path)
        mesh = staging.mesh
        proxy = commit_embedded_obj_mesh_and_build_proxy(
            mesh, model_id="pick-test", display_name="피킹 검사", source_path=path,
        )
        staging.commit()
        try:
            active = bpy.context.view_layer.objects.active
            if active is not None and active.type == "MESH":
                bpy.ops.object.mode_set(mode="EDIT")
            before = _snapshot(mesh)
            tree = embedded_pick_bvh.build_embedded_pick_bvh("pick-test", proxy)
            assert _snapshot(mesh) == before
            for point, expected_polygon, expected_z in (
                ((2.5, 3.5, 10.0), 0, 5.0),
                ((11.0, 1.0, 10.0), 1, 0.0),
                ((21.0, 0.5, 10.0), 2, 0.0),
            ):
                location, _normal, polygon, _distance = tree.ray_cast(Vector(point), Vector((0, 0, -1)))
                assert polygon == expected_polygon
                assert abs(location.z - expected_z) < 1e-6
            assert tree.ray_cast(Vector((22.0, 3.0, 10.0)), Vector((0, 0, -1)))[0] is None

            with patch.object(embedded_pick_bvh, "BVHTree") as factory:
                factory.FromObject.side_effect = RuntimeError("검증용 BVH 생성 실패")
                try:
                    embedded_pick_bvh.build_embedded_pick_bvh("pick-test", proxy)
                except RuntimeError as error:
                    assert "검증용 BVH 생성 실패" in str(error)
                else:
                    raise AssertionError("BVH 생성 실패가 전달되지 않았습니다.")
            assert _snapshot(mesh) == before

            changed_vertices = proxy.vertices_float64.copy()
            changed_vertices[0, 0] += 0.125
            changed_triangles = proxy.triangles_uint32.copy()
            changed_triangles[0] = changed_triangles[0, ::-1]
            changed_polygons = proxy.triangle_polygon_indices_uint32.copy()
            changed_polygons[0] = 1
            variants = (
                replace(proxy, vertices_float64=changed_vertices),
                replace(proxy, triangles_uint32=changed_triangles),
                replace(proxy, triangle_polygon_indices_uint32=changed_polygons),
            )
            for changed in variants:
                try:
                    embedded_pick_bvh.build_embedded_pick_bvh("pick-test", changed)
                except EmbeddedObjStoreError:
                    pass
                else:
                    raise AssertionError("같은 개수·리비전의 원본/프록시 불일치를 허용했습니다.")
                assert _snapshot(mesh) == before
            print("내장 BVH 검사 통과: Polygon 적중·사용자 문맥/UV 보존·예외 정리·동일 리비전 변조 거부")
        finally:
            if bpy.context.mode != "OBJECT":
                bpy.ops.object.mode_set(mode="OBJECT")
            remove_embedded_obj_mesh(proxy.embedded_asset_uuid, expected_model_id="pick-test")


if __name__ == "__main__":
    main()
