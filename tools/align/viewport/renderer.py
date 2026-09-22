"""확정된 표시 인덱스로 실제 COLMAP GPU 배치와 마커 draw handler를 관리합니다."""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import bpy
import gpu
import numpy as np
from bpy_extras import view3d_utils
from gpu_extras.batch import batch_for_shader
from mathutils import Matrix, Vector

from ..constants import (
    GEOMETRY_KIND_COLMAP_POINT_CLOUD,
    GEOMETRY_KIND_TRIANGLE_MESH,
    ICP_ALIGNMENT_PHASE_CANCEL_PENDING,
    ICP_ALIGNMENT_PHASE_PREPARING,
    ICP_ALIGNMENT_PHASE_PREVIEW,
    ICP_ALIGNMENT_PHASE_RUNNING,
    MOVING_COLOR,
    NEUTRAL_COLOR,
    REFERENCE_COLOR,
    ROLE_MOVING,
    ROLE_REFERENCE,
    WORKSPACE_NAME,
    marker_pair_color,
)
from ..alignment import effective_alignment_for_model, transform_positions
from ..geometry_access import geometry_kind_for_model, is_triangle_mesh_model
from ..logging_utils import get_logger
from ..state import RUNTIME
from ..constants import MARKER_GEOMETRY_SOURCE_MAIN, MARKER_GEOMETRY_SOURCE_COMPANION
from ..marker_geometry import normalize_marker_geometry_source
from .labels import clear_display_transform_errors, draw_text, draw_viewport_labels
from .meter_grid_guide import (
    clear_meter_grid_resources,
    draw_meter_grid_labels,
    draw_meter_grid_lines,
)
from .picking import marker_source_model_ids
from .point_data import (
    companion_mesh_data_for_id,
    model_geometry_is_visible,
    marker_geometry_is_visible,
    surface_model_data_for_id,
    effective_model_matrix_for_id,
    effective_position_for_model_id,
    model_data_for_id,
    point_arrays,
    runtime_models,
)
from .registry import role_for_area

_LOGGER = get_logger("viewport.renderer")
_POINT_SHADER: Any | None = None
_MESH_SHADER: Any | None = None
_MESH_TEXTURE_SHADER: Any | None = None
_MESH_TEXTURE_RESOURCES: dict[Path, "_SharedTextureResource"] = {}
_DISPLAY_TRANSFORM_ERROR_MODEL_IDS: set[str] = set()
_MESH_NORMAL_FACE_CHUNK_SIZE = 131_072
_PREVIEW_REFERENCE_TRIANGLE_FACE_COLOR = (0.08, 0.78, 0.86, 0.22)
_PREVIEW_REFERENCE_TRIANGLE_EDGE_COLOR = (0.08, 0.78, 0.86, 0.78)
_PREVIEW_MOVING_TRIANGLE_FACE_COLOR = (1.0, 0.42, 0.08, 0.22)
_PREVIEW_MOVING_TRIANGLE_EDGE_COLOR = (1.0, 0.42, 0.08, 0.78)
_ICP_VIEWPORT_PREVIEW_PHASES = frozenset(
    {
        ICP_ALIGNMENT_PHASE_PREPARING,
        ICP_ALIGNMENT_PHASE_RUNNING,
        ICP_ALIGNMENT_PHASE_CANCEL_PENDING,
        ICP_ALIGNMENT_PHASE_PREVIEW,
    }
)
_POINT_FRAGMENT_SHADER_SOURCE = (
    "void main() {"
    "  vec4 tinted_color = vec4("
    "    vertex_color.rgb * identifier_color.rgb,"
    "    vertex_color.a * identifier_color.a"
    "  );"
    "  vec4 base_color = mix(tinted_color, vertex_color, use_colmap_rgb);"
    "  fragment_color = vec4(base_color.rgb, base_color.a * opacity);"
    "}"
)
_MESH_LIGHTING_SOURCE = (
    "  float normal_length = max(length(vertex_normal), 1e-8);"
    "  vec3 unit_normal = vertex_normal / normal_length;"
    "  vec3 key_light = normalize(vec3(-0.35, 0.45, 0.82));"
    "  vec3 fill_light = normalize(vec3(0.55, -0.35, 0.55));"
    "  float key = abs(dot(unit_normal, key_light));"
    "  float fill = abs(dot(unit_normal, fill_light));"
    "  float shade = clamp(0.22 + 0.62 * key + 0.16 * fill, 0.0, 1.0);"
)
_MESH_FRAGMENT_SHADER_SOURCE = (
    "void main() {"
    + _MESH_LIGHTING_SOURCE
    + "  fragment_color = vec4(mesh_color.rgb * shade, mesh_color.a * opacity);"
    "}"
)
_MESH_TEXTURE_FRAGMENT_SHADER_SOURCE = (
    "void main() {"
    + _MESH_LIGHTING_SOURCE
    + "  vec4 base_color = mesh_color;"
    "  if (use_texture > 0.5) {"
    "    base_color = texture(diffuse_texture, texture_uv);"
    "  }"
    "  fragment_color = vec4(base_color.rgb * shade, base_color.a * opacity);"
    "}"
)


@dataclass(frozen=True)
class PointCloudDrawPass:
    """한 역할의 재사용 GPU 점 배치를 어떤 합성 상태로 그릴지 보관합니다."""

    model_ids: tuple[str, ...]
    role: str
    opacity: float = 1.0
    depth_write: bool = True
    depth_test: str | None = None
    use_colmap_rgb: bool | None = None


@dataclass(frozen=True)
class _TexturedMeshBatch:
    """한 diffuse 텍스처를 공유하는 face-corner GPU 배치와 원본 경로를 보관합니다."""

    batch: Any
    texture_path: Path


@dataclass(frozen=True)
class _MeshSurfaceBatch:
    """한 메시의 역할 색상 면과 재질별 텍스처 면 배치를 묶어 보관합니다."""

    solid_batch: Any | None
    textured_batches: tuple[_TexturedMeshBatch, ...]


@dataclass
class _SharedTextureResource:
    """같은 이미지 경로를 쓰는 모델들이 공유하는 Blender Image와 GPUTexture를 관리합니다."""

    image: Any
    texture: Any
    owned_image: bool
    model_ids: set[str]


def _geometry_revision(model_data: Any) -> int:
    """모델 또는 PointTable이 제공하는 형상 리비전을 정수로 반환합니다."""
    points = getattr(model_data, "points", model_data)
    return int(getattr(model_data, "geometry_revision", getattr(points, "geometry_revision", 0)))


def _lod_revision(model_data: Any) -> int:
    """기존 GPU cache 호환용 표시 샘플 리비전을 정수로 반환합니다."""
    points = getattr(model_data, "points", model_data)
    return int(getattr(model_data, "lod_revision", getattr(points, "lod_revision", 0)))


