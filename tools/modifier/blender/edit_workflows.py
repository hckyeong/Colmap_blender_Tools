"""Point·Camera 편집 유스케이스와 Runtime·Scene 커밋을 조정합니다."""

from __future__ import annotations

from dataclasses import dataclass

from ..core.edit_replay import history_hash, replay_history
from ..core.edits import (
    ImageObservationRemovalPlan,
    PointRemovalPlan,
    apply_image_observation_removal,
    apply_point_removal,
    image_operation_from_report,
    plan_image_observation_removal,
    plan_point_removal,
    point_operation_from_report,
    serialize_history,
)
from ..core.indexes import build_relationship_indexes
from .camera_edit_session import (
    current_camera_edit_session,
    force_end_camera_mode,
    start_camera_edit_mode,
)
from .edit_coordinator import synchronize_scene
from .point_edit_session import (
    current_edit_session,
    finish_point_edit_mode,
    force_end_point_mode,
)
from .runtime_state import (
    clear_runtime,
    ensure_runtime,
    install_state,
    state_with_current,
    write_history_settings,
    write_source_settings,
)
from .selection import (
    clear_snapshots,
    collect_selected_image_ids,
    collect_selected_point_ids,
    lock_managed_selection_objects,
    observing_images_for_points,
    validate_managed_camera_scene,
    valid_snapshot,
)


@dataclass(frozen=True)
class PendingRemove:
    target_kind: str
    dataset_uuid: str
    source_fingerprint: str
    edit_revision: int
    index_revision: int
    history_hash: str
    selected_point3d_ids: tuple[int, ...]
    selected_image_ids: tuple[int, ...]
    plan: PointRemovalPlan | ImageObservationRemovalPlan


def update_counts(
    settings,
    selected_count: int,
    observing_count: int,
    camera_target_count: int = 0,
) -> None:
    settings.selected_point_count = selected_count
    settings.observing_camera_count = observing_count
    settings.derived_orphan_count = 0
    settings.camera_target_count = camera_target_count


def _rollback_atomic_review_edit(
    context,
    previous_state,
    old_values: dict[str, object],
    review_snapshot,
) -> tuple[str, ...]:
    """Filter 후처리 실패에서 이전 권위 Runtime/Scene/Review를 복구합니다."""
    settings = context.scene.colmap_import
    for name, value in old_values.items():
        setattr(settings, name, value)
    warnings = synchronize_scene(context, previous_state, settings)
    install_state(previous_state)
    clear_snapshots()
    lock_managed_selection_objects(context, previous_state.dataset_uuid)
    from .camera_review_session import restore_review_edit_snapshot

    restore_review_edit_snapshot(context, review_snapshot)
    return warnings


