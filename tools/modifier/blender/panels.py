"""전용 Workspace의 COLMAP Modifier 패널을 구성합니다."""

import bpy

from .axis_display import CUSTOM_AXIS_PRESET, axis_summary
from .camera_filter_queue import (
    PENDING_STATUS_APPLYING,
    PENDING_STATUS_READY,
    PENDING_STATUS_STALE,
    current_camera_is_pending,
    has_pending_filter_data,
)
from .ownership import DATASET_UUID_KEY, OWNER_KEY, OWNER_TOKEN, RECORD_KIND_KEY
from .operation_guard import export_mutation_allowed
from .edit_workflows import modifier_redo_available, modifier_undo_available
from .modifier_session import CONTROL_PANEL_CATEGORY, modifier_panel_poll
from .status_display import (
    STATUS_VISIBLE_ROWS,
    request_status_display_refresh,
)


def _runtime_ready(context) -> bool:
    """현재 Scene의 Modifier Runtime과 편집 token이 일치하는지 반환합니다."""

    from .runtime_state import cached_runtime_matches_scene

    return cached_runtime_matches_scene(context.scene)


class _COLMAPModifierPanelMixin:
    """전용 Control View에서만 하위 패널을 표시합니다."""

    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = CONTROL_PANEL_CATEGORY

    @classmethod
    def poll(cls, context):
        """현재 Context가 활성 Modifier Control View인지 확인합니다."""

        return modifier_panel_poll(context)


class COLMAP_PT_modifier(_COLMAPModifierPanelMixin, bpy.types.Panel):
    """COLMAP Modifier Session 종료를 제공하는 최상위 패널입니다."""

    bl_label = "COLMAP Modifier"
    bl_idname = "COLMAP_PT_modifier"
    bl_order = 0

    def draw(self, _context):
        """전용 Workspace 종료 버튼을 표시합니다."""

        self.layout.operator(
            "colmap.exit_modifier_workspace",
            text="COLMAP Modifier 종료",
            icon="QUIT",
        )


class COLMAP_PT_status(_COLMAPModifierPanelMixin, bpy.types.Panel):
    bl_label = "COLMAP Status"
    bl_idname = "COLMAP_PT_status"
    bl_parent_id = "COLMAP_PT_modifier"
    bl_order = 10

    def draw(self, context):
        """상태 기록과 진행 중인 지연 Runtime 복원률을 표시합니다."""

        layout = self.layout
        settings = context.scene.colmap_import

        status_box = layout.box()
        request_status_display_refresh(context)
        status_box.template_list(
            "COLMAP_UL_status_lines",
            "status_display_5_rows",
            settings,
            "status_display_lines",
            settings,
            "status_display_index",
            rows=STATUS_VISIBLE_ROWS,
            maxrows=STATUS_VISIBLE_ROWS,
            sort_lock=True,
        )
        from .runtime_state import runtime_restore_job

        restore_job = runtime_restore_job(context.scene)
        if restore_job is not None and not restore_job.done and not restore_job.failed:
            status_box.label(
                text=f"Runtime Restore: {restore_job.progress.fraction * 100.0:.1f}%",
                icon="TIME",
            )


class COLMAP_PT_import(_COLMAPModifierPanelMixin, bpy.types.Panel):
    bl_label = "COLMAP Import"
    bl_idname = "COLMAP_PT_import"
    bl_parent_id = "COLMAP_PT_modifier"
    bl_order = 20
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        """가져오기와 축 및 Point·Camera 표시 설정을 한 패널에 그립니다."""

        layout = self.layout
        settings = context.scene.colmap_import

        layout.prop(settings, "dataset_root", text="Dataset Root")

        layout.prop(settings, "input_format")
        layout.operator("colmap.import_model", icon="IMPORT")

        axis_box = layout.box()
        axis_box.label(text="Display Axis")
        axis_box.prop(settings, "axis_preset")
        if settings.axis_preset == CUSTOM_AXIS_PRESET:
            axis_box.prop(settings, "axis_forward")
            axis_box.prop(settings, "axis_up")
        else:
            axis_box.label(
                text=axis_summary(settings.axis_forward, settings.axis_up)
            )
        axis_box.label(text="Export: COLMAP 원본 축 유지", icon="INFO")

        display_box = layout.box()
        display_box.label(text="Point Display")
        display_box.prop(settings, "point_size")
        display_box.label(text="Camera Display")
        display_box.prop(settings, "camera_frustum_size")


