"""Import와 Export가 공유하는 중앙 진행 Overlay를 렌더링합니다."""

from __future__ import annotations

from dataclasses import dataclass
from math import cos, isfinite, pi, sin
from pathlib import Path
from typing import Callable

import blf
import bpy
import gpu
from gpu_extras.batch import batch_for_shader


OVERLAY_WIDTH = 520
OVERLAY_HEIGHT = 196
OVERLAY_CORNER_RADIUS = 8.0
OUTER_BORDER_CORNER_RADIUS = 10.0
PROGRESS_CORNER_RADIUS = 6.0
ROUNDED_CORNER_SEGMENTS = 8
MAXIMUM_DETAIL_LINES = 3

_OVERLAY_MINIMUM_WIDTH = 300
_OVERLAY_VIEWPORT_MARGIN = 40
_OVERLAY_PADDING = 22
_DETAIL_LINE_OFFSETS = (61, 39, 17)


def _validate_single_line_text(
    value: str,
    *,
    field_name: str,
    require_ascii: bool,
) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string.")
    if "\n" in value or "\r" in value:
        raise ValueError(f"{field_name} must fit on one line.")
    if require_ascii and not value.isascii():
        raise ValueError(f"{field_name} must use English ASCII text.")


@dataclass(frozen=True, slots=True)
class ProgressOverlayView:
    """Core 진행 Snapshot을 Blender 표시 정보로 고정한 불변 View입니다."""

    title: str
    phase: str
    fraction: float
    completed_units: int
    total_units: int
    elapsed_seconds: float
    detail_lines: tuple[str, ...]

    def __post_init__(self) -> None:
        _validate_single_line_text(
            self.title,
            field_name="title",
            require_ascii=True,
        )
        _validate_single_line_text(
            self.phase,
            field_name="phase",
            require_ascii=True,
        )

        if not isinstance(self.fraction, (int, float)) or isinstance(
            self.fraction,
            bool,
        ):
            raise ValueError("fraction must be a finite number.")
        fraction = float(self.fraction)
        if not isfinite(fraction) or not 0.0 <= fraction <= 1.0:
            raise ValueError("fraction must be between 0.0 and 1.0.")

        for field_name, value in (
            ("completed_units", self.completed_units),
            ("total_units", self.total_units),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"{field_name} must be a non-negative integer.")
        if self.completed_units > self.total_units:
            raise ValueError(
                "completed_units must not be greater than total_units."
            )

        if not isinstance(self.elapsed_seconds, (int, float)) or isinstance(
            self.elapsed_seconds,
            bool,
        ):
            raise ValueError("elapsed_seconds must be a finite number.")
        elapsed_seconds = float(self.elapsed_seconds)
        if not isfinite(elapsed_seconds) or elapsed_seconds < 0.0:
            raise ValueError(
                "elapsed_seconds must be a finite non-negative number."
            )

        if not isinstance(self.detail_lines, tuple):
            raise ValueError("detail_lines must be an immutable tuple.")
        if len(self.detail_lines) > MAXIMUM_DETAIL_LINES:
            raise ValueError(
                f"detail_lines must contain at most {MAXIMUM_DETAIL_LINES} lines."
            )
        for index, line in enumerate(self.detail_lines):
            _validate_single_line_text(
                line,
                field_name=f"detail_lines[{index}]",
                require_ascii=False,
            )


def rounded_rectangle_vertices(
    x: float,
    y: float,
    width: float,
    height: float,
    corner_radius: float,
    corner_segments: int = ROUNDED_CORNER_SEGMENTS,
) -> tuple[tuple[float, float], ...]:
    """중심점과 시계 반대 방향 외곽점으로 라운드 사각형을 구성합니다."""

    if width <= 0.0 or height <= 0.0:
        return ()

    radius = max(
        0.0,
        min(float(corner_radius), width * 0.5, height * 0.5),
    )
    center = (x + width * 0.5, y + height * 0.5)
    if radius == 0.0:
        perimeter = (
            (x, y),
            (x + width, y),
            (x + width, y + height),
            (x, y + height),
        )
        return (center, *perimeter, perimeter[0])

    segments = max(1, int(corner_segments))
    quarter_turn = pi * 0.5
    corners = (
        (x + radius, y + radius, pi),
        (x + width - radius, y + radius, pi + quarter_turn),
        (x + width - radius, y + height - radius, 0.0),
        (x + radius, y + height - radius, quarter_turn),
    )
    perimeter = []
    for center_x, center_y, start_angle in corners:
        for step in range(segments + 1):
            angle = start_angle + quarter_turn * (step / segments)
            perimeter.append(
                (
                    center_x + cos(angle) * radius,
                    center_y + sin(angle) * radius,
                )
            )
    return (center, *perimeter, perimeter[0])


def _draw_rectangle(
    shader,
    x: float,
    y: float,
    width: float,
    height: float,
    color: tuple[float, float, float, float],
    *,
    corner_radius: float = 0.0,
) -> None:
    vertices = rounded_rectangle_vertices(
        x,
        y,
        width,
        height,
        corner_radius,
    )
    if not vertices:
        return
    batch = batch_for_shader(
        shader,
        "TRI_FAN",
        {"pos": vertices},
    )
    shader.bind()
    shader.uniform_float("color", color)
    batch.draw(shader)


