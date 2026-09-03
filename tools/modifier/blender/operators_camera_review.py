"""Camera Review 단계 2~3의 탐색과 Viewport 오퍼레이터입니다."""

from __future__ import annotations

import bpy

from .camera_review_session import (
    current_review_session,
    ensure_review_session,
    navigate_review,
    review_change_allowed,
)
from .camera_filter_queue import (
    PENDING_STATUS_APPLYING,
    PENDING_STATUS_READY,
    PENDING_STATUS_STALE,
    begin_pending_apply,
    clear_pending_filters,
    complete_pending_apply,
    current_camera_is_pending,
    has_pending_filter_data,
    pending_for_apply,
    queue_current_camera,
    restore_pending_after_failed_apply,
    undo_last_pending_filter,
    unqueue_current_camera,
)
from .camera_review_timeline import build_or_refresh_timeline
from .camera_review_viewport import look_through_current
from .edit_workflows import (
    PendingRemove,
    apply_pending_remove,
    prepare_image_remove,
    validate_prepared_image_remove,
)
from .operation_guard import (
    export_mutation_allowed,
    require_export_mutation_allowed,
)


def _review_ready(context) -> bool:
    settings = getattr(context.scene, "colmap_import", None)
    return bool(
        settings
        and export_mutation_allowed(context)
        and review_change_allowed(context)
        and settings.review_pending_filter_status
        not in {PENDING_STATUS_APPLYING, PENDING_STATUS_STALE}
        and settings.review_total_count > 0
        and settings.review_current_index >= 0
    )


def _viewport_window_region_available(context) -> bool:
    """현재 Context에 실제 3D Viewport WINDOW Region이 있는지 확인합니다."""

    area = getattr(context, "area", None)
    return bool(
        area is not None
        and area.type == "VIEW_3D"
        and any(region.type == "WINDOW" for region in area.regions)
    )


def _modifier_camera_view_active(context) -> bool:
    """활성 Modifier 세션의 3D Viewport가 Camera View인지 확인합니다."""

    from .modifier_session import is_session_active_for_window
    from .camera_review_timeline import modifier_camera_session_is_active

    area = getattr(context, "area", None)
    space = getattr(context, "space_data", None)
    region_3d = getattr(space, "region_3d", None)
    return bool(
        is_session_active_for_window(getattr(context, "window", None))
        and modifier_camera_session_is_active(
            current_review_session(context),
            window=getattr(context, "window", None),
        )
        and area is not None
        and area.type == "VIEW_3D"
        and region_3d is not None
        and region_3d.view_perspective == "CAMERA"
        and current_review_session(context) is not None
    )


class COLMAP_OT_review_previous(bpy.types.Operator):
    bl_idname = "colmap.review_previous"
    bl_label = "Previous"
    bl_description = "이전 생존 IMAGE_ID로 Camera Review를 이동합니다"
    bl_options = {"REGISTER"}

    @classmethod
    def poll(cls, context):
        return bool(
            _review_ready(context)
            and context.scene.colmap_import.review_current_index > 0
        )

    def execute(self, context):
        try:
            require_export_mutation_allowed(context, "Camera Review 이동")
            navigate_review(context, "PREVIOUS")
            return {"FINISHED"}
        except Exception as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}


class COLMAP_OT_review_next(bpy.types.Operator):
    bl_idname = "colmap.review_next"
    bl_label = "Next"
    bl_description = "다음 생존 IMAGE_ID로 Camera Review를 이동합니다"
    bl_options = {"REGISTER"}

    @classmethod
    def poll(cls, context):
        settings = getattr(context.scene, "colmap_import", None)
        return bool(
            _review_ready(context)
            and settings.review_current_index < settings.review_total_count - 1
        )

    def execute(self, context):
        try:
            require_export_mutation_allowed(context, "Camera Review 이동")
            navigate_review(context, "NEXT")
            return {"FINISHED"}
        except Exception as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}


class COLMAP_OT_review_look_through(bpy.types.Operator):
    bl_idname = "colmap.review_look_through"
    bl_label = "Look Through Camera"
    bl_description = "현재 3D Viewport만 선택된 COLMAP Camera View로 전환합니다"
    bl_options = {"REGISTER"}

    @classmethod
    def poll(cls, context):
        return bool(
            _review_ready(context)
            and _viewport_window_region_available(context)
        )

    def execute(self, context):
        try:
            require_export_mutation_allowed(context, "Camera View 전환")
            if not review_change_allowed(context):
                raise ValueError(
                    "Point 또는 Camera Edit Mode 중에는 Camera Review를 변경할 수 없습니다."
                )
            session = ensure_review_session(context)
            look_through_current(context, session)
            return {"FINISHED"}
        except Exception as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}


