"""Data Layers 선택 subset을 Project World에서 G/R/S로 조작하는 모달·Gizmo 경계입니다."""

from __future__ import annotations

from math import atan2, pi
from time import monotonic
from typing import Any

import bpy
from bpy.props import BoolProperty, EnumProperty, StringProperty
from bpy_extras import view3d_utils
from mathutils import Matrix, Vector

from ..alignment.layer_transform import (
    LayerTransformError,
    begin_layer_transform,
    cancel_layer_transform,
    commit_layer_transform,
    create_rotation_delta,
    create_translation_delta,
    create_uniform_scale_delta,
    layer_transform_pivot_for_selection,
    selected_model_ids_for_role,
    set_layer_transform_constraint,
    set_layer_transform_custom_pivot,
    set_layer_transform_preview_delta,
)
from ..constants import (
    LAYER_TRANSFORM_AXIS_NONE,
    LAYER_TRANSFORM_PHASE_ROTATE,
    LAYER_TRANSFORM_PHASE_SCALE,
    LAYER_TRANSFORM_PHASE_TRANSLATE,
    MODEL_STATUS_REGISTERED,
    ROLE_MOVING,
    ROLE_REFERENCE,
    SESSION_ACTIVE,
)
from ..model_repository import ModelRepositoryError
from ..project_persistence.runtime import notify_project_state_changed, restore_input_block_reason
from ..session import is_session_active_for_window, session_align_workspace
from ..state import RUNTIME
from ..status import set_workspace_status_text
from ..viewport.picking import find_viewport_under_mouse, pick_nearest_point_for_model_ids
from ..viewport.registry import find_window_region, role_for_area
from ..viewport.renderer import request_redraw
from .marker_alignment import is_icp_input_locked, is_marker_preview_active


_MODES = {
    "TRANSLATE": LAYER_TRANSFORM_PHASE_TRANSLATE,
    "ROTATE": LAYER_TRANSFORM_PHASE_ROTATE,
    "SCALE": LAYER_TRANSFORM_PHASE_SCALE,
}
_AXIS_VECTORS = {
    "X": Vector((1.0, 0.0, 0.0)),
    "Y": Vector((0.0, 1.0, 0.0)),
    "Z": Vector((0.0, 0.0, 1.0)),
}
# Arrow·dial의 local +Z를 Project World 축으로 향하게 하는 고정 회전입니다.
_GIZMO_AXIS_ROTATIONS = {
    "X": Matrix.Rotation(pi * 0.5, 4, "Y"),
    "Y": Matrix.Rotation(-pi * 0.5, 4, "X"),
    "Z": Matrix.Identity(4),
}
_MIN_SCREEN_RADIUS = 8.0


def _report(operator: bpy.types.Operator, level: set[str], message: str) -> None:
    """오퍼레이터 보고와 세션 상태줄에 같은 짧은 안내를 기록합니다."""

    if message:
        operator.report(level, message)
        set_workspace_status_text(session_align_workspace(), message)


def _pointer(value: Any) -> int:
    """RNA 객체가 아직 유효할 때만 비교 가능한 포인터 정수를 반환합니다."""

    try:
        return int(value.as_pointer()) if value is not None else 0
    except (AttributeError, ReferenceError, TypeError, ValueError):
        return 0


def _mode_from_value(value: str) -> str:
    """오퍼레이터 속성의 짧은 모드 값을 Runtime phase 상수로 변환합니다."""

    mode = _MODES.get(str(value).strip().upper())
    if mode is None:
        raise LayerTransformError("레이어 변환 모드가 올바르지 않습니다.")
    return mode


def _role_target_ids(role: str) -> tuple[str, ...]:
    """현재 A 또는 P 뷰포트 역할에서 변환할 선택 subset을 반환합니다."""

    return selected_model_ids_for_role(RUNTIME.model_catalog, role)


def layer_transform_active_block_reason() -> str | None:
    """다른 Data Layers 변경이 활성 레이어 변환과 충돌하는 이유를 반환합니다."""

    if bool(getattr(RUNTIME.layer_transform, "is_active", False)):
        return "데이터 레이어 변환을 확정하거나 취소한 뒤 레이어를 변경하십시오."
    return None


