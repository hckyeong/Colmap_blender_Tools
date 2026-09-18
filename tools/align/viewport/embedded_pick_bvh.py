"""검증된 내장 원본 Mesh로 격리된 면 피킹 BVH를 생성합니다."""

from typing import Any

import bpy
import numpy as np
from mathutils.bvhtree import BVHTree

from ..mesh_io.embedded_obj_store import (
    EmbeddedObjStoreError,
    find_embedded_obj_mesh,
    validate_embedded_obj_identity,
)


def _compare_mesh_array(collection: Any, attribute: str, dtype: Any, expected: np.ndarray) -> None:
    """Blender 기본 버퍼로 읽고 배정밀도 전체 복사 없이 프록시와 정확히 비교합니다."""
    actual = np.empty(expected.size, dtype=dtype)
    collection.foreach_get(attribute, actual)
    actual = actual.reshape(expected.shape)
    for start in range(0, len(actual), 65_536):
        if not np.array_equal(actual[start:start + 65_536], expected[start:start + 65_536]):
            raise EmbeddedObjStoreError(f"내장 OBJ Mesh의 {attribute} 값이 피킹 프록시와 다릅니다.")


def build_embedded_pick_bvh(model_id: str, model_data: Any) -> BVHTree:
    """원본과 프록시 대응을 확인하고 임시 장면에서 Polygon ID를 반환하는 BVH를 만듭니다."""
    mesh = find_embedded_obj_mesh(model_data.embedded_asset_uuid)
    validate_embedded_obj_identity(
        mesh,
        expected_asset_uuid=model_data.embedded_asset_uuid,
        expected_model_id=model_id,
        expected_revision=model_data.geometry_revision,
        require_unlinked=True,
    )
    if (
        str(model_data.model_id) != str(model_id)
        or int(mesh.get("schema_version", 0)) != int(model_data.embedded_asset_schema_version)
        or len(mesh.vertices) != len(model_data.vertices_float64)
        or len(mesh.polygons) != int(model_data.original_polygon_count)
    ):
        raise EmbeddedObjStoreError("내장 OBJ Mesh의 식별자 또는 형상 개수가 피킹 프록시와 다릅니다.")
    mesh.calc_loop_triangles()
    if len(mesh.loop_triangles) != len(model_data.triangles_uint32):
        raise EmbeddedObjStoreError("내장 OBJ Mesh의 삼각면 개수가 피킹 프록시와 다릅니다.")
    _compare_mesh_array(mesh.vertices, "co", np.float32, model_data.vertices_float64)
    _compare_mesh_array(mesh.loop_triangles, "vertices", np.int32, model_data.triangles_uint32)
    _compare_mesh_array(
        mesh.loop_triangles, "polygon_index", np.int32,
        model_data.triangle_polygon_indices_uint32,
    )

    scene = None
    obj = None
    try:
        scene = bpy.data.scenes.new(".COLMAP_ALIGN_PICK_SCENE")
        obj = bpy.data.objects.new(".COLMAP_ALIGN_PICK_OBJECT", mesh)
        scene.collection.objects.link(obj)
        view_layer = scene.view_layers[0]
        with bpy.context.temp_override(scene=scene, view_layer=view_layer):
            depsgraph = bpy.context.evaluated_depsgraph_get()
            return BVHTree.FromObject(obj, depsgraph, deform=False, epsilon=0.0)
    finally:
        try:
            if obj is not None:
                bpy.data.objects.remove(obj, do_unlink=True)
        finally:
            if scene is not None:
                bpy.data.scenes.remove(scene)
