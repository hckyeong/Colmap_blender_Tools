"""COLMAP sparse model을 Project World와 최종 출력 좌표로 바꾸는 순수 함수를 제공합니다."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

import numpy as np

from ..alignment.types import (
    Sim3Transform,
    identity_sim3_transform,
    transform_positions as _transform_positions,
)
from ..colmap_io.types import (
    ColmapModelData,
    FrameDataRecord,
    FrameRecord,
    ImageRecord,
    RigRecord,
    RigSensorRecord,
)


_EPSILON: Final[float] = 1.0e-12
_ROTATION_TOLERANCE: Final[float] = 1.0e-9
_IDENTITY_3X3: Final[np.ndarray] = np.identity(3, dtype=np.float64)
_IDENTITY_4X4: Final[np.ndarray] = np.identity(4, dtype=np.float64)


class TransformError(ValueError):
    """COLMAP pose 또는 Sim(3) 변환 계약이 맞지 않을 때 발생합니다."""


def _finite_array(
    value: object,
    *,
    label: str,
    shape: tuple[int, ...],
) -> np.ndarray:
    """유한한 float64 배열을 복사해 고정 형상으로 반환합니다."""

    array = np.array(value, dtype=np.float64, order="C", copy=True)
    if array.shape != shape:
        raise TransformError(f"{label} 형상이 올바르지 않습니다: {array.shape}")
    if not np.isfinite(array).all():
        raise TransformError(f"{label}에 NaN 또는 무한대가 있습니다.")
    return array


def _require_sim3(transform: Sim3Transform) -> Sim3Transform:
    """호출 경계에서 검증된 Sim(3) 타입만 허용합니다."""

    if not isinstance(transform, Sim3Transform):
        raise TypeError("변환에는 Sim3Transform이 필요합니다.")
    return transform


def _canonicalize_quaternion(quaternion_wxyz: np.ndarray) -> np.ndarray:
    """동일 회전의 쿼터니언 부호를 COLMAP 내보내기용으로 결정합니다."""

    quaternion = np.ascontiguousarray(quaternion_wxyz, dtype=np.float64)
    if quaternion[0] < 0.0:
        quaternion = -quaternion
    elif quaternion[0] == 0.0:
        for component in quaternion[1:]:
            if component < 0.0:
                quaternion = -quaternion
                break
            if component > 0.0:
                break
    return quaternion


def quaternion_to_rotation_matrix(quaternion_wxyz: object) -> np.ndarray:
    """Hamilton 순서 (qw, qx, qy, qz)의 쿼터니언을 proper 회전으로 바꿉니다."""

    quaternion = _finite_array(
        quaternion_wxyz,
        label="쿼터니언",
        shape=(4,),
    )
    norm = float(np.linalg.norm(quaternion))
    if norm <= _EPSILON:
        raise TransformError("쿼터니언 크기가 0에 가깝습니다.")
    qw, qx, qy, qz = quaternion / norm
    rotation = np.asarray(
        (
            (
                1.0 - 2.0 * (qy * qy + qz * qz),
                2.0 * (qx * qy - qz * qw),
                2.0 * (qx * qz + qy * qw),
            ),
            (
                2.0 * (qx * qy + qz * qw),
                1.0 - 2.0 * (qx * qx + qz * qz),
                2.0 * (qy * qz - qx * qw),
            ),
            (
                2.0 * (qx * qz - qy * qw),
                2.0 * (qy * qz + qx * qw),
                1.0 - 2.0 * (qx * qx + qy * qy),
            ),
        ),
        dtype=np.float64,
    )
    return np.ascontiguousarray(rotation, dtype=np.float64)


def _validate_rotation(rotation_float64: object, *, label: str) -> np.ndarray:
    """반사·비직교 행렬을 거부하고 proper 회전 사본을 반환합니다."""

    rotation = _finite_array(rotation_float64, label=label, shape=(3, 3))
    if not np.allclose(
        rotation.T @ rotation,
        _IDENTITY_3X3,
        rtol=0.0,
        atol=_ROTATION_TOLERANCE,
    ):
        raise TransformError(f"{label}은 직교 회전이어야 합니다.")
    if not np.isclose(
        float(np.linalg.det(rotation)),
        1.0,
        rtol=0.0,
        atol=_ROTATION_TOLERANCE,
    ):
        raise TransformError(f"{label}의 행렬식은 +1이어야 합니다.")
    return rotation


def rotation_matrix_to_quaternion(rotation_float64: object) -> np.ndarray:
    """proper 회전 행렬을 정규화·결정적 부호의 Hamilton 쿼터니언으로 바꿉니다."""

    rotation = _validate_rotation(rotation_float64, label="회전 행렬")
    trace = float(np.trace(rotation))
    if trace > 0.0:
        scale = 2.0 * float(np.sqrt(trace + 1.0))
        if scale <= _EPSILON:
            raise TransformError("회전 행렬에서 쿼터니언을 계산할 수 없습니다.")
        quaternion = np.asarray(
            (
                0.25 * scale,
                (rotation[2, 1] - rotation[1, 2]) / scale,
                (rotation[0, 2] - rotation[2, 0]) / scale,
                (rotation[1, 0] - rotation[0, 1]) / scale,
            ),
            dtype=np.float64,
        )
    elif rotation[0, 0] > rotation[1, 1] and rotation[0, 0] > rotation[2, 2]:
        scale = 2.0 * float(np.sqrt(1.0 + rotation[0, 0] - rotation[1, 1] - rotation[2, 2]))
        if scale <= _EPSILON:
            raise TransformError("회전 행렬에서 쿼터니언을 계산할 수 없습니다.")
        quaternion = np.asarray(
            (
                (rotation[2, 1] - rotation[1, 2]) / scale,
                0.25 * scale,
                (rotation[0, 1] + rotation[1, 0]) / scale,
                (rotation[0, 2] + rotation[2, 0]) / scale,
            ),
            dtype=np.float64,
        )
    elif rotation[1, 1] > rotation[2, 2]:
        scale = 2.0 * float(np.sqrt(1.0 + rotation[1, 1] - rotation[0, 0] - rotation[2, 2]))
        if scale <= _EPSILON:
            raise TransformError("회전 행렬에서 쿼터니언을 계산할 수 없습니다.")
        quaternion = np.asarray(
            (
                (rotation[0, 2] - rotation[2, 0]) / scale,
                (rotation[0, 1] + rotation[1, 0]) / scale,
                0.25 * scale,
                (rotation[1, 2] + rotation[2, 1]) / scale,
            ),
            dtype=np.float64,
        )
    else:
        scale = 2.0 * float(np.sqrt(1.0 + rotation[2, 2] - rotation[0, 0] - rotation[1, 1]))
        if scale <= _EPSILON:
            raise TransformError("회전 행렬에서 쿼터니언을 계산할 수 없습니다.")
        quaternion = np.asarray(
            (
                (rotation[1, 0] - rotation[0, 1]) / scale,
                (rotation[0, 2] + rotation[2, 0]) / scale,
                (rotation[1, 2] + rotation[2, 1]) / scale,
                0.25 * scale,
            ),
            dtype=np.float64,
        )
    norm = float(np.linalg.norm(quaternion))
    if norm <= _EPSILON or not np.isfinite(norm):
        raise TransformError("회전 행렬에서 유효한 쿼터니언을 계산하지 못했습니다.")
    return np.ascontiguousarray(
        _canonicalize_quaternion(quaternion / norm),
        dtype=np.float64,
    )


def pose_to_rotation_translation(pose_wxyz_translation: object) -> tuple[np.ndarray, np.ndarray]:
    """COLMAP world→local pose를 회전 행렬과 이동 벡터로 분리합니다."""

    pose = _finite_array(
        pose_wxyz_translation,
        label="COLMAP 자세",
        shape=(7,),
    )
    return quaternion_to_rotation_matrix(pose[:4]), np.ascontiguousarray(pose[4:])


def rotation_translation_to_pose(
    rotation_float64: object,
    translation_float64: object,
) -> np.ndarray:
    """world→local 회전·이동을 결정적 COLMAP pose 배열로 조립합니다."""

    quaternion = rotation_matrix_to_quaternion(rotation_float64)
    translation = _finite_array(
        translation_float64,
        label="자세 이동",
        shape=(3,),
    )
    return np.ascontiguousarray(
        np.concatenate((quaternion, translation)),
        dtype=np.float64,
    )


def camera_center_from_pose(pose_wxyz_translation: object) -> np.ndarray:
    """COLMAP world→camera pose에서 카메라 중심의 world 좌표를 계산합니다."""

    rotation, translation = pose_to_rotation_translation(pose_wxyz_translation)
    return np.ascontiguousarray(-rotation.T @ translation, dtype=np.float64)


def compose_project_world_transform(
    alignment: Sim3Transform,
    display_matrix_float64: object,
) -> Sim3Transform:
    """Alignment × Display Axis 순서의 source→Project World Sim(3)을 만듭니다."""

    alignment_transform = _require_sim3(alignment)
    display_matrix = _finite_array(
        display_matrix_float64,
        label="Display Axis 행렬",
        shape=(4, 4),
    )
    if not np.allclose(
        display_matrix[3],
        _IDENTITY_4X4[3],
        rtol=0.0,
        atol=_ROTATION_TOLERANCE,
    ):
        raise TransformError("Display Axis 행렬의 마지막 행이 올바르지 않습니다.")
    _validate_rotation(display_matrix[:3, :3], label="Display Axis 회전")
    return Sim3Transform(
        matrix_float64=alignment_transform.matrix_float64 @ display_matrix,
    )


def compose_anchor_colmap_output_transform(
    alignment: Sim3Transform,
    display_matrix_float64: object,
    anchor_display_matrix_float64: object,
) -> Sim3Transform:
    """Project World 결과를 Anchor의 원래 COLMAP 축으로 되돌린 출력 Sim(3)을 만듭니다."""

    project_world_transform = compose_project_world_transform(
        alignment,
        display_matrix_float64,
    )
    anchor_display_transform = compose_project_world_transform(
        identity_sim3_transform(),
        anchor_display_matrix_float64,
    )
    return Sim3Transform(
        matrix_float64=(
            anchor_display_transform.inverse_float64
            @ project_world_transform.matrix_float64
        ),
    )


def transform_point_positions(positions: object, transform: Sim3Transform) -> np.ndarray:
    """N×3 Point3D 좌표를 전달된 source→출력 Sim(3)으로 변환합니다."""

    return _transform_positions(_require_sim3(transform), positions)


def transform_cam_from_world(
    cam_from_world: object,
    transform: Sim3Transform,
) -> np.ndarray:
    """world→camera pose를 전달된 source→출력 Sim(3)에 맞춰 변환합니다."""

    world_transform = _require_sim3(transform)
    rotation_cw, translation_cw = pose_to_rotation_translation(cam_from_world)
    center_world = -rotation_cw.T @ translation_cw
    center_project = (
        world_transform.uniform_scale * world_transform.rotation_float64 @ center_world
        + world_transform.translation_float64
    )
    rotation_project_to_camera = rotation_cw @ world_transform.rotation_float64.T
    translation_project_to_camera = -rotation_project_to_camera @ center_project
    return rotation_translation_to_pose(
        rotation_project_to_camera,
        translation_project_to_camera,
    )


def transform_rig_from_world(
    rig_from_world: object,
    transform: Sim3Transform,
) -> np.ndarray:
    """world→rig pose를 image pose와 같은 world-frame 변경식으로 변환합니다."""

    return transform_cam_from_world(rig_from_world, transform)


def transform_image_record(image: ImageRecord, transform: Sim3Transform) -> ImageRecord:
    """이미지의 pose만 지정한 출력 좌표 기준으로 바꾼 새 불변 레코드를 만듭니다."""

    if not isinstance(image, ImageRecord):
        raise TypeError("이미지 변환에는 ImageRecord가 필요합니다.")
    return ImageRecord(
        image_id=image.image_id,
        camera_id=image.camera_id,
        name=image.name,
        cam_from_world=transform_cam_from_world(image.cam_from_world, transform),
    )


def transform_sensor_from_rig(
    sensor: RigSensorRecord,
    transform: Sim3Transform,
) -> RigSensorRecord:
    """리그 내부 센서의 회전은 유지하고 baseline 이동만 전역 scale로 조정합니다."""

    if not isinstance(sensor, RigSensorRecord):
        raise TypeError("리그 센서 변환에는 RigSensorRecord가 필요합니다.")
    world_transform = _require_sim3(transform)
    if sensor.sensor_from_rig is None:
        return RigSensorRecord(
            sensor_type=sensor.sensor_type,
            sensor_id=sensor.sensor_id,
        )
    rotation, translation = pose_to_rotation_translation(sensor.sensor_from_rig)
    return RigSensorRecord(
        sensor_type=sensor.sensor_type,
        sensor_id=sensor.sensor_id,
        sensor_from_rig=rotation_translation_to_pose(
            rotation,
            world_transform.uniform_scale * translation,
        ),
    )


def transform_rig_record(rig: RigRecord, transform: Sim3Transform) -> RigRecord:
    """modern rig의 모든 sensor_from_rig baseline을 지정한 출력 scale에 맞춥니다."""

    if not isinstance(rig, RigRecord):
        raise TypeError("리그 변환에는 RigRecord가 필요합니다.")
    transformed_sensors = tuple(
        transform_sensor_from_rig(sensor, transform)
        for sensor in rig.sensors
    )
    reference_sensor = transformed_sensors[0] if rig.reference_sensor is not None else None
    return RigRecord(
        rig_id=rig.rig_id,
        reference_sensor=reference_sensor,
        sensors=transformed_sensors,
    )


def transform_rig_records(
    rigs: Mapping[int, RigRecord],
    transform: Sim3Transform,
) -> dict[int, RigRecord]:
    """리그 ID 오름차순으로 변환한 독립 사전을 반환합니다."""

    return {
        rig_id: transform_rig_record(rigs[rig_id], transform)
        for rig_id in sorted(rigs)
    }


def transform_frame_record(frame: FrameRecord, transform: Sim3Transform) -> FrameRecord:
    """modern frame의 world→rig pose만 지정한 출력 좌표 기준으로 변환합니다."""

    if not isinstance(frame, FrameRecord):
        raise TypeError("프레임 변환에는 FrameRecord가 필요합니다.")
    return FrameRecord(
        frame_id=frame.frame_id,
        rig_id=frame.rig_id,
        rig_from_world=transform_rig_from_world(frame.rig_from_world, transform),
        data_ids=frame.data_ids,
    )


def transform_frame_records(
    frames: Mapping[int, FrameRecord],
    transform: Sim3Transform,
) -> dict[int, FrameRecord]:
    """프레임 ID 오름차순으로 변환한 독립 사전을 반환합니다."""

    return {
        frame_id: transform_frame_record(frames[frame_id], transform)
        for frame_id in sorted(frames)
    }


def compose_cam_from_world(
    sensor_from_rig: object | None,
    rig_from_world: object,
) -> np.ndarray:
    """sensor_from_rig × rig_from_world 관계에서 world→camera pose를 합성합니다."""

    rig_rotation, rig_translation = pose_to_rotation_translation(rig_from_world)
    if sensor_from_rig is None:
        return rotation_translation_to_pose(rig_rotation, rig_translation)
    sensor_rotation, sensor_translation = pose_to_rotation_translation(sensor_from_rig)
    return rotation_translation_to_pose(
        sensor_rotation @ rig_rotation,
        sensor_rotation @ rig_translation + sensor_translation,
    )


def normalize_legacy_rigs_and_frames(
    model: ColmapModelData,
    transform: Sim3Transform | None = None,
) -> tuple[dict[int, RigRecord], dict[int, FrameRecord]]:
    """legacy 모델에 camera별 trivial rig와 image별 frame을 결정적으로 만듭니다."""

    if not isinstance(model, ColmapModelData):
        raise TypeError("legacy 정규화에는 ColmapModelData가 필요합니다.")
    if bool(model.rigs) != bool(model.frames):
        raise TransformError("modern COLMAP 모델의 rigs와 frames는 함께 있어야 합니다.")
    if model.rigs:
        return dict(model.rigs), dict(model.frames)

    world_transform = identity_sim3_transform() if transform is None else _require_sim3(transform)
    rigs: dict[int, RigRecord] = {}
    for camera_id in sorted(model.cameras):
        reference_sensor = RigSensorRecord(
            sensor_type="CAMERA",
            sensor_id=camera_id,
        )
        rigs[camera_id] = RigRecord(
            rig_id=camera_id,
            reference_sensor=reference_sensor,
            sensors=(reference_sensor,),
        )

    frames: dict[int, FrameRecord] = {}
    for image_id in sorted(model.images.image_records):
        image = model.images.image_records[image_id]
        frames[image_id] = FrameRecord(
            frame_id=image_id,
            rig_id=image.camera_id,
            rig_from_world=transform_cam_from_world(
                image.cam_from_world,
                world_transform,
            ),
            data_ids=(
                FrameDataRecord(
                    sensor_type="CAMERA",
                    sensor_id=image.camera_id,
                    data_id=image.image_id,
                ),
            ),
        )
    return rigs, frames


__all__ = (
    "TransformError",
    "camera_center_from_pose",
    "compose_anchor_colmap_output_transform",
    "compose_cam_from_world",
    "compose_project_world_transform",
    "normalize_legacy_rigs_and_frames",
    "pose_to_rotation_translation",
    "quaternion_to_rotation_matrix",
    "rotation_matrix_to_quaternion",
    "rotation_translation_to_pose",
    "transform_cam_from_world",
    "transform_frame_record",
    "transform_frame_records",
    "transform_image_record",
    "transform_point_positions",
    "transform_rig_from_world",
    "transform_rig_record",
    "transform_rig_records",
    "transform_sensor_from_rig",
)
