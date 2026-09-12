"""Modifier 확정 Reconstruction을 값 전용 Projection Snapshot으로 제공합니다."""

from __future__ import annotations

from time import monotonic
from typing import Any

try:
    from ....shared.colmap_transfer.contracts import (
        TRANSFER_SCHEMA_VERSION,
        CameraValue,
        ColmapTransferSnapshot,
        ImageValue,
        Point3DValue,
        SnapshotContentFingerprintBuilder,
        TransferDisplayDefaults,
        snapshot_content_fingerprint,
    )
    from ....shared.colmap_transfer.registry import (
        ProviderProgress,
        register_provider,
        unregister_provider,
    )
except ImportError:
    from shared.colmap_transfer.contracts import (
        TRANSFER_SCHEMA_VERSION,
        CameraValue,
        ColmapTransferSnapshot,
        ImageValue,
        Point3DValue,
        SnapshotContentFingerprintBuilder,
        TransferDisplayDefaults,
        snapshot_content_fingerprint,
    )
    from shared.colmap_transfer.registry import (
        ProviderProgress,
        register_provider,
        unregister_provider,
    )

from .runtime_state import (
    cached_runtime,
    cached_runtime_matches_scene,
    ensure_runtime,
    request_runtime_restore,
    runtime_restore_token,
)
from .modifier_runtime_domain import domain_for_scene
from ..core.dataset_transform import IDENTITY_TRANSFORM, transform_image, transform_point, validate_transform


PROVIDER_NAME = "modifier"


def _scene(context: Any) -> Any:
    """Context 또는 Scene 입력에서 Modifier Scene을 반환합니다."""

    scene = getattr(context, "scene", None)
    return scene if scene is not None else context


def _image_paths(state: Any) -> dict[int, str]:
    """검증된 Modifier Image Manifest를 IMAGE_ID별 절대경로로 고정합니다."""

    manifest = state.image_path_manifest
    if manifest is None:
        raise ValueError("Modifier 이미지 경로 Manifest가 없습니다. 다시 Import하십시오.")
    return {
        int(image_id): str(entry.resolved_image_path)
        for image_id, entry in manifest.entries.items()
    }


def build_projection_snapshot(context: Any) -> ColmapTransferSnapshot:
    """확정된 점·카메라 변환을 Blender ID 없는 독립 Snapshot에 반영합니다."""

    scene = _scene(context)
    settings = getattr(scene, "colmap_import", None)
    if settings is None or not str(settings.dataset_uuid or ""):
        raise ValueError("COLMAP Modifier에서 데이터를 먼저 불러오세요.")
    state = ensure_runtime(scene)
    domain = domain_for_scene(scene, required=True)
    if getattr(domain, "transform_preview_active", False):
        raise ValueError("데이터셋 변환을 확정하거나 취소한 뒤 업데이트하십시오.")
    if domain.dataset_state is not state:
        raise ValueError("Modifier Domain과 Snapshot Runtime 상태가 일치하지 않습니다.")
    identity = domain.identity
    reconstruction = state.current_reconstruction
    matrix = validate_transform(getattr(state, "dataset_transform", IDENTITY_TRANSFORM))
    paths = _image_paths(state)
    cameras = tuple(
        CameraValue(
            camera_id=int(record.camera_id),
            model_id=int(record.model_id),
            model_name=str(record.model_name),
            width=int(record.width),
            height=int(record.height),
            params=tuple(record.params),
        )
        for _camera_id, record in sorted(reconstruction.cameras.items())
    )
    images = tuple(
        ImageValue(
            image_id=int(record.image_id),
            camera_id=int(record.camera_id),
            name=str(record.name),
            qvec=tuple(record.qvec),
            tvec=tuple(record.tvec),
            image_path=paths[int(record.image_id)],
        )
        for _image_id, source_record in sorted(reconstruction.images.items())
        for record in (transform_image(matrix, source_record),)
    )
    points = tuple(
        Point3DValue(
            point3d_id=int(record.point3d_id),
            xyz=transform_point(matrix, record.xyz),
            rgb=tuple(record.rgb),
            error=float(record.error),
        )
        for _point_id, record in sorted(reconstruction.points3d.items())
    )
    content_fingerprint = snapshot_content_fingerprint(
        source_domain_uuid=identity.domain_uuid,
        source_domain_generation=identity.domain_generation,
        source_dataset_uuid=str(state.dataset_uuid),
        source_revision=int(state.edit_revision),
        source_fingerprint=str(state.source.source_fingerprint),
        source_history_hash=str(state.history_hash),
        cameras=cameras,
        images=images,
        points3d=points,
    )
    return ColmapTransferSnapshot(
        schema_version=TRANSFER_SCHEMA_VERSION,
        source_domain_uuid=identity.domain_uuid,
        source_domain_generation=identity.domain_generation,
        source_dataset_uuid=str(state.dataset_uuid),
        source_revision=int(state.edit_revision),
        source_fingerprint=str(state.source.source_fingerprint),
        source_history_hash=str(state.history_hash),
        content_fingerprint=content_fingerprint,
        model_directory=str(state.source.model_directory),
        images_directory=str(state.source.images_directory),
        cameras=cameras,
        images=images,
        points3d=points,
        display_defaults=TransferDisplayDefaults(
            axis_preset=str(settings.axis_preset),
            axis_forward=str(settings.axis_forward),
            axis_up=str(settings.axis_up),
            point_size=float(settings.point_size),
            camera_frustum_size=float(settings.camera_frustum_size),
        ),
    )


