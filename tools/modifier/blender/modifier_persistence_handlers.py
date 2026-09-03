"""Modifier 독립 Domain의 작은 저장 Payload와 Blender 저장 생명주기를 연결합니다."""

from __future__ import annotations

import json
from typing import Any

import bpy
from bpy.app.handlers import persistent

from ..core.domain_persistence import (
    make_domain_payload,
    source_descriptor_from_payload,
)
from ..core.edits import serialize_history
from .modifier_project_store import (
    ModifierProjectStoreError,
    clear_staging_payload,
    read_active_payload,
    write_active_payload,
)
from .modifier_runtime_domain import domain_by_uuid, domain_for_scene
from .runtime_state import write_domain_settings


_PENDING_SAVE_TOKEN: tuple[str, int] | None = None


def _active_scene() -> Any | None:
    """현재 Blender Context의 활성 Scene을 예외 없이 반환합니다."""

    return getattr(bpy.context, "scene", None)


def _set_persistence_error(scene: Any, message: str) -> None:
    """저장·복원 오류를 비권위 Scene Mirror와 상태창에 함께 기록합니다."""

    settings = getattr(scene, "colmap_import", None)
    if settings is None:
        return
    settings.domain_persistence_dirty = True
    settings.domain_restore_error = str(message)
    settings.status_kind = "ERROR"
    settings.status = str(message)


@persistent
def save_modifier_domain_pre(*_args) -> None:
    """Blender 저장 직전에 최신 Runtime Domain을 ACTIVE Text로 원자 승격합니다."""

    global _PENDING_SAVE_TOKEN
    _PENDING_SAVE_TOKEN = None
    scene = _active_scene()
    if scene is None:
        return
    domain = domain_for_scene(scene)
    if domain is None:
        return
    try:
        domain.require_editable()
        state = domain.dataset_state
        payload = make_domain_payload(
            state.source,
            state.operations,
            domain_uuid=domain.domain_uuid,
            domain_generation=domain.domain_generation,
            edit_revision=state.edit_revision,
            history_hash_value=state.history_hash,
        )
        promoted = write_active_payload(payload)
        if (
            promoted.header.domain_uuid != domain.domain_uuid
            or promoted.header.domain_generation != domain.domain_generation
        ):
            raise ModifierProjectStoreError(
                "저장된 ACTIVE Payload가 현재 Modifier Domain과 다릅니다."
            )
        clear_staging_payload()
        _PENDING_SAVE_TOKEN = (
            domain.domain_uuid,
            domain.domain_generation,
        )
        write_domain_settings(scene.colmap_import, domain)
    except Exception as exc:
        _set_persistence_error(
            scene,
            f"Modifier 작업 이력을 저장하지 못했습니다: {exc}",
        )
        raise


@persistent
def save_modifier_domain_post(*_args) -> None:
    """저장 완료 뒤 같은 ACTIVE 세대일 때만 Runtime Dirty 상태를 해제합니다."""

    global _PENDING_SAVE_TOKEN
    token = _PENDING_SAVE_TOKEN
    _PENDING_SAVE_TOKEN = None
    if token is None:
        return
    scene = _active_scene()
    if scene is None:
        return
    domain_uuid, generation = token
    domain = domain_by_uuid(domain_uuid)
    try:
        payload = read_active_payload()
        if payload is None:
            raise ModifierProjectStoreError("저장 완료 뒤 ACTIVE Payload가 없습니다.")
        if (
            payload.header.domain_uuid != domain_uuid
            or payload.header.domain_generation != generation
        ):
            raise ModifierProjectStoreError(
                "저장 완료 뒤 ACTIVE Payload 세대가 저장 시작 token과 다릅니다."
            )
        if domain is None:
            raise ModifierProjectStoreError(
                "저장 완료를 반영할 Modifier Runtime Domain이 없습니다."
            )
        domain.mark_saved(generation)
        write_domain_settings(scene.colmap_import, domain)
        clear_staging_payload()
    except Exception as exc:
        if domain is not None:
            domain.persistence_dirty = True
        _set_persistence_error(
            scene,
            f"Modifier 저장 완료 상태를 검증하지 못했습니다: {exc}",
        )


