"""COLMAP Align 세션에서만 단축키를 가로채는 지속 모달 입력 가드입니다."""

from __future__ import annotations

from typing import Any

import bpy

try:
    from ....shared.blender_runtime.workspace_factory import (
        screen_matches_workspace,
        workspace_matches_spec,
    )
except ImportError:  # Source Tree를 최상위 tools 패키지로 실행하는 기존 테스트를 지원합니다.
    from shared.blender_runtime.workspace_factory import (
        screen_matches_workspace,
        workspace_matches_spec,
    )

from ..constants import (
    INPUT_ACTION_BLOCK_UNDO_HISTORY,
    INPUT_ACTION_BLOCK_WORKSPACE_CYCLE,
    INPUT_ACTION_DELETE_MODEL,
    INPUT_ACTION_FRAME_ALL,
    INPUT_ACTION_REDO,
    INPUT_ACTION_TRANSFORM_ROTATE,
    INPUT_ACTION_TRANSFORM_SCALE,
    INPUT_ACTION_TRANSFORM_TRANSLATE,
    INPUT_ACTION_UNDO,
    MAIN_SCREEN_OWNER_KEY,
    ROLE_REFERENCE,
)
from .marker_alignment import (
    cancel_marker_preview_from_ui,
    is_icp_input_locked,
    is_icp_running,
    is_marker_preview_active,
)
from .model_group import model_deletion_block_reason
from .navigation import invoke_space_pan_under_mouse, synchronize_space_navigation_state
from ..session import (
    can_redo_icp_commit_during_marker_preview,
    is_session_active_for_window,
    redo_session_state,
    undo_session_state,
)
from ..status import set_window_status_text
from ..state import RUNTIME
from ..viewport.registry import find_window_region, resolve_area
from ..viewport.renderer import request_redraw
from ..workspace_contract import ALIGN_WORKSPACE_SPEC


_PAGE_EVENTS = {"PAGE_UP", "PAGE_DOWN", "PAGEUP", "PAGEDOWN"}


def _is_main_session_screen(context: Any) -> bool:
    """파일 탐색기·사용자 화면이 아닌 UUID 검증된 Align 메인 화면인지 확인합니다."""
    window = getattr(context, "window", None)
    screen = getattr(window, "screen", None) if window is not None else None
    workspace = getattr(window, "workspace", None) if window is not None else None
    return bool(
        screen is not None
        and workspace_matches_spec(workspace, ALIGN_WORKSPACE_SPEC)
        and screen_matches_workspace(screen, workspace)
        and screen.get(MAIN_SCREEN_OWNER_KEY, False)
    )


def _is_merge_export_input_locked() -> bool:
    """내보내기 입력 snapshot이 사용 중인지 안전하게 반환합니다."""

    merge_state = getattr(RUNTIME, "merge_export", None)
    obj_state = getattr(RUNTIME, "obj_export", None)
    return bool(
        getattr(merge_state, "is_running", False)
        or getattr(obj_state, "is_running", False)
    )


def route_session_input(event: Any) -> str | None:
    """가짜 이벤트도 처리 가능한 순수 단축키 분류 결과를 반환합니다."""
    if getattr(event, "value", None) != "PRESS":
        return None
    event_type = getattr(event, "type", "")
    ctrl = bool(getattr(event, "ctrl", False))
    shift = bool(getattr(event, "shift", False))
    alt = bool(getattr(event, "alt", False))
    oskey = bool(getattr(event, "oskey", False))
    if event_type in {"G", "R", "S"} and not ctrl and not alt and not oskey:
        return {
            "G": INPUT_ACTION_TRANSFORM_TRANSLATE,
            "R": INPUT_ACTION_TRANSFORM_ROTATE,
            "S": INPUT_ACTION_TRANSFORM_SCALE,
        }[event_type]
    if event_type == "DEL":
        return INPUT_ACTION_DELETE_MODEL
    if event_type == "HOME" and not ctrl and not shift and not alt and not oskey:
        return INPUT_ACTION_FRAME_ALL
    if ctrl and alt and event_type == "Z":
        return INPUT_ACTION_BLOCK_UNDO_HISTORY
    if ctrl and shift and event_type == "Z":
        return INPUT_ACTION_REDO
    if ctrl and event_type == "Y":
        return INPUT_ACTION_REDO
    if ctrl and event_type == "Z":
        return INPUT_ACTION_UNDO
    if ctrl and event_type in _PAGE_EVENTS:
        return INPUT_ACTION_BLOCK_WORKSPACE_CYCLE
    return None


