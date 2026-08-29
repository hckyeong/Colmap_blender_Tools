"""Camera Review Pending 저장, Viewport overlay와 Apply 경계를 관리합니다."""

from __future__ import annotations

from dataclasses import dataclass
import json

from ..core.pending_camera_filters import (
    PendingCameraFilterToken,
    PendingCameraFilters,
    add_pending_image,
    deserialize_pending_filters,
    remove_pending_image,
    serialize_pending_filters,
    validate_pending_token,
)
from ..core.edits import validate_image_observation_removal_request
from .managed_scene import (
    iter_managed_camera_objects,
    managed_camera_map,
    require_managed_point_object,
)
from .runtime_state import (
    cached_runtime_for_pending,
    ensure_runtime,
)
from .scene_elements import PENDING_FILTER_ATTRIBUTE


PENDING_STATUS_EMPTY = "EMPTY"
PENDING_STATUS_READY = "READY"
PENDING_STATUS_STALE = "STALE"
PENDING_STATUS_APPLYING = "APPLYING"
PENDING_CAMERA_OWNER_KEY = "colmap_pending_filter_owner"
PENDING_CAMERA_PREVIOUS_HIDE_KEY = "colmap_pending_previous_hidden"


@dataclass
class _PendingOverlayRuntime:
    pending: PendingCameraFilters
    point_vertex_indices: dict[int, int]
    applying: bool = False


_RUNTIMES: dict[int, _PendingOverlayRuntime] = {}


def _scene_key(context) -> int:
    return context.scene.as_pointer()


def _token_from_state(state) -> PendingCameraFilterToken:
    return PendingCameraFilterToken(
        dataset_uuid=state.dataset_uuid,
        source_fingerprint=state.source.source_fingerprint,
        edit_revision=state.edit_revision,
        index_revision=state.index_revision,
        history_hash=state.history_hash,
    )


def _validate_with_state(pending: PendingCameraFilters, state) -> None:
    validate_pending_token(
        pending.token,
        dataset_uuid=state.dataset_uuid,
        source_fingerprint=state.source.source_fingerprint,
        edit_revision=state.edit_revision,
        index_revision=state.index_revision,
        history_hash=state.history_hash,
    )


def _raw_pending_image_count(value: str) -> int:
    try:
        raw = json.loads(value or "{}")
        image_ids = raw.get("image_ids", ())
        if not isinstance(image_ids, list):
            return 0
        return len(image_ids)
    except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
        return 0


def has_pending_filter_data(settings) -> bool:
    """Panel과 interlock이 전체 모델 검사 없이 저장 Pending 존재를 판정합니다."""
    return bool(
        settings.review_pending_filter_json
        and settings.review_pending_filter_json != "{}"
    )


def require_no_pending_filters(context, action: str) -> None:
    """Pending 또는 stale JSON이 남아 있으면 다른 구조 편집을 거부합니다."""
    settings = context.scene.colmap_import
    if has_pending_filter_data(settings):
        raise ValueError(
            f"Pending Camera Filter가 있어 {action}을(를) 실행할 수 없습니다. "
            "Apply Pending Filters 또는 Clear Pending Filters를 먼저 실행하십시오."
        )


def _point_attribute(point_object):
    attribute = point_object.data.attributes.get(PENDING_FILTER_ATTRIBUTE)
    if (
        attribute is None
        or attribute.domain != "POINT"
        or attribute.data_type != "BOOLEAN"
        or len(attribute.data) != len(point_object.data.vertices)
    ):
        raise RuntimeError(
            "Point Mesh의 Pending 표시 Attribute가 없거나 손상되었습니다. 다시 Import하십시오."
        )
    return attribute


def _point_vertex_indices(state) -> dict[int, int]:
    return {
        point_id: index
        for index, point_id in enumerate(state.point_id_order)
    }


def _set_point_flags(
    point_object,
    point_vertex_indices: dict[int, int],
    point_ids,
    value: bool,
) -> None:
    attribute = _point_attribute(point_object)
    missing = [
        point_id
        for point_id in point_ids
        if point_id not in point_vertex_indices
    ]
    if missing:
        raise RuntimeError(
            f"Point Mesh에 없는 Pending POINT3D_ID가 있습니다: {missing[:10]}"
        )
    for point_id in point_ids:
        attribute.data[point_vertex_indices[point_id]].value = value
    point_object.data.update()


