"""파일 입력과 이전 저장 자료의 Painter 준비 상태를 독립적으로 판정합니다."""

from __future__ import annotations

from typing import Any


def snapshot_is_ready(scene: Any) -> bool:
    """영구 기록과 Painter 데이터 식별자가 일치할 때 준비 완료로 판정합니다."""
    settings = getattr(scene, "projection_paint", None)
    if settings is None or str(getattr(settings, "snapshot_state", "NONE")) != "READY":
        return False
    try:
        from .camera_scene import RESOURCE_OWNER_PROJECTION_PAINT, scene_manifest

        manifest = scene_manifest(scene)
    except Exception:
        return False
    return bool(
        manifest is not None
        and manifest.resource_owner == RESOURCE_OWNER_PROJECTION_PAINT
        and manifest.dataset_uuid == str(settings.snapshot_uuid or "")
        and manifest.dataset_uuid == str(settings.dataset_uuid or "")
        and manifest.dataset_revision == int(settings.snapshot_revision)
        and manifest.source_fingerprint == str(settings.source_content_fingerprint or "")
    )


__all__ = ["snapshot_is_ready"]
