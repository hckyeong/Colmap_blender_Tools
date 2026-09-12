"""활성 COLMAP Modifier Workspace의 전용 Undo·Redo 입력만 라우팅합니다.

이 모듈은 키보드 UX를 Modifier 전용 Operator로 연결할 뿐이며, Edit 메뉴나
직접적인 Blender Undo API 호출을 차단하는 보안 경계가 아닙니다.
"""

from __future__ import annotations

from typing import Any

import bpy

from .modifier_session import (
    MAIN_SCREEN_OWNER_KEY,
    SESSION,
    is_owned_modifier_workspace,
    is_session_active_for_window,
)


ACTION_UNDO = "UNDO"
ACTION_REDO = "REDO"
ACTION_BLOCK_UNDO_HISTORY = "BLOCK_UNDO_HISTORY"

UNDO_EMPTY_MESSAGE = "실행 취소할 Modifier 편집 기록이 없습니다."
REDO_EMPTY_MESSAGE = "다시 실행할 Modifier 편집 기록이 없습니다."
UNDO_HISTORY_BLOCK_MESSAGE = (
    "COLMAP Modifier 세션에서는 Blender Undo History를 사용할 수 없습니다."
)

_SUPPORTED_AREA_TYPES = frozenset({"VIEW_3D", "DOPESHEET_EDITOR"})
_SHORTCUT_KEY_TYPES = frozenset({"Z", "Y"})
_GUARD_INTERVAL_SECONDS = 0.1

_GUARD_ACTIVE = False
_GUARD_GENERATION = 0
_GUARD_TIMER: Any = None
_GUARD_WINDOW_MANAGER: Any = None
_GUARD_WINDOW_POINTER = 0
_HELD_KEY_TYPES: set[str] = set()
_EDIT_TRANSACTION_LOCK_REASON = ""
_TRANSFORM_INPUT_CALLBACK = None
_TRANSFORM_CANCEL_CALLBACK = None


def set_transform_input_callbacks(route, cancel) -> None:
    """변환 기능이 제공하는 입력·취소 콜백을 단방향으로 연결합니다."""
    global _TRANSFORM_INPUT_CALLBACK, _TRANSFORM_CANCEL_CALLBACK
    _TRANSFORM_INPUT_CALLBACK = route
    _TRANSFORM_CANCEL_CALLBACK = cancel


def _pointer(value: Any) -> int:
    """유효한 Blender RNA 객체의 현재 프로세스 포인터를 반환합니다."""

    if value is None:
        return 0
    try:
        return int(value.as_pointer())
    except (AttributeError, ReferenceError, RuntimeError, TypeError, ValueError):
        return 0


def _window_region(area: Any) -> Any:
    """주어진 Editor Area의 기본 WINDOW Region을 반환합니다."""

    if area is None:
        return None
    try:
        return next(
            (region for region in area.regions if region.type == "WINDOW"),
            None,
        )
    except (AttributeError, ReferenceError, RuntimeError, TypeError):
        return None


def _point_inside(value: int, start: int, size: int) -> bool:
    """창 좌표 하나가 반열린 화면 구간 안에 있는지 확인합니다."""

    return start <= value < start + max(0, size)


def _area_and_region_under_mouse(context: Any, event: Any) -> tuple[Any, Any]:
    """키 입력 당시 마우스 아래 Area와 Region을 창 좌표로 찾습니다."""

    window = getattr(context, "window", None)
    screen = getattr(window, "screen", None) if window is not None else None
    mouse_x = getattr(event, "mouse_x", None)
    mouse_y = getattr(event, "mouse_y", None)
    if screen is not None and isinstance(mouse_x, int) and isinstance(mouse_y, int):
        try:
            area = next(
                (
                    candidate
                    for candidate in screen.areas
                    if _point_inside(mouse_x, int(candidate.x), int(candidate.width))
                    and _point_inside(mouse_y, int(candidate.y), int(candidate.height))
                ),
                None,
            )
            if area is not None:
                matching_regions = tuple(
                    region
                    for region in area.regions
                    if _point_inside(mouse_x, int(region.x), int(region.width))
                    and _point_inside(mouse_y, int(region.y), int(region.height))
                )
                region = next(
                    (
                        candidate
                        for candidate in matching_regions
                        if candidate.type != "WINDOW"
                    ),
                    matching_regions[0] if matching_regions else None,
                )
                return area, region
        except (AttributeError, ReferenceError, RuntimeError, TypeError, ValueError):
            pass
    return getattr(context, "area", None), getattr(context, "region", None)


