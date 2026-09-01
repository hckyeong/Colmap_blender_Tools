"""Projection Painting 패널과 영구 Manifest에 필요한 속성을 정의합니다."""

from __future__ import annotations

import bpy
from bpy.props import (
    BoolProperty,
    CollectionProperty,
    EnumProperty,
    FloatProperty,
    FloatVectorProperty,
    IntProperty,
    PointerProperty,
    StringProperty,
)

AXIS_ITEMS = (
    ("X", "+X", "양의 X축"),
    ("-X", "-X", "음의 X축"),
    ("Y", "+Y", "양의 Y축"),
    ("-Y", "-Y", "음의 Y축"),
    ("Z", "+Z", "양의 Z축"),
    ("-Z", "-Z", "음의 Z축"),
)

AXIS_PRESET_ITEMS = (
    ("REALITY_CAPTURE", "RealityCapture", "RealityCapture 표시축"),
    ("BLENDER", "Blender", "Blender 표시축"),
    ("MAYA", "Maya", "Maya 표시축"),
    ("HOUDINI", "Houdini", "Houdini 표시축"),
    ("CUSTOM", "Custom", "Forward와 Up을 직접 선택"),
)

def _mesh_object_poll(_settings, obj):
    """Mesh Object만 Paint Target으로 선택하게 제한합니다."""
    return obj is not None and obj.type == "MESH"


def _update_point_size(settings, context):
    """PP Snapshot의 Point Scale만 독립적으로 갱신합니다."""

    from .point_scene import update_dataset_point_scale

    scene = getattr(settings, "id_data", None) or getattr(context, "scene", None)
    if scene is not None:
        update_dataset_point_scale(scene, float(settings.point_size))


def _update_camera_frustum_size(settings, context):
    """PP Snapshot의 Camera Frustum만 독립적으로 갱신합니다."""

    from .camera_display import update_camera_frustum_size_from_settings

    update_camera_frustum_size_from_settings(settings, context)


def _update_camera_axes(settings, context):
    """PP Snapshot의 Camera·Point Axis Root만 독립적으로 갱신합니다."""

    from .axis_roots import axis_matrix
    from .ownership import is_owned

    scene = getattr(settings, "id_data", None) or getattr(context, "scene", None)
    dataset_uuid = str(getattr(settings, "dataset_uuid", "") or "")
    if scene is None or not dataset_uuid:
        return
    matrix = axis_matrix(
        str(settings.camera_axis_preset),
        custom_forward=str(settings.camera_forward),
        custom_up=str(settings.camera_up),
    )
    for obj in tuple(scene.objects):
        if is_owned(obj, kind="CAMERA_AXIS_ROOT", dataset_uuid=dataset_uuid):
            obj.matrix_world = matrix


def _update_review_background(settings, context):
    """Camera Review 배경 표시와 Alpha 변경을 현재 카메라 슬롯에 전달합니다."""

    from .camera_review import update_camera_review_from_settings

    update_camera_review_from_settings(settings, context)


def _update_layer_adjustment(adjustment, context):
    """Layer 보정값 변경을 대응 Shader Node에 즉시 전달합니다."""
    scene = getattr(context, "scene", None) if context is not None else None
    if scene is None or not str(adjustment.adjustment_uuid):
        return
    try:
        from .layer_shader import update_adjustment

        update_adjustment(scene, str(adjustment.adjustment_uuid))
    except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
        settings = getattr(scene, "projection_paint", None)
        if settings is not None:
            settings.status = f"Layer 보정 갱신 실패: {exc}"
            settings.status_kind = "ERROR"


def _update_layer_visibility(layer, context):
    """Layer 눈 아이콘 변경을 대응 Shader 합성 연결에 즉시 반영합니다."""
    scene = getattr(context, "scene", None) if context is not None else None
    if scene is None or not str(layer.layer_uuid):
        return
    try:
        from .layer_shader import update_layer_visibility

        update_layer_visibility(scene, str(layer.layer_uuid))
    except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
        settings = getattr(scene, "projection_paint", None)
        if settings is not None:
            settings.status = f"Layer 가시성 갱신 실패: {exc}"
            settings.status_kind = "ERROR"