def _hide_pending_camera(camera, dataset_uuid: str) -> None:
    if camera.get(PENDING_CAMERA_OWNER_KEY) != dataset_uuid:
        camera[PENDING_CAMERA_PREVIOUS_HIDE_KEY] = bool(camera.hide_get())
        camera[PENDING_CAMERA_OWNER_KEY] = dataset_uuid
    camera.hide_set(True)


def _restore_pending_camera(camera, dataset_uuid: str) -> None:
    if camera.get(PENDING_CAMERA_OWNER_KEY) != dataset_uuid:
        return
    previous_hidden = bool(camera.get(PENDING_CAMERA_PREVIOUS_HIDE_KEY, False))
    camera.hide_set(previous_hidden)
    if PENDING_CAMERA_PREVIOUS_HIDE_KEY in camera:
        del camera[PENDING_CAMERA_PREVIOUS_HIDE_KEY]
    if PENDING_CAMERA_OWNER_KEY in camera:
        del camera[PENDING_CAMERA_OWNER_KEY]


def _normalized_pending_order(value, image_ids) -> tuple[int, ...]:
    """저장된 추가 순서를 Pending IMAGE_ID 집합에 맞춰 안전하게 정규화합니다."""

    if isinstance(value, str):
        try:
            raw = json.loads(value or "[]")
        except (TypeError, ValueError, json.JSONDecodeError):
            raw = []
    elif isinstance(value, (list, tuple)):
        raw = value
    else:
        raw = []
    allowed = set(image_ids)
    ordered: list[int] = []
    seen: set[int] = set()
    for image_id in raw:
        if type(image_id) is int and image_id in allowed and image_id not in seen:
            ordered.append(image_id)
            seen.add(image_id)
    for image_id in image_ids:
        if image_id not in seen:
            ordered.append(image_id)
            seen.add(image_id)
    return tuple(ordered)


def _write_settings(
    settings,
    pending: PendingCameraFilters | None,
    status: str,
    *,
    addition_order=None,
) -> None:
    """Pending 권위값, 추가 순서와 파생 UI Count를 함께 기록합니다."""

    settings.review_pending_filter_json = serialize_pending_filters(pending)
    settings.review_pending_camera_count = len(pending.image_ids) if pending else 0
    settings.review_pending_point_count = (
        len(pending.preview_point3d_ids) if pending else 0
    )
    settings.review_pending_filter_status = status
    normalized_order = _normalized_pending_order(
        settings.review_pending_filter_order_json
        if addition_order is None
        else addition_order,
        pending.image_ids if pending else (),
    )
    settings.review_pending_filter_order_json = json.dumps(
        list(normalized_order),
        separators=(",", ":"),
    )


def _apply_viewport_overlay(
    context,
    state,
    pending: PendingCameraFilters,
    *,
    previous: PendingCameraFilters | None = None,
    point_vertex_indices: dict[int, int] | None = None,
    cameras=None,
) -> dict[int, int]:
    point_object = require_managed_point_object(context, state.dataset_uuid)
    point_indices = (
        point_vertex_indices
        if point_vertex_indices is not None
        else _point_vertex_indices(state)
    )
    previous_points = (
        previous.preview_point3d_ids
        if previous is not None
        else frozenset()
    )
    removed_points = previous_points - pending.preview_point3d_ids
    added_points = pending.preview_point3d_ids - previous_points
    if removed_points:
        _set_point_flags(point_object, point_indices, removed_points, False)
    if added_points:
        _set_point_flags(point_object, point_indices, added_points, True)

    if cameras is None:
        cameras = managed_camera_map(context, state.dataset_uuid)
    previous_images = set(previous.image_ids) if previous is not None else set()
    for image_id in previous_images - set(pending.image_ids):
        camera = cameras.get(image_id)
        if camera is not None:
            _restore_pending_camera(camera, state.dataset_uuid)
    for image_id in set(pending.image_ids) - previous_images:
        camera = cameras.get(image_id)
        if camera is None:
            raise RuntimeError(f"Pending IMAGE_ID {image_id}의 Camera Object가 없습니다.")
        _hide_pending_camera(camera, state.dataset_uuid)
    return point_indices