class ProjectionSnapshotProviderHandle:
    """Modifier Runtime 복원과 값 변환을 UI Tick 단위로 순차 실행합니다."""

    def __init__(self, context: Any) -> None:
        """Scene source token을 고정하고 필요할 때만 공용 Runtime 복원을 요청합니다."""

        self._context = context
        self._scene = _scene(context)
        self._transform = IDENTITY_TRANSFORM
        self._token = runtime_restore_token(self._scene)
        self._domain_identity = None
        self._runtime_job = None
        self._state = None
        self._phase = "RESTORE"
        self._camera_ids: tuple[int, ...] = ()
        self._image_ids: tuple[int, ...] = ()
        self._point_ids: tuple[int, ...] = ()
        self._index = 0
        self._paths: dict[int, str] = {}
        self._cameras: list[CameraValue] = []
        self._images: list[ImageValue] = []
        self._points: list[Point3DValue] = []
        self._fingerprint_builder: SnapshotContentFingerprintBuilder | None = None
        self.done = False
        self.failed = False
        self.error: Exception | None = None
        self.result: ColmapTransferSnapshot | None = None
        self.progress = ProviderProgress(
            "RUNTIME_RESTORE",
            0.0,
            message="Modifier Runtime 복원을 준비하는 중",
        )
        domain = domain_for_scene(self._scene)
        if domain is not None:
            if getattr(domain, "transform_preview_active", False):
                raise ValueError("데이터셋 변환을 확정하거나 취소한 뒤 업데이트하십시오.")
            domain.require_editable()
            self._state = domain.dataset_state
            self._domain_identity = domain.identity
            self._begin_transfer()
        elif cached_runtime_matches_scene(self._scene):
            self._state = cached_runtime(self._token.dataset_uuid)
            self._domain_identity = domain_for_scene(self._scene, required=True).identity
            self._begin_transfer()
        else:
            self._runtime_job = request_runtime_restore(self._scene)

    def advance(self, work_budget_ms: float = 20.0) -> ProviderProgress:
        """Runtime 복원 또는 값 변환을 시간 예산 안에서 한 단계 전진시킵니다."""

        if self.done or self.failed:
            return self.progress
        try:
            if self._domain_identity is None:
                if runtime_restore_token(self._scene) != self._token:
                    raise ValueError("Snapshot 준비 중 Modifier source token이 변경되었습니다.")
            elif domain_for_scene(self._scene, required=True).identity != self._domain_identity:
                raise ValueError("Snapshot 준비 중 Modifier Domain 상태가 변경되었습니다.")
            if self._phase == "RESTORE":
                self._advance_restore(work_budget_ms)
            elif self._phase in {"CAMERAS", "IMAGES", "POINTS"}:
                self._advance_values(work_budget_ms)
            elif self._phase == "FINALIZE":
                self._finalize()
            else:
                raise RuntimeError("알 수 없는 Modifier Snapshot 제공 단계입니다.")
        except Exception as exc:
            self.failed = True
            self.error = exc
            self.progress = ProviderProgress(
                "FAILED",
                self.progress.fraction,
                self.progress.completed_units,
                self.progress.total_units,
                str(exc),
            )
            raise
        return self.progress

    def abort(self) -> None:
        """부분 Transfer 값을 폐기하고 단독 Runtime 복원 요청도 중단합니다."""

        if self.done or self.failed:
            return
        if self._runtime_job is not None and not self._runtime_job.done:
            from .modifier_session import SESSION, SESSION_ACTIVE

            if SESSION.phase != SESSION_ACTIVE:
                self._runtime_job.abort()
        self._cameras.clear()
        self._images.clear()
        self._points.clear()
        self.failed = True
        self.error = RuntimeError("Modifier Snapshot 값 준비가 취소되었습니다.")
        self.progress = ProviderProgress(
            "CANCELLED",
            self.progress.fraction,
            message=str(self.error),
        )

    def _advance_restore(self, work_budget_ms: float) -> None:
        """공유 RuntimeRestoreJob 진행률을 Provider 전체 구간에 매핑합니다."""

        if self._runtime_job is None:
            raise RuntimeError("Modifier Runtime 복원 Job이 없습니다.")
        if self._runtime_job.failed:
            raise self._runtime_job.error or RuntimeError(
                "Modifier Runtime 복원이 완료 전에 중단되었습니다."
            )
        if not self._runtime_job.done:
            runtime_progress = self._runtime_job.advance(work_budget_ms)
            self.progress = ProviderProgress(
                "RUNTIME_RESTORE",
                min(0.70, float(runtime_progress.fraction) * 0.70),
                int(runtime_progress.completed_units),
                int(runtime_progress.total_units),
                str(runtime_progress.message),
            )
            return
        self._state = self._runtime_job.result
        if self._state is None:
            raise RuntimeError("완료된 Modifier Runtime 복원 결과가 없습니다.")
        self._domain_identity = domain_for_scene(self._scene, required=True).identity
        self._begin_transfer()

    def _begin_transfer(self) -> None:
        """확정 변환과 결정론적인 레코드 순서를 전달 시작 시 고정합니다."""

        if self._state is None:
            raise RuntimeError("Snapshot으로 변환할 Modifier Runtime이 없습니다.")
        reconstruction = self._state.current_reconstruction
        self._transform = validate_transform(
            getattr(self._state, "dataset_transform", IDENTITY_TRANSFORM)
        )
        self._paths = _image_paths(self._state)
        self._camera_ids = tuple(sorted(int(value) for value in reconstruction.cameras))
        self._image_ids = tuple(sorted(int(value) for value in reconstruction.images))
        self._point_ids = tuple(int(value) for value in self._state.point_id_order)
        self._fingerprint_builder = SnapshotContentFingerprintBuilder(
            source_domain_uuid=self._domain_identity.domain_uuid,
            source_domain_generation=self._domain_identity.domain_generation,
            source_dataset_uuid=str(self._state.dataset_uuid),
            source_revision=int(self._state.edit_revision),
            source_fingerprint=str(self._state.source.source_fingerprint),
            source_history_hash=str(self._state.history_hash),
        )
        self._phase = "CAMERAS"
        self._index = 0
        self.progress = ProviderProgress(
            "TRANSFER_CAMERAS",
            0.70,
            0,
            len(self._camera_ids),
            "Camera 전송값을 생성하는 중",
        )

    def _advance_values(self, work_budget_ms: float) -> None:
        """현재 종류의 전송 dataclass를 제한된 시간과 레코드 수로 생성합니다."""

        if self._state is None:
            raise RuntimeError("전송값을 생성할 Modifier Runtime이 없습니다.")
        deadline = monotonic() + max(0.001, float(work_budget_ms) / 1000.0)
        reconstruction = self._state.current_reconstruction
        processed = 0
        if self._phase == "CAMERAS":
            ids, start, end = self._camera_ids, 0.70, 0.74
        elif self._phase == "IMAGES":
            ids, start, end = self._image_ids, 0.74, 0.80
        else:
            ids, start, end = self._point_ids, 0.80, 0.97
        while self._index < len(ids) and processed < 4096 and monotonic() < deadline:
            record_id = ids[self._index]
            if self._phase == "CAMERAS":
                record = reconstruction.cameras[record_id]
                value = CameraValue(
                    camera_id=int(record.camera_id),
                    model_id=int(record.model_id),
                    model_name=str(record.model_name),
                    width=int(record.width),
                    height=int(record.height),
                    params=tuple(record.params),
                )
                self._cameras.append(value)
                self._fingerprint().add_camera(value)
            elif self._phase == "IMAGES":
                record = transform_image(self._transform, reconstruction.images[record_id])
                value = ImageValue(
                    image_id=int(record.image_id),
                    camera_id=int(record.camera_id),
                    name=str(record.name),
                    qvec=tuple(record.qvec),
                    tvec=tuple(record.tvec),
                    image_path=self._paths[int(record.image_id)],
                )
                self._images.append(value)
                self._fingerprint().add_image(value)
            else:
                record = reconstruction.points3d[record_id]
                value = Point3DValue(
                    point3d_id=int(record.point3d_id),
                    xyz=transform_point(self._transform, record.xyz),
                    rgb=tuple(record.rgb),
                    error=float(record.error),
                )
                self._points.append(value)
                self._fingerprint().add_point3d(value)
            self._index += 1
            processed += 1
        fraction = self._index / len(ids) if ids else 1.0
        labels = {
            "CAMERAS": ("TRANSFER_CAMERAS", "Camera 전송값을 생성하는 중"),
            "IMAGES": ("TRANSFER_IMAGES", "Image 전송값을 생성하는 중"),
            "POINTS": ("TRANSFER_POINTS", "Point3D 전송값을 생성하는 중"),
        }
        phase, message = labels[self._phase]
        self.progress = ProviderProgress(
            phase,
            start + ((end - start) * fraction),
            self._index,
            len(ids),
            message,
        )
        if self._index < len(ids):
            return
        if self._phase == "CAMERAS":
            self._fingerprint().finish_cameras()
            self._phase = "IMAGES"
        elif self._phase == "IMAGES":
            self._fingerprint().finish_images()
            self._phase = "POINTS"
        else:
            self._phase = "FINALIZE"
        self._index = 0

    def _fingerprint(self) -> SnapshotContentFingerprintBuilder:
        """초기화된 증분 Snapshot 지문 Builder를 반환합니다."""

        if self._fingerprint_builder is None:
            raise RuntimeError("Snapshot 지문 Builder가 준비되지 않았습니다.")
        return self._fingerprint_builder

    def _finalize(self) -> None:
        """완성된 순수 값을 지문화하고 불변 Snapshot을 한 번만 공개합니다."""

        if self._state is None or self._domain_identity is None:
            raise ValueError("Modifier Snapshot 완료 Domain token이 없습니다.")
        if domain_for_scene(self._scene, required=True).identity != self._domain_identity:
            raise ValueError("Modifier Snapshot 완료 token 검증에 실패했습니다.")
        settings = self._scene.colmap_import
        cameras = tuple(self._cameras)
        images = tuple(self._images)
        points = tuple(self._points)
        content_fingerprint = self._fingerprint().finish()
        self.result = ColmapTransferSnapshot(
            schema_version=TRANSFER_SCHEMA_VERSION,
            source_domain_uuid=self._domain_identity.domain_uuid,
            source_domain_generation=self._domain_identity.domain_generation,
            source_dataset_uuid=str(self._state.dataset_uuid),
            source_revision=int(self._state.edit_revision),
            source_fingerprint=str(self._state.source.source_fingerprint),
            source_history_hash=str(self._state.history_hash),
            content_fingerprint=content_fingerprint,
            model_directory=str(self._state.source.model_directory),
            images_directory=str(self._state.source.images_directory),
            cameras=cameras,
            images=images,
            points3d=points,
            display_defaults=TransferDisplayDefaults(
                axis_preset=str(settings.axis_preset),
                axis_forward=str(settings.axis_forward),
                axis_up=str(settings.axis_up),
                point_size=float(settings.point_size),
                camera_frustum_size=float(settings.camera_frustum_size),
            ),
        )
        self.done = True
        self.progress = ProviderProgress("DONE", 1.0, 1, 1, "Snapshot 값 생성 완료")


