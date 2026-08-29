"""COLMAP 원본 좌표를 보존한 채 Blender 표시 축만 변환합니다."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType

import bpy
from bpy_extras.io_utils import axis_conversion

from .ownership import DATASET_UUID_KEY, OWNER_KEY, OWNER_TOKEN, RECORD_KIND_KEY


AXIS_ITEMS = (
    ("X", "X", "양의 X축"),
    ("-X", "-X", "음의 X축"),
    ("Y", "Y", "양의 Y축"),
    ("-Y", "-Y", "음의 Y축"),
    ("Z", "Z", "양의 Z축"),
    ("-Z", "-Z", "음의 Z축"),
)


@dataclass(frozen=True, slots=True)
class AxisPreset:
    """Display Axis 프리셋의 UI 정보와 실제 축을 함께 보관합니다."""

    identifier: str
    label: str
    description: str
    forward_axis: str | None
    up_axis: str | None


DEFAULT_AXIS_PRESET = "REALITY_CAPTURE"
BLENDER_AXIS_PRESET = "BLENDER"
CUSTOM_AXIS_PRESET = "CUSTOM"

AXIS_PRESETS = (
    AxisPreset(
        DEFAULT_AXIS_PRESET,
        "RealityCapture (RC)",
        "RealityCapture 좌표를 Blender 표시축에 맞춥니다",
        "Z",
        "-Y",
    ),
    AxisPreset(
        BLENDER_AXIS_PRESET,
        "Blender",
        "Blender의 Y Forward, Z Up 좌표를 그대로 표시합니다",
        "Y",
        "Z",
    ),
    AxisPreset(
        "MAYA",
        "Maya",
        "Maya 기본 Y Up 좌표를 Blender 표시축에 맞춥니다",
        "-Z",
        "Y",
    ),
    AxisPreset(
        "HOUDINI",
        "Houdini",
        "Houdini 기본 Y Up 좌표를 Blender 표시축에 맞춥니다",
        "-Z",
        "Y",
    ),
    AxisPreset(
        CUSTOM_AXIS_PRESET,
        "Custom",
        "Forward Axis와 Up Axis를 직접 설정합니다",
        None,
        None,
    ),
)

AXIS_PRESET_ITEMS = tuple(
    (preset.identifier, preset.label, preset.description)
    for preset in AXIS_PRESETS
)
_AXIS_PRESET_BY_ID = MappingProxyType(
    {preset.identifier: preset for preset in AXIS_PRESETS}
)
_DEFAULT_PRESET = _AXIS_PRESET_BY_ID[DEFAULT_AXIS_PRESET]
if _DEFAULT_PRESET.forward_axis is None or _DEFAULT_PRESET.up_axis is None:
    raise RuntimeError("기본 Display Axis 프리셋에는 고정 축이 필요합니다.")
DEFAULT_FORWARD_AXIS = _DEFAULT_PRESET.forward_axis
DEFAULT_UP_AXIS = _DEFAULT_PRESET.up_axis
_VALID_AXES = frozenset(item[0] for item in AXIS_ITEMS)
_axis_update_in_progress = False


def axis_preset_items() -> tuple[tuple[str, str, str], ...]:
    """Blender EnumProperty가 사용할 불변 프리셋 항목을 반환합니다."""

    return AXIS_PRESET_ITEMS


def axes_for_preset(preset_identifier: str) -> tuple[str, str] | None:
    """프리셋의 고정 축을 반환하며 Custom은 고정값이 없음을 나타냅니다."""

    try:
        preset = _AXIS_PRESET_BY_ID[preset_identifier]
    except KeyError as exc:
        raise ValueError(
            f"지원하지 않는 Display Axis 프리셋입니다: {preset_identifier}"
        ) from exc
    if preset.forward_axis is None or preset.up_axis is None:
        return None
    return preset.forward_axis, preset.up_axis


def axis_summary(forward_axis: str, up_axis: str) -> str:
    """현재 적용된 Forward와 Up 축을 UI 요약 문자열로 만듭니다."""

    return f"Forward: {forward_axis} / Up: {up_axis}"


def infer_legacy_axis_preset(forward_axis: str, up_axis: str) -> str:
    """구형 파일의 축 조합에서 의미가 명확한 프리셋만 추론합니다."""

    axes = (forward_axis, up_axis)
    if axes == axes_for_preset(BLENDER_AXIS_PRESET):
        return BLENDER_AXIS_PRESET
    if axes == axes_for_preset(DEFAULT_AXIS_PRESET):
        return DEFAULT_AXIS_PRESET
    return CUSTOM_AXIS_PRESET


def display_axis_matrix(forward_axis: str, up_axis: str):
    """선택한 source 축을 Blender의 Y Forward, Z Up 표시축으로 변환합니다."""
    if forward_axis.lstrip("-") == up_axis.lstrip("-"):
        raise ValueError("Forward Axis와 Up Axis는 같은 축을 사용할 수 없습니다.")
    return axis_conversion(
        from_forward=forward_axis,
        from_up=up_axis,
        to_forward="Y",
        to_up="Z",
    ).to_4x4()


def _fallback_axis(excluded_axis: str) -> str:
    """겹치는 축을 피할 결정적인 양의 대체 축을 선택합니다."""

    for candidate in ("Z", "Y", "X"):
        if candidate != excluded_axis.lstrip("-"):
            return candidate
    raise RuntimeError("유효한 대체 축을 결정하지 못했습니다.")


def _managed_axis_roots(dataset_uuid: str | None):
    """지정 데이터셋에 속한 관리 Axis Root를 순회합니다."""

    for obj in bpy.data.objects:
        if (
            obj.get(OWNER_KEY) != OWNER_TOKEN
            or obj.get(RECORD_KIND_KEY) != "AXIS_ROOT"
        ):
            continue
        if dataset_uuid and obj.get(DATASET_UUID_KEY) != dataset_uuid:
            continue
        yield obj


def _redraw_viewports(context) -> None:
    """표시축 변경을 모든 3D Viewport에 즉시 반영합니다."""

    if context is None:
        return
    for window in context.window_manager.windows:
        for area in window.screen.areas:
            if area.type == "VIEW_3D":
                area.tag_redraw()


def _apply_display_axis(settings, context) -> None:
    """현재 저장된 축을 관리 Axis Root와 Viewport에 한 번 적용합니다."""

    matrix = display_axis_matrix(settings.axis_forward, settings.axis_up)
    dataset_uuid = settings.dataset_uuid or None
    if dataset_uuid is not None:
        for axis_root in _managed_axis_roots(dataset_uuid):
            axis_root.matrix_world = matrix
            axis_root["colmap_display_forward_axis"] = settings.axis_forward
            axis_root["colmap_display_up_axis"] = settings.axis_up
            axis_root.update_tag(refresh={"OBJECT"})
    _redraw_viewports(context)


def update_display_axis_preset(settings, context) -> None:
    """선택한 프리셋의 축을 저장하고 표시 변환을 한 번 갱신합니다."""

    global _axis_update_in_progress
    if _axis_update_in_progress:
        return

    preset_axes = axes_for_preset(settings.axis_preset)
    if preset_axes is None:
        _redraw_viewports(context)
        return

    _axis_update_in_progress = True
    try:
        settings.axis_forward, settings.axis_up = preset_axes
        _apply_display_axis(settings, context)
    finally:
        _axis_update_in_progress = False


def update_display_axis(settings, context, changed_axis: str) -> None:
    """축 조합을 보정하고 관리 Axis Root에 표시 변환을 즉시 적용합니다."""
    global _axis_update_in_progress
    if _axis_update_in_progress:
        return

    _axis_update_in_progress = True
    try:
        if settings.axis_forward.lstrip("-") == settings.axis_up.lstrip("-"):
            if changed_axis == "FORWARD":
                settings.axis_up = _fallback_axis(settings.axis_forward)
            else:
                settings.axis_forward = _fallback_axis(settings.axis_up)

        preset_axes = axes_for_preset(settings.axis_preset)
        current_axes = (settings.axis_forward, settings.axis_up)
        if preset_axes is not None and current_axes != preset_axes:
            settings.axis_preset = CUSTOM_AXIS_PRESET

        _apply_display_axis(settings, context)
    finally:
        _axis_update_in_progress = False


def _legacy_saved_axes(settings) -> tuple[str, str] | None:
    """구형 Scene 속성이나 Axis Root에서 마지막 표시축을 복원합니다."""

    stored_keys = set(settings.keys())
    if {"axis_forward", "axis_up"} <= stored_keys:
        axes = (
            str(settings.axis_forward),
            str(settings.axis_up),
        )
        if axes[0] in _VALID_AXES and axes[1] in _VALID_AXES:
            return axes

    dataset_uuid = str(settings.dataset_uuid or "")
    if dataset_uuid:
        for axis_root in _managed_axis_roots(dataset_uuid):
            forward_axis = str(
                axis_root.get("colmap_display_forward_axis", "")
            )
            up_axis = str(axis_root.get("colmap_display_up_axis", ""))
            if forward_axis in _VALID_AXES and up_axis in _VALID_AXES:
                return forward_axis, up_axis

    if dataset_uuid:
        return axes_for_preset(BLENDER_AXIS_PRESET)
    return None


def migrate_legacy_axis_preset(settings, context=None) -> bool:
    """프리셋 속성이 없던 `.blend`의 기존 표시 방향을 보존합니다."""

    global _axis_update_in_progress
    if "axis_preset" in settings.keys():
        return False

    legacy_axes = _legacy_saved_axes(settings)
    if legacy_axes is None:
        return False
    display_axis_matrix(*legacy_axes)

    _axis_update_in_progress = True
    try:
        settings.axis_forward, settings.axis_up = legacy_axes
        settings.axis_preset = infer_legacy_axis_preset(*legacy_axes)
        _apply_display_axis(settings, context)
    finally:
        _axis_update_in_progress = False
    return True


def migrate_loaded_axis_presets(context=None) -> int:
    """열려 있는 모든 Scene의 구형 Display Axis 설정을 한 번 이전합니다."""

    migrated_count = 0
    for scene in bpy.data.scenes:
        settings = getattr(scene, "colmap_import", None)
        if settings is not None and migrate_legacy_axis_preset(settings, context):
            migrated_count += 1
    return migrated_count


def _migrate_loaded_axis_presets_after_register():
    """Blender 등록 제한이 끝난 다음 현재 Scene의 구형 축을 이전합니다."""

    migrate_loaded_axis_presets(bpy.context)
    return None


def schedule_axis_preset_migration() -> None:
    """애드온 활성화 뒤 Display Axis 마이그레이션 Timer를 하나 예약합니다."""

    if not bpy.app.timers.is_registered(
        _migrate_loaded_axis_presets_after_register
    ):
        bpy.app.timers.register(
            _migrate_loaded_axis_presets_after_register,
            first_interval=0.0,
        )


def cancel_axis_preset_migration() -> None:
    """애드온 해제 시 실행 전인 Display Axis 마이그레이션을 취소합니다."""

    if bpy.app.timers.is_registered(
        _migrate_loaded_axis_presets_after_register
    ):
        bpy.app.timers.unregister(
            _migrate_loaded_axis_presets_after_register
        )
