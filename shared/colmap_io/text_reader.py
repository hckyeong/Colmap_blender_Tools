"""UTF-8 COLMAP text sparse model 파일을 엄격한 레코드 규칙으로 읽습니다."""

from __future__ import annotations

from array import array
from pathlib import Path
from typing import Final

import numpy as np

import logging
from .camera_models import camera_model_from_name
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


LOGGER = logging.getLogger(__name__)
MAX_RECORD_COUNT: Final = 10_000_000
MAX_VARIABLE_ITEM_COUNT: Final = 25_000_000
# images.txt의 points2D는 이미지당 한 물리 줄이므로 현실적인 대형 입력을 위해 64 MiB까지 허용합니다.
MAX_TEXT_LINE_CHARS: Final = 64 * 1024 * 1024
_SENSOR_TYPES: Final = frozenset({"CAMERA", "IMU", "INVALID"})
_UINT32_MAX: Final = int(np.iinfo(np.uint32).max)
_UINT64_MAX: Final = int(np.iinfo(np.uint64).max)


class _TextLineReader:
    """UTF-8·최대 줄 길이를 검사하며 COLMAP text 줄 번호를 추적합니다."""

    def __init__(self, path: Path) -> None:
        """읽을 경로와 아직 열지 않은 파일 핸들을 준비합니다."""
        self.path = path
        self._file: object | None = None
        self.line_number = 0

    def __enter__(self) -> _TextLineReader:
        """UTF-8 strict 모드로 text 파일을 열고 빈 파일을 거부합니다."""
        try:
            if self.path.stat().st_size <= 0:
                raise ColmapParseError(f"COLMAP text 파일이 비어 있습니다: {self.path.name}")
            self._file = self.path.open("r", encoding="utf-8-sig", errors="strict", newline="")
        except UnicodeDecodeError as error:
            raise ColmapParseError(f"COLMAP text 파일이 UTF-8이 아닙니다: {self.path.name}") from error
        except OSError as error:
            raise ColmapParseError(f"COLMAP text 파일을 열 수 없습니다: {self.path}") from error
        return self

    def __exit__(self, *_: object) -> None:
        """예외 여부와 관계없이 text 파일 핸들을 닫습니다."""
        if self._file is not None:
            self._file.close()  # type: ignore[union-attr]
            self._file = None

    def read_raw_line(self) -> str | None:
        """빈 lines도 보존한 물리 줄 하나를 제한 길이 안에서 읽습니다."""
        if self._file is None:
            raise RuntimeError("열리지 않은 COLMAP text reader를 사용했습니다.")
        try:
            line = self._file.readline(MAX_TEXT_LINE_CHARS + 1)  # type: ignore[union-attr]
        except UnicodeDecodeError as error:
            raise ColmapParseError(
                f"{self.path.name}의 UTF-8 문자열을 읽을 수 없습니다."
            ) from error
        if line == "":
            return None
        self.line_number += 1
        if len(line) > MAX_TEXT_LINE_CHARS and not line.endswith(("\n", "\r")):
            raise ColmapParseError(
                f"{self.path.name}:{self.line_number} 줄 길이가 허용 한도를 초과했습니다."
            )
        return line.rstrip("\r\n")

    def next_data_line(self) -> str | None:
        """주석과 빈 줄을 건너뛴 다음 COLMAP 데이터 줄을 반환합니다."""
        while True:
            line = self.read_raw_line()
            if line is None:
                return None
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            return stripped


def _parse_error(reader: _TextLineReader, message: str) -> ColmapParseError:
    """현재 파일·줄 번호가 포함된 사용자용 parse 오류를 만듭니다."""
    return ColmapParseError(f"{reader.path.name}:{reader.line_number} {message}")


def _parse_integer(
    token: str,
    *,
    minimum: int,
    maximum: int,
    label: str,
    reader: _TextLineReader,
) -> int:
    """기본 정수 변환 후 허용 범위를 검사합니다."""
    try:
        value = int(token)
    except ValueError as error:
        raise _parse_error(reader, f"{label}은 정수여야 합니다: {token}") from error
    if value < minimum or value > maximum:
        raise _parse_error(reader, f"{label} 범위가 올바르지 않습니다: {token}")
    return value


