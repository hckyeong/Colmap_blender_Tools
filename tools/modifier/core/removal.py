"""현재 schema의 Point와 Image 제거 계획 및 적용을 담당합니다."""

from __future__ import annotations

from typing import Iterable

from .edit_types import (
    ImageObservationRemovalPlan,
    ImageObservationRemovalReport,
    ModelCounts,
    PointRemovalPlan,
    PointRemovalReport,
)
from .indexes import RelationshipIndexes, build_relationship_indexes
from .model import ImageRecord, Point2DRecord, Reconstruction


def _counts(model: Reconstruction) -> ModelCounts:
    return ModelCounts(
        cameras=len(model.cameras),
        images=len(model.images),
        points3d=len(model.points3d),
        rigs=len(model.rigs),
        frames=len(model.frames),
    )


def _normalized_ids(values: Iterable[int], label: str) -> tuple[int, ...]:
    normalized = tuple(sorted(set(int(value) for value in values)))
    if not normalized:
        raise ValueError(f"{label}를 하나 이상 선택해야 합니다.")
    if normalized[0] < 0:
        raise ValueError(f"{label}에는 0 이상의 ID만 허용됩니다.")
    return normalized


def _validate_revision(source_revision: int) -> None:
    if type(source_revision) is not int or source_revision < 0:
        raise ValueError("삭제 계획의 source revision은 0 이상의 정수여야 합니다.")


def _affected_frames(
    model: Reconstruction,
    indexes: RelationshipIndexes,
    image_ids: set[int],
) -> set[int]:
    del model
    return {
        indexes.image_to_frame[image_id]
        for image_id in image_ids
        if image_id in indexes.image_to_frame
    }


def _validate_rig_removal(
    model: Reconstruction,
    indexes: RelationshipIndexes,
    image_ids: set[int],
) -> None:
    """영향받는 실제 다중 센서 Rig 편집을 변경 전에 거부합니다."""
    for frame_id in sorted(_affected_frames(model, indexes, image_ids)):
        frame = model.frames[frame_id]
        rig = model.rigs[frame.rig_id]
        if rig.sensors or len(frame.data_ids) != 1:
            raise ValueError(
                f"FRAME_ID {frame_id}, RIG_ID {rig.rig_id}는 실제 다중 센서 "
                "관계를 포함하므로 1차 버전에서 부분 Image 삭제를 지원하지 않습니다."
            )
        data_id = frame.data_ids[0]
        if data_id.sensor_type != 0 or data_id.data_id not in image_ids:
            raise ValueError(
                f"FRAME_ID {frame_id}의 센서 관계를 안전하게 정리할 수 없습니다."
            )


def _plan_container_cleanup(
    model: Reconstruction,
    indexes: RelationshipIndexes,
    removed_images: set[int],
) -> tuple[set[int], set[int], set[int]]:
    affected_frames = _affected_frames(model, indexes, removed_images)
    removed_frames = {
        frame_id
        for frame_id in affected_frames
        if not any(
            not (data_id.sensor_type == 0 and data_id.data_id in removed_images)
            for data_id in model.frames[frame_id].data_ids
        )
    }
    used_rigs = {
        frame.rig_id
        for frame_id, frame in model.frames.items()
        if frame_id not in removed_frames
    }
    removed_rigs = {
        rig_id
        for rig_id, rig in model.rigs.items()
        if rig_id not in used_rigs and not rig.sensors
    }

    removed_cameras = {
        camera_id
        for camera_id in model.cameras
        if indexes.camera_to_images[camera_id] <= removed_images
    }
    surviving_rig_ids = set(model.rigs) - removed_rigs
    for rig_id, rig in model.rigs.items():
        if rig_id not in surviving_rig_ids:
            continue
        if rig.ref_sensor_type == 0:
            removed_cameras.discard(rig.ref_sensor_id)
        for sensor in rig.sensors:
            if sensor.sensor_type == 0:
                removed_cameras.discard(sensor.sensor_id)
    return removed_cameras, removed_frames, removed_rigs


def validate_image_observation_removal_request(
    model: Reconstruction,
    indexes: RelationshipIndexes,
    selected_image_ids: Iterable[int],
) -> tuple[int, ...]:
    """Image 존재 여부와 1차 Rig 제한만 검사하고 정규화된 ID를 반환합니다."""
    requested = _normalized_ids(selected_image_ids, "Image")
    missing = [image_id for image_id in requested if image_id not in model.images]
    if missing:
        raise ValueError(f"현재 모델에 없는 IMAGE_ID가 선택되었습니다: {missing[:10]}")
    _validate_rig_removal(model, indexes, set(requested))
    return requested


