"""Camera Review 배경 이미지 소유권과 최대 3장 VRAM 작업 집합을 관리합니다."""

from __future__ import annotations

from pathlib import Path

import bpy

from ..core.image_path_resolution import EXACT, FALLBACK
from .ownership import (
    DATASET_UUID_KEY,
    OWNER_KEY,
    OWNER_TOKEN,
    RECORD_KIND_KEY,
    SOURCE_FINGERPRINT_KEY,
)


REVIEW_IMAGE_KIND = "REVIEW_IMAGE"
REVIEW_IMAGE_ID_KEY = "colmap_review_image_id"
REVIEW_SOURCE_PATH_KEY = "colmap_review_source_path"
REVIEW_PREVIEW_SCALE_KEY = "colmap_review_preview_scale"
_OWNED_BACKGROUND_SLOT_KEYS: set[tuple[int, int]] = set()


def _is_managed_review_image(
    image,
    dataset_uuid: str | None = None,
) -> bool:
    if (
        image is None
        or image.get(OWNER_KEY) != OWNER_TOKEN
        or image.get(RECORD_KIND_KEY) != REVIEW_IMAGE_KIND
    ):
        return False
    return (
        dataset_uuid is None
        or image.get(DATASET_UUID_KEY) == dataset_uuid
    )


def managed_review_images(dataset_uuid: str | None = None) -> tuple[bpy.types.Image, ...]:
    """소유권 token이 일치하는 Review Image 데이터블록만 반환합니다."""
    return tuple(
        image
        for image in bpy.data.images
        if _is_managed_review_image(image, dataset_uuid)
    )


def _normalized_source_path(path: Path) -> str:
    return str(path.expanduser().resolve())


def _matching_managed_image(session, entry):
    if entry.resolved_image_path is None:
        return None
    source_path = _normalized_source_path(entry.resolved_image_path)
    for image in managed_review_images(session.dataset_uuid):
        if (
            image.get(REVIEW_IMAGE_ID_KEY) == str(entry.image_id)
            and image.get(REVIEW_SOURCE_PATH_KEY) == source_path
            and image.get(SOURCE_FINGERPRINT_KEY) == session.source_fingerprint
            and image.get(REVIEW_PREVIEW_SCALE_KEY) == 1
        ):
            return image
    return None


def _load_image_datablock(path: Path):
    """사용자 Image와 분리된 애드온 소유 데이터블록을 새로 엽니다."""
    return bpy.data.images.load(str(path), check_existing=False)


def _free_image_gpu(image) -> None:
    """테스트에서 대체할 수 있는 Image.gl_free 경계입니다."""
    image.gl_free()


def ensure_managed_review_image(session, entry):
    """같은 Dataset/IMAGE_ID/경로 Image를 재사용하고 없을 때만 별도로 엽니다."""
    if (
        not entry.exact_projection_supported
        or entry.path_status not in {EXACT, FALLBACK}
        or entry.resolved_image_path is None
    ):
        return None
    existing = _matching_managed_image(session, entry)
    if existing is not None:
        return existing

    image = _load_image_datablock(entry.resolved_image_path)
    image[OWNER_KEY] = OWNER_TOKEN
    image[RECORD_KIND_KEY] = REVIEW_IMAGE_KIND
    image[DATASET_UUID_KEY] = session.dataset_uuid
    image[SOURCE_FINGERPRINT_KEY] = session.source_fingerprint
    image[REVIEW_IMAGE_ID_KEY] = str(entry.image_id)
    image[REVIEW_SOURCE_PATH_KEY] = _normalized_source_path(
        entry.resolved_image_path
    )
    image[REVIEW_PREVIEW_SCALE_KEY] = 1
    return image


def _slot_pointer(slot) -> int:
    try:
        return int(slot.as_pointer())
    except (AttributeError, ReferenceError, RuntimeError, TypeError, ValueError):
        return 0


def _slot_key(camera_data, slot) -> tuple[int, int]:
    return camera_data.as_pointer(), _slot_pointer(slot)


def _tracked_slot(session, image_id: int, camera_data):
    """세션이 직접 생성하거나 안전하게 채택한 정확한 slot만 반환합니다."""
    tracked = session.background_slots.get(image_id)
    tracked_pointer = _slot_pointer(tracked)
    if not tracked_pointer:
        session.background_slots.pop(image_id, None)
        return None
    for slot in camera_data.background_images:
        if _slot_pointer(slot) == tracked_pointer:
            return slot
    session.background_slots.pop(image_id, None)
    return None


def _candidate_slots(camera_data, image) -> tuple[object, ...]:
    return tuple(
        slot
        for slot in camera_data.background_images
        if slot.image is image
    )


def _protect_slot(session, camera_data, slot) -> None:
    pointer = _slot_pointer(slot)
    if pointer:
        session.protected_background_slot_pointers.add(
            (camera_data.as_pointer(), pointer)
        )


