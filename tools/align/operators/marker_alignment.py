"""마커 Sim(3) 미리보기 UI 전환과 후속 ICP 인계 경계를 처리합니다."""

from __future__ import annotations

from typing import Any

import bpy

from ..alignment import (
    IcpRuntimeError,
    MarkerAlignmentError,
    approve_icp_result,
    begin_icp_job,
    begin_marker_preview,
    cancel_marker_preview,
    discard_icp_preview,
    invalidate_icp,
    is_icp_result_approvable,
    poll_icp_job,
    request_icp_cancel,
    set_marker_preview_view_snapshot,
)
from ..constants import (
    ICP_ALIGNMENT_PHASE_CANCEL_PENDING,
    ICP_ALIGNMENT_PHASE_COMMITTED,
    ICP_ALIGNMENT_PHASE_FAILED,
    ICP_ALIGNMENT_PHASE_IDLE,
    ICP_ALIGNMENT_PHASE_PREPARING,
    ICP_ALIGNMENT_PHASE_PREVIEW,
    ICP_ALIGNMENT_PHASE_RUNNING,
)
from ..project_persistence.runtime import restore_input_block_reason
from ..properties import configured_icp_python_path, sync_marker_pair_list
from ..session import is_session_active_for_window, session_align_workspace
from ..state import RUNTIME
from ..status import set_workspace_status_text
from ..viewport.renderer import request_redraw
from .workspace import (
    WorkspaceConfigurationError,
    capture_moving_view_snapshot,
    find_main_window,
    restore_moving_view_snapshot,
)


_ICP_POLL_INTERVAL_SECONDS = 0.1
_AUTOMATIC_ICP_PRESET = "PRECISE"
_AUTOMATIC_ICP_BACKEND = "SMALL_GICP"


def _icp_state() -> Any | None:
    """현재 Runtime의 ICP 상태 객체를 없을 때도 안전하게 반환합니다."""
    return getattr(RUNTIME, "icp_alignment", None)


def icp_phase() -> str:
    """UI가 판단할 ICP phase를 항상 문자열로 반환합니다."""
    return str(getattr(_icp_state(), "phase", ICP_ALIGNMENT_PHASE_IDLE))


def is_icp_running() -> bool:
    """worker 진행 또는 취소 대기처럼 즉시 입력을 잠가야 하는지 반환합니다."""
    return icp_phase() in {
        ICP_ALIGNMENT_PHASE_PREPARING,
        ICP_ALIGNMENT_PHASE_RUNNING,
        ICP_ALIGNMENT_PHASE_CANCEL_PENDING,
    }


def is_icp_result_preview_active() -> bool:
    """비파괴 ICP 결과 preview를 검토 중인지 반환합니다."""
    return icp_phase() == ICP_ALIGNMENT_PHASE_PREVIEW


def is_icp_result_approval_available() -> bool:
    """PASS·REVIEW의 수치적으로 유효한 결과 미리보기에만 승인을 허용합니다."""
    state = _icp_state()
    return is_icp_result_preview_active() and is_icp_result_approvable(
        getattr(state, "result", None),
    )


def is_icp_input_locked() -> bool:
    """ICP 실행 또는 결과 preview 때문에 마커·레이어 입력을 막아야 하는지 반환합니다."""
    return is_icp_running() or is_icp_result_preview_active()


def _clear_icp_poll_timer(*, unregister: bool) -> None:
    """등록한 app timer 참조를 해제하고 필요할 때만 Blender 등록도 취소합니다."""
    callback = getattr(RUNTIME, "icp_timer", None)
    if unregister and callable(callback):
        try:
            if bpy.app.timers.is_registered(callback):
                bpy.app.timers.unregister(callback)
        except (ReferenceError, RuntimeError, ValueError):
            pass
    RUNTIME.icp_timer = None


def stop_icp_poll_timer_for_lifecycle() -> None:
    """등록 해제·load 전에 UI timer만 멈추고 worker 종료는 runtime에 맡깁니다."""
    _clear_icp_poll_timer(unregister=True)


