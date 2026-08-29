"""modern COLMAP sparse model을 little-endian binary 5파일로 안전하게 기록합니다."""

from __future__ import annotations

from collections.abc import Callable, Mapping
import os
from pathlib import Path
import struct
from typing import BinaryIO, Final
from uuid import uuid4

import numpy as np

from .camera_models import camera_model_from_id
from .errors import ColmapIOError, ColmapParseError
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


MAX_RECORD_COUNT: Final[int] = 10_000_000
MAX_VARIABLE_ITEM_COUNT: Final[int] = 25_000_000
MAX_STRING_BYTES: Final[int] = 1_048_576
_UINT32_MAX: Final[int] = int(np.iinfo(np.uint32).max)
_UINT64_MAX: Final[int] = int(np.iinfo(np.uint64).max)
_SENSOR_TYPE_IDS: Final[dict[str, int]] = {"CAMERA": 0, "IMU": 1}


class ColmapWriteError(ColmapIOError):
    """COLMAP binary 내보내기 입력·범위·파일 기록이 실패할 때 발생합니다."""


def _integer_in_range(
    value: object,
    *,
    label: str,
    maximum: int,
    positive: bool = False,
) -> int:
    """정수형 ID·개수가 지정한 unsigned 범위 안에 있는지 확인합니다."""

    if isinstance(value, bool) or not isinstance(value, (int, np.integer)):
        raise ColmapWriteError(f"{label}는 정수여야 합니다.")
    normalized = int(value)
    minimum = 1 if positive else 0
    if normalized < minimum or normalized > maximum:
        raise ColmapWriteError(f"{label}가 기록 가능한 범위를 벗어났습니다: {normalized}")
    return normalized


def _sensor_type_id(sensor_type: object, *, label: str) -> int:
    """COLMAP 센서 이름을 binary int32 값으로 엄격하게 변환합니다."""

    try:
        return _SENSOR_TYPE_IDS[str(sensor_type)]
    except KeyError as error:
        raise ColmapWriteError(f"{label}의 지원하지 않는 센서 타입입니다: {sensor_type}") from error


def _finite_float64_array(
    value: object,
    *,
    label: str,
    shape: tuple[int | None, ...],
) -> np.ndarray:
    """형상·유한성을 검사한 float64 연속 사본을 반환합니다."""

    array = np.array(value, dtype=np.float64, order="C", copy=True)
    if array.ndim != len(shape):
        raise ColmapWriteError(f"{label} 배열 차원이 올바르지 않습니다.")
    for actual, expected in zip(array.shape, shape, strict=True):
        if expected is not None and actual != expected:
            raise ColmapWriteError(f"{label} 배열 형상이 올바르지 않습니다.")
    if not np.isfinite(array).all():
        raise ColmapWriteError(f"{label}에 NaN 또는 무한대가 있습니다.")
    return array


def _pose_values(pose: object, *, label: str) -> tuple[float, ...]:
    """7개 float64 pose와 영(0)이 아닌 쿼터니언을 기록 가능한 값으로 만듭니다."""

    array = _finite_float64_array(pose, label=label, shape=(7,))
    if float(np.linalg.norm(array[:4])) <= 1.0e-12:
        raise ColmapWriteError(f"{label}의 쿼터니언 크기가 0에 가깝습니다.")
    return tuple(float(value) for value in array)


def _pack(format_text: str, *values: object, label: str) -> bytes:
    """struct 범위 오류를 호출자가 구분할 수 있는 writer 오류로 바꿉니다."""

    try:
        return struct.pack(f"<{format_text}", *values)
    except (OverflowError, struct.error, ValueError) as error:
        raise ColmapWriteError(f"{label}을 binary로 기록할 수 없습니다.") from error


def _write_bytes(stream: BinaryIO, data: bytes, *, label: str) -> None:
    """short write를 정상 기록으로 오인하지 않고 즉시 실패 처리합니다."""

    written = stream.write(data)
    if written != len(data):
        raise OSError(f"{label} 기록이 중간에 종료되었습니다.")


