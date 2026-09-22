"""데이터셋 하나의 점군·카메라와 메시 축을 미리보기 팝업에서 편집합니다."""

import bpy
import numpy as np
from bpy.props import BoolProperty, EnumProperty, FloatVectorProperty, StringProperty

from ..axis_settings import (
    axis_edit_block_reason,
    begin_axis_edit,
    centered_axis_transform,
    axis_transform_at_world_origin,
    cancel_axis_edit,
    commit_axis_edit,
    rc_axis_pair_for_model,
    update_axis_edit,
)
from ..display_axes import (
    AXIS_ITEMS,
    AXIS_PRESET_ITEMS,
    CUSTOM_AXIS_PRESET,
    DEFAULT_AXIS_PRESET,
    RC_OBJ_AXIS_PRESET,
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


def _set_component_values(operator, component: str, transform) -> None:
    """현재 화면을 유지하는 중심 기준 편집값과 반올림 전 원본값을 함께 보관합니다."""
    centered = centered_axis_transform(RUNTIME, operator.model_id, transform, companion=component == "companion")
    setattr(operator, f"{component}_preset", transform.preset_id)
    setattr(operator, f"{component}_forward", transform.forward_axis)
    setattr(operator, f"{component}_up", transform.up_axis)
    setattr(operator, f"{component}_rotation", transform.rotation_xyz_degrees)
    setattr(operator, f"{component}_translation", centered.translation_xyz)
    setattr(operator, f"{component}_scale", centered.scale_xyz)
    inputs = _AXIS_INPUTS.setdefault(operator.edit_token, {})
    inputs[component] = (transform, centered, _component_values(operator, component))
    previous = _PREVIOUS_SCALES.setdefault(operator.edit_token, {})
    previous[component] = tuple(getattr(operator, f"{component}_scale"))


def _component_values(operator, component: str) -> tuple:
    """팝업의 축 식별자와 이동·회전·배율 입력을 비교 가능한 값으로 반환합니다."""
    return tuple(getattr(operator, f"{component}_{name}") for name in ("preset", "forward", "up")) + tuple(
        tuple(getattr(operator, f"{component}_{name}")) for name in ("rotation", "translation", "scale")
    )


def _component_transform(operator, component: str):
    """실제로 수정한 입력만 반영하여 팝업을 여는 것만으로 정밀도나 화면이 바뀌지 않게 합니다."""
    values = _component_values(operator, component)
    inputs = _AXIS_INPUTS.get(operator.edit_token, {})
    if component not in inputs:
        raise ValueError("축 설정 편집 기준이 만료되었습니다. 팝업을 다시 열어 주세요.")
    original, centered, initial = inputs[component]
    if values == initial:
        return original
    exact_vectors = (centered.rotation_xyz_degrees, centered.translation_xyz, centered.scale_xyz)
    vectors = [tuple(exact if value == before else value for value, before, exact in zip(current, prior, full))
               for current, prior, full in zip(values[3:], initial[3:], exact_vectors)]
    return create_model_display_transform(
        *values[:3], rotation_xyz_degrees=vectors[0], translation_xyz=vectors[1],
        scale_xyz=vectors[2], pivot_source=centered.pivot_source,
    )


def _update_scale(operator, component: str) -> None:
    """자물쇠가 잠겨 있으면 편집 축의 변화 비율을 나머지 두 축에도 적용합니다."""
    if getattr(operator, "suspend_updates", False) or not operator.edit_token:
        return
    current = np.asarray(getattr(operator, f"{component}_scale"), dtype=np.float64)
    previous = _PREVIOUS_SCALES.setdefault(operator.edit_token, {})
    before = np.asarray(previous.get(component, current), dtype=np.float64)
    changed = np.flatnonzero(current != before)
    if getattr(operator, f"{component}_scale_locked", False) and len(changed):
        index = int(changed[0])
        linked = before * (current[index] / before[index])
        # 한 축이 입력 한계에 닿으면 전체 배율을 같은 비율로 제한합니다.
        linked *= min(1.0, 1.0e6 / float(linked.max()))
        linked *= max(1.0, 1.0e-6 / float(linked.min()))
        operator.suspend_updates = True
        try:
            setattr(operator, f"{component}_scale", tuple(linked))
        finally:
            operator.suspend_updates = False
    previous[component] = tuple(getattr(operator, f"{component}_scale"))
    _publish_preview(operator)


def _update_main_scale(operator, _context) -> None:
    """주 형상의 독립 또는 비율 고정 배율 편집을 반영합니다."""
    _update_scale(operator, "main")


def _update_companion_scale(operator, _context) -> None:
    """연결 메시의 독립 또는 비율 고정 배율 편집을 반영합니다."""
    _update_scale(operator, "companion")


def _publish_preview(operator) -> bool:
    """유효한 두 항목만 편집 서비스에 전달하고 검증 오류는 팝업에 유지합니다."""
    if getattr(operator, "suspend_updates", False) or not operator.edit_token:
        return False
    try:
        main = _component_transform(operator, "main")
        companion = _component_transform(operator, "companion") if operator.has_companion else None
        update_axis_edit(
            RUNTIME,
            operator.edit_token,
            main_transform=main,
            companion_transform=companion,
            preview_enabled=operator.preview_enabled,
        )
    except (ValueError, RuntimeError) as error:
        operator.error_message = str(error)
        request_redraw()
        return False
    operator.error_message = ""
    request_redraw()
    return True


def _update_preset(operator, component: str) -> None:
    """고정 프리셋 선택 시 해당 축을 함께 갱신한 뒤 미리보기를 반영합니다."""
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
    """고정 프리셋의 축을 직접 바꾸면 사용자 지정으로 전환하고 중복 축을 검증합니다."""
    if getattr(operator, "suspend_updates", False):
        return
    fixed_axes = axes_for_preset(getattr(operator, f"{component}_preset"))
    actual_axes = (getattr(operator, f"{component}_forward"), getattr(operator, f"{component}_up"))
    if fixed_axes is not None and fixed_axes != actual_axes:
        operator.suspend_updates = True
        try:
            setattr(operator, f"{component}_preset", CUSTOM_AXIS_PRESET)
        finally:
            operator.suspend_updates = False
    _publish_preview(operator)


def _update_main_preset(operator, _context) -> None:
    """주 형상의 프리셋 변경을 원본 축과 미리보기에 반영합니다."""
    _update_preset(operator, "main")


def _update_companion_preset(operator, _context) -> None:
    """연결 메시의 프리셋 변경을 원본 축과 미리보기에 반영합니다."""
    _update_preset(operator, "companion")


def _update_main_axes(operator, _context) -> None:
    """주 형상의 직접 축 변경을 사용자 지정 프리셋으로 반영합니다."""
    _update_axes(operator, "main")


def _update_companion_axes(operator, _context) -> None:
    """연결 메시의 직접 축 변경을 사용자 지정 프리셋으로 반영합니다."""
    _update_axes(operator, "companion")


def _update_preview(operator, _context) -> None:
    """이동·회전 또는 미리보기 선택이 바뀌면 검증된 편집값을 다시 전달합니다."""
    _publish_preview(operator)


def _draw_component(layout, operator, component: str, title: str) -> None:
    """축·이동·중심 기준 회전·잠금 배율·월드 원점 이동을 형상별로 표시합니다."""
    box = layout.box()
    header = box.row(align=True)
    header.label(text=title, icon="MESH_DATA" if component == "companion" or operator.is_standalone else "POINTCLOUD_DATA")
    reset = header.operator("colmap_align.reset_geometry_axes", text="RC 초기화", icon="LOOP_BACK")
    reset.edit_token = operator.edit_token
    reset.component = component.upper()
    box.prop(operator, f"{component}_preset", text="원본 축 프리셋")
    axes = box.row(align=True)
    axes.prop(operator, f"{component}_forward", text="Forward")
    axes.prop(operator, f"{component}_up", text="Up")
    location_header = box.row(align=True)
    location_header.label(text="이동")
    origin = location_header.operator("colmap_align.center_geometry_at_origin", text="중심을 원점으로", icon="PIVOT_CURSOR")
    origin.edit_token = operator.edit_token
    origin.component = component.upper()
    translation = box.row(align=True)
    for index, axis in enumerate(("X", "Y", "Z")):
        translation.prop(operator, f"{component}_translation", index=index, text=axis)
    box.label(text="추가 회전 · 도(°) · 바운딩 박스 중심")
    rotation = box.row(align=True)
    for index, axis in enumerate(("X", "Y", "Z")):
        rotation.prop(operator, f"{component}_rotation", index=index, text=axis)
    scale_header = box.row(align=True)
    scale_header.label(text="스케일")
    locked = getattr(operator, f"{component}_scale_locked", False)
    scale_header.prop(operator, f"{component}_scale_locked", text="", icon="LOCKED" if locked else "UNLOCKED", toggle=True)
    scale = box.row(align=True)
    for index, axis in enumerate(("X", "Y", "Z")):
        scale.prop(operator, f"{component}_scale", index=index, text=axis)
    if component == "main" and not operator.is_standalone:
        values = tuple(getattr(operator, "main_scale"))
        if max(values) - min(values) > 1.0e-6:
            box.label(text="COLMAP 내보내기는 세 축의 동일 배율이 필요합니다.", icon="INFO")


class COLMAPALIGN_OT_edit_geometry_axes(bpy.types.Operator):
    """클릭한 데이터셋의 축만 고정된 대상으로 편집하고 적용 또는 취소합니다."""

    bl_idname = "colmap_align.edit_geometry_axes"
    bl_label = "데이터셋 축 설정"
    bl_description = "이 데이터셋의 축과 이동·회전·배율을 개별 설정합니다"
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
    main_rotation: FloatVectorProperty(name="추가 회전(°)", description="원본 축 변환 뒤 적용할 X·Y·Z 회전각을 도 단위로 입력합니다", size=3, default=(0.0, 0.0, 0.0), precision=2, step=100, update=_update_preview, options={"SKIP_SAVE"})
    main_translation: FloatVectorProperty(name="이동", description="정렬 전 표시 축을 기준으로 이동합니다", size=3, default=(0, 0, 0), precision=4, update=_update_preview, options={"SKIP_SAVE"})
    main_scale: FloatVectorProperty(name="스케일", description="바운딩 박스 중심에서 각 축의 배율을 조절합니다", size=3, default=(1, 1, 1), min=1.0e-6, max=1.0e6, precision=4, update=_update_main_scale, options={"SKIP_SAVE"})
    main_scale_locked: BoolProperty(name="비율 고정", description="현재 X:Y:Z 비율을 유지하면서 세 축을 함께 조절합니다", default=False, options={"SKIP_SAVE"})
    companion_preset: EnumProperty(name="원본 축 프리셋", items=AXIS_PRESET_ITEMS, default=RC_OBJ_AXIS_PRESET, update=_update_companion_preset, options={"SKIP_SAVE"})
    companion_forward: EnumProperty(name="Forward", items=AXIS_ITEMS, default="Y", update=_update_companion_axes, options={"SKIP_SAVE"})
    companion_up: EnumProperty(name="Up", items=AXIS_ITEMS, default="Z", update=_update_companion_axes, options={"SKIP_SAVE"})
    companion_rotation: FloatVectorProperty(name="추가 회전(°)", description="원본 축 변환 뒤 적용할 X·Y·Z 회전각을 도 단위로 입력합니다", size=3, default=(0.0, 0.0, 0.0), precision=2, step=100, update=_update_preview, options={"SKIP_SAVE"})
    companion_translation: FloatVectorProperty(name="이동", description="정렬 전 표시 축을 기준으로 이동합니다", size=3, default=(0, 0, 0), precision=4, update=_update_preview, options={"SKIP_SAVE"})
    companion_scale: FloatVectorProperty(name="스케일", description="바운딩 박스 중심에서 각 축의 배율을 조절합니다", size=3, default=(1, 1, 1), min=1.0e-6, max=1.0e6, precision=4, update=_update_companion_scale, options={"SKIP_SAVE"})
    companion_scale_locked: BoolProperty(name="비율 고정", description="현재 X:Y:Z 비율을 유지하면서 세 축을 함께 조절합니다", default=False, options={"SKIP_SAVE"})

    @classmethod
    def description(cls, _context, properties):
        """축 설정 아이콘의 툴팁에 클릭 대상 레이어 이름을 표시합니다."""
        model_id = getattr(properties, "model_id", "")
        record = RUNTIME.model_catalog.models.get(model_id)
        name = record.display_name if record is not None else model_id
        return f"{name}: 원본 축과 이동·회전·배율을 개별 설정합니다"

    @classmethod
    def poll(cls, context):
        """현재 팝업의 적용 재검증을 허용하고 새 편집 충돌은 invoke에서 검사합니다."""
        return bool(is_session_active_for_window(context.window))

    def invoke(self, context, _event):
        """대상 ID와 현재 확정값을 고정한 뒤 취소 가능한 축 설정 팝업을 엽니다."""
        RUNTIME.clear_data_layer_click()
        reason = axis_edit_block_reason(RUNTIME, self.model_id)
        if reason is not None:
            self.report({"WARNING"}, reason)
            return {"CANCELLED"}
        self.suspend_updates = True
        try:
            self.edit_token = begin_axis_edit(
                RUNTIME,
                self.model_id,
                window_pointer=context.window.as_pointer(),
                workspace_pointer=context.workspace.as_pointer(),
            )
            record = RUNTIME.model_catalog.models[self.model_id]
            self.target_name = record.display_name
            self.is_standalone = record.geometry_kind == "TRIANGLE_MESH"
            state = RUNTIME.axis_settings
            self.has_companion = state.companion_transform is not None
            _set_component_values(self, "main", state.main_transform)
            if self.has_companion:
                _set_component_values(self, "companion", state.companion_transform)
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
        """속성 갱신 뒤 사용자 지정 프리셋과 검증 오류를 즉시 다시 그립니다."""
        return True

    def draw(self, _context):
        """고정된 대상 이름과 형상별 축 설정 및 미리보기 상태를 표시합니다."""
        layout = self.layout
        layout.use_property_split = False
        layout.use_property_decorate = False
        layout.label(text=self.target_name, icon="OUTLINER_DATA_MESH" if self.is_standalone else "POINTCLOUD_DATA")
        reset = layout.operator("colmap_align.reset_geometry_axes", text="RC 기본 맞춤", icon="LOOP_BACK")
        reset.edit_token = self.edit_token
        reset.component = "BOTH"
        _draw_component(layout, self, "main", "메시 (OBJ)" if self.is_standalone else "COLMAP (점군·카메라)")
        if self.has_companion:
            _draw_component(layout, self, "companion", "연결 메시 (OBJ)")
        elif not self.is_standalone:
            unavailable = layout.box()
            unavailable.enabled = False
            unavailable.label(text="연결 메시 (OBJ)", icon="MESH_DATA")
            unavailable.label(text="연결된 메시가 없습니다.")
        layout.prop(self, "preview_enabled")
        if self.error_message:
            error = layout.column(align=True)
            error.alert = True
            error.label(text=self.error_message, icon="ERROR")

    def execute(self, context):
        """검증된 두 축을 하나의 편집으로 확정하고 변경이 있으면 저장 상태를 갱신합니다."""
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
            notify_project_state_changed("데이터셋 개별 축 설정 적용")
        request_redraw()
        return {"FINISHED"}

    def cancel(self, _context):
        """취소 버튼·Esc·실패 시 확정값을 보존하고 해당 팝업의 미리보기만 해제합니다."""
        if self.edit_token:
            cancel_axis_edit(RUNTIME, self.edit_token)
            _release_dialog(self.edit_token)
            self.edit_token = ""
        request_redraw()


class COLMAPALIGN_OT_reset_geometry_axes(bpy.types.Operator):
    """열린 팝업의 형상을 RC 기본 축·이동 0·회전 0·배율 1로 초기화합니다."""

    bl_idname = "colmap_align.reset_geometry_axes"
    bl_label = "RC 축 초기화"
    bl_description = "RC 기본 축으로 되돌리고 이동·추가 회전을 0, 배율을 1로 초기화합니다"
    bl_options = {"INTERNAL"}

    edit_token: StringProperty(options={"HIDDEN", "SKIP_SAVE"})
    component: EnumProperty(items=(("BOTH", "두 항목", "두 형상을 RC 기본값으로 맞춥니다"), ("MAIN", "주 형상", "주 형상만 초기화합니다"), ("COMPANION", "연결 메시", "연결 메시만 초기화합니다")), options={"HIDDEN", "SKIP_SAVE"})

    def execute(self, _context):
        """현재 편집 토큰을 검증한 뒤 팝업 값만 초기화하고 미리보기를 갱신합니다."""
        operator = _OPEN_DIALOGS.get(self.edit_token)
        state = RUNTIME.axis_settings
        if operator is None or not state.is_active or state.token != self.edit_token:
            self.report({"WARNING"}, "초기화할 축 설정 팝업이 열려 있지 않습니다.")
            return {"CANCELLED"}
        main, companion = rc_axis_pair_for_model(RUNTIME, state.model_id)
        operator.suspend_updates = True
        try:
            if self.component in {"BOTH", "MAIN"}:
                _set_component_values(operator, "main", main)
            if self.component in {"BOTH", "COMPANION"} and companion is not None:
                _set_component_values(operator, "companion", companion)
        finally:
            operator.suspend_updates = False
        return {"FINISHED"} if _publish_preview(operator) else {"CANCELLED"}


class COLMAPALIGN_OT_center_geometry_at_origin(bpy.types.Operator):
    """열린 팝업에서 선택한 형상의 바운딩 박스 중심을 월드 원점으로 옮깁니다."""

    bl_idname = "colmap_align.center_geometry_at_origin"
    bl_label = "중심을 원점으로"
    bl_description = "데이터의 바운딩 박스 중심을 월드 원점 (0, 0, 0)으로 이동합니다"
    bl_options = {"INTERNAL"}

    edit_token: StringProperty(options={"HIDDEN", "SKIP_SAVE"})
    component: EnumProperty(items=(("MAIN", "주 형상", "주 형상의 중심을 이동합니다"), ("COMPANION", "연결 메시", "연결 메시의 중심을 이동합니다")), options={"HIDDEN", "SKIP_SAVE"})

    def execute(self, _context):
        """편집 토큰을 검증하고 확정 전 미리보기에만 원점 이동을 반영합니다."""
        operator = _OPEN_DIALOGS.get(self.edit_token)
        state = RUNTIME.axis_settings
        if operator is None or not state.is_active or state.token != self.edit_token:
            self.report({"WARNING"}, "축 설정 팝업이 열려 있지 않습니다.")
            return {"CANCELLED"}
        component = self.component.lower()
        try:
            transform = axis_transform_at_world_origin(RUNTIME, state.model_id, _component_transform(operator, component), companion=component == "companion")
            operator.suspend_updates = True
            _set_component_values(operator, component, transform)
        except (ValueError, RuntimeError) as error:
            operator.error_message = str(error)
            return {"CANCELLED"}
        finally:
            operator.suspend_updates = False
        return {"FINISHED"} if _publish_preview(operator) else {"CANCELLED"}


CLASSES = (COLMAPALIGN_OT_edit_geometry_axes, COLMAPALIGN_OT_reset_geometry_axes, COLMAPALIGN_OT_center_geometry_at_origin)
