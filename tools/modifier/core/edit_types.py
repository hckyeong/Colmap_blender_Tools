"""COLMAP 편집 계획, 보고와 이력 schema 타입을 정의합니다."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TypeAlias

LEGACY_EDIT_SCHEMA_VERSION = 1
EDIT_SCHEMA_VERSION = 2
REMOVE_SELECTED_OPERATION = "remove_selected_points_and_observing_images"
REMOVE_POINTS_OPERATION = "remove_selected_points"
REMOVE_IMAGES_OPERATION = "remove_selected_images_and_observed_points"
TRANSFORM_SCHEMA_VERSION = 3
DATASET_TRANSFORM_OPERATION = "dataset_transform"


@dataclass(frozen=True)
class ModelCounts:
    cameras: int
    images: int
    points3d: int
    rigs: int
    frames: int


# 0.2.1 이력은 당시 의미 그대로 replay해야 하므로 기존 계획과 보고를 유지합니다.
@dataclass(frozen=True)
class RemovalPlan:
    source_revision: int
    requested_point3d_ids: tuple[int, ...]
    observing_image_ids: tuple[int, ...]
    affected_point3d_ids: tuple[int, ...]
    derived_orphan_point3d_ids: tuple[int, ...]
    removed_camera_ids: tuple[int, ...]
    removed_frame_ids: tuple[int, ...]
    removed_rig_ids: tuple[int, ...]
    before_counts: ModelCounts
    after_counts: ModelCounts


@dataclass(frozen=True)
class RemovalReport:
    requested_point3d_ids: tuple[int, ...]
    observing_image_ids: tuple[int, ...]
    derived_orphan_point3d_ids: tuple[int, ...]
    removed_camera_ids: tuple[int, ...]
    removed_frame_ids: tuple[int, ...]
    removed_rig_ids: tuple[int, ...]
    before_counts: ModelCounts
    after_counts: ModelCounts
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class PointRemovalPlan:
    source_revision: int
    requested_point3d_ids: tuple[int, ...]
    affected_image_ids: tuple[int, ...]
    cleared_observation_count: int
    before_counts: ModelCounts
    after_counts: ModelCounts


@dataclass(frozen=True)
class PointRemovalReport:
    requested_point3d_ids: tuple[int, ...]
    affected_image_ids: tuple[int, ...]
    cleared_observation_count: int
    before_counts: ModelCounts
    after_counts: ModelCounts
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class ImageObservationRemovalPlan:
    source_revision: int
    requested_image_ids: tuple[int, ...]
    observed_point3d_ids: tuple[int, ...]
    affected_surviving_image_ids: tuple[int, ...]
    cleared_surviving_observation_count: int
    removed_camera_ids: tuple[int, ...]
    removed_frame_ids: tuple[int, ...]
    removed_rig_ids: tuple[int, ...]
    before_counts: ModelCounts
    after_counts: ModelCounts


@dataclass(frozen=True)
class ImageObservationRemovalReport:
    requested_image_ids: tuple[int, ...]
    observed_point3d_ids: tuple[int, ...]
    affected_surviving_image_ids: tuple[int, ...]
    cleared_surviving_observation_count: int
    removed_camera_ids: tuple[int, ...]
    removed_frame_ids: tuple[int, ...]
    removed_rig_ids: tuple[int, ...]
    before_counts: ModelCounts
    after_counts: ModelCounts
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class LegacyEditOperation:
    schema_version: int
    operation_id: str
    operation_type: str
    dataset_uuid: str
    source_fingerprint: str
    requested_point3d_ids: tuple[int, ...]
    removed_image_ids: tuple[int, ...]
    derived_orphan_point3d_ids: tuple[int, ...]
    removed_camera_ids: tuple[int, ...]
    removed_frame_ids: tuple[int, ...]
    removed_rig_ids: tuple[int, ...]
    previous_revision: int
    resulting_revision: int
    created_at_utc: str


@dataclass(frozen=True)
class EditOperation:
    schema_version: int
    operation_id: str
    operation_type: str
    dataset_uuid: str
    source_fingerprint: str
    requested_point3d_ids: tuple[int, ...]
    requested_image_ids: tuple[int, ...]
    removed_point3d_ids: tuple[int, ...]
    removed_image_ids: tuple[int, ...]
    removed_camera_ids: tuple[int, ...]
    removed_frame_ids: tuple[int, ...]
    removed_rig_ids: tuple[int, ...]
    previous_revision: int
    resulting_revision: int
    created_at_utc: str


@dataclass(frozen=True)
class TransformOperation:
    """점 배열 없이 확정된 데이터셋 변환 전후 행렬만 저장합니다."""

    schema_version: int
    operation_id: str
    operation_type: str
    dataset_uuid: str
    source_fingerprint: str
    before_transform: tuple[tuple[float, ...], ...]
    after_transform: tuple[tuple[float, ...], ...]
    previous_revision: int
    resulting_revision: int
    created_at_utc: str


AnyEditOperation: TypeAlias = LegacyEditOperation | EditOperation | TransformOperation
