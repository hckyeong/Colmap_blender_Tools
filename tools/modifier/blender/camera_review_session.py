"""Camera Review의 단일 현재 pair와 Runtime/Scene revision 수명주기를 관리합니다."""

from __future__ import annotations

from dataclasses import dataclass, field

from ..core.camera_review import (
    ReviewIndex,
    build_review_index,
    preferred_index_after_deletion,
)
from .managed_scene import managed_camera_map
from .runtime_state import ensure_runtime


@dataclass
class ReviewSession:
    """Scene Property와 분리된 Camera Review 임시 포인터 상태입니다."""

    scene: object
    scene_pointer: int
    dataset_uuid: str
    source_fingerprint: str
    edit_revision: int
    index_revision: int
    review_index: ReviewIndex
    current_index: int
    current_image_id: int | None
    frame_start: int
    camera_objects: dict[int, object]
    camera_object_pointers: tuple[tuple[int, int], ...]
    working_set_image_ids: set[int] = field(default_factory=set)
    resident_images: dict[int, object] = field(default_factory=dict)
    image_warnings: list[str] = field(default_factory=list)
    camera_background_visibility: dict[int, bool] = field(default_factory=dict)
    background_slots: dict[int, object] = field(default_factory=dict)
    protected_background_slot_pointers: set[tuple[int, int]] = field(
        default_factory=set
    )
    viewport_snapshots: dict[tuple[int, int, int], object] = field(
        default_factory=dict
    )
    render_snapshot: object | None = None
    render_last_applied: object | None = None
    update_guard: bool = False
    timeline_active: bool = False
    timeline_refresh_enabled: bool = False
    timeline_manifest_signature: tuple[tuple[object, ...], ...] = ()
    pending_image_ids: frozenset[int] = frozenset()
    pending_overlay_revision: int = 0


@dataclass
class ImportReviewTransition:
    """Import commit 전 Review 임시 override를 복구 가능하게 중단한 상태입니다."""

    session: ReviewSession
    viewport_snapshots: dict[tuple[int, int, int], object]
    render_snapshot: object | None
    render_last_applied: object | None
    timeline_active: bool
    timeline_refresh_enabled: bool
    timeline_manifest_signature: tuple[tuple[object, ...], ...]
    suspended: bool = True


@dataclass(frozen=True)
class ReviewEditSnapshot:
    """Filter 후처리 실패에서 이전 Review 전체를 재구성하는 snapshot입니다."""

    current_index: int
    current_image_id: int | None
    image_warnings: tuple[str, ...]
    viewport_snapshots: dict[tuple[int, int, int], object]
    render_snapshot: object | None
    render_last_applied: object | None
    review_properties: tuple[tuple[str, object], ...]
    timeline_state: object


_SESSIONS: dict[int, ReviewSession] = {}
_REVIEW_EDIT_PROPERTY_NAMES = (
    "review_current_index",
    "review_current_image_id",
    "review_total_count",
    "review_camera_model",
    "review_image_status",
    "review_exact_projection",
    "review_status",
    "review_vram_window",
    "review_restore_available",
)
_REVIEW_NAVIGATION_PROPERTY_NAMES = (
    "review_total_count",
    "review_current_index",
    "review_current_image_id",
    "review_camera_model",
    "review_exact_projection",
    "review_image_status",
    "review_vram_window",
    "review_status",
)


def _scene_key(context) -> int:
    return context.scene.as_pointer()


def _camera_pointer_token(cameras) -> tuple[tuple[int, int], ...]:
    return tuple(
        sorted((image_id, camera.as_pointer()) for image_id, camera in cameras.items())
    )


def _validated_camera_map(context, state) -> dict[int, object]:
    cameras = managed_camera_map(context, state.dataset_uuid)
    expected_ids = set(state.current_reconstruction.images)
    actual_ids = set(cameras)
    if actual_ids != expected_ids:
        missing = sorted(expected_ids - actual_ids)
        extra = sorted(actual_ids - expected_ids)
        raise ValueError(
            "관리 Camera map과 현재 Runtime Image가 일치하지 않습니다. "
            f"누락 {missing[:10]}, 초과 {extra[:10]}"
        )
    return cameras


def _build_index(settings, state) -> ReviewIndex:
    return build_review_index(
        state.current_reconstruction,
        state.source.images_directory,
        state.dataset_uuid,
        state.source.source_fingerprint,
        state.edit_revision,
        settings.review_frame_start,
        image_path_manifest=state.image_path_manifest,
    )


