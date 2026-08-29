"""COLMAP Binary에서 Camera와 Image Pose Header만 안전하게 읽습니다."""

from __future__ import annotations

import mmap
import struct
from pathlib import Path
from collections.abc import Generator
from typing import BinaryIO

from .camera_models import BY_ID, model_by_id
from .errors import ColmapError
from .io_common import (
    CancelCheck,
    check_cancel,
    consume_generator,
    ensure_stream_unchanged,
    open_source,
)
from .model import (
    DEFAULT_LIMITS,
    CameraRecord,
    ImagePoseRecord,
    ParserLimits,
    Point3DRecord,
    SourceFileIdentity,
    TrackElement,
)

_COUNT = struct.Struct("<Q")
_CAMERA_HEADER = struct.Struct("<IiQQ")
_IMAGE_HEADER = struct.Struct("<IdddddddI")
_POINT2D_SIZE = struct.calcsize("<ddQ")
_POINT3D_HEADER = struct.Struct("<QdddBBBd")
_TRACK = struct.Struct("<II")
_PARAMETER_STRUCTS = {
    count: struct.Struct("<" + ("d" * count))
    for count in {model.num_params for model in BY_ID.values()}
}


class BinaryCursor:
    """Binary Buffer의 범위와 현재 Offset을 관리합니다."""

    __slots__ = ("_buffer", "_length", "offset", "source")

    def __init__(self, buffer: bytes | mmap.mmap, source: Path) -> None:
        """읽기 전용 Buffer와 Source 경로를 초기화합니다."""
        self._buffer = buffer
        self._length = len(buffer)
        self.offset = 0
        self.source = source

    @property
    def remaining_bytes(self) -> int:
        """현재 Offset 뒤에 남은 Byte 수를 반환합니다."""
        return self._length - self.offset

    def unpack_one(self, record: struct.Struct, context: str) -> tuple:
        """고정 폭 Record 하나를 범위 검사 후 읽습니다."""
        start = self.offset
        end = start + record.size
        if end > self._length:
            raise ColmapError(
                f"{context}을 읽는 중 파일이 끝났습니다: "
                f"{self.source} offset={start}"
            )
        values = record.unpack_from(self._buffer, start)
        self.offset = end
        return values

    def ensure_count_fits(self, count: int, record_size: int, context: str) -> None:
        """Count가 남은 고정 폭 범위 안에 드는지 나눗셈으로 검증합니다."""
        if record_size <= 0 or count > self.remaining_bytes // record_size:
            raise ColmapError(
                f"{context} Count가 남은 파일 범위를 초과했습니다: "
                f"{self.source} offset={self.offset} count={count}"
            )

    def skip_fixed(self, count: int, record_size: int, context: str) -> None:
        """고정 폭 Payload를 해석하지 않고 Offset만 이동합니다."""
        self.ensure_count_fits(count, record_size, context)
        self.offset += count * record_size

    def read_c_string(self, max_bytes: int, context: str) -> str:
        """최대 길이 안의 NUL 종료 UTF-8 문자열을 읽습니다."""
        search_end = min(self._length, self.offset + max_bytes + 1)
        terminator = self._buffer.find(b"\0", self.offset, search_end)
        if terminator < 0:
            raise ColmapError(
                f"{context} 문자열이 NUL 종료되지 않았거나 안전 상한 "
                f"{max_bytes} bytes를 초과했습니다: {self.source} "
                f"offset={self.offset}"
            )
        encoded = self._buffer[self.offset:terminator]
        self.offset = terminator + 1
        try:
            return encoded.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise ColmapError(
                f"{context} 문자열이 올바른 UTF-8이 아닙니다: "
                f"{self.source} offset={self.offset}"
            ) from exc

    def ensure_eof(self) -> None:
        """모든 Record 뒤에 예상하지 않은 Byte가 없는지 확인합니다."""
        if self.offset != self._length:
            raise ColmapError(
                f"예상하지 않은 Binary 잔여 데이터가 있습니다: "
                f"{self.source} offset={self.offset} remaining={self.remaining_bytes}"
            )


def _mapped_buffer(
    stream: BinaryIO,
) -> tuple[bytes | mmap.mmap, mmap.mmap | None]:
    """열린 Binary Stream을 mmap하고 불가능하면 Bytes로 대체합니다."""
    stream.seek(0, 2)
    length = stream.tell()
    stream.seek(0)
    if length == 0:
        return b"", None
    try:
        mapping = mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ)
    except (OSError, ValueError):
        return stream.read(), None
    return mapping, mapping


def _check_record_cancel(
    index: int,
    limits: ParserLimits,
    cancel_check: CancelCheck,
) -> None:
    """설정된 Record 간격마다 취소 요청을 확인합니다."""
    if index % limits.cancel_check_interval == 0:
        check_cancel(cancel_check)


