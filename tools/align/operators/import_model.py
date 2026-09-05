"""COLMAP sparse model 폴더 선택과 TIMER 기반 가져오기 오퍼레이터를 제공합니다."""

from __future__ import annotations

from time import perf_counter
from pathlib import Path
from typing import Any

import bpy
from bpy.props import BoolProperty, EnumProperty, IntProperty, StringProperty

from ..display_axes import (
    AXIS_ITEMS,
    AXIS_PRESET_ITEMS,
    CUSTOM_AXIS_PRESET,
    DEFAULT_AXIS_PRESET,
    DEFAULT_FORWARD_AXIS,
    DEFAULT_UP_AXIS,
    axis_pair_summary,
    axes_for_preset,
    axis_summary,
    ensure_distinct_axis_pair,
    validate_axis_pair,
)
from ..importer import (
    IMPORT_STAGE_COMPLETED,
    IMPORT_STAGE_FAILED,
    ImportJobError,
    begin_import_job,
    cancel_import_job,
)
from ..logging_utils import get_logger
from ..project_persistence.runtime import (
    notify_project_state_changed,
    restore_input_block_reason,
)
from ..session import is_session_active_for_window
from ..state import RUNTIME
from ..status import set_workspace_status_text
from ..viewport.renderer import request_redraw
from ..viewport.display_sampling import DEFAULT_DISPLAY_POINT_LIMIT
from .marker_alignment import is_marker_preview_active
from .layer_transform import layer_transform_active_block_reason


_LOGGER = get_logger("operators.import_model")
_TIMER_INTERVAL_SECONDS = 0.01
_INPUT_FORMAT_ITEMS = (
    ("AUTO", "Auto", "완전한 BIN이 있으면 우선하고 없으면 TXT를 사용합니다"),
    ("BINARY", "Binary", "cameras.bin, images.bin, points3D.bin을 사용합니다"),
    ("TEXT", "Text", "cameras.txt, images.txt, points3D.txt를 사용합니다"),
)
_DISPLAY_POINT_LIMIT_SOFT_MIN = 10_000
_DISPLAY_POINT_LIMIT_SOFT_MAX = 2_000_000
_DISPLAY_POINT_LIMIT_MAXIMUM = 2_147_483_647


def _is_import_session_context(context: bpy.types.Context) -> bool:
    """가져오기 UI가 활성 COLMAP Align 세션의 유효 Window에서 열렸는지 확인합니다."""
    window = getattr(context, "window", None)
    return bool(window is not None and is_session_active_for_window(window))


def _import_preview_block_reason() -> str | None:
    """미리보기·변환·내보내기 중 새 가져오기를 차단할 정확한 사유를 반환합니다."""

    from ..axis_settings import axis_edit_active_block_reason

    axis_reason = axis_edit_active_block_reason(RUNTIME)
    if axis_reason is not None:
        return axis_reason
    if is_marker_preview_active():
        return "마커 정렬 미리보기를 취소한 뒤 COLMAP 모델을 가져오십시오."
    if bool(getattr(RUNTIME.merge_export, "is_running", False)):
        return "COLMAP Export가 끝난 뒤 모델을 가져오십시오."
    if bool(getattr(RUNTIME.obj_export, "is_running", False)):
        return "OBJ Export가 끝난 뒤 모델을 가져오십시오."
    transform_reason = layer_transform_active_block_reason()
    if transform_reason is not None:
        return transform_reason
    return None


def _report_status(operator: bpy.types.Operator, level: set[str], message: str) -> None:
    """Operator 보고와 Blender 상태 표시줄에 같은 사용자 결과를 전달합니다."""
    operator.report(level, message)
    workspace = getattr(bpy.context, "workspace", None)
    if workspace is not None:
        set_workspace_status_text(workspace, message)


def _show_import_popup(
    context: bpy.types.Context,
    *,
    title: str,
    icon: str,
    lines: tuple[str, ...],
) -> None:
    """GUI 가져오기 완료·실패 내용을 기준 Importer와 같은 팝업으로 표시합니다."""
    if bpy.app.background or context.window is None:
        return

    def draw_message(menu, _context) -> None:
        """팝업 본문을 전달받은 순서대로 한 줄씩 그립니다."""
        for line in lines:
            menu.layout.label(text=line)

    try:
        context.window_manager.popup_menu(draw_message, title=title, icon=icon)
    except (AttributeError, ReferenceError, RuntimeError, ValueError):
        pass


