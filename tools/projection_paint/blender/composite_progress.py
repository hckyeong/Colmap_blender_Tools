"""Composite Layer 생성의 단계별 진행 Overlay 수명주기를 관리합니다."""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from time import monotonic
from typing import Any
from uuid import uuid4

from .progress_overlay import (
    ProgressOverlayView,
    add_progress_overlay_draw_handler,
    remove_progress_overlay_draw_handler,
    request_progress_overlay_redraw,
)
from .image_backend import image_contract


@dataclass(slots=True)
class CompositeProgressSession:
    """Native Bake 시작부터 완료까지의 읽기 전용 Overlay 상태와 UI 자원을 보관합니다."""

    session_id: str
    started_at: float
    total_steps: int
    completed_steps: int = 0
    phase: str = "Validating"
    detail: str = "대상 상태를 확인하는 중입니다"
    memory_bytes: int = 0
    overlay_handle: Any = None
    window_manager: Any = None
    timer: Any = None
    indeterminate: bool = False
    bake_device_detail: str = ""
    image_size: tuple[int, int] = (0, 0)
    tile_count: int = 0
    sample_count: int = 1

    @property
    def elapsed_seconds(self) -> float:
        """Composite 작업 시작 뒤 경과한 단조 시간을 초 단위로 반환합니다."""
        return max(0.0, monotonic() - self.started_at)


_SESSIONS: dict[str, CompositeProgressSession] = {}
_PROCESS_DOT_INTERVAL_SECONDS = 0.4
_PROCESS_DOT_FRAME_COUNT = 4


def _memory_label(memory_bytes: int) -> str:
    """바이트 단위 예상 Image 메모리를 사람이 읽기 쉬운 단위로 변환합니다."""
    mib = max(0.0, float(memory_bytes) / (1024.0 * 1024.0))
    return f"예상 최소 이미지 메모리 {mib:.1f} MiB"


def composite_process_label(elapsed_seconds: float) -> str:
    """경과 시간에 따라 점 한 개부터 네 개까지 반복되는 Process 문구를 반환합니다."""
    elapsed = float(elapsed_seconds)
    if not isfinite(elapsed) or elapsed < 0.0:
        elapsed = 0.0
    frame = int(
        elapsed / _PROCESS_DOT_INTERVAL_SECONDS + 1.0e-9
    ) % _PROCESS_DOT_FRAME_COUNT
    return f"Process{'.' * (frame + 1)}"


def composite_progress_view(session: CompositeProgressSession) -> ProgressOverlayView:
    """실제 완료 단계 또는 Native Bake 대기 상태를 중앙 Overlay View로 변환합니다."""
    width, height = session.image_size
    elapsed_seconds = session.elapsed_seconds
    if session.indeterminate:
        detail_lines = (
            str(session.detail),
            f"{session.bake_device_detail} · EMIT · {width}×{height} · 버퍼 {session.tile_count}개",
            f"하단 100% 뒤에도 이미지 변환 중 · Samples {session.sample_count} · 경과 {elapsed_seconds:.1f}초",
        )
    else:
        detail_lines = (
            str(session.detail),
            _memory_label(session.memory_bytes),
            f"경과 시간 {elapsed_seconds:.1f}초",
        )
    return ProgressOverlayView(
        title="Composite Layer",
        phase=str(session.phase),
        fraction=(
            float(session.completed_steps) / float(session.total_steps)
            if session.total_steps and not session.indeterminate
            else 0.0
        ),
        completed_units=int(session.completed_steps),
        total_units=int(session.total_steps),
        elapsed_seconds=elapsed_seconds,
        detail_lines=detail_lines,
        indeterminate=bool(session.indeterminate),
        activity_text=composite_process_label(elapsed_seconds),
    )


def _draw_session(session_id: str) -> None:
    """현재 등록된 Composite 진행 세션 하나를 중앙 3D Viewport에 그립니다."""
    from .progress_overlay import draw_progress_overlay

    session = _SESSIONS.get(str(session_id))
    if session is not None:
        draw_progress_overlay(composite_progress_view(session))