def _display_indices_fingerprint(model_data: Any) -> str:
    """표시 샘플 인덱스와 원본 점 수의 결정적 짧은 fingerprint를 반환합니다."""
    arrays = point_arrays(model_data)
    normalized_indices = np.ascontiguousarray(
        arrays.display_indices,
        dtype=np.dtype("<u8"),
    )
    metadata = np.asarray(
        (int(arrays.positions.shape[0]), int(normalized_indices.size)),
        dtype=np.dtype("<u8"),
    )
    digest = hashlib.blake2b(digest_size=16)
    digest.update(metadata.tobytes())
    digest.update(normalized_indices.tobytes())
    return digest.hexdigest()


def _batch_key(model_data: Any) -> tuple[str, int, int, str, str]:
    """소유 모델·형상 종류·리비전·샘플로 점군과 메시 정점 배치를 구분합니다."""
    return (
        str(getattr(model_data, "model_id")),
        _geometry_revision(model_data),
        _lod_revision(model_data),
        _display_indices_fingerprint(model_data),
        geometry_kind_for_model(model_data),
    )


def _mesh_batch_key(model_data: Any) -> tuple[str, int, str, int, int]:
    """불변 삼각면의 모델·revision·면·텍스처 수로 GPU cache key를 만듭니다."""
    if not is_triangle_mesh_model(model_data):
        raise ValueError("삼각형 메시가 아닌 모델은 mesh batch key를 사용할 수 없습니다.")
    triangles = np.asarray(
        getattr(model_data, "triangles_uint32", getattr(model_data, "triangles"))
    )
    return (
        str(getattr(model_data, "model_id")),
        _geometry_revision(model_data),
        "TRIS",
        int(triangles.shape[0]),
        int(getattr(model_data, "diffuse_texture_count", 0)),
    )


def _cached_model_id(cache_key: Any) -> str | None:
    """새·이전 캐시 키에서 모델 ID만 안전하게 추출합니다."""
    if isinstance(cache_key, tuple) and cache_key:
        return str(cache_key[0])
    return None


def _cached_geometry_kind(cache_key: Any) -> str:
    """기존 점군 키와 새 형상별 키를 구분해 같은 소유 ID의 캐시를 보존합니다."""
    if isinstance(cache_key, tuple) and len(cache_key) >= 5:
        if cache_key[2] == "TRIS" or cache_key[-1] == GEOMETRY_KIND_TRIANGLE_MESH:
            return GEOMETRY_KIND_TRIANGLE_MESH
    return GEOMETRY_KIND_COLMAP_POINT_CLOUD


def _prepared_batch_for_model(model_id: str) -> Any | None:
    """해시 재계산 없이 소유 모델의 점 배치만 찾아 연결 표면 캐시와 구분합니다."""
    model = model_data_for_id(model_id)
    kind = geometry_kind_for_model(model) if model is not None else GEOMETRY_KIND_COLMAP_POINT_CLOUD
    for cache_key, batch in RUNTIME.gpu_batches.items():
        if (
            _cached_model_id(cache_key) == str(model_id)
            and _cached_geometry_kind(cache_key) == kind
            and not isinstance(batch, _MeshSurfaceBatch)
        ):
            return batch
    return None


def _point_shader() -> Any:
    """원본 RGB와 RGB 기반 식별색 음영을 전환하는 재사용 GPU 셰이더를 반환합니다."""
    global _POINT_SHADER
    if _POINT_SHADER is not None:
        return _POINT_SHADER

    shader_info = gpu.types.GPUShaderCreateInfo()
    shader_info.push_constant("MAT4", "ModelViewProjectionMatrix")
    shader_info.push_constant("FLOAT", "use_colmap_rgb")
    shader_info.push_constant("VEC4", "identifier_color")
    shader_info.push_constant("FLOAT", "opacity")
    shader_info.vertex_in(0, "VEC3", "pos")
    shader_info.vertex_in(1, "VEC4", "color")
    interface = gpu.types.GPUStageInterfaceInfo("colmap_align_point_interface")
    interface.smooth("VEC4", "vertex_color")
    shader_info.vertex_out(interface)
    shader_info.fragment_out(0, "VEC4", "fragment_color")
    shader_info.vertex_source(
        "void main() {"
        "  gl_Position = ModelViewProjectionMatrix * vec4(pos, 1.0);"
        "  vertex_color = color;"
        "}"
    )
    shader_info.fragment_source(_POINT_FRAGMENT_SHADER_SOURCE)
    _POINT_SHADER = gpu.shader.create_from_info(shader_info)
    return _POINT_SHADER


def _mesh_shader() -> Any:
    """삼각면에 역할 색상과 양면 조명을 적용하는 재사용 셰이더를 반환합니다."""
    global _MESH_SHADER
    if _MESH_SHADER is not None:
        return _MESH_SHADER
    shader_info = gpu.types.GPUShaderCreateInfo()
    shader_info.push_constant("MAT4", "ModelViewProjectionMatrix")
    shader_info.push_constant("MAT3", "NormalMatrix")
    shader_info.push_constant("VEC4", "mesh_color")
    shader_info.push_constant("FLOAT", "opacity")
    shader_info.vertex_in(0, "VEC3", "pos")
    shader_info.vertex_in(1, "VEC3", "normal")
    interface = gpu.types.GPUStageInterfaceInfo("colmap_align_mesh_interface")
    interface.smooth("VEC3", "vertex_normal")
    shader_info.vertex_out(interface)
    shader_info.fragment_out(0, "VEC4", "fragment_color")
    shader_info.vertex_source(
        "void main() {"
        "  gl_Position = ModelViewProjectionMatrix * vec4(pos, 1.0);"
        "  vertex_normal = normalize(NormalMatrix * normal);"
        "}"
    )
    shader_info.fragment_source(_MESH_FRAGMENT_SHADER_SOURCE)
    _MESH_SHADER = gpu.shader.create_from_info(shader_info)
    return _MESH_SHADER


def _mesh_texture_shader() -> Any:
    """UV diffuse 텍스처와 역할 색상을 전환하는 재사용 메시 셰이더를 반환합니다."""

    global _MESH_TEXTURE_SHADER
    if _MESH_TEXTURE_SHADER is not None:
        return _MESH_TEXTURE_SHADER
    shader_info = gpu.types.GPUShaderCreateInfo()
    shader_info.push_constant("MAT4", "ModelViewProjectionMatrix")
    shader_info.push_constant("MAT3", "NormalMatrix")
    shader_info.push_constant("VEC4", "mesh_color")
    shader_info.push_constant("FLOAT", "opacity")
    shader_info.push_constant("FLOAT", "use_texture")
    shader_info.sampler(0, "FLOAT_2D", "diffuse_texture")
    shader_info.vertex_in(0, "VEC3", "pos")
    shader_info.vertex_in(1, "VEC3", "normal")
    shader_info.vertex_in(2, "VEC2", "uv")
    interface = gpu.types.GPUStageInterfaceInfo("colmap_align_mesh_texture_interface")
    interface.smooth("VEC3", "vertex_normal")
    interface.smooth("VEC2", "texture_uv")
    shader_info.vertex_out(interface)
    shader_info.fragment_out(0, "VEC4", "fragment_color")
    shader_info.vertex_source(
        "void main() {"
        "  gl_Position = ModelViewProjectionMatrix * vec4(pos, 1.0);"
        "  vertex_normal = normalize(NormalMatrix * normal);"
        "  texture_uv = uv;"
        "}"
    )
    shader_info.fragment_source(_MESH_TEXTURE_FRAGMENT_SHADER_SOURCE)
    _MESH_TEXTURE_SHADER = gpu.shader.create_from_info(shader_info)
    return _MESH_TEXTURE_SHADER


