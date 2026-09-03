"""Modifier Runtime을 중립 공용 COLMAP Dataset 계약에 연결합니다."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

try:
    from ....shared.colmap_dataset.contracts import DatasetImportMetadata
    from ....shared.colmap_dataset.blender.registry import (
        unregister_display_listener,
    )
except ImportError:
    from shared.colmap_dataset.contracts import DatasetImportMetadata
    from shared.colmap_dataset.blender.registry import (
        unregister_display_listener,
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
    """같은 Source 재Import에는 Modifier Domain과 자기 Scene Mirror만 사용합니다."""

    try:
        from .modifier_runtime_domain import domain_for_scene

        domain = domain_for_scene(scene)
        if domain is not None:
            state = domain.dataset_state
            return state.dataset_uuid, Path(state.source.model_directory).resolve()
    except (AttributeError, OSError, RuntimeError, TypeError, ValueError):
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
    """신규 Import에서 Legacy 공용 Dataset을 더 이상 권위로 갱신하지 않습니다."""

    del scene, loaded, runtime_state, settings
    return None


def prepare_modifier_edit_commit(scene: Any, new_state: Any):
    """확정 편집이 Legacy 공용 Revision을 변경하지 않도록 빈 경계를 반환합니다."""

    del scene, new_state
    return None


def refresh_shared_source_after_export(
    scene: Any,
    loaded: Any,
    reconstruction: Any,
    settings: Any,
) -> None:
    """Export 결과를 Legacy 공용 Dataset에 다시 쓰지 않습니다."""

    del scene, loaded, reconstruction, settings


def synchronize_display_from_modifier(
    settings: Any,
    context: Any,
    *field_names: str,
) -> None:
    """Modifier 표시 설정을 독립 상태로 유지하고 Legacy 공유 쓰기를 생략합니다."""

    del settings, context, field_names


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
    """Legacy Dataset은 읽기 전용 입력이므로 신규 표시 Listener를 등록하지 않습니다."""


def unregister_modifier_dataset_adapter() -> None:
    """구버전 등록이 남아 있으면 Modifier 표시 Listener만 안전하게 해제합니다."""

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
