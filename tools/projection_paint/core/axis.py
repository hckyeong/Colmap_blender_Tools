"""Blender 없이 Camera와 Model 표시축 프리셋 행렬을 계산합니다."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType


@dataclass(frozen=True, slots=True)
class AxisPreset:
    """Forward와 Up 축 한 쌍의 프리셋을 보관합니다."""

    identifier: str
    label: str
    forward_axis: str | None
    up_axis: str | None


AXIS_PRESETS = (
    AxisPreset("REALITY_CAPTURE", "RealityCapture", "Z", "-Y"),
    AxisPreset("BLENDER", "Blender", "Y", "Z"),
    AxisPreset("MAYA", "Maya", "-Z", "Y"),
    AxisPreset("HOUDINI", "Houdini", "-Z", "Y"),
    AxisPreset("CUSTOM", "Custom", None, None),
)
_PRESET_BY_ID = MappingProxyType(
    {preset.identifier: preset for preset in AXIS_PRESETS}
)
_AXIS_VECTOR = MappingProxyType(
    {
        "X": (1.0, 0.0, 0.0),
        "-X": (-1.0, 0.0, 0.0),
        "Y": (0.0, 1.0, 0.0),
        "-Y": (0.0, -1.0, 0.0),
        "Z": (0.0, 0.0, 1.0),
        "-Z": (0.0, 0.0, -1.0),
    }
)


def axes_for_preset(
    preset_identifier: str,
    *,
    custom_forward: str | None = None,
    custom_up: str | None = None,
) -> tuple[str, str]:
    """프리셋 또는 Custom 입력에서 검증된 Forward와 Up을 반환합니다."""
    try:
        preset = _PRESET_BY_ID[preset_identifier.upper()]
    except KeyError as exc:
        raise ValueError(f"지원하지 않는 축 프리셋입니다: {preset_identifier}") from exc
    if preset.identifier == "CUSTOM":
        if custom_forward is None or custom_up is None:
            raise ValueError("Custom 프리셋에는 Forward와 Up이 모두 필요합니다.")
        forward, up = custom_forward, custom_up
    else:
        forward, up = preset.forward_axis, preset.up_axis
    if forward not in _AXIS_VECTOR or up not in _AXIS_VECTOR:
        raise ValueError(f"지원하지 않는 축 조합입니다: {forward}, {up}")
    if forward.lstrip("-") == up.lstrip("-"):
        raise ValueError("Forward와 Up은 같은 축 계열을 사용할 수 없습니다.")
    return forward, up


def _cross(
    left: tuple[float, float, float],
    right: tuple[float, float, float],
) -> tuple[float, float, float]:
    """두 3차원 Vector의 외적을 반환합니다."""
    return (
        left[1] * right[2] - left[2] * right[1],
        left[2] * right[0] - left[0] * right[2],
        left[0] * right[1] - left[1] * right[0],
    )


def _basis(forward_axis: str, up_axis: str) -> tuple[tuple[float, ...], ...]:
    """Forward와 Up에서 Right·Forward·Up 열 Basis를 계산합니다."""
    forward = _AXIS_VECTOR[forward_axis]
    up = _AXIS_VECTOR[up_axis]
    right = _cross(forward, up)
    return (
        (right[0], forward[0], up[0]),
        (right[1], forward[1], up[1]),
        (right[2], forward[2], up[2]),
    )


def _transpose(matrix: tuple[tuple[float, ...], ...]) -> tuple[tuple[float, ...], ...]:
    """3×3 Matrix의 전치행렬을 반환합니다."""
    return tuple(tuple(matrix[column][row] for column in range(3)) for row in range(3))


def _multiply(
    left: tuple[tuple[float, ...], ...],
    right: tuple[tuple[float, ...], ...],
) -> tuple[tuple[float, ...], ...]:
    """두 3×3 Matrix의 곱을 반환합니다."""
    return tuple(
        tuple(
            sum(left[row][index] * right[index][column] for index in range(3))
            for column in range(3)
        )
        for row in range(3)
    )


def axis_matrix(
    forward_axis: str,
    up_axis: str,
    *,
    to_forward: str = "Y",
    to_up: str = "Z",
) -> tuple[tuple[float, float, float, float], ...]:
    """Source 축을 대상 Forward·Up 축으로 바꾸는 4×4 행렬을 반환합니다."""
    for forward, up in ((forward_axis, up_axis), (to_forward, to_up)):
        if forward not in _AXIS_VECTOR or up not in _AXIS_VECTOR:
            raise ValueError(f"지원하지 않는 축 조합입니다: {forward}, {up}")
        if forward.lstrip("-") == up.lstrip("-"):
            raise ValueError("Forward와 Up은 같은 축 계열을 사용할 수 없습니다.")
    source_basis = _basis(forward_axis, up_axis)
    target_basis = _basis(to_forward, to_up)
    matrix = _multiply(target_basis, _transpose(source_basis))
    return (
        (matrix[0][0], matrix[0][1], matrix[0][2], 0.0),
        (matrix[1][0], matrix[1][1], matrix[1][2], 0.0),
        (matrix[2][0], matrix[2][1], matrix[2][2], 0.0),
        (0.0, 0.0, 0.0, 1.0),
    )