def commit_edit(
    context,
    new_state,
    status: str,
    *,
    atomic_review: bool = False,
    previous_state=None,
) -> tuple[str, ...]:
    """설정과 Scene을 커밋하고 성공한 경우에만 Runtime을 교체합니다."""
    settings = context.scene.colmap_import
    serialize_history(new_state.operations)
    if previous_state is None:
        previous_state = ensure_runtime(context.scene)
    camera_list_changed = (
        frozenset(previous_state.current_reconstruction.images)
        != frozenset(new_state.current_reconstruction.images)
    )
    review_snapshot = None
    if atomic_review:
        if previous_state is None:
            raise ValueError("원자적 Review 편집에는 이전 Runtime 상태가 필요합니다.")
        from .camera_review_session import capture_review_edit_snapshot

        review_snapshot = capture_review_edit_snapshot(context)
    prevalidated_timeline_token: tuple[tuple[object, ...], ...] = ()
    try:
        from .camera_review_session import current_review_session
        from .camera_review_timeline import capture_owned_marker_refresh_token

        review_session = current_review_session(context)
        if review_session is not None:
            prevalidated_timeline_token = capture_owned_marker_refresh_token(
                review_session
            )
    except (AttributeError, ReferenceError, RuntimeError, TypeError, ValueError):
        prevalidated_timeline_token = ()
    old_values = {
        "edit_history_json": settings.edit_history_json,
        "edit_revision": settings.edit_revision,
        "history_hash": settings.history_hash,
        "last_operation_id": settings.last_operation_id,
        "image_count": settings.image_count,
        "camera_count": settings.camera_count,
        "point3d_count": settings.point3d_count,
        "point_selection_status": settings.point_selection_status,
        "source_fingerprint": settings.source_fingerprint,
        "selected_files_json": settings.selected_files_json,
        "active_dataset_root": settings.active_dataset_root,
        "active_sparse_root": settings.active_sparse_root,
        "active_model_directory": settings.active_model_directory,
        "active_images_directory": settings.active_images_directory,
        "active_input_format": settings.active_input_format,
        "active_layout": settings.active_layout,
        "dataset_uuid": settings.dataset_uuid,
        "schema_version": settings.schema_version,
        "selected_point_count": settings.selected_point_count,
        "observing_camera_count": settings.observing_camera_count,
        "derived_orphan_count": settings.derived_orphan_count,
        "camera_target_count": settings.camera_target_count,
        "point_edit_mode_active": settings.point_edit_mode_active,
        "camera_edit_mode_active": settings.camera_edit_mode_active,
        "remove_target": settings.remove_target,
    }
    try:
        write_history_settings(settings, new_state)
        write_source_settings(settings, new_state)
        settings.image_count = str(len(new_state.current_reconstruction.images))
        settings.camera_count = str(len(new_state.current_reconstruction.cameras))
        settings.point3d_count = str(len(new_state.current_reconstruction.points3d))
        settings.point_selection_status = status
        warnings = synchronize_scene(context, new_state, settings)
    except Exception:
        for name, value in old_values.items():
            setattr(settings, name, value)
        raise
    install_state(new_state)
    clear_snapshots()
    lock_managed_selection_objects(context, new_state.dataset_uuid)
    update_counts(settings, 0, 0, 0)
    settings.point_edit_mode_active = False
    settings.camera_edit_mode_active = False
    settings.remove_target = "NONE"
    post_warnings: list[str] = []
    try:
        from .camera_review_session import refresh_review_after_edit

        post_warnings.extend(
            refresh_review_after_edit(
                context,
                state=new_state,
                rebuild_timeline=camera_list_changed,
                prevalidated_timeline_token=prevalidated_timeline_token,
            )
        )
    except Exception as exc:
        if atomic_review:
            try:
                rollback_warnings = _rollback_atomic_review_edit(
                    context,
                    previous_state,
                    old_values,
                    review_snapshot,
                )
            except Exception as rollback_exc:
                raise RuntimeError(
                    "Camera Filter 후처리 실패 후 복구에도 실패했습니다. "
                    f"원래 오류: {exc}; 복구 오류: {rollback_exc}"
                ) from exc
            message = (
                "Camera Filter 후처리에 실패하여 Runtime, Scene, Review와 "
                f"Timeline을 이전 revision으로 복구했습니다: {exc}"
            )
            if rollback_warnings:
                message += f" 복구 경고: {'; '.join(rollback_warnings)}"
            raise RuntimeError(message) from exc
        from .camera_review_session import clear_review_sessions

        clear_review_sessions(context)
        message = (
            "COLMAP 편집은 완료되었지만 Camera Review 후처리에 실패했습니다. "
            f"다시 Import하여 Timeline을 재구성하십시오: {exc}"
        )
        settings.review_status = message
        post_warnings.append(message)
    return (*warnings, *post_warnings)


def resolve_remove_target(context, state) -> tuple[str, tuple[int, ...]]:
    settings = context.scene.colmap_import
    session = current_edit_session(
        context,
        state,
        strict=settings.point_edit_mode_active,
    )
    if session is not None:
        return "POINTS", collect_selected_point_ids(context, state)
    camera_session = current_camera_edit_session(
        context,
        state,
        strict=settings.camera_edit_mode_active,
    )
    if camera_session is not None:
        return "IMAGES", collect_selected_image_ids(context, state)
    raise ValueError(
        "Edit Point Mode 또는 Edit Camera Mode에서 제거 대상을 선택하십시오."
    )


def select_observing_cameras(context, state=None):
    """Point 선택을 역추적하고 Camera 선택 세션으로 전환합니다."""
    from .camera_filter_queue import require_no_pending_filters

    require_no_pending_filters(context, "관측 Camera 선택")
    settings = context.scene.colmap_import
    if state is None:
        state = ensure_runtime(context.scene)
    session = current_edit_session(
        context,
        state,
        strict=settings.point_edit_mode_active,
    )
    if session is not None:
        selected = collect_selected_point_ids(context, state)
    else:
        snapshot = valid_snapshot(context, state)
        if snapshot is None:
            raise ValueError(
                "Edit Point Mode에서 Point3D를 선택하거나 유효한 선택 "
                "Snapshot을 먼저 만드십시오."
            )
        selected = snapshot.selected_point3d_ids
    observing = observing_images_for_points(state, selected)
    cameras = validate_managed_camera_scene(context, state)
    missing = [image_id for image_id in observing if image_id not in cameras]
    if missing:
        raise ValueError(f"관측 Camera Object가 누락되었습니다: {missing[:10]}")

    if session is not None:
        finish_point_edit_mode(
            context,
            state,
            selected_point_ids=selected,
            observing_image_ids=observing,
        )
    start_camera_edit_mode(
        context,
        state,
        selected_image_ids=observing,
    )

    update_counts(settings, len(selected), len(observing), len(observing))
    settings.remove_target = "IMAGES"
    settings.point_selection_status = (
        f"선택 Point3D {len(selected)}개를 관측한 Camera "
        f"{len(observing)}개를 선택했습니다."
    )
    return state, selected, observing


