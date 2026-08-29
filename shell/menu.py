"""세 독립 COLMAP 도구의 공통 Window 메뉴를 제공합니다."""

from __future__ import annotations

import bpy


class COLMAP_MT_tools(bpy.types.Menu):
    """세 독립 도구의 진입점을 한 메뉴에 표시합니다."""

    bl_idname = "COLMAP_MT_tools"
    bl_label = "COLMAP Tools"

    def draw(self, _context) -> None:
        """도구 사이 순서를 암시하지 않고 세 실행 항목을 표시합니다."""

        layout = self.layout
        layout.operator("colmap_align.open_workspace", text="COLMAP Align 열기", icon="ORIENTATION_GLOBAL")
        layout.operator("colmap.open_modifier_workspace", text="COLMAP Modifier 열기", icon="MODIFIER")
        layout.operator("projection_paint.open_workspace", text="Projection Painting 열기", icon="TPAINT_HLT")


def draw_window_menu(self, _context) -> None:
    """Blender Window 메뉴에 공통 COLMAP Tools 하위 메뉴를 표시합니다."""

    self.layout.separator()
    self.layout.menu(COLMAP_MT_tools.bl_idname, text=COLMAP_MT_tools.bl_label, icon="FILE_FOLDER")


CLASSES = (COLMAP_MT_tools,)

__all__ = ["CLASSES", "COLMAP_MT_tools", "draw_window_menu"]