def _owned_modifier_screen_is_active(context: Any) -> bool:
    """현재 Window가 검증된 Modifier 세션·Workspace·Screen을 소유하는지 확인합니다."""

    window = getattr(context, "window", None)
    if not is_session_active_for_window(window):
        return False
    try:
        workspace = window.workspace
        screen = window.screen
        return bool(
            is_owned_modifier_workspace(workspace)
            and _pointer(screen) == int(SESSION.control_screen_pointer)
            and screen.get(MAIN_SCREEN_OWNER_KEY, False)
        )
    except (AttributeError, ReferenceError, RuntimeError, TypeError, ValueError):
        return False


def _shortcut_scope_is_active(context: Any, event: Any) -> bool:
    """팝업·텍스트 UI를 제외한 Modifier View3D·Timeline 입력인지 판정합니다."""

    if not _owned_modifier_screen_is_active(context):
        return False
    try:
        if getattr(context, "region_popup", None) is not None:
            return False
    except (AttributeError, ReferenceError, RuntimeError):
        return False
    area, region = _area_and_region_under_mouse(context, event)
    try:
        return bool(
            area is not None
            and area.type in _SUPPORTED_AREA_TYPES
            and region is not None
            and region.type == "WINDOW"
        )
    except (AttributeError, ReferenceError, RuntimeError, TypeError):
        return False


def route_modifier_input(event: Any) -> str | None:
    """Blender Event를 엄격한 Modifier 전용 단축키 동작으로 분류합니다."""

    if getattr(event, "value", None) != "PRESS":
        return None
    event_type = str(getattr(event, "type", "") or "")
    ctrl = bool(getattr(event, "ctrl", False))
    shift = bool(getattr(event, "shift", False))
    alt = bool(getattr(event, "alt", False))
    oskey = bool(getattr(event, "oskey", False))
    hyper = bool(getattr(event, "hyper", False))
    if not ctrl or oskey or hyper:
        return None
    if event_type == "Z" and alt and not shift:
        return ACTION_BLOCK_UNDO_HISTORY
    if event_type == "Z" and shift and not alt:
        return ACTION_REDO
    if event_type == "Y" and not shift and not alt:
        return ACTION_REDO
    if event_type == "Z" and not shift and not alt:
        return ACTION_UNDO
    return None


def _release_pressed_key(event: Any) -> None:
    """키를 놓거나 Window가 비활성화되면 반복 입력 추적을 해제합니다."""

    event_type = str(getattr(event, "type", "") or "")
    if event_type == "WINDOW_DEACTIVATE":
        _HELD_KEY_TYPES.clear()
    elif (
        getattr(event, "value", None) == "RELEASE"
        and event_type in _SHORTCUT_KEY_TYPES
    ):
        _HELD_KEY_TYPES.discard(event_type)


def _record_guard_status(context: Any, message: str) -> None:
    """입력 처리 결과를 Point 상태와 Modifier 세션 상태에 함께 기록합니다."""

    normalized = str(message).strip()
    if not normalized:
        return
    try:
        settings = getattr(getattr(context, "scene", None), "colmap_import", None)
        if settings is not None:
            settings.point_selection_status = normalized
    except (AttributeError, ReferenceError, RuntimeError, TypeError, ValueError):
        pass
    SESSION.status_message = normalized


def set_modifier_input_guard_transaction_lock(reason: str) -> None:
    """편집 Transaction이 진행되는 동안 Guard가 표시할 잠금 이유를 설정합니다."""

    global _EDIT_TRANSACTION_LOCK_REASON
    normalized = str(reason).strip()
    _EDIT_TRANSACTION_LOCK_REASON = normalized or "Modifier 편집 Transaction이 진행 중입니다."


def clear_modifier_input_guard_transaction_lock() -> None:
    """완료되거나 Rollback된 편집 Transaction의 입력 잠금을 해제합니다."""

    global _EDIT_TRANSACTION_LOCK_REASON
    _EDIT_TRANSACTION_LOCK_REASON = ""


def _journal_transaction_lock_reason(scene: Any) -> str | None:
    """설치된 Runtime Journal이 노출한 Transaction 잠금 상태를 안전하게 읽습니다."""

    settings = getattr(scene, "colmap_import", None)
    domain_uuid = str(getattr(settings, "domain_uuid", "") or "")
    if not domain_uuid:
        return None
    try:
        from .modifier_runtime_domain import domain_by_uuid

        domain = domain_by_uuid(domain_uuid)
    except (ImportError, AttributeError, ReferenceError, RuntimeError, TypeError, ValueError):
        return None
    if domain is None:
        return None
    if bool(getattr(domain, "transform_preview_active", False)):
        return "데이터 변환을 확정하거나 취소한 뒤 실행하십시오."
    frozen_reason = str(getattr(domain, "frozen_reason", "") or "").strip()
    if frozen_reason:
        return f"Modifier Runtime 편집이 잠겼습니다: {frozen_reason}"
    journal = getattr(domain, "journal", None)
    if journal is None:
        return None
    reason = str(
        getattr(journal, "transaction_lock_reason", "")
        or getattr(journal, "lock_reason", "")
        or ""
    ).strip()
    if reason:
        return reason
    if any(
        bool(getattr(journal, attribute, False))
        for attribute in ("transaction_active", "transaction_running", "is_applying")
    ):
        return "Modifier 편집 Transaction이 진행 중입니다."
    return None


