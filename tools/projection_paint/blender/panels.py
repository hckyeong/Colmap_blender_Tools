"""3D Viewport Sidebar와 COLMAP Tools에 Projection Painting UI를 구성합니다."""

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

from .ui_messages import wrap_ui_message


_AXIS_SUMMARIES = {
    "REALITY_CAPTURE": ("Z", "-Y"),
    "BLENDER": ("Y", "Z"),
    "MAYA": ("-Z", "Y"),
    "HOUDINI": ("-Z", "Y"),
}

_PANEL_WARNING_MAX_COLUMNS = 37


def _panel_warning_lines(message: str) -> tuple[str, ...]:
    """긴 패널 경고를 세로 스크롤로 확인할 수 있는 제한 폭 행으로 나눕니다."""
    return wrap_ui_message(
        str(message),
        max_columns=_PANEL_WARNING_MAX_COLUMNS,
    )


def _draw_panel_warning(layout, message: str) -> None:
    """첫 행에만 경고 아이콘을 두고 나머지 행은 같은 들여쓰기로 표시합니다."""
    warning_column = layout.column(align=True)
    for index, line in enumerate(_panel_warning_lines(message)):
        warning_row = warning_column.row(align=True)
        warning_row.label(
            text=line,
            icon="ERROR" if index == 0 else "BLANK1",
        )


def _preview_check_icon(active):
    """단독 미리보기 활성 상태에 따라 체크 또는 빈 자리 아이콘을 반환합니다."""
    return "CHECKMARK" if active else "BLANK1"


def _layer_button_text(layer_name: str, active: bool) -> str:
    """활성 Paint Layer 이름에만 같은 버튼 안의 체크 문자열을 붙여 반환합니다."""
    return f"{layer_name}  ✓" if active else layer_name


def _layer_panel_display_layers(settings):
    """활성 Composite Source만 숨기고 실제 최상위 형제 순서를 반환합니다."""
    layers = tuple(settings.layers)
    if str(getattr(settings, "composite_state", "NONE")) != "ACTIVE":
        return tuple((index, layer, False) for index, layer in enumerate(layers))
    session_uuid = str(getattr(settings, "active_composite_session_uuid", "") or "")
    source_schema = int(getattr(settings, "composite_source_schema_version", 0))

    def is_source(layer):
        """현재 세션의 가상 Source 폴더로 숨길 일반 Layer인지 반환합니다."""
        if str(getattr(layer, "layer_role", "NORMAL")) != "NORMAL":
            return False
        if source_schema < 1:
            return True
        return str(getattr(layer, "composite_source_session_uuid", "") or "") == session_uuid

    display_layers = []
    for layer_index, layer in enumerate(layers):
        role = str(getattr(layer, "layer_role", "NORMAL"))
        if is_source(layer):
            continue
        display_layers.append((layer_index, layer, False))
    return tuple(display_layers)


def _layer_panel_child_indent(layer):
    """Composite의 직접 Mask·Adjustment만 Source와 같은 들여쓰기로 반환합니다."""
    if str(getattr(layer, "layer_role", "NORMAL")) == "COMPOSITE":
        return 0.12
    return 0.22


def _layer_panel_adjustment_mask_indent(layer):
    """Adjustment Mask가 부모 Adjustment보다 한 단계 더 들어간 위치를 반환합니다."""
    if str(getattr(layer, "layer_role", "NORMAL")) == "COMPOSITE":
        return 0.22
    return 0.34


def _layer_panel_source_rows(layer, source_layers):
    """Composite의 최하단에 표시할 Source 가상 행과 비활성 자식 행을 반환합니다."""
    rows = (("SOURCE", None, 0.12),)
    if not bool(getattr(layer, "composite_source_expanded", False)):
        return rows
    return rows + tuple(("SOURCE_CHILD", source, 0.22) for source in source_layers)


