"""COLMAP 구조 통합 내보내기의 Sim(3) pose·rig·projection 계약을 Blender에서 검사합니다."""

from __future__ import annotations

from pathlib import Path
import sys

import numpy as np


def _add_source_path() -> None:
    """애드온 소스 디렉터리를 Blender Python 모듈 탐색 경로에 추가합니다."""

    source_path = Path(__file__).resolve().parents[3]
    source_text = str(source_path)
    if source_text not in sys.path:
        sys.path.insert(0, source_text)


def _rotation_z(degrees: float) -> np.ndarray:
    """테스트에서 사용할 오른손 좌표계 Z축 회전 행렬을 만듭니다."""

    radians = np.deg2rad(degrees)
    cosine = float(np.cos(radians))
    sine = float(np.sin(radians))
    return np.asarray(
        (
            (cosine, -sine, 0.0),
            (sine, cosine, 0.0),
            (0.0, 0.0, 1.0),
        ),
        dtype=np.float64,
    )


def _legacy_model() -> object:
    """legacy rigs·frames가 없는 최소 유효 COLMAP 모델을 만듭니다."""

    from tools.align.colmap_io.types import (
        CameraRecord,
        ColmapModelData,
        ImageRecord,
        ImageTable,
        PointTable,
    )

    image = ImageRecord(
        image_id=9,
        camera_id=3,
        name="legacy/image.jpg",
        cam_from_world=np.asarray((1.0, 0.0, 0.0, 0.0, -1.0, 0.0, 0.0)),
    )
    return ColmapModelData(
        model_id="legacy",
        display_name="legacy",
        source_path=Path("legacy"),
        source_format="binary",
        cameras={
            3: CameraRecord(
                camera_id=3,
                model_id=1,
                model_name="PINHOLE",
                width=640,
                height=480,
                params_float64=np.asarray((500.0, 500.0, 320.0, 240.0)),
            )
        },
        images=ImageTable(
            image_records={9: image},
            point2d_offsets=np.asarray((0, 1), dtype=np.uint64),
            point2d_xy=np.asarray(((320.0, 240.0),), dtype=np.float64),
            point2d_point3d_ids=np.asarray((11,), dtype=np.uint64),
        ),
        rigs={},
        frames={},
        points=PointTable(
            point_ids=np.asarray((11,), dtype=np.uint64),
            positions=np.asarray(((2.0, 0.0, 0.0),), dtype=np.float64),
            colors=np.asarray(((10, 20, 30),), dtype=np.uint8),
            errors=np.asarray((0.25,), dtype=np.float64),
            track_offsets=np.asarray((0, 1), dtype=np.uint64),
            track_image_ids=np.asarray((9,), dtype=np.uint64),
            track_point2d_indices=np.asarray((0,), dtype=np.uint32),
            display_indices=np.asarray((0,), dtype=np.uint32),
        ),
        bounds_min=np.asarray((2.0, 0.0, 0.0), dtype=np.float64),
        bounds_max=np.asarray((2.0, 0.0, 0.0), dtype=np.float64),
    )


def test_project_world_transform_and_points() -> None:
    """Alignment × Display Axis 순서와 Point3D scale·rotation·translation을 고정합니다."""

    from tools.align.alignment.types import create_sim3_transform
    from tools.align.merge_export.transforms import (
        compose_project_world_transform,
        transform_point_positions,
    )

    alignment = create_sim3_transform(
        _rotation_z(90.0),
        2.0,
        np.asarray((10.0, 20.0, 30.0)),
    )
    display = np.identity(4, dtype=np.float64)
    display[:3, :3] = _rotation_z(90.0)
    project_transform = compose_project_world_transform(alignment, display)
    assert np.allclose(
        project_transform.matrix_float64,
        alignment.matrix_float64 @ display,
        rtol=0.0,
        atol=1.0e-12,
    )
    transformed = transform_point_positions(
        np.asarray(((1.0, 0.0, 0.0),), dtype=np.float64),
        project_transform,
    )
    assert np.allclose(transformed, np.asarray(((8.0, 20.0, 30.0))), atol=1.0e-12)