def _completion_popup_lines(job: Any) -> tuple[str, ...]:
    """완료된 데이터셋의 점군·연결 표면·축·시간을 팝업 행으로 만듭니다."""
    model = getattr(job, "staged_model", None)
    if model is None:
        return (str(getattr(job, "result_message", "COLMAP 모델 가져오기 완료")),)
    from ..model_repository import (
        model_camera_count,
        model_image_count,
        model_point_count,
    )

    elapsed_seconds = max(0.0, perf_counter() - float(job.started_at))
    display_transform = getattr(job, "display_transform", None)
    axis_line = (
        f"Display Axis: {axis_summary(display_transform)}"
        if display_transform is not None
        else "Display Axis: unavailable"
    )
    source_count = model_point_count(model)
    display_count = int(model.points.display_indices.size)
    all_points_suffix = " (All)" if display_count == source_count else ""
    return (
        f"Camera: {model_camera_count(model)}",
        f"Image: {model_image_count(model)}",
        f"Point3D: {source_count:,}",
        f"Viewport Points: {display_count:,} / {source_count:,}{all_points_suffix}",
        "Sampling: Deterministic Random",
        axis_line,
        f"Elapsed: {elapsed_seconds:.1f}s",
    ) + (
        (f"연결 표면: {job.staged_companion_mesh.face_count:,}면",)
        if getattr(job, "staged_companion_mesh", None) is not None else ()
    )


def _effective_axis_pair(operator: Any) -> tuple[str, str]:
    """고정 프리셋 또는 Custom 입력에서 실제 작업에 쓸 축 쌍을 검증합니다."""
    fixed_axes = axes_for_preset(operator.axis_preset)
    if fixed_axes is not None:
        return fixed_axes
    return validate_axis_pair(operator.axis_forward, operator.axis_up)


def _sync_file_browser_axis_pair(operator: Any) -> bool:
    """파일 탐색기 변경 뒤 고정 프리셋 축을 적용하고 Custom 축 쌍을 검증합니다."""
    forward, up = _effective_axis_pair(operator)
    changed = (
        operator.axis_forward != forward
        or operator.axis_up != up
    )
    if changed:
        operator.axis_forward = forward
        operator.axis_up = up
    return changed


def _on_file_browser_axis_preset_changed(operator: Any, _context: Any) -> None:
    """파일 탐색기 고정 프리셋 선택을 즉시 해당 축 쌍으로 동기화합니다."""
    _sync_file_browser_axis_pair(operator)


def _on_file_browser_axis_forward_changed(operator: Any, _context: Any) -> None:
    """Custom Forward 변경 때 Up 축의 같은 기준축 중복을 즉시 보정합니다."""
    if operator.axis_preset != CUSTOM_AXIS_PRESET:
        return
    forward, up = ensure_distinct_axis_pair(
        operator.axis_forward,
        operator.axis_up,
        "FORWARD",
    )
    if up != operator.axis_up:
        operator.axis_up = up
    if forward != operator.axis_forward:
        operator.axis_forward = forward


def _on_file_browser_axis_up_changed(operator: Any, _context: Any) -> None:
    """Custom Up 변경 때 Forward 축의 같은 기준축 중복을 즉시 보정합니다."""
    if operator.axis_preset != CUSTOM_AXIS_PRESET:
        return
    forward, up = ensure_distinct_axis_pair(
        operator.axis_forward,
        operator.axis_up,
        "UP",
    )
    if forward != operator.axis_forward:
        operator.axis_forward = forward
    if up != operator.axis_up:
        operator.axis_up = up