def _update_mask(mask, context):
    """Mask 눈 아이콘 변경을 UUID와 일치하는 Shader Factor에 전달합니다."""
    scene = getattr(context, "scene", None) if context is not None else None
    if scene is None or not str(mask.mask_uuid):
        return
    try:
        from .layer_shader import update_mask

        update_mask(scene, str(mask.mask_uuid))
    except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
        settings = getattr(scene, "projection_paint", None)
        if settings is not None:
            settings.status = f"Mask 갱신 실패: {exc}"
            settings.status_kind = "ERROR"


class PP_PG_mask(bpy.types.PropertyGroup):
    """Layer 또는 Adjustment에 하나만 속하는 Paint Mask 상태를 저장합니다."""

    mask_uuid: StringProperty(options={"HIDDEN"})
    ui_selected: BoolProperty(default=False, options={"HIDDEN"})
    image: PointerProperty(
        name="Mask Image",
        type=bpy.types.Image,
        options={"HIDDEN"},
    )
    enabled: BoolProperty(
        name="Enabled",
        description="이 Mask의 적용 여부를 저장합니다",
        default=True,
        update=_update_mask,
    )


class PP_PG_layer_adjustment(bpy.types.PropertyGroup):
    """Shader 색상 보정과 선택적인 자체 Mask 상태를 저장합니다."""

    adjustment_type: EnumProperty(
        name="Layer Item Type",
        items=(
            ("MASK", "Mask", "브러시로 칠할 Mask UI 항목입니다"),
            ("BRIGHTNESS", "Brightness", "밝기 보정 UI 항목입니다"),
            ("EXPOSURE", "Exposure", "노출 보정 UI 항목입니다"),
            ("HUE", "Legacy Hue", "이전 버전 Hue 항목입니다"),
            (
                "SATURATION",
                "Hue/Saturation",
                "색조와 채도를 함께 보정하는 UI 항목입니다",
            ),
        ),
    )
    adjustment_uuid: StringProperty(options={"HIDDEN"})
    ui_selected: BoolProperty(default=False, options={"HIDDEN"})
    image: PointerProperty(
        name="Legacy Mask Image",
        type=bpy.types.Image,
        options={"HIDDEN"},
    )
    mask: PointerProperty(type=PP_PG_mask)
    expanded: BoolProperty(
        name="Expanded",
        description="Adjustment의 Mask 자식 행을 펼쳐서 표시합니다",
        default=True,
    )
    enabled: BoolProperty(
        name="Enabled",
        description="이 Mask 또는 색상 보정의 적용 여부를 저장합니다",
        default=True,
        update=_update_layer_adjustment,
    )
    brightness: FloatProperty(
        name="Brightness",
        description="Brightness 보정 UI 값입니다",
        default=0.0,
        min=-100.0,
        max=100.0,
        precision=2,
        update=_update_layer_adjustment,
    )
    exposure: FloatProperty(
        name="Exposure",
        description="Exposure 보정 UI 값입니다",
        default=0.0,
        min=-10.0,
        max=10.0,
        precision=3,
        update=_update_layer_adjustment,
    )
    hue: FloatProperty(
        name="Hue",
        description="Hue 보정 UI 값입니다",
        default=0.5,
        min=0.0,
        max=1.0,
        precision=3,
        update=_update_layer_adjustment,
    )
    saturation: FloatProperty(
        name="Saturation",
        description="Saturation 보정 UI 값입니다",
        default=1.0,
        min=0.0,
        max=2.0,
        precision=3,
        update=_update_layer_adjustment,
    )