def test_output_transform_restores_anchor_colmap_axes() -> None:
    """모든 source를 Anchor의 원래 COLMAP 축으로 되돌리는 공통 출력 변환을 고정합니다."""

    from tools.align.alignment.types import create_sim3_transform, identity_sim3_transform
    from tools.align.merge_export.transforms import compose_anchor_colmap_output_transform

    anchor_display = np.identity(4, dtype=np.float64)
    anchor_display[:3, :3] = _rotation_z(90.0)
    anchor_output = compose_anchor_colmap_output_transform(
        identity_sim3_transform(),
        anchor_display,
        anchor_display,
    )
    assert np.allclose(
        anchor_output.matrix_float64,
        np.identity(4, dtype=np.float64),
        rtol=0.0,
        atol=1.0e-12,
    )

    moving_display = np.identity(4, dtype=np.float64)
    moving_display[:3, :3] = _rotation_z(-90.0)
    moving_alignment = create_sim3_transform(
        _rotation_z(30.0),
        2.0,
        np.asarray((10.0, 20.0, 30.0)),
    )
    moving_output = compose_anchor_colmap_output_transform(
        moving_alignment,
        moving_display,
        anchor_display,
    )
    assert np.allclose(
        moving_output.matrix_float64,
        np.linalg.inv(anchor_display) @ moving_alignment.matrix_float64 @ moving_display,
        rtol=0.0,
        atol=1.0e-12,
    )


def test_cam_pose_and_rig_frame_transform_match() -> None:
    """직접 변환 image pose와 scale된 rig/frame 재합성 pose가 같은지 확인합니다."""

    from tools.align.alignment.types import create_sim3_transform
    from tools.align.colmap_io.types import FrameDataRecord, FrameRecord, RigRecord, RigSensorRecord
    from tools.align.merge_export.transforms import (
        camera_center_from_pose,
        compose_cam_from_world,
        rotation_translation_to_pose,
        transform_cam_from_world,
        transform_frame_record,
        transform_rig_record,
    )

    transform = create_sim3_transform(
        _rotation_z(90.0),
        3.0,
        np.asarray((10.0, -2.0, 5.0)),
    )
    sensor_pose = rotation_translation_to_pose(
        _rotation_z(0.0),
        np.asarray((0.5, 0.0, 0.0)),
    )
    frame_pose = rotation_translation_to_pose(
        _rotation_z(30.0),
        np.asarray((1.0, -3.0, 2.0)),
    )
    direct_source_pose = compose_cam_from_world(sensor_pose, frame_pose)
    transformed_direct_pose = transform_cam_from_world(direct_source_pose, transform)
    expected_center = (
        transform.uniform_scale
        * transform.rotation_float64
        @ camera_center_from_pose(direct_source_pose)
        + transform.translation_float64
    )
    assert np.allclose(
        camera_center_from_pose(transformed_direct_pose),
        expected_center,
        rtol=0.0,
        atol=1.0e-10,
    )

    reference = RigSensorRecord(sensor_type="CAMERA", sensor_id=1)
    secondary = RigSensorRecord(
        sensor_type="CAMERA",
        sensor_id=2,
        sensor_from_rig=sensor_pose,
    )
    rig = RigRecord(
        rig_id=4,
        reference_sensor=reference,
        sensors=(reference, secondary),
    )
    frame = FrameRecord(
        frame_id=6,
        rig_id=4,
        rig_from_world=frame_pose,
        data_ids=(FrameDataRecord(sensor_type="CAMERA", sensor_id=2, data_id=12),),
    )
    transformed_rig = transform_rig_record(rig, transform)
    transformed_frame = transform_frame_record(frame, transform)
    assert np.allclose(
        transformed_rig.sensors[1].sensor_from_rig[4:],
        np.asarray((1.5, 0.0, 0.0)),
        rtol=0.0,
        atol=1.0e-12,
    )
    recomposed_pose = compose_cam_from_world(
        transformed_rig.sensors[1].sensor_from_rig,
        transformed_frame.rig_from_world,
    )
    assert np.allclose(
        recomposed_pose,
        transformed_direct_pose,
        rtol=0.0,
        atol=1.0e-10,
    )


