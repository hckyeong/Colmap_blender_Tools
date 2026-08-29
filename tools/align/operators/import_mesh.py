"""삼각형 OBJ 파일 선택과 TIMER 기반 MeshImportJob 실행을 제공합니다."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import bpy
from bpy.props import EnumProperty, StringProperty

from ..constants import OBJ_DISPLAY_VERTEX_LIMIT
from ..display_axes import (
    AXIS_ITEMS,
    AXIS_PRESET_ITEMS,
    DEFAULT_AXIS_PRESET,
    DEFAULT_FORWARD_AXIS,
    DEFAULT_UP_AXIS,
    axes_for_preset,
    create_model_display_transform,
    validate_axis_pair,
)
from ..mesh_importer import (
    MESH_IMPORT_STAGE_COMPLETED,
    MESH_IMPORT_STAGE_FAILED,
    MeshImportJobError,
    create_mesh_import_job,
)
from ..project_persistence.runtime import notify_project_state_changed, restore_input_block_reason
from ..session import is_session_active_for_window
from ..state import RUNTIME
from ..status import set_workspace_status_text
from ..viewport.renderer import request_redraw
from .import_model import fit_after_successful_import


_TIMER_INTERVAL_SECONDS = 0.01


def _report_status(operator: Any, level: set[str], message: str) -> None:
    """OBJ 오퍼레이터 결과를 Blender 보고와 상태 표시줄에 함께 전달합니다."""
    operator.report(level, message)
    workspace = getattr(bpy.context, "workspace", None)
    if workspace is not None:
        set_workspace_status_text(workspace, message)


def _axis_pair(operator: Any) -> tuple[str, str]:
    """고정 Display Axis 또는 Custom 축 입력을 검증해 실제 축 쌍을 반환합니다."""
    fixed = axes_for_preset(operator.axis_preset)
    return fixed if fixed is not None else validate_axis_pair(operator.axis_forward, operator.axis_up)


class COLMAPALIGN_OT_import_obj(bpy.types.Operator):
    """파일 탐색기에서 삼각형 OBJ와 표시 축을 선택합니다."""

    bl_idname = "colmap_align.import_obj"
    bl_label = "OBJ 메시 추가"
    bl_description = "삼각형 OBJ(최대 600,000면)를 Data Layer로 추가합니다"
    bl_options = {"REGISTER"}

    filepath: StringProperty(name="OBJ 파일", subtype="FILE_PATH", options={"SKIP_SAVE"})
    filter_glob: StringProperty(default="*.obj", options={"HIDDEN"})
    axis_preset: EnumProperty(name="Display Axis", items=AXIS_PRESET_ITEMS, default=DEFAULT_AXIS_PRESET)
    axis_forward: EnumProperty(name="Forward", items=AXIS_ITEMS, default=DEFAULT_FORWARD_AXIS)
    axis_up: EnumProperty(name="Up", items=AXIS_ITEMS, default=DEFAULT_UP_AXIS)

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        """활성 COLMAP Align 세션에서만 OBJ 선택을 허용합니다."""
        window = getattr(context, "window", None)
        return bool(
            window is not None
            and is_session_active_for_window(window)
            and not RUNTIME.import_in_progress
            and not RUNTIME.modal_active
            and not bool(getattr(RUNTIME.merge_export, "is_running", False))
            and not bool(getattr(RUNTIME.obj_export, "is_running", False))
            and restore_input_block_reason() is None
        )

    def invoke(self, context: bpy.types.Context, _event: bpy.types.Event):
        """OBJ 전용 파일 탐색기를 열고 사용자 확인 전에는 Runtime을 변경하지 않습니다."""
        settings = getattr(getattr(context, "scene", None), "colmap_align_settings", None)
        if settings is not None:
            self.filepath = str(getattr(settings, "obj_last_file", "")).strip()
            self.axis_preset = str(getattr(settings, "obj_axis_preset", self.axis_preset))
            self.axis_forward = str(getattr(settings, "obj_axis_forward", self.axis_forward))
            self.axis_up = str(getattr(settings, "obj_axis_up", self.axis_up))
        if bpy.app.background:
            return self.execute(context)
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}

    def draw(self, _context: bpy.types.Context) -> None:
        """파일 탐색기 옵션에 Display Axis와 삼각면 하드 제한 안내를 그립니다."""
        self.layout.prop(self, "axis_preset")
        if axes_for_preset(self.axis_preset) is None:
            self.layout.prop(self, "axis_forward")
            self.layout.prop(self, "axis_up")
        self.layout.label(text="삼각면 최대 600,000개, 자동 리메시 없음", icon="INFO")
        self.layout.label(text=f"마커 정점 표시 상한 {OBJ_DISPLAY_VERTEX_LIMIT:,}개", icon="INFO")

    def execute(self, context: bpy.types.Context):
        """선택한 OBJ와 축 snapshot을 내부 TIMER 작업으로 전달합니다."""
        if restore_input_block_reason() is not None:
            _report_status(self, {"WARNING"}, restore_input_block_reason() or "복원 중입니다.")
            return {"CANCELLED"}
        path = bpy.path.abspath(str(self.filepath).strip())
        if not path:
            _report_status(self, {"ERROR"}, "OBJ 파일을 선택하십시오.")
            return {"CANCELLED"}
        try:
            axis_forward, axis_up = _axis_pair(self)
            transform = create_model_display_transform(self.axis_preset, axis_forward, axis_up)
        except ValueError as error:
            _report_status(self, {"ERROR"}, str(error))
            return {"CANCELLED"}
        settings = getattr(getattr(context, "scene", None), "colmap_align_settings", None)
        if settings is not None:
            settings.obj_last_file = path
            settings.obj_axis_preset = transform.preset_id
            settings.obj_axis_forward = transform.forward_axis
            settings.obj_axis_up = transform.up_axis
            settings.import_last_directory = str(Path(path).parent)
        result = bpy.ops.colmap_align.import_obj_job(
            "INVOKE_DEFAULT",
            source_path=path,
            axis_preset=transform.preset_id,
            axis_forward=transform.forward_axis,
            axis_up=transform.up_axis,
        )
        return result


class COLMAPALIGN_OT_import_obj_job(bpy.types.Operator):
    """OBJ 읽기·검증·GPU 준비·Data Layer 원자 등록을 TIMER 단위로 실행합니다."""

    bl_idname = "colmap_align.import_obj_job"
    bl_label = "OBJ 메시 가져오기 작업"
    bl_options = {"INTERNAL"}

    source_path: StringProperty(name="OBJ source", options={"HIDDEN"})
    axis_preset: EnumProperty(name="Display Axis", items=AXIS_PRESET_ITEMS, default=DEFAULT_AXIS_PRESET, options={"HIDDEN"})
    axis_forward: EnumProperty(name="Forward", items=AXIS_ITEMS, default=DEFAULT_FORWARD_AXIS, options={"HIDDEN"})
    axis_up: EnumProperty(name="Up", items=AXIS_ITEMS, default=DEFAULT_UP_AXIS, options={"HIDDEN"})

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        """내부 OBJ 작업도 활성 세션과 비어 있는 import 슬롯에서만 시작합니다."""
        window = getattr(context, "window", None)
        return bool(
            window is not None
            and is_session_active_for_window(window)
            and not RUNTIME.import_in_progress
            and not bool(getattr(RUNTIME.merge_export, "is_running", False))
            and not bool(getattr(RUNTIME.obj_export, "is_running", False))
        )

    def _finish(self, context: bpy.types.Context, result: set[str], message: str, level: set[str]) -> set[str]:
        """OBJ TIMER를 정리하고 성공 시 저장 dirty와 해당 A/P Frame All을 갱신합니다."""
        job = RUNTIME.import_job
        timer = RUNTIME.import_timer
        if timer is not None:
            try:
                context.window_manager.event_timer_remove(timer)
            except (ReferenceError, RuntimeError):
                pass
            RUNTIME.import_timer = None
        try:
            context.window_manager.progress_end()
        except (ReferenceError, RuntimeError):
            pass
        if result == {"FINISHED"}:
            notify_project_state_changed("OBJ 메시 가져오기 완료")
            imported_model_id = (
                str(job.staged_model.model_id)
                if job is not None and job.staged_model is not None
                else ""
            )
            fit_after_successful_import(context, imported_model_id)
        _report_status(self, level, message)
        request_redraw()
        RUNTIME.clear_import_job()
        return result

    def _start(self, context: bpy.types.Context) -> set[str]:
        """MeshImportJob을 생성하고 Blender TIMER·진행 막대를 등록합니다."""
        try:
            job = create_mesh_import_job(
                self.source_path,
                axis_preset=self.axis_preset,
                axis_forward=self.axis_forward,
                axis_up=self.axis_up,
            )
        except (MeshImportJobError, ValueError) as error:
            _report_status(self, {"ERROR"}, str(error))
            return {"CANCELLED"}
        RUNTIME.import_job = job
        context.window_manager.progress_begin(0, 100)
        RUNTIME.import_timer = context.window_manager.event_timer_add(_TIMER_INTERVAL_SECONDS, window=context.window)
        context.window_manager.modal_handler_add(self)
        return {"RUNNING_MODAL"}

    def invoke(self, context: bpy.types.Context, _event: bpy.types.Event):
        """파일 탐색기에서 넘어온 OBJ 경로로 TIMER 작업을 시작합니다."""
        return self._start(context)

    def execute(self, context: bpy.types.Context):
        """스크립트 호출도 동일한 OBJ TIMER 작업 경계를 사용합니다."""
        return self._start(context)

    def modal(self, context: bpy.types.Context, event: bpy.types.Event):
        """TIMER마다 OBJ 단계 하나를 실행하고 ESC는 취소 요청으로 처리합니다."""
        job = RUNTIME.import_job
        if job is None:
            return {"CANCELLED"}
        if event.type == "ESC":
            job.request_cancel()
            return {"RUNNING_MODAL"}
        if event.type != "TIMER":
            return {"PASS_THROUGH"}
        result = job.step(RUNTIME)
        context.window_manager.progress_update(int(round(job.progress * 100.0)))
        if result == "RUNNING":
            request_redraw()
            return {"RUNNING_MODAL"}
        if result == "FINISHED" and job.stage == MESH_IMPORT_STAGE_COMPLETED:
            return self._finish(context, {"FINISHED"}, job.result_message, {"INFO"})
        return self._finish(context, {"CANCELLED"}, job.error_message or job.result_message, {"WARNING"})


CLASSES = (COLMAPALIGN_OT_import_obj, COLMAPALIGN_OT_import_obj_job)
