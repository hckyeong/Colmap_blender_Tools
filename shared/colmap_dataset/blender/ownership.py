"""공용 COLMAP Dataset Blender ID의 중립 소유권 계약입니다."""

from __future__ import annotations

from collections.abc import Iterable

from ..contracts import DATASET_OWNER_TOKEN, DATASET_SCHEMA_VERSION


OWNER_KEY = "colmap_dataset_owner"
UUID_KEY = "colmap_dataset_uuid"
SCHEMA_KEY = "colmap_dataset_schema"
ROLE_KEY = "colmap_dataset_role"
RECORD_ID_KEY = "colmap_dataset_record_id"
SOURCE_FINGERPRINT_KEY = "colmap_dataset_source_fingerprint"

DATASET_METADATA_KEYS = (
    OWNER_KEY,
    UUID_KEY,
    SCHEMA_KEY,
    ROLE_KEY,
    RECORD_ID_KEY,
    SOURCE_FINGERPRINT_KEY,
)


def mark_dataset_owned(
    data_block,
    *,
    dataset_uuid: str,
    role: str,
    source_fingerprint: str,
    record_id: str | int | None = None,
    schema_version: int = DATASET_SCHEMA_VERSION,
) -> None:
    """Blender ID에 이름과 무관한 공용 Dataset 소유 메타데이터를 기록합니다."""

    if not dataset_uuid or not role or not source_fingerprint:
        raise ValueError("공용 Dataset Blender ID 소유 메타데이터가 비어 있습니다.")
    data_block[OWNER_KEY] = DATASET_OWNER_TOKEN
    data_block[UUID_KEY] = str(dataset_uuid)
    data_block[SCHEMA_KEY] = int(schema_version)
    data_block[ROLE_KEY] = str(role)
    data_block[RECORD_ID_KEY] = "" if record_id is None else str(record_id)
    data_block[SOURCE_FINGERPRINT_KEY] = str(source_fingerprint)


def copy_dataset_metadata(source, target) -> None:
    """기존 Blender ID 정체성을 유지하며 공용 Dataset 메타데이터를 복사합니다."""

    for key in DATASET_METADATA_KEYS:
        if key in source:
            target[key] = source[key]


def is_dataset_owned(
    data_block,
    *,
    dataset_uuid: str | None = None,
    role: str | None = None,
    record_id: str | int | None = None,
) -> bool:
    """Blender ID가 요청한 공용 Dataset 소유 조건을 모두 만족하는지 확인합니다."""

    try:
        if data_block is None or data_block.get(OWNER_KEY) != DATASET_OWNER_TOKEN:
            return False
        if int(data_block.get(SCHEMA_KEY, 0)) != DATASET_SCHEMA_VERSION:
            return False
        if dataset_uuid is not None and data_block.get(UUID_KEY) != str(dataset_uuid):
            return False
        if role is not None and data_block.get(ROLE_KEY) != str(role):
            return False
        if record_id is not None and data_block.get(RECORD_ID_KEY) != str(record_id):
            return False
    except (AttributeError, ReferenceError, RuntimeError, TypeError, ValueError):
        return False
    return True


def dataset_owned_items(
    values: Iterable,
    *,
    dataset_uuid: str,
    role: str,
):
    """주어진 Blender ID 목록에서 공용 Dataset 조건에 맞는 항목만 순회합니다."""

    for value in values:
        if is_dataset_owned(value, dataset_uuid=dataset_uuid, role=role):
            yield value


__all__ = [
    "DATASET_METADATA_KEYS",
    "OWNER_KEY",
    "RECORD_ID_KEY",
    "ROLE_KEY",
    "SCHEMA_KEY",
    "SOURCE_FINGERPRINT_KEY",
    "UUID_KEY",
    "copy_dataset_metadata",
    "dataset_owned_items",
    "is_dataset_owned",
    "mark_dataset_owned",
]