def _companion_preview(operator: Any) -> tuple[str, tuple[str, ...]]:
    """선택한 COLMAP 폴더의 연결 OBJ 후보를 파일 탐색기용으로 요약합니다."""
    if not operator.import_companion_obj or not str(operator.directory).strip():
        return "", ()
    from ..colmap_io import discover_sparse_model_with_dataset_root
    from ..companion_discovery import discover_companion_obj_candidates

    try:
        root, _layout = discover_sparse_model_with_dataset_root(
            bpy.path.abspath(operator.directory), operator.input_format,
        )
        candidates = discover_companion_obj_candidates(root)
    except (ValueError, OSError, RuntimeError) as error:
        return str(error), ()
    names = tuple(path.name for path in candidates)
    if len(names) == 1:
        return f"자동 연결: {names[0]}", names
    if not names:
        return "이 폴더에는 연결할 OBJ가 없습니다.", ()
    return f"OBJ {len(names)}개: 아래 파일 경로로 하나를 선택하십시오.", names


def _remove_import_timer(window_manager: Any) -> None:
    """현재 Runtime에 등록된 가져오기 TIMER를 한 번만 제거합니다."""
    timer = RUNTIME.import_timer
    if timer is None:
        return
    try:
        window_manager.event_timer_remove(timer)
    except (ReferenceError, RuntimeError):
        pass
    finally:
        RUNTIME.import_timer = None


def _end_progress(window_manager: Any) -> None:
    """열려 있을 수 있는 Blender 창 진행 막대를 안전하게 종료합니다."""
    try:
        window_manager.progress_end()
    except (ReferenceError, RuntimeError):
        pass


def fit_after_successful_import(context: bpy.types.Context, imported_model_id: str) -> None:
    """성공한 가져오기 모델이 속한 A/P 그룹의 보이는 레이어 전체를 자동 맞춤합니다."""

    from .workspace import WorkspaceConfigurationError, find_main_window, fit_role_view
    from ..constants import MODEL_STATUS_REGISTERED, ROLE_MOVING, ROLE_REFERENCE

    window = find_main_window(context.window_manager, context.window)
    if window is None:
        return
    record = RUNTIME.model_catalog.models.get(str(imported_model_id))
    if record is None:
        _LOGGER.warning("event=import_fit_skipped reason=missing_imported_model")
        return
    role = (
        ROLE_REFERENCE
        if record.registration_status == MODEL_STATUS_REGISTERED
        else ROLE_MOVING
    )
    try:
        fit_role_view(window, role)
    except WorkspaceConfigurationError as error:
        _LOGGER.warning("event=import_fit_skipped reason=%s", error)


def cancel_active_import_job(window_manager: Any | None = None) -> bool:
    """파일 로드·unregister 전에 진행 작업과 TIMER·진행 막대를 정리합니다."""
    job = RUNTIME.import_job
    if job is None:
        return False
    job.request_cancel()
    job.step(RUNTIME)
    manager = window_manager or getattr(bpy.context, "window_manager", None)
    if manager is not None:
        _remove_import_timer(manager)
        _end_progress(manager)
    RUNTIME.clear_import_job()
    _LOGGER.info("event=import_forced_cleanup")
    return True


