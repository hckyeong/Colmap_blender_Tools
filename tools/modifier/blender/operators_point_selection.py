"""Point 관측 Camera 선택과 통합 Remove·이력 복구 오퍼레이터입니다."""

from __future__ import annotations

import bpy

from .camera_edit_session import force_end_camera_mode
from .edit_coordinator import (
    restore_authoritative_point_scene,
    synchronize_scene,
)
from .edit_workflows import (
    PendingRemove,
    apply_pending_remove,
    commit_edit as _commit_edit,
    modifier_redo_available,
    modifier_undo_available,
    prepare_remove,
    reset_edits,
    redo_last_edit,
    resolve_remove_target as _resolve_remove_target,
    select_observing_cameras,
    undo_last_edit,
    update_counts as _update_counts,
    validate_prepared_remove,
)
from .operation_guard import (
    export_mutation_allowed,
    require_export_mutation_allowed,
)
from .point_edit_session import force_end_point_mode
from .runtime_state import ensure_runtime
from .selection import PointMeshIntegrityError, clear_snapshots


class COLMAP_OT_select_observing_cameras(bpy.types.Operator):
    bl_idname = "colmap.select_observing_cameras"
    bl_label = "Select Observing Cameras"
    bl_description = (
        "선택 Point3D의 track을 역추적하고 해당 Point3D를 관측한 모든 "
        "Image의 Camera Object를 선택합니다"
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
            require_export_mutation_allowed(context, "관측 Camera 선택")
            state = ensure_runtime(context.scene)
            select_observing_cameras(context, state)
            self.report({"INFO"}, settings.point_selection_status)
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
        except ValueError as exc:
            settings.point_selection_status = f"관측 Camera 선택 실패: {exc}"
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        except Exception as exc:
            if state is not None:
                try:
                    force_end_point_mode(context, state.dataset_uuid)
                    force_end_camera_mode(context, state.dataset_uuid)
                except (RuntimeError, ValueError):
                    pass
            settings.point_selection_status = f"관측 Camera 선택 실패: {exc}"
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}


class COLMAP_OT_remove(bpy.types.Operator):
    bl_idname = "colmap.remove"
    bl_label = "Remove"
    bl_description = (
        "현재 Point Edit 선택 또는 선택된 COLMAP Camera를 기준으로 "
        "검증된 제거 작업을 실행합니다"
    )
    bl_options = {"REGISTER", "UNDO"}

    _pending: PendingRemove | None = None

    @classmethod
    def poll(cls, context):
        settings = getattr(context.scene, "colmap_import", None)
        return bool(
            settings
            and settings.dataset_uuid
            and export_mutation_allowed(context)
        )

    def _prepare(self, context):
        require_export_mutation_allowed(context, "Remove")
        state, self._pending = prepare_remove(context)
        return state, self._pending

    def _prepared_for_execute(self, context):
        require_export_mutation_allowed(context, "Remove")
        return validate_prepared_remove(context, self._pending)

    def invoke(self, context, event):
        try:
            _, pending = self._prepare(context)
            plan = pending.plan
            settings = context.scene.colmap_import
            if pending.target_kind == "POINTS":
                settings.point_selection_status = (
                    f"삭제 예정: Point3D {len(plan.requested_point3d_ids)}개, "
                    f"정리할 관측 참조 {plan.cleared_observation_count}개"
                )
            else:
                settings.point_selection_status = (
                    f"삭제 예정: Camera {len(plan.requested_image_ids)}개, "
                    f"전역 Point3D {len(plan.observed_point3d_ids)}개"
                )
            return context.window_manager.invoke_confirm(self, event)
        except PointMeshIntegrityError as exc:
            try:
                state = ensure_runtime(context.scene)
                restore_authoritative_point_scene(
                    context,
                    state,
                    f"표시 Mesh 변경을 거부하고 권위 모델로 복원했습니다: {exc}",
                )
            except Exception as restore_exc:
                context.scene.colmap_import.point_selection_status = (
                    f"표시 Mesh 복원 실패: {restore_exc}"
                )
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        except ValueError as exc:
            context.scene.colmap_import.point_selection_status = (
                f"삭제 사전검사 실패: {exc}"
            )
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        except Exception as exc:
            try:
                force_end_point_mode(
                    context,
                    context.scene.colmap_import.dataset_uuid or None,
                )
                force_end_camera_mode(
                    context,
                    context.scene.colmap_import.dataset_uuid or None,
                )
            except (RuntimeError, ValueError):
                pass
            context.scene.colmap_import.point_selection_status = f"삭제 사전검사 실패: {exc}"
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}

    def draw(self, context):
        del context
        pending = self._pending
        if pending is None:
            return
        plan = pending.plan
        if pending.target_kind == "POINTS":
            self.layout.label(
                text=f"선택 Point3D {len(plan.requested_point3d_ids)}개를 제거합니다."
            )
            self.layout.label(
                text=f"Image 관측 참조 {plan.cleared_observation_count}개를 정리합니다."
            )
            self.layout.label(text="Camera와 Image는 제거하지 않습니다.", icon="INFO")
        else:
            self.layout.label(
                text=(
                    f"선택 Camera {len(plan.requested_image_ids)}개와 관측 Point3D "
                    f"{len(plan.observed_point3d_ids)}개를 제거합니다."
                )
            )
            self.layout.label(
                text="생존 Camera가 관측한 같은 Point3D도 전역 제거됩니다.",
                icon="ERROR",
            )
        self.layout.label(text="원본 sparse 파일은 변경되지 않습니다.", icon="INFO")

    def execute(self, context):
        settings = context.scene.colmap_import
        state = None
        try:
            state, pending = self._prepared_for_execute(context)
            warnings = apply_pending_remove(context, state, pending)
            if warnings:
                settings.warning_summary = "\n".join(warnings)
            self.report({"INFO"}, settings.point_selection_status)
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
                    settings.point_selection_status = f"표시 Mesh 복원 실패: {restore_exc}"
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        except ValueError as exc:
            settings.point_selection_status = f"삭제 실패: {exc}"
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        except Exception as exc:
            if state is not None:
                try:
                    force_end_point_mode(context, state.dataset_uuid)
                    force_end_camera_mode(context, state.dataset_uuid)
                except (RuntimeError, ValueError):
                    pass
            clear_snapshots()
            _update_counts(settings, 0, 0, 0)
            settings.remove_target = "NONE"
            settings.point_selection_status = f"삭제 실패: {exc}"
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        finally:
            self._pending = None


