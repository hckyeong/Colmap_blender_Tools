"""COLMAP Modifier 전용 Workspace 진입과 종료 Operator를 제공합니다."""

from __future__ import annotations

import bpy

try:
    from ....shared.blender_runtime.workspace_handoff import (
        ACTIVATION_DEFERRED,
        WorkspaceHandoffError,
        request_workspace_activation,
    )
except ImportError:
    from shared.blender_runtime.workspace_handoff import (
        ACTIVATION_DEFERRED,
        WorkspaceHandoffError,
        request_workspace_activation,
    )

from .modifier_session import is_session_active_for_window
from .modifier_workspace import (
    ModifierWorkspaceError,
    configure_modifier_workspace,
    exit_modifier_workspace,
)


class COLMAP_OT_open_modifier_workspace(bpy.types.Operator):
    """현재 Blender Window를 COLMAP Modifier Workspace로 전환합니다."""

    bl_idname = "colmap.open_modifier_workspace"
    bl_label = "COLMAP Modifier 열기"
    bl_description = "현재 창을 COLMAP Modifier 전용 Workspace로 전환합니다"
    bl_options = {"REGISTER"}

    @classmethod
    def poll(cls, context):
        """유효한 Blender Window가 있을 때만 진입을 허용합니다."""

        return bool(
            getattr(context, "window_manager", None) is not None
            and getattr(context, "window", None) is not None
        )

    def execute(self, context):
        """다른 COLMAP 도구 종료 뒤 Modifier Workspace 구성을 시작합니다."""

        try:
            result = request_workspace_activation(context, "modifier")
        except (ModifierWorkspaceError, WorkspaceHandoffError) as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        self.report(
            {"INFO"},
            (
                "현재 COLMAP Workspace를 종료한 뒤 Modifier를 준비합니다."
                if result == ACTIVATION_DEFERRED
                else "COLMAP Modifier Workspace를 준비합니다."
            ),
        )
        return {"FINISHED"}


class COLMAP_OT_restore_modifier_workspace(bpy.types.Operator):
    """애드온 소유 Screen만 목표 단일 View Layout으로 복원합니다."""

    bl_idname = "colmap.restore_modifier_workspace"
    bl_label = "COLMAP Modifier Workspace 복원"
    bl_description = "전용 Workspace의 손상된 Layout을 다시 구성합니다"
    bl_options = {"INTERNAL"}

    @classmethod
    def poll(cls, context):
        """활성 Modifier Session의 대상 Window에서만 복원을 허용합니다."""

        return is_session_active_for_window(getattr(context, "window", None))

    def execute(self, context):
        """기존 사용자 Workspace를 건드리지 않고 소유 Screen 복원을 요청합니다."""

        try:
            configure_modifier_workspace(context, force_restore=True)
        except ModifierWorkspaceError as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        self.report({"INFO"}, "COLMAP Modifier Workspace를 복원합니다.")
        return {"FINISHED"}


class COLMAP_OT_exit_modifier_workspace(bpy.types.Operator):
    """Viewport Runtime을 정리하고 진입 전 Workspace로 돌아갑니다."""

    bl_idname = "colmap.exit_modifier_workspace"
    bl_label = "COLMAP Modifier 종료"
    bl_description = "COLMAP Modifier Session을 종료하고 이전 Workspace로 돌아갑니다"
    bl_options = {"REGISTER"}

    @classmethod
    def poll(cls, context):
        """활성 Modifier Session의 대상 Window에서만 종료를 허용합니다."""

        return is_session_active_for_window(getattr(context, "window", None))

    def execute(self, context):
        """진행 작업을 보호하면서 원래 Workspace 복귀를 실행합니다."""

        try:
            warnings = exit_modifier_workspace(context)
        except ModifierWorkspaceError as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        if warnings:
            self.report({"WARNING"}, warnings[0])
        else:
            self.report({"INFO"}, "이전 Workspace로 돌아왔습니다.")
        return {"FINISHED"}


CLASSES = (
    COLMAP_OT_open_modifier_workspace,
    COLMAP_OT_restore_modifier_workspace,
    COLMAP_OT_exit_modifier_workspace,
)


__all__ = [
    "CLASSES",
    "COLMAP_OT_exit_modifier_workspace",
    "COLMAP_OT_open_modifier_workspace",
    "COLMAP_OT_restore_modifier_workspace",
]
