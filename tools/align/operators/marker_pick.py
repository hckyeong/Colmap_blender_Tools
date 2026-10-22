"""A/P 양방향 마커 드래프트의 생성, 편집, 확정과 삭제를 처리합니다."""

from __future__ import annotations

import bpy
from bpy.props import EnumProperty, IntProperty

from ..constants import (
    DRAFT_MODE_CREATE,
    DRAFT_MODE_EDIT,
    MARKER_COMMAND_CANCEL,
    MARKER_COMMAND_CONFIRM,
    MARKER_COMMAND_NONE,
    MARKER_GEOMETRY_SOURCE_MAIN,
    MARKER_COMMAND_PAIR_CREATE,
    MARKER_COMMAND_PAIR_DELETE,
    MARKER_COMMAND_PAIR_EDIT,
    ROLE_MOVING,
    ROLE_REFERENCE,
)
from ..properties import sync_marker_pair_list
from ..project_persistence.runtime import (
    notify_project_state_changed,
    restore_input_block_reason,
)
from ..session import (
    is_session_active_for_window,
    redo_session_marker_state,
    undo_session_marker_state,
)
from ..axis_settings import axis_edit_active_block_reason
from ..state import RUNTIME
from ..model_repository import ModelRepositoryError
from ..status import set_window_status_text
from ..viewport.picking import find_viewport_under_mouse, pick_nearest_point
from ..viewport.registry import find_window_region, refresh_registry, resolve_area
from ..viewport.renderer import request_redraw
from .marker_alignment import is_marker_preview_active
from .navigation import synchronize_space_navigation_state
from .workspace import find_main_window


NAVIGATION_EVENTS = {
    "MIDDLEMOUSE",
    "WHEELUPMOUSE",
    "WHEELDOWNMOUSE",
    "WHEELINMOUSE",
    "WHEELOUTMOUSE",
    "TRACKPADPAN",
    "TRACKPADZOOM",
    "NDOF_MOTION",
}


def _has_navigation_chord(event, space_navigation_active: bool) -> bool:
    """Space 또는 수정자와 함께 누른 클릭을 뷰포트 탐색 입력으로 판별합니다."""
    return bool(
        space_navigation_active
        or getattr(event, "alt", False)
        or getattr(event, "ctrl", False)
        or getattr(event, "shift", False)
        or getattr(event, "oskey", False)
    )


def _resolve_main_context(context):
    """현재 WindowManager에서 유효한 메인 창과 A Area·Region을 찾습니다."""
    main_window = find_main_window(context.window_manager)
    if main_window is None or not refresh_registry(main_window):
        return None
    reference_area = resolve_area(main_window, ROLE_REFERENCE)
    reference_region = find_window_region(reference_area) if reference_area else None
    if reference_area is None or reference_region is None:
        return None
    return main_window, reference_area, reference_region


def _session_can_undo() -> bool:
    """열린 드래프트 또는 확정 이력 중 현재 Ctrl+Z 대상이 있는지 확인합니다."""
    journal = RUNTIME.undo_journal
    return journal.can_undo_draft if journal.scope_active else journal.can_undo


def _session_can_redo() -> bool:
    """열린 드래프트 또는 확정 이력 중 현재 Ctrl+Y 대상이 있는지 확인합니다."""
    journal = RUNTIME.undo_journal
    return journal.can_redo_draft if journal.scope_active else journal.can_redo


def _is_export_input_locked() -> bool:
    """COLMAP 또는 OBJ 내보내기가 마커 상태 변경을 잠그는지 반환합니다."""

    return bool(
        getattr(RUNTIME.merge_export, "is_running", False)
        or getattr(RUNTIME.obj_export, "is_running", False)
    )


def _is_undo_event(event) -> bool:
    """세션 입력 가드만 소비해야 하는 Undo·Redo 조합인지 판별합니다."""
    if getattr(event, "value", None) != "PRESS" or not getattr(event, "ctrl", False):
        return False
    return getattr(event, "type", "") in {"Z", "Y"}


def stop_marker_modal_timer() -> None:
    """파일 로드·등록 해제 전에 남은 마커 모달 Timer를 안전하게 제거합니다."""
    timer = RUNTIME.marker_modal_timer
    if timer is None:
        return
    window_manager = getattr(bpy.context, "window_manager", None)
    if window_manager is not None:
        try:
            window_manager.event_timer_remove(timer)
        except (ReferenceError, RuntimeError):
            pass
    RUNTIME.marker_modal_timer = None