def _restore_all_camera_visibility(context, dataset_uuid: str) -> None:
    for camera in iter_managed_camera_objects(context, dataset_uuid):
        _restore_pending_camera(camera, dataset_uuid)


def _clear_all_point_flags(context, dataset_uuid: str) -> None:
    point_object = require_managed_point_object(context, dataset_uuid)
    attribute = _point_attribute(point_object)
    for item in attribute.data:
        item.value = False
    point_object.data.update()


def cached_pending_filters(context) -> PendingCameraFilters | None:
    runtime = _RUNTIMES.get(_scene_key(context))
    if runtime is None or runtime.applying:
        return None
    return runtime.pending


def current_camera_is_pending(context) -> bool:
    """현재 Review Camera가 Pending 집합에 포함되는지 O(1) 상태로 판정합니다."""

    pending = cached_pending_filters(context)
    if pending is None:
        return False
    from .camera_review_session import current_review_session

    session = current_review_session(context)
    return bool(
        session is not None
        and session.current_image_id is not None
        and session.current_image_id in pending.image_ids
    )


def queue_current_camera(context) -> PendingCameraFilters:
    """현재 Camera를 권위 데이터 변경 없이 Pending overlay에 추가합니다."""
    state = cached_runtime_for_pending(context.scene)
    settings = context.scene.colmap_import
    from .camera_review_session import (
        current_review_session,
        set_pending_review_overlay,
    )

    session = current_review_session(context)
    if session is None or session.current_image_id is None:
        raise ValueError("Pending에 추가할 현재 Camera가 없습니다.")
    if (
        session.dataset_uuid != state.dataset_uuid
        or session.source_fingerprint != state.source.source_fingerprint
        or session.edit_revision != state.edit_revision
        or session.index_revision != state.index_revision
    ):
        raise ValueError("Camera Review와 Runtime token이 일치하지 않습니다.")

    runtime = _RUNTIMES.get(_scene_key(context))
    if runtime is None:
        pending = deserialize_pending_filters(
            settings.review_pending_filter_json,
            state.indexes,
        )
        if pending is None:
            pending = PendingCameraFilters(_token_from_state(state), (), frozenset())
        _validate_with_state(pending, state)
    else:
        if runtime.applying:
            raise ValueError("Pending Camera Filter를 적용하는 중입니다.")
        pending = runtime.pending
        _validate_with_state(pending, state)

    image_id = session.current_image_id
    if image_id in pending.image_ids:
        raise ValueError(f"IMAGE_ID {image_id}는 이미 Pending 상태입니다.")
    # 실제 다중 센서 Rig 제한은 Point·컨테이너 삭제 계획을 만들기 전에 검사합니다.
    validate_image_observation_removal_request(
        state.current_reconstruction,
        state.indexes,
        (image_id,),
    )
    updated = add_pending_image(pending, image_id, state.indexes)
    addition_order = _normalized_pending_order(
        settings.review_pending_filter_order_json,
        pending.image_ids,
    )

    from .camera_review_images import evict_review_image
    from .camera_review_timeline import (
        capture_timeline_state,
        remove_pending_camera_marker,
        restore_timeline_state,
    )

    timeline_snapshot = capture_timeline_state(session)
    old_current_index = session.current_index
    old_current_image_id = session.current_image_id
    old_image_warnings = list(session.image_warnings)
    point_indices = (
        runtime.point_vertex_indices
        if runtime is not None
        else _point_vertex_indices(state)
    )
    try:
        _apply_viewport_overlay(
            context,
            state,
            updated,
            previous=pending,
            point_vertex_indices=point_indices,
            cameras=session.camera_objects,
        )
        warnings = list(evict_review_image(session, image_id))
        warnings.extend(set_pending_review_overlay(context, session, updated.image_ids))
        warnings.extend(remove_pending_camera_marker(session, image_id))
    except Exception as original_error:
        rollback_failures: list[str] = []
        try:
            _apply_viewport_overlay(
                context,
                state,
                pending,
                previous=updated,
                point_vertex_indices=point_indices,
                cameras=session.camera_objects,
            )
        except Exception as exc:
            rollback_failures.append(f"Viewport overlay 복구 실패: {exc}")
        session.current_index = old_current_index
        session.current_image_id = old_current_image_id
        session.image_warnings = old_image_warnings
        try:
            set_pending_review_overlay(context, session, pending.image_ids)
        except Exception as exc:
            rollback_failures.append(f"Camera Review pair 복구 실패: {exc}")
        try:
            restore_timeline_state(session, timeline_snapshot)
        except Exception as exc:
            rollback_failures.append(f"Timeline 복구 실패: {exc}")
        _RUNTIMES[_scene_key(context)] = _PendingOverlayRuntime(
            pending,
            point_indices,
        )
        if rollback_failures:
            raise RuntimeError(
                "Pending 등록 실패 후 복구에도 실패했습니다. "
                f"원래 오류: {original_error}; "
                f"복구 오류: {'; '.join(rollback_failures)}"
            ) from original_error
        raise

    _RUNTIMES[_scene_key(context)] = _PendingOverlayRuntime(updated, point_indices)
    _write_settings(
        settings,
        updated,
        PENDING_STATUS_READY,
        addition_order=(*addition_order, image_id),
    )
    settings.review_status = (
        f"Pending Camera {len(updated.image_ids)}개, Point3D "
        f"{len(updated.preview_point3d_ids)}개"
    )
    if warnings:
        settings.warning_summary = "\n".join(warnings)
    return updated


