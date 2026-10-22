"""COLMAP sparse model을 TIMER 청크에 맞춰 순수 Python으로 증분 파싱합니다."""

from __future__ import annotations

from array import array
from dataclasses import dataclass
import gc
import math
from pathlib import Path
from time import perf_counter
from typing import Any, Final, Iterator

import numpy as np

import logging
from .binary_reader import (
    MAX_VARIABLE_ITEM_COUNT as _BINARY_MAX_VARIABLE_ITEM_COUNT,
    _BinaryCursor,
    _POINT2D_BINARY_DTYPE,
    _TRACK_BINARY_DTYPE,
    _allocate_array,
    _append_native_values,
    _array_from_buffer,
    _checked_byte_count,
    _read_pose,
    _read_record_count,
    _read_sensor_type,
    _read_variable_count,
)
from .camera_models import camera_model_from_id, camera_model_from_name
from .discovery import SparseModelLayout
from .errors import ColmapParseError
from .text_reader import (
    MAX_RECORD_COUNT as _TEXT_MAX_RECORD_COUNT,
    MAX_VARIABLE_ITEM_COUNT as _TEXT_MAX_VARIABLE_ITEM_COUNT,
    _TextLineReader,
    _ensure_record_limit,
    _freeze_buffer,
    _parse_error,
    _parse_float,
    _parse_integer,
    _parse_sensor_type,
)
from .types import (
    CameraRecord,
    ColmapModelData,
    FrameDataRecord,
    FrameRecord,
    ImageRecord,
    ImageTable,
    INVALID_POINT3D_ID,
    PointTable,
    RigRecord,
    RigSensorRecord,
)
from .reference_preview import ColmapReferencePreview, ReferencePointCollector


LOGGER = logging.getLogger(__name__)

STAGE_RIGS: Final = "RIGS"
STAGE_CAMERAS: Final = "CAMERAS"
STAGE_FRAMES: Final = "FRAMES"
STAGE_IMAGES: Final = "IMAGES"
STAGE_POINTS3D: Final = "POINTS3D"
STAGE_COMPLETED: Final = "COMPLETED"
STAGE_CANCELLED: Final = "CANCELLED"
STAGE_FAILED: Final = "FAILED"
_TERMINAL_STAGES: Final = frozenset(
    {STAGE_COMPLETED, STAGE_CANCELLED, STAGE_FAILED}
)
DEFAULT_MAX_RECORDS_PER_STEP: Final = 256
_UINT32_MAX: Final = int(np.iinfo(np.uint32).max)
_UINT64_MAX: Final = int(np.iinfo(np.uint64).max)


@dataclass(frozen=True, slots=True)
class IncrementalReadProgress:
    """증분 파서의 현재 단계와 레코드 진행 상태를 전달합니다."""

    stage: str
    current_count: int
    total_count: int | None
    fraction: float | None
    is_completed: bool
    is_cancelled: bool
    is_failed: bool


class _IncrementalStage:
    """한 COLMAP 파일을 레코드 단위로 읽는 내부 단계 공통 계약입니다."""

    def __init__(self, stage: str, path: Path) -> None:
        """단계 이름과 읽을 파일 경로를 보관합니다."""
        self.stage = stage
        self.path = path
        self.current_count = 0
        self.total_count: int | None = None
        self.done = False
        self._opened = False

    @property
    def has_open_file(self) -> bool:
        """현재 단계가 실제 파일 핸들을 보유하는지 반환합니다."""
        return self._opened

    def open(self) -> None:
        """첫 레코드 직전에 파일을 열고 형식별 준비를 수행합니다."""
        if self._opened:
            return
        self._open()
        self._opened = True

    def read_one(self) -> bool:
        """레코드 하나를 읽고 실제 처리 여부를 반환합니다."""
        if self.done:
            return False
        self.open()
        return self._read_one()

    def close(self) -> None:
        """완료·취소·오류 경로에서 파일 핸들을 한 번만 닫습니다."""
        if not self._opened:
            return
        try:
            self._close()
        finally:
            self._opened = False

    def _open(self) -> None:
        """형식별 파일 열기와 헤더 처리를 구현합니다."""
        raise NotImplementedError

    def _read_one(self) -> bool:
        """형식별 레코드 하나 읽기를 구현합니다."""
        raise NotImplementedError

    def _close(self) -> None:
        """형식별 파일 핸들 닫기를 구현합니다."""
        raise NotImplementedError

    def result(self) -> Any:
        """완료된 단계가 만든 불변 데이터 계약을 반환합니다."""
        raise NotImplementedError


class _BinaryStage(_IncrementalStage):
    """little-endian binary 파일을 열린 cursor 상태로 이어서 읽습니다."""

    def __init__(self, stage: str, path: Path, label: str, minimum_record_bytes: int) -> None:
        """binary 레코드 수 검증에 필요한 최소 바이트 정보를 준비합니다."""
        super().__init__(stage, path)
        self._label = label
        self._minimum_record_bytes = minimum_record_bytes
        self.cursor: _BinaryCursor | None = None

    def _open(self) -> None:
        """cursor를 열고 파일 헤더의 전체 레코드 수를 읽습니다."""
        cursor = _BinaryCursor(self.path)
        try:
            cursor.__enter__()
            self.cursor = cursor
            self.total_count = _read_record_count(
                cursor,
                self._label,
                self._minimum_record_bytes,
            )
        except Exception:
            cursor.__exit__(None, None, None)
            raise

    def _finish_if_last_record(self) -> None:
        """선언된 마지막 레코드 뒤의 trailing data를 즉시 검사합니다."""
        if self.total_count is None or self.current_count != self.total_count:
            return
        cursor = self._require_cursor()
        cursor.ensure_consumed()
        self.done = True

    def _require_cursor(self) -> _BinaryCursor:
        """열린 binary cursor를 반환하고 내부 상태 오류를 막습니다."""
        if self.cursor is None:
            raise RuntimeError("열리지 않은 binary 증분 단계를 사용했습니다.")
        return self.cursor

    def _close(self) -> None:
        """cursor 컨텍스트를 종료해 예외 상황도 포함하여 파일을 닫습니다."""
        if self.cursor is not None:
            self.cursor.__exit__(None, None, None)
            self.cursor = None