class COLMAPALIGN_OT_add_marker_pair(bpy.types.Operator):
    """N 패널의 새 마커 쌍 추가 요청을 메인 창 모달로 전달합니다."""

    bl_idname = "colmap_align.add_marker_pair"
    bl_label = "마커 쌍 추가"
    bl_description = "A 또는 P 뷰에서 점을 선택한 뒤 쌍 지정으로 확정합니다"
    bl_options = {"REGISTER"}

    @classmethod
    def poll(cls, context):
        """선택 비활성 레이어가 있는 안전한 세션에서만 입력을 허용합니다."""
        return bool(
            context.window_manager
            and is_session_active_for_window(context.window)
            and not RUNTIME.import_in_progress
            and not RUNTIME.modal_active
            and not is_marker_preview_active()
            and axis_edit_active_block_reason(RUNTIME) is None
            and not _is_export_input_locked()
            and restore_input_block_reason() is None
            and RUNTIME.workspace_setup_task is None
            and RUNTIME.model_catalog.selected_inactive_model is not None
            and RUNTIME.model_catalog.selected_inactive_model.is_visible
            and _resolve_main_context(context)
        )

    def invoke(self, context, _event):
        """레이어 선택을 유지하고 메인 창에서 입력 기준 검증과 모달 시작을 요청합니다."""
        resolved = _resolve_main_context(context)
        if resolved is None:
            self.report({"ERROR"}, "COLMAP Align 메인 듀얼 뷰가 유효하지 않습니다.")
            return {"CANCELLED"}
        main_window, area, region = resolved
        try:
            with context.temp_override(
                window=main_window,
                screen=main_window.screen,
                area=area,
                region=region,
            ):
                result = bpy.ops.colmap_align.marker_pick_modal(
                    "INVOKE_DEFAULT",
                    operation=DRAFT_MODE_CREATE,
                    target_pair_id=0,
                )
        except RuntimeError as error:
            self.report({"ERROR"}, f"메인 창에서 마커 선택을 시작하지 못했습니다: {error}")
            return {"CANCELLED"}
        if "RUNNING_MODAL" not in result:
            self.report({"WARNING"}, RUNTIME.marker_selection.message or "메인 창의 마커 모달 도구가 시작되지 않았습니다.")
            return {"CANCELLED"}
        return {"FINISHED"}


class COLMAPALIGN_OT_edit_marker_pair(bpy.types.Operator):
    """선택한 확정 쌍의 A/P 후보를 유지한 통합 편집 모달을 시작합니다."""

    bl_idname = "colmap_align.edit_marker_pair"
    bl_label = "마커 편집"
    bl_description = "기존 A/P 후보를 유지하고 어느 쪽 점이든 다시 선택해 확정합니다"
    bl_options = {"INTERNAL"}

    @classmethod
    def poll(cls, context):
        """활성 확정 쌍이 있고 드래프트가 닫힌 세션에서만 허용합니다."""
        return bool(
            is_session_active_for_window(context.window)
            and not RUNTIME.import_in_progress
            and not RUNTIME.modal_active
            and not is_marker_preview_active()
            and axis_edit_active_block_reason(RUNTIME) is None
            and not _is_export_input_locked()
            and restore_input_block_reason() is None
            and RUNTIME.marker_selection.active_pair is not None
            and _resolve_main_context(context)
        )

    def invoke(self, context, _event):
        """선택 pair_id를 고정해 메인 창 편집 모달을 실행합니다."""
        pair = RUNTIME.marker_selection.active_pair
        resolved = _resolve_main_context(context)
        if pair is None or resolved is None:
            self.report({"ERROR"}, "편집할 마커 쌍 또는 메인 듀얼 뷰를 찾을 수 없습니다.")
            return {"CANCELLED"}
        main_window, area, region = resolved
        try:
            with context.temp_override(
                window=main_window,
                screen=main_window.screen,
                area=area,
                region=region,
            ):
                result = bpy.ops.colmap_align.marker_pick_modal(
                    "INVOKE_DEFAULT",
                    operation=DRAFT_MODE_EDIT,
                    target_pair_id=pair.pair_id,
                )
        except RuntimeError as error:
            self.report({"ERROR"}, f"마커 쌍 편집을 시작하지 못했습니다: {error}")
            return {"CANCELLED"}
        return {"FINISHED"} if "RUNNING_MODAL" in result else {"CANCELLED"}


