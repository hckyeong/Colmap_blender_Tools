"""modern COLMAP sparse model을 UTF-8 text 5파일로 안전하게 기록합니다."""

from __future__ import annotations

from collections.abc import Callable, Mapping
import os
from pathlib import Path
from typing import Final, TextIO
from uuid import uuid4

import numpy as np

from .errors import ColmapIOError
from .text_reader import MAX_RECORD_COUNT, MAX_TEXT_LINE_CHARS, MAX_VARIABLE_ITEM_COUNT
from .types import (
    CameraRecord,
    ColmapModelData,
    FrameRecord,
    ImageTable,
    INVALID_POINT3D_ID,
    PointTable,
    RigRecord,
)
from .validation import validate_model


_UINT32_MAX: Final[int] = int(np.iinfo(np.uint32).max)
_UINT64_MAX: Final[int] = int(np.iinfo(np.uint64).max)


class ColmapTextWriteError(ColmapIOError):
    """COLMAP text 입력·범위·파일 기록이 실패할 때 발생합니다."""


def _unsigned_integer(
    value: object,
    *,
    label: str,
    maximum: int,
    positive: bool = False,
) -> int:
    """정수형 ID·개수를 지정한 unsigned 범위로 제한합니다."""

    if isinstance(value, bool) or not isinstance(value, (int, np.integer)):
        raise ColmapTextWriteError(f"{label}는 정수여야 합니다.")
    normalized = int(value)
    minimum = 1 if positive else 0
    if normalized < minimum or normalized > maximum:
        raise ColmapTextWriteError(f"{label}가 text 기록 범위를 벗어났습니다: {normalized}")
    return normalized


def _checked_count(count: int, *, label: str, maximum: int) -> int:
    """현재 text reader와 왕복 가능한 레코드·가변 항목 수를 확인합니다."""

    normalized = _unsigned_integer(count, label=label, maximum=_UINT64_MAX)
    if normalized > maximum:
        raise ColmapTextWriteError(f"{label}가 reader 호환 상한을 초과했습니다: {normalized}")
    return normalized


def _float_text(value: object, *, label: str) -> str:
    """float64 값을 정확히 왕복 가능한 짧은 십진 문자열로 변환합니다."""

    normalized = float(value)
    if not np.isfinite(normalized):
        raise ColmapTextWriteError(f"{label}에 NaN 또는 무한대가 있습니다.")
    return format(normalized, ".17g")


def _pose_tokens(pose: object, *, label: str) -> list[str]:
    """7개 자세 값을 text 레코드 토큰으로 변환합니다."""

    array = np.asarray(pose, dtype=np.float64)
    if array.shape != (7,) or not np.isfinite(array).all():
        raise ColmapTextWriteError(f"{label}의 자세 형식이 올바르지 않습니다.")
    return [_float_text(value, label=label) for value in array]


def _write_line(stream: TextIO, tokens: str | list[str], *, label: str) -> None:
    """한 물리 줄이 reader 제한을 넘지 않도록 UTF-8 text를 기록합니다."""

    text = tokens if isinstance(tokens, str) else " ".join(tokens)
    if len(text) + 1 > MAX_TEXT_LINE_CHARS:
        raise ColmapTextWriteError(f"{label} 줄 길이가 reader 호환 상한을 초과했습니다.")
    line = f"{text}\n"
    if stream.write(line) != len(line):
        raise OSError(f"{label} 기록이 중간에 종료되었습니다.")