def _validate_index_metadata(session: ReviewSession, state) -> None:
    review_index = session.review_index
    if (
        session.dataset_uuid != state.dataset_uuid
        or session.source_fingerprint != state.source.source_fingerprint
        or session.edit_revision != state.edit_revision
        or session.index_revision != state.index_revision
        or review_index.dataset_uuid != session.dataset_uuid
        or review_index.source_fingerprint != session.source_fingerprint
        or review_index.edit_revision != session.edit_revision
    ):
        raise ValueError("Camera Review Session과 Runtime revision이 일치하지 않습니다.")


def _validate_current_pair(session: ReviewSession) -> None:
    entries = session.review_index.entries
    visible_count = len(entries) - len(session.pending_image_ids)
    if not entries or visible_count == 0:
        if session.current_index != -1 or session.current_image_id is not None:
            raise ValueError("빈 Camera Review의 현재 pair가 비어 있지 않습니다.")
        return
    if session.current_index < 0 or session.current_index >= len(entries):
        raise ValueError("Camera Review current index가 범위를 벗어났습니다.")
    entry = entries[session.current_index]
    if entry.image_id in session.pending_image_ids:
        raise ValueError("Pending Camera가 Camera Review의 현재 pair로 남아 있습니다.")
    if session.current_image_id != entry.image_id:
        raise ValueError("Camera Review current index와 IMAGE_ID가 일치하지 않습니다.")
    if session.review_index.image_id_to_index.get(session.current_image_id) != (
        session.current_index
    ):
        raise ValueError("Camera Review IMAGE_ID 역방향 index가 현재 pair와 다릅니다.")


def _image_status(entry) -> str:
    if not entry.exact_projection_supported:
        return f"UNSUPPORTED ({entry.camera_model})"
    return entry.path_status


def _sync_scene_properties(session: ReviewSession) -> None:
    settings = session.scene.colmap_import
    entries = session.review_index.entries
    visible_count = len(entries) - len(session.pending_image_ids)
    settings.review_total_count = visible_count
    settings.review_current_index = (
        session.current_index
        - sum(
            1
            for image_id in session.pending_image_ids
            if image_id < session.current_image_id
        )
        if session.current_image_id is not None
        else -1
    )
    settings.review_current_image_id = (
        str(session.current_image_id)
        if session.current_image_id is not None
        else ""
    )
    if session.current_image_id is None:
        settings.review_camera_model = ""
        settings.review_image_status = ""
        settings.review_exact_projection = False
        settings.review_vram_window = ""
        settings.review_status = "생존 Camera가 없어 Camera Review를 종료했습니다."
        return

    entry = entries[session.current_index]
    settings.review_camera_model = entry.camera_model
    current_load_failed = any(
        warning.startswith(f"IMAGE_ID {entry.image_id} 이미지 로드 실패:")
        for warning in session.image_warnings
    )
    settings.review_image_status = (
        "LOAD_FAILED" if current_load_failed else _image_status(entry)
    )
    settings.review_exact_projection = entry.exact_projection_supported
    visible_indices = _visible_window_indices(session, session.current_index)
    settings.review_vram_window = " - ".join(
        str(entries[index].image_id)
        for index in visible_indices
    )
    base = (
        f"Camera {settings.review_current_index + 1}/{visible_count}, "
        f"IMAGE_ID {entry.image_id}"
    )
    settings.review_status = (
        f"{base}; {session.image_warnings[0]}"
        if session.image_warnings
        else base
    )


def sync_review_properties(session: ReviewSession) -> None:
    """timer callback이 현재 pair의 UI Property만 동기화하는 경계입니다."""
    _validate_current_pair(session)
    _sync_scene_properties(session)


def _visible_window_indices(
    session: ReviewSession,
    current_index: int,
) -> tuple[int, ...]:
    entries = session.review_index.entries
    pending = session.pending_image_ids
    previous = current_index - 1
    while previous >= 0 and entries[previous].image_id in pending:
        previous -= 1
    following = current_index + 1
    while following < len(entries) and entries[following].image_id in pending:
        following += 1
    return tuple(
        index
        for index in (previous, current_index, following)
        if 0 <= index < len(entries)
        and entries[index].image_id not in pending
    )