class _BinaryCamerasStage(_BinaryStage):
    """cameras.bin 레코드를 하나씩 카메라 사전에 추가합니다."""

    def __init__(self, path: Path) -> None:
        """카메라 binary 단계의 누적 사전을 준비합니다."""
        super().__init__(STAGE_CAMERAS, path, "카메라", 24)
        self.cameras: dict[int, CameraRecord] = {}

    def _read_one(self) -> bool:
        """카메라 레코드 하나를 해석하고 중복 ID를 거부합니다."""
        if self.total_count is not None and self.current_count >= self.total_count:
            self._finish_if_last_record()
            return False
        cursor = self._require_cursor()
        camera_id = cursor.read_uint32("CAMERA_ID")
        model = camera_model_from_id(cursor.read_int32("CAMERA_MODEL_ID"))
        width = cursor.read_uint64("카메라 너비")
        height = cursor.read_uint64("카메라 높이")
        if camera_id in self.cameras:
            raise ColmapParseError(f"{self.path.name}에 중복 CAMERA_ID가 있습니다: {camera_id}")
        self.cameras[camera_id] = CameraRecord(
            camera_id=camera_id,
            model_id=model.model_id,
            model_name=model.name,
            width=width,
            height=height,
            params_float64=cursor.read_float64_vector(model.parameter_count, "카메라 파라미터"),
        )
        self.current_count += 1
        self._finish_if_last_record()
        return True

    def result(self) -> dict[int, CameraRecord]:
        """완료된 카메라 ID 사전을 반환합니다."""
        if not self.done:
            raise RuntimeError("완료되지 않은 cameras 단계를 결과로 요청했습니다.")
        return self.cameras


class _BinaryImagesStage(_BinaryStage):
    """images.bin 레코드를 하나씩 이미지·points2D 테이블에 누적합니다."""

    def __init__(self, path: Path, *, discard_observations: bool = False) -> None:
        """전체 또는 참고용 정책에 맞춰 이미지와 관측 저장소를 준비합니다."""
        super().__init__(STAGE_IMAGES, path, "이미지", 73)
        self.discard_observations = discard_observations
        self.image_records: dict[int, ImageRecord] = {}
        self.point2d_offsets: np.ndarray | None = None
        self.point2d_xy_buffer = array("d")
        self.point2d_id_buffer = array("Q")
        self.point2d_total = 0

    def _open(self) -> None:
        """이미지 수를 읽은 뒤 offset 배열만 먼저 안전하게 할당합니다."""
        super()._open()
        if self.total_count is None:
            raise RuntimeError("images binary 레코드 수가 준비되지 않았습니다.")
        self.point2d_offsets = _allocate_array(
            (self.total_count + 1,),
            np.dtype(np.uint64),
            "points2D offset",
        )
        if self.total_count == 0:
            self.point2d_offsets[0] = 0

    def _read_one(self) -> bool:
        """이미지 관측은 정책에 따라 누적하거나 블록 검사 후 폐기합니다."""
        if self.total_count is not None and self.current_count >= self.total_count:
            self._finish_if_last_record()
            return False
        cursor = self._require_cursor()
        offsets = self._require_offsets()
        image_id = cursor.read_uint32("IMAGE_ID")
        pose = _read_pose(cursor, "이미지 cam_from_world")
        camera_id = cursor.read_uint32("이미지 CAMERA_ID")
        name = cursor.read_null_terminated_utf8("이미지 이름")
        point2d_count = _read_variable_count(
            cursor,
            "points2D",
            _POINT2D_BINARY_DTYPE.itemsize,
        )
        if self.point2d_total + point2d_count > _BINARY_MAX_VARIABLE_ITEM_COUNT:
            raise ColmapParseError("images.bin의 points2D 총개수가 허용 한도를 초과했습니다.")
        remaining = point2d_count
        while remaining:
            count = min(4096, remaining) if self.discard_observations else remaining
            raw = cursor.read_exact(_checked_byte_count(count, _POINT2D_BINARY_DTYPE.itemsize, "points2D"), "points2D")
            values = np.frombuffer(raw, dtype=_POINT2D_BINARY_DTYPE, count=count)
            if self.discard_observations:
                if not np.isfinite(values["xy"]).all():
                    raise ColmapParseError(f"IMAGE_ID {image_id}의 points2D 좌표가 유한하지 않습니다.")
            else:
                _append_native_values(self.point2d_xy_buffer, values["xy"], np.dtype(np.float64))
                _append_native_values(self.point2d_id_buffer, values["point3d_id"], np.dtype(np.uint64))
            remaining -= count
        if image_id in self.image_records:
            raise ColmapParseError(f"{self.path.name}에 중복 IMAGE_ID가 있습니다: {image_id}")
        self.image_records[image_id] = ImageRecord(
            image_id=image_id,
            camera_id=camera_id,
            name=name,
            cam_from_world=pose,
        )
        offsets[self.current_count] = 0 if self.discard_observations else self.point2d_total
        self.point2d_total += point2d_count
        self.current_count += 1
        if self.done:
            raise RuntimeError("완료된 images binary 단계를 다시 읽으려 했습니다.")
        if self.total_count is not None and self.current_count == self.total_count:
            offsets[-1] = 0 if self.discard_observations else self.point2d_total
        self._finish_if_last_record()
        return True

    def _require_offsets(self) -> np.ndarray:
        """초기화된 points2D offset 배열을 반환합니다."""
        if self.point2d_offsets is None:
            raise RuntimeError("images offset 배열이 초기화되지 않았습니다.")
        return self.point2d_offsets

    def result(self) -> ImageTable:
        """누적된 이미지와 points2D 버퍼를 불변 테이블로 변환합니다."""
        if not self.done:
            raise RuntimeError("완료되지 않은 images 단계를 결과로 요청했습니다.")
        point_count = len(self.point2d_id_buffer)
        return ImageTable(
            image_records=self.image_records,
            point2d_offsets=self._require_offsets(),
            point2d_xy=_array_from_buffer(
                self.point2d_xy_buffer,
                np.dtype(np.float64),
                (point_count, 2),
            ),
            point2d_point3d_ids=_array_from_buffer(
                self.point2d_id_buffer,
                np.dtype(np.uint64),
                (point_count,),
            ),
        )