def _modifier_input_lock_reason(context: Any) -> str | None:
    """Import·Export·Runtime 복원·편집 Transaction의 현재 잠금 이유를 반환합니다."""

    scene = getattr(context, "scene", None)
    settings = getattr(scene, "colmap_import", None)
    if scene is None or settings is None:
        return _EDIT_TRANSACTION_LOCK_REASON or None

    from .import_progress_session import active_import_progress_session

    if active_import_progress_session() is not None:
        return "Import 진행 중에는 Modifier 전용 Undo·Redo를 사용할 수 없습니다."

    from .export_progress_session import active_export_progress_session

    if active_export_progress_session() is not None or bool(settings.export_running):
        return "Export 진행 중에는 Modifier 전용 Undo·Redo를 사용할 수 없습니다."

    from .runtime_state import runtime_restore_job

    restore_job = runtime_restore_job(scene)
    if (
        restore_job is not None
        and not bool(getattr(restore_job, "done", False))
        and not bool(getattr(restore_job, "failed", False))
    ):
        return "Modifier Runtime 복원 중에는 Undo·Redo를 사용할 수 없습니다."

    if _EDIT_TRANSACTION_LOCK_REASON:
        return _EDIT_TRANSACTION_LOCK_REASON

    try:
        from .camera_filter_queue import PENDING_STATUS_APPLYING

        if settings.review_pending_filter_status == PENDING_STATUS_APPLYING:
            return "Modifier 편집 Transaction이 진행 중에는 Undo·Redo를 사용할 수 없습니다."
    except (ImportError, AttributeError, ReferenceError, RuntimeError, TypeError):
        pass

    return _journal_transaction_lock_reason(scene)


def _sync_session_status_from_operator(context: Any, fallback: str) -> None:
    """전용 Operator가 작성한 상세 상태를 세션 상태에도 반영합니다."""

    settings = getattr(getattr(context, "scene", None), "colmap_import", None)
    message = str(getattr(settings, "point_selection_status", "") or "").strip()
    _record_guard_status(context, message or fallback)


def _execute_modifier_undo(context: Any) -> None:
    """Modifier Undo Operator만 실행하고 Poll 실패를 빈 Journal 상태로 소비합니다."""

    try:
        if not bpy.ops.colmap.undo_last_edit.poll():
            _record_guard_status(context, UNDO_EMPTY_MESSAGE)
            return
        result = bpy.ops.colmap.undo_last_edit("EXEC_DEFAULT")
    except Exception as error:
        _record_guard_status(context, f"Modifier 전용 실행 취소 실패: {error}")
        return
    if "FINISHED" not in result:
        _sync_session_status_from_operator(context, "Modifier 전용 실행 취소에 실패했습니다.")
        return
    _sync_session_status_from_operator(context, "Modifier 전용 실행 취소를 완료했습니다.")


def _execute_modifier_redo(context: Any) -> None:
    """Modifier Redo Operator만 실행하고 Poll 실패를 빈 Journal 상태로 소비합니다."""

    try:
        if not bpy.ops.colmap.redo_last_edit.poll():
            _record_guard_status(context, REDO_EMPTY_MESSAGE)
            return
        result = bpy.ops.colmap.redo_last_edit("EXEC_DEFAULT")
    except Exception as error:
        _record_guard_status(context, f"Modifier 전용 다시 실행 실패: {error}")
        return
    if "FINISHED" not in result:
        _sync_session_status_from_operator(context, "Modifier 전용 다시 실행에 실패했습니다.")
        return
    _sync_session_status_from_operator(context, "Modifier 전용 다시 실행을 완료했습니다.")


