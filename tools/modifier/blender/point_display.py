"""Point Scale의 Scene 속성과 Geometry Nodes 입력을 양방향 동기화합니다."""

from __future__ import annotations

import bpy
from bpy.app.handlers import persistent

from .managed_scene import iter_managed_objects, iter_point_display_modifiers


_SYNC_EPSILON = 1.0e-7
_sync_in_progress = False


def _managed_point_modifiers(dataset_uuid: str | None = None):
    """기존 내부 호출 형식을 유지하며 통합 관리 조회 결과를 입력값과 묶습니다."""
    for obj in iter_managed_objects(
        bpy.data.objects,
        kind="POINT_MESH",
        dataset_uuid=dataset_uuid,
    ):
        for modifier in iter_point_display_modifiers(obj):
            node_group = modifier.node_group
            socket_identifier = node_group.get("colmap_point_scale_socket")
            if not socket_identifier:
                continue
            modifier_input = getattr(
                modifier.properties.inputs,
                str(socket_identifier),
                None,
            )
            if modifier_input is not None:
                yield obj, modifier_input


def _tag_point_display_update(obj: bpy.types.Object) -> None:
    obj.update_tag(refresh={"DATA"})
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.type == "VIEW_3D":
                area.tag_redraw()


def update_point_scale_from_settings(settings, context) -> None:
    """왼쪽 애드온 값을 관리 Modifier 입력으로 즉시 전달합니다."""
    global _sync_in_progress
    if _sync_in_progress:
        return

    dataset_uuid = settings.dataset_uuid or None
    _sync_in_progress = True
    try:
        for obj, modifier_input in _managed_point_modifiers(dataset_uuid):
            modifier_input.value = settings.point_size
            obj["colmap_point_size"] = settings.point_size
            _tag_point_display_update(obj)
    finally:
        _sync_in_progress = False


def _apply_modifier_value_to_dataset(
    settings,
    dataset_uuid: str | None,
    point_scale: float,
) -> None:
    """오른쪽에서 바뀐 값을 왼쪽과 같은 Dataset의 모든 Point Object에 반영합니다."""
    settings.point_size = point_scale
    for obj, modifier_input in _managed_point_modifiers(dataset_uuid):
        modifier_input.value = point_scale
        obj["colmap_point_size"] = point_scale
        _tag_point_display_update(obj)


@persistent
def sync_point_scale_from_modifiers(scene, depsgraph) -> None:
    """Depsgraph 평가 뒤 Modifier 직접 편집값을 Scene 속성으로 역동기화합니다."""
    del depsgraph
    global _sync_in_progress
    if _sync_in_progress or scene is None:
        return
    settings = getattr(scene, "colmap_import", None)
    if settings is None:
        return

    dataset_uuid = settings.dataset_uuid or None
    for obj, modifier_input in _managed_point_modifiers(dataset_uuid):
        point_scale = float(modifier_input.value)
        previous = float(obj.get("colmap_point_size", point_scale))
        if abs(point_scale - previous) <= _SYNC_EPSILON:
            continue

        _sync_in_progress = True
        try:
            _apply_modifier_value_to_dataset(
                settings,
                dataset_uuid,
                point_scale,
            )
        finally:
            _sync_in_progress = False
        break


def register_point_scale_sync() -> None:
    if sync_point_scale_from_modifiers not in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.append(sync_point_scale_from_modifiers)


def unregister_point_scale_sync() -> None:
    if sync_point_scale_from_modifiers in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.remove(sync_point_scale_from_modifiers)