class COLMAPALIGN_OT_import_model(bpy.types.Operator):
    """파일 탐색기에서 폴더·형식·표시 축·표시 점 상한을 확정해 가져오기를 시작합니다."""

    bl_idname = "colmap_align.import_model"
    bl_label = "COLMAP 모델 불러오기"
    bl_description = "폴더에서 표준 sparse/0 또는 단일 sparse model을 찾아 가져옵니다"
    bl_options = {"REGISTER"}

    directory: StringProperty(
        name="COLMAP Folder",
        description="COLMAP Dataset Root 또는 sparse model 폴더를 선택합니다.",
        subtype="DIR_PATH",
        options={"SKIP_SAVE"},
    )
    import_companion_obj: BoolProperty(
        name="OBJ 함께 불러오기",
        description="데이터셋 루트의 OBJ 형상을 같은 표시 축으로 연결합니다",
        default=True,
    )
    companion_obj_path: StringProperty(
        name="OBJ 파일",
        description="비워두면 OBJ 하나를 자동 연결하며, 여러 개면 루트의 파일 하나를 지정합니다",
        subtype="FILE_PATH",
        options={"SKIP_SAVE"},
    )
    filter_folder: BoolProperty(
        name="Folders Only",
        description="COLMAP Dataset Root 또는 sparse model 폴더만 선택합니다.",
        default=True,
        options={"HIDDEN", "SKIP_SAVE"},
    )
    input_format: EnumProperty(
        name="Input Format",
        description="COLMAP 입력 형식을 자동 감지하거나 명시적으로 선택합니다.",
        items=_INPUT_FORMAT_ITEMS,
        default="AUTO",
    )
    display_point_limit: IntProperty(
        name="Viewport Point Limit",
        description="가져오는 모델 하나에 표시할 최대 POINT3D 수입니다.",
        default=DEFAULT_DISPLAY_POINT_LIMIT,
        min=1,
        soft_min=_DISPLAY_POINT_LIMIT_SOFT_MIN,
        soft_max=_DISPLAY_POINT_LIMIT_SOFT_MAX,
        max=_DISPLAY_POINT_LIMIT_MAXIMUM,
        subtype="UNSIGNED",
    )
    axis_preset: EnumProperty(
        name="Display Axis",
        description="원본 COLMAP 데이터는 보존하고 화면 표시 축만 선택합니다.",
        items=AXIS_PRESET_ITEMS,
        default=DEFAULT_AXIS_PRESET,
        update=_on_file_browser_axis_preset_changed,
    )
    axis_forward: EnumProperty(
        name="Forward",
        description="Custom Display Axis의 원본 Forward 축입니다.",
        items=AXIS_ITEMS,
        default=DEFAULT_FORWARD_AXIS,
        update=_on_file_browser_axis_forward_changed,
    )
    axis_up: EnumProperty(
        name="Up",
        description="Custom Display Axis의 원본 Up 축입니다.",
        items=AXIS_ITEMS,
        default=DEFAULT_UP_AXIS,
        update=_on_file_browser_axis_up_changed,
    )

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        """활성 세션에서만 파일 선택을 열고 마커 입력과의 중첩을 막습니다."""
        settings = getattr(getattr(context, "scene", None), "colmap_align_settings", None)
        return bool(
            _is_import_session_context(context)
            and not RUNTIME.import_in_progress
            and not RUNTIME.modal_active
            and not is_marker_preview_active()
            and _import_preview_block_reason() is None
            and restore_input_block_reason() is None
            and settings is not None
        )

    def invoke(self, context: bpy.types.Context, _event: bpy.types.Event):
        """마지막 표시·연결 옵션으로 파일 탐색기를 열고 모델은 아직 추가하지 않습니다."""
        settings = context.scene.colmap_align_settings
        if not self.properties.is_property_set("directory"):
            self.directory = (
                settings.import_last_directory.strip()
                or settings.import_dataset_root.strip()
            )
        if not self.properties.is_property_set("input_format"):
            self.input_format = settings.import_input_format
        if not self.properties.is_property_set("import_companion_obj"):
            self.import_companion_obj = settings.import_companion_obj
        if not self.properties.is_property_set("display_point_limit"):
            self.display_point_limit = settings.import_display_point_limit
        if not self.properties.is_property_set("axis_preset"):
            self.axis_preset = settings.import_axis_preset
        if not self.properties.is_property_set("axis_forward"):
            self.axis_forward = settings.import_axis_forward
        if not self.properties.is_property_set("axis_up"):
            self.axis_up = settings.import_axis_up
        self.check(context)
        if bpy.app.background:
            return self.execute(context)
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}

    def draw(self, _context: bpy.types.Context) -> None:
        """파일 탐색기에 COLMAP 형식·OBJ 연결·표시 점·공통 축을 그립니다."""
        layout = self.layout
        layout.prop(self, "input_format")
        companion_box = layout.box()
        companion_box.prop(self, "import_companion_obj")
        if self.import_companion_obj:
            message, candidates = getattr(self, "_companion_preview_value", ("", ()))
            if message:
                companion_box.label(text=message, icon="INFO")
            companion_box.prop(self, "companion_obj_path", text="OBJ 파일")
            if len(candidates) > 1:
                for name in candidates[:5]:
                    companion_box.label(text=name, icon="MESH_DATA")
            companion_box.label(text="COLMAP과 같은 축 적용 · 텍스처 제외")
        point_box = layout.box()
        point_box.label(text="Viewport Points")
        point_box.prop(self, "display_point_limit", text="Point Limit")
        point_box.label(
            text=f"모델당 최대 {self.display_point_limit:,}점을 표시합니다.",
            icon="INFO",
        )
        point_box.label(text="원본 점이 더 적으면 모두 표시합니다.", icon="INFO")
        if self.display_point_limit > _DISPLAY_POINT_LIMIT_SOFT_MAX:
            warning_row = point_box.row()
            warning_row.alert = True
            warning_row.label(
                text="높은 값은 GPU 메모리와 피킹 속도를 크게 사용할 수 있습니다.",
                icon="ERROR",
            )
        axis_box = layout.box()
        axis_box.label(text="Display Axis")
        axis_box.prop(self, "axis_preset")
        fixed_axes = axes_for_preset(self.axis_preset)
        if fixed_axes is None:
            axis_box.prop(self, "axis_forward")
            axis_box.prop(self, "axis_up")
            try:
                summary = axis_pair_summary(*_effective_axis_pair(self))
            except ValueError:
                summary = "Forward와 Up은 서로 다른 기준 축이어야 합니다."
        else:
            summary = axis_pair_summary(*fixed_axes)
        axis_box.label(text=summary, icon="INFO")
        axis_box.label(text="COLMAP 원본 좌표·카메라 자세는 변경하지 않습니다.", icon="INFO")

    def check(self, _context: bpy.types.Context) -> bool:
        """파일 탐색기의 축 입력과 연결 OBJ 후보 안내를 갱신합니다."""
        before = getattr(self, "_companion_preview_value", None)
        self._companion_preview_value = _companion_preview(self)
        try:
            return _sync_file_browser_axis_pair(self) or before != self._companion_preview_value
        except ValueError:
            return before != self._companion_preview_value

    def execute(self, context: bpy.types.Context):
        """표시·연결 선택을 검증하고 하나의 데이터셋 가져오기 작업에 전달합니다."""
        restore_reason = restore_input_block_reason()
        if restore_reason is not None:
            _report_status(self, {"WARNING"}, restore_reason)
            return {"CANCELLED"}
        preview_reason = _import_preview_block_reason()
        if preview_reason is not None:
            _report_status(self, {"WARNING"}, preview_reason)
            return {"CANCELLED"}
        settings = context.scene.colmap_align_settings
        try:
            axis_forward, axis_up = _effective_axis_pair(self)
        except ValueError as error:
            _report_status(self, {"ERROR"}, str(error))
            return {"CANCELLED"}
        source_path = self.directory.strip()
        if not source_path:
            _report_status(self, {"ERROR"}, "COLMAP Folder를 선택하십시오.")
            return {"CANCELLED"}
        source_path = bpy.path.abspath(source_path)
        companion_path = ""
        if self.import_companion_obj:
            from ..colmap_io import discover_sparse_model_with_dataset_root
            from ..companion_discovery import resolve_companion_obj_path
            from ..colmap_io.discovery import standard_sparse_model_paths

            try:
                dataset_root, layout = discover_sparse_model_with_dataset_root(source_path, self.input_format)
                selected_obj = self.companion_obj_path.strip()
                if selected_obj:
                    selected_obj = bpy.path.abspath(selected_obj)
                resolved_obj = resolve_companion_obj_path(dataset_root, selected_obj)
                if resolved_obj is not None:
                    if Path(source_path).resolve() != layout.source_path.resolve() and len(
                        standard_sparse_model_paths(dataset_root, self.input_format)
                    ) > 1:
                        raise ValueError("OBJ와 연결할 sparse 모델 폴더를 직접 선택하십시오.")
                    companion_path = str(resolved_obj)
            except (ValueError, OSError, RuntimeError) as error:
                _report_status(self, {"ERROR"}, str(error))
                return {"CANCELLED"}
        display_point_limit = int(self.display_point_limit)
        settings.import_last_directory = source_path
        settings.import_dataset_root = source_path
        settings.import_input_format = self.input_format
        settings.import_companion_obj = self.import_companion_obj
        settings.import_display_point_limit = display_point_limit
        settings.import_axis_preset = self.axis_preset
        settings.import_axis_forward = axis_forward
        settings.import_axis_up = axis_up
        _LOGGER.info(
            "event=import_file_browser_confirmed directory=%s input_format=%s "
            "display_point_limit=%s axis_preset=%s forward=%s up=%s",
            source_path,
            self.input_format,
            display_point_limit,
            self.axis_preset,
            axis_forward,
            axis_up,
        )
        result = bpy.ops.colmap_align.import_model_job(
            "INVOKE_DEFAULT",
            source_path=source_path,
            input_format=self.input_format,
            display_point_limit=display_point_limit,
            axis_preset=self.axis_preset,
            axis_forward=axis_forward,
            axis_up=axis_up,
            import_companion_obj=self.import_companion_obj,
            companion_obj_path=companion_path,
        )
        return result


