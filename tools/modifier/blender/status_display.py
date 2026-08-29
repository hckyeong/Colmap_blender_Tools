"""COLMAP Status를 고정 높이 스크롤 목록과 폭 대응 줄바꿈으로 표시합니다."""

from __future__ import annotations

import unicodedata

import bpy

from .. import __version__

STATUS_VISIBLE_ROWS = 5
STATUS_FILLER_TEXT = "\u00a0"
_MIN_LINE_CELLS = 10
_LIST_HORIZONTAL_GUTTER = 68.0
_APPROXIMATE_CELL_WIDTH = 7.0
_PENDING_REFRESHES: dict[int, tuple[object, int, float]] = {}


def display_cell_width(text: str) -> int:
    """동아시아 전각 문자를 두 칸으로 계산한 보수적인 표시 폭을 반환합니다."""
    width = 0
    for character in text:
        if character == "\t":
            width += 4
        elif unicodedata.east_asian_width(character) in {"F", "W"}:
            width += 2
        else:
            width += 1
    return width


def _prefix_index_within_cells(text: str, cell_limit: int) -> int:
    used = 0
    for index, character in enumerate(text):
        character_width = display_cell_width(character)
        if used + character_width > cell_limit:
            return index
        used += character_width
    return len(text)


def wrap_status_text(text: str, cell_limit: int) -> tuple[str, ...]:
    """공백을 우선 경계로 사용하되 긴 토큰도 잘리지 않게 강제 분할합니다."""
    limit = max(_MIN_LINE_CELLS, int(cell_limit))
    wrapped: list[str] = []
    paragraphs = text.splitlines() or [""]

    for paragraph in paragraphs:
        remaining = paragraph.strip()
        if not remaining:
            wrapped.append("")
            continue

        while display_cell_width(remaining) > limit:
            prefix_end = _prefix_index_within_cells(remaining, limit)
            if prefix_end <= 0:
                prefix_end = 1
            prefix = remaining[:prefix_end]
            whitespace_index = max(prefix.rfind(" "), prefix.rfind("\t"))
            if whitespace_index > 0:
                prefix_end = whitespace_index
            line = remaining[:prefix_end].rstrip()
            if not line:
                line = remaining[:prefix_end]
            wrapped.append(line)
            remaining = remaining[prefix_end:].lstrip()

        if remaining:
            wrapped.append(remaining)

    return tuple(wrapped)


def status_cell_limit(region_width: int, ui_scale: float) -> int:
    """UIList 여백과 스크롤바를 제외한 보수적인 한 줄 문자 폭을 계산합니다."""
    scale = max(float(ui_scale), 0.5)
    usable_width = max(
        _MIN_LINE_CELLS * _APPROXIMATE_CELL_WIDTH * scale,
        float(region_width) - (_LIST_HORIZONTAL_GUTTER * scale),
    )
    return max(
        _MIN_LINE_CELLS,
        int(usable_width / (_APPROXIMATE_CELL_WIDTH * scale)),
    )


def _status_messages(settings) -> tuple[tuple[str, str], ...]:
    """첫 실행 버전 또는 가장 마지막에 갱신된 상태 메시지 하나를 반환합니다."""
    message = settings.latest_status_message.strip()
    if not message:
        return ((f"Version: {__version__}", "NONE"),)
    return ((message, settings.latest_status_icon),)


def build_status_display_lines(
    settings,
    region_width: int,
    ui_scale: float,
) -> tuple[tuple[str, str], ...]:
    """현재 상태를 주어진 Sidebar 폭에 맞는 UIList 행으로 변환합니다."""
    cell_limit = status_cell_limit(region_width, ui_scale)
    lines: list[tuple[str, str]] = []
    for message, icon in _status_messages(settings):
        wrapped = wrap_status_text(message, cell_limit)
        for index, line in enumerate(wrapped):
            lines.append((line, icon if index == 0 else "NONE"))
    lines.extend(
        (STATUS_FILLER_TEXT, "NONE")
        for _index in range(max(0, STATUS_VISIBLE_ROWS - len(lines)))
    )
    return tuple(lines)


