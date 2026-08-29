"""Blender GUI에서 전용 Paint 작업공간의 화면 배치를 검증합니다."""

from __future__ import annotations

import importlib
import json
import os
from pathlib import Path
import sys
import traceback

import bpy


if os.environ.get("PP_TEST_INSTALLED") == "1":
    addon = importlib.import_module("bl_ext.user_default.colmap_tools")
else:
    PROJECT_PARENT = Path(__file__).resolve().parents[2]
    if str(PROJECT_PARENT) not in sys.path:
        sys.path.insert(0, str(PROJECT_PARENT))
    addon = importlib.import_module("Blender_ProjectionPainting_addon")

paint_workspace = importlib.import_module(f"{addon.__name__}.blender.paint_workspace")
LAYOUT_STAGE_KEY = paint_workspace.LAYOUT_STAGE_KEY
configure_paint_workspace = paint_workspace.configure_paint_workspace
ensure_paint_workspace = paint_workspace.ensure_paint_workspace
remove_paint_workspace = paint_workspace.remove_paint_workspace


RESULT_PATH = Path(r"C:\tmp\projection_paint_workspace_layout.json")
STATE = {
    "step": 0,
    "original_workspace": "",
    "original_object_mode": "",
    "first_pointer": 0,
    "second_pointer": 0,
    "base_screen_pointers": [],
    "first_screen_pointers": [],
    "second_screen_pointers": [],
    "delete_attempts": 0,
    "second_delete_attempts": 0,
    "payload": {},
}


def _write_result(payload: dict) -> None:
    """GUI 검증 결과를 UTF-8 JSON 파일로 기록합니다."""
    RESULT_PATH.parent.mkdir(parents=True, exist_ok=True)
    RESULT_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _workspace_exists(pointer: int) -> bool:
    """저장한 Pointer와 일치하는 Workspace가 아직 남아 있는지 반환합니다."""

    return any(workspace.as_pointer() == int(pointer) for workspace in bpy.data.workspaces)


def _screen_exists(pointer: int) -> bool:
    """저장한 Pointer와 일치하는 Screen이 아직 남아 있는지 반환합니다."""

    return any(screen.as_pointer() == int(pointer) for screen in bpy.data.screens)


def _layout_payload(window) -> dict:
    """현재 Paint Workspace의 네 편집기 배치를 검증용 Mapping으로 반환합니다."""

    viewport = next(area for area in window.screen.areas if area.type == "VIEW_3D")
    timeline = next(
        area
        for area in window.screen.areas
        if area.type == "DOPESHEET_EDITOR" and area.ui_type == "TIMELINE"
    )
    outliner = next(area for area in window.screen.areas if area.type == "OUTLINER")
    properties = next(area for area in window.screen.areas if area.type == "PROPERTIES")
    areas = tuple(window.screen.areas)
    payload = {
        "paint_workspace": window.workspace.name,
        "paint_workspace_owned": (
            window.workspace.get("pp_record_kind") == "PAINT_WORKSPACE"
        ),
        "paint_workspace_object_mode": window.workspace.object_mode,
        "layout_stage": str(window.workspace.get(LAYOUT_STAGE_KEY, "")),
        "area_count": len(areas),
        "area_types": sorted(area.type for area in areas),
        "timeline_ui_type": timeline.ui_type,
        "properties_context": properties.spaces.active.context,
        "outliner_display_mode": outliner.spaces.active.display_mode,
        "viewport": {
            "x": viewport.x,
            "y": viewport.y,
            "width": viewport.width,
            "height": viewport.height,
        },
        "timeline": {
            "x": timeline.x,
            "y": timeline.y,
            "width": timeline.width,
            "height": timeline.height,
        },
        "outliner": {
            "x": outliner.x,
            "y": outliner.y,
            "width": outliner.width,
            "height": outliner.height,
        },
        "properties": {
            "x": properties.x,
            "y": properties.y,
            "width": properties.width,
            "height": properties.height,
        },
    }
    if payload["area_types"] != [
        "DOPESHEET_EDITOR",
        "OUTLINER",
        "PROPERTIES",
        "VIEW_3D",
    ]:
        raise RuntimeError("Paint 화면의 네 편집기 구성이 일치하지 않습니다.")
    if payload["paint_workspace_object_mode"] != "TEXTURE_PAINT":
        raise RuntimeError("Paint Workspace의 기본 Object Mode가 일치하지 않습니다.")
    if payload["layout_stage"] != "COMPLETE":
        raise RuntimeError("Paint Workspace 화면 구성이 완료되지 않았습니다.")
    if payload["timeline_ui_type"] != "TIMELINE":
        raise RuntimeError("하단 편집기가 Timeline이 아닙니다.")
    if payload["properties_context"] != "OBJECT":
        raise RuntimeError("Properties가 Object Context가 아닙니다.")
    if payload["outliner_display_mode"] != "VIEW_LAYER":
        raise RuntimeError("Outliner가 View Layer 표시가 아닙니다.")
    if timeline.y >= viewport.y or timeline.height >= viewport.height:
        raise RuntimeError("Timeline이 화면 하단의 작은 영역이 아닙니다.")
    if abs(timeline.width - viewport.width) > 2:
        raise RuntimeError("Timeline이 3D Viewport와 같은 가로 폭이 아닙니다.")
    if outliner.x <= viewport.x or properties.x <= viewport.x:
        raise RuntimeError("Outliner와 Properties가 오른쪽 영역에 있지 않습니다.")
    if outliner.y <= properties.y or outliner.height >= properties.height:
        raise RuntimeError("Outliner가 Properties 위의 작은 영역이 아닙니다.")
    if abs(outliner.width - properties.width) > 2:
        raise RuntimeError("Outliner와 Properties의 가로 폭이 다릅니다.")
    return payload