class COLMAPALIGN_OT_import_model_job(bpy.types.Operator):
    """COLMAP 읽기·검증·GPU 준비·원자 등록을 TIMER 단위로 실행합니다."""

    bl_idname = "colmap_align.import_model_job"
    bl_label = "COLMAP 모델 가져오기 작업"
    bl_description = "선택한 COLMAP sparse model을 검증한 뒤 Data Layers에 추가합니다"
    bl_options = {"INTERNAL"}

    source_path: StringProperty(name="COLMAP Dataset Root", options={"HIDDEN"})
    import_companion_obj: BoolProperty(default=True, options={"HIDDEN"})
    companion_obj_path: StringProperty(default="", options={"HIDDEN"})
    input_format: EnumProperty(
        name="COLMAP 입력 형식",
        items=_INPUT_FORMAT_ITEMS,
        default="AUTO",
        options={"HIDDEN"},
    )
    display_point_limit: IntProperty(
        name="Viewport Point Limit",
        default=DEFAULT_DISPLAY_POINT_LIMIT,
        min=1,
        max=_DISPLAY_POINT_LIMIT_MAXIMUM,
        subtype="UNSIGNED",
        options={"HIDDEN"},
    )
    axis_preset: EnumProperty(
        name="Display Axis",
        items=AXIS_PRESET_ITEMS,
        default=DEFAULT_AXIS_PRESET,
        options={"HIDDEN"},
    )
    axis_forward: EnumProperty(
        name="Forward",
        items=AXIS_ITEMS,
        default=DEFAULT_FORWARD_AXIS,
        options={"HIDDEN"},
    )
    axis_up: EnumProperty(
        name="Up",
        items=AXIS_ITEMS,
        default=DEFAULT_UP_AXIS,
        options={"HIDDEN"},
    )

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        """내부 TIMER 작업도 활성 COLMAP Align 세션 Window에서만 시작합니다."""
        return bool(
            _is_import_session_context(context)
            and not RUNTIME.import_in_progress
            and not RUNTIME.modal_active
            and not is_marker_preview_active()
            and _import_preview_block_reason() is None
            and restore_input_block_reason() is None
        )

    def _finish(
        self,
        context: bpy.types.Context,
        result: set[str],
        message: str,
        *,
        level: set[str],
    ) -> set[str]:
        """TIMER·진행 UI를 정리하고 최종 결과를 보고한 뒤 작업 참조를 해제합니다."""
        job = RUNTIME.import_job
        _remove_import_timer(context.window_manager)
        _end_progress(context.window_manager)
        if result == {"FINISHED"}:
            if job is not None:
                context.scene.colmap_align_settings.import_dataset_root = job.dataset_root
            notify_project_state_changed("모델 가져오기 완료")
            imported_model_id = (
                str(job.staged_model.model_id)
                if job is not None and job.staged_model is not None
                else ""
            )
            fit_after_successful_import(context, imported_model_id)
            _show_import_popup(
                context,
                title="COLMAP Import Complete",
                icon="CHECKMARK",
                lines=_completion_popup_lines(job),
            )
        elif job is not None and job.stage == IMPORT_STAGE_FAILED:
            _show_import_popup(
                context,
                title="COLMAP Import Failed",
                icon="ERROR",
                lines=(message,),
            )
        request_redraw()
        _report_status(self, level, message)
        RUNTIME.clear_import_job()
        return result

    def _start(self, context: bpy.types.Context) -> set[str]:
        """COLMAP·연결 OBJ 작업을 만들고 이벤트 TIMER와 진행 막대를 등록합니다."""
        preview_reason = _import_preview_block_reason()
        if preview_reason is not None:
            _report_status(self, {"WARNING"}, preview_reason)
            return {"CANCELLED"}
        try:
            job = begin_import_job(
                RUNTIME,
                self.source_path,
                input_format=self.input_format,
                display_point_limit=self.display_point_limit,
                axis_preset=self.axis_preset,
                axis_forward=self.axis_forward,
                axis_up=self.axis_up,
                import_companion_obj=self.import_companion_obj,
                companion_obj_path=self.companion_obj_path,
            )
        except ImportJobError as error:
            _report_status(self, {"ERROR"}, str(error))
            return {"CANCELLED"}
        context.window_manager.progress_begin(0, 100)
        timer = context.window_manager.event_timer_add(
            _TIMER_INTERVAL_SECONDS,
            window=context.window,
        )
        RUNTIME.import_timer = timer
        context.window_manager.modal_handler_add(self)
        _LOGGER.info(
            "event=import_operator_started source=%s display_point_limit=%s "
            "axis_preset=%s forward=%s up=%s",
            job.source_path,
            job.display_point_limit,
            job.display_transform.preset_id,
            job.display_transform.forward_axis,
            job.display_transform.up_axis,
        )
        return {"RUNNING_MODAL"}

    def invoke(self, context: bpy.types.Context, _event: bpy.types.Event):
        """파일 선택 오퍼레이터가 전달한 경로로 TIMER 작업을 시작합니다."""
        return self._start(context)

    def execute(self, context: bpy.types.Context):
        """스크립트 호출도 같은 TIMER 작업 경계로 시작하도록 처리합니다."""
        return self._start(context)

    def modal(self, context: bpy.types.Context, event: bpy.types.Event):
        """TIMER마다 한 단계만 진행하고 Esc는 취소 요청으로 처리합니다."""
        job = RUNTIME.import_job
        if job is None:
            return {"CANCELLED"}
        if event.type == "ESC":
            cancel_import_job(RUNTIME)
            return {"RUNNING_MODAL"}
        if event.type != "TIMER":
            return {"PASS_THROUGH"}
        step_result = job.step(RUNTIME)
        context.window_manager.progress_update(int(round(job.progress * 100.0)))
        if step_result == "RUNNING":
            request_redraw()
            return {"RUNNING_MODAL"}
        if step_result == "FINISHED" and job.stage == IMPORT_STAGE_COMPLETED:
            return self._finish(
                context,
                {"FINISHED"},
                job.result_message,
                level={"INFO"},
            )
        message = job.error_message or job.result_message or "COLMAP 모델 가져오기를 취소했습니다."
        return self._finish(context, {"CANCELLED"}, message, level={"WARNING"})


