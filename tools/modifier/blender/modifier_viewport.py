"""COLMAP Modifier 전용 Viewport의 표시 상태와 Point 프레이밍을 관리합니다."""

from __future__ import annotations

import json
from math import cos, radians, sin
from typing import Any

import bpy
from mathutils import Vector

from .modifier_session import (
    SESSION,
    WORKSPACE_VISIBILITY_SNAPSHOT_KEY,
    is_session_active_for_window,
    session_modifier_workspace,
    session_target_window,
)
from .ownership import DATASET_UUID_KEY, OWNER_KEY, OWNER_TOKEN, RECORD_KIND_KEY


DEFAULT_STARTUP_OBJECTS = (
    ("Cube", "MESH"),
    ("Camera", "CAMERA"),
    ("Light", "LIGHT"),
)
MODIFIER_VIEW_AZIMUTH_DEGREES = 45.0
MODIFIER_VIEW_ELEVATION_DEGREES = 45.0
MODIFIER_VIEW_PROJECTION = "PERSP"
VISIBILITY_SNAPSHOT_SCHEMA_VERSION = 1


def _pointer(value: Any) -> int:
    """유효한 Blender RNA 객체의 런타임 포인터를 반환합니다."""

    if value is None:
        return 0
    try:
        return int(value.as_pointer())
    except (AttributeError, ReferenceError, RuntimeError):
        return 0


def _modifier_view_rotation():
    """45도 방위각과 고도각을 바라보는 View 회전을 계산합니다."""

    azimuth = radians(MODIFIER_VIEW_AZIMUTH_DEGREES)
    elevation = radians(MODIFIER_VIEW_ELEVATION_DEGREES)
    viewer_direction = Vector(
        (
            cos(elevation) * cos(azimuth),
            -cos(elevation) * sin(azimuth),
            sin(elevation),
        )
    )
    return (-viewer_direction).to_track_quat("-Z", "Y").normalized()


def set_modifier_perspective_view(area: Any) -> None:
    """상단 3D View를 45도 원근 시점으로 설정합니다."""

    if area is None or getattr(area, "type", "") != "VIEW_3D":
        raise RuntimeError("45도 원근 시점을 설정할 3D View가 없습니다.")
    region_3d = area.spaces.active.region_3d
    region_3d.lock_rotation = False
    region_3d.view_rotation = _modifier_view_rotation()
    region_3d.view_perspective = MODIFIER_VIEW_PROJECTION
    region_3d.update()
    area.tag_redraw()


def normalize_modifier_viewport(window: Any, area: Any) -> None:
    """Modifier View3D의 오브젝트 필터를 모두 켜고 Local View를 해제합니다."""

    if area is None or getattr(area, "type", "") != "VIEW_3D":
        raise RuntimeError("표시 필터를 정규화할 Modifier 3D View가 없습니다.")
    space = area.spaces.active
    filter_names = tuple(
        prop.identifier
        for prop in space.bl_rna.properties
        if prop.identifier.startswith("show_object_viewport_")
    )
    if not filter_names:
        raise RuntimeError("현재 Blender에서 View3D 오브젝트 표시 필터를 찾지 못했습니다.")
    for filter_name in filter_names:
        setattr(space, filter_name, True)

    if getattr(space, "local_view", None) is not None:
        region = next((item for item in area.regions if item.type == "WINDOW"), None)
        if region is None:
            raise RuntimeError("Local View를 해제할 WINDOW Region을 찾지 못했습니다.")
        with bpy.context.temp_override(
            window=window,
            screen=window.screen,
            area=area,
            region=region,
        ):
            result = bpy.ops.view3d.localview(frame_selected=False)
        if "FINISHED" not in result:
            raise RuntimeError("Modifier View3D의 Local View 해제에 실패했습니다.")

    disabled = tuple(name for name in filter_names if not bool(getattr(space, name)))
    if disabled:
        raise RuntimeError(f"Modifier View3D 표시 필터 검증에 실패했습니다: {', '.join(disabled)}")
    if getattr(space, "local_view", None) is not None:
        raise RuntimeError("Modifier View3D가 Local View 상태로 남아 있습니다.")
    area.tag_redraw()


