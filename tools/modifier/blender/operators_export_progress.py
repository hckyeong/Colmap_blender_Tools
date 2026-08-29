"""Core Export Job을 Blender TIMER와 중앙 진행 Overlay에 연결합니다."""

from __future__ import annotations

import bpy
from bpy.props import StringProperty

from .export_completion import (
    export_error_message,
    finish_export_success,
)
from .export_progress_session import (
    active_export_progress_session,
    attach_export_progress_ui,
    get_export_progress_session,
    release_export_progress_session,
    request_export_progress_redraw,
)
from .progress_overlay import (
    ProgressOverlayView,
    add_progress_overlay_draw_handler,
    compact_path,
    draw_progress_overlay,
    remove_progress_overlay_draw_handler,
)
from .runtime_state import assert_export_runtime_token


# Core budget보다 짧게 예약해 완료된 tick 뒤 불필요한 대기 시간을 만들지 않습니다.
# Blender는 Modal 반환 사이에 redraw 기회를 가지므로 100 Hz 요청이 실제 작업 중
# 화면을 100 Hz로 강제 갱신하는 것은 아닙니다.
_TIMER_INTERVAL_SECONDS = 0.01
_WORK_BUDGET_MS = 25.0

_PHASE_LABELS = {
    "CREATED": "Preparing export",
    "PREPARE": "Preparing transaction",
    "COPY_SOURCE": "Copying source files",
    "WRITE_STAGING": "Writing staging files",
    "PARSE_STAGING": "Reading staging files",
    "VALIDATE_STAGING": "Validating staging model",
    "COMPARE_STAGING": "Comparing runtime and staging",
    "HASH_STAGING": "Hashing staging files",
    "COMMIT": "Replacing source files",
    "BUILD_RESULT": "Building export result",
    "CLEANUP": "Cleaning temporary files",
    "DONE": "Export complete",
    "FAILED": "Export failed",
}


def _phase_name(snapshot) -> str:
    phase = getattr(snapshot, "phase", None)
    return str(getattr(phase, "name", phase or "CREATED"))


def _snapshot_fraction(snapshot) -> float:
    try:
        return max(0.0, min(1.0, float(snapshot.overall_fraction)))
    except (AttributeError, TypeError, ValueError):
        return 0.0


def _export_progress_overlay_view(session) -> ProgressOverlayView:
    """Export Core Snapshot을 공통 표시 View로 변환합니다."""

    snapshot = session.job.progress
    # Core 파일 Transaction 뒤 Blender Runtime 전환까지 성공해야만 100%를 표시합니다.
    fraction = (
        1.0
        if session.runtime_committed
        else min(_snapshot_fraction(snapshot), 0.999)
    )
    phase_key = _phase_name(snapshot)
    phase_text = _PHASE_LABELS.get(phase_key, phase_key)
    completed = int(getattr(snapshot, "completed_units", 0))
    total = int(getattr(snapshot, "total_units", 0))
    units_text = (
        f"{completed:,} / {total:,}"
        if total > 0
        else "Calculating work units"
    )
    backup_text = "Enabled" if session.plan.backup_enabled else "Disabled"
    return ProgressOverlayView(
        title="COLMAP Export",
        phase=phase_text,
        fraction=fraction,
        completed_units=completed,
        total_units=total,
        elapsed_seconds=session.elapsed_seconds,
        detail_lines=(
            f"Processed: {units_text}    Elapsed: {session.elapsed_seconds:.1f}s",
            f"Target: {compact_path(session.plan.source.model_directory)}",
            f"Backup: {backup_text} · Blender input is locked during export.",
        ),
    )


def _draw_export_overlay() -> None:
    """사용자 입력으로 닫히지 않는 Export 중앙 진행창을 그립니다."""

    session = active_export_progress_session()
    if session is None:
        return
    draw_progress_overlay(_export_progress_overlay_view(session))


def _show_message_popup(
    context,
    *,
    title: str,
    icon: str,
    lines: tuple[str, ...],
) -> None:
    if bpy.app.background or context.window is None:
        return

    def draw_message(menu, _context):
        for line in lines:
            menu.layout.label(text=line)

    try:
        context.window_manager.popup_menu(
            draw_message,
            title=title,
            icon=icon,
        )
    except (AttributeError, ReferenceError, RuntimeError, ValueError):
        # 파일 Transaction 결과는 부가 결과창 표시 실패와 분리합니다.
        pass


def _show_success_popup(context, session, result) -> None:
    settings = context.scene.colmap_import
    backup_text = (
        compact_path(result.backup_snapshot, maximum=58)
        if result.backup_snapshot is not None
        else "Not used"
    )
    _show_message_popup(
        context,
        title="COLMAP Export Complete",
        icon="CHECKMARK",
        lines=(
            f"Camera: {settings.camera_count}",
            f"Image: {settings.image_count}",
            f"Point3D: {settings.point3d_count}",
            f"Backup: {backup_text}",
            f"Elapsed: {session.elapsed_seconds:.1f}s",
        ),
    )


def _show_failure_popup(context, message: str) -> None:
    lines = tuple(
        part.strip()
        for part in message.split(" | ")
        if part.strip()
    ) or ("An unknown export error occurred.",)
    _show_message_popup(
        context,
        title="COLMAP Export Failed",
        icon="ERROR",
        lines=lines,
    )