def prepare_remove(context):
    """현재 선택으로 불변 제거 계획을 생성합니다."""
    from .camera_filter_queue import require_no_pending_filters

    require_no_pending_filters(context, "Remove")
    state = ensure_runtime(context.scene)
    if state.edit_revision != state.index_revision:
        raise ValueError("Runtime 모델 revision과 관계 인덱스 revision이 일치하지 않습니다.")
    target_kind, selected_ids = resolve_remove_target(context, state)
    validate_managed_camera_scene(context, state)
    if target_kind == "POINTS":
        plan = plan_point_removal(
            state.current_reconstruction,
            state.indexes,
            selected_ids,
            source_revision=state.edit_revision,
        )
        selected_points = selected_ids
        selected_images: tuple[int, ...] = ()
    else:
        state, pending = prepare_image_remove(
            context,
            selected_ids,
            state=state,
        )
        settings = context.scene.colmap_import
        update_counts(settings, 0, 0, len(selected_ids))
        settings.remove_target = "IMAGES"
        return state, pending
    pending = PendingRemove(
        target_kind=target_kind,
        dataset_uuid=state.dataset_uuid,
        source_fingerprint=state.source.source_fingerprint,
        edit_revision=state.edit_revision,
        index_revision=state.index_revision,
        history_hash=state.history_hash,
        selected_point3d_ids=selected_points,
        selected_image_ids=selected_images,
        plan=plan,
    )
    settings = context.scene.colmap_import
    if target_kind == "POINTS":
        update_counts(
            settings,
            len(selected_points),
            len(plan.affected_image_ids),
            0,
        )
        settings.remove_target = "POINTS"
    else:
        update_counts(
            settings,
            0,
            0,
            len(selected_images),
        )
        settings.remove_target = "IMAGES"
    return state, pending


def prepare_image_remove(context, image_ids, *, state=None):
    """명시적인 IMAGE_ID tuple로 공통 Camera 제거 계획을 생성합니다."""
    if state is None:
        state = ensure_runtime(context.scene)
    if state.edit_revision != state.index_revision:
        raise ValueError("Runtime 모델 revision과 관계 인덱스 revision이 일치하지 않습니다.")
    supplied_image_ids = tuple(image_ids)
    selected_images = tuple(sorted(set(supplied_image_ids)))
    if not selected_images:
        raise ValueError("제거할 IMAGE_ID가 없습니다.")
    if selected_images != supplied_image_ids:
        raise ValueError("IMAGE_ID는 중복 없는 오름차순 tuple이어야 합니다.")
    validate_managed_camera_scene(context, state)
    plan = plan_image_observation_removal(
        state.current_reconstruction,
        state.indexes,
        selected_images,
        source_revision=state.edit_revision,
    )
    return state, PendingRemove(
        target_kind="IMAGES",
        dataset_uuid=state.dataset_uuid,
        source_fingerprint=state.source.source_fingerprint,
        edit_revision=state.edit_revision,
        index_revision=state.index_revision,
        history_hash=state.history_hash,
        selected_point3d_ids=(),
        selected_image_ids=selected_images,
        plan=plan,
    )


def validate_prepared_remove(context, pending: PendingRemove | None):
    """확인창 이후 대상·선택·revision이 동일한지 다시 검증합니다."""
    if pending is None:
        return prepare_remove(context)
    state = ensure_runtime(context.scene)
    target_kind, selected_ids = resolve_remove_target(context, state)
    selected_points = selected_ids if target_kind == "POINTS" else ()
    selected_images = selected_ids if target_kind == "IMAGES" else ()
    current_token = (
        target_kind,
        state.dataset_uuid,
        state.source.source_fingerprint,
        state.edit_revision,
        state.index_revision,
        state.history_hash,
        selected_points,
        selected_images,
    )
    pending_token = (
        pending.target_kind,
        pending.dataset_uuid,
        pending.source_fingerprint,
        pending.edit_revision,
        pending.index_revision,
        pending.history_hash,
        pending.selected_point3d_ids,
        pending.selected_image_ids,
    )
    if current_token != pending_token:
        raise ValueError(
            "삭제 확인 후 대상 종류, 선택 또는 편집 이력이 변경되었습니다. "
            "현재 대상으로 삭제를 다시 실행하십시오."
        )
    validate_managed_camera_scene(context, state)
    return state, pending


