"""COLMAP little-endian binary sparse 형식을 mmap offset parser로 읽습니다."""

from __future__ import annotations

import mmap
import struct
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator, TypeVar

from .camera_models import BY_ID, model_by_id
from .model import (
    CameraRecord,
    DataIdRecord,
    FrameRecord,
    ImageRecord,
    Point2DRecord,
    Point3DRecord,
    Reconstruction,
    RigRecord,
    SensorRecord,
    TrackElement,
)


_COUNT_STRUCT = struct.Struct("<Q")
_CAMERA_HEADER_STRUCT = struct.Struct("<IiQQ")
_IMAGE_HEADER_STRUCT = struct.Struct("<IdddddddI")
_POINT2D_STRUCT = struct.Struct("<ddQ")
_POINT3D_HEADER_STRUCT = struct.Struct("<QdddBBBd")
_TRACK_COUNT_STRUCT = _COUNT_STRUCT
_TRACK_STRUCT = struct.Struct("<II")
_RIG_HEADER_STRUCT = struct.Struct("<IIiI")
_SENSOR_HEADER_STRUCT = struct.Struct("<iIB")
_SENSOR_POSE_STRUCT = struct.Struct("<ddddddd")
_FRAME_HEADER_STRUCT = struct.Struct("<IIddddddd")
_DATA_ID_COUNT_STRUCT = struct.Struct("<I")
_DATA_ID_STRUCT = struct.Struct("<iIQ")
_POINT3D_SENTINEL = (1 << 64) - 1
_C_STRING_SCAN_LIMIT = 1024 * 1024

# COLMAP camera model의 파라미터 수는 고정 레지스트리에서 가져와 한 번만 준비합니다.
_CAMERA_PARAM_STRUCTS = {
    count: struct.Struct("<" + ("d" * count))
    for count in {model.num_params for model in BY_ID.values()}
}

_Buffer = bytes | mmap.mmap
_RecordT = TypeVar("_RecordT")


class BinaryCursor:
    """읽기 전용 binary buffer의 범위와 현재 offset을 관리합니다."""

    __slots__ = ("_buffer", "_length", "offset", "source")

    def __init__(self, buffer: _Buffer, source: Path):
        self._buffer = buffer
        self._length = len(buffer)
        self.offset = 0
        self.source = source

    @property
    def remaining_bytes(self) -> int:
        return self._length - self.offset

    def unpack_one(self, record_struct: struct.Struct, context: str) -> tuple:
        end = self.offset + record_struct.size
        if end > self._length:
            raise ValueError(f"{context}을(를) 읽는 중 파일이 예기치 않게 끝났습니다.")
        values = record_struct.unpack_from(self._buffer, self.offset)
        self.offset = end
        return values

    def ensure_count_fits(
        self,
        count: int,
        record_size: int,
        context: str,
    ) -> None:
        """선언 count가 최소 고정 폭만으로도 남은 범위를 넘는지 검사합니다."""

        if record_size <= 0:
            raise ValueError(f"{context}의 record 크기가 올바르지 않습니다.")
        if count > self.remaining_bytes // record_size:
            raise ValueError(f"{context}을(를) 읽는 중 파일이 예기치 않게 끝났습니다.")

    def map_fixed(
        self,
        record_struct: struct.Struct,
        count: int,
        context: str,
        factory: Callable[[tuple], _RecordT],
    ) -> tuple[_RecordT, ...]:
        """고정 폭 block을 한 번 검증하고 iter_unpack 결과를 Record로 바꿉니다."""

        self.ensure_count_fits(count, record_struct.size, context)
        if count == 0:
            return ()

        start = self.offset
        end = start + (count * record_struct.size)
        self.offset = end
        block = memoryview(self._buffer)[start:end]
        iterator = record_struct.iter_unpack(block)
        records: list[_RecordT] = []
        try:
            for values in iterator:
                records.append(factory(values))
        finally:
            # Windows에서는 exported buffer가 남아 있으면 mmap.close()가 실패합니다.
            del iterator
            block.release()
        return tuple(records)

    def read_c_string(self, context: str) -> str:
        search_end = min(self._length, self.offset + _C_STRING_SCAN_LIMIT)
        terminator = self._buffer.find(b"\0", self.offset, search_end)
        if terminator < 0:
            if self.remaining_bytes < _C_STRING_SCAN_LIMIT:
                raise ValueError(f"{context} 문자열을 읽는 중 파일이 끝났습니다.")
            raise ValueError(f"{context} 문자열이 안전 제한 1 MiB를 초과했습니다.")

        encoded = self._buffer[self.offset : terminator]
        self.offset = terminator + 1
        try:
            return encoded.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(f"{context} 문자열이 UTF-8이 아닙니다.") from exc

    def ensure_eof(self) -> None:
        if self.offset != self._length:
            raise ValueError(
                f"선언된 레코드 뒤에 해석되지 않은 데이터가 있습니다: {self.source}"
            )