def _icp_terminal_message() -> str:
    """terminal phase의 마지막 오류 또는 완료 안내를 상태 표시줄 문구로 반환합니다."""
    state = _icp_state()
    message = str(getattr(state, "last_error", "")).strip()
    if is_icp_result_preview_active():
        return "ICP 결과 미리보기를 표시합니다. 결과를 검토한 뒤 정렬 승인하십시오."
    if message:
        return message
    if icp_phase() == ICP_ALIGNMENT_PHASE_FAILED:
        return "ICP 작업이 실패하여 실행 전 초기 정렬로 돌아갔습니다."
    if icp_phase() == ICP_ALIGNMENT_PHASE_COMMITTED:
        return "ICP 정렬을 승인했습니다."
    return "ICP 작업을 종료하고 실행 전 초기 정렬을 유지합니다."


def _poll_icp_job_timer() -> float | None:
    """0.1초 간격으로 worker 상태를 읽고 terminal 시 timer를 정리합니다."""
    if not is_icp_running():
        _clear_icp_poll_timer(unregister=False)
        request_redraw()
        return None
    try:
        poll_icp_job(RUNTIME)
    except IcpRuntimeError as error:
        set_workspace_status_text(session_align_workspace(), str(error))
    request_redraw()
    if is_icp_running():
        return _ICP_POLL_INTERVAL_SECONDS
    message = _icp_terminal_message()
    set_workspace_status_text(session_align_workspace(), message)
    _clear_icp_poll_timer(unregister=False)
    return None


def _register_icp_poll_timer() -> None:
    """ICP 실행마다 중복 없이 전용 app timer callback을 등록합니다."""
    callback = getattr(RUNTIME, "icp_timer", None)
    if callable(callback):
        try:
            if bpy.app.timers.is_registered(callback):
                return
        except (ReferenceError, RuntimeError, ValueError):
            pass
    RUNTIME.icp_timer = _poll_icp_job_timer
    bpy.app.timers.register(_poll_icp_job_timer, first_interval=_ICP_POLL_INTERVAL_SECONDS)


def is_marker_preview_active() -> bool:
    """현재 Runtime에 취소 가능한 marker preview가 있는지 반환합니다."""
    preview_state = getattr(RUNTIME, "marker_alignment", None)
    return bool(preview_state is not None and preview_state.is_active)


def _export_input_block_reason() -> str | None:
    """COLMAP 또는 OBJ 내보내기 중 새 정렬 입력을 막을 사유를 반환합니다."""

    if bool(getattr(RUNTIME.merge_export, "is_running", False)):
        return "COLMAP Export가 끝난 뒤 정렬을 시작하십시오."
    if bool(getattr(RUNTIME.obj_export, "is_running", False)):
        return "OBJ Export가 끝난 뒤 정렬을 시작하십시오."
    return None


def marker_preview_start_block_reason() -> str | None:
    """미리보기 시작 전 UI가 즉시 알 수 있는 차단 사유를 반환합니다."""
    restore_reason = restore_input_block_reason()
    if restore_reason is not None:
        return restore_reason
    export_reason = _export_input_block_reason()
    if export_reason is not None:
        return export_reason
    if is_icp_input_locked():
        return "ICP 실행 또는 결과 미리보기 중에는 마커 정렬 미리보기를 변경할 수 없습니다."
    if RUNTIME.import_in_progress:
        return "COLMAP 가져오기가 끝난 뒤 마커 미리보기를 시작하십시오."
    if RUNTIME.modal_active or RUNTIME.marker_selection.has_draft:
        return "열린 마커 입력을 확정하거나 취소한 뒤 마커 미리보기를 시작하십시오."
    if RUNTIME.workspace_setup_task is not None:
        return "작업공간 구성이 끝난 뒤 마커 미리보기를 시작하십시오."
    selected = RUNTIME.model_catalog.selected_inactive_model
    if selected is None or not selected.is_visible:
        return "정렬할 표시 상태의 P 데이터 레이어를 선택하십시오."
    if len(RUNTIME.marker_selection.completed_pairs) < 3:
        return "마커 정렬에는 완성된 A/P 대응쌍이 최소 3개 필요합니다."
    return None


