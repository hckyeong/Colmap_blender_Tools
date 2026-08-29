"""Camera와 Model의 독립 Forward·Up 축 Root를 생성합니다."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

import bpy
from bpy_extras.io_utils import axis_conversion

from .ownership import MODEL_AXIS_SIGNATURE_KEY, tag_owned


VALID_AXES = frozenset({"X", "-X", "Y", "-Y", "Z", "-Z"})


@dataclass(frozen=True, slots=True)
class AxisPreset:
    """축 프리셋의 식별자와 실제 Forward·Up 값을 보관합니다."""

    identifier: str
    forward: str | None
    up: str | None


AXIS_PRESETS = MappingProxyType(
    {
        "COLMAP": AxisPreset("COLMAP", "-Z", "Y"),
        "REALITY_CAPTURE": AxisPreset("REALITY_CAPTURE", "Z", "-Y"),
        "BLENDER": AxisPreset("BLENDER", "Y", "Z"),
        "MAYA": AxisPreset("MAYA", "-Z", "Y"),
        "HOUDINI": AxisPreset("HOUDINI", "-Z", "Y"),
        "CUSTOM": AxisPreset("CUSTOM", None, None),
    }
)


def normalize_axis(axis: str) -> str:
    """UI 축 식별자를 Blender Axis 문자열로 정규화합니다."""

    aliases = {
        "POS_X": "X",
        "NEG_X": "-X",
        "POS_Y": "Y",
        "NEG_Y": "-Y",
        "POS_Z": "Z",
        "NEG_Z": "-Z",
    }
    value = aliases.get(str(axis), str(axis))
    if value not in VALID_AXES:
        raise ValueError(f"지원하지 않는 축입니다: {axis}")
    return value


def resolve_axes(
    preset: str,
    *,
    custom_forward: str = "-Z",
    custom_up: str = "Y",
) -> tuple[str, str]:
    """프리셋 또는 Custom 입력에서 유효한 Forward·Up을 결정합니다."""

    try:
        selected = AXIS_PRESETS[str(preset)]
    except KeyError as exc:
        raise ValueError(f"지원하지 않는 축 프리셋입니다: {preset}") from exc
    forward = selected.forward or normalize_axis(custom_forward)
    up = selected.up or normalize_axis(custom_up)
    if forward.lstrip("-") == up.lstrip("-"):
        raise ValueError("Forward와 Up은 같은 축 계열을 사용할 수 없습니다.")
    return forward, up


def axis_matrix(
    preset: str,
    *,
    custom_forward: str = "-Z",
    custom_up: str = "Y",
):
    """선택한 Source 축을 Blender Y Forward·Z Up으로 변환합니다."""

    forward, up = resolve_axes(
        preset,
        custom_forward=custom_forward,
        custom_up=custom_up,
    )
    return axis_conversion(
        from_forward=forward,
        from_up=up,
        to_forward="Y",
        to_up="Z",
    ).to_4x4()


def axis_signature(
    preset: str,
    *,
    custom_forward: str = "-Z",
    custom_up: str = "Y",
) -> str:
    """중복 축 적용을 판정할 결정적 Signature를 반환합니다."""

    forward, up = resolve_axes(
        preset,
        custom_forward=custom_forward,
        custom_up=custom_up,
    )
    return f"{preset}:{forward}:{up}"


def create_axis_root(
    name: str,
    *,
    kind: str,
    dataset_uuid: str,
    preset: str,
    custom_forward: str = "-Z",
    custom_up: str = "Y",
) -> Any:
    """연결 전 Axis Root Object를 직접 Data API로 생성합니다."""

    root = bpy.data.objects.new(str(name), None)
    tag_owned(root, kind, dataset_uuid=dataset_uuid)
    root.empty_display_type = "ARROWS"
    root.empty_display_size = 1.0
    root.matrix_world = axis_matrix(
        preset,
        custom_forward=custom_forward,
        custom_up=custom_up,
    )
    root[MODEL_AXIS_SIGNATURE_KEY] = axis_signature(
        preset,
        custom_forward=custom_forward,
        custom_up=custom_up,
    )
    return root
