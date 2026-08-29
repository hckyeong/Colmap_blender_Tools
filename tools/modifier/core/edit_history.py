"""편집 작업 생성, schema 검증과 canonical JSON 변환을 담당합니다."""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any, Iterable

from .edit_types import (
    EDIT_SCHEMA_VERSION,
    LEGACY_EDIT_SCHEMA_VERSION,
    REMOVE_IMAGES_OPERATION,
    REMOVE_POINTS_OPERATION,
    REMOVE_SELECTED_OPERATION,
    AnyEditOperation,
    EditOperation,
    ImageObservationRemovalReport,
    LegacyEditOperation,
    PointRemovalReport,
    RemovalReport,
)


def _new_operation_base(
    operation_type: str,
    dataset_uuid: str,
    source_fingerprint: str,
    previous_revision: int,
) -> dict[str, Any]:
    return {
        "schema_version": EDIT_SCHEMA_VERSION,
        "operation_id": str(uuid.uuid4()),
        "operation_type": operation_type,
        "dataset_uuid": dataset_uuid,
        "source_fingerprint": source_fingerprint,
        "previous_revision": previous_revision,
        "resulting_revision": previous_revision + 1,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
    }


def point_operation_from_report(
    report: PointRemovalReport,
    dataset_uuid: str,
    source_fingerprint: str,
    previous_revision: int,
) -> EditOperation:
    return EditOperation(
        **_new_operation_base(
            REMOVE_POINTS_OPERATION,
            dataset_uuid,
            source_fingerprint,
            previous_revision,
        ),
        requested_point3d_ids=report.requested_point3d_ids,
        requested_image_ids=(),
        removed_point3d_ids=report.requested_point3d_ids,
        removed_image_ids=(),
        removed_camera_ids=(),
        removed_frame_ids=(),
        removed_rig_ids=(),
    )


def image_operation_from_report(
    report: ImageObservationRemovalReport,
    dataset_uuid: str,
    source_fingerprint: str,
    previous_revision: int,
) -> EditOperation:
    return EditOperation(
        **_new_operation_base(
            REMOVE_IMAGES_OPERATION,
            dataset_uuid,
            source_fingerprint,
            previous_revision,
        ),
        requested_point3d_ids=(),
        requested_image_ids=report.requested_image_ids,
        removed_point3d_ids=report.observed_point3d_ids,
        removed_image_ids=report.requested_image_ids,
        removed_camera_ids=report.removed_camera_ids,
        removed_frame_ids=report.removed_frame_ids,
        removed_rig_ids=report.removed_rig_ids,
    )


def operation_from_report(
    report: RemovalReport,
    dataset_uuid: str,
    source_fingerprint: str,
    previous_revision: int,
) -> LegacyEditOperation:
    """0.2.1 테스트와 기존 저장 형식을 위한 schema 1 생성기입니다."""
    return LegacyEditOperation(
        schema_version=LEGACY_EDIT_SCHEMA_VERSION,
        operation_id=str(uuid.uuid4()),
        operation_type=REMOVE_SELECTED_OPERATION,
        dataset_uuid=dataset_uuid,
        source_fingerprint=source_fingerprint,
        requested_point3d_ids=report.requested_point3d_ids,
        removed_image_ids=report.observing_image_ids,
        derived_orphan_point3d_ids=report.derived_orphan_point3d_ids,
        removed_camera_ids=report.removed_camera_ids,
        removed_frame_ids=report.removed_frame_ids,
        removed_rig_ids=report.removed_rig_ids,
        previous_revision=previous_revision,
        resulting_revision=previous_revision + 1,
        created_at_utc=datetime.now(timezone.utc).isoformat(),
    )


def operation_to_dict(operation: AnyEditOperation) -> dict[str, Any]:
    return asdict(operation)


def _validate_common_operation_fields(normalized: dict[str, Any]) -> None:
    for field_name in (
        "operation_id",
        "operation_type",
        "dataset_uuid",
        "source_fingerprint",
        "created_at_utc",
    ):
        if not isinstance(normalized[field_name], str):
            raise ValueError(f"{field_name}은 문자열이어야 합니다.")
    for field_name in ("previous_revision", "resulting_revision"):
        if type(normalized[field_name]) is not int or normalized[field_name] < 0:
            raise ValueError(f"{field_name}은 0 이상의 정수여야 합니다.")
    try:
        operation_uuid = uuid.UUID(normalized["operation_id"])
        dataset_uuid = uuid.UUID(normalized["dataset_uuid"])
    except (ValueError, AttributeError) as exc:
        raise ValueError("operation_id와 dataset_uuid는 유효한 UUID여야 합니다.") from exc
    if (
        str(operation_uuid) != normalized["operation_id"]
        or str(dataset_uuid) != normalized["dataset_uuid"]
    ):
        raise ValueError("UUID는 표준 소문자 하이픈 형식이어야 합니다.")
    if re.fullmatch(r"[0-9a-f]{64}", normalized["source_fingerprint"]) is None:
        raise ValueError("source_fingerprint는 소문자 SHA-256 문자열이어야 합니다.")
    try:
        created_at = datetime.fromisoformat(normalized["created_at_utc"])
    except ValueError as exc:
        raise ValueError("created_at_utc가 올바른 ISO 8601 시각이 아닙니다.") from exc
    if created_at.tzinfo is None or created_at.utcoffset() != timezone.utc.utcoffset(created_at):
        raise ValueError("created_at_utc는 UTC timezone을 포함해야 합니다.")
    if normalized["resulting_revision"] != normalized["previous_revision"] + 1:
        raise ValueError("EditOperation revision이 연속적이지 않습니다.")


