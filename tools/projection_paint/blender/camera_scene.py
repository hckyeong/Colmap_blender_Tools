"""COLMAP Camera Scene 데이터와 Marker Manifest 및 복구 Guard를 관리합니다."""

from __future__ import annotations

from collections import deque
from dataclasses import asdict, dataclass
import json
import math
from time import perf_counter
from typing import Any, Iterable, Mapping

import bpy
from mathutils import Matrix, Quaternion, Vector

from .camera_display import (
    DEFAULT_CAMERA_FRUSTUM_SIZE,
    apply_camera_frustum_size,
    apply_camera_frustum_visibility_to_object,
    camera_display_size_from_frustum_size,
    camera_frustum_size_from_scene,
)
from .lifecycle import EpochToken, lifecycle_registry
from .ownership import (
    DATASET_REVISION_KEY,
    DATASET_UUID_KEY,
    MANIFEST_KEY,
    RECORD_ID_KEY,
    UUID_KEY,
    ensure_scene_uuid,
    is_owned,
    owned_uuid_of,
    resolve_owned,
    tag_owned,
)


MANIFEST_SCHEMA = 1
REPAIR_INTERVAL = 0.30
REPAIR_TIME_BUDGET = 0.001
MAX_REPAIR_ATTEMPTS = 4


class CameraSceneError(RuntimeError):
    """Camera Scene 계약 위반 또는 복구 불가 상태를 나타냅니다."""


@dataclass(frozen=True, slots=True)
class CameraDataManifest:
    """CAMERA_ID별 공유 Camera Data 복구 값을 보관합니다."""

    camera_id: int
    owned_uuid: str
    model_name: str
    width: int
    height: int
    params: tuple[float, ...]
    lens: float
    shift_x: float
    shift_y: float
    sensor_width: float
    sensor_fit: str


@dataclass(frozen=True, slots=True)
class CameraManifestEntry:
    """IMAGE_ID별 Camera Object와 Timeline Marker 복구 값을 보관합니다."""

    image_id: int
    camera_id: int
    frame: int
    image_name: str
    camera_uuid: str
    marker_name: str
    matrix_local: tuple[float, ...]
    qvec: tuple[float, ...]
    tvec: tuple[float, ...]


@dataclass(frozen=True, slots=True)
class CameraManifest:
    """Scene에 영구 저장할 관리 Camera Dataset Manifest입니다."""

    schema: int
    dataset_uuid: str
    dataset_revision: int
    source_fingerprint: str
    collection_uuid: str
    axis_root_uuid: str
    frame_start: int
    frame_end: int
    camera_data: tuple[CameraDataManifest, ...]
    entries: tuple[CameraManifestEntry, ...]


@dataclass(slots=True)
class CameraRuntime:
    """현재 Epoch에서만 유효한 Camera와 Marker 직접 참조를 보관합니다."""

    scene: Any
    manifest: CameraManifest
    token: EpochToken
    objects: dict[str, Any]
    camera_data: dict[str, Any]
    markers: dict[int, Any | None]
    entries_by_frame: dict[int, CameraManifestEntry]
    entries_by_camera_uuid: dict[str, CameraManifestEntry]
    data_by_uuid: dict[str, CameraDataManifest]
    data_by_camera_id: dict[int, CameraDataManifest]
    dirty_objects: deque[str]
    dirty_data: deque[str]
    queued: set[tuple[str, str]]
    marker_cursor: int = 0
    pending_frame: int | None = None
    repair_depth: int = 0
    repair_attempts: int = 0
    error: str = ""


_RUNTIMES: dict[int, CameraRuntime] = {}
_MARKER_TOOL_ID = "projection_paint"


def _marker_session_is_active(scene: Any) -> bool:
    """현재 Scene의 Marker 사용권을 Projection Paint가 소유하는지 반환합니다."""

    try:
        from ....shared.blender_runtime.scene_marker_session import (
            scene_marker_session_owned_by,
        )
    except ImportError:
        from shared.blender_runtime.scene_marker_session import (
            scene_marker_session_owned_by,
        )

    return bool(scene_marker_session_owned_by(scene, _MARKER_TOOL_ID))


def _field(value: Any, name: str, default: Any = None) -> Any:
    """Mapping과 일반 객체에서 같은 이름의 필드를 읽습니다."""

    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _pointer(value: Any) -> int:
    """유효한 RNA 객체의 포인터를 반환하고 실패하면 0을 반환합니다."""

    try:
        return int(value.as_pointer()) if value is not None else 0
    except (AttributeError, ReferenceError, RuntimeError):
        return 0


def _flat_matrix(matrix: Matrix) -> tuple[float, ...]:
    """4×4 Matrix를 행 우선 16개 실수로 직렬화합니다."""

    return tuple(float(matrix[row][column]) for row in range(4) for column in range(4))


def _matrix_from_flat(values: Iterable[float]) -> Matrix:
    """행 우선 16개 실수에서 4×4 Matrix를 복원합니다."""

    items = tuple(float(value) for value in values)
    if len(items) != 16 or not all(math.isfinite(value) for value in items):
        raise CameraSceneError("Camera Matrix가 유효하지 않습니다.")
    return Matrix(tuple(items[index : index + 4] for index in range(0, 16, 4)))


