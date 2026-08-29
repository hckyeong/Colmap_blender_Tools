"""Reconstruction의 참조 및 수치 무결성을 검사합니다."""

from __future__ import annotations

import math

from .camera_models import model_by_id, pinhole_parameters
from .model import Reconstruction


def validate_reconstruction(model: Reconstruction) -> tuple[str, ...]:
    errors: list[str] = []
    warnings: list[str] = []

    for camera_key, camera in model.cameras.items():
        if camera_key != camera.camera_id:
            errors.append(
                f"Camera dict key {camera_key}와 레코드 CAMERA_ID {camera.camera_id}가 다릅니다."
            )
        try:
            registry_model = model_by_id(camera.model_id)
        except ValueError as exc:
            errors.append(str(exc))
            continue
        if camera.model_name != registry_model.name:
            errors.append(f"CAMERA_ID {camera.camera_id}의 model ID와 이름이 일치하지 않습니다.")
        if len(camera.params) != registry_model.num_params:
            errors.append(f"CAMERA_ID {camera.camera_id}의 파라미터 수가 잘못되었습니다.")
        if camera.width <= 0 or camera.height <= 0:
            errors.append(f"CAMERA_ID {camera.camera_id}의 해상도가 유효하지 않습니다.")
        if not all(math.isfinite(value) for value in camera.params):
            errors.append(f"CAMERA_ID {camera.camera_id}의 파라미터에 유한하지 않은 값이 있습니다.")
        if registry_model.exact_blender_projection:
            fx, fy, _, _ = pinhole_parameters(camera.model_name, camera.params)
            if fx <= 0.0 or fy <= 0.0:
                errors.append(f"CAMERA_ID {camera.camera_id}의 focal length가 0보다 커야 합니다.")
        if not registry_model.exact_blender_projection:
            warnings.append(
                f"CAMERA_ID {camera.camera_id} ({camera.model_name})는 원본 파라미터를 "
                "보존하지만 Blender 배경 투영의 정확성을 보장하지 않습니다."
            )

    expected_track_pairs: dict[tuple[int, int], int] = {}
    for image_key, image in model.images.items():
        if image_key != image.image_id:
            errors.append(
                f"Image dict key {image_key}와 레코드 IMAGE_ID {image.image_id}가 다릅니다."
            )
        if image.camera_id not in model.cameras:
            errors.append(
                f"IMAGE_ID {image.image_id}가 존재하지 않는 CAMERA_ID {image.camera_id}를 참조합니다."
            )
        qnorm = math.sqrt(sum(value * value for value in image.qvec))
        if not math.isfinite(qnorm) or qnorm < 1e-12:
            errors.append(f"IMAGE_ID {image.image_id}의 quaternion이 유효하지 않습니다.")
        if not all(math.isfinite(value) for value in (*image.qvec, *image.tvec)):
            errors.append(f"IMAGE_ID {image.image_id}의 pose에 유한하지 않은 값이 있습니다.")
        for point2d_idx, point2d in enumerate(image.points2d):
            if not all(math.isfinite(value) for value in (point2d.x, point2d.y)):
                errors.append(
                    f"IMAGE_ID {image.image_id}의 POINT2D_IDX {point2d_idx} 좌표가 유효하지 않습니다."
                )
            if point2d.point3d_id is not None:
                if point2d.point3d_id not in model.points3d:
                    errors.append(
                        f"IMAGE_ID {image.image_id}의 POINT2D_IDX {point2d_idx}가 존재하지 않는 "
                        f"POINT3D_ID {point2d.point3d_id}를 참조합니다."
                    )
                expected_track_pairs[(image.image_id, point2d_idx)] = point2d.point3d_id

    actual_track_pairs: dict[tuple[int, int], int] = {}
    mismatched_track_pairs: set[tuple[int, int]] = set()
    for point_key, point in model.points3d.items():
        if point_key != point.point3d_id:
            errors.append(
                f"Point3D dict key {point_key}와 레코드 POINT3D_ID {point.point3d_id}가 다릅니다."
            )
        if not point.track:
            errors.append(f"POINT3D_ID {point.point3d_id}의 track이 비어 있습니다.")
        if not all(math.isfinite(value) for value in (*point.xyz, point.error)):
            errors.append(f"POINT3D_ID {point.point3d_id}의 수치에 유한하지 않은 값이 있습니다.")
        if any(channel < 0 or channel > 255 for channel in point.rgb):
            errors.append(f"POINT3D_ID {point.point3d_id}의 RGB 범위가 잘못되었습니다.")
        for track in point.track:
            pair = (track.image_id, track.point2d_idx)
            if pair in actual_track_pairs:
                errors.append(f"중복 Point3D track pair입니다: {pair}")
                continue
            actual_track_pairs[pair] = point.point3d_id
            if expected_track_pairs.get(pair) != point.point3d_id:
                mismatched_track_pairs.add(pair)
            image = model.images.get(track.image_id)
            if image is None:
                errors.append(
                    f"POINT3D_ID {point.point3d_id}가 존재하지 않는 IMAGE_ID "
                    f"{track.image_id}를 참조합니다."
                )
            elif track.point2d_idx < 0 or track.point2d_idx >= len(image.points2d):
                errors.append(
                    f"POINT3D_ID {point.point3d_id}의 POINT2D_IDX "
                    f"{track.point2d_idx}가 IMAGE_ID {track.image_id} 범위를 벗어납니다."
                )

    for pair, expected in expected_track_pairs.items():
        if actual_track_pairs.get(pair) != expected:
            mismatched_track_pairs.add(pair)

    # 정상 모델은 모든 Track pair를 정렬하지 않습니다. 오류가 있을 때만
    # pair 순서를 결정적으로 정렬해 기존 오류 메시지 순서를 유지합니다.
    for pair in sorted(mismatched_track_pairs):
        expected = expected_track_pairs.get(pair)
        actual = actual_track_pairs.get(pair)
        errors.append(
            f"Point2D와 track이 일치하지 않습니다: IMAGE_ID {pair[0]}, "
            f"POINT2D_IDX {pair[1]}, Point2D={expected}, track={actual}"
        )

    for rig_key, rig in model.rigs.items():
        if rig_key != rig.rig_id:
            errors.append(f"Rig dict key {rig_key}와 레코드 RIG_ID {rig.rig_id}가 다릅니다.")
        if rig.rig_id <= 0 or rig.ref_sensor_id <= 0:
            errors.append(f"RIG_ID {rig.rig_id}의 ID 또는 기준 sensor ID가 유효하지 않습니다.")
        non_ref_keys = {(sensor.sensor_type, sensor.sensor_id) for sensor in rig.sensors}
        if len(non_ref_keys) != len(rig.sensors):
            errors.append(f"RIG_ID {rig.rig_id}에 중복 sensor가 있습니다.")
        ref_key = (rig.ref_sensor_type, rig.ref_sensor_id)
        if ref_key in non_ref_keys:
            errors.append(f"RIG_ID {rig.rig_id}의 기준 sensor가 non-reference 목록에 중복되었습니다.")
        if rig.ref_sensor_type == 0 and rig.ref_sensor_id not in model.cameras:
            errors.append(
                f"RIG_ID {rig.rig_id}의 기준 CAMERA sensor {rig.ref_sensor_id}가 "
                "Camera 레코드에 없습니다."
            )
        for sensor in rig.sensors:
            if sensor.sensor_id <= 0:
                errors.append(f"RIG_ID {rig.rig_id}에 유효하지 않은 sensor ID가 있습니다.")
            if sensor.sensor_type == 0 and sensor.sensor_id not in model.cameras:
                errors.append(
                    f"RIG_ID {rig.rig_id}의 CAMERA sensor {sensor.sensor_id}가 "
                    "Camera 레코드에 없습니다."
                )
            if sensor.has_pose:
                values = (*sensor.qvec, *sensor.tvec) if sensor.qvec and sensor.tvec else ()
                if len(values) != 7 or not all(math.isfinite(value) for value in values):
                    errors.append(f"RIG_ID {rig.rig_id}의 sensor pose가 유효하지 않습니다.")
                elif math.sqrt(sum(value * value for value in sensor.qvec)) < 1e-12:
                    errors.append(f"RIG_ID {rig.rig_id}의 sensor quaternion이 유효하지 않습니다.")

    image_to_frame: dict[int, int] = {}
    for frame_key, frame in model.frames.items():
        if frame_key != frame.frame_id:
            errors.append(
                f"Frame dict key {frame_key}와 레코드 FRAME_ID {frame.frame_id}가 다릅니다."
            )
        if frame.frame_id <= 0 or frame.rig_id <= 0:
            errors.append(f"FRAME_ID {frame.frame_id}의 ID 또는 RIG_ID가 유효하지 않습니다.")
        if not all(math.isfinite(value) for value in (*frame.qvec, *frame.tvec)):
            errors.append(f"FRAME_ID {frame.frame_id}의 pose에 유한하지 않은 값이 있습니다.")
        elif math.sqrt(sum(value * value for value in frame.qvec)) < 1e-12:
            errors.append(f"FRAME_ID {frame.frame_id}의 quaternion이 유효하지 않습니다.")
        rig = model.rigs.get(frame.rig_id)
        if rig is None:
            errors.append(f"FRAME_ID {frame.frame_id}가 존재하지 않는 RIG_ID {frame.rig_id}를 참조합니다.")
            continue
        sensor_keys = {(rig.ref_sensor_type, rig.ref_sensor_id)}
        sensor_keys.update((sensor.sensor_type, sensor.sensor_id) for sensor in rig.sensors)
        frame_data_keys: set[tuple[int, int, int]] = set()
        for data_id in frame.data_ids:
            data_key = (data_id.sensor_type, data_id.sensor_id, data_id.data_id)
            if data_key in frame_data_keys:
                errors.append(f"FRAME_ID {frame.frame_id}에 중복 DATA_ID가 있습니다: {data_key}")
            frame_data_keys.add(data_key)
            if (data_id.sensor_type, data_id.sensor_id) not in sensor_keys:
                errors.append(
                    f"FRAME_ID {frame.frame_id}의 DATA_ID가 RIG_ID {frame.rig_id}에 없는 sensor를 참조합니다."
                )
            if data_id.sensor_type == 0:
                image = model.images.get(data_id.data_id)
                if image is None:
                    errors.append(
                        f"FRAME_ID {frame.frame_id}가 존재하지 않는 IMAGE_ID "
                        f"{data_id.data_id}를 참조합니다."
                    )
                    continue
                if image.camera_id != data_id.sensor_id:
                    errors.append(
                        f"FRAME_ID {frame.frame_id}의 IMAGE_ID {image.image_id}가 "
                        f"CAMERA sensor {data_id.sensor_id}와 다른 CAMERA_ID "
                        f"{image.camera_id}를 사용합니다."
                    )
                previous_frame = image_to_frame.get(image.image_id)
                if previous_frame is not None:
                    errors.append(
                        f"IMAGE_ID {image.image_id}가 FRAME_ID {previous_frame}와 "
                        f"{frame.frame_id}에 중복 연결되었습니다."
                    )
                else:
                    image_to_frame[image.image_id] = frame.frame_id

    if model.rigs or model.frames:
        for image_id in model.images:
            if image_id not in image_to_frame:
                errors.append(f"IMAGE_ID {image_id}가 어떤 Frame의 CAMERA DATA_ID에도 없습니다.")

    if errors:
        preview = "\n".join(f"- {message}" for message in errors[:25])
        extra = len(errors) - 25
        if extra > 0:
            preview += f"\n- 그 밖의 오류 {extra}개"
        raise ValueError(f"COLMAP Reconstruction 무결성 검사에 실패했습니다.\n{preview}")
    return tuple(warnings)