class COLMAP_OT_undo_last_edit(bpy.types.Operator):
    bl_idname = "colmap.undo_last_edit"
    bl_label = "Undo Last Edit"
    bl_description = "Modifier 전용 Journal의 가장 최근 편집 작업 하나를 되돌립니다"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        """현재 독립 Journal에 Undo Command가 있을 때만 활성화합니다."""

        settings = getattr(context.scene, "colmap_import", None)
        return bool(
            settings
            and settings.dataset_uuid
            and modifier_undo_available(context.scene)
            and export_mutation_allowed(context)
        )

    def execute(self, context):
        """Blender Global Undo 대신 Modifier 전용 Undo를 실행합니다."""

        settings = context.scene.colmap_import
        state = None
        try:
            require_export_mutation_allowed(context, "Undo Last Edit")
            state = undo_last_edit(context)
            self.report({"INFO"}, settings.point_selection_status)
            return {"FINISHED"}
        except Exception as exc:
            if state is not None:
                force_end_point_mode(context, state.dataset_uuid)
                force_end_camera_mode(context, state.dataset_uuid)
            settings.point_selection_status = f"마지막 편집 복구 실패: {exc}"
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}


class COLMAP_OT_redo_last_edit(bpy.types.Operator):
    """Modifier 전용 Journal의 마지막 Undo Command를 다시 적용합니다."""

    bl_idname = "colmap.redo_last_edit"
    bl_label = "Redo Last Edit"
    bl_description = "Modifier 전용 Journal에서 마지막으로 되돌린 편집을 다시 적용합니다"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        """현재 독립 Journal에 Redo Command가 있을 때만 활성화합니다."""

        settings = getattr(context.scene, "colmap_import", None)
        return bool(
            settings
            and settings.dataset_uuid
            and modifier_redo_available(context.scene)
            and export_mutation_allowed(context)
        )

    def execute(self, context):
        """Blender Global Redo 대신 Modifier 전용 Redo를 실행합니다."""

        settings = context.scene.colmap_import
        state = None
        try:
            require_export_mutation_allowed(context, "Redo Last Edit")
            state = redo_last_edit(context)
            self.report({"INFO"}, settings.point_selection_status)
            return {"FINISHED"}
        except Exception as exc:
            if state is not None:
                force_end_point_mode(context, state.dataset_uuid)
                force_end_camera_mode(context, state.dataset_uuid)
            settings.point_selection_status = f"마지막 편집 다시 적용 실패: {exc}"
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}


class COLMAP_OT_reset_edits(bpy.types.Operator):
    bl_idname = "colmap.reset_edits"
    bl_label = "Reset Edits"
    bl_description = "현재 COLMAP source에 적용된 모든 애드온 편집 이력을 제거합니다"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        """현재 편집 이력이 있을 때만 Reset을 활성화합니다."""

        settings = getattr(context.scene, "colmap_import", None)
        return bool(
            settings
            and settings.dataset_uuid
            and settings.edit_revision > 0
            and export_mutation_allowed(context)
        )

    def invoke(self, context, event):
        """전체 편집 Reset 전 사용자 확인창을 표시합니다."""

        return context.window_manager.invoke_confirm(self, event)

    def draw(self, context):
        """Reset 범위와 원본 파일 불변 안내를 확인창에 표시합니다."""

        del context
        self.layout.label(text="저장된 COLMAP 편집 작업 전체를 초기화합니다.")
        self.layout.label(text="원본 sparse 파일은 변경되지 않습니다.", icon="INFO")

    def execute(self, context):
        """현재 Session에서 Undo 가능한 Reset Command를 실행합니다."""

        settings = context.scene.colmap_import
        state = None
        try:
            require_export_mutation_allowed(context, "Reset Edits")
            state, source_changed = reset_edits(context)
            self.report(
                {"WARNING"} if source_changed else {"INFO"},
                settings.point_selection_status,
            )
            return {"FINISHED"}
        except Exception as exc:
            if state is not None:
                force_end_point_mode(context, state.dataset_uuid)
                force_end_camera_mode(context, state.dataset_uuid)
            settings.point_selection_status = f"편집 초기화 실패: {exc}"
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}


CLASSES = (
    COLMAP_OT_select_observing_cameras,
    COLMAP_OT_remove,
    COLMAP_OT_undo_last_edit,
    COLMAP_OT_redo_last_edit,
    COLMAP_OT_reset_edits,
)