def _remove_pending_filter(
    context,
    image_id: int,
    addition_order,
) -> tuple[PendingCameraFilters, tuple[str, ...]]:
    """Pending 하나의 표시·Review·Timeline을 원자적으로 복원합니다."""

    settings = context.scene.colmap_import
    if settings.review_pending_filter_status != PENDING_STATUS_READY:
        raise ValueError("Ready 상태의 Pending Camera Filter만 취소할 수 있습니다.")
    runtime = _RUNTIMES.get(_scene_key(context))
    if runtime is None or runtime.applying:
        raise ValueError("취소할 Pending Camera Filter Runtime이 없습니다.")
    state = cached_runtime_for_pending(context.scene)
    pending = runtime.pending
    _validate_with_state(pending, state)
    updated = remove_pending_image(pending, image_id, state.indexes)
    if updated is pending:
        raise ValueError(f"IMAGE_ID {image_id}는 Pending 상태가 아닙니다.")

    from .camera_review_session import (
        current_review_session,
        set_pending_review_overlay,
    )
    from .camera_review_timeline import (
        build_or_refresh_timeline,
        capture_timeline_state,
        remove_pending_camera_marker,
        restore_timeline_state,
    )

    session = current_review_session(context)
    if session is None:
        raise ValueError("Pending Camera를 복원할 Camera Review Session이 없습니다.")
    timeline_snapshot = capture_timeline_state(session)
    old_current_index = session.current_index
    old_current_image_id = session.current_image_id
    old_image_warnings = list(session.image_warnings)
    try:
        _apply_viewport_overlay(
            context,
            state,
            updated,
            previous=pending,
            point_vertex_indices=runtime.point_vertex_indices,
            cameras=session.camera_objects,
        )
        session.current_index = session.review_index.image_id_to_index[image_id]
        session.current_image_id = image_id
        warnings = list(
            set_pending_review_overlay(context, session, updated.image_ids)
        )
        if session.timeline_refresh_enabled:
            warnings.extend(build_or_refresh_timeline(context, session))
            for pending_image_id in updated.image_ids:
                warnings.extend(
                    remove_pending_camera_marker(session, pending_image_id)
                )
    except Exception as original_error:
        rollback_failures: list[str] = []
        try:
            _apply_viewport_overlay(
                context,
                state,
                pending,
                previous=updated,
                point_vertex_indices=runtime.point_vertex_indices,
                cameras=session.camera_objects,
            )
        except Exception as exc:
            rollback_failures.append(f"Viewport overlay 복구 실패: {exc}")
        session.current_index = old_current_index
        session.current_image_id = old_current_image_id
        session.image_warnings = old_image_warnings
        try:
            set_pending_review_overlay(context, session, pending.image_ids)
        except Exception as exc:
            rollback_failures.append(f"Camera Review pair 복구 실패: {exc}")
        try:
            restore_timeline_state(session, timeline_snapshot)
        except Exception as exc:
            rollback_failures.append(f"Timeline 복구 실패: {exc}")
        if rollback_failures:
            raise RuntimeError(
                "Pending 취소 실패 후 복구에도 실패했습니다. "
                f"원래 오류: {original_error}; "
                f"복구 오류: {'; '.join(rollback_failures)}"
            ) from original_error
        raise

    if updated.image_ids:
        _RUNTIMES[_scene_key(context)] = _PendingOverlayRuntime(
            updated,
            runtime.point_vertex_indices,
        )
        _write_settings(
            settings,
            updated,
            PENDING_STATUS_READY,
            addition_order=addition_order,
        )
    else:
        _RUNTIMES.pop(_scene_key(context), None)
        _write_settings(settings, None, PENDING_STATUS_EMPTY)
    settings.review_status = (
        f"마지막 Pending Camera IMAGE_ID {image_id}를 복원했습니다."
    )
    if warnings:
        settings.warning_summary = "\n".join(warnings)
    return updated, tuple(warnings)


