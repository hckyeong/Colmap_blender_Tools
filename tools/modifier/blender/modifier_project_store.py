"""Modifier 도메인 payload를 Blender Text의 고정 2-slot에 저장합니다."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..core.domain_persistence import (
    PERSISTENCE_SCHEMA_VERSION,
    DomainPersistenceError,
    DomainPersistencePayload,
    compare_domain_generations,
    decode_domain_payload,
    encode_domain_payload,
)


ACTIVE_TEXT_NAME = ".colmap_modifier_domain_active"
STAGING_TEXT_NAME = ".colmap_modifier_domain_staging"
TEXT_OWNER_KEY = "colmap_modifier_domain_owner"
TEXT_OWNER_VALUE = "COLMAP_MODIFIER_DOMAIN"
TEXT_SCHEMA_KEY = "colmap_modifier_domain_schema"


class ModifierProjectStoreError(RuntimeError):
    """Text 소유권·본문 검증 또는 slot 교체 실패를 나타냅니다."""


class ModifierProjectStoreRollbackError(ModifierProjectStoreError):
    """ACTIVE 본문 교체 실패 뒤 이전 상태 복구 실패를 나타냅니다."""


@dataclass(frozen=True, slots=True)
class _TextBackup:
    """ACTIVE Text를 본문과 소유 marker까지 원래대로 복구할 값입니다."""

    body: str
    owner: str
    schema_version: int
    use_fake_user: bool | None


def _resolve_texts(texts: Any | None) -> Any:
    """주입된 Text 컬렉션 또는 현재 Blender Text 컬렉션을 반환합니다."""

    if texts is not None:
        return texts
    try:
        import bpy
    except ImportError as exc:
        raise ModifierProjectStoreError(
            "Blender 밖에서는 테스트용 Text 컬렉션을 주입해야 합니다."
        ) from exc
    return bpy.data.texts


def _marker_value(text: Any, key: str) -> Any:
    """Blender ID custom property 값을 안전하게 읽습니다."""

    try:
        return text.get(key)
    except (AttributeError, ReferenceError, RuntimeError, TypeError) as exc:
        raise ModifierProjectStoreError("Modifier Domain Text marker를 읽지 못했습니다.") from exc


def _require_owned_slot(text: Any, slot_name: str) -> None:
    """고정 이름 Text의 owner와 schema marker를 엄격히 검증합니다."""

    if text is None:
        raise ModifierProjectStoreError(f"{slot_name} Text가 존재하지 않습니다.")
    if str(getattr(text, "name", "")) != slot_name:
        raise ModifierProjectStoreError(f"{slot_name} Text 이름이 고정 slot과 다릅니다.")
    if _marker_value(text, TEXT_OWNER_KEY) != TEXT_OWNER_VALUE:
        raise ModifierProjectStoreError(
            f"{slot_name} Text가 Modifier Domain 소유가 아닙니다."
        )
    schema_version = _marker_value(text, TEXT_SCHEMA_KEY)
    if type(schema_version) is not int or schema_version != PERSISTENCE_SCHEMA_VERSION:
        raise ModifierProjectStoreError(
            f"{slot_name} Text schema marker를 지원하지 않습니다."
        )


def _tag_slot(text: Any) -> None:
    """Text에 Modifier Domain owner·schema marker와 fake user를 기록합니다."""

    try:
        text[TEXT_OWNER_KEY] = TEXT_OWNER_VALUE
        text[TEXT_SCHEMA_KEY] = PERSISTENCE_SCHEMA_VERSION
        if hasattr(text, "use_fake_user"):
            text.use_fake_user = True
    except (AttributeError, ReferenceError, RuntimeError, TypeError) as exc:
        raise ModifierProjectStoreError("Modifier Domain Text marker를 기록하지 못했습니다.") from exc


def _text_body(text: Any, slot_name: str) -> str:
    """Text 본문을 문자열로 읽고 유효하지 않은 Blender 참조를 거부합니다."""

    try:
        body = text.as_string()
    except (AttributeError, ReferenceError, RuntimeError, TypeError) as exc:
        raise ModifierProjectStoreError(f"{slot_name} Text 본문을 읽지 못했습니다.") from exc
    if not isinstance(body, str):
        raise ModifierProjectStoreError(f"{slot_name} Text 본문이 문자열이 아닙니다.")
    return body


def _replace_text_body(text: Any, body: str, slot_name: str) -> None:
    """Text 본문을 clear·write 순서로 완전히 교체합니다."""

    try:
        text.clear()
        text.write(body)
    except (AttributeError, ReferenceError, RuntimeError, TypeError) as exc:
        raise ModifierProjectStoreError(f"{slot_name} Text 본문을 기록하지 못했습니다.") from exc


def _decode_slot(
    text: Any,
    slot_name: str,
    *,
    expected_body: str | None = None,
) -> DomainPersistencePayload:
    """Text marker·원문·checksum·payload schema를 한 경계에서 검증합니다."""

    _require_owned_slot(text, slot_name)
    body = _text_body(text, slot_name)
    if expected_body is not None and body != expected_body:
        raise ModifierProjectStoreError(
            f"{slot_name} Text 기록 결과가 canonical payload와 다릅니다."
        )
    try:
        payload = decode_domain_payload(body)
    except DomainPersistenceError as exc:
        raise ModifierProjectStoreError(
            f"{slot_name} Text payload 검증에 실패했습니다: {exc}"
        ) from exc
    if payload.header.schema_version != _marker_value(text, TEXT_SCHEMA_KEY):
        raise ModifierProjectStoreError(
            f"{slot_name} Text schema marker와 payload schema가 다릅니다."
        )
    return payload


def _require_same_payload(
    expected: DomainPersistencePayload,
    actual: DomainPersistencePayload,
    slot_name: str,
) -> None:
    """기록 후 payload가 입력과 같은 도메인·세대·내용인지 검증합니다."""

    try:
        comparison = compare_domain_generations(actual, expected)
    except DomainPersistenceError as exc:
        raise ModifierProjectStoreError(
            f"{slot_name} Text payload 식별자가 입력과 다릅니다: {exc}"
        ) from exc
    if comparison != 0:
        raise ModifierProjectStoreError(
            f"{slot_name} Text payload generation이 입력과 다릅니다."
        )


def _require_non_regressing_generation(
    current: DomainPersistencePayload,
    replacement: DomainPersistencePayload,
) -> None:
    """같은 도메인에서 이전 generation으로 되돌아가는 저장을 거부합니다."""

    if current.header.domain_uuid != replacement.header.domain_uuid:
        return
    try:
        comparison = compare_domain_generations(current, replacement)
    except DomainPersistenceError as exc:
        raise ModifierProjectStoreError(
            f"ACTIVE payload와 새 payload의 도메인 식별자가 일치하지 않습니다: {exc}"
        ) from exc
    if comparison > 0:
        raise ModifierProjectStoreError(
            "현재 ACTIVE보다 낮은 domain_generation은 저장할 수 없습니다."
        )


def _new_owned_slot(texts: Any, slot_name: str) -> Any:
    """고정 이름의 새 Text를 만들고 소유 marker를 기록합니다."""

    try:
        text = texts.new(slot_name)
    except (AttributeError, ReferenceError, RuntimeError, TypeError) as exc:
        raise ModifierProjectStoreError(f"{slot_name} Text를 생성하지 못했습니다.") from exc
    try:
        if str(getattr(text, "name", "")) != slot_name:
            raise ModifierProjectStoreError(
                f"{slot_name} 고정 이름으로 Text를 생성하지 못했습니다."
            )
        _tag_slot(text)
        return text
    except Exception:
        try:
            texts.remove(text)
        except (AttributeError, ReferenceError, RuntimeError, TypeError):
            pass
        raise


def _slot_for_write(texts: Any, slot_name: str) -> tuple[Any, bool]:
    """기존 소유 slot 또는 새로 생성한 고정 이름 slot을 반환합니다."""

    try:
        text = texts.get(slot_name)
    except (AttributeError, ReferenceError, RuntimeError, TypeError) as exc:
        raise ModifierProjectStoreError("Blender Text 컬렉션을 조회하지 못했습니다.") from exc
    if text is not None:
        _require_owned_slot(text, slot_name)
        return text, False
    return _new_owned_slot(texts, slot_name), True


def _remove_slot(texts: Any, text: Any, slot_name: str) -> None:
    """새로 만든 slot Text를 컬렉션에서 완전히 제거합니다."""

    try:
        texts.remove(text)
    except (AttributeError, ReferenceError, RuntimeError, TypeError) as exc:
        raise ModifierProjectStoreRollbackError(
            f"실패한 {slot_name} Text를 제거하지 못했습니다."
        ) from exc


def _backup_active(text: Any) -> _TextBackup:
    """검증된 ACTIVE Text의 본문·marker·fake user 상태를 복사합니다."""

    _require_owned_slot(text, ACTIVE_TEXT_NAME)
    return _TextBackup(
        body=_text_body(text, ACTIVE_TEXT_NAME),
        owner=str(_marker_value(text, TEXT_OWNER_KEY)),
        schema_version=int(_marker_value(text, TEXT_SCHEMA_KEY)),
        use_fake_user=(
            bool(text.use_fake_user) if hasattr(text, "use_fake_user") else None
        ),
    )


def _restore_active(text: Any, backup: _TextBackup) -> None:
    """교체 실패한 ACTIVE Text를 이전의 검증된 본문과 marker로 복구합니다."""

    try:
        text.clear()
        text.write(backup.body)
        text[TEXT_OWNER_KEY] = backup.owner
        text[TEXT_SCHEMA_KEY] = backup.schema_version
        if backup.use_fake_user is not None and hasattr(text, "use_fake_user"):
            text.use_fake_user = backup.use_fake_user
    except (AttributeError, ReferenceError, RuntimeError, TypeError) as exc:
        raise ModifierProjectStoreRollbackError(
            "이전 ACTIVE Modifier Domain Text를 복구하지 못했습니다."
        ) from exc
    try:
        restored = _decode_slot(text, ACTIVE_TEXT_NAME, expected_body=backup.body)
    except ModifierProjectStoreError as exc:
        raise ModifierProjectStoreRollbackError(
            "복구한 ACTIVE Modifier Domain Text를 재검증하지 못했습니다."
        ) from exc
    if restored.header.schema_version != backup.schema_version:
        raise ModifierProjectStoreRollbackError(
            "복구한 ACTIVE Text schema가 이전 marker와 다릅니다."
        )


def _cleanup_failed_staging(texts: Any, text: Any, created: bool) -> None:
    """STAGING 기록 실패 뒤 불완전한 본문 또는 새 Text를 제거합니다."""

    if created:
        _remove_slot(texts, text, STAGING_TEXT_NAME)
        return
    try:
        text.clear()
    except (AttributeError, ReferenceError, RuntimeError, TypeError) as exc:
        raise ModifierProjectStoreRollbackError(
            "실패한 STAGING Modifier Domain Text를 비우지 못했습니다."
        ) from exc


def write_active_payload(
    payload: DomainPersistencePayload,
    texts: Any | None = None,
) -> DomainPersistencePayload:
    """STAGING 검증 뒤 ACTIVE에 승격하고 실패 시 이전 ACTIVE를 복구합니다."""

    try:
        encoded = encode_domain_payload(payload)
    except DomainPersistenceError as exc:
        raise ModifierProjectStoreError(f"저장할 Modifier Domain payload가 유효하지 않습니다: {exc}") from exc
    collection = _resolve_texts(texts)

    try:
        previous_active = collection.get(ACTIVE_TEXT_NAME)
    except (AttributeError, ReferenceError, RuntimeError, TypeError) as exc:
        raise ModifierProjectStoreError("Blender Text 컬렉션을 조회하지 못했습니다.") from exc
    active_backup: _TextBackup | None = None
    if previous_active is not None:
        current_payload = _decode_slot(previous_active, ACTIVE_TEXT_NAME)
        _require_non_regressing_generation(current_payload, payload)
        active_backup = _backup_active(previous_active)

    staging, staging_created = _slot_for_write(collection, STAGING_TEXT_NAME)
    try:
        _replace_text_body(staging, encoded, STAGING_TEXT_NAME)
        _tag_slot(staging)
        staged_payload = _decode_slot(
            staging,
            STAGING_TEXT_NAME,
            expected_body=encoded,
        )
        _require_same_payload(payload, staged_payload, STAGING_TEXT_NAME)
    except Exception as exc:
        try:
            _cleanup_failed_staging(collection, staging, staging_created)
        except ModifierProjectStoreRollbackError as cleanup_error:
            raise cleanup_error from exc
        if isinstance(exc, ModifierProjectStoreError):
            raise
        raise ModifierProjectStoreError("STAGING Modifier Domain Text 기록에 실패했습니다.") from exc

    active = previous_active
    active_created = False
    if active is None:
        active, active_created = _slot_for_write(collection, ACTIVE_TEXT_NAME)
    try:
        _replace_text_body(active, encoded, ACTIVE_TEXT_NAME)
        _tag_slot(active)
        promoted_payload = _decode_slot(
            active,
            ACTIVE_TEXT_NAME,
            expected_body=encoded,
        )
        _require_same_payload(payload, promoted_payload, ACTIVE_TEXT_NAME)
        return promoted_payload
    except Exception as exc:
        try:
            if active_created:
                _remove_slot(collection, active, ACTIVE_TEXT_NAME)
            elif active_backup is not None:
                _restore_active(active, active_backup)
        except ModifierProjectStoreRollbackError as rollback_error:
            raise rollback_error from exc
        if isinstance(exc, ModifierProjectStoreError):
            raise
        raise ModifierProjectStoreError("ACTIVE Modifier Domain Text 승격에 실패했습니다.") from exc


def read_active_payload(texts: Any | None = None) -> DomainPersistencePayload | None:
    """ACTIVE 고정 slot만 권위 후보로 읽고 STAGING은 자동 승격하지 않습니다."""

    collection = _resolve_texts(texts)
    try:
        active = collection.get(ACTIVE_TEXT_NAME)
    except (AttributeError, ReferenceError, RuntimeError, TypeError) as exc:
        raise ModifierProjectStoreError("Blender Text 컬렉션을 조회하지 못했습니다.") from exc
    if active is None:
        return None
    return _decode_slot(active, ACTIVE_TEXT_NAME)


def clear_staging_payload(texts: Any | None = None) -> bool:
    """소유권이 확인된 STAGING 본문만 비우고 ACTIVE는 변경하지 않습니다."""

    collection = _resolve_texts(texts)
    try:
        staging = collection.get(STAGING_TEXT_NAME)
    except (AttributeError, ReferenceError, RuntimeError, TypeError) as exc:
        raise ModifierProjectStoreError("Blender Text 컬렉션을 조회하지 못했습니다.") from exc
    if staging is None:
        return False
    _require_owned_slot(staging, STAGING_TEXT_NAME)
    try:
        staging.clear()
    except (AttributeError, ReferenceError, RuntimeError, TypeError) as exc:
        raise ModifierProjectStoreError("STAGING Modifier Domain Text를 비우지 못했습니다.") from exc
    return True


__all__ = [
    "ACTIVE_TEXT_NAME",
    "ModifierProjectStoreError",
    "ModifierProjectStoreRollbackError",
    "STAGING_TEXT_NAME",
    "TEXT_OWNER_KEY",
    "TEXT_OWNER_VALUE",
    "TEXT_SCHEMA_KEY",
    "clear_staging_payload",
    "read_active_payload",
    "write_active_payload",
]
