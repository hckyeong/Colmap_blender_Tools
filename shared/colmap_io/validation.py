"""적재 전 COLMAP sparse model의 수치와 양방향 참조 무결성을 검증합니다."""

from __future__ import annotations

from collections.abc import Mapping

import numpy as np

import logging
from .camera_models import camera_model_from_id
from .errors import ColmapParseError, ColmapValidationError
from .types import (
    CameraRecord,
    ColmapModelData,
    FrameRecord,
    INVALID_POINT3D_ID,
    RigRecord,
    RigSensorRecord,
)


LOGGER = logging.getLogger(__name__)
_QUATERNION_EPSILON = 1.0e-12
_VALID_SENSOR_TYPES = frozenset({"CAMERA", "IMU"})


def _raise(message: str) -> None:
    """검증 실패를 같은 사용자용 예외 형식으로 발생시킵니다."""
    raise ColmapValidationError(message)


def _validate_positive_unique_ids(
    ids: np.ndarray,
    label: str,
    invalid_value: int,
) -> None:
    """ID 배열이 0보다 크고 중복되지 않는지 확인합니다."""
    if ids.size == 0:
        return
    if np.any(ids == 0):
        _raise(f"{label}는 0보다 커야 합니다.")
    if np.any(ids == invalid_value):
        _raise(f"{label}에 COLMAP 예약 invalid ID가 있습니다.")
    if np.unique(ids).size != ids.size:
        _raise(f"{label}에 중복 ID가 있습니다.")


def _validate_finite(values: np.ndarray, label: str) -> None:
    """배열에 NaN·무한대가 없는지 확인합니다."""
    if not np.all(np.isfinite(values)):
        _raise(f"{label}에 NaN 또는 무한대가 있습니다.")


def _validate_pose(pose: np.ndarray, label: str) -> None:
    """쿼터니언·이동 벡터의 유한성과 영(0) 쿼터니언을 확인합니다."""
    _validate_finite(pose, label)
    if float(np.linalg.norm(pose[:4])) <= _QUATERNION_EPSILON:
        _raise(f"{label}의 쿼터니언 크기가 0에 가깝습니다.")


def _validate_cameras(cameras: Mapping[int, CameraRecord]) -> None:
    """카메라 ID, 모델 정의, 크기와 내부 파라미터를 검증합니다."""
    _validate_positive_unique_ids(
        np.asarray(tuple(cameras), dtype=np.uint64),
        "CAMERA_ID",
        np.iinfo(np.uint32).max,
    )
    for camera_id, camera in cameras.items():
        if camera.camera_id != camera_id:
            _raise(f"CAMERA_ID 사전 키와 레코드 값이 다릅니다: {camera_id}")
        try:
            model = camera_model_from_id(camera.model_id)
        except ColmapParseError as error:
            raise ColmapValidationError(str(error)) from error
        if camera.model_name != model.name:
            _raise(f"CAMERA_ID {camera_id}의 모델 이름과 ID가 다릅니다.")
        if camera.params_float64.shape[0] != model.parameter_count:
            _raise(f"CAMERA_ID {camera_id}의 내부 파라미터 수가 모델 정의와 다릅니다.")
        if camera.width <= 0 or camera.height <= 0:
            _raise(f"CAMERA_ID {camera_id}의 이미지 너비·높이는 양수여야 합니다.")
        _validate_finite(camera.params_float64, f"CAMERA_ID {camera_id} 내부 파라미터")


def _validate_images(model: ColmapModelData) -> None:
    """이미지 ID·카메라 참조·자세·이름과 points2D 수치를 검증합니다."""
    images = model.images
    image_ids = np.asarray(tuple(images.image_records), dtype=np.uint64)
    _validate_positive_unique_ids(image_ids, "IMAGE_ID", np.iinfo(np.uint32).max)
    for image_id, image in images.image_records.items():
        if image.image_id != image_id:
            _raise(f"IMAGE_ID 사전 키와 레코드 값이 다릅니다: {image_id}")
        if image.camera_id not in model.cameras:
            _raise(f"IMAGE_ID {image_id}가 존재하지 않는 CAMERA_ID {image.camera_id}를 참조합니다.")
        if not image.name or image.name.strip() != image.name or "\0" in image.name:
            _raise(f"IMAGE_ID {image_id}의 이미지 이름이 비어 있거나 올바르지 않습니다.")
        try:
            image.name.encode("utf-8", errors="strict")
        except UnicodeEncodeError as error:
            raise ColmapValidationError(
                f"IMAGE_ID {image_id}의 이미지 이름이 UTF-8로 인코딩되지 않습니다."
            ) from error
        _validate_pose(image.cam_from_world, f"IMAGE_ID {image_id} cam_from_world")
    _validate_finite(images.point2d_xy, "points2D 좌표")