def hide_default_startup_objects(window: Any) -> None:
    """기본 Cube·Camera·Light를 숨기고 진입 전 상태를 Session에 저장합니다."""

    if SESSION.visibility_view_layer_name:
        return
    view_layer = getattr(window, "view_layer", None)
    if view_layer is None:
        raise RuntimeError("기본 오브젝트를 숨길 View Layer가 없습니다.")
    states: list[tuple[Any, int, str, str, bool]] = []
    try:
        snapshot_text = str(
            window.workspace.get(WORKSPACE_VISIBILITY_SNAPSHOT_KEY, "") or ""
        )
        if snapshot_text:
            payload = json.loads(snapshot_text)
            if payload.get("schema") != VISIBILITY_SNAPSHOT_SCHEMA_VERSION:
                raise ValueError("지원하지 않는 기본 오브젝트 숨김 Snapshot입니다.")
            view_layer_name = str(payload.get("view_layer", "") or "")
            saved_objects = {
                (str(item["name"]), str(item["type"])): bool(item["hidden"])
                for item in payload.get("objects", ())
                if isinstance(item, dict)
                and "name" in item
                and "type" in item
                and "hidden" in item
            }
        else:
            view_layer_name = view_layer.name
            saved_objects = {}

        for object_name, object_type in DEFAULT_STARTUP_OBJECTS:
            obj = view_layer.objects.get(object_name)
            if (
                obj is None
                or obj.type != object_type
                or obj.get(OWNER_KEY) == OWNER_TOKEN
            ):
                continue
            hidden = saved_objects.get(
                (object_name, object_type),
                bool(obj.hide_get(view_layer=view_layer)),
            )
            states.append((obj, _pointer(obj), obj.name, obj.type, hidden))
        if not snapshot_text:
            payload = {
                "schema": VISIBILITY_SNAPSHOT_SCHEMA_VERSION,
                "view_layer": view_layer_name,
                "objects": [
                    {"name": name, "type": object_type, "hidden": hidden}
                    for _reference, _pointer_value, name, object_type, hidden in states
                ],
            }
            window.workspace[WORKSPACE_VISIBILITY_SNAPSHOT_KEY] = json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
            )
        for obj, _object_pointer, _object_name, _object_type, _hidden in states:
            obj.hide_set(True, view_layer=view_layer)
    except (AttributeError, KeyError, ReferenceError, RuntimeError, TypeError, ValueError) as error:
        for obj, _object_pointer, _object_name, _object_type, hidden in states:
            try:
                obj.hide_set(hidden, view_layer=view_layer)
            except (AttributeError, ReferenceError, RuntimeError):
                continue
        raise RuntimeError(f"기본 오브젝트 숨김 처리에 실패했습니다: {error}") from error
    SESSION.bind_default_object_visibility(view_layer_name, tuple(states))


def restore_default_startup_objects(window: Any) -> tuple[str, ...]:
    """Session 진입 전에 기록한 기본 오브젝트 숨김 상태를 복원합니다."""

    states = SESSION.default_object_visibility
    if not states and not SESSION.visibility_view_layer_name:
        return ()
    modifier_workspace = session_modifier_workspace()
    scene = getattr(window, "scene", None)
    view_layer = (
        scene.view_layers.get(SESSION.visibility_view_layer_name)
        if scene is not None
        else None
    )
    warnings: list[str] = []
    if view_layer is None:
        warnings.append("기본 오브젝트 숨김 상태를 복원할 View Layer를 찾지 못했습니다.")
    else:
        for reference, object_pointer, object_name, object_type, hidden in states:
            obj = reference if _pointer(reference) == object_pointer else None
            if obj is None:
                obj = next(
                    (
                        candidate
                        for candidate in bpy.data.objects
                        if _pointer(candidate) == object_pointer
                    ),
                    None,
                )
            if obj is None:
                candidate = view_layer.objects.get(object_name)
                if (
                    candidate is not None
                    and candidate.type == object_type
                    and candidate.get(OWNER_KEY) != OWNER_TOKEN
                ):
                    obj = candidate
            if obj is None or obj.name not in view_layer.objects:
                continue
            try:
                obj.hide_set(hidden, view_layer=view_layer)
            except (AttributeError, ReferenceError, RuntimeError) as error:
                warnings.append(f"{object_name} 숨김 상태 복원 실패: {error}")
    if not warnings:
        try:
            workspace = modifier_workspace or window.workspace
            del workspace[WORKSPACE_VISIBILITY_SNAPSHOT_KEY]
        except (KeyError, ReferenceError, RuntimeError):
            pass
    SESSION.clear_default_object_visibility()
    return tuple(warnings)


