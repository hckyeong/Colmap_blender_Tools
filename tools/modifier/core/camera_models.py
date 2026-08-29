"""COLMAP 카메라 모델 ID와 파라미터 수의 고정 레지스트리입니다."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CameraModel:
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

BY_ID = {model.model_id: model for model in _MODELS}
BY_NAME = {model.name: model for model in _MODELS}


def model_by_id(model_id: int) -> CameraModel:
    try:
        return BY_ID[model_id]
    except KeyError as exc:
        raise ValueError(f"지원하지 않는 COLMAP 카메라 model ID입니다: {model_id}") from exc


def model_by_name(name: str) -> CameraModel:
    try:
        return BY_NAME[name]
    except KeyError as exc:
        raise ValueError(f"지원하지 않는 COLMAP 카메라 model입니다: {name}") from exc


def pinhole_parameters(model_name: str, params: tuple[float, ...]) -> tuple[float, float, float, float]:
    if model_name == "SIMPLE_PINHOLE":
        focal, cx, cy = params
        return focal, focal, cx, cy
    if model_name == "PINHOLE":
        fx, fy, cx, cy = params
        return fx, fy, cx, cy
    raise ValueError(f"정확한 pinhole 변환 대상이 아닙니다: {model_name}")