def _validate_points(model: ColmapModelData, *, allow_empty_points: bool = False) -> None:
    """빈 점군 허용 정책을 적용하고 점 ID·좌표·오차·표시 배열을 검증합니다."""
    points = model.points
    if points.count == 0 and not allow_empty_points:
        _raise("현재 기능에서 사용할 수 없는 0개 점 COLMAP 모델은 가져올 수 없습니다.")
    _validate_positive_unique_ids(
        points.point_ids,
        "POINT3D_ID",
        np.iinfo(np.uint64).max,
    )
    _validate_finite(points.positions, "POINT3D XYZ")
    _validate_finite(points.errors, "POINT3D 재투영 오차")
    if points.colors.dtype != np.uint8 or points.colors.shape != (points.count, 3):
        _raise("POINT3D RGB 배열 형식이 올바르지 않습니다.")
    if points.display_indices.size and np.any(points.display_indices >= points.count):
        _raise("POINT3D 표시 인덱스가 원본 배열 범위를 벗어났습니다.")


def _validate_rig_sensor(
    sensor: RigSensorRecord,
    *,
    rig_id: int,
    cameras: Mapping[int, CameraRecord],
) -> None:
    """리그 센서 타입·ID·카메라 참조와 선택 자세를 검증합니다."""
    if sensor.sensor_type not in _VALID_SENSOR_TYPES:
        _raise(f"RIG_ID {rig_id}에 지원하지 않는 센서 타입이 있습니다: {sensor.sensor_type}")
    if sensor.sensor_id <= 0 or sensor.sensor_id == np.iinfo(np.uint32).max:
        _raise(f"RIG_ID {rig_id}의 센서 ID는 0보다 커야 합니다.")
    if sensor.sensor_type == "CAMERA" and sensor.sensor_id not in cameras:
        _raise(f"RIG_ID {rig_id}가 존재하지 않는 CAMERA_ID {sensor.sensor_id}를 참조합니다.")
    if sensor.sensor_from_rig is not None:
        _validate_pose(sensor.sensor_from_rig, f"RIG_ID {rig_id} sensor_from_rig")


def _validate_rigs(rigs: Mapping[int, RigRecord], cameras: Mapping[int, CameraRecord]) -> None:
    """선택 리그의 센서 관계와 중복 없는 기준 센서를 검증합니다."""
    _validate_positive_unique_ids(
        np.asarray(tuple(rigs), dtype=np.uint64),
        "RIG_ID",
        np.iinfo(np.uint32).max,
    )
    for rig_id, rig in rigs.items():
        if rig.rig_id != rig_id:
            _raise(f"RIG_ID 사전 키와 레코드 값이 다릅니다: {rig_id}")
        if rig.reference_sensor is None or not rig.sensors:
            _raise(f"RIG_ID {rig_id}에 기준 센서가 없습니다.")
        if rig.sensors[0] != rig.reference_sensor:
            _raise(f"RIG_ID {rig_id}의 첫 센서가 기준 센서와 다릅니다.")
        seen_sensor_keys: set[tuple[str, int]] = set()
        for sensor in rig.sensors:
            _validate_rig_sensor(sensor, rig_id=rig_id, cameras=cameras)
            sensor_key = (sensor.sensor_type, sensor.sensor_id)
            if sensor_key in seen_sensor_keys:
                _raise(f"RIG_ID {rig_id}에 중복 센서가 있습니다.")
            seen_sensor_keys.add(sensor_key)