def colmap_camera_matrix(qvec: Iterable[float], tvec: Iterable[float]) -> Matrix:
    """COLMAP World-to-Camera Pose를 Blender Camera Matrix로 변환합니다."""

    quaternion_values = tuple(float(value) for value in qvec)
    translation_values = tuple(float(value) for value in tvec)
    if len(quaternion_values) != 4 or len(translation_values) != 3:
        raise CameraSceneError("COLMAP Pose 차원이 올바르지 않습니다.")
    if not all(math.isfinite(value) for value in quaternion_values + translation_values):
        raise CameraSceneError("COLMAP Pose에 NaN 또는 Inf가 있습니다.")
    quaternion = Quaternion(quaternion_values)
    if quaternion.magnitude <= 1.0e-12:
        raise CameraSceneError("COLMAP Quaternion 크기가 0입니다.")
    world_to_camera = quaternion.normalized().to_matrix()
    camera_to_world = world_to_camera.transposed()
    conversion = Matrix(
        ((1.0, 0.0, 0.0), (0.0, -1.0, 0.0), (0.0, 0.0, -1.0))
    )
    matrix = (camera_to_world @ conversion).to_4x4()
    matrix.translation = -(camera_to_world @ Vector(translation_values))
    return matrix


def pinhole_values(record: Any) -> tuple[str, int, int, tuple[float, ...]]:
    """지원 Camera Record에서 모델·해상도·파라미터를 검증해 반환합니다."""

    model = str(
        _field(record, "model_name", _field(record, "model", "")) or ""
    ).upper()
    width = int(_field(record, "width", 0))
    height = int(_field(record, "height", 0))
    params = tuple(float(value) for value in _field(record, "params", ()))
    if model not in {"PINHOLE", "SIMPLE_PINHOLE"}:
        raise CameraSceneError(f"지원하지 않는 Camera Model입니다: {model}")
    expected = 4 if model == "PINHOLE" else 3
    if width <= 0 or height <= 0 or len(params) != expected:
        raise CameraSceneError("Camera 해상도 또는 파라미터 수가 올바르지 않습니다.")
    if not all(math.isfinite(value) for value in params):
        raise CameraSceneError("Camera 파라미터에 NaN 또는 Inf가 있습니다.")
    fx = params[0]
    fy = params[1] if model == "PINHOLE" else params[0]
    if fx <= 0.0 or fy <= 0.0:
        raise CameraSceneError("Camera 초점거리는 0보다 커야 합니다.")
    return model, width, height, params


def configure_camera_data(camera_data: Any, record: Any) -> CameraDataManifest:
    """Blender Camera Data에 검증된 Pinhole Intrinsics를 설정합니다."""

    model, width, height, params = pinhole_values(record)
    camera_id = int(_field(record, "camera_id"))
    if model == "PINHOLE":
        fx, fy, cx, cy = params
    else:
        fx, cx, cy = params
        fy = fx
    camera_data.type = "PERSP"
    camera_data.sensor_fit = "HORIZONTAL"
    camera_data.sensor_width = 36.0
    camera_data.lens = 36.0 * fx / width
    camera_data.shift_x = 0.5 - cx / width
    camera_data.shift_y = (cy - height * 0.5) / width * (fx / fy)
    camera_data["pp_camera_model"] = model
    camera_data["pp_camera_width"] = width
    camera_data["pp_camera_height"] = height
    camera_data["pp_camera_params"] = list(params)
    return CameraDataManifest(
        camera_id=camera_id,
        owned_uuid=owned_uuid_of(camera_data),
        model_name=model,
        width=width,
        height=height,
        params=params,
        lens=float(camera_data.lens),
        shift_x=float(camera_data.shift_x),
        shift_y=float(camera_data.shift_y),
        sensor_width=float(camera_data.sensor_width),
        sensor_fit=str(camera_data.sensor_fit),
    )


def create_camera_data(
    record: Any,
    dataset_uuid: str,
    camera_frustum_size: float = DEFAULT_CAMERA_FRUSTUM_SIZE,
) -> tuple[Any, CameraDataManifest]:
    """CAMERA_ID 하나의 공유 Blender Camera Data를 직접 생성합니다."""

    camera_id = int(_field(record, "camera_id"))
    camera_data = bpy.data.cameras.new(f"PP_CAMERA_DATA_{camera_id}")
    tag_owned(
        camera_data,
        "CAMERA_DATA",
        dataset_uuid=dataset_uuid,
        record_id=camera_id,
    )
    try:
        manifest = configure_camera_data(camera_data, record)
        camera_data.display_size = camera_display_size_from_frustum_size(
            camera_frustum_size
        )
    except Exception:
        bpy.data.cameras.remove(camera_data)
        raise
    return camera_data, manifest


