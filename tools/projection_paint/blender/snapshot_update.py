"""사용자 요청 시에만 Modifier 값을 PP 독립 Snapshot으로 복사합니다."""

from __future__ import annotations

from time import monotonic
from typing import Any

import bpy

try:
    from ....shared.colmap_transfer.registry import (
        open_snapshot_provider,
        provider_available,
        validate_open_provider_snapshot,
    )
except ImportError:
    from shared.colmap_transfer.registry import (
        open_snapshot_provider,
        provider_available,
        validate_open_provider_snapshot,
    )

from .camera_import import CameraImportJob
from .progress_overlay import (
    ProgressOverlayView,
    add_progress_overlay_draw_handler,
    draw_progress_overlay,
    remove_progress_overlay_draw_handler,
    request_progress_overlay_redraw,
)
from .snapshot_scene_transaction import build_snapshot_import_request


_TIMER_INTERVAL_SECONDS = 0.01


def _snapshot_progress_values(job: CameraImportJob) -> tuple[str, float, int, int]:
    """Snapshot Job의 단계와 전체 진행률을 단조 증가 값으로 변환합니다."""

    phase, completed, total = job.progress_units()
    state = str(getattr(job.state, "value", job.state))
    if state == "VALIDATING":
        return (phase, 0.0, 0, 0)
    if state == "STAGING":
        fraction = float(completed) / float(total) if total else 0.0
        return (phase, fraction * 0.97, completed, total)
    if state == "READY_TO_COMMIT":
        return ("Preparing scene update", 0.98, total, total)
    if state == "COMMITTING":
        return ("Updating Blender scene", 0.99, total, total)
    if state == "ACTIVE":
        return ("Snapshot update complete", 1.0, total, total)
    return ("Snapshot update stopped", 0.0, 0, total)


def _combined_progress_values(
    provider_handle: Any | None,
    job: CameraImportJob | None,
) -> tuple[str, float, int, int]:
    """Provider 복원·값 생성과 PP Staging 진행률을 하나의 단조 증가 구간으로 합칩니다."""

    if job is None:
        if provider_handle is None:
            return ("Preparing Modifier runtime", 0.0, 0, 0)
        progress = provider_handle.progress
        return (
            str(progress.message or progress.phase),
            min(0.45, max(0.0, float(progress.fraction)) * 0.45),
            int(progress.completed_units),
            int(progress.total_units),
        )
    phase, fraction, completed, total = _snapshot_progress_values(job)
    if fraction >= 1.0:
        return (phase, 1.0, completed, total)
    return (phase, 0.45 + (max(0.0, float(fraction)) * 0.55), completed, total)


def _snapshot_progress_overlay_view(
    provider_handle: Any | None,
    job: CameraImportJob | float | None = None,
    context: Any = None,
    started_at: float = 0.0,
) -> ProgressOverlayView:
    """Runtime 복원부터 Scene Commit까지의 현재 상태를 중앙 진행창 값으로 변환합니다."""

    if isinstance(provider_handle, CameraImportJob) and isinstance(job, (int, float)):
        started_at = float(job)
        job = provider_handle
        provider_handle = None
    if context is None:
        context = bpy.context
    phase, fraction, completed, total = _combined_progress_values(provider_handle, job)
    elapsed_seconds = max(0.0, monotonic() - float(started_at))
    units_text = (
        f"{completed:,} / {total:,}" if total > 0 else "작업량 계산 중"
    )
    if job is not None:
        request = job.request
        count_text = f"Camera {len(request.images):,}개 · Point3D {len(request.points):,}개"
        revision = int(request.source_revision)
    else:
        modifier = getattr(getattr(context, "scene", None), "colmap_import", None)
        count_text = "Modifier Runtime과 전송값 준비 중"
        revision = int(getattr(modifier, "edit_revision", 0) or 0)
    return ProgressOverlayView(
        title="COLMAP Data Update",
        phase=phase,
        fraction=max(0.0, min(float(fraction), 1.0)),
        completed_units=min(completed, total) if total else 0,
        total_units=total,
        elapsed_seconds=elapsed_seconds,
        detail_lines=(
            f"처리량: {units_text} · 경과 {elapsed_seconds:.1f}초",
            count_text,
            (
                f"Modifier Revision {revision} · "
                "완료 전까지 기존 Snapshot 유지"
            ),
        ),
    )


