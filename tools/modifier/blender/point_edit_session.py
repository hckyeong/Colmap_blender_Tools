"""관리 Point 선택 모드의 세션과 수명주기를 관리합니다."""

from __future__ import annotations

from dataclasses import dataclass

import bpy

from .managed_scene import (
    iter_managed_point_objects,
    iter_point_display_modifiers,
    require_point_display_modifier,
)
from .ownership import DATASET_UUID_KEY, OWNER_KEY, OWNER_TOKEN, RECORD_KIND_KEY
from .selection import (
    SelectionSnapshot,
    clear_snapshot,
    collect_selected_point_ids,
    collect_selected_point_ids_from_object_mesh,
    lock_managed_point_objects,
    managed_point_object,
    observing_images_for_points,
    set_snapshot,
)


@dataclass(frozen=True)
class EditPointSession:
    dataset_uuid: str
    scene_pointer: int
    point_object_pointer: int
    point_mesh_pointer: int
    source_fingerprint: str
    edit_revision: int
    index_revision: int
    history_hash: str


_SESSIONS: dict[int, EditPointSession] = {}
_PROGRAMMATIC_ENDS: set[int] = set()
# 지연 복원 실행은 coordinator가 담당하고 세션 진입은 이 저장소를 읽기만 합니다.
_PENDING_RESTORES: dict[int, tuple[EditPointSession, str]] = {}


def _scene_key(context) -> int:
    return context.scene.as_pointer()


def _point_display_modifier(point_object):
    """기존 내부 호출 경로를 유지하는 관리 Modifier 조회 wrapper입니다."""
    return require_point_display_modifier(point_object)


def _set_point_display_edit_visibility(point_object, visible: bool) -> None:
    """Edit Mode에서 평가된 Point Cloud 표시 여부를 전환하고 Viewport를 갱신합니다."""
    modifier = _point_display_modifier(point_object)
    modifier.show_in_editmode = visible
    point_object.update_tag(refresh={"DATA"})
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.type == "VIEW_3D":
                area.tag_redraw()


def _restore_point_display_visibility(context, dataset_uuid: str | None = None) -> None:
    """오류 복구 경계에서도 관리 Point 표시를 Object Mode 기본값으로 되돌립니다."""
    for point_object in iter_managed_point_objects(context, dataset_uuid):
        for modifier in iter_point_display_modifiers(point_object):
            modifier.show_in_editmode = True
        point_object.update_tag(refresh={"DATA"})
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.type == "VIEW_3D":
                area.tag_redraw()


def current_edit_session(context, state, *, strict: bool = False) -> EditPointSession | None:
    session = _SESSIONS.get(_scene_key(context))
    settings = context.scene.colmap_import
    if session is None:
        if strict and settings.point_edit_mode_active:
            raise ValueError("Edit Point Mode 상태가 손상되었습니다. 모드를 다시 시작하십시오.")
        return None
    token_valid = (
        session.dataset_uuid == state.dataset_uuid
        and session.source_fingerprint == state.source.source_fingerprint
        and session.edit_revision == state.edit_revision
        and session.index_revision == state.index_revision
        and session.history_hash == state.history_hash
    )
    try:
        point_object = managed_point_object(context, state.dataset_uuid)
    except ValueError:
        point_object = None
    mode_valid = (
        point_object is not None
        and point_object.as_pointer() == session.point_object_pointer
        and point_object.data.as_pointer() == session.point_mesh_pointer
        and context.active_object is point_object
        and point_object.mode == "EDIT"
    )
    if token_valid and mode_valid:
        return session
    if strict or settings.point_edit_mode_active:
        raise ValueError(
            "Edit Point Mode의 Object, Mesh 또는 revision이 현재 상태와 일치하지 않습니다."
        )
    return None


