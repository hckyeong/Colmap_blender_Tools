"""Modifier 도메인의 source·편집 이력을 결정론적으로 영속화합니다."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath
from typing import Any

from .edit_history import operation_from_dict, operation_to_dict, dataset_transform_from_history
from .edit_replay import history_hash
from .edit_types import AnyEditOperation, EditOperation, LegacyEditOperation, TransformOperation
from .model import SourceDescriptor


PERSISTENCE_SCHEMA_VERSION = 1
SOURCE_SCHEMA_VERSION = 1
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")


class DomainPersistenceError(ValueError):
    """도메인 영속 데이터가 계약을 위반했음을 나타냅니다."""


class UnsupportedDomainSchemaError(DomainPersistenceError):
    """자동 변환할 수 없는 영속 또는 source schema임을 나타냅니다."""


class DomainChecksumError(DomainPersistenceError):
    """도메인 envelope의 content checksum 손상을 나타냅니다."""


class DomainSourceIdentityError(DomainPersistenceError):
    """도메인과 편집 이력의 source 식별자 불일치를 나타냅니다."""


@dataclass(frozen=True, slots=True)
class DomainPersistenceHeader:
    """도메인 식별자와 단조 generation 및 편집 이력 token입니다."""

    schema_version: int
    domain_uuid: str
    domain_generation: int
    dataset_uuid: str
    edit_revision: int
    history_hash: str


@dataclass(frozen=True, slots=True)
class DomainSourceMetadata:
    """Runtime을 다시 읽기 위한 SourceDescriptor의 영속 필드입니다."""

    dataset_root: str
    sparse_root: str
    model_directory: str
    images_directory: str
    input_format: str
    layout: str
    selected_files: tuple[str, ...]
    source_fingerprint: str
    schema_version: int


@dataclass(frozen=True, slots=True)
class DomainPersistencePayload:
    """Runtime 전체 상태를 제외한 Modifier 도메인 영속 payload입니다."""

    header: DomainPersistenceHeader
    source: DomainSourceMetadata
    operations: tuple[AnyEditOperation, ...]


def _canonical_json(value: Any) -> str:
    """값을 결정론적인 compact UTF-8 JSON 문자열로 변환합니다."""

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _content_checksum(payload_value: dict[str, Any]) -> str:
    """checksum 필드가 없는 canonical payload의 SHA-256을 계산합니다."""

    canonical = _canonical_json(payload_value).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _require_exact_int(value: Any, field_name: str) -> int:
    """bool을 제외한 0 이상의 정수 필드를 검증합니다."""

    if type(value) is not int or value < 0:
        raise DomainPersistenceError(f"{field_name}은 0 이상의 정수여야 합니다.")
    return value


def _require_canonical_uuid(value: Any, field_name: str) -> str:
    """UUID 문자열이 표준 소문자 하이픈 형식인지 검증합니다."""

    if not isinstance(value, str):
        raise DomainPersistenceError(f"{field_name}은 UUID 문자열이어야 합니다.")
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError) as exc:
        raise DomainPersistenceError(f"{field_name}은 유효한 UUID여야 합니다.") from exc
    if str(parsed) != value:
        raise DomainPersistenceError(
            f"{field_name}은 표준 소문자 하이픈 UUID 형식이어야 합니다."
        )
    return value


def _require_sha256(value: Any, field_name: str) -> str:
    """필드가 소문자 SHA-256 문자열인지 검증합니다."""

    if not isinstance(value, str) or _SHA256_PATTERN.fullmatch(value) is None:
        raise DomainPersistenceError(
            f"{field_name}은 소문자 SHA-256 문자열이어야 합니다."
        )
    return value


def _absolute_pure_path(value: Any, field_name: str) -> PurePath:
    """POSIX와 Windows 경로 문법을 보존하며 절대경로를 검증합니다."""

    if not isinstance(value, str) or not value or "\x00" in value:
        raise DomainPersistenceError(f"{field_name}은 비어 있지 않은 경로 문자열이어야 합니다.")

    windows_path = PureWindowsPath(value)
    posix_path = PurePosixPath(value)
    if windows_path.drive and windows_path.is_absolute():
        selected: PurePath = windows_path
        raw_parts = value.replace("/", "\\").split("\\")
    elif posix_path.is_absolute():
        selected = posix_path
        raw_parts = value.split("/")
    else:
        raise DomainPersistenceError(f"{field_name}은 절대경로여야 합니다: {value}")

    if any(part in {".", ".."} for part in raw_parts) or ".." in selected.parts:
        raise DomainPersistenceError(f"{field_name}에 정규화되지 않은 경로 구간이 있습니다.")
    return selected


def _validate_source_metadata(source: DomainSourceMetadata) -> None:
    """SourceDescriptor 필드와 선택 파일의 source 경계를 검증합니다."""

    if not isinstance(source, DomainSourceMetadata):
        raise DomainPersistenceError("source는 DomainSourceMetadata여야 합니다.")
    if type(source.schema_version) is not int:
        raise DomainPersistenceError("source schema_version은 정수여야 합니다.")
    if source.schema_version != SOURCE_SCHEMA_VERSION:
        raise UnsupportedDomainSchemaError(
            f"지원하지 않는 source schema version입니다: {source.schema_version}"
        )
    if source.input_format not in {"binary", "text"}:
        raise DomainPersistenceError("source input_format은 binary 또는 text여야 합니다.")
    if source.layout not in {"legacy", "modern"}:
        raise DomainPersistenceError("source layout은 legacy 또는 modern이어야 합니다.")
    _require_sha256(source.source_fingerprint, "source_fingerprint")

    _absolute_pure_path(source.dataset_root, "dataset_root")
    sparse_root = _absolute_pure_path(source.sparse_root, "sparse_root")
    model_directory = _absolute_pure_path(source.model_directory, "model_directory")
    _absolute_pure_path(source.images_directory, "images_directory")
    expected_sparse_root = (
        model_directory.parent
        if model_directory.name.isdigit()
        and model_directory.parent.name.lower() == "sparse"
        else model_directory
    )
    if sparse_root != expected_sparse_root:
        raise DomainPersistenceError(
            "sparse_root가 model_directory에서 유도되는 source 경계와 다릅니다."
        )

    if not isinstance(source.selected_files, tuple):
        raise DomainPersistenceError("selected_files는 경로 tuple이어야 합니다.")
    extension = "bin" if source.input_format == "binary" else "txt"
    stems = (
        ("rigs", "cameras", "frames", "images", "points3D")
        if source.layout == "modern"
        else ("cameras", "images", "points3D")
    )
    expected_files = tuple(model_directory / f"{stem}.{extension}" for stem in stems)
    selected_files = tuple(
        _absolute_pure_path(value, f"selected_files[{index}]")
        for index, value in enumerate(source.selected_files)
    )
    if selected_files != expected_files:
        raise DomainPersistenceError(
            "selected_files가 model_directory의 확정 COLMAP source 파일 집합과 다릅니다."
        )


def _validated_operations(
    operations: tuple[AnyEditOperation, ...],
) -> tuple[AnyEditOperation, ...]:
    """각 operation을 기존 역직렬화 계약으로 다시 검증합니다."""

    if not isinstance(operations, tuple):
        raise DomainPersistenceError("operations는 EditOperation tuple이어야 합니다.")
    validated: list[AnyEditOperation] = []
    for index, operation in enumerate(operations):
        if not isinstance(operation, (LegacyEditOperation, EditOperation, TransformOperation)):
            raise DomainPersistenceError(
                f"operations[{index}]가 지원하는 EditOperation이 아닙니다."
            )
        operation_value = json.loads(_canonical_json(operation_to_dict(operation)))
        try:
            normalized = operation_from_dict(operation_value)
        except ValueError as exc:
            raise DomainPersistenceError(
                f"operations[{index}] 검증에 실패했습니다: {exc}"
            ) from exc
        if normalized != operation:
            raise DomainPersistenceError(
                f"operations[{index}]의 역직렬화 결과가 원본과 다릅니다."
            )
        validated.append(normalized)
    return tuple(validated)


def validate_domain_payload(payload: DomainPersistencePayload) -> None:
    """도메인 payload 전체의 schema·source·이력 연속성을 엄격히 검증합니다."""

    if not isinstance(payload, DomainPersistencePayload):
        raise DomainPersistenceError("payload는 DomainPersistencePayload여야 합니다.")
    header = payload.header
    if not isinstance(header, DomainPersistenceHeader):
        raise DomainPersistenceError("header는 DomainPersistenceHeader여야 합니다.")
    if type(header.schema_version) is not int:
        raise DomainPersistenceError("schema_version은 정수여야 합니다.")
    if header.schema_version != PERSISTENCE_SCHEMA_VERSION:
        raise UnsupportedDomainSchemaError(
            f"지원하지 않는 domain persistence schema version입니다: "
            f"{header.schema_version}"
        )
    _require_canonical_uuid(header.domain_uuid, "domain_uuid")
    _require_canonical_uuid(header.dataset_uuid, "dataset_uuid")
    _require_exact_int(header.domain_generation, "domain_generation")
    _require_exact_int(header.edit_revision, "edit_revision")
    _require_sha256(header.history_hash, "history_hash")
    _validate_source_metadata(payload.source)

    operations = _validated_operations(payload.operations)
    try:
        dataset_transform_from_history(operations)
    except ValueError as exc:
        raise DomainPersistenceError(str(exc)) from exc
    if len(operations) != header.edit_revision:
        raise DomainPersistenceError(
            "operations 길이와 edit_revision이 일치하지 않습니다."
        )
    if history_hash(operations) != header.history_hash:
        raise DomainPersistenceError("history_hash가 operations와 일치하지 않습니다.")
    for expected_revision, operation in enumerate(operations):
        if operation.dataset_uuid != header.dataset_uuid:
            raise DomainSourceIdentityError(
                f"operations[{expected_revision}]의 dataset_uuid가 source와 다릅니다."
            )
        if operation.source_fingerprint != payload.source.source_fingerprint:
            raise DomainSourceIdentityError(
                f"operations[{expected_revision}]의 source_fingerprint가 source와 다릅니다."
            )
        if (
            operation.previous_revision != expected_revision
            or operation.resulting_revision != expected_revision + 1
        ):
            raise DomainPersistenceError(
                f"operations[{expected_revision}]의 revision 순서가 연속적이지 않습니다."
            )


def make_domain_payload(
    source: SourceDescriptor,
    operations: tuple[AnyEditOperation, ...],
    *,
    domain_uuid: str,
    domain_generation: int,
    edit_revision: int | None = None,
    history_hash_value: str | None = None,
) -> DomainPersistencePayload:
    """SourceDescriptor와 확정 이력으로 검증된 영속 payload를 생성합니다."""

    if not isinstance(source, SourceDescriptor):
        raise DomainPersistenceError("source는 SourceDescriptor여야 합니다.")
    operation_tuple = tuple(operations) if isinstance(operations, tuple) else operations
    revision = len(operation_tuple) if edit_revision is None else edit_revision
    stored_history_hash = (
        history_hash(operation_tuple)
        if history_hash_value is None
        else history_hash_value
    )
    payload = DomainPersistencePayload(
        header=DomainPersistenceHeader(
            schema_version=PERSISTENCE_SCHEMA_VERSION,
            domain_uuid=domain_uuid,
            domain_generation=domain_generation,
            dataset_uuid=source.dataset_uuid,
            edit_revision=revision,
            history_hash=stored_history_hash,
        ),
        source=DomainSourceMetadata(
            dataset_root=str(source.dataset_root),
            sparse_root=str(source.sparse_root),
            model_directory=str(source.model_directory),
            images_directory=str(source.images_directory),
            input_format=source.input_format,
            layout=source.layout,
            selected_files=tuple(str(path) for path in source.selected_files),
            source_fingerprint=source.source_fingerprint,
            schema_version=source.schema_version,
        ),
        operations=operation_tuple,
    )
    validate_domain_payload(payload)
    return payload


def _payload_to_dict(payload: DomainPersistencePayload) -> dict[str, Any]:
    """검증된 payload를 canonical JSON용 기본 타입 객체로 변환합니다."""

    validate_domain_payload(payload)
    return {
        "header": asdict(payload.header),
        "operations": [operation_to_dict(item) for item in payload.operations],
        "source": {
            **asdict(payload.source),
            "selected_files": list(payload.source.selected_files),
        },
    }


def encode_domain_payload(payload: DomainPersistencePayload) -> str:
    """payload checksum을 포함한 canonical UTF-8 JSON envelope를 생성합니다."""

    payload_value = _payload_to_dict(payload)
    envelope = {
        "checksum": _content_checksum(payload_value),
        "payload": payload_value,
    }
    return _canonical_json(envelope)


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """JSON 객체의 중복 key를 손상으로 간주해 거부합니다."""

    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise DomainPersistenceError(f"JSON 객체에 중복 key가 있습니다: {key}")
        value[key] = item
    return value


def _reject_json_constant(value: str) -> None:
    """JSON 표준 밖의 NaN·Infinity 상수를 거부합니다."""

    raise DomainPersistenceError(f"지원하지 않는 JSON 숫자 상수입니다: {value}")


def _require_exact_fields(
    value: Any,
    expected: set[str],
    field_name: str,
) -> dict[str, Any]:
    """JSON 객체가 요구 필드만 정확히 포함하는지 검증합니다."""

    if not isinstance(value, dict) or set(value) != expected:
        raise DomainPersistenceError(f"{field_name} 필드 구성이 schema와 일치하지 않습니다.")
    return value


def _payload_from_dict(value: Any) -> DomainPersistencePayload:
    """검증 전 JSON payload를 불변 도메인 타입으로 재구성합니다."""

    payload_value = _require_exact_fields(
        value,
        {"header", "operations", "source"},
        "payload",
    )
    header_value = _require_exact_fields(
        payload_value["header"],
        {
            "schema_version",
            "domain_uuid",
            "domain_generation",
            "dataset_uuid",
            "edit_revision",
            "history_hash",
        },
        "header",
    )
    source_value = _require_exact_fields(
        payload_value["source"],
        {
            "dataset_root",
            "sparse_root",
            "model_directory",
            "images_directory",
            "input_format",
            "layout",
            "selected_files",
            "source_fingerprint",
            "schema_version",
        },
        "source",
    )
    selected_files = source_value["selected_files"]
    if not isinstance(selected_files, list) or any(
        not isinstance(item, str) for item in selected_files
    ):
        raise DomainPersistenceError("selected_files는 경로 문자열 JSON 목록이어야 합니다.")
    raw_operations = payload_value["operations"]
    if not isinstance(raw_operations, list):
        raise DomainPersistenceError("operations는 JSON 목록이어야 합니다.")
    try:
        operations = tuple(operation_from_dict(item) for item in raw_operations)
    except ValueError as exc:
        raise DomainPersistenceError(f"operations 역직렬화에 실패했습니다: {exc}") from exc

    payload = DomainPersistencePayload(
        header=DomainPersistenceHeader(**header_value),
        source=DomainSourceMetadata(
            **{
                **source_value,
                "selected_files": tuple(selected_files),
            }
        ),
        operations=operations,
    )
    validate_domain_payload(payload)
    return payload


def decode_domain_payload(encoded: str | bytes) -> DomainPersistencePayload:
    """UTF-8 JSON envelope의 checksum과 payload 계약을 검증해 복원합니다."""

    if isinstance(encoded, bytes):
        try:
            text = encoded.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise DomainPersistenceError("영속 payload가 올바른 UTF-8이 아닙니다.") from exc
    elif isinstance(encoded, str):
        text = encoded
    else:
        raise DomainPersistenceError("영속 payload는 str 또는 UTF-8 bytes여야 합니다.")
    try:
        decoded = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
    except json.JSONDecodeError as exc:
        raise DomainPersistenceError(f"영속 payload JSON이 손상되었습니다: {exc}") from exc

    envelope = _require_exact_fields(decoded, {"checksum", "payload"}, "envelope")
    checksum = _require_sha256(envelope["checksum"], "checksum")
    payload_value = envelope["payload"]
    if not isinstance(payload_value, dict):
        raise DomainPersistenceError("payload는 JSON 객체여야 합니다.")
    if _content_checksum(payload_value) != checksum:
        raise DomainChecksumError("영속 payload content checksum이 일치하지 않습니다.")
    return _payload_from_dict(payload_value)


def source_descriptor_from_payload(
    payload: DomainPersistencePayload,
) -> SourceDescriptor:
    """검증된 payload에서 SourceDescriptor를 안전하게 재구성합니다."""

    validate_domain_payload(payload)
    source = payload.source
    return SourceDescriptor(
        dataset_root=Path(source.dataset_root),
        sparse_root=Path(source.sparse_root),
        model_directory=Path(source.model_directory),
        images_directory=Path(source.images_directory),
        input_format=source.input_format,
        layout=source.layout,
        selected_files=tuple(Path(path) for path in source.selected_files),
        source_fingerprint=source.source_fingerprint,
        dataset_uuid=payload.header.dataset_uuid,
        schema_version=source.schema_version,
    )


def operations_from_payload(
    payload: DomainPersistencePayload,
) -> tuple[AnyEditOperation, ...]:
    """검증된 payload에서 확정 EditOperation tuple을 반환합니다."""

    validate_domain_payload(payload)
    return payload.operations


def compare_domain_generations(
    left: DomainPersistencePayload,
    right: DomainPersistencePayload,
) -> int:
    """같은 도메인 payload generation을 -1·0·1로 비교합니다."""

    validate_domain_payload(left)
    validate_domain_payload(right)
    if left.header.domain_uuid != right.header.domain_uuid:
        raise DomainSourceIdentityError("서로 다른 domain_uuid의 generation은 비교할 수 없습니다.")
    if left.header.dataset_uuid != right.header.dataset_uuid:
        raise DomainSourceIdentityError(
            "같은 도메인의 payload가 서로 다른 dataset_uuid를 가리킵니다."
        )
    if left.header.domain_generation == right.header.domain_generation:
        if left != right:
            raise DomainPersistenceError(
                "같은 domain_generation에 서로 다른 payload가 존재합니다."
            )
        return 0
    return -1 if left.header.domain_generation < right.header.domain_generation else 1


__all__ = [
    "DomainChecksumError",
    "DomainPersistenceError",
    "DomainPersistenceHeader",
    "DomainPersistencePayload",
    "DomainSourceIdentityError",
    "DomainSourceMetadata",
    "PERSISTENCE_SCHEMA_VERSION",
    "SOURCE_SCHEMA_VERSION",
    "UnsupportedDomainSchemaError",
    "compare_domain_generations",
    "decode_domain_payload",
    "encode_domain_payload",
    "make_domain_payload",
    "operations_from_payload",
    "source_descriptor_from_payload",
    "validate_domain_payload",
]