class COLMAP_OT_review_background_toggle(bpy.types.Operator):
    """Modifier Camera View의 Review 배경 이미지 표시를 전환합니다."""

    bl_idname = "colmap.review_background_toggle"
    bl_label = "Toggle Background Image"
    bl_description = "Camera View에서 Modifier Review 배경 이미지를 켜거나 끕니다"
    bl_options = {"REGISTER"}

    @classmethod
    def poll(cls, context):
        """활성 Modifier 세션의 Camera View에서만 단축키 실행을 허용합니다."""

        return _modifier_camera_view_active(context)

    def execute(self, context):
        """설정값을 반전해 기존 Review 배경 갱신 경로를 실행합니다."""

        settings = context.scene.colmap_import
        try:
            settings.review_background_enabled = not bool(
                settings.review_background_enabled
            )
            state = "ON" if settings.review_background_enabled else "OFF"
            self.report({"INFO"}, f"Background Image: {state}")
            return {"FINISHED"}
        except Exception as exc:
            settings.review_status = f"Background Image 전환 실패: {exc}"
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}


class COLMAP_OT_review_build_timeline(bpy.types.Operator):
    bl_idname = "colmap.review_build_timeline"
    bl_label = "Build / Refresh Timeline"
    bl_description = "생존 Camera를 연속 frame의 소유 Marker로 연결하거나 갱신합니다"
    bl_options = {"REGISTER"}

    @classmethod
    def poll(cls, context):
        settings = getattr(context.scene, "colmap_import", None)
        return bool(
            _review_ready(context)
            and settings
            and not has_pending_filter_data(settings)
        )

    def execute(self, context):
        settings = context.scene.colmap_import
        try:
            require_export_mutation_allowed(context, "Timeline 갱신")
            session = ensure_review_session(context)
            warnings = build_or_refresh_timeline(context, session)
            settings.review_status = (
                warnings[0]
                if warnings
                else (
                    f"Timeline Camera {len(session.review_index.entries)}개를 "
                    f"Frame {session.frame_start}부터 갱신했습니다."
                )
            )
            if warnings:
                self.report({"WARNING"}, warnings[0])
            return {"FINISHED"}
        except Exception as exc:
            settings.review_status = f"Timeline 갱신 실패: {exc}"
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}


class COLMAP_OT_review_filter_current(bpy.types.Operator):
    bl_idname = "colmap.review_filter_current"
    bl_label = "Filter Current Camera"
    bl_description = "현재 Camera를 실제 삭제 없이 Pending Filter 목록에 추가합니다"
    bl_options = {"REGISTER"}

    @classmethod
    def poll(cls, context):
        return _review_ready(context)

    def execute(self, context):
        settings = context.scene.colmap_import
        try:
            require_export_mutation_allowed(context, "Camera Filter 변경")
            if current_camera_is_pending(context):
                pending, warnings = unqueue_current_camera(context)
                message = (
                    f"현재 Camera를 Pending에서 제외했습니다. "
                    f"남은 Camera {len(pending.image_ids)}개"
                )
            else:
                pending = queue_current_camera(context)
                warnings = ()
                message = f"Pending Camera {len(pending.image_ids)}개를 등록했습니다."
            if warnings:
                settings.warning_summary = "\n".join(warnings)
                self.report({"WARNING"}, warnings[0])
            else:
                self.report({"INFO"}, message)
            return {"FINISHED"}
        except Exception as exc:
            settings.review_status = f"Camera Filter 변경 실패: {exc}"
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}


class COLMAP_OT_review_undo_last_filter(bpy.types.Operator):
    bl_idname = "colmap.review_undo_last_filter"
    bl_label = "Undo Last Filter"
    bl_description = "가장 최근에 추가한 Pending Camera 하나를 실제 데이터 변경 없이 복원합니다"
    bl_options = {"REGISTER"}

    @classmethod
    def poll(cls, context):
        settings = getattr(context.scene, "colmap_import", None)
        return bool(
            settings
            and export_mutation_allowed(context)
            and review_change_allowed(context)
            and settings.review_pending_filter_status == PENDING_STATUS_READY
            and settings.review_pending_camera_count > 0
        )

    def execute(self, context):
        settings = context.scene.colmap_import
        try:
            require_export_mutation_allowed(context, "마지막 Pending Filter 취소")
            pending, warnings = undo_last_pending_filter(context)
            if warnings:
                settings.warning_summary = "\n".join(warnings)
                self.report({"WARNING"}, warnings[0])
            else:
                self.report(
                    {"INFO"},
                    f"마지막 Pending Filter를 취소했습니다. 남은 Camera {len(pending.image_ids)}개",
                )
            return {"FINISHED"}
        except Exception as exc:
            settings.review_status = f"마지막 Pending Filter 취소 실패: {exc}"
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}


