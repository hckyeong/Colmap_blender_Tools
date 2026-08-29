"""저장된 실제 데이터 파일에서 Paint 진입 직후 화면 상태를 검증합니다."""

from __future__ import annotations

import json
from pathlib import Path
import traceback

import bpy


RESULT_PATH = Path(r"C:\tmp\projection_paint_entry_visibility.json")
STATE = {"entered": False, "waits": 0, "cleaned": False}


def _viewport_parts():
    """현재 Window의 첫 3D Viewport와 Window Region을 반환합니다."""

    window = bpy.context.window
    if window is None:
        raise RuntimeError("검증할 Blender Window가 없습니다.")
    area = next(
        (candidate for candidate in window.screen.areas if candidate.type == "VIEW_3D"),
        None,
    )
    if area is None:
        raise RuntimeError("검증할 3D Viewport가 없습니다.")
    region = next(
        (candidate for candidate in area.regions if candidate.type == "WINDOW"),
        None,
    )
    if region is None:
        raise RuntimeError("검증할 3D Viewport Window Region이 없습니다.")
    return window, area, region, area.spaces.active


def _write_result(payload: dict) -> None:
    """검증 결과를 UTF-8 JSON 파일로 기록합니다."""

    RESULT_PATH.parent.mkdir(parents=True, exist_ok=True)
    RESULT_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _enter_paint() -> None:
    """저장된 Target으로 Paint 진입을 시작합니다."""

    settings = bpy.context.scene.projection_paint
    target = settings.paint_target
    if target is None:
        raise RuntimeError("저장된 Paint Target이 없습니다.")
    result = bpy.ops.projection_paint.toggle_paint()
    if result != {"FINISHED"}:
        raise RuntimeError(f"Paint 진입이 실패했습니다: {result}, {settings.status}")
    STATE["entered"] = True


