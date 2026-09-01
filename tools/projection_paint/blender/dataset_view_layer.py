"""Projection Paint 전용 View Layer와 독립 Snapshot 가시성을 관리합니다."""

from __future__ import annotations

from typing import Any

try:
    from ....shared.colmap_dataset.blender.view_layers import (
        is_dataset_view_layer,
        layer_collection_for_collection,
        mark_dataset_view_layer,
    )
except ImportError:
    from shared.colmap_dataset.blender.view_layers import (
        is_dataset_view_layer,
        layer_collection_for_collection,
        mark_dataset_view_layer,
    )

from .ownership import is_owned, tag_owned


VIEW_LAYER_NAME = "COLMAP Projection Paint"
VIEW_LAYER_KIND = "PAINT_DATA_VIEW_LAYER"
VIEW_LAYER_TOOL_ID = "projection_paint"


def _pointer(value: Any) -> int:
    """유효한 Blender RNA 객체의 포인터를 반환하고 실패하면 0을 반환합니다."""

    try:
        return int(value.as_pointer()) if value is not None else 0
    except (AttributeError, ReferenceError, RuntimeError, TypeError):
        return 0


def is_projection_dataset_view_layer(view_layer: Any) -> bool:
    """View Layer가 PP 독립 Snapshot 표시 권한을 갖는지 확인합니다."""

    return bool(
        is_dataset_view_layer(view_layer, VIEW_LAYER_TOOL_ID)
        and is_owned(view_layer, kind=VIEW_LAYER_KIND)
    )


def ensure_projection_dataset_view_layer(scene: Any) -> Any:
    """Scene에 중복 없는 Projection Paint 전용 View Layer를 생성하거나 반환합니다."""

    matches = tuple(
        view_layer
        for view_layer in getattr(scene, "view_layers", ())
        if is_projection_dataset_view_layer(view_layer)
    )
    if len(matches) > 1:
        raise RuntimeError("Projection Paint 전용 View Layer가 중복되었습니다.")
    if matches:
        view_layer = matches[0]
        mark_dataset_view_layer(view_layer, VIEW_LAYER_TOOL_ID)
    else:
        view_layer = scene.view_layers.new(VIEW_LAYER_NAME)
        tag_owned(view_layer, VIEW_LAYER_KIND)
        mark_dataset_view_layer(view_layer, VIEW_LAYER_TOOL_ID)
    settings = getattr(scene, "projection_paint", None)
    if settings is not None:
        settings.dataset_view_layer_name = str(view_layer.name)
    return view_layer


def _snapshot_collections(scene: Any) -> tuple[Any, ...]:
    """Scene Root에 연결된 PP 독립 Camera Collection을 반환합니다."""

    return tuple(
        collection
        for collection in tuple(getattr(getattr(scene, "collection", None), "children", ()))
        if is_owned(collection, kind="CAMERA_COLLECTION")
    )


def synchronize_projection_dataset_visibility(scene: Any) -> Any:
    """PP Snapshot Collection을 PP 전용 Layer에만 표시합니다."""

    view_layer = ensure_projection_dataset_view_layer(scene)
    snapshot_collections = _snapshot_collections(scene)
    for candidate in tuple(getattr(scene, "view_layers", ())):
        is_pp_layer = _pointer(candidate) == _pointer(view_layer)
        for collection in snapshot_collections:
            layer_collection = layer_collection_for_collection(
                candidate.layer_collection,
                collection,
            )
            if layer_collection is None:
                continue
            layer_collection.exclude = not is_pp_layer
        candidate.update()
    return view_layer


def activate_projection_dataset_view_layer(scene: Any, window: Any) -> Any:
    """Source View Layer를 보존하고 Window를 PP 전용 Dataset View Layer에 연결합니다."""

    settings = getattr(scene, "projection_paint", None)
    current = getattr(window, "view_layer", None)
    if settings is not None and not is_projection_dataset_view_layer(current):
        settings.source_view_layer_name = str(getattr(current, "name", "") or "")
    view_layer = synchronize_projection_dataset_visibility(scene)
    window.view_layer = view_layer
    if _pointer(getattr(window, "view_layer", None)) != _pointer(view_layer):
        raise RuntimeError("Projection Paint 전용 View Layer로 전환하지 못했습니다.")
    return view_layer


def restore_projection_source_view_layer(scene: Any, window: Any) -> Any | None:
    """PP 종료 시 일반 View Layer로 복귀하고 Snapshot Root 제외를 유지합니다."""

    settings = getattr(scene, "projection_paint", None)
    source_name = str(getattr(settings, "source_view_layer_name", "") or "")
    source = getattr(scene, "view_layers", {}).get(source_name) if source_name else None
    if source is None or is_projection_dataset_view_layer(source):
        source = next(
            (
                view_layer
                for view_layer in getattr(scene, "view_layers", ())
                if not is_dataset_view_layer(view_layer)
            ),
            None,
        )
    if source is not None:
        window.view_layer = source
    synchronize_projection_dataset_visibility(scene)
    return source


__all__ = [
    "VIEW_LAYER_KIND",
    "VIEW_LAYER_NAME",
    "activate_projection_dataset_view_layer",
    "ensure_projection_dataset_view_layer",
    "is_projection_dataset_view_layer",
    "restore_projection_source_view_layer",
    "synchronize_projection_dataset_visibility",
]