class COLMAPALIGN_OT_cancel_import_model(bpy.types.Operator):
    """현재 실행 중인 COLMAP 가져오기 작업의 다음 단계를 취소합니다."""

    bl_idname = "colmap_align.cancel_import_model"
    bl_label = "가져오기 취소"
    bl_description = "현재 COLMAP 모델 가져오기를 취소하고 기존 데이터 레이어를 유지합니다"
    bl_options = {"REGISTER"}

    @classmethod
    def poll(cls, _context: bpy.types.Context) -> bool:
        """일반 가져오기만 취소하고 자동 복원은 전용 취소 동작으로 분리합니다."""
        return RUNTIME.import_in_progress and restore_input_block_reason() is None

    def execute(self, _context: bpy.types.Context):
        """자동 복원과 섞이지 않는 일반 ImportJob에만 취소 플래그를 요청합니다."""
        restore_reason = restore_input_block_reason()
        if restore_reason is not None:
            self.report({"WARNING"}, restore_reason)
            return {"CANCELLED"}
        if not cancel_import_job(RUNTIME):
            self.report({"WARNING"}, "취소할 COLMAP 가져오기 작업이 없습니다.")
            return {"CANCELLED"}
        self.report({"INFO"}, "COLMAP 모델 가져오기 취소를 요청했습니다.")
        return {"FINISHED"}


CLASSES = (
    COLMAPALIGN_OT_import_model,
    COLMAPALIGN_OT_import_model_job,
    COLMAPALIGN_OT_cancel_import_model,
)
