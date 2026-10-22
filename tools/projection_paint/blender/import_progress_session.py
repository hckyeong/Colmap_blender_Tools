"""COLMAP Import 작업과 진행 Overlay 자원의 수명주기를 관리합니다."""

from __future__ import annotations

from dataclasses import dataclass
from time import monotonic
from typing import Any, Callable
from uuid import uuid4

from .progress_overlay import (
    remove_progress_overlay_draw_handler,
    request_progress_overlay_redraw,
)


@dataclass(slots=True)
class ImportProgressSession:
    """Core 준비부터 Scene Commit까지 한 Import 실행 상태를 소유합니다."""

    job_id: str
    scene: Any
    request_factory: Callable[[Any], Any]
    started_at: float
    stage: str = "PREPARE_REQUEST"
    request_job: Any = None
    request: Any = None
    job: Any = None
    validation_job: Any = None
    result: Any = None
    timer: Any = None
    window_manager: Any = None
    overlay_handle: Any = None
    final_screen_shown: bool = False
    last_redraw_time: float = 0.0
    last_redraw_phase: str = ""
    last_work_time: float = 0.0

    @property
    def elapsed_seconds(self) -> float:
        """Import 시작 뒤 경과한 단조 시간을 초 단위로 반환합니다."""

        return max(0.0, monotonic() - self.started_at)


_SESSIONS: dict[str, ImportProgressSession] = {}
_ACTIVE_JOB_ID: str | None = None


def create_import_progress_session(
    scene: Any,
    request_factory: Callable[[Any], Any],
) -> ImportProgressSession:
    """동시에 하나의 Import만 허용하고 새 진행 세션을 등록합니다."""

    global _ACTIVE_JOB_ID
    if _ACTIVE_JOB_ID is not None:
        raise RuntimeError("이미 COLMAP Import가 진행 중입니다.")
    job_id = uuid4().hex
    session = ImportProgressSession(
        job_id=job_id,
        scene=scene,
        request_factory=request_factory,
        started_at=monotonic(),
    )
    _SESSIONS[job_id] = session
    _ACTIVE_JOB_ID = job_id
    return session


def get_import_progress_session(job_id: str) -> ImportProgressSession | None:
    """Job ID와 일치하는 Import 진행 세션을 반환합니다."""

    return _SESSIONS.get(job_id)


def active_import_progress_session() -> ImportProgressSession | None:
    """현재 활성 Import 진행 세션을 반환합니다."""

    if _ACTIVE_JOB_ID is None:
        return None
    return _SESSIONS.get(_ACTIVE_JOB_ID)


def import_progress_session_count() -> int:
    """테스트와 진단을 위해 등록된 Import 세션 수를 반환합니다."""

    return len(_SESSIONS)


def attach_import_progress_ui(
    job_id: str,
    *,
    timer: Any,
    window_manager: Any,
    overlay_handle: Any,
) -> None:
    """Modal Operator가 만든 Timer와 Overlay를 세션 소유로 이전합니다."""

    session = _SESSIONS[job_id]
    if session.timer is not None or session.overlay_handle is not None:
        raise RuntimeError("Import 진행 UI가 이미 연결되어 있습니다.")
    session.timer = timer
    session.window_manager = window_manager
    session.overlay_handle = overlay_handle


def _remove_session_ui(session: ImportProgressSession) -> None:
    """진행 세션이 소유한 Event Timer와 Draw Handler를 해제합니다."""

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


def release_import_progress_session(
    job_id: str,
    *,
    reset_status: bool = False,
) -> ImportProgressSession | None:
    """Import Job과 UI 자원을 정리하고 Registry에서 세션을 제거합니다."""

    global _ACTIVE_JOB_ID
    session = _SESSIONS.pop(job_id, None)
    if session is None:
        return None
    cleanup_error: Exception | None = None
    try:
        request_job = session.request_job
        if request_job is not None and not bool(
            getattr(request_job, "done", False)
        ):
            try:
                request_job.abort()
            except Exception as exc:
                cleanup_error = exc
        validation_job = session.validation_job
        if validation_job is not None and not bool(
            getattr(validation_job, "done", False)
        ):
            try:
                validation_job.abort()
            except Exception as exc:
                if cleanup_error is None:
                    cleanup_error = exc
        job = session.job
        state = getattr(job, "state", None)
        if job is not None and str(getattr(state, "value", state)) != "ACTIVE":
            try:
                job.abort()
            except Exception as exc:
                if cleanup_error is None:
                    cleanup_error = exc
    finally:
        _remove_session_ui(session)
        if _ACTIVE_JOB_ID == job_id:
            _ACTIVE_JOB_ID = None
        if reset_status:
            try:
                settings = session.scene.projection_paint
                if settings.status_kind == "RUNNING":
                    settings.status_kind = "ERROR"
                    settings.status = (
                        "Import가 Blender 상태 변경으로 중단되었습니다. "
                        "기존 성공 장면은 유지되었습니다."
                    )
            except (AttributeError, ReferenceError, RuntimeError):
                pass
        request_progress_overlay_redraw()
    if cleanup_error is not None:
        raise RuntimeError(f"Import 진행 자원 정리에 실패했습니다: {cleanup_error}")
    return session


def clear_import_progress_sessions(*_args: Any) -> None:
    """파일 상태 변경과 Extension 해제에서 남은 Import 작업을 정리합니다."""

    for job_id in tuple(_SESSIONS):
        try:
            release_import_progress_session(job_id, reset_status=True)
        except Exception:
            pass
