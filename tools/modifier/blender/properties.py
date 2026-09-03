"""COLMAP Import 패널의 대기 경로와 마지막 성공 metadata를 정의합니다."""

from __future__ import annotations

from pathlib import Path

import bpy
from bpy.props import (
    BoolProperty,
    CollectionProperty,
    EnumProperty,
    FloatProperty,
    IntProperty,
    StringProperty,
)

from ..core.edit_replay import history_hash
from ..core.import_types import (
    CAMERA_FRUSTUM_SIZE_MAX,
    CAMERA_FRUSTUM_SIZE_MIN,
    DEFAULT_CAMERA_FRUSTUM_SIZE,
)
from .axis_display import (
    AXIS_ITEMS,
    DEFAULT_AXIS_PRESET,
    DEFAULT_FORWARD_AXIS,
    DEFAULT_UP_AXIS,
    axis_preset_items,
    update_display_axis,
    update_display_axis_preset,
)
from .camera_display import update_camera_frustum_size_from_settings
from .dataset_adapter import synchronize_display_from_modifier
from .point_display import update_point_scale_from_settings


def _set_latest_status(settings, prefix: str, message: str, icon: str) -> None:
    """여러 기능의 상태 중 가장 마지막에 갱신된 메시지 하나를 기록합니다."""
    text = str(message).strip()
    if not text:
        return
    settings.latest_status_message = f"{prefix}: {text}"
    settings.latest_status_icon = icon


def _update_import_status(settings, context):
    del context
    icon = "ERROR" if settings.status_kind == "ERROR" else "INFO"
    _set_latest_status(settings, "Import", settings.status, icon)


def _update_point_selection_status(settings, context):
    del context
    _set_latest_status(
        settings,
        "Edit",
        settings.point_selection_status,
        "NONE",
    )


def _update_review_status(settings, context):
    del context
    _set_latest_status(settings, "Review", settings.review_status, "INFO")


def _update_export_status(settings, context):
    del context
    _set_latest_status(settings, "Export", settings.export_status, "INFO")


def _update_warning_summary(settings, context):
    del context
    warnings = tuple(
        line.strip()
        for line in settings.warning_summary.splitlines()
        if line.strip()
    )
    if warnings:
        _set_latest_status(settings, "Warning", warnings[-1], "ERROR")


def _update_dataset_root_paths(settings, context):
    """Dataset Root 입력 즉시 표준 COLMAP 하위 경로를 UI에 반영합니다."""
    del context
    raw_root = settings.dataset_root.strip()
    if not raw_root:
        settings.status = "Dataset Root가 비어 있어 수동 경로를 사용합니다."
        settings.status_kind = "READY"
        return

    try:
        root = Path(bpy.path.abspath(raw_root)).expanduser().resolve()
    except (OSError, RuntimeError, ValueError) as exc:
        settings.sparse_directory = ""
        settings.images_directory = ""
        settings.status = f"Dataset Root 경로 오류: {exc}"
        settings.status_kind = "ERROR"
        return

    sparse = root / "sparse" / "0"
    images = root / "images"
    settings.sparse_directory = str(sparse.resolve()) if sparse.is_dir() else ""
    settings.images_directory = str(images.resolve()) if images.is_dir() else ""

    if settings.sparse_directory and settings.images_directory:
        settings.status = "표준 sparse/0 및 images 폴더를 찾았습니다."
        settings.status_kind = "DISCOVERED"
    else:
        settings.status = "Import 실행 시 Dataset Root 내부를 다시 탐색합니다."
        settings.status_kind = "READY"


def _update_point_scale(settings, context):
    """Modifier Point Scale을 Scene 요소와 공용 Dataset에 함께 반영합니다."""

    update_point_scale_from_settings(settings, context)
    synchronize_display_from_modifier(settings, context, "point_size")


def _update_camera_frustum_size(settings, context):
    """Camera Frustum Size 변경을 관리 Camera Data에 즉시 반영합니다."""

    update_camera_frustum_size_from_settings(settings, context)
    synchronize_display_from_modifier(settings, context, "camera_frustum_size")


