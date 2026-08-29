"""Dataset 자동 탐색과 증분 COLMAP Import Operator를 제공합니다."""

from __future__ import annotations

import json
import os
from pathlib import Path
from time import perf_counter

import bpy
from bpy.props import StringProperty

from ..core.discovery import discover_dataset
from ..core.fingerprint import SourceFingerprintJob, source_fingerprint
from ..core.import_types import ImportPlan
from ..core.layout import resolve_layout
from .camera_edit_session import force_end_camera_mode
from .camera_review_session import (
    begin_import_review_transition,
    complete_import_review_transition,
    initialize_review_session,
    rollback_import_review_transition,
)
from .import_progress_session import (
    active_import_progress_session,
    attach_import_progress_ui,
    create_import_progress_session,
    get_import_progress_session,
    release_import_progress_session,
)
from .import_scene_job import ImportSceneBuildJob
from .modifier_viewport import frame_imported_points
from .modifier_data_visibility import synchronize_modifier_data_visibility
from .operation_guard import (
    export_mutation_allowed,
    require_export_mutation_allowed,
)
from .point_edit_session import force_end_point_mode
from .progress_overlay import (
    ProgressOverlayView,
    add_progress_overlay_draw_handler,
    compact_path,
    draw_progress_overlay,
    remove_progress_overlay_draw_handler,
    request_progress_overlay_redraw,
)
from .runtime_state import (
    install_state,
    loaded_for_current,
    prepare_import_state,
    write_history_settings,
)
from .scene_bridge import commit_staging
from .selection import clear_snapshots


_TIMER_INTERVAL_SECONDS = 0.01
_WORK_BUDGET_MS = 25.0

_CORE_PHASE_LABELS = {
    "CREATED": "Preparing import",
    "DISCOVER": "Discovering dataset",
    "HASH_SOURCE_BEFORE": "Hashing source files",
    "READ_MODEL": "Reading COLMAP files",
    "VALIDATE_MODEL": "Validating reconstruction",
    "VALIDATE_IMAGES": "Validating image files",
    "HASH_SOURCE_AFTER": "Checking source consistency",
    "BUILD_RESULT": "Building validated result",
    "DONE": "COLMAP files loaded",
    "FAILED": "Import failed",
}

_SCENE_PHASE_LABELS = {
    "CREATED": "Preparing Blender staging",
    "CREATE_COLLECTION_TREE": "Creating collections",
    "PREPARE_POINT_BUFFERS": "Preparing point buffers",
    "CREATE_POINT_MESH": "Creating point mesh",
    "CREATE_POINT_ATTRIBUTES": "Writing point attributes",
    "CREATE_POINT_DISPLAY": "Creating point display",
    "CREATE_CAMERAS": "Creating cameras",
    "READY_TO_COMMIT": "Blender staging complete",
    "FAILED": "Import failed",
}


def _matching_dataset_uuid(settings, model_directory: str) -> str | None:
    """같은 모델 경로를 다시 읽을 때 기존 Dataset UUID를 재사용합니다."""

    if not settings.dataset_uuid or not settings.active_model_directory:
        return None
    try:
        active = Path(settings.active_model_directory).expanduser().resolve()
        requested = Path(model_directory).expanduser().resolve()
    except (OSError, RuntimeError, ValueError):
        return None
    return settings.dataset_uuid if active == requested else None


def _enable_point_colors_in_viewport(context) -> bool:
    """열린 Viewport의 Point 색상을 켜고 Relation Line을 숨깁니다."""

    updated = False
    window_manager = getattr(context, "window_manager", None)
    if window_manager is not None:
        for window in window_manager.windows:
            for area in window.screen.areas:
                if area.type != "VIEW_3D":
                    continue
                space = area.spaces.active
                if not hasattr(space, "shading"):
                    continue
                space.shading.color_type = "VERTEX"
                if hasattr(space, "overlay"):
                    space.overlay.show_relationship_lines = False
                area.tag_redraw()
                updated = True

    if updated:
        return True

    area = getattr(context, "area", None)
    space = getattr(context, "space_data", None)
    if area is not None and area.type == "VIEW_3D" and hasattr(space, "shading"):
        space.shading.color_type = "VERTEX"
        if hasattr(space, "overlay"):
            space.overlay.show_relationship_lines = False
        area.tag_redraw()
        return True
    return False