def direct_align_start_block_reason() -> str | None:
    """마커 없이 A 하나 이상과 P 하나로 Align을 시작할 수 없는 이유를 반환합니다."""

    restore_reason = restore_input_block_reason()
    if restore_reason is not None:
        return restore_reason
    export_reason = _export_input_block_reason()
    if export_reason is not None:
        return export_reason

    if is_icp_input_locked():
        return "ICP 실행 또는 결과 미리보기 중에는 새 Align을 시작할 수 없습니다."
    if RUNTIME.import_in_progress:
        return "COLMAP 가져오기가 끝난 뒤 Align을 시작하십시오."
    if RUNTIME.modal_active or RUNTIME.marker_selection.has_draft:
        return "열린 마커 입력을 확정하거나 취소한 뒤 Align을 시작하십시오."
    if RUNTIME.workspace_setup_task is not None:
        return "작업공간 구성이 끝난 뒤 Align을 시작하십시오."
    catalog = RUNTIME.model_catalog
    if not catalog.selected_reference_model_ids:
        return "Align을 시작하려면 A 레이어를 하나 이상 선택하십시오."
    selected_moving_ids = catalog.selected_moving_model_ids
    if not selected_moving_ids:
        return "정렬할 P 레이어를 하나 선택하십시오."
    if len(selected_moving_ids) > 1:
        return "Align은 P 레이어 하나에만 적용할 수 있습니다."
    selected = catalog.selected_alignment_moving_model
    if selected is None:
        return "정렬할 P 레이어를 하나 선택하십시오."
    if not selected.is_visible:
        return "정렬할 표시 상태의 P 데이터 레이어를 선택하십시오."
    return None


def align_start_block_reason(context: bpy.types.Context) -> str | None:
    """고정 자동 정렬의 초기값 계약에 맞는 Align 차단 사유를 반환합니다."""

    if is_marker_preview_active():
        return None
    del context
    return direct_align_start_block_reason()


def marker_preview_button_label() -> str:
    """같은 UI 슬롯에서 사용할 3점 마커 정렬 전환 버튼 문구를 반환합니다."""
    return "3점 마커 정렬 취소" if is_marker_preview_active() else "3점 마커 정렬"


def _report_status(operator: bpy.types.Operator, level: set[str], message: str) -> None:
    """오퍼레이터 결과를 상태 표시줄과 Blender 보고 영역에 함께 전달합니다."""
    operator.report(level, message)
    set_workspace_status_text(session_align_workspace(), message)


def _main_window_for_context(context: bpy.types.Context) -> Any:
    """현재 UI 컨텍스트에 연결된 COLMAP Align 메인 Window를 검증해 반환합니다."""
    window = find_main_window(context.window_manager, context.window)
    if window is None:
        raise WorkspaceConfigurationError("마커 미리보기를 표시할 COLMAP Align 메인 창을 찾을 수 없습니다.")
    return window


def _restore_preview_view_snapshot(context: bpy.types.Context, snapshot: Any) -> bool:
    """취소된 preview의 P 뷰 snapshot을 현재 메인 Window에 최선으로 복원합니다."""
    if snapshot is None:
        return False
    try:
        window = _main_window_for_context(context)
    except WorkspaceConfigurationError:
        return False
    return restore_moving_view_snapshot(window, snapshot)


def cancel_marker_preview_from_ui(context: bpy.types.Context) -> bool:
    """버튼·Esc 공통 경로에서 preview를 취소하고 이전 P 시점을 복원합니다."""
    if is_icp_input_locked():
        return False
    snapshot = cancel_marker_preview(RUNTIME)
    if snapshot is None:
        return False
    _restore_preview_view_snapshot(context, snapshot)
    request_redraw()
    return True


def discard_marker_preview_for_lifecycle() -> bool:
    """세션 종료·파일 load·등록 해제에서 preview 참조만 안전하게 정리합니다."""
    if not is_marker_preview_active():
        return False
    cancel_marker_preview(RUNTIME)
    request_redraw()
    return True


def _begin_marker_preview_from_ui(context: bpy.types.Context):
    """Scene의 마커 스케일 설정으로 solver를 실행하고 취소용 P 시점을 보관합니다."""
    reason = marker_preview_start_block_reason()
    if reason is not None:
        raise MarkerAlignmentError(reason)
    window = _main_window_for_context(context)
    snapshot = capture_moving_view_snapshot(window)
    settings = context.scene.colmap_align_settings
    result = begin_marker_preview(
        RUNTIME,
        use_uniform_scale=bool(settings.marker_use_uniform_scale),
    )
    try:
        set_marker_preview_view_snapshot(RUNTIME, snapshot)
    except (MarkerAlignmentError, ValueError):
        cancelled_snapshot = cancel_marker_preview(RUNTIME)
        restore_moving_view_snapshot(window, cancelled_snapshot or snapshot)
        raise
    return result


