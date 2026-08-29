"""Paint 준비 진행 Overlay와 Modal 작업 자원의 수명주기를 관리합니다."""

from __future__ import annotations

from dataclasses import dataclass
from time import monotonic
from typing import Any
from uuid import uuid4

from .progress_overlay import (
    remove_progress_overlay_draw_handler,
    request_progress_overlay_redraw,
)


@dataclass(slots=True)
class PaintProgressSession:
    """한 번의 Paint 이미지 준비와 Paint 진입 작업 및 UI 자원을 소유합니다."""

    job_id: str
    scene: Any
    job: Any
    started_at: float
    timer: Any = None
    window_manager: Any = None
    overlay_handle: Any = None
    final_screen_shown: bool = False

    @property
    def elapsed_seconds(self) -> float:
        """Paint 준비 시작 뒤 경과한 단조 시간을 초 단위로 반환합니다."""

        return max(0.0, monotonic() - self.started_at)


_SESSIONS: dict[str, PaintProgressSession] = {}
_ACTIVE_JOB_ID: str | None = None


def create_paint_progress_session(scene: Any, job: Any) -> PaintProgressSession:
    """동시에 하나의 Paint 준비만 허용하고 새 진행 세션을 등록합니다."""

    global _ACTIVE_JOB_ID
    if _ACTIVE_JOB_ID is not None:
        raise RuntimeError("이미 Paint 준비가 진행 중입니다.")
    job_id = uuid4().hex
    session = PaintProgressSession(
        job_id=job_id,
        scene=scene,
        job=job,
        started_at=monotonic(),
    )
    _SESSIONS[job_id] = session
    _ACTIVE_JOB_ID = job_id
    return session


def get_paint_progress_session(job_id: str) -> PaintProgressSession | None:
    """Job ID와 일치하는 Paint 준비 진행 세션을 반환합니다."""

    return _SESSIONS.get(job_id)


def active_paint_progress_session() -> PaintProgressSession | None:
    """현재 활성 Paint 준비 진행 세션을 반환합니다."""

    if _ACTIVE_JOB_ID is None:
        return None
    return _SESSIONS.get(_ACTIVE_JOB_ID)


def paint_progress_session_count() -> int:
    """테스트와 진단을 위해 등록된 Paint 준비 세션 수를 반환합니다."""

    return len(_SESSIONS)


def attach_paint_progress_ui(
    job_id: str,
    *,
    timer: Any,
    window_manager: Any,
    overlay_handle: Any,
) -> None:
    """Modal Operator가 만든 Timer와 Overlay를 진행 세션 소유로 이전합니다."""

    session = _SESSIONS[job_id]
    if session.timer is not None or session.overlay_handle is not None:
        raise RuntimeError("Paint 준비 진행 UI가 이미 연결되어 있습니다.")
    session.timer = timer
    session.window_manager = window_manager
    session.overlay_handle = overlay_handle


def _remove_session_ui(session: PaintProgressSession) -> None:
    """Paint 준비 세션이 소유한 Event Timer와 Draw Handler를 해제합니다."""

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


def detach_paint_progress_ui(job_id: str) -> PaintProgressSession | None:
    """완료된 변환 Overlay만 닫고 후속 Paint 진입 작업은 Registry에 유지합니다."""

    session = _SESSIONS.get(job_id)
    if session is None:
        return None
    _remove_session_ui(session)
    request_progress_overlay_redraw()
    return session


def release_paint_progress_session(
    job_id: str,
    *,
    reset_status: bool = False,
) -> PaintProgressSession | None:
    """Paint 준비 작업과 UI 자원을 정리하고 Registry에서 세션을 제거합니다."""

    global _ACTIVE_JOB_ID
    session = _SESSIONS.pop(job_id, None)
    if session is None:
        return None
    cleanup_error: Exception | None = None
    try:
        if not bool(getattr(session.job, "done", False)):
            try:
                session.job.abort()
            except Exception as exc:
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
                    settings.status = "Paint 준비가 Blender 상태 변경으로 중단되었습니다."
                    settings.paint_state = "ERROR"
            except (AttributeError, ReferenceError, RuntimeError):
                pass
        request_progress_overlay_redraw()
    if cleanup_error is not None:
        raise RuntimeError(f"Paint 준비 진행 자원 정리에 실패했습니다: {cleanup_error}")
    return session


def clear_paint_progress_sessions(*_args: Any) -> None:
    """파일 상태 변경과 Extension 해제에서 남은 Paint 준비 작업을 정리합니다."""

    for job_id in tuple(_SESSIONS):
        try:
            release_paint_progress_session(job_id, reset_status=True)
        except Exception:
            pass


__all__ = [
    "PaintProgressSession",
    "active_paint_progress_session",
    "attach_paint_progress_ui",
    "clear_paint_progress_sessions",
    "create_paint_progress_session",
    "detach_paint_progress_ui",
    "get_paint_progress_session",
    "paint_progress_session_count",
    "release_paint_progress_session",
]