def _image_with_cleared_points(
    image: ImageRecord,
    removed_point_ids: set[int],
) -> tuple[ImageRecord, int]:
    cleared = 0
    points2d: list[Point2DRecord] = []
    for point in image.points2d:
        if point.point3d_id in removed_point_ids:
            points2d.append(Point2DRecord(point.x, point.y, None))
            cleared += 1
        else:
            points2d.append(point)
    if not cleared:
        return image, 0
    return ImageRecord(
        image_id=image.image_id,
        qvec=image.qvec,
        tvec=image.tvec,
        camera_id=image.camera_id,
        name=image.name,
        points2d=tuple(points2d),
    ), cleared


def plan_point_removal(
    model: Reconstruction,
    indexes: RelationshipIndexes,
    selected_point_ids: Iterable[int],
    source_revision: int,
) -> PointRemovalPlan:
    """선택 Point3D만 제거하고 Image 관측 참조를 비우는 계획을 만듭니다."""
    _validate_revision(source_revision)
    requested = _normalized_ids(selected_point_ids, "Point3D")
    missing = [point_id for point_id in requested if point_id not in model.points3d]
    if missing:
        raise ValueError(f"현재 모델에 없는 POINT3D_ID가 선택되었습니다: {missing[:10]}")
    affected_images = {
        image_id
        for point_id in requested
        for image_id in indexes.point_to_images[point_id]
    }
    cleared_count = sum(len(model.points3d[point_id].track) for point_id in requested)
    before = _counts(model)
    return PointRemovalPlan(
        source_revision=source_revision,
        requested_point3d_ids=requested,
        affected_image_ids=tuple(sorted(affected_images)),
        cleared_observation_count=cleared_count,
        before_counts=before,
        after_counts=ModelCounts(
            cameras=before.cameras,
            images=before.images,
            points3d=before.points3d - len(requested),
            rigs=before.rigs,
            frames=before.frames,
        ),
    )


def apply_point_removal(
    model: Reconstruction,
    plan: PointRemovalPlan,
    current_revision: int,
) -> tuple[Reconstruction, RelationshipIndexes, PointRemovalReport]:
    """Point-only 계획을 copy-on-write로 적용합니다."""
    if plan.source_revision != current_revision:
        raise ValueError(
            "삭제 계획을 만든 revision과 현재 revision이 다릅니다. "
            "현재 선택으로 삭제를 다시 실행하십시오."
        )
    if _counts(model) != plan.before_counts:
        raise ValueError("삭제 계획의 입력 개수가 현재 Reconstruction과 일치하지 않습니다.")
    removed_points = set(plan.requested_point3d_ids)
    affected_images = set(plan.affected_image_ids)
    if not removed_points <= set(model.points3d):
        raise ValueError("삭제 계획에 현재 Reconstruction에 없는 Point3D가 있습니다.")
    expected_affected = {
        item.image_id
        for point_id in removed_points
        for item in model.points3d[point_id].track
    }
    if expected_affected != affected_images:
        raise ValueError("삭제 계획의 영향받는 Image 집합이 현재 모델과 일치하지 않습니다.")

    points3d = dict(model.points3d)
    for point_id in removed_points:
        del points3d[point_id]
    images = dict(model.images)
    cleared_count = 0
    for image_id in affected_images:
        updated, cleared = _image_with_cleared_points(
            model.images[image_id],
            removed_points,
        )
        if not cleared:
            raise ValueError(f"IMAGE_ID {image_id}에서 제거 Point 참조를 찾지 못했습니다.")
        images[image_id] = updated
        cleared_count += cleared
    if cleared_count != plan.cleared_observation_count:
        raise ValueError("삭제 계획의 Point2D 참조 개수가 현재 모델과 일치하지 않습니다.")

    result = Reconstruction(
        cameras=dict(model.cameras),
        images=images,
        points3d=points3d,
        rigs=dict(model.rigs),
        frames=dict(model.frames),
    )
    result_indexes = build_relationship_indexes(result)
    report = PointRemovalReport(
        requested_point3d_ids=plan.requested_point3d_ids,
        affected_image_ids=plan.affected_image_ids,
        cleared_observation_count=cleared_count,
        before_counts=plan.before_counts,
        after_counts=_counts(result),
    )
    if report.after_counts != plan.after_counts:
        raise ValueError("삭제 후 개수가 PointRemovalPlan과 일치하지 않습니다.")
    return result, result_indexes, report


