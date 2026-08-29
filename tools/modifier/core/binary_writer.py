"""COLMAP sparse Reconstruction을 little-endian binary 레이아웃으로 기록합니다."""

from __future__ import annotations

import struct
from pathlib import Path
from typing import BinaryIO

from .integrity import validate_reconstruction
from .model import Reconstruction
from .text_writer import _validate_model_fields_for_write

_COUNT_STRUCT = struct.Struct("<Q")
_CAMERA_HEADER_STRUCT = struct.Struct("<IiQQ")
_IMAGE_HEADER_STRUCT = struct.Struct("<IdddddddI")
_POINT2D_STRUCT = struct.Struct("<ddQ")
_POINT3D_HEADER_STRUCT = struct.Struct("<QdddBBBd")
_TRACK_STRUCT = struct.Struct("<II")
_RIG_HEADER_STRUCT = struct.Struct("<IIiI")
_SENSOR_HEADER_STRUCT = struct.Struct("<iIB")
_SENSOR_POSE_STRUCT = struct.Struct("<ddddddd")
_FRAME_HEADER_STRUCT = struct.Struct("<IIddddddd")
_DATA_ID_COUNT_STRUCT = struct.Struct("<I")
_DATA_ID_STRUCT = struct.Struct("<iIQ")
_POINT3D_SENTINEL = (1 << 64) - 1
_WRITE_BUFFER_BYTES = 1 * 1024 * 1024
_CAMERA_PARAM_STRUCTS: dict[int, struct.Struct] = {}


def _camera_param_struct(count: int) -> struct.Struct:
    record_struct = _CAMERA_PARAM_STRUCTS.get(count)
    if record_struct is None:
        record_struct = struct.Struct("<" + ("d" * count))
        _CAMERA_PARAM_STRUCTS[count] = record_struct
    return record_struct


def _serialize_cameras(
    stream: BinaryIO,
    model: Reconstruction,
) -> None:
    write = stream.write
    count_pack = _COUNT_STRUCT.pack
    header_pack = _CAMERA_HEADER_STRUCT.pack
    write(count_pack(len(model.cameras)))
    for camera in model.cameras.values():
        write(
            header_pack(
                camera.camera_id,
                camera.model_id,
                camera.width,
                camera.height,
            )
        )
        write(_camera_param_struct(len(camera.params)).pack(*camera.params))


def _serialize_images(
    stream: BinaryIO,
    model: Reconstruction,
) -> None:
    write = stream.write
    count_pack = _COUNT_STRUCT.pack
    header_pack = _IMAGE_HEADER_STRUCT.pack
    point_pack = _POINT2D_STRUCT.pack
    write(count_pack(len(model.images)))
    for image in model.images.values():
        write(
            header_pack(
                image.image_id,
                *image.qvec,
                *image.tvec,
                image.camera_id,
            )
        )
        write(image.name.encode("utf-8", errors="strict"))
        write(b"\0")
        write(count_pack(len(image.points2d)))
        for point in image.points2d:
            write(
                point_pack(
                    point.x,
                    point.y,
                    _POINT3D_SENTINEL
                    if point.point3d_id is None
                    else point.point3d_id,
                )
            )


def _serialize_points3d(
    stream: BinaryIO,
    model: Reconstruction,
) -> None:
    write = stream.write
    count_pack = _COUNT_STRUCT.pack
    header_pack = _POINT3D_HEADER_STRUCT.pack
    track_pack = _TRACK_STRUCT.pack
    write(count_pack(len(model.points3d)))
    for point in model.points3d.values():
        write(
            header_pack(
                point.point3d_id,
                *point.xyz,
                *point.rgb,
                point.error,
            )
        )
        write(count_pack(len(point.track)))
        for track in point.track:
            write(track_pack(track.image_id, track.point2d_idx))


