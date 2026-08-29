"""COLMAP Align 세션의 Space 기반 뷰포트 탐색 상태와 실행을 공유합니다."""

from __future__ import annotations

from typing import Any

import bpy

from ..viewport.picking import find_viewport_under_mouse


def _event_follows_space_release(event: Any) -> bool:
    """현재 이벤트 직전에 소비된 Space 해제가 있었는지 확인합니다."""
    return bool(
        getattr(event, "type_prev", "") == "SPACE"
        and getattr(event, "value_prev", "") == "RELEASE"
    )


def synchronize_space_navigation_state(event: Any, is_active: bool) -> bool:
    """현재·직전 이벤트로 Space 탐색 상태를 갱신합니다."""
    if getattr(event, "type", "") == "WINDOW_DEACTIVATE" or _event_follows_space_release(event):
        return False
    if getattr(event, "type", "") != "SPACE":
        return is_active
    if getattr(event, "value", "") == "PRESS":
        return True
    if getattr(event, "value", "") == "RELEASE":
        return False
    return is_active


def invoke_space_pan_under_mouse(context: Any, event: Any) -> bool:
    """마우스 아래 역할 뷰포트에서 Blender의 기본 화면 이동을 시작합니다."""
    window = getattr(context, "window", None)
    if window is None:
        return False
    viewport = find_viewport_under_mouse(
        window,
        int(getattr(event, "mouse_x", -1)),
        int(getattr(event, "mouse_y", -1)),
    )
    if viewport is None:
        return False
    area, region, _role = viewport
    with bpy.context.temp_override(
        window=window,
        screen=window.screen,
        area=area,
        region=region,
    ):
        if not bpy.ops.view3d.move.poll():
            return False
        result = bpy.ops.view3d.move("INVOKE_DEFAULT")
    return "RUNNING_MODAL" in result
