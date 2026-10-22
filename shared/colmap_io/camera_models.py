"""COLMAP 4.0.x sparse model의 공식 카메라 모델 표를 제공합니다."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from .errors import ColmapParseError


@dataclass(frozen=True, slots=True)
class CameraModel:
    """COLMAP 카메라 모델의 정수 ID·이름·파라미터 수를 정의합니다."""

    model_id: int
    name: str
    parameter_count: int


# COLMAP CameraModelId 열거형과 동일한 4.0.x 모델 순서입니다.
CAMERA_MODELS: Final[tuple[CameraModel, ...]] = (
    CameraModel(0, "SIMPLE_PINHOLE", 3),
    CameraModel(1, "PINHOLE", 4),
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
CAMERA_MODELS_BY_ID: Final[dict[int, CameraModel]] = {
    model.model_id: model for model in CAMERA_MODELS
}
CAMERA_MODELS_BY_NAME: Final[dict[str, CameraModel]] = {
    model.name: model for model in CAMERA_MODELS
}


def camera_model_from_id(model_id: int) -> CameraModel:
    """binary 파일의 카메라 모델 ID를 검증된 모델 정의로 변환합니다."""
    model = CAMERA_MODELS_BY_ID.get(model_id)
    if model is None:
        raise ColmapParseError(f"지원하지 않는 COLMAP 카메라 모델 ID입니다: {model_id}")
    return model


def camera_model_from_name(model_name: str) -> CameraModel:
    """text 파일의 카메라 모델 이름을 검증된 모델 정의로 변환합니다."""
    model = CAMERA_MODELS_BY_NAME.get(model_name)
    if model is None:
        raise ColmapParseError(f"지원하지 않는 COLMAP 카메라 모델 이름입니다: {model_name}")
    return model
