"""COLMAP 공식 text sparse 형식을 표준 라이브러리만으로 읽습니다."""

from __future__ import annotations

from pathlib import Path

from .camera_models import model_by_name
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

SENSOR_TYPE_BY_NAME = {"CAMERA": 0, "IMU": 1}


def _data_lines(path: Path) -> list[str]:
    with path.open("r", encoding="utf-8-sig") as stream:
        return [line.strip() for line in stream if line.strip() and not line.lstrip().startswith("#")]


def _sensor_type(value: str) -> int:
    if value in SENSOR_TYPE_BY_NAME:
        return SENSOR_TYPE_BY_NAME[value]
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(f"알 수 없는 sensor type입니다: {value}") from exc


def read_cameras_text(path: Path) -> dict[int, CameraRecord]:
    records: dict[int, CameraRecord] = {}
    for line_number, line in enumerate(_data_lines(path), 1):
        values = line.split()
        if len(values) < 5:
            raise ValueError(f"{path}의 camera 레코드가 불완전합니다: 데이터 행 {line_number}")
        camera_id = int(values[0])
        model = model_by_name(values[1])
        params = tuple(float(value) for value in values[4:])
        if len(params) != model.num_params:
            raise ValueError(
                f"CAMERA_ID {camera_id}의 {model.name} 파라미터 수가 "
                f"{len(params)}개이며 예상값은 {model.num_params}개입니다."
            )
        if camera_id in records:
            raise ValueError(f"중복 CAMERA_ID입니다: {camera_id}")
        records[camera_id] = CameraRecord(
            camera_id, model.model_id, model.name, int(values[2]), int(values[3]), params
        )
    return records


def read_images_text(path: Path) -> dict[int, ImageRecord]:
    with path.open("r", encoding="utf-8-sig") as stream:
        physical_lines = stream.readlines()
    records: dict[int, ImageRecord] = {}
    index = 0
    while index < len(physical_lines):
        header = physical_lines[index].strip()
        index += 1
        if not header or header.startswith("#"):
            continue
        fields = header.split(maxsplit=9)
        if len(fields) != 10:
            raise ValueError(f"{path}의 image 헤더가 불완전합니다: {header}")
        if index >= len(physical_lines):
            raise ValueError(f"IMAGE_ID {fields[0]}의 Point2D 행이 누락되었습니다.")
        points_line = physical_lines[index].strip()
        index += 1
        point_values = points_line.split()
        if len(point_values) % 3:
            raise ValueError(f"IMAGE_ID {fields[0]}의 Point2D 값 개수가 3의 배수가 아닙니다.")
        points = []
        for offset in range(0, len(point_values), 3):
            point3d_value = int(point_values[offset + 2])
            points.append(
                Point2DRecord(
                    float(point_values[offset]),
                    float(point_values[offset + 1]),
                    None if point3d_value == -1 else point3d_value,
                )
            )
        image_id = int(fields[0])
        if image_id in records:
            raise ValueError(f"중복 IMAGE_ID입니다: {image_id}")
        records[image_id] = ImageRecord(
            image_id=image_id,
            qvec=tuple(float(value) for value in fields[1:5]),
            tvec=tuple(float(value) for value in fields[5:8]),
            camera_id=int(fields[8]),
            name=fields[9],
            points2d=tuple(points),
        )
    return records


def read_points3d_text(path: Path) -> dict[int, Point3DRecord]:
    records: dict[int, Point3DRecord] = {}
    for line in _data_lines(path):
        values = line.split()
        if len(values) < 8 or (len(values) - 8) % 2:
            raise ValueError(f"{path}의 Point3D 레코드가 불완전합니다: {line}")
        point3d_id = int(values[0])
        track = tuple(
            TrackElement(int(values[offset]), int(values[offset + 1]))
            for offset in range(8, len(values), 2)
        )
        if point3d_id in records:
            raise ValueError(f"중복 POINT3D_ID입니다: {point3d_id}")
        records[point3d_id] = Point3DRecord(
            point3d_id=point3d_id,
            xyz=tuple(float(value) for value in values[1:4]),
            rgb=tuple(int(value) for value in values[4:7]),
            error=float(values[7]),
            track=track,
        )
    return records


def read_rigs_text(path: Path) -> dict[int, RigRecord]:
    records: dict[int, RigRecord] = {}
    for line in _data_lines(path):
        values = line.split()
        if len(values) < 4:
            raise ValueError(f"{path}의 Rig 레코드가 불완전합니다: {line}")
        rig_id = int(values[0])
        num_sensors = int(values[1])
        if num_sensors < 1:
            raise ValueError(f"RIG_ID {rig_id}의 NUM_SENSORS는 1 이상이어야 합니다.")
        ref_type = _sensor_type(values[2])
        ref_id = int(values[3])
        sensors: list[SensorRecord] = []
        offset = 4
        # 파일의 SENSORS[]에는 별도 헤더로 기록된 기준 sensor를 제외한 항목만 있습니다.
        for _ in range(num_sensors - 1):
            if offset + 3 > len(values):
                raise ValueError(f"RIG_ID {rig_id}의 sensor 레코드가 누락되었습니다.")
            sensor_type = _sensor_type(values[offset])
            sensor_id = int(values[offset + 1])
            has_pose = bool(int(values[offset + 2]))
            offset += 3
            qvec = tvec = None
            if has_pose:
                if offset + 7 > len(values):
                    raise ValueError(f"RIG_ID {rig_id}의 sensor pose가 불완전합니다.")
                qvec = tuple(float(value) for value in values[offset : offset + 4])
                tvec = tuple(float(value) for value in values[offset + 4 : offset + 7])
                offset += 7
            sensors.append(SensorRecord(sensor_type, sensor_id, has_pose, qvec, tvec))
        if offset != len(values):
            raise ValueError(f"RIG_ID {rig_id} 뒤에 해석되지 않은 값이 있습니다.")
        if rig_id in records:
            raise ValueError(f"중복 RIG_ID입니다: {rig_id}")
        records[rig_id] = RigRecord(rig_id, ref_type, ref_id, tuple(sensors))
    return records


def read_frames_text(path: Path) -> dict[int, FrameRecord]:
    records: dict[int, FrameRecord] = {}
    for line in _data_lines(path):
        values = line.split()
        if len(values) < 10:
            raise ValueError(f"{path}의 Frame 레코드가 불완전합니다: {line}")
        frame_id = int(values[0])
        num_data_ids = int(values[9])
        if len(values) != 10 + num_data_ids * 3:
            raise ValueError(f"FRAME_ID {frame_id}의 DATA_IDS 개수가 선언과 다릅니다.")
        data_ids = tuple(
            DataIdRecord(
                _sensor_type(values[offset]),
                int(values[offset + 1]),
                int(values[offset + 2]),
            )
            for offset in range(10, len(values), 3)
        )
        if frame_id in records:
            raise ValueError(f"중복 FRAME_ID입니다: {frame_id}")
        records[frame_id] = FrameRecord(
            frame_id=frame_id,
            rig_id=int(values[1]),
            qvec=tuple(float(value) for value in values[2:6]),
            tvec=tuple(float(value) for value in values[6:9]),
            data_ids=data_ids,
        )
    return records


def read_model_text(directory: Path, modern: bool) -> Reconstruction:
    return Reconstruction(
        cameras=read_cameras_text(directory / "cameras.txt"),
        images=read_images_text(directory / "images.txt"),
        points3d=read_points3d_text(directory / "points3D.txt"),
        rigs=read_rigs_text(directory / "rigs.txt") if modern else {},
        frames=read_frames_text(directory / "frames.txt") if modern else {},
    )
