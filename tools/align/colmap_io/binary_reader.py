"""little-endian COLMAP binary sparse model 파일을 안전하게 읽습니다."""

from __future__ import annotations

from array import array
from pathlib import Path
import struct
import sys
from typing import Final

import numpy as np

from ..logging_utils import get_logger
from .camera_models import camera_model_from_id
from .errors import ColmapParseError
from .types import (
    CameraRecord,
    FrameDataRecord,
    FrameRecord,
    ImageRecord,
    ImageTable,
    INVALID_POINT3D_ID,
    PointTable,
    RigRecord,
    RigSensorRecord,
)


LOGGER = get_logger("colmap_io.binary_reader")
MAX_RECORD_COUNT: Final = 10_000_000
MAX_VARIABLE_ITEM_COUNT: Final = 25_000_000
MAX_STRING_BYTES: Final = 1_048_576
MAX_ARRAY_BYTES: Final = 512 * 1024 * 1024
_POINT2D_BINARY_DTYPE: Final = np.dtype(
    [("xy", "<f8", (2,)), ("point3d_id", "<u8")],
    align=False,
)
_TRACK_BINARY_DTYPE: Final = np.dtype(
    [("image_id", "<u4"), ("point2d_idx", "<u4")],
    align=False,
)


class _BinaryCursor:
    """파일 크기와 현재 위치를 함께 추적하며 정확한 바이트 수만 읽습니다."""

    def __init__(self, path: Path) -> None:
        """읽을 파일의 크기를 확인하고 아직 열지 않은 cursor를 준비합니다."""
        self.path = path
        try:
            self.size = path.stat().st_size
        except OSError as error:
            raise ColmapParseError(f"COLMAP binary 파일 정보를 읽을 수 없습니다: {path}") from error
        if self.size <= 0:
            raise ColmapParseError(f"COLMAP binary 파일이 비어 있습니다: {path.name}")
        self._file: object | None = None
        self.position = 0

    def __enter__(self) -> _BinaryCursor:
        """파일 핸들을 열어 컨텍스트 안에서만 사용하게 합니다."""
        try:
            self._file = self.path.open("rb")
        except OSError as error:
            raise ColmapParseError(f"COLMAP binary 파일을 열 수 없습니다: {self.path}") from error
        return self

    def __exit__(self, *_: object) -> None:
        """예외 여부와 관계없이 파일 핸들을 닫습니다."""
        if self._file is not None:
            self._file.close()  # type: ignore[union-attr]
            self._file = None

    @property
    def remaining(self) -> int:
        """아직 읽지 않은 파일 바이트 수를 반환합니다."""
        return self.size - self.position

    def read_exact(self, byte_count: int, label: str) -> bytes:
        """범위를 먼저 검사한 뒤 지정 바이트를 모두 읽거나 명확히 실패합니다."""
        if byte_count < 0 or byte_count > self.remaining:
            raise ColmapParseError(
                f"{self.path.name}의 {label}을 읽는 중 파일이 조기 종료되었습니다."
            )
        if self._file is None:
            raise RuntimeError("열리지 않은 COLMAP binary cursor를 사용했습니다.")
        data = self._file.read(byte_count)  # type: ignore[union-attr]
        self.position += len(data)
        if len(data) != byte_count:
            raise ColmapParseError(
                f"{self.path.name}의 {label}을 읽는 중 파일이 조기 종료되었습니다."
            )
        return data

    def unpack(self, format_text: str, label: str) -> tuple[object, ...]:
        """little-endian 고정 크기 필드를 읽어 Python 값으로 변환합니다."""
        format_with_endian = f"<{format_text}"
        raw = self.read_exact(struct.calcsize(format_with_endian), label)
        return struct.unpack(format_with_endian, raw)

    def read_uint8(self, label: str) -> int:
        """uint8 필드를 읽습니다."""
        return int(self.unpack("B", label)[0])

    def read_uint32(self, label: str) -> int:
        """uint32 필드를 읽습니다."""
        return int(self.unpack("I", label)[0])

    def read_int32(self, label: str) -> int:
        """int32 필드를 읽습니다."""
        return int(self.unpack("i", label)[0])

    def read_uint64(self, label: str) -> int:
        """uint64 필드를 읽습니다."""
        return int(self.unpack("Q", label)[0])

    def read_float64(self, label: str) -> float:
        """float64 필드를 읽습니다."""
        return float(self.unpack("d", label)[0])

    def read_float64_vector(self, count: int, label: str) -> np.ndarray:
        """연속된 little-endian float64 값들을 독립 NumPy 배열로 읽습니다."""
        byte_count = _checked_byte_count(count, 8, label)
        raw = self.read_exact(byte_count, label)
        return np.frombuffer(raw, dtype="<f8", count=count).astype(np.float64, copy=True)

    def read_null_terminated_utf8(self, label: str) -> str:
        """길이 제한·UTF-8 검사를 거친 NUL 종료 문자열을 읽습니다."""
        value = bytearray()
        while True:
            if len(value) >= MAX_STRING_BYTES:
                raise ColmapParseError(
                    f"{self.path.name}의 {label} 문자열이 허용 길이를 초과했습니다."
                )
            byte = self.read_exact(1, label)
            if byte == b"\0":
                break
            value.extend(byte)
        try:
            decoded = value.decode("utf-8", errors="strict")
        except UnicodeDecodeError as error:
            raise ColmapParseError(
                f"{self.path.name}의 {label}이 UTF-8 문자열이 아닙니다."
            ) from error
        if not decoded:
            raise ColmapParseError(f"{self.path.name}의 이미지 이름이 비어 있습니다.")
        return decoded

    def ensure_consumed(self) -> None:
        """정상 레코드 뒤의 비정상 trailing data를 명시적으로 거부합니다."""
        if self.remaining:
            raise ColmapParseError(
                f"{self.path.name}에 선언된 레코드 뒤 trailing data가 {self.remaining}바이트 남았습니다."
            )