def _assert_session_runtime_current(context, session) -> None:
    """각 tick에서 확인창 당시 Runtime 객체와 token이 유지되는지 검사합니다."""

    current = assert_export_runtime_token(
        context.scene,
        session.plan.runtime_token,
    )
    if (
        current is not session.state
        or current.current_reconstruction
        is not session.state.current_reconstruction
    ):
        raise ValueError(
            "The runtime cache changed during export. Start the export again."
        )


class COLMAP_OT_export_progress(bpy.types.Operator):
    """확인된 Export Job 하나를 TIMER 단위로 진행합니다."""

    bl_idname = "colmap.export_progress"
    bl_label = "COLMAP Export Progress"
    bl_description = "Displays COLMAP export progress"
    bl_options = {"INTERNAL", "BLOCKING"}

    job_id: StringProperty(options={"HIDDEN", "SKIP_SAVE"})

    @classmethod
    def poll(cls, _context):
        return active_export_progress_session() is not None

    def invoke(self, context, event):
        del event
        session = get_export_progress_session(self.job_id)
        if session is None:
            self.report({"ERROR"}, "The export progress session was not found.")
            return {"CANCELLED"}
        if session.timer is not None:
            self.report({"WARNING"}, "The export progress UI is already running.")
            return {"CANCELLED"}
        if context.window is None:
            try:
                release_export_progress_session(self.job_id)
            except Exception as exc:
                self.report({"ERROR"}, f"Failed to clean up the export job: {exc}")
                return {"CANCELLED"}
            self.report(
                {"ERROR"},
                "No Blender window is available for the export progress UI.",
            )
            return {"CANCELLED"}

        overlay_handle = None
        timer = None
        try:
            overlay_handle = add_progress_overlay_draw_handler(
                _draw_export_overlay,
            )
            timer = context.window_manager.event_timer_add(
                _TIMER_INTERVAL_SECONDS,
                window=context.window,
            )
            attach_export_progress_ui(
                self.job_id,
                timer=timer,
                window_manager=context.window_manager,
                overlay_handle=overlay_handle,
            )
            context.window_manager.modal_handler_add(self)
        except Exception as exc:
            if timer is not None:
                try:
                    context.window_manager.event_timer_remove(timer)
                except (ReferenceError, RuntimeError, ValueError):
                    pass
            if overlay_handle is not None:
                remove_progress_overlay_draw_handler(overlay_handle)
            try:
                release_export_progress_session(self.job_id)
                message = f"Failed to start the export progress UI: {exc}"
            except Exception as abort_exc:
                message = (
                    "Failed to start and clean up the export progress UI: "
                    f"{exc} | {abort_exc}"
                )
            context.scene.colmap_import.export_status = message
            self.report({"ERROR"}, message)
            return {"CANCELLED"}

        request_export_progress_redraw()
        return {"RUNNING_MODAL"}

    def _fail(self, context, session, exc: Exception):
        message = export_error_message(exc)
        context.scene.colmap_import.export_status = f"Export failed: {message}"
        release_export_progress_session(self.job_id)
        _show_failure_popup(context, message)
        self.report({"ERROR"}, message)
        return {"CANCELLED"}

    def _finish(self, context, session):
        if not session.runtime_committed:
            try:
                result = session.job.result
                warnings = finish_export_success(context, session.state, result)
            except Exception as exc:
                message = (
                    "The files were exported, but the Blender runtime update failed. "
                    f"Import the dataset again: {exc}"
                )
                context.scene.colmap_import.export_status = message
                release_export_progress_session(self.job_id)
                _show_failure_popup(context, message)
                self.report({"ERROR"}, message)
                return {"CANCELLED"}

            session.completion_result = result
            session.completion_warnings = warnings
            session.runtime_committed = True
            request_export_progress_redraw()
            # Blender가 Runtime까지 반영된 100% 상태를 한 번 그린 뒤 결과창으로 전환합니다.
            return {"RUNNING_MODAL"}

        result = session.completion_result
        warnings = session.completion_warnings
        release_export_progress_session(self.job_id)
        _show_success_popup(context, session, result)
        if warnings:
            self.report({"WARNING"}, warnings[0])
        else:
            self.report({"INFO"}, context.scene.colmap_import.export_status)
        return {"FINISHED"}

    def modal(self, context, event):
        session = get_export_progress_session(self.job_id)
        if session is None:
            return {"CANCELLED"}

        # Esc, 우클릭과 외부 클릭을 포함한 사용자 입력은 Export를 취소하지 않습니다.
        if event.type != "TIMER":
            return {"RUNNING_MODAL"}

        if session.runtime_committed:
            return self._finish(context, session)

        try:
            _assert_session_runtime_current(context, session)
            if not session.job.done:
                session.job.advance(work_budget_ms=_WORK_BUDGET_MS)
        except Exception as exc:
            return self._fail(context, session, exc)

        request_export_progress_redraw()
        if session.job.failed:
            error = session.job.error
            if error is None:
                error = RuntimeError("The export failed without error details.")
            return self._fail(context, session, error)
        if session.job.done:
            return self._finish(context, session)
        return {"RUNNING_MODAL"}

    def cancel(self, context):
        """Blender가 Operator를 강제 종료할 때 UI 자원만 안전하게 회수합니다."""

        session = get_export_progress_session(self.job_id)
        if session is not None:
            try:
                release_export_progress_session(self.job_id)
            except Exception as exc:
                context.scene.colmap_import.export_status = (
                    "Rollback failed while the export operator was closing. "
                    f"Check the source and recovery data: {exc}"
                )
                return
            context.scene.colmap_import.export_status = (
                "Blender closed the export progress operator."
            )


CLASSES = (COLMAP_OT_export_progress,)