def _session_control_context(window: Any) -> dict[str, Any] | None:
    """활성 Session의 상단 3D View WINDOW Context를 다시 탐색합니다."""

    if not is_session_active_for_window(window):
        return None
    area = next(
        (
            candidate
            for candidate in window.screen.areas
            if candidate.type == "VIEW_3D"
            and _pointer(candidate) == SESSION.control_area_pointer
        ),
        None,
    )
    if area is None:
        return None
    region = next((item for item in area.regions if item.type == "WINDOW"), None)
    if region is None:
        return None
    return {
        "window": window,
        "screen": window.screen,
        "area": area,
        "region": region,
        "scene": window.scene,
        "view_layer": window.view_layer,
    }


def _managed_point_object(view_layer: Any, dataset_uuid: str):
    """현재 Dataset의 관리 Point Mesh를 View Layer에서 찾습니다."""

    return next(
        (
            obj
            for obj in view_layer.objects
            if obj.get(OWNER_KEY) == OWNER_TOKEN
            and obj.get(RECORD_KIND_KEY) == "POINT_MESH"
            and obj.get(DATASET_UUID_KEY) == dataset_uuid
            and obj.type == "MESH"
            and len(obj.data.vertices) > 0
        ),
        None,
    )


def frame_imported_points(dataset_uuid: str) -> str | None:
    """활성 Modifier View에서 관리 Point 범위만 선택해 Frame All을 수행합니다."""

    window = session_target_window()
    override = _session_control_context(window)
    if window is None or override is None:
        return None
    view_layer = window.view_layer
    point_object = _managed_point_object(view_layer, dataset_uuid)
    if point_object is None:
        return None
    selected_objects = tuple(obj for obj in view_layer.objects if obj.select_get(view_layer=view_layer))
    active_object = view_layer.objects.active
    point_hide_select = bool(point_object.hide_select)
    try:
        for obj in selected_objects:
            obj.select_set(False, view_layer=view_layer)
        point_object.hide_select = False
        point_object.select_set(True, view_layer=view_layer)
        view_layer.objects.active = point_object
        with bpy.context.temp_override(**override):
            result = bpy.ops.view3d.view_selected(use_all_regions=False)
        if "FINISHED" not in result:
            return "COLMAP Point 범위의 Frame All을 완료하지 못했습니다."
        override["area"].spaces.active.region_3d.view_perspective = (
            MODIFIER_VIEW_PROJECTION
        )
        override["area"].tag_redraw()
        return None
    except (AttributeError, ReferenceError, RuntimeError) as error:
        return f"COLMAP Point Frame All 실패: {error}"
    finally:
        try:
            point_object.select_set(False, view_layer=view_layer)
            point_object.hide_select = point_hide_select
            for obj in selected_objects:
                obj.select_set(True, view_layer=view_layer)
            view_layer.objects.active = active_object
        except (AttributeError, ReferenceError, RuntimeError):
            pass


__all__ = [
    "DEFAULT_STARTUP_OBJECTS",
    "MODIFIER_VIEW_AZIMUTH_DEGREES",
    "MODIFIER_VIEW_ELEVATION_DEGREES",
    "MODIFIER_VIEW_PROJECTION",
    "VISIBILITY_SNAPSHOT_SCHEMA_VERSION",
    "frame_imported_points",
    "hide_default_startup_objects",
    "normalize_modifier_viewport",
    "restore_default_startup_objects",
    "set_modifier_perspective_view",
]