def _next_visible_index(
    session: ReviewSession,
    start_index: int,
    direction: int,
) -> int | None:
    entries = session.review_index.entries
    index = start_index + direction
    while 0 <= index < len(entries):
        if entries[index].image_id not in session.pending_image_ids:
            return index
        index += direction
    return None


def set_pending_review_overlay(
    context,
    session: ReviewSession,
    image_ids,
) -> tuple[str, ...]:
    """Pending 집합을 Review에 적용하고 현재 pair를 가장 가까운 생존 항목으로 옮깁니다."""
    pending = frozenset(int(image_id) for image_id in image_ids)
    unknown = pending - set(session.review_index.image_id_to_index)
    if unknown:
        raise ValueError(f"ReviewIndex에 없는 Pending IMAGE_ID가 있습니다: {sorted(unknown)[:10]}")
    if session.pending_image_ids != pending:
        session.pending_image_ids = pending
        session.pending_overlay_revision += 1

    entries = session.review_index.entries
    if len(pending) == len(entries):
        session.current_index = -1
        session.current_image_id = None
        from .camera_review_images import cleanup_review_images
        from .camera_review_viewport import restore_session_views

        warnings = [*cleanup_review_images(session), *restore_session_views(session)]
        _sync_scene_properties(session)
        return tuple(warnings)

    if (
        session.current_image_id is None
        or session.current_image_id in pending
    ):
        old_index = (
            session.current_index
            if session.current_image_id is not None
            else -1
        )
        target_index = _next_visible_index(session, old_index, 1)
        if target_index is None:
            target_index = _next_visible_index(session, old_index + 1, -1)
        if target_index is None:
            raise RuntimeError("Pending이 아닌 Camera Review 항목을 찾지 못했습니다.")
        session.current_index = target_index
        session.current_image_id = entries[target_index].image_id

    from .camera_review_images import update_working_set
    from .camera_review_timeline import sync_playhead_to_current
    from .camera_review_viewport import update_review_view_for_current

    warnings = list(update_working_set(session, session.current_index))
    _validate_current_pair(session)
    _sync_scene_properties(session)
    update_review_view_for_current(context, session)
    sync_playhead_to_current(session)
    return tuple(warnings)


def _new_pair(
    review_index: ReviewIndex,
    *,
    preferred_image_id: int | None,
    preferred_old_index: int,
) -> tuple[int, int | None]:
    if not review_index.entries:
        return -1, None
    if (
        preferred_image_id is not None
        and preferred_image_id in review_index.image_id_to_index
    ):
        index = review_index.image_id_to_index[preferred_image_id]
        return index, preferred_image_id
    old_index = max(0, preferred_old_index)
    index = preferred_index_after_deletion(old_index, len(review_index.entries))
    if index is None:
        return -1, None
    return index, review_index.entries[index].image_id


def _prepare_initial_working_set(session: ReviewSession) -> None:
    if session.current_image_id is None:
        _sync_scene_properties(session)
        return
    from .camera_review_images import update_working_set

    try:
        update_working_set(session, session.current_index)
    except Exception as exc:
        session.image_warnings = [str(exc)]
    _sync_scene_properties(session)


def initialize_review_session(context, state=None) -> ReviewSession:
    """Import 성공 경계에서 첫 생존 IMAGE_ID로 새 Review Session을 설치합니다."""
    if state is None:
        state = ensure_runtime(context.scene)
    previous_session = _SESSIONS.get(_scene_key(context))
    clear_review_sessions(context)
    protected_slot_pointers = (
        set(previous_session.protected_background_slot_pointers)
        if previous_session is not None
        else set()
    )
    settings = context.scene.colmap_import
    review_index = _build_index(settings, state)
    cameras = _validated_camera_map(context, state)
    current_index = 0 if review_index.entries else -1
    current_image_id = (
        review_index.entries[0].image_id if review_index.entries else None
    )
    session = ReviewSession(
        scene=context.scene,
        scene_pointer=context.scene.as_pointer(),
        dataset_uuid=state.dataset_uuid,
        source_fingerprint=state.source.source_fingerprint,
        edit_revision=state.edit_revision,
        index_revision=state.index_revision,
        review_index=review_index,
        current_index=current_index,
        current_image_id=current_image_id,
        frame_start=settings.review_frame_start,
        camera_objects=cameras,
        camera_object_pointers=_camera_pointer_token(cameras),
        protected_background_slot_pointers=protected_slot_pointers,
    )
    _SESSIONS[session.scene_pointer] = session
    from .camera_review_timeline import attach_session

    attach_session(session)
    settings.review_restore_available = False
    _prepare_initial_working_set(session)
    return session