def _apply_dataset_discovery(settings, dataset_root: Path):
    """Dataset Root 탐색 결과를 Import 대기 경로에 반영하는 호환 경계입니다."""

    result = discover_dataset(dataset_root, settings.input_format)
    settings.dataset_root = str(dataset_root)
    settings.sparse_directory = (
        str(result.sparse_directory) if result.sparse_directory is not None else ""
    )
    settings.images_directory = (
        str(result.images_directory) if result.images_directory is not None else ""
    )
    settings.status = result.status
    settings.status_kind = "DISCOVERED"
    return result


def _resolved_optional_path(value: str) -> Path | None:
    if not value:
        return None
    return Path(bpy.path.abspath(value)).expanduser().resolve()


def _snapshot_import_plan(settings) -> ImportPlan:
    """현재 RNA 입력을 절대경로 기반의 불변 ImportPlan으로 고정합니다."""

    dataset_root = _resolved_optional_path(settings.dataset_root)
    if dataset_root is not None:
        # Dataset Root가 권위 입력일 때는 Job이 sparse/images를 다시 탐색합니다.
        sparse_directory = None
        images_directory = None
    else:
        sparse_directory = _resolved_optional_path(settings.sparse_directory)
        images_directory = _resolved_optional_path(settings.images_directory)
        if sparse_directory is None or images_directory is None:
            raise ValueError(
                "Dataset Root 또는 Sparse/Images Directory가 필요합니다."
            )
        dataset_root = Path(
            os.path.commonpath((str(sparse_directory), str(images_directory)))
        ).resolve()

    matching_model = _resolved_optional_path(settings.active_model_directory)
    matching_uuid = settings.dataset_uuid or None
    return ImportPlan(
        dataset_root=dataset_root,
        sparse_directory=sparse_directory,
        images_directory=images_directory,
        input_format=settings.input_format,
        point_size=settings.point_size,
        camera_frustum_size=settings.camera_frustum_size,
        axis_forward=settings.axis_forward,
        axis_up=settings.axis_up,
        matching_dataset_uuid=matching_uuid,
        matching_model_directory=matching_model,
    )


def _prepare_import_session(context):
    """공통 사전검사를 거쳐 Import Session을 생성합니다."""

    settings = context.scene.colmap_import
    from .camera_filter_queue import require_no_pending_filters

    require_export_mutation_allowed(context, "Import")
    require_no_pending_filters(context, "Import")
    plan = _snapshot_import_plan(settings)

    force_end_point_mode(context)
    force_end_camera_mode(context)
    clear_snapshots()
    settings.status_kind = "IMPORTING"
    settings.status = "Importing: preparing the progress job."
    try:
        return create_import_progress_session(context.scene, plan)
    except Exception:
        settings.status_kind = "ERROR"
        raise


def _phase_name(value) -> str:
    phase = getattr(value, "phase", value)
    return str(getattr(phase, "name", phase or "CREATED"))


def _progress_values(session) -> tuple[str, float, int, int]:
    """현재 Import 단계의 영어 문구와 전체 진행률을 계산합니다."""

    if session.stage == "CORE":
        snapshot = session.job.progress
        phase = _CORE_PHASE_LABELS.get(_phase_name(snapshot), _phase_name(snapshot))
        fraction = min(float(snapshot.overall_fraction) * 0.68, 0.68)
        return (
            phase,
            fraction,
            int(snapshot.completed_units),
            int(snapshot.total_units),
        )
    if session.stage == "PREPARE_RUNTIME":
        return ("Building relationship indexes", 0.69, 0, 1)
    if session.stage == "BUILD_SCENE" and session.scene_job is not None:
        snapshot = session.scene_job.progress
        phase = _SCENE_PHASE_LABELS.get(_phase_name(snapshot), _phase_name(snapshot))
        fraction = 0.70 + min(float(snapshot.overall_fraction), 1.0) * 0.23
        return (
            phase,
            min(fraction, 0.93),
            int(snapshot.completed_units),
            int(snapshot.total_units),
        )
    if session.stage == "VERIFY_SOURCE":
        job = session.verification_job
        completed = int(getattr(job, "completed_bytes", 0))
        total = int(getattr(job, "total_bytes", 0))
        phase_fraction = completed / total if total else 0.0
        return (
            "Verifying source files",
            0.93 + min(phase_fraction, 1.0) * 0.03,
            completed,
            total,
        )
    if session.stage == "VERIFY_IMAGES":
        total = len(session.image_entries)
        completed = min(session.image_cursor, total)
        phase_fraction = completed / total if total else 1.0
        return (
            "Verifying image paths",
            0.96 + phase_fraction * 0.02,
            completed,
            total,
        )
    if session.stage == "COMMIT":
        return ("Updating Blender scene", 0.99, 0, 1)
    if session.stage == "DONE":
        return ("Import complete", 1.0, 1, 1)
    return ("Preparing import", 0.0, 0, 0)


