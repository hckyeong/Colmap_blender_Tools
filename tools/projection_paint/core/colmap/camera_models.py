"""COLMAP Camera Model과 정확한 Pinhole 변환 계약을 정의합니다."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType

from .errors import ColmapError


@dataclass(frozen=True, slots=True)
class CameraModel:
    """COLMAP Camera Model ID와 Parameter 수를 보관합니다."""

    model_id: int
    name: str
    num_params: int
    exact_blender_projection: bool = False


_MODELS = (
    CameraModel(0, "SIMPLE_PINHOLE", 3, True),
    CameraModel(1, "PINHOLE", 4, True),
    CameraModel(2, "SIMPLE_RADIAL", 4),
    CameraModel(3, "RADIAL", 5),
    CameraModel(4, "OPENCV", 8),
    CameraModel(5, "OPENCV_FISHEYE", 8),
    CameraModel(6, "FULL_OPENCV", 12),
    CameraModel(7, "FOV", 5),
    CameraModel(8, "SIMPLE_RADIAL_FISHEYE", 4),
    CameraModel(9, "RADIAL_FISHEYE", 5),
    CameraModel(10, "THIN_PRISM_FISHEYE", 12),
    CameraModel(11, "RAD_TAN_THIN_PRISM_FISHEYE", 16),
    CameraModel(12, "SIMPLE_DIVISION", 4),
    CameraModel(13, "DIVISION", 5),
    CameraModel(14, "SIMPLE_FISHEYE", 3),
    CameraModel(15, "FISHEYE", 4),
    CameraModel(16, "EUCM", 6),
    CameraModel(17, "EQUIRECTANGULAR", 2),
)

BY_ID = MappingProxyType({model.model_id: model for model in _MODELS})
BY_NAME = MappingProxyType({model.name: model for model in _MODELS})
SUPPORTED_PAINT_MODELS = frozenset(
    model.name for model in _MODELS if model.exact_blender_projection
)


def model_by_id(model_id: int) -> CameraModel:
    """Camera Model ID에 대응하는 고정 정의를 반환합니다."""
    try:
        return BY_ID[model_id]
    except KeyError as exc:
        raise ColmapError(
            f"지원하지 않는 COLMAP Camera Model ID입니다: {model_id}"
        ) from exc


def model_by_name(model_name: str) -> CameraModel:
    """Camera Model 이름에 대응하는 고정 정의를 반환합니다."""
    try:
        return BY_NAME[model_name]
    except KeyError as exc:
        raise ColmapError(
            f"지원하지 않는 COLMAP Camera Model입니다: {model_name}"
        ) from exc


def pinhole_parameters(
    model_name: str,
    params: tuple[float, ...],
) -> tuple[float, float, float, float]:
    """정확히 지원하는 Camera Model을 fx, fy, cx, cy로 변환합니다."""
    if model_name == "SIMPLE_PINHOLE":
        focal, cx, cy = params
        return focal, focal, cx, cy
    if model_name == "PINHOLE":
        fx, fy, cx, cy = params
        return fx, fy, cx, cy
    raise ColmapError(
        f"정확한 Pinhole 변환을 지원하지 않는 Camera Model입니다: {model_name}"
    )