def _checked_byte_count(item_count: int, item_size: int, label: str) -> int:
    """가변 배열의 곱셈 overflow와 과도한 단일 배열 할당을 차단합니다."""
    if item_count < 0 or item_size <= 0:
        raise ColmapParseError(f"{label}의 길이 정보가 올바르지 않습니다.")
    if item_count > MAX_VARIABLE_ITEM_COUNT:
        raise ColmapParseError(f"{label}의 선언 개수가 허용 한도를 초과했습니다: {item_count}")
    if item_count > MAX_ARRAY_BYTES // item_size:
        raise ColmapParseError(f"{label}의 선언 크기가 메모리 안전 한도를 초과했습니다.")
    return item_count * item_size


def _read_record_count(cursor: _BinaryCursor, label: str, minimum_record_bytes: int) -> int:
    """레코드 수와 남은 최소 파일 크기를 함께 검증합니다."""
    count = cursor.read_uint64(f"{label} 레코드 수")
    if count > MAX_RECORD_COUNT:
        raise ColmapParseError(f"{cursor.path.name}의 {label} 레코드 수가 허용 한도를 초과했습니다.")
    if count and count > cursor.remaining // minimum_record_bytes:
        raise ColmapParseError(
            f"{cursor.path.name}의 {label} 레코드 수가 남은 파일 크기와 맞지 않습니다."
        )
    return count


def _read_variable_count(
    cursor: _BinaryCursor,
    label: str,
    item_size: int,
) -> int:
    """가변 배열 개수와 남은 바이트를 검증해 반환합니다."""
    count = cursor.read_uint64(f"{label} 개수")
    byte_count = _checked_byte_count(count, item_size, label)
    if byte_count > cursor.remaining:
        raise ColmapParseError(
            f"{cursor.path.name}의 {label} 개수가 남은 파일 크기와 맞지 않습니다."
        )
    return count


def _append_native_values(destination: array, values: np.ndarray, dtype: np.dtype) -> None:
    """little-endian NumPy 값을 적은 Python 객체 수로 연속 버퍼에 추가합니다."""
    normalized = np.ascontiguousarray(values, dtype=dtype)
    raw = normalized.tobytes(order="C")
    if sys.byteorder == "little":
        destination.frombytes(raw)
        return
    copied = array(destination.typecode)
    copied.frombytes(raw)
    copied.byteswap()
    destination.extend(copied)


def _array_from_buffer(buffer: array, dtype: np.dtype, shape: tuple[int, ...]) -> np.ndarray:
    """내부 array 버퍼를 지정 dtype·shape의 독립 NumPy 배열로 변환합니다."""
    result = np.asarray(buffer, dtype=dtype).copy()
    return np.ascontiguousarray(result.reshape(shape))


