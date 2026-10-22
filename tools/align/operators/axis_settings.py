"""데이터셋 하나의 점군·카메라와 메시 축을 미리보기 팝업에서 편집합니다."""

import bpy
import numpy as np
from bpy.props import BoolProperty, EnumProperty, FloatVectorProperty, StringProperty

from ..axis_settings import (
    axis_edit_block_reason,
    begin_axis_edit,
    cancel_axis_edit,
    commit_axis_edit,
    rc_axis_pair_for_model,
    update_axis_edit,
    center_axis_gizmo_on_bounds,
    move_axis_geometry_gizmo_to_origin,
)
from ..axis_edit_transform import prepare_dataset_axis_edit, compose_dataset_geometry_transform
from ..display_axes import (
    AXIS_ITEMS,
    AXIS_PRESET_ITEMS,
    CUSTOM_AXIS_PRESET,
    DEFAULT_AXIS_PRESET,
    axes_for_preset,
    create_model_display_transform,
)
from ..project_persistence.runtime import notify_project_state_changed
from ..session import is_session_active_for_window
from ..state import RUNTIME
from ..viewport.renderer import request_redraw


_OPEN_DIALOGS: dict[str, object] = {}
# RNA 갱신 콜백의 속성 래퍼도 같은 편집 토큰으로 정밀도·배율 기준에 접근합니다.
_AXIS_INPUTS: dict[str, dict] = {}
_PREVIOUS_SCALES: dict[str, dict] = {}


def clear_axis_edit_dialogs() -> None:
    """세션 종료·파일 로드·등록 해제 뒤 팝업과 편집 기준 및 감시 타이머를 정리합니다."""
    _OPEN_DIALOGS.clear()
    _AXIS_INPUTS.clear()
    _PREVIOUS_SCALES.clear()
    if bpy.app.timers.is_registered(_watch_axis_edit_dialogs):
        bpy.app.timers.unregister(_watch_axis_edit_dialogs)


def _release_dialog(token: str) -> None:
    """끝난 팝업과 편집 기준을 해제하고 마지막 팝업이면 수명 감시도 중지합니다."""
    _OPEN_DIALOGS.pop(token, None)
    _AXIS_INPUTS.pop(token, None)
    _PREVIOUS_SCALES.pop(token, None)
    if not _OPEN_DIALOGS:
        clear_axis_edit_dialogs()


def _watch_axis_edit_dialogs():
    """대상 창 닫힘·작업공간 이탈·모델 삭제를 감지하면 임시 편집과 기준을 정리합니다."""
    state = RUNTIME.axis_settings
    if not _OPEN_DIALOGS or not state.is_active or state.token not in _OPEN_DIALOGS:
        _OPEN_DIALOGS.clear()
        _AXIS_INPUTS.clear()
        _PREVIOUS_SCALES.clear()
        return None
    manager = getattr(bpy.context, "window_manager", None)
    windows = getattr(manager, "windows", ())
    owner = next((window for window in windows if window.as_pointer() == state.window_pointer), None)
    workspace = getattr(owner, "workspace", None)
    invalid = (
        owner is None
        or workspace is None
        or workspace.as_pointer() != state.workspace_pointer
        or state.model_id not in RUNTIME.model_repository.models
        or state.model_id not in RUNTIME.model_catalog.models
    )
    if invalid:
        cancel_axis_edit(RUNTIME, state.token)
        _OPEN_DIALOGS.clear()
        _AXIS_INPUTS.clear()
        _PREVIOUS_SCALES.clear()
        request_redraw()
        return None
    return 0.1


def _ensure_dialog_watch() -> None:
    """네이티브 대화상자가 끝날 때까지 하나의 수명 감시 타이머만 유지합니다."""
    if not bpy.app.timers.is_registered(_watch_axis_edit_dialogs):
        bpy.app.timers.register(_watch_axis_edit_dialogs, first_interval=0.1)


