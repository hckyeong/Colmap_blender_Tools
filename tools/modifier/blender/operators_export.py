"""현재 편집 결과를 원본 COLMAP sparse 모델에 안전하게 반영합니다."""

from __future__ import annotations

import bpy

from ..core.export_transaction import execute_export
from ..core.export_validation import prepare_export_plan
from .camera_filter_queue import (
    has_pending_filter_data,
    require_no_pending_filters,
)
from .export_completion import (
    commit_exported_runtime as _commit_exported_runtime,
    export_error_message as _export_error_message,
    finish_export_success,
)
from .export_progress_session import (
    create_export_progress_session,
    release_export_progress_session,
)
from .operation_guard import export_mutation_allowed
from .runtime_state import (
    assert_export_runtime_token,
    cached_runtime_for_export,
    validated_runtime_token,
)


def _export_ready(context) -> bool:
    settings = getattr(context.scene, "colmap_import", None)
    return bool(
        settings
        and settings.dataset_uuid
        and settings.active_model_directory
        and settings.edit_revision > 0
        and not settings.point_edit_mode_active
        and not settings.camera_edit_mode_active
        and not has_pending_filter_data(settings)
        and not settings.export_running
        and export_mutation_allowed(context)
    )


def _prepare_export(context):
    settings = context.scene.colmap_import
    require_no_pending_filters(context, "Export")
    if settings.point_edit_mode_active or settings.camera_edit_mode_active:
        raise ValueError("Edit Point/Camera Mode를 종료한 뒤 Export하십시오.")

    state = cached_runtime_for_export(context.scene)
    token = validated_runtime_token(state)
    return state, prepare_export_plan(
        state.source,
        backup_enabled=settings.export_backup_enabled,
        runtime_token=token,
        output_format=settings.export_output_format,
    )


def _execution_inputs(context, prepared_plan):
    """확인창 계획을 재사용하거나 EXEC_DEFAULT 계획을 정확히 한 번 만듭니다."""

    if prepared_plan is None:
        return _prepare_export(context)

    settings = context.scene.colmap_import
    require_no_pending_filters(context, "Export")
    if settings.point_edit_mode_active or settings.camera_edit_mode_active:
        raise ValueError("Edit Point/Camera Mode를 종료한 뒤 Export하십시오.")
    if settings.export_backup_enabled != prepared_plan.backup_enabled:
        raise ValueError(
            "Export 확인 이후 Backup 설정이 변경되었습니다. Export를 다시 실행하십시오."
        )
    requested_format = (
        prepared_plan.source.input_format
        if settings.export_output_format == "SOURCE"
        else settings.export_output_format.lower()
    )
    if requested_format != prepared_plan.output_format:
        raise ValueError(
            "Export 확인 이후 출력 형식이 변경되었습니다. Export를 다시 실행하십시오."
        )
    state = assert_export_runtime_token(context.scene, prepared_plan.runtime_token)
    return state, prepared_plan