class COLMAPALIGN_OT_delete_marker_pair(bpy.types.Operator):
    """UIList에서 선택한 확정 쌍을 독립적인 원자 Undo 명령으로 삭제합니다."""

    bl_idname = "colmap_align.delete_marker_pair"
    bl_label = "마커 쌍 삭제"
    bl_description = "선택한 A/P 마커 쌍을 삭제합니다"
    bl_options = {"INTERNAL"}

    @classmethod
    def poll(cls, context):
        """드래프트가 닫히고 활성 확정 쌍이 있는 세션에서만 허용합니다."""
        return bool(
            is_session_active_for_window(context.window)
            and not RUNTIME.import_in_progress
            and not RUNTIME.modal_active
            and not is_marker_preview_active()
            and axis_edit_active_block_reason(RUNTIME) is None
            and not _is_export_input_locked()
            and restore_input_block_reason() is None
            and RUNTIME.marker_selection.active_pair is not None
        )

    def execute(self, context):
        """삭제 전후 스냅샷을 하나의 PAIR_DELETE 이력으로 기록합니다."""
        if not self.poll(context):
            return {"CANCELLED"}
        before = RUNTIME.marker_selection.snapshot()
        removed = RUNTIME.marker_selection.delete_active_pair()
        if removed is None:
            return {"CANCELLED"}
        RUNTIME.undo_journal.record_committed(
            before,
            RUNTIME.marker_selection.snapshot(),
            "마커 쌍 삭제",
            MARKER_COMMAND_PAIR_DELETE,
        )
        notify_project_state_changed("마커 쌍 삭제")
        sync_marker_pair_list(context.window_manager)
        request_redraw()
        return {"FINISHED"}


class COLMAPALIGN_OT_confirm_marker_selection(bpy.types.Operator):
    """준비된 드래프트를 모달 소유자가 확정하도록 명령만 큐에 넣습니다."""

    bl_idname = "colmap_align.confirm_marker_selection"
    bl_label = "쌍 지정"
    bl_description = "현재 A/P 후보를 하나의 대응쌍으로 확정합니다"
    bl_options = {"INTERNAL"}

    @classmethod
    def poll(cls, context):
        """신규 후보 완성 또는 기존 쌍의 실제 변경이 준비된 모달에서만 허용합니다."""
        return bool(
            is_session_active_for_window(context.window)
            and RUNTIME.modal_active
            and RUNTIME.marker_selection.can_confirm_draft()
            and RUNTIME.marker_command != MARKER_COMMAND_CANCEL
            and restore_input_block_reason() is None
        )

    def execute(self, context):
        """취소보다 낮은 우선순위의 확정 요청을 기록합니다."""
        if not self.poll(context) or not RUNTIME.request_marker_command(MARKER_COMMAND_CONFIRM):
            return {"CANCELLED"}
        RUNTIME.marker_selection.message = "마커 쌍 확정을 요청했습니다."
        request_redraw()
        return {"FINISHED"}


class COLMAPALIGN_OT_cancel_marker_selection(bpy.types.Operator):
    """열린 드래프트 전체를 모달 소유자가 취소하도록 요청합니다."""

    bl_idname = "colmap_align.cancel_marker_selection"
    bl_label = "선택 취소"
    bl_description = "진행 중인 A/P 후보 입력을 모두 폐기합니다"
    bl_options = {"INTERNAL"}

    @classmethod
    def poll(cls, context):
        """열린 드래프트가 있고 아직 취소가 예약되지 않았을 때만 허용합니다."""
        return bool(
            is_session_active_for_window(context.window)
            and RUNTIME.modal_active
            and RUNTIME.marker_selection.has_draft
            and RUNTIME.marker_command != MARKER_COMMAND_CANCEL
            and restore_input_block_reason() is None
        )

    def execute(self, context):
        """확정 요청을 덮어쓸 수 있는 취소 요청을 큐에 기록합니다."""
        if not self.poll(context):
            return {"CANCELLED"}
        RUNTIME.request_marker_command(MARKER_COMMAND_CANCEL)
        RUNTIME.marker_selection.message = "진행 중인 마커 쌍 입력을 취소합니다."
        request_redraw()
        return {"FINISHED"}