class COLMAPALIGN_OT_toggle_marker_preview(bpy.types.Operator):
    """같은 버튼 슬롯에서 marker preview 시작과 취소를 전환합니다."""

    bl_idname = "colmap_align.toggle_marker_preview"
    bl_label = "마커 정렬 미리보기"
    bl_description = "마커 Sim(3) 결과를 미리 보거나 이전 P 위치로 되돌립니다"
    bl_options = {"REGISTER"}

    @classmethod
    def poll(cls, context):
        """활성 COLMAP Align 세션에서만 marker preview 전환을 허용합니다."""
        return bool(
            is_session_active_for_window(context.window)
            and not is_icp_input_locked()
            and (is_marker_preview_active() or not RUNTIME.import_in_progress)
        )

    def execute(self, context):
        """현재 phase에 따라 preview를 시작하거나 취소하고 즉시 다시 그립니다."""
        if is_icp_input_locked():
            _report_status(
                self,
                {"WARNING"},
                "ICP 실행 또는 결과 미리보기 중에는 마커 미리보기를 취소할 수 없습니다.",
            )
            return {"CANCELLED"}
        if is_marker_preview_active():
            if not cancel_marker_preview_from_ui(context):
                _report_status(self, {"WARNING"}, "취소할 마커 정렬 미리보기가 없습니다.")
                return {"CANCELLED"}
            _report_status(self, {"INFO"}, "마커 정렬 미리보기를 취소하고 이전 P 시점으로 복원했습니다.")
            return {"FINISHED"}
        try:
            result = _begin_marker_preview_from_ui(context)
        except (MarkerAlignmentError, WorkspaceConfigurationError, ValueError) as error:
            _report_status(self, {"WARNING"}, str(error))
            request_redraw()
            return {"CANCELLED"}
        _report_status(
            self,
            {"INFO"},
            f"마커 {result.metrics.count}쌍의 정렬 미리보기를 표시합니다.",
        )
        request_redraw()
        return {"FINISHED"}


class COLMAPALIGN_OT_align_marker_preview(bpy.types.Operator):
    """마커 미리보기 또는 현재 P 위치에서 정밀 Small GICP·Refine을 시작합니다."""

    bl_idname = "colmap_align.align_marker_preview"
    bl_label = "Align"
    bl_description = "마커 미리보기 또는 현재 P 위치에서 Small GICP 정밀 후 Refine 정밀을 자동 실행합니다"
    bl_options = {"INTERNAL"}

    @classmethod
    def poll(cls, context):
        """직접 호출도 활성 COLMAP Align 세션에서만 안전하게 받습니다."""
        return bool(
            is_session_active_for_window(context.window)
            and align_start_block_reason(context) is None
            and not is_icp_input_locked()
            and not RUNTIME.import_in_progress
            and not RUNTIME.modal_active
            and not RUNTIME.marker_selection.has_draft
        )

    def execute(self, context):
        """고정된 정밀 Small GICP·Refine 파이프라인을 시작하고 polling을 등록합니다."""
        uses_marker_preview = is_marker_preview_active()
        start_reason = align_start_block_reason(context)
        if start_reason is not None:
            _report_status(self, {"WARNING"}, start_reason)
            return {"CANCELLED"}
        if is_icp_input_locked():
            _report_status(self, {"WARNING"}, "현재 ICP 상태에서는 새 Align을 시작할 수 없습니다.")
            return {"CANCELLED"}
        try:
            begin_icp_job(
                RUNTIME,
                preset=_AUTOMATIC_ICP_PRESET,
                scale_refine_enabled=True,
                solver_backend=_AUTOMATIC_ICP_BACKEND,
                open3d_python_path=configured_icp_python_path(context),
            )
            _register_icp_poll_timer()
        except (IcpRuntimeError, MarkerAlignmentError, RuntimeError, ValueError) as error:
            if is_icp_running():
                try:
                    invalidate_icp(RUNTIME, str(error))
                except IcpRuntimeError:
                    pass
            _report_status(self, {"WARNING"}, str(error))
            request_redraw()
            return {"CANCELLED"}
        _report_status(
            self,
            {"INFO"},
            "자동 2단계 정렬 · Small GICP 정밀 → Refine 정밀 · A 그룹 전체 "
            f"{len(RUNTIME.model_catalog.active_models())}개 기준으로 "
            f"{'마커 미리보기' if uses_marker_preview else '현재 P 위치'} 초기값 정렬을 시작했습니다.",
        )
        request_redraw()
        return {"FINISHED"}