def _mesh_vertex_normals(vertices: Any, triangles: Any) -> np.ndarray:
    """삼각면 면적을 가중치로 누적해 OBJ 메시의 부드러운 단위 정점 법선을 만듭니다."""
    normalized_vertices = np.asarray(vertices, dtype=np.float64)
    normalized_triangles = np.asarray(triangles, dtype=np.uint32)
    vertex_normals = np.zeros(normalized_vertices.shape, dtype=np.float64)
    for start in range(0, int(normalized_triangles.shape[0]), _MESH_NORMAL_FACE_CHUNK_SIZE):
        triangle_block = normalized_triangles[start : start + _MESH_NORMAL_FACE_CHUNK_SIZE]
        first = normalized_vertices[triangle_block[:, 0]]
        second = normalized_vertices[triangle_block[:, 1]]
        third = normalized_vertices[triangle_block[:, 2]]
        face_normals = np.cross(second - first, third - first)
        np.add.at(vertex_normals, triangle_block[:, 0], face_normals)
        np.add.at(vertex_normals, triangle_block[:, 1], face_normals)
        np.add.at(vertex_normals, triangle_block[:, 2], face_normals)
    lengths = np.linalg.norm(vertex_normals, axis=1)
    valid = lengths > np.finfo(np.float64).eps
    vertex_normals[valid] /= lengths[valid, np.newaxis]
    vertex_normals[~valid] = (0.0, 0.0, 1.0)
    return np.ascontiguousarray(vertex_normals, dtype=np.float32)


def _mesh_surface_arrays(model_data: Any, *, normals: np.ndarray | None = None) -> tuple[np.ndarray, tuple[tuple[int, np.ndarray, np.ndarray, np.ndarray], ...]]:
    """전달받은 정점 법선을 재사용해 메시 면을 역할 색상 인덱스와 재질별 위치·법선·UV로 분리합니다."""

    vertices = np.ascontiguousarray(model_data.vertices_float64, dtype=np.float32)
    triangles = np.ascontiguousarray(model_data.triangles_uint32, dtype=np.uint32)
    if normals is None:
        normals = _mesh_vertex_normals(vertices, triangles)
    face_materials = np.asarray(model_data.triangle_material_indices_int32, dtype=np.int32)
    face_texcoords = np.asarray(model_data.triangle_texcoord_indices_int32, dtype=np.int32)
    texcoords = np.asarray(model_data.texcoords_float64, dtype=np.float32)
    textured_face_mask = np.zeros((triangles.shape[0],), dtype=bool)
    textured_groups: list[tuple[int, np.ndarray, np.ndarray, np.ndarray]] = []
    for material_index, material in enumerate(model_data.materials):
        if not material.has_available_diffuse_texture:
            continue
        mask = (face_materials == material_index) & np.all(face_texcoords >= 0, axis=1)
        if not np.any(mask):
            continue
        textured_face_mask |= mask
        group_triangles = triangles[mask]
        group_texcoords = face_texcoords[mask]
        textured_groups.append(
            (
                material_index,
                np.ascontiguousarray(vertices[group_triangles].reshape((-1, 3)), dtype=np.float32),
                np.ascontiguousarray(normals[group_triangles].reshape((-1, 3)), dtype=np.float32),
                np.ascontiguousarray(texcoords[group_texcoords, :2].reshape((-1, 2)), dtype=np.float32),
            )
        )
    solid_triangles = np.ascontiguousarray(triangles[~textured_face_mask], dtype=np.uint32)
    return solid_triangles, tuple(textured_groups)


def _find_loaded_image_for_path(path: Path) -> Any | None:
    """Blender Image 데이터 블록에서 같은 절대 파일 경로를 쓰는 이미지를 찾습니다."""

    normalized = path.resolve(strict=False)
    for image in bpy.data.images:
        raw = str(getattr(image, "filepath", "") or "").strip()
        if not raw:
            continue
        try:
            image_path = Path(bpy.path.abspath(raw)).expanduser().resolve(strict=False)
        except (OSError, RuntimeError, ValueError):
            continue
        if image_path == normalized:
            return image
    return None


def _acquire_texture_resource(model_id: str, path: Path) -> _SharedTextureResource:
    """공유 이미지·GPU 텍스처를 준비하고 실패하면 새로 읽은 이미지도 정리합니다."""

    normalized = path.resolve(strict=False)
    cached = _MESH_TEXTURE_RESOURCES.get(normalized)
    if cached is not None:
        cached.model_ids.add(str(model_id))
        return cached
    existing = _find_loaded_image_for_path(normalized)
    image = existing or bpy.data.images.load(str(normalized), check_existing=True)
    try:
        texture = gpu.texture.from_image(image)
    except Exception:
        if existing is None:
            try:
                bpy.data.images.remove(image)
            except (ReferenceError, RuntimeError, ValueError):
                pass
        raise
    resource = _SharedTextureResource(
        image=image,
        texture=texture,
        owned_image=existing is None,
        model_ids={str(model_id)},
    )
    _MESH_TEXTURE_RESOURCES[normalized] = resource
    return resource


def _release_texture_resources_for_model(model_id: str) -> None:
    """모델의 공유 텍스처를 반납하고 유효한 마지막 소유 이미지만 제거합니다."""

    normalized_model_id = str(model_id)
    for path, resource in tuple(_MESH_TEXTURE_RESOURCES.items()):
        resource.model_ids.discard(normalized_model_id)
        if resource.model_ids:
            continue
        _MESH_TEXTURE_RESOURCES.pop(path, None)
        if resource.owned_image:
            try:
                if getattr(resource.image, "users", 0) == 0:
                    bpy.data.images.remove(resource.image)
            except (ReferenceError, RuntimeError, ValueError):
                pass


def _release_all_texture_resources() -> None:
    """등록 해제·파일 교체에서 모든 모델의 공유 텍스처 사용권을 정리합니다."""

    model_ids = {
        model_id
        for resource in _MESH_TEXTURE_RESOURCES.values()
        for model_id in resource.model_ids
    }
    for model_id in model_ids:
        _release_texture_resources_for_model(model_id)


