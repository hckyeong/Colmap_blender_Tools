"""통합 Extension의 정적 도구 식별 정보를 제공합니다."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ToolDescriptor:
    """독립 도구의 패키지명과 공개 실행 Operator를 설명합니다."""

    identifier: str
    package_name: str
    entry_operator: str


TOOLS = (
    ToolDescriptor("align", "tools.align", "colmap_align.open_workspace"),
    ToolDescriptor("modifier", "tools.modifier", "colmap.open_modifier_workspace"),
    ToolDescriptor("projection_paint", "tools.projection_paint", "projection_paint.open_workspace"),
)

__all__ = ["TOOLS", "ToolDescriptor"]
