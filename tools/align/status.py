"""COLMAP Align 메시지를 Blender 표준 상태 표시줄에 전달합니다."""

from __future__ import annotations

from typing import Any


def set_workspace_status_text(workspace: Any | None, text: str | None) -> bool:
    """주어진 작업공간의 Blender 하단 상태 표시줄 문구를 설정합니다."""
    if workspace is None:
        return False
    try:
        workspace.status_text_set(text)
    except (AttributeError, ReferenceError, RuntimeError, TypeError):
        return False
    return True


def set_window_status_text(window: Any | None, text: str | None) -> bool:
    """주어진 창이 표시 중인 작업공간의 상태 표시줄 문구를 설정합니다."""
    return set_workspace_status_text(getattr(window, "workspace", None), text)