def parse_cameras_binary_steps(
    path: Path | str,
    *,
    limits: ParserLimits = DEFAULT_LIMITS,
    cancel_check: CancelCheck = None,
) -> Generator[tuple[int, int], None, tuple[dict[int, CameraRecord], SourceFileIdentity]]:
    """cameras.bin을 증분 해석하며 처리 Byte와 전체 Byte를 전달합니다."""
    resolved, stream, identity = open_source(path, limits)
    mapping: mmap.mmap | None = None
    try:
        buffer, mapping = _mapped_buffer(stream)
        cursor = BinaryCursor(buffer, resolved)
        (count,) = cursor.unpack_one(_COUNT, "Camera 개수")
        if count > limits.max_cameras:
            raise ColmapError(
                f"Camera 수가 안전 상한을 초과했습니다: {count}"
            )
        cursor.ensure_count_fits(count, _CAMERA_HEADER.size, "Camera Record")
        records: dict[int, CameraRecord] = {}
        yield cursor.offset, identity.size
        for index in range(count):
            _check_record_cancel(index, limits, cancel_check)
            offset = cursor.offset
            camera_id, model_id, width, height = cursor.unpack_one(
                _CAMERA_HEADER,
                "Camera Header",
            )
            model = model_by_id(model_id)
            params = cursor.unpack_one(
                _PARAMETER_STRUCTS[model.num_params],
                f"CAMERA_ID {camera_id} Parameter",
            )
            if camera_id in records:
                raise ColmapError(
                    f"중복 CAMERA_ID입니다: {camera_id} "
                    f"({resolved} offset={offset})"
                )
            records[camera_id] = CameraRecord(
                camera_id=camera_id,
                model_id=model_id,
                model_name=model.name,
                width=width,
                height=height,
                params=tuple(params),
            )
            if (index + 1) % limits.cancel_check_interval == 0:
                yield cursor.offset, identity.size
        cursor.ensure_eof()
        ensure_stream_unchanged(resolved, stream, identity)
        check_cancel(cancel_check)
        return records, identity
    finally:
        if mapping is not None:
            mapping.close()
        stream.close()


def parse_cameras_binary(
    path: Path | str,
    *,
    limits: ParserLimits = DEFAULT_LIMITS,
    cancel_check: CancelCheck = None,
) -> tuple[dict[int, CameraRecord], SourceFileIdentity]:
    """cameras.bin 증분 Parser를 동기 완료해 Camera Record를 반환합니다."""

    return consume_generator(
        parse_cameras_binary_steps(
            path,
            limits=limits,
            cancel_check=cancel_check,
        )
    )


def parse_images_binary_steps(
    path: Path | str,
    *,
    limits: ParserLimits = DEFAULT_LIMITS,
    cancel_check: CancelCheck = None,
) -> Generator[tuple[int, int], None, tuple[dict[int, ImagePoseRecord], SourceFileIdentity]]:
    """images.bin을 증분 해석하고 Point2D는 객체 생성 없이 건너뜁니다."""
    resolved, stream, identity = open_source(path, limits)
    mapping: mmap.mmap | None = None
    try:
        buffer, mapping = _mapped_buffer(stream)
        cursor = BinaryCursor(buffer, resolved)
        (count,) = cursor.unpack_one(_COUNT, "Image 개수")
        if count > limits.max_images:
            raise ColmapError(
                f"Image 수가 안전 상한을 초과했습니다: {count}"
            )
        cursor.ensure_count_fits(count, _IMAGE_HEADER.size, "Image Record")
        records: dict[int, ImagePoseRecord] = {}
        yield cursor.offset, identity.size
        for index in range(count):
            _check_record_cancel(index, limits, cancel_check)
            offset = cursor.offset
            values = cursor.unpack_one(_IMAGE_HEADER, "Image Header")
            image_id = values[0]
            image_name = cursor.read_c_string(
                limits.max_image_name_bytes,
                f"IMAGE_ID {image_id} 이름",
            )
            (point_count,) = cursor.unpack_one(
                _COUNT,
                f"IMAGE_ID {image_id} Point2D 개수",
            )
            cursor.skip_fixed(
                point_count,
                _POINT2D_SIZE,
                f"IMAGE_ID {image_id} Point2D",
            )
            if image_id in records:
                raise ColmapError(
                    f"중복 IMAGE_ID입니다: {image_id} "
                    f"({resolved} offset={offset})"
                )
            records[image_id] = ImagePoseRecord(
                image_id=image_id,
                camera_id=values[8],
                qvec=tuple(values[1:5]),
                tvec=tuple(values[5:8]),
                image_name=image_name,
            )
            if (index + 1) % limits.cancel_check_interval == 0:
                yield cursor.offset, identity.size
        cursor.ensure_eof()
        ensure_stream_unchanged(resolved, stream, identity)
        check_cancel(cancel_check)
        return records, identity
    finally:
        if mapping is not None:
            mapping.close()
        stream.close()