class _BinaryPointsStage(_BinaryStage):
    """points3D.bin을 한 POINT3D 레코드씩 누적해 전체 파일 일괄 읽기를 피합니다."""

    def __init__(self, path: Path, *, preview: ReferencePointCollector | None = None) -> None:
        """전체 점 누적 또는 참고 표본 누적에 필요한 저장소를 준비합니다."""
        super().__init__(STAGE_POINTS3D, path, "3D 점", 51)
        self.preview = preview
        self.point_ids: np.ndarray | None = None
        self.positions: np.ndarray | None = None
        self.colors: np.ndarray | None = None
        self.errors: np.ndarray | None = None
        self.track_offsets: np.ndarray | None = None
        self.track_image_id_buffer = array("Q")
        self.track_point2d_index_buffer = array("I")
        self.track_total = 0

    def _open(self) -> None:
        """선언된 점 수에 맞는 고정 배열만 안전하게 할당합니다."""
        super()._open()
        if self.total_count is None:
            raise RuntimeError("points3D binary 레코드 수가 준비되지 않았습니다.")
        point_count = self.total_count
        if self.preview is not None:
            return
        self.point_ids = _allocate_array((point_count,), np.dtype(np.uint64), "POINT3D_ID")
        self.positions = _allocate_array((point_count, 3), np.dtype(np.float64), "점 좌표")
        self.colors = _allocate_array((point_count, 3), np.dtype(np.uint8), "점 RGB")
        self.errors = _allocate_array((point_count,), np.dtype(np.float64), "점 오차")
        self.track_offsets = _allocate_array(
            (point_count + 1,),
            np.dtype(np.uint64),
            "점 트랙 offset",
        )
        if point_count == 0:
            self.track_offsets[0] = 0

    def _read_one(self) -> bool:
        """점은 전체 또는 대표점 저장소에 넣고 참고 관측은 검사 후 폐기합니다."""
        if self.total_count is not None and self.current_count >= self.total_count:
            self._finish_if_last_record()
            return False
        cursor = self._require_cursor()
        point_index = self.current_count
        point_id = cursor.read_uint64("POINT3D_ID")
        xyz = cursor.read_float64_vector(3, "POINT3D XYZ")
        rgb = np.frombuffer(
            cursor.read_exact(3, "POINT3D RGB"),
            dtype=np.uint8,
            count=3,
        )
        error = cursor.read_float64("POINT3D 재투영 오차")
        if self.preview is not None:
            self.preview.add(point_id, xyz, rgb, error)
        else:
            point_ids, positions, colors, errors, offsets = self._require_arrays()
            point_ids[point_index], positions[point_index], colors[point_index], errors[point_index] = point_id, xyz, rgb, error
        track_length = _read_variable_count(
            cursor,
            "POINT3D 관측 트랙",
            _TRACK_BINARY_DTYPE.itemsize,
        )
        if self.track_total + track_length > _BINARY_MAX_VARIABLE_ITEM_COUNT:
            raise ColmapParseError("points3D.bin의 관측 트랙 총개수가 허용 한도를 초과했습니다.")
        remaining = track_length
        while remaining:
            count = min(4096, remaining) if self.preview is not None else remaining
            raw = cursor.read_exact(_checked_byte_count(count, _TRACK_BINARY_DTYPE.itemsize, "POINT3D 관측 트랙"), "POINT3D 관측 트랙")
            track_values = np.frombuffer(raw, dtype=_TRACK_BINARY_DTYPE, count=count)
            if self.preview is not None:
                if np.any(track_values["image_id"] == 0):
                    raise ColmapParseError(f"POINT3D_ID {point_id}의 트랙 IMAGE_ID가 올바르지 않습니다.")
            else:
                _append_native_values(self.track_image_id_buffer, track_values["image_id"], np.dtype(np.uint64))
                _append_native_values(self.track_point2d_index_buffer, track_values["point2d_idx"], np.dtype(np.uint32))
            remaining -= count
        if self.preview is None:
            offsets[point_index] = self.track_total
        self.track_total += track_length
        self.current_count += 1
        if self.preview is None and self.total_count is not None and self.current_count == self.total_count:
            offsets[-1] = self.track_total
        self._finish_if_last_record()
        return True

    def _require_arrays(self) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        """초기화된 POINT3D 누적 배열을 순서대로 반환합니다."""
        values = (self.point_ids, self.positions, self.colors, self.errors, self.track_offsets)
        if any(value is None for value in values):
            raise RuntimeError("points3D 누적 배열이 초기화되지 않았습니다.")
        return values  # type: ignore[return-value]

    def result(self) -> PointTable:
        """전체 누적 또는 검증된 참고 표본을 불변 점 테이블로 반환합니다."""
        if not self.done:
            raise RuntimeError("완료되지 않은 points3D 단계를 결과로 요청했습니다.")
        if self.preview is not None:
            return self.preview.result()
        point_ids, positions, colors, errors, offsets = self._require_arrays()
        point_count = self.current_count
        display_dtype = np.dtype(
            np.uint32 if point_count <= _UINT32_MAX else np.uint64
        )
        return PointTable(
            point_ids=point_ids,
            positions=positions,
            colors=colors,
            errors=errors,
            track_offsets=offsets,
            track_image_ids=_array_from_buffer(
                self.track_image_id_buffer,
                np.dtype(np.uint64),
                (self.track_total,),
            ),
            track_point2d_indices=_array_from_buffer(
                self.track_point2d_index_buffer,
                np.dtype(np.uint32),
                (self.track_total,),
            ),
            display_indices=np.arange(point_count, dtype=display_dtype),
        )


class _BinaryRigsStage(_BinaryStage):
    """rigs.bin 레코드를 하나씩 리그 사전에 추가합니다."""

    def __init__(self, path: Path) -> None:
        """리그 binary 단계의 누적 사전을 준비합니다."""
        super().__init__(STAGE_RIGS, path, "리그", 8)
        self.rigs: dict[int, RigRecord] = {}

    def _read_one(self) -> bool:
        """리그 하나와 소속 센서를 읽고 중복 리그 ID를 거부합니다."""
        if self.total_count is not None and self.current_count >= self.total_count:
            self._finish_if_last_record()
            return False
        cursor = self._require_cursor()
        rig_id = cursor.read_uint32("RIG_ID")
        sensor_count = cursor.read_uint32("리그 센서 수")
        if sensor_count > _BINARY_MAX_VARIABLE_ITEM_COUNT:
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
        if rig_id in self.rigs:
            raise ColmapParseError(f"{self.path.name}에 중복 RIG_ID가 있습니다: {rig_id}")
        self.rigs[rig_id] = RigRecord(
            rig_id=rig_id,
            reference_sensor=reference_sensor,
            sensors=tuple(sensors),
        )
        self.current_count += 1
        self._finish_if_last_record()
        return True

    def result(self) -> dict[int, RigRecord]:
        """완료된 리그 ID 사전을 반환합니다."""
        if not self.done:
            raise RuntimeError("완료되지 않은 rigs 단계를 결과로 요청했습니다.")
        return self.rigs


class _BinaryFramesStage(_BinaryStage):
    """frames.bin 레코드를 하나씩 프레임 사전에 추가합니다."""

    def __init__(self, path: Path) -> None:
        """프레임 binary 단계의 누적 사전을 준비합니다."""
        super().__init__(STAGE_FRAMES, path, "프레임", 68)
        self.frames: dict[int, FrameRecord] = {}

    def _read_one(self) -> bool:
        """프레임 하나와 센서 데이터 연결을 읽습니다."""
        if self.total_count is not None and self.current_count >= self.total_count:
            self._finish_if_last_record()
            return False
        cursor = self._require_cursor()
        frame_id = cursor.read_uint32("FRAME_ID")
        rig_id = cursor.read_uint32("프레임 RIG_ID")
        rig_from_world = _read_pose(cursor, "rig_from_world")
        data_count = cursor.read_uint32("프레임 데이터 ID 수")
        if data_count > _BINARY_MAX_VARIABLE_ITEM_COUNT:
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
        if frame_id in self.frames:
            raise ColmapParseError(f"{self.path.name}에 중복 FRAME_ID가 있습니다: {frame_id}")
        self.frames[frame_id] = FrameRecord(
            frame_id=frame_id,
            rig_id=rig_id,
            rig_from_world=rig_from_world,
            data_ids=data_ids,
        )
        self.current_count += 1
        self._finish_if_last_record()
        return True

    def result(self) -> dict[int, FrameRecord]:
        """완료된 프레임 ID 사전을 반환합니다."""
        if not self.done:
            raise RuntimeError("완료되지 않은 frames 단계를 결과로 요청했습니다.")
        return self.frames