def undo_last_pending_filter(
    context,
) -> tuple[PendingCameraFilters, tuple[str, ...]]:
    """사용자가 가장 최근에 추가한 Pending Camera 하나를 취소합니다."""

    runtime = _RUNTIMES.get(_scene_key(context))
    if runtime is None or runtime.applying:
        raise ValueError("되돌릴 Pending Camera Filter가 없습니다.")
    order = _normalized_pending_order(
        context.scene.colmap_import.review_pending_filter_order_json,
        runtime.pending.image_ids,
    )
    if not order:
        raise ValueError("되돌릴 Pending Camera 추가 순서가 없습니다.")
    return _remove_pending_filter(context, order[-1], order[:-1])


def unqueue_current_camera(
    context,
) -> tuple[PendingCameraFilters, tuple[str, ...]]:
    """현재 Review Camera가 Pending이면 해당 항목만 취소합니다."""

    from .camera_review_session import current_review_session

    session = current_review_session(context)
    if session is None or session.current_image_id is None:
        raise ValueError("Pending에서 제외할 현재 Camera가 없습니다.")
    runtime = _RUNTIMES.get(_scene_key(context))
    if runtime is None:
        raise ValueError("취소할 Pending Camera Filter가 없습니다.")
    image_id = session.current_image_id
    order = _normalized_pending_order(
        context.scene.colmap_import.review_pending_filter_order_json,
        runtime.pending.image_ids,
    )
    return _remove_pending_filter(
        context,
        image_id,
        tuple(item for item in order if item != image_id),
    )


def restore_pending_filters(context, *, state=None, session=None):
    """저장 JSON이 유효할 때만 Pending overlay를 복원하고 stale은 잠급니다."""
    settings = context.scene.colmap_import
    if not has_pending_filter_data(settings):
        _write_settings(settings, None, PENDING_STATUS_EMPTY)
        _RUNTIMES.pop(_scene_key(context), None)
        if session is not None and session.pending_image_ids:
            from .camera_review_session import set_pending_review_overlay

            set_pending_review_overlay(context, session, ())
        return None
    if state is None:
        state = ensure_runtime(context.scene)
    try:
        pending = deserialize_pending_filters(
            settings.review_pending_filter_json,
            state.indexes,
        )
        if pending is None:
            raise ValueError("Pending JSON에 IMAGE_ID가 없습니다.")
        _validate_with_state(pending, state)
        if session is None:
            from .camera_review_session import current_review_session

            session = current_review_session(context)
        point_indices = _apply_viewport_overlay(context, state, pending)
        if session is not None:
            from .camera_review_images import evict_review_image
            from .camera_review_session import set_pending_review_overlay

            warnings: list[str] = []
            for image_id in pending.image_ids:
                warnings.extend(evict_review_image(session, image_id))
            warnings.extend(set_pending_review_overlay(context, session, pending.image_ids))
            if warnings:
                settings.warning_summary = "\n".join(warnings)
        _RUNTIMES[_scene_key(context)] = _PendingOverlayRuntime(
            pending,
            point_indices,
        )
        _write_settings(settings, pending, PENDING_STATUS_READY)
        return pending
    except Exception as exc:
        _RUNTIMES.pop(_scene_key(context), None)
        settings.review_pending_camera_count = _raw_pending_image_count(
            settings.review_pending_filter_json
        )
        settings.review_pending_point_count = 0
        settings.review_pending_filter_status = PENDING_STATUS_STALE
        settings.review_status = (
            f"Pending Camera Filter가 stale 상태입니다. Clear만 허용됩니다: {exc}"
        )
        return None