class COLMAPALIGN_OT_undo_marker_selection(bpy.types.Operator):
    """N 패널 버튼으로 세션 입력 가드와 같은 전용 Undo를 실행합니다."""

    bl_idname = "colmap_align.undo_marker_selection"
    bl_label = "되돌리기"
    bl_description = "열린 입력은 한 점씩, 확정 쌍은 한 명령씩 되돌립니다"
    bl_options = {"INTERNAL"}

    @classmethod
    def poll(cls, context):
        """현재 세션이 선택한 Undo 범위에 실제 이력이 있을 때만 허용합니다."""
        return bool(
            is_session_active_for_window(context.window)
            and not is_marker_preview_active()
            and axis_edit_active_block_reason(RUNTIME) is None
            and not _is_export_input_locked()
            and _session_can_undo()
            and restore_input_block_reason() is None
        )

    def execute(self, context):
        """버튼 입력도 Ctrl+Z와 동일한 세션 전용 라우팅을 사용합니다."""
        return {"FINISHED"} if self.poll(context) and undo_session_marker_state() else {"CANCELLED"}


class COLMAPALIGN_OT_redo_marker_selection(bpy.types.Operator):
    """N 패널 버튼으로 세션 입력 가드와 같은 전용 Redo를 실행합니다."""

    bl_idname = "colmap_align.redo_marker_selection"
    bl_label = "다시 실행"
    bl_description = "열린 입력 또는 확정 마커 쌍의 마지막 되돌리기를 다시 실행합니다"
    bl_options = {"INTERNAL"}

    @classmethod
    def poll(cls, context):
        """현재 세션이 선택한 Redo 범위에 실제 이력이 있을 때만 허용합니다."""
        return bool(
            is_session_active_for_window(context.window)
            and not is_marker_preview_active()
            and axis_edit_active_block_reason(RUNTIME) is None
            and not _is_export_input_locked()
            and _session_can_redo()
            and restore_input_block_reason() is None
        )

    def execute(self, context):
        """버튼 입력도 Ctrl+Y와 동일한 세션 전용 라우팅을 사용합니다."""
        return {"FINISHED"} if self.poll(context) and redo_session_marker_state() else {"CANCELLED"}


