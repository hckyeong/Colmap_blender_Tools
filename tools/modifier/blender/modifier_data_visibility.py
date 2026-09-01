"""Modifier 관리 데이터를 전용 View Layer에만 표시하도록 격리합니다."""

from __future__ import annotations

from typing import Any

import bpy

try:
    from ....shared.colmap_dataset.blender.view_layers import (
        is_dataset_view_layer,
        mark_dataset_view_layer,
    )
except ImportError:
    from shared.colmap_dataset.blender.view_layers import (
        is_dataset_view_layer,
        mark_dataset_view_layer,
    )

from .managed_scene import is_managed, iter_managed_roots
from .ownership import OWNER_KEY, OWNER_TOKEN, RECORD_KIND_KEY


MODIFIER_DATA_VIEW_LAYER_NAME = "COLMAP Modifier Data"
MODIFIER_DATA_VIEW_LAYER_KIND = "MODIFIER_DATA_VIEW_LAYER"
MODIFIER_DATA_VIEW_LAYER_SCHEMA_KEY = "colmap_modifier_data_view_layer_schema"
MODIFIER_DATA_VIEW_LAYER_SCHEMA_VERSION = 1


def _pointer(value: Any) -> int:
    """유효한 Blender RNA 객체의 포인터를 반환하고 실패하면 0을 반환합니다."""

    try:
        return int(value.as_pointer()) if value is not None else 0
    except (AttributeError, ReferenceError, RuntimeError, TypeError):
        return 0


def is_modifier_data_view_layer(view_layer: Any) -> bool:
    """View Layer가 현행 Modifier 데이터 전용 소유본인지 확인합니다."""

    try:
        return bool(
            view_layer is not None
            and view_layer.get(OWNER_KEY) == OWNER_TOKEN
            and view_layer.get(RECORD_KIND_KEY) == MODIFIER_DATA_VIEW_LAYER_KIND
            and int(view_layer.get(MODIFIER_DATA_VIEW_LAYER_SCHEMA_KEY, 0))
            == MODIFIER_DATA_VIEW_LAYER_SCHEMA_VERSION
        )
    except (AttributeError, ReferenceError, RuntimeError, TypeError, ValueError):
        return False


def _owned_modifier_view_layers(scene: Any) -> tuple[Any, ...]:
    """Scene에 속한 현행 Modifier 데이터 View Layer를 반환합니다."""

    return tuple(
        view_layer
        for view_layer in getattr(scene, "view_layers", ())
        if is_modifier_data_view_layer(view_layer)
    )


def ensure_modifier_data_view_layer(scene: Any) -> Any:
    """Scene에 이름 충돌과 무관한 Modifier 데이터 전용 View Layer를 보장합니다."""

    if scene is None:
        raise RuntimeError("Modifier 데이터 View Layer를 만들 Scene이 없습니다.")
    owned_layers = _owned_modifier_view_layers(scene)
    if len(owned_layers) > 1:
        raise RuntimeError("Modifier 데이터 전용 View Layer가 중복되었습니다.")
    if owned_layers:
        mark_dataset_view_layer(owned_layers[0], "modifier")
        return owned_layers[0]
    try:
        view_layer = scene.view_layers.new(MODIFIER_DATA_VIEW_LAYER_NAME)
        view_layer[OWNER_KEY] = OWNER_TOKEN
        view_layer[RECORD_KIND_KEY] = MODIFIER_DATA_VIEW_LAYER_KIND
        view_layer[MODIFIER_DATA_VIEW_LAYER_SCHEMA_KEY] = (
            MODIFIER_DATA_VIEW_LAYER_SCHEMA_VERSION
        )
        mark_dataset_view_layer(view_layer, "modifier")
    except (AttributeError, ReferenceError, RuntimeError, TypeError) as error:
        raise RuntimeError(
            f"Modifier 데이터 전용 View Layer 생성에 실패했습니다: {error}"
        ) from error
    return view_layer


def _layer_collection_for_collection(layer_collection: Any, collection: Any) -> Any | None:
    """View Layer 트리에서 지정 Collection을 감싼 Layer Collection을 찾습니다."""

    if _pointer(getattr(layer_collection, "collection", None)) == _pointer(collection):
        return layer_collection
    for child in getattr(layer_collection, "children", ()):
        found = _layer_collection_for_collection(child, collection)
        if found is not None:
            return found
    return None