class PP_PG_layer(bpy.types.PropertyGroup):
    """독립 Paint Image와 Shader Graph를 가진 Paint Layer를 저장합니다."""

    name: StringProperty(
        name="Layer Name",
        description="Paint Layer 목록에 표시할 이름입니다",
        default="Layer",
    )
    layer_uuid: StringProperty(options={"HIDDEN"})
    layer_role: EnumProperty(
        name="Layer Role",
        items=(
            ("NORMAL", "Normal", "Composite 생성 전부터 존재한 일반 Layer입니다"),
            ("COMPOSITE", "Composite", "현재 합성 결과를 가진 작업 기준 Layer입니다"),
        ),
        default="NORMAL",
        options={"HIDDEN"},
    )
    composite_session_uuid: StringProperty(default="", options={"HIDDEN"})
    composite_generation: IntProperty(default=0, min=0, options={"HIDDEN"})
    composite_source_session_uuid: StringProperty(default="", options={"HIDDEN"})
    composite_bake_included: BoolProperty(default=False, options={"HIDDEN"})
    composite_saved_visible: BoolProperty(default=True, options={"HIDDEN"})
    composite_source_expanded: BoolProperty(default=True, options={"HIDDEN"})
    composite_has_baked_alpha: BoolProperty(default=False, options={"HIDDEN"})
    ui_selected: BoolProperty(default=False, options={"HIDDEN"})
    is_original_layer: BoolProperty(default=False, options={"HIDDEN"})
    image: PointerProperty(
        name="Layer Image",
        type=bpy.types.Image,
        options={"HIDDEN"},
    )
    visible: BoolProperty(
        name="Visible",
        description="이 Paint Layer를 최종 Base Color 합성에 표시합니다",
        default=True,
        update=_update_layer_visibility,
    )
    expanded: BoolProperty(
        name="Expanded",
        description="Layer의 Mask와 보정 자식 행을 펼쳐서 표시합니다",
        default=True,
    )
    mask: PointerProperty(type=PP_PG_mask)
    adjustments: CollectionProperty(type=PP_PG_layer_adjustment)
    active_adjustment_index: IntProperty(default=-1, min=-1)


