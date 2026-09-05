"""통합 Extension에서 공통으로 표시할 환경설정을 제공합니다."""

from __future__ import annotations

import bpy
from bpy.props import IntProperty, StringProperty


def root_package_name() -> str:
    """현재 설치 Repository를 포함한 Root Extension 패키지명을 반환합니다."""

    return __package__.rsplit(".shell", 1)[0]


class COLMAPTOOLS_AP_preferences(bpy.types.AddonPreferences):
    """도구 독립 설정과 Align CPU 상한·개발 실행 경로를 보관합니다."""

    bl_idname = root_package_name()

    cc_cpu_thread_limit: IntProperty(
        name="CPU 스레드",
        description="CC 정렬의 최대 CPU 스레드 수입니다. 0은 자동, 1은 단일 스레드입니다",
        default=0, min=0, max=1024,
    )

    open3d_python_path: StringProperty(
        name="외부 ICP Python",
        description="COLMAP Align Full 개발 프로필에서 사용할 외부 CPython 실행 파일입니다",
        subtype="FILE_PATH",
    )

    def draw(self, _context) -> None:
        """정식 배포 프로필에 맞는 Align 실행 환경과 CPU 설정을 표시합니다."""

        from ..tools.align.distribution_profile import (
            DISTRIBUTION_PROFILE, SMALL_GICP_PROFILE, includes_optional_backends,
        )
        from ..tools.align.properties import cc_cpu_thread_summary

        layout = self.layout
        layout.use_property_split = True
        layout.use_property_decorate = False
        align_box = layout.box()
        align_box.label(text="COLMAP Align", icon="ORIENTATION_GLOBAL")
        if includes_optional_backends():
            align_box.prop(self, "open3d_python_path")
        if DISTRIBUTION_PROFILE != SMALL_GICP_PROFILE:
            align_box.prop(self, "cc_cpu_thread_limit")
            align_box.label(text=cc_cpu_thread_summary(_context))
            align_box.operator("colmap_align.validate_cccore_environment", text="CC 정렬 환경 검사", icon="CHECKMARK")
        else:
            align_box.label(text="Small GICP 비교용 실행 환경이 포함되어 있습니다.")


CLASSES = (COLMAPTOOLS_AP_preferences,)

__all__ = ["CLASSES", "COLMAPTOOLS_AP_preferences", "root_package_name"]