def _allocate_array(shape: tuple[int, ...], dtype: np.dtype, label: str) -> np.ndarray:
    """안전한 바이트 한도 안에서만 NumPy 배열을 할당합니다."""
    item_count = 1
    for dimension in shape:
        if dimension < 0:
            raise ColmapParseError(f"{label} 배열 형상이 올바르지 않습니다.")
        item_count *= dimension
    if item_count > MAX_ARRAY_BYTES // dtype.itemsize:
        raise ColmapParseError(f"{label} 배열이 메모리 안전 한도를 초과했습니다.")
    try:
        return np.empty(shape, dtype=dtype)
    except MemoryError as error:
        raise ColmapParseError(f"{label} 배열을 할당할 메모리가 부족합니다.") from error


def _read_pose(cursor: _BinaryCursor, label: str) -> np.ndarray:
    """COLMAP 쿼터니언과 이동 벡터의 7개 float64 값을 읽습니다."""
    return cursor.read_float64_vector(7, label)


def read_cameras_binary(path: str | Path) -> dict[int, CameraRecord]:
    """cameras.bin을 카메라 ID 기반 불변 레코드 사전으로 읽습니다."""
    binary_path = Path(path)
    LOGGER.info("event=binary_read_started stage=cameras path=%s", binary_path)
    cameras: dict[int, CameraRecord] = {}
    with _BinaryCursor(binary_path) as cursor:
        camera_count = _read_record_count(cursor, "카메라", 24)
        for _ in range(camera_count):
            camera_id = cursor.read_uint32("CAMERA_ID")
            model = camera_model_from_id(cursor.read_int32("CAMERA_MODEL_ID"))
            width = cursor.read_uint64("카메라 너비")
            height = cursor.read_uint64("카메라 높이")
            params = cursor.read_float64_vector(model.parameter_count, "카메라 파라미터")
            if camera_id in cameras:
                raise ColmapParseError(f"{binary_path.name}에 중복 CAMERA_ID가 있습니다: {camera_id}")
            cameras[camera_id] = CameraRecord(
                camera_id=camera_id,
                model_id=model.model_id,
                model_name=model.name,
                width=width,
                height=height,
                params_float64=params,
            )
        cursor.ensure_consumed()
    LOGGER.info("event=binary_read_completed stage=cameras count=%d", len(cameras))
    return cameras


def read_images_binary(path: str | Path) -> ImageTable:
    """images.bin을 이미지 레코드와 offset 기반 points2D 테이블로 읽습니다."""
    binary_path = Path(path)
    LOGGER.info("event=binary_read_started stage=images path=%s", binary_path)
    image_records: dict[int, ImageRecord] = {}
    point2d_offsets: np.ndarray
    point2d_xy_buffer = array("d")
    point2d_id_buffer = array("Q")
    with _BinaryCursor(binary_path) as cursor:
        image_count = _read_record_count(cursor, "이미지", 73)
        point2d_offsets = _allocate_array((image_count + 1,), np.dtype(np.uint64), "points2D offset")
        point2d_total = 0
        for image_index in range(image_count):
            image_id = cursor.read_uint32("IMAGE_ID")
            pose = _read_pose(cursor, "이미지 cam_from_world")
            camera_id = cursor.read_uint32("이미지 CAMERA_ID")
            name = cursor.read_null_terminated_utf8("이미지 이름")
            point2d_count = _read_variable_count(cursor, "points2D", _POINT2D_BINARY_DTYPE.itemsize)
            if point2d_total + point2d_count > MAX_VARIABLE_ITEM_COUNT:
                raise ColmapParseError("images.bin의 points2D 총개수가 허용 한도를 초과했습니다.")
            raw = cursor.read_exact(
                _checked_byte_count(point2d_count, _POINT2D_BINARY_DTYPE.itemsize, "points2D"),
                "points2D",
            )
            values = np.frombuffer(raw, dtype=_POINT2D_BINARY_DTYPE, count=point2d_count)
            _append_native_values(point2d_xy_buffer, values["xy"], np.dtype(np.float64))
            _append_native_values(point2d_id_buffer, values["point3d_id"], np.dtype(np.uint64))
            if image_id in image_records:
                raise ColmapParseError(f"{binary_path.name}에 중복 IMAGE_ID가 있습니다: {image_id}")
            image_records[image_id] = ImageRecord(
                image_id=image_id,
                camera_id=camera_id,
                name=name,
                cam_from_world=pose,
            )
            point2d_offsets[image_index] = point2d_total
            point2d_total += point2d_count
        point2d_offsets[-1] = point2d_total
        cursor.ensure_consumed()
    point2d_xy = _array_from_buffer(
        point2d_xy_buffer,
        np.dtype(np.float64),
        (len(point2d_id_buffer), 2),
    )
    point2d_point3d_ids = _array_from_buffer(
        point2d_id_buffer,
        np.dtype(np.uint64),
        (len(point2d_id_buffer),),
    )
    table = ImageTable(
        image_records=image_records,
        point2d_offsets=point2d_offsets,
        point2d_xy=point2d_xy,
        point2d_point3d_ids=point2d_point3d_ids,
    )
    LOGGER.info(
        "event=binary_read_completed stage=images image_count=%d point2d_count=%d",
        table.count,
        table.point2d_xy.shape[0],
    )
    return table