def modifier_snapshot_available(context: Any) -> bool:
    """Modifier 제공자와 Scene의 확정 Dataset 식별자가 있는지 반환합니다."""

    scene = getattr(context, "scene", None)
    settings = getattr(scene, "colmap_import", None)
    return bool(
        provider_available("modifier")
        and settings is not None
        and str(getattr(settings, "dataset_uuid", "") or "")
    )


def snapshot_is_ready(scene: Any) -> bool:
    """Scene Manifest와 PP Snapshot 영구 token이 서로 일치하는지 반환합니다."""

    settings = getattr(scene, "projection_paint", None)
    if settings is None or str(getattr(settings, "snapshot_state", "NONE")) != "READY":
        return False
    try:
        from .camera_scene import RESOURCE_OWNER_PROJECTION_PAINT, scene_manifest

        manifest = scene_manifest(scene)
    except Exception:
        return False
    return bool(
        manifest is not None
        and manifest.resource_owner == RESOURCE_OWNER_PROJECTION_PAINT
        and manifest.dataset_uuid == str(settings.snapshot_uuid or "")
        and manifest.dataset_uuid == str(settings.dataset_uuid or "")
        and manifest.dataset_revision == int(settings.snapshot_revision)
        and manifest.source_fingerprint == str(settings.source_content_fingerprint or "")
    )


def snapshot_update_available(scene: Any) -> bool:
    """현재 Modifier 복합 Identity가 마지막 독립 Snapshot과 다른지 반환합니다."""

    settings = getattr(scene, "projection_paint", None)
    modifier = getattr(scene, "colmap_import", None)
    if settings is None or modifier is None or not snapshot_is_ready(scene):
        return False
    return bool(
        str(getattr(modifier, "domain_uuid", "") or "")
        != str(settings.source_modifier_domain_uuid or "")
        or int(getattr(modifier, "domain_generation", 0))
        != int(settings.source_modifier_domain_generation)
        or str(getattr(modifier, "dataset_uuid", "") or "")
        != str(settings.source_modifier_dataset_uuid or "")
        or int(getattr(modifier, "edit_revision", 0))
        != int(settings.source_modifier_revision)
        or str(getattr(modifier, "source_fingerprint", "") or "")
        != str(settings.source_modifier_fingerprint or "")
        or str(getattr(modifier, "history_hash", "") or "")
        != str(settings.source_modifier_history_hash or "")
    )