def layer_transform_start_block_reason(role: str | None = None) -> str | None:
    """새 G/R/S 또는 Gizmo 변환을 시작할 수 없는 단일 정확한 이유를 반환합니다."""

    from ..axis_settings import axis_edit_active_block_reason

    axis_reason = axis_edit_active_block_reason(RUNTIME)
    if axis_reason is not None:
        return axis_reason

    restore_reason = restore_input_block_reason()
    if restore_reason is not None:
        return restore_reason
    if not RUNTIME.session_active or RUNTIME.session.phase != SESSION_ACTIVE:
        return "활성 COLMAP Align 세션에서만 데이터 레이어를 변환할 수 있습니다."
    if RUNTIME.import_in_progress:
        return "COLMAP 가져오기가 끝난 뒤 데이터 레이어를 변환하십시오."
    if RUNTIME.workspace_setup_task is not None:
        return "작업공간 구성이 끝난 뒤 데이터 레이어를 변환하십시오."
    if RUNTIME.modal_active or RUNTIME.marker_selection.has_draft:
        return "열린 마커 입력을 확정하거나 취소한 뒤 데이터 레이어를 변환하십시오."
    if is_marker_preview_active() or is_icp_input_locked():
        return "마커 또는 ICP 미리보기를 정리한 뒤 데이터 레이어를 변환하십시오."
    merge_state = getattr(RUNTIME, "merge_export", None)
    if bool(getattr(merge_state, "is_running", False)):
        return "구조 통합 내보내기 중에는 데이터 레이어를 변환할 수 없습니다."
    obj_state = getattr(RUNTIME, "obj_export", None)
    if bool(getattr(obj_state, "is_running", False)):
        return "OBJ 내보내기 중에는 데이터 레이어를 변환할 수 없습니다."
    active_reason = layer_transform_active_block_reason()
    if active_reason is not None:
        return active_reason
    if role is not None and not _role_target_ids(role):
        return "현재 역할 뷰포트에서 변환할 데이터 레이어를 선택하십시오."
    return None


def _viewport_for_event(context: Any, event: Any) -> tuple[Any, Any, str] | None:
    """N 패널·헤더를 제외하고 실제 마우스 아래 A/P WINDOW 뷰포트를 찾습니다."""

    window = getattr(context, "window", None)
    mouse_x = getattr(event, "mouse_x", None)
    mouse_y = getattr(event, "mouse_y", None)
    if window is not None and mouse_x is not None and mouse_y is not None:
        resolved = find_viewport_under_mouse(window, int(mouse_x), int(mouse_y))
        if resolved is not None:
            return resolved
    area = getattr(context, "area", None)
    region = getattr(context, "region", None)
    role = role_for_area(window, area) if window is not None and area is not None else None
    if role is not None and getattr(region, "type", "") == "WINDOW":
        return area, region, role
    return None


def _region_mouse(region: Any, event: Any) -> Vector:
    """Window 절대 마우스 좌표를 현재 VIEW_3D WINDOW Region 좌표로 바꿉니다."""

    return Vector(
        (
            float(getattr(event, "mouse_x", 0.0)) - float(getattr(region, "x", 0.0)),
            float(getattr(event, "mouse_y", 0.0)) - float(getattr(region, "y", 0.0)),
        )
    )


def _visible_role_target_ids(role: str) -> tuple[str, ...]:
    """현재 역할 선택 중 실제 뷰포트에 표시되는 피킹 대상만 반환합니다."""

    catalog = RUNTIME.model_catalog
    return tuple(
        model_id
        for model_id in _role_target_ids(role)
        if model_id in catalog.models and bool(getattr(catalog.models[model_id], "is_visible", False))
    )


