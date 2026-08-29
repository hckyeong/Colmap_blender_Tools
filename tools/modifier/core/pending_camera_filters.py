"""Camera Review 지연 필터의 불변 token, 합집합과 JSON을 관리합니다."""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
import uuid
from typing import Iterable

from .indexes import RelationshipIndexes


PENDING_CAMERA_FILTER_SCHEMA_VERSION = 1
_SERIALIZED_FIELDS = frozenset(
    (
        "schema_version",
        "dataset_uuid",
        "source_fingerprint",
        "edit_revision",
        "index_revision",
        "history_hash",
        "image_ids",
    )
)
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")


def _validate_uuid(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("Pending Dataset UUID는 문자열이어야 합니다.")
    try:
        parsed = uuid.UUID(value)
    except (AttributeError, ValueError) as exc:
        raise ValueError("Pending Dataset UUID가 올바르지 않습니다.") from exc
    if str(parsed) != value:
        raise ValueError("Pending Dataset UUID는 표준 소문자 하이픈 형식이어야 합니다.")
    return value


def _validate_sha256(value: object, label: str) -> str:
    if not isinstance(value, str) or _SHA256_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{label}는 소문자 SHA-256 문자열이어야 합니다.")
    return value


def _validate_revision(value: object, label: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"{label}은 0 이상의 정수여야 합니다.")
    return value


def _validate_image_ids(
    values: object,
    *,
    allow_empty: bool,
) -> tuple[int, ...]:
    if not isinstance(values, (list, tuple)):
        raise ValueError("Pending IMAGE_ID는 목록 또는 tuple이어야 합니다.")
    image_ids = tuple(values)
    if any(type(image_id) is not int or image_id < 0 for image_id in image_ids):
        raise ValueError("Pending IMAGE_ID에는 0 이상의 정수만 허용됩니다.")
    if image_ids != tuple(sorted(set(image_ids))):
        raise ValueError("Pending IMAGE_ID는 중복 없는 오름차순이어야 합니다.")
    if not allow_empty and not image_ids:
        raise ValueError("Pending IMAGE_ID가 하나 이상 필요합니다.")
    return image_ids


@dataclass(frozen=True)
class PendingCameraFilterToken:
    dataset_uuid: str
    source_fingerprint: str
    edit_revision: int
    index_revision: int
    history_hash: str

    def __post_init__(self) -> None:
        _validate_uuid(self.dataset_uuid)
        _validate_sha256(self.source_fingerprint, "Pending source fingerprint")
        _validate_revision(self.edit_revision, "Pending edit revision")
        _validate_revision(self.index_revision, "Pending index revision")
        _validate_sha256(self.history_hash, "Pending history hash")
        if self.edit_revision != self.index_revision:
            raise ValueError(
                "Pending token의 모델 revision과 관계 인덱스 revision이 다릅니다."
            )


@dataclass(frozen=True)
class PendingCameraFilters:
    token: PendingCameraFilterToken
    image_ids: tuple[int, ...]
    preview_point3d_ids: frozenset[int]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "image_ids",
            _validate_image_ids(self.image_ids, allow_empty=True),
        )
        point_ids = frozenset(self.preview_point3d_ids)
        if any(type(point_id) is not int or point_id < 0 for point_id in point_ids):
            raise ValueError("Pending Point3D ID에는 0 이상의 정수만 허용됩니다.")
        object.__setattr__(self, "preview_point3d_ids", point_ids)


def validate_pending_token(
    token: PendingCameraFilterToken,
    *,
    dataset_uuid: str,
    source_fingerprint: str,
    edit_revision: int,
    index_revision: int,
    history_hash: str,
) -> None:
    """현재 권위 Runtime token과 다르면 자동 적용하지 않고 거부합니다."""
    current = PendingCameraFilterToken(
        dataset_uuid=dataset_uuid,
        source_fingerprint=source_fingerprint,
        edit_revision=edit_revision,
        index_revision=index_revision,
        history_hash=history_hash,
    )
    if token != current:
        raise ValueError(
            "Pending Camera Filter가 현재 Dataset, source 또는 revision과 다릅니다."
        )


