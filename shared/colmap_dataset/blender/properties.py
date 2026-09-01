"""Scene.colmap_dataset에 저장할 공용 COLMAP Dataset PropertyGroup입니다."""

from __future__ import annotations

import bpy
from bpy.props import EnumProperty, FloatProperty, IntProperty, StringProperty

from ..contracts import AXIS_ITEMS, AXIS_PRESET_ITEMS, DATASET_SCHEMA_VERSION


_AXIS_PRESET_AXES = {
    "REALITY_CAPTURE": ("Z", "-Y"),
    "BLENDER": ("Y", "Z"),
    "MAYA": ("-Z", "Y"),
    "HOUDINI": ("-Z", "Y"),
}
_AXIS_UPDATE_IN_PROGRESS = False


def _notify_axis_preset(settings, context) -> None:
    """공용 표시축 프리셋의 고정 축을 한 Revision으로 소비 Adapter에 알립니다."""

    global _AXIS_UPDATE_IN_PROGRESS
    if _AXIS_UPDATE_IN_PROGRESS:
        return
    from .registry import notify_display_properties_changed

    changed_fields = {"axis_preset"}
    axes = _AXIS_PRESET_AXES.get(str(settings.axis_preset))
    _AXIS_UPDATE_IN_PROGRESS = True
    try:
        if axes is not None:
            if settings.axis_forward != axes[0]:
                settings.axis_forward = axes[0]
                changed_fields.add("axis_forward")
            if settings.axis_up != axes[1]:
                settings.axis_up = axes[1]
                changed_fields.add("axis_up")
    finally:
        _AXIS_UPDATE_IN_PROGRESS = False
    notify_display_properties_changed(settings, context, frozenset(changed_fields))


def _notify_axis_forward(settings, context) -> None:
    """공용 Forward Axis 변경을 등록된 소비 Adapter에 알립니다."""

    if _AXIS_UPDATE_IN_PROGRESS:
        return
    from .registry import notify_display_property_changed

    notify_display_property_changed(settings, context, "axis_forward")


def _notify_axis_up(settings, context) -> None:
    """공용 Up Axis 변경을 등록된 소비 Adapter에 알립니다."""

    if _AXIS_UPDATE_IN_PROGRESS:
        return
    from .registry import notify_display_property_changed

    notify_display_property_changed(settings, context, "axis_up")


def _notify_point_size(settings, context) -> None:
    """공용 Point Scale 변경을 등록된 소비 Adapter에 알립니다."""

    from .registry import notify_display_property_changed

    notify_display_property_changed(settings, context, "point_size")


def _notify_camera_frustum_size(settings, context) -> None:
    """공용 Camera Frustum Size 변경을 등록된 소비 Adapter에 알립니다."""

    from .registry import notify_display_property_changed

    notify_display_property_changed(settings, context, "camera_frustum_size")


class COLMAP_PG_dataset(bpy.types.PropertyGroup):
    """한 Scene의 활성 공용 COLMAP Dataset 영구 상태입니다."""

    dataset_uuid: StringProperty(options={"HIDDEN"})
    dataset_revision: IntProperty(default=0, min=0, options={"HIDDEN"})
    display_revision: IntProperty(default=0, min=0, options={"HIDDEN"})
    source_fingerprint: StringProperty(options={"HIDDEN"})
    dataset_root: StringProperty(subtype="DIR_PATH", options={"HIDDEN"})
    sparse_root: StringProperty(subtype="DIR_PATH", options={"HIDDEN"})
    model_directory: StringProperty(subtype="DIR_PATH", options={"HIDDEN"})
    images_directory: StringProperty(subtype="DIR_PATH", options={"HIDDEN"})
    input_format: StringProperty(options={"HIDDEN"})
    layout: StringProperty(options={"HIDDEN"})
    selected_files_json: StringProperty(default="[]", options={"HIDDEN"})
    image_path_manifest_json: StringProperty(default="[]", options={"HIDDEN"})
    camera_count: IntProperty(default=0, min=0, options={"HIDDEN"})
    image_count: IntProperty(default=0, min=0, options={"HIDDEN"})
    point3d_count: IntProperty(default=0, min=0, options={"HIDDEN"})
    schema_version: IntProperty(
        default=DATASET_SCHEMA_VERSION,
        min=DATASET_SCHEMA_VERSION,
        max=DATASET_SCHEMA_VERSION,
        options={"HIDDEN"},
    )
    source_schema_version: IntProperty(default=0, min=0, options={"HIDDEN"})
    axis_preset: EnumProperty(
        name="Preset",
        items=AXIS_PRESET_ITEMS,
        default="REALITY_CAPTURE",
        update=_notify_axis_preset,
    )
    axis_forward: EnumProperty(
        name="Forward Axis",
        items=AXIS_ITEMS,
        default="Z",
        update=_notify_axis_forward,
    )
    axis_up: EnumProperty(
        name="Up Axis",
        items=AXIS_ITEMS,
        default="-Y",
        update=_notify_axis_up,
    )
    point_size: FloatProperty(
        name="Point Scale",
        default=0.05,
        min=0.000001,
        soft_max=1.0,
        precision=4,
        update=_notify_point_size,
    )
    camera_frustum_size: FloatProperty(
        name="Camera Frustum Size",
        default=1.0,
        min=0.01,
        max=1000.0,
        soft_max=10.0,
        precision=3,
        subtype="DISTANCE",
        update=_notify_camera_frustum_size,
    )


CLASSES = (COLMAP_PG_dataset,)


__all__ = ["CLASSES", "COLMAP_PG_dataset"]