def set_layer_transform_pivot_from_event(context: Any, event: Any) -> bool:
    """화면 ID·깊이로 고른 정밀 표면점에 기즈모와 카메라 회전 중심을 옮깁니다."""

    viewport = _viewport_for_event(context, event)
    if viewport is None:
        return False
    area, region, role = viewport
    if layer_transform_start_block_reason(role) is not None:
        return False
    target_ids = _role_target_ids(role)
    visible_target_ids = _visible_role_target_ids(role)
    if not visible_target_ids:
        return False
    space = getattr(getattr(area, "spaces", None), "active", None)
    region_3d = getattr(space, "region_3d", None)
    if region_3d is None:
        return False
    mouse = _region_mouse(region, event)
    try:
        result = pick_nearest_point_for_model_ids(
            visible_target_ids,
            region,
            region_3d,
            (float(mouse.x), float(mouse.y)),
            screen_surface=True,
        )
        if result is None:
            message = "더블클릭 지점에서 선택 레이어의 포인트를 찾지 못했습니다."
            RUNTIME.layer_transform.message = message
            set_workspace_status_text(session_align_workspace(), message)
            request_redraw()
            return True
        pivot = set_layer_transform_custom_pivot(
            RUNTIME,
            role=role,
            model_ids=target_ids,
            source_model_id=result.model_id,
            source_position=result.position,
        )
    except (ModelRepositoryError, LayerTransformError) as error:
        message = str(error)
        RUNTIME.layer_transform.message = message
        set_workspace_status_text(session_align_workspace(), message)
        request_redraw()
        return True
    region_3d.view_location = Vector(tuple(float(value) for value in pivot))
    region_3d.update()
    message = "기즈모와 카메라 회전 중심을 선택한 포인트로 옮겼습니다."
    RUNTIME.layer_transform.message = message
    set_workspace_status_text(session_align_workspace(), message)
    request_redraw()
    return True


def _double_click_interval_seconds(context: Any) -> float:
    """Blender 사용자 입력 설정의 더블클릭 시간을 초 단위로 반환합니다."""

    preferences = getattr(context, "preferences", None)
    inputs = getattr(preferences, "inputs", None)
    interval_ms = getattr(inputs, "mouse_double_click_time", 350)
    return max(1.0, float(interval_ms)) / 1000.0


def _double_click_move_threshold(context: Any) -> int:
    """Blender 마우스 드래그 임계값을 더블클릭 위치 허용 오차로 반환합니다."""

    preferences = getattr(context, "preferences", None)
    inputs = getattr(preferences, "inputs", None)
    return max(1, int(getattr(inputs, "drag_threshold_mouse", 3)))


def _is_viewport_pivot_double_click(context: Any, event: Any) -> bool:
    """같은 A/P 뷰포트의 설정 시간·위치 내 연속 PRESS를 더블클릭으로 판정합니다."""

    state = RUNTIME.layer_transform
    if getattr(event, "value", "") == "DOUBLE_CLICK":
        state.clear_pivot_click()
        return True
    if getattr(event, "value", "") != "PRESS":
        return False
    window_pointer = _pointer(getattr(context, "window", None))
    area_pointer = _pointer(getattr(context, "area", None))
    mouse = (
        int(getattr(event, "mouse_x", 0)),
        int(getattr(event, "mouse_y", 0)),
    )
    current_time = monotonic()
    elapsed = current_time - state.pivot_click_time
    threshold = _double_click_move_threshold(context)
    offset_x = mouse[0] - state.pivot_click_mouse[0]
    offset_y = mouse[1] - state.pivot_click_mouse[1]
    is_double = bool(
        window_pointer
        and area_pointer
        and state.pivot_click_window_pointer == window_pointer
        and state.pivot_click_area_pointer == area_pointer
        and 0.0 <= elapsed <= _double_click_interval_seconds(context)
        and offset_x * offset_x + offset_y * offset_y <= threshold * threshold
    )
    if is_double:
        state.clear_pivot_click()
    else:
        state.pivot_click_time = current_time
        state.pivot_click_window_pointer = window_pointer
        state.pivot_click_area_pointer = area_pointer
        state.pivot_click_mouse = mouse
    return is_double