def validate_prepared_image_remove(
    context,
    pending: PendingRemove,
    image_ids,
    *,
    state=None,
):
    """확인창 뒤 명시 IMAGE_ID 계획의 Runtime token과 대상을 재검증합니다."""
    if pending.target_kind != "IMAGES":
        raise ValueError("명시 IMAGE_ID 제거 계획의 대상 종류가 잘못되었습니다.")
    selected_images = tuple(image_ids)
    if state is None:
        state = ensure_runtime(context.scene)
    current_token = (
        state.dataset_uuid,
        state.source.source_fingerprint,
        state.edit_revision,
        state.index_revision,
        state.history_hash,
        selected_images,
    )
    pending_token = (
        pending.dataset_uuid,
        pending.source_fingerprint,
        pending.edit_revision,
        pending.index_revision,
        pending.history_hash,
        pending.selected_image_ids,
    )
    if current_token != pending_token:
        raise ValueError(
            "삭제 확인 후 Dataset, source, revision 또는 IMAGE_ID가 변경되었습니다."
        )
    validate_managed_camera_scene(context, state)
    return state, pending


def apply_pending_remove(
    context,
    state,
    pending: PendingRemove,
    *,
    atomic_review: bool = False,
) -> tuple[str, ...]:
    """검증된 제거 계획을 Core에 적용하고 Scene까지 커밋합니다."""
    if pending.target_kind == "POINTS":
        result, result_indexes, report = apply_point_removal(
            state.current_reconstruction,
            pending.plan,
            current_revision=state.edit_revision,
        )
        operation = point_operation_from_report(
            report,
            state.dataset_uuid,
            state.source.source_fingerprint,
            len(state.operations),
        )
        status = (
            f"완료: Point3D {len(report.requested_point3d_ids)}개를 제거하고 "
            f"관측 참조 {report.cleared_observation_count}개를 정리했습니다."
        )
    else:
        result, result_indexes, report = apply_image_observation_removal(
            state.current_reconstruction,
            pending.plan,
            current_revision=state.edit_revision,
        )
        operation = image_operation_from_report(
            report,
            state.dataset_uuid,
            state.source.source_fingerprint,
            len(state.operations),
        )
        status = (
            f"완료: Camera {len(report.requested_image_ids)}개와 Point3D "
            f"{len(report.observed_point3d_ids)}개를 제거했습니다."
        )
    operations = (*state.operations, operation)
    new_state = state_with_current(
        state,
        result,
        result_indexes,
        operations,
    )
    force_end_point_mode(context, state.dataset_uuid)
    force_end_camera_mode(context, state.dataset_uuid)
    return commit_edit(
        context,
        new_state,
        status,
        atomic_review=atomic_review,
        previous_state=state,
    )


def undo_last_edit(context):
    """가장 최근 편집을 제외한 이력을 재생하고 Scene에 반영합니다."""
    from .camera_filter_queue import require_no_pending_filters

    require_no_pending_filters(context, "Undo Last Edit")
    settings = context.scene.colmap_import
    force_end_point_mode(context, settings.dataset_uuid)
    force_end_camera_mode(context, settings.dataset_uuid)
    state = ensure_runtime(context.scene)
    operations = state.operations[:-1]
    current, indexes = replay_history(
        state.baseline_reconstruction,
        operations,
        state.dataset_uuid,
        state.source.source_fingerprint,
    )
    new_state = state_with_current(state, current, indexes, operations)
    commit_edit(
        context,
        new_state,
        "마지막 COLMAP 편집을 되돌렸습니다.",
        previous_state=state,
    )
    return state


def reset_edits(context) -> tuple[object | None, bool]:
    """모든 편집 이력을 초기화하고 source 불일치 여부를 함께 반환합니다."""
    from .camera_filter_queue import require_no_pending_filters

    require_no_pending_filters(context, "Reset Edits")
    settings = context.scene.colmap_import
    force_end_point_mode(context, settings.dataset_uuid)
    force_end_camera_mode(context, settings.dataset_uuid)
    try:
        state = ensure_runtime(context.scene)
        baseline_indexes = build_relationship_indexes(state.baseline_reconstruction)
        new_state = state_with_current(
            state,
            state.baseline_reconstruction,
            baseline_indexes,
            (),
        )
    except ValueError as exc:
        if "source" not in str(exc) and "fingerprint" not in str(exc):
            raise
        settings.edit_history_json = "[]"
        settings.edit_revision = 0
        settings.history_hash = history_hash(())
        settings.last_operation_id = ""
        clear_snapshots()
        clear_runtime()
        update_counts(settings, 0, 0, 0)
        settings.remove_target = "NONE"
        settings.point_selection_status = (
            "원본 source 변경을 감지해 편집 이력을 초기화했습니다. "
            "Import를 실행해 현재 source로 장면을 갱신하십시오."
        )
        return None, True
    commit_edit(
        context,
        new_state,
        "모든 COLMAP 편집을 초기화했습니다.",
        previous_state=state,
    )
    return state, False