def _draw_text(
    text: str,
    x: float,
    y: float,
    size: int,
    color: tuple[float, float, float, float],
) -> None:
    font_id = 0
    blf.size(font_id, size)
    blf.color(font_id, *color)
    blf.position(font_id, x, y, 0)
    blf.draw(font_id, text)


def compact_path(path: Path | str, maximum: int = 66) -> str:
    """긴 경로의 뒤쪽을 보존하여 Overlay 폭에 맞게 축약합니다."""

    if maximum < 2:
        raise ValueError("maximum must be at least 2.")
    text = str(path)
    if len(text) <= maximum:
        return text
    return f"…{text[-(maximum - 1):]}"


def draw_progress_overlay(view: ProgressOverlayView) -> None:
    """현재 3D Viewport 중앙에 검증된 진행 View를 그립니다."""

    context = bpy.context
    region = getattr(context, "region", None)
    area = getattr(context, "area", None)
    if region is None or area is None or area.type != "VIEW_3D":
        return

    width = min(
        OVERLAY_WIDTH,
        max(_OVERLAY_MINIMUM_WIDTH, region.width - _OVERLAY_VIEWPORT_MARGIN),
    )
    height = OVERLAY_HEIGHT
    x = (region.width - width) * 0.5
    y = (region.height - height) * 0.5

    shader = gpu.shader.from_builtin("UNIFORM_COLOR")
    gpu.state.blend_set("ALPHA")
    try:
        _draw_rectangle(
            shader,
            x - 2,
            y - 2,
            width + 4,
            height + 4,
            (0.05, 0.05, 0.05, 0.95),
            corner_radius=OUTER_BORDER_CORNER_RADIUS,
        )
        _draw_rectangle(
            shader,
            x,
            y,
            width,
            height,
            (0.12, 0.12, 0.12, 0.98),
            corner_radius=OVERLAY_CORNER_RADIUS,
        )

        bar_x = x + _OVERLAY_PADDING
        bar_y = y + 91
        bar_width = width - _OVERLAY_PADDING * 2
        _draw_rectangle(
            shader,
            bar_x,
            bar_y,
            bar_width,
            20,
            (0.03, 0.03, 0.03, 1.0),
            corner_radius=PROGRESS_CORNER_RADIUS,
        )
        if view.fraction > 0.0:
            _draw_rectangle(
                shader,
                bar_x,
                bar_y,
                bar_width * view.fraction,
                20,
                (0.18, 0.46, 0.78, 1.0),
                corner_radius=PROGRESS_CORNER_RADIUS,
            )

        _draw_text(
            view.title,
            x + _OVERLAY_PADDING,
            y + height - 36,
            18,
            (1.0, 1.0, 1.0, 1.0),
        )
        _draw_text(
            view.phase,
            x + _OVERLAY_PADDING,
            y + height - 66,
            15,
            (0.9, 0.9, 0.9, 1.0),
        )
        _draw_text(
            f"{view.fraction * 100.0:5.1f}%",
            x + width - 78,
            bar_y + 3,
            13,
            (1.0, 1.0, 1.0, 1.0),
        )
        for line, offset in zip(view.detail_lines, _DETAIL_LINE_OFFSETS):
            size = 12 if offset == _DETAIL_LINE_OFFSETS[0] else 11
            color = (
                (0.82, 0.82, 0.82, 1.0)
                if offset == _DETAIL_LINE_OFFSETS[0]
                else (0.68, 0.68, 0.68, 1.0)
            )
            _draw_text(
                line,
                x + _OVERLAY_PADDING,
                y + offset,
                size,
                color,
            )
    finally:
        gpu.state.blend_set("NONE")


def add_progress_overlay_draw_handler(
    draw_callback: Callable[[], None],
):
    """공통 POST_PIXEL Draw Handler를 등록합니다."""

    if bpy.app.background:
        return None
    return bpy.types.SpaceView3D.draw_handler_add(
        draw_callback,
        (),
        "WINDOW",
        "POST_PIXEL",
    )


def remove_progress_overlay_draw_handler(overlay_handle) -> None:
    """등록된 공통 Draw Handler를 안전하게 제거합니다."""

    if overlay_handle is None:
        return
    try:
        bpy.types.SpaceView3D.draw_handler_remove(
            overlay_handle,
            "WINDOW",
        )
    except (ReferenceError, RuntimeError, ValueError):
        pass


def request_progress_overlay_redraw() -> None:
    """열린 모든 3D Viewport에 진행 Overlay 갱신을 요청합니다."""

    window_manager = getattr(bpy.context, "window_manager", None)
    windows = getattr(window_manager, "windows", ())
    try:
        for window in windows:
            screen = getattr(window, "screen", None)
            for area in getattr(screen, "areas", ()):
                if area.type == "VIEW_3D":
                    area.tag_redraw()
    except (AttributeError, ReferenceError, RuntimeError):
        # 파일 load와 Extension 해제 중에는 Window/Area RNA가 먼저 사라질 수 있습니다.
        pass