def _project_pivot(region: Any, region_3d: Any, pivot: Vector) -> Vector | None:
    """Project World pivot을 현재 Region 화면 좌표로 투영하고 실패는 None으로 반환합니다."""

    projected = view3d_utils.location_3d_to_region_2d(region, region_3d, pivot)
    return Vector(projected) if projected is not None else None


def _view_plane_point(region: Any, region_3d: Any, mouse: Vector, pivot: Vector) -> Vector | None:
    """마우스 광선과 pivot을 지나는 화면 평면의 교점을 안전하게 계산합니다."""

    origin = view3d_utils.region_2d_to_origin_3d(region, region_3d, mouse)
    direction = view3d_utils.region_2d_to_vector_3d(region, region_3d, mouse)
    normal = region_3d.view_rotation @ Vector((0.0, 0.0, -1.0))
    denominator = direction.dot(normal)
    if abs(denominator) <= 1.0e-9:
        return None
    distance = (pivot - origin).dot(normal) / denominator
    point = origin + direction * distance
    return point if all(abs(value) < float("inf") for value in point) else None


def _axis_translation(
    region: Any,
    region_3d: Any,
    pivot: Vector,
    start_mouse: Vector,
    current_mouse: Vector,
    axis: str,
) -> Vector | None:
    """화면에 투영한 Project World 축으로 제한된 이동량을 계산합니다."""

    axis_vector = _AXIS_VECTORS[axis]
    pivot_screen = _project_pivot(region, region_3d, pivot)
    axis_screen = _project_pivot(region, region_3d, pivot + axis_vector)
    if pivot_screen is None or axis_screen is None:
        return None
    direction = axis_screen - pivot_screen
    length_squared = float(direction.length_squared)
    if length_squared <= 1.0e-9:
        return None
    screen_delta = current_mouse - start_mouse
    return axis_vector * (screen_delta.dot(direction) / length_squared)


def _rotation_angle(start_vector: Vector, current_vector: Vector) -> float | None:
    """pivot 화면 반경이 충분할 때만 부호 있는 회전 각도를 반환합니다."""

    if start_vector.length < _MIN_SCREEN_RADIUS or current_vector.length < _MIN_SCREEN_RADIUS:
        return None
    cross = start_vector.x * current_vector.y - start_vector.y * current_vector.x
    dot = start_vector.dot(current_vector)
    return float(atan2(cross, dot))


def _uniform_scale_ratio(start_vector: Vector, current_vector: Vector) -> float | None:
    """pivot 기준 화면 반경 비율로 양수 균일 스케일만 계산합니다."""

    start_radius = float(start_vector.length)
    current_radius = float(current_vector.length)
    if start_radius < _MIN_SCREEN_RADIUS or current_radius <= 1.0e-9:
        return None
    return max(1.0e-6, current_radius / start_radius)


def _gizmo_matrix(pivot: Any, axis: str | None = None) -> Matrix:
    """기즈모 local +Z를 지정 Project World 축에 맞춰 공통 pivot 행렬을 생성합니다."""

    rotation = _GIZMO_AXIS_ROTATIONS.get(str(axis).upper(), Matrix.Identity(4))
    return Matrix.Translation(Vector(pivot)) @ rotation


def _gizmo_poll(context: Any) -> bool:
    """활성 선택 기즈모와 축 팝업 대상의 조작 불가능한 위치 표시를 허용합니다."""

    if context.area is None or context.region is None or context.region.type != "WINDOW":
        return False
    settings = getattr(getattr(context, "scene", None), "colmap_align_settings", None)
    if settings is None or not bool(getattr(settings, "show_transform_gizmo", False)):
        return False
    role = role_for_area(context.window, context.area)
    axes = RUNTIME.axis_settings
    if axes.is_active:
        record = RUNTIME.model_catalog.models.get(axes.model_id)
        target_role = ROLE_REFERENCE if record is not None and record.registration_status == MODEL_STATUS_REGISTERED else ROLE_MOVING
        return bool(record is not None and role == target_role
            and _pointer(context.window) == axes.window_pointer
            and _pointer(getattr(context, "workspace", None)) == axes.workspace_pointer
            and is_session_active_for_window(context.window))
    return bool(role is not None and layer_transform_start_block_reason(role) is None)