def read_points3d_binary(path: str | Path) -> PointTable:
    """points3D.bin을 원본 ID와 관측 트랙을 보존한 연속 배열로 읽습니다."""
    binary_path = Path(path)
    LOGGER.info("event=binary_read_started stage=points3D path=%s", binary_path)
    track_image_id_buffer = array("Q")
    track_point2d_index_buffer = array("I")
    with _BinaryCursor(binary_path) as cursor:
        point_count = _read_record_count(cursor, "3D 점", 51)
        point_ids = _allocate_array((point_count,), np.dtype(np.uint64), "POINT3D_ID")
        positions = _allocate_array((point_count, 3), np.dtype(np.float64), "점 좌표")
        colors = _allocate_array((point_count, 3), np.dtype(np.uint8), "점 RGB")
        errors = _allocate_array((point_count,), np.dtype(np.float64), "점 오차")
        track_offsets = _allocate_array((point_count + 1,), np.dtype(np.uint64), "점 트랙 offset")
        track_total = 0
        for point_index in range(point_count):
            point_ids[point_index] = cursor.read_uint64("POINT3D_ID")
            positions[point_index] = cursor.read_float64_vector(3, "POINT3D XYZ")
            colors[point_index] = np.frombuffer(
                cursor.read_exact(3, "POINT3D RGB"),
                dtype=np.uint8,
                count=3,
            )
            errors[point_index] = cursor.read_float64("POINT3D 재투영 오차")
            track_length = _read_variable_count(cursor, "POINT3D 관측 트랙", _TRACK_BINARY_DTYPE.itemsize)
            if track_total + track_length > MAX_VARIABLE_ITEM_COUNT:
                raise ColmapParseError("points3D.bin의 관측 트랙 총개수가 허용 한도를 초과했습니다.")
            raw = cursor.read_exact(
                _checked_byte_count(track_length, _TRACK_BINARY_DTYPE.itemsize, "POINT3D 관측 트랙"),
                "POINT3D 관측 트랙",
            )
            track_values = np.frombuffer(raw, dtype=_TRACK_BINARY_DTYPE, count=track_length)
            _append_native_values(
                track_image_id_buffer,
                track_values["image_id"],
                np.dtype(np.uint64),
            )
            _append_native_values(
                track_point2d_index_buffer,
                track_values["point2d_idx"],
                np.dtype(np.uint32),
            )
            track_offsets[point_index] = track_total
            track_total += track_length
        track_offsets[-1] = track_total
        cursor.ensure_consumed()
    track_image_ids = _array_from_buffer(
        track_image_id_buffer,
        np.dtype(np.uint64),
        (len(track_image_id_buffer),),
    )
    track_point2d_indices = _array_from_buffer(
        track_point2d_index_buffer,
        np.dtype(np.uint32),
        (len(track_point2d_index_buffer),),
    )
    display_dtype = np.dtype(np.uint32 if point_count <= np.iinfo(np.uint32).max else np.uint64)
    display_indices = np.arange(point_count, dtype=display_dtype)
    table = PointTable(
        point_ids=point_ids,
        positions=positions,
        colors=colors,
        errors=errors,
        track_offsets=track_offsets,
        track_image_ids=track_image_ids,
        track_point2d_indices=track_point2d_indices,
        display_indices=display_indices,
    )
    LOGGER.info(
        "event=binary_read_completed stage=points3D point_count=%d track_count=%d",
        table.count,
        table.track_image_ids.shape[0],
    )
    return table