def _clear_point_edit_selection(context, point_object) -> None:
    """현재 Edit BMesh 선택을 지우고 원본 Mesh까지 한 번 동기화합니다."""
    if context.active_object is not point_object:
        raise RuntimeError("관리 Point Object가 활성 Object가 아닙니다.")
    if (
        point_object.get(OWNER_KEY) != OWNER_TOKEN
        or point_object.get(RECORD_KIND_KEY) != "POINT_MESH"
    ):
        raise RuntimeError("선택 초기화 대상이 관리 Point Object가 아닙니다.")
    if point_object.mode != "EDIT":
        raise RuntimeError("Point 선택 초기화는 Edit Mode에서만 실행할 수 있습니다.")

    bpy.ops.mesh.select_mode(type="VERT")
    if tuple(context.tool_settings.mesh_select_mode) != (True, False, False):
        raise RuntimeError("Vertex Select Mode를 활성화하지 못했습니다.")
    if bpy.ops.mesh.select_all(action="DESELECT") != {"FINISHED"}:
        raise RuntimeError("이전 Point 선택을 해제하지 못했습니다.")
    if not point_object.update_from_editmode():
        raise RuntimeError("Edit Mesh 선택 상태를 원본 Mesh에 동기화하지 못했습니다.")


def start_point_edit_mode(context, state) -> None:
    settings = context.scene.colmap_import
    if settings.camera_edit_mode_active:
        raise ValueError("Edit Camera Mode를 먼저 종료하십시오.")
    if _scene_key(context) in _PENDING_RESTORES:
        raise ValueError("외부 모드 종료에서 발견된 Point Mesh를 복원하는 중입니다.")
    if state.edit_revision != state.index_revision:
        raise ValueError("Runtime 모델 revision과 관계 인덱스 revision이 일치하지 않습니다.")
    if context.mode != "OBJECT":
        raise ValueError("다른 Object의 Edit Mode를 종료한 뒤 다시 실행하십시오.")
    point_object = managed_point_object(context, state.dataset_uuid)
    collect_selected_point_ids_from_object_mesh(
        context,
        state,
        allow_empty=True,
    )
    try:
        for obj in context.view_layer.objects:
            obj.select_set(False)
        point_object.hide_select = False
        point_object.select_set(True)
        context.view_layer.objects.active = point_object
        # 평가된 Point Cloud 전체가 활성 Object의 선택색으로 보이지 않도록 숨기고,
        # Edit Mesh 원본 정점만 실제 선택 상태를 표시하게 합니다.
        _set_point_display_edit_visibility(point_object, False)
        if bpy.ops.object.mode_set(mode="EDIT") != {"FINISHED"}:
            raise RuntimeError("관리 Point Object의 Edit Mode에 진입하지 못했습니다.")
        _clear_point_edit_selection(context, point_object)
    except Exception:
        try:
            force_end_point_mode(context, state.dataset_uuid)
        except (RuntimeError, ValueError):
            # 원래 진입 실패 원인을 유지하고 호출자에게 전달합니다.
            pass
        raise

    # 동기화가 끝난 새 세션만 이전 Snapshot을 폐기하고 Runtime 상태로 확정합니다.
    clear_snapshot(state.dataset_uuid)
    _SESSIONS[_scene_key(context)] = EditPointSession(
        dataset_uuid=state.dataset_uuid,
        scene_pointer=context.scene.as_pointer(),
        point_object_pointer=point_object.as_pointer(),
        point_mesh_pointer=point_object.data.as_pointer(),
        source_fingerprint=state.source.source_fingerprint,
        edit_revision=state.edit_revision,
        index_revision=state.index_revision,
        history_hash=state.history_hash,
    )
    settings.point_edit_mode_active = True
    settings.camera_edit_mode_active = False
    settings.remove_target = "POINTS"
    settings.selected_point_count = 0
    settings.observing_camera_count = 0
    settings.camera_target_count = 0
    settings.point_selection_status = "Edit Point Mode: 정점을 선택한 뒤 Remove를 실행하십시오."