def _parse_float(token: str, label: str, reader: _TextLineReader) -> float:
    """COLMAP text의 실수 필드를 Python float로 변환합니다."""
    try:
        return float(token)
    except ValueError as error:
        raise _parse_error(reader, f"{label}이 실수가 아닙니다: {token}") from error


def _parse_sensor_type(token: str, reader: _TextLineReader) -> str:
    """공식 COLMAP 센서 타입 문자열만 허용합니다."""
    if token not in _SENSOR_TYPES:
        raise _parse_error(reader, f"지원하지 않는 센서 타입입니다: {token}")
    return token


def _freeze_buffer(buffer: array, dtype: np.dtype, shape: tuple[int, ...]) -> np.ndarray:
    """내부 array 버퍼를 독립적인 연속 NumPy 배열로 변환합니다."""
    return np.ascontiguousarray(np.asarray(buffer, dtype=dtype).copy().reshape(shape))


def _ensure_record_limit(count: int, label: str, reader: _TextLineReader) -> None:
    """텍스트 파일이 과도한 레코드를 누적하지 못하게 합니다."""
    if count > MAX_RECORD_COUNT:
        raise _parse_error(reader, f"{label} 레코드 수가 허용 한도를 초과했습니다.")


def read_cameras_text(path: str | Path) -> dict[int, CameraRecord]:
    """cameras.txt를 카메라 ID 기반 불변 레코드 사전으로 읽습니다."""
    text_path = Path(path)
    LOGGER.info("event=text_read_started stage=cameras path=%s", text_path)
    cameras: dict[int, CameraRecord] = {}
    with _TextLineReader(text_path) as reader:
        while (line := reader.next_data_line()) is not None:
            tokens = line.split()
            if len(tokens) < 4:
                raise _parse_error(reader, "카메라 레코드 필드 수가 부족합니다.")
            camera_id = _parse_integer(
                tokens[0], minimum=0, maximum=_UINT32_MAX, label="CAMERA_ID", reader=reader
            )
            model = camera_model_from_name(tokens[1])
            width = _parse_integer(
                tokens[2], minimum=0, maximum=_UINT64_MAX, label="카메라 너비", reader=reader
            )
            height = _parse_integer(
                tokens[3], minimum=0, maximum=_UINT64_MAX, label="카메라 높이", reader=reader
            )
            if len(tokens) != 4 + model.parameter_count:
                raise _parse_error(
                    reader,
                    f"{model.name} 카메라 파라미터 수는 {model.parameter_count}개여야 합니다.",
                )
            if camera_id in cameras:
                raise _parse_error(reader, f"중복 CAMERA_ID가 있습니다: {camera_id}")
            cameras[camera_id] = CameraRecord(
                camera_id=camera_id,
                model_id=model.model_id,
                model_name=model.name,
                width=width,
                height=height,
                params_float64=np.asarray(
                    [_parse_float(token, "카메라 파라미터", reader) for token in tokens[4:]],
                    dtype=np.float64,
                ),
            )
            _ensure_record_limit(len(cameras), "카메라", reader)
    LOGGER.info("event=text_read_completed stage=cameras count=%d", len(cameras))
    return cameras