def _source_child_status_icon(source, *, source_schema: int = 1):
    """현재 schema에 맞춰 Bake 제외 또는 Legacy 숨김 Source 아이콘을 반환합니다."""
    if int(source_schema) >= 1:
        return None if bool(source.composite_bake_included) else "HIDE_ON"
    if not bool(getattr(source, "composite_saved_visible", True)):
        return "HIDE_ON"
    return None


def _layer_panel_source_insertions(display_layers):
    """각 Composite의 Source 묶음을 자체 보조 항목 뒤에 둘 위치를 반환합니다."""
    insertions = []
    for composite_index, (_layer_index, layer, _composite_child) in enumerate(display_layers):
        if str(getattr(layer, "layer_role", "NORMAL")) != "COMPOSITE":
            continue
        insertions.append((composite_index, layer))
    return tuple(insertions)


def _draw_composite_source_rows(tree, layer, source_layers, *, source_schema: int = 1):
    """Composite의 최하단 Source 가상 행과 비활성 소스 자식 행을 어둡게 그립니다."""
    for row_type, source, indent in _layer_panel_source_rows(layer, source_layers):
        source_container = tree.row(align=True)
        if row_type == "SOURCE_CHILD":
            source_container.enabled = False
        source_split = source_container.split(factor=indent, align=True)
        source_branch = source_split.column(align=True)
        source_branch.enabled = False
        source_branch.alignment = "RIGHT"
        source_branch.label(text="↳")
        source_row = source_split.row(align=True)
        if row_type == "SOURCE":
            source_row.prop(
                layer,
                "composite_source_expanded",
                text="",
                icon=(
                    "TRIA_DOWN"
                    if layer.composite_source_expanded
                    else "TRIA_RIGHT"
                ),
                emboss=False,
            )
            source_label = source_row.row(align=True)
            source_label.enabled = False
            source_label.label(text="Source")
            continue
        source_row.label(text=str(source.name))
        status_icon = _source_child_status_icon(source, source_schema=source_schema)
        if status_icon is not None:
            source_row.label(text="", icon=status_icon)


def _axis_summary(settings):
    """선택한 Camera 표시축을 참조 Import UI 형식으로 반환합니다."""
    forward, up = _AXIS_SUMMARIES.get(
        settings.camera_axis_preset,
        (settings.camera_forward, settings.camera_up),
    )
    return f"Forward: {forward}, Up: {up}"


def _colmap_tools_menu_available() -> bool:
    """외부 COLMAP Tools 메뉴가 현재 Blender에 등록되어 있는지 반환합니다."""

    return getattr(bpy.types, "COLMAP_MT_tools", None) is not None


def _paint_workspace_active(context) -> bool:
    """현재 Window가 Scene의 전용 Paint Workspace를 표시하는지 반환합니다."""

    window = getattr(context, "window", None)
    settings = getattr(getattr(context, "scene", None), "projection_paint", None)
    workspace = getattr(settings, "paint_workspace", None)
    return bool(
        window is not None
        and settings is not None
        and workspace is not None
        and getattr(window, "workspace", None)
        is workspace
    )


def _draw_paint_mode_operator(layout, settings, *, text: str) -> None:
    """Projection Painting 진입과 종료를 같은 Toggle Operator로 표시합니다."""

    layout.operator(
        "projection_paint.toggle_paint",
        text=str(text),
        icon="TPAINT_HLT",
        depress=bool(settings.paint_enabled),
    )


def _draw_paint_setup(layout, settings, *, include_paint_mode: bool) -> None:
    """Paint Target과 해상도 및 세션 제어를 지정한 Layout에 표시합니다."""

    layout.prop(settings, "paint_target")
    layout.prop(settings, "use_base_resolution")
    resolution_row = layout.row()
    resolution_row.enabled = not bool(settings.use_base_resolution)
    resolution_row.prop(settings, "overlay_resolution")
    if include_paint_mode:
        _draw_paint_mode_operator(layout, settings, text="Painting Mode")
    layout.operator(
        "projection_paint.reconnect_stencil",
        icon="FILE_REFRESH",
    )
    layout.operator(
        "projection_paint.project_stencil",
        icon="IMAGE_DATA",
    )
    if settings.recovery_available:
        layout.operator("projection_paint.recover_overlay", icon="RECOVER_LAST")


