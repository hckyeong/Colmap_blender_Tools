"""관리 Point 선택 모드를 노출하는 Blender 오퍼레이터입니다."""

from __future__ import annotations

import bpy

from .edit_coordinator import (
    _restore_pending_point_meshes,
    clear_edit_sessions,
    normalize_external_mode_change,
    register_edit_mode_handler,
    restore_authoritative_point_scene,
    unregister_edit_mode_handler,
)
from .point_edit_session import (
    EditPointSession,
    _PENDING_RESTORES,
    _PROGRAMMATIC_ENDS,
    _SESSIONS,
    _clear_point_edit_selection,
    _point_display_modifier,
    _restore_point_display_visibility,
    _scene_key,
    _session_matches_state,
    _set_point_display_edit_visibility,
    current_edit_session,
    finish_point_edit_mode,
    force_end_point_mode,
    start_point_edit_mode,
)
from .operation_guard import (
    export_mutation_allowed,
    require_export_mutation_allowed,
)
from .runtime_state import ensure_runtime
from .selection import PointMeshIntegrityError


class COLMAP_OT_toggle_edit_point_mode(bpy.types.Operator):
    bl_idname = "colmap.toggle_edit_point_mode"
    bl_label = "Edit Point Mode"
    bl_description = (
        "COLMAP Point Object의 선택 잠금을 임시 해제하고 Point3D 선택 전용 "
        "Edit Mode를 시작하거나 종료합니다"
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

            require_export_mutation_allowed(context, "Edit Point Mode 전환")
            require_no_pending_filters(context, "Edit Point Mode 전환")
            state = ensure_runtime(context.scene)
            if settings.point_edit_mode_active:
                selected = finish_point_edit_mode(context, state)
                settings.point_selection_status = (
                    f"Point 선택 {len(selected)}개를 보존하고 잠금 상태로 돌아왔습니다."
                )
            else:
                start_point_edit_mode(context, state)
            return {"FINISHED"}
        except PointMeshIntegrityError as exc:
            if state is not None:
                try:
                    restore_authoritative_point_scene(
                        context,
                        state,
                        f"표시 Mesh 변경을 거부하고 권위 모델로 복원했습니다: {exc}",
                    )
                except Exception as restore_exc:
                    settings.point_selection_status = (
                        f"표시 Mesh 복원 실패: {restore_exc}"
                    )
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        except Exception as exc:
            if state is not None:
                try:
                    force_end_point_mode(context, state.dataset_uuid)
                except (RuntimeError, ValueError):
                    pass
            settings.point_selection_status = f"Edit Point Mode 전환 실패: {exc}"
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}


CLASSES = (COLMAP_OT_toggle_edit_point_mode,)