class _TextStage(_IncrementalStage):
    """UTF-8 text 파일을 줄 기반 레코드 경계에서 이어 읽습니다."""

    def __init__(self, stage: str, path: Path) -> None:
        """text 단계의 아직 열지 않은 줄 reader를 준비합니다."""
        super().__init__(stage, path)
        self.reader: _TextLineReader | None = None

    def _open(self) -> None:
        """UTF-8 strict reader를 열고 text 총량 미확정을 표시합니다."""
        reader = _TextLineReader(self.path)
        try:
            reader.__enter__()
            self.reader = reader
            self.total_count = None
        except Exception:
            reader.__exit__(None, None, None)
            raise

    def _next_data_line(self) -> str | None:
        """다음 데이터 레코드 줄을 읽고 EOF에서 단계 완료를 기록합니다."""
        if self.done:
            return None
        reader = self._require_reader()
        line = reader.next_data_line()
        if line is None:
            self.done = True
            self.total_count = self.current_count
        return line

    def _require_reader(self) -> _TextLineReader:
        """열린 text reader를 반환하고 내부 상태 오류를 막습니다."""
        if self.reader is None:
            raise RuntimeError("열리지 않은 text 증분 단계를 사용했습니다.")
        return self.reader

    def _close(self) -> None:
        """text reader 컨텍스트를 종료해 파일 핸들을 닫습니다."""
        if self.reader is not None:
            self.reader.__exit__(None, None, None)
            self.reader = None