class COLMAPALIGN_OT_validate_open3d_environment(bpy.types.Operator):
    """애드온 환경설정의 외부 CPython Open3D 환경을 명시적으로 검사합니다."""

    bl_idname = "colmap_align.validate_open3d_environment"
    bl_label = "Open3D 환경 검사"
    bl_description = "CPython 3.12 x64와 Open3D 0.19.0 등록 API를 검사합니다"
    bl_options = {"INTERNAL"}

    @classmethod
    def poll(cls, _context):
        """ICP 작업이 실행 중이 아닐 때만 환경 검사를 허용합니다."""

        return not is_icp_running()

    def execute(self, context):
        """Blender 밖의 검사 프로세스를 실행하고 확인된 버전을 보고합니다."""

        python_path = configured_icp_python_path(context)
        if not python_path:
            self.report(
                {"WARNING"},
                "Open3D 검증 환경을 사용할 수 없습니다. CPython 3.12 실행 경로를 설정하십시오.",
            )
            return {"CANCELLED"}
        try:
            from ..alignment.icp_backend import validate_open3d_environment

            environment = validate_open3d_environment(python_path)
        except (RuntimeError, OSError, ValueError) as error:
            self.report({"WARNING"}, str(error))
            return {"CANCELLED"}
        python_version = str(environment.get("worker_python_version", "3.12"))
        open3d_version = str(environment.get("open3d_version", "0.19.0"))
        self.report(
            {"INFO"},
            f"Open3D 환경 확인 완료 · Python {python_version} · Open3D {open3d_version}",
        )
        return {"FINISHED"}


class COLMAPALIGN_OT_validate_small_gicp_environment(bpy.types.Operator):
    """배포판 내장 또는 Full 빌드 외부 CPython small_gicp 환경을 검사합니다."""

    bl_idname = "colmap_align.validate_small_gicp_environment"
    bl_label = "small_gicp 환경 검사"
    bl_description = "CPython 3.12 x64와 small_gicp 1.0.1 GICP API를 검사합니다"
    bl_options = {"INTERNAL"}

    @classmethod
    def poll(cls, _context):
        """ICP 작업이 실행 중이 아닐 때만 환경 검사를 허용합니다."""

        return not is_icp_running()

    def execute(self, context):
        """Blender 밖의 검사 프로세스를 실행하고 확인된 버전을 보고합니다."""

        python_path = configured_icp_python_path(context)
        if not python_path:
            self.report(
                {"WARNING"},
                "small_gicp 환경을 사용할 수 없습니다. CPython 3.12 실행 경로를 설정하십시오.",
            )
            return {"CANCELLED"}
        try:
            from ..alignment.icp_backend import validate_small_gicp_environment

            environment = validate_small_gicp_environment(python_path)
        except (RuntimeError, OSError, ValueError) as error:
            self.report({"WARNING"}, str(error))
            return {"CANCELLED"}
        python_version = str(environment.get("worker_python_version", "3.12"))
        small_gicp_version = str(environment.get("small_gicp_version", "1.0.1"))
        self.report(
            {"INFO"},
            f"small_gicp 환경 확인 완료 · Python {python_version} · small_gicp {small_gicp_version}",
        )
        return {"FINISHED"}