def route_modifier_input_event(context: Any, event: Any) -> set[str]:
    """현재 Context의 단축키를 통과시키거나 Modifier 전용 동작으로 소비합니다."""

    if _TRANSFORM_INPUT_CALLBACK is not None and _TRANSFORM_INPUT_CALLBACK(context, event):
        return {"RUNNING_MODAL"}
    action = route_modifier_input(event)
    if action is None or not _shortcut_scope_is_active(context, event):
        return {"PASS_THROUGH"}

    event_type = str(getattr(event, "type", "") or "")
    if bool(getattr(event, "is_repeat", False)) or event_type in _HELD_KEY_TYPES:
        return {"RUNNING_MODAL"}
    _HELD_KEY_TYPES.add(event_type)

    if action == ACTION_BLOCK_UNDO_HISTORY:
        _record_guard_status(context, UNDO_HISTORY_BLOCK_MESSAGE)
        return {"RUNNING_MODAL"}

    lock_reason = _modifier_input_lock_reason(context)
    if lock_reason is not None:
        _record_guard_status(context, lock_reason)
        return {"RUNNING_MODAL"}

    if action == ACTION_UNDO:
        _execute_modifier_undo(context)
    elif action == ACTION_REDO:
        _execute_modifier_redo(context)
    return {"RUNNING_MODAL"}


def _guard_start_area(window: Any) -> tuple[Any, Any]:
    """입력 Guard를 등록할 Modifier View3D 또는 Timeline WINDOW Region을 찾습니다."""

    try:
        areas = tuple(window.screen.areas)
    except (AttributeError, ReferenceError, RuntimeError, TypeError):
        return None, None
    for area_type in ("VIEW_3D", "DOPESHEET_EDITOR"):
        for area in areas:
            if getattr(area, "type", None) != area_type:
                continue
            region = _window_region(area)
            if region is not None:
                return area, region
    return None, None


def start_modifier_input_guard(window: Any | None = None) -> bool:
    """활성 Modifier 소유 Window에 입력 Guard 모달을 정확히 한 번 시작합니다."""

    target_window = window or getattr(bpy.context, "window", None)
    if not is_session_active_for_window(target_window):
        return False
    pointer = _pointer(target_window)
    if _GUARD_ACTIVE and _GUARD_WINDOW_POINTER == pointer and _GUARD_TIMER is not None:
        return True
    if _GUARD_ACTIVE or _GUARD_TIMER is not None:
        stop_modifier_input_guard()
    area, region = _guard_start_area(target_window)
    if area is None or region is None:
        raise RuntimeError("Modifier 입력 Guard를 시작할 View3D·Timeline 영역이 없습니다.")
    with bpy.context.temp_override(
        window=target_window,
        screen=target_window.screen,
        area=area,
        region=region,
    ):
        result = bpy.ops.colmap.modifier_input_guard("INVOKE_DEFAULT")
    if "RUNNING_MODAL" not in result:
        raise RuntimeError("Modifier 입력 Guard 모달을 시작하지 못했습니다.")
    return True


def stop_modifier_input_guard() -> None:
    """종료·등록 해제 전에 Guard Timer와 Window 소유 상태를 모두 정리합니다."""

    if _TRANSFORM_CANCEL_CALLBACK is not None:
        _TRANSFORM_CANCEL_CALLBACK()
    global _GUARD_ACTIVE
    global _GUARD_GENERATION
    global _GUARD_TIMER
    global _GUARD_WINDOW_MANAGER
    global _GUARD_WINDOW_POINTER
    _GUARD_GENERATION += 1
    timer = _GUARD_TIMER
    manager = _GUARD_WINDOW_MANAGER or getattr(bpy.context, "window_manager", None)
    if timer is not None and manager is not None:
        try:
            manager.event_timer_remove(timer)
        except (AttributeError, ReferenceError, RuntimeError, TypeError, ValueError):
            pass
    _GUARD_TIMER = None
    _GUARD_WINDOW_MANAGER = None
    _GUARD_WINDOW_POINTER = 0
    _GUARD_ACTIVE = False
    _HELD_KEY_TYPES.clear()
    clear_modifier_input_guard_transaction_lock()


def rearm_modifier_input_guard(window: Any | None = None) -> bool:
    """Workspace 복귀나 파일 상태 변경 뒤 Guard를 새 세대로 다시 연결합니다."""

    target_window = window or getattr(bpy.context, "window", None)
    stop_modifier_input_guard()
    return start_modifier_input_guard(target_window)


def start_input_guard(window: Any | None = None) -> bool:
    """Lifecycle 통합용 짧은 이름으로 Modifier 입력 Guard를 시작합니다."""

    return start_modifier_input_guard(window)


def stop_input_guard() -> None:
    """Lifecycle 통합용 짧은 이름으로 Modifier 입력 Guard를 정리합니다."""

    stop_modifier_input_guard()