class COLMAPALIGN_OT_marker_pick_modal(bpy.types.Operator):
    """메인 창 A/P 뷰에서 드래프트 후보만 갱신하는 내부 모달 도구입니다."""

    bl_idname = "colmap_align.marker_pick_modal"
    bl_label = "COLMAP Align 마커 선택"
    bl_description = "메인 A/P 뷰포트에서 대응점 후보를 선택합니다"
    bl_options = {"INTERNAL"}

    operation: EnumProperty(
        items=(
            (DRAFT_MODE_CREATE, "생성", "새 마커 쌍을 만듭니다"),
            (DRAFT_MODE_EDIT, "편집", "기존 쌍을 편집합니다"),
        ),
        default=DRAFT_MODE_CREATE,
        options={"HIDDEN"},
    )
    target_pair_id: IntProperty(default=0, options={"HIDDEN"})

    @classmethod
    def poll(cls, context):
        """호출 컨텍스트가 등록된 메인 작업 창일 때만 실행을 허용합니다."""
        resolved = _resolve_main_context(context)
        return bool(
            resolved
            and is_session_active_for_window(context.window)
            and not is_marker_preview_active()
            and axis_edit_active_block_reason(RUNTIME) is None
            and not _is_export_input_locked()
            and restore_input_block_reason() is None
            and resolved[0].as_pointer() == context.window.as_pointer()
        )

    def invoke(self, context, _event):
        """표시·종류·원본 기준을 고정하고 새 또는 편집 드래프트와 Undo를 시작합니다."""
        baseline = RUNTIME.marker_selection.snapshot()
        if self.operation == DRAFT_MODE_EDIT:
            started = RUNTIME.marker_selection.begin_edit_draft(self.target_pair_id, runtime=RUNTIME)
        else:
            started = RUNTIME.marker_selection.begin_create_draft(runtime=RUNTIME)
        if not started:
            self._report_input_rejection(context, RUNTIME.marker_selection.message)
            return {"CANCELLED"}
        if not RUNTIME.undo_journal.begin_scope(baseline):
            RUNTIME.marker_selection.restore_snapshot(baseline)
            self._report_input_rejection(context, "마커 드래프트의 되돌리기 범위를 시작하지 못했습니다.")
            return {"CANCELLED"}
        RUNTIME.modal_active = True
        RUNTIME.clear_marker_command()
        self._space_navigation_active = False
        self._lifecycle_timer = context.window_manager.event_timer_add(
            time_step=0.1,
            window=context.window,
        )
        RUNTIME.marker_modal_timer = self._lifecycle_timer
        context.window_manager.modal_handler_add(self)
        request_redraw()
        return {"RUNNING_MODAL"}

    def _report_input_rejection(self, context, message: str) -> None:
        """입력 거부 이유를 후보·Undo 변경 없이 상태 표시줄과 화면에 전달합니다."""
        RUNTIME.marker_selection.message = message
        set_window_status_text(context.window, message)
        self.report({"WARNING"}, message)
        request_redraw()

    def _remove_lifecycle_timer(self, context) -> None:
        """모달 명령을 소비할 Window Timer를 누수 없이 제거합니다."""
        timer = getattr(self, "_lifecycle_timer", None)
        if timer is None:
            return
        try:
            context.window_manager.event_timer_remove(timer)
        except (ReferenceError, RuntimeError):
            pass
        if RUNTIME.marker_modal_timer is timer:
            RUNTIME.marker_modal_timer = None
        self._lifecycle_timer = None

    def _finish(self, context, result: set[str]) -> set[str]:
        """모달 종료 뒤 Timer·명령·UIList 미러와 화면을 정리합니다."""
        self._remove_lifecycle_timer(context)
        self._space_navigation_active = False
        RUNTIME.modal_active = False
        RUNTIME.clear_marker_command()
        sync_marker_pair_list(context.window_manager)
        request_redraw()
        return result

    def _cancel_selection(self, context) -> set[str]:
        """드래프트 범위를 시작 전 스냅샷으로 되돌리고 모달을 종료합니다."""
        RUNTIME.undo_journal.cancel_scope(RUNTIME.marker_selection)
        RUNTIME.marker_selection.message = RUNTIME.undo_journal.last_action
        return self._finish(context, {"CANCELLED"})

    def _confirm_selection(self, context) -> set[str] | None:
        """입력 지문을 검사한 뒤 드래프트를 하나의 생성 또는 편집 명령으로 확정합니다."""
        reason = RUNTIME.marker_selection.draft_geometry_rejection_message()
        if reason is not None:
            self._report_input_rejection(context, reason)
            return None
        before_commit = RUNTIME.marker_selection.snapshot()
        draft = RUNTIME.marker_selection.draft
        pair = RUNTIME.marker_selection.commit_draft()
        if pair is None or draft is None:
            request_redraw()
            return None
        if draft.mode == DRAFT_MODE_CREATE:
            action, kind = "마커 쌍 추가", MARKER_COMMAND_PAIR_CREATE
        else:
            action, kind = "마커 쌍 편집", MARKER_COMMAND_PAIR_EDIT
        if not RUNTIME.undo_journal.commit_scope(RUNTIME.marker_selection.snapshot(), action, kind):
            RUNTIME.marker_selection.restore_snapshot(before_commit)
            if not RUNTIME.undo_journal.scope_active:
                RUNTIME.marker_selection.cancel_draft()
                RUNTIME.marker_selection.message = "Undo 이력을 확정하지 못해 현재 입력을 취소했습니다."
                self.report({"ERROR"}, "마커 쌍 Undo 이력을 확정하지 못했습니다.")
                return self._finish(context, {"CANCELLED"})
            self.report({"ERROR"}, "마커 쌍 Undo 이력을 확정하지 못했습니다.")
            RUNTIME.marker_selection.message = "Undo 이력을 확정하지 못했습니다. 현재 입력을 다시 확인하십시오."
            request_redraw()
            return None
        RUNTIME.marker_selection.message = RUNTIME.undo_journal.last_action
        notify_project_state_changed("마커 쌍 확정")
        return self._finish(context, {"FINISHED"})

    def _apply_pending_command(self, context) -> set[str] | None:
        """N 패널 명령을 취소 우선순위로 모달 소유 상태에 적용합니다."""
        command = RUNTIME.take_marker_command()
        if command == MARKER_COMMAND_CANCEL:
            return self._cancel_selection(context)
        if command == MARKER_COMMAND_CONFIRM:
            return self._confirm_selection(context)
        return None

    def modal(self, context, event):
        """탐색을 허용하며 마커 선택·입력 지문·역할 종류의 오류를 드래프트 변경 전에 보고합니다."""
        if not is_session_active_for_window(context.window):
            return self._cancel_selection(context)
        command_result = self._apply_pending_command(context)
        if command_result is not None:
            return command_result
        if _is_undo_event(event):
            return {"PASS_THROUGH"}
        self._space_navigation_active = synchronize_space_navigation_state(
            event,
            getattr(self, "_space_navigation_active", False),
        )
        if event.type == "WINDOW_DEACTIVATE":
            return {"PASS_THROUGH"}
        if event.type == "SPACE":
            return {"PASS_THROUGH"}
        if event.type in {"ESC", "RIGHTMOUSE"}:
            # 드래프트 종료는 N 패널의 확정·취소 버튼 명령만 소유합니다.
            return {"PASS_THROUGH"}
        if event.type in NAVIGATION_EVENTS:
            return {"PASS_THROUGH"}
        if event.type != "LEFTMOUSE" or event.value != "PRESS":
            return {"PASS_THROUGH"}
        if _has_navigation_chord(event, getattr(self, "_space_navigation_active", False)):
            return {"PASS_THROUGH"}
        if context.window is None or not refresh_registry(context.window):
            self.report({"ERROR"}, "COLMAP Align 메인 듀얼 뷰 구성이 유효하지 않습니다.")
            return self._cancel_selection(context)

        viewport = find_viewport_under_mouse(context.window, event.mouse_x, event.mouse_y)
        if viewport is None:
            # N 패널·헤더 등의 클릭은 UI 오퍼레이터가 처리하도록 통과시킵니다.
            return {"PASS_THROUGH"}
        area, region, role = viewport
        region_3d = area.spaces.active.region_3d
        mouse_region = (event.mouse_x - region.x, event.mouse_y - region.y)
        reason = RUNTIME.marker_selection.draft_geometry_rejection_message()
        if reason is not None:
            self._report_input_rejection(context, reason)
            return {"RUNNING_MODAL"}
        try:
            picked = pick_nearest_point(role, region, region_3d, mouse_region)
        except ModelRepositoryError as error:
            self._report_input_rejection(context, str(error))
            return {"RUNNING_MODAL"}
        if picked is None:
            return {"PASS_THROUGH"}
        pick_arguments = {
            "model_id": picked.model_id,
            "point_kind": getattr(picked, "point_kind", "COLMAP_POINT"),
            "source_point_id": getattr(picked, "source_point_id", picked.point3d_id),
            "geometry_source": getattr(picked, "geometry_source", MARKER_GEOMETRY_SOURCE_MAIN),
        }
        if not RUNTIME.marker_selection.can_select_draft_point(
            role, picked.point3d_id, picked.position, **pick_arguments,
        ):
            reason = RUNTIME.marker_selection._draft_pick_rejection_message(
                role, picked.point3d_id, picked.position, **pick_arguments,
            )
            if reason and reason != "이미 선택한 점입니다.":
                self._report_input_rejection(context, reason)
                return {"RUNNING_MODAL"}
            return {"PASS_THROUGH"}
        before = RUNTIME.marker_selection.snapshot()
        changed = RUNTIME.marker_selection.select_draft_point(
            role, picked.point3d_id, picked.position, **pick_arguments,
        )
        if changed:
            after = RUNTIME.marker_selection.snapshot()
            action = "A점 선택" if role == ROLE_REFERENCE else "P점 선택"
            RUNTIME.undo_journal.record_draft(before, after, action)
            request_redraw()
            return {"RUNNING_MODAL"}
        return {"PASS_THROUGH"}

    def cancel(self, context):
        """Blender가 모달을 외부 취소해도 드래프트 범위를 안전하게 정리합니다."""
        if RUNTIME.undo_journal.scope_active:
            self._cancel_selection(context)
        else:
            self._finish(context, {"CANCELLED"})


CLASSES = (
    COLMAPALIGN_OT_add_marker_pair,
    COLMAPALIGN_OT_edit_marker_pair,
    COLMAPALIGN_OT_delete_marker_pair,
    COLMAPALIGN_OT_confirm_marker_selection,
    COLMAPALIGN_OT_cancel_marker_selection,
    COLMAPALIGN_OT_undo_marker_selection,
    COLMAPALIGN_OT_redo_marker_selection,
    COLMAPALIGN_OT_marker_pick_modal,
)