class COLMAP_OT_review_apply_pending_filters(bpy.types.Operator):
    bl_idname = "colmap.review_apply_pending_filters"
    bl_label = "Apply Pending Filters"
    bl_description = "대기 중인 모든 Camera와 관측 Point3D를 하나의 편집 작업으로 제거합니다"
    bl_options = {"REGISTER", "UNDO"}

    _pending_remove: PendingRemove | None = None
    _image_ids: tuple[int, ...] = ()

    @classmethod
    def poll(cls, context):
        settings = getattr(context.scene, "colmap_import", None)
        return bool(
            settings
            and export_mutation_allowed(context)
            and settings.review_pending_filter_status == PENDING_STATUS_READY
            and settings.review_pending_camera_count > 0
        )

    def _prepare(self, context):
        from .runtime_state import cached_runtime_for_pending

        require_export_mutation_allowed(context, "Pending Filter 적용")
        if not review_change_allowed(context):
            raise ValueError(
                "Point 또는 Camera Edit Mode 중에는 Pending Filter를 적용할 수 없습니다."
            )
        state = cached_runtime_for_pending(context.scene)
        pending = pending_for_apply(context, state)
        state, self._pending_remove = prepare_image_remove(
            context,
            pending.image_ids,
            state=state,
        )
        self._image_ids = pending.image_ids
        return state, pending, self._pending_remove

    def invoke(self, context, event):
        try:
            _, pending, remove_pending = self._prepare(context)
            context.scene.colmap_import.review_status = (
                f"적용 예정: Camera {len(pending.image_ids)}개, Point3D "
                f"{len(remove_pending.plan.observed_point3d_ids)}개"
            )
            return context.window_manager.invoke_confirm(self, event)
        except Exception as exc:
            context.scene.colmap_import.review_status = (
                f"Pending Filter 사전검사 실패: {exc}"
            )
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}

    def draw(self, context):
        del context
        if self._pending_remove is None:
            return
        plan = self._pending_remove.plan
        self.layout.label(
            text=f"대기 Camera {len(plan.requested_image_ids)}개를 제거합니다."
        )
        self.layout.label(
            text=f"관측 Point3D {len(plan.observed_point3d_ids)}개를 전역 제거합니다."
        )
        self.layout.label(text="하나의 Undo 작업으로 기록됩니다.", icon="INFO")

    def execute(self, context):
        settings = context.scene.colmap_import
        applying_started = False
        try:
            from .runtime_state import ensure_runtime

            require_export_mutation_allowed(context, "Pending Filter 적용")
            if self._pending_remove is None:
                self._prepare(context)
            state = ensure_runtime(context.scene)
            state, remove_pending = validate_prepared_image_remove(
                context,
                self._pending_remove,
                self._image_ids,
                state=state,
            )
            pending = pending_for_apply(context, state)
            if pending.image_ids != self._image_ids:
                raise ValueError("확인 후 Pending IMAGE_ID 목록이 변경되었습니다.")
            begin_pending_apply(context, pending)
            applying_started = True
            warnings = apply_pending_remove(
                context,
                state,
                remove_pending,
                atomic_review=True,
            )
            complete_pending_apply(context)
            settings.review_status = (
                f"Pending Camera {len(pending.image_ids)}개와 Point3D "
                f"{len(remove_pending.plan.observed_point3d_ids)}개를 적용했습니다."
            )
            if warnings:
                settings.warning_summary = "\n".join(warnings)
                self.report({"WARNING"}, warnings[0])
            else:
                self.report({"INFO"}, settings.review_status)
            return {"FINISHED"}
        except Exception as exc:
            if applying_started:
                try:
                    restore_pending_after_failed_apply(context)
                except Exception as restore_exc:
                    settings.review_status = (
                        f"Pending Filter 적용 및 표시 복원 실패: {exc}; {restore_exc}"
                    )
                    self.report({"ERROR"}, settings.review_status)
                    return {"CANCELLED"}
            settings.review_status = f"Pending Filter 적용 실패: {exc}"
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        finally:
            self._pending_remove = None
            self._image_ids = ()


class COLMAP_OT_review_clear_pending_filters(bpy.types.Operator):
    bl_idname = "colmap.review_clear_pending_filters"
    bl_label = "Clear Pending Filters"
    bl_description = "실제 데이터 변경 없이 모든 Pending Filter 표시를 취소합니다"
    bl_options = {"REGISTER"}

    @classmethod
    def poll(cls, context):
        settings = getattr(context.scene, "colmap_import", None)
        return bool(
            settings
            and export_mutation_allowed(context)
            and has_pending_filter_data(settings)
            and settings.review_pending_filter_status
            != PENDING_STATUS_APPLYING
        )

    def execute(self, context):
        try:
            require_export_mutation_allowed(context, "Pending Filter 취소")
            warnings = clear_pending_filters(context)
            if warnings:
                context.scene.colmap_import.warning_summary = "\n".join(warnings)
                self.report({"WARNING"}, warnings[0])
            return {"FINISHED"}
        except Exception as exc:
            context.scene.colmap_import.review_status = (
                f"Pending Filter 취소 실패: {exc}"
            )
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}


CLASSES = (
    COLMAP_OT_review_previous,
    COLMAP_OT_review_next,
    COLMAP_OT_review_look_through,
    COLMAP_OT_review_background_toggle,
    COLMAP_OT_review_filter_current,
    COLMAP_OT_review_undo_last_filter,
    COLMAP_OT_review_apply_pending_filters,
    COLMAP_OT_review_clear_pending_filters,
    COLMAP_OT_review_build_timeline,
)
