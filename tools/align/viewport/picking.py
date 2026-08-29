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
    MARKER_POINT_KIND_MESH_SURFACE,
    MARKER_POINT_KIND_MESH_VERTEX,
    PICK_RADIUS_PX,
    ROLE_MOVING,
    ROLE_REFERENCE,
)
from ..geometry_access import geometry_kind_for_model
from ..display_axes import ModelDisplayTransform, display_positions
from ..logging_utils import get_logger
from ..model_repository import ModelRepositoryError
from ..state import RUNTIME
from .point_data import (
    effective_positions_for_model_id,
    effective_model_matrix_for_id,
    model_data_for_id,
    point_arrays,
    runtime_models,
)
from .registry import find_window_region, role_for_area


_LOGGER = get_logger("viewport.picking")
_MESH_SURFACE_BVH_CACHE: dict[tuple[str, int, int, int, int], BVHTree] = {}


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


def clear_mesh_surface_pick_cache() -> None:
    """세션 종료·파일 교체 시 메시 면 피킹 BVH 참조를 모두 해제합니다."""

    _MESH_SURFACE_BVH_CACHE.clear()


def discard_mesh_surface_pick_cache_for_model(model_id: str) -> None:
    """지정 모델의 이전 geometry revision BVH를 피킹 캐시에서 제거합니다."""

    normalized_id = str(model_id)
    stale_keys = tuple(key for key in _MESH_SURFACE_BVH_CACHE if key[0] == normalized_id)
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


