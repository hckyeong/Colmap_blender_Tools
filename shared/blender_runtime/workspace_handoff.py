"""독립 COLMAP 도구 사이의 Workspace 종료·진입 순서를 조정합니다."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

import bpy
from bpy.app.handlers import persistent

from .scene_marker_session import clear_scene_marker_sessions
from .workspace_factory import (
    STATE_READY,
    WORKSPACE_STATE_KEY,
    WORKSPACE_TOOL_ID_KEY,
    owned_uuid_of,
    pointer,
    source_workspace_from_metadata,
    workspace_is_used_by_other_window,
)


ACTIVATION_DEFERRED = "DEFERRED"
ACTIVATION_STARTED = "STARTED"
_TICK_INTERVAL_SECONDS = 0.05
_MAX_WAIT_ATTEMPTS = 400


class WorkspaceHandoffError(RuntimeError):
    """도구 종료나 다음 Workspace 진입 계약을 완료하지 못했음을 나타냅니다."""


@dataclass(frozen=True, slots=True)
class WorkspaceHandoffAdapter:
    """한 독립 도구가 공용 전환 조정기에 제공하는 최소 생명주기 계약입니다."""

    tool_id: str
    label: str
    start: Callable[[Any], None]
    is_engaged: Callable[[Any], bool]
    request_exit: Callable[[Any, Any], None]
    is_settled: Callable[[Any], bool]
    report_error: Callable[[str], None]


@dataclass(slots=True)
class _PendingHandoff:
    """한 Window에서 이전 도구 종료와 사용자가 선택한 Workspace 복귀를 기다립니다."""

    window_pointer: int
    target_tool_id: str = ""
    destination_workspace_pointer: int = 0
    phase: str = "REQUEST_EXIT"
    attempts: int = 0
    exiting_tool_id: str = ""
    departed_tool_id: str = ""
    last_exiting_tool_id: str = ""
    cleanup_workspace_pointer: int = 0
    cleanup_restore_pointer: int = 0
    cleanup_phase: str = ""


_ADAPTERS: dict[str, WorkspaceHandoffAdapter] = {}
_PENDING_HANDOFFS: dict[int, _PendingHandoff] = {}
_LAST_WORKSPACE_POINTERS: dict[int, int] = {}
_MSGBUS_OWNER = object()
_RUNTIME_REGISTERED = False
_HANDOFF_TICK_RUNNING = False


def workspace_tool_id(workspace: Any) -> str:
    """공용 소유권이 유효한 Workspace의 도구 ID만 반환합니다."""

    if not owned_uuid_of(workspace):
        return ""
    try:
        value = workspace.get(WORKSPACE_TOOL_ID_KEY, "")
    except (AttributeError, ReferenceError, RuntimeError, TypeError):
        return ""
    return str(value or "")


def _window_by_pointer(window_pointer: int) -> Any | None:
    """현재 WindowManager에서 저장한 Pointer와 일치하는 Window를 반환합니다."""

    manager = getattr(bpy.context, "window_manager", None)
    if manager is None:
        return None
    return next(
        (
            window
            for window in manager.windows
            if pointer(window) == int(window_pointer)
        ),
        None,
    )


def _workspace_by_pointer(workspace_pointer: int) -> Any | None:
    """현재 Main에서 저장한 Pointer와 일치하는 Workspace를 반환합니다."""

    return next(
        (
            workspace
            for workspace in bpy.data.workspaces
            if pointer(workspace) == int(workspace_pointer)
        ),
        None,
    )


def _target_stale_workspaces(target_tool_id: str) -> tuple[Any, ...]:
    """대상 도구의 공용 소유 Workspace 중 READY가 아닌 잔여본을 반환합니다."""

    return tuple(
        workspace
        for workspace in bpy.data.workspaces
        if workspace_tool_id(workspace) == str(target_tool_id)
        and workspace.get(WORKSPACE_STATE_KEY) != STATE_READY
    )


def _invoke_active_workspace_delete(window: Any) -> bool:
    """현재 Window에서 공식 Blender Workspace 삭제 연산을 실행합니다."""

    try:
        with bpy.context.temp_override(window=window):
            result = bpy.ops.workspace.delete()
    except (AttributeError, ReferenceError, RuntimeError, TypeError):
        return False
    return "FINISHED" in result


def _advance_target_cleanup(pending: _PendingHandoff, window: Any) -> bool:
    """비활성 FAILED·CREATING 대상 Workspace를 활성화·삭제·복귀 Tick으로 정리합니다."""

    if not pending.cleanup_phase:
        target_adapter = _ADAPTERS.get(pending.target_tool_id)
        if target_adapter is not None and _adapter_is_engaged(target_adapter, window):
            return False
        stale_workspaces = _target_stale_workspaces(pending.target_tool_id)
        if not stale_workspaces:
            return False
        workspace = stale_workspaces[0]
        if workspace_is_used_by_other_window(workspace, window):
            raise WorkspaceHandoffError(
                "준비가 완료되지 않은 대상 Workspace를 다른 Blender Window가 사용 중입니다."
            )
        restore_workspace = getattr(window, "workspace", None)
        if restore_workspace is workspace:
            restore_workspace = source_workspace_from_metadata(workspace)
        if restore_workspace is None or pointer(restore_workspace) == pointer(workspace):
            raise WorkspaceHandoffError(
                "실패 Workspace 삭제 뒤 복귀할 Source Workspace를 찾지 못했습니다."
            )
        pending.cleanup_workspace_pointer = pointer(workspace)
        pending.cleanup_restore_pointer = pointer(restore_workspace)
        pending.cleanup_phase = "ACTIVATE"
        pending.attempts = 0

    workspace = _workspace_by_pointer(pending.cleanup_workspace_pointer)
    restore_workspace = _workspace_by_pointer(pending.cleanup_restore_pointer)
    if pending.cleanup_phase == "ACTIVATE":
        if workspace is None:
            pending.cleanup_phase = "RESTORE"
            return True
        if getattr(window, "workspace", None) is not workspace:
            window.workspace = workspace
            pending.attempts += 1
            if pending.attempts >= _MAX_WAIT_ATTEMPTS:
                raise WorkspaceHandoffError("실패 Workspace 활성화 대기 시간이 초과되었습니다.")
            return True
        if not _invoke_active_workspace_delete(window):
            raise WorkspaceHandoffError("실패 Workspace의 공식 삭제 연산이 거부되었습니다.")
        pending.cleanup_phase = "WAIT_DELETE"
        pending.attempts = 0
        return True
    if pending.cleanup_phase == "WAIT_DELETE":
        if workspace is not None:
            pending.attempts += 1
            if pending.attempts >= _MAX_WAIT_ATTEMPTS:
                raise WorkspaceHandoffError("실패 Workspace 삭제 완료 대기 시간이 초과되었습니다.")
            return True
        pending.cleanup_phase = "RESTORE"
    if pending.cleanup_phase == "RESTORE":
        if restore_workspace is None:
            raise WorkspaceHandoffError("실패 Workspace 삭제 뒤 Source Workspace가 사라졌습니다.")
        if getattr(window, "workspace", None) is not restore_workspace:
            window.workspace = restore_workspace
            pending.attempts += 1
            if pending.attempts >= _MAX_WAIT_ATTEMPTS:
                raise WorkspaceHandoffError("Source Workspace 복귀 대기 시간이 초과되었습니다.")
            return True
        pending.cleanup_workspace_pointer = 0
        pending.cleanup_restore_pointer = 0
        pending.cleanup_phase = ""
        pending.attempts = 0
        return bool(_target_stale_workspaces(pending.target_tool_id))
    raise WorkspaceHandoffError(f"알 수 없는 실패 Workspace 정리 단계입니다: {pending.cleanup_phase}")


def _adapter_is_engaged(adapter: WorkspaceHandoffAdapter, window: Any) -> bool:
    """삭제된 RNA 참조 예외를 전환 실패로 확산하지 않고 도구 점유 상태를 판정합니다."""

    try:
        return bool(adapter.is_engaged(window))
    except (AttributeError, ReferenceError, RuntimeError, TypeError):
        return False


def _blocking_adapters(window: Any, target_tool_id: str) -> tuple[WorkspaceHandoffAdapter, ...]:
    """대상 Workspace 이동 전에 종료되어야 하는 도구 계약을 반환합니다."""

    active_tool_id = workspace_tool_id(getattr(window, "workspace", None))
    blockers = tuple(
        adapter
        for tool_id, adapter in _ADAPTERS.items()
        if tool_id != target_tool_id
        and (
            _adapter_is_engaged(adapter, window)
            or active_tool_id == tool_id
        )
    )
    return tuple(
        sorted(
            blockers,
            key=lambda adapter: 0 if adapter.tool_id == active_tool_id else 1,
        )
    )


def _adapter_is_settled(adapter: WorkspaceHandoffAdapter, window: Any) -> bool:
    """도구별 종료 완료 판정 예외를 거짓으로 바꿔 다음 진입을 차단합니다."""

    try:
        return bool(adapter.is_settled(window))
    except (AttributeError, ReferenceError, RuntimeError, TypeError):
        return False


def _report_handoff_error(
    target_tool_id: str,
    message: str,
    *,
    fallback_tool_id: str = "",
) -> None:
    """대상 또는 종료 중인 도구 상태 영역에 비동기 전환 실패를 기록합니다."""

    adapter = _ADAPTERS.get(target_tool_id) or _ADAPTERS.get(fallback_tool_id)
    if adapter is None:
        return
    try:
        adapter.report_error(str(message))
    except (AttributeError, ReferenceError, RuntimeError, TypeError):
        pass


def _start_adapter(adapter: WorkspaceHandoffAdapter, window: Any) -> None:
    """대상 Window Override에서 독립 도구의 기존 시작 계약을 실행합니다."""

    try:
        with bpy.context.temp_override(window=window):
            adapter.start(bpy.context)
    except Exception as error:
        raise WorkspaceHandoffError(
            f"{adapter.label} Workspace 시작에 실패했습니다: {error}"
        ) from error


def _request_blocker_exit(adapter: WorkspaceHandoffAdapter, window: Any) -> None:
    """대상 Window Override에서 현재 도구의 기존 종료 계약을 한 번 요청합니다."""

    try:
        with bpy.context.temp_override(window=window):
            adapter.request_exit(bpy.context, window)
    except Exception as error:
        raise WorkspaceHandoffError(
            f"{adapter.label} Workspace 자동 종료에 실패했습니다: {error}"
        ) from error


def _retarget_pending_handoff(
    pending: _PendingHandoff,
    target_tool_id: str,
    destination_workspace_pointer: int = 0,
) -> None:
    """대기 중인 전환의 도구 대상 또는 일반 Workspace 목적지를 안전하게 교체합니다."""

    normalized = str(target_tool_id).strip()
    if pending.target_tool_id != normalized:
        pending.cleanup_workspace_pointer = 0
        pending.cleanup_restore_pointer = 0
        pending.cleanup_phase = ""
    pending.target_tool_id = normalized
    pending.destination_workspace_pointer = int(destination_workspace_pointer)


def _remember_current_workspaces() -> None:
    """등록 시점의 Window별 Workspace를 직전 전환 상태로 기록합니다."""

    manager = getattr(bpy.context, "window_manager", None)
    if manager is None:
        return
    _LAST_WORKSPACE_POINTERS.clear()
    for window in tuple(manager.windows):
        window_pointer = pointer(window)
        workspace_pointer = pointer(getattr(window, "workspace", None))
        if window_pointer and workspace_pointer:
            _LAST_WORKSPACE_POINTERS[window_pointer] = workspace_pointer


def _restore_requested_destination(pending: _PendingHandoff, window: Any) -> bool:
    """세션 종료가 Source로 복귀한 뒤 사용자가 선택한 일반 Workspace를 다시 표시합니다."""

    destination = _workspace_by_pointer(pending.destination_workspace_pointer)
    if destination is None:
        raise WorkspaceHandoffError("사용자가 선택한 Workspace가 세션 종료 중 사라졌습니다.")
    if workspace_tool_id(destination):
        raise WorkspaceHandoffError("일반 Workspace 복귀 대상의 소유권이 변경되었습니다.")
    if getattr(window, "workspace", None) is destination:
        return True
    try:
        window.workspace = destination
    except (AttributeError, ReferenceError, RuntimeError, TypeError) as error:
        raise WorkspaceHandoffError(
            f"사용자가 선택한 Workspace로 이동하지 못했습니다: {error}"
        ) from error
    pending.attempts += 1
    if pending.attempts >= _MAX_WAIT_ATTEMPTS:
        raise WorkspaceHandoffError("사용자가 선택한 Workspace 복귀 대기 시간이 초과되었습니다.")
    return False


def _advance_handoffs() -> float | None:
    """대기 중인 도구 종료와 다음 Workspace 진입을 한 단계 전진시킵니다."""

    if not _PENDING_HANDOFFS:
        return None
    for window_pointer, pending in tuple(_PENDING_HANDOFFS.items()):
        window = _window_by_pointer(window_pointer)
        target_adapter = (
            _ADAPTERS.get(pending.target_tool_id)
            if pending.target_tool_id
            else None
        )
        if window is None or (pending.target_tool_id and target_adapter is None):
            _PENDING_HANDOFFS.pop(window_pointer, None)
            continue
        try:
            if pending.target_tool_id and pending.cleanup_phase:
                if _advance_target_cleanup(pending, window):
                    continue
            blockers = _blocking_adapters(window, pending.target_tool_id)
            departed_adapter = _ADAPTERS.get(pending.departed_tool_id)
            if pending.phase == "REQUEST_EXIT" and (
                departed_adapter is not None or blockers
            ):
                blocker = departed_adapter or blockers[0]
                _request_blocker_exit(blocker, window)
                pending.departed_tool_id = ""
                pending.phase = "WAIT_SETTLED"
                pending.attempts = 0
                pending.exiting_tool_id = blocker.tool_id
                pending.last_exiting_tool_id = blocker.tool_id
                continue
            if blockers:
                exiting_adapter = _ADAPTERS.get(pending.exiting_tool_id)
                if exiting_adapter is None or _adapter_is_settled(exiting_adapter, window):
                    pending.phase = "REQUEST_EXIT"
                    pending.exiting_tool_id = ""
                else:
                    pending.attempts += 1
                    if pending.attempts >= _MAX_WAIT_ATTEMPTS:
                        raise WorkspaceHandoffError(
                            f"{exiting_adapter.label} Workspace 종료 완료 대기 시간이 초과되었습니다."
                        )
                continue
            pending.exiting_tool_id = ""
            if pending.target_tool_id:
                pending.phase = "REQUEST_EXIT"
                pending.attempts = 0
                if _advance_target_cleanup(pending, window):
                    continue
                if target_adapter is None:
                    raise WorkspaceHandoffError("대상 COLMAP 도구 계약이 사라졌습니다.")
                _start_adapter(target_adapter, window)
            else:
                if pending.phase != "RESTORE_DESTINATION":
                    pending.phase = "RESTORE_DESTINATION"
                    pending.attempts = 0
                if not _restore_requested_destination(pending, window):
                    continue
            _PENDING_HANDOFFS.pop(window_pointer, None)
        except WorkspaceHandoffError as error:
            _PENDING_HANDOFFS.pop(window_pointer, None)
            _report_handoff_error(
                pending.target_tool_id,
                str(error),
                fallback_tool_id=(
                    pending.exiting_tool_id or pending.last_exiting_tool_id
                ),
            )
    return _TICK_INTERVAL_SECONDS if _PENDING_HANDOFFS else None


def _handoff_tick() -> float | None:
    """Blender 화면 갱신 중 같은 전환 Timer가 중첩 실행되지 않도록 보호합니다."""

    global _HANDOFF_TICK_RUNNING
    if _HANDOFF_TICK_RUNNING:
        return _TICK_INTERVAL_SECONDS if _PENDING_HANDOFFS else None
    _HANDOFF_TICK_RUNNING = True
    try:
        return _advance_handoffs()
    finally:
        _HANDOFF_TICK_RUNNING = False


def _ensure_handoff_timer() -> None:
    """대기 요청이 있을 때 공용 전환 Timer를 중복 없이 등록합니다."""

    if not _HANDOFF_TICK_RUNNING and not bpy.app.timers.is_registered(_handoff_tick):
        bpy.app.timers.register(_handoff_tick, first_interval=0.0)


def request_workspace_activation(context: Any, target_tool_id: str) -> str:
    """현재 도구를 종료한 뒤 대상 도구 Workspace를 열도록 요청합니다."""

    adapter = _ADAPTERS.get(str(target_tool_id))
    if adapter is None:
        raise WorkspaceHandoffError(
            f"등록되지 않은 COLMAP Workspace 도구입니다: {target_tool_id}"
        )
    window = getattr(context, "window", None)
    window_pointer = pointer(window)
    if not window_pointer:
        raise WorkspaceHandoffError("COLMAP Workspace를 전환할 Blender Window가 없습니다.")
    _LAST_WORKSPACE_POINTERS.setdefault(
        window_pointer,
        pointer(getattr(window, "workspace", None)),
    )
    pending = _PENDING_HANDOFFS.get(window_pointer)
    if pending is not None:
        _retarget_pending_handoff(pending, adapter.tool_id)
        _ensure_handoff_timer()
        return ACTIVATION_DEFERRED
    blockers = _blocking_adapters(window, adapter.tool_id)
    if not blockers and not _target_stale_workspaces(adapter.tool_id):
        _start_adapter(adapter, window)
        return ACTIVATION_STARTED
    _PENDING_HANDOFFS[window_pointer] = _PendingHandoff(
        window_pointer=window_pointer,
        target_tool_id=adapter.tool_id,
    )
    _ensure_handoff_timer()
    return ACTIVATION_DEFERRED


def _on_workspace_changed() -> None:
    """수동 Workspace 전환을 현재 COLMAP 세션 종료와 대상 유지 요청으로 변환합니다."""

    manager = getattr(bpy.context, "window_manager", None)
    if manager is None:
        return
    for window in tuple(manager.windows):
        workspace = getattr(window, "workspace", None)
        workspace_pointer = pointer(workspace)
        if not workspace_pointer:
            continue
        target_tool_id = workspace_tool_id(workspace)
        window_pointer = pointer(window)
        previous_workspace = _workspace_by_pointer(
            _LAST_WORKSPACE_POINTERS.get(window_pointer, 0)
        )
        previous_tool_id = workspace_tool_id(previous_workspace)
        _LAST_WORKSPACE_POINTERS[window_pointer] = workspace_pointer
        departed_tool_id = (
            previous_tool_id
            if previous_tool_id
            and pointer(previous_workspace) != workspace_pointer
            and previous_tool_id != target_tool_id
            else ""
        )
        pending = _PENDING_HANDOFFS.get(window_pointer)
        if pending is not None:
            if target_tool_id and target_tool_id != pending.target_tool_id:
                _retarget_pending_handoff(
                    pending,
                    target_tool_id,
                    workspace_pointer,
                )
            _ensure_handoff_timer()
            continue
        if target_tool_id:
            target_adapter = _ADAPTERS.get(target_tool_id)
            blockers = _blocking_adapters(window, target_tool_id)
            if (
                not departed_tool_id
                and not blockers
                and (target_adapter is None or _adapter_is_engaged(target_adapter, window))
            ):
                continue
            _PENDING_HANDOFFS[window_pointer] = _PendingHandoff(
                window_pointer=window_pointer,
                target_tool_id=target_tool_id,
                destination_workspace_pointer=workspace_pointer,
                departed_tool_id=departed_tool_id,
            )
            _ensure_handoff_timer()
            continue
        if not departed_tool_id and not _blocking_adapters(window, ""):
            continue
        _PENDING_HANDOFFS[window_pointer] = _PendingHandoff(
            window_pointer=window_pointer,
            destination_workspace_pointer=workspace_pointer,
            departed_tool_id=departed_tool_id,
        )
        _ensure_handoff_timer()


@persistent
def clear_workspace_handoffs_before_load(*_args: Any) -> None:
    """파일 교체 전에 이전 Window Pointer와 대기 중인 자동 전환 요청을 폐기합니다."""

    if bpy.app.timers.is_registered(_handoff_tick):
        bpy.app.timers.unregister(_handoff_tick)
    _PENDING_HANDOFFS.clear()
    _LAST_WORKSPACE_POINTERS.clear()
    clear_scene_marker_sessions()


@persistent
def remember_workspace_handoffs_after_load(*_args: Any) -> None:
    """파일 로드 뒤 Message Bus 구독과 새 Window·Workspace Pointer를 복구합니다."""

    _subscribe_workspace_changes()
    _remember_current_workspaces()


def _subscribe_workspace_changes() -> None:
    """파일 로드로 사라질 수 있는 Workspace 변경 구독을 현재 Runtime에 다시 연결합니다."""

    bpy.msgbus.clear_by_owner(_MSGBUS_OWNER)
    bpy.msgbus.subscribe_rna(
        key=(bpy.types.Window, "workspace"),
        owner=_MSGBUS_OWNER,
        args=(),
        notify=_on_workspace_changed,
        options={"PERSISTENT"},
    )


def _register_runtime() -> None:
    """Workspace Message Bus를 공용 전환 조정기 소유로 한 번 등록합니다."""

    global _RUNTIME_REGISTERED
    if _RUNTIME_REGISTERED:
        return
    _subscribe_workspace_changes()
    if clear_workspace_handoffs_before_load not in bpy.app.handlers.load_pre:
        bpy.app.handlers.load_pre.append(clear_workspace_handoffs_before_load)
    if remember_workspace_handoffs_after_load not in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.append(remember_workspace_handoffs_after_load)
    _remember_current_workspaces()
    _RUNTIME_REGISTERED = True


def _unregister_runtime() -> None:
    """공용 전환 Timer와 Message Bus 및 대기 요청을 모두 해제합니다."""

    global _RUNTIME_REGISTERED
    if bpy.app.timers.is_registered(_handoff_tick):
        bpy.app.timers.unregister(_handoff_tick)
    bpy.msgbus.clear_by_owner(_MSGBUS_OWNER)
    if clear_workspace_handoffs_before_load in bpy.app.handlers.load_pre:
        bpy.app.handlers.load_pre.remove(clear_workspace_handoffs_before_load)
    if remember_workspace_handoffs_after_load in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(remember_workspace_handoffs_after_load)
    _PENDING_HANDOFFS.clear()
    _LAST_WORKSPACE_POINTERS.clear()
    clear_scene_marker_sessions()
    _RUNTIME_REGISTERED = False


def register_workspace_handoff_adapter(adapter: WorkspaceHandoffAdapter) -> None:
    """독립 도구의 Workspace 생명주기 계약을 Tool ID로 등록합니다."""

    tool_id = str(adapter.tool_id).strip()
    if not tool_id:
        raise WorkspaceHandoffError("Workspace 전환 도구 ID는 비어 있을 수 없습니다.")
    existing = _ADAPTERS.get(tool_id)
    if existing is not None and existing is not adapter:
        raise WorkspaceHandoffError(f"Workspace 전환 도구가 이미 등록되었습니다: {tool_id}")
    _ADAPTERS[tool_id] = adapter
    _register_runtime()


def unregister_workspace_handoff_adapter(tool_id: str) -> None:
    """지정한 독립 도구 계약과 그 도구를 대상으로 한 대기 요청을 제거합니다."""

    normalized = str(tool_id).strip()
    _ADAPTERS.pop(normalized, None)
    for window_pointer, pending in tuple(_PENDING_HANDOFFS.items()):
        if pending.target_tool_id == normalized:
            _PENDING_HANDOFFS.pop(window_pointer, None)
    if not _ADAPTERS:
        _unregister_runtime()


__all__ = [
    "ACTIVATION_DEFERRED",
    "ACTIVATION_STARTED",
    "WorkspaceHandoffAdapter",
    "WorkspaceHandoffError",
    "clear_workspace_handoffs_before_load",
    "remember_workspace_handoffs_after_load",
    "register_workspace_handoff_adapter",
    "request_workspace_activation",
    "unregister_workspace_handoff_adapter",
    "workspace_tool_id",
]
