"""마우스 뷰포트와 실제 COLMAP 점 테이블의 NumPy 벡터화 피킹을 처리합니다."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any

import numpy as np
from bpy_extras import view3d_utils
from mathutils import Vector
from mathutils.bvhtree import BVHTree

from ..constants import (
    MARKER_POINT_KIND_COLMAP,
    MARKER_GEOMETRY_SOURCE_MAIN,
    MARKER_GEOMETRY_SOURCE_COMPANION,
    MARKER_POINT_KIND_MESH_SURFACE,
    MARKER_POINT_KIND_MESH_VERTEX,
    PICK_RADIUS_PX,
    ROLE_MOVING,
    ROLE_REFERENCE,
)
from ..geometry_access import display_point_indices_for_model, geometry_kind_for_model
from ..marker_geometry import normalize_marker_geometry_source
from ..display_axes import ModelDisplayTransform, display_positions
from ..logging_utils import get_logger
from ..model_repository import ModelRepositoryError
from ..state import RUNTIME
from .point_data import (
    effective_positions_for_model_id,
    effective_model_matrix_for_id,
    model_data_for_id,
    visible_marker_geometry_for_id,
    point_arrays,
    runtime_models,
)
from .registry import find_window_region, role_for_area
from .embedded_pick_bvh import build_embedded_pick_bvh


_LOGGER = get_logger("viewport.picking")
_MESH_SURFACE_BVH_CACHE: dict[tuple[str, str, int, int, int, int], _MeshSurfacePickBvh] = {}


@dataclass(frozen=True)
class _MeshSurfacePickBvh:
    """검색 구조와 원본 Polygon에서 안정 프록시 Triangle ID로 가는 대응을 보관합니다."""

    tree: BVHTree
    polygon_indices: np.ndarray | None = None
    triangle_order: np.ndarray | None = None

    def triangle_candidates(self, face_index: int) -> range | np.ndarray:
        """전체 배열의 형 변환 없이 적중 원본 면의 프록시 삼각형을 ID 순서로 반환합니다."""
        if self.polygon_indices is None:
            return range(face_index, face_index + 1)
        polygon_id = np.uint32(face_index)
        first = int(np.searchsorted(self.polygon_indices, polygon_id, side="left"))
        last = int(np.searchsorted(self.polygon_indices, polygon_id, side="right"))
        return range(first, last) if self.triangle_order is None else self.triangle_order[first:last]


@dataclass(frozen=True)
class PickResult:
    """피킹된 원본 점 또는 면의 모델·안정 ID·Source Local 좌표를 보관합니다."""

    model_id: str
    point3d_id: int
    position: tuple[float, float, float]
    screen_distance: float
    depth: float
    point_kind: str = MARKER_POINT_KIND_COLMAP
    source_point_id: int | None = None
    geometry_source: str = MARKER_GEOMETRY_SOURCE_MAIN


def clear_mesh_surface_pick_cache() -> None:
    """세션 종료·파일 교체 시 기존 BVH와 화면 선택 GPU 셰이더 참조를 해제합니다."""

    from .screen_pick import clear_screen_pick_resources
    _MESH_SURFACE_BVH_CACHE.clear()
    clear_screen_pick_resources()


def discard_mesh_surface_pick_cache_for_model(
    model_id: str,
    geometry_source: str | None = None,
) -> None:
    """소유 모델 전체 또는 지정 형상 슬롯의 이전 피킹 캐시를 제거합니다."""

    normalized_id = str(model_id)
    source = None if geometry_source is None else normalize_marker_geometry_source(geometry_source)
    stale_keys = tuple(
        key for key in _MESH_SURFACE_BVH_CACHE
        if key[0] == normalized_id and (source is None or key[1] == source)
    )
    for key in stale_keys:
        _MESH_SURFACE_BVH_CACHE.pop(key, None)


def _region_contains_window_coordinate(region: Any, mouse_x: int, mouse_y: int) -> bool:
    """한 Region의 실제 Window 좌표 사각형에 마우스가 포함되는지 반환합니다."""

    return bool(
        int(getattr(region, "width", 0)) > 0
        and int(getattr(region, "height", 0)) > 0
        and int(region.x) <= mouse_x < int(region.x) + int(region.width)
        and int(region.y) <= mouse_y < int(region.y) + int(region.height)
    )


def find_viewport_under_mouse(window: Any, mouse_x: int, mouse_y: int) -> tuple[Any, Any, str] | None:
    """겹친 UI를 제외하고 Window 좌표 아래의 역할 VIEW_3D 영역을 찾습니다."""

    for area in window.screen.areas:
        if area.type != "VIEW_3D":
            continue
        if not (area.x <= mouse_x < area.x + area.width):
            continue
        if not (area.y <= mouse_y < area.y + area.height):
            continue
        region = find_window_region(area)
        if region is None:
            continue
        if not _region_contains_window_coordinate(region, mouse_x, mouse_y):
            continue
        if any(
            item.type != "WINDOW"
            and _region_contains_window_coordinate(item, mouse_x, mouse_y)
            for item in area.regions
        ):
            # N 패널·헤더처럼 WINDOW와 겹치는 UI 클릭은 뷰포트 입력이 아닙니다.
            return None
        role = role_for_area(window, area)
        if role is not None:
            return area, region, role
    return None


def marker_source_model_ids(role: str) -> tuple[str, ...]:
    """A는 표시 중인 활성 전체, P는 선택 비활성 하나의 피킹 모델을 반환합니다."""
    catalog = RUNTIME.model_catalog
    available_model_ids = set(runtime_models())
    if role == ROLE_REFERENCE:
        return tuple(
            model.model_id
            for model in catalog.active_models()
            if model.is_visible and model.model_id in available_model_ids
        )
    if role == ROLE_MOVING:
        selected = catalog.selected_inactive_model
        if (
            selected is not None
            and selected.is_visible
            and selected.model_id in available_model_ids
        ):
            return (selected.model_id,)
    return ()


def marker_pick_model_ids(role: str) -> tuple[str, ...]:
    """역할에 속한 표시 레이어 중 주 형상 또는 연결 표면이 보이는 소유자를 반환합니다."""
    return tuple(
        model_id
        for model_id in marker_source_model_ids(role)
        if visible_marker_geometry_for_id(model_id) is not None
    )


def marker_source_model_id(role: str) -> str | None:
    """단일 모델이 필요한 기존 호출에 첫 피킹 모델 ID를 호환 반환합니다."""
    model_ids = marker_source_model_ids(role)
    return model_ids[0] if model_ids else None


def _matrix_array(value: Any) -> np.ndarray | None:
    """Blender 행렬을 NumPy 4x4 float64 배열로 안전하게 변환합니다."""
    try:
        matrix = np.asarray(value, dtype=np.float64)
    except (TypeError, ValueError):
        return None
    return matrix if matrix.shape == (4, 4) and np.isfinite(matrix).all() else None


def _view_ray(
    region: Any,
    region_3d: Any,
    mouse_region: tuple[float, float],
) -> tuple[np.ndarray, np.ndarray]:
    """현재 3D 뷰의 마우스 좌표를 Project World 광선 원점·방향으로 변환합니다."""

    origin = np.asarray(
        view3d_utils.region_2d_to_origin_3d(region, region_3d, mouse_region),
        dtype=np.float64,
    )
    direction = np.asarray(
        view3d_utils.region_2d_to_vector_3d(region, region_3d, mouse_region),
        dtype=np.float64,
    )
    direction_length = float(np.linalg.norm(direction))
    if (
        origin.shape != (3,)
        or direction.shape != (3,)
        or not np.isfinite(origin).all()
        or not np.isfinite(direction).all()
        or direction_length <= np.finfo(np.float64).eps
    ):
        raise ValueError("뷰포트 피킹 광선을 계산할 수 없습니다.")
    return origin, np.ascontiguousarray(direction / direction_length, dtype=np.float64)


def _screen_view_ray(
    region: Any, region_3d: Any, mouse_region: tuple[float, float],
) -> tuple[np.ndarray, np.ndarray]:
    """화면 투영을 배정밀도로 역산해 GPU 픽셀과 일치하는 근평면 광선을 만듭니다."""
    width, height = int(region.width), int(region.height)
    projection = _matrix_array(region_3d.perspective_matrix)
    if width <= 0 or height <= 0 or projection is None:
        raise ValueError("마커 선택 광선의 화면 투영이 올바르지 않습니다.")
    x, y = float(mouse_region[0]), float(mouse_region[1])
    if not np.isfinite((x, y)).all() or not (0 <= x < width and 0 <= y < height):
        raise ValueError("마커 선택 위치가 뷰포트 범위를 벗어났습니다.")
    try:
        inverse = np.linalg.inv(projection)
    except np.linalg.LinAlgError as error:
        raise ValueError("마커 선택 투영의 역행렬을 계산할 수 없습니다.") from error
    near = inverse @ np.asarray((2 * x / width - 1, 2 * y / height - 1, -1, 1))
    middle = inverse @ np.asarray((2 * x / width - 1, 2 * y / height - 1, 0, 1))
    if not np.isfinite((near, middle)).all() or min(abs(near[3]), abs(middle[3])) <= np.finfo(np.float64).tiny:
        raise ValueError("마커 선택 광선을 유한한 좌표로 복원할 수 없습니다.")
    origin = near[:3] / near[3]
    direction = middle[:3] / middle[3] - origin
    length = float(np.linalg.norm(direction))
    if not math.isfinite(length) or length <= np.finfo(np.float64).tiny:
        raise ValueError("마커 선택 광선 방향을 계산할 수 없습니다.")
    return np.ascontiguousarray(origin), np.ascontiguousarray(direction / length)


def _ray_in_source_local(
    model_id: str,
    ray_origin: np.ndarray,
    ray_direction: np.ndarray,
    geometry_source: str = MARKER_GEOMETRY_SOURCE_MAIN,
) -> tuple[np.ndarray, np.ndarray]:
    """선택한 형상 축과 소유자 공통 정렬을 역변환해 원본 피킹 광선을 만듭니다."""

    source = normalize_marker_geometry_source(geometry_source)
    model_matrix = effective_model_matrix_for_id(
        model_id, companion=source == MARKER_GEOMETRY_SOURCE_COMPANION,
    )
    try:
        inverse = np.linalg.inv(model_matrix)
    except np.linalg.LinAlgError as error:
        raise ModelRepositoryError("메시 표시 행렬의 역행렬을 계산할 수 없습니다.") from error
    origin_homogeneous = np.ones(4, dtype=np.float64)
    origin_homogeneous[:3] = ray_origin
    local_origin_homogeneous = inverse @ origin_homogeneous
    weight = float(local_origin_homogeneous[3])
    if not np.isfinite(local_origin_homogeneous).all() or abs(weight) <= np.finfo(np.float64).eps:
        raise ModelRepositoryError("피킹 광선 원점을 Source Local로 변환할 수 없습니다.")
    local_origin = local_origin_homogeneous[:3] / weight
    local_direction = inverse[:3, :3] @ ray_direction
    direction_length = float(np.linalg.norm(local_direction))
    if not np.isfinite(local_direction).all() or direction_length <= np.finfo(np.float64).eps:
        raise ModelRepositoryError("피킹 광선 방향을 Source Local로 변환할 수 없습니다.")
    return (
        np.ascontiguousarray(local_origin, dtype=np.float64),
        np.ascontiguousarray(local_direction / direction_length, dtype=np.float64),
    )


def _ray_intersects_bounds(
    ray_origin: np.ndarray,
    ray_direction: np.ndarray,
    minimum: np.ndarray,
    maximum: np.ndarray,
) -> bool:
    """광선이 Source Local AABB의 앞쪽 구간과 교차하는지 slab 검사로 판별합니다."""

    near = -math.inf
    far = math.inf
    epsilon = np.finfo(np.float64).eps
    for axis in range(3):
        direction = float(ray_direction[axis])
        origin = float(ray_origin[axis])
        if abs(direction) <= epsilon:
            if origin < float(minimum[axis]) or origin > float(maximum[axis]):
                return False
            continue
        first = (float(minimum[axis]) - origin) / direction
        second = (float(maximum[axis]) - origin) / direction
        near = max(near, min(first, second))
        far = min(far, max(first, second))
        if far < max(near, 0.0):
            return False
    return far >= max(near, 0.0)


def _mesh_surface_bvh(
    model_id: str,
    model_data: Any,
    geometry_source: str = MARKER_GEOMETRY_SOURCE_MAIN,
) -> _MeshSurfacePickBvh:
    """내장 Mesh의 네이티브 검색 구조와 프록시 대응을 형상 리비전별로 재사용합니다."""

    vertices = np.asarray(
        getattr(model_data, "vertices_float64", getattr(model_data, "vertices")),
        dtype=np.float64,
    )
    triangles = np.asarray(
        getattr(model_data, "triangles_uint32", getattr(model_data, "triangles")),
        dtype=np.uint32,
    )
    source = normalize_marker_geometry_source(geometry_source)
    cache_key = (
        str(model_id),
        source,
        id(model_data),
        int(getattr(model_data, "geometry_revision", 0)),
        int(vertices.shape[0]),
        int(triangles.shape[0]),
    )
    cached = _MESH_SURFACE_BVH_CACHE.get(cache_key)
    if cached is not None:
        return cached
    discard_mesh_surface_pick_cache_for_model(model_id, source)
    if str(getattr(model_data, "embedded_asset_uuid", "")).strip():
        try:
            tree = build_embedded_pick_bvh(model_id, model_data)
        except RuntimeError as error:
            raise ModelRepositoryError(f"내장 메시의 면 선택 검색 구조를 만들 수 없습니다: {error}") from error
        polygon_indices = np.asarray(model_data.triangle_polygon_indices_uint32, dtype=np.uint32)
        triangle_order = None
        if np.any(polygon_indices[1:] < polygon_indices[:-1]):
            triangle_order = np.argsort(polygon_indices, kind="stable")
            polygon_indices = polygon_indices[triangle_order]
            triangle_order.setflags(write=False)
            polygon_indices.setflags(write=False)
        result = _MeshSurfacePickBvh(tree, polygon_indices, triangle_order)
    else:
        # 내장 원본이 없는 배열 모델의 기존 피킹 계약도 유지합니다.
        result = _MeshSurfacePickBvh(BVHTree.FromPolygons(
            vertices, triangles, all_triangles=True, epsilon=0.0,
        ))
    _MESH_SURFACE_BVH_CACHE[cache_key] = result
    return result


def _ray_triangle_position_float64(
    ray_origin: np.ndarray,
    ray_direction: np.ndarray,
    triangle: np.ndarray,
) -> np.ndarray | None:
    """검색된 원본 삼각면에 광선을 float64로 교차해 저장 좌표를 계산합니다."""

    edge_first = triangle[1] - triangle[0]
    edge_second = triangle[2] - triangle[0]
    cross_direction = np.cross(ray_direction, edge_second)
    determinant = float(np.dot(edge_first, cross_direction))
    determinant_tolerance = np.finfo(np.float64).eps * max(
        float(np.linalg.norm(edge_first)) * float(np.linalg.norm(edge_second)),
        1.0,
    )
    if abs(determinant) <= determinant_tolerance:
        return None
    inverse_determinant = 1.0 / determinant
    offset = ray_origin - triangle[0]
    first_weight = float(np.dot(offset, cross_direction)) * inverse_determinant
    cross_offset = np.cross(offset, edge_first)
    second_weight = float(np.dot(ray_direction, cross_offset)) * inverse_determinant
    distance = float(np.dot(edge_second, cross_offset)) * inverse_determinant
    tolerance = 1.0e-12
    if (
        distance < -tolerance
        or first_weight < -tolerance
        or second_weight < -tolerance
        or first_weight + second_weight > 1.0 + tolerance
    ):
        return None
    return np.ascontiguousarray(
        ray_origin + max(distance, 0.0) * ray_direction,
        dtype=np.float64,
    )


def _pick_model_mesh_surface_from_ray(
    model_id: str,
    model_data: Any,
    region: Any,
    region_3d: Any,
    ray_origin: np.ndarray,
    ray_direction: np.ndarray,
    geometry_source: str = MARKER_GEOMETRY_SOURCE_MAIN,
) -> PickResult | None:
    """적중 원본 면의 프록시 삼각형만 재교차해 기존 Triangle ID와 float64 좌표를 보존합니다."""

    local_origin, local_direction = _ray_in_source_local(
        model_id,
        ray_origin,
        ray_direction,
        geometry_source,
    )
    minimum = np.asarray(getattr(model_data, "bounds_min"), dtype=np.float64)
    maximum = np.asarray(getattr(model_data, "bounds_max"), dtype=np.float64)
    if not _ray_intersects_bounds(local_origin, local_direction, minimum, maximum):
        return None
    surface_bvh = _mesh_surface_bvh(
        model_id,
        model_data,
        geometry_source,
    )
    location, _normal, face_index, _distance = surface_bvh.tree.ray_cast(
        Vector(local_origin.tolist()), Vector(local_direction.tolist()),
    )
    if location is None or face_index is None:
        return None
    vertices = np.asarray(
        getattr(model_data, "vertices_float64", getattr(model_data, "vertices")),
        dtype=np.float64,
    )
    triangles = np.asarray(
        getattr(model_data, "triangles_uint32", getattr(model_data, "triangles")),
        dtype=np.uint32,
    )
    local_position = None
    nearest_distance = math.inf
    triangle_index = -1
    for candidate_index in surface_bvh.triangle_candidates(int(face_index)):
        triangle = vertices[triangles[int(candidate_index)]]
        candidate_position = _ray_triangle_position_float64(local_origin, local_direction, triangle)
        if candidate_position is None:
            continue
        distance = float(np.dot(candidate_position - local_origin, local_direction))
        if distance < nearest_distance:
            nearest_distance = distance
            local_position = candidate_position
            triangle_index = int(candidate_index)
    if local_position is None:
        return None
    project_position = effective_positions_for_model_id(
        model_id,
        local_position[np.newaxis, :],
        companion=geometry_source == MARKER_GEOMETRY_SOURCE_COMPANION,
    )[0]
    ray_depth = float(np.dot(project_position - ray_origin, ray_direction))
    if not np.isfinite(project_position).all() or not math.isfinite(ray_depth) or ray_depth < 0.0:
        return None
    position = tuple(float(value) for value in local_position)
    return PickResult(
        model_id=model_id,
        point3d_id=triangle_index,
        position=position,
        screen_distance=0.0,
        depth=ray_depth,
        point_kind=MARKER_POINT_KIND_MESH_SURFACE,
        source_point_id=triangle_index,
        geometry_source=normalize_marker_geometry_source(geometry_source),
    )


def _pick_model_mesh_surface_from_screen(
    model_id: str, model_data: Any, region: Any, region_3d: Any,
    mouse_region: tuple[float, float], ray_origin: np.ndarray, ray_direction: np.ndarray,
    geometry_source: str = MARKER_GEOMETRY_SOURCE_MAIN,
) -> PickResult | None:
    """화면 ID 후보만 배정밀도로 재교차하여 원본 ID와 Source Local 좌표를 반환합니다."""
    from .renderer import prepared_mesh_pick_batch
    from .screen_pick import screen_triangle_candidates

    source = normalize_marker_geometry_source(geometry_source)
    local_origin, local_direction = _ray_in_source_local(model_id, ray_origin, ray_direction, source)
    if not _ray_intersects_bounds(local_origin, local_direction, model_data.bounds_min, model_data.bounds_max):
        return None
    try:
        candidates = screen_triangle_candidates(
            prepared_mesh_pick_batch(model_id, model_data),
            effective_model_matrix_for_id(model_id, companion=source == MARKER_GEOMETRY_SOURCE_COMPANION),
            region, region_3d, mouse_region, len(model_data.triangles_uint32),
            (model_data.bounds_min, model_data.bounds_max),
        )
    except (RuntimeError, ValueError, ReferenceError) as error:
        raise ModelRepositoryError(f"화면에서 마커 표면을 선택할 수 없습니다: {error}") from error
    best = None
    nearest = math.inf
    for triangle_id in candidates:
        triangle = model_data.vertices_float64[model_data.triangles_uint32[triangle_id]]
        local_position = _ray_triangle_position_float64(local_origin, local_direction, triangle)
        if local_position is None:
            continue
        project_position = effective_positions_for_model_id(
            model_id, local_position[np.newaxis, :], companion=source == MARKER_GEOMETRY_SOURCE_COMPANION,
        )[0]
        depth = float(np.dot(project_position - ray_origin, ray_direction))
        if np.isfinite(project_position).all() and math.isfinite(depth) and 0 <= depth < nearest:
            nearest = depth
            best = PickResult(
                model_id=model_id, point3d_id=triangle_id,
                position=tuple(float(value) for value in local_position),
                screen_distance=0.0, depth=depth, point_kind=MARKER_POINT_KIND_MESH_SURFACE,
                source_point_id=triangle_id, geometry_source=source,
            )
    return best


def _project_positions(
    region: Any,
    region_3d: Any,
    positions: np.ndarray,
    transform: ModelDisplayTransform | None = None,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """원본 좌표에 표시 축만 임시 적용해 현재 view-projection으로 투영합니다."""
    source_positions = np.asarray(positions, dtype=np.float64)
    shown_positions = (
        display_positions(transform, source_positions)
        if transform is not None
        else source_positions
    )
    point_count = int(shown_positions.shape[0])
    homogeneous = np.empty((point_count, 4), dtype=np.float64)
    homogeneous[:, :3] = shown_positions
    homogeneous[:, 3] = 1.0

    view_matrix = _matrix_array(getattr(region_3d, "view_matrix", None))
    if view_matrix is None:
        raise ValueError("뷰 행렬을 읽을 수 없습니다.")
    view_positions = homogeneous @ view_matrix.T
    depths = -view_positions[:, 2]
    valid = np.isfinite(depths) & (depths > 0.0)

    perspective_matrix = _matrix_array(getattr(region_3d, "perspective_matrix", None))
    if perspective_matrix is None:
        # 최소 fixture는 투영 행렬을 제공하지 않으므로 로컬 XY를 직교 화면 좌표로 사용합니다.
        screen_positions = shown_positions[:, :2]
    else:
        clip_positions = homogeneous @ perspective_matrix.T
        clip_w = clip_positions[:, 3]
        valid &= np.isfinite(clip_w) & (np.abs(clip_w) > np.finfo(np.float64).eps)
        normalized = np.zeros((point_count, 2), dtype=np.float64)
        nonzero_w = np.abs(clip_w) > np.finfo(np.float64).eps
        normalized[nonzero_w] = (
            clip_positions[nonzero_w, :2] / clip_w[nonzero_w, np.newaxis]
        )
        screen_positions = np.empty_like(normalized)
        screen_positions[:, 0] = (normalized[:, 0] * 0.5 + 0.5) * float(region.width)
        screen_positions[:, 1] = (normalized[:, 1] * 0.5 + 0.5) * float(region.height)

    valid &= np.isfinite(screen_positions).all(axis=1)
    return screen_positions, depths, valid


def _pick_model_nearest_point(
    model_id: str,
    region: Any,
    region_3d: Any,
    mouse_region: tuple[float, float],
    radius_squared: float,
    ray_origin: np.ndarray | None = None,
    ray_direction: np.ndarray | None = None,
) -> PickResult | None:
    """메시의 전체 ID·색상을 만들지 않고 표시 정점만 기존 깊이 우선순위로 선택합니다."""
    model_data = model_data_for_id(model_id)
    if model_data is None:
        return None
    is_mesh = geometry_kind_for_model(model_data) == "TRIANGLE_MESH"
    if is_mesh:
        positions = np.asarray(
            getattr(model_data, "vertices_float64", getattr(model_data, "vertices")),
            dtype=np.float64,
        )
        display_indices = display_point_indices_for_model(model_data)
        point_ids = None
    else:
        arrays = point_arrays(model_data)
        positions = arrays.positions
        display_indices = arrays.display_indices
        point_ids = arrays.point_ids
    if display_indices.size == 0:
        return None

    source_display_positions = positions[display_indices]
    shown_positions = effective_positions_for_model_id(
        model_id,
        source_display_positions,
    )
    screen_positions, depths, valid = _project_positions(
        region,
        region_3d,
        shown_positions,
    )
    has_view_ray = ray_origin is not None and ray_direction is not None
    if has_view_ray:
        depths = (shown_positions - ray_origin) @ ray_direction
        valid &= np.isfinite(depths) & (depths >= 0.0)
    mouse_x, mouse_y = mouse_region
    distance_squared = (
        (screen_positions[:, 0] - float(mouse_x)) ** 2
        + (screen_positions[:, 1] - float(mouse_y)) ** 2
    )
    candidate_offsets = np.flatnonzero(valid & (distance_squared <= radius_squared))
    if candidate_offsets.size == 0:
        return None

    # 실제 광선에서는 앞쪽 가림점을 보존하고 같은 깊이에서 화면 거리를 비교합니다.
    candidate_keys = (
        (distance_squared[candidate_offsets], depths[candidate_offsets])
        if has_view_ray
        else (depths[candidate_offsets], distance_squared[candidate_offsets])
    )
    candidate_order = np.lexsort(candidate_keys)
    display_offset = int(candidate_offsets[int(candidate_order[0])])
    source_index = int(display_indices[display_offset])
    position = positions[source_index]
    point_kind = (
        MARKER_POINT_KIND_MESH_VERTEX
        if is_mesh
        else MARKER_POINT_KIND_COLMAP
    )
    source_point_id = source_index if point_ids is None else int(point_ids[source_index])
    return PickResult(
        model_id=model_id,
        point3d_id=source_point_id,
        position=(float(position[0]), float(position[1]), float(position[2])),
        screen_distance=math.sqrt(float(distance_squared[display_offset])),
        depth=float(depths[display_offset]),
        point_kind=point_kind,
        source_point_id=source_point_id,
    )


def pick_nearest_point_for_model_ids(
    model_ids: tuple[str, ...],
    region: Any,
    region_3d: Any,
    mouse_region: tuple[float, float],
    radius: float = PICK_RADIUS_PX,
    *,
    screen_surface: bool = False,
) -> PickResult | None:
    """표시 레이어를 선택하며 화면 표면 모드는 마커와 피벗의 ID·깊이 경로를 공유합니다."""

    if radius <= 0.0:
        return None
    normalized_ids = tuple(dict.fromkeys(str(model_id).strip() for model_id in model_ids if str(model_id).strip()))
    if not normalized_ids:
        return None

    needs_screen_ray = screen_surface and any(
        geometry is not None and geometry_kind_for_model(geometry[1]) == "TRIANGLE_MESH"
        for geometry in (visible_marker_geometry_for_id(model_id) for model_id in normalized_ids)
    )
    radius_squared = float(radius) * float(radius)
    try:
        ray_origin, ray_direction = (
            _screen_view_ray(region, region_3d, mouse_region)
            if needs_screen_ray else _view_ray(region, region_3d, mouse_region)
        )
    except (AttributeError, RuntimeError, TypeError, ValueError):
        if needs_screen_ray:
            raise ModelRepositoryError("마커 선택에 사용할 화면 광선을 계산할 수 없습니다.")
        ray_origin = ray_direction = None

    best_key: tuple[float, float] | None = None
    best_result: PickResult | None = None
    for model_id in normalized_ids:
        try:
            visible_geometry = visible_marker_geometry_for_id(model_id)
            if visible_geometry is None:
                continue
            geometry_source, model_data = visible_geometry
            is_mesh = geometry_kind_for_model(model_data) == "TRIANGLE_MESH"
            if is_mesh and ray_origin is not None and ray_direction is not None:
                if screen_surface:
                    result = _pick_model_mesh_surface_from_screen(
                        model_id, model_data, region, region_3d, mouse_region,
                        ray_origin, ray_direction, geometry_source,
                    )
                else:
                    result = _pick_model_mesh_surface_from_ray(
                        model_id, model_data, region, region_3d,
                        ray_origin, ray_direction, geometry_source,
                    )
            else:
                result = None
            if result is None and geometry_source == MARKER_GEOMETRY_SOURCE_MAIN:
                result = _pick_model_nearest_point(
                    model_id, region, region_3d, mouse_region,
                    radius_squared, ray_origin, ray_direction,
                )
        except (ModelRepositoryError, TypeError, ValueError, IndexError) as error:
            if screen_surface:
                raise ModelRepositoryError(str(error)) from error
            _LOGGER.error(
                "event=pick_projection_failed model_id=%s reason=%s",
                model_id,
                error,
            )
            continue
        if result is None:
            continue
        result_key = (
            (result.depth, result.screen_distance * result.screen_distance)
            if ray_origin is not None
            else (result.screen_distance * result.screen_distance, result.depth)
        )
        if best_key is None or result_key < best_key:
            best_key = result_key
            best_result = result
    return best_result


def pick_nearest_point(
    role: str,
    region: Any,
    region_3d: Any,
    mouse_region: tuple[float, float],
    radius: float = PICK_RADIUS_PX,
) -> PickResult | None:
    """역할별 마커 표면은 화면 ID·깊이로 찾고 점군은 기존 점 선택을 유지합니다."""

    return pick_nearest_point_for_model_ids(
        marker_pick_model_ids(role),
        region,
        region_3d,
        mouse_region,
        radius,
        screen_surface=True,
    )


__all__ = (
    "PickResult",
    "clear_mesh_surface_pick_cache",
    "discard_mesh_surface_pick_cache_for_model",
    "find_viewport_under_mouse",
    "marker_source_model_id",
    "marker_source_model_ids",
    "marker_pick_model_ids",
    "pick_nearest_point",
    "pick_nearest_point_for_model_ids",
)