def create_composite_progress(context: Any, memory_bytes: int) -> CompositeProgressSession:
    """Composite 생성 직후 읽기 전용 중앙 Overlay를 등록합니다."""
    session = CompositeProgressSession(
        session_id=uuid4().hex,
        started_at=monotonic(),
        total_steps=7,
        memory_bytes=max(0, int(memory_bytes)),
    )
    _SESSIONS[session.session_id] = session
    session.window_manager = getattr(context, "window_manager", None)
    session.overlay_handle = add_progress_overlay_draw_handler(
        lambda: _draw_session(session.session_id)
    )
    request_progress_overlay_redraw()
    return session


def attach_composite_progress_timer(
    session: CompositeProgressSession,
    timer: Any,
) -> None:
    """Native Bake를 감시하는 Modal Event Timer를 진행 세션의 정리 대상으로 기록합니다."""
    if session.timer is not None:
        raise RuntimeError("Composite 진행 Event Timer가 이미 연결되어 있습니다.")
    session.timer = timer


def composite_progress_session_count() -> int:
    """테스트와 진단을 위해 현재 등록된 Composite 진행 세션 수를 반환합니다."""
    return len(_SESSIONS)


def update_composite_progress(
    session: CompositeProgressSession,
    completed_steps: int,
    phase: str,
    detail: str,
) -> None:
    """실제로 끝난 Composite 단계 수만 읽기 전용 Overlay에 반영합니다."""
    session.completed_steps = max(0, min(int(completed_steps), session.total_steps))
    session.phase = str(phase)
    session.detail = str(detail)
    request_progress_overlay_redraw()


def begin_composite_native_bake(
    session: CompositeProgressSession,
    *,
    phase: str,
    detail: str,
    device_detail: str,
    image: Any,
    sample_count: int,
) -> None:
    """Native Bake 중 가짜 백분율 없이 장치·Image 계약만 Overlay에 표시합니다."""
    session.indeterminate = True
    session.phase = str(phase)
    session.detail = str(detail)
    session.bake_device_detail = str(device_detail)
    session.image_size = tuple(int(value) for value in getattr(image, "size", (0, 0)))
    session.tile_count = len(image_contract(image).tile_numbers)
    session.sample_count = int(sample_count)
    request_progress_overlay_redraw()


def end_composite_native_bake(session: CompositeProgressSession) -> None:
    """Native Bake 종료 뒤 실제 완료 단계 표시로 Overlay를 되돌립니다."""
    session.indeterminate = False
    request_progress_overlay_redraw()


def release_composite_progress(session: CompositeProgressSession | None) -> None:
    """완료·실패·취소 뒤 Composite 읽기 전용 Overlay와 Timer를 해제합니다."""
    if session is None:
        return
    _SESSIONS.pop(session.session_id, None)
    if session.timer is not None and session.window_manager is not None:
        try:
            session.window_manager.event_timer_remove(session.timer)
        except (ReferenceError, RuntimeError, ValueError):
            pass
        session.timer = None
    remove_progress_overlay_draw_handler(session.overlay_handle)
    session.overlay_handle = None
    session.window_manager = None
    request_progress_overlay_redraw()


def clear_composite_progress_sessions(*_args: Any) -> None:
    """불러오기·Undo·Redo·애드온 해제 전에 남은 Composite 진행 UI를 정리합니다."""
    for session in tuple(_SESSIONS.values()):
        release_composite_progress(session)


__all__ = [
    "CompositeProgressSession",
    "attach_composite_progress_timer",
    "begin_composite_native_bake",
    "clear_composite_progress_sessions",
    "composite_progress_session_count",
    "composite_progress_view",
    "composite_process_label",
    "create_composite_progress",
    "end_composite_native_bake",
    "release_composite_progress",
    "update_composite_progress",
]