def read_images_text(path: str | Path) -> ImageTable:
    """images.txt의 두 줄 레코드를 이미지·points2D 연속 테이블로 읽습니다."""
    text_path = Path(path)
    LOGGER.info("event=text_read_started stage=images path=%s", text_path)
    image_records: dict[int, ImageRecord] = {}
    offsets = array("Q")
    xy_values = array("d")
    point3d_ids = array("Q")
    point2d_total = 0
    with _TextLineReader(text_path) as reader:
        while (metadata_line := reader.next_data_line()) is not None:
            # 앞의 9개 고정 필드만 분리해 공백이 포함된 이미지 이름 전체를 보존합니다.
            metadata = metadata_line.split(maxsplit=9)
            if len(metadata) != 10:
                raise _parse_error(reader, "이미지 메타데이터는 정확히 10개 필드여야 합니다.")
            image_id = _parse_integer(
                metadata[0], minimum=0, maximum=_UINT32_MAX, label="IMAGE_ID", reader=reader
            )
            pose = np.asarray(
                [_parse_float(token, "이미지 자세", reader) for token in metadata[1:8]],
                dtype=np.float64,
            )
            camera_id = _parse_integer(
                metadata[8], minimum=0, maximum=_UINT32_MAX, label="CAMERA_ID", reader=reader
            )
            name = metadata[9]
            if not name:
                raise _parse_error(reader, "이미지 이름이 비어 있습니다.")
            points_line = reader.read_raw_line()
            if points_line is None:
                raise _parse_error(reader, "이미지 points2D 줄이 누락되었습니다.")
            point_tokens = points_line.strip().split()
            if points_line.strip().startswith("#"):
                raise _parse_error(reader, "이미지 points2D 줄에 주석만 둘 수 없습니다.")
            if len(point_tokens) % 3:
                raise _parse_error(reader, "이미지 points2D는 X Y POINT3D_ID의 3개 단위여야 합니다.")
            point2d_count = len(point_tokens) // 3
            if point2d_total + point2d_count > MAX_VARIABLE_ITEM_COUNT:
                raise _parse_error(reader, "points2D 총개수가 허용 한도를 초과했습니다.")
            if image_id in image_records:
                raise _parse_error(reader, f"중복 IMAGE_ID가 있습니다: {image_id}")
            for token_index in range(0, len(point_tokens), 3):
                xy_values.append(_parse_float(point_tokens[token_index], "points2D X", reader))
                xy_values.append(_parse_float(point_tokens[token_index + 1], "points2D Y", reader))
                point_id_token = point_tokens[token_index + 2]
                point_id = _parse_integer(
                    point_id_token,
                    minimum=-1,
                    maximum=_UINT64_MAX,
                    label="points2D POINT3D_ID",
                    reader=reader,
                )
                point3d_ids.append(INVALID_POINT3D_ID if point_id == -1 else point_id)
            image_records[image_id] = ImageRecord(
                image_id=image_id,
                camera_id=camera_id,
                name=name,
                cam_from_world=pose,
            )
            offsets.append(point2d_total)
            point2d_total += point2d_count
            _ensure_record_limit(len(image_records), "이미지", reader)
        offsets.append(point2d_total)
    point2d_xy = _freeze_buffer(xy_values, np.dtype(np.float64), (len(point3d_ids), 2))
    table = ImageTable(
        image_records=image_records,
        point2d_offsets=_freeze_buffer(offsets, np.dtype(np.uint64), (len(offsets),)),
        point2d_xy=point2d_xy,
        point2d_point3d_ids=_freeze_buffer(
            point3d_ids,
            np.dtype(np.uint64),
            (len(point3d_ids),),
        ),
    )
    LOGGER.info(
        "event=text_read_completed stage=images image_count=%d point2d_count=%d",
        table.count,
        table.point2d_xy.shape[0],
    )
    return table