def _batch_vertex_data(model_data: Any) -> tuple[np.ndarray, np.ndarray, int, int]:
    """원본 배열을 건드리지 않고 현재 표시 샘플의 GPU 속성 배열을 만듭니다."""
    arrays = point_arrays(model_data)
    display_indices = arrays.display_indices
    if display_indices.size == 0:
        raise ValueError("표시 샘플에 점이 없어 GPU 배치를 만들 수 없습니다.")
    display_positions = np.ascontiguousarray(
        arrays.positions[display_indices],
        dtype=np.float32,
    )
    display_colors = np.empty((display_indices.size, 4), dtype=np.float32)
    display_colors[:, :3] = arrays.colors[display_indices].astype(np.float32) / 255.0
    display_colors[:, 3] = 1.0
    return display_positions, display_colors, int(arrays.positions.shape[0]), int(display_indices.size)


def prepare_gpu_batch_for_model(model_id: str, model_data: Any | None = None) -> Any:
    """메시 법선을 한 번 계산해 GPU 배치를 준비하고 같은 소유 모델의 다른 형상 캐시는 보존합니다."""
    data = model_data if model_data is not None else model_data_for_id(model_id)
    if data is None:
        raise KeyError(f"GPU 배치를 만들 모델을 찾을 수 없습니다: {model_id}")
    if str(getattr(data, "model_id", model_id)) != str(model_id):
        raise ValueError("GPU 배치 모델 ID와 데이터 모델 ID가 일치하지 않습니다.")

    cache_key = _batch_key(data)
    cached_batch = RUNTIME.gpu_batches.get(cache_key)
    if cached_batch is not None and (
        not is_triangle_mesh_model(data) or _mesh_batch_key(data) in RUNTIME.gpu_batches
    ):
        return cached_batch

    if is_triangle_mesh_model(data):
        _release_texture_resources_for_model(model_id)

    try:
        positions, colors, source_count, display_count = _batch_vertex_data(data)
        batch = batch_for_shader(
            _point_shader(),
            "POINTS",
            {"pos": positions, "color": colors},
        )
    except Exception as error:
        _LOGGER.error(
            "event=gpu_batch_prepare_failed model_id=%s reason=%s",
            model_id,
            error,
        )
        raise

    stale_keys = tuple(
        key
        for key in RUNTIME.gpu_batches
        if (
            _cached_model_id(key) == str(model_id)
            and _cached_geometry_kind(key) == geometry_kind_for_model(data)
            and key != cache_key
        )
    )
    for stale_key in stale_keys:
        RUNTIME.gpu_batches.pop(stale_key, None)
    RUNTIME.gpu_batches[cache_key] = batch
    if is_triangle_mesh_model(data):
        mesh_key = _mesh_batch_key(data)
        try:
            mesh_vertices = np.ascontiguousarray(data.vertices_float64, dtype=np.float32)
            mesh_triangles = np.ascontiguousarray(
                data.triangles_uint32,
                dtype=np.uint32,
            )
            mesh_normals = _mesh_vertex_normals(mesh_vertices, mesh_triangles)
            solid_triangles, textured_groups = _mesh_surface_arrays(data, normals=mesh_normals)
            solid_batch = (
                None
                if solid_triangles.size == 0
                else batch_for_shader(
                    _mesh_shader(),
                    "TRIS",
                    {
                        "pos": mesh_vertices,
                        "normal": mesh_normals,
                    },
                    indices=solid_triangles,
                )
            )
            textured_batches = []
            for material_index, positions, normals, uvs in textured_groups:
                texture_path = data.materials[material_index].diffuse_texture_path
                if texture_path is None:
                    continue
                _acquire_texture_resource(model_id, texture_path)
                textured_batches.append(
                    _TexturedMeshBatch(
                        batch=batch_for_shader(
                            _mesh_texture_shader(),
                            "TRIS",
                            {"pos": positions, "normal": normals, "uv": uvs},
                        ),
                        texture_path=texture_path.resolve(strict=False),
                    )
                )
            RUNTIME.gpu_batches[mesh_key] = _MeshSurfaceBatch(
                solid_batch=solid_batch,
                textured_batches=tuple(textured_batches),
            )
        except Exception as error:
            RUNTIME.gpu_batches.pop(cache_key, None)
            _release_texture_resources_for_model(model_id)
            _LOGGER.error(
                "event=mesh_gpu_batch_prepare_failed model_id=%s reason=%s",
                model_id,
                error,
            )
            raise
    _LOGGER.info(
        "event=gpu_batch_prepared model_id=%s source_count=%d display_count=%d geometry_revision=%d "
        "display_sample_revision=%d display_sample_fingerprint=%s",
        model_id,
        source_count,
        display_count,
        cache_key[1],
        cache_key[2],
        cache_key[3],
    )
    return batch


def discard_gpu_batch_for_model(model_id: str) -> None:
    """지정 모델의 모든 형상·표시 샘플 리비전 GPU 배치 참조를 해제합니다."""
    stale_keys = tuple(
        key for key in RUNTIME.gpu_batches if _cached_model_id(key) == str(model_id)
    )
    for stale_key in stale_keys:
        RUNTIME.gpu_batches.pop(stale_key, None)
    _release_texture_resources_for_model(model_id)


def prepare_restored_gpu_batches(runtime: Any = RUNTIME) -> None:
    """복원 형상의 GPU·텍스처를 격리 준비하고 모두 성공한 경우에만 기존 자원을 교체합니다."""
    global _MESH_TEXTURE_RESOURCES
    if runtime is not RUNTIME:
        raise ValueError("GPU 복원은 현재 렌더러 런타임에서만 확정할 수 있습니다.")
    previous_batches = runtime.gpu_batches
    previous_resources = _MESH_TEXTURE_RESOURCES
    runtime.gpu_batches = {}
    _MESH_TEXTURE_RESOURCES = {}
    try:
        repository = runtime.model_repository
        for model_id, model in repository.models.items():
            prepare_gpu_batch_for_model(model_id, model)
            companion = companion_mesh_data_for_id(model_id)
            if companion is not None:
                prepare_gpu_batch_for_model(model_id, companion)
    except Exception:
        try:
            _release_all_texture_resources()
        finally:
            runtime.gpu_batches = previous_batches
            _MESH_TEXTURE_RESOURCES = previous_resources
        raise
    prepared_resources = _MESH_TEXTURE_RESOURCES
    for path, prepared in prepared_resources.items():
        previous = previous_resources.get(path)
        if previous is not None and prepared.image is previous.image:
            # 재사용한 Blender 이미지의 소유권은 새 캐시로 넘겨 기존 정리에서 삭제되지 않게 합니다.
            prepared.owned_image = previous.owned_image
            previous.owned_image = False
    _MESH_TEXTURE_RESOURCES = previous_resources
    _release_all_texture_resources()
    previous_resources.update(prepared_resources)