@contextmanager
def _open_cursor(path: Path) -> Iterator[BinaryCursor]:
    """mmap을 우선 사용하고 지원되지 않는 파일시스템에서만 bytes로 대체합니다."""

    mapping: mmap.mmap | None = None
    fallback: bytes | None = None
    with path.open("rb") as stream:
        stream.seek(0, 2)
        length = stream.tell()
        stream.seek(0)
        if length:
            try:
                mapping = mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ)
            except (OSError, ValueError):
                fallback = stream.read()
        else:
            # 길이 0 mmap은 Windows에서 OSError이므로 Cursor의 EOF 오류로 통일합니다.
            fallback = b""

        cursor = BinaryCursor(mapping if mapping is not None else fallback or b"", path)
        try:
            yield cursor
        finally:
            # Record에는 buffer slice를 저장하지 않으므로 이 시점에는 export가 없어야 합니다.
            del cursor
            if mapping is not None:
                mapping.close()


def _point2d_record(values: tuple) -> Point2DRecord:
    x, y, point3d_id = values
    return Point2DRecord(
        x,
        y,
        None if point3d_id == _POINT3D_SENTINEL else point3d_id,
    )


def _track_element(values: tuple) -> TrackElement:
    return TrackElement(values[0], values[1])


def _data_id_record(values: tuple) -> DataIdRecord:
    return DataIdRecord(values[0], values[1], values[2])


def read_cameras_binary(path: Path) -> dict[int, CameraRecord]:
    records: dict[int, CameraRecord] = {}
    with _open_cursor(path) as cursor:
        (count,) = cursor.unpack_one(_COUNT_STRUCT, "Camera 개수")
        cursor.ensure_count_fits(count, _CAMERA_HEADER_STRUCT.size, "Camera 레코드")
        for _ in range(count):
            camera_id, model_id, width, height = cursor.unpack_one(
                _CAMERA_HEADER_STRUCT,
                "Camera 헤더",
            )
            model = model_by_id(model_id)
            params = cursor.unpack_one(
                _CAMERA_PARAM_STRUCTS[model.num_params],
                f"CAMERA_ID {camera_id} 파라미터",
            )
            if camera_id in records:
                raise ValueError(f"중복 CAMERA_ID입니다: {camera_id}")
            records[camera_id] = CameraRecord(
                camera_id,
                model_id,
                model.name,
                width,
                height,
                tuple(params),
            )
        cursor.ensure_eof()
    return records


def read_images_binary(path: Path) -> dict[int, ImageRecord]:
    records: dict[int, ImageRecord] = {}
    with _open_cursor(path) as cursor:
        (count,) = cursor.unpack_one(_COUNT_STRUCT, "Image 개수")
        cursor.ensure_count_fits(count, _IMAGE_HEADER_STRUCT.size, "Image 레코드")
        for _ in range(count):
            values = cursor.unpack_one(_IMAGE_HEADER_STRUCT, "Image 헤더")
            image_id = values[0]
            name = cursor.read_c_string(f"IMAGE_ID {image_id} 이름")
            (point_count,) = cursor.unpack_one(
                _COUNT_STRUCT,
                f"IMAGE_ID {image_id} Point2D 개수",
            )
            points = cursor.map_fixed(
                _POINT2D_STRUCT,
                point_count,
                f"IMAGE_ID {image_id} Point2D",
                _point2d_record,
            )
            if image_id in records:
                raise ValueError(f"중복 IMAGE_ID입니다: {image_id}")
            records[image_id] = ImageRecord(
                image_id=image_id,
                qvec=tuple(values[1:5]),
                tvec=tuple(values[5:8]),
                camera_id=values[8],
                name=name,
                points2d=points,
            )
        cursor.ensure_eof()
    return records