class COLMAPALIGN_OT_cancel_icp(bpy.types.Operator):
    """실행 중인 ICP worker에만 취소 요청을 보내고 초기 정렬은 유지합니다."""

    bl_idname = "colmap_align.cancel_icp"
    bl_label = "ICP 취소"
    bl_description = "실행 중인 ICP 계산을 취소하고 실행 전 초기 정렬로 돌아갑니다"
    bl_options = {"INTERNAL"}

    @classmethod
    def poll(cls, context):
        """활성 세션의 취소 가능한 ICP 실행 상태에서만 버튼을 활성화합니다."""
        return bool(
            is_session_active_for_window(context.window)
            and icp_phase() in {ICP_ALIGNMENT_PHASE_PREPARING, ICP_ALIGNMENT_PHASE_RUNNING}
        )

    def execute(self, _context):
        """worker 취소 sentinel을 요청하고 완료 polling은 기존 timer에 맡깁니다."""
        try:
            requested = request_icp_cancel(RUNTIME)
        except IcpRuntimeError as error:
            _report_status(self, {"WARNING"}, str(error))
            return {"CANCELLED"}
        if not requested:
            _report_status(self, {"WARNING"}, "취소할 ICP 작업이 없습니다.")
            return {"CANCELLED"}
        _report_status(self, {"INFO"}, "ICP 취소를 요청했습니다. worker 종료를 확인하는 중입니다.")
        request_redraw()
        return {"FINISHED"}


class COLMAPALIGN_OT_discard_icp_preview(bpy.types.Operator):
    """ICP 결과 미리보기만 버리고 실행 전 초기 정렬을 다시 사용합니다."""

    bl_idname = "colmap_align.discard_icp_preview"
    bl_label = "결과 버리기"
    bl_description = "ICP 결과를 버리고 실행 전 초기 정렬로 돌아갑니다"
    bl_options = {"INTERNAL"}

    @classmethod
    def poll(cls, context):
        """활성 세션의 검토 가능한 ICP 결과에서만 버리기를 허용합니다."""
        return bool(
            is_session_active_for_window(context.window)
            and is_icp_result_preview_active()
        )

    def execute(self, _context):
        """공개 runtime API만 호출해 ICP preview를 정리합니다."""
        try:
            discard_icp_preview(RUNTIME)
        except IcpRuntimeError as error:
            _report_status(self, {"WARNING"}, str(error))
            return {"CANCELLED"}
        _report_status(self, {"INFO"}, "ICP 결과를 버리고 실행 전 초기 정렬로 돌아갔습니다.")
        request_redraw()
        return {"FINISHED"}


class COLMAPALIGN_OT_approve_icp_result(bpy.types.Operator):
    """검토한 ICP 결과를 확정하고 남아 있는 마커 쌍을 정리합니다."""

    bl_idname = "colmap_align.approve_icp_result"
    bl_label = "정렬 승인"
    bl_description = "ICP 결과를 확정하고 P 모델을 A 그룹에 편입한 뒤 남은 마커 쌍을 정리합니다"
    bl_options = {"REGISTER"}

    @classmethod
    def poll(cls, context):
        """활성 세션의 유효 ICP 결과 preview에서만 승인을 허용합니다."""
        return bool(
            is_session_active_for_window(context.window)
            and is_icp_result_approval_available()
        )

    def execute(self, context):
        """공개 승인 API를 실행하고 삭제된 마커 쌍의 UI 미러를 동기화합니다."""
        try:
            approve_icp_result(RUNTIME)
        except IcpRuntimeError as error:
            _report_status(self, {"WARNING"}, str(error))
            request_redraw()
            return {"CANCELLED"}
        sync_marker_pair_list(context.window_manager)
        _report_status(
            self,
            {"INFO"},
            "ICP 정렬을 승인하고 P 모델을 A 그룹에 편입한 뒤 남은 마커 쌍을 정리했습니다.",
        )
        request_redraw()
        return {"FINISHED"}


CLASSES = (
    COLMAPALIGN_OT_toggle_marker_preview,
    COLMAPALIGN_OT_align_marker_preview,
    # Open3D backend 환경 검사 operator는 구현 보존 상태로 등록하지 않습니다.
    # COLMAPALIGN_OT_validate_open3d_environment,
    COLMAPALIGN_OT_validate_small_gicp_environment,
    COLMAPALIGN_OT_cancel_icp,
    COLMAPALIGN_OT_discard_icp_preview,
    COLMAPALIGN_OT_approve_icp_result,
)