def ensure_gpu_batches() -> None:
    """데이터 변경 경계에서 점군·독립 메시·연결 표면 캐시를 함께 동기화합니다."""
    models = runtime_models()
    geometries = list(models.items())
    geometries.extend(
        (model_id, companion)
        for model_id in models
        if (companion := companion_mesh_data_for_id(model_id)) is not None
    )
    expected_keys = {
        key
        for _model_id, model_data in geometries
        for key in (
            (_batch_key(model_data), _mesh_batch_key(model_data))
            if is_triangle_mesh_model(model_data)
            else (_batch_key(model_data),)
        )
    }
    for model_id, model_data in geometries:
        cache_key = _batch_key(model_data)
        if cache_key in RUNTIME.gpu_batches and (
            not is_triangle_mesh_model(model_data)
            or _mesh_batch_key(model_data) in RUNTIME.gpu_batches
        ):
            continue
        try:
            prepare_gpu_batch_for_model(model_id, model_data)
        except Exception:
            # draw callback이 실패한 모델을 반복 생성하지 않도록 기존 유효 모델만 유지합니다.
            continue
    stale_keys = tuple(key for key in RUNTIME.gpu_batches if key not in expected_keys)
    for stale_key in stale_keys:
        RUNTIME.gpu_batches.pop(stale_key, None)


def _active_role() -> str | None:
    """등록된 뷰포트에서만 draw callback Area의 A/P 역할을 확인합니다."""
    context = bpy.context
    if context.window is None or context.area is None:
        return None
    if context.window.workspace.name != WORKSPACE_NAME:
        return None
    if not RUNTIME.viewport_registry.is_main_complete():
        return None
    return role_for_area(context.window, context.area)


def render_model_ids_for_role(
    role: str,
    catalog=None,
    available_model_ids: set[str] | frozenset[str] | None = None,
) -> tuple[str, ...]:
    """A 전체, 선택 P 또는 선택이 비어 있을 때 유일한 P의 표시 가능 ID를 반환합니다."""
    catalog = catalog or RUNTIME.model_catalog
    available = set(runtime_models()) if available_model_ids is None else set(available_model_ids)
    if role == ROLE_REFERENCE:
        return tuple(
            model.model_id
            for model in catalog.active_models()
            if model.is_visible and model.model_id in available
        )
    if role == ROLE_MOVING:
        selected_moving_model_ids = tuple(
            getattr(catalog, "selected_moving_model_ids", ())
        )
        selected_model_ids = tuple(
            model_id
            for model_id in selected_moving_model_ids
            if (
                model_id in catalog.models
                and catalog.models[model_id].is_visible
                and model_id in available
            )
        )
        if selected_moving_model_ids:
            return selected_model_ids
        inactive_models = getattr(catalog, "inactive_models", None)
        if not callable(inactive_models):
            return ()
        visible_model_ids = tuple(
            model.model_id
            for model in inactive_models()
            if model.is_visible and model.model_id in available
        )
        return visible_model_ids if len(visible_model_ids) == 1 else ()
    return ()


def multiselection_model_ids_for_role(
    role: str,
    catalog=None,
    available_model_ids: set[str] | frozenset[str] | None = None,
) -> tuple[str, ...]:
    """현재 다중 선택에서 표시 가능한 A 또는 P 역할 모델 전체를 반환합니다."""

    catalog = catalog or RUNTIME.model_catalog
    available = set(runtime_models()) if available_model_ids is None else set(available_model_ids)
    if role == ROLE_REFERENCE:
        selected_ids = getattr(catalog, "selected_reference_model_ids", ())
    elif role == ROLE_MOVING:
        selected_ids = getattr(catalog, "selected_moving_model_ids", ())
    else:
        return ()
    return tuple(
        model_id
        for model_id in selected_ids
        if (
            model_id in catalog.models
            and catalog.models[model_id].is_visible
            and model_id in available
        )
    )


def _model_view_projection_matrix(base_matrix: Any, model_id: str) -> Matrix:
    """원본 GPU 점 배치에 적용할 모델별 유효 Project World MVP 행렬을 만듭니다."""
    model_matrix = Matrix(effective_model_matrix_for_id(model_id).tolist())
    return base_matrix @ model_matrix


def _mesh_draw_matrices(
    projection_matrix: Any,
    view_matrix: Any,
    model_id: str,
    *,
    companion: bool = False,
) -> tuple[Matrix, Matrix]:
    """독립·연결 메시의 해당 축으로 위치 MVP와 법선용 역전치 행렬을 만듭니다."""
    model_matrix = Matrix(
        effective_model_matrix_for_id(model_id, companion=companion).tolist(),
    )
    model_view_matrix = view_matrix @ model_matrix
    normal_matrix = model_view_matrix.to_3x3().inverted_safe().transposed()
    return projection_matrix @ model_view_matrix, normal_matrix


def _display_marker_position(
    model_id: str,
    source_position: Any,
    geometry_source: str = MARKER_GEOMETRY_SOURCE_MAIN,
) -> np.ndarray:
    """마커가 속한 형상 축과 소유 모델의 공통 정렬로 표시 좌표를 계산합니다."""
    source = normalize_marker_geometry_source(geometry_source)
    return effective_position_for_model_id(
        model_id, source_position, companion=source == MARKER_GEOMETRY_SOURCE_COMPANION,
    )


def _log_display_transform_error_once(model_id: str, error: Exception) -> None:
    """축 변환 누락을 프레임마다 반복하지 않고 한 번만 구조화 로그로 남깁니다."""
    normalized_model_id = str(model_id)
    if normalized_model_id in _DISPLAY_TRANSFORM_ERROR_MODEL_IDS:
        return
    _DISPLAY_TRANSFORM_ERROR_MODEL_IDS.add(normalized_model_id)
    _LOGGER.error(
        "event=display_transform_unavailable model_id=%s error_type=%s reason=%s",
        normalized_model_id,
        type(error).__name__,
        error,
    )


def _marker_entries(role: str):
    """마커가 속한 형상이 보이는 확정·드래프트 마커만 중복 없이 반환합니다."""
    state = RUNTIME.marker_selection
    entries = []
    visible_model_ids = set(marker_source_model_ids(role))
    draft = state.draft
    suppressed_pair_id = draft.source_pair_id if draft is not None else None
    for pair in state.completed_pairs:
        if pair.pair_id == suppressed_pair_id:
            continue
        point = pair.reference if role == ROLE_REFERENCE else pair.moving
        geometry_source = getattr(point, "geometry_source", MARKER_GEOMETRY_SOURCE_MAIN)
        if point.model_id and (
            point.model_id not in visible_model_ids
            or not marker_geometry_is_visible(point.model_id, geometry_source)
        ):
            continue
        entries.append(
            (
                point.local_position,
                pair.pair_id,
                pair.pair_id == state.active_pair_id,
                point.model_id,
                geometry_source,
            )
        )
    if draft is not None:
        point = draft.reference if role == ROLE_REFERENCE else draft.moving
        if point is not None:
            geometry_source = getattr(point, "geometry_source", MARKER_GEOMETRY_SOURCE_MAIN)
            if point.model_id and (
                point.model_id not in visible_model_ids
                or not marker_geometry_is_visible(point.model_id, geometry_source)
            ):
                return entries
            # 생성·편집 드래프트는 이전 UIList 선택과 무관하게 현재 후보를 강조합니다.
            entries.append((point.local_position, draft.pair_id, True, point.model_id, geometry_source))
    return entries