def _start_from_guard(context: Any, event: Any, mode: str) -> bool:
    """세션 가드가 마우스 아래 뷰포트 override에서 G/R/S 모달을 시작하도록 연결합니다."""

    resolved = _viewport_for_event(context, event)
    if resolved is None:
        return False
    area, region, role = resolved
    if layer_transform_start_block_reason(role) is not None:
        return False
    try:
        with bpy.context.temp_override(
            window=context.window,
            screen=context.window.screen,
            area=area,
            region=region,
        ):
            result = bpy.ops.colmap_align.transform_data_layers(
                "INVOKE_DEFAULT",
                mode=mode,
                axis=LAYER_TRANSFORM_AXIS_NONE,
                from_gizmo=False,
            )
    except (RuntimeError, ReferenceError):
        return False
    return "RUNNING_MODAL" in result


def start_layer_transform_from_guard(context: Any, event: Any, mode: str) -> bool:
    """세션 입력 가드가 사용할 공개 G/R/S 시작 함수를 제공합니다."""

    return _start_from_guard(context, event, mode)


class COLMAPALIGN_OT_set_layer_transform_pivot(bpy.types.Operator):
    """A/P 뷰포트 더블클릭을 사용자 Transform Pivot 설정으로 전달합니다."""

    bl_idname = "colmap_align.set_layer_transform_pivot"
    bl_label = "데이터 레이어 Transform Pivot 설정"
    bl_description = "더블클릭한 데이터 포인트로 기즈모와 카메라 회전 중심을 옮깁니다"
    bl_options = {"INTERNAL"}

    @classmethod
    def poll(cls, context):
        """활성 세션의 A/P VIEW_3D WINDOW에서만 더블클릭 오퍼레이터를 허용합니다."""

        window = getattr(context, "window", None)
        area = getattr(context, "area", None)
        region = getattr(context, "region", None)
        return bool(
            window is not None
            and area is not None
            and getattr(area, "type", "") == "VIEW_3D"
            and getattr(region, "type", "") == "WINDOW"
            and is_session_active_for_window(window)
            and role_for_area(window, area) is not None
        )

    def invoke(self, context, event):
        """실제 연속 PRESS를 판정해 피벗을 설정하고 첫 클릭은 기존 입력으로 통과시킵니다."""

        if not self.poll(context):
            return {"PASS_THROUGH"}
        if not _is_viewport_pivot_double_click(context, event):
            return {"PASS_THROUGH"}
        return (
            {"FINISHED"}
            if set_layer_transform_pivot_from_event(context, event)
            else {"PASS_THROUGH"}
        )


