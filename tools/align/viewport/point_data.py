"""실제 COLMAP 점 테이블과 테스트 fixture의 읽기 전용 조회 경계를 제공합니다."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import numpy as np

from ..alignment import effective_alignment_for_model
from ..axis_settings import effective_axis_transform
from ..colmap_io.types import ColmapModelData
from ..geometry_access import (
    bounds_for_model,
    display_point_indices_for_model,
    geometry_kind_for_model,
    registration_point_ids_for_model,
    registration_positions_for_model,
)
from ..constants import (
    GEOMETRY_KIND_COLMAP_POINT_CLOUD,
    GEOMETRY_KIND_TRIANGLE_MESH,
    MARKER_GEOMETRY_SOURCE_MAIN,
    MARKER_GEOMETRY_SOURCE_COMPANION,
)
from ..marker_geometry import marker_geometry_model_for_source, normalize_marker_geometry_source
from ..display_axes import ModelDisplayTransform, identity_display_transform
from ..model_repository import ModelRepositoryError
from ..state import RUNTIME


_FIXTURE_IDENTITY_TRANSFORM = identity_display_transform()


@dataclass(frozen=True)
class PointArrays:
    """원본 점 ID·좌표·색상과 표시 LOD 인덱스를 같은 기준으로 보관합니다."""

    point_ids: np.ndarray
    positions: np.ndarray
    colors: np.ndarray
    display_indices: np.ndarray
    geometry_kind: str = GEOMETRY_KIND_COLMAP_POINT_CLOUD


def runtime_models() -> Mapping[str, Any]:
    """Repository를 우선해 현재 렌더링 가능한 모델 사전을 반환합니다."""
    repository = getattr(RUNTIME, "model_repository", None)
    models = getattr(repository, "models", None)
    if isinstance(models, Mapping) and models:
        return models
    point_clouds = getattr(RUNTIME, "point_clouds", None)
    return point_clouds if isinstance(point_clouds, Mapping) else {}


def model_data_for_id(model_id: str) -> Any | None:
    """모델 ID에 해당하는 실제 모델 또는 명시적 테스트 fixture를 반환합니다."""
    return runtime_models().get(model_id)


def companion_mesh_data_for_id(model_id: str) -> Any | None:
    """소유 COLMAP ID에 연결된 표면을 저장소의 선택적 조회 경계로 반환합니다."""
    repository = getattr(RUNTIME, "model_repository", None)
    getter = getattr(repository, "companion_mesh_for_model_id", None)
    if callable(getter):
        return getter(model_id)
    meshes = getattr(repository, "companion_meshes", None)
    return meshes.get(model_id) if isinstance(meshes, Mapping) else None



def marker_geometry_data_for_id(
    model_id: str,
    geometry_source: str = MARKER_GEOMETRY_SOURCE_MAIN,
) -> Any | None:
    """마커 슬롯의 실제 원본을 조회하고 주 형상의 명시적 테스트 자료를 지원합니다."""
    source = normalize_marker_geometry_source(geometry_source)
    repository = getattr(RUNTIME, "model_repository", None)
    model = marker_geometry_model_for_source(repository, model_id, source)
    if model is None and source == MARKER_GEOMETRY_SOURCE_MAIN:
        return model_data_for_id(model_id)
    return model


def marker_geometry_is_visible(
    model_id: str,
    geometry_source: str = MARKER_GEOMETRY_SOURCE_MAIN,
) -> bool:
    """마커가 속한 형상 슬롯의 존재와 실제 표시 토글을 함께 확인합니다."""
    model = marker_geometry_data_for_id(model_id, geometry_source)
    return model is not None and model_geometry_is_visible(model_id, geometry_kind_for_model(model))


def visible_marker_geometry_for_id(model_id: str) -> tuple[str, Any] | None:
    """레이어에서 선택되어 보이는 하나의 마커 형상 슬롯과 원본을 반환합니다."""
    for source in (MARKER_GEOMETRY_SOURCE_MAIN, MARKER_GEOMETRY_SOURCE_COMPANION):
        model = marker_geometry_data_for_id(model_id, source)
        if model is not None and model_geometry_is_visible(model_id, geometry_kind_for_model(model)):
            return source, model
    return None


def surface_model_data_for_id(model_id: str) -> Any | None:
    """독립 OBJ 또는 COLMAP 소유 연결 표면을 같은 소유 ID로 조회합니다."""
    model = model_data_for_id(model_id)
    if model is None:
        return None
    if geometry_kind_for_model(model) == GEOMETRY_KIND_TRIANGLE_MESH:
        return model
    return companion_mesh_data_for_id(model_id)


def model_layer_is_visible(model_id: str, catalog=None) -> bool:
    """축 팝업의 대상만 임시로 표시하고 확정된 레이어 눈 상태는 그대로 유지합니다."""
    state = getattr(RUNTIME, "axis_settings", None)
    if state is not None and state.is_active and state.model_id == model_id:
        return True
    catalog = catalog or getattr(RUNTIME, "model_catalog", None)
    record = getattr(catalog, "models", {}).get(model_id)
    return record is None or bool(getattr(record, "is_visible", True))


def model_geometry_is_visible(model_id: str, geometry_kind: str) -> bool:
    """축 팝업에서는 존재하는 두 형상을 표시하고 평소에는 기존 표시 토글을 따릅니다."""
    state = getattr(RUNTIME, "axis_settings", None)
    if state is not None and state.is_active and state.model_id == model_id:
        if geometry_kind == GEOMETRY_KIND_TRIANGLE_MESH:
            return surface_model_data_for_id(model_id) is not None
        model = model_data_for_id(model_id)
        return model is not None and geometry_kind_for_model(model) == GEOMETRY_KIND_COLMAP_POINT_CLOUD
    if not model_layer_is_visible(model_id):
        return False
    catalog = getattr(RUNTIME, "model_catalog", None)
    record = getattr(catalog, "models", {}).get(model_id)
    field = "show_surface" if geometry_kind == GEOMETRY_KIND_TRIANGLE_MESH else "show_points"
    return bool(getattr(record, field, True))


def display_transform_for_model_id(
    model_id: str,
    *,
    companion: bool = False,
) -> ModelDisplayTransform:
    """주 형상 또는 연결 표면의 확정·임시 미리보기 축 변환을 반환합니다."""
    repository = getattr(RUNTIME, "model_repository", None)
    getter_name = (
        "companion_display_transform_for_model_id"
        if companion
        else "display_transform_for_model_id"
    )
    transforms = getattr(
        repository,
        "companion_display_transforms" if companion else "display_transforms",
        None,
    )
    transform = transforms.get(model_id) if isinstance(transforms, Mapping) else None
    if isinstance(transform, ModelDisplayTransform):
        if callable(getattr(repository, getter_name, None)):
            models = getattr(repository, "models", None)
            if isinstance(models, Mapping) and model_id in models:
                return effective_axis_transform(RUNTIME, model_id, companion=companion)
        return transform
    if transform is not None:
        raise ModelRepositoryError(
            f"COLMAP 모델의 표시 축 변환 형식이 올바르지 않습니다: {model_id}"
        )
    model_data = model_data_for_id(model_id)
    if isinstance(model_data, ColmapModelData):
        raise ModelRepositoryError(
            f"COLMAP 모델의 표시 축 변환이 누락되었습니다: {model_id}"
        )
    # 실제 가져온 ColmapModelData가 아닌 명시적 단위 테스트 fixture만 Identity를 사용합니다.
    return _FIXTURE_IDENTITY_TRANSFORM


def effective_model_matrix_for_id(
    model_id: str,
    *,
    companion: bool = False,
) -> np.ndarray:
    """형상별 축 뒤에 소유 모델의 공통 유효 정렬을 적용한 행렬을 반환합니다."""
    display_transform = display_transform_for_model_id(model_id, companion=companion)
    alignment = effective_alignment_for_model(RUNTIME, model_id)
    alignment_matrix = np.asarray(alignment.matrix_float64, dtype=np.float64)
    matrix = alignment_matrix @ np.asarray(display_transform.matrix_float64, dtype=np.float64)
    if matrix.shape != (4, 4) or not np.isfinite(matrix).all():
        raise ModelRepositoryError(
            f"COLMAP 모델의 유효 표시 행렬 형식이 올바르지 않습니다: {model_id}"
        )
    return np.ascontiguousarray(matrix, dtype=np.float64)


def effective_positions_for_model_id(
    model_id: str,
    source_positions: Any,
    *,
    companion: bool = False,
) -> np.ndarray:
    """선택한 형상의 원본 점 배열에 해당 축과 소유 모델의 공통 정렬을 적용합니다."""
    positions = _normalize_positions(source_positions)
    matrix = effective_model_matrix_for_id(model_id, companion=companion)
    homogeneous = np.ones((positions.shape[0], 4), dtype=np.float64)
    homogeneous[:, :3] = positions
    transformed = homogeneous @ matrix.T
    weights = transformed[:, 3]
    if (
        not np.isfinite(transformed).all()
        or np.any(np.abs(weights) <= np.finfo(np.float64).eps)
    ):
        raise ModelRepositoryError(
            f"COLMAP 모델의 유효 표시 좌표를 계산할 수 없습니다: {model_id}"
        )
    return np.ascontiguousarray(
        transformed[:, :3] / weights[:, np.newaxis],
        dtype=np.float64,
    )


def effective_position_for_model_id(
    model_id: str,
    source_position: Any,
    *,
    companion: bool = False,
) -> np.ndarray:
    """선택한 형상의 원본 점 하나를 현재 프로젝트 좌표로 변환합니다."""
    position = np.asarray(source_position, dtype=np.float64)
    if position.shape != (3,) or not np.isfinite(position).all():
        raise ValueError("점 좌표는 유한한 길이 3 배열이어야 합니다.")
    return effective_positions_for_model_id(
        model_id, position[np.newaxis, :], companion=companion,
    )[0]


def effective_bounds_for_model_id(
    model_id: str,
    source_bounds_min: Any,
    source_bounds_max: Any,
    *,
    companion: bool = False,
) -> tuple[np.ndarray, np.ndarray]:
    """선택한 형상의 원본 경계를 형상별 축·공통 정렬을 거친 프로젝트 경계로 바꿉니다."""
    minimum = np.asarray(source_bounds_min, dtype=np.float64)
    maximum = np.asarray(source_bounds_max, dtype=np.float64)
    if (
        minimum.shape != (3,)
        or maximum.shape != (3,)
        or not np.isfinite(minimum).all()
        or not np.isfinite(maximum).all()
        or np.any(minimum > maximum)
    ):
        raise ValueError("모델 경계는 유한한 최소·최대 길이 3 좌표여야 합니다.")
    corners = np.asarray(
        [
            (x, y, z)
            for x in (minimum[0], maximum[0])
            for y in (minimum[1], maximum[1])
            for z in (minimum[2], maximum[2])
        ],
        dtype=np.float64,
    )
    transformed_corners = effective_positions_for_model_id(
        model_id, corners, companion=companion,
    )
    return (
        np.ascontiguousarray(transformed_corners.min(axis=0), dtype=np.float64),
        np.ascontiguousarray(transformed_corners.max(axis=0), dtype=np.float64),
    )


def point_table_for_model(model_data: Any) -> Any:
    """모델 데이터의 points 테이블 또는 레거시 점군 객체를 반환합니다."""
    points = getattr(model_data, "points", None)
    return points if points is not None else model_data


def _normalize_positions(value: Any) -> np.ndarray:
    """점 좌표를 Nx3 float64 배열로 검증해 반환합니다."""
    positions = np.asarray(value, dtype=np.float64)
    if positions.ndim != 2 or positions.shape[1] != 3:
        raise ValueError("점 좌표는 Nx3 배열이어야 합니다.")
    if not np.isfinite(positions).all():
        raise ValueError("점 좌표에 유한하지 않은 값이 있습니다.")
    return positions


def _normalize_point_ids(value: Any, count: int) -> np.ndarray:
    """원본 POINT3D_ID 배열의 길이와 정수 형식을 검증해 반환합니다."""
    point_ids = np.asarray(value)
    if point_ids.ndim != 1 or point_ids.shape[0] != count:
        raise ValueError("점 ID 배열 길이가 좌표 개수와 일치하지 않습니다.")
    if not np.issubdtype(point_ids.dtype, np.integer):
        raise ValueError("점 ID 배열은 정수 형식이어야 합니다.")
    return point_ids


def _normalize_colors(value: Any, count: int) -> np.ndarray:
    """원본 RGB를 Nx3 uint8 배열로 검증해 반환합니다."""
    colors = np.asarray(value)
    if colors.ndim != 2 or colors.shape != (count, 3):
        raise ValueError("점 색상은 좌표 개수와 같은 Nx3 배열이어야 합니다.")
    if colors.dtype != np.uint8:
        raise ValueError("점 색상은 uint8 RGB 배열이어야 합니다.")
    return colors


def _normalize_display_indices(value: Any, count: int) -> np.ndarray:
    """LOD 원본 배열 인덱스의 범위와 정수 형식을 검증해 반환합니다."""
    display_indices = np.asarray(value)
    if display_indices.ndim != 1:
        raise ValueError("표시 LOD 인덱스는 1차원 배열이어야 합니다.")
    if not np.issubdtype(display_indices.dtype, np.integer):
        raise ValueError("표시 LOD 인덱스는 정수 형식이어야 합니다.")
    if display_indices.size and (
        np.any(display_indices < 0) or np.any(display_indices >= count)
    ):
        raise ValueError("표시 LOD 인덱스가 원본 점 범위를 벗어났습니다.")
    return display_indices


def point_arrays(model_data: Any) -> PointArrays:
    """COLMAP 등록점 또는 메시 원본 정점 overlay를 같은 점 배열 계약으로 만듭니다."""
    if geometry_kind_for_model(model_data) == "TRIANGLE_MESH":
        positions = np.asarray(getattr(model_data, "vertices_float64", getattr(model_data, "vertices")), dtype=np.float64)
        count = int(positions.shape[0])
        point_ids = np.arange(count, dtype=np.uint64)
        colors = np.full((count, 3), 255, dtype=np.uint8)
        display_indices = display_point_indices_for_model(model_data).astype(np.uint64, copy=False)
        return PointArrays(
            point_ids=np.ascontiguousarray(point_ids),
            positions=np.ascontiguousarray(positions),
            colors=colors,
            display_indices=np.ascontiguousarray(display_indices),
            geometry_kind=GEOMETRY_KIND_TRIANGLE_MESH,
        )
    table = point_table_for_model(model_data)
    positions = _normalize_positions(getattr(table, "positions"))
    count = int(positions.shape[0])
    point_ids = _normalize_point_ids(getattr(table, "point_ids"), count)

    colors_value = getattr(table, "colors", None)
    if colors_value is None:
        # 기존 Demo fixture는 RGB가 없으므로 흰색을 사용해 렌더 경계를 유지합니다.
        colors = np.full((count, 3), 255, dtype=np.uint8)
    else:
        colors = _normalize_colors(colors_value, count)

    display_value = getattr(table, "display_indices", None)
    if display_value is None:
        index_dtype = np.uint32 if count <= np.iinfo(np.uint32).max else np.uint64
        display_indices = np.arange(count, dtype=index_dtype)
    else:
        display_indices = _normalize_display_indices(display_value, count)
    return PointArrays(
        point_ids=point_ids,
        positions=positions,
        colors=colors,
        display_indices=display_indices,
    )