def _identifier_color(role: str) -> tuple[float, float, float, float]:
    """원본 RGB를 끈 경우 역할 기반 식별색을 반환합니다."""
    if role == ROLE_REFERENCE:
        return REFERENCE_COLOR
    if role == ROLE_MOVING:
        return MOVING_COLOR
    return NEUTRAL_COLOR


def _draw_model_meshes(
    model_ids: tuple[str, ...],
    role: str,
    *,
    opacity: float = 1.0,
    depth_write: bool = True,
    depth_test: str | None = None,
) -> None:
    """독립·연결 표면을 형상별 축과 공통 소유 모델 정렬·역할 색상으로 그립니다."""
    solid_shader = _mesh_shader()
    texture_shader = _mesh_texture_shader()
    previous_depth_test = gpu.state.depth_test_get()
    previous_depth_mask = gpu.state.depth_mask_get()
    try:
        if depth_test is not None:
            gpu.state.depth_test_set(depth_test)
        gpu.state.depth_mask_set(bool(depth_write))
        projection_matrix = gpu.matrix.get_projection_matrix()
        view_matrix = gpu.matrix.get_model_view_matrix()
        mesh_color = _identifier_color(role)
        use_texture = bool(bpy.context.scene.colmap_align_settings.use_colmap_rgb)
        for model_id in model_ids:
            model_data = surface_model_data_for_id(model_id)
            if (
                not model_geometry_is_visible(model_id, GEOMETRY_KIND_TRIANGLE_MESH)
                or model_data is None
                or not is_triangle_mesh_model(model_data)
            ):
                continue
            mesh_key = _mesh_batch_key(model_data)
            surface_batch = RUNTIME.gpu_batches.get(mesh_key)
            if not isinstance(surface_batch, _MeshSurfaceBatch):
                continue
            try:
                model_view_projection, normal_matrix = _mesh_draw_matrices(
                    projection_matrix,
                    view_matrix,
                    model_id,
                    companion=model_data is not model_data_for_id(model_id),
                )
            except Exception as error:
                _log_display_transform_error_once(model_id, error)
                continue
            if surface_batch.solid_batch is not None:
                solid_shader.bind()
                solid_shader.uniform_float("ModelViewProjectionMatrix", model_view_projection)
                solid_shader.uniform_float("NormalMatrix", normal_matrix)
                solid_shader.uniform_float("mesh_color", mesh_color)
                solid_shader.uniform_float("opacity", float(opacity))
                surface_batch.solid_batch.draw(solid_shader)
            for textured_batch in surface_batch.textured_batches:
                resource = _MESH_TEXTURE_RESOURCES.get(textured_batch.texture_path)
                if resource is None:
                    continue
                texture_shader.bind()
                texture_shader.uniform_float("ModelViewProjectionMatrix", model_view_projection)
                texture_shader.uniform_float("NormalMatrix", normal_matrix)
                texture_shader.uniform_float("mesh_color", mesh_color)
                texture_shader.uniform_float("opacity", float(opacity))
                texture_shader.uniform_float("use_texture", 1.0 if use_texture else 0.0)
                texture_shader.uniform_sampler("diffuse_texture", resource.texture)
                textured_batch.batch.draw(texture_shader)
    finally:
        gpu.state.depth_mask_set(previous_depth_mask)
        gpu.state.depth_test_set(previous_depth_test)


def _draw_model_point_clouds(
    model_ids: tuple[str, ...],
    role: str,
    *,
    opacity: float = 1.0,
    depth_write: bool = True,
    depth_test: str | None = None,
    use_colmap_rgb: bool | None = None,
) -> None:
    """표시 중인 점군 또는 독립 메시 정점 배치를 역할·투명도·깊이 정책으로 그립니다."""
    settings = bpy.context.scene.colmap_align_settings
    shader = _point_shader()
    previous_depth_test = gpu.state.depth_test_get()
    previous_depth_mask = gpu.state.depth_mask_get()
    try:
        if depth_test is not None:
            gpu.state.depth_test_set(depth_test)
        gpu.state.depth_mask_set(bool(depth_write))
        shader.bind()
        base_mvp = gpu.matrix.get_projection_matrix() @ gpu.matrix.get_model_view_matrix()
        shader.uniform_float(
            "use_colmap_rgb",
            1.0
            if (settings.use_colmap_rgb if use_colmap_rgb is None else use_colmap_rgb)
            else 0.0,
        )
        shader.uniform_float("identifier_color", _identifier_color(role))
        shader.uniform_float("opacity", float(opacity))
        gpu.state.point_size_set(settings.point_size)
        for model_id in model_ids:
            model = model_data_for_id(model_id)
            kind = geometry_kind_for_model(model) if model is not None else GEOMETRY_KIND_COLMAP_POINT_CLOUD
            if not model_geometry_is_visible(model_id, kind):
                continue
            batch = _prepared_batch_for_model(model_id)
            if batch is None:
                continue
            try:
                model_mvp = _model_view_projection_matrix(base_mvp, model_id)
            except Exception as error:
                _log_display_transform_error_once(model_id, error)
                continue
            shader.uniform_float("ModelViewProjectionMatrix", model_mvp)
            batch.draw(shader)
    finally:
        gpu.state.depth_mask_set(previous_depth_mask)
        gpu.state.depth_test_set(previous_depth_test)


def _marker_preview_result():
    """현재 유효한 marker preview 결과만 draw callback에 전달합니다."""
    preview_state = getattr(RUNTIME, "marker_alignment", None)
    if preview_state is None or not preview_state.is_active:
        return None
    return preview_state.result


def _marker_preview_triangle_result():
    """ICP 완료 전까지만 대표 삼각형을 그릴 marker preview 결과를 반환합니다."""
    result = _marker_preview_result()
    if result is None:
        return None
    icp_state = getattr(RUNTIME, "icp_alignment", None)
    if str(getattr(icp_state, "phase", "")) == ICP_ALIGNMENT_PHASE_PREVIEW:
        return None
    return result


def _is_icp_viewport_preview_active() -> bool:
    """ICP 실행·결과 중 양쪽 뷰에 상대 모델을 함께 표시할지 반환합니다."""
    icp_state = getattr(RUNTIME, "icp_alignment", None)
    return str(getattr(icp_state, "phase", "")) in _ICP_VIEWPORT_PREVIEW_PHASES