def _has_untracked_background_reference(session, image) -> bool:
    tracked_pointers = {
        _slot_pointer(slot)
        for slot in session.background_slots.values()
        if _slot_pointer(slot)
    }
    for camera_data in bpy.data.cameras:
        for slot in camera_data.background_images:
            if slot.image is image and _slot_pointer(slot) not in tracked_pointers:
                _protect_slot(session, camera_data, slot)
                return True
    return False


def _restore_camera_background_visibility(session, camera_data) -> None:
    pointer = camera_data.as_pointer()
    original = session.camera_background_visibility.pop(pointer, None)
    # Review가 적용한 True가 그대로일 때만 원래 값을 복원합니다.
    if original is not None and camera_data.show_background_images:
        camera_data.show_background_images = original


def _remove_tracked_slot_for_image(session, image_id: int, image) -> None:
    """세션이 기록한 정확한 slot만 숨기고 제거합니다."""
    camera_object = session.camera_objects.get(image_id)
    if camera_object is None:
        session.background_slots.pop(image_id, None)
        return
    camera_data = camera_object.data
    slot = _tracked_slot(session, image_id, camera_data)
    if slot is None:
        _restore_camera_background_visibility(session, camera_data)
        return

    session.background_slots.pop(image_id, None)
    _OWNED_BACKGROUND_SLOT_KEYS.discard(_slot_key(camera_data, slot))
    if slot.image is image:
        slot.show_background_image = False
        camera_data.background_images.remove(slot)
    else:
        # 사용자가 애드온 slot의 참조를 바꿨다면 더 이상 소유 slot로 다루지 않습니다.
        _protect_slot(session, camera_data, slot)
    _restore_camera_background_visibility(session, camera_data)


def _connect_entry_background(session, entry, image) -> None:
    camera_object = session.camera_objects[entry.image_id]
    camera_data = camera_object.data
    pointer = camera_data.as_pointer()
    if pointer not in session.camera_background_visibility:
        session.camera_background_visibility[pointer] = bool(
            camera_data.show_background_images
        )

    matching_slot = _tracked_slot(session, entry.image_id, camera_data)
    if matching_slot is not None and matching_slot.image is not image:
        _protect_slot(session, camera_data, matching_slot)
        _OWNED_BACKGROUND_SLOT_KEYS.discard(
            _slot_key(camera_data, matching_slot)
        )
        session.background_slots.pop(entry.image_id, None)
        matching_slot = None

    if matching_slot is None:
        candidates = _candidate_slots(camera_data, image)
        adoptable = tuple(
            slot
            for slot in candidates
            if (
                _slot_key(camera_data, slot) in _OWNED_BACKGROUND_SLOT_KEYS
                and _slot_key(camera_data, slot)
                not in session.protected_background_slot_pointers
            )
        )
        if len(adoptable) == 1:
            matching_slot = adoptable[0]
        else:
            # 여러 후보는 어느 것도 소유 slot로 추론하지 않고 모두 보존합니다.
            for slot in candidates:
                _protect_slot(session, camera_data, slot)
            matching_slot = camera_data.background_images.new()
            matching_slot.image = image
            _OWNED_BACKGROUND_SLOT_KEYS.add(
                _slot_key(camera_data, matching_slot)
            )
        session.background_slots[entry.image_id] = matching_slot

    settings = session.scene.colmap_import
    matching_slot.show_background_image = settings.review_background_enabled
    matching_slot.alpha = settings.review_background_alpha
    matching_slot.display_depth = "FRONT"
    matching_slot.frame_method = "FIT"
    camera_data.show_background_images = True


def update_review_slot_style(
    session,
    *,
    alpha: float,
    enabled: bool,
    context,
) -> None:
    """Camera Review가 소유한 background slot의 표시 상태와 Alpha만 변경합니다."""
    for image_id, camera_object in tuple(session.camera_objects.items()):
        try:
            camera_data = camera_object.data
            slot = _tracked_slot(session, image_id, camera_data)
            if slot is None:
                continue
            slot.alpha = alpha
            slot.display_depth = "FRONT"
            slot.show_background_image = enabled
            camera_data.update_tag()
        except (AttributeError, ReferenceError, RuntimeError, TypeError, ValueError):
            continue

    window_manager = getattr(context, "window_manager", None)
    if window_manager is None:
        return
    for window in window_manager.windows:
        for area in window.screen.areas:
            if area.type == "VIEW_3D":
                area.tag_redraw()