def finish_point_edit_mode(
    context,
    state,
    *,
    selected_point_ids: tuple[int, ...] | None = None,
    observing_image_ids: tuple[int, ...] | None = None,
) -> tuple[int, ...]:
    """선택을 snapshot으로 저장하고 Object Mode와 잠금 상태로 복귀합니다."""
    current_edit_session(context, state, strict=True)
    point_object = managed_point_object(context, state.dataset_uuid)
    if selected_point_ids is None:
        selected_point_ids = collect_selected_point_ids(
            context,
            state,
            allow_empty=True,
        )
    if observing_image_ids is None:
        observing_image_ids = (
            observing_images_for_points(state, selected_point_ids)
            if selected_point_ids
            else ()
        )
    snapshot = None
    if selected_point_ids:
        snapshot = SelectionSnapshot(
            dataset_uuid=state.dataset_uuid,
            scene_pointer=context.scene.as_pointer(),
            point_object_pointer=point_object.as_pointer(),
            point_mesh_pointer=point_object.data.as_pointer(),
            selected_point3d_ids=selected_point_ids,
            observing_image_ids=observing_image_ids,
            source_fingerprint=state.source.source_fingerprint,
            edit_revision=state.edit_revision,
            index_revision=state.index_revision,
            history_hash=state.history_hash,
        )
    # Camera Mode가 종료 시 Object 선택을 지우는 것처럼 Point Mode도 Snapshot을
    # 만든 뒤 Edit Mesh 선택을 지워 다음 세션에 화면 선택 상태를 넘기지 않습니다.
    _clear_point_edit_selection(context, point_object)
    scene_key = _scene_key(context)
    _PROGRAMMATIC_ENDS.add(scene_key)
    try:
        bpy.ops.object.mode_set(mode="OBJECT")
    finally:
        _PROGRAMMATIC_ENDS.discard(scene_key)
    _set_point_display_edit_visibility(point_object, True)
    lock_managed_point_objects(context, state.dataset_uuid)
    _SESSIONS.pop(scene_key, None)
    settings = context.scene.colmap_import
    settings.point_edit_mode_active = False
    settings.remove_target = "NONE"
    settings.selected_point_count = len(selected_point_ids)
    settings.observing_camera_count = len(observing_image_ids)
    if snapshot is None:
        clear_snapshot(state.dataset_uuid)
    else:
        set_snapshot(snapshot)
    return selected_point_ids


def force_end_point_mode(context, dataset_uuid: str | None = None) -> None:
    """Mesh를 검증하지 않고 관리 Edit Mode를 종료해 잠금 불변 조건을 복원합니다."""
    active = getattr(context, "active_object", None)
    try:
        if (
            active is not None
            and active.mode == "EDIT"
            and active.get(OWNER_KEY) == OWNER_TOKEN
            and active.get(RECORD_KIND_KEY) == "POINT_MESH"
            and (
                dataset_uuid is None
                or active.get(DATASET_UUID_KEY) == dataset_uuid
            )
        ):
            scene_key = _scene_key(context)
            _PROGRAMMATIC_ENDS.add(scene_key)
            try:
                bpy.ops.object.mode_set(mode="OBJECT")
            finally:
                _PROGRAMMATIC_ENDS.discard(scene_key)
    finally:
        _restore_point_display_visibility(context, dataset_uuid)
    lock_managed_point_objects(context, dataset_uuid)
    _SESSIONS.pop(_scene_key(context), None)
    settings = getattr(context.scene, "colmap_import", None)
    if settings is not None:
        settings.point_edit_mode_active = False
        if not settings.camera_edit_mode_active:
            settings.remove_target = "NONE"


def clear_point_edit_sessions(context=None) -> None:
    if context is not None:
        try:
            force_end_point_mode(context)
        except (RuntimeError, ValueError):
            # Extension 해제 중 유효한 View Layer context가 없으면 세션만 폐기합니다.
            pass
    _SESSIONS.clear()
    _PROGRAMMATIC_ENDS.clear()


def abandon_point_edit_sessions_for_global_undo() -> None:
    """이전 Mesh/Object를 정리하지 않고 Point Edit Registry만 폐기합니다."""

    _SESSIONS.clear()
    _PROGRAMMATIC_ENDS.clear()
    _PENDING_RESTORES.clear()


def _session_matches_state(session: EditPointSession, state) -> bool:
    return (
        session.dataset_uuid == state.dataset_uuid
        and session.source_fingerprint == state.source.source_fingerprint
        and session.edit_revision == state.edit_revision
        and session.index_revision == state.index_revision
        and session.history_hash == state.history_hash
    )
