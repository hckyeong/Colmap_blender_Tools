"""Modifier 편집 전후의 영향 범위 Delta를 캡처하고 원자 적용합니다."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from typing import Generic, Mapping, TypeVar

from .edit_types import (
    EDIT_SCHEMA_VERSION,
    REMOVE_IMAGES_OPERATION,
    REMOVE_POINTS_OPERATION,
    EditOperation,
    ImageObservationRemovalPlan,
    ModelCounts,
    PointRemovalPlan,
)
from .indexes import RelationshipIndexes
from .model import (
    CameraRecord,
    FrameRecord,
    ImageRecord,
    Point3DRecord,
    Reconstruction,
    RigRecord,
)


ValueT = TypeVar("ValueT")
RemovalPlan = PointRemovalPlan | ImageObservationRemovalPlan


@dataclass(frozen=True)
class MappingEntryDelta(Generic[ValueT]):
    """단일 정수 key의 삽입·제거·교체 전후 값을 보관합니다."""

    key: int
    before: ValueT | None
    after: ValueT | None

    @property
    def is_insertion(self) -> bool:
        """없던 entry가 추가되는 Delta인지 반환합니다."""

        return self.before is None and self.after is not None

    @property
    def is_removal(self) -> bool:
        """기존 entry가 제거되는 Delta인지 반환합니다."""

        return self.before is not None and self.after is None

    @property
    def is_replacement(self) -> bool:
        """기존 entry가 다른 값으로 교체되는 Delta인지 반환합니다."""

        return (
            self.before is not None
            and self.after is not None
            and self.before != self.after
        )


@dataclass(frozen=True)
class EditStateToken:
    """Reconstruction과 관계 인덱스의 동일 Source·revision을 나타냅니다."""

    dataset_uuid: str
    source_fingerprint: str
    edit_revision: int
    index_revision: int

    def __post_init__(self) -> None:
        """Source 식별자와 두 revision의 기본 형식을 검증합니다."""

        _validate_source_token(self.dataset_uuid, self.source_fingerprint)
        _validate_revision(self.edit_revision, "편집 revision")
        _validate_revision(self.index_revision, "인덱스 revision")


@dataclass(frozen=True)
class EditDelta:
    """한 EditOperation의 forward·inverse 적용에 필요한 영향 entry만 보관합니다."""

    operation_type: str
    dataset_uuid: str
    source_fingerprint: str
    previous_revision: int
    resulting_revision: int
    cameras: tuple[MappingEntryDelta[CameraRecord], ...] = ()
    images: tuple[MappingEntryDelta[ImageRecord], ...] = ()
    points3d: tuple[MappingEntryDelta[Point3DRecord], ...] = ()
    rigs: tuple[MappingEntryDelta[RigRecord], ...] = ()
    frames: tuple[MappingEntryDelta[FrameRecord], ...] = ()
    point_to_images: tuple[MappingEntryDelta[frozenset[int]], ...] = ()
    image_to_points: tuple[MappingEntryDelta[frozenset[int]], ...] = ()
    camera_to_images: tuple[MappingEntryDelta[frozenset[int]], ...] = ()
    image_to_frame: tuple[MappingEntryDelta[int], ...] = ()

    def __post_init__(self) -> None:
        """Delta token, revision과 각 mapping의 중복 없는 key를 검증합니다."""

        if self.operation_type not in {
            REMOVE_POINTS_OPERATION,
            REMOVE_IMAGES_OPERATION,
        }:
            raise ValueError(f"지원하지 않는 Delta 작업입니다: {self.operation_type}")
        _validate_source_token(self.dataset_uuid, self.source_fingerprint)
        _validate_revision(self.previous_revision, "Delta 이전 revision")
        _validate_revision(self.resulting_revision, "Delta 결과 revision")
        if self.resulting_revision != self.previous_revision + 1:
            raise ValueError("Delta revision이 연속적이지 않습니다.")
        for label, entries in _delta_entry_groups(self):
            _validate_entry_group(entries, label)


def _validate_source_token(dataset_uuid: str, source_fingerprint: str) -> None:
    """Dataset UUID와 source SHA-256 token 형식을 검증합니다."""

    if not isinstance(dataset_uuid, str):
        raise ValueError("Dataset UUID는 문자열이어야 합니다.")
    try:
        normalized_uuid = str(uuid.UUID(dataset_uuid))
    except (ValueError, AttributeError) as exc:
        raise ValueError("Dataset UUID가 올바르지 않습니다.") from exc
    if normalized_uuid != dataset_uuid:
        raise ValueError("Dataset UUID는 표준 소문자 하이픈 형식이어야 합니다.")
    if (
        not isinstance(source_fingerprint, str)
        or re.fullmatch(r"[0-9a-f]{64}", source_fingerprint) is None
    ):
        raise ValueError("source fingerprint는 소문자 SHA-256 문자열이어야 합니다.")


def _validate_revision(value: int, label: str) -> None:
    """revision이 bool이 아닌 0 이상의 정수인지 검증합니다."""

    if type(value) is not int or value < 0:
        raise ValueError(f"{label}은 0 이상의 정수여야 합니다.")


def _delta_entry_groups(
    delta: EditDelta,
) -> tuple[tuple[str, tuple[MappingEntryDelta[object], ...]], ...]:
    """Delta의 Reconstruction·index entry 묶음을 고정 순서로 반환합니다."""

    return (
        ("Camera", delta.cameras),
        ("Image", delta.images),
        ("Point3D", delta.points3d),
        ("Rig", delta.rigs),
        ("Frame", delta.frames),
        ("point_to_images", delta.point_to_images),
        ("image_to_points", delta.image_to_points),
        ("camera_to_images", delta.camera_to_images),
        ("image_to_frame", delta.image_to_frame),
    )


def _validate_entry_group(
    entries: tuple[MappingEntryDelta[object], ...],
    label: str,
) -> None:
    """한 mapping의 entry key가 정규화되어 있고 중복되지 않는지 검사합니다."""

    if not isinstance(entries, tuple):
        raise ValueError(f"{label} Delta entry는 tuple이어야 합니다.")
    keys: list[int] = []
    for entry in entries:
        if not isinstance(entry, MappingEntryDelta):
            raise ValueError(f"{label} Delta entry 형식이 올바르지 않습니다.")
        if type(entry.key) is not int or entry.key < 0:
            raise ValueError(f"{label} Delta key는 0 이상의 정수여야 합니다.")
        keys.append(entry.key)
    if keys != sorted(set(keys)):
        raise ValueError(f"{label} Delta key는 중복 없는 오름차순이어야 합니다.")


def _counts(model: Reconstruction) -> ModelCounts:
    """전체 순회 없이 Reconstruction mapping의 개수만 반환합니다."""

    return ModelCounts(
        cameras=len(model.cameras),
        images=len(model.images),
        points3d=len(model.points3d),
        rigs=len(model.rigs),
        frames=len(model.frames),
    )


def _normalized_plan_ids(values: tuple[int, ...], label: str) -> tuple[int, ...]:
    """계획의 영향 ID가 중복 없는 오름차순인지 검증합니다."""

    if any(type(value) is not int or value < 0 for value in values):
        raise ValueError(f"{label}에는 0 이상의 정수 ID만 허용됩니다.")
    if values != tuple(sorted(set(values))):
        raise ValueError(f"{label}는 중복 없는 오름차순이어야 합니다.")
    return values


def _entry_value(mapping: Mapping[int, ValueT], key: int) -> ValueT | None:
    """entry가 없으면 None, 있으면 해당 값을 전체 순회 없이 반환합니다."""

    return mapping[key] if key in mapping else None


def _capture_entries(
    before: Mapping[int, ValueT],
    after: Mapping[int, ValueT],
    keys: tuple[int, ...],
) -> tuple[MappingEntryDelta[ValueT], ...]:
    """명시된 영향 key만 조회해 전후 entry Delta를 캡처합니다."""

    normalized_keys = tuple(sorted(set(keys)))
    return tuple(
        MappingEntryDelta(
            key=key,
            before=_entry_value(before, key),
            after=_entry_value(after, key),
        )
        for key in normalized_keys
    )


def _require_present(
    mapping: Mapping[int, object],
    keys: tuple[int, ...],
    label: str,
) -> None:
    """영향 key가 캡처 입력 mapping에 모두 존재하는지 확인합니다."""

    missing = [key for key in keys if key not in mapping]
    if missing:
        raise ValueError(f"Delta 입력에 없는 {label} ID가 있습니다: {missing[:10]}")


def _require_absent(
    mapping: Mapping[int, object],
    keys: tuple[int, ...],
    label: str,
) -> None:
    """제거 대상 key가 결과 mapping에서 모두 사라졌는지 확인합니다."""

    remaining = [key for key in keys if key in mapping]
    if remaining:
        raise ValueError(f"Delta 결과에 제거되지 않은 {label} ID가 있습니다: {remaining[:10]}")


def _validate_operation_and_plan(
    operation: EditOperation,
    plan: RemovalPlan,
) -> None:
    """신규 schema 작업과 제거 계획의 ID·revision 결과가 일치하는지 검증합니다."""

    if not isinstance(operation, EditOperation) or operation.schema_version != EDIT_SCHEMA_VERSION:
        raise ValueError("Legacy schema 1 작업은 신규 warm Delta command로 만들 수 없습니다.")
    if operation.previous_revision != plan.source_revision:
        raise ValueError("EditOperation과 제거 계획의 이전 revision이 다릅니다.")
    if operation.resulting_revision != operation.previous_revision + 1:
        raise ValueError("EditOperation revision이 연속적이지 않습니다.")
    if isinstance(plan, PointRemovalPlan):
        expected = (
            REMOVE_POINTS_OPERATION,
            plan.requested_point3d_ids,
            (),
            plan.requested_point3d_ids,
            (),
            (),
            (),
            (),
        )
    elif isinstance(plan, ImageObservationRemovalPlan):
        expected = (
            REMOVE_IMAGES_OPERATION,
            (),
            plan.requested_image_ids,
            plan.observed_point3d_ids,
            plan.requested_image_ids,
            plan.removed_camera_ids,
            plan.removed_frame_ids,
            plan.removed_rig_ids,
        )
    else:
        raise TypeError("지원하지 않는 제거 계획 형식입니다.")
    actual = (
        operation.operation_type,
        operation.requested_point3d_ids,
        operation.requested_image_ids,
        operation.removed_point3d_ids,
        operation.removed_image_ids,
        operation.removed_camera_ids,
        operation.removed_frame_ids,
        operation.removed_rig_ids,
    )
    if actual != expected:
        raise ValueError("EditOperation의 저장 결과와 제거 계획의 영향 ID가 다릅니다.")


def _validate_capture_boundaries(
    before: Reconstruction,
    after: Reconstruction,
    plan: RemovalPlan,
) -> None:
    """캡처 전후 Reconstruction 개수와 제거 대상의 존재 경계를 검증합니다."""

    if _counts(before) != plan.before_counts:
        raise ValueError("Delta 캡처 전 Reconstruction 개수가 제거 계획과 다릅니다.")
    if _counts(after) != plan.after_counts:
        raise ValueError("Delta 캡처 후 Reconstruction 개수가 제거 계획과 다릅니다.")
    if isinstance(plan, PointRemovalPlan):
        point_ids = _normalized_plan_ids(
            plan.requested_point3d_ids,
            "Point 제거 계획 ID",
        )
        image_ids = _normalized_plan_ids(
            plan.affected_image_ids,
            "Point 제거 영향 Image ID",
        )
        _require_present(before.points3d, point_ids, "POINT3D")
        _require_absent(after.points3d, point_ids, "POINT3D")
        _require_present(before.images, image_ids, "IMAGE")
        _require_present(after.images, image_ids, "IMAGE")
        return
    image_ids = _normalized_plan_ids(plan.requested_image_ids, "Image 제거 계획 ID")
    point_ids = _normalized_plan_ids(
        plan.observed_point3d_ids,
        "Image 제거 영향 Point3D ID",
    )
    surviving_image_ids = _normalized_plan_ids(
        plan.affected_surviving_image_ids,
        "Image 제거 영향 생존 Image ID",
    )
    camera_ids = _normalized_plan_ids(plan.removed_camera_ids, "제거 Camera ID")
    frame_ids = _normalized_plan_ids(plan.removed_frame_ids, "제거 Frame ID")
    rig_ids = _normalized_plan_ids(plan.removed_rig_ids, "제거 Rig ID")
    for before_mapping, after_mapping, keys, label in (
        (before.images, after.images, image_ids, "IMAGE"),
        (before.points3d, after.points3d, point_ids, "POINT3D"),
        (before.cameras, after.cameras, camera_ids, "CAMERA"),
        (before.frames, after.frames, frame_ids, "FRAME"),
        (before.rigs, after.rigs, rig_ids, "RIG"),
    ):
        _require_present(before_mapping, keys, label)
        _require_absent(after_mapping, keys, label)
    _require_present(before.images, surviving_image_ids, "생존 IMAGE")
    _require_present(after.images, surviving_image_ids, "생존 IMAGE")


def capture_edit_delta(
    before_reconstruction: Reconstruction,
    before_indexes: RelationshipIndexes,
    after_reconstruction: Reconstruction,
    after_indexes: RelationshipIndexes,
    plan: RemovalPlan,
    operation: EditOperation,
) -> EditDelta:
    """제거 계획의 영향 ID만 사용해 양방향 warm Delta를 캡처합니다."""

    _validate_operation_and_plan(operation, plan)
    _validate_capture_boundaries(before_reconstruction, after_reconstruction, plan)
    if isinstance(plan, PointRemovalPlan):
        point_ids = plan.requested_point3d_ids
        image_ids = plan.affected_image_ids
        return EditDelta(
            operation_type=operation.operation_type,
            dataset_uuid=operation.dataset_uuid,
            source_fingerprint=operation.source_fingerprint,
            previous_revision=operation.previous_revision,
            resulting_revision=operation.resulting_revision,
            images=_capture_entries(
                before_reconstruction.images,
                after_reconstruction.images,
                image_ids,
            ),
            points3d=_capture_entries(
                before_reconstruction.points3d,
                after_reconstruction.points3d,
                point_ids,
            ),
            point_to_images=_capture_entries(
                before_indexes.point_to_images,
                after_indexes.point_to_images,
                point_ids,
            ),
            image_to_points=_capture_entries(
                before_indexes.image_to_points,
                after_indexes.image_to_points,
                image_ids,
            ),
        )

    removed_image_ids = plan.requested_image_ids
    surviving_image_ids = plan.affected_surviving_image_ids
    image_ids = tuple(sorted(set(removed_image_ids) | set(surviving_image_ids)))
    camera_index_ids = tuple(
        sorted(
            set(plan.removed_camera_ids)
            | {
                before_reconstruction.images[image_id].camera_id
                for image_id in removed_image_ids
            }
        )
    )
    return EditDelta(
        operation_type=operation.operation_type,
        dataset_uuid=operation.dataset_uuid,
        source_fingerprint=operation.source_fingerprint,
        previous_revision=operation.previous_revision,
        resulting_revision=operation.resulting_revision,
        cameras=_capture_entries(
            before_reconstruction.cameras,
            after_reconstruction.cameras,
            plan.removed_camera_ids,
        ),
        images=_capture_entries(
            before_reconstruction.images,
            after_reconstruction.images,
            image_ids,
        ),
        points3d=_capture_entries(
            before_reconstruction.points3d,
            after_reconstruction.points3d,
            plan.observed_point3d_ids,
        ),
        rigs=_capture_entries(
            before_reconstruction.rigs,
            after_reconstruction.rigs,
            plan.removed_rig_ids,
        ),
        frames=_capture_entries(
            before_reconstruction.frames,
            after_reconstruction.frames,
            plan.removed_frame_ids,
        ),
        point_to_images=_capture_entries(
            before_indexes.point_to_images,
            after_indexes.point_to_images,
            plan.observed_point3d_ids,
        ),
        image_to_points=_capture_entries(
            before_indexes.image_to_points,
            after_indexes.image_to_points,
            image_ids,
        ),
        camera_to_images=_capture_entries(
            before_indexes.camera_to_images,
            after_indexes.camera_to_images,
            camera_index_ids,
        ),
        image_to_frame=_capture_entries(
            before_indexes.image_to_frame,
            after_indexes.image_to_frame,
            removed_image_ids,
        ),
    )


def capture_point_removal_delta(
    before_reconstruction: Reconstruction,
    before_indexes: RelationshipIndexes,
    after_reconstruction: Reconstruction,
    after_indexes: RelationshipIndexes,
    plan: PointRemovalPlan,
    operation: EditOperation,
) -> EditDelta:
    """PointRemovalPlan 전용으로 영향 범위 Delta를 캡처합니다."""

    return capture_edit_delta(
        before_reconstruction,
        before_indexes,
        after_reconstruction,
        after_indexes,
        plan,
        operation,
    )


def capture_image_observation_removal_delta(
    before_reconstruction: Reconstruction,
    before_indexes: RelationshipIndexes,
    after_reconstruction: Reconstruction,
    after_indexes: RelationshipIndexes,
    plan: ImageObservationRemovalPlan,
    operation: EditOperation,
) -> EditDelta:
    """ImageObservationRemovalPlan 전용으로 영향 범위 Delta를 캡처합니다."""

    return capture_edit_delta(
        before_reconstruction,
        before_indexes,
        after_reconstruction,
        after_indexes,
        plan,
        operation,
    )


def _mapping_targets(
    reconstruction: Reconstruction,
    indexes: RelationshipIndexes,
    delta: EditDelta,
) -> tuple[tuple[str, dict[int, object], tuple[MappingEntryDelta[object], ...]], ...]:
    """실제 수정할 dict와 해당 Delta entry를 고정 순서로 연결합니다."""

    return (
        ("Camera", reconstruction.cameras, delta.cameras),
        ("Image", reconstruction.images, delta.images),
        ("Point3D", reconstruction.points3d, delta.points3d),
        ("Rig", reconstruction.rigs, delta.rigs),
        ("Frame", reconstruction.frames, delta.frames),
        ("point_to_images", indexes.point_to_images, delta.point_to_images),
        ("image_to_points", indexes.image_to_points, delta.image_to_points),
        ("camera_to_images", indexes.camera_to_images, delta.camera_to_images),
        ("image_to_frame", indexes.image_to_frame, delta.image_to_frame),
    )


def _validate_current_entry(
    mapping: Mapping[int, object],
    entry: MappingEntryDelta[object],
    expected: object | None,
    label: str,
) -> None:
    """현재 영향 entry가 캡처한 기대 값과 정확히 같은지 검사합니다."""

    if expected is None:
        if entry.key in mapping:
            raise ValueError(f"{label} ID {entry.key}가 기대와 달리 존재합니다.")
        return
    if entry.key not in mapping or mapping[entry.key] != expected:
        raise ValueError(f"{label} ID {entry.key}의 현재 값이 Delta 기대 값과 다릅니다.")


def _validate_apply_token(
    token: EditStateToken,
    delta: EditDelta,
    expected_revision: int,
) -> None:
    """현재 Source token과 모델·인덱스 revision이 Delta 경계와 같은지 검사합니다."""

    if token.dataset_uuid != delta.dataset_uuid:
        raise ValueError("Delta의 Dataset UUID가 현재 Runtime token과 다릅니다.")
    if token.source_fingerprint != delta.source_fingerprint:
        raise ValueError("Delta의 source fingerprint가 현재 Runtime token과 다릅니다.")
    if token.edit_revision != token.index_revision:
        raise ValueError("Runtime 모델과 관계 인덱스 revision이 다릅니다.")
    if token.edit_revision != expected_revision:
        raise ValueError("Delta 적용 revision이 현재 Runtime token과 다릅니다.")


def _set_entry_value(
    mapping: dict[int, object],
    key: int,
    value: object | None,
) -> None:
    """None은 제거, 그 외 값은 삽입 또는 교체로 dict에 적용합니다."""

    if value is None:
        mapping.pop(key, None)
    else:
        mapping[key] = value


def _apply_delta(
    reconstruction: Reconstruction,
    indexes: RelationshipIndexes,
    delta: EditDelta,
    token: EditStateToken,
    *,
    forward: bool,
) -> EditStateToken:
    """모든 기대 값을 사전 검증한 뒤 영향 key만 원자적으로 적용합니다."""

    expected_revision = delta.previous_revision if forward else delta.resulting_revision
    resulting_revision = delta.resulting_revision if forward else delta.previous_revision
    _validate_apply_token(token, delta, expected_revision)
    targets = _mapping_targets(reconstruction, indexes, delta)
    for label, mapping, entries in targets:
        for entry in entries:
            expected = entry.before if forward else entry.after
            _validate_current_entry(mapping, entry, expected, label)

    applied: list[tuple[dict[int, object], int, object | None]] = []
    try:
        for _label, mapping, entries in targets:
            for entry in entries:
                previous = entry.before if forward else entry.after
                replacement = entry.after if forward else entry.before
                applied.append((mapping, entry.key, previous))
                _set_entry_value(mapping, entry.key, replacement)
    except Exception:
        for mapping, key, previous in reversed(applied):
            _set_entry_value(mapping, key, previous)
        raise
    return EditStateToken(
        dataset_uuid=token.dataset_uuid,
        source_fingerprint=token.source_fingerprint,
        edit_revision=resulting_revision,
        index_revision=resulting_revision,
    )


def apply_forward(
    reconstruction: Reconstruction,
    indexes: RelationshipIndexes,
    delta: EditDelta,
    token: EditStateToken,
) -> EditStateToken:
    """기존 dict identity를 유지하며 캡처한 forward Delta를 적용합니다."""

    return _apply_delta(reconstruction, indexes, delta, token, forward=True)


def apply_inverse(
    reconstruction: Reconstruction,
    indexes: RelationshipIndexes,
    delta: EditDelta,
    token: EditStateToken,
) -> EditStateToken:
    """기존 dict identity를 유지하며 캡처한 inverse Delta를 적용합니다."""

    return _apply_delta(reconstruction, indexes, delta, token, forward=False)


__all__ = [
    "MappingEntryDelta",
    "EditStateToken",
    "EditDelta",
    "capture_edit_delta",
    "capture_point_removal_delta",
    "capture_image_observation_removal_delta",
    "apply_forward",
    "apply_inverse",
]