def _import_progress_overlay_view(session) -> ProgressOverlayView:
    """현재 Import Session을 공통 Overlay View로 변환합니다."""

    phase, fraction, completed, total = _progress_values(session)
    if total <= 0:
        units_text = "Calculating work units"
        completed = 0
        total = 0
    else:
        completed = min(completed, total)
        units_text = f"{completed:,} / {total:,}"

    resolved_layout = session.job.resolved_layout
    format_text = (
        f"{resolved_layout.input_format.title()} / {resolved_layout.layout.title()}"
        if resolved_layout is not None
        else session.plan.input_format.title()
    )
    return ProgressOverlayView(
        title="COLMAP Import",
        phase=phase,
        fraction=max(0.0, min(fraction, 1.0)),
        completed_units=completed,
        total_units=total,
        elapsed_seconds=session.elapsed_seconds,
        detail_lines=(
            f"Processed: {units_text}    Elapsed: {session.elapsed_seconds:.1f}s",
            f"Dataset: {compact_path(session.plan.dataset_root)}",
            f"Format: {format_text} · Blender input is locked during import.",
        ),
    )


def _draw_import_overlay() -> None:
    """활성 Import Session의 중앙 진행창을 그립니다."""

    session = active_import_progress_session()
    if session is not None:
        draw_progress_overlay(_import_progress_overlay_view(session))


def _begin_source_verification(session) -> None:
    loaded = session.job.result
    layout = resolve_layout(
        loaded.source.model_directory,
        loaded.source.input_format.upper(),
    )
    verification_job = source_fingerprint(layout, incremental=True)
    if not isinstance(verification_job, SourceFingerprintJob):
        raise RuntimeError("증분 source fingerprint Job을 만들 수 없습니다.")
    session.verification_job = verification_job
    session.stage = "VERIFY_SOURCE"


def _advance_source_verification(session, work_budget_ms: float | None) -> None:
    job = session.verification_job
    if not isinstance(job, SourceFingerprintJob):
        raise RuntimeError("Source 재확인 Job이 없습니다.")
    job.advance(work_budget_ms)
    if not job.done:
        return
    loaded = session.job.result
    if job.result != loaded.source.source_fingerprint:
        raise ValueError("장면 구성 중 COLMAP source가 변경되었습니다. 다시 Import하십시오.")
    job.close()
    session.verification_job = None
    manifest = loaded.image_path_manifest
    if manifest is None:
        raise ValueError("Loader의 검증된 이미지 경로 manifest가 없습니다.")
    session.image_entries = tuple(manifest.entries.values())
    session.image_cursor = 0
    session.missing_images = []
    session.changed_images = []
    session.stage = "VERIFY_IMAGES"