def read_rigs_binary(path: str | Path) -> dict[int, RigRecord]:
    """선택 rigs.bin을 리그 ID 기반 레코드 사전으로 읽습니다."""
    binary_path = Path(path)
    LOGGER.info("event=binary_read_started stage=rigs path=%s", binary_path)
    rigs: dict[int, RigRecord] = {}
    with _BinaryCursor(binary_path) as cursor:
        rig_count = _read_record_count(cursor, "리그", 8)
        for _ in range(rig_count):
            rig_id = cursor.read_uint32("RIG_ID")
            sensor_count = cursor.read_uint32("리그 센서 수")
            if sensor_count > MAX_VARIABLE_ITEM_COUNT:
                raise ColmapParseError("rigs.bin의 센서 개수가 허용 한도를 초과했습니다.")
            reference_sensor: RigSensorRecord | None = None
            sensors: list[RigSensorRecord] = []
            if sensor_count:
                reference_sensor = RigSensorRecord(
                    sensor_type=_read_sensor_type(cursor, "기준 센서 타입"),
                    sensor_id=cursor.read_uint32("기준 센서 ID"),
                )
                sensors.append(reference_sensor)
            for _sensor_index in range(max(sensor_count - 1, 0)):
                sensor_type = _read_sensor_type(cursor, "리그 센서 타입")
                sensor_id = cursor.read_uint32("리그 센서 ID")
                has_pose = cursor.read_uint8("리그 센서 자세 플래그")
                if has_pose not in (0, 1):
                    raise ColmapParseError("rigs.bin의 센서 자세 플래그는 0 또는 1이어야 합니다.")
                sensors.append(
                    RigSensorRecord(
                        sensor_type=sensor_type,
                        sensor_id=sensor_id,
                        sensor_from_rig=_read_pose(cursor, "sensor_from_rig") if has_pose else None,
                    )
                )
            if rig_id in rigs:
                raise ColmapParseError(f"{binary_path.name}에 중복 RIG_ID가 있습니다: {rig_id}")
            rigs[rig_id] = RigRecord(
                rig_id=rig_id,
                reference_sensor=reference_sensor,
                sensors=tuple(sensors),
            )
        cursor.ensure_consumed()
    LOGGER.info("event=binary_read_completed stage=rigs count=%d", len(rigs))
    return rigs


def _read_sensor_type(cursor: _BinaryCursor, label: str) -> str:
    """binary 정수 센서 타입을 COLMAP 이름으로 변환합니다."""
    sensor_type_id = cursor.read_int32(label)
    sensor_types = {-1: "INVALID", 0: "CAMERA", 1: "IMU"}
    sensor_type = sensor_types.get(sensor_type_id)
    if sensor_type is None:
        raise ColmapParseError(f"지원하지 않는 COLMAP 센서 타입입니다: {sensor_type_id}")
    return sensor_type


def read_frames_binary(path: str | Path) -> dict[int, FrameRecord]:
    """선택 frames.bin을 프레임 ID 기반 레코드 사전으로 읽습니다."""
    binary_path = Path(path)
    LOGGER.info("event=binary_read_started stage=frames path=%s", binary_path)
    frames: dict[int, FrameRecord] = {}
    with _BinaryCursor(binary_path) as cursor:
        frame_count = _read_record_count(cursor, "프레임", 68)
        for _ in range(frame_count):
            frame_id = cursor.read_uint32("FRAME_ID")
            rig_id = cursor.read_uint32("프레임 RIG_ID")
            rig_from_world = _read_pose(cursor, "rig_from_world")
            data_count = cursor.read_uint32("프레임 데이터 ID 수")
            if data_count > MAX_VARIABLE_ITEM_COUNT:
                raise ColmapParseError("frames.bin의 데이터 ID 개수가 허용 한도를 초과했습니다.")
            if data_count and data_count > cursor.remaining // 16:
                raise ColmapParseError("frames.bin의 데이터 ID 개수가 남은 파일 크기와 맞지 않습니다.")
            data_ids = tuple(
                FrameDataRecord(
                    sensor_type=_read_sensor_type(cursor, "프레임 센서 타입"),
                    sensor_id=cursor.read_uint32("프레임 센서 ID"),
                    data_id=cursor.read_uint64("프레임 데이터 ID"),
                )
                for _data_index in range(data_count)
            )
            if frame_id in frames:
                raise ColmapParseError(f"{binary_path.name}에 중복 FRAME_ID가 있습니다: {frame_id}")
            frames[frame_id] = FrameRecord(
                frame_id=frame_id,
                rig_id=rig_id,
                rig_from_world=rig_from_world,
                data_ids=data_ids,
            )
        cursor.ensure_consumed()
    LOGGER.info("event=binary_read_completed stage=frames count=%d", len(frames))
    return frames