class _TextCamerasStage(_TextStage):
    """cameras.txt 레코드를 하나씩 카메라 사전에 추가합니다."""

    def __init__(self, path: Path) -> None:
        """카메라 text 단계의 누적 사전을 준비합니다."""
        super().__init__(STAGE_CAMERAS, path)
        self.cameras: dict[int, CameraRecord] = {}

    def _read_one(self) -> bool:
        """카메라 텍스트 레코드 하나를 해석합니다."""
        line = self._next_data_line()
        if line is None:
            return False
        reader = self._require_reader()
        tokens = line.split()
        if len(tokens) < 4:
            raise _parse_error(reader, "카메라 레코드 필드 수가 부족합니다.")
        camera_id = _parse_integer(
            tokens[0],
            minimum=0,
            maximum=_UINT32_MAX,
            label="CAMERA_ID",
            reader=reader,
        )
        model = camera_model_from_name(tokens[1])
        width = _parse_integer(
            tokens[2],
            minimum=0,
            maximum=_UINT64_MAX,
            label="카메라 너비",
            reader=reader,
        )
        height = _parse_integer(
            tokens[3],
            minimum=0,
            maximum=_UINT64_MAX,
            label="카메라 높이",
            reader=reader,
        )
        if len(tokens) != 4 + model.parameter_count:
            raise _parse_error(
                reader,
                f"{model.name} 카메라 파라미터 수는 {model.parameter_count}개여야 합니다.",
            )
        if camera_id in self.cameras:
            raise _parse_error(reader, f"중복 CAMERA_ID가 있습니다: {camera_id}")
        self.cameras[camera_id] = CameraRecord(
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
        self.current_count += 1
        _ensure_record_limit(self.current_count, "카메라", reader)
        return True

    def result(self) -> dict[int, CameraRecord]:
        """완료된 카메라 ID 사전을 반환합니다."""
        if not self.done:
            raise RuntimeError("완료되지 않은 cameras 단계를 결과로 요청했습니다.")
        return self.cameras


class _TextImagesStage(_TextStage):
    """images.txt의 두 줄 레코드를 하나씩 이미지·points2D 버퍼에 누적합니다."""

    def __init__(self, path: Path, *, discard_observations: bool = False) -> None:
        """참고용 입력에서는 관측을 검사한 뒤 보관하지 않도록 준비합니다."""
        super().__init__(STAGE_IMAGES, path)
        self.discard_observations = discard_observations
        self.image_records: dict[int, ImageRecord] = {}
        self.offsets = array("Q")
        self.xy_values = array("d")
        self.point3d_ids = array("Q")
        self.point2d_total = 0
        self._final_offset_written = False

    def _read_one(self) -> bool:
        """이미지 메타데이터와 관측 수치를 검사하고 정책에 맞는 값만 보관합니다."""
        metadata_line = self._next_data_line()
        if metadata_line is None:
            if not self._final_offset_written:
                self.offsets.append(0 if self.discard_observations else self.point2d_total)
                self._final_offset_written = True
            return False
        reader = self._require_reader()
        # 앞의 9개 고정 필드만 분리해 공백이 포함된 이미지 이름 전체를 보존합니다.
        metadata = metadata_line.split(maxsplit=9)
        if len(metadata) != 10:
            raise _parse_error(reader, "이미지 메타데이터는 정확히 10개 필드여야 합니다.")
        image_id = _parse_integer(
            metadata[0],
            minimum=0,
            maximum=_UINT32_MAX,
            label="IMAGE_ID",
            reader=reader,
        )
        pose = np.asarray(
            [_parse_float(token, "이미지 자세", reader) for token in metadata[1:8]],
            dtype=np.float64,
        )
        camera_id = _parse_integer(
            metadata[8],
            minimum=0,
            maximum=_UINT32_MAX,
            label="CAMERA_ID",
            reader=reader,
        )
        name = metadata[9]
        if not name:
            raise _parse_error(reader, "이미지 이름이 비어 있습니다.")
        points_line = reader.read_raw_line()
        if points_line is None:
            raise _parse_error(reader, "이미지 points2D 줄이 누락되었습니다.")
        if points_line.strip().startswith("#"):
            raise _parse_error(reader, "이미지 points2D 줄에 주석만 둘 수 없습니다.")
        point_tokens = points_line.strip().split()
        if len(point_tokens) % 3:
            raise _parse_error(reader, "이미지 points2D는 X Y POINT3D_ID의 3개 단위여야 합니다.")
        point2d_count = len(point_tokens) // 3
        if self.point2d_total + point2d_count > _TEXT_MAX_VARIABLE_ITEM_COUNT:
            raise _parse_error(reader, "points2D 총개수가 허용 한도를 초과했습니다.")
        if image_id in self.image_records:
            raise _parse_error(reader, f"중복 IMAGE_ID가 있습니다: {image_id}")
        preview_xy = array("d")
        for token_index in range(0, len(point_tokens), 3):
            x = _parse_float(point_tokens[token_index], "points2D X", reader)
            y = _parse_float(point_tokens[token_index + 1], "points2D Y", reader)
            point_id = _parse_integer(
                point_tokens[token_index + 2],
                minimum=-1,
                maximum=_UINT64_MAX,
                label="points2D POINT3D_ID",
                reader=reader,
            )
            if not self.discard_observations:
                self.xy_values.extend((x, y))
                self.point3d_ids.append(INVALID_POINT3D_ID if point_id == -1 else point_id)
            else:
                preview_xy.extend((x, y))
                if len(preview_xy) >= 8192:
                    if not np.isfinite(np.frombuffer(preview_xy, dtype=np.float64)).all():
                        raise _parse_error(reader, f"IMAGE_ID {image_id}의 points2D 좌표가 유한하지 않습니다.")
                    preview_xy = array("d")
        if preview_xy and not np.isfinite(np.frombuffer(preview_xy, dtype=np.float64)).all():
            raise _parse_error(reader, f"IMAGE_ID {image_id}의 points2D 좌표가 유한하지 않습니다.")
        self.image_records[image_id] = ImageRecord(
            image_id=image_id,
            camera_id=camera_id,
            name=name,
            cam_from_world=pose,
        )
        self.offsets.append(0 if self.discard_observations else self.point2d_total)
        self.point2d_total += point2d_count
        self.current_count += 1
        _ensure_record_limit(self.current_count, "이미지", reader)
        return True

    def result(self) -> ImageTable:
        """누적된 text 이미지와 points2D 버퍼를 불변 테이블로 변환합니다."""
        if not self.done:
            raise RuntimeError("완료되지 않은 images 단계를 결과로 요청했습니다.")
        if not self._final_offset_written:
            raise RuntimeError("images 최종 offset이 기록되지 않았습니다.")
        point_count = len(self.point3d_ids)
        return ImageTable(
            image_records=self.image_records,
            point2d_offsets=_freeze_buffer(
                self.offsets,
                np.dtype(np.uint64),
                (len(self.offsets),),
            ),
            point2d_xy=_freeze_buffer(
                self.xy_values,
                np.dtype(np.float64),
                (point_count, 2),
            ),
            point2d_point3d_ids=_freeze_buffer(
                self.point3d_ids,
                np.dtype(np.uint64),
                (point_count,),
            ),
        )


class _TextPointsStage(_TextStage):
    """points3D.txt를 한 POINT3D 줄씩 누적해 전체 파일 일괄 읽기를 피합니다."""

    def __init__(self, path: Path, *, preview: ReferencePointCollector | None = None) -> None:
        """전체 배열 또는 읽는 중 표본을 모으는 참고 저장소를 준비합니다."""
        super().__init__(STAGE_POINTS3D, path)
        self.preview = preview
        self.point_ids = array("Q")
        self.positions = array("d")
        self.colors = array("B")
        self.errors = array("d")
        self.track_offsets = array("Q")
        self.track_image_ids = array("Q")
        self.track_point2d_indices = array("I")
        self.seen_point_ids: set[int] = set()
        self.track_total = 0
        self._final_offset_written = False

    def _read_one(self) -> bool:
        """점과 관측을 검사하고 전체 누적 또는 참고 표본 누적을 수행합니다."""
        line = self._next_data_line()
        if line is None:
            if not self._final_offset_written:
                if self.preview is None:
                    self.track_offsets.append(self.track_total)
                self._final_offset_written = True
            return False
        reader = self._require_reader()
        tokens = line.split()
        if len(tokens) < 8:
            raise _parse_error(reader, "3D 점 레코드 필드 수가 부족합니다.")
        if (len(tokens) - 8) % 2:
            raise _parse_error(reader, "3D 점 트랙은 IMAGE_ID POINT2D_IDX의 2개 단위여야 합니다.")
        point_id = _parse_integer(
            tokens[0],
            minimum=0,
            maximum=_UINT64_MAX,
            label="POINT3D_ID",
            reader=reader,
        )
        if self.preview is None and point_id in self.seen_point_ids:
            raise _parse_error(reader, f"중복 POINT3D_ID가 있습니다: {point_id}")
        track_length = (len(tokens) - 8) // 2
        if self.track_total + track_length > _TEXT_MAX_VARIABLE_ITEM_COUNT:
            raise _parse_error(reader, "POINT3D 관측 트랙 총개수가 허용 한도를 초과했습니다.")
        xyz = tuple(_parse_float(token, "POINT3D 좌표", reader) for token in tokens[1:4])
        rgb = tuple(
            _parse_integer(
                token,
                minimum=0,
                maximum=255,
                label="POINT3D RGB",
                reader=reader,
            )
            for token in tokens[4:7]
        )
        error = _parse_float(tokens[7], "POINT3D 재투영 오차", reader)
        if self.preview is not None:
            self.preview.add(point_id, xyz, rgb, error)
        else:
            self.seen_point_ids.add(point_id)
            self.point_ids.append(point_id)
            self.positions.extend(xyz)
            self.colors.extend(rgb)
            self.errors.append(error)
            self.track_offsets.append(self.track_total)
        for token_index in range(8, len(tokens), 2):
            image_id = _parse_integer(
                tokens[token_index],
                minimum=1 if self.preview is not None else 0,
                maximum=_UINT32_MAX,
                label="트랙 IMAGE_ID",
                reader=reader,
            )
            point2d_index = _parse_integer(
                tokens[token_index + 1],
                minimum=0,
                maximum=_UINT32_MAX,
                label="트랙 POINT2D_IDX",
                reader=reader,
            )
            if self.preview is None:
                self.track_image_ids.append(image_id)
                self.track_point2d_indices.append(point2d_index)
        self.track_total += track_length
        self.current_count += 1
        _ensure_record_limit(self.current_count, "3D 점", reader)
        return True

    def result(self) -> PointTable:
        """텍스트 전체 입력 또는 검증된 참고 표본을 불변 테이블로 반환합니다."""
        if not self.done:
            raise RuntimeError("완료되지 않은 points3D 단계를 결과로 요청했습니다.")
        if not self._final_offset_written:
            raise RuntimeError("points3D 최종 track offset이 기록되지 않았습니다.")
        if self.preview is not None:
            return self.preview.result()
        point_count = len(self.point_ids)
        display_dtype = np.dtype(
            np.uint32 if point_count <= _UINT32_MAX else np.uint64
        )
        return PointTable(
            point_ids=_freeze_buffer(
                self.point_ids,
                np.dtype(np.uint64),
                (point_count,),
            ),
            positions=_freeze_buffer(
                self.positions,
                np.dtype(np.float64),
                (point_count, 3),
            ),
            colors=_freeze_buffer(
                self.colors,
                np.dtype(np.uint8),
                (point_count, 3),
            ),
            errors=_freeze_buffer(
                self.errors,
                np.dtype(np.float64),
                (point_count,),
            ),
            track_offsets=_freeze_buffer(
                self.track_offsets,
                np.dtype(np.uint64),
                (point_count + 1,),
            ),
            track_image_ids=_freeze_buffer(
                self.track_image_ids,
                np.dtype(np.uint64),
                (self.track_total,),
            ),
            track_point2d_indices=_freeze_buffer(
                self.track_point2d_indices,
                np.dtype(np.uint32),
                (self.track_total,),
            ),
            display_indices=np.arange(point_count, dtype=display_dtype),
        )


class _TextRigsStage(_TextStage):
    """rigs.txt 레코드를 하나씩 리그 사전에 추가합니다."""

    def __init__(self, path: Path) -> None:
        """리그 text 단계의 누적 사전을 준비합니다."""
        super().__init__(STAGE_RIGS, path)
        self.rigs: dict[int, RigRecord] = {}

    def _read_one(self) -> bool:
        """리그 한 줄의 기준·추가 센서와 선택 자세를 해석합니다."""
        line = self._next_data_line()
        if line is None:
            return False
        reader = self._require_reader()
        tokens = line.split()
        if len(tokens) < 2:
            raise _parse_error(reader, "리그 레코드 필드 수가 부족합니다.")
        rig_id = _parse_integer(
            tokens[0],
            minimum=0,
            maximum=_UINT32_MAX,
            label="RIG_ID",
            reader=reader,
        )
        sensor_count = _parse_integer(
            tokens[1],
            minimum=0,
            maximum=_TEXT_MAX_VARIABLE_ITEM_COUNT,
            label="리그 센서 수",
            reader=reader,
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
                tokens[token_index + 2],
                minimum=0,
                maximum=1,
                label="센서 자세 플래그",
                reader=reader,
            )
            token_index += 3
            sensor_from_rig: np.ndarray | None = None
            if has_pose:
                if len(tokens) < token_index + 7:
                    raise _parse_error(reader, "리그 센서 자세 필드가 부족합니다.")
                sensor_from_rig = np.asarray(
                    [
                        _parse_float(token, "sensor_from_rig", reader)
                        for token in tokens[token_index : token_index + 7]
                    ],
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
        if rig_id in self.rigs:
            raise _parse_error(reader, f"중복 RIG_ID가 있습니다: {rig_id}")
        self.rigs[rig_id] = RigRecord(rig_id, reference_sensor, tuple(sensors))
        self.current_count += 1
        _ensure_record_limit(self.current_count, "리그", reader)
        return True

    def result(self) -> dict[int, RigRecord]:
        """완료된 리그 ID 사전을 반환합니다."""
        if not self.done:
            raise RuntimeError("완료되지 않은 rigs 단계를 결과로 요청했습니다.")
        return self.rigs


class _TextFramesStage(_TextStage):
    """frames.txt 레코드를 하나씩 프레임 사전에 추가합니다."""

    def __init__(self, path: Path) -> None:
        """프레임 text 단계의 누적 사전을 준비합니다."""
        super().__init__(STAGE_FRAMES, path)
        self.frames: dict[int, FrameRecord] = {}

    def _read_one(self) -> bool:
        """프레임 한 줄과 센서 데이터 연결을 해석합니다."""
        line = self._next_data_line()
        if line is None:
            return False
        reader = self._require_reader()
        tokens = line.split()
        if len(tokens) < 10:
            raise _parse_error(reader, "프레임 레코드 필드 수가 부족합니다.")
        frame_id = _parse_integer(
            tokens[0],
            minimum=0,
            maximum=_UINT32_MAX,
            label="FRAME_ID",
            reader=reader,
        )
        rig_id = _parse_integer(
            tokens[1],
            minimum=0,
            maximum=_UINT32_MAX,
            label="RIG_ID",
            reader=reader,
        )
        rig_from_world = np.asarray(
            [_parse_float(token, "rig_from_world", reader) for token in tokens[2:9]],
            dtype=np.float64,
        )
        data_count = _parse_integer(
            tokens[9],
            minimum=0,
            maximum=_TEXT_MAX_VARIABLE_ITEM_COUNT,
            label="프레임 데이터 ID 수",
            reader=reader,
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
        if frame_id in self.frames:
            raise _parse_error(reader, f"중복 FRAME_ID가 있습니다: {frame_id}")
        self.frames[frame_id] = FrameRecord(
            frame_id=frame_id,
            rig_id=rig_id,
            rig_from_world=rig_from_world,
            data_ids=data_ids,
        )
        self.current_count += 1
        _ensure_record_limit(self.current_count, "프레임", reader)
        return True

    def result(self) -> dict[int, FrameRecord]:
        """완료된 프레임 ID 사전을 반환합니다."""
        if not self.done:
            raise RuntimeError("완료되지 않은 frames 단계를 결과로 요청했습니다.")
        return self.frames


def _calculate_parsed_bounds(positions: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """검증 전 모델도 만들 수 있게 원본 좌표에서 경계를 계산합니다."""
    if positions.ndim != 2 or positions.shape[1] != 3:
        raise ValueError("POINT3D 좌표 배열 형상이 올바르지 않습니다.")
    if positions.shape[0] == 0:
        empty_bounds = np.zeros(3, dtype=np.float64)
        return empty_bounds.copy(), empty_bounds.copy()
    return (
        np.ascontiguousarray(np.min(positions, axis=0), dtype=np.float64),
        np.ascontiguousarray(np.max(positions, axis=0), dtype=np.float64),
    )


class IncrementalSparseModelReader(Iterator[IncrementalReadProgress]):
    """COLMAP 파일을 TIMER 청크에서 안전하게 이어 읽는 순수 Python reader입니다."""

    def __init__(
        self,
        layout: SparseModelLayout,
        *,
        model_id: str = "",
        display_name: str | None = None,
        geometry_revision: int = 0,
        preview_point_limit: int | None = None,
    ) -> None:
        """전체 읽기 기본값을 유지하면서 명시적인 참고용 보관 정책을 준비합니다."""
        if not isinstance(layout, SparseModelLayout):
            raise TypeError("IncrementalSparseModelReader에는 SparseModelLayout이 필요합니다.")
        self.layout = layout
        self.model_id = model_id
        self.display_name = display_name or layout.source_path.name
        self.geometry_revision = geometry_revision
        self._preview = None if preview_point_limit is None else ReferencePointCollector(preview_point_limit)
        self._source_track_count = 0
        self._source_observation_count = 0
        self.stage = self._first_stage_name()
        self._stage_entries = self._create_stage_entries()
        self._stage_index = 0
        self._active_stage: _IncrementalStage | None = None
        self._rigs: dict[int, RigRecord] = {}
        self._cameras: dict[int, CameraRecord] = {}
        self._frames: dict[int, FrameRecord] = {}
        self._images: ImageTable | None = None
        self._points: PointTable | None = None
        self._model: ColmapModelData | ColmapReferencePreview | None = None
        self._error: Exception | None = None
        self._chunk_count = 0
        LOGGER.info(
            "event=incremental_reader_created source=%s format=%s initial_stage=%s",
            layout.source_path,
            layout.selected_format,
            self.stage,
        )

    def __enter__(self) -> IncrementalSparseModelReader:
        """with 문에서도 취소 시 파일 정리를 보장하도록 자기 자신을 반환합니다."""
        return self

    def __exit__(self, *_: object) -> None:
        """완료 전 with 블록을 벗어나면 열린 파일을 취소·정리합니다."""
        self.close()

    def __iter__(self) -> IncrementalSparseModelReader:
        """기본 레코드 예산의 next 호출을 지원하도록 자기 자신을 반환합니다."""
        return self

    def __next__(self) -> IncrementalReadProgress:
        """기본 레코드 예산으로 한 청크를 처리하고 종료 시 반복을 멈춥니다."""
        if self.is_terminal:
            raise StopIteration
        return self.step()

    @property
    def is_completed(self) -> bool:
        """파싱 전 단계가 정상 완료됐는지 반환합니다."""
        return self.stage == STAGE_COMPLETED

    @property
    def is_cancelled(self) -> bool:
        """호출자가 취소를 요청해 파일이 닫혔는지 반환합니다."""
        return self.stage == STAGE_CANCELLED

    @property
    def is_failed(self) -> bool:
        """파싱 오류 때문에 파일이 닫히고 실패 상태인지 반환합니다."""
        return self.stage == STAGE_FAILED

    @property
    def is_terminal(self) -> bool:
        """완료·취소·오류 중 하나로 더 읽을 일이 없는지 반환합니다."""
        return self.stage in _TERMINAL_STAGES

    @property
    def has_open_file(self) -> bool:
        """테스트와 생명주기 점검용으로 열린 입력 핸들 존재 여부를 반환합니다."""
        return bool(self._active_stage is not None and self._active_stage.has_open_file)

    @property
    def error(self) -> Exception | None:
        """오류로 종료된 경우 원래 예외 객체를 반환합니다."""
        return self._error

    @property
    def model(self) -> ColmapModelData | ColmapReferencePreview:
        """정책에 맞는 전체 모델 또는 별도 참고 자료형의 완료 결과를 반환합니다."""
        if self._model is None or not self.is_completed:
            raise RuntimeError("완료되지 않은 증분 COLMAP 모델을 요청했습니다.")
        return self._model

    @property
    def progress(self) -> IncrementalReadProgress:
        """현재 활성 파일 단계의 건수 또는 text 미확정 총량을 반환합니다."""
        if self.is_completed:
            point_count = self._model.points.count if self._model is not None else 0
            return IncrementalReadProgress(
                stage=STAGE_COMPLETED,
                current_count=point_count,
                total_count=point_count,
                fraction=1.0,
                is_completed=True,
                is_cancelled=False,
                is_failed=False,
            )
        active_stage = self._active_stage
        if active_stage is None:
            return IncrementalReadProgress(
                stage=self.stage,
                current_count=0,
                total_count=None,
                fraction=None,
                is_completed=False,
                is_cancelled=self.is_cancelled,
                is_failed=self.is_failed,
            )
        total_count = active_stage.total_count
        fraction = None
        if total_count is not None:
            fraction = 1.0 if total_count == 0 else min(
                1.0,
                active_stage.current_count / total_count,
            )
        return IncrementalReadProgress(
            stage=self.stage,
            current_count=active_stage.current_count,
            total_count=total_count,
            fraction=fraction,
            is_completed=False,
            is_cancelled=self.is_cancelled,
            is_failed=self.is_failed,
        )

    def step(
        self,
        *,
        max_records: int | None = DEFAULT_MAX_RECORDS_PER_STEP,
        time_budget_seconds: float | None = None,
    ) -> IncrementalReadProgress:
        """레코드 수와 시간 예산 경계 안에서만 한 TIMER 청크를 처리합니다."""
        # Blender COLMAP 0.16.7의 GPL-3.0-or-later 파서처럼 청크 동안 순환 GC를 멈춥니다.
        self._validate_step_budget(max_records, time_budget_seconds)
        if self.is_terminal:
            return self.progress
        deadline = (
            perf_counter() + time_budget_seconds
            if time_budget_seconds is not None
            else None
        )
        processed_records = 0
        self._chunk_count += 1
        gc_was_enabled = gc.isenabled()
        if gc_was_enabled:
            gc.disable()
        try:
            try:
                while True:
                    if max_records is not None and processed_records >= max_records:
                        break
                    if deadline is not None and perf_counter() >= deadline:
                        break
                    active_stage = self._get_or_create_active_stage()
                    if active_stage is None:
                        self._complete_model()
                        break
                    processed = active_stage.read_one()
                    if active_stage.done:
                        self._complete_active_stage(active_stage)
                    if processed:
                        processed_records += 1
                        if self.is_terminal:
                            break
                        continue
                    if active_stage.done:
                        break
                    raise RuntimeError("증분 COLMAP 단계가 레코드 없이 진행을 멈췄습니다.")
            except Exception as error:
                self._fail(error)
                raise
        finally:
            if gc_was_enabled:
                gc.enable()
                # COLMAP Importer와 같이 이번 청크에서 생성한 세대만 정리합니다.
                gc.collect(0)
        progress = self.progress
        LOGGER.debug(
            "event=incremental_chunk_completed chunk=%d stage=%s processed_records=%d current=%s total=%s",
            self._chunk_count,
            progress.stage,
            processed_records,
            progress.current_count,
            progress.total_count,
        )
        return progress

    def cancel(self) -> None:
        """진행 중인 파일을 닫고 기존 Runtime에 영향을 주지 않는 취소 상태로 전환합니다."""
        if self.is_terminal:
            return
        previous_stage = self.stage
        self._close_active_stage()
        self.stage = STAGE_CANCELLED
        LOGGER.info(
            "event=incremental_reader_cancelled source=%s previous_stage=%s",
            self.layout.source_path,
            previous_stage,
        )

    def close(self) -> None:
        """호출자 종료 경로에서 reader를 취소해 파일 핸들을 정리합니다."""
        self.cancel()

    def _first_stage_name(self) -> str:
        """선택 파일 구성을 반영한 첫 파싱 단계 이름을 반환합니다."""
        if self.layout.rigs_path is not None:
            return STAGE_RIGS
        return STAGE_CAMERAS

    def _create_stage_entries(self) -> tuple[tuple[str, Path], ...]:
        """선택 파일을 COLMAP 의존 순서의 파싱 단계 목록으로 만듭니다."""
        entries: list[tuple[str, Path]] = []
        if self.layout.rigs_path is not None:
            entries.append((STAGE_RIGS, self.layout.rigs_path))
        entries.append((STAGE_CAMERAS, self.layout.cameras_path))
        if self.layout.frames_path is not None:
            entries.append((STAGE_FRAMES, self.layout.frames_path))
        entries.append((STAGE_IMAGES, self.layout.images_path))
        entries.append((STAGE_POINTS3D, self.layout.points3d_path))
        return tuple(entries)

    def _get_or_create_active_stage(self) -> _IncrementalStage | None:
        """현재 파일 단계 reader를 지연 생성하고 마지막이면 없음으로 반환합니다."""
        if self._active_stage is not None:
            return self._active_stage
        if self._stage_index >= len(self._stage_entries):
            return None
        stage, path = self._stage_entries[self._stage_index]
        self.stage = stage
        self._active_stage = self._create_stage(stage, path)
        LOGGER.info(
            "event=incremental_stage_started source=%s format=%s stage=%s",
            self.layout.source_path,
            self.layout.selected_format,
            stage,
        )
        return self._active_stage

    def _create_stage(self, stage: str, path: Path) -> _IncrementalStage:
        """형식별 기존 파서에 명시적인 관측 폐기·표본 정책만 전달합니다."""
        if stage == STAGE_IMAGES:
            parser = _BinaryImagesStage if self.layout.selected_format == "binary" else _TextImagesStage
            return parser(path, discard_observations=self._preview is not None)
        if stage == STAGE_POINTS3D:
            parser = _BinaryPointsStage if self.layout.selected_format == "binary" else _TextPointsStage
            return parser(path, preview=self._preview)
        if self.layout.selected_format == "binary":
            binary_stages: dict[str, type[_IncrementalStage]] = {
                STAGE_RIGS: _BinaryRigsStage,
                STAGE_CAMERAS: _BinaryCamerasStage,
                STAGE_FRAMES: _BinaryFramesStage,
                STAGE_IMAGES: _BinaryImagesStage,
                STAGE_POINTS3D: _BinaryPointsStage,
            }
            return binary_stages[stage](path)
        text_stages: dict[str, type[_IncrementalStage]] = {
            STAGE_RIGS: _TextRigsStage,
            STAGE_CAMERAS: _TextCamerasStage,
            STAGE_FRAMES: _TextFramesStage,
            STAGE_IMAGES: _TextImagesStage,
            STAGE_POINTS3D: _TextPointsStage,
        }
        return text_stages[stage](path)

    def _complete_active_stage(self, active_stage: _IncrementalStage) -> None:
        """완료 결과와 폐기 전 관측 수를 옮기고 입력 핸들을 닫습니다."""
        if active_stage is not self._active_stage:
            raise RuntimeError("다른 증분 단계의 완료를 요청했습니다.")
        stage = active_stage.stage
        result = active_stage.result()
        if stage == STAGE_IMAGES:
            self._source_observation_count = active_stage.point2d_total
        elif stage == STAGE_POINTS3D:
            self._source_track_count = active_stage.track_total
        self._store_stage_result(stage, result)
        current_count = active_stage.current_count
        total_count = active_stage.total_count
        self._close_active_stage()
        self._stage_index += 1
        if self._stage_index < len(self._stage_entries):
            self.stage = self._stage_entries[self._stage_index][0]
        else:
            self._complete_model()
        LOGGER.info(
            "event=incremental_stage_completed source=%s stage=%s current=%d total=%s",
            self.layout.source_path,
            stage,
            current_count,
            total_count,
        )

    def _store_stage_result(self, stage: str, result: Any) -> None:
        """단계별 불변 결과를 모델 완성 전 임시 필드에 보관합니다."""
        if stage == STAGE_RIGS:
            self._rigs = result
        elif stage == STAGE_CAMERAS:
            self._cameras = result
        elif stage == STAGE_FRAMES:
            self._frames = result
        elif stage == STAGE_IMAGES:
            self._images = result
        elif stage == STAGE_POINTS3D:
            self._points = result
        else:
            raise RuntimeError(f"알 수 없는 증분 COLMAP 단계입니다: {stage}")

    def _complete_model(self) -> None:
        """표본 결과는 전체 모델과 구별하고 원본 전체의 경계·관측 수를 연결합니다."""
        if self._images is None or self._points is None:
            raise RuntimeError("완성할 images 또는 points3D 결과가 없습니다.")
        if self._preview is not None:
            self._model = ColmapReferencePreview(
                cameras=self._cameras, images=self._images, rigs=self._rigs, frames=self._frames,
                points=self._points, bounds_min=self._preview.bounds_min, bounds_max=self._preview.bounds_max,
                source_point_count=self._preview.source_count, source_track_count=self._source_track_count,
                source_observation_count=self._source_observation_count, point_limit=self._preview.limit,
                source_indices=self._preview.indices, source_path=self.layout.source_path,
                source_format=self.layout.selected_format,
            )
        else:
            bounds_min, bounds_max = _calculate_parsed_bounds(self._points.positions)
            self._model = ColmapModelData(
                model_id=self.model_id,
                display_name=self.display_name,
                source_path=self.layout.source_path,
                source_format=self.layout.selected_format,
                cameras=self._cameras,
                images=self._images,
                rigs=self._rigs,
                frames=self._frames,
                points=self._points,
                bounds_min=bounds_min,
                bounds_max=bounds_max,
                geometry_revision=self.geometry_revision,
            )
        self.stage = STAGE_COMPLETED
        LOGGER.info(
            "event=incremental_reader_completed source=%s cameras=%d images=%d points=%d",
            self.layout.source_path,
            len(self._model.cameras),
            self._model.images.count,
            self._model.points.count,
        )

    def _close_active_stage(self) -> None:
        """현재 파일 단계가 있다면 예외를 숨기지 않고 닫아 참조를 제거합니다."""
        active_stage = self._active_stage
        self._active_stage = None
        if active_stage is not None:
            active_stage.close()

    def _fail(self, error: Exception) -> None:
        """원래 오류를 보관하고 열린 파일을 닫은 뒤 실패 상태로 전환합니다."""
        self._error = error
        try:
            self._close_active_stage()
        finally:
            self.stage = STAGE_FAILED
        LOGGER.warning(
            "event=incremental_reader_failed source=%s error_type=%s reason=%s",
            self.layout.source_path,
            type(error).__name__,
            error,
        )

    @staticmethod
    def _validate_step_budget(
        max_records: int | None,
        time_budget_seconds: float | None,
    ) -> None:
        """호출자가 무한 또는 음수 청크 예산을 지정하지 못하게 검증합니다."""
        if max_records is None and time_budget_seconds is None:
            raise ValueError("max_records 또는 time_budget_seconds 중 하나가 필요합니다.")
        if max_records is not None and (
            isinstance(max_records, bool)
            or not isinstance(max_records, int)
            or max_records <= 0
        ):
            raise ValueError("max_records는 0보다 큰 정수여야 합니다.")
        if time_budget_seconds is not None and (
            isinstance(time_budget_seconds, bool)
            or not isinstance(time_budget_seconds, (int, float))
            or not math.isfinite(float(time_budget_seconds))
            or time_budget_seconds <= 0.0
        ):
            raise ValueError("time_budget_seconds는 유한한 양수여야 합니다.")


def create_incremental_sparse_model_reader(
    layout: SparseModelLayout,
    *,
    model_id: str = "",
    display_name: str | None = None,
    geometry_revision: int = 0,
    preview_point_limit: int | None = None,
) -> IncrementalSparseModelReader:
    """전체 읽기를 기본으로 하고 명시적인 참고 표본 상한을 선택적으로 연결합니다."""
    return IncrementalSparseModelReader(
        layout,
        model_id=model_id,
        display_name=display_name,
        geometry_revision=geometry_revision,
        preview_point_limit=preview_point_limit,
    )


__all__ = (
    "DEFAULT_MAX_RECORDS_PER_STEP",
    "IncrementalReadProgress",
    "IncrementalSparseModelReader",
    "STAGE_CAMERAS",
    "STAGE_CANCELLED",
    "STAGE_COMPLETED",
    "STAGE_FAILED",
    "STAGE_FRAMES",
    "STAGE_IMAGES",
    "STAGE_POINTS3D",
    "STAGE_RIGS",
    "create_incremental_sparse_model_reader",
)