def _session_is_current(context, session: ReviewSession, state) -> bool:
    if (
        session.scene_pointer != context.scene.as_pointer()
        or session.frame_start != context.scene.colmap_import.review_frame_start
    ):
        return False
    try:
        _validate_index_metadata(session, state)
        _validate_current_pair(session)
        if (
            len(session.camera_objects) != len(session.review_index.entries)
            or len(session.camera_object_pointers) != len(session.camera_objects)
        ):
            return False
        if session.current_image_id is not None:
            camera = session.camera_objects.get(session.current_image_id)
            if camera is None or camera.as_pointer() == 0:
                return False
    except (AttributeError, ReferenceError, RuntimeError, ValueError):
        return False
    return True


def _rebuild_session(context, session: ReviewSession, state) -> ReviewSession:
    """stale revision 또는 Camera map을 현재 Runtime에서 정확히 한 번 재구성합니다."""
    from .camera_review_images import cleanup_review_images

    old_index = session.current_index
    old_image_id = session.current_image_id
    frame_start_changed = (
        session.frame_start
        != context.scene.colmap_import.review_frame_start
    )
    if frame_start_changed:
        session.timeline_active = False
        session.timeline_refresh_enabled = False
        session.timeline_manifest_signature = ()
    try:
        cleanup_review_images(session)
    except (ReferenceError, RuntimeError, ValueError):
        session.resident_images.clear()
        session.working_set_image_ids.clear()

    review_index = _build_index(context.scene.colmap_import, state)
    cameras = _validated_camera_map(context, state)
    current_index, current_image_id = _new_pair(
        review_index,
        preferred_image_id=old_image_id,
        preferred_old_index=old_index,
    )
    session.dataset_uuid = state.dataset_uuid
    session.source_fingerprint = state.source.source_fingerprint
    session.edit_revision = state.edit_revision
    session.index_revision = state.index_revision
    session.review_index = review_index
    session.current_index = current_index
    session.current_image_id = current_image_id
    session.frame_start = context.scene.colmap_import.review_frame_start
    session.camera_objects = cameras
    session.camera_object_pointers = _camera_pointer_token(cameras)
    from .camera_review_timeline import attach_session

    attach_session(session)
    _validate_index_metadata(session, state)
    _validate_current_pair(session)
    _prepare_initial_working_set(session)
    from .camera_review_viewport import (
        restore_session_views,
        update_review_view_for_current,
    )

    if session.current_image_id is None:
        restore_session_views(session)
    else:
        update_review_view_for_current(context, session)
    return session


def ensure_review_session(context, *, state=None) -> ReviewSession:
    """현재 Runtime과 Camera map을 검증하고 stale 세션은 한 번 재구성합니다."""
    if state is None:
        state = ensure_runtime(context.scene)
    session = _SESSIONS.get(_scene_key(context))
    if session is None:
        settings = context.scene.colmap_import
        review_index = _build_index(settings, state)
        cameras = _validated_camera_map(context, state)
        preferred_image_id = None
        if settings.review_current_image_id:
            try:
                preferred_image_id = int(settings.review_current_image_id)
            except ValueError:
                preferred_image_id = None
        current_index, current_image_id = _new_pair(
            review_index,
            preferred_image_id=preferred_image_id,
            preferred_old_index=settings.review_current_index,
        )
        session = ReviewSession(
            scene=context.scene,
            scene_pointer=context.scene.as_pointer(),
            dataset_uuid=state.dataset_uuid,
            source_fingerprint=state.source.source_fingerprint,
            edit_revision=state.edit_revision,
            index_revision=state.index_revision,
            review_index=review_index,
            current_index=current_index,
            current_image_id=current_image_id,
            frame_start=settings.review_frame_start,
            camera_objects=cameras,
            camera_object_pointers=_camera_pointer_token(cameras),
        )
        _SESSIONS[session.scene_pointer] = session
        from .camera_review_timeline import attach_session

        attach_session(session)
        _prepare_initial_working_set(session)
        return session
    if not _session_is_current(context, session, state):
        session = _rebuild_session(context, session, state)
    _validate_current_pair(session)
    _sync_scene_properties(session)
    return session


