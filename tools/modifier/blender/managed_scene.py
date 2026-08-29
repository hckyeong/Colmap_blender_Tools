"""애드온이 소유한 Scene 요소의 판정, 조회와 선택 잠금을 한곳에서 관리합니다."""

from __future__ import annotations

from collections.abc import Iterable, Iterator

import bpy

from .ownership import DATASET_UUID_KEY, OWNER_KEY, OWNER_TOKEN, RECORD_KIND_KEY


def is_managed(
    data_block,
    *,
    kind: str | None = None,
    dataset_uuid: str | None = None,
) -> bool:
    """데이터블록이 요청한 애드온 소유권 조건을 만족하는지 반환합니다."""
    if data_block.get(OWNER_KEY) != OWNER_TOKEN:
        return False
    if kind is not None and data_block.get(RECORD_KIND_KEY) != kind:
        return False
    if (
        dataset_uuid is not None
        and data_block.get(DATASET_UUID_KEY) != dataset_uuid
    ):
        return False
    return True


def iter_managed_objects(
    objects: Iterable[bpy.types.Object],
    *,
    kind: str,
    dataset_uuid: str | None = None,
) -> Iterator[bpy.types.Object]:
    """주어진 조회 범위에서 조건에 맞는 관리 Object만 순회합니다."""
    for obj in objects:
        if is_managed(obj, kind=kind, dataset_uuid=dataset_uuid):
            yield obj


def iter_managed_collection_objects(
    collection: bpy.types.Collection,
    *,
    kind: str,
) -> Iterator[bpy.types.Object]:
    """Collection에 직접 연결된 관리 Object만 순회합니다."""
    yield from iter_managed_objects(collection.objects, kind=kind)


def iter_managed_point_objects(
    context,
    dataset_uuid: str | None = None,
) -> Iterator[bpy.types.Object]:
    """현재 View Layer 범위의 관리 Point Object를 순회합니다."""
    yield from iter_managed_objects(
        context.view_layer.objects,
        kind="POINT_MESH",
        dataset_uuid=dataset_uuid,
    )


def iter_managed_camera_objects(
    context,
    dataset_uuid: str | None = None,
) -> Iterator[bpy.types.Object]:
    """현재 View Layer 범위의 관리 Camera Object를 순회합니다."""
    yield from iter_managed_objects(
        context.view_layer.objects,
        kind="CAMERA",
        dataset_uuid=dataset_uuid,
    )


def require_managed_point_object(context, dataset_uuid: str) -> bpy.types.Object:
    """현재 View Layer에서 정식 관리 Point Mesh 하나를 검증해 반환합니다."""
    matches = list(iter_managed_point_objects(context, dataset_uuid))
    if len(matches) != 1:
        raise ValueError(
            "활성 데이터셋의 정식 관리 Point Object가 정확히 하나가 아닙니다. "
            "복제된 관리 속성을 제거하거나 다시 Import하십시오."
        )
    obj = matches[0]
    if obj.type != "MESH":
        raise ValueError("관리 Point Object가 Mesh가 아닙니다. 다시 Import하십시오.")
    owned_collections = [
        collection
        for collection in obj.users_collection
        if is_managed(
            collection,
            kind="POINTS_COLLECTION",
            dataset_uuid=dataset_uuid,
        )
    ]
    if len(owned_collections) != 1:
        raise ValueError("Point Object가 정식 관리 POINTS Collection에 속하지 않습니다.")
    return obj


def managed_camera_map(context, dataset_uuid: str) -> dict[int, bpy.types.Object]:
    """현재 View Layer의 관리 Camera를 IMAGE_ID 정수 키로 반환합니다."""
    result: dict[int, bpy.types.Object] = {}
    for obj in iter_managed_camera_objects(context, dataset_uuid):
        raw_id = obj.get("colmap_image_id_text")
        try:
            image_id = int(raw_id)
        except (TypeError, ValueError) as exc:
            raise ValueError("관리 Camera Object의 IMAGE_ID가 손상되었습니다.") from exc
        if image_id in result:
            raise ValueError(f"IMAGE_ID {image_id}의 Camera Object가 중복되었습니다.")
        result[image_id] = obj
    return result