def plan_image_observation_removal(
    model: Reconstruction,
    indexes: RelationshipIndexes,
    selected_image_ids: Iterable[int],
    source_revision: int,
) -> ImageObservationRemovalPlan:
    """선택 Image와 해당 Image가 관측한 Point3D를 전역 제거하도록 계획합니다."""
    _validate_revision(source_revision)
    requested = validate_image_observation_removal_request(
        model,
        indexes,
        selected_image_ids,
    )
    removed_images = set(requested)
    observed_points = {
        point_id
        for image_id in requested
        for point_id in indexes.image_to_points[image_id]
    }
    affected_surviving_images = {
        image_id
        for point_id in observed_points
        for image_id in indexes.point_to_images[point_id]
        if image_id not in removed_images
    }
    cleared_count = sum(
        1
        for point_id in observed_points
        for track in model.points3d[point_id].track
        if track.image_id not in removed_images
    )
    removed_cameras, removed_frames, removed_rigs = _plan_container_cleanup(
        model,
        indexes,
        removed_images,
    )
    before = _counts(model)
    return ImageObservationRemovalPlan(
        source_revision=source_revision,
        requested_image_ids=requested,
        observed_point3d_ids=tuple(sorted(observed_points)),
        affected_surviving_image_ids=tuple(sorted(affected_surviving_images)),
        cleared_surviving_observation_count=cleared_count,
        removed_camera_ids=tuple(sorted(removed_cameras)),
        removed_frame_ids=tuple(sorted(removed_frames)),
        removed_rig_ids=tuple(sorted(removed_rigs)),
        before_counts=before,
        after_counts=ModelCounts(
            cameras=before.cameras - len(removed_cameras),
            images=before.images - len(removed_images),
            points3d=before.points3d - len(observed_points),
            rigs=before.rigs - len(removed_rigs),
            frames=before.frames - len(removed_frames),
        ),
    )


def apply_image_observation_removal(
    model: Reconstruction,
    plan: ImageObservationRemovalPlan,
    current_revision: int,
) -> tuple[Reconstruction, RelationshipIndexes, ImageObservationRemovalReport]:
    """Image 기반 전역 Point 제거 계획을 copy-on-write로 적용합니다."""
    if plan.source_revision != current_revision:
        raise ValueError(
            "삭제 계획을 만든 revision과 현재 revision이 다릅니다. "
            "현재 선택으로 삭제를 다시 실행하십시오."
        )
    if _counts(model) != plan.before_counts:
        raise ValueError("삭제 계획의 입력 개수가 현재 Reconstruction과 일치하지 않습니다.")

    removed_images = set(plan.requested_image_ids)
    removed_points = set(plan.observed_point3d_ids)
    affected_images = set(plan.affected_surviving_image_ids)
    if not removed_images <= set(model.images):
        raise ValueError("삭제 계획에 현재 Reconstruction에 없는 Image가 있습니다.")
    if not removed_points <= set(model.points3d):
        raise ValueError("삭제 계획에 현재 Reconstruction에 없는 Point3D가 있습니다.")
    if affected_images & removed_images or not affected_images <= set(model.images):
        raise ValueError("삭제 계획의 영향받는 생존 Image 집합이 올바르지 않습니다.")

    points3d = dict(model.points3d)
    for point_id in removed_points:
        del points3d[point_id]
    images = {
        image_id: image
        for image_id, image in model.images.items()
        if image_id not in removed_images
    }
    cleared_count = 0
    for image_id in affected_images:
        updated, cleared = _image_with_cleared_points(
            model.images[image_id],
            removed_points,
        )
        if not cleared:
            raise ValueError(f"IMAGE_ID {image_id}에서 제거 Point 참조를 찾지 못했습니다.")
        images[image_id] = updated
        cleared_count += cleared
    if cleared_count != plan.cleared_surviving_observation_count:
        raise ValueError("삭제 계획의 생존 Point2D 참조 개수가 현재 모델과 일치하지 않습니다.")

    removed_camera_ids = set(plan.removed_camera_ids)
    removed_frame_ids = set(plan.removed_frame_ids)
    removed_rig_ids = set(plan.removed_rig_ids)
    for ids, records, label in (
        (removed_camera_ids, model.cameras, "Camera"),
        (removed_frame_ids, model.frames, "Frame"),
        (removed_rig_ids, model.rigs, "Rig"),
    ):
        if not ids <= set(records):
            raise ValueError(f"삭제 계획에 현재 Reconstruction에 없는 {label}가 있습니다.")
    result = Reconstruction(
        cameras={
            camera_id: camera
            for camera_id, camera in model.cameras.items()
            if camera_id not in removed_camera_ids
        },
        images=images,
        points3d=points3d,
        rigs={
            rig_id: rig
            for rig_id, rig in model.rigs.items()
            if rig_id not in removed_rig_ids
        },
        frames={
            frame_id: frame
            for frame_id, frame in model.frames.items()
            if frame_id not in removed_frame_ids
        },
    )
    result_indexes = build_relationship_indexes(result)
    report = ImageObservationRemovalReport(
        requested_image_ids=plan.requested_image_ids,
        observed_point3d_ids=plan.observed_point3d_ids,
        affected_surviving_image_ids=plan.affected_surviving_image_ids,
        cleared_surviving_observation_count=cleared_count,
        removed_camera_ids=plan.removed_camera_ids,
        removed_frame_ids=plan.removed_frame_ids,
        removed_rig_ids=plan.removed_rig_ids,
        before_counts=plan.before_counts,
        after_counts=_counts(result),
    )
    if report.after_counts != plan.after_counts:
        raise ValueError("삭제 후 개수가 ImageObservationRemovalPlan과 일치하지 않습니다.")
    return result, result_indexes, report