def current_review_session(
    context,
    *,
    strict: bool = False,
) -> ReviewSession | None:
    """현재 Scene의 세션을 반환하고 strict 모드에서는 모든 token을 검증합니다."""
    session = _SESSIONS.get(_scene_key(context))
    if session is None:
        if strict:
            raise ValueError("현재 Scene에 Camera Review Session이 없습니다.")
        return None
    if strict:
        state = ensure_runtime(context.scene)
        _validate_index_metadata(session, state)
        _validate_current_pair(session)
        cameras = _validated_camera_map(context, state)
        if session.camera_object_pointers != _camera_pointer_token(cameras):
            raise ValueError("Camera Review Session의 관리 Camera map이 stale 상태입니다.")
    return session


def review_change_allowed(context) -> bool:
    settings = getattr(context.scene, "colmap_import", None)
    return bool(
        settings
        and settings.dataset_uuid
        and not settings.point_edit_mode_active
        and not settings.camera_edit_mode_active
    )


def update_review_background_from_settings(settings, context) -> None:
    """현재 Scene의 Review Session에 UI 표시 상태와 Alpha를 전달합니다."""
    if context is None or getattr(context, "scene", None) is None:
        return
    session = current_review_session(context)
    if session is None or session.scene is not context.scene:
        return
    from .camera_review_images import update_review_slot_style

    update_review_slot_style(
        session,
        alpha=float(settings.review_background_alpha),
        enabled=bool(settings.review_background_enabled),
        context=context,
    )


def navigate_review(context, direction: str) -> ReviewSession:
    """Core O(1) clamp helper로 현재 pair와 작업 집합을 원자적으로 전환합니다."""
    if not review_change_allowed(context):
        raise ValueError("Point 또는 Camera Edit Mode 중에는 Camera Review를 변경할 수 없습니다.")
    session = ensure_review_session(context)
    if session.current_image_id is None:
        raise ValueError("탐색할 생존 Camera가 없습니다.")
    if direction == "PREVIOUS":
        target_index = _next_visible_index(session, session.current_index, -1)
    elif direction == "NEXT":
        target_index = _next_visible_index(session, session.current_index, 1)
    else:
        raise ValueError(f"알 수 없는 Camera Review 탐색 방향입니다: {direction}")
    if target_index is None or target_index == session.current_index:
        return session

    from .camera_review_images import update_working_set

    settings = context.scene.colmap_import
    old_index = session.current_index
    old_image_id = session.current_image_id
    old_image_warnings = list(session.image_warnings)
    old_frame = context.scene.frame_current
    old_scene_camera = context.scene.camera
    old_properties = {
        name: getattr(settings, name)
        for name in _REVIEW_NAVIGATION_PROPERTY_NAMES
    }
    working_set_prepared = False
    try:
        # 현재 이미지 로드가 실패하면 pair와 Viewport는 기존 상태를 유지합니다.
        update_working_set(session, target_index)
        working_set_prepared = True
        session.current_index = target_index
        session.current_image_id = session.review_index.entries[target_index].image_id
        _validate_current_pair(session)
        _sync_scene_properties(session)

        from .camera_review_viewport import update_review_view_for_current

        update_review_view_for_current(context, session)
        from .camera_review_timeline import sync_playhead_to_current

        sync_playhead_to_current(session)
        return session
    except Exception as original_error:
        rollback_failures: list[str] = []
        session.current_index = old_index
        session.current_image_id = old_image_id
        if working_set_prepared:
            try:
                update_working_set(session, old_index)
            except Exception as exc:
                rollback_failures.append(f"이미지 작업 집합 복구 실패: {exc}")
        try:
            session.update_guard = True
            context.scene.frame_set(old_frame)
            context.scene.camera = old_scene_camera
        except Exception as exc:
            rollback_failures.append(f"frame/Scene Camera 복구 실패: {exc}")
        finally:
            session.update_guard = False
        try:
            from .camera_review_viewport import update_review_view_for_current

            update_review_view_for_current(context, session)
        except Exception as exc:
            rollback_failures.append(f"Viewport 복구 실패: {exc}")
        session.image_warnings = old_image_warnings
        for name, value in old_properties.items():
            setattr(settings, name, value)
        if rollback_failures:
            raise RuntimeError(
                "Camera Review 전환 실패 후 복구에도 실패했습니다. "
                f"원래 오류: {original_error}; "
                f"복구 오류: {'; '.join(rollback_failures)}"
            ) from original_error
        raise