class COLMAP_PT_point_selection(_COLMAPModifierPanelMixin, bpy.types.Panel):
    bl_label = "Point Selection"
    bl_idname = "COLMAP_PT_point_selection"
    bl_parent_id = "COLMAP_PT_modifier"
    bl_order = 30
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        """Runtime 준비 여부에 따라 Point·Camera 편집 입력을 잠가 표시합니다."""

        layout = self.layout
        layout.enabled = _runtime_ready(context)
        settings = context.scene.colmap_import
        pending = has_pending_filter_data(settings)

        mode_box = layout.box()
        if settings.point_edit_mode_active:
            mode_text = "Mode: Editing Points"
        elif settings.camera_edit_mode_active:
            mode_text = "Mode: Editing Cameras"
        else:
            mode_text = "Mode: Locked"
        mode_box.label(text=mode_text)

        point_mode_row = mode_box.row()
        point_mode_row.enabled = bool(
            not pending and not settings.camera_edit_mode_active
        )
        point_mode_row.operator(
            "colmap.toggle_edit_point_mode",
            text=(
                "Exit Point Mode"
                if settings.point_edit_mode_active
                else "Edit Point Mode"
            ),
            depress=settings.point_edit_mode_active,
            icon="EDITMODE_HLT",
        )
        camera_mode_row = mode_box.row()
        camera_mode_row.enabled = bool(
            not pending and not settings.point_edit_mode_active
        )
        camera_mode_row.operator(
            "colmap.toggle_edit_camera_mode",
            text=(
                "Exit Camera Mode"
                if settings.camera_edit_mode_active
                else "Edit Camera Mode"
            ),
            depress=settings.camera_edit_mode_active,
            icon="CAMERA_DATA",
        )

        selected_objects = tuple(context.selected_objects)
        selected_cameras = [
            obj
            for obj in selected_objects
            if obj.get(OWNER_KEY) == OWNER_TOKEN
            and obj.get(RECORD_KIND_KEY) == "CAMERA"
            and obj.get(DATASET_UUID_KEY) == settings.dataset_uuid
        ]
        mixed_selection = len(selected_cameras) != len(selected_objects)

        counts = layout.box()
        counts.label(text=f"Selected Points: {settings.selected_point_count}")
        counts.label(text=f"Observing Cameras: {settings.observing_camera_count}")
        counts.label(
            text=(
                f"Selected Cameras: {len(selected_cameras)}"
                if settings.camera_edit_mode_active
                else f"Selected Cameras: {settings.camera_target_count}"
            )
        )

        column = layout.column(align=True)
        column.enabled = bool(
            settings.dataset_uuid
            and not pending
            and not settings.camera_edit_mode_active
        )
        column.operator("colmap.select_observing_cameras", icon="RESTRICT_SELECT_OFF")

        if settings.point_edit_mode_active:
            target_text = "Remove Target: Selected Points"
            remove_enabled = True
        elif (
            settings.camera_edit_mode_active
            and selected_cameras
            and not mixed_selection
        ):
            target_text = (
                f"Remove Target: {len(selected_cameras)} Cameras + Observed Points"
            )
            remove_enabled = True
        else:
            target_text = "Remove Target: None"
            remove_enabled = False
        layout.label(text=target_text)
        remove_row = layout.row()
        remove_row.enabled = bool(
            settings.dataset_uuid and not pending and remove_enabled
        )
        remove_row.operator("colmap.remove", icon="TRASH")

        history_row = layout.row(align=True)
        undo_column = history_row.column(align=True)
        undo_column.enabled = bool(
            not pending and modifier_undo_available(context.scene)
        )
        undo_column.operator("colmap.undo_last_edit", icon="LOOP_BACK")
        redo_column = history_row.column(align=True)
        redo_column.enabled = bool(
            not pending and modifier_redo_available(context.scene)
        )
        redo_column.operator("colmap.redo_last_edit", icon="LOOP_FORWARDS")
        reset_row = layout.row()
        reset_row.enabled = bool(not pending and settings.edit_revision > 0)
        reset_row.operator("colmap.reset_edits", icon="FILE_REFRESH")