class COLMAPALIGN_OT_transform_data_layers(bpy.types.Operator):
    """현재 A/P 뷰포트 선택 subset만 G/R/S로 live preview하는 모달 오퍼레이터입니다."""

    bl_idname = "colmap_align.transform_data_layers"
    bl_label = "데이터 레이어 변환"
    bl_description = "현재 뷰포트에서 선택한 데이터 레이어를 Project World 기준으로 변환합니다"
    bl_options = {"INTERNAL", "BLOCKING", "GRAB_CURSOR"}

    mode: EnumProperty(
        items=(
            ("TRANSLATE", "이동", "선택 레이어를 이동합니다"),
            ("ROTATE", "회전", "선택 레이어를 회전합니다"),
            ("SCALE", "스케일", "선택 레이어를 균일 스케일합니다"),
        ),
        options={"HIDDEN", "SKIP_SAVE"},
    )
    axis: StringProperty(default=LAYER_TRANSFORM_AXIS_NONE, options={"HIDDEN", "SKIP_SAVE"})
    from_gizmo: BoolProperty(default=False, options={"HIDDEN", "SKIP_SAVE"})

    @classmethod
    def poll(cls, context):
        """세션 A/P VIEW_3D WINDOW에서만 레이어 변환 모달을 시작합니다."""

        return bool(
            is_session_active_for_window(context.window)
            and context.area is not None
            and context.area.type == "VIEW_3D"
            and context.region is not None
            and context.region.type == "WINDOW"
        )

    def invoke(self, context, event):
        """역할 subset의 baseline·pivot·입력 포인터를 동결한 뒤 모달을 시작합니다."""

        resolved = _viewport_for_event(context, event)
        if resolved is None:
            _report(self, {"WARNING"}, "A 또는 P 뷰포트 위에서만 데이터 레이어를 변환할 수 있습니다.")
            return {"CANCELLED"}
        area, region, role = resolved
        reason = layer_transform_start_block_reason(role)
        if reason is not None:
            _report(self, {"WARNING"}, reason)
            return {"CANCELLED"}
        try:
            phase = _mode_from_value(self.mode)
            state = begin_layer_transform(
                RUNTIME,
                role=role,
                model_ids=_role_target_ids(role),
                phase=phase,
                window=context.window,
                area=area,
                region=region,
            )
            if self.axis and self.axis != LAYER_TRANSFORM_AXIS_NONE:
                set_layer_transform_constraint(RUNTIME, self.axis)
        except LayerTransformError as error:
            _report(self, {"WARNING"}, str(error))
            return {"CANCELLED"}
        space = area.spaces.active
        region_3d = getattr(space, "region_3d", None)
        if region_3d is None:
            cancel_layer_transform(RUNTIME)
            _report(self, {"ERROR"}, "레이어 변환에 필요한 3D 뷰 정보를 찾지 못했습니다.")
            return {"CANCELLED"}
        self._window_pointer = _pointer(context.window)
        self._area_pointer = _pointer(area)
        self._region_pointer = _pointer(region)
        self._generation = state.generation
        self._region = region
        self._region_3d = region_3d
        self._start_mouse = _region_mouse(region, event)
        self._pivot = Vector(state.pivot_float64)
        self._start_plane_point = _view_plane_point(region, region_3d, self._start_mouse, self._pivot)
        self._pivot_screen = _project_pivot(region, region_3d, self._pivot)
        self._start_pivot_vector = (
            self._start_mouse - self._pivot_screen if self._pivot_screen is not None else Vector((0.0, 0.0))
        )
        RUNTIME.modal_active = True
        context.window_manager.modal_handler_add(self)
        request_redraw()
        return {"RUNNING_MODAL"}

    def _context_is_current(self, context: Any) -> bool:
        """시작 때 동결한 window·area·region·generation이 여전히 같은지 확인합니다."""

        state = RUNTIME.layer_transform
        return bool(
            state.is_active
            and state.generation == getattr(self, "_generation", -1)
            and _pointer(context.window) == getattr(self, "_window_pointer", 0)
            and _pointer(context.area) == getattr(self, "_area_pointer", 0)
            and _pointer(context.region) == getattr(self, "_region_pointer", 0)
        )

    def _preview_from_mouse(self, event: Any) -> bool:
        """현재 모드·축 제약·마우스 위치로 유효한 Sim(3) preview delta를 갱신합니다."""

        state = RUNTIME.layer_transform
        mouse = _region_mouse(self._region, event)
        try:
            if state.phase == LAYER_TRANSFORM_PHASE_TRANSLATE:
                if state.constraint_axis in _AXIS_VECTORS:
                    translation = _axis_translation(
                        self._region,
                        self._region_3d,
                        self._pivot,
                        self._start_mouse,
                        mouse,
                        state.constraint_axis,
                    )
                    if translation is None:
                        state.message = "축이 화면에 충분히 투영되지 않아 마지막 유효 변환을 유지합니다."
                        return False
                else:
                    current_point = _view_plane_point(self._region, self._region_3d, mouse, self._pivot)
                    if current_point is None or self._start_plane_point is None:
                        state.message = "뷰 평면 교차를 계산할 수 없어 마지막 유효 변환을 유지합니다."
                        return False
                    translation = current_point - self._start_plane_point
                delta = create_translation_delta(tuple(translation))
            elif state.phase == LAYER_TRANSFORM_PHASE_ROTATE:
                if self._pivot_screen is None:
                    state.message = "회전 pivot을 화면에 투영할 수 없어 마지막 유효 변환을 유지합니다."
                    return False
                angle = _rotation_angle(self._start_pivot_vector, mouse - self._pivot_screen)
                if angle is None:
                    state.message = "회전 반경이 너무 작아 마지막 유효 변환을 유지합니다."
                    return False
                axis = (
                    _AXIS_VECTORS[state.constraint_axis]
                    if state.constraint_axis in _AXIS_VECTORS
                    else self._region_3d.view_rotation @ Vector((0.0, 0.0, -1.0))
                )
                delta = create_rotation_delta(angle, tuple(axis), tuple(self._pivot))
            else:
                if self._pivot_screen is None:
                    state.message = "스케일 pivot을 화면에 투영할 수 없어 마지막 유효 변환을 유지합니다."
                    return False
                ratio = _uniform_scale_ratio(self._start_pivot_vector, mouse - self._pivot_screen)
                if ratio is None:
                    state.message = "스케일 반경이 너무 작아 마지막 유효 변환을 유지합니다."
                    return False
                delta = create_uniform_scale_delta(ratio, tuple(self._pivot))
            set_layer_transform_preview_delta(RUNTIME, delta)
        except LayerTransformError as error:
            state.message = str(error)
            return False
        state.message = ""
        request_redraw()
        return True

    def _finish(self, context: Any, *, commit: bool) -> set[str]:
        """LMB/Enter 확정 또는 RMB/Esc 취소를 한 경계에서 끝내고 redraw를 요청합니다."""

        try:
            if commit:
                result = commit_layer_transform(RUNTIME, action="데이터 레이어 G/R/S 변환")
                if result is not None:
                    notify_project_state_changed("Data Layers G/R/S 변환 확정")
                    _report(self, {"INFO"}, "데이터 레이어 변환을 확정했습니다.")
                else:
                    _report(self, {"INFO"}, "변경된 데이터 레이어 변환이 없습니다.")
                return {"FINISHED"}
            cancel_layer_transform(RUNTIME, message="데이터 레이어 변환을 취소했습니다.")
            _report(self, {"INFO"}, "데이터 레이어 변환을 취소했습니다.")
            return {"CANCELLED"}
        except LayerTransformError as error:
            # stale·기록 실패도 preview를 남기면 이후 입력이 잠기므로 Repository 변경 없이 즉시 폐기합니다.
            cancel_layer_transform(RUNTIME, message=str(error))
            _report(self, {"ERROR"}, str(error))
            return {"CANCELLED"}
        finally:
            RUNTIME.modal_active = False
            request_redraw()

    def modal(self, context, event):
        """X/Y/Z 제약·live preview·확정·취소와 창 비활성화를 처리합니다."""

        if event.type == "WINDOW_DEACTIVATE":
            return self._finish(context, commit=False)
        if not self._context_is_current(context):
            return self._finish(context, commit=False)
        if event.type in {"ESC", "RIGHTMOUSE"} and event.value == "PRESS":
            return self._finish(context, commit=False)
        if event.type in {"RET", "NUMPAD_ENTER"} and event.value == "PRESS":
            return self._finish(context, commit=True)
        if event.type == "LEFTMOUSE":
            if (self.from_gizmo and event.value == "RELEASE") or (
                not self.from_gizmo and event.value == "PRESS"
            ):
                return self._finish(context, commit=True)
        if event.type in _AXIS_VECTORS and event.value == "PRESS":
            try:
                set_layer_transform_constraint(RUNTIME, event.type)
                self._preview_from_mouse(event)
            except LayerTransformError as error:
                _report(self, {"WARNING"}, str(error))
            request_redraw()
            return {"RUNNING_MODAL"}
        if event.type == "MOUSEMOVE":
            self._preview_from_mouse(event)
            return {"RUNNING_MODAL"}
        return {"RUNNING_MODAL"}

    def cancel(self, context):
        """Blender가 모달을 강제로 중단해도 Repository를 바꾸지 않고 preview를 정리합니다."""

        if bool(getattr(RUNTIME.layer_transform, "is_active", False)):
            cancel_layer_transform(RUNTIME, message="데이터 레이어 변환을 취소했습니다.")
        RUNTIME.modal_active = False
        request_redraw()