def draw_colmap_tools_projection_paint(self, context) -> None:
    """Window의 COLMAP Tools 메뉴에서 전용 Paint 작업공간을 엽니다."""

    del context
    self.layout.operator_context = "INVOKE_DEFAULT"
    self.layout.operator(
        "projection_paint.open_workspace",
        text="Projection Painting",
        icon="TPAINT_HLT",
    )


class PP_OT_open_paint_workspace(bpy.types.Operator):
    """COLMAP Tools 메뉴에서 설정용 Projection Paint 작업공간을 엽니다."""

    bl_idname = "projection_paint.open_workspace"
    bl_label = "Projection Painting"
    bl_description = "Paint 준비 없이 Projection Paint 작업공간만 엽니다"
    bl_options = {"REGISTER"}

    def execute(self, context):
        """다른 COLMAP 도구 종료 뒤 Paint Workspace 구성을 시작합니다."""

        from .paint_workspace import PaintWorkspaceError

        try:
            result = request_workspace_activation(context, "projection_paint")
        except (PaintWorkspaceError, WorkspaceHandoffError, RuntimeError, ValueError) as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        if result == ACTIVATION_DEFERRED:
            self.report(
                {"INFO"},
                "현재 COLMAP Workspace를 종료한 뒤 Projection Paint를 준비합니다.",
            )
        return {"FINISHED"}


class PP_PT_status(bpy.types.Panel):
    """현재 Dataset, Paint와 Overlay 내구성 상태를 표시합니다."""

    bl_label = "Projection Painting"
    bl_idname = "PP_PT_status"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Projection Paint"
    bl_options = {"HIDE_HEADER"}

    def draw(self, context):
        """현재 자동화 상태를 읽기 전용으로 표시합니다."""
        settings = context.scene.projection_paint
        box = self.layout.box()
        box.label(text="Status")
        box.label(text=settings.status, icon="ERROR" if settings.status_kind == "ERROR" else "INFO")
        box.label(text=f"Paint: {settings.paint_state}")
        box.label(text=f"Overlay: {settings.durability_state}")
        if settings.current_image_id:
            box.label(text=f"IMAGE_ID: {settings.current_image_id}")
        if settings.current_image_name:
            box.label(text=f"Image: {settings.current_image_name}")


class PP_PT_colmap(bpy.types.Panel):
    """참조 COLMAP Importer와 같은 Import UI를 표시합니다."""

    bl_label = "COLMAP Import"
    bl_idname = "PP_PT_colmap"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Projection Paint"

    def draw(self, context):
        """Dataset, Import와 Point·Camera 표시 설정을 참조 순서로 표시합니다."""
        settings = context.scene.projection_paint
        layout = self.layout
        layout.prop(settings, "dataset_root", text="Dataset Root")
        layout.prop(settings, "input_format")
        layout.operator("projection_paint.import_colmap", icon="IMPORT")

        axis_box = layout.box()
        axis_box.label(text="Display Axis")
        axis_box.prop(settings, "camera_axis_preset", text="Preset")
        if settings.camera_axis_preset == "CUSTOM":
            axis_box.prop(settings, "camera_forward", text="Forward Axis")
            axis_box.prop(settings, "camera_up", text="Up Axis")
        else:
            axis_box.label(text=_axis_summary(settings))
        axis_box.label(text="COLMAP 원본 축 유지", icon="INFO")

        display_box = layout.box()
        display_box.label(text="Point Display")
        display_box.prop(settings, "point_size")
        display_box.label(text="Camera Display")
        display_box.prop(settings, "camera_frustum_size")