def _common_values(operator) -> tuple:
    """데이터셋 공통 이동·회전·배율 입력을 비교 가능한 값으로 반환합니다."""
    return tuple(tuple(getattr(operator, f"dataset_{name}")) for name in ("rotation", "translation", "scale"))


def _component_values(operator, component: str) -> tuple:
    """각 형상의 원본 축 프리셋과 전방·상향축만 반환합니다."""
    return tuple(getattr(operator, f"{component}_{name}") for name in ("preset", "forward", "up"))


def _set_dialog_values(operator, main, companion) -> None:
    """기존 두 형상 배치를 유지한 개별 원본 축과 공통 중심 변환을 입력에 연결합니다."""
    basis = prepare_dataset_axis_edit(RUNTIME, operator.model_id, main, companion)
    inputs = {"basis": basis}
    for component, transform in (("main", main), ("companion", companion)):
        if transform is None:
            continue
        for name, value in (("preset", transform.preset_id), ("forward", transform.forward_axis), ("up", transform.up_axis)):
            setattr(operator, f"{component}_{name}", value)
        inputs[component] = (transform, _component_values(operator, component))
    operator.dataset_rotation = basis.common.rotation_xyz_degrees
    operator.dataset_translation = basis.common.translation_xyz
    operator.dataset_scale = basis.common.scale_xyz
    inputs["common_values"] = _common_values(operator)
    _AXIS_INPUTS[operator.edit_token] = inputs
    _PREVIOUS_SCALES[operator.edit_token] = tuple(operator.dataset_scale)


def _common_transform(operator):
    """수정하지 않은 RNA 성분은 원래 float64 공통 변환값을 사용합니다."""
    inputs = _AXIS_INPUTS.get(operator.edit_token)
    if inputs is None:
        raise ValueError("축 설정 편집 기준이 만료되었습니다. 팝업을 다시 열어 주세요.")
    centered = inputs["basis"].common
    initial, values = inputs["common_values"], _common_values(operator)
    if values == initial:
        return centered
    exact = (centered.rotation_xyz_degrees, centered.translation_xyz, centered.scale_xyz)
    vectors = [tuple(full if value == before else value for value, before, full in zip(current, prior, precise))
               for current, prior, precise in zip(values, initial, exact)]
    return create_model_display_transform(
        "BLENDER", "Y", "Z", rotation_xyz_degrees=vectors[0],
        translation_xyz=vectors[1], scale_xyz=vectors[2], pivot_source=centered.pivot_source,
    )


def _component_transform(operator, component: str, common=None):
    """원본 축은 개별로 조절하고 공통 변환은 두 형상에 같은 변화량으로 적용합니다."""
    inputs = _AXIS_INPUTS.get(operator.edit_token)
    if inputs is None:
        raise ValueError("축 설정 편집 기준이 만료되었습니다. 팝업을 다시 열어 주세요.")
    if component not in inputs:
        return None
    original, initial = inputs[component]
    axes = _component_values(operator, component)
    if axes == initial and _common_values(operator) == inputs["common_values"]:
        return original
    common = common if common is not None else _common_transform(operator)
    return compose_dataset_geometry_transform(common, *axes, getattr(inputs["basis"], f"{component}_basis"))


def _candidate_pair(operator):
    """같은 공통 변환으로 두 형상의 유효한 미리보기 후보를 생성합니다."""
    common = _common_transform(operator)
    return _component_transform(operator, "main", common), _component_transform(operator, "companion", common)


def _publish_preview(operator) -> bool:
    """개별 축과 공통 변환의 두 후보를 원자적으로 전달하고 오류는 팝업에 유지합니다."""
    if getattr(operator, "suspend_updates", False) or not operator.edit_token:
        return False
    try:
        main, companion = _candidate_pair(operator)
        update_axis_edit(RUNTIME, operator.edit_token, main_transform=main,
                         companion_transform=companion, preview_enabled=operator.preview_enabled)
    except (ValueError, RuntimeError) as error:
        operator.error_message = str(error)
        request_redraw()
        return False
    operator.error_message = ""
    request_redraw()
    return True