def create_camera_object(
    image: Any,
    camera_data: Any,
    axis_root: Any,
    collection: Any,
    dataset_uuid: str,
) -> tuple[Any, tuple[float, ...], tuple[float, ...]]:
    """IMAGE_ID 하나의 잠긴 Camera Object를 직접 생성합니다."""

    image_id = int(_field(image, "image_id"))
    camera_id = int(_field(image, "camera_id"))
    qvec = tuple(float(value) for value in _field(image, "qvec", ()))
    tvec = tuple(float(value) for value in _field(image, "tvec", ()))
    matrix = colmap_camera_matrix(qvec, tvec)
    camera_object = bpy.data.objects.new(f"PP_IMAGE_{image_id}", camera_data)
    tag_owned(
        camera_object,
        "CAMERA_OBJECT",
        dataset_uuid=dataset_uuid,
        record_id=image_id,
    )
    camera_object["pp_camera_id"] = str(camera_id)
    camera_object["pp_image_name"] = str(
        _field(image, "name", _field(image, "image_name", ""))
    )
    camera_object["pp_qvec"] = list(qvec)
    camera_object["pp_tvec"] = list(tvec)
    camera_object.parent = axis_root
    camera_object.matrix_parent_inverse = Matrix.Identity(4)
    camera_object.matrix_basis = matrix
    camera_object.hide_select = True
    camera_object.lock_location = (True, True, True)
    camera_object.lock_rotation = (True, True, True)
    camera_object.lock_scale = (True, True, True)
    collection.objects.link(camera_object)
    return camera_object, qvec, tvec


def manifest_to_json(manifest: CameraManifest) -> str:
    """Camera Manifest를 결정적인 UTF-8 JSON 문자열로 직렬화합니다."""

    return json.dumps(asdict(manifest), ensure_ascii=False, separators=(",", ":"))