def read_points3d_text(path: str | Path) -> PointTable:
    """points3D.txt를 원본 ID와 관측 트랙을 보존한 연속 배열로 읽습니다."""
    text_path = Path(path)
    LOGGER.info("event=text_read_started stage=points3D path=%s", text_path)
    point_ids = array("Q")
    positions = array("d")
    colors = array("B")
    errors = array("d")
    track_offsets = array("Q")
    track_image_ids = array("Q")
    track_point2d_indices = array("I")
    seen_point_ids: set[int] = set()
    track_total = 0
    with _TextLineReader(text_path) as reader:
        while (line := reader.next_data_line()) is not None:
            tokens = line.split()
            if len(tokens) < 8:
                raise _parse_error(reader, "3D 점 레코드 필드 수가 부족합니다.")
            if (len(tokens) - 8) % 2:
                raise _parse_error(reader, "3D 점 트랙은 IMAGE_ID POINT2D_IDX의 2개 단위여야 합니다.")
            point_id = _parse_integer(
                tokens[0], minimum=0, maximum=_UINT64_MAX, label="POINT3D_ID", reader=reader
            )
            if point_id in seen_point_ids:
                raise _parse_error(reader, f"중복 POINT3D_ID가 있습니다: {point_id}")
            seen_point_ids.add(point_id)
            point_ids.append(point_id)
            positions.extend(
                _parse_float(token, "POINT3D 좌표", reader) for token in tokens[1:4]
            )
            colors.extend(
                _parse_integer(token, minimum=0, maximum=255, label="POINT3D RGB", reader=reader)
                for token in tokens[4:7]
            )
            errors.append(_parse_float(tokens[7], "POINT3D 재투영 오차", reader))
            track_length = (len(tokens) - 8) // 2
            if track_total + track_length > MAX_VARIABLE_ITEM_COUNT:
                raise _parse_error(reader, "POINT3D 관측 트랙 총개수가 허용 한도를 초과했습니다.")
            track_offsets.append(track_total)
            for token_index in range(8, len(tokens), 2):
                track_image_ids.append(
                    _parse_integer(
                        tokens[token_index],
                        minimum=0,
                        maximum=_UINT32_MAX,
                        label="트랙 IMAGE_ID",
                        reader=reader,
                    )
                )
                track_point2d_indices.append(
                    _parse_integer(
                        tokens[token_index + 1],
                        minimum=0,
                        maximum=_UINT32_MAX,
                        label="트랙 POINT2D_IDX",
                        reader=reader,
                    )
                )
            track_total += track_length
            _ensure_record_limit(len(point_ids), "3D 점", reader)
        track_offsets.append(track_total)
    point_count = len(point_ids)
    display_dtype = np.dtype(np.uint32 if point_count <= _UINT32_MAX else np.uint64)
    table = PointTable(
        point_ids=_freeze_buffer(point_ids, np.dtype(np.uint64), (point_count,)),
        positions=_freeze_buffer(positions, np.dtype(np.float64), (point_count, 3)),
        colors=_freeze_buffer(colors, np.dtype(np.uint8), (point_count, 3)),
        errors=_freeze_buffer(errors, np.dtype(np.float64), (point_count,)),
        track_offsets=_freeze_buffer(
            track_offsets,
            np.dtype(np.uint64),
            (point_count + 1,),
        ),
        track_image_ids=_freeze_buffer(
            track_image_ids,
            np.dtype(np.uint64),
            (track_total,),
        ),
        track_point2d_indices=_freeze_buffer(
            track_point2d_indices,
            np.dtype(np.uint32),
            (track_total,),
        ),
        display_indices=np.arange(point_count, dtype=display_dtype),
    )
    LOGGER.info(
        "event=text_read_completed stage=points3D point_count=%d track_count=%d",
        table.count,
        table.track_image_ids.shape[0],
    )
    return table


def read_rigs_text(path: str | Path) -> dict[int, RigRecord]:
    """선택 rigs.txt를 리그 ID 기반 레코드 사전으로 읽습니다."""
    text_path = Path(path)
    LOGGER.info("event=text_read_started stage=rigs path=%s", text_path)
    rigs: dict[int, RigRecord] = {}
    with _TextLineReader(text_path) as reader:
        while (line := reader.next_data_line()) is not None:
            tokens = line.split()
            if len(tokens) < 2:
                raise _parse_error(reader, "리그 레코드 필드 수가 부족합니다.")
            rig_id = _parse_integer(
                tokens[0], minimum=0, maximum=_UINT32_MAX, label="RIG_ID", reader=reader
            )
            sensor_count = _parse_integer(
                tokens[1], minimum=0, maximum=MAX_VARIABLE_ITEM_COUNT, label="리그 센서 수", reader=reader
            )
            token_index = 2
            reference_sensor: RigSensorRecord | None = None
            sensors: list[RigSensorRecord] = []
            if sensor_count:
                if len(tokens) < token_index + 2:
                    raise _parse_error(reader, "리그 기준 센서 필드가 부족합니다.")
                reference_sensor = RigSensorRecord(
                    sensor_type=_parse_sensor_type(tokens[token_index], reader),
                    sensor_id=_parse_integer(
                        tokens[token_index + 1],
                        minimum=0,
                        maximum=_UINT32_MAX,
                        label="기준 센서 ID",
                        reader=reader,
                    ),
                )
                sensors.append(reference_sensor)
                token_index += 2
            for _sensor_index in range(max(sensor_count - 1, 0)):
                if len(tokens) < token_index + 3:
                    raise _parse_error(reader, "리그 센서 필드가 부족합니다.")
                sensor_type = _parse_sensor_type(tokens[token_index], reader)
                sensor_id = _parse_integer(
                    tokens[token_index + 1],
                    minimum=0,
                    maximum=_UINT32_MAX,
                    label="리그 센서 ID",
                    reader=reader,
                )
                has_pose = _parse_integer(
                    tokens[token_index + 2], minimum=0, maximum=1, label="센서 자세 플래그", reader=reader
                )
                token_index += 3
                sensor_from_rig: np.ndarray | None = None
                if has_pose:
                    if len(tokens) < token_index + 7:
                        raise _parse_error(reader, "리그 센서 자세 필드가 부족합니다.")
                    sensor_from_rig = np.asarray(
                        [_parse_float(token, "sensor_from_rig", reader) for token in tokens[token_index:token_index + 7]],
                        dtype=np.float64,
                    )
                    token_index += 7
                sensors.append(
                    RigSensorRecord(
                        sensor_type=sensor_type,
                        sensor_id=sensor_id,
                        sensor_from_rig=sensor_from_rig,
                    )
                )
            if token_index != len(tokens):
                raise _parse_error(reader, "리그 레코드에 해석할 수 없는 추가 필드가 있습니다.")
            if rig_id in rigs:
                raise _parse_error(reader, f"중복 RIG_ID가 있습니다: {rig_id}")
            rigs[rig_id] = RigRecord(rig_id, reference_sensor, tuple(sensors))
            _ensure_record_limit(len(rigs), "리그", reader)
    LOGGER.info("event=text_read_completed stage=rigs count=%d", len(rigs))
    return rigs


