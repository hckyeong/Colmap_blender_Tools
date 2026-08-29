"""통합 Extension에서 공통으로 표시할 환경설정을 제공합니다."""

from __future__ import annotations

import bpy
from bpy.props import StringProperty


def root_package_name() -> str:
    """현재 설치 Repository를 포함한 Root Extension 패키지명을 반환합니다."""

    return __package__.rsplit(".shell", 1)[0]


class COLMAPTOOLS_AP_preferences(bpy.types.AddonPreferences):
    """도구 독립 설정과 Align 선택형 외부 ICP 경로를 보관합니다."""

    bl_idname = root_package_name()

    open3d_python_path: StringProperty(
        name="외부 ICP Python",
        description="COLMAP Align Full 개발 프로필에서 사용할 외부 CPython 실행 파일입니다",
        subtype="FILE_PATH",
    )

    def draw(self, _context) -> None:
        """통합 Extension의 공통 설정을 도구별 섹션으로 표시합니다."""

        layout = self.layout
        layout.use_property_split = True
        layout.use_property_decorate = False
        align_box = layout.box()
        align_box.label(text="COLMAP Align", icon="ORIENTATION_GLOBAL")
        align_box.prop(self, "open3d_python_path")


CLASSES = (COLMAPTOOLS_AP_preferences,)

__all__ = ["CLASSES", "COLMAPTOOLS_AP_preferences", "root_package_name"]