def _update_review_background(settings, context):
    from .camera_review_session import update_review_background_from_settings

    update_review_background_from_settings(settings, context)


def _update_axis_forward(settings, context):
    """Forward Axis 변경을 표시 변환에 반영합니다."""

    update_display_axis(settings, context, "FORWARD")
    synchronize_display_from_modifier(
        settings,
        context,
        "axis_preset",
        "axis_forward",
        "axis_up",
    )


def _update_axis_up(settings, context):
    """Up Axis 변경을 표시 변환에 반영합니다."""

    update_display_axis(settings, context, "UP")
    synchronize_display_from_modifier(
        settings,
        context,
        "axis_preset",
        "axis_forward",
        "axis_up",
    )


def _update_axis_preset(settings, context):
    """Display Axis 프리셋을 실제 Forward와 Up 축에 적용합니다."""

    update_display_axis_preset(settings, context)
    synchronize_display_from_modifier(
        settings,
        context,
        "axis_preset",
        "axis_forward",
        "axis_up",
    )


class COLMAP_PG_status_display_line(bpy.types.PropertyGroup):
    """Status UIList에 표시할 한 줄짜리 파생 표시 데이터입니다."""

    text: StringProperty(options={"HIDDEN", "SKIP_SAVE"})
    icon: StringProperty(default="NONE", options={"HIDDEN", "SKIP_SAVE"})


