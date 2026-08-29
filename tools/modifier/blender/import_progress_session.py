"""Blender Import 진행 작업과 임시 UI 자원의 수명주기를 관리합니다."""

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
    from ..core.import_job import ImportLoadJob
    from ..core.import_types import ImportPlan
    from .import_scene_job import ImportSceneBuildJob


@dataclass(slots=True)
class ImportProgressSession:
    """Core와 Blender staging 상태를 RNA 밖에서 한 수명주기로 소유합니다."""

    job_id: str
    scene: Any
    plan: ImportPlan
    job: ImportLoadJob
    started_at: float
    timer: Any = None
    window_manager: Any = None
    overlay_handle: Any = None
    stage: str = "CORE"
    runtime_state: Any = None
    display_loaded: Any = None
    scene_job: ImportSceneBuildJob | None = None
    verification_job: Any = None
    image_entries: tuple[Any, ...] = ()
    image_cursor: int = 0
    missing_images: list[str] | None = None
    changed_images: list[str] | None = None
    scene_committed: bool = False
    completion_warnings: tuple[str, ...] = ()
    final_screen_shown: bool = False

    @property
    def elapsed_seconds(self) -> float:
        return max(0.0, monotonic() - self.started_at)


_SESSIONS: dict[str, ImportProgressSession] = {}
_ACTIVE_JOB_ID: str | None = None


def create_import_progress_session(
    scene,
    plan: ImportPlan,
) -> ImportProgressSession:
    """동시에 하나의 Import만 허용하고 Core Job을 등록합니다."""

    global _ACTIVE_JOB_ID
    if _ACTIVE_JOB_ID is not None:
        raise RuntimeError("이미 Import 진행 작업이 등록되어 있습니다.")

    from .export_progress_session import active_export_progress_session

    if active_export_progress_session() is not None:
        raise RuntimeError("Export 진행 중에는 Import를 시작할 수 없습니다.")

    from ..core.import_job import ImportLoadJob

    job_id = uuid4().hex
    session = ImportProgressSession(
        job_id=job_id,
        scene=scene,
        plan=plan,
        job=ImportLoadJob(plan),
        started_at=monotonic(),
    )
    _SESSIONS[job_id] = session
    _ACTIVE_JOB_ID = job_id
    return session


def get_import_progress_session(job_id: str) -> ImportProgressSession | None:
    return _SESSIONS.get(job_id)


def active_import_progress_session() -> ImportProgressSession | None:
    if _ACTIVE_JOB_ID is None:
        return None
    return _SESSIONS.get(_ACTIVE_JOB_ID)


def import_progress_session_count() -> int:
    """자동 테스트에서 Registry 누수를 확인하는 읽기 전용 경계입니다."""

    return len(_SESSIONS)


def attach_import_progress_ui(
    job_id: str,
    *,
    timer,
    window_manager,
    overlay_handle,
) -> None:
    """Modal Operator가 만든 Timer와 Overlay를 세션 소유로 이전합니다."""

    session = _SESSIONS[job_id]
    if session.timer is not None or session.overlay_handle is not None:
        raise RuntimeError("Import 진행 UI가 이미 연결되어 있습니다.")
    session.timer = timer
    session.window_manager = window_manager
    session.overlay_handle = overlay_handle


def _remove_session_ui(session: ImportProgressSession) -> None:
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
    """Core·staging·UI 자원과 Registry를 한 경계에서 제거합니다."""

    global _ACTIVE_JOB_ID
    session = _SESSIONS.pop(job_id, None)
    if session is None:
        return None

    cleanup_errors: list[Exception] = []
    try:
        if not session.job.done:
            try:
                session.job.abort()
            except Exception as exc:
                cleanup_errors.append(exc)
        if session.scene_job is not None and not session.scene_committed:
            try:
                session.scene_job.abort()
            except Exception as exc:
                cleanup_errors.append(exc)
        if session.verification_job is not None:
            try:
                session.verification_job.close()
            except Exception as exc:
                cleanup_errors.append(exc)
            session.verification_job = None
    finally:
        _remove_session_ui(session)
        if _ACTIVE_JOB_ID == job_id:
            _ACTIVE_JOB_ID = None
        if reset_status:
            try:
                settings = session.scene.colmap_import
                if settings.status_kind == "IMPORTING":
                    settings.status_kind = "ERROR"
                    settings.status = (
                        "Import가 Blender 상태 변경으로 중단되었습니다. "
                        "기존 성공 장면은 유지되었습니다."
                    )
            except (AttributeError, ReferenceError, RuntimeError):
                pass
        request_progress_overlay_redraw()

    if cleanup_errors:
        details = " | ".join(str(error) for error in cleanup_errors)
        raise RuntimeError(f"Import 진행 자원 정리에 실패했습니다: {details}")
    return session


def clear_import_progress_sessions() -> None:
    """파일 상태 변경과 Extension 해제에서 남은 Import 작업을 정리합니다."""

    for job_id in tuple(_SESSIONS):
        try:
            release_import_progress_session(job_id, reset_status=True)
        except Exception:
            # 파일 load/unregister 중에는 유효한 Scene RNA가 없을 수 있으므로
            # 가능한 자원을 모두 정리한 뒤 추가 예외를 전파하지 않습니다.
            pass


def abandon_import_progress_sessions_for_global_undo() -> None:
    """이전 Scene을 읽지 않고 Import UI callback과 Registry만 폐기합니다."""

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