def _verify_entry() -> bool:
    """화면 전환 뒤 Target·Camera View·Stencil 연결을 함께 검사합니다."""

    settings = bpy.context.scene.projection_paint
    target = settings.paint_target
    window, area, region, space = _viewport_parts()
    from bl_ext.user_default.colmap_tools.tools.projection_paint.blender.paint_runtime import (
        active_paint_runtime,
    )

    runtime = active_paint_runtime()
    if runtime is None:
        raise RuntimeError("Paint Runtime이 시작되지 않았습니다.")
    if settings.paint_state != "ACTIVE" and STATE["waits"] < 20:
        STATE["waits"] += 1
        return False
    brush = runtime.stencil.brush
    texture = runtime.stencil.managed_texture
    slot = brush.texture_slot
    brush_properties = tuple(
        prop.identifier
        for prop in brush.bl_rna.properties
        if any(token in prop.identifier for token in ("color", "blend", "texture"))
    )
    areas = tuple(window.screen.areas)
    timeline_area = next(
        (
            candidate
            for candidate in areas
            if candidate.type == "DOPESHEET_EDITOR"
            and candidate.ui_type == "TIMELINE"
        ),
        None,
    )
    outliner_area = next(
        (candidate for candidate in areas if candidate.type == "OUTLINER"),
        None,
    )
    properties_area = next(
        (candidate for candidate in areas if candidate.type == "PROPERTIES"),
        None,
    )
    point_objects = tuple(
        obj
        for obj in bpy.context.scene.objects
        if obj.get("pp_record_kind") == "POINT_MESH"
        and obj.get("pp_dataset_uuid") == settings.dataset_uuid
    )
    payload = {
        "success": True,
        "workspace": window.workspace.name,
        "workspace_owned": window.workspace.get("pp_record_kind") == "PAINT_WORKSPACE",
        "area_types": sorted(candidate.type for candidate in areas),
        "timeline_ui_types": [
            candidate.ui_type
            for candidate in areas
            if candidate.type == "DOPESHEET_EDITOR"
        ],
        "outliner_count": sum(
            candidate.type == "OUTLINER" for candidate in areas
        ),
        "properties_count": sum(
            candidate.type == "PROPERTIES" for candidate in areas
        ),
        "properties_context": (
            properties_area.spaces.active.context
            if properties_area is not None
            else ""
        ),
        "right_panel_stack": (
            outliner_area is not None
            and properties_area is not None
            and outliner_area.x > area.x
            and properties_area.x > area.x
            and outliner_area.y > properties_area.y
            and outliner_area.height < properties_area.height
        ),
        "area_count": len(areas),
        "timeline_below_viewport": (
            timeline_area is not None
            and timeline_area.y < area.y
            and timeline_area.height < area.height
            and abs(timeline_area.width - area.width) <= 2
        ),
        "point_hidden": all(
            obj.hide_get(view_layer=window.view_layer) for obj in point_objects
        ),
        "view_layer": window.view_layer.name,
        "area_type": area.type,
        "region_type": region.type,
        "target_name": target.name,
        "target_mode": target.mode,
        "target_selected": target.select_get(view_layer=window.view_layer),
        "target_active": window.view_layer.objects.active is target,
        "target_hide_viewport": bool(target.hide_viewport),
        "target_hide_get": bool(target.hide_get(view_layer=window.view_layer)),
        "target_visible": bool(
            target.visible_get(view_layer=window.view_layer, viewport=space)
        ),
        "target_visible_in_viewport": bool(target.visible_in_viewport_get(space)),
        "camera_view": space.region_3d.view_perspective,
        "shading": space.shading.type,
        "scene_camera_matches": bpy.context.scene.camera is runtime.stencil.camera,
        "stencil_image_id": runtime.stencil.source_image_id,
        "stencil_mapping": runtime.stencil.brush.texture_slot.map_mode,
        "stencil_connected": (
            brush.texture is texture
        ),
        "brush_properties": brush_properties,
        "brush_color": (
            list(brush.color) if hasattr(brush, "color") else None
        ),
        "brush_blend": getattr(brush, "blend", None),
        "brush_color_type": getattr(brush, "color_type", None),
        "texture_slot_properties": {
            prop.identifier: getattr(slot, prop.identifier)
            for prop in slot.bl_rna.properties
            if prop.type in {"BOOLEAN", "ENUM", "FLOAT", "INT"}
            and not bool(getattr(prop, "is_array", False))
        },
        "texture_use_color_ramp": bool(texture.use_color_ramp),
        "texture_intensity": float(texture.intensity),
        "texture_color_factors": [
            float(texture.factor_red),
            float(texture.factor_green),
            float(texture.factor_blue),
        ],
        "source_colorspace": runtime.stencil.source_image.colorspace_settings.name,
    }
    if not payload["target_visible"]:
        raise RuntimeError("Texture Paint Viewport에서 Paint Target이 보이지 않습니다.")
    if (
        payload["area_types"]
        != ["DOPESHEET_EDITOR", "OUTLINER", "PROPERTIES", "VIEW_3D"]
        or payload["timeline_ui_types"] != ["TIMELINE"]
        or payload["properties_context"] != "OBJECT"
    ):
        raise RuntimeError("Paint 화면의 네 편집기 구성이 일치하지 않습니다.")
    if not payload["timeline_below_viewport"]:
        raise RuntimeError("Timeline이 3D Viewport 아래의 가로 영역이 아닙니다.")
    if not payload["right_panel_stack"]:
        raise RuntimeError("Outliner와 Properties가 오른쪽 위아래 영역이 아닙니다.")
    if point_objects and not payload["point_hidden"]:
        raise RuntimeError("현재 Dataset의 COLMAP Point 눈 표시가 꺼지지 않았습니다.")
    if payload["camera_view"] != "CAMERA":
        raise RuntimeError("Texture Paint Viewport가 Camera View가 아닙니다.")
    if payload["shading"] not in {"MATERIAL", "RENDERED"}:
        raise RuntimeError("Texture Paint Viewport에 Material Preview가 적용되지 않았습니다.")
    if not payload["stencil_connected"]:
        raise RuntimeError("현재 Frame의 Stencil Texture가 연결되지 않았습니다.")
    _write_result(payload)
    bpy.ops.wm.quit_blender()
    return True


def _run() -> float | None:
    """GUI Event Loop에서 Paint 진입 검증을 실행합니다."""

    try:
        if not STATE["cleaned"]:
            settings = bpy.context.scene.projection_paint
            if bool(settings.paint_enabled):
                result = bpy.ops.projection_paint.toggle_paint()
                if result != {"FINISHED"}:
                    raise RuntimeError(f"기존 Paint 종료가 실패했습니다: {result}")
                STATE["cleaned"] = True
                return 0.1
            STATE["cleaned"] = True
        if not STATE["entered"]:
            _enter_paint()
            return 0.1
        if not _verify_entry():
            return 0.1
    except Exception as exc:
        _write_result(
            {
                "success": False,
                "error": str(exc),
                "traceback": traceback.format_exc(),
            }
        )
        bpy.ops.wm.quit_blender()
    return None


bpy.app.timers.register(_run, first_interval=0.5)
