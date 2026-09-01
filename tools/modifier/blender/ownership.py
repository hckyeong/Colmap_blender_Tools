"""기존 Modifier와 공용 Dataset 소유권을 함께 식별하는 호환 Adapter입니다."""

try:
    from ....shared.colmap_dataset.blender.ownership import (
        DATASET_METADATA_KEYS,
        RECORD_ID_KEY as COMMON_RECORD_ID_KEY,
        ROLE_KEY as COMMON_ROLE_KEY,
        copy_dataset_metadata,
        is_dataset_owned,
        mark_dataset_owned,
    )
except ImportError:
    from shared.colmap_dataset.blender.ownership import (
        DATASET_METADATA_KEYS,
        RECORD_ID_KEY as COMMON_RECORD_ID_KEY,
        ROLE_KEY as COMMON_ROLE_KEY,
        copy_dataset_metadata,
        is_dataset_owned,
        mark_dataset_owned,
    )

OWNER_KEY = "blender_colmap_owner"
OWNER_TOKEN = "blender_colmap"
SCHEMA_KEY = "blender_colmap_schema"
DATASET_UUID_KEY = "blender_colmap_dataset_uuid"
RECORD_KIND_KEY = "blender_colmap_record_kind"
SOURCE_FINGERPRINT_KEY = "blender_colmap_source_fingerprint"


LEGACY_METADATA_KEYS = (
    OWNER_KEY,
    SCHEMA_KEY,
    DATASET_UUID_KEY,
    RECORD_KIND_KEY,
    SOURCE_FINGERPRINT_KEY,
)


__all__ = [
    "COMMON_RECORD_ID_KEY",
    "COMMON_ROLE_KEY",
    "DATASET_METADATA_KEYS",
    "DATASET_UUID_KEY",
    "LEGACY_METADATA_KEYS",
    "OWNER_KEY",
    "OWNER_TOKEN",
    "RECORD_KIND_KEY",
    "SCHEMA_KEY",
    "SOURCE_FINGERPRINT_KEY",
    "copy_dataset_metadata",
    "is_dataset_owned",
    "mark_dataset_owned",
]