def _apply_session_action(action: str) -> None:
    """분류된 세션 전용 명령을 런타임 상태에 적용하고 화면을 갱신합니다."""
    if action in {INPUT_ACTION_UNDO, INPUT_ACTION_REDO} and _is_merge_export_input_locked():
        message = "내보내기 중에는 Undo·Redo를 사용할 수 없습니다. 작업을 취소하거나 완료를 기다리십시오."
        RUNTIME.marker_selection.message = message
        set_window_status_text(bpy.context.window, message)
        request_redraw()
        return
    if action in {INPUT_ACTION_UNDO, INPUT_ACTION_REDO} and is_icp_input_locked():
        message = "ICP 실행 또는 결과 미리보기 중에는 Undo·Redo를 사용할 수 없습니다."
        RUNTIME.marker_selection.message = message
        set_window_status_text(bpy.context.window, message)
        request_redraw()
        return
    allows_icp_commit_redo = (
        action == INPUT_ACTION_REDO
        and can_redo_icp_commit_during_marker_preview()
    )
    if (
        action in {INPUT_ACTION_UNDO, INPUT_ACTION_REDO}
        and is_marker_preview_active()
        and not allows_icp_commit_redo
    ):
        message = "마커 정렬 미리보기 중에는 마커 Undo·Redo를 사용할 수 없습니다."
        RUNTIME.marker_selection.message = message
        set_window_status_text(bpy.context.window, message)
        request_redraw()
        return
    if action == INPUT_ACTION_UNDO:
        undo_session_state()
    elif action == INPUT_ACTION_REDO:
        redo_session_state()
    elif action == INPUT_ACTION_DELETE_MODEL:
        if _is_merge_export_input_locked():
            message = "구조 통합 내보내기 중에는 데이터 레이어를 삭제할 수 없습니다."
            RUNTIME.model_catalog.message = message
            set_window_status_text(bpy.context.window, message)
            request_redraw()
            return
        if is_icp_input_locked():
            message = "ICP 실행 또는 결과 미리보기 중에는 데이터 레이어를 삭제할 수 없습니다."
            RUNTIME.model_catalog.message = message
            set_window_status_text(bpy.context.window, message)
            request_redraw()
            return
        reason = model_deletion_block_reason()
        if reason is None:
            bpy.ops.colmap_align.delete_selected_model("INVOKE_DEFAULT")
        else:
            RUNTIME.model_catalog.message = reason
            set_window_status_text(bpy.context.window, reason)
    elif action == INPUT_ACTION_BLOCK_UNDO_HISTORY:
        message = "COLMAP Align 세션에서는 Blender Undo History를 사용할 수 없습니다."
        RUNTIME.marker_selection.message = message
        set_window_status_text(bpy.context.window, message)
    elif action == INPUT_ACTION_BLOCK_WORKSPACE_CYCLE:
        message = "COLMAP Align 세션 중에는 작업공간 순환을 사용할 수 없습니다."
        RUNTIME.marker_selection.message = message
        set_window_status_text(bpy.context.window, message)
    request_redraw()


def _start_layer_transform_from_action(context: Any, event: Any, action: str) -> bool:
    """메인 화면의 마우스 아래 A/P WINDOW에서만 세션 가드 G/R/S 모달을 시작합니다."""

    if not _is_main_session_screen(context):
        return False
    mode = {
        INPUT_ACTION_TRANSFORM_TRANSLATE: "TRANSLATE",
        INPUT_ACTION_TRANSFORM_ROTATE: "ROTATE",
        INPUT_ACTION_TRANSFORM_SCALE: "SCALE",
    }.get(action)
    if mode is None:
        return False
    from .layer_transform import start_layer_transform_from_guard

    return start_layer_transform_from_guard(context, event, mode)


def start_input_guard(window: Any) -> bool:
    """대상 Window에 가드 모달을 한 번만 연결하고 실패는 호출자에게 전달합니다."""
    if RUNTIME.session.input_guard_active:
        return True
    if not is_session_active_for_window(window):
        return False
    area = resolve_area(window, ROLE_REFERENCE)
    region = find_window_region(area) if area is not None else None
    if area is None or region is None:
        raise RuntimeError("세션 입력 가드를 시작할 A 뷰포트 컨텍스트를 찾지 못했습니다.")
    with bpy.context.temp_override(
        window=window,
        screen=window.screen,
        area=area,
        region=region,
    ):
        result = bpy.ops.colmap_align.session_input_guard("INVOKE_DEFAULT")
    if "RUNNING_MODAL" not in result:
        raise RuntimeError("COLMAP Align 세션 입력 가드를 시작하지 못했습니다.")
    return True


def stop_input_guard_timer() -> None:
    """등록 해제와 세션 종료 전에 가드가 만든 Window Timer를 명시적으로 제거합니다."""
    timer = RUNTIME.session_guard_timer
    if timer is not None:
        window_manager = getattr(bpy.context, "window_manager", None)
        if window_manager is not None:
            try:
                window_manager.event_timer_remove(timer)
            except (ReferenceError, RuntimeError):
                pass
    RUNTIME.session_guard_timer = None
    RUNTIME.session.input_guard_active = False
    RUNTIME.session.input_guard_window_pointer = 0