def _update_dataset_scale(operator, _context) -> None:
    """잠금 상태에서는 편집한 축의 변화 비율을 나머지 축과 두 형상에 함께 적용합니다."""
    if getattr(operator, "suspend_updates", False) or not operator.edit_token:
        return
    current = np.asarray(operator.dataset_scale, dtype=np.float64)
    before = np.asarray(_PREVIOUS_SCALES.get(operator.edit_token, current), dtype=np.float64)
    changed = np.flatnonzero(current != before)
    if getattr(operator, "dataset_scale_locked", False) and len(changed):
        linked = before * (current[int(changed[0])] / before[int(changed[0])])
        # 한 축이 입력 한계에 닿으면 세 축의 비율을 함께 유지합니다.
        linked *= min(1.0, 1.0e6 / float(linked.max()))
        linked *= max(1.0, 1.0e-6 / float(linked.min()))
        operator.suspend_updates = True
        try:
            operator.dataset_scale = tuple(linked)
        finally:
            operator.suspend_updates = False
    _PREVIOUS_SCALES[operator.edit_token] = tuple(operator.dataset_scale)
    _publish_preview(operator)


def _update_preset(operator, component: str) -> None:
    """선택한 형상의 원본 축만 프리셋에 맞게 변경합니다."""
    if getattr(operator, "suspend_updates", False):
        return
    operator.suspend_updates = True
    try:
        axes = axes_for_preset(getattr(operator, f"{component}_preset"))
        if axes is not None:
            setattr(operator, f"{component}_forward", axes[0])
            setattr(operator, f"{component}_up", axes[1])
    finally:
        operator.suspend_updates = False
    _publish_preview(operator)


def _update_axes(operator, component: str) -> None:
    """개별 원본 축을 직접 바꾸면 사용자 지정으로 전환하고 중복 축을 검증합니다."""
    if getattr(operator, "suspend_updates", False):
        return
    fixed = axes_for_preset(getattr(operator, f"{component}_preset"))
    actual = (getattr(operator, f"{component}_forward"), getattr(operator, f"{component}_up"))
    if fixed is not None and fixed != actual:
        operator.suspend_updates = True
        try:
            setattr(operator, f"{component}_preset", CUSTOM_AXIS_PRESET)
        finally:
            operator.suspend_updates = False
    _publish_preview(operator)


def _update_main_preset(operator, _context) -> None:
    """COLMAP 또는 독립 OBJ의 원본 축 프리셋 변경을 반영합니다."""
    _update_preset(operator, "main")


def _update_companion_preset(operator, _context) -> None:
    """연결 OBJ의 원본 축 프리셋 변경만 반영합니다."""
    _update_preset(operator, "companion")


def _update_main_axes(operator, _context) -> None:
    """주 형상의 전방·상향축 변경만 반영합니다."""
    _update_axes(operator, "main")


def _update_companion_axes(operator, _context) -> None:
    """연결 OBJ의 전방·상향축 변경만 반영합니다."""
    _update_axes(operator, "companion")


def _update_preview(operator, _context) -> None:
    """공통 이동·회전 또는 미리보기 선택 변경을 두 형상에 반영합니다."""
    _publish_preview(operator)


def _draw_component(layout, operator, component: str, title: str) -> None:
    """형상별 설정에는 원본 축 프리셋과 전방·상향축만 표시합니다."""
    box = layout.box()
    header = box.row(align=True)
    header.label(text=title, icon="MESH_DATA" if component == "companion" or operator.is_standalone else "POINTCLOUD_DATA")
    reset = header.operator("colmap_align.reset_geometry_axes", text="RC 축 초기화", icon="LOOP_BACK")
    reset.edit_token, reset.component = operator.edit_token, component.upper()
    box.prop(operator, f"{component}_preset", text="원본 축 프리셋")
    axes = box.row(align=True)
    axes.prop(operator, f"{component}_forward", text="Forward")
    axes.prop(operator, f"{component}_up", text="Up")