@persistent
def save_modifier_domain_failed(*_args) -> None:
    """Blender 저장 실패 시 Runtime Dirty를 유지하고 STAGING만 정리합니다."""

    global _PENDING_SAVE_TOKEN
    token = _PENDING_SAVE_TOKEN
    _PENDING_SAVE_TOKEN = None
    scene = _active_scene()
    if scene is None:
        return
    domain = domain_by_uuid(token[0]) if token is not None else domain_for_scene(scene)
    if domain is not None:
        domain.persistence_dirty = True
        write_domain_settings(scene.colmap_import, domain)
    try:
        clear_staging_payload()
    except ModifierProjectStoreError:
        pass
    _set_persistence_error(
        scene,
        "Blender 파일 저장이 실패하여 Modifier 작업 이력은 저장 필요 상태입니다.",
    )


def restore_domain_mirror_from_active(scene: Any) -> bool:
    """검증된 ACTIVE Payload를 자동 Source 복원용 Scene Mirror로만 전개합니다."""

    settings = getattr(scene, "colmap_import", None)
    if settings is None:
        return False
    try:
        payload = read_active_payload()
    except Exception as exc:
        _set_persistence_error(
            scene,
            f"저장된 Modifier 작업 이력이 손상되었습니다: {exc}",
        )
        return False
    if payload is None:
        if str(getattr(settings, "dataset_uuid", "") or ""):
            _set_persistence_error(
                scene,
                "새 Modifier 저장 구조의 작업 이력이 없습니다. Dataset을 다시 Import하십시오.",
            )
        return False
    try:
        source = source_descriptor_from_payload(payload)
        settings.dataset_root = str(source.dataset_root)
        settings.sparse_directory = str(source.model_directory)
        settings.images_directory = str(source.images_directory)
        settings.active_dataset_root = str(source.dataset_root)
        settings.active_sparse_root = str(source.sparse_root)
        settings.active_model_directory = str(source.model_directory)
        settings.active_images_directory = str(source.images_directory)
        settings.active_input_format = str(source.input_format)
        settings.active_layout = str(source.layout)
        settings.selected_files_json = json.dumps(
            [str(path) for path in source.selected_files],
            ensure_ascii=False,
        )
        settings.source_fingerprint = str(source.source_fingerprint)
        settings.dataset_uuid = str(source.dataset_uuid)
        settings.schema_version = str(source.schema_version)
        settings.edit_history_json = serialize_history(payload.operations)
        settings.edit_revision = int(payload.header.edit_revision)
        settings.history_hash = str(payload.header.history_hash)
        settings.last_operation_id = (
            payload.operations[-1].operation_id if payload.operations else ""
        )
        settings.domain_uuid = str(payload.header.domain_uuid)
        settings.domain_generation = int(payload.header.domain_generation)
        settings.domain_saved_generation = int(payload.header.domain_generation)
        settings.domain_persistence_dirty = False
        settings.domain_restore_error = ""
        settings.status_kind = "IMPORTING"
        settings.status = "저장된 Modifier Runtime을 자동 복원하는 중입니다."
        return True
    except Exception as exc:
        _set_persistence_error(
            scene,
            f"저장된 Modifier 작업 이력을 복원 준비하지 못했습니다: {exc}",
        )
        return False


def register_modifier_persistence_handlers() -> None:
    """Modifier 저장 전·후·실패 Handler를 중복 없이 등록합니다."""

    if save_modifier_domain_pre not in bpy.app.handlers.save_pre:
        bpy.app.handlers.save_pre.append(save_modifier_domain_pre)
    if save_modifier_domain_post not in bpy.app.handlers.save_post:
        bpy.app.handlers.save_post.append(save_modifier_domain_post)
    failed_handlers = getattr(bpy.app.handlers, "save_post_fail", None)
    if failed_handlers is not None and save_modifier_domain_failed not in failed_handlers:
        failed_handlers.append(save_modifier_domain_failed)


def unregister_modifier_persistence_handlers() -> None:
    """Modifier 저장 Handler와 대기 중인 저장 token을 정리합니다."""

    global _PENDING_SAVE_TOKEN
    for handlers, callback in (
        (bpy.app.handlers.save_pre, save_modifier_domain_pre),
        (bpy.app.handlers.save_post, save_modifier_domain_post),
    ):
        if callback in handlers:
            handlers.remove(callback)
    failed_handlers = getattr(bpy.app.handlers, "save_post_fail", None)
    if failed_handlers is not None and save_modifier_domain_failed in failed_handlers:
        failed_handlers.remove(save_modifier_domain_failed)
    _PENDING_SAVE_TOKEN = None


__all__ = [
    "register_modifier_persistence_handlers",
    "restore_domain_mirror_from_active",
    "save_modifier_domain_failed",
    "save_modifier_domain_post",
    "save_modifier_domain_pre",
    "unregister_modifier_persistence_handlers",
]