class PP_PG_settings(bpy.types.PropertyGroup):
    """Projection Painting의 사용자 설정과 영구 상태를 저장합니다."""

    dataset_root: StringProperty(
        name="Dataset Root",
        description="선택하면 sparse/0과 images 폴더를 자동 탐색합니다",
        subtype="DIR_PATH",
    )
    sparse_directory: StringProperty(name="Sparse Directory", subtype="DIR_PATH")
    images_directory: StringProperty(name="Images Directory", subtype="DIR_PATH")
    input_format: EnumProperty(
        name="Input Format",
        description="COLMAP 입력 형식을 자동 감지하거나 명시적으로 선택합니다",
        items=(
            ("AUTO", "Auto", "완전한 BIN이 있으면 우선하고, 없으면 TXT를 사용합니다"),
            ("BINARY", "Binary", "cameras.bin, images.bin, points3D.bin을 사용합니다"),
            ("TEXT", "Text", "cameras.txt, images.txt, points3D.txt를 사용합니다"),
        ),
        default="AUTO",
    )
    point_size: FloatProperty(
        name="Point Scale",
        description="Import된 COLMAP Point3D 표시 반지름입니다",
        default=0.05,
        min=0.000001,
        soft_max=1.0,
        precision=4,
        update=_update_point_size,
    )
    camera_frustum_size: FloatProperty(
        name="Camera Frustum Size",
        description="PP Snapshot 카메라 프러스텀 표시 크기입니다",
        default=1.0,
        min=0.01,
        max=1000.0,
        soft_max=10.0,
        precision=3,
        subtype="DISTANCE",
        update=_update_camera_frustum_size,
    )
    camera_axis_preset: EnumProperty(
        name="Preset",
        description="원본 애플리케이션의 표시축 프리셋입니다",
        items=AXIS_PRESET_ITEMS,
        default="REALITY_CAPTURE",
        update=_update_camera_axes,
    )
    camera_forward: EnumProperty(
        name="Forward Axis",
        description="COLMAP 원본에서 전방으로 간주할 표시 전용 축입니다",
        items=AXIS_ITEMS,
        default="-Z",
        update=_update_camera_axes,
    )
    camera_up: EnumProperty(
        name="Up Axis",
        description="COLMAP 원본에서 위쪽으로 간주할 표시 전용 축입니다",
        items=AXIS_ITEMS,
        default="Y",
        update=_update_camera_axes,
    )
    frame_start: IntProperty(name="Frame Start", default=1, min=-1_048_574, max=1_048_574)
    review_background_alpha: FloatProperty(
        name="Background Alpha",
        description="현재 카메라 배경 이미지의 불투명도를 변경합니다",
        default=1.0,
        min=0.0,
        max=1.0,
        subtype="FACTOR",
        update=_update_review_background,
    )
    review_background_enabled: BoolProperty(
        name="Show Background Image",
        description="현재 카메라 배경 이미지 표시를 켜거나 끕니다",
        default=False,
        update=_update_review_background,
    )
    review_background_image: PointerProperty(
        name="Camera Review Image",
        type=bpy.types.Image,
        options={"HIDDEN"},
    )
    review_background_camera_data: PointerProperty(
        name="Camera Review Data",
        type=bpy.types.Camera,
        options={"HIDDEN"},
    )
    review_background_image_id: IntProperty(default=0, min=0, options={"HIDDEN"})
    review_background_slot_index: IntProperty(default=-1, min=-1, options={"HIDDEN"})
    paint_target: PointerProperty(
        name="Paint Target",
        type=bpy.types.Object,
        poll=_mesh_object_poll,
    )
    paint_material: PointerProperty(
        name="Paint Material",
        type=bpy.types.Material,
        options={"HIDDEN"},
    )
    overlay_image: PointerProperty(
        name="Paint Overlay",
        type=bpy.types.Image,
        options={"HIDDEN"},
    )
    active_paint_image: PointerProperty(
        name="Active Paint Image",
        type=bpy.types.Image,
        options={"HIDDEN"},
    )
    composite_state: EnumProperty(
        name="Composite State",
        items=(
            ("NONE", "None", "Composite 작업 레이어가 없습니다"),
            ("CREATING", "Creating", "Composite 베이크를 준비 중입니다"),
            ("RECOMPOSITING", "Recompositing", "기존 Composite를 평면화하고 있습니다"),
            ("ACTIVE", "Active", "Composite 작업 레이어가 활성 상태입니다"),
            ("RELEASING", "Releasing", "원본 Layer 체인을 복구 중입니다"),
            ("STALE", "Stale", "Composite 계약을 확인할 수 없습니다"),
        ),
        default="NONE",
        options={"HIDDEN"},
    )
    active_composite_session_uuid: StringProperty(default="", options={"HIDDEN"})
    composite_include_alpha: BoolProperty(
        name="Alpha 추가 베이크",
        description="다음 Composite 생성 또는 재생성에서 빈 영역과 Layer 투명도를 보존합니다",
        default=False,
    )
    composite_source_schema_version: IntProperty(default=0, min=0, options={"HIDDEN"})
    composite_layer_uuid: StringProperty(default="", options={"HIDDEN"})
    composite_saved_active_layer_uuid: StringProperty(default="", options={"HIDDEN"})
    original_layer_name: StringProperty(
        default="Original Layer",
        options={"HIDDEN"},
    )
    original_layer_visible: BoolProperty(
        name="Original Layer Visible",
        description="원본 Base Color를 최종 Layer 합성에 포함할지 저장합니다",
        default=True,
        options={"HIDDEN"},
    )
    original_layer_preview: BoolProperty(
        name="Original Layer Preview",
        description="원본 Base Color만 단독으로 표시하는 상태를 저장합니다",
        default=False,
        options={"HIDDEN"},
    )
    original_layer_schema_version: IntProperty(default=0, options={"HIDDEN"})
    original_source_image: PointerProperty(
        name="Original Source Image",
        type=bpy.types.Image,
        options={"HIDDEN"},
    )
    original_source_color: FloatVectorProperty(
        size=4,
        default=(0.8, 0.8, 0.8, 1.0),
        options={"HIDDEN"},
    )
    original_source_node_uuid: StringProperty(default="", options={"HIDDEN"})
    original_source_socket_identifier: StringProperty(
        default="",
        options={"HIDDEN"},
    )
    layers: CollectionProperty(type=PP_PG_layer)
    active_layer_index: IntProperty(default=-1, min=-1)
    layer_selection_anchor_uuid: StringProperty(default="", options={"HIDDEN"})
    active_mask_uuid: StringProperty(default="", options={"HIDDEN"})
    paint_mask_mode: BoolProperty(default=False, options={"HIDDEN"})
    layer_preview_uuid: StringProperty(default="", options={"HIDDEN"})
    mask_preview_adjustment_uuid: StringProperty(default="", options={"HIDDEN"})
    stencil_texture: PointerProperty(
        name="Stencil Texture",
        type=bpy.types.Texture,
        options={"HIDDEN"},
    )
    stencil_brush: PointerProperty(
        name="Stencil Brush",
        type=bpy.types.Brush,
        options={"HIDDEN"},
    )
    stencil_source_image: PointerProperty(
        name="Stencil Source",
        type=bpy.types.Image,
        options={"HIDDEN"},
    )
    original_canvas: PointerProperty(
        name="Original Paint Canvas",
        type=bpy.types.Image,
        options={"HIDDEN"},
    )
    original_active_object: PointerProperty(
        name="Original Active Object",
        type=bpy.types.Object,
        options={"HIDDEN"},
    )
    paint_workspace: PointerProperty(
        name="Paint Workspace",
        type=bpy.types.WorkSpace,
        options={"HIDDEN"},
    )
    dataset_view_layer_name: StringProperty(default="", options={"HIDDEN"})
    source_view_layer_name: StringProperty(default="", options={"HIDDEN"})
    paint_enabled: BoolProperty(default=False, options={"HIDDEN"})
    manual_stencil_disabled: BoolProperty(default=False, options={"HIDDEN"})
    stencil_outside_camera_view: BoolProperty(default=False, options={"HIDDEN"})
    stencil_state: EnumProperty(
        name="Stencil State",
        items=(
            ("DISCONNECTED", "Disconnected", "연결된 Draw Brush가 없습니다"),
            ("CONNECTED", "Connected", "현재 Draw Brush에 Camera Stencil이 연결됐습니다"),
            ("CAMERA_SUSPENDED", "Camera Suspended", "Camera View 밖에서 연결을 일시 중단했습니다"),
            ("MASK_SUSPENDED", "Mask Suspended", "Projection Paint Mask 편집 중입니다"),
            ("BRUSH_CHANGED", "Brush Changed", "활성 Brush가 연결 Brush와 다릅니다"),
            ("MANUAL_DISABLED", "Manual Disabled", "사용자가 Stencil 연결을 해제했습니다"),
            ("ERROR", "Error", "Stencil 연결 계약이 손상됐습니다"),
        ),
        default="DISCONNECTED",
        options={"HIDDEN", "SKIP_SAVE"},
    )
    stencil_reason: StringProperty(default="", options={"HIDDEN", "SKIP_SAVE"})
    stencil_frame_corners_json: StringProperty(default="", options={"HIDDEN"})
    paint_snapshot_json: StringProperty(default="{}", options={"HIDDEN"})
    paint_mesh_uuid: StringProperty(default="", options={"HIDDEN"})
    paint_uv_name: StringProperty(default="", options={"HIDDEN"})
    paint_material_slot_index: IntProperty(default=0, min=0, options={"HIDDEN"})
    paint_material_slot_link: StringProperty(default="", options={"HIDDEN"})
    active_image_id: IntProperty(default=0, min=0, options={"HIDDEN"})
    state_schema_version: IntProperty(default=0, min=0, options={"HIDDEN"})
    original_render_width: IntProperty(default=0, min=0, options={"HIDDEN"})
    original_render_height: IntProperty(default=0, min=0, options={"HIDDEN"})
    applied_render_width: IntProperty(default=0, min=0, options={"HIDDEN"})
    applied_render_height: IntProperty(default=0, min=0, options={"HIDDEN"})
    render_resolution_overridden: BoolProperty(default=False, options={"HIDDEN"})
    overlay_resolution: EnumProperty(
        name="Resolution",
        description="기존 Base Texture가 없을 때 생성할 정사각 Paint Overlay 해상도입니다",
        items=(
            ("RES_1K", "1K", "1024 × 1024"),
            ("RES_2K", "2K", "2048 × 2048"),
            ("RES_4K", "4K", "4096 × 4096"),
            ("RES_8K", "8K", "8192 × 8192"),
        ),
        default="RES_2K",
    )
    use_base_resolution: BoolProperty(
        name="Use Base Resolution",
        description="Source Texture 해상도를 사용하고 끄면 선택한 고정 해상도를 사용합니다",
        default=True,
    )
    status: StringProperty(name="Status", default="준비")
    status_kind: EnumProperty(
        name="Status Kind",
        items=(
            ("READY", "Ready", ""),
            ("RUNNING", "Running", ""),
            ("SUCCESS", "Success", ""),
            ("WARNING", "Warning", ""),
            ("ERROR", "Error", ""),
        ),
        default="READY",
        options={"HIDDEN"},
    )
    paint_state: EnumProperty(
        name="Paint State",
        items=(
            ("IDLE", "Idle", ""),
            ("ENTERING", "Entering", ""),
            ("ACTIVE", "Active", ""),
            ("FROZEN", "Frozen", ""),
            ("MANUAL_DISABLED", "Manual Disabled", ""),
            ("STALE", "Stale", ""),
            ("SUSPENDING", "Suspending", ""),
            ("EXITING", "Exiting", ""),
            ("ERROR", "Error", ""),
        ),
        default="IDLE",
        options={"HIDDEN"},
    )
    durability_state: EnumProperty(
        name="Overlay",
        items=(
            ("EMPTY", "Empty", ""),
            ("DURABLE", "Durable", ""),
            ("DIRTY", "Dirty", ""),
            ("DATA_AT_RISK", "Data At Risk", ""),
        ),
        default="EMPTY",
        options={"HIDDEN"},
    )
    current_image_id: StringProperty(name="IMAGE_ID", options={"HIDDEN", "SKIP_SAVE"})
    current_image_name: StringProperty(name="Image", options={"HIDDEN", "SKIP_SAVE"})
    dataset_uuid: StringProperty(options={"HIDDEN"})
    dataset_revision: IntProperty(default=0, min=0, options={"HIDDEN"})
    display_revision: IntProperty(default=0, min=0, options={"HIDDEN"})
    source_fingerprint: StringProperty(options={"HIDDEN"})
    snapshot_uuid: StringProperty(options={"HIDDEN"})
    snapshot_revision: IntProperty(default=0, min=0, options={"HIDDEN"})
    source_modifier_dataset_uuid: StringProperty(options={"HIDDEN"})
    source_modifier_revision: IntProperty(default=0, min=0, options={"HIDDEN"})
    source_modifier_fingerprint: StringProperty(options={"HIDDEN"})
    source_content_fingerprint: StringProperty(options={"HIDDEN"})
    snapshot_camera_count: IntProperty(default=0, min=0, options={"HIDDEN"})
    snapshot_image_count: IntProperty(default=0, min=0, options={"HIDDEN"})
    snapshot_point_count: IntProperty(default=0, min=0, options={"HIDDEN"})
    snapshot_created_at: StringProperty(options={"HIDDEN"})
    snapshot_state: EnumProperty(
        name="Snapshot State",
        items=(
            ("NONE", "None", ""),
            ("UPDATING", "Updating", ""),
            ("READY", "Ready", ""),
            ("STALE", "Stale", ""),
            ("ERROR", "Error", ""),
        ),
        default="NONE",
        options={"HIDDEN"},
    )
    snapshot_schema_version: IntProperty(default=0, min=0, options={"HIDDEN"})
    dataset_manifest_json: StringProperty(default="{}", options={"HIDDEN"})
    image_path_manifest_json: StringProperty(default="{}", options={"HIDDEN"})
    marker_manifest_json: StringProperty(default="[]", options={"HIDDEN"})
    overlay_registry_json: StringProperty(default="[]", options={"HIDDEN"})
    recovery_available: BoolProperty(default=False, options={"HIDDEN"})
    recovery_path: StringProperty(options={"HIDDEN"})
    session_epoch: IntProperty(default=0, min=0, options={"HIDDEN", "SKIP_SAVE"})


CLASSES = (PP_PG_mask, PP_PG_layer_adjustment, PP_PG_layer, PP_PG_settings)