def read_points3d_binary(path: Path) -> dict[int, Point3DRecord]:
    records: dict[int, Point3DRecord] = {}
    with _open_cursor(path) as cursor:
        (count,) = cursor.unpack_one(_COUNT_STRUCT, "Point3D 개수")
        cursor.ensure_count_fits(count, _POINT3D_HEADER_STRUCT.size, "Point3D 레코드")
        for _ in range(count):
            values = cursor.unpack_one(_POINT3D_HEADER_STRUCT, "Point3D 헤더")
            point3d_id = values[0]
            (track_count,) = cursor.unpack_one(
                _TRACK_COUNT_STRUCT,
                f"POINT3D_ID {point3d_id} track 개수",
            )
            track = cursor.map_fixed(
                _TRACK_STRUCT,
                track_count,
                f"POINT3D_ID {point3d_id} track",
                _track_element,
            )
            if point3d_id in records:
                raise ValueError(f"중복 POINT3D_ID입니다: {point3d_id}")
            records[point3d_id] = Point3DRecord(
                point3d_id=point3d_id,
                xyz=tuple(values[1:4]),
                rgb=tuple(values[4:7]),
                error=values[7],
                track=track,
            )
        cursor.ensure_eof()
    return records


def _read_sensor(cursor: BinaryCursor, context: str) -> SensorRecord:
    sensor_type, sensor_id, has_pose = cursor.unpack_one(_SENSOR_HEADER_STRUCT, context)
    if has_pose not in (0, 1):
        raise ValueError(f"{context}의 HAS_POSE 값이 0 또는 1이 아닙니다.")
    if has_pose:
        pose = cursor.unpack_one(_SENSOR_POSE_STRUCT, f"{context} pose")
        return SensorRecord(
            sensor_type,
            sensor_id,
            True,
            tuple(pose[:4]),
            tuple(pose[4:]),
        )
    return SensorRecord(sensor_type, sensor_id, False)


def read_rigs_binary(path: Path) -> dict[int, RigRecord]:
    records: dict[int, RigRecord] = {}
    with _open_cursor(path) as cursor:
        (count,) = cursor.unpack_one(_COUNT_STRUCT, "Rig 개수")
        cursor.ensure_count_fits(count, _RIG_HEADER_STRUCT.size, "Rig 레코드")
        for _ in range(count):
            rig_id, num_sensors, ref_type, ref_id = cursor.unpack_one(
                _RIG_HEADER_STRUCT,
                "Rig 헤더",
            )
            if num_sensors < 1:
                raise ValueError(
                    f"RIG_ID {rig_id}의 NUM_SENSORS는 1 이상이어야 합니다."
                )
            sensor_count = num_sensors - 1
            cursor.ensure_count_fits(
                sensor_count,
                _SENSOR_HEADER_STRUCT.size,
                f"RIG_ID {rig_id} sensor",
            )
            sensors = tuple(
                _read_sensor(cursor, f"RIG_ID {rig_id} sensor")
                for _ in range(sensor_count)
            )
            if rig_id in records:
                raise ValueError(f"중복 RIG_ID입니다: {rig_id}")
            records[rig_id] = RigRecord(rig_id, ref_type, ref_id, sensors)
        cursor.ensure_eof()
    return records


def read_frames_binary(path: Path) -> dict[int, FrameRecord]:
    records: dict[int, FrameRecord] = {}
    with _open_cursor(path) as cursor:
        (count,) = cursor.unpack_one(_COUNT_STRUCT, "Frame 개수")
        cursor.ensure_count_fits(count, _FRAME_HEADER_STRUCT.size, "Frame 레코드")
        for _ in range(count):
            values = cursor.unpack_one(_FRAME_HEADER_STRUCT, "Frame 헤더")
            frame_id, rig_id = values[:2]
            (num_data_ids,) = cursor.unpack_one(
                _DATA_ID_COUNT_STRUCT,
                f"FRAME_ID {frame_id} DATA_IDS 개수",
            )
            data_ids = cursor.map_fixed(
                _DATA_ID_STRUCT,
                num_data_ids,
                f"FRAME_ID {frame_id} DATA_ID",
                _data_id_record,
            )
            if frame_id in records:
                raise ValueError(f"중복 FRAME_ID입니다: {frame_id}")
            records[frame_id] = FrameRecord(
                frame_id,
                rig_id,
                tuple(values[2:6]),
                tuple(values[6:9]),
                data_ids,
            )
        cursor.ensure_eof()
    return records


def read_model_binary(directory: Path, modern: bool) -> Reconstruction:
    return Reconstruction(
        cameras=read_cameras_binary(directory / "cameras.bin"),
        images=read_images_binary(directory / "images.bin"),
        points3d=read_points3d_binary(directory / "points3D.bin"),
        rigs=read_rigs_binary(directory / "rigs.bin") if modern else {},
        frames=read_frames_binary(directory / "frames.bin") if modern else {},
    )
