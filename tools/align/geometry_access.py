"""COLMAP 점군과 삼각형 메시의 등록점·ID·경계 접근을 한 곳에서 제공합니다."""

from __future__ import annotations

from typing import Any

import numpy as np

from .colmap_io.types import ColmapModelData
from .constants import (
    GEOMETRY_KIND_COLMAP_POINT_CLOUD,
    GEOMETRY_KIND_TRIANGLE_MESH,
    MARKER_POINT_KIND_COLMAP,
    MARKER_POINT_KIND_MESH_VERTEX,
)
from .mesh_io.types import TriangleMeshModelData


def geometry_kind_for_model(model: Any) -> str:
    """모델 인스턴스 또는 명시적 속성에서 두 형상 종류 중 하나를 반환합니다."""
    if isinstance(model, TriangleMeshModelData):
        return GEOMETRY_KIND_TRIANGLE_MESH
    explicit = str(getattr(model, "geometry_kind", "")).strip().upper()
    if explicit in {GEOMETRY_KIND_COLMAP_POINT_CLOUD, GEOMETRY_KIND_TRIANGLE_MESH}:
        return explicit
    if (
        isinstance(model, ColmapModelData)
        or getattr(model, "points", None) is not None
        or (
            getattr(model, "positions", None) is not None
            and getattr(model, "point_ids", None) is not None
        )
    ):
        return GEOMETRY_KIND_COLMAP_POINT_CLOUD
    raise TypeError("지원하지 않는 형상 모델입니다.")


def is_triangle_mesh_model(model: Any) -> bool:
    """모델이 삼각형 메시인지 반환합니다."""
    return geometry_kind_for_model(model) == GEOMETRY_KIND_TRIANGLE_MESH


def registration_positions_for_model(model: Any) -> np.ndarray:
    """정렬에 사용할 원본 등록점 좌표를 연속 float64 배열로 반환합니다."""
    kind = geometry_kind_for_model(model)
    if kind == GEOMETRY_KIND_TRIANGLE_MESH:
        vertices = np.asarray(getattr(model, "vertices_float64", getattr(model, "vertices", None)), dtype=np.float64)
        indices = np.asarray(getattr(model, "registration_vertex_indices"), dtype=np.int64)
        if vertices.ndim != 2 or vertices.shape[1] != 3 or indices.ndim != 1 or np.any(indices < 0) or np.any(indices >= vertices.shape[0]):
            raise ValueError("메시 등록 정점 배열 형식이 올바르지 않습니다.")
        positions = np.ascontiguousarray(vertices[indices], dtype=np.float64)
    else:
        points = getattr(model, "points", None)
        positions = np.asarray(getattr(points, "positions", None), dtype=np.float64)
        if positions.ndim != 2 or positions.shape[1] != 3:
            raise ValueError("COLMAP 등록점 좌표 배열 형식이 올바르지 않습니다.")
    if not np.isfinite(positions).all():
        raise ValueError("등록점 좌표에 유한하지 않은 값이 있습니다.")
    positions.setflags(write=False)
    return positions


def registration_point_ids_for_model(model: Any) -> np.ndarray:
    """등록점 좌표와 같은 순서의 안정 uint64 ID 배열을 반환합니다."""
    kind = geometry_kind_for_model(model)
    if kind == GEOMETRY_KIND_TRIANGLE_MESH:
        ids = np.asarray(getattr(model, "registration_vertex_indices"), dtype=np.uint64)
    else:
        ids = np.asarray(getattr(getattr(model, "points", None), "point_ids", None), dtype=np.uint64)
    if ids.ndim != 1 or ids.shape[0] != registration_positions_for_model(model).shape[0]:
        raise ValueError("등록점 ID 배열 길이가 좌표 배열과 다릅니다.")
    ids = np.ascontiguousarray(ids, dtype=np.uint64)
    ids.setflags(write=False)
    return ids


