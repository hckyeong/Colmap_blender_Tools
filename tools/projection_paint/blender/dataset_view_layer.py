"""Projection Paint 전용 View Layer와 독립 Snapshot 가시성을 관리합니다."""

from __future__ import annotations

from typing import Any, Iterable

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

from .ownership import is_owned, owned_uuid_of, resolve_owned, tag_owned


VIEW_LAYER_NAME = "COLMAP Projection Paint"
VIEW_LAYER_KIND = "PAINT_DATA_VIEW_LAYER"
VIEW_LAYER_TOOL_ID = "projection_paint"
PAINT_REFERENCE_KINDS = frozenset({"POINT_MESH", "CAMERA_OBJECT", "CAMERA_AXIS_ROOT"})
# 카메라 전체 숨김은 배경 사진도 차단하므로 점군과 축만 오브젝트 단위로 숨깁니다.
PAINT_HIDDEN_REFERENCE_KINDS = PAINT_REFERENCE_KINDS - {"CAMERA_OBJECT"}


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
    """Scene Root에 연결된 PP 카메라와 독립 OBJ 컬렉션을 반환합니다."""

    return tuple(
        collection
        for collection in tuple(getattr(getattr(scene, "collection", None), "children", ()))
        if is_owned(collection)
        and collection.get("pp_record_kind") in {"CAMERA_COLLECTION", "MODEL_COLLECTION"}
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


def hide_paint_dataset_references(scene: Any, view_layer: Any) -> list[dict[str, Any]]:
    """카메라 배경을 보존하고 페인트 View Layer의 현재 점군·축 표시만 숨깁니다."""
    settings = getattr(scene, "projection_paint", None)
    dataset_uuid = str(getattr(settings, "dataset_uuid", "") or "")
    if not dataset_uuid:
        return []
    objects = tuple(
        obj
        for obj in scene.objects
        if obj.get("pp_record_kind") in PAINT_HIDDEN_REFERENCE_KINDS
        and is_owned(obj, dataset_uuid=dataset_uuid)
        and view_layer.objects.get(obj.name) is obj
    )
    records = [
        {
            "uuid": owned_uuid_of(obj),
            "kind": str(obj.get("pp_record_kind")),
            "dataset_uuid": dataset_uuid,
            "view_layer": str(view_layer.name),
            "original_hidden": bool(obj.hide_get(view_layer=view_layer)),
            "applied_hidden": True,
        }
        for obj in objects
    ]
    changed = []
    try:
        for obj, record in zip(objects, records, strict=True):
            if not record["original_hidden"]:
                obj.hide_set(True, view_layer=view_layer)
                changed.append(obj)
    except (ReferenceError, RuntimeError):
        for obj in reversed(changed):
            obj.hide_set(False, view_layer=view_layer)
        raise
    return records


def restore_paint_dataset_references(scene: Any, records: Iterable[dict[str, Any]]) -> None:
    """이전 카메라 숨김 기록도 포함해 자신이 적용한 참고 표시만 원래 상태로 복구합니다."""
    for record in records:
        kind = str(record.get("kind", ""))
        if kind not in PAINT_REFERENCE_KINDS:
            continue
        view_layer = scene.view_layers.get(str(record.get("view_layer", "")))
        obj = resolve_owned(
            scene.objects,
            str(record.get("uuid", "")),
            kind=kind,
            dataset_uuid=str(record.get("dataset_uuid", "")),
        )
        if view_layer is None or obj is None or view_layer.objects.get(obj.name) is not obj:
            continue
        applied_hidden = bool(record.get("applied_hidden", True))
        if bool(obj.hide_get(view_layer=view_layer)) == applied_hidden:
            obj.hide_set(bool(record.get("original_hidden", False)), view_layer=view_layer)


def hide_other_paint_objects(scene: Any, view_layer: Any, target: Any) -> None:
    """페인트 대상과 배경용 카메라 외의 오브젝트를 현재 뷰 레이어에서만 숨깁니다."""
    settings = scene.projection_paint
    if settings.paint_object_visibility_active:
        return
    records = settings.paint_object_visibility
    if records:
        raise RuntimeError("이전 페인트 오브젝트 표시 기록을 먼저 복구해야 합니다.")
    dataset_uuid = str(settings.dataset_uuid or "")
    try:
        for obj in tuple(view_layer.objects):
            if obj is target or obj is scene.camera:
                continue
            # 프레임을 바꿔도 배경 사진이 유지되도록 관리 카메라 자체는 숨기지 않습니다.
            if (
                obj.type == "CAMERA" and dataset_uuid
                and is_owned(obj, kind="CAMERA_OBJECT", dataset_uuid=dataset_uuid)
            ):
                continue
            record = records.add()
            record.object = obj
            record.view_layer_name = view_layer.name
            record.view_layer_uuid = owned_uuid_of(view_layer)
            record.original_hidden = bool(obj.hide_get(view_layer=view_layer))
            if not record.original_hidden:
                obj.hide_set(True, view_layer=view_layer)
    except (AttributeError, ReferenceError, RuntimeError, TypeError):
        restore_other_paint_objects(scene)
        raise
    settings.paint_object_visibility_active = True


def restore_other_paint_objects(scene: Any) -> None:
    """저장된 실제 오브젝트 참조로 자신이 적용한 숨김만 원복하고 사용자 변경은 유지합니다."""
    settings = scene.projection_paint
    for record in settings.paint_object_visibility:
        obj = record.object
        view_layer = (
            resolve_owned(scene.view_layers, record.view_layer_uuid, kind=VIEW_LAYER_KIND)
            if record.view_layer_uuid else scene.view_layers.get(record.view_layer_name)
        )
        if (
            obj is None or view_layer is None
            or view_layer.objects.get(obj.name) is not obj
        ):
            continue
        if obj.hide_get(view_layer=view_layer):
            obj.hide_set(record.original_hidden, view_layer=view_layer)
    settings.paint_object_visibility.clear()
    settings.paint_object_visibility_active = False


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
    "hide_paint_dataset_references",
    "hide_other_paint_objects",
    "is_projection_dataset_view_layer",
    "restore_projection_source_view_layer",
    "restore_paint_dataset_references",
    "restore_other_paint_objects",
    "synchronize_projection_dataset_visibility",
]
