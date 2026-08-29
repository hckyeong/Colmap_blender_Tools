"""관리 Camera 선택 모드의 세션과 수명주기를 관리합니다."""

from __future__ import annotations

from dataclasses import dataclass

from .selection import (
    clear_snapshot,
    lock_managed_camera_objects,
    lock_managed_point_objects,
    validate_managed_camera_scene,
)


@dataclass(frozen=True)
class EditCameraSession:
    dataset_uuid: str
    scene_pointer: int
    source_fingerprint: str
    edit_revision: int
    index_revision: int
    history_hash: str
    camera_object_pointers: tuple[tuple[int, int], ...]


_SESSIONS: dict[int, EditCameraSession] = {}


def _scene_key(context) -> int:
    return context.scene.as_pointer()


def _camera_pointer_token(cameras) -> tuple[tuple[int, int], ...]:
    return tuple(
        sorted((image_id, obj.as_pointer()) for image_id, obj in cameras.items())
    )


def current_camera_edit_session(context, state, *, strict: bool = False):
    settings = context.scene.colmap_import
    session = _SESSIONS.get(_scene_key(context))
    if session is None:
        if strict or settings.camera_edit_mode_active:
            raise ValueError("Edit Camera Mode 상태가 손상되었습니다. 모드를 다시 시작하십시오.")
        return None
    if not settings.camera_edit_mode_active:
        if strict:
            raise ValueError("Edit Camera Mode UI 상태와 Runtime 세션이 다릅니다.")
        return None

    cameras = validate_managed_camera_scene(context, state)
    valid = (
        context.mode == "OBJECT"
        and session.dataset_uuid == state.dataset_uuid
        and session.scene_pointer == context.scene.as_pointer()
        and session.source_fingerprint == state.source.source_fingerprint
        and session.edit_revision == state.edit_revision
        and session.index_revision == state.index_revision
        and session.history_hash == state.history_hash
        and session.camera_object_pointers == _camera_pointer_token(cameras)
    )
    if valid:
        return session
    if strict or settings.camera_edit_mode_active:
        raise ValueError(
            "Edit Camera Mode의 Camera Object 또는 revision이 현재 상태와 "
            "일치하지 않습니다."
        )
    return None


def start_camera_edit_mode(
    context,
    state,
    *,
    selected_image_ids: tuple[int, ...] = (),
) -> None:
    settings = context.scene.colmap_import
    if settings.point_edit_mode_active:
        raise ValueError("Edit Point Mode를 먼저 종료하십시오.")
    if context.mode != "OBJECT":
        raise ValueError("다른 Object의 Edit Mode를 종료한 뒤 다시 실행하십시오.")
    if state.edit_revision != state.index_revision:
        raise ValueError("Runtime 모델 revision과 관계 인덱스 revision이 일치하지 않습니다.")

    cameras = validate_managed_camera_scene(context, state)
    unknown = sorted(set(selected_image_ids) - set(cameras))
    if unknown:
        raise ValueError(f"현재 Scene에 없는 IMAGE_ID가 요청되었습니다: {unknown[:10]}")

    clear_snapshot(state.dataset_uuid)
    lock_managed_point_objects(context, state.dataset_uuid)
    for obj in context.view_layer.objects:
        obj.select_set(False)
    for camera in cameras.values():
        camera.hide_select = False
    for image_id in selected_image_ids:
        cameras[image_id].select_set(True)
    if selected_image_ids:
        context.view_layer.objects.active = cameras[selected_image_ids[0]]

    _SESSIONS[_scene_key(context)] = EditCameraSession(
        dataset_uuid=state.dataset_uuid,
        scene_pointer=context.scene.as_pointer(),
        source_fingerprint=state.source.source_fingerprint,
        edit_revision=state.edit_revision,
        index_revision=state.index_revision,
        history_hash=state.history_hash,
        camera_object_pointers=_camera_pointer_token(cameras),
    )
    settings.camera_edit_mode_active = True
    settings.point_edit_mode_active = False
    settings.remove_target = "IMAGES" if selected_image_ids else "NONE"
    settings.camera_target_count = len(selected_image_ids)
    settings.point_selection_status = (
        f"Edit Camera Mode: Camera {len(selected_image_ids)}개가 선택되었습니다. "
        "Remove 버튼으로만 제거할 수 있습니다."
    )


def finish_camera_edit_mode(context, state) -> tuple[int, ...]:
    current_camera_edit_session(context, state, strict=True)
    cameras = validate_managed_camera_scene(context, state)
    selected = tuple(
        sorted(image_id for image_id, obj in cameras.items() if obj.select_get())
    )
    lock_managed_camera_objects(context, state.dataset_uuid)
    _SESSIONS.pop(_scene_key(context), None)
    settings = context.scene.colmap_import
    settings.camera_edit_mode_active = False
    settings.camera_target_count = 0
    settings.remove_target = "NONE"
    return selected


def force_end_camera_mode(context, dataset_uuid: str | None = None) -> None:
    lock_managed_camera_objects(context, dataset_uuid)
    _SESSIONS.pop(_scene_key(context), None)
    settings = getattr(context.scene, "colmap_import", None)
    if settings is not None:
        settings.camera_edit_mode_active = False
        settings.camera_target_count = 0
        if not settings.point_edit_mode_active:
            settings.remove_target = "NONE"


def clear_camera_edit_sessions(context=None) -> None:
    if context is not None:
        try:
            force_end_camera_mode(context)
        except (RuntimeError, ValueError):
            # Extension 해제 또는 파일 전환 중 context가 불완전하면 세션만 폐기합니다.
            pass
    _SESSIONS.clear()


def abandon_camera_edit_sessions_for_global_undo() -> None:
    """이전 Camera Object를 잠그지 않고 Edit Registry만 폐기합니다."""

    _SESSIONS.clear()