def pending_filters_from_image_ids(
    token: PendingCameraFilterToken,
    image_ids: Iterable[int],
    indexes: RelationshipIndexes,
) -> PendingCameraFilters:
    """권위 IMAGE_ID에서 표시용 Point3D 합집합을 다시 계산합니다."""
    normalized = _validate_image_ids(tuple(image_ids), allow_empty=True)
    missing = [
        image_id
        for image_id in normalized
        if image_id not in indexes.image_to_points
    ]
    if missing:
        raise ValueError(
            f"현재 관계 인덱스에 없는 Pending IMAGE_ID가 있습니다: {missing[:10]}"
        )
    point_ids = frozenset(
        point_id
        for image_id in normalized
        for point_id in indexes.image_to_points[image_id]
    )
    return PendingCameraFilters(token, normalized, point_ids)


def add_pending_image(
    pending: PendingCameraFilters,
    image_id: int,
    indexes: RelationshipIndexes,
) -> PendingCameraFilters:
    """IMAGE_ID 하나와 관측 Point3D를 기존 불변 Pending에 합칩니다."""
    if type(image_id) is not int or image_id < 0:
        raise ValueError("Pending IMAGE_ID는 0 이상의 정수여야 합니다.")
    if image_id in pending.image_ids:
        return pending
    if image_id not in indexes.image_to_points:
        raise ValueError(f"현재 관계 인덱스에 IMAGE_ID {image_id}가 없습니다.")
    image_ids = tuple(sorted((*pending.image_ids, image_id)))
    points = pending.preview_point3d_ids | indexes.image_to_points[image_id]
    return PendingCameraFilters(pending.token, image_ids, frozenset(points))


def remove_pending_image(
    pending: PendingCameraFilters,
    image_id: int,
    indexes: RelationshipIndexes,
) -> PendingCameraFilters:
    """IMAGE_ID 하나를 제거하고 남은 Camera의 Point3D 합집합을 재계산합니다."""

    if type(image_id) is not int or image_id < 0:
        raise ValueError("Pending IMAGE_ID는 0 이상의 정수여야 합니다.")
    if image_id not in pending.image_ids:
        return pending
    remaining = tuple(
        pending_image_id
        for pending_image_id in pending.image_ids
        if pending_image_id != image_id
    )
    return pending_filters_from_image_ids(pending.token, remaining, indexes)


def serialize_pending_filters(pending: PendingCameraFilters | None) -> str:
    """Point 파생 캐시를 제외한 Pending 의도만 canonical JSON으로 저장합니다."""
    if pending is None or not pending.image_ids:
        return "{}"
    token = pending.token
    return json.dumps(
        {
            "schema_version": PENDING_CAMERA_FILTER_SCHEMA_VERSION,
            "dataset_uuid": token.dataset_uuid,
            "source_fingerprint": token.source_fingerprint,
            "edit_revision": token.edit_revision,
            "index_revision": token.index_revision,
            "history_hash": token.history_hash,
            "image_ids": list(pending.image_ids),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def deserialize_pending_filters(
    value: str,
    indexes: RelationshipIndexes,
) -> PendingCameraFilters | None:
    """저장 JSON을 엄격히 검증하고 Point 표시 집합을 현재 인덱스로 재생성합니다."""
    if not value:
        return None
    try:
        raw = json.loads(value)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Pending Camera Filter JSON이 손상되었습니다: {exc}") from exc
    if raw == {}:
        return None
    if not isinstance(raw, dict) or set(raw) != _SERIALIZED_FIELDS:
        raise ValueError("Pending Camera Filter JSON 필드 구성이 올바르지 않습니다.")
    if (
        type(raw["schema_version"]) is not int
        or raw["schema_version"] != PENDING_CAMERA_FILTER_SCHEMA_VERSION
    ):
        raise ValueError("지원하지 않는 Pending Camera Filter schema입니다.")
    image_ids = _validate_image_ids(raw["image_ids"], allow_empty=False)
    token = PendingCameraFilterToken(
        dataset_uuid=raw["dataset_uuid"],
        source_fingerprint=raw["source_fingerprint"],
        edit_revision=raw["edit_revision"],
        index_revision=raw["index_revision"],
        history_hash=raw["history_hash"],
    )
    return pending_filters_from_image_ids(token, image_ids, indexes)


__all__ = [
    "PENDING_CAMERA_FILTER_SCHEMA_VERSION",
    "PendingCameraFilterToken",
    "PendingCameraFilters",
    "add_pending_image",
    "deserialize_pending_filters",
    "pending_filters_from_image_ids",
    "remove_pending_image",
    "serialize_pending_filters",
    "validate_pending_token",
]