def _write_float64_values(
    stream: BinaryIO,
    values: np.ndarray,
    *,
    label: str,
) -> None:
    """float64 배열을 호스트 endianness와 무관하게 little-endian으로 기록합니다."""

    normalized = np.ascontiguousarray(values, dtype=np.dtype("<f8"))
    _write_bytes(stream, normalized.tobytes(order="C"), label=label)


def _write_atomically(path: Path, callback: Callable[[BinaryIO], None]) -> None:
    """파일 하나를 같은 디렉터리 임시 파일에 쓴 뒤 os.replace로 확정합니다."""

    temporary_path = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temporary_path.open("xb") as stream:
            callback(stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
    except OSError as error:
        try:
            temporary_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise ColmapWriteError(f"COLMAP binary 파일을 기록할 수 없습니다: {path}") from error
    except Exception:
        try:
            temporary_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _prepare_output_path(path: str | Path) -> Path:
    """개별 binary 파일 경로가 실제 파일명과 부모 디렉터리를 갖는지 확인합니다."""

    output_path = Path(path)
    if not output_path.name:
        raise ColmapWriteError("COLMAP binary 출력 파일명이 비어 있습니다.")
    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise ColmapWriteError(
            f"COLMAP binary 출력 폴더를 만들 수 없습니다: {output_path.parent}"
        ) from error
    return output_path


def _checked_record_count(count: int, *, label: str) -> int:
    """현재 reader와 왕복 가능한 레코드 개수 상한을 적용합니다."""

    normalized = _integer_in_range(count, label=label, maximum=_UINT64_MAX)
    if normalized > MAX_RECORD_COUNT:
        raise ColmapWriteError(f"{label}가 reader 호환 상한을 초과했습니다: {normalized}")
    return normalized


def _checked_variable_count(count: int, *, label: str) -> int:
    """가변 points2D·track·sensor 배열의 reader 호환 상한을 적용합니다."""

    normalized = _integer_in_range(count, label=label, maximum=_UINT64_MAX)
    if normalized > MAX_VARIABLE_ITEM_COUNT:
        raise ColmapWriteError(f"{label}가 reader 호환 상한을 초과했습니다: {normalized}")
    return normalized


def _write_cameras(stream: BinaryIO, cameras: Mapping[int, CameraRecord]) -> None:
    """cameras.bin의 ID 오름차순 레코드를 현재 reader 역형식으로 기록합니다."""

    _write_bytes(
        stream,
        _pack("Q", _checked_record_count(len(cameras), label="카메라 개수"), label="카메라 개수"),
        label="카메라 개수",
    )
    for camera_id in sorted(cameras):
        camera = cameras[camera_id]
        if not isinstance(camera, CameraRecord) or camera.camera_id != camera_id:
            raise ColmapWriteError(f"CAMERA_ID {camera_id} 레코드가 올바르지 않습니다.")
        try:
            model = camera_model_from_id(camera.model_id)
        except ColmapParseError as error:
            raise ColmapWriteError(str(error)) from error
        if camera.model_name != model.name:
            raise ColmapWriteError(f"CAMERA_ID {camera_id}의 모델 이름과 ID가 다릅니다.")
        params = _finite_float64_array(
            camera.params_float64,
            label=f"CAMERA_ID {camera_id} 파라미터",
            shape=(model.parameter_count,),
        )
        _write_bytes(
            stream,
            _pack(
                "IiQQ",
                _integer_in_range(camera_id, label="CAMERA_ID", maximum=_UINT32_MAX, positive=True),
                int(camera.model_id),
                _integer_in_range(camera.width, label="카메라 너비", maximum=_UINT64_MAX, positive=True),
                _integer_in_range(camera.height, label="카메라 높이", maximum=_UINT64_MAX, positive=True),
                label=f"CAMERA_ID {camera_id}",
            ),
            label=f"CAMERA_ID {camera_id}",
        )
        _write_float64_values(stream, params, label=f"CAMERA_ID {camera_id} 파라미터")


def write_cameras_binary(
    cameras: Mapping[int, CameraRecord],
    path: str | Path,
) -> Path:
    """카메라 사전을 little-endian cameras.bin 파일로 원자 기록합니다."""

    output_path = _prepare_output_path(path)
    _write_atomically(output_path, lambda stream: _write_cameras(stream, cameras))
    return output_path


def _image_name_bytes(name: object, *, image_id: int) -> bytes:
    """NUL 없는 UTF-8 이미지 이름을 reader 허용 크기 안에서 인코딩합니다."""

    if not isinstance(name, str) or not name or name.strip() != name or "\0" in name:
        raise ColmapWriteError(f"IMAGE_ID {image_id}의 이미지 이름이 올바르지 않습니다.")
    try:
        encoded = name.encode("utf-8", errors="strict")
    except UnicodeEncodeError as error:
        raise ColmapWriteError(f"IMAGE_ID {image_id} 이름을 UTF-8로 인코딩할 수 없습니다.") from error
    if len(encoded) >= MAX_STRING_BYTES:
        raise ColmapWriteError(f"IMAGE_ID {image_id} 이름이 reader 허용 길이를 초과했습니다.")
    return encoded


def _write_images(stream: BinaryIO, images: ImageTable) -> None:
    """images.bin의 pose·NUL UTF-8 이름·points2D 배열을 ID 오름차순으로 기록합니다."""

    if not isinstance(images, ImageTable):
        raise ColmapWriteError("images.bin 기록에는 ImageTable이 필요합니다.")
    _write_bytes(
        stream,
        _pack("Q", _checked_record_count(images.count, label="이미지 개수"), label="이미지 개수"),
        label="이미지 개수",
    )
    total_points2d = 0
    for image_id in sorted(images.image_records):
        image = images.image_records[image_id]
        if image.image_id != image_id:
            raise ColmapWriteError(f"IMAGE_ID {image_id} 레코드가 올바르지 않습니다.")
        point_range = images.point_range(image_id)
        points2d_xy = _finite_float64_array(
            images.point2d_xy[point_range],
            label=f"IMAGE_ID {image_id} points2D",
            shape=(None, 2),
        )
        point3d_ids = np.ascontiguousarray(
            images.point2d_point3d_ids[point_range],
            dtype=np.uint64,
        )
        point_count = _checked_variable_count(
            points2d_xy.shape[0],
            label=f"IMAGE_ID {image_id} points2D 개수",
        )
        total_points2d += point_count
        if total_points2d > MAX_VARIABLE_ITEM_COUNT:
            raise ColmapWriteError("images.bin의 points2D 총개수가 reader 호환 상한을 초과했습니다.")
        _write_bytes(
            stream,
            _pack(
                "I7dI",
                _integer_in_range(image_id, label="IMAGE_ID", maximum=_UINT32_MAX, positive=True),
                *_pose_values(image.cam_from_world, label=f"IMAGE_ID {image_id} cam_from_world"),
                _integer_in_range(
                    image.camera_id,
                    label=f"IMAGE_ID {image_id} CAMERA_ID",
                    maximum=_UINT32_MAX,
                    positive=True,
                ),
                label=f"IMAGE_ID {image_id}",
            ),
            label=f"IMAGE_ID {image_id}",
        )
        _write_bytes(
            stream,
            _image_name_bytes(image.name, image_id=image_id) + b"\0",
            label=f"IMAGE_ID {image_id} 이름",
        )
        _write_bytes(
            stream,
            _pack("Q", point_count, label=f"IMAGE_ID {image_id} points2D 개수"),
            label=f"IMAGE_ID {image_id} points2D 개수",
        )
        if point_count:
            records = np.empty(
                (point_count,),
                dtype=np.dtype([("xy", "<f8", (2,)), ("point3d_id", "<u8")], align=False),
            )
            records["xy"] = points2d_xy
            records["point3d_id"] = point3d_ids
            _write_bytes(
                stream,
                records.tobytes(order="C"),
                label=f"IMAGE_ID {image_id} points2D",
            )


def write_images_binary(images: ImageTable, path: str | Path) -> Path:
    """이미지·points2D 테이블을 little-endian images.bin 파일로 원자 기록합니다."""

    output_path = _prepare_output_path(path)
    _write_atomically(output_path, lambda stream: _write_images(stream, images))
    return output_path


def _write_points3d(stream: BinaryIO, points: PointTable) -> None:
    """points3D.bin의 Point3D와 양방향 track을 POINT3D_ID 오름차순으로 기록합니다."""

    if not isinstance(points, PointTable):
        raise ColmapWriteError("points3D.bin 기록에는 PointTable이 필요합니다.")
    point_count = _checked_record_count(points.count, label="3D 점 개수")
    _write_bytes(
        stream,
        _pack("Q", point_count, label="3D 점 개수"),
        label="3D 점 개수",
    )
    total_track_count = 0
    sorted_indices = np.argsort(points.point_ids, kind="stable")
    for raw_index in sorted_indices:
        point_index = int(raw_index)
        point_id = _integer_in_range(
            points.point_ids[point_index],
            label="POINT3D_ID",
            maximum=_UINT64_MAX,
            positive=True,
        )
        if point_id == INVALID_POINT3D_ID:
            raise ColmapWriteError("POINT3D_ID에 COLMAP invalid sentinel을 기록할 수 없습니다.")
        position = _finite_float64_array(
            points.positions[point_index],
            label=f"POINT3D_ID {point_id} 좌표",
            shape=(3,),
        )
        error = float(points.errors[point_index])
        if not np.isfinite(error):
            raise ColmapWriteError(f"POINT3D_ID {point_id} 재투영 오차가 유한하지 않습니다.")
        color = np.ascontiguousarray(points.colors[point_index], dtype=np.uint8)
        if color.shape != (3,):
            raise ColmapWriteError(f"POINT3D_ID {point_id} RGB 형상이 올바르지 않습니다.")
        track_range = points.track_range(point_index)
        track_image_ids = points.track_image_ids[track_range]
        track_point2d_indices = points.track_point2d_indices[track_range]
        track_count = _checked_variable_count(
            track_image_ids.shape[0],
            label=f"POINT3D_ID {point_id} track 개수",
        )
        total_track_count += track_count
        if total_track_count > MAX_VARIABLE_ITEM_COUNT:
            raise ColmapWriteError("points3D.bin의 track 총개수가 reader 호환 상한을 초과했습니다.")
        _write_bytes(
            stream,
            _pack(
                "Q3d3BdQ",
                point_id,
                *(float(value) for value in position),
                *(int(value) for value in color),
                error,
                track_count,
                label=f"POINT3D_ID {point_id}",
            ),
            label=f"POINT3D_ID {point_id}",
        )
        if track_count:
            records = np.empty(
                (track_count,),
                dtype=np.dtype([("image_id", "<u4"), ("point2d_idx", "<u4")], align=False),
            )
            records["image_id"] = np.asarray(
                [
                    _integer_in_range(
                        value,
                        label=f"POINT3D_ID {point_id} track IMAGE_ID",
                        maximum=_UINT32_MAX,
                        positive=True,
                    )
                    for value in track_image_ids
                ],
                dtype=np.uint32,
            )
            records["point2d_idx"] = np.asarray(
                [
                    _integer_in_range(
                        value,
                        label=f"POINT3D_ID {point_id} track POINT2D_IDX",
                        maximum=_UINT32_MAX,
                    )
                    for value in track_point2d_indices
                ],
                dtype=np.uint32,
            )
            _write_bytes(
                stream,
                records.tobytes(order="C"),
                label=f"POINT3D_ID {point_id} track",
            )


def write_points3d_binary(points: PointTable, path: str | Path) -> Path:
    """Point3D·track 테이블을 little-endian points3D.bin 파일로 원자 기록합니다."""

    output_path = _prepare_output_path(path)
    _write_atomically(output_path, lambda stream: _write_points3d(stream, points))
    return output_path


def _write_rigs(stream: BinaryIO, rigs: Mapping[int, RigRecord]) -> None:
    """rigs.bin의 reference sensor와 선택 sensor pose를 ID 오름차순으로 기록합니다."""

    _write_bytes(
        stream,
        _pack("Q", _checked_record_count(len(rigs), label="리그 개수"), label="리그 개수"),
        label="리그 개수",
    )
    for rig_id in sorted(rigs):
        rig = rigs[rig_id]
        if not isinstance(rig, RigRecord) or rig.rig_id != rig_id:
            raise ColmapWriteError(f"RIG_ID {rig_id} 레코드가 올바르지 않습니다.")
        sensor_count = _checked_variable_count(
            len(rig.sensors),
            label=f"RIG_ID {rig_id} 센서 개수",
        )
        if sensor_count > _UINT32_MAX:
            raise ColmapWriteError(f"RIG_ID {rig_id} 센서 개수가 uint32 범위를 벗어났습니다.")
        if rig.reference_sensor is None or not rig.sensors or rig.sensors[0] != rig.reference_sensor:
            raise ColmapWriteError(f"RIG_ID {rig_id}의 기준 센서 계약이 올바르지 않습니다.")
        if rig.reference_sensor.sensor_from_rig is not None:
            raise ColmapWriteError(f"RIG_ID {rig_id} 기준 센서에는 sensor_from_rig가 없어야 합니다.")
        _write_bytes(
            stream,
            _pack(
                "II",
                _integer_in_range(rig_id, label="RIG_ID", maximum=_UINT32_MAX, positive=True),
                sensor_count,
                label=f"RIG_ID {rig_id}",
            ),
            label=f"RIG_ID {rig_id}",
        )
        reference = rig.reference_sensor
        _write_bytes(
            stream,
            _pack(
                "iI",
                _sensor_type_id(reference.sensor_type, label=f"RIG_ID {rig_id} 기준 센서"),
                _integer_in_range(
                    reference.sensor_id,
                    label=f"RIG_ID {rig_id} 기준 센서 ID",
                    maximum=_UINT32_MAX,
                    positive=True,
                ),
                label=f"RIG_ID {rig_id} 기준 센서",
            ),
            label=f"RIG_ID {rig_id} 기준 센서",
        )
        for sensor in rig.sensors[1:]:
            has_pose = sensor.sensor_from_rig is not None
            _write_bytes(
                stream,
                _pack(
                    "iIB",
                    _sensor_type_id(sensor.sensor_type, label=f"RIG_ID {rig_id} 센서"),
                    _integer_in_range(
                        sensor.sensor_id,
                        label=f"RIG_ID {rig_id} 센서 ID",
                        maximum=_UINT32_MAX,
                        positive=True,
                    ),
                    1 if has_pose else 0,
                    label=f"RIG_ID {rig_id} 센서",
                ),
                label=f"RIG_ID {rig_id} 센서",
            )
            if has_pose:
                _write_bytes(
                    stream,
                    _pack(
                        "7d",
                        *_pose_values(
                            sensor.sensor_from_rig,
                            label=f"RIG_ID {rig_id} sensor_from_rig",
                        ),
                        label=f"RIG_ID {rig_id} sensor_from_rig",
                    ),
                    label=f"RIG_ID {rig_id} sensor_from_rig",
                )


def write_rigs_binary(rigs: Mapping[int, RigRecord], path: str | Path) -> Path:
    """리그 사전을 little-endian rigs.bin 파일로 원자 기록합니다."""

    output_path = _prepare_output_path(path)
    _write_atomically(output_path, lambda stream: _write_rigs(stream, rigs))
    return output_path


def _write_frames(stream: BinaryIO, frames: Mapping[int, FrameRecord]) -> None:
    """frames.bin의 rig pose와 sensor data 연결을 ID 오름차순으로 기록합니다."""

    _write_bytes(
        stream,
        _pack("Q", _checked_record_count(len(frames), label="프레임 개수"), label="프레임 개수"),
        label="프레임 개수",
    )
    for frame_id in sorted(frames):
        frame = frames[frame_id]
        if not isinstance(frame, FrameRecord) or frame.frame_id != frame_id:
            raise ColmapWriteError(f"FRAME_ID {frame_id} 레코드가 올바르지 않습니다.")
        data_count = _checked_variable_count(
            len(frame.data_ids),
            label=f"FRAME_ID {frame_id} 데이터 개수",
        )
        if data_count > _UINT32_MAX:
            raise ColmapWriteError(f"FRAME_ID {frame_id} 데이터 개수가 uint32 범위를 벗어났습니다.")
        _write_bytes(
            stream,
            _pack(
                "II7dI",
                _integer_in_range(frame_id, label="FRAME_ID", maximum=_UINT32_MAX, positive=True),
                _integer_in_range(
                    frame.rig_id,
                    label=f"FRAME_ID {frame_id} RIG_ID",
                    maximum=_UINT32_MAX,
                    positive=True,
                ),
                *_pose_values(frame.rig_from_world, label=f"FRAME_ID {frame_id} rig_from_world"),
                data_count,
                label=f"FRAME_ID {frame_id}",
            ),
            label=f"FRAME_ID {frame_id}",
        )
        for data in frame.data_ids:
            _write_bytes(
                stream,
                _pack(
                    "iIQ",
                    _sensor_type_id(data.sensor_type, label=f"FRAME_ID {frame_id} 데이터"),
                    _integer_in_range(
                        data.sensor_id,
                        label=f"FRAME_ID {frame_id} 센서 ID",
                        maximum=_UINT32_MAX,
                        positive=True,
                    ),
                    _integer_in_range(
                        data.data_id,
                        label=f"FRAME_ID {frame_id} DATA_ID",
                        maximum=_UINT64_MAX,
                        positive=True,
                    ),
                    label=f"FRAME_ID {frame_id} 데이터",
                ),
                label=f"FRAME_ID {frame_id} 데이터",
            )


def write_frames_binary(frames: Mapping[int, FrameRecord], path: str | Path) -> Path:
    """프레임 사전을 little-endian frames.bin 파일로 원자 기록합니다."""

    output_path = _prepare_output_path(path)
    _write_atomically(output_path, lambda stream: _write_frames(stream, frames))
    return output_path


def _require_modern_model(model: ColmapModelData) -> None:
    """5파일 출력 전에 legacy 모델이 trivial rig/frame으로 정규화됐는지 확인합니다."""

    if not model.rigs or not model.frames:
        raise ColmapWriteError(
            "modern 5파일 내보내기 전에 legacy 모델을 trivial rigs/frames로 정규화해야 합니다."
        )


def write_binary_model(
    model: ColmapModelData,
    output_directory: str | Path,
) -> dict[str, Path]:
    """검증된 modern sparse model을 rigs→cameras→frames→images→points 순서로 기록합니다."""

    if not isinstance(model, ColmapModelData):
        raise TypeError("COLMAP binary 내보내기에는 ColmapModelData가 필요합니다.")
    validate_model(model)
    _require_modern_model(model)
    directory = Path(output_directory)
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise ColmapWriteError(f"COLMAP binary 출력 폴더를 만들 수 없습니다: {directory}") from error
    if not directory.is_dir():
        raise ColmapWriteError(f"COLMAP binary 출력 경로가 폴더가 아닙니다: {directory}")

    output_paths = {
        "rigs.bin": directory / "rigs.bin",
        "cameras.bin": directory / "cameras.bin",
        "frames.bin": directory / "frames.bin",
        "images.bin": directory / "images.bin",
        "points3D.bin": directory / "points3D.bin",
    }
    write_rigs_binary(model.rigs, output_paths["rigs.bin"])
    write_cameras_binary(model.cameras, output_paths["cameras.bin"])
    write_frames_binary(model.frames, output_paths["frames.bin"])
    write_images_binary(model.images, output_paths["images.bin"])
    write_points3d_binary(model.points, output_paths["points3D.bin"])
    return output_paths


__all__ = (
    "ColmapWriteError",
    "write_binary_model",
    "write_cameras_binary",
    "write_frames_binary",
    "write_images_binary",
    "write_points3d_binary",
    "write_rigs_binary",
)