def _ray_in_source_local(
    model_id: str,
    ray_origin: np.ndarray,
    ray_direction: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    """Project World 피킹 광선을 모델의 Source Local 광선으로 변환합니다."""

    model_matrix = effective_model_matrix_for_id(model_id)
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


def _mesh_surface_bvh(model_id: str, model_data: Any) -> BVHTree:
    """모델 identity와 geometry revision이 같은 삼각면 BVH를 재사용합니다."""

    vertices = np.asarray(
        getattr(model_data, "vertices_float64", getattr(model_data, "vertices")),
        dtype=np.float64,
    )
    triangles = np.asarray(
        getattr(model_data, "triangles_uint32", getattr(model_data, "triangles")),
        dtype=np.uint32,
    )
    cache_key = (
        str(model_id),
        id(model_data),
        int(getattr(model_data, "geometry_revision", 0)),
        int(vertices.shape[0]),
        int(triangles.shape[0]),
    )
    cached = _MESH_SURFACE_BVH_CACHE.get(cache_key)
    if cached is not None:
        return cached
    discard_mesh_surface_pick_cache_for_model(model_id)
    tree = BVHTree.FromPolygons(
        vertices,
        triangles,
        all_triangles=True,
        epsilon=0.0,
    )
    _MESH_SURFACE_BVH_CACHE[cache_key] = tree
    return tree


def _ray_triangle_position_float64(
    ray_origin: np.ndarray,
    ray_direction: np.ndarray,
    triangle: np.ndarray,
) -> np.ndarray | None:
    """BVH가 찾은 삼각면에 광선을 float64로 다시 교차해 저장 좌표를 계산합니다."""

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
) -> PickResult | None:
    """메시의 가장 앞쪽 삼각면 교차점을 Source Local 면 마커로 반환합니다."""

    local_origin, local_direction = _ray_in_source_local(
        model_id,
        ray_origin,
        ray_direction,
    )
    minimum = np.asarray(getattr(model_data, "bounds_min"), dtype=np.float64)
    maximum = np.asarray(getattr(model_data, "bounds_max"), dtype=np.float64)
    if not _ray_intersects_bounds(local_origin, local_direction, minimum, maximum):
        return None
    location, _normal, face_index, _distance = _mesh_surface_bvh(
        model_id,
        model_data,
    ).ray_cast(Vector(local_origin.tolist()), Vector(local_direction.tolist()))
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
    triangle = vertices[triangles[int(face_index)].astype(np.int64, copy=False)]
    local_position = _ray_triangle_position_float64(
        local_origin,
        local_direction,
        triangle,
    )
    if local_position is None:
        local_position = np.asarray(location, dtype=np.float64)
    project_position = effective_positions_for_model_id(
        model_id,
        local_position[np.newaxis, :],
    )[0]
    ray_depth = float(np.dot(project_position - ray_origin, ray_direction))
    if not np.isfinite(project_position).all() or not math.isfinite(ray_depth) or ray_depth < 0.0:
        return None
    position = tuple(float(value) for value in local_position)
    return PickResult(
        model_id=model_id,
        point3d_id=int(face_index),
        position=position,
        screen_distance=0.0,
        depth=ray_depth,
        point_kind=MARKER_POINT_KIND_MESH_SURFACE,
        source_point_id=int(face_index),
    )


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
) -> PickResult | None:
    """한 모델의 표시 LOD를 벡터화 투영해 가장 가까운 원본 점을 반환합니다."""
    model_data = model_data_for_id(model_id)
    if model_data is None:
        return None
    arrays = point_arrays(model_data)
    display_indices = arrays.display_indices
    if display_indices.size == 0:
        return None

    source_display_positions = arrays.positions[display_indices]
    shown_positions = effective_positions_for_model_id(
        model_id,
        source_display_positions,
    )
    screen_positions, depths, valid = _project_positions(
        region,
        region_3d,
        shown_positions,
    )
    mouse_x, mouse_y = mouse_region
    distance_squared = (
        (screen_positions[:, 0] - float(mouse_x)) ** 2
        + (screen_positions[:, 1] - float(mouse_y)) ** 2
    )
    candidate_offsets = np.flatnonzero(valid & (distance_squared <= radius_squared))
    if candidate_offsets.size == 0:
        return None

    # 화면 거리 우선, 동일 거리에서는 앞쪽 깊이 우선으로 결정적으로 한 점을 고릅니다.
    candidate_order = np.lexsort(
        (depths[candidate_offsets], distance_squared[candidate_offsets])
    )
    display_offset = int(candidate_offsets[int(candidate_order[0])])
    source_index = int(display_indices[display_offset])
    position = arrays.positions[source_index]
    point_kind = (
        MARKER_POINT_KIND_MESH_VERTEX
        if geometry_kind_for_model(model_data) == "TRIANGLE_MESH"
        else MARKER_POINT_KIND_COLMAP
    )
    source_point_id = int(arrays.point_ids[source_index])
    return PickResult(
        model_id=model_id,
        point3d_id=int(arrays.point_ids[source_index]),
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
) -> PickResult | None:
    """지정 모델 LOD를 화면 거리와 깊이 순으로 비교해 가장 가까운 점을 반환합니다."""

    if radius <= 0.0:
        return None
    normalized_ids = tuple(dict.fromkeys(str(model_id).strip() for model_id in model_ids if str(model_id).strip()))
    if not normalized_ids:
        return None

    radius_squared = float(radius) * float(radius)
    try:
        ray_origin, ray_direction = _view_ray(region, region_3d, mouse_region)
    except (AttributeError, RuntimeError, TypeError, ValueError):
        ray_origin = ray_direction = None

    best_key: tuple[float, float] | None = None
    best_result: PickResult | None = None
    for model_id in normalized_ids:
        try:
            model_data = model_data_for_id(model_id)
            if (
                model_data is not None
                and geometry_kind_for_model(model_data) == "TRIANGLE_MESH"
                and ray_origin is not None
                and ray_direction is not None
            ):
                result = _pick_model_mesh_surface_from_ray(
                    model_id,
                    model_data,
                    region,
                    region_3d,
                    ray_origin,
                    ray_direction,
                )
            else:
                result = None
            if result is None:
                result = _pick_model_nearest_point(
                    model_id,
                    region,
                    region_3d,
                    mouse_region,
                    radius_squared,
                )
        except (ModelRepositoryError, TypeError, ValueError, IndexError) as error:
            _LOGGER.error(
                "event=pick_projection_failed model_id=%s reason=%s",
                model_id,
                error,
            )
            continue
        if result is None:
            continue
        result_key = (result.screen_distance * result.screen_distance, result.depth)
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
    """역할의 모든 허용 모델 LOD를 화면 거리와 깊이 순으로 함께 비교합니다."""

    return pick_nearest_point_for_model_ids(
        marker_source_model_ids(role),
        region,
        region_3d,
        mouse_region,
        radius,
    )


__all__ = (
    "PickResult",
    "clear_mesh_surface_pick_cache",
    "discard_mesh_surface_pick_cache_for_model",
    "find_viewport_under_mouse",
    "marker_source_model_id",
    "marker_source_model_ids",
    "pick_nearest_point",
    "pick_nearest_point_for_model_ids",
)
