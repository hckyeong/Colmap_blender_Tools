"""비연결 Blender Mesh를 OBJ 원본 권위로 저장하고 프록시를 생성합니다."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import bpy
import numpy as np

from .types import ObjMaterialData, TriangleMeshModelData


EMBEDDED_OBJ_OWNER = "COLMAP_ALIGN_EMBEDDED_OBJ"
EMBEDDED_OBJ_RETIRED_OWNER = "COLMAP_ALIGN_EMBEDDED_OBJ_RETIRED"
EMBEDDED_OBJ_SUPPORT_OWNER = "COLMAP_ALIGN_EMBEDDED_OBJ_SUPPORT"
EMBEDDED_OBJ_SCHEMA_VERSION = 1
EMBEDDED_OBJ_NAME_PREFIX = ".COLMAP_ALIGN_OBJ_"
_OWNER_KEY = "owner"
_SCHEMA_KEY = "schema_version"
_UUID_KEY = "asset_uuid"
_MODEL_ID_KEY = "model_id"
_SOURCE_NAME_KEY = "source_name"
_SOURCE_PATH_KEY = "source_path"
_REVISION_KEY = "geometry_revision"


class EmbeddedObjStoreError(RuntimeError):
    """내장 OBJ Mesh의 소유권·구조가 계약과 다를 때 발생합니다."""


@dataclass(frozen=True, slots=True)
class EmbeddedObjExportGeometry:
    """내장 Mesh에서 Export 시작 시 한 번만 복사한 가변 코너 배열입니다."""

    asset_uuid: str
    schema_version: int
    data_name_hint: str
    geometry_revision: int
    vertices_float64: np.ndarray
    polygon_loop_starts_uint32: np.ndarray
    polygon_loop_totals_uint32: np.ndarray
    loop_vertex_indices_uint32: np.ndarray
    texcoords_float64: np.ndarray
    texcoord_component_counts_uint8: np.ndarray
    loop_texcoord_indices_int32: np.ndarray
    materials: tuple[ObjMaterialData, ...]
    polygon_material_indices_int32: np.ndarray

    @property
    def polygon_count(self) -> int:
        """원본 Polygon 개수를 반환합니다."""

        return int(self.polygon_loop_starts_uint32.shape[0])


def _uuid_text(value: object, label: str = "내장 OBJ asset UUID") -> str:
    """UUID 문자열을 canonical 소문자 hex 형식으로 검증합니다."""

    raw = str(value).strip()
    try:
        return UUID(raw).hex
    except (ValueError, AttributeError, TypeError) as error:
        raise EmbeddedObjStoreError(f"{label}가 올바르지 않습니다.") from error


def _mesh_objects(mesh: Any) -> tuple[Any, ...]:
    """지정 Mesh를 연결한 Blender Object를 모두 반환합니다."""

    return tuple(obj for obj in bpy.data.objects if getattr(obj, "data", None) == mesh)


def _foreach_array(collection: Any, attribute: str, dtype: Any, shape: tuple[int, ...]) -> np.ndarray:
    """Blender collection 속성을 연속 NumPy 배열로 복사합니다."""

    array = np.empty(int(np.prod(shape, dtype=np.int64)), dtype=dtype)
    collection.foreach_get(attribute, array)
    return np.ascontiguousarray(array.reshape(shape), dtype=dtype)


def _image_path_for_material(material: Any) -> Path | None:
    """재질 노드에서 첫 diffuse 이미지의 외부 경로를 찾습니다."""

    node_tree = getattr(material, "node_tree", None)
    nodes = getattr(node_tree, "nodes", ()) if node_tree is not None else ()
    for node in nodes:
        image = getattr(node, "image", None)
        raw_path = str(getattr(image, "filepath", "") or "").strip() if image is not None else ""
        if not raw_path:
            continue
        try:
            absolute = bpy.path.abspath(raw_path, library=getattr(image, "library", None))
        except (AttributeError, TypeError, ValueError):
            absolute = bpy.path.abspath(raw_path)
        return Path(absolute).expanduser().resolve(strict=False)
    return None


def _material_data(mesh: Any) -> tuple[ObjMaterialData, ...]:
    """Mesh material slot을 기존 OBJ 렌더·텍스처 복사 계약으로 변환합니다."""

    materials: list[ObjMaterialData] = []
    used_names: set[str] = set()
    for index, material in enumerate(mesh.materials):
        if material is None:
            name = f"material_{index + 1}"
            color = (0.8, 0.8, 0.8)
            texture_path = None
        else:
            name = str(material.get("colmap_align_source_name", material.name)).strip()
            if not name:
                name = f"material_{index + 1}"
            diffuse = tuple(float(value) for value in material.diffuse_color[:3])
            color = diffuse if len(diffuse) == 3 else (0.8, 0.8, 0.8)
            texture_path = _image_path_for_material(material)
        if name in used_names:
            name = f"{name}_{index + 1}"
        used_names.add(name)
        materials.append(
            ObjMaterialData(
                name=name,
                diffuse_color=color,
                diffuse_texture_path=texture_path,
            )
        )
    return tuple(materials)


def validate_embedded_obj_mesh(
    mesh: Any,
    *,
    expected_asset_uuid: str | None = None,
    expected_model_id: str | None = None,
    expected_revision: int | None = None,
    require_unlinked: bool = True,
) -> Any:
    """내장 OBJ Mesh의 소유권·식별자·기본 Polygon 구조를 원자 경계에서 검증합니다."""

    if not isinstance(mesh, bpy.types.Mesh):
        raise EmbeddedObjStoreError("내장 OBJ 데이터가 Blender Mesh가 아닙니다.")
    if str(mesh.get(_OWNER_KEY, "")) != EMBEDDED_OBJ_OWNER:
        raise EmbeddedObjStoreError("내장 OBJ Mesh 소유권이 올바르지 않습니다.")
    if int(mesh.get(_SCHEMA_KEY, 0)) != EMBEDDED_OBJ_SCHEMA_VERSION:
        raise EmbeddedObjStoreError("지원하지 않는 내장 OBJ Mesh schema입니다.")
    asset_uuid = _uuid_text(mesh.get(_UUID_KEY, ""))
    if expected_asset_uuid is not None and asset_uuid != _uuid_text(expected_asset_uuid):
        raise EmbeddedObjStoreError("내장 OBJ Mesh UUID가 저장 상태와 다릅니다.")
    model_id = str(mesh.get(_MODEL_ID_KEY, "")).strip()
    if not model_id:
        raise EmbeddedObjStoreError("내장 OBJ Mesh 모델 ID가 비어 있습니다.")
    if expected_model_id is not None and model_id != str(expected_model_id).strip():
        raise EmbeddedObjStoreError("내장 OBJ Mesh 모델 ID가 저장 상태와 다릅니다.")
    revision = int(mesh.get(_REVISION_KEY, 0))
    if revision < 1 or (expected_revision is not None and revision != int(expected_revision)):
        raise EmbeddedObjStoreError("내장 OBJ Mesh geometry revision이 저장 상태와 다릅니다.")
    if not bool(mesh.use_fake_user):
        raise EmbeddedObjStoreError("내장 OBJ Mesh의 Fake User가 해제되었습니다.")
    if require_unlinked and _mesh_objects(mesh):
        raise EmbeddedObjStoreError("내장 OBJ Mesh가 Scene Object에 연결되어 있습니다.")
    vertex_count = len(mesh.vertices)
    polygon_count = len(mesh.polygons)
    loop_count = len(mesh.loops)
    if vertex_count < 1 or polygon_count < 1 or loop_count < 3:
        raise EmbeddedObjStoreError("내장 OBJ Mesh 형상이 비어 있습니다.")
    coordinates = _foreach_array(mesh.vertices, "co", np.float64, (vertex_count, 3))
    if not np.isfinite(coordinates).all():
        raise EmbeddedObjStoreError("내장 OBJ Mesh 정점에 유한하지 않은 값이 있습니다.")
    starts = _foreach_array(mesh.polygons, "loop_start", np.int64, (polygon_count,))
    totals = _foreach_array(mesh.polygons, "loop_total", np.int64, (polygon_count,))
    if np.any(totals < 3) or int(totals.sum()) != loop_count:
        raise EmbeddedObjStoreError("내장 OBJ Mesh Polygon corner 구조가 올바르지 않습니다.")
    if starts[0] != 0 or np.any(starts[1:] != starts[:-1] + totals[:-1]):
        raise EmbeddedObjStoreError("내장 OBJ Mesh Loop 범위가 연속적이지 않습니다.")
    loop_vertices = _foreach_array(mesh.loops, "vertex_index", np.int64, (loop_count,))
    if np.any(loop_vertices < 0) or np.any(loop_vertices >= vertex_count):
        raise EmbeddedObjStoreError("내장 OBJ Mesh Loop 정점 인덱스가 범위를 벗어났습니다.")
    for start, total in zip(starts, totals, strict=True):
        corners = loop_vertices[int(start) : int(start + total)]
        if np.unique(corners).shape[0] != corners.shape[0]:
            raise EmbeddedObjStoreError("내장 OBJ Mesh Polygon이 같은 정점을 반복합니다.")
    mesh.calc_loop_triangles()
    if len(mesh.loop_triangles) < 1:
        raise EmbeddedObjStoreError("내장 OBJ Mesh 삼각형 프록시를 만들 수 없습니다.")
    return mesh


def commit_embedded_obj_mesh(
    mesh: Any,
    *,
    model_id: str,
    source_path: str | Path,
    asset_uuid: str | None = None,
    geometry_revision: int = 1,
) -> str:
    """Staging Mesh에 안정 식별자와 Fake User를 지정해 비연결 원본으로 확정합니다."""

    normalized_model_id = str(model_id).strip()
    if not normalized_model_id:
        raise EmbeddedObjStoreError("내장 OBJ Mesh 모델 ID가 비어 있습니다.")
    source = Path(source_path).expanduser().resolve(strict=False)
    normalized_uuid = _uuid_text(asset_uuid or uuid4().hex)
    if find_embedded_obj_mesh(normalized_uuid, allow_missing=True) is not None:
        raise EmbeddedObjStoreError("같은 UUID의 내장 OBJ Mesh가 이미 있습니다.")
    revision = int(geometry_revision)
    if revision < 1:
        raise EmbeddedObjStoreError("내장 OBJ geometry revision은 1 이상이어야 합니다.")
    if _mesh_objects(mesh):
        raise EmbeddedObjStoreError("확정할 OBJ Mesh가 아직 Scene Object에 연결되어 있습니다.")
    mesh[_OWNER_KEY] = EMBEDDED_OBJ_OWNER
    mesh[_SCHEMA_KEY] = EMBEDDED_OBJ_SCHEMA_VERSION
    mesh[_UUID_KEY] = normalized_uuid
    mesh[_MODEL_ID_KEY] = normalized_model_id
    mesh[_SOURCE_NAME_KEY] = source.name
    mesh[_SOURCE_PATH_KEY] = str(source)
    mesh[_REVISION_KEY] = revision
    mesh.name = f"{EMBEDDED_OBJ_NAME_PREFIX}{normalized_uuid}"
    mesh.use_fake_user = True
    validate_embedded_obj_mesh(
        mesh,
        expected_asset_uuid=normalized_uuid,
        expected_model_id=normalized_model_id,
        expected_revision=revision,
    )
    return normalized_uuid


def find_embedded_obj_mesh(asset_uuid: str, *, allow_missing: bool = False) -> Any | None:
    """Data Block 이름과 무관하게 UUID가 일치하는 소유 Mesh를 유일하게 찾습니다."""

    normalized_uuid = _uuid_text(asset_uuid)
    matches = [
        mesh
        for mesh in bpy.data.meshes
        if str(mesh.get(_OWNER_KEY, "")) == EMBEDDED_OBJ_OWNER
        and str(mesh.get(_UUID_KEY, "")).replace("-", "").casefold() == normalized_uuid
    ]
    if len(matches) > 1:
        raise EmbeddedObjStoreError("같은 UUID의 내장 OBJ Mesh가 둘 이상입니다.")
    if not matches:
        if allow_missing:
            return None
        raise EmbeddedObjStoreError("내장 OBJ Mesh를 찾을 수 없습니다.")
    return matches[0]


def remove_embedded_obj_mesh(asset_uuid: str, *, expected_model_id: str | None = None) -> bool:
    """정확히 UUID가 일치하는 Align 소유 비연결 Mesh 하나만 제거합니다."""

    mesh = find_embedded_obj_mesh(asset_uuid, allow_missing=True)
    if mesh is None:
        return False
    validate_embedded_obj_mesh(
        mesh,
        expected_asset_uuid=asset_uuid,
        expected_model_id=expected_model_id,
    )
    bpy.data.meshes.remove(mesh)
    sweep_embedded_obj_support_data()
    return True


def retire_embedded_obj_mesh(asset_uuid: str, *, expected_model_id: str | None = None) -> bool:
    """최종 제거 예외 시 Mesh를 저장되지 않는 비소유 tombstone으로 격리합니다."""

    mesh = find_embedded_obj_mesh(asset_uuid, allow_missing=True)
    if mesh is None:
        return False
    if _uuid_text(mesh.get(_UUID_KEY, "")) != _uuid_text(asset_uuid):
        raise EmbeddedObjStoreError("격리할 내장 OBJ Mesh UUID가 다릅니다.")
    if expected_model_id is not None and str(mesh.get(_MODEL_ID_KEY, "")).strip() != str(expected_model_id).strip():
        raise EmbeddedObjStoreError("격리할 내장 OBJ Mesh 모델 ID가 다릅니다.")
    if _mesh_objects(mesh):
        raise EmbeddedObjStoreError("Scene Object에 연결된 내장 OBJ Mesh는 격리할 수 없습니다.")
    mesh[_OWNER_KEY] = EMBEDDED_OBJ_RETIRED_OWNER
    mesh.use_fake_user = False
    return True


def validate_embedded_obj_project_cleanup() -> int:
    """새 프로젝트가 정리할 ACTIVE·RETIRED Mesh가 모두 비연결인지 먼저 확인합니다."""

    owners = {EMBEDDED_OBJ_OWNER, EMBEDDED_OBJ_RETIRED_OWNER}
    meshes = [mesh for mesh in bpy.data.meshes if str(mesh.get(_OWNER_KEY, "")) in owners]
    for mesh in meshes:
        if _mesh_objects(mesh):
            raise EmbeddedObjStoreError("Scene Object에 연결된 Align 소유 Mesh는 새 프로젝트에서 제거할 수 없습니다.")
    return len(meshes)


def sweep_embedded_obj_support_data() -> int:
    """내장 OBJ Import가 만든 zero-user Material과 Image만 소유 태그로 정리합니다."""

    removed = 0
    for material in tuple(bpy.data.materials):
        if (
            str(material.get(_OWNER_KEY, "")) == EMBEDDED_OBJ_SUPPORT_OWNER
            and material.users == 0
        ):
            try:
                bpy.data.materials.remove(material)
                removed += 1
            except (ReferenceError, RuntimeError, TypeError, ValueError):
                continue
    for image in tuple(bpy.data.images):
        if (
            str(image.get(_OWNER_KEY, "")) == EMBEDDED_OBJ_SUPPORT_OWNER
            and image.users == 0
        ):
            try:
                bpy.data.images.remove(image)
                removed += 1
            except (ReferenceError, RuntimeError, TypeError, ValueError):
                continue
    return removed


def remove_all_embedded_obj_meshes() -> int:
    """새 프로젝트에서 ACTIVE·RETIRED 비연결 Mesh를 제거하고 실패 항목은 tombstone으로 둡니다."""

    validate_embedded_obj_project_cleanup()
    owners = {EMBEDDED_OBJ_OWNER, EMBEDDED_OBJ_RETIRED_OWNER}
    meshes = [mesh for mesh in bpy.data.meshes if str(mesh.get(_OWNER_KEY, "")) in owners]
    for mesh in meshes:
        try:
            bpy.data.meshes.remove(mesh)
        except (ReferenceError, RuntimeError, TypeError, ValueError):
            mesh[_OWNER_KEY] = EMBEDDED_OBJ_RETIRED_OWNER
            mesh.use_fake_user = False
    sweep_embedded_obj_support_data()
    return len(meshes)


def build_triangle_proxy_from_embedded_mesh(
    mesh: Any,
    *,
    model_id: str,
    display_name: str,
    source_path: str | Path,
    display_vertex_indices: np.ndarray | None = None,
    geometry_only: bool = False,
) -> TriangleMeshModelData:
    """원본 Polygon을 바꾸지 않고 calc_loop_triangles 기반 정렬 프록시를 생성합니다."""

    validate_embedded_obj_mesh(mesh, expected_model_id=model_id)
    vertex_count = len(mesh.vertices)
    mesh.calc_loop_triangles()
    triangle_count = len(mesh.loop_triangles)
    vertices = _foreach_array(mesh.vertices, "co", np.float64, (vertex_count, 3))
    triangles = _foreach_array(mesh.loop_triangles, "vertices", np.uint32, (triangle_count, 3))
    triangle_polygons = _foreach_array(
        mesh.loop_triangles, "polygon_index", np.uint32, (triangle_count,)
    )
    triangle_loops = _foreach_array(mesh.loop_triangles, "loops", np.uint32, (triangle_count, 3))
    registration = np.ascontiguousarray(np.unique(triangles.reshape(-1)), dtype=np.uint32)
    display = registration if display_vertex_indices is None else np.asarray(display_vertex_indices, dtype=np.uint32)
    uv_layer = None if geometry_only else mesh.uv_layers.active
    if uv_layer is None:
        texcoords = np.empty((0, 3), dtype=np.float64)
        component_counts = np.empty((0,), dtype=np.uint8)
        triangle_texcoords = np.full((triangle_count, 3), -1, dtype=np.int32)
    else:
        uv_values = _foreach_array(uv_layer.data, "uv", np.float64, (len(mesh.loops), 2))
        texcoords = np.zeros((len(mesh.loops), 3), dtype=np.float64)
        texcoords[:, :2] = uv_values
        component_counts = np.full((len(mesh.loops),), 2, dtype=np.uint8)
        triangle_texcoords = np.ascontiguousarray(triangle_loops, dtype=np.int32)
    polygon_materials = _foreach_array(
        mesh.polygons, "material_index", np.int32, (len(mesh.polygons),)
    )
    materials = () if geometry_only else _material_data(mesh)
    triangle_materials = (
        polygon_materials[triangle_polygons]
        if materials
        else np.full((triangle_count,), -1, dtype=np.int32)
    )
    return TriangleMeshModelData(
        model_id=model_id,
        display_name=display_name,
        source_path=Path(source_path),
        vertices_float64=vertices,
        triangles_uint32=triangles,
        registration_vertex_indices=registration,
        display_vertex_indices=display,
        bounds_min=vertices.min(axis=0),
        bounds_max=vertices.max(axis=0),
        geometry_revision=int(mesh[_REVISION_KEY]),
        texcoords_float64=texcoords,
        texcoord_component_counts_uint8=component_counts,
        triangle_texcoord_indices_int32=triangle_texcoords,
        materials=materials,
        triangle_material_indices_int32=triangle_materials,
        triangle_polygon_indices_uint32=triangle_polygons,
        triangle_loop_indices_uint32=triangle_loops,
        embedded_asset_uuid=str(mesh[_UUID_KEY]),
        embedded_asset_schema_version=int(mesh[_SCHEMA_KEY]),
        embedded_data_name_hint=str(mesh.name),
        original_polygon_count=len(mesh.polygons),
    )


def extract_embedded_obj_export_geometry(
    asset_uuid: str,
    *,
    expected_model_id: str,
    expected_revision: int,
) -> EmbeddedObjExportGeometry:
    """내장 원본 Mesh를 검증하고 Export 작업 전용 가변 코너 배열을 복사합니다."""

    mesh = find_embedded_obj_mesh(asset_uuid)
    validate_embedded_obj_mesh(
        mesh,
        expected_asset_uuid=asset_uuid,
        expected_model_id=expected_model_id,
        expected_revision=expected_revision,
    )
    vertices = _foreach_array(mesh.vertices, "co", np.float64, (len(mesh.vertices), 3))
    starts = _foreach_array(mesh.polygons, "loop_start", np.uint32, (len(mesh.polygons),))
    totals = _foreach_array(mesh.polygons, "loop_total", np.uint32, (len(mesh.polygons),))
    loop_vertices = _foreach_array(mesh.loops, "vertex_index", np.uint32, (len(mesh.loops),))
    uv_layer = mesh.uv_layers.active
    if uv_layer is None:
        texcoords = np.empty((0, 3), dtype=np.float64)
        component_counts = np.empty((0,), dtype=np.uint8)
        loop_texcoords = np.full((len(mesh.loops),), -1, dtype=np.int32)
    else:
        values = _foreach_array(uv_layer.data, "uv", np.float64, (len(mesh.loops), 2))
        texcoords = np.zeros((len(mesh.loops), 3), dtype=np.float64)
        texcoords[:, :2] = values
        component_counts = np.full((len(mesh.loops),), 2, dtype=np.uint8)
        loop_texcoords = np.arange(len(mesh.loops), dtype=np.int32)
    materials = _material_data(mesh)
    polygon_materials = (
        _foreach_array(mesh.polygons, "material_index", np.int32, (len(mesh.polygons),))
        if materials
        else np.full((len(mesh.polygons),), -1, dtype=np.int32)
    )
    return EmbeddedObjExportGeometry(
        asset_uuid=str(mesh[_UUID_KEY]),
        schema_version=int(mesh[_SCHEMA_KEY]),
        data_name_hint=str(mesh.name),
        geometry_revision=int(mesh[_REVISION_KEY]),
        vertices_float64=vertices,
        polygon_loop_starts_uint32=starts,
        polygon_loop_totals_uint32=totals,
        loop_vertex_indices_uint32=loop_vertices,
        texcoords_float64=texcoords,
        texcoord_component_counts_uint8=component_counts,
        loop_texcoord_indices_int32=loop_texcoords,
        materials=materials,
        polygon_material_indices_int32=polygon_materials,
    )


__all__ = (
    "EMBEDDED_OBJ_OWNER",
    "EMBEDDED_OBJ_RETIRED_OWNER",
    "EMBEDDED_OBJ_SUPPORT_OWNER",
    "EMBEDDED_OBJ_SCHEMA_VERSION",
    "EmbeddedObjExportGeometry",
    "EmbeddedObjStoreError",
    "build_triangle_proxy_from_embedded_mesh",
    "commit_embedded_obj_mesh",
    "extract_embedded_obj_export_geometry",
    "find_embedded_obj_mesh",
    "remove_all_embedded_obj_meshes",
    "remove_embedded_obj_mesh",
    "retire_embedded_obj_mesh",
    "sweep_embedded_obj_support_data",
    "validate_embedded_obj_project_cleanup",
    "validate_embedded_obj_mesh",
)
