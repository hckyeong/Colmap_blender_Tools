"""긴 확인 메시지를 제한된 폭의 Blender 대화상자에 표시합니다."""

from __future__ import annotations

import re
import unicodedata
from typing import Any


CONFIRM_DIALOG_WIDTH = 520
CONFIRM_MESSAGE_MAX_COLUMNS = 68


def _character_columns(character: str) -> int:
    """한글·전각 문자는 두 칸, 나머지 문자는 한 칸으로 계산합니다."""
    return 2 if unicodedata.east_asian_width(character) in {"W", "F"} else 1


def ui_text_columns(text: str) -> int:
    """Blender UI에서 비교할 문자열의 대략적인 표시 폭을 반환합니다."""
    return sum(_character_columns(character) for character in str(text))


def _split_wide_token(token: str, max_columns: int) -> tuple[str, ...]:
    """경로처럼 공백 없는 긴 토큰을 표시 폭 안에서만 안전하게 나눕니다."""
    parts: list[str] = []
    current: list[str] = []
    current_columns = 0
    for character in str(token):
        character_columns = _character_columns(character)
        if current and current_columns + character_columns > max_columns:
            parts.append("".join(current))
            current = []
            current_columns = 0
        current.append(character)
        current_columns += character_columns
    if current:
        parts.append("".join(current))
    return tuple(parts)


def _wrap_sentence(sentence: str, max_columns: int) -> tuple[str, ...]:
    """한 문장을 단어 경계 우선으로 지정 표시 폭에 맞춥니다."""
    words = str(sentence).split()
    if not words:
        return ()
    lines: list[str] = []
    current = ""
    for word in words:
        pieces = (
            _split_wide_token(word, max_columns)
            if ui_text_columns(word) > max_columns
            else (word,)
        )
        for piece_index, piece in enumerate(pieces):
            separator = " " if current and piece_index == 0 else ""
            candidate = f"{current}{separator}{piece}"
            if current and ui_text_columns(candidate) > max_columns:
                lines.append(current)
                current = piece
            else:
                current = candidate
            if piece_index < len(pieces) - 1:
                lines.append(current)
                current = ""
    if current:
        lines.append(current)
    return tuple(lines)


def wrap_ui_message(
    message: str,
    *,
    max_columns: int = CONFIRM_MESSAGE_MAX_COLUMNS,
) -> tuple[str, ...]:
    """명시 줄과 문장 경계를 보존하며 긴 확인 메시지를 여러 행으로 나눕니다."""
    if int(max_columns) < 1:
        raise ValueError("메시지 최대 표시 폭은 1 이상이어야 합니다.")
    lines: list[str] = []
    for paragraph in str(message).replace("\r", "").split("\n"):
        normalized = " ".join(paragraph.split())
        if not normalized:
            if lines and lines[-1] != "":
                lines.append("")
            continue
        sentences = re.split(r"(?<=[.!?])\s+", normalized)
        for sentence in sentences:
            lines.extend(_wrap_sentence(sentence, int(max_columns)))
    while lines and lines[-1] == "":
        lines.pop()
    return tuple(lines) if lines else ("",)


def justify_ui_line(
    line: str,
    *,
    max_columns: int = CONFIRM_MESSAGE_MAX_COLUMNS,
) -> str:
    """문장 마지막 행이 아닌 메시지 행의 단어 간격을 좌우 폭에 맞춥니다."""
    text = str(line)
    words = text.split()
    missing_columns = int(max_columns) - ui_text_columns(text)
    if len(words) < 2 or missing_columns <= 0:
        return text
    gap_count = len(words) - 1
    base_extra, remainder = divmod(missing_columns, gap_count)
    parts = [words[0]]
    for gap_index, word in enumerate(words[1:]):
        extra = base_extra + (1 if gap_index < remainder else 0)
        parts.append(f"{' ' * (1 + extra)}{word}")
    return "".join(parts)


def draw_wrapped_confirmation(operator: Any, _context: Any) -> None:
    """공통 확인창에 아이콘과 자동 줄바꿈된 메시지를 행별로 그립니다."""
    lines = tuple(getattr(operator, "_wrapped_confirm_lines", ("",)))
    icon = str(getattr(operator, "_wrapped_confirm_icon", "INFO") or "INFO")
    column = operator.layout.column(align=True)
    for index, line in enumerate(lines):
        text = str(line)
        if (
            index < len(lines) - 1
            and text
            and not re.search(r"[.!?]$", text)
        ):
            text = justify_ui_line(text)
        column.label(
            text=text,
            icon=icon if index == 0 else "BLANK1",
        )


def invoke_wrapped_confirmation(
    operator: Any,
    context: Any,
    *,
    title: str,
    message: str,
    confirm_text: str,
    icon: str,
) -> set[str]:
    """고정 폭과 공통 줄바꿈 규칙으로 확인 대화상자를 엽니다."""
    operator._wrapped_confirm_lines = wrap_ui_message(message)
    operator._wrapped_confirm_icon = str(icon or "INFO")
    return context.window_manager.invoke_props_dialog(
        operator,
        width=CONFIRM_DIALOG_WIDTH,
        title=str(title),
        confirm_text=str(confirm_text),
        cancel_default=False,
        translate=False,
    )


__all__ = (
    "CONFIRM_DIALOG_WIDTH",
    "CONFIRM_MESSAGE_MAX_COLUMNS",
    "draw_wrapped_confirmation",
    "invoke_wrapped_confirmation",
    "justify_ui_line",
    "ui_text_columns",
    "wrap_ui_message",
)