def _advance_image_verification(
    session,
    work_budget_ms: float | None,
) -> None:
    deadline = (
        None
        if work_budget_ms is None
        else perf_counter() + work_budget_ms / 1000.0
    )
    while session.image_cursor < len(session.image_entries):
        entry = session.image_entries[session.image_cursor]
        stored_path = entry.resolved_image_path
        try:
            current_path = stored_path.resolve(strict=True)
        except (OSError, RuntimeError):
            session.missing_images.append(entry.image_name)
        else:
            if current_path != stored_path or not current_path.is_file():
                session.changed_images.append(entry.image_name)
        session.image_cursor += 1
        if (
            session.image_cursor % 64 == 0
            and deadline is not None
            and perf_counter() >= deadline
        ):
            return

    if session.missing_images:
        shown = ", ".join(session.missing_images[:5])
        count = len(session.missing_images)
        suffix = f" 외 {count - 5}개" if count > 5 else ""
        raise ValueError(
            f"Images Directory에서 이미지 파일을 찾지 못했습니다: {shown}{suffix}"
        )
    if session.changed_images:
        shown = ", ".join(session.changed_images[:5])
        count = len(session.changed_images)
        suffix = f" 외 {count - 5}개" if count > 5 else ""
        raise ValueError(
            "Loader 검증 뒤 이미지 canonical 경로가 변경되었습니다: "
            f"{shown}{suffix}"
        )
    session.stage = "COMMIT"


def _append_warning(settings, warning: str) -> None:
    settings.warning_summary = "\n".join(
        item for item in (settings.warning_summary, warning) if item
    )


def _initialize_review_after_import(context, runtime_state) -> None:
    """Import 성공 뒤 Camera Review와 Timeline을 기존 정책으로 초기화합니다."""

    settings = context.scene.colmap_import
    try:
        review_session = initialize_review_session(context, runtime_state)
    except Exception as exc:
        review_warning = f"Camera Review 초기화 실패: {exc}"
        _append_warning(settings, review_warning)
        settings.review_current_index = -1
        settings.review_current_image_id = ""
        settings.review_total_count = 0
        settings.review_camera_model = ""
        settings.review_image_status = ""
        settings.review_exact_projection = False
        settings.review_vram_window = ""
        settings.review_restore_available = False
        settings.review_status = review_warning
        return

    try:
        from .camera_review_timeline import build_or_refresh_timeline

        timeline_warnings = build_or_refresh_timeline(
            context,
            review_session,
            allow_empty=True,
        )
        if timeline_warnings:
            settings.warning_summary = "\n".join(
                item
                for item in (settings.warning_summary, *timeline_warnings)
                if item
            )
            settings.review_status = (
                f"Timeline 자동 생성 경고: {timeline_warnings[0]}"
            )
    except Exception as exc:
        timeline_warning = f"Timeline 자동 생성 실패: {exc}"
        _append_warning(settings, timeline_warning)
        settings.review_status = timeline_warning


def _commit_import(context, session) -> None:
    """검증된 staging을 원자 commit하고 Runtime·Review 상태를 완료합니다."""

    settings = context.scene.colmap_import
    loaded = session.job.result
    runtime_state = session.runtime_state
    if runtime_state is None or session.scene_job is None:
        raise RuntimeError("commit할 Import Runtime 또는 Scene Job이 없습니다.")
    staged = session.scene_job.result
    source = loaded.source

    # commit 뒤에는 이전 Scene을 복원할 수 없으므로 직렬화·count 계산을 먼저 끝냅니다.
    selected_files_json = json.dumps(
        [str(path) for path in source.selected_files],
        ensure_ascii=False,
    )
    camera_count = str(len(runtime_state.current_reconstruction.cameras))
    image_count = str(len(runtime_state.current_reconstruction.images))
    point3d_count = str(len(runtime_state.current_reconstruction.points3d))

    review_transition = begin_import_review_transition(context)
    try:
        cleanup_warnings = commit_staging(context.scene, staged)
    except Exception:
        rollback_import_review_transition(context, review_transition)
        raise

    # commit 직후 staging은 Scene 소유가 되므로 강제 종료 cleanup 대상에서 제외합니다.
    session.scene_committed = True
    session.scene_job.release_staging_ownership()
    complete_import_review_transition(review_transition)
    context.view_layer.update()

    settings.sparse_directory = str(source.model_directory)
    settings.images_directory = str(source.images_directory)
    settings.active_dataset_root = str(source.dataset_root)
    settings.active_sparse_root = str(source.sparse_root)
    settings.active_model_directory = str(source.model_directory)
    settings.active_images_directory = str(source.images_directory)
    settings.active_input_format = source.input_format
    settings.active_layout = source.layout
    settings.source_fingerprint = source.source_fingerprint
    settings.dataset_uuid = source.dataset_uuid
    settings.schema_version = str(source.schema_version)
    settings.selected_files_json = selected_files_json
    settings.camera_count = camera_count
    settings.image_count = image_count
    settings.point3d_count = point3d_count
    settings.warning_summary = "\n".join((*loaded.warnings, *cleanup_warnings))
    write_history_settings(settings, runtime_state)
    install_state(runtime_state)
    _initialize_review_after_import(context, runtime_state)

    clear_snapshots()
    force_end_point_mode(context, runtime_state.dataset_uuid)
    force_end_camera_mode(context, runtime_state.dataset_uuid)
    settings.selected_point_count = 0
    settings.observing_camera_count = 0
    settings.derived_orphan_count = 0
    settings.camera_target_count = 0
    settings.remove_target = "NONE"
    settings.point_selection_status = (
        "Point와 Camera가 잠겨 있습니다. 전용 Edit Mode로 선택을 시작하십시오."
    )
    settings.status_kind = "SUCCESS"
    settings.status = (
        f"완료: Camera {camera_count}, Image {image_count}, "
        f"Point3D {point3d_count} ({source.input_format}/{source.layout})"
    )
    _enable_point_colors_in_viewport(context)
    frame_warning = frame_imported_points(runtime_state.dataset_uuid)
    if frame_warning:
        _append_warning(settings, frame_warning)
    synchronize_modifier_data_visibility(
        context.scene,
        create_view_layer=False,
    )
    session.completion_warnings = tuple(
        item for item in settings.warning_summary.splitlines() if item
    )
    session.stage = "DONE"