class PP_OT_update_colmap_snapshot(bpy.types.Operator):
    """Modifier 확정값을 새 PP 전용 Blender ID Snapshot으로 업데이트합니다."""

    bl_idname = "projection_paint.update_colmap_snapshot"
    bl_label = "COLMAP 데이터 업데이트"
    bl_description = "COLMAP Modifier의 현재 확정 데이터를 독립 Snapshot으로 복사합니다"
    bl_options = {"REGISTER", "UNDO", "BLOCKING"}

    _job: CameraImportJob | None = None
    _provider_handle: Any | None = None
    _timer: Any | None = None
    _overlay_handle: Any | None = None
    _result: Any | None = None
    _started_at = 0.0
    _final_screen_shown = False
    _previous_state = "NONE"

    @classmethod
    def poll(cls, context: Any) -> bool:
        """Modifier Dataset이 있고 Paint·Snapshot 작업이 멈춘 경우에만 허용합니다."""

        from .paint_session import paint_runtime_is_active

        settings = getattr(getattr(context, "scene", None), "projection_paint", None)
        return bool(
            settings is not None
            and modifier_snapshot_available(context)
            and not paint_runtime_is_active(context.scene)
            and str(getattr(settings, "snapshot_state", "NONE")) != "UPDATING"
        )

    def _prepare(self, context: Any) -> None:
        """기존 Snapshot을 보존한 채 진행 UI가 시작할 Scene 상태만 먼저 고정합니다."""

        settings = context.scene.projection_paint
        self._previous_state = str(settings.snapshot_state)
        self._result = None
        self._provider_handle = None
        self._job = None
        self._final_screen_shown = False
        self._started_at = monotonic()
        settings.snapshot_state = "UPDATING"
        settings.status = "COLMAP 데이터 업데이트 중: 0.0%"
        settings.status_kind = "RUNNING"

    def _draw_progress_overlay(self) -> None:
        """Provider 시작 전 0%부터 Scene Commit까지 중앙 진행률을 그립니다."""

        draw_progress_overlay(
            _snapshot_progress_overlay_view(
                self._provider_handle,
                self._job,
                bpy.context,
                self._started_at,
            )
        )

    def _start_progress_ui(self) -> None:
        """GUI Snapshot 업데이트의 중앙 진행창을 등록합니다."""

        self._overlay_handle = add_progress_overlay_draw_handler(
            self._draw_progress_overlay
        )
        request_progress_overlay_redraw()

    def _update_progress_feedback(self, context: Any) -> None:
        """현재 백분율을 Scene 상태와 중앙 진행창에 함께 반영합니다."""

        _phase, fraction, _completed, _total = _combined_progress_values(
            self._provider_handle,
            self._job,
        )
        settings = context.scene.projection_paint
        settings.status = f"COLMAP 데이터 업데이트 중: {fraction * 100.0:.1f}%"
        settings.status_kind = "RUNNING"
        request_progress_overlay_redraw()

    def _finish_success(self, context: Any, result: Any) -> set[str] | None:
        """Commit 결과를 연결하고 100% 화면을 한 Tick 표시한 뒤 종료합니다."""

        settings = context.scene.projection_paint
        if self._result is None:
            self._result = result
            try:
                from .camera_review import initialize_camera_review

                initialize_camera_review(context)
            except Exception as exc:
                settings.status = (
                    f"Snapshot은 완료됐지만 Camera Review 연결에 실패했습니다: {exc}"
                )
                settings.status_kind = "WARNING"
            else:
                settings.status = (
                    f"COLMAP Snapshot 업데이트 완료: Camera {result.camera_count}개 · "
                    f"Point {result.point_count}개"
                )
                settings.status_kind = "SUCCESS"
        if self._overlay_handle is not None and not self._final_screen_shown:
            self._final_screen_shown = True
            request_progress_overlay_redraw()
            return None
        self._cleanup_progress_ui(context)
        self._provider_handle = None
        self._job = None
        self._result = None
        return {"FINISHED"}

    def _finish_error(self, context: Any, exc: Exception) -> set[str]:
        """Staging을 폐기하고 기존 READY Snapshot을 그대로 유지합니다."""

        if self._job is not None:
            self._job.abort()
        if self._provider_handle is not None:
            self._provider_handle.abort()
        settings = context.scene.projection_paint
        settings.snapshot_state = (
            self._previous_state
            if self._previous_state in {"READY", "STALE"}
            else "ERROR"
        )
        settings.status = f"COLMAP Snapshot 업데이트 실패: {exc}"
        settings.status_kind = "ERROR"
        self.report({"ERROR"}, str(exc))
        self._cleanup_progress_ui(context)
        self._provider_handle = None
        self._job = None
        self._result = None
        return {"CANCELLED"}

    def _advance(self, context: Any) -> set[str] | None:
        """한 Event Tick에서 Provider 복원·값 생성 또는 PP Staging을 전진시킵니다."""

        if self._result is not None:
            return self._finish_success(context, self._result)
        if self._provider_handle is None:
            self._provider_handle = open_snapshot_provider(context, "modifier")
            self._update_progress_feedback(context)
            return None
        if not self._provider_handle.done:
            self._provider_handle.advance(20.0)
            self._update_progress_feedback(context)
            return None
        if self._job is None:
            snapshot = self._provider_handle.result
            if snapshot is None:
                raise RuntimeError("Modifier Snapshot Provider 결과가 없습니다.")
            provider_handle = self._provider_handle
            scene = context.scene
            request = build_snapshot_import_request(
                context,
                snapshot,
                source_validator=lambda: validate_open_provider_snapshot(
                    scene,
                    provider_handle,
                    snapshot,
                    "modifier",
                ),
            )
            self._job = CameraImportJob(context.scene, request)
            self._update_progress_feedback(context)
            return None
        if self._job.step(time_budget=0.025, hard_cap=256):
            result = self._job.commit()
            return self._finish_success(context, result)
        self._update_progress_feedback(context)
        return None

    def _cleanup_progress_ui(self, context: Any) -> None:
        """Modal Timer와 중앙 진행창을 중복 호출에도 안전하게 해제합니다."""

        if self._timer is not None:
            try:
                context.window_manager.event_timer_remove(self._timer)
            except (ReferenceError, RuntimeError, ValueError):
                pass
        self._timer = None
        remove_progress_overlay_draw_handler(self._overlay_handle)
        self._overlay_handle = None
        request_progress_overlay_redraw()

    def execute(self, context: Any) -> set[str]:
        """Background에서는 Snapshot Staging과 Commit을 동기 완료합니다."""

        try:
            self._prepare(context)
            while True:
                result = self._advance(context)
                if result is not None:
                    return result
        except Exception as exc:
            return self._finish_error(context, exc)

    def invoke(self, context: Any, event: Any) -> set[str]:
        """GUI에서는 긴 Point·Camera 생성을 Modal Timer로 분할합니다."""

        del event
        if bpy.app.background or getattr(context, "window", None) is None:
            return self.execute(context)
        try:
            self._prepare(context)
            self._start_progress_ui()
            self._timer = context.window_manager.event_timer_add(
                _TIMER_INTERVAL_SECONDS,
                window=context.window,
            )
            context.window_manager.modal_handler_add(self)
            return {"RUNNING_MODAL"}
        except Exception as exc:
            return self._finish_error(context, exc)

    def modal(self, context: Any, event: Any) -> set[str]:
        """Timer에서만 Staging을 전진시키고 ESC에는 기존 Snapshot을 유지합니다."""

        if event.type == "ESC":
            return self._finish_error(context, RuntimeError("사용자가 업데이트를 취소했습니다."))
        if event.type != "TIMER":
            return {"RUNNING_MODAL"}
        try:
            result = self._advance(context)
            return result if result is not None else {"RUNNING_MODAL"}
        except Exception as exc:
            return self._finish_error(context, exc)

    def cancel(self, context: Any) -> None:
        """Blender가 Modal을 취소하면 Staging ID와 진행 UI를 정리합니다."""

        if self._job is not None:
            self._job.abort()
        settings = context.scene.projection_paint
        settings.snapshot_state = (
            self._previous_state
            if self._previous_state in {"READY", "STALE"}
            else "ERROR"
        )
        settings.status = "COLMAP Snapshot 업데이트가 중단되었습니다."
        settings.status_kind = "ERROR"
        self._cleanup_progress_ui(context)
        if self._provider_handle is not None:
            self._provider_handle.abort()
        self._provider_handle = None
        self._job = None
        self._result = None


CLASSES = (PP_OT_update_colmap_snapshot,)


__all__ = [
    "CLASSES",
    "modifier_snapshot_available",
    "snapshot_is_ready",
    "snapshot_update_available",
    "PP_OT_update_colmap_snapshot",
]
