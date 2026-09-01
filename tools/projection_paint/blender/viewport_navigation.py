"""Projection Paint 작업공간에 Industry Compatible 뷰포트 탐색을 연결합니다."""

from __future__ import annotations

from typing import Any

import bpy


def projection_paint_viewport_active(context: Any) -> bool:
    """현재 입력이 Projection Paint 작업공간의 3D Viewport에서 발생했는지 확인합니다."""

    scene = getattr(context, "scene", None)
    settings = getattr(scene, "projection_paint", None)
    window = getattr(context, "window", None)
    area = getattr(context, "area", None)
    region = getattr(context, "region", None)
    return bool(
        settings is not None
        and window is not None
        and getattr(window, "workspace", None)
        is getattr(settings, "paint_workspace", None)
        and area is not None
        and str(getattr(area, "type", "")) == "VIEW_3D"
        and region is not None
        and str(getattr(region, "type", "")) == "WINDOW"
    )


def _invoke_native_navigation(operator_name: str) -> set[str]:
    """현재 마우스 이벤트를 Blender 기본 View3D 탐색 연산자에 그대로 전달합니다."""

    operator = getattr(bpy.ops.view3d, str(operator_name))
    return set(operator("INVOKE_DEFAULT"))


class _ProjectionPaintIndustryNavigation:
    """Projection Paint 범위와 Blender 기본 탐색 호출을 공유합니다."""

    native_operator = ""

    @classmethod
    def poll(cls, context: Any) -> bool:
        """Projection Paint의 실제 3D Viewport에서만 단축키를 활성화합니다."""

        return projection_paint_viewport_active(context)

    def invoke(self, _context: Any, _event: Any) -> set[str]:
        """선택한 Industry Compatible 입력을 대응하는 Blender 기본 탐색으로 시작합니다."""

        try:
            return _invoke_native_navigation(self.native_operator)
        except (AttributeError, RuntimeError) as exc:
            self.report({"ERROR"}, f"Blender 뷰포트 탐색을 시작하지 못했습니다: {exc}")
            return {"CANCELLED"}


class PP_OT_industry_orbit(_ProjectionPaintIndustryNavigation, bpy.types.Operator):
    """Alt+좌클릭을 Blender 기본 Orbit View에 전달합니다."""

    bl_idname = "projection_paint.industry_orbit"
    bl_label = "Projection Paint Industry Orbit"
    bl_options = {"INTERNAL"}
    native_operator = "rotate"


class PP_OT_industry_pan(_ProjectionPaintIndustryNavigation, bpy.types.Operator):
    """Alt+가운데 버튼을 Blender 기본 Pan View에 전달합니다."""

    bl_idname = "projection_paint.industry_pan"
    bl_label = "Projection Paint Industry Pan"
    bl_options = {"INTERNAL"}
    native_operator = "move"


class PP_OT_industry_zoom(_ProjectionPaintIndustryNavigation, bpy.types.Operator):
    """Alt+우클릭을 Blender 기본 Zoom View에 전달합니다."""

    bl_idname = "projection_paint.industry_zoom"
    bl_label = "Projection Paint Industry Zoom"
    bl_options = {"INTERNAL"}
    native_operator = "zoom"


CLASSES = (
    PP_OT_industry_orbit,
    PP_OT_industry_pan,
    PP_OT_industry_zoom,
)


__all__ = [
    "CLASSES",
    "PP_OT_industry_orbit",
    "PP_OT_industry_pan",
    "PP_OT_industry_zoom",
    "projection_paint_viewport_active",
]
