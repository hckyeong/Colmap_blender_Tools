"""COLMAP sparse model을 Blender 밖에서도 다룰 수 있는 불변 데이터 타입입니다."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Mapping, TypeAlias, TypeVar

import numpy as np
import numpy.typing as npt


Float64Array: TypeAlias = npt.NDArray[np.float64]
UInt64Array: TypeAlias = npt.NDArray[np.uint64]
UInt32Array: TypeAlias = npt.NDArray[np.uint32]
UInt8Array: TypeAlias = npt.NDArray[np.uint8]
K = TypeVar("K")
V = TypeVar("V")

# COLMAP binary 파일에서 유효하지 않은 POINT3D_ID를 표현하는 uint64 최대값입니다.
INVALID_POINT3D_ID = int(np.iinfo(np.uint64).max)


def _freeze_array(
    value: npt.ArrayLike,
    dtype: npt.DTypeLike,
    shape: tuple[int | None, ...],
    field_name: str,
) -> npt.NDArray[np.generic]:
    """배열의 dtype·형상·연속성을 검증하고 읽기 전용으로 고정합니다."""
    array = np.ascontiguousarray(value, dtype=dtype)
    if array.ndim != len(shape):
        raise ValueError(f"{field_name} 배열 차원이 올바르지 않습니다.")
    for actual_size, expected_size in zip(array.shape, shape, strict=True):
        if expected_size is not None and actual_size != expected_size:
            raise ValueError(f"{field_name} 배열 형상이 올바르지 않습니다.")
    array.setflags(write=False)
    return array


def _freeze_mapping(value: Mapping[K, V]) -> Mapping[K, V]:
    """입력 사전을 복사해 이후 외부 변경이 모델에 반영되지 않게 합니다."""
    return MappingProxyType(dict(value))


@dataclass(frozen=True, slots=True)
class CameraRecord:
    """한 COLMAP 카메라의 내부 파라미터를 보관합니다."""

    camera_id: int
    model_id: int
    model_name: str
    width: int
    height: int
    params_float64: Float64Array

    def __post_init__(self) -> None:
        """카메라 파라미터 배열을 float64 연속 배열로 고정합니다."""
        object.__setattr__(
            self,
            "params_float64",
            _freeze_array(self.params_float64, np.float64, (None,), "카메라 파라미터"),
        )


@dataclass(frozen=True, slots=True)
class ImageRecord:
    """한 이미지의 COLMAP 원본 카메라 자세와 이름을 보관합니다."""

    image_id: int
    camera_id: int
    name: str
    cam_from_world: Float64Array

    def __post_init__(self) -> None:
        """쿼터니언과 이동 벡터를 하나의 float64 배열로 고정합니다."""
        object.__setattr__(
            self,
            "cam_from_world",
            _freeze_array(self.cam_from_world, np.float64, (7,), "이미지 자세"),
        )


@dataclass(frozen=True, slots=True)
class ImageTable:
    """이미지 레코드와 points2D를 offset 기반 연속 배열로 보관합니다."""

    image_records: Mapping[int, ImageRecord]
    point2d_offsets: UInt64Array
    point2d_xy: Float64Array
    point2d_point3d_ids: UInt64Array

    def __post_init__(self) -> None:
        """points2D 배열 간 길이와 레코드 순서를 검증해 고정합니다."""
        records = _freeze_mapping(self.image_records)
        offsets = _freeze_array(
            self.point2d_offsets,
            np.uint64,
            (len(records) + 1,),
            "points2D offset",
        )
        xy = _freeze_array(self.point2d_xy, np.float64, (None, 2), "points2D 좌표")
        point3d_ids = _freeze_array(
            self.point2d_point3d_ids,
            np.uint64,
            (xy.shape[0],),
            "points2D POINT3D_ID",
        )
        if int(offsets[0]) != 0 or int(offsets[-1]) != xy.shape[0]:
            raise ValueError("points2D offset 범위가 좌표 배열과 일치하지 않습니다.")
        if np.any(offsets[1:] < offsets[:-1]):
            raise ValueError("points2D offset은 오름차순이어야 합니다.")
        object.__setattr__(self, "image_records", records)
        object.__setattr__(self, "point2d_offsets", offsets)
        object.__setattr__(self, "point2d_xy", xy)
        object.__setattr__(self, "point2d_point3d_ids", point3d_ids)

    def point_range(self, image_id: int) -> slice:
        """지정 이미지의 points2D가 놓인 연속 배열 구간을 반환합니다."""
        try:
            image_index = tuple(self.image_records).index(image_id)
        except ValueError as error:
            raise KeyError(f"존재하지 않는 IMAGE_ID입니다: {image_id}") from error
        return slice(
            int(self.point2d_offsets[image_index]),
            int(self.point2d_offsets[image_index + 1]),
        )

    @property
    def count(self) -> int:
        """적재된 이미지 개수를 반환합니다."""
        return len(self.image_records)


@dataclass(frozen=True, slots=True)
class PointTable:
    """희소점과 관측 트랙을 연속 NumPy 배열로 보관합니다."""

    point_ids: UInt64Array
    positions: Float64Array
    colors: UInt8Array
    errors: Float64Array
    track_offsets: UInt64Array
    track_image_ids: UInt64Array
    track_point2d_indices: UInt32Array | UInt64Array
    display_indices: UInt32Array | UInt64Array

    def __post_init__(self) -> None:
        """점·트랙·표시 배열의 dtype과 offset 불변조건을 고정합니다."""
        point_ids = _freeze_array(self.point_ids, np.uint64, (None,), "POINT3D_ID")
        point_count = point_ids.shape[0]
        positions = _freeze_array(self.positions, np.float64, (point_count, 3), "점 좌표")
        colors = _freeze_array(self.colors, np.uint8, (point_count, 3), "점 RGB")
        errors = _freeze_array(self.errors, np.float64, (point_count,), "점 오차")
        track_offsets = _freeze_array(
            self.track_offsets,
            np.uint64,
            (point_count + 1,),
            "점 트랙 offset",
        )
        track_count = int(track_offsets[-1]) if track_offsets.size else 0
        track_image_ids = _freeze_array(
            self.track_image_ids,
            np.uint64,
            (track_count,),
            "점 트랙 IMAGE_ID",
        )
        track_index_input = np.ascontiguousarray(self.track_point2d_indices)
        if track_index_input.dtype not in (np.dtype(np.uint32), np.dtype(np.uint64)):
            raise ValueError("점 트랙 POINT2D_IDX는 uint32 또는 uint64여야 합니다.")
        track_point2d_indices = _freeze_array(
            track_index_input,
            track_index_input.dtype,
            (track_count,),
            "점 트랙 POINT2D_IDX",
        )
        display_input = np.ascontiguousarray(self.display_indices)
        if display_input.dtype not in (np.dtype(np.uint32), np.dtype(np.uint64)):
            raise ValueError("표시 인덱스는 uint32 또는 uint64여야 합니다.")
        display_indices = _freeze_array(
            display_input,
            display_input.dtype,
            (None,),
            "표시 인덱스",
        )
        if int(track_offsets[0]) != 0:
            raise ValueError("점 트랙 offset은 0부터 시작해야 합니다.")
        if np.any(track_offsets[1:] < track_offsets[:-1]):
            raise ValueError("점 트랙 offset은 오름차순이어야 합니다.")
        if np.any(display_indices >= point_count):
            raise ValueError("표시 인덱스가 점 배열 범위를 벗어났습니다.")
        object.__setattr__(self, "point_ids", point_ids)
        object.__setattr__(self, "positions", positions)
        object.__setattr__(self, "colors", colors)
        object.__setattr__(self, "errors", errors)
        object.__setattr__(self, "track_offsets", track_offsets)
        object.__setattr__(self, "track_image_ids", track_image_ids)
        object.__setattr__(self, "track_point2d_indices", track_point2d_indices)
        object.__setattr__(self, "display_indices", display_indices)

    @property
    def count(self) -> int:
        """원본 희소점 개수를 반환합니다."""
        return self.point_ids.shape[0]

    def track_range(self, point_index: int) -> slice:
        """점 배열 인덱스에 대응하는 관측 트랙 구간을 반환합니다."""
        if point_index < 0 or point_index >= self.count:
            raise IndexError("점 배열 인덱스가 범위를 벗어났습니다.")
        return slice(
            int(self.track_offsets[point_index]),
            int(self.track_offsets[point_index + 1]),
        )


@dataclass(frozen=True, slots=True)
class RigSensorRecord:
    """리그의 센서 식별자와 선택적 sensor_from_rig 자세를 보관합니다."""

    sensor_type: str
    sensor_id: int
    sensor_from_rig: Float64Array | None = None

    def __post_init__(self) -> None:
        """존재하는 센서 자세만 float64 배열로 고정합니다."""
        if self.sensor_from_rig is not None:
            object.__setattr__(
                self,
                "sensor_from_rig",
                _freeze_array(self.sensor_from_rig, np.float64, (7,), "센서 자세"),
            )


@dataclass(frozen=True, slots=True)
class RigRecord:
    """COLMAP 리그의 기준 센서와 나머지 센서를 보관합니다."""

    rig_id: int
    reference_sensor: RigSensorRecord | None
    sensors: tuple[RigSensorRecord, ...]


@dataclass(frozen=True, slots=True)
class FrameDataRecord:
    """프레임 안에 노출된 센서 데이터 식별자를 보관합니다."""

    sensor_type: str
    sensor_id: int
    data_id: int


@dataclass(frozen=True, slots=True)
class FrameRecord:
    """리그 인스턴스의 원본 자세와 노출 데이터 연결을 보관합니다."""

    frame_id: int
    rig_id: int
    rig_from_world: Float64Array
    data_ids: tuple[FrameDataRecord, ...]

    def __post_init__(self) -> None:
        """프레임 자세를 float64 연속 배열로 고정합니다."""
        object.__setattr__(
            self,
            "rig_from_world",
            _freeze_array(self.rig_from_world, np.float64, (7,), "프레임 자세"),
        )


@dataclass(frozen=True, slots=True)
class ColmapModelData:
    """한 sparse model의 CPU 적재 결과와 렌더링용 점 테이블을 보관합니다."""

    model_id: str
    display_name: str
    source_path: Path
    source_format: str
    cameras: Mapping[int, CameraRecord]
    images: ImageTable
    rigs: Mapping[int, RigRecord]
    frames: Mapping[int, FrameRecord]
    points: PointTable
    bounds_min: Float64Array
    bounds_max: Float64Array
    geometry_revision: int = 0

    def __post_init__(self) -> None:
        """모델 경로·사전·경계 배열을 불변 형태로 고정합니다."""
        if self.source_format not in {"binary", "text"}:
            raise ValueError("COLMAP 소스 형식은 binary 또는 text여야 합니다.")
        if not self.display_name:
            raise ValueError("COLMAP 모델 표시 이름이 비어 있습니다.")
        if self.geometry_revision < 0:
            raise ValueError("geometry revision은 음수일 수 없습니다.")
        object.__setattr__(self, "source_path", Path(self.source_path))
        object.__setattr__(self, "cameras", _freeze_mapping(self.cameras))
        object.__setattr__(self, "rigs", _freeze_mapping(self.rigs))
        object.__setattr__(self, "frames", _freeze_mapping(self.frames))
        object.__setattr__(
            self,
            "bounds_min",
            _freeze_array(self.bounds_min, np.float64, (3,), "점 경계 최소값"),
        )
        object.__setattr__(
            self,
            "bounds_max",
            _freeze_array(self.bounds_max, np.float64, (3,), "점 경계 최대값"),
        )


# 초기 계획서와 호출부가 사용하는 이름을 같은 데이터 계약으로 유지합니다.
ParsedColmapModel = ColmapModelData