def _draw_dataset_transform(layout, operator) -> None:
    """기즈모 두 버튼의 즉시 실행과 적용을 기다리는 공통 변환 입력을 구분합니다."""
    box = layout.box()
    header = box.row(align=True)
    header.label(text="데이터셋 공통 변환")
    reset = header.operator("colmap_align.reset_dataset_axis_transform", text="변환 초기화", icon="LOOP_BACK")
    reset.edit_token = operator.edit_token
    pivot = box.column(align=True)
    pivot.label(text="즉시 실행 · 팝업 취소 후에도 유지", icon="INFO")
    center = pivot.operator("colmap_align.center_axis_gizmo_on_bounds", text="기즈모를 바운딩 박스 중심으로", icon="PIVOT_BOUNDBOX")
    center.edit_token = operator.edit_token
    move = pivot.operator("colmap_align.move_axis_gizmo_to_origin", text="물체를 이동해 기즈모를 원점으로", icon="PIVOT_CURSOR")
    move.edit_token = operator.edit_token
    location = box.row(align=True)
    location.label(text="이동")
    origin = location.operator("colmap_align.center_geometry_at_origin", text="중심을 원점으로", icon="PIVOT_CURSOR")
    origin.edit_token, origin.component = operator.edit_token, "BOTH"
    for name, label in (("translation", None), ("rotation", "회전 · 도(°) · 데이터셋 바운딩 박스 중심"), ("scale", "스케일")):
        if label is not None:
            header = box.row(align=True)
            header.label(text=label)
            if name == "scale":
                locked = operator.dataset_scale_locked
                header.prop(operator, "dataset_scale_locked", text="", icon="LOCKED" if locked else "UNLOCKED", toggle=True)
        row = box.row(align=True)
        for index, axis in enumerate(("X", "Y", "Z")):
            row.prop(operator, f"dataset_{name}", index=index, text=axis)
    if not operator.is_standalone and max(operator.dataset_scale) - min(operator.dataset_scale) > 1.0e-6:
        box.label(text="COLMAP 내보내기는 세 축의 동일 배율이 필요합니다.", icon="INFO")


