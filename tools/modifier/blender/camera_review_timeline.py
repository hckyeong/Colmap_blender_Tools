"""Camera Review Timeline Marker 소유권과 frame 동기화를 관리합니다."""

from __future__ import annotations

from dataclasses import dataclass
import json
import time
from types import SimpleNamespace

import bpy
from bpy.app.handlers import persistent

from .ownership import DATASET_UUID_KEY, OWNER_KEY, OWNER_TOKEN, RECORD_KIND_KEY


_MANIFEST_FIELDS = frozenset(
    (
        "dataset_uuid",
        "source_fingerprint",
        "edit_revision",
        "marker_name",
        "frame",
        "image_id",
        "camera",
    )
)
_TIMELINE_SESSIONS: dict[int, object] = {}
_MARKER_SESSION_TOOL_ID = "modifier"


@dataclass(frozen=True)
class _MarkerSnapshot:
    name: str
    frame: int
    camera: object | None
    camera_identity: tuple[str, str] | None


@dataclass(frozen=True)
class TimelineStateSnapshot:
    """Filter 원자 복구에 필요한 현재 데이터셋 Timeline 상태입니다."""

    manifest_json: str
    markers: tuple[_MarkerSnapshot, ...]
    camera_bindings: tuple[_MarkerSnapshot, ...]
    timeline_active: bool
    timeline_refresh_enabled: bool
    timeline_manifest_signature: tuple[tuple[object, ...], ...]
    frame: int
    scene_camera: object | None
    scene_camera_identity: tuple[str, str] | None


@dataclass(frozen=True)
class TimelineRestoreResult:
    """저장 manifest의 비파괴 Timeline Runtime 복원 결과입니다."""

    restored: bool
    marker_count: int
    current_image_id: int | None
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class _PendingTimelineUpdate:
    session: object
    target_index: int
    target_image_id: int
    target_frame: int
    session_token: tuple[object, ...]
    previous_index: int
    previous_image_id: int | None
    previous_frame: int
    previous_scene_camera: object | None


_PENDING_UPDATES: dict[int, _PendingTimelineUpdate] = {}
_MARKER_GUARD_DELAY_SECONDS = 0.15
_MARKER_GUARD_DIRTY_AT: dict[int, float] = {}
_MARKER_GUARD_REPAIRING: set[int] = set()
_REVIEW_UI_FIELDS = (
    "review_total_count",
    "review_current_index",
    "review_current_image_id",
    "review_camera_model",
    "review_exact_projection",
    "review_image_status",
    "review_vram_window",
    "review_status",
)


def marker_base_name(dataset_uuid: str, image_id: int) -> str:
    """사용자가 읽을 수 있고 데이터셋마다 결정적인 Marker 기본 이름입니다."""
    return f"COLMAP Review {dataset_uuid[:8]} IMAGE_ID {image_id}"


def _camera_identity(camera) -> tuple[str, str] | None:
    """Modifier 관리 Camera의 데이터셋과 IMAGE_ID identity를 반환합니다."""

    if (
        camera is None
        or camera.get(OWNER_KEY) != OWNER_TOKEN
        or camera.get(RECORD_KIND_KEY) != "CAMERA"
    ):
        return None
    dataset_uuid = camera.get(DATASET_UUID_KEY)
    image_id = camera.get("colmap_image_id_text")
    if not isinstance(dataset_uuid, str) or not isinstance(image_id, str):
        return None
    return dataset_uuid, image_id


def _validated_manifest_entry(value) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != _MANIFEST_FIELDS:
        raise ValueError("Timeline manifest entry 필드가 올바르지 않습니다.")
    camera = value["camera"]
    if (
        not isinstance(value["dataset_uuid"], str)
        or not isinstance(value["source_fingerprint"], str)
        or not isinstance(value["edit_revision"], int)
        or not isinstance(value["marker_name"], str)
        or not isinstance(value["frame"], int)
        or not isinstance(value["image_id"], str)
        or not value["image_id"].isdigit()
        or not isinstance(camera, dict)
        or set(camera) != {"dataset_uuid", "image_id", "name"}
        or not isinstance(camera["dataset_uuid"], str)
        or not isinstance(camera["image_id"], str)
        or not camera["image_id"].isdigit()
        or not isinstance(camera["name"], str)
    ):
        raise ValueError("Timeline manifest entry 값이 올바르지 않습니다.")
    if (
        camera["dataset_uuid"] != value["dataset_uuid"]
        or camera["image_id"] != value["image_id"]
    ):
        raise ValueError("Timeline manifest Camera identity가 entry와 다릅니다.")
    return value


def _read_manifest(settings) -> tuple[list[dict[str, object]], bool]:
    try:
        raw = json.loads(settings.review_marker_manifest_json)
        if not isinstance(raw, list):
            raise ValueError("Timeline manifest 최상위 값은 배열이어야 합니다.")
    except (TypeError, ValueError, json.JSONDecodeError):
        return [], True
    entries: list[dict[str, object]] = []
    damaged = False
    for item in raw:
        try:
            entries.append(_validated_manifest_entry(item))
        except (TypeError, ValueError):
            damaged = True
    return entries, damaged


def _manifest_sort_key(entry: dict[str, object]) -> tuple[object, ...]:
    return (
        entry["dataset_uuid"],
        entry["frame"],
        int(entry["image_id"]),
        entry["marker_name"],
    )


