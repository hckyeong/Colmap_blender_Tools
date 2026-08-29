"""Camera Review 목록, frame 매핑과 O(1) 탐색 helper를 제공합니다."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

from .image_path_resolution import resolve_image_path
from .model import ImagePathManifest, Reconstruction


_EXACT_PROJECTION_MODELS = frozenset(("PINHOLE", "SIMPLE_PINHOLE"))


@dataclass(frozen=True)
class ReviewEntry:
    """등록 Image 하나와 연결된 불변 Camera Review 항목입니다."""

    image_id: int
    camera_id: int
    image_name: str
    resolved_image_path: Path | None
    path_status: str
    camera_model: str
    exact_projection_supported: bool
    width: int
    height: int


@dataclass(frozen=True)
class ReviewIndex:
    """현재 편집 revision에서 한 번 생성하는 불변 Review 인덱스입니다."""

    dataset_uuid: str
    source_fingerprint: str
    edit_revision: int
    entries: tuple[ReviewEntry, ...]
    image_id_to_index: Mapping[int, int]
    frame_to_image_id: Mapping[int, int]

    def __post_init__(self) -> None:
        entries = tuple(self.entries)
        image_ids = tuple(entry.image_id for entry in entries)
        if image_ids != tuple(sorted(image_ids)) or len(image_ids) != len(set(image_ids)):
            raise ValueError("ReviewEntry는 중복 없이 IMAGE_ID 오름차순이어야 합니다.")

        expected_index = {
            entry.image_id: index
            for index, entry in enumerate(entries)
        }
        supplied_index = dict(self.image_id_to_index)
        if supplied_index != expected_index:
            raise ValueError("IMAGE_ID와 Review index 매핑이 ReviewEntry와 일치하지 않습니다.")

        supplied_frames = dict(self.frame_to_image_id)
        if len(supplied_frames) != len(entries):
            raise ValueError("frame 매핑 수가 ReviewEntry 수와 일치하지 않습니다.")
        if tuple(supplied_frames.values()) != image_ids:
            raise ValueError("frame 매핑 순서가 ReviewEntry 순서와 일치하지 않습니다.")
        frame_numbers = tuple(supplied_frames)
        if frame_numbers and frame_numbers != tuple(
            range(frame_numbers[0], frame_numbers[0] + len(frame_numbers))
        ):
            raise ValueError("Camera Review frame은 연속이어야 합니다.")

        object.__setattr__(self, "entries", entries)
        object.__setattr__(
            self,
            "image_id_to_index",
            MappingProxyType(supplied_index),
        )
        object.__setattr__(
            self,
            "frame_to_image_id",
            MappingProxyType(supplied_frames),
        )


def _validate_reconstruction_keys(reconstruction: Reconstruction) -> None:
    camera_record_ids: set[int] = set()
    for camera_key, camera in reconstruction.cameras.items():
        if camera_key != camera.camera_id:
            raise ValueError(
                f"Camera dict key {camera_key}와 CameraRecord.camera_id "
                f"{camera.camera_id}가 일치하지 않습니다."
            )
        if camera.camera_id in camera_record_ids:
            raise ValueError(f"중복 CAMERA_ID가 있습니다: {camera.camera_id}")
        camera_record_ids.add(camera.camera_id)

    image_record_ids: set[int] = set()
    for image_key, image in reconstruction.images.items():
        if image_key != image.image_id:
            raise ValueError(
                f"Image dict key {image_key}와 ImageRecord.image_id "
                f"{image.image_id}가 일치하지 않습니다."
            )
        if image.image_id in image_record_ids:
            raise ValueError(f"중복 IMAGE_ID가 있습니다: {image.image_id}")
        image_record_ids.add(image.image_id)
        if image.camera_id not in reconstruction.cameras:
            raise ValueError(
                f"IMAGE_ID {image.image_id}가 존재하지 않는 "
                f"CAMERA_ID {image.camera_id}를 참조합니다."
            )


def build_review_index(
    reconstruction: Reconstruction,
    images_directory: Path | str,
    dataset_uuid: str,
    source_fingerprint: str,
    edit_revision: int,
    frame_start: int,
    image_path_manifest: ImagePathManifest | None = None,
) -> ReviewIndex:
    """현재 Reconstruction에서 IMAGE_ID 순서의 ReviewIndex를 생성합니다."""

    _validate_reconstruction_keys(reconstruction)
    manifest_entries = None
    if image_path_manifest is not None:
        resolved_images_directory = Path(images_directory).expanduser().resolve()
        if image_path_manifest.dataset_uuid != dataset_uuid:
            raise ValueError("이미지 경로 manifest의 Dataset UUID가 Review와 다릅니다.")
        if image_path_manifest.source_fingerprint != source_fingerprint:
            raise ValueError(
                "이미지 경로 manifest의 source fingerprint가 Review와 다릅니다."
            )
        if image_path_manifest.images_directory != resolved_images_directory:
            raise ValueError(
                "이미지 경로 manifest의 Images Directory가 Review와 다릅니다."
            )
        missing_image_ids = set(reconstruction.images) - set(
            image_path_manifest.entries
        )
        if missing_image_ids:
            raise ValueError(
                "이미지 경로 manifest에 현재 Review Image가 누락되었습니다: "
                f"{sorted(missing_image_ids)[:10]}"
            )
        manifest_entries = image_path_manifest.entries

    entries: list[ReviewEntry] = []
    for image_id in sorted(reconstruction.images):
        image = reconstruction.images[image_id]
        camera = reconstruction.cameras[image.camera_id]
        if manifest_entries is None:
            path_resolution = resolve_image_path(images_directory, image.name)
            resolved_image_path = path_resolution.resolved_image_path
            path_status = path_resolution.path_status
        else:
            manifest_entry = manifest_entries[image_id]
            if manifest_entry.image_name != image.name:
                raise ValueError(
                    f"IMAGE_ID {image_id}의 이름이 이미지 경로 manifest와 다릅니다."
                )
            resolved_image_path = manifest_entry.resolved_image_path
            path_status = manifest_entry.path_status
        entries.append(
            ReviewEntry(
                image_id=image.image_id,
                camera_id=image.camera_id,
                image_name=image.name,
                resolved_image_path=resolved_image_path,
                path_status=path_status,
                camera_model=camera.model_name,
                exact_projection_supported=(
                    camera.model_name in _EXACT_PROJECTION_MODELS
                ),
                width=camera.width,
                height=camera.height,
            )
        )

    entry_tuple = tuple(entries)
    image_id_to_index = {
        entry.image_id: index
        for index, entry in enumerate(entry_tuple)
    }
    frame_to_image_id = {
        frame_start + index: entry.image_id
        for index, entry in enumerate(entry_tuple)
    }
    return ReviewIndex(
        dataset_uuid=dataset_uuid,
        source_fingerprint=source_fingerprint,
        edit_revision=edit_revision,
        entries=entry_tuple,
        image_id_to_index=image_id_to_index,
        frame_to_image_id=frame_to_image_id,
    )


def _validate_entry_count(entry_count: int) -> None:
    if entry_count < 0:
        raise ValueError("ReviewEntry 수는 음수일 수 없습니다.")


def clamp_index(current_index: int, entry_count: int) -> int | None:
    """빈 목록은 None, 그 외 index는 유효 범위로 Clamp합니다."""

    _validate_entry_count(entry_count)
    if entry_count == 0:
        return None
    return max(0, min(current_index, entry_count - 1))


def previous_index(current_index: int, entry_count: int) -> int | None:
    """현재 index의 이전 위치를 첫 항목에서 Clamp합니다."""

    clamped = clamp_index(current_index, entry_count)
    if clamped is None:
        return None
    return max(0, clamped - 1)


def next_index(current_index: int, entry_count: int) -> int | None:
    """현재 index의 다음 위치를 마지막 항목에서 Clamp합니다."""

    clamped = clamp_index(current_index, entry_count)
    if clamped is None:
        return None
    return min(entry_count - 1, clamped + 1)


def _current_index(
    review_index: ReviewIndex,
    *,
    current_image_id: int | None,
    current_index: int | None,
) -> int | None:
    if not review_index.entries:
        return None
    if (current_image_id is None) == (current_index is None):
        raise ValueError("current_image_id와 current_index 중 하나만 지정해야 합니다.")
    if current_image_id is not None:
        try:
            return review_index.image_id_to_index[current_image_id]
        except KeyError as exc:
            raise ValueError(
                f"ReviewIndex에 IMAGE_ID {current_image_id}가 없습니다."
            ) from exc
    return clamp_index(current_index, len(review_index.entries))


def navigate_previous(
    review_index: ReviewIndex,
    *,
    current_image_id: int | None = None,
    current_index: int | None = None,
) -> int | None:
    """현재 IMAGE_ID 또는 index에서 이전 Review index를 O(1)로 구합니다."""

    resolved_index = _current_index(
        review_index,
        current_image_id=current_image_id,
        current_index=current_index,
    )
    if resolved_index is None:
        return None
    return previous_index(resolved_index, len(review_index.entries))


def navigate_next(
    review_index: ReviewIndex,
    *,
    current_image_id: int | None = None,
    current_index: int | None = None,
) -> int | None:
    """현재 IMAGE_ID 또는 index에서 다음 Review index를 O(1)로 구합니다."""

    resolved_index = _current_index(
        review_index,
        current_image_id=current_image_id,
        current_index=current_index,
    )
    if resolved_index is None:
        return None
    return next_index(resolved_index, len(review_index.entries))


def preferred_index_after_deletion(
    old_index: int,
    new_entry_count: int,
) -> int | None:
    """삭제 뒤 다음 생존 항목을 우선하고 없으면 새 마지막을 선택합니다."""

    _validate_entry_count(new_entry_count)
    if new_entry_count == 0:
        return None
    if old_index < 0:
        raise ValueError("삭제 전 Review index는 음수일 수 없습니다.")
    if old_index < new_entry_count:
        return old_index
    return new_entry_count - 1


__all__ = [
    "ReviewEntry",
    "ReviewIndex",
    "build_review_index",
    "clamp_index",
    "navigate_next",
    "navigate_previous",
    "next_index",
    "preferred_index_after_deletion",
    "previous_index",
]