class COLMAPALIGN_OT_edit_geometry_axes(bpy.types.Operator):
    """개별 원본 축과 데이터셋 공통 변환을 두 형상이 보이는 팝업에서 편집합니다."""
    bl_idname = "colmap_align.edit_geometry_axes"
    bl_label = "데이터셋 축 설정"
    bl_description = "COLMAP과 OBJ의 원본 축을 각각 보정하고 이동·회전·스케일은 함께 조절합니다"
    bl_options = {"INTERNAL"}

    suspend_updates: BoolProperty(default=False, options={"HIDDEN", "SKIP_SAVE"})
    model_id: StringProperty(options={"HIDDEN", "SKIP_SAVE"})
    edit_token: StringProperty(options={"HIDDEN", "SKIP_SAVE"})
    target_name: StringProperty(options={"HIDDEN", "SKIP_SAVE"})
    is_standalone: BoolProperty(options={"HIDDEN", "SKIP_SAVE"})
    has_companion: BoolProperty(options={"HIDDEN", "SKIP_SAVE"})
    error_message: StringProperty(options={"HIDDEN", "SKIP_SAVE"})
    preview_enabled: BoolProperty(name="미리보기", default=True, update=_update_preview, options={"SKIP_SAVE"})
    main_preset: EnumProperty(name="원본 축 프리셋", items=AXIS_PRESET_ITEMS, default=DEFAULT_AXIS_PRESET, update=_update_main_preset, options={"SKIP_SAVE"})
    main_forward: EnumProperty(name="Forward", items=AXIS_ITEMS, default="Z", update=_update_main_axes, options={"SKIP_SAVE"})
    main_up: EnumProperty(name="Up", items=AXIS_ITEMS, default="-Y", update=_update_main_axes, options={"SKIP_SAVE"})
    companion_preset: EnumProperty(name="원본 축 프리셋", items=AXIS_PRESET_ITEMS, default="REALITY_CAPTURE_OBJ", update=_update_companion_preset, options={"SKIP_SAVE"})
    companion_forward: EnumProperty(name="Forward", items=AXIS_ITEMS, default="Y", update=_update_companion_axes, options={"SKIP_SAVE"})
    companion_up: EnumProperty(name="Up", items=AXIS_ITEMS, default="Z", update=_update_companion_axes, options={"SKIP_SAVE"})
    dataset_rotation: FloatVectorProperty(name="회전(°)", description="전체 바운딩 박스 중심을 기준으로 두 형상을 함께 회전합니다", size=3, default=(0, 0, 0), precision=2, step=100, update=_update_preview, options={"SKIP_SAVE"})
    dataset_translation: FloatVectorProperty(name="이동", description="정렬 전 표시 축을 기준으로 두 형상을 함께 이동합니다", size=3, default=(0, 0, 0), precision=4, update=_update_preview, options={"SKIP_SAVE"})
    dataset_scale: FloatVectorProperty(name="스케일", description="전체 바운딩 박스 중심에서 두 형상의 배율을 함께 조절합니다", size=3, default=(1, 1, 1), min=1.0e-6, max=1.0e6, precision=4, update=_update_dataset_scale, options={"SKIP_SAVE"})
    dataset_scale_locked: BoolProperty(name="비율 고정", description="현재 X:Y:Z 비율을 유지하면서 세 축을 함께 조절합니다", default=False, options={"SKIP_SAVE"})

    @classmethod
    def description(cls, _context, properties):
        """축 설정 아이콘의 툴팁에 대상 데이터셋과 편집 범위를 표시합니다."""
        model_id = getattr(properties, "model_id", "")
        record = RUNTIME.model_catalog.models.get(model_id)
        name = record.display_name if record is not None else model_id
        return f"{name}: 원본 축은 개별 보정하고 이동·회전·스케일은 함께 조절합니다"

    @classmethod
    def poll(cls, context):
        """활성 Align 창에서 축 팝업의 적용과 새 편집을 허용합니다."""
        return bool(is_session_active_for_window(context.window))

    def invoke(self, context, _event):
        """현재 배치를 보존하는 입력을 준비하고 대상의 존재하는 두 형상을 함께 표시합니다."""
        RUNTIME.clear_data_layer_click()
        reason = axis_edit_block_reason(RUNTIME, self.model_id)
        if reason is not None:
            self.report({"WARNING"}, reason)
            return {"CANCELLED"}
        self.suspend_updates = True
        try:
            self.edit_token = begin_axis_edit(
                RUNTIME, self.model_id, window_pointer=context.window.as_pointer(),
                workspace_pointer=context.workspace.as_pointer(),
            )
            record = RUNTIME.model_catalog.models[self.model_id]
            self.target_name = record.display_name
            self.is_standalone = record.geometry_kind == "TRIANGLE_MESH"
            state = RUNTIME.axis_settings
            self.has_companion = state.companion_transform is not None
            _set_dialog_values(self, state.main_transform, state.companion_transform)
            self.preview_enabled = True
            self.error_message = ""
            _OPEN_DIALOGS[self.edit_token] = self
            _ensure_dialog_watch()
        except (ValueError, RuntimeError) as error:
            if self.edit_token:
                cancel_axis_edit(RUNTIME, self.edit_token)
                _release_dialog(self.edit_token)
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        finally:
            self.suspend_updates = False
        if not _publish_preview(self):
            self.report({"ERROR"}, self.error_message)
            self.cancel(context)
            return {"CANCELLED"}
        try:
            return context.window_manager.invoke_props_dialog(self, width=460, confirm_text="적용")
        except Exception:
            self.cancel(context)
            raise

    def check(self, _context):
        """속성 변경 뒤 축 검증과 공통 변환 안내를 다시 그립니다."""
        return True

    def draw(self, _context):
        """존재하는 형상의 원본 축과 한 벌의 공통 변환을 구분해서 표시합니다."""
        layout = self.layout
        layout.use_property_split = False
        layout.use_property_decorate = False
        layout.label(text=self.target_name, icon="OUTLINER_DATA_MESH" if self.is_standalone else "POINTCLOUD_DATA")
        _draw_component(layout, self, "main", "메시 (OBJ)" if self.is_standalone else "COLMAP (점군·카메라)")
        if self.has_companion:
            _draw_component(layout, self, "companion", "연결 메시 (OBJ)")
        _draw_dataset_transform(layout, self)
        layout.prop(self, "preview_enabled")
        if self.error_message:
            error = layout.column(align=True)
            error.alert = True
            error.label(text=self.error_message, icon="ERROR")

    def execute(self, context):
        """개별 축과 두 형상의 공통 변환을 하나의 편집으로 확정합니다."""
        if not _publish_preview(self):
            self.report({"ERROR"}, self.error_message or "축 설정 팝업을 다시 열어 주십시오.")
            self.cancel(context)
            return {"CANCELLED"}
        try:
            changed = commit_axis_edit(RUNTIME, self.edit_token)
        except (ValueError, RuntimeError) as error:
            self.report({"ERROR"}, str(error))
            self.cancel(context)
            return {"CANCELLED"}
        _release_dialog(self.edit_token)
        self.edit_token = ""
        if changed:
            notify_project_state_changed("데이터셋 원본 축·공통 변환 적용")
        request_redraw()
        return {"FINISHED"}

    def cancel(self, _context):
        """임시 축과 동시 표시를 해제하고 기존 확정값과 표시 선택을 유지합니다."""
        if self.edit_token:
            cancel_axis_edit(RUNTIME, self.edit_token)
            _release_dialog(self.edit_token)
            self.edit_token = ""
        request_redraw()