def _run() -> float | None:
    """GUI Event Loop에서 Paint Workspace 생성·삭제·재생성을 순서대로 검증합니다."""
    try:
        if STATE["step"] == 0:
            addon.register()
            STATE["original_workspace"] = bpy.context.window.workspace.name
            STATE["original_object_mode"] = bpy.context.window.workspace.object_mode
            STATE["base_screen_pointers"] = [
                screen.as_pointer() for screen in bpy.data.screens
            ]
            ensure_paint_workspace(bpy.context)
            STATE["step"] = 1
            return 0.1
        if STATE["step"] == 1:
            if not configure_paint_workspace(bpy.context):
                return 0.1
            STATE["step"] = 2
            return 0.1
        if STATE["step"] == 2:
            window = bpy.context.window
            first_layout = _layout_payload(window)
            STATE["first_pointer"] = window.workspace.as_pointer()
            STATE["first_screen_pointers"] = [
                screen.as_pointer() for screen in window.workspace.screens
            ]
            STATE["payload"] = {"first_layout": first_layout}
            window.workspace = bpy.data.workspaces[STATE["original_workspace"]]
            remove_paint_workspace(
                bpy.context.scene,
                window=window,
                workspace=next(
                    workspace
                    for workspace in bpy.data.workspaces
                    if workspace.as_pointer() == STATE["first_pointer"]
                ),
            )
            STATE["step"] = 3
            return 0.1
        if STATE["step"] == 3:
            if _workspace_exists(STATE["first_pointer"]):
                STATE["delete_attempts"] += 1
                if STATE["delete_attempts"] >= 100:
                    raise RuntimeError(
                        "첫 Paint Workspace 삭제 시간이 초과되었습니다: "
                        f"current={bpy.context.window.workspace.name}, "
                        f"workspaces={[workspace.name for workspace in bpy.data.workspaces]}"
                    )
                return 0.1
            if bpy.context.scene.projection_paint.paint_workspace is not None:
                raise RuntimeError("종료 뒤 Paint Workspace Pointer가 남았습니다.")
            if any(
                _screen_exists(pointer)
                for pointer in STATE["first_screen_pointers"]
            ):
                raise RuntimeError("첫 Paint Workspace의 관리 Screen이 남았습니다.")
            ensure_paint_workspace(bpy.context)
            STATE["step"] = 4
            return 0.1
        if STATE["step"] == 4:
            if not configure_paint_workspace(bpy.context):
                return 0.1
            STATE["step"] = 5
            return 0.1
        if STATE["step"] == 5:
            window = bpy.context.window
            second_layout = _layout_payload(window)
            STATE["second_pointer"] = window.workspace.as_pointer()
            STATE["second_screen_pointers"] = [
                screen.as_pointer() for screen in window.workspace.screens
            ]
            if STATE["second_pointer"] == STATE["first_pointer"]:
                raise RuntimeError("두 번째 진입이 이전 Paint Workspace를 재사용했습니다.")
            STATE["payload"]["second_layout"] = second_layout
            window.workspace = bpy.data.workspaces[STATE["original_workspace"]]
            STATE["step"] = 6
            return 0.1
        window = bpy.context.window
        if STATE["step"] == 6:
            if window.workspace.name != STATE["original_workspace"]:
                return 0.1
            second_workspace = next(
                workspace
                for workspace in bpy.data.workspaces
                if workspace.as_pointer() == STATE["second_pointer"]
            )
            if not remove_paint_workspace(
                bpy.context.scene,
                window=window,
                workspace=second_workspace,
            ):
                raise RuntimeError("비활성 Paint Workspace 정리에 실패했습니다.")
            STATE["step"] = 7
            return 0.1
        if _workspace_exists(STATE["second_pointer"]):
            STATE["second_delete_attempts"] += 1
            if STATE["second_delete_attempts"] >= 100:
                raise RuntimeError("비활성 Paint Workspace 삭제 시간이 초과되었습니다.")
            return 0.1
        if any(
            _screen_exists(pointer)
            for pointer in STATE["second_screen_pointers"]
        ):
            STATE["second_delete_attempts"] += 1
            if STATE["second_delete_attempts"] < 100:
                return 0.1
            remaining = [
                (screen.name, int(screen.users), screen.as_pointer())
                for screen in bpy.data.screens
                if screen.as_pointer() in STATE["second_screen_pointers"]
            ]
            raise RuntimeError(
                "비활성 Paint Workspace의 관리 Screen이 남았습니다: "
                f"remaining={remaining}"
            )
        if {
            screen.as_pointer() for screen in bpy.data.screens
        } != set(STATE["base_screen_pointers"]):
            raise RuntimeError("Paint Workspace 종료 뒤 Screen 구성이 원상 복구되지 않았습니다.")
        if bpy.data.workspaces[STATE["original_workspace"]].object_mode != STATE[
            "original_object_mode"
        ]:
            raise RuntimeError("원래 Workspace의 Object Mode가 변경되었습니다.")
        _write_result(
            {
                "success": True,
                "original_workspace": STATE["original_workspace"],
                "original_object_mode": STATE["original_object_mode"],
                "first_pointer": STATE["first_pointer"],
                "second_pointer": STATE["second_pointer"],
                "first_deleted": True,
                "second_inactive_deleted": True,
                "managed_screens_deleted": True,
                "fresh_workspace_created": True,
                **STATE["payload"],
            }
        )
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
