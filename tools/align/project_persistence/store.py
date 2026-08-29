"""Blender Text 데이터블록에 프로젝트 상태를 안전하게 보관합니다."""

from __future__ import annotations

from typing import Any

import bpy

from ..constants import (
    PROJECT_PERSISTENCE_SCHEMA_VERSION,
    PROJECT_TEXT_OWNER_KEY,
    PROJECT_TEXT_OWNER_VALUE,
    PROJECT_TEXT_SCHEMA_KEY,
    PROJECT_TEXT_STATE_ACTIVE,
    PROJECT_TEXT_STATE_KEY,
    PROJECT_TEXT_STATE_RETIRED,
    PROJECT_TEXT_STATE_STAGING,
    PROJECT_TEXT_UUID_KEY,
)
from .codec import (
    ProjectPersistenceCodecError,
    decode_project_state,
    encode_project_state,
    project_state_schema_version,
)
from .types import ProjectStateSnapshot


class ProjectTextStoreError(RuntimeError):
    """Text 소유권 확인 또는 원자 교체가 실패할 때 사용자에게 전달합니다."""


def _owned_project_state(text: Any, state: str | None = None) -> bool:
    """Text의 명시적 owner·state 표식이 프로젝트 상태 계약과 일치하는지 확인합니다."""

    if text is None or text.get(PROJECT_TEXT_OWNER_KEY) != PROJECT_TEXT_OWNER_VALUE:
        return False
    return state is None or text.get(PROJECT_TEXT_STATE_KEY) == state


def active_project_text() -> Any | None:
    """정확히 하나의 ACTIVE owner Text만 반환하고 충돌은 자동 선택하지 않습니다."""

    matches = [
        text
        for text in bpy.data.texts
        if _owned_project_state(text, PROJECT_TEXT_STATE_ACTIVE)
    ]
    if len(matches) > 1:
        raise ProjectTextStoreError("활성 COLMAP Align 프로젝트 Text가 둘 이상입니다. 자동 복원을 중단했습니다.")
    return matches[0] if matches else None


def _set_text_owner(text: Any, state: str, snapshot: ProjectStateSnapshot) -> None:
    """새 Text의 owner·state·schema 식별자를 schema snapshot과 함께 기록합니다."""

    text[PROJECT_TEXT_OWNER_KEY] = PROJECT_TEXT_OWNER_VALUE
    text[PROJECT_TEXT_STATE_KEY] = state
    text[PROJECT_TEXT_UUID_KEY] = snapshot.project_uuid
    text[PROJECT_TEXT_SCHEMA_KEY] = PROJECT_PERSISTENCE_SCHEMA_VERSION
    text.use_fake_user = True


def _verify_staging_text(text: Any, payload: str, snapshot: ProjectStateSnapshot) -> None:
    """승격 전에 Text 원문과 소유 표식이 입력 snapshot과 같은지 검증합니다."""

    if text.as_string() != payload:
        raise ProjectTextStoreError("프로젝트 상태 Text 기록 결과가 입력 JSON과 일치하지 않습니다.")
    if not _owned_project_state(text, PROJECT_TEXT_STATE_STAGING):
        raise ProjectTextStoreError("프로젝트 상태 임시 Text 소유 표식이 올바르지 않습니다.")
    if text.get(PROJECT_TEXT_UUID_KEY) != snapshot.project_uuid:
        raise ProjectTextStoreError("프로젝트 상태 임시 Text UUID가 올바르지 않습니다.")
    if text.get(PROJECT_TEXT_SCHEMA_KEY) != PROJECT_PERSISTENCE_SCHEMA_VERSION:
        raise ProjectTextStoreError("프로젝트 상태 임시 Text schema 표식이 올바르지 않습니다.")


def write_active_project_state(snapshot: ProjectStateSnapshot) -> Any:
    """STAGING Text 작성 뒤 ACTIVE owner를 원자적으로 교체해 이전 상태를 보존합니다."""

    try:
        payload = encode_project_state(snapshot)
    except ProjectPersistenceCodecError as error:
        raise ProjectTextStoreError(str(error)) from error
    previous = active_project_text()
    staging = None
    previous_state = None
    promoted = False
    try:
        staging = bpy.data.texts.new(f".colmap_align_project_{snapshot.project_uuid}")
        _set_text_owner(staging, PROJECT_TEXT_STATE_STAGING, snapshot)
        staging.write(payload)
        _verify_staging_text(staging, payload, snapshot)
        if previous is not None:
            previous_state = previous.get(PROJECT_TEXT_STATE_KEY)
            previous[PROJECT_TEXT_STATE_KEY] = PROJECT_TEXT_STATE_RETIRED
        staging[PROJECT_TEXT_STATE_KEY] = PROJECT_TEXT_STATE_ACTIVE
        promoted = True
        if previous is not None:
            try:
                bpy.data.texts.remove(previous)
            except (AttributeError, ReferenceError, RuntimeError):
                # ACTIVE 승격은 끝났으므로 이전 소유 Text를 RETIRED로 남겨 복구 가능성을 유지합니다.
                pass
        return staging
    except Exception as error:
        if previous is not None and previous_state is not None:
            try:
                previous[PROJECT_TEXT_STATE_KEY] = previous_state
            except (AttributeError, ReferenceError, RuntimeError):
                pass
        if staging is not None and not promoted:
            try:
                bpy.data.texts.remove(staging)
            except (AttributeError, ReferenceError, RuntimeError):
                pass
        if isinstance(error, ProjectTextStoreError):
            raise
        raise ProjectTextStoreError("프로젝트 상태 Text를 안전하게 교체하지 못했습니다.") from error


def read_active_project_state() -> ProjectStateSnapshot | None:
    """유일한 ACTIVE Text의 원본 schema·owner UUID를 migration snapshot과 교차 검증합니다."""

    text = active_project_text()
    if text is None:
        return None
    try:
        payload = text.as_string()
        stored_schema_version = project_state_schema_version(payload)
        snapshot = decode_project_state(payload)
    except ProjectPersistenceCodecError as error:
        raise ProjectTextStoreError(str(error)) from error
    if text.get(PROJECT_TEXT_UUID_KEY) != snapshot.project_uuid:
        raise ProjectTextStoreError("프로젝트 Text owner UUID와 JSON project_uuid가 다릅니다.")
    if text.get(PROJECT_TEXT_SCHEMA_KEY) != stored_schema_version:
        raise ProjectTextStoreError("프로젝트 Text owner schema와 JSON schema가 다릅니다.")
    return snapshot


def remove_active_project_state() -> bool:
    """마지막 모델 삭제 때 현재 ACTIVE 애드온 소유 Text만 제거합니다."""

    text = active_project_text()
    if text is None:
        return False
    bpy.data.texts.remove(text)
    return True


def retire_active_project_state() -> bool:
    """사용자가 새 프로젝트를 명시했을 때 ACTIVE Text만 RETIRED 상태로 바꿉니다."""

    text = active_project_text()
    if text is None:
        return False
    text[PROJECT_TEXT_STATE_KEY] = PROJECT_TEXT_STATE_RETIRED
    return True


__all__ = (
    "ProjectTextStoreError",
    "active_project_text",
    "read_active_project_state",
    "remove_active_project_state",
    "retire_active_project_state",
    "write_active_project_state",
)