class COLMAPALIGN_OT_reset_geometry_axes(bpy.types.Operator):
    """선택한 형상의 원본 축만 RC 기본값으로 되돌립니다."""
    bl_idname = "colmap_align.reset_geometry_axes"
    bl_label = "RC 축 초기화"
    bl_description = "원본 축만 RC 기본값으로 되돌리고 데이터셋 공통 변환은 유지합니다"
    bl_options = {"INTERNAL"}
    edit_token: StringProperty(options={"HIDDEN", "SKIP_SAVE"})
    component: EnumProperty(items=(("BOTH", "두 항목", "두 형상의 원본 축 초기화"), ("MAIN", "주 형상", "주 형상 원본 축 초기화"), ("COMPANION", "연결 메시", "연결 OBJ 원본 축 초기화")), options={"HIDDEN", "SKIP_SAVE"})

    def execute(self, _context):
        """유효한 팝업에서 해당 형상의 원본 축 입력만 초기화합니다."""
        operator = _OPEN_DIALOGS.get(self.edit_token)
        if operator is None or not RUNTIME.axis_settings.is_active or RUNTIME.axis_settings.token != self.edit_token:
            self.report({"WARNING"}, "초기화할 축 설정 팝업이 열려 있지 않습니다.")
            return {"CANCELLED"}
        main, companion = rc_axis_pair_for_model(RUNTIME, operator.model_id)
        operator.suspend_updates = True
        try:
            for component, transform in (("main", main), ("companion", companion)):
                if transform is not None and self.component in {"BOTH", component.upper()}:
                    for name, value in (("preset", transform.preset_id), ("forward", transform.forward_axis), ("up", transform.up_axis)):
                        setattr(operator, f"{component}_{name}", value)
        finally:
            operator.suspend_updates = False
        return {"FINISHED"} if _publish_preview(operator) else {"CANCELLED"}