def _normalize_id_fields(
    normalized: dict[str, Any],
    field_names: Iterable[str],
) -> None:
    for field_name in field_names:
        if not isinstance(normalized[field_name], list):
            raise ValueError(f"{field_name}은 JSON 목록이어야 합니다.")
        if any(type(item) is not int or item < 0 for item in normalized[field_name]):
            raise ValueError(f"{field_name}에는 0 이상의 정수 ID만 허용됩니다.")
        values = tuple(int(item) for item in normalized[field_name])
        if values != tuple(sorted(set(values))):
            raise ValueError(f"{field_name}은 중복 없는 오름차순 ID 목록이어야 합니다.")
        normalized[field_name] = values


def operation_from_dict(value: dict[str, Any]) -> AnyEditOperation:
    if not isinstance(value, dict):
        raise ValueError("EditOperation은 JSON 객체여야 합니다.")
    if type(value.get("schema_version")) is not int:
        raise ValueError("EditOperation schema_version은 정수여야 합니다.")
    schema_version = value["schema_version"]
    if schema_version == LEGACY_EDIT_SCHEMA_VERSION:
        operation_class = LegacyEditOperation
        valid_types = {REMOVE_SELECTED_OPERATION}
        tuple_fields = (
            "requested_point3d_ids",
            "removed_image_ids",
            "derived_orphan_point3d_ids",
            "removed_camera_ids",
            "removed_frame_ids",
            "removed_rig_ids",
        )
    elif schema_version == EDIT_SCHEMA_VERSION:
        operation_class = EditOperation
        valid_types = {REMOVE_POINTS_OPERATION, REMOVE_IMAGES_OPERATION}
        tuple_fields = (
            "requested_point3d_ids",
            "requested_image_ids",
            "removed_point3d_ids",
            "removed_image_ids",
            "removed_camera_ids",
            "removed_frame_ids",
            "removed_rig_ids",
        )
    else:
        raise ValueError("지원하지 않는 EditOperation schema version입니다.")

    required = {field.name for field in operation_class.__dataclass_fields__.values()}
    if set(value) != required:
        raise ValueError("EditOperation 필드 구성이 해당 schema와 일치하지 않습니다.")
    if value.get("operation_type") not in valid_types:
        raise ValueError(f"지원하지 않는 편집 작업입니다: {value.get('operation_type')}")
    normalized = dict(value)
    _normalize_id_fields(normalized, tuple_fields)
    _validate_common_operation_fields(normalized)
    operation = operation_class(**normalized)
    if isinstance(operation, EditOperation):
        if operation.operation_type == REMOVE_POINTS_OPERATION:
            if (
                not operation.requested_point3d_ids
                or operation.requested_image_ids
                or operation.removed_point3d_ids != operation.requested_point3d_ids
                or operation.removed_image_ids
                or operation.removed_camera_ids
                or operation.removed_frame_ids
                or operation.removed_rig_ids
            ):
                raise ValueError("Point 제거 EditOperation의 ID 필드 구성이 올바르지 않습니다.")
        elif (
            operation.requested_point3d_ids
            or not operation.requested_image_ids
            or operation.removed_image_ids != operation.requested_image_ids
        ):
            raise ValueError("Image 제거 EditOperation의 ID 필드 구성이 올바르지 않습니다.")
    return operation


def serialize_history(operations: Iterable[AnyEditOperation]) -> str:
    return json.dumps(
        [operation_to_dict(operation) for operation in operations],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def deserialize_history(value: str) -> tuple[AnyEditOperation, ...]:
    try:
        raw = json.loads(value or "[]")
    except json.JSONDecodeError as exc:
        raise ValueError(f"편집 이력 JSON이 손상되었습니다: {exc}") from exc
    if not isinstance(raw, list):
        raise ValueError("편집 이력 JSON의 최상위 값은 목록이어야 합니다.")
    return tuple(operation_from_dict(item) for item in raw)