class COLMAPALIGN_GGT_layer_transform(bpy.types.GizmoGroup):
    """선택 subset의 공통 Project World pivot에 G/R/S 시작점을 제공하는 커스텀 GizmoGroup입니다."""

    bl_idname = "COLMAPALIGN_GGT_layer_transform"
    bl_label = "COLMAP Align 레이어 변환"
    bl_space_type = "VIEW_3D"
    bl_region_type = "WINDOW"
    bl_options = {"3D", "PERSISTENT"}

    @classmethod
    def poll(cls, context):
        """현재 역할의 선택 또는 축 팝업 대상에 기즈모를 노출합니다."""

        return _gizmo_poll(context)

    def _bind_operator(self, gizmo: Any, mode: str, axis: str = LAYER_TRANSFORM_AXIS_NONE) -> None:
        """모든 기즈모 손잡이를 동일 모달 오퍼레이터와 확정 규칙으로 연결합니다."""

        operator = gizmo.target_set_operator("colmap_align.transform_data_layers")
        operator.mode = mode
        operator.axis = axis
        operator.from_gizmo = True

    def setup(self, _context):
        """전역 X/Y/Z 이동·회전과 중앙 균일 스케일 기즈모를 한 번 생성합니다."""

        self._translate = []
        self._rotate = []
        for axis, color in zip(("X", "Y", "Z"), ((0.9, 0.2, 0.2), (0.2, 0.9, 0.2), (0.2, 0.4, 0.95)), strict=True):
            arrow = self.gizmos.new("GIZMO_GT_arrow_3d")
            arrow.color = color
            arrow.alpha = 0.75
            arrow.scale_basis = 0.75
            self._bind_operator(arrow, "TRANSLATE", axis)
            self._translate.append((axis, arrow))
            dial = self.gizmos.new("GIZMO_GT_dial_3d")
            dial.color = color
            dial.alpha = 0.55
            dial.scale_basis = 0.9
            self._bind_operator(dial, "ROTATE", axis)
            self._rotate.append((axis, dial))
        # Blender 기본 3D 이동 핸들을 중앙 균일 스케일 시작점으로 재사용하되 실제 연산은 S 모달만 수행합니다.
        self._scale = self.gizmos.new("GIZMO_GT_move_3d")
        self._scale.color = (0.95, 0.8, 0.15)
        self._scale.alpha = 0.85
        self._scale.scale_basis = 0.2
        self._bind_operator(self._scale, "SCALE")

    def draw_prepare(self, context):
        """작은 피벗 행렬을 갱신하고 축 팝업에서는 위치만 표시하며 드래그를 차단합니다."""

        state = RUNTIME.layer_transform
        axes = RUNTIME.axis_settings
        target_role = role_for_area(context.window, context.area)
        target_ids = _role_target_ids(target_role) if target_role is not None else ()
        if axes.is_active:
            from ..axis_settings import axis_gizmo_world_position, AxisSettingsError
            try:
                pivot = axis_gizmo_world_position(RUNTIME, axes.token, displayed=True)
            except AxisSettingsError:
                return
        elif state.is_active:
            pivot = state.pivot_float64
        else:
            try:
                pivot = layer_transform_pivot_for_selection(RUNTIME, target_role, target_ids)
            except LayerTransformError:
                return
        for axis, gizmo in self._translate:
            gizmo.matrix_basis = _gizmo_matrix(pivot, axis)
            gizmo.hide_select = axes.is_active
        for axis, gizmo in self._rotate:
            gizmo.matrix_basis = _gizmo_matrix(pivot, axis)
            gizmo.hide_select = axes.is_active
        self._scale.matrix_basis = _gizmo_matrix(pivot)
        self._scale.hide_select = axes.is_active


CLASSES = (
    COLMAPALIGN_OT_set_layer_transform_pivot,
    COLMAPALIGN_OT_transform_data_layers,
    COLMAPALIGN_GGT_layer_transform,
)