def display_point_indices_for_model(model: Any) -> np.ndarray:
    """뷰포트 오버레이에 사용할 원본 정점·점 테이블 인덱스를 반환합니다."""
    kind = geometry_kind_for_model(model)
    if kind == GEOMETRY_KIND_TRIANGLE_MESH:
        source = getattr(model, "display_vertex_indices", None)
        if source is None:
            source = getattr(model, "registration_vertex_indices")
        count = np.asarray(getattr(model, "vertices_float64", getattr(model, "vertices"))).shape[0]
    else:
        points = getattr(model, "points")
        source = getattr(points, "display_indices", None)
        count = np.asarray(getattr(points, "positions")).shape[0]
        if source is None:
            source = np.arange(count, dtype=np.uint32)
    indices = np.asarray(source)
    if indices.ndim != 1 or not np.issubdtype(indices.dtype, np.integer) or np.any(indices < 0) or np.any(indices >= count):
        raise ValueError("표시 인덱스가 원본 형상 범위를 벗어났습니다.")
    indices = np.ascontiguousarray(indices, dtype=np.uint64)
    indices.setflags(write=False)
    return indices


def source_position_for_registration_id(
    model: Any,
    point_kind: str,
    point_id: int,
) -> np.ndarray:
    """COLMAP POINT3D_ID 또는 OBJ 정점 ID를 Source Local 좌표로 조회합니다."""
    kind = str(point_kind).strip().upper()
    normalized_id = int(point_id)
    if normalized_id < 0:
        raise ValueError("등록점 ID는 음수일 수 없습니다.")
    geometry_kind = geometry_kind_for_model(model)
    if kind == MARKER_POINT_KIND_MESH_VERTEX:
        if geometry_kind != GEOMETRY_KIND_TRIANGLE_MESH:
            raise ValueError("메시 정점 ID는 삼각형 메시에서만 사용할 수 있습니다.")
        vertices = np.asarray(getattr(model, "vertices_float64", getattr(model, "vertices")), dtype=np.float64)
        if normalized_id >= vertices.shape[0]:
            raise KeyError(f"OBJ 원본 정점 ID를 찾을 수 없습니다: {normalized_id}")
        return np.ascontiguousarray(vertices[normalized_id], dtype=np.float64)
    if kind != MARKER_POINT_KIND_COLMAP:
        raise ValueError(f"지원하지 않는 등록점 종류입니다: {point_kind}")
    if geometry_kind != GEOMETRY_KIND_COLMAP_POINT_CLOUD:
        raise ValueError("COLMAP POINT3D_ID는 삼각형 메시에서 사용할 수 없습니다.")
    points = getattr(model, "points", None)
    ids = np.asarray(getattr(points, "point_ids", None), dtype=np.uint64)
    positions = np.asarray(getattr(points, "positions", None), dtype=np.float64)
    matches = np.flatnonzero(ids == np.uint64(normalized_id))
    if matches.size != 1:
        raise KeyError(f"COLMAP POINT3D_ID를 찾을 수 없습니다: {normalized_id}")
    return np.ascontiguousarray(positions[int(matches[0])], dtype=np.float64)