def point_cloud_draw_passes_for_role(role: str) -> tuple[PointCloudDrawPass, ...]:
    """마커 입력 중 역할별 형상을 고정하고 일반 선택·미리보기 제출 순서를 반환합니다."""
    if role not in {ROLE_REFERENCE, ROLE_MOVING}:
        return ()

    if RUNTIME.marker_selection.has_draft:
        return (PointCloudDrawPass(model_ids=marker_source_model_ids(role), role=role),)

    catalog = RUNTIME.model_catalog
    selected_reference_ids = multiselection_model_ids_for_role(ROLE_REFERENCE)
    selected_moving_ids = multiselection_model_ids_for_role(ROLE_MOVING)
    if len(getattr(catalog, "selected_model_ids", ())) >= 2:
        # 복수 선택 중에는 개수와 역할 조합에 관계없이 양쪽 뷰에 선택된 모든 점군을 겹쳐 표시합니다.
        return tuple(
            draw_pass
            for draw_pass in (
                PointCloudDrawPass(
                    model_ids=selected_reference_ids,
                    role=ROLE_REFERENCE,
                    use_colmap_rgb=False,
                )
                if selected_reference_ids
                else None,
                PointCloudDrawPass(
                    model_ids=selected_moving_ids,
                    role=ROLE_MOVING,
                    use_colmap_rgb=False,
                )
                if selected_moving_ids
                else None,
            )
            if draw_pass is not None
        )

    own_pass = PointCloudDrawPass(
        model_ids=render_model_ids_for_role(role),
        role=role,
    )
    marker_preview_result = _marker_preview_result()
    if role == ROLE_REFERENCE and _is_icp_viewport_preview_active():
        # ICP 중 A 뷰에서도 P를 일반 불투명 렌더링으로 함께 표시합니다.
        return (
            own_pass,
            PointCloudDrawPass(
                model_ids=render_model_ids_for_role(ROLE_MOVING),
                role=ROLE_MOVING,
            ),
        )
    if role != ROLE_MOVING or marker_preview_result is None:
        return (own_pass,)

    # P 뷰에서는 A도 일반 불투명 렌더링으로 함께 표시합니다.
    return (
        own_pass,
        PointCloudDrawPass(
            model_ids=render_model_ids_for_role(ROLE_REFERENCE),
            role=ROLE_REFERENCE,
        ),
    )


def _draw_point_clouds(role: str) -> None:
    """표시 중인 COLMAP 점군만 제출하고 마커 입력 중에도 메시 정점은 겹쳐 그리지 않습니다."""
    for draw_pass in point_cloud_draw_passes_for_role(role):
        model_ids = tuple(
            model_id
            for model_id in draw_pass.model_ids
            if (
                (model := model_data_for_id(model_id)) is not None
                and model_geometry_is_visible(model_id, geometry_kind_for_model(model))
                and not is_triangle_mesh_model(model)
            )
        )
        if not model_ids:
            continue
        _draw_model_point_clouds(
            model_ids,
            draw_pass.role,
            opacity=draw_pass.opacity,
            depth_write=draw_pass.depth_write,
            depth_test=draw_pass.depth_test,
            use_colmap_rgb=draw_pass.use_colmap_rgb,
        )


def _draw_meshes(role: str) -> None:
    """양쪽 뷰와 미리보기에서 보이는 표면을 점군과 동일한 역할·변환 순서로 제출합니다."""
    for draw_pass in point_cloud_draw_passes_for_role(role):
        model_ids = tuple(
            model_id
            for model_id in draw_pass.model_ids
            if model_geometry_is_visible(model_id, GEOMETRY_KIND_TRIANGLE_MESH)
            and surface_model_data_for_id(model_id) is not None
        )
        if model_ids:
            _draw_model_meshes(
                model_ids,
                draw_pass.role,
                opacity=draw_pass.opacity,
                depth_write=draw_pass.depth_write,
                depth_test=draw_pass.depth_test,
            )


def preview_triangle_vertices(result) -> tuple[np.ndarray, np.ndarray]:
    """대표 삼각형의 A와 현재 marker·ICP preview P 정점을 Project World로 반환합니다."""
    triangle = result.representative_triangle
    reference_vertices = np.asarray(triangle.reference_vertices, dtype=np.float64)
    moving_vertices = _preview_moving_triangle_vertices(result)
    if (
        reference_vertices.shape != (3, 3)
        or moving_vertices.shape != (3, 3)
        or not np.isfinite(reference_vertices).all()
        or not np.isfinite(moving_vertices).all()
    ):
        raise ValueError("대표 삼각형 정점 형식이 올바르지 않습니다.")
    return (
        np.ascontiguousarray(reference_vertices, dtype=np.float32),
        np.ascontiguousarray(moving_vertices, dtype=np.float32),
    )


def _preview_moving_triangle_vertices(result) -> np.ndarray:
    """P 삼각형에 marker 기준 대비 현재 유효 alignment를 정확히 한 번 적용합니다."""
    triangle = result.representative_triangle
    source_project_vertices = np.asarray(triangle.moving_vertices, dtype=np.float64)
    moving_model_id = str(getattr(result, "moving_model_id", "")).strip()
    base_alignment = getattr(result, "base_alignment", None)
    if not moving_model_id or base_alignment is None:
        # 간단한 legacy fixture는 기존 marker delta 계약만 제공하므로 호환 경로를 유지합니다.
        return transform_positions(result.delta_transform, source_project_vertices)
    base_matrix = np.asarray(base_alignment.matrix_float64, dtype=np.float64)
    current_alignment = effective_alignment_for_model(RUNTIME, moving_model_id)
    current_matrix = np.asarray(current_alignment.matrix_float64, dtype=np.float64)
    if (
        base_matrix.shape != (4, 4)
        or current_matrix.shape != (4, 4)
        or not np.isfinite(base_matrix).all()
        or not np.isfinite(current_matrix).all()
    ):
        raise ValueError("대표 삼각형의 유효 alignment 행렬이 올바르지 않습니다.")
    # 삼각형 정점은 base committed Project World 좌표이므로 candidate를 한 번만 적용합니다.
    relative_matrix = current_matrix @ np.linalg.inv(base_matrix)
    homogeneous = np.ones((source_project_vertices.shape[0], 4), dtype=np.float64)
    homogeneous[:, :3] = source_project_vertices
    transformed = (relative_matrix @ homogeneous.T).T
    weights = transformed[:, 3]
    if (
        source_project_vertices.shape != (3, 3)
        or not np.isfinite(transformed).all()
        or np.any(np.abs(weights) <= np.finfo(np.float64).eps)
    ):
        raise ValueError("대표 P 삼각형의 유효 preview 좌표를 계산할 수 없습니다.")
    return np.ascontiguousarray(
        transformed[:, :3] / weights[:, np.newaxis],
        dtype=np.float64,
    )


def _draw_preview_triangle(
    vertices: np.ndarray,
    face_color: tuple[float, float, float, float],
    edge_color: tuple[float, float, float, float],
) -> None:
    """작은 GPU 삼각형 면과 외곽선을 그리고 변경한 GPU 상태를 정리합니다."""
    previous_blend = gpu.state.blend_get()
    previous_depth_test = gpu.state.depth_test_get()
    previous_depth_mask = gpu.state.depth_mask_get()
    previous_line_width = gpu.state.line_width_get()
    shader = gpu.shader.from_builtin("UNIFORM_COLOR")
    face_batch = batch_for_shader(shader, "TRIS", {"pos": vertices})
    edge_vertices = np.asarray(
        (
            vertices[0],
            vertices[1],
            vertices[1],
            vertices[2],
            vertices[2],
            vertices[0],
        ),
        dtype=np.float32,
    )
    edge_batch = batch_for_shader(shader, "LINES", {"pos": edge_vertices})
    try:
        gpu.state.blend_set("ALPHA")
        gpu.state.depth_test_set("LESS_EQUAL")
        gpu.state.depth_mask_set(False)
        shader.bind()
        shader.uniform_float("color", face_color)
        face_batch.draw(shader)
        shader.uniform_float("color", edge_color)
        gpu.state.line_width_set(1.5)
        edge_batch.draw(shader)
    finally:
        gpu.state.line_width_set(previous_line_width)
        gpu.state.depth_mask_set(previous_depth_mask)
        gpu.state.depth_test_set(previous_depth_test)
        gpu.state.blend_set(previous_blend)


