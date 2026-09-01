"""공용 Dataset Root를 허용된 도구 전용 View Layer에만 표시합니다."""

from __future__ import annotations

from typing import Any

from .ownership import is_dataset_owned
from .registry import snapshot


VIEW_LAYER_OWNER_KEY = "colmap_dataset_view_layer_owner"
VIEW_LAYER_SCHEMA_KEY = "colmap_dataset_view_layer_schema"
VIEW_LAYER_SCHEMA_VERSION = 1


def _pointer(value: Any) -> int:
    """유효한 Blender RNA 객체의 포인터를 반환하고 실패하면 0을 반환합니다."""

    try:
        return int(value.as_pointer()) if value is not None else 0
    except (AttributeError, ReferenceError, RuntimeError, TypeError):
        return 0


def mark_dataset_view_layer(view_layer: Any, tool_id: str) -> None:
    """공용 Dataset을 표시할 도구 전용 View Layer임을 중립 키로 기록합니다."""

    if view_layer is None or not str(tool_id).strip():
        raise ValueError("공용 Dataset View Layer 식별값이 비어 있습니다.")
    view_layer[VIEW_LAYER_OWNER_KEY] = str(tool_id)
    view_layer[VIEW_LAYER_SCHEMA_KEY] = VIEW_LAYER_SCHEMA_VERSION


def dataset_view_layer_owner(view_layer: Any) -> str:
    """현행 Schema의 공용 Dataset View Layer 도구 식별자를 반환합니다."""

    try:
        if int(view_layer.get(VIEW_LAYER_SCHEMA_KEY, 0)) != VIEW_LAYER_SCHEMA_VERSION:
            return ""
        return str(view_layer.get(VIEW_LAYER_OWNER_KEY, "") or "")
    except (AttributeError, ReferenceError, RuntimeError, TypeError, ValueError):
        return ""


def is_dataset_view_layer(view_layer: Any, tool_id: str | None = None) -> bool:
    """View Layer가 공용 Dataset 표시 권한과 선택 도구 ID를 만족하는지 확인합니다."""

    owner = dataset_view_layer_owner(view_layer)
    return bool(owner and (tool_id is None or owner == str(tool_id)))


def layer_collection_for_collection(layer_collection: Any, collection: Any) -> Any | None:
    """View Layer 트리에서 지정 Collection을 감싼 Layer Collection을 찾습니다."""

    if _pointer(getattr(layer_collection, "collection", None)) == _pointer(collection):
        return layer_collection
    for child in tuple(getattr(layer_collection, "children", ())):
        found = layer_collection_for_collection(child, collection)
        if found is not None:
            return found
    return None


def _active_dataset_roots(scene: Any) -> tuple[Any, ...]:
    """현재 Scene의 활성 UUID와 일치하는 공용 Dataset Root를 반환합니다."""

    if getattr(scene, "colmap_dataset", None) is None:
        return ()
    current = snapshot(scene)
    if not current.available:
        return ()
    return tuple(
        collection
        for collection in getattr(getattr(scene, "collection", None), "children", ())
        if is_dataset_owned(
            collection,
            dataset_uuid=current.dataset_uuid,
            role="ROOT",
        )
    )


def synchronize_dataset_root_visibility(scene: Any) -> None:
    """공용 Root를 등록된 도구 View Layer에만 표시하고 일반 Layer에서 제외합니다."""

    roots = _active_dataset_roots(scene)
    if not roots:
        return
    if len(roots) != 1:
        raise RuntimeError("활성 공용 Dataset Root Collection이 정확히 하나가 아닙니다.")
    root = roots[0]
    for view_layer in tuple(getattr(scene, "view_layers", ())):
        layer_collection = layer_collection_for_collection(
            view_layer.layer_collection,
            root,
        )
        if layer_collection is None:
            raise RuntimeError(
                f"{view_layer.name} View Layer에서 공용 Dataset Root를 찾지 못했습니다."
            )
        should_exclude = not is_dataset_view_layer(view_layer)
        layer_collection.exclude = should_exclude
        if bool(layer_collection.exclude) != should_exclude:
            raise RuntimeError(
                f"{view_layer.name} View Layer의 공용 Dataset 표시 상태를 확정하지 못했습니다."
            )
        view_layer.update()


__all__ = [
    "VIEW_LAYER_OWNER_KEY",
    "VIEW_LAYER_SCHEMA_KEY",
    "VIEW_LAYER_SCHEMA_VERSION",
    "dataset_view_layer_owner",
    "is_dataset_view_layer",
    "layer_collection_for_collection",
    "mark_dataset_view_layer",
    "synchronize_dataset_root_visibility",
]
