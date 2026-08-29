"""Camera Frustum Size를 관리 Camera Data에 동기화합니다."""

from __future__ import annotations

import json
import math

import bpy

from .ownership import is_owned


CAMERA_FRUSTUM_SIZE_MIN = 0.0
CAMERA_FRUSTUM_SIZE_MAX = 1000.0
DEFAULT_CAMERA_FRUSTUM_SIZE = 1.0
BLENDER_CAMERA_DISPLAY_SIZE_MIN = 0.01
FRUSTUM_ZERO_VISIBILITY_STATE_KEY = "pp_frustum_zero_visibility_state"


def _managed_camera_data(dataset_uuid: str | None = None):
    """요청한 Dataset에 속한 정식 관리 Camera Data만 순회합니다."""

    for camera_data in bpy.data.cameras:
        if is_owned(
            camera_data,
            kind="CAMERA_DATA",
            dataset_uuid=dataset_uuid,
        ):
            yield camera_data


def _managed_camera_objects(scene, dataset_uuid: str):
    """현재 Scene과 Dataset에 속한 정식 관리 Camera Object만 순회합니다."""

    for camera_object in scene.objects:
        if is_owned(
            camera_object,
            kind="CAMERA_OBJECT",
            dataset_uuid=dataset_uuid,
        ):
            yield camera_object


def camera_frustum_size_from_scene(scene) -> float:
    """Scene 권위 설정에서 검증된 Camera 표시 크기를 반환합니다."""

    settings = getattr(scene, "projection_paint", None)
    if settings is None:
        return DEFAULT_CAMERA_FRUSTUM_SIZE
    try:
        value = float(settings.camera_frustum_size)
    except (AttributeError, TypeError, ValueError):
        return DEFAULT_CAMERA_FRUSTUM_SIZE
    if (
        not math.isfinite(value)
        or value < CAMERA_FRUSTUM_SIZE_MIN
        or value > CAMERA_FRUSTUM_SIZE_MAX
    ):
        return DEFAULT_CAMERA_FRUSTUM_SIZE
    return value


def camera_display_size_from_frustum_size(frustum_size: float) -> float:
    """Scene 프러스텀 크기를 Blender Camera가 허용하는 표시 크기로 변환합니다."""

    return max(BLENDER_CAMERA_DISPLAY_SIZE_MIN, float(frustum_size))


def _visibility_state(camera_object) -> dict[str, bool] | None:
    """프러스텀 0 적용 전에 저장한 뷰 레이어별 눈 표시를 읽습니다."""

    raw = str(camera_object.get(FRUSTUM_ZERO_VISIBILITY_STATE_KEY, "") or "")
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict) or not all(
        isinstance(name, str) and isinstance(hidden, bool)
        for name, hidden in value.items()
    ):
        return None
    return value


def apply_camera_frustum_visibility_to_object(
    scene,
    camera_object,
    *,
    hidden: bool,
) -> bool:
    """프러스텀 0의 눈 숨김을 적용하거나 이전 표시 상태를 복구합니다."""

    states = _visibility_state(camera_object)
    if states is None:
        raise RuntimeError(
            f"Camera 프러스텀 표시 복구 상태가 손상되었습니다: {camera_object.name}"
        )
    changed = False
    if hidden:
        for view_layer in scene.view_layers:
            if view_layer.name not in states:
                states[view_layer.name] = bool(
                    camera_object.hide_get(view_layer=view_layer)
                )
            if not camera_object.hide_get(view_layer=view_layer):
                camera_object.hide_set(True, view_layer=view_layer)
                changed = True
        payload = json.dumps(states, ensure_ascii=False, sort_keys=True)
        if camera_object.get(FRUSTUM_ZERO_VISIBILITY_STATE_KEY) != payload:
            camera_object[FRUSTUM_ZERO_VISIBILITY_STATE_KEY] = payload
            changed = True
        return changed

    if not states:
        return False
    for view_layer in scene.view_layers:
        if view_layer.name not in states:
            continue
        current_hidden = bool(camera_object.hide_get(view_layer=view_layer))
        original_hidden = bool(states[view_layer.name])
        if current_hidden and current_hidden != original_hidden:
            camera_object.hide_set(original_hidden, view_layer=view_layer)
            changed = True
    del camera_object[FRUSTUM_ZERO_VISIBILITY_STATE_KEY]
    return True


def _redraw_viewports() -> None:
    """열려 있는 3D Viewport에 Camera 표시 변경을 즉시 다시 그립니다."""

    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.type == "VIEW_3D":
                area.tag_redraw()


def apply_camera_frustum_size(
    scene,
    *,
    dataset_uuid: str | None = None,
    redraw: bool = True,
) -> int:
    """Scene 설정값을 지정 Dataset의 관리 Camera Data에 반영합니다."""

    settings = getattr(scene, "projection_paint", None)
    if settings is None:
        return 0
    target_dataset_uuid = dataset_uuid
    if target_dataset_uuid is None:
        target_dataset_uuid = str(getattr(settings, "dataset_uuid", "") or "")
        if not target_dataset_uuid:
            return 0
    frustum_size = camera_frustum_size_from_scene(scene)
    display_size = camera_display_size_from_frustum_size(frustum_size)
    updated = 0
    for camera_data in _managed_camera_data(target_dataset_uuid):
        if abs(float(camera_data.display_size) - display_size) <= 1.0e-9:
            continue
        camera_data.display_size = display_size
        camera_data.update_tag()
        updated += 1
    visibility_changed = False
    for camera_object in _managed_camera_objects(scene, target_dataset_uuid):
        visibility_changed |= apply_camera_frustum_visibility_to_object(
            scene,
            camera_object,
            hidden=frustum_size == 0.0,
        )
    if (updated or visibility_changed) and redraw:
        _redraw_viewports()
    return updated


def update_camera_frustum_size_from_settings(settings, context) -> None:
    """UI 변경값을 현재 Dataset의 관리 Camera Data에 즉시 반영합니다."""

    del context
    scene = getattr(settings, "id_data", None)
    if not isinstance(scene, bpy.types.Scene):
        return
    apply_camera_frustum_size(scene)


__all__ = [
    "BLENDER_CAMERA_DISPLAY_SIZE_MIN",
    "CAMERA_FRUSTUM_SIZE_MAX",
    "CAMERA_FRUSTUM_SIZE_MIN",
    "DEFAULT_CAMERA_FRUSTUM_SIZE",
    "FRUSTUM_ZERO_VISIBILITY_STATE_KEY",
    "apply_camera_frustum_size",
    "apply_camera_frustum_visibility_to_object",
    "camera_display_size_from_frustum_size",
    "camera_frustum_size_from_scene",
    "update_camera_frustum_size_from_settings",
]