def _isolate_modifier_view_layer_collections(
    view_layer: Any,
    managed_roots: tuple[Any, ...],
) -> None:
    """Modifier 전용 View Layer에서 관리 Root 외의 최상위 Collection을 제외합니다."""

    managed_root_pointers = {_pointer(root) for root in managed_roots}
    for layer_collection in tuple(view_layer.layer_collection.children):
        should_exclude = (
            _pointer(getattr(layer_collection, "collection", None))
            not in managed_root_pointers
        )
        layer_collection.exclude = should_exclude
        if bool(layer_collection.exclude) != should_exclude:
            raise RuntimeError(
                f"{view_layer.name} View Layer의 일반 Collection 제외 상태를 확정하지 못했습니다."
            )


def _hide_foreign_root_objects(view_layer: Any) -> None:
    """Collection 제외로 가릴 수 없는 Scene Root의 일반 Object를 전용 View Layer에서 숨깁니다."""

    for obj in tuple(view_layer.objects):
        if is_managed(obj):
            continue
        try:
            obj.hide_set(True, view_layer=view_layer)
        except (AttributeError, ReferenceError, RuntimeError, TypeError) as error:
            raise RuntimeError(
                f"{obj.name} Object를 Modifier 전용 View Layer에서 숨기지 못했습니다: {error}"
            ) from error
        if not bool(obj.hide_get(view_layer=view_layer)):
            raise RuntimeError(
                f"{obj.name} Object의 Modifier 전용 View Layer 숨김 상태를 확정하지 못했습니다."
            )


def synchronize_modifier_data_visibility(
    scene: Any,
    *,
    create_view_layer: bool,
) -> Any | None:
    """전용 View Layer에는 관리 데이터만 표시하고 일반 View Layer에서는 제외합니다."""

    if scene is None:
        raise RuntimeError("Modifier 데이터 가시성을 동기화할 Scene이 없습니다.")
    managed_roots = tuple(iter_managed_roots(scene))
    owned_layers = _owned_modifier_view_layers(scene)
    if not managed_roots and not owned_layers and not create_view_layer:
        return None
    if not owned_layers and not create_view_layer:
        return None
    modifier_view_layer = ensure_modifier_data_view_layer(scene)
    for view_layer in tuple(scene.view_layers):
        is_modifier_view_layer = _pointer(view_layer) == _pointer(modifier_view_layer)
        should_exclude = not is_dataset_view_layer(view_layer, "modifier")
        for root in managed_roots:
            layer_collection = _layer_collection_for_collection(
                view_layer.layer_collection,
                root,
            )
            if layer_collection is None:
                raise RuntimeError(
                    f"{view_layer.name} View Layer에서 Modifier 관리 Root를 찾지 못했습니다."
                )
            layer_collection.exclude = should_exclude
            if bool(layer_collection.exclude) != should_exclude:
                raise RuntimeError(
                    f"{view_layer.name} View Layer의 Modifier 데이터 제외 상태를 확정하지 못했습니다."
                )
        if is_modifier_view_layer:
            _isolate_modifier_view_layer_collections(view_layer, managed_roots)
        view_layer.update()
        if is_modifier_view_layer:
            _hide_foreign_root_objects(view_layer)
            view_layer.update()
    return modifier_view_layer


def activate_modifier_data_view_layer(window: Any) -> Any:
    """Modifier Workspace Window를 데이터 전용 View Layer에 연결합니다."""

    scene = getattr(window, "scene", None)
    if scene is None:
        raise RuntimeError("Modifier 데이터 View Layer를 활성화할 Scene이 없습니다.")
    view_layer = synchronize_modifier_data_visibility(
        scene,
        create_view_layer=True,
    )
    if view_layer is None:
        raise RuntimeError("Modifier 데이터 전용 View Layer를 준비하지 못했습니다.")
    try:
        window.view_layer = view_layer
    except (AttributeError, ReferenceError, RuntimeError, TypeError) as error:
        raise RuntimeError(
            f"Modifier 데이터 전용 View Layer 활성화에 실패했습니다: {error}"
        ) from error
    if _pointer(getattr(window, "view_layer", None)) != _pointer(view_layer):
        raise RuntimeError("Modifier Workspace가 데이터 전용 View Layer로 전환되지 않았습니다.")
    return view_layer


__all__ = [
    "MODIFIER_DATA_VIEW_LAYER_KIND",
    "MODIFIER_DATA_VIEW_LAYER_NAME",
    "MODIFIER_DATA_VIEW_LAYER_SCHEMA_KEY",
    "MODIFIER_DATA_VIEW_LAYER_SCHEMA_VERSION",
    "activate_modifier_data_view_layer",
    "ensure_modifier_data_view_layer",
    "is_modifier_data_view_layer",
    "synchronize_modifier_data_visibility",
]