class PP_PT_paint(bpy.types.Panel):
    """Paint Target, Overlay와 Texture Paint 진입 UI를 표시합니다."""

    bl_label = "Paint Setup"
    bl_idname = "PP_PT_paint"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Projection Paint"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        """Paint Target과 Overlay 해상도 및 세션 버튼을 표시합니다."""
        settings = context.scene.projection_paint
        _draw_paint_setup(
            self.layout,
            settings,
            include_paint_mode=(
                _paint_workspace_active(context)
                or not _colmap_tools_menu_available()
            ),
        )


class PP_PT_camera_review(bpy.types.Panel):
    """현재 Frame의 COLMAP 이미지를 Camera Background로 검토하는 UI를 표시합니다."""

    bl_label = "Camera Review"
    bl_idname = "PP_PT_camera_review"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Projection Paint"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        """현재 IMAGE_ID와 배경 표시 토글 및 Alpha를 표시합니다."""

        settings = context.scene.projection_paint
        layout = self.layout
        info = layout.box()
        info.label(
            text=(
                f"IMAGE_ID: {settings.current_image_id}"
                if settings.current_image_id
                else "IMAGE_ID: -"
            )
        )
        info.label(
            text=(
                f"Image: {settings.current_image_name}"
                if settings.current_image_name
                else "Image: -"
            ),
            icon="IMAGE_DATA",
        )
        layout.prop(settings, "review_background_alpha", slider=False)
        layout.prop(
            settings,
            "review_background_enabled",
            toggle=False,
            icon="HIDE_OFF" if settings.review_background_enabled else "HIDE_ON",
        )
        layout.operator(
            "projection_paint.review_look_through",
            icon="CAMERA_DATA",
        )


class PP_PT_layers(bpy.types.Panel):
    """Paint Setup 아래에 논리 Paint Layer 목록을 표시합니다."""

    bl_label = "Layers"
    bl_idname = "PP_PT_layers"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Projection Paint"
    bl_parent_id = "PP_PT_paint"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        """Layer 부모와 들여쓴 보조 자식 행 및 관리 버튼을 표시합니다."""
        settings = context.scene.projection_paint
        from .layers import (
            active_layer_adjustment,
            adjustment_mask_row_selected,
            adjustment_row_selected,
            adjustment_ui,
            layer_mask_row_selected,
            layer_preview_active,
            layer_row_selected,
            mask_preview_active,
        )
        from .layer_shader import (
            composite_layer_is_editable,
            composite_state_locks_layer_edits,
        )

        row = self.layout.row()
        tree = row.column(align=True)
        active_index = int(settings.active_layer_index)
        composite_active = str(settings.composite_state) == "ACTIVE"
        source_session_uuid = str(settings.active_composite_session_uuid or "")
        source_schema = int(getattr(settings, "composite_source_schema_version", 0))
        source_layers = tuple(
            layer
            for layer in settings.layers
            if str(getattr(layer, "layer_role", "NORMAL")) == "NORMAL"
            and (
                source_schema < 1
                or str(getattr(layer, "composite_source_session_uuid", "") or "")
                == source_session_uuid
            )
        )
        display_layers = _layer_panel_display_layers(settings)
        source_insertions = {
            display_index: composite_layer
            for display_index, composite_layer in _layer_panel_source_insertions(
                display_layers
            )
        }
        for display_index, (layer_index, layer, composite_child) in enumerate(
            display_layers
        ):
            layer_editable = composite_layer_is_editable(settings, layer)
            parent_container = tree.row(align=True)
            if composite_child:
                parent_split = parent_container.split(factor=0.12, align=True)
                parent_branch = parent_split.column(align=True)
                parent_branch.alignment = "RIGHT"
                parent_branch.label(text="↳")
                parent = parent_split.row(align=True)
            else:
                parent = parent_container
            parent.enabled = layer_editable
            parent.context_pointer_set("projection_paint_layer", layer)
            toggle = parent.operator(
                "projection_paint.layer_toggle",
                text="",
                icon="TRIA_DOWN" if layer.expanded else "TRIA_RIGHT",
                emboss=False,
            )
            toggle.layer_uuid = str(layer.layer_uuid)
            parent_selected = layer_row_selected(settings, layer_index)
            parent.operator_context = "INVOKE_DEFAULT"
            select = parent.operator(
                "projection_paint.layer_select",
                text=_layer_button_text(
                    str(layer.name),
                    layer_index == active_index,
                ),
                depress=parent_selected,
                emboss=parent_selected,
            )
            select.layer_uuid = str(layer.layer_uuid)
            layer_preview = parent.operator(
                "projection_paint.layer_preview_toggle",
                text="",
                icon=_preview_check_icon(
                    layer_preview_active(settings, str(layer.layer_uuid))
                ),
                emboss=False,
            )
            layer_preview.layer_uuid = str(layer.layer_uuid)
            parent.prop(
                layer,
                "visible",
                text="",
                icon="HIDE_OFF" if layer.visible else "HIDE_ON",
                emboss=False,
            )
            if not layer.expanded:
                source_layer = source_insertions.get(display_index)
                if (
                    composite_active
                    and source_layer is not None
                    and source_layer.expanded
                ):
                    _draw_composite_source_rows(
                        tree,
                        source_layer,
                        source_layers,
                        source_schema=source_schema,
                    )
                continue
            if str(layer.mask.mask_uuid or ""):
                child = tree.row(align=True)
                child.enabled = layer_editable
                child_split = child.split(
                    factor=_layer_panel_child_indent(layer),
                    align=True,
                )
                branch = child_split.column(align=True)
                branch.alignment = "RIGHT"
                branch.label(text="↳")
                child_content = child_split.row(align=True)
                selected = layer_mask_row_selected(settings, layer_index)
                select_mask = child_content.operator(
                    "projection_paint.layer_mask_select",
                    text="Mask",
                    depress=selected,
                    emboss=selected,
                )
                select_mask.layer_uuid = str(layer.layer_uuid)
                select_mask.mask_uuid = str(layer.mask.mask_uuid)
                preview_active = mask_preview_active(
                    settings,
                    str(layer.mask.mask_uuid),
                )
                preview = child_content.operator(
                    "projection_paint.mask_preview_toggle",
                    text="",
                    icon=_preview_check_icon(preview_active),
                    emboss=False,
                )
                preview.layer_uuid = str(layer.layer_uuid)
                preview.mask_uuid = str(layer.mask.mask_uuid)
                child_content.prop(
                    layer.mask,
                    "enabled",
                    text="",
                    icon="HIDE_OFF" if layer.mask.enabled else "HIDE_ON",
                    emboss=False,
                )
            for adjustment_index, adjustment in enumerate(layer.adjustments):
                label, _icon = adjustment_ui(adjustment.adjustment_type)
                child = tree.row(align=True)
                child.enabled = layer_editable
                child_split = child.split(
                    factor=_layer_panel_child_indent(layer),
                    align=True,
                )
                branch = child_split.column(align=True)
                branch.alignment = "RIGHT"
                branch.label(text="↳")
                child_content = child_split.row(align=True)
                if str(adjustment.mask.mask_uuid or ""):
                    adjustment_toggle = child_content.operator(
                        "projection_paint.adjustment_toggle",
                        text="",
                        icon=(
                            "TRIA_DOWN" if adjustment.expanded else "TRIA_RIGHT"
                        ),
                        emboss=False,
                    )
                    adjustment_toggle.layer_uuid = str(layer.layer_uuid)
                    adjustment_toggle.adjustment_uuid = str(
                        adjustment.adjustment_uuid
                    )
                is_selected = adjustment_row_selected(
                    settings,
                    layer_index,
                    adjustment_index,
                )
                child_operator = child_content.operator(
                    "projection_paint.layer_adjustment_select",
                    text=label,
                    depress=is_selected,
                    emboss=is_selected,
                )
                child_operator.layer_uuid = str(layer.layer_uuid)
                child_operator.adjustment_uuid = str(
                    adjustment.adjustment_uuid
                )
                child_content.prop(
                    adjustment,
                    "enabled",
                    text="",
                    icon="HIDE_OFF" if adjustment.enabled else "HIDE_ON",
                    emboss=False,
                )
                if str(adjustment.mask.mask_uuid or "") and adjustment.expanded:
                    mask_child = tree.row(align=True)
                    mask_child.enabled = layer_editable
                    mask_split = mask_child.split(
                        factor=_layer_panel_adjustment_mask_indent(layer),
                        align=True,
                    )
                    mask_branch = mask_split.column(align=True)
                    mask_branch.alignment = "RIGHT"
                    mask_branch.label(text="↳")
                    mask_content = mask_split.row(align=True)
                    mask_selected = adjustment_mask_row_selected(
                        settings,
                        layer_index,
                        adjustment_index,
                    )
                    select_mask = mask_content.operator(
                        "projection_paint.adjustment_mask_select",
                        text="Mask",
                        depress=mask_selected,
                        emboss=mask_selected,
                    )
                    select_mask.layer_uuid = str(layer.layer_uuid)
                    select_mask.adjustment_uuid = str(adjustment.adjustment_uuid)
                    select_mask.mask_uuid = str(adjustment.mask.mask_uuid)
                    preview_active = mask_preview_active(
                        settings,
                        str(adjustment.mask.mask_uuid),
                    )
                    preview = mask_content.operator(
                        "projection_paint.mask_preview_toggle",
                        text="",
                        icon=_preview_check_icon(preview_active),
                        emboss=False,
                    )
                    preview.layer_uuid = str(layer.layer_uuid)
                    preview.mask_uuid = str(adjustment.mask.mask_uuid)
                    mask_content.prop(
                        adjustment.mask,
                        "enabled",
                        text="",
                        icon=(
                            "HIDE_OFF" if adjustment.mask.enabled else "HIDE_ON"
                        ),
                        emboss=False,
                    )
            source_layer = source_insertions.get(display_index)
            if not composite_active or source_layer is None or not source_layer.expanded:
                continue
            _draw_composite_source_rows(
                tree,
                source_layer,
                source_layers,
                source_schema=source_schema,
            )
        buttons = row.column(align=True)
        add_button = buttons.column(align=True)
        add_button.enabled = not composite_state_locks_layer_edits(settings)
        add_button.operator("projection_paint.layer_add", text="", icon="ADD")
        remove_button = buttons.column(align=True)
        active_layer = (
            settings.layers[active_index]
            if 0 <= active_index < len(settings.layers)
            else None
        )
        remove_button.enabled = bool(
            active_layer is not None
            and composite_layer_is_editable(settings, active_layer)
            and str(getattr(active_layer, "layer_role", "NORMAL")) != "COMPOSITE"
        )
        remove_button.operator("projection_paint.layer_remove", text="", icon="REMOVE")
        buttons.separator()
        move_up = buttons.column(align=True)
        move_up.enabled = (
            active_index > 0
            and str(settings.composite_state) == "NONE"
        )
        operator = move_up.operator(
            "projection_paint.layer_move",
            text="",
            icon="TRIA_UP",
        )
        operator.direction = "UP"
        move_down = buttons.column(align=True)
        move_down.enabled = (
            0 <= active_index < len(settings.layers) - 1
            and str(settings.composite_state) == "NONE"
        )
        operator = move_down.operator(
            "projection_paint.layer_move",
            text="",
            icon="TRIA_DOWN",
        )
        operator.direction = "DOWN"
        if (
            settings.paint_material is not None
            and str(settings.composite_state) == "NONE"
            and not any(
                bool(getattr(layer, "is_original_layer", False))
                for layer in settings.layers
            )
        ):
            buttons.separator()
            buttons.operator(
                "projection_paint.original_layer_restore",
                text="",
                icon="FILE_REFRESH",
            )
        composite_options = self.layout.column(align=True)
        if str(settings.composite_state) == "ACTIVE":
            current_composite = next(
                (
                    layer
                    for layer in settings.layers
                    if str(getattr(layer, "layer_role", "NORMAL")) == "COMPOSITE"
                    and str(getattr(layer, "composite_session_uuid", "") or "")
                    == str(settings.active_composite_session_uuid or "")
                ),
                None,
            )
            if current_composite is not None:
                composite_options.label(
                    text=(
                        "현재 결과: Color + Alpha"
                        if bool(current_composite.composite_has_baked_alpha)
                        else "현재 결과: Color"
                    )
                )
        composite_options.prop(settings, "composite_include_alpha")
        try:
            from .composite_layer import composite_alpha_warning

            alpha_warning = composite_alpha_warning(settings)
        except (AttributeError, RuntimeError, TypeError, ValueError):
            alpha_warning = ""
        if alpha_warning and not bool(settings.composite_include_alpha):
            _draw_panel_warning(composite_options, alpha_warning)
        composite_actions = self.layout.row(align=True)
        if str(settings.composite_state) == "ACTIVE":
            active_composite = next(
                (
                    layer
                    for layer in settings.layers
                    if str(getattr(layer, "layer_role", "NORMAL")) == "COMPOSITE"
                    and str(getattr(layer, "composite_session_uuid", "") or "")
                    == str(settings.active_composite_session_uuid or "")
                ),
                None,
            )
            recomposite = composite_actions.row(align=True)
            recomposite.enabled = bool(
                active_composite is not None
                and bool(active_composite.ui_selected)
                and bool(active_composite.visible)
            )
            recomposite.operator(
                "projection_paint.composite_layer_create",
                text="Composite 다시 생성",
                icon="NODE_MATERIAL",
            )
            composite_actions.operator(
                "projection_paint.composite_layer_release",
                text="Composite 해제",
                icon="LOOP_BACK",
            )
        else:
            composite_actions.enabled = str(settings.composite_state) == "NONE"
            composite_actions.operator(
                "projection_paint.composite_layer_create",
                text="Composite Layer 생성",
                icon="NODE_MATERIAL",
            )
        export_row = self.layout.row(align=True)
        export_row.operator(
            "projection_paint.texture_export",
            text="Export Texture",
            icon="EXPORT",
        )
        if active_layer is None:
            return
        selected_adjustment = active_layer_adjustment(active_layer)
        if selected_adjustment is not None and not str(settings.active_mask_uuid or ""):
            label, icon = adjustment_ui(selected_adjustment.adjustment_type)
            adjustment_box = self.layout.box()
            adjustment_box.enabled = composite_layer_is_editable(settings, active_layer)
            header = adjustment_box.row(align=True)
            header.label(text=label, icon=icon)
            header.prop(selected_adjustment, "enabled", text="Enabled")
            delete_operator = header.operator(
                "projection_paint.layer_adjustment_remove",
                text="",
                icon="X",
            )
            delete_operator.layer_uuid = str(active_layer.layer_uuid)
            delete_operator.adjustment_uuid = str(
                selected_adjustment.adjustment_uuid
            )
        parameter_adjustments = [
            adjustment
            for adjustment in active_layer.adjustments
            if adjustment.adjustment_type != "MASK"
        ]
        if not parameter_adjustments:
            return
        parameter_box = self.layout.box()
        parameter_box.enabled = composite_layer_is_editable(settings, active_layer)
        for adjustment in parameter_adjustments:
            if adjustment.adjustment_type == "BRIGHTNESS":
                parameter_box.prop(adjustment, "brightness")
            elif adjustment.adjustment_type == "EXPOSURE":
                parameter_box.prop(adjustment, "exposure")
            elif adjustment.adjustment_type in {"HUE", "SATURATION"}:
                parameter_box.prop(adjustment, "hue")
                parameter_box.prop(adjustment, "saturation")


CLASSES = (
    PP_OT_open_paint_workspace,
    PP_PT_status,
    PP_PT_colmap,
    PP_PT_camera_review,
    PP_PT_paint,
    PP_PT_layers,
)
