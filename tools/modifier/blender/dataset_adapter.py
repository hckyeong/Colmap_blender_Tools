"""Modifier Runtime을 중립 공용 COLMAP Dataset 계약에 연결합니다."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

try:
    from ....shared.colmap_dataset.contracts import DatasetImportMetadata
    from ....shared.colmap_dataset.blender.registry import (
        begin_edit_commit,
        begin_import_commit,
        refresh_source_metadata,
        register_display_listener,
        snapshot,
        unregister_display_listener,
        update_display_settings,
    )
except ImportError:
    from shared.colmap_dataset.contracts import DatasetImportMetadata
    from shared.colmap_dataset.blender.registry import (
        begin_edit_commit,
        begin_import_commit,
        refresh_source_metadata,
        register_display_listener,
        snapshot,
        unregister_display_listener,
        update_display_settings,
    )


_DISPLAY_SYNC_IN_PROGRESS = False


def _shared_property_available(scene: Any) -> bool:
    """통합 Root가 Scene.colmap_dataset을 등록했는지 확인합니다."""

    return bool(scene is not None and getattr(scene, "colmap_dataset", None) is not None)


def _image_manifest_json(loaded: Any) -> str:
    """Modifier의 검증된 이미지 경로 Manifest를 중립 JSON으로 직렬화합니다."""

    manifest = getattr(loaded, "image_path_manifest", None)
    if manifest is None:
        return "[]"
    entries = [
        {
            "image_id": int(entry.image_id),
            "image_name": str(entry.image_name),
            "resolved_image_path": str(entry.resolved_image_path),
            "path_status": str(entry.path_status),
        }
        for _image_id, entry in sorted(manifest.entries.items())
    ]
    return json.dumps(entries, ensure_ascii=False, separators=(",", ":"))


def build_import_metadata(loaded: Any, reconstruction: Any, settings: Any) -> DatasetImportMetadata:
    """Modifier LoadedReconstruction과 표시값을 공용 Import 메타데이터로 변환합니다."""

    source = loaded.source
    return DatasetImportMetadata(
        dataset_uuid=str(source.dataset_uuid),
        source_fingerprint=str(source.source_fingerprint),
        dataset_root=str(source.dataset_root),
        sparse_root=str(source.sparse_root),
        model_directory=str(source.model_directory),
        images_directory=str(source.images_directory),
        input_format=str(source.input_format),
        layout=str(source.layout),
        selected_files_json=json.dumps(
            [str(path) for path in source.selected_files],
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        image_path_manifest_json=_image_manifest_json(loaded),
        camera_count=len(reconstruction.cameras),
        image_count=len(reconstruction.images),
        point3d_count=len(reconstruction.points3d),
        source_schema_version=int(source.schema_version),
        axis_preset=str(settings.axis_preset),
        axis_forward=str(settings.axis_forward),
        axis_up=str(settings.axis_up),
        point_size=float(settings.point_size),
        camera_frustum_size=float(settings.camera_frustum_size),
    )


def matching_import_identity(scene: Any, settings: Any) -> tuple[str | None, Path | None]:
    """같은 Source 재Import에 공용 UUID를 우선하고 Legacy 설정을 보조로 사용합니다."""

    if _shared_property_available(scene):
        current = snapshot(scene)
        if current.available:
            try:
                return current.dataset_uuid, Path(current.model_directory).resolve()
            except (OSError, RuntimeError, ValueError):
                return None, None
    if not settings.dataset_uuid or not settings.active_model_directory:
        return None, None
    try:
        return str(settings.dataset_uuid), Path(settings.active_model_directory).resolve()
    except (OSError, RuntimeError, ValueError):
        return None, None


def prepare_modifier_import_commit(
    scene: Any,
    loaded: Any,
    runtime_state: Any,
    settings: Any,
):
    """Modifier Scene commit과 함께 적용할 공용 Dataset transaction을 준비합니다."""

    if not _shared_property_available(scene):
        return None
    metadata = build_import_metadata(
        loaded,
        runtime_state.current_reconstruction,
        settings,
    )
    return begin_import_commit(scene, metadata)


def prepare_modifier_edit_commit(scene: Any, new_state: Any):
    """확정 편집 Scene 동기화와 함께 적용할 공용 Revision transaction을 준비합니다."""

    if not _shared_property_available(scene):
        return None
    reconstruction = new_state.current_reconstruction
    return begin_edit_commit(
        scene,
        dataset_uuid=str(new_state.dataset_uuid),
        camera_count=len(reconstruction.cameras),
        image_count=len(reconstruction.images),
        point3d_count=len(reconstruction.points3d),
    )


def refresh_shared_source_after_export(
    scene: Any,
    loaded: Any,
    reconstruction: Any,
    settings: Any,
) -> None:
    """Export 성공의 새 Source 기준값을 Dataset Revision 증가 없이 반영합니다."""

    if not _shared_property_available(scene):
        return
    metadata = build_import_metadata(loaded, reconstruction, settings)
    refresh_source_metadata(scene, metadata)


def synchronize_display_from_modifier(
    settings: Any,
    context: Any,
    *field_names: str,
) -> None:
    """Modifier UI 표시값을 같은 UUID의 공용 Dataset 속성에 한 Revision으로 반영합니다."""

    if _DISPLAY_SYNC_IN_PROGRESS:
        return
    scene = getattr(settings, "id_data", None) or getattr(context, "scene", None)
    if not _shared_property_available(scene):
        return
    current = snapshot(scene)
    if not current.available or current.dataset_uuid != str(settings.dataset_uuid):
        return
    values = {field_name: getattr(settings, field_name) for field_name in field_names}
    update_display_settings(scene, **values)


def _apply_shared_display_to_modifier(
    scene: Any,
    current: Any,
    changed_fields: frozenset[str],
) -> None:
    """공용 표시 변경을 기존 Modifier RNA Callback을 통해 Scene 요소에 적용합니다."""

    global _DISPLAY_SYNC_IN_PROGRESS
    if _DISPLAY_SYNC_IN_PROGRESS:
        return
    settings = getattr(scene, "colmap_import", None)
    if settings is None or str(settings.dataset_uuid) != current.dataset_uuid:
        return
    _DISPLAY_SYNC_IN_PROGRESS = True
    try:
        if changed_fields & {"axis_preset", "axis_forward", "axis_up"}:
            if settings.axis_preset != current.axis_preset:
                settings.axis_preset = current.axis_preset
            if settings.axis_forward != current.axis_forward:
                settings.axis_forward = current.axis_forward
            if settings.axis_up != current.axis_up:
                settings.axis_up = current.axis_up
        if "point_size" in changed_fields and settings.point_size != current.point_size:
            settings.point_size = current.point_size
        if (
            "camera_frustum_size" in changed_fields
            and settings.camera_frustum_size != current.camera_frustum_size
        ):
            settings.camera_frustum_size = current.camera_frustum_size
    finally:
        _DISPLAY_SYNC_IN_PROGRESS = False


def register_modifier_dataset_adapter() -> None:
    """Modifier의 공용 표시 속성 소비 Adapter를 등록합니다."""

    register_display_listener("modifier", _apply_shared_display_to_modifier)


def unregister_modifier_dataset_adapter() -> None:
    """Modifier가 등록한 공용 표시 속성 소비 Adapter만 해제합니다."""

    unregister_display_listener("modifier")


__all__ = [
    "build_import_metadata",
    "matching_import_identity",
    "prepare_modifier_edit_commit",
    "prepare_modifier_import_commit",
    "refresh_shared_source_after_export",
    "register_modifier_dataset_adapter",
    "synchronize_display_from_modifier",
    "unregister_modifier_dataset_adapter",
]