class COLMAP_PG_import_settings(bpy.types.PropertyGroup):
    dataset_root: StringProperty(
        name="Dataset Root",
        description="선택하면 sparse/0과 images 폴더를 자동 탐색합니다",
        subtype="DIR_PATH",
        update=_update_dataset_root_paths,
    )
    sparse_directory: StringProperty(
        name="Sparse Directory",
        description="cameras, images, points3D 모델 파일이 있는 폴더",
        subtype="DIR_PATH",
    )
    images_directory: StringProperty(
        name="Images Directory",
        description="COLMAP Image 이름의 기준이 되는 이미지 폴더",
        subtype="DIR_PATH",
    )
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
        description="Import된 COLMAP Point3D 표시 반지름을 실시간으로 변경합니다",
        default=0.05,
        min=0.000001,
        soft_max=1.0,
        precision=4,
        update=_update_point_scale,
    )
    camera_frustum_size: FloatProperty(
        name="Camera Frustum Size",
        description="Import한 COLMAP 카메라 프러스텀의 Viewport 표시 크기를 변경합니다",
        default=DEFAULT_CAMERA_FRUSTUM_SIZE,
        min=CAMERA_FRUSTUM_SIZE_MIN,
        max=CAMERA_FRUSTUM_SIZE_MAX,
        soft_max=10.0,
        precision=3,
        subtype="DISTANCE",
        update=_update_camera_frustum_size,
    )
    axis_preset: EnumProperty(
        name="Preset",
        description="원본 애플리케이션의 표시축 프리셋",
        items=axis_preset_items(),
        default=DEFAULT_AXIS_PRESET,
        update=_update_axis_preset,
    )
    axis_forward: EnumProperty(
        name="Forward Axis",
        description="COLMAP 원본에서 전방으로 간주할 표시 전용 축",
        items=AXIS_ITEMS,
        default=DEFAULT_FORWARD_AXIS,
        update=_update_axis_forward,
    )
    axis_up: EnumProperty(
        name="Up Axis",
        description="COLMAP 원본에서 위쪽으로 간주할 표시 전용 축",
        items=AXIS_ITEMS,
        default=DEFAULT_UP_AXIS,
        update=_update_axis_up,
    )
    status: StringProperty(
        name="Status",
        default="Ready",
        update=_update_import_status,
    )
    status_kind: EnumProperty(
        name="Status Kind",
        items=(
            ("READY", "Ready", ""),
            ("DISCOVERED", "Discovered", ""),
            ("IMPORTING", "Importing", ""),
            ("SUCCESS", "Success", ""),
            ("ERROR", "Error", ""),
        ),
        default="READY",
        options={"HIDDEN"},
        update=_update_import_status,
    )
    latest_status_message: StringProperty(
        options={"HIDDEN", "SKIP_SAVE"},
    )
    latest_status_icon: StringProperty(
        default="NONE",
        options={"HIDDEN", "SKIP_SAVE"},
    )
    status_display_lines: CollectionProperty(
        type=COLMAP_PG_status_display_line,
        options={"HIDDEN", "SKIP_SAVE"},
    )
    status_display_index: IntProperty(
        default=-1,
        min=-1,
        max=-1,
        options={"HIDDEN", "SKIP_SAVE"},
    )

    # 아래 값은 대기 경로가 아니라 마지막으로 성공한 Import의 metadata입니다.
    active_dataset_root: StringProperty(options={"HIDDEN"})
    active_sparse_root: StringProperty(options={"HIDDEN"})
    active_model_directory: StringProperty(options={"HIDDEN"})
    active_images_directory: StringProperty(options={"HIDDEN"})
    active_input_format: StringProperty(options={"HIDDEN"})
    active_layout: StringProperty(options={"HIDDEN"})
    source_fingerprint: StringProperty(options={"HIDDEN"})
    dataset_uuid: StringProperty(options={"HIDDEN"})
    # 독립 Runtime 식별자는 UI 권위가 아닌 저장·재연결용 Mirror입니다.
    domain_uuid: StringProperty(options={"HIDDEN"})
    domain_generation: IntProperty(default=0, min=0, options={"HIDDEN"})
    domain_saved_generation: IntProperty(default=0, min=0, options={"HIDDEN"})
    domain_persistence_dirty: BoolProperty(default=False, options={"HIDDEN"})
    domain_restore_error: StringProperty(default="", options={"HIDDEN"})
    schema_version: StringProperty(options={"HIDDEN"})
    selected_files_json: StringProperty(options={"HIDDEN"})
    camera_count: StringProperty(options={"HIDDEN"})
    image_count: StringProperty(options={"HIDDEN"})
    point3d_count: StringProperty(options={"HIDDEN"})
    warning_summary: StringProperty(
        options={"HIDDEN"},
        update=_update_warning_summary,
    )
    edit_history_json: StringProperty(default="[]", options={"HIDDEN"})
    edit_revision: IntProperty(default=0, min=0, options={"HIDDEN"})
    history_hash: StringProperty(default=history_hash(()), options={"HIDDEN"})
    last_operation_id: StringProperty(options={"HIDDEN"})
    selected_point_count: IntProperty(default=0, min=0, options={"HIDDEN"})
    observing_camera_count: IntProperty(default=0, min=0, options={"HIDDEN"})
    derived_orphan_count: IntProperty(default=0, min=0, options={"HIDDEN"})
    point_edit_mode_active: BoolProperty(default=False, options={"HIDDEN", "SKIP_SAVE"})
    camera_edit_mode_active: BoolProperty(
        default=False,
        options={"HIDDEN", "SKIP_SAVE"},
    )
    camera_target_count: IntProperty(
        default=0,
        min=0,
        options={"HIDDEN", "SKIP_SAVE"},
    )
    remove_target: EnumProperty(
        items=(
            ("NONE", "None", ""),
            ("POINTS", "Selected Points", ""),
            ("IMAGES", "Selected Cameras + Points", ""),
        ),
        default="NONE",
        options={"HIDDEN", "SKIP_SAVE"},
    )
    point_selection_status: StringProperty(
        default="Point3D를 선택하십시오.",
        update=_update_point_selection_status,
    )
    review_current_index: IntProperty(
        name="Current Review Index",
        default=-1,
        min=-1,
        options={"HIDDEN", "SKIP_SAVE"},
    )
    review_current_image_id: StringProperty(
        name="Current IMAGE_ID",
        default="",
        options={"HIDDEN", "SKIP_SAVE"},
    )
    review_total_count: IntProperty(
        name="Review Camera Count",
        default=0,
        min=0,
        options={"HIDDEN", "SKIP_SAVE"},
    )
    review_camera_model: StringProperty(
        name="Camera Model",
        default="",
        options={"HIDDEN", "SKIP_SAVE"},
    )
    review_image_status: StringProperty(
        name="Image Status",
        default="",
        options={"HIDDEN", "SKIP_SAVE"},
    )
    review_exact_projection: BoolProperty(
        name="Exact Projection",
        default=False,
        options={"HIDDEN", "SKIP_SAVE"},
    )
    review_status: StringProperty(
        name="Review Status",
        default="Camera Review 준비 중",
        options={"HIDDEN", "SKIP_SAVE"},
        update=_update_review_status,
    )
    review_pending_filter_json: StringProperty(
        name="Pending Camera Filters",
        default="{}",
        options={"HIDDEN"},
    )
    review_pending_filter_order_json: StringProperty(
        name="Pending Camera Filter Order",
        default="[]",
        options={"HIDDEN"},
    )
    review_pending_camera_count: IntProperty(
        name="Pending Camera Count",
        default=0,
        min=0,
        options={"HIDDEN", "SKIP_SAVE"},
    )
    review_pending_point_count: IntProperty(
        name="Pending Point3D Count",
        default=0,
        min=0,
        options={"HIDDEN", "SKIP_SAVE"},
    )
    review_pending_filter_status: StringProperty(
        name="Pending Camera Filter Status",
        default="EMPTY",
        options={"HIDDEN", "SKIP_SAVE"},
    )
    review_frame_start: IntProperty(
        name="Frame Start",
        default=1,
    )
    review_background_alpha: FloatProperty(
        name="Background Alpha",
        description="Camera Review 배경 이미지의 불투명도를 실시간으로 변경합니다",
        default=1.0,
        min=0.0,
        max=1.0,
        subtype="FACTOR",
        update=_update_review_background,
    )
    review_background_enabled: BoolProperty(
        name="Show Background Image",
        description="Camera Review 배경 이미지 표시를 실시간으로 켜거나 끕니다",
        default=True,
        update=_update_review_background,
    )
    review_marker_manifest_json: StringProperty(
        name="Review Marker Manifest",
        default="[]",
        options={"HIDDEN"},
    )
    review_vram_window: StringProperty(
        name="VRAM Window",
        default="",
        options={"HIDDEN", "SKIP_SAVE"},
    )
    review_restore_available: BoolProperty(
        name="Restore Available",
        default=False,
        options={"HIDDEN", "SKIP_SAVE"},
    )
    export_backup_enabled: BoolProperty(
        name="Backup",
        description=(
            "원본 sparse 파일을 형제 폴더 sparse_backup에 단순 복사한 뒤 "
            "현재 편집 결과로 덮어씁니다"
        ),
        default=True,
    )
    export_output_format: EnumProperty(
        name="Export Format",
        description="COLMAP sparse 모델을 기록할 출력 형식입니다",
        items=(
            (
                "SOURCE",
                "Same as Source",
                "Import한 원본과 같은 TXT 또는 BIN 형식을 사용합니다",
            ),
            (
                "TEXT",
                "Text (.txt)",
                "사람이 읽을 수 있는 cameras.txt, images.txt, points3D.txt를 기록합니다",
            ),
            (
                "BINARY",
                "Binary (.bin)",
                "빠르게 읽을 수 있는 cameras.bin, images.bin, points3D.bin을 기록합니다",
            ),
        ),
        default="SOURCE",
    )
    export_running: BoolProperty(
        name="Export Running",
        default=False,
        options={"HIDDEN", "SKIP_SAVE"},
    )
    export_status: StringProperty(
        name="Export Status",
        default="Export 준비",
        options={"HIDDEN", "SKIP_SAVE"},
        update=_update_export_status,
    )


CLASSES = (
    COLMAP_PG_status_display_line,
    COLMAP_PG_import_settings,
)
