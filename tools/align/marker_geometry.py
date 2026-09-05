"""마커가 선택한 주 형상·연결 표면과 해당 축을 일관되게 조회합니다."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .constants import (
    GEOMETRY_KIND_TRIANGLE_MESH,
    MARKER_GEOMETRY_SOURCE_MAIN,
    MARKER_GEOMETRY_SOURCE_COMPANION,
    MARKER_GEOMETRY_SOURCES,
)
from .geometry_access import geometry_kind_for_model


if TYPE_CHECKING:
    from .display_axes import ModelDisplayTransform


@dataclass(frozen=True)
class MarkerGeometry:
    """소유 모델을 유지한 마커 형상 슬롯과 확정 축·원본 지문을 보관합니다."""

    model_id: str
    geometry_source: str
    model_data: Any
    display_transform: ModelDisplayTransform
    source_fingerprint: str


def normalize_marker_geometry_source(value: str) -> str:
    """알려진 마커 형상 슬롯만 정규화하여 반환합니다."""
    source = str(value).strip().upper()
    if source not in MARKER_GEOMETRY_SOURCES:
        raise ValueError("마커 형상 출처가 올바르지 않습니다.")
    return source


def marker_geometry_model_for_source(
    repository: Any,
    model_id: str,
    geometry_source: str = MARKER_GEOMETRY_SOURCE_MAIN,
) -> Any | None:
    """소유자가 존재할 때 명시한 슬롯의 실제 원본 형상만 조회합니다."""
    source = normalize_marker_geometry_source(geometry_source)
    owner = getattr(repository, "models", {}).get(model_id)
    if owner is None or source == MARKER_GEOMETRY_SOURCE_MAIN:
        return owner
    getter = getattr(repository, "companion_mesh_for_model_id", None)
    if callable(getter):
        model = getter(model_id)
    else:
        model = getattr(repository, "companion_meshes", {}).get(model_id)
    if model is not None and geometry_kind_for_model(model) != GEOMETRY_KIND_TRIANGLE_MESH:
        raise ValueError("연결 마커 형상은 삼각형 메시여야 합니다.")
    return model


def resolve_marker_geometry(
    repository: Any,
    model_id: str,
    geometry_source: str = MARKER_GEOMETRY_SOURCE_MAIN,
) -> MarkerGeometry:
    """Blender의 형상 조회 시에만 축 타입을 불러와 원본과 확정 축을 검증합니다."""
    from .display_axes import ModelDisplayTransform

    source = normalize_marker_geometry_source(geometry_source)
    model = marker_geometry_model_for_source(repository, model_id, source)
    if model is None:
        raise ValueError(f"마커가 참조한 원본 형상이 없습니다: {model_id} / {source}")
    companion = source == MARKER_GEOMETRY_SOURCE_COMPANION
    getter_name = "companion_display_transform_for_model_id" if companion else "display_transform_for_model_id"
    transforms_name = "companion_display_transforms" if companion else "display_transforms"
    resolver = getattr(repository, getter_name, None)
    try:
        transform = resolver(model_id) if callable(resolver) else getattr(repository, transforms_name, {}).get(model_id)
    except (KeyError, RuntimeError) as error:
        raise ValueError(f"마커 형상의 축 설정이 없습니다: {model_id} / {source}") from error
    if not isinstance(transform, ModelDisplayTransform):
        raise ValueError(f"마커 형상의 축 설정이 올바르지 않습니다: {model_id} / {source}")
    metadata_map = getattr(repository, "companion_sources" if companion else "source_metadata", {})
    metadata = metadata_map.get(model_id) if isinstance(metadata_map, Mapping) else None
    return MarkerGeometry(
        model_id=str(model_id),
        geometry_source=source,
        model_data=model,
        display_transform=transform,
        source_fingerprint=str(getattr(metadata, "source_fingerprint", "")),
    )