def _advance_import_session(
    context,
    session,
    *,
    work_budget_ms: float | None,
) -> None:
    """Core, staging, 재검증과 commit을 같은 순서로 한 단계 진행합니다."""

    if session.stage == "CORE":
        if not session.job.done:
            session.job.advance(work_budget_ms)
        if session.job.done:
            session.stage = "PREPARE_RUNTIME"
        return

    if session.stage == "PREPARE_RUNTIME":
        loaded = session.job.result
        runtime_state = prepare_import_state(context.scene.colmap_import, loaded)
        session.runtime_state = runtime_state
        session.display_loaded = loaded_for_current(runtime_state)
        session.scene_job = ImportSceneBuildJob(
            session.display_loaded,
            point_size=session.plan.point_size,
            camera_frustum_size=session.plan.camera_frustum_size,
            forward_axis=session.plan.axis_forward,
            up_axis=session.plan.axis_up,
            point_id_order=runtime_state.point_id_order,
        )
        session.stage = "BUILD_SCENE"
        return

    if session.stage == "BUILD_SCENE":
        if session.scene_job is None:
            raise RuntimeError("Blender staging Job이 없습니다.")
        if not session.scene_job.done:
            session.scene_job.advance(work_budget_ms=work_budget_ms)
        if session.scene_job.done:
            _begin_source_verification(session)
        return

    if session.stage == "VERIFY_SOURCE":
        _advance_source_verification(session, work_budget_ms)
        return

    if session.stage == "VERIFY_IMAGES":
        _advance_image_verification(session, work_budget_ms)
        return

    if session.stage == "COMMIT":
        _commit_import(context, session)
        return

    if session.stage != "DONE":
        raise RuntimeError(f"지원하지 않는 Import 진행 단계입니다: {session.stage}")


def _show_import_popup(
    context,
    *,
    title: str,
    icon: str,
    lines: tuple[str, ...],
) -> None:
    if bpy.app.background or context.window is None:
        return

    def draw_message(menu, _context):
        for line in lines:
            menu.layout.label(text=line)

    try:
        context.window_manager.popup_menu(draw_message, title=title, icon=icon)
    except (AttributeError, ReferenceError, RuntimeError, ValueError):
        pass