def begin_import_review_transition(context) -> ImportReviewTransition | None:
    """Scene commit 전에 Review View/Render/background를 복구 가능하게 중단합니다."""
    session = current_review_session(context)
    if session is None:
        return None

    transition = ImportReviewTransition(
        session=session,
        viewport_snapshots=dict(session.viewport_snapshots),
        render_snapshot=session.render_snapshot,
        render_last_applied=session.render_last_applied,
        timeline_active=session.timeline_active,
        timeline_refresh_enabled=session.timeline_refresh_enabled,
        timeline_manifest_signature=session.timeline_manifest_signature,
    )
    from .camera_review_images import cleanup_review_images
    from .camera_review_timeline import detach_session
    from .camera_review_viewport import restore_session_views

    try:
        detach_session(session)
        restore_session_views(session)
        cleanup_review_images(session)
    except Exception:
        rollback_import_review_transition(context, transition)
        raise
    return transition


def rollback_import_review_transition(
    context,
    transition: ImportReviewTransition | None,
) -> None:
    """실패한 Import가 중단 전 Review 논리·Viewport 상태를 다시 활성화합니다."""
    if transition is None or not transition.suspended:
        return
    session = transition.session
    _SESSIONS[session.scene_pointer] = session

    from .camera_review_images import update_working_set

    if session.current_index >= 0:
        update_working_set(session, session.current_index)

    session.viewport_snapshots = dict(transition.viewport_snapshots)
    session.render_snapshot = transition.render_snapshot
    session.render_last_applied = transition.render_last_applied
    if session.viewport_snapshots and session.current_image_id is not None:
        from .camera_review_viewport import update_review_view_for_current

        update_review_view_for_current(context, session)

    session.timeline_active = transition.timeline_active
    session.timeline_refresh_enabled = transition.timeline_refresh_enabled
    session.timeline_manifest_signature = transition.timeline_manifest_signature
    if session.timeline_refresh_enabled:
        from .camera_review_timeline import attach_session

        attach_session(session)
    _sync_scene_properties(session)
    context.scene.colmap_import.review_restore_available = bool(
        session.viewport_snapshots
    )
    transition.suspended = False


def complete_import_review_transition(
    transition: ImportReviewTransition | None,
) -> None:
    """성공한 Scene commit 뒤 구 Review Session을 추가 복원 없이 폐기합니다."""
    if transition is None or not transition.suspended:
        return
    session = transition.session
    if _SESSIONS.get(session.scene_pointer) is session:
        _SESSIONS.pop(session.scene_pointer, None)
    session.viewport_snapshots.clear()
    session.render_snapshot = None
    session.render_last_applied = None
    session.background_slots.clear()
    session.resident_images.clear()
    session.working_set_image_ids.clear()
    transition.suspended = False


def capture_review_edit_snapshot(context) -> ReviewEditSnapshot | None:
    """Filter Scene commit 전에 Review/Timeline 복구 기준을 고정합니다."""
    session = current_review_session(context)
    if session is None:
        return None
    from .camera_review_timeline import capture_timeline_state

    settings = context.scene.colmap_import
    return ReviewEditSnapshot(
        current_index=session.current_index,
        current_image_id=session.current_image_id,
        image_warnings=tuple(session.image_warnings),
        viewport_snapshots=dict(session.viewport_snapshots),
        render_snapshot=session.render_snapshot,
        render_last_applied=session.render_last_applied,
        review_properties=tuple(
            (name, getattr(settings, name))
            for name in _REVIEW_EDIT_PROPERTY_NAMES
        ),
        timeline_state=capture_timeline_state(session),
    )