def validated_mesh_surface_position(
    model: Any,
    face_id: int,
    position: object,
) -> np.ndarray:
    """메시 삼각면 ID와 Source Local 좌표의 평면·내부 일치를 검증합니다."""

    if geometry_kind_for_model(model) != GEOMETRY_KIND_TRIANGLE_MESH:
        raise ValueError("메시 면 마커는 삼각형 메시에서만 사용할 수 있습니다.")
    normalized_face_id = int(face_id)
    triangles = np.asarray(
        getattr(model, "triangles_uint32", getattr(model, "triangles")),
        dtype=np.uint32,
    )
    vertices = np.asarray(
        getattr(model, "vertices_float64", getattr(model, "vertices")),
        dtype=np.float64,
    )
    if normalized_face_id < 0 or normalized_face_id >= triangles.shape[0]:
        raise KeyError(f"OBJ 삼각면 ID를 찾을 수 없습니다: {normalized_face_id}")
    surface_position = np.asarray(position, dtype=np.float64)
    if surface_position.shape != (3,) or not np.isfinite(surface_position).all():
        raise ValueError("면 마커 Source Local 좌표가 올바르지 않습니다.")

    triangle = vertices[triangles[normalized_face_id].astype(np.int64, copy=False)]
    edge_first = triangle[1] - triangle[0]
    edge_second = triangle[2] - triangle[0]
    normal = np.cross(edge_first, edge_second)
    normal_length = float(np.linalg.norm(normal))
    maximum_edge = max(
        float(np.linalg.norm(edge_first)),
        float(np.linalg.norm(edge_second)),
        float(np.linalg.norm(triangle[2] - triangle[1])),
    )
    if normal_length <= np.finfo(np.float64).eps * max(maximum_edge * maximum_edge, 1.0):
        raise ValueError("면 마커가 참조한 삼각면이 퇴화되었습니다.")

    offset = surface_position - triangle[0]
    plane_distance = abs(float(np.dot(offset, normal))) / normal_length
    tolerance = max(maximum_edge, 1.0) * 1.0e-9
    if plane_distance > tolerance:
        raise ValueError("면 마커 좌표가 저장된 삼각면 평면에 있지 않습니다.")

    dot00 = float(np.dot(edge_first, edge_first))
    dot01 = float(np.dot(edge_first, edge_second))
    dot11 = float(np.dot(edge_second, edge_second))
    dot20 = float(np.dot(offset, edge_first))
    dot21 = float(np.dot(offset, edge_second))
    denominator = dot00 * dot11 - dot01 * dot01
    if abs(denominator) <= np.finfo(np.float64).eps * max(dot00 * dot11, 1.0):
        raise ValueError("면 마커가 참조한 삼각면이 퇴화되었습니다.")
    first_weight = (dot11 * dot20 - dot01 * dot21) / denominator
    second_weight = (dot00 * dot21 - dot01 * dot20) / denominator
    barycentric_tolerance = 1.0e-9
    if (
        first_weight < -barycentric_tolerance
        or second_weight < -barycentric_tolerance
        or first_weight + second_weight > 1.0 + barycentric_tolerance
    ):
        raise ValueError("면 마커 좌표가 저장된 삼각면 밖에 있습니다.")
    return np.ascontiguousarray(surface_position, dtype=np.float64)


def bounds_for_model(model: Any) -> tuple[np.ndarray, np.ndarray]:
    """COLMAP 또는 메시 Source Local bounds를 읽기 전용 float64 쌍으로 반환합니다."""
    minimum = np.asarray(getattr(model, "bounds_min"), dtype=np.float64)
    maximum = np.asarray(getattr(model, "bounds_max"), dtype=np.float64)
    if minimum.shape != (3,) or maximum.shape != (3,) or not np.isfinite(minimum).all() or not np.isfinite(maximum).all() or np.any(minimum > maximum):
        raise ValueError("모델 bounds가 유한한 길이 3 좌표가 아닙니다.")
    return np.ascontiguousarray(minimum, dtype=np.float64), np.ascontiguousarray(maximum, dtype=np.float64)


def point_kind_for_model(model: Any) -> str:
    """모델의 등록점이 COLMAP 점인지 메시 원본 정점인지 반환합니다."""
    return MARKER_POINT_KIND_MESH_VERTEX if is_triangle_mesh_model(model) else MARKER_POINT_KIND_COLMAP


def source_position_for_model_id(model: Any, point_id: int) -> np.ndarray:
    """기존 COLMAP 전용 호출과 호환되는 모델 종류 자동 좌표 조회를 제공합니다."""
    return source_position_for_registration_id(model, point_kind_for_model(model), point_id)


# 계획서의 짧은 API 이름을 그대로 노출합니다.
registration_positions = registration_positions_for_model
registration_point_ids = registration_point_ids_for_model
display_point_indices = display_point_indices_for_model
source_position_for_registration = source_position_for_registration_id
bounds_for = bounds_for_model


__all__ = (
    "bounds_for",
    "bounds_for_model",
    "display_point_indices",
    "display_point_indices_for_model",
    "geometry_kind_for_model",
    "is_triangle_mesh_model",
    "point_kind_for_model",
    "registration_point_ids",
    "registration_point_ids_for_model",
    "registration_positions",
    "registration_positions_for_model",
    "source_position_for_model_id",
    "source_position_for_registration",
    "source_position_for_registration_id",
    "validated_mesh_surface_position",
)
