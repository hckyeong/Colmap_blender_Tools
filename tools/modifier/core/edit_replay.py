"""COLMAP 편집 이력을 schema별로 검증하며 결정론적으로 재생합니다."""

from __future__ import annotations

import hashlib
from typing import Iterable

from .edit_history import serialize_history
from .edit_types import (
    AnyEditOperation,
    EditOperation,
    REMOVE_IMAGES_OPERATION,
    REMOVE_POINTS_OPERATION,
)
from .legacy_removal import apply_removal, plan_removal
from .removal import (
    apply_image_observation_removal,
    apply_point_removal,
    plan_image_observation_removal,
    plan_point_removal,
)
from .indexes import (
    RelationshipIndexes,
    build_relationship_indexes,
    build_relationship_indexes_validated,
)
from .model import Reconstruction, ValidatedReconstructionToken


def history_hash(operations: Iterable[AnyEditOperation]) -> str:
    serialized = serialize_history(tuple(operations))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def replay_history(
    baseline: Reconstruction,
    operations: Iterable[AnyEditOperation],
    dataset_uuid: str,
    source_fingerprint: str,
    validation_token: ValidatedReconstructionToken | None = None,
) -> tuple[Reconstruction, RelationshipIndexes]:
    current = baseline
    indexes = (
        build_relationship_indexes(current)
        if validation_token is None
        else build_relationship_indexes_validated(
            current,
            validation_token,
            dataset_uuid=dataset_uuid,
            source_fingerprint=source_fingerprint,
            validation_revision=0,
        )
    )
    expected_revision = 0
    for operation in operations:
        if operation.dataset_uuid != dataset_uuid:
            raise ValueError("편집 이력의 Dataset UUID가 현재 원본과 다릅니다.")
        if operation.source_fingerprint != source_fingerprint:
            raise ValueError("편집 이력의 source fingerprint가 현재 원본과 다릅니다.")
        if operation.previous_revision != expected_revision:
            raise ValueError("편집 이력 revision 순서가 연속적이지 않습니다.")

        if not isinstance(operation, EditOperation):
            plan = plan_removal(
                current,
                indexes,
                operation.requested_point3d_ids,
                source_revision=expected_revision,
            )
            current, indexes, report = apply_removal(
                current,
                plan,
                current_revision=expected_revision,
            )
            expected = (
                operation.removed_image_ids,
                operation.derived_orphan_point3d_ids,
                operation.removed_camera_ids,
                operation.removed_frame_ids,
                operation.removed_rig_ids,
            )
            actual = (
                report.observing_image_ids,
                report.derived_orphan_point3d_ids,
                report.removed_camera_ids,
                report.removed_frame_ids,
                report.removed_rig_ids,
            )
        elif operation.operation_type == REMOVE_POINTS_OPERATION:
            plan = plan_point_removal(
                current,
                indexes,
                operation.requested_point3d_ids,
                source_revision=expected_revision,
            )
            current, indexes, report = apply_point_removal(
                current,
                plan,
                current_revision=expected_revision,
            )
            expected = (
                operation.removed_point3d_ids,
                operation.removed_image_ids,
                operation.removed_camera_ids,
                operation.removed_frame_ids,
                operation.removed_rig_ids,
            )
            actual = (
                report.requested_point3d_ids,
                (),
                (),
                (),
                (),
            )
        elif operation.operation_type == REMOVE_IMAGES_OPERATION:
            plan = plan_image_observation_removal(
                current,
                indexes,
                operation.requested_image_ids,
                source_revision=expected_revision,
            )
            current, indexes, report = apply_image_observation_removal(
                current,
                plan,
                current_revision=expected_revision,
            )
            expected = (
                operation.removed_point3d_ids,
                operation.removed_image_ids,
                operation.removed_camera_ids,
                operation.removed_frame_ids,
                operation.removed_rig_ids,
            )
            actual = (
                report.observed_point3d_ids,
                report.requested_image_ids,
                report.removed_camera_ids,
                report.removed_frame_ids,
                report.removed_rig_ids,
            )
        else:
            raise ValueError(f"지원하지 않는 편집 작업입니다: {operation.operation_type}")

        if actual != expected:
            raise ValueError(
                f"편집 작업 {operation.operation_id}의 저장 결과와 replay 결과가 다릅니다."
            )
        expected_revision = operation.resulting_revision
    return current, indexes