def _validate_frames(model: ColmapModelData) -> None:
    """선택 프레임의 리그·센서·이미지 양방향 연결을 검증합니다."""
    frames: Mapping[int, FrameRecord] = model.frames
    _validate_positive_unique_ids(
        np.asarray(tuple(frames), dtype=np.uint64),
        "FRAME_ID",
        np.iinfo(np.uint32).max,
    )
    frame_image_ids: set[int] = set()
    for frame_id, frame in frames.items():
        if frame.frame_id != frame_id:
            _raise(f"FRAME_ID 사전 키와 레코드 값이 다릅니다: {frame_id}")
        rig = model.rigs.get(frame.rig_id)
        if rig is None:
            _raise(f"FRAME_ID {frame_id}가 존재하지 않는 RIG_ID {frame.rig_id}를 참조합니다.")
        _validate_pose(frame.rig_from_world, f"FRAME_ID {frame_id} rig_from_world")
        rig_sensor_keys = {(sensor.sensor_type, sensor.sensor_id) for sensor in rig.sensors}
        data_keys: set[tuple[str, int, int]] = set()
        for data in frame.data_ids:
            sensor_key = (data.sensor_type, data.sensor_id)
            data_key = (data.sensor_type, data.sensor_id, data.data_id)
            if data.sensor_type not in _VALID_SENSOR_TYPES:
                _raise(f"FRAME_ID {frame_id}에 지원하지 않는 센서 타입이 있습니다.")
            if sensor_key not in rig_sensor_keys:
                _raise(f"FRAME_ID {frame_id}의 센서가 RIG_ID {frame.rig_id}에 없습니다.")
            if data.data_id <= 0 or data.data_id == np.iinfo(np.uint32).max:
                _raise(f"FRAME_ID {frame_id}의 DATA_ID는 0보다 커야 합니다.")
            if data_key in data_keys:
                _raise(f"FRAME_ID {frame_id}에 중복 DATA_ID 연결이 있습니다.")
            data_keys.add(data_key)
            if data.sensor_type != "CAMERA":
                continue
            image = model.images.image_records.get(data.data_id)
            if image is None:
                _raise(f"FRAME_ID {frame_id}가 존재하지 않는 IMAGE_ID {data.data_id}를 참조합니다.")
            if image.camera_id != data.sensor_id:
                _raise(
                    f"FRAME_ID {frame_id}의 IMAGE_ID {data.data_id} CAMERA_ID가 센서 ID와 다릅니다."
                )
            if data.data_id in frame_image_ids:
                _raise(f"IMAGE_ID {data.data_id}가 둘 이상의 프레임에 연결되었습니다.")
            frame_image_ids.add(data.data_id)
    if frames and frame_image_ids != set(model.images.image_records):
        _raise("frames의 CAMERA DATA_ID와 images의 IMAGE_ID 집합이 일치하지 않습니다.")


def _validate_observation_links(model: ColmapModelData) -> None:
    """points3D 트랙과 images points2D의 양방향 참조를 하나도 빠짐없이 검증합니다."""
    # Blender COLMAP 0.16.7의 GPL-3.0-or-later 선형 pair 검증을 배열 계약에 맞게 이식했습니다.
    points = model.points
    images = model.images
    point_ids = {int(point_id) for point_id in points.point_ids}
    image_index_by_id = {
        image_id: index for index, image_id in enumerate(images.image_records)
    }
    expected_track_pairs: dict[tuple[int, int], int] = {}
    for image_index, image_id in enumerate(images.image_records):
        image_start = int(images.point2d_offsets[image_index])
        image_stop = int(images.point2d_offsets[image_index + 1])
        linked_ids = images.point2d_point3d_ids[image_start:image_stop]
        for point2d_index, point_id_value in enumerate(linked_ids):
            point_id = int(point_id_value)
            if point_id == INVALID_POINT3D_ID:
                continue
            if point_id not in point_ids:
                _raise(f"IMAGE_ID {image_id}가 존재하지 않는 POINT3D_ID {point_id}를 참조합니다.")
            expected_track_pairs[(image_id, point2d_index)] = point_id

    actual_track_pairs: dict[tuple[int, int], int] = {}
    mismatched_track_pairs: set[tuple[int, int]] = set()
    for point_index, point_id_value in enumerate(points.point_ids):
        point_id = int(point_id_value)
        track_range = points.track_range(point_index)
        for track_index in range(track_range.start, track_range.stop):
            image_id = int(points.track_image_ids[track_index])
            point2d_index = int(points.track_point2d_indices[track_index])
            image_index = image_index_by_id.get(image_id)
            if image_index is None:
                _raise(f"POINT3D_ID {point_id}의 트랙이 존재하지 않는 IMAGE_ID {image_id}를 참조합니다.")
            image_slice = slice(
                int(images.point2d_offsets[image_index]),
                int(images.point2d_offsets[image_index + 1]),
            )
            if point2d_index >= image_slice.stop - image_slice.start:
                _raise(
                    f"POINT3D_ID {point_id}의 POINT2D_IDX {point2d_index}가 IMAGE_ID {image_id} 범위를 벗어났습니다."
                )
            track_key = (image_id, point2d_index)
            if track_key in actual_track_pairs:
                _raise(f"중복 Point3D 트랙 관측입니다: IMAGE_ID {image_id}, POINT2D_IDX {point2d_index}")
            actual_track_pairs[track_key] = point_id
            if expected_track_pairs.get(track_key) != point_id:
                mismatched_track_pairs.add(track_key)

    for track_key, expected_point_id in expected_track_pairs.items():
        if actual_track_pairs.get(track_key) != expected_point_id:
            mismatched_track_pairs.add(track_key)

    if mismatched_track_pairs:
        image_id, point2d_index = min(mismatched_track_pairs)
        expected_point_id = expected_track_pairs.get((image_id, point2d_index))
        actual_point_id = actual_track_pairs.get((image_id, point2d_index))
        if actual_point_id is None and expected_point_id is not None:
            _raise(
                f"IMAGE_ID {image_id}의 POINT3D_ID {expected_point_id}가 points3D 트랙에 없습니다."
            )
        _raise(
            f"POINT3D 트랙과 IMAGE_ID {image_id}의 POINT2D_IDX {point2d_index} 참조가 "
            f"일치하지 않습니다: points2D={expected_point_id}, track={actual_point_id}"
        )


