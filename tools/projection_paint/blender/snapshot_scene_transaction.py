"""Modifier 값 Snapshot을 PP 전용 Camera Import Transaction 입력으로 변환합니다."""

from __future__ import annotations

from datetime import datetime, timezone
from collections.abc import Callable
from typing import Any
from uuid import uuid4

try:
    from ....shared.colmap_transfer.contracts import ColmapTransferSnapshot
    from ....shared.colmap_transfer.registry import validate_provider_snapshot
except ImportError:
    from shared.colmap_transfer.contracts import ColmapTransferSnapshot
    from shared.colmap_transfer.registry import validate_provider_snapshot

from .camera_import import CameraImportRequest


SNAPSHOT_SCHEMA_VERSION = 1


def _preserved_display_settings(settings: Any, snapshot: ColmapTransferSnapshot) -> tuple[Any, ...]:
    """최초 복사는 Modifier 기본값을, 재업데이트는 현재 PP 표시값을 반환합니다."""

    if str(getattr(settings, "snapshot_state", "NONE")) in {
        "READY",
        "STALE",
        "UPDATING",
    } and str(getattr(settings, "snapshot_uuid", "") or ""):
        return (
            str(settings.camera_axis_preset),
            str(settings.camera_forward),
            str(settings.camera_up),
            float(settings.point_size),
            float(settings.camera_frustum_size),
        )
    defaults = snapshot.display_defaults
    return (
        defaults.axis_preset,
        defaults.axis_forward,
        defaults.axis_up,
        defaults.point_size,
        defaults.camera_frustum_size,
    )


def build_snapshot_import_request(
    context: Any,
    snapshot: ColmapTransferSnapshot,
    *,
    source_validator: Callable[[], bool] | None = None,
) -> CameraImportRequest:
    """불변 전송값으로 새 PP Snapshot UUID의 원자적 Import 요청을 만듭니다."""

    settings = context.scene.projection_paint
    axis_preset, axis_forward, axis_up, point_size, frustum_size = (
        _preserved_display_settings(settings, snapshot)
    )
    preferred_image_id = 0
    try:
        preferred_image_id = int(str(settings.current_image_id or "0"))
    except ValueError:
        preferred_image_id = int(getattr(settings, "active_image_id", 0) or 0)
    snapshot_uuid = uuid4().hex
    target_revision = max(1, int(getattr(settings, "snapshot_revision", 0)) + 1)

    def validate_source() -> bool:
        """Commit 직전 증분 Handle 또는 일반 Provider의 현재 token을 재검증합니다."""

        if source_validator is not None:
            return bool(source_validator())
        return validate_provider_snapshot(context, snapshot)

    return CameraImportRequest(
        dataset_uuid=snapshot_uuid,
        dataset_revision=target_revision,
        source_fingerprint=snapshot.content_fingerprint,
        cameras={item.camera_id: item for item in snapshot.cameras},
        images=snapshot.images,
        points=snapshot.points3d,
        frame_start=1,
        axis_preset=str(axis_preset),
        custom_forward=str(axis_forward),
        custom_up=str(axis_up),
        source_validator=validate_source,
        model_root=snapshot.model_directory,
        images_root=snapshot.images_directory,
        image_paths=snapshot.image_paths,
        point_size=float(point_size),
        camera_frustum_size=float(frustum_size),
        core_validated=True,
        snapshot_schema_version=SNAPSHOT_SCHEMA_VERSION,
        source_domain_uuid=snapshot.source_domain_uuid,
        source_domain_generation=snapshot.source_domain_generation,
        source_dataset_uuid=snapshot.source_dataset_uuid,
        source_revision=snapshot.source_revision,
        source_modifier_fingerprint=snapshot.source_fingerprint,
        source_history_hash=snapshot.source_history_hash,
        content_fingerprint=snapshot.content_fingerprint,
        snapshot_created_at=datetime.now(timezone.utc).isoformat(),
        preferred_image_id=preferred_image_id,
    )


__all__ = ["SNAPSHOT_SCHEMA_VERSION", "build_snapshot_import_request"]