class COLMAPALIGN_OT_reset_dataset_axis_transform(bpy.types.Operator):
    """원본 축과 기존 개별 보정은 유지하고 데이터셋 공통 변환을 초기화합니다."""
    bl_idname = "colmap_align.reset_dataset_axis_transform"
    bl_label = "데이터셋 변환 초기화"
    bl_description = "공통 이동·회전을 0, 스케일을 1로 초기화합니다"
    bl_options = {"INTERNAL"}
    edit_token: StringProperty(options={"HIDDEN", "SKIP_SAVE"})

    def execute(self, _context):
        """현재 팝업의 공통 이동·회전·배율 입력만 기본값으로 설정합니다."""
        operator = _OPEN_DIALOGS.get(self.edit_token)
        if operator is None or not RUNTIME.axis_settings.is_active or RUNTIME.axis_settings.token != self.edit_token:
            self.report({"WARNING"}, "축 설정 팝업이 열려 있지 않습니다.")
            return {"CANCELLED"}
        operator.suspend_updates = True
        try:
            operator.dataset_rotation = operator.dataset_translation = (0.0, 0.0, 0.0)
            operator.dataset_scale = (1.0, 1.0, 1.0)
            _PREVIOUS_SCALES[self.edit_token] = tuple(operator.dataset_scale)
        finally:
            operator.suspend_updates = False
        return {"FINISHED"} if _publish_preview(operator) else {"CANCELLED"}


class COLMAPALIGN_OT_center_geometry_at_origin(bpy.types.Operator):
    """존재하는 두 형상의 전체 경계 중심을 월드 원점으로 함께 옮깁니다."""
    bl_idname = "colmap_align.center_geometry_at_origin"
    bl_label = "중심을 원점으로"
    bl_description = "데이터셋 전체 바운딩 박스 중심을 월드 원점으로 이동합니다"
    bl_options = {"INTERNAL"}
    edit_token: StringProperty(options={"HIDDEN", "SKIP_SAVE"})
    # 구형 호출 식별자는 유지하되 원점 이동은 항상 데이터셋 전체에 적용합니다.
    component: EnumProperty(items=(("BOTH", "데이터셋", "전체 중심 이동"), ("MAIN", "주 형상", "데이터셋 전체 중심 이동"), ("COMPANION", "연결 메시", "데이터셋 전체 중심 이동")), options={"HIDDEN", "SKIP_SAVE"})

    def execute(self, _context):
        """정렬까지 반영한 전체 중심을 원점으로 옮기고 공통 입력을 갱신합니다."""
        operator = _OPEN_DIALOGS.get(self.edit_token)
        if operator is None or not RUNTIME.axis_settings.is_active or RUNTIME.axis_settings.token != self.edit_token:
            self.report({"WARNING"}, "축 설정 팝업이 열려 있지 않습니다.")
            return {"CANCELLED"}
        try:
            main, companion = _candidate_pair(operator)
            basis = prepare_dataset_axis_edit(RUNTIME, operator.model_id, main, companion)
            common = basis.common
            alignment = RUNTIME.model_repository.alignment_transform_for_model_id(operator.model_id).matrix_float64
            target = np.linalg.solve(alignment, np.array((0.0, 0.0, 0.0, 1.0)))[:3]
            moved = create_model_display_transform(
                "BLENDER", "Y", "Z", rotation_xyz_degrees=common.rotation_xyz_degrees,
                scale_xyz=common.scale_xyz, pivot_source=common.pivot_source,
                translation_xyz=target - common.pivot_source,
            )
            main = compose_dataset_geometry_transform(moved, *_component_values(operator, "main"), basis.main_basis)
            companion = compose_dataset_geometry_transform(moved, *_component_values(operator, "companion"), basis.companion_basis) if companion is not None else None
            operator.suspend_updates = True
            _set_dialog_values(operator, main, companion)
        except (ValueError, RuntimeError) as error:
            operator.error_message = str(error)
            return {"CANCELLED"}
        finally:
            operator.suspend_updates = False
        return {"FINISHED"} if _publish_preview(operator) else {"CANCELLED"}