def rearm_input_guard(window: Any | None = None) -> bool:
    """Lifecycle 통합용 짧은 이름으로 Modifier 입력 Guard를 재연결합니다."""

    return rearm_modifier_input_guard(window)


class COLMAP_OT_modifier_input_guard(bpy.types.Operator):
    """활성 Modifier Window에서 전용 Undo·Redo 키만 소비하는 지속 모달입니다."""

    bl_idname = "colmap.modifier_input_guard"
    bl_label = "COLMAP Modifier 입력 Guard"
    bl_description = "Modifier 전용 Undo·Redo 단축키를 활성 세션에 연결합니다"
    bl_options = {"INTERNAL"}

    @classmethod
    def poll(cls, context: Any) -> bool:
        """검증된 Modifier 소유 Window에서만 Guard 시작을 허용합니다."""

        return _owned_modifier_screen_is_active(context)

    def invoke(self, context: Any, _event: Any) -> set[str]:
        """Window Timer와 세대 토큰을 가진 지속 모달 Handler를 등록합니다."""

        global _GUARD_ACTIVE
        global _GUARD_GENERATION
        global _GUARD_TIMER
        global _GUARD_WINDOW_MANAGER
        global _GUARD_WINDOW_POINTER
        if not self.poll(context) or _GUARD_ACTIVE:
            return {"CANCELLED"}
        _GUARD_GENERATION += 1
        self._generation = _GUARD_GENERATION
        self._window_pointer = _pointer(context.window)
        self._window_manager = context.window_manager
        self._timer = None
        try:
            self._timer = context.window_manager.event_timer_add(
                _GUARD_INTERVAL_SECONDS,
                window=context.window,
            )
            context.window_manager.modal_handler_add(self)
        except (AttributeError, ReferenceError, RuntimeError, TypeError, ValueError):
            if self._timer is not None:
                try:
                    context.window_manager.event_timer_remove(self._timer)
                except (AttributeError, ReferenceError, RuntimeError, TypeError, ValueError):
                    pass
                self._timer = None
            return {"CANCELLED"}
        _GUARD_TIMER = self._timer
        _GUARD_WINDOW_MANAGER = context.window_manager
        _GUARD_WINDOW_POINTER = self._window_pointer
        _GUARD_ACTIVE = True
        _HELD_KEY_TYPES.clear()
        return {"RUNNING_MODAL"}

    def _finish(self) -> None:
        """자기 세대의 Timer와 전역 소유 상태만 대칭적으로 정리합니다."""

        global _GUARD_ACTIVE
        global _GUARD_TIMER
        global _GUARD_WINDOW_MANAGER
        global _GUARD_WINDOW_POINTER
        timer = getattr(self, "_timer", None)
        manager = getattr(self, "_window_manager", None)
        if timer is not None and manager is not None:
            try:
                manager.event_timer_remove(timer)
            except (AttributeError, ReferenceError, RuntimeError, TypeError, ValueError):
                pass
            self._timer = None
        if getattr(self, "_generation", -1) == _GUARD_GENERATION:
            _GUARD_TIMER = None
            _GUARD_WINDOW_MANAGER = None
            _GUARD_WINDOW_POINTER = 0
            _GUARD_ACTIVE = False
            _HELD_KEY_TYPES.clear()
            clear_modifier_input_guard_transaction_lock()

    def modal(self, context: Any, event: Any) -> set[str]:
        """세션 밖에서는 종료하고 활성 범위의 전용 단축키만 라우팅합니다."""

        if (
            getattr(self, "_generation", -1) != _GUARD_GENERATION
            or _pointer(getattr(context, "window", None))
            != getattr(self, "_window_pointer", 0)
            or not _owned_modifier_screen_is_active(context)
        ):
            self._finish()
            return {"CANCELLED", "PASS_THROUGH"}
        _release_pressed_key(event)
        return route_modifier_input_event(context, event)

    def cancel(self, _context: Any) -> None:
        """Blender가 모달을 중단할 때 남은 Timer와 전역 참조를 회수합니다."""

        self._finish()


CLASSES = (COLMAP_OT_modifier_input_guard,)


__all__ = [
    "ACTION_BLOCK_UNDO_HISTORY",
    "ACTION_REDO",
    "ACTION_UNDO",
    "CLASSES",
    "COLMAP_OT_modifier_input_guard",
    "clear_modifier_input_guard_transaction_lock",
    "rearm_input_guard",
    "rearm_modifier_input_guard",
    "route_modifier_input",
    "route_modifier_input_event",
    "set_modifier_input_guard_transaction_lock",
    "start_input_guard",
    "start_modifier_input_guard",
    "stop_input_guard",
    "stop_modifier_input_guard",
]