class COLMAP_PT_camera_review(_COLMAPModifierPanelMixin, bpy.types.Panel):
    bl_label = "Camera Review"
    bl_idname = "COLMAP_PT_camera_review"
    bl_parent_id = "COLMAP_PT_modifier"
    bl_order = 40
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        """Runtime 준비 전 Camera Review 입력을 잠가 저장 상태만 보존합니다."""

        layout = self.layout
        layout.enabled = _runtime_ready(context)
        settings = context.scene.colmap_import
        pending = has_pending_filter_data(settings)
        editing = (
            settings.point_edit_mode_active
            or settings.camera_edit_mode_active
        )

        if settings.review_current_index >= 0:
            current = settings.review_current_index + 1
        else:
            current = 0
        info = layout.box()
        info.label(text=f"Camera: {current} / {settings.review_total_count}")
        info.label(
            text=(
                f"IMAGE_ID: {settings.review_current_image_id}"
                if settings.review_current_image_id
                else "IMAGE_ID: -"
            )
        )
        info.label(
            text=(
                f"Model: {settings.review_camera_model}"
                if settings.review_camera_model
                else "Model: -"
            )
        )
        image_icon = (
            "ERROR"
            if settings.review_image_status.startswith(
                ("MISSING", "AMBIGUOUS", "UNSUPPORTED", "LOAD_FAILED")
            )
            else "IMAGE_DATA"
        )
        info.label(
            text=(
                f"Image: {settings.review_image_status}"
                if settings.review_image_status
                else "Image: -"
            ),
            icon=image_icon,
        )
        if settings.review_camera_model and not settings.review_exact_projection:
            info.label(text="정확한 배경 투영을 지원하지 않습니다.", icon="ERROR")

        layout.prop(
            settings,
            "review_background_alpha",
            text="Background Alpha",
            slider=False,
        )
        layout.prop(
            settings,
            "review_background_enabled",
            text="Show Background Image",
            toggle=False,
            icon=(
                "HIDE_OFF"
                if settings.review_background_enabled
                else "HIDE_ON"
            ),
        )

        look_row = layout.row()
        look_row.enabled = bool(
            not editing and settings.review_current_index >= 0
        )
        look_row.operator("colmap.review_look_through", icon="CAMERA_DATA")

        pending_box = layout.box()
        pending_box.label(text="Pending Camera Filters")
        pending_box.label(
            text=f"Pending Cameras: {settings.review_pending_camera_count}"
        )
        pending_box.label(
            text=f"Pending Point3D: {settings.review_pending_point_count}"
        )

        filter_row = pending_box.row()
        filter_row.enabled = bool(
            not editing
            and settings.review_current_index >= 0
            and settings.review_total_count > 0
            and settings.review_pending_filter_status
            not in {PENDING_STATUS_APPLYING, PENDING_STATUS_STALE}
        )
        is_current_pending = current_camera_is_pending(context)
        filter_row.operator(
            "colmap.review_filter_current",
            text=(
                "Unfilter Current Camera"
                if is_current_pending
                else "Filter Current Camera"
            ),
            icon="X" if is_current_pending else "TRASH",
        )

        undo_row = pending_box.row()
        undo_row.enabled = bool(
            not editing
            and pending
            and settings.review_pending_filter_status == PENDING_STATUS_READY
        )
        undo_row.operator(
            "colmap.review_undo_last_filter",
            text="Undo Last Filter",
            icon="LOOP_BACK",
        )

        pending_actions = pending_box.row(align=True)
        apply_button = pending_actions.row()
        apply_button.enabled = bool(
            pending
            and settings.review_pending_filter_status == PENDING_STATUS_READY
        )
        apply_button.operator(
            "colmap.review_apply_pending_filters",
            text="Apply",
            icon="CHECKMARK",
        )
        clear_button = pending_actions.row()
        clear_button.enabled = bool(
            pending
            and settings.review_pending_filter_status
            != PENDING_STATUS_APPLYING
        )
        clear_button.operator(
            "colmap.review_clear_pending_filters",
            text="Clear All",
            icon="X",
        )
        if settings.review_pending_filter_status == PENDING_STATUS_STALE:
            pending_box.label(text="Stale: Clear만 허용됩니다.", icon="ERROR")


class COLMAP_PT_export(_COLMAPModifierPanelMixin, bpy.types.Panel):
    bl_label = "COLMAP Export"
    bl_idname = "COLMAP_PT_export"
    bl_parent_id = "COLMAP_PT_modifier"
    bl_order = 50
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        """Runtime과 Scene token이 일치할 때만 Export 입력을 활성화합니다."""

        layout = self.layout
        layout.enabled = _runtime_ready(context)
        settings = context.scene.colmap_import

        layout.prop(settings, "export_output_format", text="Format")
        layout.prop(settings, "export_backup_enabled", text="Backup")
        row = layout.row()
        row.enabled = bool(
            settings.dataset_uuid
            and settings.edit_revision > 0
            and not settings.point_edit_mode_active
            and not settings.camera_edit_mode_active
            and not has_pending_filter_data(settings)
            and not settings.export_running
            and export_mutation_allowed(context)
        )
        row.operator("colmap.export", text="Export", icon="EXPORT")


CLASSES = (
    COLMAP_PT_modifier,
    COLMAP_PT_status,
    COLMAP_PT_import,
    COLMAP_PT_point_selection,
    COLMAP_PT_camera_review,
    COLMAP_PT_export,
)