def restore_review_edit_snapshot(
    context,
    snapshot: ReviewEditSnapshot | None,
) -> ReviewSession | None:
    """권위 Runtime/Scene 복구 뒤 Filter 전 Review/Timeline을 재구성합니다."""
    clear_review_sessions(context)
    if snapshot is None:
        return None
    settings = context.scene.colmap_import
    for name, value in snapshot.review_properties:
        setattr(settings, name, value)
    session = ensure_review_session(context)
    if (
        session.current_index != snapshot.current_index
        or session.current_image_id != snapshot.current_image_id
    ):
        raise RuntimeError("Filter 전 Camera Review pair를 복구하지 못했습니다.")
    if session.current_index >= 0:
        from .camera_review_images import update_working_set

        update_working_set(session, session.current_index)
    session.image_warnings = list(snapshot.image_warnings)
    session.viewport_snapshots = dict(snapshot.viewport_snapshots)
    session.render_snapshot = snapshot.render_snapshot
    session.render_last_applied = snapshot.render_last_applied
    from .camera_review_timeline import restore_timeline_state

    restore_timeline_state(session, snapshot.timeline_state)
    if session.viewport_snapshots and session.current_image_id is not None:
        from .camera_review_viewport import update_review_view_for_current

        update_review_view_for_current(context, session)
    for name, value in snapshot.review_properties:
        setattr(settings, name, value)
    return session


def refresh_review_after_edit(
    context,
    *,
    state=None,
    rebuild_timeline: bool = False,
    prevalidated_timeline_token: tuple[tuple[object, ...], ...] = (),
) -> tuple[str, ...]:
    """성공한 편집 revision에서 Review를 갱신하고 요청된 Timeline만 재생성합니다."""
    refreshed = ensure_review_session(context, state=state)
    warnings: list[str] = []
    if rebuild_timeline:
        from .camera_review_timeline import build_or_refresh_timeline

        warnings.extend(
            build_or_refresh_timeline(
                context,
                refreshed,
                allow_empty=True,
                prevalidated_refresh_token=prevalidated_timeline_token,
            )
        )
    else:
        from .camera_review_timeline import refresh_timeline_manifest_revision

        warnings.extend(refresh_timeline_manifest_revision(refreshed))
    if refreshed.current_image_id is None:
        from .camera_review_viewport import restore_session_views

        warnings.extend(restore_session_views(refreshed))
    return tuple(warnings)


def _clear_review_session_values(sessions) -> None:
    """선택한 Review Session의 임시 참조만 안전하게 폐기합니다."""

    from .camera_review_images import cleanup_review_images
    from .camera_review_timeline import detach_session
    from .camera_review_viewport import restore_session_views

    for session in sessions:
        if session is None:
            continue
        detach_session(session)
        try:
            restore_session_views(session)
        except (AttributeError, ReferenceError, RuntimeError, ValueError):
            session.viewport_snapshots.clear()
        try:
            cleanup_review_images(session)
        except (AttributeError, ReferenceError, RuntimeError, ValueError):
            session.resident_images.clear()
            session.working_set_image_ids.clear()
        _SESSIONS.pop(session.scene_pointer, None)
        try:
            settings = session.scene.colmap_import
            settings.review_restore_available = False
            settings.review_vram_window = ""
        except (AttributeError, ReferenceError, RuntimeError):
            pass


def clear_review_sessions_for_dataset(dataset_uuid: str) -> None:
    """Undo로 Scene ID가 교체되어도 지정 Dataset의 stale 세션만 정리합니다."""

    _clear_review_session_values(
        tuple(
            session
            for session in _SESSIONS.values()
            if session.dataset_uuid == dataset_uuid
        )
    )


def clear_review_sessions(context=None) -> None:
    """snapshot, background와 VRAM을 정리하고 모든 임시 세션을 폐기합니다."""

    sessions = (
        (_SESSIONS.get(_scene_key(context)),)
        if context is not None
        else tuple(_SESSIONS.values())
    )
    _clear_review_session_values(sessions)


def abandon_review_sessions_for_global_undo() -> None:
    """Review가 소유한 이전 Blender ID를 읽지 않고 Registry만 폐기합니다."""

    from .camera_review_timeline import (
        abandon_timeline_sessions_for_global_undo,
    )

    abandon_timeline_sessions_for_global_undo()
    _SESSIONS.clear()


__all__ = [
    "ImportReviewTransition",
    "ReviewSession",
    "abandon_review_sessions_for_global_undo",
    "begin_import_review_transition",
    "capture_review_edit_snapshot",
    "clear_review_sessions",
    "clear_review_sessions_for_dataset",
    "complete_import_review_transition",
    "current_review_session",
    "ensure_review_session",
    "initialize_review_session",
    "navigate_review",
    "refresh_review_after_edit",
    "restore_review_edit_snapshot",
    "review_change_allowed",
    "rollback_import_review_transition",
    "set_pending_review_overlay",
    "sync_review_properties",
    "update_review_background_from_settings",
]
