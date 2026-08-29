"""COLMAP sparse Reconstruction을 공식 text 레이아웃으로 기록합니다."""

from __future__ import annotations

import math
from pathlib import Path
from typing import TextIO

from .integrity import validate_reconstruction
from .model import Reconstruction

_UINT32_MAX = (1 << 32) - 1
_UINT64_MAX = (1 << 64) - 1
_INT32_MIN = -(1 << 31)
_INT32_MAX = (1 << 31) - 1
_MAX_IMAGE_NAME_BYTES = (1 << 20) - 1
_SENSOR_NAME_BY_TYPE = {0: "CAMERA", 1: "IMU"}


def _require_integer(value: int, minimum: int, maximum: int, context: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{context}은(는) 정수여야 합니다.")
    if value < minimum or value > maximum:
        raise ValueError(
            f"{context} 값 {value}이(가) 허용 범위 [{minimum}, {maximum}]를 벗어났습니다."
        )


def _require_float_sequence(values: tuple[float, ...], length: int, context: str) -> None:
    if len(values) != length:
        raise ValueError(f"{context} 값 개수는 {length}개여야 합니다.")
    if not all(isinstance(value, (int, float)) and math.isfinite(value) for value in values):
        raise ValueError(f"{context}에 유한하지 않은 수치가 있습니다.")


def _validate_image_name(name: str, context: str) -> None:
    if not isinstance(name, str) or not name:
        raise ValueError(f"{context} 이름은 비어 있지 않은 문자열이어야 합니다.")
    if "\0" in name or "\n" in name or "\r" in name:
        raise ValueError(f"{context} 이름에는 NUL 또는 개행 문자를 사용할 수 없습니다.")
    if name != name.strip():
        raise ValueError(f"{context} 이름의 시작과 끝에는 공백을 사용할 수 없습니다.")
    try:
        encoded = name.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{context} 이름을 안전한 UTF-8로 기록할 수 없습니다.") from exc
    if len(encoded) > _MAX_IMAGE_NAME_BYTES:
        raise ValueError(f"{context} 이름이 안전 제한 1 MiB를 초과했습니다.")


def _validate_model_fields_for_write(model: Reconstruction, *, modern: bool) -> None:
    """Writer 고유 field 범위와 손실 가능성을 쓰기 전에 검사합니다."""

    if not modern and (model.rigs or model.frames):
        raise ValueError("legacy layout으로 Rig 또는 Frame 레코드를 손실 없이 기록할 수 없습니다.")

    _require_integer(len(model.cameras), 0, _UINT64_MAX, "Camera 개수")
    _require_integer(len(model.images), 0, _UINT64_MAX, "Image 개수")
    _require_integer(len(model.points3d), 0, _UINT64_MAX, "Point3D 개수")
    _require_integer(len(model.rigs), 0, _UINT64_MAX, "Rig 개수")
    _require_integer(len(model.frames), 0, _UINT64_MAX, "Frame 개수")

    for camera in model.cameras.values():
        context = f"CAMERA_ID {camera.camera_id}"
        _require_integer(camera.camera_id, 0, _UINT32_MAX, f"{context} ID")
        _require_integer(camera.model_id, _INT32_MIN, _INT32_MAX, f"{context} MODEL_ID")
        _require_integer(camera.width, 1, _UINT64_MAX, f"{context} WIDTH")
        _require_integer(camera.height, 1, _UINT64_MAX, f"{context} HEIGHT")
        if not all(
            isinstance(value, (int, float)) and math.isfinite(value)
            for value in camera.params
        ):
            raise ValueError(f"{context} 파라미터에 유한하지 않은 수치가 있습니다.")

    for image in model.images.values():
        context = f"IMAGE_ID {image.image_id}"
        _require_integer(image.image_id, 0, _UINT32_MAX, f"{context} ID")
        _require_integer(image.camera_id, 0, _UINT32_MAX, f"{context} CAMERA_ID")
        _require_float_sequence(image.qvec, 4, f"{context} QVEC")
        _require_float_sequence(image.tvec, 3, f"{context} TVEC")
        _validate_image_name(image.name, context)
        _require_integer(len(image.points2d), 0, _UINT64_MAX, f"{context} Point2D 개수")
        for point_index, point in enumerate(image.points2d):
            point_context = f"{context} POINT2D_IDX {point_index}"
            _require_float_sequence((point.x, point.y), 2, point_context)
            if point.point3d_id is not None:
                _require_integer(
                    point.point3d_id,
                    0,
                    _UINT64_MAX - 1,
                    f"{point_context} POINT3D_ID",
                )

    for point in model.points3d.values():
        context = f"POINT3D_ID {point.point3d_id}"
        _require_integer(point.point3d_id, 0, _UINT64_MAX - 1, f"{context} ID")
        _require_float_sequence(point.xyz, 3, f"{context} XYZ")
        if len(point.rgb) != 3:
            raise ValueError(f"{context} RGB 값 개수는 3개여야 합니다.")
        for channel_index, channel in enumerate(point.rgb):
            _require_integer(channel, 0, 255, f"{context} RGB[{channel_index}]")
        _require_float_sequence((point.error,), 1, f"{context} ERROR")
        _require_integer(len(point.track), 0, _UINT64_MAX, f"{context} track 개수")
        for track_index, track in enumerate(point.track):
            track_context = f"{context} TRACK[{track_index}]"
            _require_integer(track.image_id, 0, _UINT32_MAX, f"{track_context} IMAGE_ID")
            _require_integer(
                track.point2d_idx,
                0,
                _UINT32_MAX,
                f"{track_context} POINT2D_IDX",
            )

    for rig in model.rigs.values():
        context = f"RIG_ID {rig.rig_id}"
        _require_integer(rig.rig_id, 0, _UINT32_MAX, f"{context} ID")
        _require_integer(
            len(rig.sensors) + 1,
            1,
            _UINT32_MAX,
            f"{context} sensor 개수",
        )
        _require_integer(
            rig.ref_sensor_type,
            _INT32_MIN,
            _INT32_MAX,
            f"{context} 기준 SENSOR_TYPE",
        )
        _require_integer(
            rig.ref_sensor_id,
            0,
            _UINT32_MAX,
            f"{context} 기준 SENSOR_ID",
        )
        for sensor_index, sensor in enumerate(rig.sensors):
            sensor_context = f"{context} SENSOR[{sensor_index}]"
            _require_integer(
                sensor.sensor_type,
                _INT32_MIN,
                _INT32_MAX,
                f"{sensor_context} TYPE",
            )
            _require_integer(
                sensor.sensor_id,
                0,
                _UINT32_MAX,
                f"{sensor_context} ID",
            )
            if not isinstance(sensor.has_pose, bool):
                raise ValueError(f"{sensor_context} HAS_POSE는 bool이어야 합니다.")
            if sensor.has_pose:
                if sensor.qvec is None or sensor.tvec is None:
                    raise ValueError(f"{sensor_context} pose가 누락되었습니다.")
                _require_float_sequence(sensor.qvec, 4, f"{sensor_context} QVEC")
                _require_float_sequence(sensor.tvec, 3, f"{sensor_context} TVEC")
            elif sensor.qvec is not None or sensor.tvec is not None:
                raise ValueError(
                    f"{sensor_context} HAS_POSE가 false인데 pose 값이 존재합니다."
                )

    for frame in model.frames.values():
        context = f"FRAME_ID {frame.frame_id}"
        _require_integer(frame.frame_id, 0, _UINT32_MAX, f"{context} ID")
        _require_integer(frame.rig_id, 0, _UINT32_MAX, f"{context} RIG_ID")
        _require_float_sequence(frame.qvec, 4, f"{context} QVEC")
        _require_float_sequence(frame.tvec, 3, f"{context} TVEC")
        _require_integer(len(frame.data_ids), 0, _UINT32_MAX, f"{context} DATA_IDS 개수")
        for data_index, data_id in enumerate(frame.data_ids):
            data_context = f"{context} DATA_ID[{data_index}]"
            _require_integer(
                data_id.sensor_type,
                _INT32_MIN,
                _INT32_MAX,
                f"{data_context} SENSOR_TYPE",
            )
            _require_integer(
                data_id.sensor_id,
                0,
                _UINT32_MAX,
                f"{data_context} SENSOR_ID",
            )
            _require_integer(data_id.data_id, 0, _UINT64_MAX, f"{data_context} DATA_ID")


def _format_float(value: float) -> str:
    return format(value, ".17g")


def _sensor_type_text(sensor_type: int) -> str:
    return _SENSOR_NAME_BY_TYPE.get(sensor_type, str(sensor_type))


def _open_text(path: Path) -> TextIO:
    return path.open("w", encoding="utf-8", errors="strict", newline="\n")


def _write_cameras(path: Path, model: Reconstruction) -> None:
    with _open_text(path) as stream:
        stream.write("# Camera list: CAMERA_ID, MODEL, WIDTH, HEIGHT, PARAMS[]\n")
        stream.write(f"# Number of cameras: {len(model.cameras)}\n")
        for camera in model.cameras.values():
            values = [
                str(camera.camera_id),
                camera.model_name,
                str(camera.width),
                str(camera.height),
                *(_format_float(value) for value in camera.params),
            ]
            stream.write(" ".join(values) + "\n")


def _write_images(path: Path, model: Reconstruction) -> None:
    with _open_text(path) as stream:
        stream.write(
            "# Image list with two lines per image: "
            "IMAGE_ID, QW, QX, QY, QZ, TX, TY, TZ, CAMERA_ID, NAME\n"
        )
        stream.write("# POINTS2D[] as (X, Y, POINT3D_ID)\n")
        stream.write(f"# Number of images: {len(model.images)}\n")
        for image in model.images.values():
            header = [
                str(image.image_id),
                *(_format_float(value) for value in image.qvec),
                *(_format_float(value) for value in image.tvec),
                str(image.camera_id),
                image.name,
            ]
            stream.write(" ".join(header) + "\n")
            point_values: list[str] = []
            for point in image.points2d:
                point_values.extend(
                    (
                        _format_float(point.x),
                        _format_float(point.y),
                        "-1" if point.point3d_id is None else str(point.point3d_id),
                    )
                )
            stream.write(" ".join(point_values) + "\n")


def _write_points3d(path: Path, model: Reconstruction) -> None:
    with _open_text(path) as stream:
        stream.write(
            "# 3D point list: POINT3D_ID, X, Y, Z, R, G, B, ERROR, "
            "TRACK[] as (IMAGE_ID, POINT2D_IDX)\n"
        )
        stream.write(f"# Number of points: {len(model.points3d)}\n")
        for point in model.points3d.values():
            values = [
                str(point.point3d_id),
                *(_format_float(value) for value in point.xyz),
                *(str(value) for value in point.rgb),
                _format_float(point.error),
            ]
            for track in point.track:
                values.extend((str(track.image_id), str(track.point2d_idx)))
            stream.write(" ".join(values) + "\n")


def _write_rigs(path: Path, model: Reconstruction) -> None:
    with _open_text(path) as stream:
        stream.write(
            "# Rig list: RIG_ID, NUM_SENSORS, REF_SENSOR_TYPE, REF_SENSOR_ID, SENSORS[]\n"
        )
        stream.write(f"# Number of rigs: {len(model.rigs)}\n")
        for rig in model.rigs.values():
            values = [
                str(rig.rig_id),
                str(len(rig.sensors) + 1),
                _sensor_type_text(rig.ref_sensor_type),
                str(rig.ref_sensor_id),
            ]
            for sensor in rig.sensors:
                values.extend(
                    (
                        _sensor_type_text(sensor.sensor_type),
                        str(sensor.sensor_id),
                        "1" if sensor.has_pose else "0",
                    )
                )
                if sensor.has_pose:
                    values.extend(_format_float(value) for value in sensor.qvec or ())
                    values.extend(_format_float(value) for value in sensor.tvec or ())
            stream.write(" ".join(values) + "\n")


def _write_frames(path: Path, model: Reconstruction) -> None:
    with _open_text(path) as stream:
        stream.write(
            "# Frame list: FRAME_ID, RIG_ID, RIG_FROM_WORLD[QW QX QY QZ TX TY TZ], "
            "NUM_DATA_IDS, DATA_IDS[]\n"
        )
        stream.write(f"# Number of frames: {len(model.frames)}\n")
        for frame in model.frames.values():
            values = [
                str(frame.frame_id),
                str(frame.rig_id),
                *(_format_float(value) for value in frame.qvec),
                *(_format_float(value) for value in frame.tvec),
                str(len(frame.data_ids)),
            ]
            for data_id in frame.data_ids:
                values.extend(
                    (
                        _sensor_type_text(data_id.sensor_type),
                        str(data_id.sensor_id),
                        str(data_id.data_id),
                    )
                )
            stream.write(" ".join(values) + "\n")


def _write_model_text_validated(
    directory: Path | str,
    model: Reconstruction,
    modern: bool,
) -> tuple[Path, ...]:
    """무결성이 검증된 모델을 Writer 고유 field 검사 후 TXT로 기록합니다."""

    target = Path(directory)
    if not target.is_dir():
        raise ValueError(f"TXT Export 대상 디렉터리가 존재하지 않습니다: {target}")
    _validate_model_fields_for_write(model, modern=modern)

    paths: list[Path] = []
    if modern:
        rigs_path = target / "rigs.txt"
        _write_rigs(rigs_path, model)
        paths.append(rigs_path)

    cameras_path = target / "cameras.txt"
    _write_cameras(cameras_path, model)
    paths.append(cameras_path)

    if modern:
        frames_path = target / "frames.txt"
        _write_frames(frames_path, model)
        paths.append(frames_path)

    images_path = target / "images.txt"
    points_path = target / "points3D.txt"
    _write_images(images_path, model)
    _write_points3d(points_path, model)
    paths.extend((images_path, points_path))
    return tuple(paths)


def write_model_text(
    directory: Path | str,
    model: Reconstruction,
    modern: bool,
) -> tuple[Path, ...]:
    """Reconstruction을 기존 layout과 같은 TXT 3개 또는 5개 파일로 기록합니다."""

    validate_reconstruction(model)
    return _write_model_text_validated(directory, model, modern)
