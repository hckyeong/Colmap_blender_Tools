"""애드온 소유 Blender ID를 이름과 무관한 UUID로 식별합니다."""

from __future__ import annotations

from typing import Any, Iterable
from uuid import uuid4


OWNER_KEY = "pp_owner"
OWNER_TOKEN = "blender_projection_painting"
SCHEMA_KEY = "pp_schema"
UUID_KEY = "pp_uuid"
DATASET_UUID_KEY = "pp_dataset_uuid"
RECORD_KIND_KEY = "pp_record_kind"
RECORD_ID_KEY = "pp_record_id"
SOURCE_FINGERPRINT_KEY = "pp_source_fingerprint"
SCENE_UUID_KEY = "pp_scene_uuid"
MANIFEST_KEY = "pp_camera_manifest"
DATASET_REVISION_KEY = "pp_dataset_revision"
MODEL_AXIS_REVISION_KEY = "pp_model_axis_revision"
MODEL_AXIS_SIGNATURE_KEY = "pp_model_axis_signature"
SNAPSHOT_OWNER_KEY = "pp_snapshot_owner"
SNAPSHOT_OWNER_TOKEN = "projection_paint_snapshot"
SNAPSHOT_UUID_KEY = "pp_snapshot_uuid"
SNAPSHOT_SCHEMA_KEY = "pp_snapshot_schema"
SNAPSHOT_ROLE_KEY = "pp_snapshot_role"
SNAPSHOT_RECORD_ID_KEY = "pp_snapshot_record_id"
SNAPSHOT_SOURCE_UUID_KEY = "pp_snapshot_source_uuid"
SNAPSHOT_SOURCE_REVISION_KEY = "pp_snapshot_source_revision"
SNAPSHOT_SOURCE_DOMAIN_UUID_KEY = "pp_snapshot_source_domain_uuid"
SNAPSHOT_SOURCE_DOMAIN_GENERATION_KEY = "pp_snapshot_source_domain_generation"
SNAPSHOT_SOURCE_HISTORY_HASH_KEY = "pp_snapshot_source_history_hash"
PROJECT_STENCIL_DRAW_BRUSH_ROLE = "PROJECT_STENCIL_DRAW_BRUSH"

CURRENT_SCHEMA = 1


def new_uuid() -> str:
    """충돌 가능성이 낮은 새 소유권 UUID를 반환합니다."""

    return uuid4().hex


def ensure_scene_uuid(scene: Any) -> str:
    """Scene에 영구 식별자가 없으면 생성한 뒤 반환합니다."""

    value = str(scene.get(SCENE_UUID_KEY, "") or "")
    if not value:
        value = new_uuid()
        scene[SCENE_UUID_KEY] = value
    return value


def tag_owned(
    id_block: Any,
    kind: str,
    *,
    dataset_uuid: str = "",
    record_id: int | str | None = None,
    source_fingerprint: str = "",
    owned_uuid: str | None = None,
) -> str:
    """Blender ID에 소유권과 역할 Metadata를 기록합니다."""

    token = owned_uuid or new_uuid()
    id_block[OWNER_KEY] = OWNER_TOKEN
    id_block[SCHEMA_KEY] = CURRENT_SCHEMA
    id_block[UUID_KEY] = token
    id_block[RECORD_KIND_KEY] = str(kind)
    if dataset_uuid:
        id_block[DATASET_UUID_KEY] = str(dataset_uuid)
    if record_id is not None:
        id_block[RECORD_ID_KEY] = str(record_id)
    if source_fingerprint:
        id_block[SOURCE_FINGERPRINT_KEY] = str(source_fingerprint)
    return token


def is_owned(
    id_block: Any,
    *,
    kind: str | None = None,
    dataset_uuid: str | None = None,
) -> bool:
    """Blender ID가 요청한 애드온 소유 범위인지 확인합니다."""

    try:
        if id_block.get(OWNER_KEY) != OWNER_TOKEN:
            return False
        if kind is not None and id_block.get(RECORD_KIND_KEY) != kind:
            return False
        if (
            dataset_uuid is not None
            and id_block.get(DATASET_UUID_KEY) != dataset_uuid
        ):
            return False
        return True
    except (AttributeError, ReferenceError, RuntimeError):
        return False


def owned_uuid_of(id_block: Any) -> str:
    """유효한 애드온 소유 ID의 UUID를 반환합니다."""

    if not is_owned(id_block):
        return ""
    try:
        return str(id_block.get(UUID_KEY, "") or "")
    except (AttributeError, ReferenceError, RuntimeError):
        return ""


def dataset_uuid_of(id_block: Any) -> str:
    """유효한 애드온 소유 ID의 Dataset UUID를 반환합니다."""

    if not is_owned(id_block):
        return ""
    try:
        return str(id_block.get(DATASET_UUID_KEY, "") or "")
    except (AttributeError, ReferenceError, RuntimeError):
        return ""


def resolve_owned(
    values: Iterable[Any],
    owned_uuid: str,
    *,
    kind: str | None = None,
    dataset_uuid: str | None = None,
) -> Any | None:
    """허용된 전체 복구 경로에서 UUID가 일치하는 소유 ID를 찾습니다."""

    matches = []
    for value in values:
        if not is_owned(value, kind=kind, dataset_uuid=dataset_uuid):
            continue
        if owned_uuid_of(value) == owned_uuid:
            matches.append(value)
            if len(matches) > 1:
                return None
    return matches[0] if matches else None


def owned_identity(id_block: Any) -> tuple[str, str, str]:
    """소유 ID의 Dataset, 역할과 UUID를 불변 비교값으로 반환합니다."""

    if not is_owned(id_block):
        return "", "", ""


def tag_snapshot_resource(
    id_block: Any,
    *,
    snapshot_uuid: str,
    source_domain_uuid: str,
    source_domain_generation: int,
    source_dataset_uuid: str,
    source_revision: int,
    source_history_hash: str,
    schema_version: int,
) -> None:
    """PP COLMAP ID에 독립 Snapshot 출처와 역할 Metadata를 추가합니다."""

    if not is_owned(id_block, dataset_uuid=snapshot_uuid):
        raise ValueError("Projection Painting 소유 ID가 아닙니다.")
    id_block[SNAPSHOT_OWNER_KEY] = SNAPSHOT_OWNER_TOKEN
    id_block[SNAPSHOT_UUID_KEY] = str(snapshot_uuid)
    id_block[SNAPSHOT_SCHEMA_KEY] = int(schema_version)
    id_block[SNAPSHOT_ROLE_KEY] = str(id_block.get(RECORD_KIND_KEY, "") or "")
    id_block[SNAPSHOT_RECORD_ID_KEY] = str(id_block.get(RECORD_ID_KEY, "") or "")
    id_block[SNAPSHOT_SOURCE_DOMAIN_UUID_KEY] = str(source_domain_uuid)
    id_block[SNAPSHOT_SOURCE_DOMAIN_GENERATION_KEY] = int(source_domain_generation)
    id_block[SNAPSHOT_SOURCE_UUID_KEY] = str(source_dataset_uuid)
    id_block[SNAPSHOT_SOURCE_REVISION_KEY] = int(source_revision)
    id_block[SNAPSHOT_SOURCE_HISTORY_HASH_KEY] = str(source_history_hash)
    try:
        return (
            str(id_block.get(DATASET_UUID_KEY, "") or ""),
            str(id_block.get(RECORD_KIND_KEY, "") or ""),
            str(id_block.get(UUID_KEY, "") or ""),
        )
    except (AttributeError, ReferenceError, RuntimeError):
        return "", "", ""