class COLMAP_OT_import(bpy.types.Operator):
    bl_idname = "colmap.import_model"
    bl_label = "Import"
    bl_description = "선택한 COLMAP 모델을 검증하고 Point3D와 Camera를 장면에 구성합니다"
    bl_options = {"REGISTER", "UNDO", "BLOCKING"}

    job_id: StringProperty(options={"HIDDEN", "SKIP_SAVE"})

    @classmethod
    def poll(cls, context):
        settings = getattr(context.scene, "colmap_import", None)
        return bool(
            settings
            and (
                settings.dataset_root
                or (settings.sparse_directory and settings.images_directory)
            )
            and settings.status_kind != "IMPORTING"
            and export_mutation_allowed(context)
        )

    def _start_error(self, context, exc: Exception):
        settings = context.scene.colmap_import
        settings.status_kind = "ERROR"
        settings.status = f"Import 실패: {exc}"
        self.report({"ERROR"}, str(exc))
        return {"CANCELLED"}

    def invoke(self, context, event):
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
                RuntimeError(f"Import progress UI start failed: {exc}"),
            )

        request_progress_overlay_redraw()
        return {"RUNNING_MODAL"}

    def execute(self, context):
        try:
            session = _prepare_import_session(context)
        except Exception as exc:
            return self._start_error(context, exc)
        self.job_id = session.job_id

        try:
            while session.stage != "DONE":
                _advance_import_session(
                    context,
                    session,
                    work_budget_ms=None,
                )
        except Exception as exc:
            return self._fail(context, session, exc)

        release_import_progress_session(session.job_id)
        self.report({"INFO"}, context.scene.colmap_import.status)
        return {"FINISHED"}

    def _fail(self, context, session, exc: Exception):
        settings = context.scene.colmap_import
        committed = session.scene_committed
        cleanup_message = ""
        try:
            release_import_progress_session(session.job_id)
        except Exception as cleanup_exc:
            cleanup_message = f" | cleanup failed: {cleanup_exc}"
        settings.status_kind = "ERROR"
        if committed:
            settings.status = (
                "Import 장면은 반영되었지만 Blender 상태 완료에 실패했습니다. "
                f"다시 Import하십시오: {exc}{cleanup_message}"
            )
        else:
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

    def _finish(self, context, session):
        if not session.final_screen_shown:
            session.final_screen_shown = True
            request_progress_overlay_redraw()
            return {"RUNNING_MODAL"}

        release_import_progress_session(session.job_id)
        settings = context.scene.colmap_import
        lines = (
            f"Camera: {settings.camera_count}",
            f"Image: {settings.image_count}",
            f"Point3D: {settings.point3d_count}",
            f"Elapsed: {session.elapsed_seconds:.1f}s",
        )
        if session.completion_warnings:
            lines = (*lines, f"Warning: {session.completion_warnings[0]}")
        _show_import_popup(
            context,
            title="COLMAP Import Complete",
            icon="CHECKMARK",
            lines=lines,
        )
        if session.completion_warnings:
            self.report({"WARNING"}, session.completion_warnings[0])
        else:
            self.report({"INFO"}, settings.status)
        return {"FINISHED"}

    def modal(self, context, event):
        session = get_import_progress_session(self.job_id)
        if session is None:
            return {"CANCELLED"}

        # Esc, 우클릭과 외부 클릭은 Import를 취소하거나 진행시키지 않습니다.
        if event.type != "TIMER":
            return {"RUNNING_MODAL"}

        if session.stage == "DONE":
            return self._finish(context, session)
        try:
            _advance_import_session(
                context,
                session,
                work_budget_ms=_WORK_BUDGET_MS,
            )
        except Exception as exc:
            return self._fail(context, session, exc)

        request_progress_overlay_redraw()
        if session.stage == "DONE":
            return self._finish(context, session)
        return {"RUNNING_MODAL"}

    def cancel(self, context):
        """Blender 강제 종료 시 Import 자원을 회수하고 기존 장면을 유지합니다."""

        session = get_import_progress_session(self.job_id)
        if session is None:
            return
        try:
            release_import_progress_session(session.job_id)
        except Exception as exc:
            context.scene.colmap_import.status = (
                f"Import 강제 종료 중 staging 정리에 실패했습니다: {exc}"
            )
            context.scene.colmap_import.status_kind = "ERROR"
            return
        context.scene.colmap_import.status = (
            "Import가 Blender에 의해 중단되었습니다. 기존 성공 장면은 유지되었습니다."
        )
        context.scene.colmap_import.status_kind = "ERROR"


CLASSES = (COLMAP_OT_import,)
