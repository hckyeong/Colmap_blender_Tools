"""검증된 COLMAP Reconstruction을 Blender Scene에 2단계로 Commit합니다."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import json
import math
from time import perf_counter
from typing import Any, Callable, Iterable, Mapping

import bpy
from bpy.props import StringProperty

from .axis_roots import create_axis_root
from .camera_display import (
    CAMERA_FRUSTUM_SIZE_MAX,
    CAMERA_FRUSTUM_SIZE_MIN,
    DEFAULT_CAMERA_FRUSTUM_SIZE,
)
from .camera_scene import (
    MANIFEST_SCHEMA,
    CameraDataManifest,
    CameraManifest,
    CameraManifestEntry,
    CameraSceneError,
    assert_marker_frames_available,
    build_manifest_entry,
    clear_camera_runtime,
    create_camera_data,
    create_camera_object,
    install_camera_runtime,
    managed_marker_candidates,
    manifest_to_json,
    scene_manifest,
)
from .ownership import (
    DATASET_REVISION_KEY,
    DATASET_UUID_KEY,
    MANIFEST_KEY,
    UUID_KEY,
    is_owned,
    owned_uuid_of,
    resolve_owned,
    tag_owned,
)
from .point_scene import (
    PointSceneBuildJob,
    PointSceneResources,
    remove_point_scene_resources,
)
from .import_progress_session import (
    active_import_progress_session,
    attach_import_progress_ui,
    create_import_progress_session,
    get_import_progress_session,
    release_import_progress_session,
)
from .progress_overlay import (
    ProgressOverlayView,
    add_progress_overlay_draw_handler,
    compact_path,
    draw_progress_overlay,
    remove_progress_overlay_draw_handler,
    request_progress_overlay_redraw,
)
from .transactions import TransactionJournal


_TIMER_INTERVAL_SECONDS = 0.01
_MARKER_TOOL_ID = "projection_paint"


def _projection_marker_session_is_active(scene: Any) -> bool:
    """Import Scene의 Marker 사용권을 Projection Paint가 소유하는지 반환합니다."""

    try:
        from ....shared.blender_runtime.scene_marker_session import (
            scene_marker_session_owned_by,
        )
    except ImportError:
        from shared.blender_runtime.scene_marker_session import (
            scene_marker_session_owned_by,
        )

    return bool(scene_marker_session_owned_by(scene, _MARKER_TOOL_ID))
_WORK_BUDGET_SECONDS = 0.025


class CameraImportError(RuntimeError):
    """Camera Import 검증·Staging·Commit 실패를 나타냅니다."""


class CameraImportState(str, Enum):
    """Camera Import Job의 허용 상태 전이를 정의합니다."""

    VALIDATING = "VALIDATING"
    STAGING = "STAGING"
    READY_TO_COMMIT = "READY_TO_COMMIT"
    COMMITTING = "COMMITTING"
    ACTIVE = "ACTIVE"
    ABORTED = "ABORTED"


@dataclass(frozen=True, slots=True)
class CameraImportRequest:
    """Core가 준비한 Reconstruction Domain과 Commit 계약을 전달합니다."""

    dataset_uuid: str
    dataset_revision: int
    source_fingerprint: str
    cameras: Mapping[int, Any] | Iterable[Any]
    images: Iterable[Any]
    points: Iterable[Any] = field(default_factory=tuple)
    frame_start: int = 1
    axis_preset: str = "COLMAP"
    custom_forward: str = "-Z"
    custom_up: str = "Y"
    source_validator: Callable[[], bool] | None = None
    source_validation_job_factory: Callable[[], Any] | None = None
    model_root: str = ""
    images_root: str = ""
    image_paths: Mapping[int, str] = field(default_factory=dict)
    point_size: float = 0.05
    camera_frustum_size: float = DEFAULT_CAMERA_FRUSTUM_SIZE
    core_validated: bool = False
    snapshot_schema_version: int = 0
    source_domain_uuid: str = ""
    source_domain_generation: int = 0
    source_dataset_uuid: str = ""
    source_revision: int = 0
    source_modifier_fingerprint: str = ""
    source_history_hash: str = ""
    content_fingerprint: str = ""
    snapshot_created_at: str = ""
    preferred_image_id: int = 0


@dataclass(frozen=True, slots=True)
class CameraImportResult:
    """Commit된 Camera, Marker, Camera Data와 Point 수를 반환합니다."""

    dataset_uuid: str
    camera_count: int
    camera_data_count: int
    marker_count: int
    point_count: int


def _field(value: Any, name: str, default: Any = None) -> Any:
    """Mapping과 일반 객체에서 같은 이름의 필드를 읽습니다."""

    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _camera_map(values: Mapping[int, Any] | Iterable[Any]) -> dict[int, Any]:
    """Camera 입력을 중복 검증된 CAMERA_ID Mapping으로 정규화합니다."""

    iterable = values.values() if isinstance(values, Mapping) else values
    result: dict[int, Any] = {}
    for record in iterable:
        camera_id = int(_field(record, "camera_id"))
        if camera_id in result:
            raise CameraImportError(f"CAMERA_ID가 중복되었습니다: {camera_id}")
        result[camera_id] = record
    if not result:
        raise CameraImportError("Camera Record가 없습니다.")
    return result


def _image_records(values: Iterable[Any]) -> tuple[Any, ...]:
    """Image 입력을 IMAGE_ID 순서의 중복 없는 Tuple로 정규화합니다."""

    result = sorted(tuple(values), key=lambda item: int(_field(item, "image_id")))
    image_ids = [int(_field(item, "image_id")) for item in result]
    if not result:
        raise CameraImportError("Image Pose Record가 없습니다.")
    if len(set(image_ids)) != len(image_ids):
        raise CameraImportError("IMAGE_ID가 중복되었습니다.")
    return tuple(result)


def _point_records(values: Iterable[Any]) -> tuple[Any, ...]:
    """Point 입력을 POINT3D_ID 순서의 중복 없는 Tuple로 정규화합니다."""
    result = sorted(tuple(values), key=lambda item: int(_field(item, "point3d_id")))
    point_ids = [int(_field(item, "point3d_id")) for item in result]
    if len(set(point_ids)) != len(point_ids):
        raise CameraImportError("POINT3D_ID가 중복되었습니다.")
    return tuple(result)


def _remove_staging(
    collection: Any,
    camera_data: Iterable[Any],
    point_resources: PointSceneResources | None,
) -> None:
    """연결 여부와 무관하게 모든 Staging Blender ID를 제거합니다."""

    remove_point_scene_resources(point_resources)
    if collection is not None:
        try:
            objects = tuple(collection.objects)
        except (AttributeError, ReferenceError):
            objects = ()
        for obj in objects:
            try:
                if is_owned(obj):
                    bpy.data.objects.remove(obj, do_unlink=True)
            except (ReferenceError, RuntimeError):
                pass
        try:
            if collection.name in bpy.data.collections:
                bpy.data.collections.remove(collection)
        except (AttributeError, ReferenceError, RuntimeError):
            pass
    for data in tuple(camera_data):
        try:
            if is_owned(data) and data.users == 0:
                bpy.data.cameras.remove(data)
        except (AttributeError, ReferenceError, RuntimeError):
            pass


def _remove_owned_data(data: Any, dataset_uuid: str) -> None:
    """사용자가 없는 소유 Camera 또는 Mesh Data를 실제 저장소에서 제거합니다."""
    try:
        if not is_owned(data, dataset_uuid=dataset_uuid) or data.users != 0:
            return
        if isinstance(data, bpy.types.Camera):
            bpy.data.cameras.remove(data)
        elif isinstance(data, bpy.types.Mesh):
            bpy.data.meshes.remove(data)
    except (AttributeError, ReferenceError, RuntimeError):
        pass


def _remove_marker(scene: Any, marker: Any) -> None:
    """Transaction이 만든 정확한 Marker 참조만 제거합니다."""

    try:
        scene.timeline_markers.remove(marker)
    except (AttributeError, ReferenceError, RuntimeError, TypeError):
        pass


def _cleanup_old_dataset(scene: Any, manifest: CameraManifest | None) -> None:
    """새 Commit 성공 뒤 이전 Manifest가 소유한 데이터만 제거합니다."""

    if manifest is None:
        return
    marker_pointers = managed_marker_candidates(scene, manifest)
    for marker in tuple(scene.timeline_markers):
        try:
            if int(marker.as_pointer()) in marker_pointers:
                scene.timeline_markers.remove(marker)
        except (AttributeError, ReferenceError, RuntimeError):
            pass
    collection = resolve_owned(
        bpy.data.collections,
        manifest.collection_uuid,
        kind="CAMERA_COLLECTION",
        dataset_uuid=manifest.dataset_uuid,
    )
    if collection is None:
        return
    data_blocks = []
    for obj in tuple(collection.objects):
        try:
            if is_owned(obj, dataset_uuid=manifest.dataset_uuid):
                if getattr(obj, "data", None) is not None:
                    data_blocks.append(obj.data)
                bpy.data.objects.remove(obj, do_unlink=True)
        except (AttributeError, ReferenceError, RuntimeError):
            pass
    try:
        bpy.data.collections.remove(collection)
    except (AttributeError, ReferenceError, RuntimeError):
        pass
    for data in data_blocks:
        _remove_owned_data(data, manifest.dataset_uuid)
    for values in (bpy.data.node_groups, bpy.data.materials):
        for data in tuple(values):
            try:
                if (
                    is_owned(data, dataset_uuid=manifest.dataset_uuid)
                    and data.users == 0
                ):
                    values.remove(data)
            except (AttributeError, ReferenceError, RuntimeError):
                pass


class CameraImportJob:
    """Camera Dataset을 연결 전 Staging하고 짧게 Commit하는 증분 Job입니다."""

    def __init__(
        self,
        scene: Any,
        request: CameraImportRequest,
        *,
        fault_injector: Callable[[str], None] | None = None,
    ) -> None:
        """검증 전 Camera Import Job을 생성합니다."""

        self.scene = scene
        self.request = request
        self.fault_injector = fault_injector
        self.state = CameraImportState.VALIDATING
        self.cameras: dict[int, Any] = {}
        self.images: tuple[Any, ...] = ()
        self.points: tuple[Any, ...] = ()
        self.old_manifest: CameraManifest | None = None
        self.collection: Any | None = None
        self.axis_root: Any | None = None
        self.camera_data: dict[int, Any] = {}
        self.point_resources: PointSceneResources | None = None
        self._point_job: PointSceneBuildJob | None = None
        self.data_manifests: list[CameraDataManifest] = []
        self.entries: list[CameraManifestEntry] = []
        self._camera_ids: list[int] = []
        self._camera_index = 0
        self._image_index = 0
        self._journal = TransactionJournal("Camera Import Staging")

    def _fault(self, label: str) -> None:
        """테스트 오류 주입 Callback을 지정 지점에서 실행합니다."""

        if self.fault_injector is not None:
            self.fault_injector(label)

    def validate(self) -> None:
        """Scene 변경 전에 참조·Frame·Marker 충돌을 전부 검증합니다."""

        if self.state is not CameraImportState.VALIDATING:
            return
        if not str(self.request.dataset_uuid):
            raise CameraImportError("Dataset UUID가 비어 있습니다.")
        if int(self.request.dataset_revision) <= 0:
            raise CameraImportError("Dataset Revision은 1 이상이어야 합니다.")
        camera_frustum_size = self.request.camera_frustum_size
        if (
            isinstance(camera_frustum_size, bool)
            or not isinstance(camera_frustum_size, (int, float))
            or not math.isfinite(camera_frustum_size)
            or camera_frustum_size < CAMERA_FRUSTUM_SIZE_MIN
            or camera_frustum_size > CAMERA_FRUSTUM_SIZE_MAX
        ):
            raise CameraImportError(
                "Camera Frustum Size는 0.0 이상 1000.0 이하의 "
                "유한한 값이어야 합니다."
            )
        if self.request.core_validated:
            self.cameras = dict(self.request.cameras)
            self.images = tuple(self.request.images)
            self.points = tuple(self.request.points)
        else:
            self.cameras = _camera_map(self.request.cameras)
            self.images = _image_records(self.request.images)
            self.points = _point_records(self.request.points)
        image_ids = {int(_field(image, "image_id")) for image in self.images}
        path_ids = {int(image_id) for image_id in self.request.image_paths}
        if path_ids and path_ids != image_ids:
            raise CameraImportError("IMAGE_ID와 Source Image 경로 Manifest가 일치하지 않습니다.")
        for image in self.images:
            camera_id = int(_field(image, "camera_id"))
            if camera_id not in self.cameras:
                raise CameraImportError(
                    f"IMAGE_ID {_field(image, 'image_id')}의 CAMERA_ID가 없습니다."
                )
        frame_start = int(self.request.frame_start)
        frames = range(frame_start, frame_start + len(self.images))
        self.old_manifest = scene_manifest(self.scene)
        assert_marker_frames_available(self.scene, frames, self.old_manifest)
        self._fault("validated")
        self.state = CameraImportState.STAGING

    def _begin_staging(self) -> None:
        """연결되지 않은 Collection과 Camera Axis Root를 생성합니다."""

        if self.collection is not None:
            return
        self.collection = bpy.data.collections.new(
            f"PP_COLMAP_{str(self.request.dataset_uuid)[:8]}"
        )

        def cleanup() -> None:
            """실패 시 연결 전 Staging 데이터만 제거합니다."""

            if self._point_job is not None:
                self._point_job.abort()
            _remove_staging(
                self.collection,
                self.camera_data.values(),
                self.point_resources,
            )

        self._journal.add_rollback("Staging 데이터 제거", cleanup)
        tag_owned(
            self.collection,
            "CAMERA_COLLECTION",
            dataset_uuid=self.request.dataset_uuid,
            source_fingerprint=self.request.source_fingerprint,
        )
        self.axis_root = create_axis_root(
            "PP_CAMERA_AXIS_ROOT",
            kind="CAMERA_AXIS_ROOT",
            dataset_uuid=self.request.dataset_uuid,
            preset=self.request.axis_preset,
            custom_forward=self.request.custom_forward,
            custom_up=self.request.custom_up,
        )
        self.collection.objects.link(self.axis_root)
        self._point_job = PointSceneBuildJob(
            self.points,
            self.collection,
            self.axis_root,
            self.request.dataset_uuid,
            self.request.point_size,
        )
        self._camera_ids = sorted(self.cameras)

    def _step_once(self, *, time_budget: float, hard_cap: int) -> bool:
        """Camera Data와 Object를 한 번의 시간 예산 안에서 생성합니다."""

        if self.state is CameraImportState.VALIDATING:
            self.validate()
        if self.state is CameraImportState.READY_TO_COMMIT:
            return True
        if self.state is not CameraImportState.STAGING:
            raise CameraImportError(f"Staging할 수 없는 상태입니다: {self.state}")
        staging_started = self.collection is None
        self._begin_staging()
        if staging_started:
            return False
        if self._point_job is not None and not self._point_job.done:
            if self._point_job.advance(
                time_budget=time_budget,
                hard_cap=max(4_096, hard_cap * 64),
            ):
                self.point_resources = self._point_job.result
            return False
        deadline = perf_counter() + max(0.001, float(time_budget))
        created = 0
        while (
            self._camera_index < len(self._camera_ids)
            and created < hard_cap
            and perf_counter() < deadline
        ):
            camera_id = self._camera_ids[self._camera_index]
            data, manifest = create_camera_data(
                self.cameras[camera_id],
                self.request.dataset_uuid,
                self.request.camera_frustum_size,
            )
            self.camera_data[camera_id] = data
            self.data_manifests.append(manifest)
            self._camera_index += 1
            created += 1
        while (
            self._camera_index == len(self._camera_ids)
            and self._image_index < len(self.images)
            and created < hard_cap
            and perf_counter() < deadline
        ):
            image = self.images[self._image_index]
            image_id = int(_field(image, "image_id"))
            camera_id = int(_field(image, "camera_id"))
            obj, qvec, tvec = create_camera_object(
                image,
                self.camera_data[camera_id],
                self.axis_root,
                self.collection,
                self.request.dataset_uuid,
            )
            frame = int(self.request.frame_start) + self._image_index
            marker_name = (
                f"PP_CAM_{str(self.request.dataset_uuid)[:8]}_{frame}_{image_id}"
            )
            self.entries.append(
                build_manifest_entry(
                    image,
                    obj,
                    frame,
                    marker_name,
                    qvec,
                    tvec,
                )
            )
            self._image_index += 1
            created += 1
        if self._image_index == len(self.images):
            self._fault("staged")
            self.state = CameraImportState.READY_TO_COMMIT
            return True
        return False

    def step(self, *, time_budget: float = 0.010, hard_cap: int = 256) -> bool:
        """증분 Staging 오류가 발생하면 부분 Blender ID를 즉시 정리합니다."""

        try:
            return self._step_once(
                time_budget=max(0.001, float(time_budget)),
                hard_cap=max(1, int(hard_cap)),
            )
        except Exception:
            self.abort()
            raise

    def _manifest(self) -> CameraManifest:
        """완료된 Staging Blender ID에서 영구 Manifest를 만듭니다."""

        if self.collection is None or self.axis_root is None:
            raise CameraImportError("Staging Collection이 준비되지 않았습니다.")
        return CameraManifest(
            schema=MANIFEST_SCHEMA,
            dataset_uuid=str(self.request.dataset_uuid),
            dataset_revision=int(self.request.dataset_revision),
            source_fingerprint=str(self.request.source_fingerprint),
            collection_uuid=owned_uuid_of(self.collection),
            axis_root_uuid=owned_uuid_of(self.axis_root),
            frame_start=int(self.request.frame_start),
            frame_end=int(self.request.frame_start) + len(self.entries) - 1,
            camera_data=tuple(self.data_manifests),
            entries=tuple(self.entries),
        )

    def commit(self, *, source_prevalidated: bool = False) -> CameraImportResult:
        """Staging Dataset을 짧은 비취소 Transaction으로 Scene에 연결합니다."""

        if self.state is not CameraImportState.READY_TO_COMMIT:
            raise CameraImportError("완료되지 않은 Staging은 Commit할 수 없습니다.")
        if not _projection_marker_session_is_active(self.scene):
            self.abort()
            raise CameraImportError("Projection Paint Marker 사용권이 없어 Commit할 수 없습니다.")
        if not source_prevalidated and self.request.source_validator is not None:
            try:
                source_is_current = bool(self.request.source_validator())
            except Exception:
                self.abort()
                raise
            if not source_is_current:
                self.abort()
                raise CameraImportError("Commit 직전 Source Fingerprint가 변경되었습니다.")
        self.state = CameraImportState.COMMITTING
        manifest = self._manifest()
        journal = TransactionJournal("Camera Import Commit")
        created_markers = []
        try:
            journal.record_mapping_value(self.scene, MANIFEST_KEY)
            journal.record_mapping_value(self.scene, DATASET_UUID_KEY)
            journal.record_mapping_value(self.scene, DATASET_REVISION_KEY)
            for attribute in ("camera", "frame_start", "frame_end", "frame_current"):
                journal.record_attribute(self.scene, attribute)
            self.scene.collection.children.link(self.collection)

            def unlink_collection() -> None:
                """Commit 실패 시 새 Collection의 Scene 연결만 해제합니다."""

                try:
                    self.scene.collection.children.unlink(self.collection)
                except (AttributeError, ReferenceError, RuntimeError):
                    pass

            journal.add_rollback("새 Collection 연결 해제", unlink_collection)
            for entry in manifest.entries:
                camera_object = next(
                    obj
                    for obj in self.collection.objects
                    if owned_uuid_of(obj) == entry.camera_uuid
                )
                marker = self.scene.timeline_markers.new(
                    entry.marker_name,
                    frame=entry.frame,
                )
                marker.camera = camera_object
                created_markers.append(marker)

                def remove_created_marker(item=marker) -> None:
                    """Commit 실패 시 현재 반복에서 만든 Marker를 제거합니다."""

                    _remove_marker(self.scene, item)

                journal.add_rollback(
                    f"Marker {entry.image_id} 제거",
                    remove_created_marker,
                )
            self.scene[MANIFEST_KEY] = manifest_to_json(manifest)
            self.scene[DATASET_UUID_KEY] = manifest.dataset_uuid
            self.scene[DATASET_REVISION_KEY] = manifest.dataset_revision
            self.scene.frame_start = manifest.frame_start
            self.scene.frame_end = manifest.frame_end
            preferred_entry = next(
                (
                    entry
                    for entry in manifest.entries
                    if entry.image_id == int(self.request.preferred_image_id)
                ),
                manifest.entries[0],
            )
            self.scene.frame_current = preferred_entry.frame
            first_camera = next(
                obj
                for obj in self.collection.objects
                if owned_uuid_of(obj) == preferred_entry.camera_uuid
            )
            from .camera_scene import (
                apply_projection_camera_state,
                projection_camera_session_is_active,
            )

            if projection_camera_session_is_active(self.scene):
                apply_projection_camera_state(self.scene, first_camera)
            settings = getattr(self.scene, "projection_paint", None)
            if settings is not None:
                for attribute in (
                    "dataset_uuid",
                    "dataset_revision",
                    "source_fingerprint",
                    "dataset_manifest_json",
                    "sparse_directory",
                    "images_directory",
                    "image_path_manifest_json",
                    "marker_manifest_json",
                    "snapshot_uuid",
                    "snapshot_revision",
                    "source_modifier_domain_uuid",
                    "source_modifier_domain_generation",
                    "source_modifier_dataset_uuid",
                    "source_modifier_revision",
                    "source_modifier_fingerprint",
                    "source_modifier_history_hash",
                    "source_content_fingerprint",
                    "snapshot_camera_count",
                    "snapshot_image_count",
                    "snapshot_point_count",
                    "snapshot_created_at",
                    "snapshot_state",
                    "snapshot_schema_version",
                    "point_size",
                    "camera_frustum_size",
                    "camera_axis_preset",
                    "camera_forward",
                    "camera_up",
                ):
                    journal.record_attribute(settings, attribute)
                settings.dataset_uuid = manifest.dataset_uuid
                settings.dataset_revision = manifest.dataset_revision
                settings.source_fingerprint = manifest.source_fingerprint
                settings.dataset_manifest_json = manifest_to_json(manifest)
                settings.sparse_directory = str(self.request.model_root)
                settings.images_directory = str(self.request.images_root)
                settings.image_path_manifest_json = json.dumps(
                    {
                        str(image_id): str(path)
                        for image_id, path in sorted(self.request.image_paths.items())
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                settings.marker_manifest_json = json_marker_manifest(manifest)
                if int(self.request.snapshot_schema_version) > 0:
                    settings.snapshot_uuid = manifest.dataset_uuid
                    settings.snapshot_revision = manifest.dataset_revision
                    settings.source_modifier_domain_uuid = str(
                        self.request.source_domain_uuid
                    )
                    settings.source_modifier_domain_generation = int(
                        self.request.source_domain_generation
                    )
                    settings.source_modifier_dataset_uuid = str(
                        self.request.source_dataset_uuid
                    )
                    settings.source_modifier_revision = int(self.request.source_revision)
                    settings.source_modifier_fingerprint = str(
                        self.request.source_modifier_fingerprint
                    )
                    settings.source_modifier_history_hash = str(
                        self.request.source_history_hash
                    )
                    settings.source_content_fingerprint = str(
                        self.request.content_fingerprint
                    )
                    settings.snapshot_camera_count = len(manifest.camera_data)
                    settings.snapshot_image_count = len(manifest.entries)
                    settings.snapshot_point_count = len(self.points)
                    settings.snapshot_created_at = str(self.request.snapshot_created_at)
                    settings.snapshot_state = "READY"
                    settings.snapshot_schema_version = int(
                        self.request.snapshot_schema_version
                    )
                    settings.point_size = float(self.request.point_size)
                    settings.camera_frustum_size = float(
                        self.request.camera_frustum_size
                    )
                    settings.camera_axis_preset = str(self.request.axis_preset)
                    settings.camera_forward = str(self.request.custom_forward)
                    settings.camera_up = str(self.request.custom_up)
                    from .ownership import tag_snapshot_resource

                    resources = [self.collection, *tuple(self.collection.all_objects)]
                    resources.extend(
                        data
                        for obj in tuple(self.collection.all_objects)
                        for data in (getattr(obj, "data", None),)
                        if data is not None
                    )
                    if self.point_resources is not None:
                        resources.extend(
                            (
                                self.point_resources.mesh,
                                self.point_resources.material,
                                self.point_resources.node_group,
                            )
                        )
                    for resource in resources:
                        tag_snapshot_resource(
                            resource,
                            snapshot_uuid=manifest.dataset_uuid,
                            source_domain_uuid=self.request.source_domain_uuid,
                            source_domain_generation=self.request.source_domain_generation,
                            source_dataset_uuid=self.request.source_dataset_uuid,
                            source_revision=self.request.source_revision,
                            source_history_hash=self.request.source_history_hash,
                            schema_version=self.request.snapshot_schema_version,
                        )
            self._fault("before_runtime")
            install_camera_runtime(self.scene, manifest)
            self._fault("committed")
            journal.commit()
            self._journal.commit()
            self.state = CameraImportState.ACTIVE
        except Exception:
            clear_camera_runtime()
            journal.abort()
            self.abort()
            if self.old_manifest is not None:
                try:
                    install_camera_runtime(self.scene, self.old_manifest)
                except CameraSceneError:
                    pass
            raise
        _cleanup_old_dataset(self.scene, self.old_manifest)
        return CameraImportResult(
            dataset_uuid=manifest.dataset_uuid,
            camera_count=len(manifest.entries),
            camera_data_count=len(manifest.camera_data),
            marker_count=len(created_markers),
            point_count=(
                self.point_resources.point_count
                if self.point_resources is not None
                else 0
            ),
        )

    def run_to_completion(self) -> CameraImportResult:
        """Background 또는 테스트에서 Job을 대기 없이 동기 완료합니다."""

        while not self.step(time_budget=1.0, hard_cap=1_000_000):
            pass
        return self.commit()

    def progress_units(self) -> tuple[str, int, int]:
        """현재 Scene Staging 단계와 완료·전체 작업량을 반환합니다."""

        point_total = len(self.points) + 7
        point_completed = (
            self._point_job.completed_units
            if self._point_job is not None
            else 0
        )
        total = point_total + len(self._camera_ids) + len(self.images)
        completed = min(
            point_completed + self._camera_index + self._image_index,
            total,
        )
        if self.state is CameraImportState.VALIDATING:
            return ("Validating Blender scene", 0, 0)
        if self.state is CameraImportState.STAGING:
            if self.collection is None:
                return ("Creating Blender collection", completed, total)
            if self._point_job is not None and not self._point_job.done:
                return (self._point_job.phase_message, completed, total)
            if self._camera_index < len(self._camera_ids):
                return ("Creating camera data", completed, total)
            return ("Creating cameras", completed, total)
        if self.state is CameraImportState.READY_TO_COMMIT:
            return ("Blender staging complete", total, total)
        if self.state is CameraImportState.COMMITTING:
            return ("Updating Blender scene", total, total)
        if self.state is CameraImportState.ACTIVE:
            return ("Import complete", total, total)
        return ("Import stopped", completed, total)

    def abort(self) -> None:
        """진행 중 Staging을 여러 번 호출해도 같은 상태로 정리합니다."""

        if self.state is CameraImportState.ACTIVE:
            return
        self._journal.abort()
        self.state = CameraImportState.ABORTED


def json_marker_manifest(manifest: CameraManifest) -> str:
    """UI Property에 필요한 최소 Marker Manifest JSON을 반환합니다."""

    return json.dumps(
        [
            {
                "image_id": entry.image_id,
                "frame": entry.frame,
                "name": entry.marker_name,
                "camera_uuid": entry.camera_uuid,
            }
            for entry in manifest.entries
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    )


_REQUEST_FACTORY: Callable[[Any], Any] | None = None


def set_import_request_factory(
    factory: Callable[[Any], Any] | None,
) -> None:
    """Core Import Adapter Factory를 순환 의존 없이 연결합니다."""

    global _REQUEST_FACTORY
    _REQUEST_FACTORY = factory


def _prepare_import_session(context: Any):
    """현재 Scene 입력을 고정할 Import 진행 세션을 생성합니다."""

    if _REQUEST_FACTORY is None:
        raise CameraImportError("COLMAP Core Import Adapter가 아직 연결되지 않았습니다.")
    settings = context.scene.projection_paint
    settings.status = "COLMAP Import를 준비하고 있습니다."
    settings.status_kind = "RUNNING"
    try:
        return create_import_progress_session(context.scene, _REQUEST_FACTORY)
    except Exception:
        settings.status_kind = "ERROR"
        raise


def _progress_values(session: Any) -> tuple[str, float, int, int]:
    """현재 Import 단계의 문구와 전체 진행률을 계산합니다."""

    if session.stage == "PREPARE_REQUEST":
        request_job = session.request_job
        if request_job is None:
            return ("Preparing COLMAP import", 0.0, 0, 0)
        snapshot = request_job.progress
        return (
            snapshot.message,
            snapshot.overall_fraction * 0.55,
            snapshot.completed_units,
            snapshot.total_units,
        )
    if session.stage == "STAGING" and session.job is not None:
        phase, completed, total = session.job.progress_units()
        phase_fraction = completed / total if total else 0.0
        return (phase, 0.55 + phase_fraction * 0.33, completed, total)
    if session.stage == "REVALIDATE" and session.validation_job is not None:
        snapshot = session.validation_job.progress
        return (
            f"Rechecking: {snapshot.message}",
            0.88 + snapshot.overall_fraction * 0.10,
            snapshot.completed_units,
            snapshot.total_units,
        )
    if session.stage == "COMMIT":
        return ("Updating Blender scene", 0.99, 0, 1)
    if session.stage == "DONE":
        return ("Import complete", 1.0, 1, 1)
    return ("Preparing import", 0.0, 0, 0)


def _import_progress_overlay_view(session: Any) -> ProgressOverlayView:
    """현재 Import Session을 중앙 Overlay 표시값으로 변환합니다."""

    phase, fraction, completed, total = _progress_values(session)
    settings = session.scene.projection_paint
    dataset_path = (
        str(settings.dataset_root).strip()
        or str(settings.sparse_directory).strip()
        or "Not selected"
    )
    units_text = (
        f"{completed:,} / {total:,}" if total > 0 else "Calculating work units"
    )
    return ProgressOverlayView(
        title="COLMAP Import",
        phase=phase,
        fraction=max(0.0, min(float(fraction), 1.0)),
        completed_units=min(completed, total) if total else 0,
        total_units=total,
        elapsed_seconds=session.elapsed_seconds,
        detail_lines=(
            f"Processed: {units_text}    Elapsed: {session.elapsed_seconds:.1f}s",
            f"Dataset: {compact_path(dataset_path)}",
            f"Format: {str(settings.input_format).title()} · Blender input is locked.",
        ),
    )


def _draw_import_overlay() -> None:
    """활성 Import Session의 중앙 진행창을 그립니다."""

    session = active_import_progress_session()
    if session is not None:
        draw_progress_overlay(_import_progress_overlay_view(session))


def _hide_camera_relationship_lines(context: Any) -> None:
    """Import를 실행한 Window의 모든 3D Viewport에서 관계선을 숨깁니다."""

    window = getattr(context, "window", None)
    screen = getattr(window, "screen", None)
    for area in tuple(getattr(screen, "areas", ())):
        if getattr(area, "type", "") != "VIEW_3D":
            continue
        space = getattr(getattr(area, "spaces", None), "active", None)
        overlay = getattr(space, "overlay", None)
        if overlay is None:
            continue
        try:
            overlay.show_relationship_lines = False
            area.tag_redraw()
        except (AttributeError, ReferenceError, RuntimeError, TypeError):
            continue


def _complete_import(context: Any, session: Any) -> None:
    """성공한 Import 결과를 Scene 상태와 완료 문구에 반영합니다."""

    result = session.result
    if result is None:
        raise CameraImportError("완료된 Camera Import 결과가 없습니다.")
    settings = context.scene.projection_paint
    settings.status = (
        f"Camera {result.camera_count}개, Point {result.point_count}개를 "
        "Import했습니다."
    )
    settings.status_kind = "SUCCESS"
    _hide_camera_relationship_lines(context)
    try:
        from .camera_review import initialize_camera_review

        initialize_camera_review(context)
    except Exception as exc:
        settings.status = (
            f"Camera {result.camera_count}개, Point {result.point_count}개를 "
            f"Import했지만 Camera Review 연결에 실패했습니다: {exc}"
        )
        settings.status_kind = "WARNING"
    session.stage = "DONE"


def _advance_import_session(context: Any, session: Any) -> None:
    """Core 준비, Blender Staging과 Commit을 한 단계 진행합니다."""

    if session.stage == "PREPARE_REQUEST":
        if session.request_job is None:
            prepared = session.request_factory(context)
            if isinstance(prepared, CameraImportRequest):
                session.request = prepared
                session.job = CameraImportJob(session.scene, prepared)
                session.stage = "STAGING"
            else:
                session.request_job = prepared
            return
        session.request_job.advance(
            work_budget_ms=_WORK_BUDGET_SECONDS * 1000.0,
        )
        if session.request_job.done:
            session.request = session.request_job.result
            session.job = CameraImportJob(session.scene, session.request)
            session.stage = "STAGING"
        return
    if session.stage == "STAGING":
        if session.job is None:
            raise CameraImportError("Camera Import Job이 준비되지 않았습니다.")
        if session.job.step(
            time_budget=_WORK_BUDGET_SECONDS,
            hard_cap=256,
        ):
            factory = session.request.source_validation_job_factory
            if factory is None:
                session.stage = "COMMIT"
            else:
                session.validation_job = factory()
                session.stage = "REVALIDATE"
        return
    if session.stage == "REVALIDATE":
        if session.validation_job is None:
            raise CameraImportError("Source 재검증 Job이 준비되지 않았습니다.")
        session.validation_job.advance(
            work_budget_ms=_WORK_BUDGET_SECONDS * 1000.0,
        )
        if session.validation_job.done:
            session.stage = "COMMIT"
        return
    if session.stage == "COMMIT":
        session.result = session.job.commit(
            source_prevalidated=(
                session.validation_job is not None
                and session.validation_job.done
            )
        )
        _complete_import(context, session)
        return
    if session.stage != "DONE":
        raise CameraImportError(f"지원하지 않는 Import 단계입니다: {session.stage}")


def _show_import_popup(
    context: Any,
    *,
    title: str,
    icon: str,
    lines: tuple[str, ...],
) -> None:
    """GUI 환경에서 Import 완료 또는 실패 Popup을 표시합니다."""

    if bpy.app.background or context.window is None:
        return

    def draw_message(menu: Any, _context: Any) -> None:
        """Popup 본문을 전달받은 한 줄 목록으로 구성합니다."""

        for line in lines:
            menu.layout.label(text=line)

    try:
        context.window_manager.popup_menu(draw_message, title=title, icon=icon)
    except (AttributeError, ReferenceError, RuntimeError, ValueError):
        pass


class PP_OT_import_colmap(bpy.types.Operator):
    """Core가 준비한 COLMAP Camera, Image와 Point3D를 Scene에 Import합니다."""

    bl_idname = "projection_paint.import_colmap"
    bl_label = "Import"
    bl_description = "선택한 COLMAP 모델을 검증하고 Point3D와 Camera를 장면에 구성합니다"
    bl_options = {"REGISTER", "UNDO", "BLOCKING"}

    job_id: StringProperty(options={"HIDDEN", "SKIP_SAVE"})

    @classmethod
    def poll(cls, context: Any) -> bool:
        """입력 경로가 있고 다른 Import가 없을 때만 실행을 허용합니다."""

        settings = getattr(context.scene, "projection_paint", None)
        return bool(
            _REQUEST_FACTORY is not None
            and settings is not None
            and (
                str(settings.dataset_root).strip()
                or (
                    str(settings.sparse_directory).strip()
                    and str(settings.images_directory).strip()
                )
            )
            and active_import_progress_session() is None
        )

    def _start_error(self, context: Any, exc: Exception) -> set[str]:
        """Import 시작 실패를 Scene 상태와 Operator Report에 기록합니다."""

        settings = context.scene.projection_paint
        settings.status_kind = "ERROR"
        settings.status = f"Import 실패: {exc}"
        self.report({"ERROR"}, str(exc))
        return {"CANCELLED"}

    def invoke(self, context: Any, event: Any) -> set[str]:
        """GUI에서는 Overlay와 Event Timer를 연결한 Modal Import를 시작합니다."""

        del event
        if bpy.app.background or context.window is None:
            return self.execute(context)
        try:
            session = _prepare_import_session(context)
        except Exception as exc:
            return self._start_error(context, exc)
        self.job_id = session.job_id
        overlay_handle = None
        timer = None
        try:
            overlay_handle = add_progress_overlay_draw_handler(_draw_import_overlay)
            timer = context.window_manager.event_timer_add(
                _TIMER_INTERVAL_SECONDS,
                window=context.window,
            )
            attach_import_progress_ui(
                session.job_id,
                timer=timer,
                window_manager=context.window_manager,
                overlay_handle=overlay_handle,
            )
            context.window_manager.modal_handler_add(self)
        except Exception as exc:
            attached = get_import_progress_session(session.job_id)
            if attached is not None and attached.timer is not None:
                try:
                    release_import_progress_session(session.job_id)
                except Exception:
                    pass
            else:
                if timer is not None:
                    try:
                        context.window_manager.event_timer_remove(timer)
                    except (ReferenceError, RuntimeError, ValueError):
                        pass
                remove_progress_overlay_draw_handler(overlay_handle)
                try:
                    release_import_progress_session(session.job_id)
                except Exception:
                    pass
            return self._start_error(
                context,
                RuntimeError(f"Import 진행 UI 시작 실패: {exc}"),
            )
        request_progress_overlay_redraw()
        return {"RUNNING_MODAL"}

    def execute(self, context: Any) -> set[str]:
        """Background에서는 동일한 진행 세션을 동기적으로 완료합니다."""

        try:
            session = _prepare_import_session(context)
        except Exception as exc:
            return self._start_error(context, exc)
        self.job_id = session.job_id
        try:
            while session.stage != "DONE":
                _advance_import_session(context, session)
        except Exception as exc:
            return self._fail(context, session, exc)
        release_import_progress_session(session.job_id)
        self.report({"INFO"}, context.scene.projection_paint.status)
        return {"FINISHED"}

    def _fail(
        self,
        context: Any,
        session: Any,
        exc: Exception,
    ) -> set[str]:
        """진행 중 오류를 정리하고 기존 성공 장면 보존 상태를 보고합니다."""

        cleanup_message = ""
        try:
            release_import_progress_session(session.job_id)
        except Exception as cleanup_exc:
            cleanup_message = f" | 정리 실패: {cleanup_exc}"
        settings = context.scene.projection_paint
        settings.status_kind = "ERROR"
        settings.status = (
            f"Import 실패: {exc} 기존 성공 장면은 유지되었습니다."
            f"{cleanup_message}"
        )
        _show_import_popup(
            context,
            title="COLMAP Import Failed",
            icon="ERROR",
            lines=(str(exc),),
        )
        self.report({"ERROR"}, str(exc))
        return {"CANCELLED"}

    def _finish(self, context: Any, session: Any) -> set[str]:
        """100% 완료 화면을 한 번 그린 뒤 Session을 닫고 결과를 보고합니다."""

        if not session.final_screen_shown:
            session.final_screen_shown = True
            request_progress_overlay_redraw()
            return {"RUNNING_MODAL"}
        result = session.result
        elapsed = session.elapsed_seconds
        release_import_progress_session(session.job_id)
        _show_import_popup(
            context,
            title="COLMAP Import Complete",
            icon="CHECKMARK",
            lines=(
                f"Camera: {result.camera_count}",
                f"Point3D: {result.point_count}",
                f"Elapsed: {elapsed:.1f}s",
            ),
        )
        self.report({"INFO"}, context.scene.projection_paint.status)
        return {"FINISHED"}

    def modal(self, context: Any, event: Any) -> set[str]:
        """Timer Event마다 제한된 Scene Staging 작업을 진행하고 Overlay를 갱신합니다."""

        session = get_import_progress_session(self.job_id)
        if session is None:
            return {"CANCELLED"}
        if event.type != "TIMER":
            return {"RUNNING_MODAL"}
        if session.stage == "DONE":
            return self._finish(context, session)
        try:
            _advance_import_session(context, session)
        except Exception as exc:
            return self._fail(context, session, exc)
        request_progress_overlay_redraw()
        if session.stage == "DONE":
            return self._finish(context, session)
        return {"RUNNING_MODAL"}

    def cancel(self, context: Any) -> None:
        """Blender 강제 종료 시 Import 자원을 회수하고 기존 장면을 유지합니다."""

        session = get_import_progress_session(self.job_id)
        if session is None:
            return
        try:
            release_import_progress_session(session.job_id)
        except Exception as exc:
            context.scene.projection_paint.status = (
                f"Import 강제 종료 중 Staging 정리에 실패했습니다: {exc}"
            )
            context.scene.projection_paint.status_kind = "ERROR"
            return
        context.scene.projection_paint.status = (
            "Import가 Blender에 의해 중단되었습니다. 기존 성공 장면은 유지되었습니다."
        )
        context.scene.projection_paint.status_kind = "ERROR"


CLASSES = (PP_OT_import_colmap,)
