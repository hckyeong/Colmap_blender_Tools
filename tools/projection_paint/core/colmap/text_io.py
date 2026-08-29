"""COLMAP Text에서 Camera와 Image Pose Header만 증분 방식으로 읽습니다."""

from __future__ import annotations

import codecs
from collections.abc import Generator
from pathlib import Path
from typing import BinaryIO

from .camera_models import model_by_name
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

_ASCII_WHITESPACE = frozenset(b" \t\r\n\v\f")


class TextSourceReader:
    """UTF-8 COLMAP Text의 물리 행과 취소 상태를 관리합니다."""

    __slots__ = ("stream", "source", "limits", "cancel_check", "line_number")

    def __init__(
        self,
        stream: BinaryIO,
        source: Path,
        limits: ParserLimits,
        cancel_check: CancelCheck,
    ) -> None:
        """열린 Binary Stream을 Text 행 Reader로 초기화합니다."""
        self.stream = stream
        self.source = source
        self.limits = limits
        self.cancel_check = cancel_check
        self.line_number = 0

    def _read_bounded_line(self) -> bytes | None:
        """Header 한 행을 최대 길이 안에서 읽습니다."""
        check_cancel(self.cancel_check)
        raw = self.stream.readline(self.limits.max_text_header_bytes + 1)
        if raw == b"":
            return None
        self.line_number += 1
        if len(raw) > self.limits.max_text_header_bytes:
            raise ColmapError(
                f"Text Header가 안전 상한을 초과했습니다: "
                f"{self.source} line={self.line_number}"
            )
        if self.line_number == 1 and raw.startswith(codecs.BOM_UTF8):
            raw = raw[len(codecs.BOM_UTF8):]
        return raw

    def next_data_line(self) -> tuple[int, str] | None:
        """빈 행과 Comment를 건너뛰고 다음 UTF-8 Header를 반환합니다."""
        while True:
            raw = self._read_bounded_line()
            if raw is None:
                return None
            stripped = raw.strip()
            if not stripped or stripped.startswith(b"#"):
                continue
            try:
                decoded = stripped.decode("utf-8", errors="strict")
            except UnicodeDecodeError as exc:
                raise ColmapError(
                    f"Text Header가 올바른 UTF-8이 아닙니다: "
                    f"{self.source} line={self.line_number}"
                ) from exc
            return self.line_number, decoded

    def discard_point2d_line_steps(
        self,
        image_id: str,
    ) -> Generator[None, None, None]:
        """Point2D 물리 행을 Chunk 단위로 폐기하며 제어권을 반환합니다."""
        check_cancel(self.cancel_check)
        decoder = codecs.getincrementaldecoder("utf-8")(errors="strict")
        total = 0
        token_count = 0
        in_token = False
        received = False
        first_chunk = True
        while True:
            chunk = self.stream.readline(self.limits.text_chunk_bytes)
            if chunk == b"":
                if not received:
                    raise ColmapError(
                        f"IMAGE_ID {image_id}의 Point2D 물리 행이 누락되었습니다: "
                        f"{self.source} line={self.line_number + 1}"
                    )
                break
            if first_chunk:
                self.line_number += 1
                first_chunk = False
            received = True
            total += len(chunk)
            if total > self.limits.max_text_point_line_bytes:
                raise ColmapError(
                    f"IMAGE_ID {image_id}의 Point2D 행이 안전 상한을 초과했습니다: "
                    f"{self.source} line={self.line_number}"
                )
            if b"\x00" in chunk or b"#" in chunk:
                raise ColmapError(
                    f"IMAGE_ID {image_id}의 Point2D 행에 허용되지 않는 문자가 있습니다: "
                    f"{self.source} line={self.line_number}"
                )
            try:
                decoder.decode(chunk, final=False)
            except UnicodeDecodeError as exc:
                raise ColmapError(
                    f"IMAGE_ID {image_id}의 Point2D 행이 올바른 UTF-8이 아닙니다: "
                    f"{self.source} line={self.line_number}"
                ) from exc
            for value in chunk:
                is_space = value in _ASCII_WHITESPACE
                if not is_space and not in_token:
                    token_count += 1
                in_token = not is_space
            check_cancel(self.cancel_check)
            yield None
            if chunk.endswith(b"\n"):
                break
        try:
            decoder.decode(b"", final=True)
        except UnicodeDecodeError as exc:
            raise ColmapError(
                f"IMAGE_ID {image_id}의 Point2D 행이 올바른 UTF-8이 아닙니다: "
                f"{self.source} line={self.line_number}"
            ) from exc
        if token_count % 3 != 0:
            raise ColmapError(
                f"IMAGE_ID {image_id}의 Point2D Token 수가 3의 배수가 아닙니다: "
                f"{self.source} line={self.line_number}"
            )

    def discard_point2d_line(self, image_id: str) -> None:
        """증분 Point2D 행 폐기를 동기 완료합니다."""

        consume_generator(self.discard_point2d_line_steps(image_id))