def _write_manifest(settings, entries: list[dict[str, object]]) -> None:
    settings.review_marker_manifest_json = json.dumps(
        sorted(entries, key=_manifest_sort_key),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _marker_matches_manifest(marker, entry: dict[str, object]) -> bool:
    """Marker의 이름·frame·Camera 이름과 Modifier identity를 Manifest와 대조합니다."""

    if marker is None:
        return False
    camera = entry["camera"]
    return bool(
        marker.name == entry["marker_name"]
        and marker.frame == entry["frame"]
        and marker.camera is not None
        and marker.camera.name == camera["name"]
        and _camera_identity(marker.camera)
        == (camera["dataset_uuid"], camera["image_id"])
    )


def _unique_marker_name(markers, base_name: str) -> str:
    if markers.get(base_name) is None:
        return base_name
    suffix = 1
    while True:
        candidate = f"{base_name}.{suffix:03d}"
        if markers.get(candidate) is None:
            return candidate
        suffix += 1


def _entry_for_marker(session, marker, image_id: int) -> dict[str, object]:
    camera = session.camera_objects[image_id]
    return {
        "dataset_uuid": session.dataset_uuid,
        "source_fingerprint": session.source_fingerprint,
        "edit_revision": session.edit_revision,
        "marker_name": marker.name,
        "frame": marker.frame,
        "image_id": str(image_id),
        "camera": {
            "dataset_uuid": session.dataset_uuid,
            "image_id": str(image_id),
            "name": camera.name,
        },
    }


def _snapshot_marker(marker) -> _MarkerSnapshot:
    return _MarkerSnapshot(
        name=marker.name,
        frame=marker.frame,
        camera=marker.camera,
        camera_identity=_camera_identity(marker.camera),
    )


def _create_timeline_marker(markers, name: str, frame: int, camera):
    """Timeline Build가 테스트 가능한 단일 Marker 생성 경계입니다."""
    marker = markers.new(name, frame=frame)
    try:
        marker.camera = camera
        return marker
    except Exception:
        _remove_exact_marker(markers, marker, strict=True)
        raise


def _remove_exact_marker(markers, marker, *, strict: bool = False) -> None:
    """저장한 Marker Pointer가 아직 동일할 때만 Marker를 제거합니다."""

    try:
        pointer = marker.as_pointer()
        current = markers.get(marker.name)
        if current is not None and current.as_pointer() == pointer:
            markers.remove(current)
    except (AttributeError, ReferenceError, RuntimeError, TypeError, ValueError) as exc:
        if strict:
            raise RuntimeError("신규 Timeline Marker를 제거하지 못했습니다.") from exc


def _restore_marker_snapshot(markers, snapshot: _MarkerSnapshot, session=None):
    """제거 전 Snapshot의 이름·frame·Camera 연결로 Marker를 복구합니다."""

    if markers.get(snapshot.name) is not None:
        raise RuntimeError(f"Marker 복구 이름이 이미 존재합니다: {snapshot.name}")
    camera = snapshot.camera
    identity = snapshot.camera_identity
    if (
        session is not None
        and identity is not None
        and identity[0] == session.dataset_uuid
    ):
        camera = session.camera_objects.get(int(identity[1]))
    marker = markers.new(snapshot.name, frame=snapshot.frame)
    marker.camera = camera
    return marker


def capture_timeline_state(session) -> TimelineStateSnapshot:
    """Filter commit 전에 검증된 현재 데이터셋 Marker와 playhead를 고정합니다."""
    settings = session.scene.colmap_import
    manifest, damaged = _read_manifest(settings)
    markers: list[_MarkerSnapshot] = []
    if not damaged:
        for entry in manifest:
            if entry["dataset_uuid"] != session.dataset_uuid:
                continue
            marker = session.scene.timeline_markers.get(entry["marker_name"])
            if _marker_matches_manifest(marker, entry):
                markers.append(_snapshot_marker(marker))
    return TimelineStateSnapshot(
        manifest_json=settings.review_marker_manifest_json,
        markers=tuple(markers),
        camera_bindings=tuple(
            _snapshot_marker(marker)
            for marker in session.scene.timeline_markers
            if (
                _camera_identity(marker.camera) is not None
                and _camera_identity(marker.camera)[0] == session.dataset_uuid
            )
        ),
        timeline_active=session.timeline_active,
        timeline_refresh_enabled=session.timeline_refresh_enabled,
        timeline_manifest_signature=session.timeline_manifest_signature,
        frame=session.scene.frame_current,
        scene_camera=session.scene.camera,
        scene_camera_identity=_camera_identity(session.scene.camera),
    )


def restore_timeline_state(session, snapshot: TimelineStateSnapshot) -> None:
    """현재 데이터셋 Marker만 교체해 Filter 전 Timeline 상태를 복원합니다."""
    scene = session.scene
    markers = scene.timeline_markers
    manifest, _damaged = _read_manifest(scene.colmap_import)
    removable_names = {
        entry["marker_name"]
        for entry in manifest
        if entry["dataset_uuid"] == session.dataset_uuid
    }
    removable_names.update(item.name for item in snapshot.markers)
    for name in removable_names:
        marker = markers.get(name)
        if marker is None:
            continue
        identity = _camera_identity(marker.camera)
        known_snapshot = next(
            (
                item
                for item in snapshot.markers
                if item.name == marker.name and item.frame == marker.frame
            ),
            None,
        )
        if (
            known_snapshot is not None
            or identity is not None
            and identity[0] == session.dataset_uuid
        ):
            markers.remove(marker)

    restored: list[object] = []
    try:
        for marker_snapshot in snapshot.markers:
            restored.append(
                _restore_marker_snapshot(markers, marker_snapshot, session)
            )
    except Exception:
        for marker in reversed(restored):
            _remove_exact_marker(markers, marker)
        raise

    for binding in snapshot.camera_bindings:
        marker = markers.get(binding.name)
        identity = binding.camera_identity
        if (
            marker is None
            or marker.frame != binding.frame
            or identity is None
            or identity[0] != session.dataset_uuid
        ):
            continue
        camera = session.camera_objects.get(int(identity[1]))
        if camera is None:
            raise RuntimeError(
                f"Marker Camera binding 복구 대상을 찾을 수 없습니다: {binding.name}"
            )
        marker.camera = camera

    scene.colmap_import.review_marker_manifest_json = snapshot.manifest_json
    session.timeline_active = snapshot.timeline_active
    session.timeline_refresh_enabled = snapshot.timeline_refresh_enabled
    session.timeline_manifest_signature = snapshot.timeline_manifest_signature
    if snapshot.timeline_refresh_enabled:
        attach_session(session)
    camera = snapshot.scene_camera
    identity = snapshot.scene_camera_identity
    if identity is not None and identity[0] == session.dataset_uuid:
        camera = session.camera_objects.get(int(identity[1]))
    session.update_guard = True
    try:
        scene.frame_set(snapshot.frame)
        scene.camera = camera
    finally:
        session.update_guard = False


def _session_token(session) -> tuple[object, ...]:
    return (
        session.dataset_uuid,
        session.source_fingerprint,
        session.edit_revision,
        session.index_revision,
        session.frame_start,
        session.pending_overlay_revision,
    )


def _sync_review_properties(session) -> None:
    """Timeline timer가 확정한 현재 pair를 UI Property에 반영합니다."""
    settings = session.scene.colmap_import
    entries = session.review_index.entries
    entry = entries[session.current_index]
    visible_count = len(entries) - len(session.pending_image_ids)
    visible_index = (
        session.current_index
        - sum(
            1
            for image_id in session.pending_image_ids
            if image_id < session.current_image_id
        )
    )
    settings.review_total_count = visible_count
    settings.review_current_index = visible_index
    settings.review_current_image_id = str(session.current_image_id)
    settings.review_camera_model = entry.camera_model
    settings.review_exact_projection = entry.exact_projection_supported
    settings.review_image_status = (
        entry.path_status
        if entry.exact_projection_supported
        else f"UNSUPPORTED ({entry.camera_model})"
    )
    previous = session.current_index - 1
    while previous >= 0 and entries[previous].image_id in session.pending_image_ids:
        previous -= 1
    following = session.current_index + 1
    while (
        following < len(entries)
        and entries[following].image_id in session.pending_image_ids
    ):
        following += 1
    settings.review_vram_window = " - ".join(
        str(entries[index].image_id)
        for index in (previous, session.current_index, following)
        if 0 <= index < len(entries)
    )
    status = (
        f"Camera {visible_index + 1}/{visible_count}, "
        f"IMAGE_ID {entry.image_id}"
    )
    settings.review_status = (
        f"{status}; {session.image_warnings[0]}"
        if session.image_warnings
        else status
    )


def attach_session(session) -> None:
    """frame handler가 parse나 Scene scan 없이 찾을 수 있도록 세션을 연결합니다."""
    if not modifier_marker_session_owned(session.scene):
        _TIMELINE_SESSIONS.pop(session.scene_pointer, None)
        return
    _TIMELINE_SESSIONS[session.scene_pointer] = session


def detach_session(session) -> None:
    """세션의 pending 작업을 제거하고 Timeline runtime 연결을 끊습니다."""
    scene_pointer = session.scene_pointer
    _TIMELINE_SESSIONS.pop(scene_pointer, None)
    _PENDING_UPDATES.pop(scene_pointer, None)
    _MARKER_GUARD_DIRTY_AT.pop(scene_pointer, None)
    _MARKER_GUARD_REPAIRING.discard(scene_pointer)
    session.timeline_active = False
    session.timeline_refresh_enabled = False
    session.timeline_manifest_signature = ()
    if not _PENDING_UPDATES and bpy.app.timers.is_registered(
        process_pending_timeline_updates
    ):
        bpy.app.timers.unregister(process_pending_timeline_updates)
    if not _MARKER_GUARD_DIRTY_AT and bpy.app.timers.is_registered(
        process_pending_marker_guard_updates
    ):
        bpy.app.timers.unregister(process_pending_marker_guard_updates)


def abandon_timeline_sessions_for_global_undo() -> None:
    """Undo 이전 Session을 역참조하지 않고 Timeline callback 소유권만 버립니다."""

    _TIMELINE_SESSIONS.clear()
    _PENDING_UPDATES.clear()
    _MARKER_GUARD_DIRTY_AT.clear()
    _MARKER_GUARD_REPAIRING.clear()
    if bpy.app.timers.is_registered(process_pending_timeline_updates):
        bpy.app.timers.unregister(process_pending_timeline_updates)
    if bpy.app.timers.is_registered(process_pending_marker_guard_updates):
        bpy.app.timers.unregister(process_pending_marker_guard_updates)


def _activate_timeline(session, entries: list[dict[str, object]]) -> None:
    session.timeline_manifest_signature = tuple(
        (
            item["marker_name"],
            item["frame"],
            item["image_id"],
            item["camera"]["dataset_uuid"],
            item["camera"]["image_id"],
        )
        for item in entries
        if item["dataset_uuid"] == session.dataset_uuid
    )
    session.timeline_active = bool(session.timeline_manifest_signature)
    session.timeline_refresh_enabled = True
    attach_session(session)


def _deactivate_timeline_runtime(session) -> None:
    """Scene 데이터를 건드리지 않고 현재 Timeline Runtime 연결만 끊습니다."""

    scene_pointer = session.scene_pointer
    if _TIMELINE_SESSIONS.get(scene_pointer) is session:
        _TIMELINE_SESSIONS.pop(scene_pointer, None)
    _PENDING_UPDATES.pop(scene_pointer, None)
    _MARKER_GUARD_DIRTY_AT.pop(scene_pointer, None)
    _MARKER_GUARD_REPAIRING.discard(scene_pointer)
    session.timeline_active = False
    session.timeline_refresh_enabled = False
    session.timeline_manifest_signature = ()
    if not _PENDING_UPDATES and bpy.app.timers.is_registered(
        process_pending_timeline_updates
    ):
        bpy.app.timers.unregister(process_pending_timeline_updates)
    if not _MARKER_GUARD_DIRTY_AT and bpy.app.timers.is_registered(
        process_pending_marker_guard_updates
    ):
        bpy.app.timers.unregister(process_pending_marker_guard_updates)


def _scene_marker_session_functions():
    """공용 Scene Marker 세션 API를 설치 경로와 Source 경로에서 불러옵니다."""

    try:
        from ....shared.blender_runtime.scene_marker_session import (
            acquire_scene_marker_session,
            release_scene_marker_session,
            scene_marker_session_owned_by,
        )
    except ImportError:
        from shared.blender_runtime.scene_marker_session import (
            acquire_scene_marker_session,
            release_scene_marker_session,
            scene_marker_session_owned_by,
        )
    return (
        acquire_scene_marker_session,
        release_scene_marker_session,
        scene_marker_session_owned_by,
    )


def acquire_modifier_marker_session(scene, *, window=None) -> None:
    """현재 Scene의 Timeline Marker 사용권을 Modifier에 배타적으로 획득합니다."""

    acquire, _release, _owned_by = _scene_marker_session_functions()
    acquired = acquire(scene, _MARKER_SESSION_TOOL_ID, window=window)
    if acquired is False:
        raise RuntimeError(
            "현재 Scene의 카메라 프레임 키를 다른 COLMAP 도구가 사용 중입니다. "
            "해당 도구를 종료한 뒤 다시 시도하십시오."
        )


def release_modifier_marker_session(scene) -> None:
    """현재 Scene에서 Modifier가 보유한 Timeline Marker 사용권을 반납합니다."""

    _acquire, release, _owned_by = _scene_marker_session_functions()
    release(scene, _MARKER_SESSION_TOOL_ID)


def modifier_marker_session_owned(scene) -> bool:
    """현재 Scene의 Timeline Marker 사용권을 Modifier가 보유하는지 반환합니다."""

    try:
        _acquire, _release, owned_by = _scene_marker_session_functions()
        return bool(owned_by(scene, _MARKER_SESSION_TOOL_ID))
    except (AttributeError, ImportError, ReferenceError, RuntimeError, TypeError):
        return False


def _restore_timeline_runtime_snapshot(
    session,
    snapshot: tuple[bool, bool, tuple[tuple[object, ...], ...]],
) -> None:
    """Attach·Detach 실패 전 Timeline Runtime 플래그와 Callback 연결을 복구합니다."""

    (
        session.timeline_active,
        session.timeline_refresh_enabled,
        session.timeline_manifest_signature,
    ) = snapshot
    if session.timeline_refresh_enabled:
        attach_session(session)


def _validated_attach_entries(session) -> list[dict[str, object]]:
    """Manifest를 현재 ReviewIndex·revision·Camera identity와 대조합니다."""

    manifest, damaged = _read_manifest(session.scene.colmap_import)
    if damaged:
        raise ValueError("Timeline manifest JSON이 손상되었습니다.")
    owned_entries = [
        entry for entry in manifest if entry["dataset_uuid"] == session.dataset_uuid
    ]
    if not owned_entries:
        return []
    all_pairs = {
        entry.image_id: session.frame_start + index
        for index, entry in enumerate(session.review_index.entries)
    }
    expected_pairs = _expected_timeline_pairs(session)
    image_ids = [int(entry["image_id"]) for entry in owned_entries]
    marker_names = [str(entry["marker_name"]) for entry in owned_entries]
    frames = [int(entry["frame"]) for entry in owned_entries]
    if len(set(image_ids)) != len(image_ids):
        raise ValueError("Timeline manifest에 중복 IMAGE_ID가 있습니다.")
    if len(set(marker_names)) != len(marker_names):
        raise ValueError("Timeline manifest에 중복 Marker 이름이 있습니다.")
    if len(set(frames)) != len(frames):
        raise ValueError("Timeline manifest에 중복 frame이 있습니다.")
    if (
        not set(expected_pairs).issubset(image_ids)
        or not set(image_ids).issubset(all_pairs)
        or set(image_ids) - set(expected_pairs) - set(session.pending_image_ids)
    ):
        raise ValueError("Timeline manifest IMAGE_ID 집합이 Camera Review와 다릅니다.")
    for entry in owned_entries:
        image_id = int(entry["image_id"])
        camera = session.camera_objects.get(image_id)
        mismatch_fields: list[str] = []
        if entry["source_fingerprint"] != session.source_fingerprint:
            mismatch_fields.append("source fingerprint")
        if entry["edit_revision"] != session.edit_revision:
            mismatch_fields.append("edit revision")
        if entry["frame"] != all_pairs[image_id]:
            mismatch_fields.append("frame")
        if camera is None:
            mismatch_fields.append("Camera")
        elif _camera_identity(camera) != (session.dataset_uuid, str(image_id)):
            mismatch_fields.append("Camera identity")
        elif entry["camera"]["name"] != camera.name:
            mismatch_fields.append("Camera name")
        if mismatch_fields:
            raise ValueError(
                f"IMAGE_ID {image_id} Timeline Manifest의 "
                f"{', '.join(mismatch_fields)} 값이 현재 Runtime과 다릅니다."
            )
    return sorted(
        (
            entry
            for entry in owned_entries
            if int(entry["image_id"]) not in session.pending_image_ids
        ),
        key=lambda item: int(item["frame"]),
    )


def attach_review_timeline_markers(session) -> int:
    """검증된 Manifest Marker를 충돌 없이 원자적으로 Scene에 연결합니다."""

    if not modifier_marker_session_owned(session.scene):
        raise RuntimeError("Modifier Timeline Marker 사용권이 없습니다.")
    runtime_snapshot = (
        session.timeline_active,
        session.timeline_refresh_enabled,
        session.timeline_manifest_signature,
    )
    _deactivate_timeline_runtime(session)
    created: list[object] = []
    try:
        entries = _validated_attach_entries(session)
        markers = session.scene.timeline_markers
        reusable: dict[str, object] = {}
        for entry in entries:
            image_id = int(entry["image_id"])
            expected_camera = session.camera_objects[image_id]
            existing = markers.get(entry["marker_name"])
            if existing is not None:
                if (
                    not _marker_matches_manifest(existing, entry)
                    or existing.camera is not expected_camera
                ):
                    raise ValueError(
                        f"Timeline Marker 이름 충돌이 있습니다: {entry['marker_name']}"
                    )
                reusable[entry["marker_name"]] = existing
            for candidate in markers:
                if candidate.frame != entry["frame"] or candidate.camera is None:
                    continue
                if existing is not None and candidate.as_pointer() == existing.as_pointer():
                    continue
                raise ValueError(
                    f"Frame {entry['frame']}에 외부 Camera Marker가 있습니다: "
                    f"{candidate.name}"
                )

        for entry in entries:
            if entry["marker_name"] in reusable:
                continue
            image_id = int(entry["image_id"])
            marker = _create_timeline_marker(
                markers,
                str(entry["marker_name"]),
                int(entry["frame"]),
                session.camera_objects[image_id],
            )
            if not _marker_matches_manifest(marker, entry):
                raise RuntimeError(
                    f"Timeline Marker 연결 검증에 실패했습니다: {entry['marker_name']}"
                )
            created.append(marker)
        _activate_timeline(session, entries)
        if entries and session.current_image_id is not None:
            sync_playhead_to_current(session)
        return len(entries)
    except Exception as original_error:
        rollback_failures: list[str] = []
        for marker in reversed(created):
            try:
                _remove_exact_marker(session.scene.timeline_markers, marker, strict=True)
            except Exception as exc:
                rollback_failures.append(str(exc))
        _restore_timeline_runtime_snapshot(session, runtime_snapshot)
        if rollback_failures:
            raise RuntimeError(
                "Timeline Marker Attach 실패 후 복구에도 실패했습니다. "
                f"원래 오류: {original_error}; 복구 오류: {'; '.join(rollback_failures)}"
            ) from original_error
        raise


def detach_review_timeline_markers(session) -> int:
    """Manifest와 Camera identity가 정확한 Modifier Marker만 원자적으로 분리합니다."""

    runtime_snapshot = (
        session.timeline_active,
        session.timeline_refresh_enabled,
        session.timeline_manifest_signature,
    )
    _deactivate_timeline_runtime(session)
    removed: list[_MarkerSnapshot] = []
    try:
        manifest, damaged = _read_manifest(session.scene.colmap_import)
        if damaged:
            raise ValueError("Timeline manifest JSON이 손상되었습니다.")
        markers = session.scene.timeline_markers
        targets: list[object] = []
        for entry in manifest:
            if entry["dataset_uuid"] != session.dataset_uuid:
                continue
            marker = markers.get(entry["marker_name"])
            if marker is None:
                continue
            if _marker_matches_manifest(marker, entry):
                targets.append(marker)
                continue
            raise ValueError(
                f"소유권이 모호한 Timeline Marker를 보존했습니다: {entry['marker_name']}"
            )
        snapshots = tuple(_snapshot_marker(marker) for marker in targets)
        for marker, snapshot in zip(targets, snapshots, strict=True):
            pointer = marker.as_pointer()
            try:
                _remove_exact_marker(markers, marker, strict=True)
            except Exception:
                current = markers.get(snapshot.name)
                if current is None or current.as_pointer() != pointer:
                    removed.append(snapshot)
                raise
            remaining = markers.get(snapshot.name)
            if remaining is not None and remaining.as_pointer() == pointer:
                raise RuntimeError(
                    f"Timeline Marker가 제거되지 않았습니다: {snapshot.name}"
                )
            removed.append(snapshot)
        return len(removed)
    except Exception as original_error:
        rollback_failures: list[str] = []
        for snapshot in removed:
            try:
                _restore_marker_snapshot(session.scene.timeline_markers, snapshot, session)
            except Exception as exc:
                rollback_failures.append(f"{snapshot.name}: {exc}")
        _restore_timeline_runtime_snapshot(session, runtime_snapshot)
        if rollback_failures:
            raise RuntimeError(
                "Timeline Marker Detach 실패 후 복구에도 실패했습니다. "
                f"원래 오류: {original_error}; 복구 오류: {'; '.join(rollback_failures)}"
            ) from original_error
        raise


def detach_modifier_timeline_markers_for_scenes(scenes) -> int:
    """ReviewSession 없이 모든 Scene의 검증된 Modifier Marker를 원자적으로 분리합니다."""

    prepared: list[tuple[object, tuple[object, ...], tuple[_MarkerSnapshot, ...]]] = []
    runtime_snapshots: list[
        tuple[object, tuple[bool, bool, tuple[tuple[object, ...], ...]]]
    ] = []
    for scene in tuple(scenes):
        settings = getattr(scene, "colmap_import", None)
        if settings is None:
            continue
        manifest, damaged = _read_manifest(settings)
        if damaged:
            raise ValueError(
                f"Scene '{scene.name}'의 Timeline manifest JSON이 손상되었습니다."
            )
        targets: list[object] = []
        for entry in manifest:
            marker = scene.timeline_markers.get(entry["marker_name"])
            if marker is None:
                continue
            if not _marker_matches_manifest(marker, entry):
                raise ValueError(
                    "소유권이 모호한 Timeline Marker를 보존했습니다: "
                    f"{entry['marker_name']}"
                )
            targets.append(marker)
        prepared.append(
            (
                scene,
                tuple(targets),
                tuple(_snapshot_marker(marker) for marker in targets),
            )
        )

    for scene, _targets, _snapshots in prepared:
        try:
            session = _TIMELINE_SESSIONS.get(scene.as_pointer())
        except (AttributeError, ReferenceError, RuntimeError):
            session = None
        if session is None:
            continue
        runtime_snapshots.append(
            (
                session,
                (
                    session.timeline_active,
                    session.timeline_refresh_enabled,
                    session.timeline_manifest_signature,
                ),
            )
        )
        _deactivate_timeline_runtime(session)

    removed: list[tuple[object, _MarkerSnapshot]] = []
    try:
        for scene, targets, snapshots in prepared:
            markers = scene.timeline_markers
            for marker, snapshot in zip(targets, snapshots, strict=True):
                pointer = marker.as_pointer()
                try:
                    _remove_exact_marker(markers, marker, strict=True)
                except Exception:
                    current = markers.get(snapshot.name)
                    if current is None or current.as_pointer() != pointer:
                        removed.append((scene, snapshot))
                    raise
                current = markers.get(snapshot.name)
                if current is not None and current.as_pointer() == pointer:
                    raise RuntimeError(
                        f"Timeline Marker가 제거되지 않았습니다: {snapshot.name}"
                    )
                removed.append((scene, snapshot))
        return len(removed)
    except Exception as original_error:
        rollback_failures: list[str] = []
        for scene, snapshot in reversed(removed):
            try:
                _restore_marker_snapshot(scene.timeline_markers, snapshot)
            except Exception as exc:
                rollback_failures.append(f"{scene.name}/{snapshot.name}: {exc}")
        for session, runtime_snapshot in runtime_snapshots:
            _restore_timeline_runtime_snapshot(session, runtime_snapshot)
        if rollback_failures:
            raise RuntimeError(
                "Extension 해제 Marker Detach 실패 후 복구에도 실패했습니다. "
                f"원래 오류: {original_error}; 복구 오류: {'; '.join(rollback_failures)}"
            ) from original_error
        raise


def detach_modifier_timeline_markers_from_scene(scene) -> int:
    """ReviewSession 없이 한 Scene의 검증된 Modifier Marker만 분리합니다."""

    return detach_modifier_timeline_markers_for_scenes((scene,))


def modifier_review_markers_attached(scene) -> bool:
    """Manifest와 정확히 일치하는 Modifier Camera Marker가 Scene에 남았는지 반환합니다."""

    settings = getattr(scene, "colmap_import", None)
    if settings is None:
        return False
    manifest, damaged = _read_manifest(settings)
    if damaged:
        return True
    return any(
        entry["dataset_uuid"] == settings.dataset_uuid
        and _marker_matches_manifest(
            scene.timeline_markers.get(entry["marker_name"]), entry
        )
        for entry in manifest
    )


def refresh_timeline_manifest_revision(session) -> tuple[str, ...]:
    """Camera 구성이 그대로인 편집 뒤 Manifest의 Runtime revision만 갱신합니다."""

    settings = session.scene.colmap_import
    manifest, damaged = _read_manifest(settings)
    if damaged:
        return ("Timeline manifest가 손상되어 편집 revision을 갱신하지 않았습니다.",)
    all_pairs = {
        entry.image_id: session.frame_start + index
        for index, entry in enumerate(session.review_index.entries)
    }
    updated_entries: list[dict[str, object]] = []
    changed = False
    for entry in manifest:
        if entry["dataset_uuid"] != session.dataset_uuid:
            updated_entries.append(entry)
            continue
        image_id = int(entry["image_id"])
        camera = session.camera_objects.get(image_id)
        if (
            image_id not in all_pairs
            or entry["frame"] != all_pairs[image_id]
            or camera is None
            or _camera_identity(camera) != (session.dataset_uuid, str(image_id))
            or entry["camera"]["name"] != camera.name
        ):
            raise ValueError(
                f"IMAGE_ID {image_id} Timeline Manifest Camera 계약이 현재 Runtime과 다릅니다."
            )
        replacement = dict(entry)
        replacement["source_fingerprint"] = session.source_fingerprint
        replacement["edit_revision"] = session.edit_revision
        changed = changed or replacement != entry
        updated_entries.append(replacement)
    if changed:
        _write_manifest(settings, updated_entries)
    return ()


def _expected_timeline_pairs(session) -> dict[int, int]:
    """Pending Camera를 제외한 IMAGE_ID별 저장 frame 계약을 반환합니다."""

    return {
        entry.image_id: session.frame_start + index
        for index, entry in enumerate(session.review_index.entries)
        if entry.image_id not in session.pending_image_ids
    }


def _validated_restore_entries(session) -> list[dict[str, object]]:
    """Scene을 변경하지 않고 현재 데이터셋 manifest와 Marker를 전수검증합니다."""

    manifest, damaged = _read_manifest(session.scene.colmap_import)
    if damaged:
        raise ValueError("Timeline manifest JSON이 손상되었습니다.")
    owned_entries = [
        entry
        for entry in manifest
        if entry["dataset_uuid"] == session.dataset_uuid
    ]
    expected_pairs = _expected_timeline_pairs(session)
    if not owned_entries:
        return []

    image_ids = [int(entry["image_id"]) for entry in owned_entries]
    marker_names = [str(entry["marker_name"]) for entry in owned_entries]
    frames = [int(entry["frame"]) for entry in owned_entries]
    if len(set(image_ids)) != len(image_ids):
        raise ValueError("Timeline manifest에 중복 IMAGE_ID가 있습니다.")
    if len(set(marker_names)) != len(marker_names):
        raise ValueError("Timeline manifest에 중복 Marker 이름이 있습니다.")
    if len(set(frames)) != len(frames):
        raise ValueError("Timeline manifest에 중복 frame이 있습니다.")
    if set(image_ids) != set(expected_pairs):
        missing = sorted(set(expected_pairs) - set(image_ids))
        extra = sorted(set(image_ids) - set(expected_pairs))
        raise ValueError(
            "Timeline manifest IMAGE_ID 집합이 Camera Review와 다릅니다. "
            f"누락 {missing[:10]}, 초과 {extra[:10]}"
        )

    validated_entries: list[dict[str, object]] = []
    for entry in owned_entries:
        image_id = int(entry["image_id"])
        expected_frame = expected_pairs[image_id]
        if (
            entry["source_fingerprint"] != session.source_fingerprint
            or entry["edit_revision"] != session.edit_revision
        ):
            raise ValueError(
                f"IMAGE_ID {image_id} Timeline token이 현재 Runtime과 다릅니다."
            )
        if entry["frame"] != expected_frame:
            raise ValueError(
                f"IMAGE_ID {image_id} Marker frame이 "
                f"{expected_frame}이 아닙니다."
            )
        marker = session.scene.timeline_markers.get(entry["marker_name"])
        if marker is None:
            raise ValueError(
                f"IMAGE_ID {image_id} Marker가 없습니다: "
                f"{entry['marker_name']}"
            )
        expected_camera = session.camera_objects.get(image_id)
        if (
            expected_camera is None
            or marker.camera is not expected_camera
            or not _marker_matches_manifest(marker, entry)
            or entry["camera"]["name"] != expected_camera.name
        ):
            raise ValueError(
                f"IMAGE_ID {image_id} Marker Camera 연결이 올바르지 않습니다."
            )
        validated_entries.append(entry)
    return sorted(validated_entries, key=lambda item: int(item["frame"]))


def _apply_timeline_target(
    session,
    *,
    target_index: int,
    target_image_id: int,
    target_frame: int,
    rollback_frame: int,
    rollback_scene_camera,
) -> None:
    """작업 집합·Review pair·Scene Camera·UI를 하나의 복구 경계로 적용합니다."""

    if (
        session.review_index.image_id_to_index.get(target_image_id)
        != target_index
        or session.review_index.frame_to_image_id.get(target_frame)
        != target_image_id
        or target_image_id in session.pending_image_ids
    ):
        raise ValueError("Timeline 적용 대상이 현재 ReviewIndex와 일치하지 않습니다.")

    settings = session.scene.colmap_import
    old_index = session.current_index
    old_image_id = session.current_image_id
    old_session_token = _session_token(session)
    old_ui = {
        field: getattr(settings, field)
        for field in _REVIEW_UI_FIELDS
    }
    old_image_warnings = list(session.image_warnings)
    working_set_prepared = False
    try:
        from .camera_review_images import update_working_set
        from .camera_review_viewport import update_review_view_for_current

        update_working_set(session, target_index)
        working_set_prepared = True
        if (
            _session_token(session) != old_session_token
            or (session.current_index, session.current_image_id)
            != (old_index, old_image_id)
        ):
            raise RuntimeError(
                "Timeline 이미지 준비 중 Camera Review 상태가 변경되었습니다."
            )
        camera = session.camera_objects.get(target_image_id)
        if camera is None:
            raise ValueError("Timeline 대상 Camera cache가 없습니다.")
        session.current_index = target_index
        session.current_image_id = target_image_id
        session.scene.camera = camera
        _sync_review_properties(session)
        update_review_view_for_current(
            SimpleNamespace(scene=session.scene),
            session,
        )
    except Exception as original_error:
        rollback_failures: list[str] = []
        session.current_index = old_index
        session.current_image_id = old_image_id
        if working_set_prepared and old_index >= 0:
            try:
                from .camera_review_images import update_working_set

                update_working_set(session, old_index)
            except Exception as exc:
                rollback_failures.append(f"이미지 작업 집합 복구 실패: {exc}")
        try:
            session.update_guard = True
            session.scene.frame_set(rollback_frame)
            session.scene.camera = rollback_scene_camera
        except (AttributeError, ReferenceError, RuntimeError) as exc:
            rollback_failures.append(f"frame/Scene Camera 복구 실패: {exc}")
        finally:
            session.update_guard = False
        try:
            from .camera_review_viewport import update_review_view_for_current

            update_review_view_for_current(
                SimpleNamespace(scene=session.scene),
                session,
            )
        except Exception as exc:
            rollback_failures.append(f"Viewport 복구 실패: {exc}")
        session.image_warnings = old_image_warnings
        for field, value in old_ui.items():
            setattr(settings, field, value)
        if rollback_failures:
            raise RuntimeError(
                "Timeline 전환 실패 후 복구에도 실패했습니다. "
                f"원래 오류: {original_error}; "
                f"복구 오류: {'; '.join(rollback_failures)}"
            ) from original_error
        raise


def restore_timeline_runtime_from_manifest(
    context,
    session,
) -> TimelineRestoreResult:
    """저장 Marker를 변경하지 않고 Timeline Runtime과 현재 frame을 복원합니다."""

    _deactivate_timeline_runtime(session)
    try:
        if not modifier_marker_session_owned(session.scene):
            detach_review_timeline_markers(session)
            return TimelineRestoreResult(False, 0, session.current_image_id)
        entries = _validated_restore_entries(session)
        if not entries:
            return TimelineRestoreResult(False, 0, session.current_image_id)

        _activate_timeline(session, entries)
        target_image_id = session.review_index.frame_to_image_id.get(
            session.scene.frame_current
        )
        if (
            target_image_id is not None
            and target_image_id not in session.pending_image_ids
        ):
            target_index = session.review_index.image_id_to_index.get(
                target_image_id
            )
            if target_index is None:
                raise ValueError("현재 frame의 Review index가 없습니다.")
            old_frame = session.scene.frame_current
            old_scene_camera = session.scene.camera
            _apply_timeline_target(
                session,
                target_index=target_index,
                target_image_id=target_image_id,
                target_frame=session.scene.frame_current,
                rollback_frame=old_frame,
                rollback_scene_camera=old_scene_camera,
            )
        return TimelineRestoreResult(
            True,
            len(entries),
            session.current_image_id,
        )
    except Exception as exc:
        _deactivate_timeline_runtime(session)
        return TimelineRestoreResult(
            False,
            0,
            session.current_image_id,
            (str(exc),),
        )


def capture_owned_marker_refresh_token(session) -> tuple[tuple[object, ...], ...]:
    """Scene commit 전에 Camera identity까지 일치한 소유 Marker를 고정합니다."""
    if not session.timeline_refresh_enabled:
        return ()
    manifest, damaged = _read_manifest(session.scene.colmap_import)
    if damaged:
        return ()
    verified: list[tuple[object, ...]] = []
    for entry in manifest:
        if entry["dataset_uuid"] != session.dataset_uuid:
            continue
        marker = session.scene.timeline_markers.get(entry["marker_name"])
        if _marker_matches_manifest(marker, entry):
            verified.append(
                (
                    entry["marker_name"],
                    entry["frame"],
                    entry["dataset_uuid"],
                    entry["image_id"],
                )
            )
    return tuple(verified)


def remove_pending_camera_marker(session, image_id: int) -> tuple[str, ...]:
    """Pending Camera의 검증된 소유 Marker만 제거하고 기존 frame은 유지합니다."""
    if not session.timeline_refresh_enabled:
        return ()
    settings = session.scene.colmap_import
    manifest, damaged = _read_manifest(settings)
    if damaged:
        return (
            "Timeline manifest가 손상되어 Pending Camera Marker를 자동 제거하지 않았습니다.",
        )

    current_entries: list[dict[str, object]] = []
    warnings: list[str] = []
    removed = False
    for entry in manifest:
        if (
            entry["dataset_uuid"] != session.dataset_uuid
            or int(entry["image_id"]) != image_id
        ):
            current_entries.append(entry)
            continue
        marker = session.scene.timeline_markers.get(entry["marker_name"])
        if _marker_matches_manifest(marker, entry):
            session.scene.timeline_markers.remove(marker)
            removed = True
        else:
            current_entries.append(entry)
            warnings.append(
                f"소유권이 불일치한 Marker를 보존했습니다: {entry['marker_name']}"
            )

    if removed:
        _write_manifest(settings, current_entries)
    owned_entries = [
        entry
        for entry in current_entries
        if entry["dataset_uuid"] == session.dataset_uuid
    ]
    _activate_timeline(session, owned_entries)
    return tuple(warnings)


def _rollback_failed_build(
    session,
    *,
    manifest_json: str,
    removed_markers: list[_MarkerSnapshot],
    created_markers: list[object],
    timeline_active: bool,
    timeline_refresh_enabled: bool,
    timeline_manifest_signature: tuple[tuple[object, ...], ...],
    frame: int,
    scene_camera,
) -> None:
    """Timeline Build에서 변경한 항목을 역순으로 복구합니다."""
    scene = session.scene
    markers = scene.timeline_markers
    failures: list[str] = []
    for marker in reversed(created_markers):
        try:
            _remove_exact_marker(markers, marker, strict=True)
        except Exception as exc:
            failures.append(f"신규 Marker 제거 실패: {exc}")
    for snapshot in removed_markers:
        try:
            _restore_marker_snapshot(markers, snapshot, session)
        except Exception as exc:
            failures.append(f"기존 Marker 복구 실패 {snapshot.name}: {exc}")
    try:
        scene.colmap_import.review_marker_manifest_json = manifest_json
    except Exception as exc:
        failures.append(f"manifest 복구 실패: {exc}")
    session.timeline_active = timeline_active
    session.timeline_refresh_enabled = timeline_refresh_enabled
    session.timeline_manifest_signature = timeline_manifest_signature
    try:
        session.update_guard = True
        scene.frame_set(frame)
        scene.camera = scene_camera
    except Exception as exc:
        failures.append(f"playhead/Scene.camera 복구 실패: {exc}")
    finally:
        session.update_guard = False
    if failures:
        raise RuntimeError("; ".join(failures))


def build_or_refresh_timeline(
    context,
    session,
    *,
    allow_empty: bool = False,
    prevalidated_refresh_token: tuple[tuple[object, ...], ...] = (),
) -> tuple[str, ...]:
    """현재 데이터셋 소유 Marker만 안전하게 교체하고 manifest를 갱신합니다."""
    if not modifier_marker_session_owned(session.scene):
        detach_review_timeline_markers(session)
        return ("비활성 Modifier에서는 Camera Marker를 생성하지 않습니다.",)
    if not session.review_index.entries and not allow_empty:
        raise ValueError("Timeline을 만들 생존 Camera가 없습니다.")
    settings = context.scene.colmap_import
    if settings.point_edit_mode_active or settings.camera_edit_mode_active:
        raise ValueError("Point 또는 Camera Edit Mode 중에는 Timeline을 변경할 수 없습니다.")

    manifest, damaged = _read_manifest(settings)
    warnings: list[str] = []
    if damaged:
        warnings.append(
            "Timeline manifest가 손상되어 기존 Marker를 보존하고 현재 데이터셋 manifest를 재구성했습니다."
        )
    foreign_entries = [
        item for item in manifest
        if item["dataset_uuid"] != session.dataset_uuid
    ]
    old_manifest_json = settings.review_marker_manifest_json
    old_timeline_active = session.timeline_active
    old_timeline_refresh_enabled = session.timeline_refresh_enabled
    old_timeline_manifest_signature = session.timeline_manifest_signature
    old_frame = context.scene.frame_current
    old_scene_camera = context.scene.camera
    removed_markers: list[_MarkerSnapshot] = []
    created_markers: list[object] = []
    prevalidated_set = set(prevalidated_refresh_token)
    markers = context.scene.timeline_markers
    try:
        if not damaged:
            for entry in (
                item for item in manifest
                if item["dataset_uuid"] == session.dataset_uuid
            ):
                marker = markers.get(entry["marker_name"])
                prevalidated = (
                    entry["marker_name"],
                    entry["frame"],
                    entry["dataset_uuid"],
                    entry["image_id"],
                ) in prevalidated_set
                if _marker_matches_manifest(marker, entry) or (
                    prevalidated
                    and marker is not None
                    and marker.name == entry["marker_name"]
                    and marker.frame == entry["frame"]
                ):
                    removed_markers.append(_snapshot_marker(marker))
                    markers.remove(marker)
                elif marker is not None:
                    warnings.append(
                        f"소유권이 불일치한 Marker를 보존했습니다: {entry['marker_name']}"
                    )

        current_entries: list[dict[str, object]] = []
        for index, review_entry in enumerate(session.review_index.entries):
            frame = session.frame_start + index
            base_name = marker_base_name(
                session.dataset_uuid,
                review_entry.image_id,
            )
            marker = _create_timeline_marker(
                markers,
                _unique_marker_name(markers, base_name),
                frame,
                session.camera_objects[review_entry.image_id],
            )
            created_markers.append(marker)
            current_entries.append(
                _entry_for_marker(session, marker, review_entry.image_id)
            )

        _write_manifest(settings, [*foreign_entries, *current_entries])
        _activate_timeline(session, current_entries)
        if current_entries:
            sync_playhead_to_current(session)
        return tuple(warnings)
    except Exception as original_error:
        try:
            _rollback_failed_build(
                session,
                manifest_json=old_manifest_json,
                removed_markers=removed_markers,
                created_markers=created_markers,
                timeline_active=old_timeline_active,
                timeline_refresh_enabled=old_timeline_refresh_enabled,
                timeline_manifest_signature=old_timeline_manifest_signature,
                frame=old_frame,
                scene_camera=old_scene_camera,
            )
        except Exception as rollback_error:
            raise RuntimeError(
                "Timeline Build 실패 후 복구에도 실패했습니다. "
                f"원래 오류: {original_error}; 복구 오류: {rollback_error}"
            ) from original_error
        raise


def sync_playhead_to_current(session) -> None:
    """Previous/Next와 Build에서 현재 pair를 활성 Timeline frame에 맞춥니다."""
    if not session.timeline_active or session.current_image_id is None:
        return
    frame = session.frame_start + session.current_index
    if (
        session.review_index.frame_to_image_id.get(frame)
        != session.current_image_id
    ):
        session.timeline_active = False
        session.timeline_manifest_signature = ()
        return
    session.update_guard = True
    try:
        session.scene.frame_set(frame)
        session.scene.camera = session.camera_objects[session.current_image_id]
    finally:
        session.update_guard = False


def _schedule_update(
    session,
    target_index: int,
    target_image_id: int,
    target_frame: int,
) -> None:
    previous_frame = (
        session.frame_start + session.current_index
        if session.current_index >= 0
        else target_frame
    )
    _PENDING_UPDATES[session.scene_pointer] = _PendingTimelineUpdate(
        session=session,
        target_index=target_index,
        target_image_id=target_image_id,
        target_frame=target_frame,
        session_token=_session_token(session),
        previous_index=session.current_index,
        previous_image_id=session.current_image_id,
        previous_frame=previous_frame,
        previous_scene_camera=session.scene.camera,
    )
    if not bpy.app.timers.is_registered(process_pending_timeline_updates):
        bpy.app.timers.register(
            process_pending_timeline_updates,
            first_interval=0.0,
        )


@persistent
def frame_change_post(scene, *_args) -> None:
    """검증된 frame map과 캐시만 사용해 실제 갱신을 timer에 예약합니다."""
    try:
        scene_pointer = scene.as_pointer()
        session = _TIMELINE_SESSIONS.get(scene_pointer)
        if (
            session is None
            or not session.timeline_active
            or not session.timeline_manifest_signature
            or session.update_guard
            or session.scene is not scene
        ):
            return
        settings = scene.colmap_import
        if (
            settings.point_edit_mode_active
            or settings.camera_edit_mode_active
            or settings.dataset_uuid != session.dataset_uuid
            or settings.source_fingerprint != session.source_fingerprint
            or settings.edit_revision != session.edit_revision
        ):
            return
        image_id = session.review_index.frame_to_image_id.get(scene.frame_current)
        if image_id is None or image_id in session.pending_image_ids:
            return
        target_index = session.review_index.image_id_to_index.get(image_id)
        camera = session.camera_objects.get(image_id)
        if target_index is None or camera is None:
            return
        _schedule_update(
            session,
            target_index,
            image_id,
            scene.frame_current,
        )
    except (AttributeError, ReferenceError, RuntimeError, TypeError, ValueError):
        return


def process_pending_timeline_updates():
    """예약된 이미지 작업 집합과 Viewport 갱신을 Blender 주 스레드에서 수행합니다."""
    pending = tuple(_PENDING_UPDATES.values())
    _PENDING_UPDATES.clear()
    for update in pending:
        session = update.session
        if (
            not session.timeline_active
            or update.session_token != _session_token(session)
            or update.target_image_id in session.pending_image_ids
            or (
                session.current_index,
                session.current_image_id,
            )
            != (
                update.previous_index,
                update.previous_image_id,
            )
            or session.scene.frame_current != update.target_frame
        ):
            continue
        settings = session.scene.colmap_import
        try:
            if (
                update.session_token != _session_token(session)
                or (
                    session.current_index,
                    session.current_image_id,
                )
                != (
                    update.previous_index,
                    update.previous_image_id,
                )
            ):
                raise RuntimeError(
                    "Timeline 이미지 준비 중 Camera Review 상태가 변경되었습니다."
                )
            _apply_timeline_target(
                session,
                target_index=update.target_index,
                target_image_id=update.target_image_id,
                target_frame=update.target_frame,
                rollback_frame=update.previous_frame,
                rollback_scene_camera=update.previous_scene_camera,
            )
        except (AttributeError, ReferenceError, RuntimeError, TypeError, ValueError) as exc:
            settings.review_status = f"Timeline frame 전환 실패: {exc}"
    return None


def _marker_guard_session_valid(session) -> bool:
    """Marker 복구가 현재 Camera Review 세션에만 적용되는지 검증합니다."""
    try:
        settings = session.scene.colmap_import
        return bool(
            session.timeline_active
            and session.timeline_refresh_enabled
            and session.timeline_manifest_signature
            and modifier_marker_session_owned(session.scene)
            and not session.update_guard
            and settings.dataset_uuid == session.dataset_uuid
            and settings.source_fingerprint == session.source_fingerprint
            and settings.edit_revision == session.edit_revision
        )
    except (AttributeError, ReferenceError, RuntimeError, TypeError, ValueError):
        return False


def _resync_after_marker_restore(session) -> tuple[str, ...]:
    """Marker 복구 뒤 현재 Review pair를 Scene과 Viewport에 다시 적용합니다."""
    warnings: list[str] = []
    scene_pointer = session.scene_pointer
    _PENDING_UPDATES.pop(scene_pointer, None)

    current_camera = session.camera_objects.get(session.current_image_id)
    if current_camera is not None:
        session.scene.camera = current_camera
        try:
            from .camera_review_viewport import update_review_view_for_current

            update_review_view_for_current(
                SimpleNamespace(scene=session.scene),
                session,
            )
        except (
            AttributeError,
            ReferenceError,
            RuntimeError,
            TypeError,
            ValueError,
        ) as exc:
            warnings.append(f"Camera Review Viewport 재동기화 실패: {exc}")

    target_image_id = session.review_index.frame_to_image_id.get(
        session.scene.frame_current
    )
    if (
        target_image_id is None
        or target_image_id in session.pending_image_ids
        or target_image_id == session.current_image_id
    ):
        return tuple(warnings)

    target_index = session.review_index.image_id_to_index.get(target_image_id)
    target_camera = session.camera_objects.get(target_image_id)
    if target_index is None or target_camera is None:
        warnings.append("현재 Frame의 Camera Review 대상을 다시 찾지 못했습니다.")
        return tuple(warnings)
    _schedule_update(
        session,
        target_index,
        target_image_id,
        session.scene.frame_current,
    )
    return tuple(warnings)


def restore_moved_owned_markers(session) -> tuple[str, ...]:
    """이동된 현재 데이터셋 소유 Camera Marker만 manifest 위치로 복구합니다."""
    if not _marker_guard_session_valid(session):
        return ()

    restored_names: list[str] = []
    scene_pointer = session.scene_pointer
    _MARKER_GUARD_REPAIRING.add(scene_pointer)
    session.update_guard = True
    try:
        markers = session.scene.timeline_markers
        for (
            marker_name,
            expected_frame,
            _image_id,
            camera_dataset_uuid,
            camera_image_id,
        ) in session.timeline_manifest_signature:
            marker = markers.get(marker_name)
            if (
                marker is None
                or _camera_identity(marker.camera)
                != (camera_dataset_uuid, camera_image_id)
                or marker.frame == expected_frame
            ):
                continue
            marker.frame = expected_frame
            marker.select = False
            restored_names.append(marker_name)
        if restored_names:
            warnings = _resync_after_marker_restore(session)
            status = (
                "Camera Marker move cancelled; "
                f"restored {len(restored_names)} marker(s)."
            )
            if warnings:
                status = f"{status} {warnings[0]}"
            session.scene.colmap_import.review_status = status
    finally:
        session.update_guard = False
        _MARKER_GUARD_REPAIRING.discard(scene_pointer)
    return tuple(restored_names)


def _schedule_marker_guard(session) -> None:
    """Scene 변경이 멈춘 뒤 한 번만 Marker 위치 검사를 예약합니다."""
    _MARKER_GUARD_DIRTY_AT[session.scene_pointer] = time.monotonic()
    if not bpy.app.timers.is_registered(process_pending_marker_guard_updates):
        bpy.app.timers.register(
            process_pending_marker_guard_updates,
            first_interval=_MARKER_GUARD_DELAY_SECONDS,
        )


@persistent
def timeline_marker_guard_post(scene, depsgraph) -> None:
    """Scene 변경 이벤트에서 현재 데이터셋 Marker 검사만 지연 예약합니다."""
    try:
        scene_pointer = scene.as_pointer()
        session = _TIMELINE_SESSIONS.get(scene_pointer)
        if (
            session is None
            or scene_pointer in _MARKER_GUARD_REPAIRING
            or not _marker_guard_session_valid(session)
        ):
            return
        if depsgraph is not None and not any(
            update.id is scene
            or getattr(update.id, "original", None) is scene
            for update in depsgraph.updates
        ):
            return
        _schedule_marker_guard(session)
    except (AttributeError, ReferenceError, RuntimeError, TypeError, ValueError):
        return


def process_pending_marker_guard_updates():
    """마지막 Scene 변경 이후 이동된 COLMAP Marker를 한 번만 복구합니다."""
    now = time.monotonic()
    ready_scene_pointers = tuple(
        scene_pointer
        for scene_pointer, dirty_at in _MARKER_GUARD_DIRTY_AT.items()
        if now - dirty_at >= _MARKER_GUARD_DELAY_SECONDS
    )
    for scene_pointer in ready_scene_pointers:
        _MARKER_GUARD_DIRTY_AT.pop(scene_pointer, None)
        session = _TIMELINE_SESSIONS.get(scene_pointer)
        if session is None:
            continue
        try:
            restore_moved_owned_markers(session)
        except (
            AttributeError,
            ReferenceError,
            RuntimeError,
            TypeError,
            ValueError,
        ) as exc:
            try:
                session.scene.colmap_import.review_status = (
                    f"Camera Marker move restore failed: {exc}"
                )
            except (AttributeError, ReferenceError, RuntimeError):
                pass

    if not _MARKER_GUARD_DIRTY_AT:
        return None
    remaining = min(
        max(
            0.01,
            _MARKER_GUARD_DELAY_SECONDS - (now - dirty_at),
        )
        for dirty_at in _MARKER_GUARD_DIRTY_AT.values()
    )
    return remaining


def register_frame_change_handler() -> None:
    if frame_change_post not in bpy.app.handlers.frame_change_post:
        bpy.app.handlers.frame_change_post.append(frame_change_post)
    if timeline_marker_guard_post not in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.append(timeline_marker_guard_post)


def unregister_frame_change_handler() -> None:
    if frame_change_post in bpy.app.handlers.frame_change_post:
        bpy.app.handlers.frame_change_post.remove(frame_change_post)
    if timeline_marker_guard_post in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.remove(timeline_marker_guard_post)
    _PENDING_UPDATES.clear()
    _TIMELINE_SESSIONS.clear()
    _MARKER_GUARD_DIRTY_AT.clear()
    _MARKER_GUARD_REPAIRING.clear()
    if bpy.app.timers.is_registered(process_pending_timeline_updates):
        bpy.app.timers.unregister(process_pending_timeline_updates)
    if bpy.app.timers.is_registered(process_pending_marker_guard_updates):
        bpy.app.timers.unregister(process_pending_marker_guard_updates)


__all__ = [
    "TimelineRestoreResult",
    "abandon_timeline_sessions_for_global_undo",
    "attach_session",
    "attach_review_timeline_markers",
    "acquire_modifier_marker_session",
    "build_or_refresh_timeline",
    "capture_owned_marker_refresh_token",
    "capture_timeline_state",
    "detach_session",
    "detach_modifier_timeline_markers_for_scenes",
    "detach_modifier_timeline_markers_from_scene",
    "detach_review_timeline_markers",
    "frame_change_post",
    "marker_base_name",
    "modifier_marker_session_owned",
    "modifier_review_markers_attached",
    "process_pending_marker_guard_updates",
    "process_pending_timeline_updates",
    "refresh_timeline_manifest_revision",
    "register_frame_change_handler",
    "remove_pending_camera_marker",
    "release_modifier_marker_session",
    "restore_moved_owned_markers",
    "restore_timeline_runtime_from_manifest",
    "restore_timeline_state",
    "sync_playhead_to_current",
    "timeline_marker_guard_post",
    "unregister_frame_change_handler",
]