def manifest_from_json(payload: str) -> CameraManifest:
    """저장 JSON을 검증된 Camera Manifest로 역직렬화합니다."""

    try:
        raw = json.loads(str(payload))
        camera_data = tuple(
            CameraDataManifest(
                camera_id=int(item["camera_id"]),
                owned_uuid=str(item["owned_uuid"]),
                model_name=str(item["model_name"]),
                width=int(item["width"]),
                height=int(item["height"]),
                params=tuple(float(value) for value in item["params"]),
                lens=float(item["lens"]),
                shift_x=float(item["shift_x"]),
                shift_y=float(item["shift_y"]),
                sensor_width=float(item["sensor_width"]),
                sensor_fit=str(item["sensor_fit"]),
            )
            for item in raw["camera_data"]
        )
        entries = tuple(
            CameraManifestEntry(
                image_id=int(item["image_id"]),
                camera_id=int(item["camera_id"]),
                frame=int(item["frame"]),
                image_name=str(item["image_name"]),
                camera_uuid=str(item["camera_uuid"]),
                marker_name=str(item["marker_name"]),
                matrix_local=tuple(float(value) for value in item["matrix_local"]),
                qvec=tuple(float(value) for value in item["qvec"]),
                tvec=tuple(float(value) for value in item["tvec"]),
            )
            for item in raw["entries"]
        )
        manifest = CameraManifest(
            schema=int(raw["schema"]),
            dataset_uuid=str(raw["dataset_uuid"]),
            dataset_revision=int(raw["dataset_revision"]),
            source_fingerprint=str(raw["source_fingerprint"]),
            collection_uuid=str(raw["collection_uuid"]),
            axis_root_uuid=str(raw["axis_root_uuid"]),
            frame_start=int(raw["frame_start"]),
            frame_end=int(raw["frame_end"]),
            camera_data=camera_data,
            entries=entries,
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise CameraSceneError(f"Camera Manifest가 손상되었습니다: {exc}") from exc
    if manifest.schema != MANIFEST_SCHEMA or not manifest.dataset_uuid:
        raise CameraSceneError("Camera Manifest Schema 또는 Dataset UUID가 올바르지 않습니다.")
    if len({entry.image_id for entry in manifest.entries}) != len(manifest.entries):
        raise CameraSceneError("Camera Manifest IMAGE_ID가 중복되었습니다.")
    return manifest


def scene_manifest(scene: Any) -> CameraManifest | None:
    """Scene에 실제 존재하는 권위 Camera Manifest를 반환합니다."""

    try:
        payload = str(scene.get(MANIFEST_KEY, "") or "")
    except (AttributeError, ReferenceError, RuntimeError):
        return None
    return manifest_from_json(payload) if payload else None


def managed_marker_candidates(scene: Any, manifest: CameraManifest) -> set[int]:
    """현재 Manifest와 정확히 일치하는 관리 Marker 포인터를 반환합니다."""

    object_by_uuid = {
        owned_uuid_of(obj): obj
        for obj in scene.objects
        if is_owned(obj, kind="CAMERA_OBJECT", dataset_uuid=manifest.dataset_uuid)
    }
    expected = {
        (
            entry.marker_name,
            entry.frame,
            _pointer(object_by_uuid.get(entry.camera_uuid)),
        )
        for entry in manifest.entries
    }
    return {
        _pointer(marker)
        for marker in scene.timeline_markers
        if (
            str(marker.name),
            int(marker.frame),
            _pointer(getattr(marker, "camera", None)),
        )
        in expected
    }


def projection_markers_attached(scene: Any) -> bool:
    """Manifest의 모든 Projection Marker가 정확히 한 개씩 연결됐는지 검증합니다."""

    manifest = scene_manifest(scene)
    if manifest is None:
        return _marker_session_is_active(scene)
    object_by_uuid = {
        owned_uuid_of(obj): obj
        for obj in scene.objects
        if is_owned(obj, kind="CAMERA_OBJECT", dataset_uuid=manifest.dataset_uuid)
    }
    if any(entry.camera_uuid not in object_by_uuid for entry in manifest.entries):
        return False
    expected = {
        (entry.marker_name, entry.frame, _pointer(object_by_uuid[entry.camera_uuid]))
        for entry in manifest.entries
    }
    actual = [
        (str(marker.name), int(marker.frame), _pointer(getattr(marker, "camera", None)))
        for marker in scene.timeline_markers
        if (
            str(marker.name),
            int(marker.frame),
            _pointer(getattr(marker, "camera", None)),
        )
        in expected
    ]
    return bool(
        _marker_session_is_active(scene)
        and len(actual) == len(manifest.entries)
        and len(set(actual)) == len(manifest.entries)
    )


def projection_markers_detached(scene: Any) -> bool:
    """Manifest와 정확히 일치하는 Projection Marker가 모두 제거됐는지 반환합니다."""

    manifest = scene_manifest(scene)
    return manifest is None or not managed_marker_candidates(scene, manifest)


def detach_projection_markers(scene: Any) -> None:
    """정확히 검증된 Projection Marker만 원자적으로 제거합니다."""

    manifest = scene_manifest(scene)
    if manifest is None:
        return
    runtime = runtime_for_scene(scene)
    candidates = managed_marker_candidates(scene, manifest)
    snapshots: list[tuple[int, str, int, Any]] = []
    for marker in tuple(scene.timeline_markers):
        if _pointer(marker) not in candidates:
            continue
        entry = next(
            (
                item
                for item in manifest.entries
                if item.marker_name == str(marker.name)
                and item.frame == int(marker.frame)
                and item.camera_uuid == owned_uuid_of(getattr(marker, "camera", None))
            ),
            None,
        )
        if entry is not None:
            snapshots.append((entry.image_id, str(marker.name), int(marker.frame), marker.camera))
    removed: list[tuple[int, str, int, Any]] = []
    try:
        for snapshot in snapshots:
            marker = next(
                item
                for item in tuple(scene.timeline_markers)
                if _pointer(item) in candidates
                and str(item.name) == snapshot[1]
                and int(item.frame) == snapshot[2]
                and item.camera is snapshot[3]
            )
            scene.timeline_markers.remove(marker)
            removed.append(snapshot)
        if any(_pointer(marker) in candidates for marker in scene.timeline_markers):
            raise CameraSceneError("Projection Marker를 완전히 분리하지 못했습니다.")
    except Exception as exc:
        for image_id, name, frame, camera in removed:
            marker = scene.timeline_markers.new(name, frame=frame)
            marker.camera = camera
            if runtime is not None:
                runtime.markers[image_id] = marker
        raise CameraSceneError(f"Projection Marker 분리 실패: {exc}") from exc
    if runtime is not None:
        for entry in manifest.entries:
            runtime.markers[entry.image_id] = None


def attach_projection_markers(scene: Any) -> None:
    """Manifest를 검증한 뒤 누락된 Projection Marker를 원자적으로 연결합니다."""

    if not _marker_session_is_active(scene):
        raise CameraSceneError("Projection Paint Marker 사용권이 없습니다.")
    manifest = scene_manifest(scene)
    if manifest is None:
        return
    runtime = runtime_for_scene(scene) or install_camera_runtime(scene, manifest)
    object_by_uuid = {
        owned_uuid_of(obj): obj
        for obj in scene.objects
        if is_owned(obj, kind="CAMERA_OBJECT", dataset_uuid=manifest.dataset_uuid)
    }
    expected_names = {entry.marker_name for entry in manifest.entries}
    target_frames = {entry.frame for entry in manifest.entries}
    exact_pointers = managed_marker_candidates(scene, manifest)
    for entry in manifest.entries:
        if entry.camera_uuid not in object_by_uuid:
            raise CameraSceneError(f"IMAGE_ID {entry.image_id}의 Camera Object가 없습니다.")
    conflicts = [
        marker
        for marker in scene.timeline_markers
        if _pointer(marker) not in exact_pointers
        and (
            str(marker.name) in expected_names
            or (
                int(marker.frame) in target_frames
                and getattr(marker, "camera", None) is not None
            )
        )
    ]
    if conflicts:
        details = ", ".join(f"{marker.name}@{marker.frame}" for marker in conflicts[:5])
        raise CameraSceneError(f"외부 Camera Marker와 충돌합니다: {details}")
    created: list[tuple[int, Any]] = []
    try:
        for entry in manifest.entries:
            existing = [
                marker
                for marker in scene.timeline_markers
                if _pointer(marker) in exact_pointers
                and str(marker.name) == entry.marker_name
                and int(marker.frame) == entry.frame
                and marker.camera is object_by_uuid[entry.camera_uuid]
            ]
            if len(existing) > 1:
                raise CameraSceneError(
                    f"관리 Marker 후보가 모호합니다: IMAGE_ID {entry.image_id}"
                )
            if existing:
                runtime.markers[entry.image_id] = existing[0]
                continue
            marker = scene.timeline_markers.new(entry.marker_name, frame=entry.frame)
            marker.camera = object_by_uuid[entry.camera_uuid]
            runtime.markers[entry.image_id] = marker
            created.append((entry.image_id, marker))
        if not projection_markers_attached(scene):
            raise CameraSceneError("Projection Marker 연결 결과가 Manifest와 다릅니다.")
    except Exception:
        for image_id, marker in reversed(created):
            try:
                scene.timeline_markers.remove(marker)
            except (ReferenceError, RuntimeError, ValueError):
                pass
            runtime.markers[image_id] = None
        raise


def assert_marker_frames_available(
    scene: Any,
    frames: Iterable[int],
    old_manifest: CameraManifest | None,
) -> None:
    """외부 Camera Marker와 겹치는 Import Frame을 변경 없이 거부합니다."""

    target_frames = {int(frame) for frame in frames}
    managed = managed_marker_candidates(scene, old_manifest) if old_manifest else set()
    conflicts = [
        marker
        for marker in scene.timeline_markers
        if int(marker.frame) in target_frames
        and getattr(marker, "camera", None) is not None
        and _pointer(marker) not in managed
    ]
    if conflicts:
        details = ", ".join(f"{marker.name}@{marker.frame}" for marker in conflicts[:5])
        raise CameraSceneError(f"외부 Camera Marker와 Frame이 충돌합니다: {details}")


def build_manifest_entry(
    image: Any,
    camera_object: Any,
    frame: int,
    marker_name: str,
    qvec: tuple[float, ...],
    tvec: tuple[float, ...],
) -> CameraManifestEntry:
    """생성된 Camera Object에서 영구 IMAGE_ID Manifest 항목을 만듭니다."""

    return CameraManifestEntry(
        image_id=int(_field(image, "image_id")),
        camera_id=int(_field(image, "camera_id")),
        frame=int(frame),
        image_name=str(_field(image, "name", _field(image, "image_name", ""))),
        camera_uuid=owned_uuid_of(camera_object),
        marker_name=str(marker_name),
        matrix_local=_flat_matrix(camera_object.matrix_basis),
        qvec=qvec,
        tvec=tvec,
    )


def clear_camera_runtime(_reason: str = "", _epoch: int = 0) -> None:
    """Undo·Load 경계를 넘지 않도록 모든 직접 RNA Runtime 참조를 폐기합니다."""

    _RUNTIMES.clear()


def _runtime_key(scene: Any) -> int:
    """현재 Epoch 안에서 Scene Runtime을 찾을 포인터 Key를 반환합니다."""

    return _pointer(scene)


def runtime_for_scene(scene: Any) -> CameraRuntime | None:
    """현재 Scene과 Epoch에 유효한 Camera Runtime을 반환합니다."""

    runtime = _RUNTIMES.get(_runtime_key(scene))
    if runtime is None:
        return None
    registry = lifecycle_registry()
    if not registry.token_is_current(
        runtime.token,
        scene,
        dataset_revision=runtime.manifest.dataset_revision,
    ):
        _RUNTIMES.pop(_runtime_key(scene), None)
        return None
    return runtime


def _resolve_runtime(scene: Any, manifest: CameraManifest) -> CameraRuntime:
    """Load·Undo 뒤 UUID와 복합 Marker 조건으로 Runtime을 다시 만듭니다."""

    objects = {
        owned_uuid_of(obj): obj
        for obj in scene.objects
        if is_owned(obj, kind="CAMERA_OBJECT", dataset_uuid=manifest.dataset_uuid)
    }
    camera_data = {
        owned_uuid_of(data): data
        for data in bpy.data.cameras
        if is_owned(data, kind="CAMERA_DATA", dataset_uuid=manifest.dataset_uuid)
    }
    marker_index: dict[tuple[str, int, int], list[Any]] = {}
    for marker in scene.timeline_markers:
        key = (
            str(marker.name),
            int(marker.frame),
            _pointer(getattr(marker, "camera", None)),
        )
        marker_index.setdefault(key, []).append(marker)
    markers: dict[int, Any | None] = {}
    for entry in manifest.entries:
        camera_object = objects.get(entry.camera_uuid)
        key = (entry.marker_name, entry.frame, _pointer(camera_object))
        candidates = marker_index.get(key, [])
        if len(candidates) > 1:
            raise CameraSceneError(
                f"관리 Marker 후보가 모호합니다: IMAGE_ID {entry.image_id}"
            )
        markers[entry.image_id] = candidates[0] if candidates else None
    token = lifecycle_registry().capture_token(
        scene,
        dataset_revision=manifest.dataset_revision,
    )
    return CameraRuntime(
        scene=scene,
        manifest=manifest,
        token=token,
        objects=objects,
        camera_data=camera_data,
        markers=markers,
        entries_by_frame={entry.frame: entry for entry in manifest.entries},
        entries_by_camera_uuid={
            entry.camera_uuid: entry for entry in manifest.entries
        },
        data_by_uuid={
            entry.owned_uuid: entry for entry in manifest.camera_data
        },
        data_by_camera_id={
            entry.camera_id: entry for entry in manifest.camera_data
        },
        dirty_objects=deque(),
        dirty_data=deque(),
        queued=set(),
    )


def install_camera_runtime(scene: Any, manifest: CameraManifest) -> CameraRuntime:
    """Commit된 Camera Manifest의 현재 Epoch Runtime과 Guard를 설치합니다."""

    ensure_scene_uuid(scene)
    runtime = _resolve_runtime(scene, manifest)
    _RUNTIMES[_runtime_key(scene)] = runtime
    apply_camera_frustum_size(
        scene,
        dataset_uuid=manifest.dataset_uuid,
    )
    _ensure_repair_timer()
    return runtime


def rebuild_camera_runtime(scene: Any, _reason: str, _epoch: int) -> None:
    """Scene Manifest가 있을 때만 Camera Runtime을 재구축합니다."""

    if scene is None:
        return
    manifest = scene_manifest(scene)
    if manifest is None:
        _RUNTIMES.pop(_runtime_key(scene), None)
        return
    try:
        install_camera_runtime(scene, manifest)
        if _marker_session_is_active(scene):
            attach_projection_markers(scene)
        from .camera_review import refresh_camera_review

        refresh_camera_review(scene)
    except CameraSceneError as exc:
        settings = getattr(scene, "projection_paint", None)
        if settings is not None:
            settings.status = str(exc)
            settings.status_kind = "ERROR"


def register_camera_runtime_hooks() -> None:
    """Lifecycle 중단과 재구축 Hook을 정확히 한 번 등록합니다."""

    registry = lifecycle_registry()
    registry.add_barrier_hook(clear_camera_runtime)
    registry.add_rebuild_hook(rebuild_camera_runtime)


def unregister_camera_runtime_hooks() -> None:
    """Lifecycle Hook과 Camera Runtime 직접 참조를 제거합니다."""

    registry = lifecycle_registry()
    registry.remove_barrier_hook(clear_camera_runtime)
    registry.remove_rebuild_hook(rebuild_camera_runtime)
    clear_camera_runtime()


def collect_dirty_camera_ids(scene: Any, depsgraph: Any) -> None:
    """Depsgraph Update에서 관리 Camera UUID만 O(1) Queue에 추가합니다."""

    runtime = runtime_for_scene(scene)
    if runtime is None or runtime.repair_depth or runtime.error:
        return
    for update in depsgraph.updates:
        changed = getattr(update, "id", None)
        if is_owned(
            changed,
            kind="CAMERA_OBJECT",
            dataset_uuid=runtime.manifest.dataset_uuid,
        ):
            token = owned_uuid_of(changed)
            key = ("OBJECT", token)
            if token and key not in runtime.queued:
                runtime.queued.add(key)
                runtime.dirty_objects.append(token)
        elif is_owned(
            changed,
            kind="CAMERA_DATA",
            dataset_uuid=runtime.manifest.dataset_uuid,
        ):
            token = owned_uuid_of(changed)
            key = ("DATA", token)
            if token and key not in runtime.queued:
                runtime.queued.add(key)
                runtime.dirty_data.append(token)
    if runtime.queued:
        _ensure_repair_timer(first_interval=0.05)


def queue_frame_change(scene: Any) -> None:
    """Frame Handler에서 현재 Frame 검증만 예약하고 Blender 데이터는 쓰지 않습니다."""

    runtime = runtime_for_scene(scene)
    if runtime is None or runtime.error:
        return
    runtime.pending_frame = int(scene.frame_current)
    _ensure_repair_timer(first_interval=0.0)


def _restore_camera_data(data: Any, expected: CameraDataManifest) -> bool:
    """Camera Intrinsics가 다를 때만 Manifest 값으로 복구합니다."""

    changed = bool(
        data.type != "PERSP"
        or data.sensor_fit != expected.sensor_fit
        or abs(data.sensor_width - expected.sensor_width) > 1.0e-6
        or abs(data.lens - expected.lens) > 1.0e-6
        or abs(data.shift_x - expected.shift_x) > 1.0e-7
        or abs(data.shift_y - expected.shift_y) > 1.0e-7
    )
    if changed:
        data.type = "PERSP"
        data.sensor_fit = expected.sensor_fit
        data.sensor_width = expected.sensor_width
        data.lens = expected.lens
        data.shift_x = expected.shift_x
        data.shift_y = expected.shift_y
    scalar_metadata = {
        "pp_camera_model": expected.model_name,
        "pp_camera_width": expected.width,
        "pp_camera_height": expected.height,
    }
    for key, value in scalar_metadata.items():
        if data.get(key) != value:
            data[key] = value
            changed = True
    current_params = tuple(float(value) for value in data.get("pp_camera_params", ()))
    if current_params != expected.params:
        data["pp_camera_params"] = list(expected.params)
        changed = True
    return changed


def _ensure_camera_data(
    runtime: CameraRuntime,
    expected: CameraDataManifest,
) -> Any | None:
    """삭제된 공유 Camera Data를 Manifest UUID로 재생성합니다."""

    data = runtime.camera_data.get(expected.owned_uuid)
    if data is not None and _pointer(data):
        return data
    data = bpy.data.cameras.new(f"PP_CAMERA_DATA_{expected.camera_id}")
    tag_owned(
        data,
        "CAMERA_DATA",
        dataset_uuid=runtime.manifest.dataset_uuid,
        record_id=expected.camera_id,
        owned_uuid=expected.owned_uuid,
    )
    data["pp_camera_model"] = expected.model_name
    data["pp_camera_width"] = expected.width
    data["pp_camera_height"] = expected.height
    data["pp_camera_params"] = list(expected.params)
    _restore_camera_data(data, expected)
    data.display_size = camera_display_size_from_frustum_size(
        camera_frustum_size_from_scene(runtime.scene)
    )
    runtime.camera_data[expected.owned_uuid] = data
    return data


def _restore_camera_object(runtime: CameraRuntime, owned_uuid: str) -> bool:
    """관리 Camera Transform과 Data 연결을 Manifest 값으로 복구합니다."""

    entry = runtime.entries_by_camera_uuid.get(owned_uuid)
    if entry is None:
        return False
    expected_data = runtime.data_by_camera_id.get(entry.camera_id)
    if expected_data is None:
        runtime.error = f"Camera Data Manifest가 없습니다: CAMERA_ID {entry.camera_id}"
        return False
    data = _ensure_camera_data(runtime, expected_data)
    obj = runtime.objects.get(owned_uuid)
    if not _pointer(obj):
        collection = resolve_owned(
            bpy.data.collections,
            runtime.manifest.collection_uuid,
            kind="CAMERA_COLLECTION",
            dataset_uuid=runtime.manifest.dataset_uuid,
        )
        root = resolve_owned(
            runtime.scene.objects,
            runtime.manifest.axis_root_uuid,
            kind="CAMERA_AXIS_ROOT",
            dataset_uuid=runtime.manifest.dataset_uuid,
        )
        if collection is None or root is None or data is None:
            runtime.error = f"Camera Object 복구 Root가 없습니다: IMAGE_ID {entry.image_id}"
            return False
        obj = bpy.data.objects.new(f"PP_IMAGE_{entry.image_id}", data)
        tag_owned(
            obj,
            "CAMERA_OBJECT",
            dataset_uuid=runtime.manifest.dataset_uuid,
            record_id=entry.image_id,
            owned_uuid=entry.camera_uuid,
        )
        obj["pp_camera_id"] = str(entry.camera_id)
        obj["pp_image_name"] = entry.image_name
        obj["pp_qvec"] = list(entry.qvec)
        obj["pp_tvec"] = list(entry.tvec)
        obj.parent = root
        obj.matrix_parent_inverse = Matrix.Identity(4)
        obj.matrix_basis = _matrix_from_flat(entry.matrix_local)
        obj.hide_select = True
        obj.lock_location = (True, True, True)
        obj.lock_rotation = (True, True, True)
        obj.lock_scale = (True, True, True)
        collection.objects.link(obj)
        runtime.objects[owned_uuid] = obj
        apply_camera_frustum_visibility_to_object(
            runtime.scene,
            obj,
            hidden=camera_frustum_size_from_scene(runtime.scene) == 0.0,
        )
        return True
    changed = False
    if obj.data is not data:
        obj.data = data
        changed = True
    expected_matrix = _matrix_from_flat(entry.matrix_local)
    if any(
        abs(float(obj.matrix_basis[row][column]) - float(expected_matrix[row][column]))
        > 1.0e-7
        for row in range(4)
        for column in range(4)
    ):
        obj.matrix_basis = expected_matrix
        changed = True
    if not obj.hide_select:
        obj.hide_select = True
        changed = True
    for attribute in ("lock_location", "lock_rotation", "lock_scale"):
        if tuple(getattr(obj, attribute)) != (True, True, True):
            setattr(obj, attribute, (True, True, True))
            changed = True
    return changed


def _restore_marker(runtime: CameraRuntime, entry: CameraManifestEntry) -> bool:
    """관리 Marker 이동·삭제·이름·Camera 연결을 Manifest로 복구합니다."""

    if not _marker_session_is_active(runtime.scene):
        runtime.markers[entry.image_id] = None
        return False
    marker = runtime.markers.get(entry.image_id)
    camera_object = runtime.objects.get(entry.camera_uuid)
    if camera_object is None or not _pointer(camera_object):
        return False
    if not _pointer(marker):
        marker = runtime.scene.timeline_markers.new(entry.marker_name, frame=entry.frame)
        marker.camera = camera_object
        runtime.markers[entry.image_id] = marker
        return True
    changed = False
    if marker.name != entry.marker_name:
        marker.name = entry.marker_name
        changed = True
    if marker.frame != entry.frame:
        marker.frame = entry.frame
        changed = True
    if marker.camera is not camera_object:
        marker.camera = camera_object
        changed = True
    return changed


def _enter_camera_views(scene: Any, camera: Any) -> bool:
    """현재 Scene을 표시하는 모든 3D View를 관리 Camera 시점으로 전환합니다."""

    changed = False
    window_manager = getattr(bpy.context, "window_manager", None)
    for window in tuple(getattr(window_manager, "windows", ())):
        if getattr(window, "scene", None) is not scene:
            continue
        for area in tuple(window.screen.areas):
            if area.type != "VIEW_3D":
                continue
            space = area.spaces.active
            if getattr(space, "camera", None) is not camera:
                space.camera = camera
                changed = True
            rv3d = space.region_3d
            if rv3d.view_perspective != "CAMERA":
                rv3d.view_perspective = "CAMERA"
                changed = True
    return changed


def activate_managed_frame(
    scene: Any,
    frame: int,
    context: Any = None,
    *,
    refresh_source: bool = True,
) -> CameraManifestEntry:
    """관리 Frame의 Marker·Camera·Viewport를 동기 적용하고 Pending 요청을 소비합니다."""

    runtime = runtime_for_scene(scene)
    if runtime is None:
        manifest = scene_manifest(scene)
        if manifest is None:
            raise CameraSceneError("현재 Scene에 Camera Manifest가 없습니다.")
        runtime = install_camera_runtime(scene, manifest)
    entry = runtime.entries_by_frame.get(int(frame))
    if entry is None:
        raise CameraSceneError(f"Frame {int(frame)}에 관리 Camera가 없습니다.")
    if int(scene.frame_current) != int(frame):
        scene.frame_set(int(frame))
    if runtime.pending_frame == int(frame):
        runtime.pending_frame = None
    _restore_camera_object(runtime, entry.camera_uuid)
    camera = runtime.objects.get(entry.camera_uuid)
    if camera is None or not _pointer(camera):
        raise CameraSceneError(f"IMAGE_ID {entry.image_id}의 Camera Object가 없습니다.")
    _restore_marker(runtime, entry)
    scene.camera = camera
    if context is not None:
        from .camera_review import initialize_camera_review

        initialize_camera_review(context)
    else:
        _enter_camera_views(scene, camera)
        from .camera_review import refresh_camera_review

        refresh_camera_review(scene)
    if refresh_source:
        from .paint_runtime import refresh_frame_source

        refresh_frame_source(scene)
    return entry


def _repair_runtime(runtime: CameraRuntime, deadline: float) -> None:
    """한 Runtime의 Camera와 Marker를 짧은 시간 예산 안에서 복구합니다."""

    if runtime.repair_depth or runtime.error:
        return
    runtime.repair_depth += 1
    wrote = False
    try:
        if runtime.pending_frame is not None:
            frame = runtime.pending_frame
            entry = runtime.entries_by_frame.get(frame)
            if entry is not None:
                activate_managed_frame(runtime.scene, frame)
                wrote = True
            else:
                runtime.pending_frame = None
        while runtime.dirty_objects and perf_counter() < deadline:
            owned_uuid = runtime.dirty_objects.popleft()
            runtime.queued.discard(("OBJECT", owned_uuid))
            wrote |= _restore_camera_object(runtime, owned_uuid)
        while runtime.dirty_data and perf_counter() < deadline:
            owned_uuid = runtime.dirty_data.popleft()
            runtime.queued.discard(("DATA", owned_uuid))
            data = runtime.camera_data.get(owned_uuid)
            expected = runtime.data_by_uuid.get(owned_uuid)
            if data is not None and expected is not None and _pointer(data):
                wrote |= _restore_camera_data(data, expected)
        entries = runtime.manifest.entries if _marker_session_is_active(runtime.scene) else ()
        while entries and perf_counter() < deadline:
            entry = entries[runtime.marker_cursor]
            if not _pointer(runtime.objects.get(entry.camera_uuid)):
                wrote |= _restore_camera_object(runtime, entry.camera_uuid)
            wrote |= _restore_marker(runtime, entry)
            runtime.marker_cursor = (runtime.marker_cursor + 1) % len(entries)
            if runtime.marker_cursor == 0:
                break
    finally:
        runtime.repair_depth -= 1
    if wrote:
        runtime.repair_attempts += 1
        if runtime.repair_attempts > MAX_REPAIR_ATTEMPTS:
            runtime.error = "Camera Guard가 반복 복구 상한을 초과했습니다."
    else:
        runtime.repair_attempts = 0


def _repair_timer() -> float | None:
    """모든 활성 Scene Runtime을 1ms Slice로 복구합니다."""

    if not _RUNTIMES:
        return None
    deadline = perf_counter() + REPAIR_TIME_BUDGET
    for runtime in tuple(_RUNTIMES.values()):
        if perf_counter() >= deadline:
            break
        if lifecycle_registry().token_is_current(
            runtime.token,
            runtime.scene,
            dataset_revision=runtime.manifest.dataset_revision,
        ):
            _repair_runtime(runtime, deadline)
    return REPAIR_INTERVAL if _RUNTIMES else None


def _ensure_repair_timer(first_interval: float = REPAIR_INTERVAL) -> None:
    """Camera Guard Timer를 현재 Epoch에 정확히 하나 등록합니다."""

    lifecycle_registry().register_timer(
        _repair_timer,
        component="camera_repair",
        first_interval=first_interval,
        persistent_timer=False,
    )
