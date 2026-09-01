"""schema 1 편집 이력을 당시 삭제 의미 그대로 재생합니다."""

from __future__ import annotations

from typing import Iterable

from .edit_types import ModelCounts, RemovalPlan, RemovalReport
from .indexes import RelationshipIndexes, build_relationship_indexes
from .model import Point3DRecord, Reconstruction
from .removal import (
    _counts,
    _indexes_after_removal,
    _normalized_ids,
    _plan_container_cleanup,
    _validate_rig_removal,
    _validate_revision,
)


def plan_removal(
    model: Reconstruction,
    indexes: RelationshipIndexes,
    selected_point_ids: Iterable[int],
    source_revision: int,
) -> RemovalPlan:
    """0.2.1 schema 1 작업의 기존 삭제 대상을 계산합니다."""
    _validate_revision(source_revision)
    requested = _normalized_ids(selected_point_ids, "Point3D")
    missing = [point_id for point_id in requested if point_id not in model.points3d]
    if missing:
        raise ValueError(f"현재 모델에 없는 POINT3D_ID가 선택되었습니다: {missing[:10]}")
    observing = {
        image_id
        for point_id in requested
        for image_id in indexes.point_to_images[point_id]
    }
    if not observing:
        raise ValueError("선택 Point3D를 관측한 Image가 없습니다.")
    _validate_rig_removal(model, indexes, observing)

    requested_set = set(requested)
    affected = {
        point_id
        for image_id in observing
        for point_id in indexes.image_to_points[image_id]
    }
    derived = {
        point_id
        for point_id in affected
        if point_id not in requested_set
        and indexes.point_to_images[point_id]
        and indexes.point_to_images[point_id] <= observing
    }
    removed_cameras, removed_frames, removed_rigs = _plan_container_cleanup(
        model,
        indexes,
        observing,
    )
    before = _counts(model)
    removed_points = requested_set | derived
    return RemovalPlan(
        source_revision=source_revision,
        requested_point3d_ids=requested,
        observing_image_ids=tuple(sorted(observing)),
        affected_point3d_ids=tuple(sorted(affected)),
        derived_orphan_point3d_ids=tuple(sorted(derived)),
        removed_camera_ids=tuple(sorted(removed_cameras)),
        removed_frame_ids=tuple(sorted(removed_frames)),
        removed_rig_ids=tuple(sorted(removed_rigs)),
        before_counts=before,
        after_counts=ModelCounts(
            cameras=before.cameras - len(removed_cameras),
            images=before.images - len(observing),
            points3d=before.points3d - len(removed_points),
            rigs=before.rigs - len(removed_rigs),
            frames=before.frames - len(removed_frames),
        ),
    )


def _apply_removal(
    model: Reconstruction,
    removal_plan: RemovalPlan,
    current_revision: int,
    *,
    indexes: RelationshipIndexes | None = None,
) -> tuple[Reconstruction, RelationshipIndexes, RemovalReport]:
    """0.2.1 작업을 모델과 선택적 기존 인덱스에 당시 의미 그대로 적용합니다."""
    if removal_plan.source_revision != current_revision:
        raise ValueError(
            "삭제 계획을 만든 revision과 현재 revision이 다릅니다. "
            "현재 선택으로 삭제를 다시 실행하십시오."
        )
    if _counts(model) != removal_plan.before_counts:
        raise ValueError("삭제 계획의 입력 개수가 현재 Reconstruction과 일치하지 않습니다.")
    observing = set(removal_plan.observing_image_ids)
    removed_points = set(removal_plan.requested_point3d_ids)
    removed_points.update(removal_plan.derived_orphan_point3d_ids)
    affected = set(removal_plan.affected_point3d_ids)
    if not removed_points <= set(model.points3d):
        raise ValueError("삭제 계획에 현재 Reconstruction에 없는 Point3D가 있습니다.")
    if not observing <= set(model.images):
        raise ValueError("삭제 계획에 현재 Reconstruction에 없는 Image가 있습니다.")
    if not affected <= set(model.points3d) or not removed_points <= affected:
        raise ValueError("삭제 계획의 영향받는 Point3D 집합이 현재 모델과 일치하지 않습니다.")

    points3d = dict(model.points3d)
    for point_id in removed_points:
        del points3d[point_id]
    for point_id in affected - removed_points:
        point = model.points3d[point_id]
        track = tuple(item for item in point.track if item.image_id not in observing)
        if not track:
            raise ValueError(f"RemovalPlan에 누락된 고립 POINT3D_ID가 있습니다: {point_id}")
        if track != point.track:
            points3d[point_id] = Point3DRecord(
                point3d_id=point.point3d_id,
                xyz=point.xyz,
                rgb=point.rgb,
                error=point.error,
                track=track,
            )

    removed_frame_ids = set(removal_plan.removed_frame_ids)
    removed_rig_ids = set(removal_plan.removed_rig_ids)
    removed_camera_ids = set(removal_plan.removed_camera_ids)
    result = Reconstruction(
        cameras={
            camera_id: camera
            for camera_id, camera in model.cameras.items()
            if camera_id not in removed_camera_ids
        },
        images={
            image_id: image
            for image_id, image in model.images.items()
            if image_id not in observing
        },
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
    result_indexes = (
        build_relationship_indexes(result)
        if indexes is None
        else _indexes_after_removal(
            indexes,
            removed_point_ids=removed_points,
            removed_image_ids=observing,
            removed_camera_ids=removed_camera_ids,
            affected_point_ids=affected - removed_points,
        )
    )
    report = RemovalReport(
        requested_point3d_ids=removal_plan.requested_point3d_ids,
        observing_image_ids=removal_plan.observing_image_ids,
        derived_orphan_point3d_ids=removal_plan.derived_orphan_point3d_ids,
        removed_camera_ids=removal_plan.removed_camera_ids,
        removed_frame_ids=removal_plan.removed_frame_ids,
        removed_rig_ids=removal_plan.removed_rig_ids,
        before_counts=removal_plan.before_counts,
        after_counts=_counts(result),
    )
    if report.after_counts != removal_plan.after_counts:
        raise ValueError("삭제 후 개수가 RemovalPlan과 일치하지 않습니다.")
    return result, result_indexes, report


def apply_removal(
    model: Reconstruction,
    removal_plan: RemovalPlan,
    current_revision: int,
) -> tuple[Reconstruction, RelationshipIndexes, RemovalReport]:
    """기존 공개 API대로 schema 1 작업을 적용하고 전체 인덱스를 재생성합니다."""

    return _apply_removal(model, removal_plan, current_revision)


def apply_removal_indexed(
    model: Reconstruction,
    removal_plan: RemovalPlan,
    current_revision: int,
    indexes: RelationshipIndexes,
) -> tuple[Reconstruction, RelationshipIndexes, RemovalReport]:
    """검증된 기존 인덱스에서 schema 1 제거 영향 항목만 갱신합니다."""

    return _apply_removal(
        model,
        removal_plan,
        current_revision,
        indexes=indexes,
    )