def test_legacy_normalization_and_quaternion_sign() -> None:
    """legacy trivial rig/frame과 180도 quaternion 부호 규칙을 확인합니다."""

    from tools.align.alignment.types import create_sim3_transform
    from tools.align.merge_export.transforms import (
        normalize_legacy_rigs_and_frames,
        rotation_matrix_to_quaternion,
        transform_cam_from_world,
    )

    model = _legacy_model()
    transform = create_sim3_transform(
        _rotation_z(180.0),
        2.0,
        np.asarray((4.0, 5.0, 6.0)),
    )
    rigs, frames = normalize_legacy_rigs_and_frames(model, transform)
    assert tuple(rigs) == (3,)
    assert rigs[3].reference_sensor.sensor_type == "CAMERA"
    assert rigs[3].reference_sensor.sensor_id == 3
    assert tuple(frames) == (9,)
    assert frames[9].rig_id == 3
    assert frames[9].data_ids[0].data_id == 9
    assert np.allclose(
        frames[9].rig_from_world,
        transform_cam_from_world(
            model.images.image_records[9].cam_from_world,
            transform,
        ),
        rtol=0.0,
        atol=1.0e-12,
    )

    quaternion = rotation_matrix_to_quaternion(np.diag((-1.0, -1.0, 1.0)))
    assert quaternion[0] == 0.0
    assert quaternion[3] > 0.0
    assert np.isclose(float(np.linalg.norm(quaternion)), 1.0, rtol=0.0, atol=1.0e-12)


def _camera_for_model(model_id: int, params: tuple[float, ...]) -> object:
    """18개 공식 모델 투영 검사에 쓸 CameraRecord를 만듭니다."""

    from tools.align.colmap_io.camera_models import camera_model_from_id
    from tools.align.colmap_io.types import CameraRecord

    model = camera_model_from_id(model_id)
    return CameraRecord(
        camera_id=model_id + 1,
        model_id=model_id,
        model_name=model.name,
        width=640,
        height=480,
        params_float64=np.asarray(params, dtype=np.float64),
    )


def test_projection_covers_all_camera_models() -> None:
    """18개 모델이 pinhole fallback 없이 유한한 투영과 재투영 오차를 계산하는지 확인합니다."""

    from tools.align.colmap_io.projection import (
        project_camera_point,
        project_world_point,
        reprojection_error_pixels,
    )

    parameter_sets = (
        (500.0, 320.0, 240.0),
        (500.0, 510.0, 320.0, 240.0),
        (500.0, 320.0, 240.0, 0.0),
        (500.0, 320.0, 240.0, 0.0, 0.0),
        (500.0, 510.0, 320.0, 240.0, 0.0, 0.0, 0.0, 0.0),
        (500.0, 510.0, 320.0, 240.0, 0.0, 0.0, 0.0, 0.0),
        (500.0, 510.0, 320.0, 240.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0),
        (500.0, 510.0, 320.0, 240.0, 0.5),
        (500.0, 320.0, 240.0, 0.0),
        (500.0, 320.0, 240.0, 0.0, 0.0),
        (500.0, 510.0, 320.0, 240.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0),
        (
            500.0,
            510.0,
            320.0,
            240.0,
            0.0,
            0.0,
            0.0,
            0.0,
            0.0,
            0.0,
            0.0,
            0.0,
            0.0,
            0.0,
            0.0,
            0.0,
        ),
        (500.0, 320.0, 240.0, 0.0),
        (500.0, 510.0, 320.0, 240.0, 0.0),
        (500.0, 320.0, 240.0),
        (500.0, 510.0, 320.0, 240.0),
        (500.0, 510.0, 320.0, 240.0, 0.0, 1.0),
        (640.0, 480.0),
    )
    point_camera = np.asarray((0.1, -0.2, 1.0), dtype=np.float64)
    for model_id, params in enumerate(parameter_sets):
        camera = _camera_for_model(model_id, params)
        projected = project_camera_point(camera, point_camera)
        assert projected.shape == (2,)
        assert np.isfinite(projected).all()

    pinhole = _camera_for_model(1, parameter_sets[1])
    pose = np.asarray((1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0), dtype=np.float64)
    point_world = np.asarray((0.1, 0.2, 1.0), dtype=np.float64)
    observed = project_world_point(pinhole, pose, point_world)
    assert np.isclose(
        reprojection_error_pixels(pinhole, pose, point_world, observed),
        0.0,
        rtol=0.0,
        atol=1.0e-12,
    )


def run_all() -> None:
    """merge export transform·projection 검사를 Blender 한 프로세스에서 실행합니다."""

    _add_source_path()
    test_project_world_transform_and_points()
    test_output_transform_restores_anchor_colmap_axes()
    test_cam_pose_and_rig_frame_transform_match()
    test_legacy_normalization_and_quaternion_sign()
    test_projection_covers_all_camera_models()
    print("test_merge_export_transforms: 통과")


if __name__ == "__main__":
    run_all()
