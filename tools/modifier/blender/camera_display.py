"""Camera Frustum Size를 관리 Camera Data에 동기화합니다."""

from __future__ import annotations

import bpy

from .managed_scene import is_managed, iter_managed_objects


def _managed_camera_data(dataset_uuid: str | None = None):
    """요청한 Dataset에 속한 정식 관리 Camera Data만 순회합니다."""

    visited: set[int] = set()
    for obj in iter_managed_objects(
        bpy.data.objects,
        kind="CAMERA",
        dataset_uuid=dataset_uuid,
    ):
        camera_data = obj.data
        if not isinstance(camera_data, bpy.types.Camera):
            continue
        if not is_managed(
            camera_data,
            kind="CAMERA_DATA",
            dataset_uuid=dataset_uuid,
        ):
            continue
        pointer = camera_data.as_pointer()
        if pointer in visited:
            continue
        visited.add(pointer)
        yield camera_data


def _redraw_viewports() -> None:
    """열려 있는 3D Viewport에 Camera 표시 변경을 즉시 다시 그립니다."""

    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.type == "VIEW_3D":
                area.tag_redraw()


def update_camera_frustum_size_from_settings(settings, context) -> None:
    """COLMAP Import 설정값을 현재 Dataset의 관리 Camera에 반영합니다."""

    del context
    dataset_uuid = settings.dataset_uuid or None
    updated = False
    for camera_data in _managed_camera_data(dataset_uuid):
        camera_data.display_size = settings.camera_frustum_size
        camera_data.update_tag()
        updated = True
    if updated:
        _redraw_viewports()