def _draw_marker_preview_geometry(role: str) -> None:
    """ICP 완료 전 A/P 역할별로 캐시된 대표 삼각형만 그립니다."""
    result = _marker_preview_triangle_result()
    if result is None:
        return
    reference_vertices, moving_vertices = preview_triangle_vertices(result)
    if role == ROLE_REFERENCE:
        _draw_preview_triangle(
            reference_vertices,
            _PREVIEW_REFERENCE_TRIANGLE_FACE_COLOR,
            _PREVIEW_REFERENCE_TRIANGLE_EDGE_COLOR,
        )
        return
    if role != ROLE_MOVING:
        return
    _draw_preview_triangle(
        reference_vertices,
        _PREVIEW_REFERENCE_TRIANGLE_FACE_COLOR,
        _PREVIEW_REFERENCE_TRIANGLE_EDGE_COLOR,
    )
    _draw_preview_triangle(
        moving_vertices,
        _PREVIEW_MOVING_TRIANGLE_FACE_COLOR,
        _PREVIEW_MOVING_TRIANGLE_EDGE_COLOR,
    )


def _marker_circle_layers(pair_id: int, is_active: bool):
    """쌍 색상을 공유하는 원·외곽선 레이어와 활성 강조 크기를 반환합니다."""
    color = marker_pair_color(pair_id)
    outer_radius = 11.0 if is_active else 9.0
    outer_width = 5.0 if is_active else 3.0
    return (
        (outer_radius, color, outer_width),
        (6.0, color, 2.0),
    )


def _draw_markers(role: str) -> None:
    """형상별 표시 좌표에 같은 쌍의 원·외곽선을 그리고 활성 쌍을 강조합니다."""
    shader = gpu.shader.from_builtin("UNIFORM_COLOR")
    for source_position, pair_id, is_active, model_id, geometry_source in _marker_entries(role):
        try:
            position = _display_marker_position(model_id, source_position, geometry_source)
        except Exception as error:
            _log_display_transform_error_once(model_id, error)
            continue
        for radius, color, line_width in _marker_circle_layers(pair_id, is_active):
            _draw_marker_circle(shader, position, radius, color, line_width)


def _draw_marker_circle(shader, position, radius_px: float, color, line_width: float) -> None:
    """3차원 위치를 기준 깊이로 삼아 일정 픽셀 반경의 원 둘레를 생성합니다."""
    context = bpy.context
    region = context.region
    region_3d = context.space_data.region_3d
    center_2d = view3d_utils.location_3d_to_region_2d(
        region,
        region_3d,
        Vector(position),
        default=None,
    )
    if center_2d is None:
        return
    center_3d = Vector(position)
    vertices = []
    for step in range(25):
        angle = math.tau * step / 24.0
        screen_position = (
            center_2d.x + math.cos(angle) * radius_px,
            center_2d.y + math.sin(angle) * radius_px,
        )
        vertices.append(
            view3d_utils.region_2d_to_location_3d(
                region,
                region_3d,
                screen_position,
                center_3d,
            )
        )
    batch = batch_for_shader(shader, "LINE_STRIP", {"pos": vertices})
    shader.bind()
    shader.uniform_float("color", color)
    gpu.state.line_width_set(line_width)
    batch.draw(shader)


def _draw_post_view() -> None:
    """POST_VIEW 단계에서 형상·미터 눈금·preview 도형·3차원 마커를 그립니다."""
    role = _active_role()
    if role is None:
        return
    previous_blend = gpu.state.blend_get()
    previous_depth_test = gpu.state.depth_test_get()
    previous_depth_mask = gpu.state.depth_mask_get()
    previous_line_width = gpu.state.line_width_get()
    try:
        gpu.state.blend_set("ALPHA")
        gpu.state.depth_test_set("LESS_EQUAL")
        _draw_meshes(role)
        _draw_point_clouds(role)
        draw_meter_grid_lines(bpy.context)
        _draw_marker_preview_geometry(role)
        _draw_markers(role)
    finally:
        gpu.state.point_size_set(1.0)
        gpu.state.line_width_set(previous_line_width)
        gpu.state.depth_mask_set(previous_depth_mask)
        gpu.state.depth_test_set(previous_depth_test)
        gpu.state.blend_set(previous_blend)


def _draw_post_pixel() -> None:
    """POST_PIXEL 단계에서 미터 눈금 글자 위에 역할 라벨과 마커 안내를 표시합니다."""
    role = _active_role()
    if role is not None:
        draw_meter_grid_labels(bpy.context, draw_text)
        draw_viewport_labels(role)


def install_draw_handlers() -> None:
    """중복 없이 3D와 2D draw handler를 각각 하나씩 등록합니다."""
    if RUNTIME.draw_handlers:
        return
    post_view = bpy.types.SpaceView3D.draw_handler_add(
        _draw_post_view,
        (),
        "WINDOW",
        "POST_VIEW",
    )
    post_pixel = bpy.types.SpaceView3D.draw_handler_add(
        _draw_post_pixel,
        (),
        "WINDOW",
        "POST_PIXEL",
    )
    RUNTIME.draw_handlers.extend((("WINDOW", post_view), ("WINDOW", post_pixel)))


def remove_draw_handlers() -> None:
    """등록된 모든 draw handler와 형상·미터 눈금 GPU 자원을 해제합니다."""
    global _POINT_SHADER, _MESH_SHADER, _MESH_TEXTURE_SHADER
    while RUNTIME.draw_handlers:
        region_type, handle = RUNTIME.draw_handlers.pop()
        try:
            bpy.types.SpaceView3D.draw_handler_remove(handle, region_type)
        except (ReferenceError, RuntimeError, ValueError):
            pass
    _release_all_texture_resources()
    RUNTIME.gpu_batches.clear()
    _DISPLAY_TRANSFORM_ERROR_MODEL_IDS.clear()
    clear_display_transform_errors()
    clear_meter_grid_resources()
    _POINT_SHADER = None
    _MESH_SHADER = None
    _MESH_TEXTURE_SHADER = None


def request_redraw() -> None:
    """열린 모든 Window의 3D Viewport에 다시 그리기를 요청합니다."""
    window_manager = getattr(bpy.context, "window_manager", None)
    if window_manager is None:
        return
    for window in window_manager.windows:
        for area in window.screen.areas:
            if area.type == "VIEW_3D":
                area.tag_redraw()