def _serialize_rigs(
    stream: BinaryIO,
    model: Reconstruction,
) -> None:
    write = stream.write
    count_pack = _COUNT_STRUCT.pack
    header_pack = _RIG_HEADER_STRUCT.pack
    sensor_pack = _SENSOR_HEADER_STRUCT.pack
    pose_pack = _SENSOR_POSE_STRUCT.pack
    write(count_pack(len(model.rigs)))
    for rig in model.rigs.values():
        write(
            header_pack(
                rig.rig_id,
                len(rig.sensors) + 1,
                rig.ref_sensor_type,
                rig.ref_sensor_id,
            )
        )
        for sensor in rig.sensors:
            write(
                sensor_pack(
                    sensor.sensor_type,
                    sensor.sensor_id,
                    int(sensor.has_pose),
                )
            )
            if sensor.has_pose:
                write(pose_pack(*(sensor.qvec or ()), *(sensor.tvec or ())))


def _serialize_frames(
    stream: BinaryIO,
    model: Reconstruction,
) -> None:
    write = stream.write
    count_pack = _COUNT_STRUCT.pack
    header_pack = _FRAME_HEADER_STRUCT.pack
    data_count_pack = _DATA_ID_COUNT_STRUCT.pack
    data_pack = _DATA_ID_STRUCT.pack
    write(count_pack(len(model.frames)))
    for frame in model.frames.values():
        write(
            header_pack(
                frame.frame_id,
                frame.rig_id,
                *frame.qvec,
                *frame.tvec,
            )
        )
        write(data_count_pack(len(frame.data_ids)))
        for data_id in frame.data_ids:
            write(
                data_pack(
                    data_id.sensor_type,
                    data_id.sensor_id,
                    data_id.data_id,
                )
            )


def _write_cameras(path: Path, model: Reconstruction) -> None:
    with path.open("wb", buffering=_WRITE_BUFFER_BYTES) as stream:
        _serialize_cameras(stream, model)


def _write_images(path: Path, model: Reconstruction) -> None:
    with path.open("wb", buffering=_WRITE_BUFFER_BYTES) as stream:
        _serialize_images(stream, model)


def _write_points3d(path: Path, model: Reconstruction) -> None:
    with path.open("wb", buffering=_WRITE_BUFFER_BYTES) as stream:
        _serialize_points3d(stream, model)


def _write_rigs(path: Path, model: Reconstruction) -> None:
    with path.open("wb", buffering=_WRITE_BUFFER_BYTES) as stream:
        _serialize_rigs(stream, model)


def _write_frames(path: Path, model: Reconstruction) -> None:
    with path.open("wb", buffering=_WRITE_BUFFER_BYTES) as stream:
        _serialize_frames(stream, model)


def _write_model_binary_validated(
    directory: Path | str,
    model: Reconstruction,
    modern: bool,
) -> tuple[Path, ...]:
    """무결성이 검증된 모델을 Writer 고유 field 검사 후 BIN으로 기록합니다."""

    target = Path(directory)
    if not target.is_dir():
        raise ValueError(f"BIN Export 대상 디렉터리가 존재하지 않습니다: {target}")
    _validate_model_fields_for_write(model, modern=modern)

    paths: list[Path] = []
    if modern:
        rigs_path = target / "rigs.bin"
        _write_rigs(rigs_path, model)
        paths.append(rigs_path)

    cameras_path = target / "cameras.bin"
    _write_cameras(cameras_path, model)
    paths.append(cameras_path)

    if modern:
        frames_path = target / "frames.bin"
        _write_frames(frames_path, model)
        paths.append(frames_path)

    images_path = target / "images.bin"
    points_path = target / "points3D.bin"
    _write_images(images_path, model)
    _write_points3d(points_path, model)
    paths.extend((images_path, points_path))
    return tuple(paths)


def write_model_binary(
    directory: Path | str,
    model: Reconstruction,
    modern: bool,
) -> tuple[Path, ...]:
    """Reconstruction을 기존 layout과 같은 BIN 3개 또는 5개 파일로 기록합니다."""

    validate_reconstruction(model)
    return _write_model_binary_validated(directory, model, modern)