def projection_snapshot_is_current(context: Any, snapshot: ColmapTransferSnapshot) -> bool:
    """Modifier Domain 복합 Identity가 생성 시점과 같은지 확인합니다."""

    scene = _scene(context)
    try:
        domain = domain_for_scene(scene, required=True)
        identity = domain.identity
    except (AttributeError, ReferenceError, RuntimeError, TypeError, ValueError):
        return False
    return bool(
        identity.domain_uuid == snapshot.source_domain_uuid
        and identity.domain_generation == snapshot.source_domain_generation
        and identity.dataset_uuid == snapshot.source_dataset_uuid
        and identity.edit_revision == snapshot.source_revision
        and identity.source_fingerprint == snapshot.source_fingerprint
        and identity.history_hash == snapshot.source_history_hash
    )


def open_projection_snapshot_provider(context: Any) -> ProjectionSnapshotProviderHandle:
    """공용 Registry에서 사용할 Modifier 증분 Snapshot Handle을 생성합니다."""

    return ProjectionSnapshotProviderHandle(context)


def register_projection_snapshot_provider() -> None:
    """Modifier 값 Snapshot 제공자를 중립 Registry에 등록합니다."""

    register_provider(
        PROVIDER_NAME,
        build_projection_snapshot,
        projection_snapshot_is_current,
        handle_factory=open_projection_snapshot_provider,
    )


def unregister_projection_snapshot_provider() -> None:
    """Modifier 값 Snapshot 제공자를 중립 Registry에서 해제합니다."""

    unregister_provider(PROVIDER_NAME)


__all__ = [
    "PROVIDER_NAME",
    "ProjectionSnapshotProviderHandle",
    "build_projection_snapshot",
    "open_projection_snapshot_provider",
    "projection_snapshot_is_current",
    "register_projection_snapshot_provider",
    "unregister_projection_snapshot_provider",
]
