"""Scene Timeline Marker를 사용하는 도구의 단일 소유권을 관리합니다."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


class SceneMarkerSessionError(RuntimeError):
    """Scene Marker 세션 소유권 계약 위반을 나타냅니다."""


@dataclass(frozen=True, slots=True)
class _SceneMarkerLease:
    """Blender ID 참조 없이 Scene Marker 세션의 표시 정보를 보관합니다."""

    scene_pointer: int
    tool_id: str
    window_pointer: int
    scene_label: str
    window_label: str


_SCENE_MARKER_SESSIONS: dict[int, _SceneMarkerLease] = {}


def _pointer(value: Any) -> int:
    """삭제된 RNA를 포함한 객체의 Pointer를 안전하게 정수로 변환합니다."""

    if value is None:
        return 0
    try:
        as_pointer = getattr(value, "as_pointer", None)
    except (AttributeError, ReferenceError, RuntimeError):
        return 0
    if callable(as_pointer):
        try:
            return int(as_pointer())
        except (ReferenceError, RuntimeError, TypeError, ValueError):
            return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _label(value: Any, fallback: str) -> str:
    """삭제된 RNA에서도 예외 없이 표시용 이름을 생성합니다."""

    try:
        name = str(getattr(value, "name", "")).strip()
    except (AttributeError, ReferenceError, RuntimeError, TypeError, ValueError):
        name = ""
    return name or fallback


def _tool_id(tool_id: str) -> str:
    """도구 ID를 정규화하고 빈 식별자를 차단합니다."""

    normalized = str(tool_id).strip()
    if not normalized:
        raise SceneMarkerSessionError("Scene Marker 세션 도구 ID는 비어 있을 수 없습니다.")
    return normalized


def _scene_pointer(scene: Any) -> int:
    """유효한 Scene Pointer를 반환하고 삭제된 Scene 요청을 차단합니다."""

    scene_pointer = _pointer(scene)
    if not scene_pointer:
        raise SceneMarkerSessionError("Scene Marker 세션을 사용할 유효한 Blender Scene이 없습니다.")
    return scene_pointer


def _window_pointer(window: Any) -> int:
    """Window가 제공되었다면 유효한 Pointer인지 확인합니다."""

    if window is None:
        return 0
    window_pointer = _pointer(window)
    if not window_pointer:
        raise SceneMarkerSessionError("Scene Marker 세션을 사용할 Blender Window가 유효하지 않습니다.")
    return window_pointer


def acquire_scene_marker_session(
    scene: Any,
    tool_id: str,
    window: Any = None,
) -> _SceneMarkerLease:
    """Scene Timeline Marker 단일 소유권을 획득하고 멱등 Lease를 반환합니다."""

    normalized_tool_id = _tool_id(tool_id)
    scene_pointer = _scene_pointer(scene)
    window_pointer = _window_pointer(window)
    existing = _SCENE_MARKER_SESSIONS.get(scene_pointer)
    if existing is not None:
        if existing.tool_id != normalized_tool_id:
            raise SceneMarkerSessionError(
                f"Scene '{existing.scene_label}'의 Timeline Marker는 이미 "
                f"'{existing.tool_id}' 도구가 사용 중이므로 "
                f"'{normalized_tool_id}' 도구가 동시에 사용할 수 없습니다."
            )
        if existing.window_pointer != window_pointer:
            raise SceneMarkerSessionError(
                f"Scene '{existing.scene_label}'의 Timeline Marker는 이미 "
                f"'{normalized_tool_id}' 도구의 다른 Blender Window가 사용 중입니다."
            )
        return existing

    lease = _SceneMarkerLease(
        scene_pointer=scene_pointer,
        tool_id=normalized_tool_id,
        window_pointer=window_pointer,
        scene_label=_label(scene, f"Scene Pointer {scene_pointer}"),
        window_label=(
            _label(window, f"Window Pointer {window_pointer}")
            if window is not None
            else "Window 없음"
        ),
    )
    _SCENE_MARKER_SESSIONS[scene_pointer] = lease
    return lease


def release_scene_marker_session(scene: Any, tool_id: str) -> None:
    """정확한 소유 도구만 Scene Marker 세션을 해제하며 미소유 해제는 허용합니다."""

    normalized_tool_id = _tool_id(tool_id)
    scene_pointer = _pointer(scene)
    if not scene_pointer:
        return
    existing = _SCENE_MARKER_SESSIONS.get(scene_pointer)
    if existing is None:
        return
    if existing.tool_id != normalized_tool_id:
        raise SceneMarkerSessionError(
            f"Scene '{existing.scene_label}'의 Timeline Marker 세션은 "
            f"'{existing.tool_id}' 도구 소유이므로 '{normalized_tool_id}' 도구가 해제할 수 없습니다."
        )
    _SCENE_MARKER_SESSIONS.pop(scene_pointer, None)


def scene_marker_session_owned_by(scene: Any, tool_id: str) -> bool:
    """Scene Marker 세션이 지정한 도구 소유인지 안전하게 확인합니다."""

    normalized_tool_id = _tool_id(tool_id)
    scene_pointer = _pointer(scene)
    if not scene_pointer:
        return False
    existing = _SCENE_MARKER_SESSIONS.get(scene_pointer)
    return existing is not None and existing.tool_id == normalized_tool_id


def scene_marker_session_owner(scene: Any) -> str:
    """Scene Marker 세션 소유 도구 ID를 반환하고 미소유이면 빈 문자열을 반환합니다."""

    scene_pointer = _pointer(scene)
    if not scene_pointer:
        return ""
    existing = _SCENE_MARKER_SESSIONS.get(scene_pointer)
    return existing.tool_id if existing is not None else ""


def clear_scene_marker_sessions() -> None:
    """파일 Load 전이나 Extension 해제 시 모든 Scene Marker Lease를 폐기합니다."""

    _SCENE_MARKER_SESSIONS.clear()


__all__ = [
    "SceneMarkerSessionError",
    "acquire_scene_marker_session",
    "clear_scene_marker_sessions",
    "release_scene_marker_session",
    "scene_marker_session_owned_by",
    "scene_marker_session_owner",
]
