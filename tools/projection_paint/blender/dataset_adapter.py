"""Projection Painting을 공용 COLMAP Dataset 읽기 전용 소비자로 연결합니다."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from mathutils import Matrix

try:
    from ....shared.colmap_dataset.blender.ownership import (
        RECORD_ID_KEY as SHARED_RECORD_ID_KEY,
        SOURCE_FINGERPRINT_KEY as SHARED_SOURCE_FINGERPRINT_KEY,
        is_dataset_owned,
    )
    from ....shared.colmap_dataset.blender.registry import (
        register_display_listener,
        resources as dataset_resources,
        snapshot as dataset_snapshot,
        unregister_display_listener,
        update_display_settings,
    )
except ImportError:
    from shared.colmap_dataset.blender.ownership import (
        RECORD_ID_KEY as SHARED_RECORD_ID_KEY,
        SOURCE_FINGERPRINT_KEY as SHARED_SOURCE_FINGERPRINT_KEY,
        is_dataset_owned,
    )
    from shared.colmap_dataset.blender.registry import (
        register_display_listener,
        resources as dataset_resources,
        snapshot as dataset_snapshot,
        unregister_display_listener,
        update_display_settings,
    )

from .camera_scene import (
    MANIFEST_SCHEMA,
    RESOURCE_OWNER_SHARED_DATASET,
    CameraDataManifest,
    CameraManifest,
    CameraManifestEntry,
    CameraSceneError,
    clear_camera_runtime,
    detach_projection_markers,
    install_camera_runtime,
    manifest_to_json,
    scene_manifest,
    shared_camera_data_uuid,
    shared_camera_uuid,
)
from .ownership import DATASET_REVISION_KEY, DATASET_UUID_KEY, MANIFEST_KEY


class ProjectionDatasetError(RuntimeError):
    """공용 Dataset을 PP Runtime으로 읽는 과정의 검증 실패를 나타냅니다."""


_DISPLAY_SYNC_IN_PROGRESS = False


def _shared_property_available(scene: Any) -> bool:
    """Root 수명주기가 Scene.colmap_dataset을 등록했는지 확인합니다."""

    return bool(scene is not None and getattr(scene, "colmap_dataset", None) is not None)


def shared_snapshot(scene: Any):
    """등록된 공용 Dataset Snapshot을 반환하고 없으면 None을 반환합니다."""

    if not _shared_property_available(scene):
        return None
    try:
        return dataset_snapshot(scene)
    except (AttributeError, ReferenceError, RuntimeError, TypeError, ValueError):
        return None


def shared_dataset_available(scene: Any) -> bool:
    """PP가 소비할 최소 공용 Dataset 식별자와 Camera 수량이 있는지 반환합니다."""

    current = shared_snapshot(scene)
    return bool(current is not None and current.available and current.image_count > 0)


def shared_dataset_is_bound(scene: Any) -> bool:
    """PP 영구 Manifest가 현재 공용 UUID와 Revision에 정확히 바인딩됐는지 확인합니다."""

    current = shared_snapshot(scene)
    settings = getattr(scene, "projection_paint", None)
    if current is None or settings is None or not current.available:
        return False
    try:
        manifest = scene_manifest(scene)
    except CameraSceneError:
        return False
    return bool(
        manifest is not None
        and manifest.resource_owner == RESOURCE_OWNER_SHARED_DATASET
        and manifest.dataset_uuid == current.dataset_uuid
        and manifest.dataset_revision == current.dataset_revision
        and manifest.source_fingerprint == current.source_fingerprint
        and str(settings.dataset_uuid) == current.dataset_uuid
        and int(settings.dataset_revision) == current.dataset_revision
        and int(settings.display_revision) == current.display_revision
        and str(settings.source_fingerprint) == current.source_fingerprint
    )


def _float_tuple(value: Any, length: int, label: str) -> tuple[float, ...]:
    """Blender ID 배열을 지정 길이의 유한 실수 Tuple로 검증합니다."""

    try:
        result = tuple(float(item) for item in value)
    except (TypeError, ValueError) as exc:
        raise ProjectionDatasetError(f"{label} Metadata가 올바르지 않습니다.") from exc
    if len(result) != length or not all(math.isfinite(item) for item in result):
        raise ProjectionDatasetError(f"{label} Metadata 차원 또는 값이 올바르지 않습니다.")
    return result


def _flat_matrix(matrix: Matrix) -> tuple[float, ...]:
    """공용 Camera의 4×4 Local Matrix를 행 우선 Tuple로 고정합니다."""

    values = tuple(float(matrix[row][column]) for row in range(4) for column in range(4))
    if not all(math.isfinite(value) for value in values):
        raise ProjectionDatasetError("공용 Camera Transform에 NaN 또는 Inf가 있습니다.")
    return values


def _image_paths(current: Any) -> dict[int, Path]:
    """공용 Image Path Manifest를 IMAGE_ID별 검증된 절대경로로 변환합니다."""

    try:
        raw = json.loads(str(current.image_path_manifest_json))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ProjectionDatasetError("공용 Image Path Manifest가 손상되었습니다.") from exc
    if isinstance(raw, dict):
        entries = tuple(raw.values())
    elif isinstance(raw, list):
        entries = tuple(raw)
    else:
        raise ProjectionDatasetError("공용 Image Path Manifest 형식이 올바르지 않습니다.")
    result: dict[int, Path] = {}
    for item in entries:
        if not isinstance(item, dict):
            raise ProjectionDatasetError("공용 Image Path 항목 형식이 올바르지 않습니다.")
        try:
            image_id = int(item["image_id"])
            status = str(item["path_status"])
            path = Path(str(item["resolved_image_path"])).expanduser().resolve()
        except (KeyError, OSError, TypeError, ValueError) as exc:
            raise ProjectionDatasetError("공용 Image Path 항목이 손상되었습니다.") from exc
        if image_id <= 0 or image_id in result or status not in {"EXACT", "FALLBACK"}:
            raise ProjectionDatasetError("공용 Image Path IMAGE_ID 또는 상태가 올바르지 않습니다.")
        if not path.is_absolute():
            raise ProjectionDatasetError(f"IMAGE_ID {image_id} 경로가 절대경로가 아닙니다.")
        result[image_id] = path
    return result

def _camera_metadata(camera: Any, image_id: int, current: Any) -> tuple[Any, ...]:
    """공용 Camera Object/Data의 IMAGE·Pose·Pinhole Intrinsics를 검증합니다."""

    data = getattr(camera, "data", None)
    if not is_dataset_owned(
        camera,
        dataset_uuid=current.dataset_uuid,
        role="CAMERA",
        record_id=image_id,
    ) or not is_dataset_owned(
        data,
        dataset_uuid=current.dataset_uuid,
        role="CAMERA_DATA",
        record_id=image_id,
    ):
        raise ProjectionDatasetError(f"IMAGE_ID {image_id} 공용 Camera 소유권이 다릅니다.")
    if (
        str(camera.get(SHARED_SOURCE_FINGERPRINT_KEY, ""))
        != current.source_fingerprint
        or str(data.get(SHARED_SOURCE_FINGERPRINT_KEY, ""))
        != current.source_fingerprint
    ):
        raise ProjectionDatasetError(f"IMAGE_ID {image_id} Source 지문이 다릅니다.")
    try:
        metadata_image_id = int(camera.get("colmap_image_id_text", 0))
        camera_id = int(camera.get("colmap_camera_id_text", 0))
        image_name = str(camera.get("colmap_image_name", "") or "")
        qvec = _float_tuple(camera.get("colmap_qvec", ()), 4, "qvec")
        tvec = _float_tuple(camera.get("colmap_tvec", ()), 3, "tvec")
        model = str(data.get("colmap_model", "") or "").upper()
        width = int(data.get("colmap_width", 0))
        height = int(data.get("colmap_height", 0))
        params = tuple(
            float(value)
            for value in json.loads(str(data.get("colmap_params_json", "[]")))
        )
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ProjectionDatasetError(
            f"IMAGE_ID {image_id} Modifier Camera Metadata가 손상되었습니다."
        ) from exc
    if metadata_image_id != image_id or camera_id <= 0 or not image_name:
        raise ProjectionDatasetError(f"IMAGE_ID {image_id} Image Metadata가 올바르지 않습니다.")
    if model not in {"PINHOLE", "SIMPLE_PINHOLE"}:
        raise ProjectionDatasetError(
            f"Projection Painting에서 지원하지 않는 Camera Model입니다: {model or '-'}"
        )
    expected_params = 4 if model == "PINHOLE" else 3
    if (
        width <= 0
        or height <= 0
        or len(params) != expected_params
        or not all(math.isfinite(value) for value in params)
    ):
        raise ProjectionDatasetError(f"IMAGE_ID {image_id} Camera Intrinsics가 올바르지 않습니다.")
    if model == "PINHOLE":
        fx, fy, cx, cy = params
    else:
        fx, cx, cy = params
        fy = fx
    if fx <= 0.0 or fy <= 0.0 or getattr(data, "type", "") != "PERSP":
        raise ProjectionDatasetError(f"IMAGE_ID {image_id} Pinhole 초점거리가 올바르지 않습니다.")
    expected_lens = 36.0 * fx / width
    expected_shift_x = 0.5 - cx / width
    expected_shift_y = (cy - height * 0.5) / width * (fx / fy)
    actual = (
        float(data.lens),
        float(data.shift_x),
        float(data.shift_y),
        float(data.sensor_width),
    )
    expected = (expected_lens, expected_shift_x, expected_shift_y, 36.0)
    if any(abs(left - right) > 1.0e-5 for left, right in zip(actual, expected)):
        raise ProjectionDatasetError(f"IMAGE_ID {image_id} Blender Camera Intrinsics가 다릅니다.")
    if str(data.sensor_fit) != "HORIZONTAL":
        raise ProjectionDatasetError(f"IMAGE_ID {image_id} Camera Sensor Fit이 다릅니다.")
    return data, camera_id, image_name, qvec, tvec, model, width, height, params


def build_shared_camera_manifest(scene: Any) -> tuple[CameraManifest, dict[int, Path]]:
    """공용 Snapshot과 동일 Blender Camera Pointer로 PP Camera Manifest를 구축합니다."""

    current = shared_snapshot(scene)
    if current is None or not current.available:
        raise ProjectionDatasetError("COLMAP Modifier에서 데이터를 먼저 불러오세요.")
    resources = dataset_resources(scene)
    if resources is None or not resources.camera_objects:
        raise ProjectionDatasetError("공용 Dataset에 Projection Painting Camera가 없습니다.")
    image_paths = _image_paths(current)
    camera_ids = set(resources.camera_objects)
    if image_paths.keys() != camera_ids:
        raise ProjectionDatasetError("공용 Camera와 Image Path의 IMAGE_ID가 일치하지 않습니다.")
    settings = getattr(scene, "projection_paint", None)
    frame_start = int(getattr(settings, "frame_start", 1))
    data_entries: list[CameraDataManifest] = []
    camera_entries: list[CameraManifestEntry] = []
    for offset, (image_id, camera) in enumerate(sorted(resources.camera_objects.items())):
        (
            data,
            camera_id,
            image_name,
            qvec,
            tvec,
            model,
            width,
            height,
            params,
        ) = _camera_metadata(camera, image_id, current)
        if getattr(camera, "parent", None) is not resources.axis_root:
            raise ProjectionDatasetError(f"IMAGE_ID {image_id} Axis Root 연결이 다릅니다.")
        data_entries.append(
            CameraDataManifest(
                camera_id=camera_id,
                owned_uuid=shared_camera_data_uuid(image_id),
                model_name=model,
                width=width,
                height=height,
                params=params,
                lens=float(data.lens),
                shift_x=float(data.shift_x),
                shift_y=float(data.shift_y),
                sensor_width=float(data.sensor_width),
                sensor_fit=str(data.sensor_fit),
            )
        )
        frame = frame_start + offset
        camera_entries.append(
            CameraManifestEntry(
                image_id=image_id,
                camera_id=camera_id,
                frame=frame,
                image_name=image_name,
                camera_uuid=shared_camera_uuid(image_id),
                marker_name=(
                    f"PP_CAM_{current.dataset_uuid[:8]}_{frame}_{image_id}"
                ),
                matrix_local=_flat_matrix(camera.matrix_basis),
                qvec=qvec,
                tvec=tvec,
            )
        )
    manifest = CameraManifest(
        schema=MANIFEST_SCHEMA,
        dataset_uuid=current.dataset_uuid,
        dataset_revision=current.dataset_revision,
        source_fingerprint=current.source_fingerprint,
        collection_uuid="shared-dataset-root",
        axis_root_uuid="shared-dataset-axis-root",
        frame_start=frame_start,
        frame_end=frame_start + len(camera_entries) - 1,
        camera_data=tuple(data_entries),
        entries=tuple(camera_entries),
        resource_owner=RESOURCE_OWNER_SHARED_DATASET,
    )
    return manifest, image_paths


def _marker_manifest_json(manifest: CameraManifest) -> str:
    """PP UI 복구용 Marker 목록을 결정적인 IMAGE_ID 순서로 직렬화합니다."""

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


def _release_dataset_links(scene: Any) -> None:
    """Dataset 교체 전에 PP Marker·Camera Review·Stencil Source 연결만 해제합니다."""

    try:
        detach_projection_markers(scene)
    except (CameraSceneError, RuntimeError, ValueError):
        pass
    from .camera_review import release_camera_review_links
    from .paint_runtime import stop_paint_runtime
    from .stencil_session import detach_source_image

    settings = getattr(scene, "projection_paint", None)
    source_image = getattr(settings, "stencil_source_image", None)
    stencil_texture = getattr(settings, "stencil_texture", None)
    stop_paint_runtime(scene=scene, restore_resolution=False)
    if source_image is not None:
        detach_source_image(source_image)
        if (
            stencil_texture is not None
            and getattr(stencil_texture, "image", None) is source_image
        ):
            stencil_texture.image = None
        if getattr(settings, "stencil_source_image", None) is source_image:
            settings.stencil_source_image = None
            settings.active_image_id = 0
    release_camera_review_links(scene)
    clear_camera_runtime()


def _write_binding(scene: Any, manifest: CameraManifest, image_paths: dict[int, Path]) -> None:
    """검증된 공용 Manifest와 Revision을 PP 영구 상태에 한 번 기록합니다."""

    global _DISPLAY_SYNC_IN_PROGRESS
    current = dataset_snapshot(scene)
    settings = scene.projection_paint
    manifest_json = manifest_to_json(manifest)
    paths_json = json.dumps(
        {str(image_id): str(path) for image_id, path in sorted(image_paths.items())},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    _DISPLAY_SYNC_IN_PROGRESS = True
    try:
        settings.dataset_uuid = current.dataset_uuid
        settings.dataset_revision = current.dataset_revision
        settings.display_revision = current.display_revision
        settings.source_fingerprint = current.source_fingerprint
        settings.dataset_manifest_json = manifest_json
        settings.image_path_manifest_json = paths_json
        settings.marker_manifest_json = _marker_manifest_json(manifest)
        settings.sparse_directory = current.model_directory
        settings.images_directory = current.images_directory
        settings.point_size = current.point_size
        settings.camera_frustum_size = current.camera_frustum_size
        settings.camera_axis_preset = current.axis_preset
        settings.camera_forward = current.axis_forward
        settings.camera_up = current.axis_up
    finally:
        _DISPLAY_SYNC_IN_PROGRESS = False
    scene[MANIFEST_KEY] = manifest_json
    scene[DATASET_UUID_KEY] = manifest.dataset_uuid
    scene[DATASET_REVISION_KEY] = manifest.dataset_revision


def bind_shared_dataset(scene: Any, reason: str = "") -> CameraManifest:
    """현재 공용 Dataset으로 PP Runtime을 재바인딩하고 Paint 자산은 보존합니다."""

    del reason
    if not shared_dataset_available(scene):
        raise ProjectionDatasetError("COLMAP Modifier에서 데이터를 먼저 불러오세요.")
    manifest, image_paths = build_shared_camera_manifest(scene)
    settings = getattr(scene, "projection_paint", None)
    if settings is None:
        raise ProjectionDatasetError("Projection Paint Scene 설정이 없습니다.")
    previous_uuid = str(settings.dataset_uuid or "")
    previous_revision = int(settings.dataset_revision)
    current_image_text = str(settings.current_image_id or "")
    previous_image_id = (
        int(current_image_text)
        if current_image_text.isdigit()
        else int(settings.active_image_id)
    )
    if previous_uuid != manifest.dataset_uuid or previous_revision != manifest.dataset_revision:
        _release_dataset_links(scene)
    else:
        clear_camera_runtime()
    _write_binding(scene, manifest, image_paths)
    entries_by_image = {entry.image_id: entry for entry in manifest.entries}
    active_entry = entries_by_image.get(previous_image_id, manifest.entries[0])
    scene.frame_start = manifest.frame_start
    scene.frame_end = manifest.frame_end
    runtime = install_camera_runtime(scene, manifest)
    scene.frame_set(active_entry.frame)
    from .camera_scene import (
        apply_projection_camera_state,
        projection_camera_session_is_active,
    )

    if projection_camera_session_is_active(scene):
        apply_projection_camera_state(
            scene,
            runtime.objects[active_entry.camera_uuid],
        )
    settings.current_image_id = str(active_entry.image_id)
    settings.current_image_name = active_entry.image_name
    settings.status = (
        f"공용 Dataset 연결: Camera {len(manifest.entries)}, "
        f"Revision {manifest.dataset_revision}"
    )
    settings.status_kind = "SUCCESS"
    return manifest


def synchronize_display_from_legacy(
    settings: Any,
    context: Any,
    *field_names: str,
) -> None:
    """Legacy PP 표시 속성 변경을 공용 표시 계약의 단일 갱신 경로로 전달합니다."""

    if _DISPLAY_SYNC_IN_PROGRESS:
        return
    scene = getattr(settings, "id_data", None) or getattr(context, "scene", None)
    current = shared_snapshot(scene)
    if (
        current is None
        or not current.available
        or str(settings.dataset_uuid) != current.dataset_uuid
    ):
        return
    mapping = {
        "point_size": "point_size",
        "camera_frustum_size": "camera_frustum_size",
        "camera_axis_preset": "axis_preset",
        "camera_forward": "axis_forward",
        "camera_up": "axis_up",
    }
    changes = {
        mapping[field_name]: getattr(settings, field_name)
        for field_name in field_names
        if field_name in mapping
    }
    if "camera_frustum_size" in changes and float(changes["camera_frustum_size"]) <= 0.0:
        return
    update_display_settings(scene, **changes)


def _apply_shared_display(scene: Any, current: Any, changed_fields: frozenset[str]) -> None:
    """공용 표시 변경을 PP Legacy 표시값과 저장 Revision에 읽기 전용 반영합니다."""

    global _DISPLAY_SYNC_IN_PROGRESS
    del current, changed_fields
    settings = getattr(scene, "projection_paint", None)
    if settings is None:
        return
    try:
        current = dataset_snapshot(scene)
        if (
            str(settings.dataset_uuid) != current.dataset_uuid
            or int(settings.dataset_revision) != current.dataset_revision
        ):
            bind_shared_dataset(scene, "dataset_listener")
            settings = scene.projection_paint
        _DISPLAY_SYNC_IN_PROGRESS = True
        settings.display_revision = current.display_revision
        settings.point_size = current.point_size
        settings.camera_frustum_size = current.camera_frustum_size
        settings.camera_axis_preset = current.axis_preset
        settings.camera_forward = current.axis_forward
        settings.camera_up = current.axis_up
    except (CameraSceneError, ProjectionDatasetError, RuntimeError, ValueError) as exc:
        settings.status = f"공용 Dataset 갱신 실패: {exc}"
        settings.status_kind = "ERROR"
    finally:
        _DISPLAY_SYNC_IN_PROGRESS = False


def rebuild_shared_dataset_binding(scene: Any, reason: str, _epoch: int) -> None:
    """등록·Load·Undo·Redo에서 현재 공용 Dataset으로 PP Manifest를 재구축합니다."""

    if scene is None:
        return
    settings = getattr(scene, "projection_paint", None)
    if settings is None:
        return
    if not shared_dataset_available(scene):
        settings.status = "COLMAP Modifier에서 데이터를 먼저 불러오세요."
        settings.status_kind = "READY"
        return
    try:
        bind_shared_dataset(scene, reason)
    except (CameraSceneError, ProjectionDatasetError, RuntimeError, ValueError) as exc:
        settings.status = f"공용 Dataset 연결 실패: {exc}"
        settings.status_kind = "ERROR"


def register_projection_dataset_adapter() -> None:
    """공용 표시 Listener와 PP 수명주기 Rebuild Hook을 등록합니다."""

    from .lifecycle import lifecycle_registry

    register_display_listener("projection_paint", _apply_shared_display)
    lifecycle_registry().add_rebuild_hook(rebuild_shared_dataset_binding)


def unregister_projection_dataset_adapter() -> None:
    """PP가 등록한 공용 Dataset Listener와 Rebuild Hook만 해제합니다."""

    from .lifecycle import lifecycle_registry

    unregister_display_listener("projection_paint")
    lifecycle_registry().remove_rebuild_hook(rebuild_shared_dataset_binding)


__all__ = [
    "ProjectionDatasetError",
    "bind_shared_dataset",
    "build_shared_camera_manifest",
    "rebuild_shared_dataset_binding",
    "register_projection_dataset_adapter",
    "shared_dataset_available",
    "shared_dataset_is_bound",
    "shared_snapshot",
    "synchronize_display_from_legacy",
    "unregister_projection_dataset_adapter",
]