def _parse_int(value: str, context: str) -> int:
    """Text 정수를 일관된 오류 메시지로 변환합니다."""
    try:
        return int(value)
    except ValueError as exc:
        raise ColmapError(f"{context} 정수가 올바르지 않습니다: {value}") from exc


def _parse_float(value: str, context: str) -> float:
    """Text 실수를 일관된 오류 메시지로 변환합니다."""
    try:
        return float(value)
    except ValueError as exc:
        raise ColmapError(f"{context} 실수가 올바르지 않습니다: {value}") from exc


def parse_cameras_text_steps(
    path: Path | str,
    *,
    limits: ParserLimits = DEFAULT_LIMITS,
    cancel_check: CancelCheck = None,
) -> Generator[tuple[int, int], None, tuple[dict[int, CameraRecord], SourceFileIdentity]]:
    """cameras.txt를 증분 해석하며 처리 Byte와 전체 Byte를 전달합니다."""
    resolved, stream, identity = open_source(path, limits)
    try:
        reader = TextSourceReader(stream, resolved, limits, cancel_check)
        records: dict[int, CameraRecord] = {}
        yield 0, identity.size
        while True:
            item = reader.next_data_line()
            if item is None:
                break
            line_number, line = item
            fields = line.split()
            if len(fields) < 5:
                raise ColmapError(
                    f"Camera Header가 불완전합니다: "
                    f"{resolved} line={line_number}"
                )
            camera_id = _parse_int(fields[0], "CAMERA_ID")
            model = model_by_name(fields[1])
            if len(fields[4:]) != model.num_params:
                raise ColmapError(
                    f"CAMERA_ID {camera_id}의 Parameter 수가 잘못되었습니다: "
                    f"{resolved} line={line_number}"
                )
            if camera_id in records:
                raise ColmapError(
                    f"중복 CAMERA_ID입니다: {camera_id} "
                    f"({resolved} line={line_number})"
                )
            records[camera_id] = CameraRecord(
                camera_id=camera_id,
                model_id=model.model_id,
                model_name=model.name,
                width=_parse_int(fields[2], f"CAMERA_ID {camera_id} Width"),
                height=_parse_int(fields[3], f"CAMERA_ID {camera_id} Height"),
                params=tuple(
                    _parse_float(value, f"CAMERA_ID {camera_id} Parameter")
                    for value in fields[4:]
                ),
            )
            if len(records) > limits.max_cameras:
                raise ColmapError(
                    f"Camera 수가 안전 상한을 초과했습니다: {len(records)}"
                )
            if len(records) % limits.cancel_check_interval == 0:
                yield min(stream.tell(), identity.size), identity.size
        ensure_stream_unchanged(resolved, stream, identity)
        check_cancel(cancel_check)
        return records, identity
    finally:
        stream.close()


def parse_cameras_text(
    path: Path | str,
    *,
    limits: ParserLimits = DEFAULT_LIMITS,
    cancel_check: CancelCheck = None,
) -> tuple[dict[int, CameraRecord], SourceFileIdentity]:
    """cameras.txt 증분 Parser를 동기 완료해 Camera Record를 반환합니다."""

    return consume_generator(
        parse_cameras_text_steps(
            path,
            limits=limits,
            cancel_check=cancel_check,
        )
    )


def parse_images_text_steps(
    path: Path | str,
    *,
    limits: ParserLimits = DEFAULT_LIMITS,
    cancel_check: CancelCheck = None,
) -> Generator[tuple[int, int], None, tuple[dict[int, ImagePoseRecord], SourceFileIdentity]]:
    """images.txt를 증분 해석하고 Point2D 행은 객체 생성 없이 폐기합니다."""
    resolved, stream, identity = open_source(path, limits)
    try:
        reader = TextSourceReader(stream, resolved, limits, cancel_check)
        records: dict[int, ImagePoseRecord] = {}
        yield 0, identity.size
        while True:
            item = reader.next_data_line()
            if item is None:
                break
            line_number, line = item
            fields = line.split(maxsplit=9)
            if len(fields) != 10:
                raise ColmapError(
                    f"Image Header가 불완전합니다: "
                    f"{resolved} line={line_number}"
                )
            image_id = _parse_int(fields[0], "IMAGE_ID")
            for _unused in reader.discard_point2d_line_steps(fields[0]):
                yield min(stream.tell(), identity.size), identity.size
            if image_id in records:
                raise ColmapError(
                    f"중복 IMAGE_ID입니다: {image_id} "
                    f"({resolved} line={line_number})"
                )
            records[image_id] = ImagePoseRecord(
                image_id=image_id,
                camera_id=_parse_int(fields[8], f"IMAGE_ID {image_id} CAMERA_ID"),
                qvec=tuple(
                    _parse_float(value, f"IMAGE_ID {image_id} QVEC")
                    for value in fields[1:5]
                ),
                tvec=tuple(
                    _parse_float(value, f"IMAGE_ID {image_id} TVEC")
                    for value in fields[5:8]
                ),
                image_name=fields[9],
            )
            if len(records) > limits.max_images:
                raise ColmapError(
                    f"Image 수가 안전 상한을 초과했습니다: {len(records)}"
                )
            if len(records) % limits.cancel_check_interval == 0:
                yield min(stream.tell(), identity.size), identity.size
        ensure_stream_unchanged(resolved, stream, identity)
        check_cancel(cancel_check)
        return records, identity
    finally:
        stream.close()