def iter_managed_roots(scene: bpy.types.Scene) -> Iterator[bpy.types.Collection]:
    """Scene Root에 직접 연결된 관리 Root Collection을 순회합니다."""
    for collection in scene.collection.children:
        if is_managed(collection, kind="ROOT"):
            yield collection


def require_managed_child_collection(
    collection: bpy.types.Collection,
    kind: str,
) -> bpy.types.Collection:
    """관리 하위 Collection이 정확히 하나인지 검증해 반환합니다."""
    matches = [
        child
        for child in collection.children
        if is_managed(child, kind=kind)
    ]
    if len(matches) != 1:
        raise RuntimeError(f"{kind} 관리 Collection이 정확히 하나가 아닙니다.")
    return matches[0]


def require_collection_point_object(
    collection: bpy.types.Collection,
) -> bpy.types.Object:
    """Collection에 직접 연결된 관리 Point Object 하나를 반환합니다."""
    points = list(iter_managed_collection_objects(collection, kind="POINT_MESH"))
    if len(points) != 1:
        raise RuntimeError("관리 Point Object가 정확히 하나가 아닙니다.")
    return points[0]


def collection_camera_map(
    collection: bpy.types.Collection,
) -> dict[str, bpy.types.Object]:
    """Collection의 관리 Camera를 기존 Scene commit용 문자열 키로 반환합니다."""
    result: dict[str, bpy.types.Object] = {}
    for obj in iter_managed_collection_objects(collection, kind="CAMERA"):
        image_id = obj.get("colmap_image_id_text")
        if not image_id or image_id in result:
            raise RuntimeError("관리 Camera의 IMAGE_ID가 없거나 중복되었습니다.")
        result[str(image_id)] = obj
    return result


def find_collection_axis_root(
    collection: bpy.types.Collection,
) -> bpy.types.Object | None:
    """Collection의 선택적 Axis Root를 반환하고 중복은 거부합니다."""
    roots = list(iter_managed_collection_objects(collection, kind="AXIS_ROOT"))
    if len(roots) > 1:
        raise RuntimeError("관리 Axis Root가 중복되었습니다.")
    return roots[0] if roots else None


def iter_point_display_modifiers(
    point_object: bpy.types.Object,
) -> Iterator[bpy.types.NodesModifier]:
    """Point Object와 같은 Dataset에 속한 관리 표시 Modifier를 순회합니다."""
    dataset_uuid = point_object.get(DATASET_UUID_KEY)
    for modifier in point_object.modifiers:
        node_group = getattr(modifier, "node_group", None)
        if (
            modifier.type == "NODES"
            and node_group is not None
            and is_managed(
                node_group,
                kind="POINT_DISPLAY_NODES",
                dataset_uuid=dataset_uuid,
            )
        ):
            yield modifier


def require_point_display_modifier(
    point_object: bpy.types.Object,
) -> bpy.types.NodesModifier:
    """관리 Point 표시 Modifier가 정확히 하나인지 검증해 반환합니다."""
    modifiers = list(iter_point_display_modifiers(point_object))
    if len(modifiers) != 1:
        raise RuntimeError("관리 Point 표시 Modifier가 정확히 하나가 아닙니다.")
    return modifiers[0]


def lock_managed_point_objects(context, dataset_uuid: str | None = None) -> None:
    """Mesh를 순회하지 않고 관리 Point Object 선택을 잠급니다."""
    active = getattr(context.view_layer.objects, "active", None)
    for obj in iter_managed_point_objects(context, dataset_uuid):
        obj.hide_select = True
        obj.select_set(False)
        if active is obj:
            context.view_layer.objects.active = None


def lock_managed_camera_objects(context, dataset_uuid: str | None = None) -> None:
    """관리 Camera Object를 선택 해제하고 기본 선택 잠금 상태로 되돌립니다."""
    active = getattr(context.view_layer.objects, "active", None)
    for obj in iter_managed_camera_objects(context, dataset_uuid):
        obj.hide_select = True
        obj.select_set(False)
        if active is obj:
            context.view_layer.objects.active = None


def lock_managed_selection_objects(
    context,
    dataset_uuid: str | None = None,
) -> None:
    """Point와 Camera 관리 Object의 선택 잠금 불변 조건을 함께 복원합니다."""
    lock_managed_point_objects(context, dataset_uuid)
    lock_managed_camera_objects(context, dataset_uuid)
