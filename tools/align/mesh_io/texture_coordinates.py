"""모델 하나의 UV 값과 성분 수를 손실 없이 작은 읽기 전용 배열로 보관합니다."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True, slots=True)
class MeshTextureCoordinates:
    """Blender 코너 UV는 float32 두 성분, 기존 OBJ UV는 float64 세 성분으로 보관합니다."""

    coordinates: np.ndarray
    component_counts: np.ndarray

    def __post_init__(self) -> None:
        """입력 표현을 검증하고 상수 성분 수의 작은 기반 배열을 그대로 유지합니다."""
        values = np.asarray(self.coordinates)
        if values.ndim != 2 or (values.dtype, values.shape[1]) not in (
            (np.dtype(np.float32), 2), (np.dtype(np.float64), 3),
        ):
            raise ValueError("UV는 float32 두 성분 또는 float64 세 성분이어야 합니다.")
        values = np.ascontiguousarray(values)
        for start in range(0, len(values), 262_144):
            if not np.isfinite(values[start:start + 262_144]).all():
                raise ValueError("OBJ UV 좌표에 유한하지 않은 값이 있습니다.")
        counts = np.asarray(self.component_counts, dtype=np.uint8)
        if counts.shape != (len(values),):
            raise ValueError("OBJ UV 좌표와 성분 수의 개수가 일치하지 않습니다.")
        if counts.size:
            inspected = counts[:1] if counts.strides == (0,) else counts
            if np.any(inspected < 1) or np.any(inspected > values.shape[1]):
                raise ValueError("OBJ UV 성분 수가 저장 표현의 범위를 벗어났습니다.")
        if counts.strides != (0,):
            counts = np.ascontiguousarray(counts)
        values.setflags(write=False)
        counts.setflags(write=False)
        object.__setattr__(self, "coordinates", values)
        object.__setattr__(self, "component_counts", counts)

    @classmethod
    def from_blender_corners(cls, coordinates: np.ndarray) -> MeshTextureCoordinates:
        """Blender에서 읽은 float32 코너 UV와 모델 전용 상수 성분 수를 보관합니다."""
        values = np.asarray(coordinates)
        if values.dtype != np.float32 or values.ndim != 2 or values.shape[1] != 2:
            raise ValueError("Blender 코너 UV는 원본 float32 두 성분이어야 합니다.")
        count = np.array([2], dtype=np.uint8)
        count.setflags(write=False)
        return cls(values, np.broadcast_to(count, (len(values),)))

    @classmethod
    def from_legacy(cls, coordinates: object = None, component_counts: object = None) -> MeshTextureCoordinates:
        """기존 OBJ 호출의 배정밀도 UV와 1~3성분 정보를 손실 없이 유지합니다."""
        values = np.empty((0, 3), dtype=np.float64) if coordinates is None else np.ascontiguousarray(coordinates, dtype=np.float64)
        counts = np.empty((0,), dtype=np.uint8) if component_counts is None else np.ascontiguousarray(component_counts, dtype=np.uint8)
        return cls(values, counts)

    def legacy_float64(self) -> np.ndarray:
        """기존 API에서 명시적으로 요청한 경우에만 세 성분 배정밀도 사본을 반환합니다."""
        if self.coordinates.dtype == np.float64:
            return self.coordinates
        values = np.zeros((len(self.coordinates), 3), dtype=np.float64)
        values[:, :2] = self.coordinates
        values.setflags(write=False)
        return values