def _desired_entry_indices(session, target_index: int) -> tuple[int, ...]:
    """Pending Camera를 건너뛴 이전·현재·다음 항목의 원본 index를 반환합니다."""
    entries = session.review_index.entries
    pending = session.pending_image_ids
    if entries[target_index].image_id in pending:
        raise ValueError("Pending Camera를 이미지 작업 집합의 현재 항목으로 사용할 수 없습니다.")

    previous = target_index - 1
    while previous >= 0 and entries[previous].image_id in pending:
        previous -= 1
    following = target_index + 1
    while following < len(entries) and entries[following].image_id in pending:
        following += 1
    return tuple(
        index
        for index in (previous, target_index, following)
        if 0 <= index < len(entries)
    )


def evict_review_image(session, image_id: int) -> tuple[str, ...]:
    """Pending Camera의 관리 background를 끊고 가능한 경우 VRAM을 반납합니다."""
    image = session.resident_images.get(image_id)
    if image is None:
        session.working_set_image_ids.discard(image_id)
        return ()
    _remove_tracked_slot_for_image(session, image_id, image)
    if _has_untracked_background_reference(session, image):
        return (
            f"IMAGE_ID {image_id}는 사용자 background 참조가 있어 VRAM 해제를 보류했습니다.",
        )
    try:
        _free_image_gpu(image)
    except Exception as exc:
        return (f"IMAGE_ID {image_id} VRAM 해제 실패: {exc}",)
    session.resident_images.pop(image_id, None)
    session.working_set_image_ids.discard(image_id)
    return ()


def update_working_set(session, target_index: int) -> tuple[str, ...]:
    """target 중심 최대 3장을 준비한 뒤 background와 VRAM 상태를 전환합니다.

    현재 항목 로드가 실패하면 background나 기존 작업 집합을 변경하기 전에
    예외를 전달합니다. 이웃 항목 실패는 경고로만 기록합니다.
    """
    review_index = session.review_index
    if target_index < 0 or target_index >= len(review_index.entries):
        raise ValueError("Camera Review 작업 집합 index가 범위를 벗어났습니다.")

    desired_indices = _desired_entry_indices(session, target_index)
    desired_ids = {
        review_index.entries[index].image_id
        for index in desired_indices
    }
    target_entry = review_index.entries[target_index]
    desired_entries = tuple(
        review_index.entries[index]
        for index in desired_indices
    )
    prepared: dict[int, bpy.types.Image] = {}
    warnings: list[str] = []

    for entry in desired_entries:
        if (
            not entry.exact_projection_supported
            or entry.path_status not in {EXACT, FALLBACK}
        ):
            continue
        try:
            prepared[entry.image_id] = ensure_managed_review_image(session, entry)
        except Exception as exc:
            message = f"IMAGE_ID {entry.image_id} 이미지 로드 실패: {exc}"
            if entry.image_id == target_entry.image_id:
                session.image_warnings = [message]
                session.scene.colmap_import.review_status = message
                session.scene.colmap_import.review_image_status = "LOAD_FAILED"
                raise RuntimeError(message) from exc
            warnings.append(message)

    # 모든 필수 로드가 성공한 뒤에만 관리 background를 바꿉니다.
    for entry in desired_entries:
        image = prepared.get(entry.image_id)
        if image is not None:
            _connect_entry_background(session, entry, image)
            session.resident_images[entry.image_id] = image

    for image_id, image in tuple(session.resident_images.items()):
        if image_id in desired_ids:
            continue
        _remove_tracked_slot_for_image(session, image_id, image)
        if _has_untracked_background_reference(session, image):
            warnings.append(
                f"IMAGE_ID {image_id}는 사용자 background 참조가 있어 "
                "VRAM 해제를 보류했습니다."
            )
            continue
        try:
            _free_image_gpu(image)
        except Exception as exc:
            warnings.append(f"IMAGE_ID {image_id} VRAM 해제 실패: {exc}")
            continue
        session.resident_images.pop(image_id, None)

    session.working_set_image_ids = desired_ids
    session.image_warnings = warnings
    return tuple(warnings)


def cleanup_review_images(session) -> tuple[str, ...]:
    """모든 관리 background 참조를 끊고 resident Image의 VRAM을 반납합니다."""
    warnings: list[str] = []
    for image_id, image in tuple(session.resident_images.items()):
        _remove_tracked_slot_for_image(session, image_id, image)
        if _has_untracked_background_reference(session, image):
            warnings.append(
                f"IMAGE_ID {image_id}는 사용자 background 참조가 있어 "
                "VRAM 해제를 보류했습니다."
            )
            continue
        try:
            _free_image_gpu(image)
        except Exception as exc:
            warnings.append(f"IMAGE_ID {image_id} VRAM 해제 실패: {exc}")
            continue
        session.resident_images.pop(image_id, None)
    session.working_set_image_ids.clear()
    session.image_warnings = warnings
    return tuple(warnings)


__all__ = [
    "REVIEW_IMAGE_KIND",
    "cleanup_review_images",
    "evict_review_image",
    "ensure_managed_review_image",
    "managed_review_images",
    "update_review_slot_style",
    "update_working_set",
]