class COLMAP_OT_export(bpy.types.Operator):
    bl_idname = "colmap.export"
    bl_label = "Export"
    bl_description = (
        "현재 편집 결과를 원본 COLMAP sparse 파일에 덮어씁니다. "
        "Backup을 켜면 먼저 sparse_backup에 원본 스냅샷을 저장합니다"
    )
    bl_options = {"REGISTER"}

    _prepared_plan = None

    @classmethod
    def poll(cls, context):
        return _export_ready(context)

    def invoke(self, context, event):
        settings = context.scene.colmap_import
        try:
            _, self._prepared_plan = _prepare_export(context)
        except Exception as exc:
            settings.export_status = f"Export 사전검사 실패: {exc}"
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        return context.window_manager.invoke_props_dialog(
            self,
            width=460,
            title="COLMAP Export 확인",
            confirm_text="Export",
        )

    def draw(self, context):
        settings = context.scene.colmap_import
        layout = self.layout
        layout.label(
            text="현재 편집 결과로 원본 sparse 파일을 덮어씁니다.",
            icon="ERROR",
        )
        layout.label(text=f"대상: {settings.active_model_directory}")
        output_format = (
            self._prepared_plan.output_format
            if self._prepared_plan is not None
            else (
                settings.active_input_format
                if settings.export_output_format == "SOURCE"
                else settings.export_output_format.lower()
            )
        )
        layout.label(
            text=(
                "출력 형식: Text (.txt)"
                if output_format == "text"
                else "출력 형식: Binary (.bin)"
            )
        )
        if (
            self._prepared_plan is not None
            and self._prepared_plan.output_format
            != self._prepared_plan.source_layout.input_format
        ):
            layout.label(
                text="형식 전환 후 반대 형식 모델 파일은 제거됩니다.",
                icon="INFO",
            )
        if settings.export_backup_enabled:
            layout.label(
                text="원본은 sparse_backup 스냅샷으로 먼저 보존됩니다.",
                icon="CHECKMARK",
            )
        else:
            layout.label(
                text="Backup이 꺼져 있어 영구 백업이 남지 않습니다.",
                icon="ERROR",
            )
        layout.label(text="Blender Undo로 디스크 파일을 복구할 수 없습니다.")

    def execute(self, context):
        """확정된 데이터셋 변환을 출력 작업에 전달하고 새 기준선을 설치합니다."""

        settings = context.scene.colmap_import
        if settings.export_running:
            self.report({"WARNING"}, "이미 Export가 진행 중입니다.")
            return {"CANCELLED"}

        use_modal_progress = bool(
            self._prepared_plan is not None
            and not bpy.app.background
            and context.window is not None
        )
        if use_modal_progress:
            return self._start_modal_export(context)

        settings.export_running = True
        settings.export_status = "Export 중: 원본과 편집 결과를 검증하고 있습니다."
        try:
            state, plan = _execution_inputs(context, self._prepared_plan)
            result = execute_export(
                plan, state.current_reconstruction,
                dataset_transform=state.dataset_transform,
            )
        except Exception as exc:
            error_message = _export_error_message(exc)
            settings.export_status = f"Export 실패: {error_message}"
            self.report({"ERROR"}, error_message)
            return {"CANCELLED"}
        finally:
            settings.export_running = False
            self._prepared_plan = None

        try:
            warnings = finish_export_success(context, state, result)
        except Exception as exc:
            settings.export_status = (
                "파일 Export는 완료되었지만 Blender 상태 갱신에 실패했습니다. "
                f"다시 Import하십시오: {exc}"
            )
            self.report({"ERROR"}, settings.export_status)
            return {"CANCELLED"}

        if warnings:
            self.report({"WARNING"}, warnings[0])
        else:
            self.report({"INFO"}, settings.export_status)
        return {"FINISHED"}

    def _start_modal_export(self, context):
        """확인창 경로만 Core Job과 Modal 진행 UI로 위임합니다."""

        settings = context.scene.colmap_import
        session = None
        settings.export_running = True
        settings.export_status = "Export 진행창을 준비하고 있습니다."
        try:
            state, plan = _execution_inputs(context, self._prepared_plan)
            session = create_export_progress_session(
                context.scene,
                state,
                plan,
            )
            operator_result = bpy.ops.colmap.export_progress(
                "INVOKE_DEFAULT",
                job_id=session.job_id,
            )
            if "RUNNING_MODAL" not in operator_result:
                raise RuntimeError("Export 진행 Operator를 시작하지 못했습니다.")
        except Exception as exc:
            if session is not None:
                try:
                    release_export_progress_session(session.job_id)
                except Exception as abort_exc:
                    settings.export_status = (
                        "Export 시작 실패 후 작업 정리에도 실패했습니다. "
                        f"원본과 복구 자료를 확인하십시오: {abort_exc}"
                    )
            else:
                settings.export_running = False
            if not settings.export_status.startswith(
                (
                    "Export 진행 UI 시작 실패",
                    "Export 시작 실패 후 작업 정리에도 실패",
                )
            ):
                settings.export_status = f"Export 시작 실패: {exc}"
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        finally:
            self._prepared_plan = None

        settings.export_status = "Export 진행 중: 단계별 작업 상태를 표시합니다."
        return {"FINISHED"}


CLASSES = (COLMAP_OT_export,)