def sync_status_display_lines(
    settings,
    region_width: int,
    ui_scale: float,
) -> tuple[tuple[str, str], ...]:
    """폭 또는 상태가 달라졌을 때만 파생 UIList 행을 갱신합니다."""
    desired = build_status_display_lines(settings, region_width, ui_scale)
    current = tuple(
        (item.text, item.icon)
        for item in settings.status_display_lines
    )
    if current == desired:
        return desired

    settings.status_display_lines.clear()
    for text, icon in desired:
        item = settings.status_display_lines.add()
        item.text = text
        item.icon = icon
    settings.status_display_index = min(
        settings.status_display_index,
        max(0, len(desired) - 1),
    )
    return desired


def _context_display_metrics(context) -> tuple[int, float]:
    region = getattr(context, "region", None)
    region_width = max(1, int(getattr(region, "width", 320)))
    preferences = getattr(context, "preferences", None)
    system = getattr(preferences, "system", None)
    ui_scale = max(0.5, float(getattr(system, "ui_scale", 1.0)))
    return region_width, ui_scale


def _tag_view3d_redraws() -> None:
    screen = getattr(bpy.context, "screen", None)
    if screen is None:
        return
    for area in screen.areas:
        if area.type == "VIEW_3D":
            area.tag_redraw()


def _apply_pending_status_refreshes():
    requests = tuple(_PENDING_REFRESHES.values())
    _PENDING_REFRESHES.clear()
    for scene, region_width, ui_scale in requests:
        try:
            settings = scene.colmap_import
            sync_status_display_lines(settings, region_width, ui_scale)
        except (AttributeError, ReferenceError, RuntimeError):
            continue
    _tag_view3d_redraws()
    return None


def request_status_display_refresh(context) -> None:
    """Panel draw에서는 요청만 기록하고 실제 RNA 변경은 Timer에서 수행합니다."""
    scene = getattr(context, "scene", None)
    settings = getattr(scene, "colmap_import", None)
    if settings is None:
        return
    region_width, ui_scale = _context_display_metrics(context)
    desired = build_status_display_lines(settings, region_width, ui_scale)
    current = tuple(
        (item.text, item.icon)
        for item in settings.status_display_lines
    )
    if current == desired:
        _PENDING_REFRESHES.pop(scene.as_pointer(), None)
        return

    _PENDING_REFRESHES[scene.as_pointer()] = (
        scene,
        region_width,
        ui_scale,
    )
    if not bpy.app.timers.is_registered(_apply_pending_status_refreshes):
        bpy.app.timers.register(
            _apply_pending_status_refreshes,
            first_interval=0.0,
        )


def initialize_status_display(context) -> None:
    """등록 직후 첫 Panel draw 전에 현재 Scene의 5행 표시 데이터를 준비합니다."""
    scene = getattr(context, "scene", None)
    settings = getattr(scene, "colmap_import", None)
    if settings is None:
        return
    region_width, ui_scale = _context_display_metrics(context)
    sync_status_display_lines(settings, region_width, ui_scale)


def clear_status_display_runtime() -> None:
    """Extension 해제 시 예약된 Status 갱신만 안전하게 제거합니다."""
    _PENDING_REFRESHES.clear()
    if bpy.app.timers.is_registered(_apply_pending_status_refreshes):
        bpy.app.timers.unregister(_apply_pending_status_refreshes)


class COLMAP_UL_status_lines(bpy.types.UIList):
    """읽기 전용 Status 행과 Blender 기본 스크롤을 제공합니다."""

    bl_idname = "COLMAP_UL_status_lines"

    def draw_item(
        self,
        _context,
        layout,
        _data,
        item,
        _icon,
        _active_data,
        _active_propname,
        _index=0,
        _flt_flag=0,
    ):
        if self.layout_type in {"DEFAULT", "COMPACT"}:
            layout.label(
                text=item.text,
                icon=item.icon,
                translate=False,
            )
        else:
            layout.label(text="", icon=item.icon)


CLASSES = (COLMAP_UL_status_lines,)


__all__ = [
    "CLASSES",
    "COLMAP_UL_status_lines",
    "STATUS_FILLER_TEXT",
    "STATUS_VISIBLE_ROWS",
    "build_status_display_lines",
    "clear_status_display_runtime",
    "display_cell_width",
    "initialize_status_display",
    "request_status_display_refresh",
    "status_cell_limit",
    "sync_status_display_lines",
    "wrap_status_text",
]
