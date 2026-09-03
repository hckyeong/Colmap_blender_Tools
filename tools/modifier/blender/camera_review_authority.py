"""Modifier Camera 세션 권한을 Timeline과 Viewport에서 공통 검증합니다."""

from __future__ import annotations


_MARKER_SESSION_TOOL_ID = "modifier"


def modifier_camera_session_is_active(session=None, *, window=None) -> bool:
    """Workspace Session·Window·Scene lease가 모두 일치하는지 확인합니다."""

    try:
        from .modifier_session import (
            SESSION,
            SESSION_ACTIVE,
            is_session_active_for_window,
            is_owned_modifier_workspace,
            session_target_window,
        )
        try:
            from ....shared.blender_runtime.scene_marker_session import (
                scene_marker_session_owned_by_window,
            )
        except ImportError:
            from shared.blender_runtime.scene_marker_session import (
                scene_marker_session_owned_by_window,
            )

        active_window = window or session_target_window()
        scene = getattr(session, "scene", None) if session is not None else getattr(
            active_window,
            "scene",
            None,
        )
        return bool(
            SESSION.phase == SESSION_ACTIVE
            and active_window is not None
            and scene is not None
            and is_session_active_for_window(active_window)
            and is_owned_modifier_workspace(getattr(active_window, "workspace", None))
            and getattr(active_window, "scene", None) is scene
            and scene_marker_session_owned_by_window(
                scene,
                _MARKER_SESSION_TOOL_ID,
                active_window,
            )
        )
    except (AttributeError, ImportError, ReferenceError, RuntimeError, TypeError):
        return False


def set_modifier_scene_camera(session, camera) -> None:
    """검증된 활성 Modifier Camera 세션에서만 Scene Camera를 기록합니다."""

    if not modifier_camera_session_is_active(session):
        raise RuntimeError("활성 Modifier Camera 세션이 아닙니다.")
    session.scene.camera = camera


__all__ = (
    "modifier_camera_session_is_active",
    "set_modifier_scene_camera",
)