def parse_images_text(
    path: Path | str,
    *,
    limits: ParserLimits = DEFAULT_LIMITS,
    cancel_check: CancelCheck = None,
) -> tuple[dict[int, ImagePoseRecord], SourceFileIdentity]:
    """images.txt 증분 Parser를 동기 완료해 Image Pose를 반환합니다."""

    return consume_generator(
        parse_images_text_steps(
            path,
            limits=limits,
            cancel_check=cancel_check,
        )
    )


def parse_points3d_text_steps(
    path: Path | str,
    *,
    limits: ParserLimits = DEFAULT_LIMITS,
    cancel_check: CancelCheck = None,
) -> Generator[tuple[int, int], None, tuple[dict[int, Point3DRecord], SourceFileIdentity]]:
    """points3D.txt를 증분 해석하며 처리 Byte와 전체 Byte를 전달합니다."""
    resolved, stream, identity = open_source(path, limits)
    try:
        reader = TextSourceReader(stream, resolved, limits, cancel_check)
        records: dict[int, Point3DRecord] = {}
        total_track_elements = 0
        yield 0, identity.size
        while True:
            item = reader.next_data_line()
            if item is None:
                break
            line_number, line = item
            fields = line.split()
            if len(fields) < 8 or (len(fields) - 8) % 2:
                raise ColmapError(
                    f"Point3D Record가 불완전합니다: "
                    f"{resolved} line={line_number}"
                )
            point3d_id = _parse_int(fields[0], "POINT3D_ID")
            if point3d_id in records:
                raise ColmapError(
                    f"중복 POINT3D_ID입니다: {point3d_id} "
                    f"({resolved} line={line_number})"
                )
            track = tuple(
                TrackElement(
                    _parse_int(fields[offset], f"POINT3D_ID {point3d_id} IMAGE_ID"),
                    _parse_int(
                        fields[offset + 1],
                        f"POINT3D_ID {point3d_id} POINT2D_IDX",
                    ),
                )
                for offset in range(8, len(fields), 2)
            )
            total_track_elements += len(track)
            if total_track_elements > limits.max_track_elements:
                raise ColmapError(
                    "Point3D Track 수가 안전 상한을 초과했습니다: "
                    f"{total_track_elements}"
                )
            records[point3d_id] = Point3DRecord(
                point3d_id=point3d_id,
                xyz=tuple(
                    _parse_float(value, f"POINT3D_ID {point3d_id} XYZ")
                    for value in fields[1:4]
                ),
                rgb=tuple(
                    _parse_int(value, f"POINT3D_ID {point3d_id} RGB")
                    for value in fields[4:7]
                ),
                error=_parse_float(fields[7], f"POINT3D_ID {point3d_id} ERROR"),
                track=track,
            )
            if len(records) > limits.max_points3d:
                raise ColmapError(
                    f"Point3D 수가 안전 상한을 초과했습니다: {len(records)}"
                )
            if len(records) % limits.cancel_check_interval == 0:
                yield min(stream.tell(), identity.size), identity.size
        ensure_stream_unchanged(resolved, stream, identity)
        check_cancel(cancel_check)
        return records, identity
    finally:
        stream.close()


def parse_points3d_text(
    path: Path | str,
    *,
    limits: ParserLimits = DEFAULT_LIMITS,
    cancel_check: CancelCheck = None,
) -> tuple[dict[int, Point3DRecord], SourceFileIdentity]:
    """points3D.txt 증분 Parser를 동기 완료해 Point3D를 반환합니다."""

    return consume_generator(
        parse_points3d_text_steps(
            path,
            limits=limits,
            cancel_check=cancel_check,
        )
    )


def read_cameras_text(
    path: Path | str,
    *,
    limits: ParserLimits = DEFAULT_LIMITS,
) -> dict[int, CameraRecord]:
    """호환용 진입점으로 Text Camera Mapping만 반환합니다."""
    records, _identity = parse_cameras_text(path, limits=limits)
    return records


def read_images_text(
    path: Path | str,
    *,
    limits: ParserLimits = DEFAULT_LIMITS,
) -> dict[int, ImagePoseRecord]:
    """호환용 진입점으로 Text Image Pose Mapping만 반환합니다."""
    records, _identity = parse_images_text(path, limits=limits)
    return records


def read_points3d_text(
    path: Path | str,
    *,
    limits: ParserLimits = DEFAULT_LIMITS,
) -> dict[int, Point3DRecord]:
    """호환용 진입점으로 Text Point3D Mapping만 반환합니다."""
    records, _identity = parse_points3d_text(path, limits=limits)
    return records
