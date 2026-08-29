"""COLMAP 원본 좌표를 보존하는 모델별 표시 축 변환을 정의합니다."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from types import MappingProxyType
from typing import Literal

import numpy as np
from bpy_extras.io_utils import axis_conversion


AxisChange = Literal["FORWARD", "UP"]

AXIS_ITEMS = (
    ("X", "X", "양의 X축"),
    ("-X", "-X", "음의 X축"),
    ("Y", "Y", "양의 Y축"),
    ("-Y", "-Y", "음의 Y축"),
    ("Z", "Z", "양의 Z축"),
    ("-Z", "-Z", "음의 Z축"),
)
"""Blender EnumProperty와 축 검증에 공통으로 사용할 축 항목입니다."""


@dataclass(frozen=True, slots=True)
class DisplayAxisPreset:
    """표시 축 프리셋의 식별자·UI 문구·고정 축을 함께 보관합니다."""

    identifier: str
    label: str
    description: str
    forward_axis: str | None
    up_axis: str | None


DEFAULT_AXIS_PRESET = "REALITY_CAPTURE"
"""기준 Blender COLMAP Importer와 동일한 기본 표시 축 프리셋입니다."""

BLENDER_AXIS_PRESET = "BLENDER"
"""기존 COLMAP Align 무변환 표시와 같은 Blender 축 프리셋입니다."""

CUSTOM_AXIS_PRESET = "CUSTOM"
"""사용자가 Forward와 Up을 직접 지정하는 프리셋 식별자입니다."""


AXIS_PRESETS = (
    DisplayAxisPreset(
        DEFAULT_AXIS_PRESET,
        "RealityCapture (RC)",
        "RealityCapture 좌표를 Blender 표시축에 맞춥니다",
        "Z",
        "-Y",
    ),
    DisplayAxisPreset(
        BLENDER_AXIS_PRESET,
        "Blender",
        "Blender의 Y Forward, Z Up 좌표를 그대로 표시합니다",
        "Y",
        "Z",
    ),
    DisplayAxisPreset(
        "MAYA",
        "Maya",
        "Maya 기본 Y Up 좌표를 Blender 표시축에 맞춥니다",
        "-Z",
        "Y",
    ),
    DisplayAxisPreset(
        "HOUDINI",
        "Houdini",
        "Houdini 기본 Y Up 좌표를 Blender 표시축에 맞충니다",
        "-Z",
        "Y",
    ),
    DisplayAxisPreset(
        CUSTOM_AXIS_PRESET,
        "Custom",
        "Forward Axis와 Up Axis를 직접 설정합니다",
        None,
        None,
    ),
)
"""기준 Importer와 같은 표시 축 프리셋 정의입니다."""


AXIS_PRESET_ITEMS = tuple(
    (preset.identifier, preset.label, preset.description)
    for preset in AXIS_PRESETS
)
"""Blender EnumProperty에 전달할 불변 프리셋 항목입니다."""

_AXIS_PRESETS_BY_ID = MappingProxyType(
    {preset.identifier: preset for preset in AXIS_PRESETS}
)
_VALID_AXES = frozenset(item[0] for item in AXIS_ITEMS)
_DEFAULT_PRESET = _AXIS_PRESETS_BY_ID[DEFAULT_AXIS_PRESET]
if _DEFAULT_PRESET.forward_axis is None or _DEFAULT_PRESET.up_axis is None:
    raise RuntimeError("기본 표시 축 프리셋에는 고정 축이 필요합니다.")
DEFAULT_FORWARD_AXIS = _DEFAULT_PRESET.forward_axis
DEFAULT_UP_AXIS = _DEFAULT_PRESET.up_axis


def axis_preset_items() -> tuple[tuple[str, str, str], ...]:
    """Blender RNA가 사용할 표시 축 프리셋 목록을 반환합니다."""

    return AXIS_PRESET_ITEMS


def axis_preset_for_id(preset_id: str) -> DisplayAxisPreset:
    """식별자에 해당하는 표시 축 프리셋을 검증해 반환합니다."""

    try:
        return _AXIS_PRESETS_BY_ID[str(preset_id)]
    except KeyError as error:
        raise ValueError(f"지원하지 않는 Display Axis 프리셋입니다: {preset_id}") from error


def axes_for_preset(preset_id: str) -> tuple[str, str] | None:
    """고정 프리셋의 Forward·Up 축을 반환하고 Custom은 None을 반환합니다."""

    preset = axis_preset_for_id(preset_id)
    if preset.forward_axis is None or preset.up_axis is None:
        return None
    return preset.forward_axis, preset.up_axis


def axis_pair_summary(forward_axis: str, up_axis: str) -> str:
    """File Browser에 표시할 Forward·Up 한 줄 요약을 만듭니다."""

    return f"Forward: {forward_axis} / Up: {up_axis}"


def validate_axis_pair(forward_axis: str, up_axis: str) -> tuple[str, str]:
    """지원 축과 서로 다른 기준축 조합인지 검증해 정규 문자열로 반환합니다."""

    forward = str(forward_axis)
    up = str(up_axis)
    if forward not in _VALID_AXES:
        raise ValueError(f"지원하지 않는 Forward Axis입니다: {forward_axis}")
    if up not in _VALID_AXES:
        raise ValueError(f"지원하지 않는 Up Axis입니다: {up_axis}")
    if forward.lstrip("-") == up.lstrip("-"):
        raise ValueError(
            "Forward Axis와 Up Axis는 같은 기준축을 사용할 수 없습니다: "
            f"{forward} / {up}"
        )
    return forward, up


def _fallback_axis(excluded_axis: str) -> str:
    """중복 축을 보정할 때 사용할 결정적인 양의 대체 축을 고릅니다."""

    excluded = excluded_axis.lstrip("-")
    for candidate in ("Z", "Y", "X"):
        if candidate != excluded:
            return candidate
    raise RuntimeError("서로 다른 대체 축을 결정하지 못했습니다.")


def ensure_distinct_axis_pair(
    forward_axis: str,
    up_axis: str,
    changed_axis: AxisChange,
) -> tuple[str, str]:
    """Custom UI의 같은 기준축 입력을 변경하지 않은 축만 결정적으로 보정합니다."""

    if changed_axis not in {"FORWARD", "UP"}:
        raise ValueError(f"지원하지 않는 축 변경 종류입니다: {changed_axis}")
    forward = str(forward_axis)
    up = str(up_axis)
    if forward not in _VALID_AXES or up not in _VALID_AXES:
        return validate_axis_pair(forward, up)
    if forward.lstrip("-") != up.lstrip("-"):
        return forward, up
    if changed_axis == "FORWARD":
        up = _fallback_axis(forward)
    else:
        forward = _fallback_axis(up)
    return validate_axis_pair(forward, up)


def _freeze_matrix(value: object, field_name: str) -> np.ndarray:
    """4×4 float64 행렬을 검증하고 읽기 전용 연속 배열로 고정합니다."""

    matrix = np.ascontiguousarray(value, dtype=np.float64)
    if matrix.shape != (4, 4) or not np.isfinite(matrix).all():
        raise ValueError(f"{field_name}은 유한한 4×4 float64 행렬이어야 합니다.")
    matrix.setflags(write=False)
    return matrix


def _transform_fingerprint(
    preset_id: str,
    forward_axis: str,
    up_axis: str,
    matrix_float64: np.ndarray,
) -> str:
    """축 식별자와 행렬 값에서 안정적인 짧은 변환 지문을 생성합니다."""

    digest = hashlib.blake2b(digest_size=16)
    digest.update(str(preset_id).encode("utf-8"))
    digest.update(b"\0")
    digest.update(str(forward_axis).encode("utf-8"))
    digest.update(b"\0")
    digest.update(str(up_axis).encode("utf-8"))
    digest.update(b"\0")
    digest.update(np.ascontiguousarray(matrix_float64, dtype=np.dtype("<f8")).tobytes())
    return digest.hexdigest()


@dataclass(frozen=True, slots=True)
class ModelDisplayTransform:
    """한 COLMAP 모델의 원본 좌표와 분리된 불변 표시 축 변환입니다."""

    preset_id: str
    forward_axis: str
    up_axis: str
    matrix_float64: np.ndarray
    inverse_float64: np.ndarray
    fingerprint: str = ""

    def __post_init__(self) -> None:
        """축·행렬·역행렬 관계를 검증해 안전한 모델 표시 변환으로 고정합니다."""

        preset = axis_preset_for_id(self.preset_id)
        forward, up = validate_axis_pair(self.forward_axis, self.up_axis)
        fixed_axes = axes_for_preset(preset.identifier)
        if fixed_axes is not None and (forward, up) != fixed_axes:
            raise ValueError(
                f"{preset.label} 프리셋의 고정 축은 "
                f"{fixed_axes[0]} / {fixed_axes[1]}입니다."
            )
        matrix = _freeze_matrix(self.matrix_float64, "표시 축 행렬")
        inverse = _freeze_matrix(self.inverse_float64, "표시 축 역행렬")
        if not np.allclose(matrix @ inverse, np.identity(4), rtol=0.0, atol=1.0e-12):
            raise ValueError("표시 축 행렬과 역행렬의 곱이 Identity가 아닙니다.")
        fingerprint = _transform_fingerprint(preset.identifier, forward, up, matrix)
        if self.fingerprint and self.fingerprint != fingerprint:
            raise ValueError("표시 축 변환 fingerprint가 행렬 값과 일치하지 않습니다.")
        object.__setattr__(self, "preset_id", preset.identifier)
        object.__setattr__(self, "forward_axis", forward)
        object.__setattr__(self, "up_axis", up)
        object.__setattr__(self, "matrix_float64", matrix)
        object.__setattr__(self, "inverse_float64", inverse)
        object.__setattr__(self, "fingerprint", fingerprint)


def create_model_display_transform(
    preset_id: str,
    forward_axis: str,
    up_axis: str,
) -> ModelDisplayTransform:
    """선택 snapshot으로 source→Blender 표시 축 변환과 역행렬을 생성합니다."""

    preset = axis_preset_for_id(preset_id)
    forward, up = validate_axis_pair(forward_axis, up_axis)
    fixed_axes = axes_for_preset(preset.identifier)
    if fixed_axes is not None and (forward, up) != fixed_axes:
        raise ValueError(
            f"{preset.label} 프리셋의 축과 전달된 값이 일치하지 않습니다: "
            f"{forward} / {up}"
        )
    matrix = np.asarray(
        axis_conversion(
            from_forward=forward,
            from_up=up,
            to_forward="Y",
            to_up="Z",
        ).to_4x4(),
        dtype=np.float64,
    )
    inverse = np.linalg.inv(matrix)
    return ModelDisplayTransform(
        preset_id=preset.identifier,
        forward_axis=forward,
        up_axis=up,
        matrix_float64=matrix,
        inverse_float64=inverse,
    )


def identity_display_transform() -> ModelDisplayTransform:
    """명시적인 테스트 fixture 호환에만 사용할 Blender Identity 표시 변환을 만듭니다."""

    return create_model_display_transform(BLENDER_AXIS_PRESET, "Y", "Z")


def display_position(
    transform: ModelDisplayTransform,
    source_position: object,
) -> np.ndarray:
    """원본 한 점을 복사한 Display Local 좌표로 변환합니다."""

    source = np.asarray(source_position, dtype=np.float64)
    if source.shape != (3,) or not np.isfinite(source).all():
        raise ValueError("원본 점 좌표는 유한한 길이 3 배열이어야 합니다.")
    return display_positions(transform, source[np.newaxis, :])[0]


def display_positions(
    transform: ModelDisplayTransform,
    source_positions: object,
) -> np.ndarray:
    """원본 점 배열을 변경하지 않고 새 Display Local 좌표 배열을 반환합니다."""

    source = np.asarray(source_positions, dtype=np.float64)
    if source.ndim != 2 or source.shape[1] != 3 or not np.isfinite(source).all():
        raise ValueError("원본 점 좌표는 유한한 N×3 배열이어야 합니다.")
    homogeneous = np.ones((source.shape[0], 4), dtype=np.float64)
    homogeneous[:, :3] = source
    displayed_homogeneous = homogeneous @ transform.matrix_float64.T
    weights = displayed_homogeneous[:, 3]
    if np.any(np.abs(weights) <= np.finfo(np.float64).eps):
        raise ValueError("표시 축 변환 결과의 동차 좌표가 0입니다.")
    displayed = displayed_homogeneous[:, :3] / weights[:, np.newaxis]
    if not np.isfinite(displayed).all():
        raise ValueError("표시 축 변환 결과에 유한하지 않은 좌표가 있습니다.")
    return displayed


def display_bounds(
    transform: ModelDisplayTransform,
    source_bounds_min: object,
    source_bounds_max: object,
) -> tuple[np.ndarray, np.ndarray]:
    """원본 AABB 여덟 모서리를 변환해 올바른 Display Local AABB를 계산합니다."""

    minimum = np.asarray(source_bounds_min, dtype=np.float64)
    maximum = np.asarray(source_bounds_max, dtype=np.float64)
    if (
        minimum.shape != (3,)
        or maximum.shape != (3,)
        or not np.isfinite(minimum).all()
        or not np.isfinite(maximum).all()
        or np.any(minimum > maximum)
    ):
        raise ValueError("원본 경계는 유한한 최소·최대 길이 3 배열이어야 합니다.")
    corners = np.asarray(
        [
            (x, y, z)
            for x in (minimum[0], maximum[0])
            for y in (minimum[1], maximum[1])
            for z in (minimum[2], maximum[2])
        ],
        dtype=np.float64,
    )
    displayed_corners = display_positions(transform, corners)
    return displayed_corners.min(axis=0), displayed_corners.max(axis=0)


def axis_summary(transform: ModelDisplayTransform) -> str:
    """진행 오버레이와 완료 팝업에 표시할 프리셋·축 요약을 만듭니다."""

    preset = axis_preset_for_id(transform.preset_id)
    return f"{preset.label} ({transform.forward_axis} / {transform.up_axis})"


__all__ = (
    "AXIS_ITEMS",
    "AXIS_PRESET_ITEMS",
    "AXIS_PRESETS",
    "BLENDER_AXIS_PRESET",
    "CUSTOM_AXIS_PRESET",
    "DEFAULT_AXIS_PRESET",
    "DEFAULT_FORWARD_AXIS",
    "DEFAULT_UP_AXIS",
    "DisplayAxisPreset",
    "ModelDisplayTransform",
    "axis_pair_summary",
    "axis_preset_for_id",
    "axis_preset_items",
    "axis_summary",
    "axes_for_preset",
    "create_model_display_transform",
    "display_bounds",
    "display_position",
    "display_positions",
    "ensure_distinct_axis_pair",
    "identity_display_transform",
    "validate_axis_pair",
)