def calculate_bounds(points: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """유효한 float64 점 좌표에서 미리 계산한 축 정렬 경계를 반환합니다."""
    if points.ndim != 2 or points.shape[1] != 3 or points.shape[0] == 0:
        raise ColmapValidationError("점 경계를 계산할 유효한 3D 점 배열이 없습니다.")
    _validate_finite(points, "점 경계 계산 좌표")
    return (
        np.ascontiguousarray(np.min(points, axis=0), dtype=np.float64),
        np.ascontiguousarray(np.max(points, axis=0), dtype=np.float64),
    )


def validate_model_structure(
    model: ColmapModelData,
    *,
    allow_empty_points: bool = False,
) -> None:
    """참고 표본을 전체 모델로 취급하지 않고 전체 입력의 수치·참조·경계를 검증합니다."""
    if not isinstance(model, ColmapModelData):
        raise TypeError("전체 구조 검증에는 완전한 ColmapModelData가 필요합니다.")
    # 배열 자료형·형상·오프셋은 불변 자료형 생성자가 먼저 검증합니다.
    _validate_cameras(model.cameras)
    _validate_images(model)
    _validate_points(model, allow_empty_points=allow_empty_points)
    _validate_rigs(model.rigs, model.cameras)
    _validate_frames(model)
    if model.points.count:
        bounds_min, bounds_max = calculate_bounds(model.points.positions)
    else:
        # 공통 파서의 빈 점군은 영벡터 경계를 사용합니다.
        bounds_min = bounds_max = np.zeros(3, dtype=np.float64)
    if not np.array_equal(model.bounds_min, bounds_min) or not np.array_equal(model.bounds_max, bounds_max):
        _raise("저장된 점 경계가 원본 POINT3D 좌표와 일치하지 않습니다.")


def validate_reference_preview(model) -> None:
    """읽는 중 검사한 원본 수치와 별개로 참고 자료의 카메라·리그·프레임을 검증합니다."""
    from .reference_preview import ColmapReferencePreview
    if not isinstance(model, ColmapReferencePreview):
        raise TypeError("참고 구조 검증에는 ColmapReferencePreview가 필요합니다.")
    _validate_cameras(model.cameras)
    _validate_images(model)
    _validate_rigs(model.rigs, model.cameras)
    _validate_frames(model)


def validate_model(model: ColmapModelData) -> None:
    """Runtime 등록 전에 sparse model 전체의 엄격한 무결성을 검증합니다."""
    LOGGER.info(
        "event=validation_started stage=integrity camera_count=%d image_count=%d point_count=%d",
        len(model.cameras),
        model.images.count,
        model.points.count,
    )
    try:
        validate_model_structure(model)
        _validate_observation_links(model)
    except ColmapValidationError:
        LOGGER.warning("event=validation_failed stage=integrity source=%s", model.source_path)
        raise
    LOGGER.info(
        "event=validation_completed stage=integrity camera_count=%d image_count=%d point_count=%d track_count=%d",
        len(model.cameras),
        model.images.count,
        model.points.count,
        model.points.track_image_ids.shape[0],
    )