def pending_for_apply(context, state) -> PendingCameraFilters:
    """Apply 직전 source 검증을 마친 Runtime으로 Pending token을 다시 검증합니다."""
    settings = context.scene.colmap_import
    pending = deserialize_pending_filters(
        settings.review_pending_filter_json,
        state.indexes,
    )
    if pending is None:
        raise ValueError("적용할 Pending Camera Filter가 없습니다.")
    _validate_with_state(pending, state)
    return pending


def begin_pending_apply(context, pending: PendingCameraFilters) -> None:
    """Review 후처리가 Pending을 무시하도록 overlay token만 APPLYING으로 전환합니다."""
    runtime = _RUNTIMES.get(_scene_key(context))
    if runtime is None or runtime.pending != pending:
        raise ValueError("현재 Pending Runtime과 적용 대상이 일치하지 않습니다.")
    runtime.applying = True
    context.scene.colmap_import.review_pending_filter_status = PENDING_STATUS_APPLYING
    from .camera_review_session import current_review_session

    session = current_review_session(context)
    if session is not None and session.pending_image_ids:
        session.pending_image_ids = frozenset()
        session.pending_overlay_revision += 1


def complete_pending_apply(context) -> None:
    """배치 편집 전체 성공 뒤에만 Pending 저장 상태를 제거합니다."""
    _RUNTIMES.pop(_scene_key(context), None)
    _write_settings(
        context.scene.colmap_import,
        None,
        PENDING_STATUS_EMPTY,
    )


def restore_pending_after_failed_apply(context) -> None:
    """실패 rollback 뒤 보존된 JSON의 Pending overlay를 다시 적용합니다."""
    runtime = _RUNTIMES.get(_scene_key(context))
    if runtime is not None:
        runtime.applying = False
    state = cached_runtime_for_pending(context.scene)
    from .camera_review_session import current_review_session

    restore_pending_filters(
        context,
        state=state,
        session=current_review_session(context),
    )


def _clear_stale_pending_filters(context) -> tuple[str, ...]:
    """손상된 overlay 정리는 경고로 남기되 stale 저장 상태는 반드시 해제합니다."""
    settings = context.scene.colmap_import
    warnings: list[str] = []
    dataset_uuid = settings.dataset_uuid
    if dataset_uuid:
        try:
            _clear_all_point_flags(context, dataset_uuid)
        except (AttributeError, ReferenceError, RuntimeError, ValueError) as exc:
            warnings.append(f"stale Pending Point3D 표시 복원 실패: {exc}")
        try:
            _restore_all_camera_visibility(context, dataset_uuid)
        except (AttributeError, ReferenceError, RuntimeError, ValueError) as exc:
            warnings.append(f"stale Pending Camera 표시 복원 실패: {exc}")

    from .camera_review_session import (
        current_review_session,
        set_pending_review_overlay,
    )
    from .camera_review_timeline import build_or_refresh_timeline

    session = current_review_session(context)
    if session is not None:
        timeline_was_active = session.timeline_refresh_enabled
        try:
            warnings.extend(set_pending_review_overlay(context, session, ()))
        except (AttributeError, ReferenceError, RuntimeError, ValueError) as exc:
            warnings.append(f"stale Pending Review 복원 실패: {exc}")
        if timeline_was_active:
            try:
                warnings.extend(build_or_refresh_timeline(context, session))
            except (AttributeError, ReferenceError, RuntimeError, ValueError) as exc:
                warnings.append(f"stale Pending Timeline 복원 실패: {exc}")

    _RUNTIMES.pop(_scene_key(context), None)
    _write_settings(settings, None, PENDING_STATUS_EMPTY)
    settings.review_status = (
        f"stale Pending Camera Filter를 취소했습니다: {warnings[0]}"
        if warnings
        else "stale Pending Camera Filter를 취소했습니다."
    )
    return tuple(warnings)


