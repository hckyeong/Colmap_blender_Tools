"""Blender Export 진행 작업과 임시 UI 자원의 수명주기를 관리합니다."""

from __future__ import annotations

from dataclasses import dataclass
from time import monotonic
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from .progress_overlay import (
    remove_progress_overlay_draw_handler,
    request_progress_overlay_redraw,
)

if TYPE_CHECKING:
    from ..core.export_job import ExportTransactionJob
    from ..core.export_types import ExportPlan
    from .runtime_state import DatasetRuntimeState


@dataclass(slots=True)
class ExportProgressSession:
    """대형 Core Job을 RNA Property에 넣지 않는 메모리 전용 세션입니다."""

    job_id: str
    dataset_uuid: str
    state: DatasetRuntimeState
    plan: ExportPlan
    job: ExportTransactionJob
    scene: Any
    started_at: float
    timer: Any = None
    window_manager: Any = None
    overlay_handle: Any = None
    runtime_committed: bool = False
    completion_result: Any = None
    completion_warnings: tuple[str, ...] = ()

    @property
    def elapsed_seconds(self) -> float:
        return max(0.0, monotonic() - self.started_at)


_SESSIONS: dict[str, ExportProgressSession] = {}
_ACTIVE_JOB_ID: str | None = None


def create_export_progress_session(
    scene,
    state: DatasetRuntimeState,
    plan: ExportPlan,
) -> ExportProgressSession:
    """동시에 하나의 Export만 허용하고 Core Job을 등록합니다."""

    global _ACTIVE_JOB_ID
    if _ACTIVE_JOB_ID is not None:
        raise RuntimeError("이미 Export 진행 작업이 등록되어 있습니다.")

    # Core 상태 머신은 이 경계에서만 생성하며 Blender UI를 알지 못합니다.
    from ..core.export_job import ExportTransactionJob

    job_id = uuid4().hex
    session = ExportProgressSession(
        job_id=job_id,
        dataset_uuid=state.dataset_uuid,
        state=state,
        plan=plan,
        job=ExportTransactionJob(plan, state.current_reconstruction),
        scene=scene,
        started_at=monotonic(),
    )
    _SESSIONS[job_id] = session
    _ACTIVE_JOB_ID = job_id
    return session


def get_export_progress_session(job_id: str) -> ExportProgressSession | None:
    return _SESSIONS.get(job_id)


def active_export_progress_session() -> ExportProgressSession | None:
    if _ACTIVE_JOB_ID is None:
        return None
    return _SESSIONS.get(_ACTIVE_JOB_ID)


def export_progress_session_count() -> int:
    """자동 테스트에서 Registry 누수를 확인하는 읽기 전용 경계입니다."""

    return len(_SESSIONS)


def attach_export_progress_ui(
    job_id: str,
    *,
    timer,
    window_manager,
    overlay_handle,
) -> None:
    """Modal Operator가 만든 Timer와 Overlay를 세션 소유로 이전합니다."""

    session = _SESSIONS[job_id]
    if session.timer is not None or session.overlay_handle is not None:
        raise RuntimeError("Export 진행 UI가 이미 연결되어 있습니다.")
    session.timer = timer
    session.window_manager = window_manager
    session.overlay_handle = overlay_handle


def _remove_session_ui(session: ExportProgressSession) -> None:
    if session.timer is not None and session.window_manager is not None:
        try:
            session.window_manager.event_timer_remove(session.timer)
        except (ReferenceError, RuntimeError, ValueError):
            pass
        session.timer = None
        session.window_manager = None

    if session.overlay_handle is not None:
        remove_progress_overlay_draw_handler(session.overlay_handle)
        session.overlay_handle = None


def release_export_progress_session(
    job_id: str,
    *,
    reset_running: bool = True,
) -> ExportProgressSession | None:
    """Core 정리 후 Timer, Overlay와 Registry를 공통 경계에서 제거합니다."""

    global _ACTIVE_JOB_ID
    session = _SESSIONS.pop(job_id, None)
    if session is None:
        return None
    abort_error: Exception | None = None
    try:
        if not session.job.done and not session.job.failed:
            session.job.abort()
    except Exception as exc:
        abort_error = exc
    finally:
        _remove_session_ui(session)
        if _ACTIVE_JOB_ID == job_id:
            _ACTIVE_JOB_ID = None

        if reset_running:
            try:
                settings = session.scene.colmap_import
                settings.export_running = False
            except (AttributeError, ReferenceError, RuntimeError):
                pass
        request_export_progress_redraw()
    if abort_error is not None:
        raise abort_error
    return session


def clear_export_progress_sessions() -> None:
    """Extension 해제 시 남은 진행 UI와 Registry를 대칭적으로 정리합니다."""

    for job_id in tuple(_SESSIONS):
        session = _SESSIONS.get(job_id)
        try:
            release_export_progress_session(job_id)
        except Exception as exc:
            if session is None:
                continue
            try:
                settings = session.scene.colmap_import
                settings.export_status = (
                    "Export 강제 종료 중 rollback에 실패했습니다. "
                    f"원본과 복구 자료를 확인하십시오: {exc}"
                )
            except (AttributeError, ReferenceError, RuntimeError):
                pass


def abandon_export_progress_sessions_for_global_undo() -> None:
    """이전 Scene을 읽지 않고 Export UI callback과 Registry만 폐기합니다."""

    global _ACTIVE_JOB_ID
    sessions = tuple(_SESSIONS.values())
    _SESSIONS.clear()
    _ACTIVE_JOB_ID = None
    for session in sessions:
        if session.timer is not None and session.window_manager is not None:
            try:
                session.window_manager.event_timer_remove(session.timer)
            except (ReferenceError, RuntimeError, ValueError):
                pass
        if session.overlay_handle is not None:
            remove_progress_overlay_draw_handler(session.overlay_handle)


def request_export_progress_redraw() -> None:
    """기존 Export 호출 경계를 공통 Overlay redraw로 연결합니다."""

    request_progress_overlay_redraw()