def _write_atomically(path: Path, callback: Callable[[TextIO], None]) -> None:
    """파일 하나를 같은 디렉터리 임시 파일에 쓴 뒤 원자 교체합니다."""

    temporary_path = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temporary_path.open("x", encoding="utf-8", errors="strict", newline="\n") as stream:
            callback(stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
    except OSError as error:
        try:
            temporary_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise ColmapTextWriteError(f"COLMAP text 파일을 기록할 수 없습니다: {path}") from error
    except Exception:
        try:
            temporary_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _write_cameras(stream: TextIO, cameras: Mapping[int, CameraRecord]) -> None:
    """카메라 레코드를 CAMERA_ID 오름차순으로 기록합니다."""

    _checked_count(len(cameras), label="카메라 레코드 수", maximum=MAX_RECORD_COUNT)
    _write_line(stream, "# Camera list with one line of data per camera:", label="cameras.txt 헤더")
    _write_line(stream, "#   CAMERA_ID, MODEL, WIDTH, HEIGHT, PARAMS[]", label="cameras.txt 헤더")
    _write_line(stream, f"# Number of cameras: {len(cameras)}", label="cameras.txt 헤더")
    for camera_id in sorted(cameras):
        camera = cameras[camera_id]
        tokens = [
            str(_unsigned_integer(camera_id, label="CAMERA_ID", maximum=_UINT32_MAX, positive=True)),
            camera.model_name,
            str(_unsigned_integer(camera.width, label=f"CAMERA_ID {camera_id} 너비", maximum=_UINT64_MAX, positive=True)),
            str(_unsigned_integer(camera.height, label=f"CAMERA_ID {camera_id} 높이", maximum=_UINT64_MAX, positive=True)),
        ]
        tokens.extend(_float_text(value, label=f"CAMERA_ID {camera_id} 파라미터") for value in camera.params_float64)
        _write_line(stream, tokens, label=f"CAMERA_ID {camera_id}")


def _write_rigs(stream: TextIO, rigs: Mapping[int, RigRecord]) -> None:
    """리그와 선택 센서 자세를 RIG_ID 오름차순으로 기록합니다."""

    _checked_count(len(rigs), label="리그 레코드 수", maximum=MAX_RECORD_COUNT)
    _write_line(stream, "# Rig calib list with one line of data per calib:", label="rigs.txt 헤더")
    _write_line(stream, "#   RIG_ID, NUM_SENSORS, REF_SENSOR_TYPE, REF_SENSOR_ID, SENSORS[] as (SENSOR_TYPE, SENSOR_ID, HAS_POSE, [QW, QX, QY, QZ, TX, TY, TZ])", label="rigs.txt 헤더")
    _write_line(stream, f"# Number of rigs: {len(rigs)}", label="rigs.txt 헤더")
    for rig_id in sorted(rigs):
        rig = rigs[rig_id]
        sensor_count = _checked_count(len(rig.sensors), label=f"RIG_ID {rig_id} 센서 수", maximum=MAX_VARIABLE_ITEM_COUNT)
        if rig.reference_sensor is None or not rig.sensors:
            raise ColmapTextWriteError(f"RIG_ID {rig_id}에 기준 센서가 없습니다.")
        reference = rig.reference_sensor
        tokens = [
            str(_unsigned_integer(rig_id, label="RIG_ID", maximum=_UINT32_MAX, positive=True)),
            str(sensor_count),
            reference.sensor_type,
            str(_unsigned_integer(reference.sensor_id, label=f"RIG_ID {rig_id} 기준 센서 ID", maximum=_UINT32_MAX, positive=True)),
        ]
        for sensor in rig.sensors[1:]:
            tokens.extend(
                (
                    sensor.sensor_type,
                    str(_unsigned_integer(sensor.sensor_id, label=f"RIG_ID {rig_id} 센서 ID", maximum=_UINT32_MAX, positive=True)),
                    "1" if sensor.sensor_from_rig is not None else "0",
                )
            )
            if sensor.sensor_from_rig is not None:
                tokens.extend(_pose_tokens(sensor.sensor_from_rig, label=f"RIG_ID {rig_id} sensor_from_rig"))
        _write_line(stream, tokens, label=f"RIG_ID {rig_id}")


def _write_frames(stream: TextIO, frames: Mapping[int, FrameRecord]) -> None:
    """프레임 자세와 센서 데이터 연결을 FRAME_ID 오름차순으로 기록합니다."""

    _checked_count(len(frames), label="프레임 레코드 수", maximum=MAX_RECORD_COUNT)
    _write_line(stream, "# Frame list with one line of data per frame:", label="frames.txt 헤더")
    _write_line(stream, "#   FRAME_ID, RIG_ID, RIG_FROM_WORLD[QW, QX, QY, QZ, TX, TY, TZ], NUM_DATA_IDS, DATA_IDS[] as (SENSOR_TYPE, SENSOR_ID, DATA_ID)", label="frames.txt 헤더")
    _write_line(stream, f"# Number of frames: {len(frames)}", label="frames.txt 헤더")
    for frame_id in sorted(frames):
        frame = frames[frame_id]
        data_count = _checked_count(len(frame.data_ids), label=f"FRAME_ID {frame_id} 데이터 수", maximum=MAX_VARIABLE_ITEM_COUNT)
        tokens = [
            str(_unsigned_integer(frame_id, label="FRAME_ID", maximum=_UINT32_MAX, positive=True)),
            str(_unsigned_integer(frame.rig_id, label=f"FRAME_ID {frame_id} RIG_ID", maximum=_UINT32_MAX, positive=True)),
            *_pose_tokens(frame.rig_from_world, label=f"FRAME_ID {frame_id} rig_from_world"),
            str(data_count),
        ]
        for data in frame.data_ids:
            tokens.extend(
                (
                    data.sensor_type,
                    str(_unsigned_integer(data.sensor_id, label=f"FRAME_ID {frame_id} 센서 ID", maximum=_UINT32_MAX, positive=True)),
                    str(_unsigned_integer(data.data_id, label=f"FRAME_ID {frame_id} DATA_ID", maximum=_UINT64_MAX, positive=True)),
                )
            )
        _write_line(stream, tokens, label=f"FRAME_ID {frame_id}")


def _write_images(stream: TextIO, images: ImageTable) -> None:
    """이미지 자세와 points2D 두 줄 레코드를 IMAGE_ID 오름차순으로 기록합니다."""

    _checked_count(images.count, label="이미지 레코드 수", maximum=MAX_RECORD_COUNT)
    _checked_count(images.point2d_xy.shape[0], label="points2D 총개수", maximum=MAX_VARIABLE_ITEM_COUNT)
    _write_line(stream, "# Image list with two lines of data per image:", label="images.txt 헤더")
    _write_line(stream, "#   IMAGE_ID, QW, QX, QY, QZ, TX, TY, TZ, CAMERA_ID, NAME", label="images.txt 헤더")
    _write_line(stream, "#   POINTS2D[] as (X, Y, POINT3D_ID)", label="images.txt 헤더")
    mean_points = images.point2d_xy.shape[0] / images.count if images.count else 0.0
    _write_line(stream, f"# Number of images: {images.count}, mean observations per image: {_float_text(mean_points, label='이미지 평균 관측 수')}", label="images.txt 헤더")
    for image_id in sorted(images.image_records):
        image = images.image_records[image_id]
        # 일반 공백은 이름 일부로 보존하고 물리 레코드를 깨뜨리는 줄바꿈만 거부합니다.
        if "\r" in image.name or "\n" in image.name:
            raise ColmapTextWriteError(
                f"IMAGE_ID {image_id} 이름에 text 레코드로 기록할 수 없는 줄바꿈이 있습니다."
            )
        metadata = [
            str(_unsigned_integer(image_id, label="IMAGE_ID", maximum=_UINT32_MAX, positive=True)),
            *_pose_tokens(image.cam_from_world, label=f"IMAGE_ID {image_id} cam_from_world"),
            str(_unsigned_integer(image.camera_id, label=f"IMAGE_ID {image_id} CAMERA_ID", maximum=_UINT32_MAX, positive=True)),
            image.name,
        ]
        _write_line(stream, metadata, label=f"IMAGE_ID {image_id} 메타데이터")
        point_tokens: list[str] = []
        point_range = images.point_range(image_id)
        for point_index in range(point_range.start, point_range.stop):
            point_id = int(images.point2d_point3d_ids[point_index])
            point_tokens.extend(
                (
                    _float_text(images.point2d_xy[point_index, 0], label=f"IMAGE_ID {image_id} points2D X"),
                    _float_text(images.point2d_xy[point_index, 1], label=f"IMAGE_ID {image_id} points2D Y"),
                    "-1" if point_id == INVALID_POINT3D_ID else str(_unsigned_integer(point_id, label=f"IMAGE_ID {image_id} POINT3D_ID", maximum=_UINT64_MAX, positive=True)),
                )
            )
        _write_line(stream, point_tokens, label=f"IMAGE_ID {image_id} points2D")


def _write_points3d(stream: TextIO, points: PointTable) -> None:
    """3D 점과 관측 트랙을 POINT3D_ID 오름차순으로 기록합니다."""

    _checked_count(points.count, label="3D 점 레코드 수", maximum=MAX_RECORD_COUNT)
    _checked_count(points.track_image_ids.shape[0], label="POINT3D 트랙 총개수", maximum=MAX_VARIABLE_ITEM_COUNT)
    _write_line(stream, "# 3D point list with one line of data per point:", label="points3D.txt 헤더")
    _write_line(stream, "#   POINT3D_ID, X, Y, Z, R, G, B, ERROR, TRACK[] as (IMAGE_ID, POINT2D_IDX)", label="points3D.txt 헤더")
    mean_track = points.track_image_ids.shape[0] / points.count if points.count else 0.0
    _write_line(stream, f"# Number of points: {points.count}, mean track length: {_float_text(mean_track, label='평균 트랙 길이')}", label="points3D.txt 헤더")
    point_index_by_id = {int(point_id): index for index, point_id in enumerate(points.point_ids)}
    for point_id in sorted(point_index_by_id):
        point_index = point_index_by_id[point_id]
        tokens = [
            str(_unsigned_integer(point_id, label="POINT3D_ID", maximum=_UINT64_MAX, positive=True)),
            *(_float_text(value, label=f"POINT3D_ID {point_id} 좌표") for value in points.positions[point_index]),
            *(str(int(value)) for value in points.colors[point_index]),
            _float_text(points.errors[point_index], label=f"POINT3D_ID {point_id} 오차"),
        ]
        track_range = points.track_range(point_index)
        for track_index in range(track_range.start, track_range.stop):
            tokens.extend(
                (
                    str(_unsigned_integer(points.track_image_ids[track_index], label=f"POINT3D_ID {point_id} 트랙 IMAGE_ID", maximum=_UINT32_MAX, positive=True)),
                    str(_unsigned_integer(points.track_point2d_indices[track_index], label=f"POINT3D_ID {point_id} 트랙 POINT2D_IDX", maximum=_UINT32_MAX)),
                )
            )
        _write_line(stream, tokens, label=f"POINT3D_ID {point_id}")


def write_text_model(
    model: ColmapModelData,
    output_directory: str | Path,
) -> dict[str, Path]:
    """검증된 modern sparse model을 공식 text 5파일 순서로 원자 기록합니다."""

    if not isinstance(model, ColmapModelData):
        raise TypeError("COLMAP text 내보내기에는 ColmapModelData가 필요합니다.")
    validate_model(model)
    if not model.rigs or not model.frames:
        raise ColmapTextWriteError(
            "modern 5파일 내보내기 전에 legacy 모델을 trivial rigs/frames로 정규화해야 합니다."
        )
    directory = Path(output_directory)
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise ColmapTextWriteError(f"COLMAP text 출력 폴더를 만들 수 없습니다: {directory}") from error
    if not directory.is_dir():
        raise ColmapTextWriteError(f"COLMAP text 출력 경로가 폴더가 아닙니다: {directory}")

    output_paths = {
        "rigs.txt": directory / "rigs.txt",
        "cameras.txt": directory / "cameras.txt",
        "frames.txt": directory / "frames.txt",
        "images.txt": directory / "images.txt",
        "points3D.txt": directory / "points3D.txt",
    }
    _write_atomically(output_paths["rigs.txt"], lambda stream: _write_rigs(stream, model.rigs))
    _write_atomically(output_paths["cameras.txt"], lambda stream: _write_cameras(stream, model.cameras))
    _write_atomically(output_paths["frames.txt"], lambda stream: _write_frames(stream, model.frames))
    _write_atomically(output_paths["images.txt"], lambda stream: _write_images(stream, model.images))
    _write_atomically(output_paths["points3D.txt"], lambda stream: _write_points3d(stream, model.points))
    return output_paths


__all__ = ("ColmapTextWriteError", "write_text_model")