class COLMAPALIGN_OT_center_axis_gizmo_on_bounds(bpy.types.Operator):
    """물체를 움직이지 않고 팝업 대상의 전체 경계 중심을 기즈모 중심으로 지정합니다."""
    bl_idname = "colmap_align.center_axis_gizmo_on_bounds"
    bl_label = "기즈모를 바운딩 박스 중심으로"
    bl_description = "물체는 유지하고 기즈모만 표시된 전체 경계 중심으로 즉시 옮깁니다. 팝업을 닫은 뒤 실행 취소할 수 있습니다"
    bl_options = {"INTERNAL"}
    edit_token: StringProperty(options={"HIDDEN", "SKIP_SAVE"})

    def execute(self, _context):
        """기즈모 중심만 즉시 확정하고 별도 실행 취소 이력을 남깁니다."""
        operator = _OPEN_DIALOGS.get(self.edit_token)
        if operator is None or not _publish_preview(operator):
            self.report({"WARNING"}, "유효한 축 설정 팝업이 열려 있지 않습니다.")
            return {"CANCELLED"}
        try:
            center_axis_gizmo_on_bounds(RUNTIME, self.edit_token)
        except (ValueError, RuntimeError) as error:
            operator.error_message = str(error)
            return {"CANCELLED"}
        request_redraw()
        return {"FINISHED"}


class COLMAPALIGN_OT_move_axis_gizmo_to_origin(bpy.types.Operator):
    """현재 기즈모를 기준으로 대상 물체를 움직여 월드 원점에 맞춥니다."""
    bl_idname = "colmap_align.move_axis_gizmo_to_origin"
    bl_label = "물체를 이동해 기즈모를 원점으로"
    bl_description = "현재 표시 기즈모가 월드 원점에 오도록 물체를 즉시 이동합니다. 중심 지정과 독립적이며 팝업을 닫은 뒤 실행 취소할 수 있습니다"
    bl_options = {"INTERNAL"}
    edit_token: StringProperty(options={"HIDDEN", "SKIP_SAVE"})

    def execute(self, _context):
        """기즈모 기준 이동을 별도 이력으로 확정하고 미적용 입력의 정밀 기준을 갱신합니다."""
        operator = _OPEN_DIALOGS.get(self.edit_token)
        if operator is None or not _publish_preview(operator):
            self.report({"WARNING"}, "유효한 축 설정 팝업이 열려 있지 않습니다.")
            return {"CANCELLED"}
        operator.suspend_updates = True
        try:
            changed = move_axis_geometry_gizmo_to_origin(RUNTIME, self.edit_token)
            state = RUNTIME.axis_settings
            _set_dialog_values(operator, state.main_transform, state.companion_transform)
        except (ValueError, RuntimeError) as error:
            operator.error_message = str(error)
            return {"CANCELLED"}
        finally:
            operator.suspend_updates = False
        if changed:
            notify_project_state_changed("기즈모 기준 물체 원점 이동")
        return {"FINISHED"} if _publish_preview(operator) else {"CANCELLED"}


CLASSES = (
    COLMAPALIGN_OT_edit_geometry_axes, COLMAPALIGN_OT_reset_geometry_axes,
    COLMAPALIGN_OT_reset_dataset_axis_transform, COLMAPALIGN_OT_center_geometry_at_origin,
    COLMAPALIGN_OT_center_axis_gizmo_on_bounds, COLMAPALIGN_OT_move_axis_gizmo_to_origin,
)