def read_frames_text(path: str | Path) -> dict[int, FrameRecord]:
    """선택 frames.txt를 프레임 ID 기반 레코드 사전으로 읽습니다."""
    text_path = Path(path)
    LOGGER.info("event=text_read_started stage=frames path=%s", text_path)
    frames: dict[int, FrameRecord] = {}
    with _TextLineReader(text_path) as reader:
        while (line := reader.next_data_line()) is not None:
            tokens = line.split()
            if len(tokens) < 10:
                raise _parse_error(reader, "프레임 레코드 필드 수가 부족합니다.")
            frame_id = _parse_integer(
                tokens[0], minimum=0, maximum=_UINT32_MAX, label="FRAME_ID", reader=reader
            )
            rig_id = _parse_integer(
                tokens[1], minimum=0, maximum=_UINT32_MAX, label="RIG_ID", reader=reader
            )
            rig_from_world = np.asarray(
                [_parse_float(token, "rig_from_world", reader) for token in tokens[2:9]],
                dtype=np.float64,
            )
            data_count = _parse_integer(
                tokens[9], minimum=0, maximum=MAX_VARIABLE_ITEM_COUNT, label="프레임 데이터 ID 수", reader=reader
            )
            if len(tokens) != 10 + data_count * 3:
                raise _parse_error(reader, "프레임 데이터 ID는 SENSOR_TYPE SENSOR_ID DATA_ID의 3개 단위여야 합니다.")
            data_ids = tuple(
                FrameDataRecord(
                    sensor_type=_parse_sensor_type(tokens[token_index], reader),
                    sensor_id=_parse_integer(
                        tokens[token_index + 1],
                        minimum=0,
                        maximum=_UINT32_MAX,
                        label="프레임 센서 ID",
                        reader=reader,
                    ),
                    data_id=_parse_integer(
                        tokens[token_index + 2],
                        minimum=0,
                        maximum=_UINT64_MAX,
                        label="프레임 데이터 ID",
                        reader=reader,
                    ),
                )
                for token_index in range(10, len(tokens), 3)
            )
            if frame_id in frames:
                raise _parse_error(reader, f"중복 FRAME_ID가 있습니다: {frame_id}")
            frames[frame_id] = FrameRecord(
                frame_id=frame_id,
                rig_id=rig_id,
                rig_from_world=rig_from_world,
                data_ids=data_ids,
            )
            _ensure_record_limit(len(frames), "프레임", reader)
    LOGGER.info("event=text_read_completed stage=frames count=%d", len(frames))
    return frames
