"""저장된 COLMAP 원본의 재연결·재시도·명시적 새 프로젝트 시작을 제공합니다."""

from __future__ import annotations

import bpy
from bpy.props import BoolProperty, StringProperty

from ..project_persistence.runtime import (
    ProjectPersistenceRuntimeError,
    cancel_project_restore,
    relink_project_source,
    retry_project_restore,
    start_new_project_after_restore_error,
)
from ..state import RUNTIME
from ..status import set_workspace_status_text


def _report(operator: bpy.types.Operator, level: set[str], message: str) -> None:
    """복원 결과를 Blender 보고와 상태 표시줄에 같은 문구로 전달합니다."""

    operator.report(level, message)
    workspace = getattr(bpy.context, "workspace", None)
    if workspace is not None:
        set_workspace_status_text(workspace, message)


class COLMAPALIGN_OT_relink_project_source(bpy.types.Operator):
    """사용자가 고른 동일 source 후보를 검증 뒤 전체 복원에 사용합니다."""

    bl_idname = "colmap_align.relink_project_source"
    bl_label = "원본 다시 연결"
    bl_description = "저장 당시와 같은 COLMAP 원본 폴더를 선택해 전체 프로젝트 복원을 다시 시도합니다"
    bl_options = {"INTERNAL"}

    model_id: StringProperty(options={"HIDDEN"})
    directory: StringProperty(subtype="DIR_PATH", options={"HIDDEN"})
    filter_folder: BoolProperty(default=True, options={"HIDDEN", "SKIP_SAVE"})

    @classmethod
    def poll(cls, _context):
        """원본 재연결이 필요한 저장 프로젝트에서만 파일 탐색기를 엽니다."""

        return RUNTIME.project_restore.phase == "NEEDS_RELINK"

    def invoke(self, context, _event):
        """UI 호출에서는 폴더 선택을 열고 background 호출에서는 즉시 검증합니다."""

        if not self.poll(context):
            return {"CANCELLED"}
        if bpy.app.background:
            return self.execute(context)
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}

    def execute(self, _context):
        """선택 후보를 저장하지 않은 채 지문 검증이 포함된 전체 복원을 재시작합니다."""

        try:
            relink_project_source(self.model_id, bpy.path.abspath(self.directory))
            started = retry_project_restore()
        except ProjectPersistenceRuntimeError as error:
            _report(self, {"ERROR"}, str(error))
            return {"CANCELLED"}
        if not started:
            _report(
                self,
                {"ERROR"},
                RUNTIME.project_restore.last_error or "선택한 원본으로 프로젝트 복원을 시작하지 못했습니다.",
            )
            return {"CANCELLED"}
        _report(self, {"INFO"}, "선택한 원본 후보를 검증하며 프로젝트 복원을 다시 시작했습니다.")
        return {"FINISHED"}


class COLMAPALIGN_OT_retry_project_restore(bpy.types.Operator):
    """현재 저장 snapshot을 바꾸지 않고 자동 복원 전체를 다시 시작합니다."""

    bl_idname = "colmap_align.retry_project_restore"
    bl_label = "재시도"
    bl_description = "저장된 COLMAP Align 프로젝트 복원을 다시 시도합니다"
    bl_options = {"INTERNAL"}

    @classmethod
    def poll(cls, _context):
        """복원 오류 또는 재연결 대기 상태에서만 재시도를 허용합니다."""

        return RUNTIME.project_restore.phase in {"NEEDS_RELINK", "FAILED"}

    def execute(self, _context):
        """기존 snapshot의 source 후보를 다시 preflight하고 timer 복원을 예약합니다."""

        try:
            started = retry_project_restore()
        except ProjectPersistenceRuntimeError as error:
            _report(self, {"ERROR"}, str(error))
            return {"CANCELLED"}
        if not started:
            _report(self, {"WARNING"}, RUNTIME.project_restore.last_error or "프로젝트 복원을 시작하지 못했습니다.")
            return {"CANCELLED"}
        _report(self, {"INFO"}, "저장된 프로젝트 복원을 다시 시작했습니다.")
        return {"FINISHED"}


class COLMAPALIGN_OT_cancel_project_restore(bpy.types.Operator):
    """실행 중인 프로젝트 자동 복원을 취소하고 기존 정상 Runtime을 유지합니다."""

    bl_idname = "colmap_align.cancel_project_restore"
    bl_label = "복원 취소"
    bl_description = "실행 중인 프로젝트 자동 복원을 취소합니다"
    bl_options = {"INTERNAL"}

    @classmethod
    def poll(cls, _context):
        """복원 또는 원본 재연결 대기 중일 때만 취소를 활성화합니다."""

        return RUNTIME.project_restore.phase in {"RESTORING", "NEEDS_RELINK"}

    def execute(self, _context):
        """저장 Text와 기존 작업을 유지한 채 복원용 가져오기와 임시 상태를 폐기합니다."""

        if not cancel_project_restore():
            return {"CANCELLED"}
        _report(self, {"INFO"}, "프로젝트 자동 복원을 취소했습니다. 저장된 상태는 유지합니다.")
        return {"FINISHED"}


class COLMAPALIGN_OT_start_new_project_after_restore_error(bpy.types.Operator):
    """사용자 확인 뒤 실패한 ACTIVE project Text를 RETIRED로 바꿉니다."""

    bl_idname = "colmap_align.start_new_project_after_restore_error"
    bl_label = "새 프로젝트 시작"
    bl_description = "저장된 복원 상태를 유지한 채 새 COLMAP Align 프로젝트를 시작합니다"
    bl_options = {"INTERNAL"}

    confirmed: BoolProperty(default=False, options={"HIDDEN", "SKIP_SAVE"})

    @classmethod
    def poll(cls, _context):
        """손상·누락·불일치로 복원이 실패한 프로젝트에서만 새 시작을 허용합니다."""

        return RUNTIME.project_restore.phase in {"NEEDS_RELINK", "FAILED"}

    def invoke(self, context, event):
        """원본과 기존 `.blend`를 삭제하지 않는다는 확인 대화상자를 표시합니다."""

        if not self.poll(context):
            return {"CANCELLED"}
        if self.confirmed or bpy.app.background:
            return self.execute(context)
        return context.window_manager.invoke_confirm(
            self,
            event,
            title="저장된 프로젝트 복원을 취소하고 새 프로젝트를 시작하시겠습니까?",
            message="원본 COLMAP과 기존 .blend 파일은 삭제되지 않습니다.",
            confirm_text="새 프로젝트 시작",
            icon="WARNING",
        )

    def execute(self, _context):
        """ACTIVE owner만 RETIRED로 전환하고 수동 import가 가능한 빈 Runtime을 만듭니다."""

        try:
            start_new_project_after_restore_error()
        except ProjectPersistenceRuntimeError as error:
            _report(self, {"ERROR"}, str(error))
            return {"CANCELLED"}
        _report(self, {"INFO"}, "저장된 프로젝트 상태를 은퇴하고 새 프로젝트를 시작할 수 있습니다.")
        return {"FINISHED"}


CLASSES = (
    COLMAPALIGN_OT_relink_project_source,
    COLMAPALIGN_OT_retry_project_restore,
    COLMAPALIGN_OT_cancel_project_restore,
    COLMAPALIGN_OT_start_new_project_after_restore_error,
)