def clear_pending_filters(context) -> tuple[str, ...]:
    """권위 데이터 변경 없이 Pending Camera·Point 표시와 JSON을 복원합니다."""
    settings = context.scene.colmap_import
    if not has_pending_filter_data(settings):
        _write_settings(settings, None, PENDING_STATUS_EMPTY)
        return ()
    runtime = _RUNTIMES.get(_scene_key(context))
    if (
        settings.review_pending_filter_status == PENDING_STATUS_STALE
        or runtime is None
        and settings.review_pending_filter_status != PENDING_STATUS_READY
    ):
        return _clear_stale_pending_filters(context)
    state = None
    pending = runtime.pending if runtime is not None else None
    try:
        state = cached_runtime_for_pending(context.scene)
    except ValueError:
        state = None

    from .camera_review_session import (
        current_review_session,
        set_pending_review_overlay,
    )
    from .camera_review_timeline import (
        build_or_refresh_timeline,
        capture_timeline_state,
        restore_timeline_state,
    )

    session = current_review_session(context)
    timeline_snapshot = capture_timeline_state(session) if session is not None else None
    timeline_was_active = bool(session and session.timeline_refresh_enabled)
    try:
        if state is not None:
            point_object = require_managed_point_object(context, state.dataset_uuid)
            point_indices = _point_vertex_indices(state)
            point_ids = (
                pending.preview_point3d_ids
                if pending is not None
                else tuple(point_indices)
            )
            _set_point_flags(point_object, point_indices, point_ids, False)
            _restore_all_camera_visibility(context, state.dataset_uuid)
        elif settings.dataset_uuid:
            _clear_all_point_flags(context, settings.dataset_uuid)
            _restore_all_camera_visibility(context, settings.dataset_uuid)

        warnings: list[str] = []
        if session is not None:
            warnings.extend(set_pending_review_overlay(context, session, ()))
            if timeline_was_active:
                warnings.extend(build_or_refresh_timeline(context, session))
    except Exception:
        if state is not None and pending is not None:
            _apply_viewport_overlay(context, state, pending)
            if session is not None:
                set_pending_review_overlay(context, session, pending.image_ids)
        if session is not None and timeline_snapshot is not None:
            restore_timeline_state(session, timeline_snapshot)
        raise

    _RUNTIMES.pop(_scene_key(context), None)
    _write_settings(settings, None, PENDING_STATUS_EMPTY)
    settings.review_status = "Pending Camera Filter를 모두 취소했습니다."
    return tuple(warnings)


def clear_pending_runtime() -> None:
    """파일 전환과 Extension 해제에서 Python 임시 cache만 폐기합니다."""
    _RUNTIMES.clear()


def release_pending_overlay(context) -> tuple[str, ...]:
    """Extension 해제 전에 저장 JSON은 보존하고 Viewport 표시만 복원합니다."""
    settings = getattr(context.scene, "colmap_import", None)
    if settings is None:
        clear_pending_runtime()
        return ()
    warnings: list[str] = []
    dataset_uuid = settings.dataset_uuid
    if dataset_uuid:
        try:
            _clear_all_point_flags(context, dataset_uuid)
        except (AttributeError, ReferenceError, RuntimeError, ValueError) as exc:
            warnings.append(f"Pending Point3D 표시 해제 실패: {exc}")
        try:
            _restore_all_camera_visibility(context, dataset_uuid)
        except (AttributeError, ReferenceError, RuntimeError, ValueError) as exc:
            warnings.append(f"Pending Camera 표시 해제 실패: {exc}")
    clear_pending_runtime()
    return tuple(warnings)


__all__ = [
    "PENDING_STATUS_APPLYING",
    "PENDING_STATUS_EMPTY",
    "PENDING_STATUS_READY",
    "PENDING_STATUS_STALE",
    "begin_pending_apply",
    "cached_pending_filters",
    "clear_pending_filters",
    "clear_pending_runtime",
    "complete_pending_apply",
    "current_camera_is_pending",
    "has_pending_filter_data",
    "pending_for_apply",
    "queue_current_camera",
    "release_pending_overlay",
    "require_no_pending_filters",
    "restore_pending_after_failed_apply",
    "restore_pending_filters",
    "undo_last_pending_filter",
    "unqueue_current_camera",
]