def parse_images_binary(
    path: Path | str,
    *,
    limits: ParserLimits = DEFAULT_LIMITS,
    cancel_check: CancelCheck = None,
) -> tuple[dict[int, ImagePoseRecord], SourceFileIdentity]:
    """images.bin 증분 Parser를 동기 완료해 Image Pose를 반환합니다."""

    return consume_generator(
        parse_images_binary_steps(
            path,
            limits=limits,
            cancel_check=cancel_check,
        )
    )


def parse_points3d_binary_steps(
    path: Path | str,
    *,
    limits: ParserLimits = DEFAULT_LIMITS,
    cancel_check: CancelCheck = None,
) -> Generator[tuple[int, int], None, tuple[dict[int, Point3DRecord], SourceFileIdentity]]:
    """points3D.bin을 증분 해석하며 처리 Byte와 전체 Byte를 전달합니다."""
    resolved, stream, identity = open_source(path, limits)
    mapping: mmap.mmap | None = None
    try:
        buffer, mapping = _mapped_buffer(stream)
        cursor = BinaryCursor(buffer, resolved)
        (count,) = cursor.unpack_one(_COUNT, "Point3D 개수")
        if count > limits.max_points3d:
            raise ColmapError(
                f"Point3D 수가 안전 상한을 초과했습니다: {count}"
            )
        cursor.ensure_count_fits(count, _POINT3D_HEADER.size + _COUNT.size, "Point3D Record")
        records: dict[int, Point3DRecord] = {}
        total_track_elements = 0
        yield cursor.offset, identity.size
        for index in range(count):
            _check_record_cancel(index, limits, cancel_check)
            offset = cursor.offset
            values = cursor.unpack_one(_POINT3D_HEADER, "Point3D Header")
            point3d_id = values[0]
            (track_count,) = cursor.unpack_one(
                _COUNT,
                f"POINT3D_ID {point3d_id} Track 개수",
            )
            total_track_elements += track_count
            if total_track_elements > limits.max_track_elements:
                raise ColmapError(
                    "Point3D Track 수가 안전 상한을 초과했습니다: "
                    f"{total_track_elements}"
                )
            cursor.ensure_count_fits(
                track_count,
                _TRACK.size,
                f"POINT3D_ID {point3d_id} Track",
            )
            track_items = []
            for track_index in range(track_count):
                track_items.append(
                    TrackElement(
                        *cursor.unpack_one(
                            _TRACK,
                            f"POINT3D_ID {point3d_id} Track",
                        )
                    )
                )
                if (track_index + 1) % 4_096 == 0:
                    yield cursor.offset, identity.size
            track = tuple(track_items)
            if point3d_id in records:
                raise ColmapError(
                    f"중복 POINT3D_ID입니다: {point3d_id} "
                    f"({resolved} offset={offset})"
                )
            records[point3d_id] = Point3DRecord(
                point3d_id=point3d_id,
                xyz=tuple(values[1:4]),
                rgb=tuple(values[4:7]),
                error=values[7],
                track=track,
            )
            if (index + 1) % limits.cancel_check_interval == 0:
                yield cursor.offset, identity.size
        cursor.ensure_eof()
        ensure_stream_unchanged(resolved, stream, identity)
        check_cancel(cancel_check)
        return records, identity
    finally:
        if mapping is not None:
            mapping.close()
        stream.close()


def parse_points3d_binary(
    path: Path | str,
    *,
    limits: ParserLimits = DEFAULT_LIMITS,
    cancel_check: CancelCheck = None,
) -> tuple[dict[int, Point3DRecord], SourceFileIdentity]:
    """points3D.bin 증분 Parser를 동기 완료해 Point3D를 반환합니다."""

    return consume_generator(
        parse_points3d_binary_steps(
            path,
            limits=limits,
            cancel_check=cancel_check,
        )
    )


def read_cameras_binary(
    path: Path | str,
    *,
    limits: ParserLimits = DEFAULT_LIMITS,
) -> dict[int, CameraRecord]:
    """호환용 진입점으로 Binary Camera Mapping만 반환합니다."""
    records, _identity = parse_cameras_binary(path, limits=limits)
    return records


def read_images_binary(
    path: Path | str,
    *,
    limits: ParserLimits = DEFAULT_LIMITS,
) -> dict[int, ImagePoseRecord]:
    """호환용 진입점으로 Binary Image Pose Mapping만 반환합니다."""
    records, _identity = parse_images_binary(path, limits=limits)
    return records


def read_points3d_binary(
    path: Path | str,
    *,
    limits: ParserLimits = DEFAULT_LIMITS,
) -> dict[int, Point3DRecord]:
    """호환용 진입점으로 Binary Point3D Mapping만 반환합니다."""
    records, _identity = parse_points3d_binary(path, limits=limits)
    return records
