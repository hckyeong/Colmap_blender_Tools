"""관리 Camera 선택 모드를 노출하는 Blender 오퍼레이터입니다."""

from __future__ import annotations

import bpy

from .camera_edit_session import (
    EditCameraSession,
    _SESSIONS,
    clear_camera_edit_sessions,
    current_camera_edit_session,
    finish_camera_edit_mode,
    force_end_camera_mode,
    start_camera_edit_mode,
)
from .operation_guard import (
    export_mutation_allowed,
    require_export_mutation_allowed,
)
from .runtime_state import ensure_runtime


class COLMAP_OT_toggle_edit_camera_mode(bpy.types.Operator):
    bl_idname = "colmap.toggle_edit_camera_mode"
    bl_label = "Edit Camera Mode"
    bl_description = (
        "COLMAP Camera 선택 잠금을 임시 해제하고 Camera 선택 전용 모드를 "
        "시작하거나 종료합니다"
    )
    bl_options = {"REGISTER"}

    @classmethod
    def poll(cls, context):
        settings = getattr(context.scene, "colmap_import", None)
        return bool(
            settings
            and settings.dataset_uuid
            and export_mutation_allowed(context)
        )

    def execute(self, context):
        settings = context.scene.colmap_import
        state = None
        try:
            from .camera_filter_queue import require_no_pending_filters

            require_export_mutation_allowed(context, "Edit Camera Mode 전환")
            require_no_pending_filters(context, "Edit Camera Mode 전환")
            state = ensure_runtime(context.scene)
            if settings.camera_edit_mode_active:
                selected = finish_camera_edit_mode(context, state)
                settings.point_selection_status = (
                    f"Camera 선택 {len(selected)}개를 해제하고 잠금 상태로 돌아왔습니다."
                )
            else:
                start_camera_edit_mode(context, state)
            return {"FINISHED"}
        except Exception as exc:
            if state is not None:
                try:
                    force_end_camera_mode(context, state.dataset_uuid)
                except (RuntimeError, ValueError):
                    pass
            settings.point_selection_status = f"Edit Camera Mode 전환 실패: {exc}"
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}


CLASSES = (COLMAP_OT_toggle_edit_camera_mode,)