class COLMAPALIGN_OT_session_input_guard(bpy.types.Operator):
    """활성 COLMAP Align Window에서 세션 전용 편집·보호 단축키를 소비합니다."""

    bl_idname = "colmap_align.session_input_guard"
    bl_label = "COLMAP Align 세션 입력 가드"
    bl_description = "세션 전용 Undo·모델 삭제와 작업공간 보호 단축키를 처리합니다"
    bl_options = {"INTERNAL"}

    @classmethod
    def poll(cls, context):
        """현재 컨텍스트가 세션 대상 Window일 때만 가드를 시작합니다."""
        return is_session_active_for_window(context.window)

    def invoke(self, context, _event):
        """주기 Timer를 가진 지속 모달을 등록해 종료 후에도 누수 없이 감시합니다."""
        if not self.poll(context):
            return {"CANCELLED"}
        if RUNTIME.session.input_guard_active:
            return {"CANCELLED"}
        self._window_pointer = context.window.as_pointer()
        self._session_generation = RUNTIME.session.generation
        self._space_navigation_active = False
        self._timer = context.window_manager.event_timer_add(0.1, window=context.window)
        RUNTIME.session_guard_timer = self._timer
        RUNTIME.session.input_guard_active = True
        RUNTIME.session.input_guard_window_pointer = self._window_pointer
        context.window_manager.modal_handler_add(self)
        return {"RUNNING_MODAL"}

    def _finish(self, context) -> None:
        """가드 Timer와 런타임 활성 플래그를 해당 Window에 한해서만 제거합니다."""
        timer = getattr(self, "_timer", None)
        if timer is not None:
            try:
                context.window_manager.event_timer_remove(timer)
            except (ReferenceError, RuntimeError):
                pass
            self._timer = None
        if (
            RUNTIME.session.input_guard_window_pointer == getattr(self, "_window_pointer", 0)
            and getattr(self, "_session_generation", -1) == RUNTIME.session.generation
        ):
            RUNTIME.session.input_guard_active = False
            RUNTIME.session.input_guard_window_pointer = 0
            RUNTIME.session_guard_timer = None

    def modal(self, context, event):
        """세션 밖에서는 종료하고, 세션 안에서는 지정 단축키만 소비합니다."""
        if (
            getattr(self, "_session_generation", -1) != RUNTIME.session.generation
            or not is_session_active_for_window(context.window)
        ):
            self._finish(context)
            return {"CANCELLED", "PASS_THROUGH"}
        self._space_navigation_active = synchronize_space_navigation_state(
            event,
            getattr(self, "_space_navigation_active", False),
        )
        if event.type == "WINDOW_DEACTIVATE":
            return {"PASS_THROUGH"}
        if event.type == "SPACE":
            return {"PASS_THROUGH"}
        if (
            getattr(self, "_space_navigation_active", False)
            and event.type == "MIDDLEMOUSE"
            and event.value == "PRESS"
            and _is_main_session_screen(context)
            and invoke_space_pan_under_mouse(context, event)
        ):
            return {"RUNNING_MODAL"}
        if (
            event.type == "ESC"
            and event.value == "PRESS"
            and _is_main_session_screen(context)
            and is_marker_preview_active()
        ):
            if is_icp_input_locked():
                message = (
                    "ICP 실행 중에는 취소가 완료될 때까지 마커 미리보기를 변경할 수 없습니다."
                    if is_icp_running()
                    else "ICP 결과 미리보기 중에는 결과 버리기 또는 정렬 승인을 선택하십시오."
                )
                set_window_status_text(context.window, message)
                request_redraw()
                return {"RUNNING_MODAL"}
            if cancel_marker_preview_from_ui(context):
                set_window_status_text(
                    context.window,
                    "마커 정렬 미리보기를 취소하고 이전 P 시점으로 복원했습니다.",
                )
                return {"RUNNING_MODAL"}
        action = route_session_input(event)
        if action is None:
            return {"PASS_THROUGH"}
        if action == INPUT_ACTION_FRAME_ALL:
            from .view_framing import frame_visible_role_under_mouse

            return (
                {"RUNNING_MODAL"}
                if frame_visible_role_under_mouse(context, event)
                else {"PASS_THROUGH"}
            )
        if action in {
            INPUT_ACTION_TRANSFORM_TRANSLATE,
            INPUT_ACTION_TRANSFORM_ROTATE,
            INPUT_ACTION_TRANSFORM_SCALE,
        }:
            return {
                "RUNNING_MODAL"
                if _start_layer_transform_from_action(context, event, action)
                else "PASS_THROUGH"
            }
        if action == INPUT_ACTION_DELETE_MODEL and not _is_main_session_screen(context):
            return {"PASS_THROUGH"}
        _apply_session_action(action)
        return {"RUNNING_MODAL"}

    def cancel(self, context):
        """Blender가 모달을 중단할 때 Timer와 세션 가드 플래그를 정리합니다."""
        self._finish(context)


CLASSES = (COLMAPALIGN_OT_session_input_guard,)
