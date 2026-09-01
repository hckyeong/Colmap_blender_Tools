"""Paint Layer 목록과 색상 보정 UI 연산자를 제공합니다."""

from __future__ import annotations

from dataclasses import dataclass
import time
import uuid

import bpy
from bpy.props import EnumProperty, IntProperty, StringProperty

from .ui_messages import draw_wrapped_confirmation, invoke_wrapped_confirmation


_ADJUSTMENT_ITEMS = (
    ("BRIGHTNESS", "Brightness", "밝기 보정 UI 항목을 추가합니다"),
    ("EXPOSURE", "Exposure", "노출 보정 UI 항목을 추가합니다"),
    (
        "SATURATION",
        "Hue/Saturation",
        "색조와 채도 보정 UI 항목을 추가합니다",
    ),
)

_ADJUSTMENT_UI = {
    "BRIGHTNESS": ("Brightness", "MOD_BRIGHTNESS_CONTRAST"),
    "EXPOSURE": ("Exposure", "LIGHT_SUN"),
    "HUE": ("Hue/Saturation", "MOD_HUE_SATURATION"),
    "SATURATION": ("Hue/Saturation", "MOD_HUE_SATURATION"),
}


_LAYER_UI_POINTER_EVENT_MAX_AGE_SECONDS = 0.5
_LAYER_UI_DUPLICATE_PRESS_MAX_AGE_SECONDS = 0.02
_LAYER_UI_POINTER_POSITION_TOLERANCE_PX = 5.0
_LAYER_UI_TRACKER_RETRY_SECONDS = 0.1
_layer_ui_event_tracking_enabled = True
_layer_ui_pointer_events: dict[int, "_LayerUIPointerEvent"] = {}
_layer_ui_pointer_releases: set[int] = set()
_layer_ui_tracked_windows: set[int] = set()
_layer_ui_double_click_candidates: dict[int, "_LayerUIDoubleClickCandidate"] = {}


@dataclass(frozen=True)
class _LayerUIPointerEvent:
    """실제 LEFTMOUSE 입력에서 보존한 modifier와 트리 선택 스냅샷입니다."""

    value: str
    ctrl: bool
    shift: bool
    mouse_x: int
    mouse_y: int
    recorded_at: float
    scene_key: int = 0
    selected_item_keys: tuple[str, ...] = ()
    selection_anchor_uuid: str = ""


@dataclass(frozen=True)
class _LayerUIDoubleClickCandidate:
    """두 번째 클릭 판정에 필요한 포인터 정수와 항목 키·시각만 보관합니다."""

    window_key: int
    scene_key: int
    workspace_key: int
    item_key: str
    clicked_at: float


@dataclass(frozen=True)
class _LayerTreeSelectionItem:
    """선택 가능한 Layer 트리 행의 종류와 Collection 위치를 보관합니다."""

    key: str
    kind: str
    layer_index: int
    adjustment_index: int = -1


@dataclass(frozen=True)
class _LayerTreeDeleteItem:
    """복수 삭제 대상을 RNA 참조 없이 UUID와 원래 위치로 고정합니다."""

    key: str
    kind: str
    layer_uuid: str
    item_uuid: str
    adjustment_uuid: str = ""
    layer_index: int = -1
    adjustment_index: int = -1


def _layer_ui_window_key(window) -> int:
    """창별 실제 포인터 입력 스냅샷을 위한 안정 키를 반환합니다."""
    try:
        return int(window.as_pointer()) if window is not None else 0
    except ReferenceError:
        return 0


def _layer_ui_scene_key(scene) -> int:
    """트리 선택을 기록한 Scene을 다른 Scene과 구분하는 안정 키를 반환합니다."""
    try:
        as_pointer = getattr(scene, "as_pointer", None)
        return int(as_pointer()) if callable(as_pointer) else id(scene)
    except ReferenceError:
        return 0


def _layer_ui_workspace_key(workspace) -> int:
    """작업공간 전환을 감지할 수 있도록 RNA 포인터 정수만 반환합니다."""

    try:
        if workspace is None:
            return 0
        as_pointer = getattr(workspace, "as_pointer", None)
        return int(as_pointer()) if callable(as_pointer) else id(workspace)
    except (AttributeError, ReferenceError, RuntimeError, TypeError):
        return 0


def _clear_layer_ui_double_click_candidate(window_key: int = 0) -> None:
    """지정 창 또는 모든 창의 더블클릭 후보를 즉시 폐기합니다."""

    if int(window_key):
        _layer_ui_double_click_candidates.pop(int(window_key), None)
        return
    _layer_ui_double_click_candidates.clear()


def clear_layer_ui_runtime_state(_reason: str = "", _epoch: int = 0) -> None:
    """Load·Undo·Redo 수명주기 경계에서 포인터·더블클릭 임시 상태를 비웁니다."""

    _layer_ui_pointer_events.clear()
    _layer_ui_pointer_releases.clear()
    _clear_layer_ui_double_click_candidate()


def _tag_layer_ui_redraw() -> None:
    """Timer에서 트리 선택을 바꾼 뒤 모든 현재 Area를 다시 그리도록 요청합니다."""
    window_manager = getattr(getattr(bpy, "context", None), "window_manager", None)
    for window in tuple(getattr(window_manager, "windows", ())):
        screen = getattr(window, "screen", None)
        for area in tuple(getattr(screen, "areas", ())):
            try:
                area.tag_redraw()
            except ReferenceError:
                continue


def _layer_ui_pointer_position_tolerance(context) -> int:
    """Blender UI 배율을 반영한 클릭 좌표 허용 범위를 반환합니다."""
    preferences = getattr(context, "preferences", None)
    system = getattr(preferences, "system", None)
    try:
        ui_scale = float(getattr(system, "ui_scale", 1.0) or 1.0)
    except (TypeError, ValueError):
        ui_scale = 1.0
    return max(1, round(_LAYER_UI_POINTER_POSITION_TOLERANCE_PX * ui_scale))


def _layer_ui_pointer_position_matches(
    context,
    snapshot,
    mouse_x: int,
    mouse_y: int,
) -> bool:
    """자연스러운 손떨림 범위의 PRESS·RELEASE를 같은 클릭으로 판정합니다."""
    tolerance = _layer_ui_pointer_position_tolerance(context)
    return (
        abs(int(mouse_x) - snapshot.mouse_x) <= tolerance
        and abs(int(mouse_y) - snapshot.mouse_y) <= tolerance
    )


def _discard_pending_layer_ui_pointer_event(window_key: int, snapshot) -> bool:
    """확정되지 않은 입력만 폐기하고 현재 트리 선택은 유지합니다."""
    if _layer_ui_pointer_events.get(window_key) is not snapshot:
        return False
    _layer_ui_pointer_events.pop(window_key, None)
    _layer_ui_pointer_releases.discard(window_key)
    return True


def _clear_pending_layer_ui_selection(window_key: int, snapshot) -> bool:
    """버튼이 소비하지 않은 RELEASE가 확정된 Scene 트리 선택을 해제합니다."""
    if not _discard_pending_layer_ui_pointer_event(window_key, snapshot):
        return False
    _clear_layer_ui_double_click_candidate(window_key)
    if not snapshot.scene_key:
        return False
    scene = next(
        (
            candidate
            for candidate in tuple(getattr(bpy.data, "scenes", ()))
            if _layer_ui_scene_key(candidate) == snapshot.scene_key
        ),
        None,
    )
    settings = getattr(scene, "projection_paint", None)
    if settings is None:
        return False
    _clear_layer_tree_selection(settings)
    settings.layer_selection_anchor_uuid = ""
    _tag_layer_ui_redraw()
    return True


def _finalize_pending_layer_ui_selection_clear() -> float | None:
    """미소비 RELEASE만 선택 해제하고 만료된 PRESS는 입력만 폐기합니다."""
    now = time.monotonic()
    next_delay = None
    for window_key, snapshot in tuple(_layer_ui_pointer_events.items()):
        age = now - snapshot.recorded_at
        if window_key in _layer_ui_pointer_releases:
            _clear_pending_layer_ui_selection(window_key, snapshot)
            continue
        if age >= _LAYER_UI_POINTER_EVENT_MAX_AGE_SECONDS:
            _discard_pending_layer_ui_pointer_event(window_key, snapshot)
            continue
        remaining = _LAYER_UI_POINTER_EVENT_MAX_AGE_SECONDS - age
        next_delay = remaining if next_delay is None else min(next_delay, remaining)
    return next_delay


def _schedule_layer_ui_selection_clear(*, immediate: bool) -> None:
    """입력 Dispatch 뒤 후보 해제를 Lifecycle 소유 App Timer로 예약합니다."""
    if bpy.app.background or not _layer_ui_event_tracking_enabled:
        return
    from .lifecycle import lifecycle_registry

    registry = lifecycle_registry()
    if immediate:
        registry.unregister_timer(_finalize_pending_layer_ui_selection_clear)
    registry.register_timer(
        _finalize_pending_layer_ui_selection_clear,
        component="layer_ui_selection_clear",
        first_interval=(0.0 if immediate else _LAYER_UI_POINTER_EVENT_MAX_AGE_SECONDS),
    )


def _record_layer_ui_pointer_event(window, event, *, context=None) -> None:
    """LEFTMOUSE PRESS를 보존하고 레이어 버튼이 소비할 때까지 후보 변경을 미룹니다."""
    if str(getattr(event, "type", "") or "") != "LEFTMOUSE":
        return
    value = str(getattr(event, "value", "") or "")
    if value not in {"PRESS", "RELEASE", "CLICK", "DOUBLE_CLICK"}:
        return
    window_key = _layer_ui_window_key(window)
    if not window_key:
        return
    mouse_x = int(getattr(event, "mouse_x", 0))
    mouse_y = int(getattr(event, "mouse_y", 0))
    ctrl = bool(getattr(event, "ctrl", False))
    shift = bool(getattr(event, "shift", False))
    now = time.monotonic()
    existing = _layer_ui_pointer_events.get(window_key)
    if value != "PRESS" and existing is not None and existing.value == "PRESS":
        if (
            now - existing.recorded_at
            <= _LAYER_UI_POINTER_EVENT_MAX_AGE_SECONDS
            and _layer_ui_pointer_position_matches(
                context,
                existing,
                mouse_x,
                mouse_y,
            )
        ):
            _layer_ui_pointer_releases.add(window_key)
            _schedule_layer_ui_selection_clear(immediate=True)
        return
    if value == "PRESS" and existing is not None:
        if (
            existing.value == "PRESS"
            and now - existing.recorded_at
            <= _LAYER_UI_DUPLICATE_PRESS_MAX_AGE_SECONDS
            and _layer_ui_pointer_position_matches(
                context,
                existing,
                mouse_x,
                mouse_y,
            )
            and existing.ctrl == ctrl
            and existing.shift == shift
        ):
            return
        _discard_pending_layer_ui_pointer_event(window_key, existing)
    scene = getattr(context, "scene", None)
    settings = getattr(scene, "projection_paint", None)
    scene_key = 0
    selected_item_keys: tuple[str, ...] = ()
    selection_anchor_uuid = ""
    if value == "PRESS" and settings is not None:
        scene_key = _layer_ui_scene_key(scene)
        selected_item_keys = _selected_layer_tree_keys(settings)
        selection_anchor_uuid = str(
            getattr(settings, "layer_selection_anchor_uuid", "") or ""
        )
    _layer_ui_pointer_events[window_key] = _LayerUIPointerEvent(
        value=value,
        ctrl=ctrl,
        shift=shift,
        mouse_x=mouse_x,
        mouse_y=mouse_y,
        recorded_at=now,
        scene_key=scene_key,
        selected_item_keys=selected_item_keys,
        selection_anchor_uuid=selection_anchor_uuid,
    )
    _layer_ui_pointer_releases.discard(window_key)
    if value == "PRESS" and scene_key:
        _schedule_layer_ui_selection_clear(immediate=False)


def _layer_ui_pointer_event(context, event):
    """버튼 invoke 직전의 같은 실제 LEFTMOUSE 입력만 반환합니다."""
    window_key = _layer_ui_window_key(getattr(context, "window", None))
    snapshot = _layer_ui_pointer_events.get(window_key)
    if snapshot is None:
        return None
    if time.monotonic() - snapshot.recorded_at > _LAYER_UI_POINTER_EVENT_MAX_AGE_SECONDS:
        _discard_pending_layer_ui_pointer_event(window_key, snapshot)
        return None
    event_value = str(getattr(event, "value", "") or "")
    press_to_button_release = (
        snapshot.value == "PRESS"
        and event_value in {"RELEASE", "CLICK", "DOUBLE_CLICK"}
    )
    if event_value and event_value != snapshot.value and not press_to_button_release:
        return None
    mouse_x = getattr(event, "mouse_x", None)
    mouse_y = getattr(event, "mouse_y", None)
    if (
        mouse_x is not None
        and mouse_y is not None
        and not _layer_ui_pointer_position_matches(
            context,
            snapshot,
            int(mouse_x),
            int(mouse_y),
        )
    ):
        return None
    if mouse_x is not None and mouse_y is None and int(mouse_x) != snapshot.mouse_x:
        return None
    if mouse_y is not None and mouse_x is None and int(mouse_y) != snapshot.mouse_y:
        return None
    _layer_ui_pointer_releases.discard(window_key)
    return _layer_ui_pointer_events.pop(window_key)


def _restore_layer_ui_pointer_selection(context, event=None):
    """레이어 관련 버튼이 소비한 PRESS의 트리 선택과 앵커를 복원합니다."""
    snapshot = _layer_ui_pointer_event(context, event)
    if snapshot is None or not snapshot.scene_key:
        return snapshot
    scene = getattr(context, "scene", None)
    if _layer_ui_scene_key(scene) != snapshot.scene_key:
        return snapshot
    settings = getattr(scene, "projection_paint", None)
    if settings is None:
        return snapshot
    _restore_layer_tree_selection_keys(settings, snapshot.selected_item_keys)
    settings.layer_selection_anchor_uuid = snapshot.selection_anchor_uuid
    return snapshot


def _layer_ui_modifiers(context, event) -> tuple[bool, bool]:
    """레이어 버튼이 소비한 선택과 실제 modifier를 함께 복원합니다."""
    snapshot = _restore_layer_ui_pointer_selection(context, event)
    if snapshot is not None:
        return snapshot.ctrl, snapshot.shift
    return bool(getattr(event, "ctrl", False)), bool(getattr(event, "shift", False))


def enable_layer_ui_event_tracking() -> None:
    """현재 Extension 수명주기에서 실제 포인터 입력 추적을 다시 허용합니다."""
    global _layer_ui_event_tracking_enabled
    _layer_ui_event_tracking_enabled = True


def disable_layer_ui_event_tracking() -> None:
    """해제한 Extension의 모달 추적기와 임시 입력 상태를 무효화합니다."""
    global _layer_ui_event_tracking_enabled
    _layer_ui_event_tracking_enabled = False
    try:
        from .lifecycle import lifecycle_registry

        lifecycle_registry().unregister_timer(
            _finalize_pending_layer_ui_selection_clear
        )
        lifecycle_registry().remove_barrier_hook(clear_layer_ui_runtime_state)
    except (AttributeError, RuntimeError):
        pass
    clear_layer_ui_runtime_state()
    _layer_ui_tracked_windows.clear()


def _layer_ui_event_tracker_context_ready(context) -> bool:
    """등록 중 제한 Context가 아닌 실제 UI override 가능 Context인지 확인합니다."""
    return bool(
        callable(getattr(context, "temp_override", None))
        and getattr(context, "window_manager", None) is not None
    )


class PP_OT_layer_ui_pointer_tracker(bpy.types.Operator):
    """일반 UI 버튼 전에 실제 LEFTMOUSE modifier를 PASS_THROUGH로 기록합니다."""

    bl_idname = "projection_paint.layer_ui_pointer_tracker"
    bl_label = "Track Layer UI Pointer"
    bl_options = {"INTERNAL"}

    def invoke(self, context, _event):
        """현재 창에 한 개의 비간섭 모달 입력 추적기만 등록합니다."""
        if bpy.app.background or not _layer_ui_event_tracking_enabled:
            return {"CANCELLED"}
        window_key = _layer_ui_window_key(getattr(context, "window", None))
        if not window_key or window_key in _layer_ui_tracked_windows:
            return {"CANCELLED"}
        self._window_key = window_key
        _layer_ui_tracked_windows.add(window_key)
        context.window_manager.modal_handler_add(self)
        return {"RUNNING_MODAL"}

    def modal(self, context, event):
        """다른 UI 처리를 막지 않고 포인터 입력과 후보 해제를 갱신합니다."""
        if not _layer_ui_event_tracking_enabled:
            _layer_ui_tracked_windows.discard(getattr(self, "_window_key", 0))
            return {"CANCELLED"}
        _record_layer_ui_pointer_event(
            getattr(context, "window", None),
            event,
            context=context,
        )
        return {"PASS_THROUGH"}


class PP_OT_block_multiselect_paint_stroke(bpy.types.Operator):
    """복수 선택 또는 읽기 전용 Original의 Paint 스트로크를 소비합니다."""

    bl_idname = "projection_paint.block_multiselect_paint_stroke"
    bl_label = "Block Multi-Selection Paint Stroke"
    bl_options = {"INTERNAL"}

    @classmethod
    def poll(cls, context):
        """활성 Paint Viewport의 복수 선택·외부 Original 입력만 차단합니다."""
        scene = getattr(context, "scene", None)
        settings = getattr(scene, "projection_paint", None)
        area = getattr(context, "area", None)
        region = getattr(context, "region", None)
        window = getattr(context, "window", None)
        target = getattr(settings, "paint_target", None) if settings is not None else None
        view_layer = getattr(context, "view_layer", None)
        read_only_original = False
        if settings is not None and not bool(getattr(settings, "paint_mask_mode", False)):
            index = int(getattr(settings, "active_layer_index", -1))
            available_layers = getattr(settings, "layers", ())
            layer = (
                available_layers[index]
                if 0 <= index < len(available_layers)
                else None
            )
            if layer is not None:
                from .layer_shader import original_layer_is_read_only

                read_only_original = original_layer_is_read_only(settings, layer)
        return bool(
            settings is not None
            and bool(getattr(settings, "paint_enabled", False))
            and (
                layer_tree_selection_count(settings) > 1
                or read_only_original
            )
            and area is not None
            and str(getattr(area, "type", "")) == "VIEW_3D"
            and region is not None
            and str(getattr(region, "type", "")) == "WINDOW"
            and window is not None
            and getattr(window, "workspace", None)
            is getattr(settings, "paint_workspace", None)
            and target is not None
            and str(getattr(target, "mode", "")) == "TEXTURE_PAINT"
            and view_layer is not None
            and getattr(view_layer.objects, "active", None) is target
        )

    def execute(self, context):
        """차단 이유를 기록하고 Native Paint 연산자까지 입력 전달을 막습니다."""
        settings = context.scene.projection_paint
        index = int(getattr(settings, "active_layer_index", -1))
        available_layers = getattr(settings, "layers", ())
        layer = (
            available_layers[index]
            if 0 <= index < len(available_layers)
            else None
        )
        from .layer_shader import original_layer_is_read_only

        message = (
            "Original Layer는 외부 원본을 직접 참조하므로 페인팅할 수 없습니다."
            if layer is not None
            and not bool(settings.paint_mask_mode)
            and original_layer_is_read_only(settings, layer)
            else "복수 레이어 선택 중에는 페인팅할 수 없습니다."
        )
        settings.status = message
        settings.status_kind = "WARNING"
        self.report({"WARNING"}, message)
        return {"FINISHED"}

    def invoke(self, context, _event):
        """실제 Pixel·Mask Stroke만 소비하고 Clone Source 지정과 비Paint 도구는 통과시킵니다."""
        event = _event
        event_type = str(getattr(event, "type", "") or "")
        if event_type not in {"LEFTMOUSE", "PEN", "ERASER"}:
            return {"PASS_THROUGH"}
        if bool(getattr(event, "alt", False)):
            return {"PASS_THROUGH"}
        from .paint_tool_bridge import active_tool_id, paint_tool_spec

        tool_id = active_tool_id(context)
        if paint_tool_spec(tool_id) is None:
            return {"PASS_THROUGH"}
        clone_source_gesture = bool(
            tool_id == "builtin_brush.clone"
            and event_type in {"LEFTMOUSE", "PEN"}
            and bool(getattr(event, "ctrl", False))
        )
        if clone_source_gesture:
            return {"PASS_THROUGH"}
        return self.execute(context)


def layer_ui_event_tracker_heartbeat() -> float | None:
    """각 Blender 창에 실제 UI 버튼 modifier 추적기를 하나씩 보장합니다."""
    if bpy.app.background or not _layer_ui_event_tracking_enabled:
        return None
    context = getattr(bpy, "context", None)
    if not _layer_ui_event_tracker_context_ready(context):
        return _LAYER_UI_TRACKER_RETRY_SECONDS
    window_manager = context.window_manager
    windows = tuple(getattr(window_manager, "windows", ()))
    if not windows:
        return _LAYER_UI_TRACKER_RETRY_SECONDS
    live_window_keys = set()
    for window in windows:
        window_key = _layer_ui_window_key(window)
        live_window_keys.add(window_key)
        candidate = _layer_ui_double_click_candidates.get(window_key)
        if (
            candidate is not None
            and candidate.workspace_key
            != _layer_ui_workspace_key(getattr(window, "workspace", None))
        ):
            _clear_layer_ui_double_click_candidate(window_key)
        if not window_key or window_key in _layer_ui_tracked_windows:
            continue
        screen = getattr(window, "screen", None)
        area = next(
            (candidate for candidate in getattr(screen, "areas", ()) if candidate.type == "VIEW_3D"),
            None,
        )
        region = next(
            (
                candidate
                for candidate in getattr(area, "regions", ())
                if candidate.type == "WINDOW"
            ),
            None,
        )
        if area is None or region is None:
            continue
        try:
            with context.temp_override(
                window=window,
                screen=screen,
                area=area,
                region=region,
            ):
                bpy.ops.projection_paint.layer_ui_pointer_tracker("INVOKE_DEFAULT")
        except (AttributeError, ReferenceError, RuntimeError, TypeError):
            continue
    for window_key in tuple(_layer_ui_double_click_candidates):
        if window_key not in live_window_keys:
            _clear_layer_ui_double_click_candidate(window_key)
    return 1.0


def _next_layer_name(layers) -> str:
    """현재 목록에서 사용하지 않는 가장 작은 Layer 번호를 반환합니다."""
    names = {str(layer.name) for layer in layers}
    index = 1
    while f"Layer {index}" in names:
        index += 1
    return f"Layer {index}"


def _find_layer(settings, layer_uuid: str):
    """고유 UUID와 일치하는 논리 Paint Layer를 반환합니다."""
    return next(
        (
            layer
            for layer in settings.layers
            if str(layer.layer_uuid) == str(layer_uuid)
        ),
        None,
    )


def _layer_editable(settings, layer) -> bool:
    """Composite 활성 중 원본 Layer가 일반 편집 경로에 들어오지 않게 제한합니다."""
    from .layer_shader import composite_layer_is_editable

    return composite_layer_is_editable(settings, layer)


def _context_layer(context):
    """트리의 우클릭 Context가 가리키는 논리 Paint Layer를 반환합니다."""
    layer = getattr(context, "projection_paint_layer", None)
    identifier = str(
        getattr(getattr(layer, "bl_rna", None), "identifier", "") or ""
    )
    if identifier == "PP_PG_layer":
        return layer
    button_operator = getattr(context, "button_operator", None)
    operator_identifier = str(
        getattr(
            getattr(button_operator, "bl_rna", None),
            "identifier",
            "",
        )
        or ""
    )
    if operator_identifier in {
        "PROJECTION_PAINT_OT_layer_select",
        "PROJECTION_PAINT_OT_layer_toggle",
    }:
        settings = getattr(
            getattr(context, "scene", None),
            "projection_paint",
            None,
        )
        return (
            _find_layer(
                settings,
                str(getattr(button_operator, "layer_uuid", "") or ""),
            )
            if settings is not None
            else None
        )
    button_pointer = getattr(context, "button_pointer", None)
    button_identifier = str(
        getattr(
            getattr(button_pointer, "bl_rna", None),
            "identifier",
            "",
        )
        or ""
    )
    button_property = str(
        getattr(getattr(context, "button_prop", None), "identifier", "")
        or ""
    )
    if (
        button_identifier == "PP_PG_layer"
        and button_property == "name"
    ):
        return button_pointer
    if (
        button_identifier == "PP_PG_settings"
        and button_property == "active_layer_index"
    ):
        index = int(button_pointer.active_layer_index)
        return (
            button_pointer.layers[index]
            if 0 <= index < len(button_pointer.layers)
            else None
        )
    return None


def _adjustment_types(layer) -> set[str]:
    """Layer에 이미 추가된 색상 보정 타입 집합을 반환합니다."""
    return {
        (
            "SATURATION"
            if str(adjustment.adjustment_type) == "HUE"
            else str(adjustment.adjustment_type)
        )
        for adjustment in layer.adjustments
    }


def _find_adjustment_index(layer, adjustment_uuid: str) -> int:
    """고유 UUID와 일치하는 Layer 색상 보정 Index를 반환합니다."""
    return next(
        (
            index
            for index, adjustment in enumerate(layer.adjustments)
            if str(adjustment.adjustment_uuid) == str(adjustment_uuid)
        ),
        -1,
    )


def _layer_tree_item_key(kind: str, item_uuid: str) -> str:
    """Layer는 기존 UUID를 유지하고 자식 행은 종류 접두사로 구분합니다."""
    return str(item_uuid) if str(kind) == "LAYER" else f"{kind}:{item_uuid}"


def _layer_tree_selection_items(
    settings,
    *,
    visible_only: bool,
    editable_only: bool,
) -> tuple[_LayerTreeSelectionItem, ...]:
    """현재 Layer 트리 행을 화면 순서와 선택 가능 조건에 맞춰 반환합니다."""
    items: list[_LayerTreeSelectionItem] = []
    for layer_index, layer in enumerate(settings.layers):
        if editable_only and not _layer_editable(settings, layer):
            continue
        layer_uuid = str(layer.layer_uuid or "")
        if not layer_uuid:
            continue
        items.append(
            _LayerTreeSelectionItem(
                key=_layer_tree_item_key("LAYER", layer_uuid),
                kind="LAYER",
                layer_index=layer_index,
            )
        )
        if visible_only and not bool(layer.expanded):
            continue
        layer_mask_uuid = str(layer.mask.mask_uuid or "")
        if layer_mask_uuid:
            items.append(
                _LayerTreeSelectionItem(
                    key=_layer_tree_item_key("LAYER_MASK", layer_mask_uuid),
                    kind="LAYER_MASK",
                    layer_index=layer_index,
                )
            )
        for adjustment_index, adjustment in enumerate(layer.adjustments):
            adjustment_uuid = str(adjustment.adjustment_uuid or "")
            if not adjustment_uuid:
                continue
            items.append(
                _LayerTreeSelectionItem(
                    key=_layer_tree_item_key("ADJUSTMENT", adjustment_uuid),
                    kind="ADJUSTMENT",
                    layer_index=layer_index,
                    adjustment_index=adjustment_index,
                )
            )
            if visible_only and not bool(adjustment.expanded):
                continue
            adjustment_mask_uuid = str(adjustment.mask.mask_uuid or "")
            if adjustment_mask_uuid:
                items.append(
                    _LayerTreeSelectionItem(
                        key=_layer_tree_item_key(
                            "ADJUSTMENT_MASK",
                            adjustment_mask_uuid,
                        ),
                        kind="ADJUSTMENT_MASK",
                        layer_index=layer_index,
                        adjustment_index=adjustment_index,
                    )
                )
    return tuple(items)


def _layer_tree_item_data(settings, item: _LayerTreeSelectionItem):
    """선택 행에 대응하는 Layer·Adjustment·Mask PropertyGroup을 반환합니다."""
    layer = settings.layers[item.layer_index]
    adjustment = (
        layer.adjustments[item.adjustment_index]
        if item.adjustment_index >= 0
        else None
    )
    if item.kind == "LAYER":
        value = layer
    elif item.kind == "LAYER_MASK":
        value = layer.mask
    elif item.kind == "ADJUSTMENT":
        value = adjustment
    else:
        value = adjustment.mask
    return layer, adjustment, value


def _layer_tree_item_selected(settings, item: _LayerTreeSelectionItem) -> bool:
    """지정 트리 행의 저장된 UI 선택 여부를 반환합니다."""
    _layer, _adjustment, value = _layer_tree_item_data(settings, item)
    return bool(getattr(value, "ui_selected", False))


def _set_layer_tree_item_selected(
    settings,
    item: _LayerTreeSelectionItem,
    selected: bool,
) -> None:
    """지정 트리 행의 UI 선택 상태를 저장합니다."""
    _layer, _adjustment, value = _layer_tree_item_data(settings, item)
    value.ui_selected = bool(selected)


def _clear_layer_tree_selection(settings) -> None:
    """Layer·Mask·Adjustment의 모든 UI 선택 상태를 해제합니다."""
    for item in _layer_tree_selection_items(
        settings,
        visible_only=False,
        editable_only=False,
    ):
        _set_layer_tree_item_selected(settings, item, False)


def _selected_layer_tree_keys(settings) -> tuple[str, ...]:
    """Pointer 입력 전에 저장할 모든 선택 행 키를 반환합니다."""
    return tuple(
        item.key
        for item in _layer_tree_selection_items(
            settings,
            visible_only=False,
            editable_only=False,
        )
        if _layer_tree_item_selected(settings, item)
    )


def _restore_layer_tree_selection_keys(settings, selected_keys) -> None:
    """저장된 행 키 집합으로 Layer 트리 UI 선택 상태를 복원합니다."""
    selected = {str(key) for key in selected_keys}
    for item in _layer_tree_selection_items(
        settings,
        visible_only=False,
        editable_only=False,
    ):
        _set_layer_tree_item_selected(settings, item, item.key in selected)


def layer_tree_selection_count(settings) -> int:
    """현재 편집 가능한 트리에서 선택된 Layer·Mask·Adjustment 수를 반환합니다."""
    return sum(
        1
        for item in _layer_tree_selection_items(
            settings,
            visible_only=False,
            editable_only=True,
        )
        if _layer_tree_item_selected(settings, item)
    )


def _select_layer_tree_item(
    settings,
    item_key: str,
    *,
    ctrl: bool,
    shift: bool,
) -> _LayerTreeSelectionItem | None:
    """일반 클릭은 단일 선택하고 Ctrl·Shift 클릭만 복수 선택합니다."""
    items = _layer_tree_selection_items(
        settings,
        visible_only=True,
        editable_only=True,
    )
    target_position = next(
        (index for index, item in enumerate(items) if item.key == str(item_key)),
        -1,
    )
    if target_position < 0:
        return None
    target = items[target_position]
    anchor_key = str(getattr(settings, "layer_selection_anchor_uuid", "") or "")
    anchor_position = next(
        (index for index, item in enumerate(items) if item.key == anchor_key),
        -1,
    )
    if shift and anchor_position >= 0:
        if not ctrl:
            _clear_layer_tree_selection(settings)
        start, end = sorted((anchor_position, target_position))
        for item in items[start : end + 1]:
            _set_layer_tree_item_selected(settings, item, True)
        return target
    if not ctrl:
        _clear_layer_tree_selection(settings)
    if ctrl:
        _set_layer_tree_item_selected(
            settings,
            target,
            not _layer_tree_item_selected(settings, target),
        )
        if layer_tree_selection_count(settings) == 0:
            _set_layer_tree_item_selected(settings, target, True)
    else:
        _set_layer_tree_item_selected(settings, target, True)
    settings.layer_selection_anchor_uuid = target.key
    return target


def _activate_layer_tree_item(context, item: _LayerTreeSelectionItem) -> None:
    """선택 행을 활성 Layer·Mask·Adjustment와 실제 Paint Image에 연결합니다."""
    settings = context.scene.projection_paint
    layer, adjustment, _value = _layer_tree_item_data(settings, item)
    settings.active_layer_index = int(item.layer_index)
    if item.kind == "LAYER":
        layer.active_adjustment_index = -1
        settings.active_mask_uuid = ""
        if layer.image is not None:
            from .layer_shader import activate_layer_paint_image

            activate_layer_paint_image(context, str(layer.layer_uuid))
        return
    if item.kind == "ADJUSTMENT":
        layer.active_adjustment_index = int(item.adjustment_index)
        settings.active_mask_uuid = ""
        if layer.image is not None:
            from .layer_shader import activate_layer_paint_image

            activate_layer_paint_image(context, str(layer.layer_uuid))
            layer.active_adjustment_index = int(item.adjustment_index)
        return
    layer.active_adjustment_index = (
        int(item.adjustment_index) if adjustment is not None else -1
    )
    mask = adjustment.mask if adjustment is not None else layer.mask
    settings.active_mask_uuid = str(mask.mask_uuid)
    if mask.image is not None:
        from .layer_shader import activate_mask_paint_image

        activate_mask_paint_image(
            context,
            str(layer.layer_uuid),
            str(mask.mask_uuid),
        )


def _select_and_activate_layer_tree_item(
    context,
    item_key: str,
    *,
    ctrl: bool,
    shift: bool,
) -> bool:
    """트리 선택 규칙을 적용하고 선택 집합의 활성 행을 즉시 갱신합니다."""
    settings = context.scene.projection_paint
    target = _select_layer_tree_item(
        settings,
        item_key,
        ctrl=ctrl,
        shift=shift,
    )
    if target is None:
        return False
    active = target
    if not _layer_tree_item_selected(settings, target):
        active = next(
            (
                item
                for item in reversed(
                    _layer_tree_selection_items(
                        settings,
                        visible_only=True,
                        editable_only=True,
                    )
                )
                if _layer_tree_item_selected(settings, item)
            ),
            target,
        )
    _activate_layer_tree_item(context, active)
    return True


def _mouse_double_click_seconds(context) -> float:
    """Blender 입력 설정의 더블클릭 허용 시간을 초 단위로 반환합니다."""

    inputs = getattr(getattr(context, "preferences", None), "inputs", None)
    try:
        return max(0.001, int(inputs.mouse_double_click_time) / 1000.0)
    except (AttributeError, TypeError, ValueError):
        return 0.35


def _layer_ui_click_activates(
    context,
    item_key: str,
    *,
    ctrl: bool,
    shift: bool,
) -> bool:
    """같은 창·장면·작업공간·항목의 두 번째 무수정 클릭만 활성화합니다."""

    window = getattr(context, "window", None)
    window_key = _layer_ui_window_key(window)
    if not window_key:
        return False
    if ctrl or shift:
        _clear_layer_ui_double_click_candidate(window_key)
        return False
    scene_key = _layer_ui_scene_key(getattr(context, "scene", None))
    workspace_key = _layer_ui_workspace_key(getattr(window, "workspace", None))
    now = time.monotonic()
    previous = _layer_ui_double_click_candidates.get(window_key)
    within_interval = bool(
        previous is not None
        and previous.scene_key == scene_key
        and previous.workspace_key == workspace_key
        and previous.item_key == str(item_key)
        and now - previous.clicked_at <= _mouse_double_click_seconds(context)
    )
    if within_interval:
        _clear_layer_ui_double_click_candidate(window_key)
        return True
    _layer_ui_double_click_candidates[window_key] = _LayerUIDoubleClickCandidate(
        window_key=window_key,
        scene_key=scene_key,
        workspace_key=workspace_key,
        item_key=str(item_key),
        clicked_at=now,
    )
    return False


def _select_layer_tree_item_from_ui(
    context,
    item_key: str,
    *,
    ctrl: bool,
    shift: bool,
) -> bool:
    """UI 클릭은 먼저 선택하고 같은 항목의 두 번째 클릭에서만 활성화합니다."""

    settings = context.scene.projection_paint
    target = _select_layer_tree_item(
        settings,
        item_key,
        ctrl=ctrl,
        shift=shift,
    )
    if target is None:
        _clear_layer_ui_double_click_candidate(
            _layer_ui_window_key(getattr(context, "window", None))
        )
        return False
    if _layer_ui_click_activates(
        context,
        item_key,
        ctrl=ctrl,
        shift=shift,
    ):
        _activate_layer_tree_item(context, target)
    return True


def _context_adjustment(context):
    """마우스가 가리키는 Layer 보조 아이콘의 Layer와 Index를 반환합니다."""
    button_operator = getattr(context, "button_operator", None)
    identifier = str(
        getattr(
            getattr(button_operator, "bl_rna", None),
            "identifier",
            "",
        )
        or ""
    )
    if identifier != "PROJECTION_PAINT_OT_layer_adjustment_select":
        return None
    settings = getattr(getattr(context, "scene", None), "projection_paint", None)
    if settings is None:
        return None
    layer = _find_layer(
        settings,
        str(getattr(button_operator, "layer_uuid", "") or ""),
    )
    if layer is None:
        return None
    index = _find_adjustment_index(
        layer,
        str(getattr(button_operator, "adjustment_uuid", "") or ""),
    )
    return (layer, index) if index >= 0 else None


def _context_mask(context):
    """마우스가 가리키는 Layer 또는 Adjustment Mask 부모를 반환합니다."""
    button_operator = getattr(context, "button_operator", None)
    identifier = str(
        getattr(getattr(button_operator, "bl_rna", None), "identifier", "")
        or ""
    )
    if identifier not in {
        "PROJECTION_PAINT_OT_layer_mask_select",
        "PROJECTION_PAINT_OT_adjustment_mask_select",
    }:
        return None
    settings = getattr(getattr(context, "scene", None), "projection_paint", None)
    layer = (
        _find_layer(settings, str(getattr(button_operator, "layer_uuid", "")))
        if settings is not None
        else None
    )
    if layer is None:
        return None
    if identifier == "PROJECTION_PAINT_OT_layer_mask_select":
        return layer, None, layer.mask
    index = _find_adjustment_index(
        layer,
        str(getattr(button_operator, "adjustment_uuid", "")),
    )
    if index < 0:
        return None
    return layer, layer.adjustments[index], layer.adjustments[index].mask


def _reconcile_if_ready(context) -> bool:
    """관리 Material과 Overlay가 있으면 Layer Shader Graph를 동기화합니다."""
    settings = getattr(getattr(context, "scene", None), "projection_paint", None)
    if (
        settings is None
        or settings.paint_material is None
        or settings.overlay_image is None
    ):
        return False
    if str(settings.composite_state) == "STALE":
        raise RuntimeError("손상된 Composite 상태에서는 Layer Shader Graph를 자동으로 재구성할 수 없습니다.")
    from .layer_shader import reconcile_layer_graph

    reconcile_layer_graph(context.scene)
    return True


def _remove_adjustment(
    layer,
    index: int,
    context=None,
    *,
    reconcile_graph: bool = True,
) -> bool:
    """Layer 보조 항목을 제거하고 필요할 때만 Shader Graph를 재조정합니다."""
    if index < 0 or index >= len(layer.adjustments):
        return False
    if context is not None and not _layer_editable(
        context.scene.projection_paint,
        layer,
    ):
        return False
    if context is not None:
        from .layer_shader import remove_adjustment_resources

        remove_adjustment_resources(
            context.scene,
            layer,
            layer.adjustments[index],
        )
    active_index = int(layer.active_adjustment_index)
    layer.adjustments.remove(index)
    if len(layer.adjustments) == 0:
        layer.active_adjustment_index = -1
    elif active_index > index:
        layer.active_adjustment_index = active_index - 1
    elif active_index == index:
        layer.active_adjustment_index = min(index, len(layer.adjustments) - 1)
    if context is not None and reconcile_graph:
        _reconcile_if_ready(context)
    return True


def active_layer_adjustment(layer):
    """Layer에서 현재 선택된 색상 보정 항목을 반환합니다."""
    index = int(layer.active_adjustment_index)
    return (
        layer.adjustments[index]
        if 0 <= index < len(layer.adjustments)
        else None
    )


def active_layer_tree_item(settings):
    """트리에서 유일하게 선택된 Layer·Mask·Adjustment 항목을 반환합니다."""
    layer_index = int(settings.active_layer_index)
    if layer_index < 0 or layer_index >= len(settings.layers):
        return None
    layer = settings.layers[layer_index]
    adjustment_index = int(layer.active_adjustment_index)
    adjustment = (
        layer.adjustments[adjustment_index]
        if 0 <= adjustment_index < len(layer.adjustments)
        else None
    )
    mask_uuid = str(settings.active_mask_uuid or "")
    if mask_uuid:
        if (
            adjustment is not None
            and str(adjustment.mask.mask_uuid or "") == mask_uuid
        ):
            return "ADJUSTMENT_MASK", layer, adjustment
        if (
            adjustment is None
            and str(layer.mask.mask_uuid or "") == mask_uuid
        ):
            return "LAYER_MASK", layer, None
        return None
    if adjustment is not None:
        return "ADJUSTMENT", layer, adjustment
    return "LAYER", layer, None


def active_layer_tree_item_key(settings) -> str:
    """실제 Paint 활성 항목의 종류와 UUID를 하나의 트리 키로 반환합니다."""

    active = active_layer_tree_item(settings)
    if active is None:
        return ""
    kind, layer, adjustment = active
    if kind == "LAYER":
        item_uuid = str(layer.layer_uuid or "")
    elif kind == "LAYER_MASK":
        item_uuid = str(layer.mask.mask_uuid or "")
    elif kind == "ADJUSTMENT":
        item_uuid = str(adjustment.adjustment_uuid or "")
    else:
        item_uuid = str(adjustment.mask.mask_uuid or "")
    return _layer_tree_item_key(kind, item_uuid) if item_uuid else ""


def layer_row_selected(settings, layer_index: int) -> bool:
    """지정 Layer 행의 UI 선택 상태를 반환합니다."""
    return bool(
        0 <= int(layer_index) < len(settings.layers)
        and bool(getattr(settings.layers[layer_index], "ui_selected", False))
    )


def adjustment_row_selected(
    settings,
    layer_index: int,
    adjustment_index: int,
) -> bool:
    """지정 색상 보정 행의 UI 선택 상태를 반환합니다."""
    if not 0 <= int(layer_index) < len(settings.layers):
        return False
    layer = settings.layers[layer_index]
    return bool(
        0 <= int(adjustment_index) < len(layer.adjustments)
        and bool(getattr(layer.adjustments[adjustment_index], "ui_selected", False))
    )


def layer_mask_row_selected(settings, layer_index: int) -> bool:
    """지정 Layer Mask 행의 UI 선택 상태를 반환합니다."""
    if not 0 <= int(layer_index) < len(settings.layers):
        return False
    layer = settings.layers[layer_index]
    return bool(
        str(layer.mask.mask_uuid or "")
        and bool(getattr(layer.mask, "ui_selected", False))
    )


def adjustment_mask_row_selected(
    settings,
    layer_index: int,
    adjustment_index: int,
) -> bool:
    """지정 Adjustment Mask 행의 UI 선택 상태를 반환합니다."""
    if not 0 <= int(layer_index) < len(settings.layers):
        return False
    layer = settings.layers[layer_index]
    if not 0 <= int(adjustment_index) < len(layer.adjustments):
        return False
    adjustment = layer.adjustments[adjustment_index]
    return bool(
        str(adjustment.mask.mask_uuid or "")
        and bool(getattr(adjustment.mask, "ui_selected", False))
    )


def mask_preview_active(settings, adjustment_uuid: str) -> bool:
    """지정 Mask가 현재 Base Color 단독 미리보기인지 반환합니다."""
    return bool(
        str(adjustment_uuid)
        and str(settings.mask_preview_adjustment_uuid) == str(adjustment_uuid)
    )


def layer_preview_active(settings, layer_uuid: str) -> bool:
    """지정 Layer가 현재 Base Color 단독 미리보기인지 반환합니다."""
    return bool(
        str(layer_uuid)
        and str(settings.layer_preview_uuid) == str(layer_uuid)
    )


def adjustment_ui(adjustment_type: str) -> tuple[str, str]:
    """색상 보정 타입에 대응하는 표시 이름과 Blender Icon을 반환합니다."""
    return _ADJUSTMENT_UI.get(
        str(adjustment_type),
        ("Adjustment", "QUESTION"),
    )


def _last_layer_reset_message(layer_name: str, resolution_label: str) -> str:
    """마지막 Layer 교체 확인창에 표시할 데이터 폐기 문구를 반환합니다."""
    return (
        f"{layer_name}과 모든 Mask·보정 작업을 삭제하고 새 {resolution_label} Layer 1을 생성합니다. "
        "되돌리기는 Blender Undo 기록이 있을 때만 가능합니다."
    )


def _last_writable_layer_reset_required(settings, layer) -> bool:
    """읽기 전용 Original을 제외한 마지막 Paint Layer 삭제인지 반환합니다."""
    if len(settings.layers) == 1:
        return True
    from .layer_shader import original_layer_is_read_only

    writable = tuple(
        candidate
        for candidate in settings.layers
        if not original_layer_is_read_only(settings, candidate)
    )
    return bool(
        len(writable) == 1
        and str(writable[0].layer_uuid) == str(layer.layer_uuid)
    )


def draw_layer_list_context_menu(menu, context) -> None:
    """Layer 행에는 추가 메뉴를, 보조 아이콘에는 삭제 메뉴를 표시합니다."""
    hovered_mask = _context_mask(context)
    if hovered_mask is not None:
        layer, adjustment, _mask = hovered_mask
        layout = menu.layout
        layout.separator()
        operator = layout.operator(
            (
                "projection_paint.adjustment_mask_remove"
                if adjustment is not None
                else "projection_paint.layer_mask_remove"
            ),
            text="Delete Mask",
            icon="X",
        )
        operator.layer_uuid = str(layer.layer_uuid)
        if adjustment is not None:
            operator.adjustment_uuid = str(adjustment.adjustment_uuid)
        return
    hovered_adjustment = _context_adjustment(context)
    if hovered_adjustment is not None:
        layer, index = hovered_adjustment
        adjustment = layer.adjustments[index]
        label, _icon = adjustment_ui(adjustment.adjustment_type)
        layout = menu.layout
        layout.separator()
        if not str(adjustment.mask.mask_uuid or ""):
            add_mask = layout.operator(
                "projection_paint.adjustment_mask_add",
                text="Add Mask",
                icon="MOD_MASK",
            )
            add_mask.layer_uuid = str(layer.layer_uuid)
            add_mask.adjustment_uuid = str(adjustment.adjustment_uuid)
        operator = layout.operator(
            "projection_paint.layer_adjustment_remove",
            text=f"Delete {label}",
            icon="X",
        )
        operator.layer_uuid = str(layer.layer_uuid)
        operator.adjustment_uuid = str(adjustment.adjustment_uuid)
        return
    layer = _context_layer(context)
    if layer is None:
        return
    existing_types = _adjustment_types(layer)
    layout = menu.layout
    layout.separator()
    if not str(layer.mask.mask_uuid or ""):
        add_mask = layout.operator(
            "projection_paint.layer_mask_add",
            text="Add Mask",
            icon="MOD_MASK",
        )
        add_mask.layer_uuid = str(layer.layer_uuid)
    rename_operator = layout.operator(
        "projection_paint.layer_rename",
        text="Rename Layer",
        icon="GREASEPENCIL",
    )
    rename_operator.layer_uuid = str(layer.layer_uuid)
    layout.separator()
    layout.label(text="Add Adjustment", icon="COLOR")
    for adjustment_type, _name, _description in _ADJUSTMENT_ITEMS:
        label, icon = _ADJUSTMENT_UI[adjustment_type]
        entry = layout.row()
        entry.enabled = adjustment_type not in existing_types
        operator = entry.operator(
            "projection_paint.layer_adjustment_add",
            text=f"Add {label}",
            icon=icon,
        )
        operator.layer_uuid = str(layer.layer_uuid)
        operator.adjustment_type = adjustment_type


class PP_OT_layer_select(bpy.types.Operator):
    """Paint Layer UI 선택과 더블클릭 활성화를 분리해 처리합니다."""

    bl_idname = "projection_paint.layer_select"
    bl_label = "Select Paint Layer"
    bl_description = "클릭하면 선택하고 같은 항목을 더블클릭하면 활성화합니다"
    bl_options = {"INTERNAL"}

    layer_uuid: StringProperty(options={"HIDDEN"})

    @classmethod
    def poll(cls, context):
        """하나 이상의 논리 Paint Layer가 있을 때만 선택을 허용합니다."""
        settings = getattr(getattr(context, "scene", None), "projection_paint", None)
        return bool(settings is not None and len(settings.layers) > 0)

    def _activate_paint_layer(self, context):
        """스크립트 호환 호출에서 대상 Layer만 선택하고 즉시 활성화합니다."""
        settings = context.scene.projection_paint
        layer = _find_layer(settings, self.layer_uuid)
        if layer is None:
            return {"CANCELLED"}
        if not _layer_editable(settings, layer):
            self.report({"WARNING"}, "Composite 활성 중에는 기존 Layer를 선택할 수 없습니다.")
            return {"CANCELLED"}
        try:
            selected = _select_and_activate_layer_tree_item(
                context,
                _layer_tree_item_key("LAYER", str(layer.layer_uuid)),
                ctrl=False,
                shift=False,
            )
        except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        if not selected:
            return {"CANCELLED"}
        return {"FINISHED"}

    def invoke(self, context, event):
        """실제 modifier를 반영해 선택하고 무수정 더블클릭에서만 활성화합니다."""
        settings = context.scene.projection_paint
        layer = _find_layer(settings, self.layer_uuid)
        if layer is None or not _layer_editable(settings, layer):
            return {"CANCELLED"}
        ctrl, shift = _layer_ui_modifiers(context, event)
        try:
            selected = _select_layer_tree_item_from_ui(
                context,
                _layer_tree_item_key("LAYER", str(layer.layer_uuid)),
                ctrl=ctrl,
                shift=shift,
            )
        except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        if not selected:
            return {"CANCELLED"}
        return {"FINISHED"}

    def execute(self, context):
        """스크립트 직접 호출은 기존 호환을 위해 활성 Paint Layer 선택으로 처리합니다."""
        return self._activate_paint_layer(context)


class PP_OT_layer_rename(bpy.types.Operator):
    """우클릭으로 선택한 Paint Layer 이름을 변경합니다."""

    bl_idname = "projection_paint.layer_rename"
    bl_label = "Rename Paint Layer"
    bl_description = "이 Paint Layer의 이름을 변경합니다"
    bl_options = {"REGISTER", "UNDO"}

    layer_uuid: StringProperty(options={"HIDDEN"})
    new_name: StringProperty(name="Layer Name")

    @classmethod
    def poll(cls, context):
        """하나 이상의 논리 Paint Layer가 있을 때만 이름 변경을 허용합니다."""
        settings = getattr(getattr(context, "scene", None), "projection_paint", None)
        return bool(settings is not None and len(settings.layers) > 0)

    def invoke(self, context, _event):
        """현재 Layer 이름을 채운 속성 대화상자를 표시합니다."""
        layer = _find_layer(context.scene.projection_paint, self.layer_uuid)
        if layer is None:
            return {"CANCELLED"}
        self.new_name = str(layer.name)
        return context.window_manager.invoke_props_dialog(self)

    def execute(self, context):
        """비어 있지 않은 새 이름을 UUID가 일치하는 Layer에 저장합니다."""
        layer = _find_layer(context.scene.projection_paint, self.layer_uuid)
        new_name = str(self.new_name).strip()
        if (
            layer is None
            or not new_name
            or not _layer_editable(context.scene.projection_paint, layer)
        ):
            return {"CANCELLED"}
        layer.name = new_name
        from .layer_shader import rename_layer_frame

        rename_layer_frame(context.scene, str(layer.layer_uuid))
        return {"FINISHED"}


class PP_OT_layer_toggle(bpy.types.Operator):
    """Paint Layer의 들여쓰기된 자식 행을 접거나 펼칩니다."""

    bl_idname = "projection_paint.layer_toggle"
    bl_label = "Toggle Paint Layer"
    bl_description = "이 Layer의 Mask와 보정 자식 행을 접거나 펼칩니다"
    bl_options = {"INTERNAL"}

    layer_uuid: StringProperty(options={"HIDDEN"})

    @classmethod
    def poll(cls, context):
        """하나 이상의 논리 Paint Layer가 있을 때만 전환을 허용합니다."""
        settings = getattr(getattr(context, "scene", None), "projection_paint", None)
        return bool(settings is not None and len(settings.layers) > 0)

    def execute(self, context):
        """UUID가 일치하는 Layer의 자식 표시 펼침 상태만 변경합니다."""
        settings = context.scene.projection_paint
        layer = _find_layer(settings, self.layer_uuid)
        if layer is None or not _layer_editable(settings, layer):
            return {"CANCELLED"}
        layer.expanded = not bool(layer.expanded)
        return {"FINISHED"}


class PP_OT_layer_adjustment_select(bpy.types.Operator):
    """색상 보정 UI 선택과 더블클릭 활성화를 분리해 처리합니다."""

    bl_idname = "projection_paint.layer_adjustment_select"
    bl_label = "Select Layer Adjustment"
    bl_description = "이 색상 보정의 파라미터를 표시합니다"
    bl_options = {"INTERNAL"}

    layer_uuid: StringProperty(options={"HIDDEN"})
    adjustment_uuid: StringProperty(options={"HIDDEN"})

    @classmethod
    def poll(cls, context):
        """하나 이상의 논리 Paint Layer가 있을 때만 선택을 허용합니다."""
        settings = getattr(getattr(context, "scene", None), "projection_paint", None)
        return bool(settings is not None and len(settings.layers) > 0)

    def execute(self, context):
        """스크립트 호출에서는 대상 색상 보정만 선택하고 활성화합니다."""
        settings = context.scene.projection_paint
        layer = _find_layer(settings, self.layer_uuid)
        if layer is None or not _layer_editable(settings, layer):
            return {"CANCELLED"}
        index = _find_adjustment_index(layer, self.adjustment_uuid)
        if index < 0 or not _layer_editable(settings, layer):
            return {"CANCELLED"}
        try:
            selected = _select_and_activate_layer_tree_item(
                context,
                _layer_tree_item_key(
                    "ADJUSTMENT",
                    str(layer.adjustments[index].adjustment_uuid),
                ),
                ctrl=False,
                shift=False,
            )
        except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        return {"FINISHED"} if selected else {"CANCELLED"}

    def invoke(self, context, event):
        """색상 보정 행을 선택하고 무수정 더블클릭에서만 활성화합니다."""
        settings = context.scene.projection_paint
        layer = _find_layer(settings, self.layer_uuid)
        index = (
            _find_adjustment_index(layer, self.adjustment_uuid)
            if layer is not None
            else -1
        )
        if index < 0 or not _layer_editable(settings, layer):
            return {"CANCELLED"}
        ctrl, shift = _layer_ui_modifiers(context, event)
        try:
            selected = _select_layer_tree_item_from_ui(
                context,
                _layer_tree_item_key(
                    "ADJUSTMENT",
                    str(layer.adjustments[index].adjustment_uuid),
                ),
                ctrl=ctrl,
                shift=shift,
            )
        except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        return {"FINISHED"} if selected else {"CANCELLED"}


class PP_OT_adjustment_toggle(bpy.types.Operator):
    """Adjustment의 Mask 자식 행을 접고 부모를 유일 선택으로 전환합니다."""

    bl_idname = "projection_paint.adjustment_toggle"
    bl_label = "Toggle Adjustment"
    bl_options = {"INTERNAL"}

    layer_uuid: StringProperty(options={"HIDDEN"})
    adjustment_uuid: StringProperty(options={"HIDDEN"})

    def execute(self, context):
        """대상 Adjustment의 펼침과 선택 상태를 함께 갱신합니다."""
        settings = context.scene.projection_paint
        layer = _find_layer(settings, self.layer_uuid)
        index = (
            _find_adjustment_index(layer, self.adjustment_uuid)
            if layer is not None
            else -1
        )
        if index < 0 or not _layer_editable(settings, layer):
            return {"CANCELLED"}
        adjustment = layer.adjustments[index]
        adjustment.expanded = not bool(adjustment.expanded)
        settings.active_layer_index = next(
            layer_index
            for layer_index, candidate in enumerate(settings.layers)
            if str(candidate.layer_uuid) == str(layer.layer_uuid)
        )
        settings.active_mask_uuid = ""
        if layer.image is not None:
            from .layer_shader import activate_layer_paint_image

            activate_layer_paint_image(context, str(layer.layer_uuid))
        layer.active_adjustment_index = index
        _clear_layer_tree_selection(settings)
        adjustment.ui_selected = True
        settings.layer_selection_anchor_uuid = _layer_tree_item_key(
            "ADJUSTMENT",
            str(adjustment.adjustment_uuid),
        )
        return {"FINISHED"}


class PP_OT_layer_mask_add(bpy.types.Operator):
    """선택 Layer에 하나의 브러시 Paint Mask를 추가합니다."""

    bl_idname = "projection_paint.layer_mask_add"
    bl_label = "Add Layer Mask"
    bl_description = "이 Paint Layer에 Mask를 추가합니다"
    bl_options = {"REGISTER", "UNDO"}

    layer_uuid: StringProperty(options={"HIDDEN"})

    def execute(self, context):
        """비어 있는 Layer Mask를 생성하고 활성 Paint Image로 선택합니다."""
        settings = context.scene.projection_paint
        layer = _find_layer(settings, self.layer_uuid)
        if (
            layer is None
            or not _layer_editable(settings, layer)
            or str(layer.mask.mask_uuid or "")
        ):
            return {"CANCELLED"}
        layer.mask.mask_uuid = uuid.uuid4().hex
        settings.active_layer_index = next(
            index
            for index, candidate in enumerate(settings.layers)
            if str(candidate.layer_uuid) == str(layer.layer_uuid)
        )
        layer.active_adjustment_index = -1
        settings.active_mask_uuid = str(layer.mask.mask_uuid)
        try:
            _reconcile_if_ready(context)
            if layer.mask.image is not None:
                from .layer_shader import activate_mask_paint_image

                activate_mask_paint_image(
                    context,
                    str(layer.layer_uuid),
                    str(layer.mask.mask_uuid),
                )
        except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
            if layer.mask.image is not None:
                from .layer_shader import remove_mask_resources

                remove_mask_resources(context.scene, layer, layer.mask)
            else:
                layer.mask.mask_uuid = ""
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        _clear_layer_tree_selection(settings)
        layer.mask.ui_selected = True
        settings.layer_selection_anchor_uuid = _layer_tree_item_key(
            "LAYER_MASK",
            str(layer.mask.mask_uuid),
        )
        return {"FINISHED"}


class PP_OT_adjustment_mask_add(bpy.types.Operator):
    """선택 Adjustment에 하나의 브러시 Paint Mask를 추가합니다."""

    bl_idname = "projection_paint.adjustment_mask_add"
    bl_label = "Add Adjustment Mask"
    bl_description = "이 Adjustment에 자체 Mask를 추가합니다"
    bl_options = {"REGISTER", "UNDO"}

    layer_uuid: StringProperty(options={"HIDDEN"})
    adjustment_uuid: StringProperty(options={"HIDDEN"})

    def execute(self, context):
        """비어 있는 Adjustment Mask를 생성하고 활성 Paint Image로 선택합니다."""
        settings = context.scene.projection_paint
        layer = _find_layer(settings, self.layer_uuid)
        index = (
            _find_adjustment_index(layer, self.adjustment_uuid)
            if layer is not None
            else -1
        )
        if index < 0 or not _layer_editable(settings, layer):
            return {"CANCELLED"}
        adjustment = layer.adjustments[index]
        if str(adjustment.mask.mask_uuid or ""):
            return {"CANCELLED"}
        adjustment.mask.mask_uuid = uuid.uuid4().hex
        settings.active_layer_index = next(
            layer_index
            for layer_index, candidate in enumerate(settings.layers)
            if str(candidate.layer_uuid) == str(layer.layer_uuid)
        )
        layer.active_adjustment_index = index
        settings.active_mask_uuid = str(adjustment.mask.mask_uuid)
        try:
            _reconcile_if_ready(context)
            if adjustment.mask.image is not None:
                from .layer_shader import activate_mask_paint_image

                activate_mask_paint_image(
                    context,
                    str(layer.layer_uuid),
                    str(adjustment.mask.mask_uuid),
                )
        except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
            if adjustment.mask.image is not None:
                from .layer_shader import remove_mask_resources

                remove_mask_resources(
                    context.scene,
                    layer,
                    adjustment.mask,
                    adjustment,
                )
            else:
                adjustment.mask.mask_uuid = ""
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        _clear_layer_tree_selection(settings)
        adjustment.mask.ui_selected = True
        settings.layer_selection_anchor_uuid = _layer_tree_item_key(
            "ADJUSTMENT_MASK",
            str(adjustment.mask.mask_uuid),
        )
        return {"FINISHED"}


class PP_OT_layer_mask_select(bpy.types.Operator):
    """Layer Mask UI 선택과 더블클릭 활성화를 분리해 처리합니다."""

    bl_idname = "projection_paint.layer_mask_select"
    bl_label = "Select Layer Mask"
    bl_options = {"INTERNAL"}

    layer_uuid: StringProperty(options={"HIDDEN"})
    mask_uuid: StringProperty(options={"HIDDEN"})

    def execute(self, context):
        """스크립트 호출에서는 대상 Layer Mask만 선택하고 활성화합니다."""
        settings = context.scene.projection_paint
        layer = _find_layer(settings, self.layer_uuid)
        if (
            layer is None
            or not _layer_editable(settings, layer)
            or str(layer.mask.mask_uuid) != str(self.mask_uuid)
        ):
            return {"CANCELLED"}
        try:
            selected = _select_and_activate_layer_tree_item(
                context,
                _layer_tree_item_key("LAYER_MASK", str(layer.mask.mask_uuid)),
                ctrl=False,
                shift=False,
            )
        except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        return {"FINISHED"} if selected else {"CANCELLED"}

    def invoke(self, context, event):
        """Layer Mask 행을 선택하고 무수정 더블클릭에서만 활성화합니다."""
        settings = context.scene.projection_paint
        layer = _find_layer(settings, self.layer_uuid)
        if (
            layer is None
            or not _layer_editable(settings, layer)
            or str(layer.mask.mask_uuid) != str(self.mask_uuid)
        ):
            return {"CANCELLED"}
        ctrl, shift = _layer_ui_modifiers(context, event)
        try:
            selected = _select_layer_tree_item_from_ui(
                context,
                _layer_tree_item_key("LAYER_MASK", str(layer.mask.mask_uuid)),
                ctrl=ctrl,
                shift=shift,
            )
        except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        return {"FINISHED"} if selected else {"CANCELLED"}


class PP_OT_adjustment_mask_select(bpy.types.Operator):
    """Adjustment Mask UI 선택과 더블클릭 활성화를 분리해 처리합니다."""

    bl_idname = "projection_paint.adjustment_mask_select"
    bl_label = "Select Adjustment Mask"
    bl_options = {"INTERNAL"}

    layer_uuid: StringProperty(options={"HIDDEN"})
    adjustment_uuid: StringProperty(options={"HIDDEN"})
    mask_uuid: StringProperty(options={"HIDDEN"})

    def execute(self, context):
        """스크립트 호출에서는 대상 Adjustment Mask만 선택하고 활성화합니다."""
        settings = context.scene.projection_paint
        layer = _find_layer(settings, self.layer_uuid)
        index = (
            _find_adjustment_index(layer, self.adjustment_uuid)
            if layer is not None
            else -1
        )
        if index < 0 or not _layer_editable(settings, layer):
            return {"CANCELLED"}
        adjustment = layer.adjustments[index]
        if str(adjustment.mask.mask_uuid) != str(self.mask_uuid):
            return {"CANCELLED"}
        try:
            selected = _select_and_activate_layer_tree_item(
                context,
                _layer_tree_item_key(
                    "ADJUSTMENT_MASK",
                    str(adjustment.mask.mask_uuid),
                ),
                ctrl=False,
                shift=False,
            )
        except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        return {"FINISHED"} if selected else {"CANCELLED"}

    def invoke(self, context, event):
        """Adjustment Mask를 선택하고 무수정 더블클릭에서만 활성화합니다."""
        settings = context.scene.projection_paint
        layer = _find_layer(settings, self.layer_uuid)
        index = (
            _find_adjustment_index(layer, self.adjustment_uuid)
            if layer is not None
            else -1
        )
        if index < 0 or not _layer_editable(settings, layer):
            return {"CANCELLED"}
        adjustment = layer.adjustments[index]
        if str(adjustment.mask.mask_uuid) != str(self.mask_uuid):
            return {"CANCELLED"}
        ctrl, shift = _layer_ui_modifiers(context, event)
        try:
            selected = _select_layer_tree_item_from_ui(
                context,
                _layer_tree_item_key(
                    "ADJUSTMENT_MASK",
                    str(adjustment.mask.mask_uuid),
                ),
                ctrl=ctrl,
                shift=shift,
            )
        except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        return {"FINISHED"} if selected else {"CANCELLED"}


class PP_OT_layer_mask_remove(bpy.types.Operator):
    """Layer Mask와 애드온 소유 Image·Node를 제거합니다."""

    bl_idname = "projection_paint.layer_mask_remove"
    bl_label = "Delete Layer Mask"
    bl_options = {"REGISTER", "UNDO"}

    layer_uuid: StringProperty(options={"HIDDEN"})

    def execute(self, context):
        """UUID가 있는 Layer Mask 자원을 정리하고 Layer 합성을 복구합니다."""
        layer = _find_layer(context.scene.projection_paint, self.layer_uuid)
        if (
            layer is None
            or not _layer_editable(context.scene.projection_paint, layer)
            or not str(layer.mask.mask_uuid or "")
        ):
            return {"CANCELLED"}
        from .layer_shader import remove_mask_resources

        remove_mask_resources(context.scene, layer, layer.mask)
        _reconcile_if_ready(context)
        return {"FINISHED"}


class PP_OT_adjustment_mask_remove(bpy.types.Operator):
    """Adjustment Mask와 애드온 소유 Image·Node를 제거합니다."""

    bl_idname = "projection_paint.adjustment_mask_remove"
    bl_label = "Delete Adjustment Mask"
    bl_options = {"REGISTER", "UNDO"}

    layer_uuid: StringProperty(options={"HIDDEN"})
    adjustment_uuid: StringProperty(options={"HIDDEN"})

    def execute(self, context):
        """UUID가 있는 Adjustment Mask를 제거하고 전역 보정으로 복구합니다."""
        settings = context.scene.projection_paint
        layer = _find_layer(settings, self.layer_uuid)
        index = (
            _find_adjustment_index(layer, self.adjustment_uuid)
            if layer is not None
            else -1
        )
        if index < 0 or not _layer_editable(settings, layer):
            return {"CANCELLED"}
        adjustment = layer.adjustments[index]
        if not str(adjustment.mask.mask_uuid or ""):
            return {"CANCELLED"}
        from .layer_shader import remove_mask_resources

        remove_mask_resources(context.scene, layer, adjustment.mask, adjustment)
        _reconcile_if_ready(context)
        return {"FINISHED"}


class PP_OT_original_layer_restore(bpy.types.Operator):
    """삭제된 Original Layer를 현재 Layer Stack 계약으로 복원합니다."""

    bl_idname = "projection_paint.original_layer_restore"
    bl_label = "Restore Original Layer"
    bl_description = "현재 Layer Stack 해상도와 UDIM 타일에 맞춰 Original Layer를 복원합니다"
    bl_options = {"REGISTER", "UNDO"}

    draw = draw_wrapped_confirmation

    @classmethod
    def poll(cls, context):
        """Original Layer가 없고 안정된 일반 Layer 상태일 때만 복원을 허용합니다."""
        settings = getattr(getattr(context, "scene", None), "projection_paint", None)
        if (
            settings is None
            or settings.paint_material is None
            or str(settings.composite_state) != "NONE"
        ):
            return False
        return not any(
            bool(getattr(layer, "is_original_layer", False))
            for layer in settings.layers
        )

    def execute(self, context):
        """복원 Transaction 실패를 사용자에게 전달하고 기존 Stack은 복구 함수에 맡깁니다."""
        try:
            from .layer_shader import restore_original_layer
            from .paint_session import _resolution_label

            restored = restore_original_layer(context)
            image = restored.image
            size = (int(image.size[0]), int(image.size[1]))
            tile_count = max(1, len(tuple(getattr(image, "tiles", ()))))
            tile_label = f" · UDIM 타일 {tile_count}개" if tile_count > 1 else ""
            settings = context.scene.projection_paint
            settings.status = (
                f"Original Layer 복원 완료 · {_resolution_label(size)}{tile_label}"
            )
            settings.status_kind = "SUCCESS"
        except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        return {"FINISHED"}

    def invoke(self, context, event):
        """현재 Stack 계약으로 생성할 Original의 해상도와 최소 메모리를 확인합니다."""
        settings = context.scene.projection_paint
        try:
            from .composite_layer import composite_memory_label
            from .paint_session import _resolution_label

            image = next(
                layer.image
                for layer in settings.layers
                if layer.image is not None
            )
            size = (int(image.size[0]), int(image.size[1]))
            tile_count = max(1, len(tuple(getattr(image, "tiles", ()))))
            tile_label = f" · UDIM 타일 {tile_count}개" if tile_count > 1 else ""
            message = (
                f"현재 Stack과 같은 {_resolution_label(size)}{tile_label} Original Layer를 생성합니다. "
                f"{composite_memory_label(settings)}"
            )
        except (AttributeError, RuntimeError, TypeError, ValueError, StopIteration) as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        return invoke_wrapped_confirmation(
            self,
            context,
            title="Original Layer 복원",
            message=message,
            confirm_text="Original Layer 복원",
            icon="STATUS_WARNING",
        )


class PP_OT_mask_preview_toggle(bpy.types.Operator):
    """선택 Mask 원본을 Material Base Color에 흑백으로 단독 표시합니다."""

    bl_idname = "projection_paint.mask_preview_toggle"
    bl_label = "Toggle Mask Preview"
    bl_description = "선택 Mask를 흑백 표시하거나 전체 Layer 합성으로 돌아갑니다"
    bl_options = {"INTERNAL", "UNDO"}

    layer_uuid: StringProperty(options={"HIDDEN"})
    mask_uuid: StringProperty(options={"HIDDEN"})
    adjustment_uuid: StringProperty(options={"HIDDEN"})

    @classmethod
    def poll(cls, context):
        """관리 Material과 Layer가 준비되었을 때만 Mask 미리보기를 허용합니다."""
        settings = getattr(getattr(context, "scene", None), "projection_paint", None)
        return bool(
            settings is not None
            and settings.paint_material is not None
            and len(settings.layers) > 0
        )

    def execute(self, context):
        """지정 Mask의 단독 미리보기 상태를 토글합니다."""
        settings = context.scene.projection_paint
        layer = _find_layer(settings, self.layer_uuid)
        mask_uuid = str(self.mask_uuid or self.adjustment_uuid)
        from .layer_shader import find_mask

        owner_layer, _adjustment, mask = find_mask(settings, mask_uuid)
        if (
            layer is None
            or not _layer_editable(settings, layer)
            or owner_layer is None
            or str(owner_layer.layer_uuid) != str(layer.layer_uuid)
            or mask is None
        ):
            return {"CANCELLED"}
        try:
            from .layer_shader import toggle_mask_preview

            toggle_mask_preview(
                context.scene,
                str(layer.layer_uuid),
                mask_uuid,
            )
        except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        return {"FINISHED"}


class PP_OT_layer_preview_toggle(bpy.types.Operator):
    """선택 Layer만 Mask·Adjustment를 포함해 Material Base Color에 표시합니다."""

    bl_idname = "projection_paint.layer_preview_toggle"
    bl_label = "Toggle Layer Preview"
    bl_description = "선택 Layer만 Mask·보정을 포함해 표시하거나 전체 합성으로 돌아갑니다"
    bl_options = {"INTERNAL", "UNDO"}

    layer_uuid: StringProperty(options={"HIDDEN"})

    @classmethod
    def poll(cls, context):
        """관리 Material과 Layer가 준비되었을 때만 Layer 미리보기를 허용합니다."""
        settings = getattr(getattr(context, "scene", None), "projection_paint", None)
        return bool(
            settings is not None
            and settings.paint_material is not None
            and len(settings.layers) > 0
        )

    def execute(self, context):
        """지정 Layer 개인 단독 보기를 전역 체크 그룹에서 토글합니다."""
        settings = context.scene.projection_paint
        layer = _find_layer(settings, self.layer_uuid)
        if layer is None or not _layer_editable(settings, layer):
            return {"CANCELLED"}
        try:
            from .layer_shader import toggle_layer_preview

            toggle_layer_preview(context.scene, str(layer.layer_uuid))
        except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        return {"FINISHED"}


class PP_OT_layer_adjustment_remove(bpy.types.Operator):
    """지정한 Layer 보조 아이콘과 저장된 UI 값을 제거합니다."""

    bl_idname = "projection_paint.layer_adjustment_remove"
    bl_label = "Delete Layer Item"
    bl_description = "이 보정 또는 Mask 항목을 삭제합니다"
    bl_options = {"REGISTER", "UNDO"}

    layer_uuid: StringProperty(options={"HIDDEN"})
    adjustment_uuid: StringProperty(options={"HIDDEN"})

    @classmethod
    def poll(cls, context):
        """하나 이상의 논리 Paint Layer가 있을 때만 삭제를 허용합니다."""
        settings = getattr(getattr(context, "scene", None), "projection_paint", None)
        return bool(settings is not None and len(settings.layers) > 0)

    def execute(self, context):
        """UUID로 지정한 보조 항목을 제거하고 선택 Index를 보정합니다."""
        settings = context.scene.projection_paint
        layer = _find_layer(settings, self.layer_uuid)
        if layer is None or not _layer_editable(settings, layer):
            return {"CANCELLED"}
        index = _find_adjustment_index(layer, self.adjustment_uuid)
        return (
            {"FINISHED"}
            if _remove_adjustment(layer, index, context)
            else {"CANCELLED"}
        )


def _layer_tree_delete_item(
    settings,
    item: _LayerTreeSelectionItem,
) -> _LayerTreeDeleteItem:
    """현재 선택 행을 UUID 권위의 삭제 Snapshot으로 변환합니다."""

    layer, adjustment, value = _layer_tree_item_data(settings, item)
    if item.kind == "LAYER":
        item_uuid = str(layer.layer_uuid or "")
    elif item.kind == "LAYER_MASK":
        item_uuid = str(layer.mask.mask_uuid or "")
    elif item.kind == "ADJUSTMENT":
        item_uuid = str(adjustment.adjustment_uuid or "")
    else:
        item_uuid = str(adjustment.mask.mask_uuid or "")
    return _LayerTreeDeleteItem(
        key=str(item.key),
        kind=str(item.kind),
        layer_uuid=str(layer.layer_uuid or ""),
        item_uuid=item_uuid,
        adjustment_uuid=(
            str(adjustment.adjustment_uuid or "")
            if adjustment is not None
            else ""
        ),
        layer_index=int(item.layer_index),
        adjustment_index=int(item.adjustment_index),
    )


def _selected_layer_tree_delete_items(settings) -> tuple[_LayerTreeDeleteItem, ...]:
    """선택된 모든 트리 항목을 화면 Index와 UUID Snapshot으로 반환합니다."""

    return tuple(
        _layer_tree_delete_item(settings, item)
        for item in _layer_tree_selection_items(
            settings,
            visible_only=False,
            editable_only=False,
        )
        if _layer_tree_item_selected(settings, item)
    )


def layer_tree_selected_item_count(settings) -> int:
    """편집 잠금 여부와 무관하게 저장된 UI 선택 항목 수를 반환합니다."""

    return len(_selected_layer_tree_delete_items(settings))


def _normalize_layer_tree_delete_items(
    items: tuple[_LayerTreeDeleteItem, ...],
) -> tuple[_LayerTreeDeleteItem, ...]:
    """선택된 부모가 소유한 Layer·Adjustment 자식 삭제를 중복 목록에서 제외합니다."""

    selected_layers = {
        item.layer_uuid for item in items if item.kind == "LAYER"
    }
    selected_adjustments = {
        (item.layer_uuid, item.item_uuid)
        for item in items
        if item.kind == "ADJUSTMENT"
    }
    normalized = []
    for item in items:
        if item.kind != "LAYER" and item.layer_uuid in selected_layers:
            continue
        if (
            item.kind == "ADJUSTMENT_MASK"
            and (item.layer_uuid, item.adjustment_uuid) in selected_adjustments
        ):
            continue
        normalized.append(item)
    depth = {
        "LAYER": 1,
        "ADJUSTMENT": 2,
        "LAYER_MASK": 3,
        "ADJUSTMENT_MASK": 3,
    }
    return tuple(
        sorted(
            normalized,
            key=lambda item: (
                -depth[item.kind],
                -item.layer_index,
                -item.adjustment_index,
            ),
        )
    )


def _resolve_layer_tree_delete_item(settings, item: _LayerTreeDeleteItem):
    """고정 UUID로 현재 Layer·Adjustment·Mask를 다시 찾아 Index 이동을 피합니다."""

    layer = _find_layer(settings, item.layer_uuid)
    if layer is None:
        return None
    layer_index = next(
        (
            index
            for index, candidate in enumerate(settings.layers)
            if str(candidate.layer_uuid) == item.layer_uuid
        ),
        -1,
    )
    if item.kind == "LAYER":
        return layer_index, -1, layer, None, layer
    if item.kind == "LAYER_MASK":
        if str(layer.mask.mask_uuid or "") != item.item_uuid:
            return None
        return layer_index, -1, layer, None, layer.mask
    adjustment_index = _find_adjustment_index(layer, item.adjustment_uuid)
    if adjustment_index < 0:
        return None
    adjustment = layer.adjustments[adjustment_index]
    if item.kind == "ADJUSTMENT":
        if str(adjustment.adjustment_uuid or "") != item.item_uuid:
            return None
        return layer_index, adjustment_index, layer, adjustment, adjustment
    if str(adjustment.mask.mask_uuid or "") != item.item_uuid:
        return None
    return layer_index, adjustment_index, layer, adjustment, adjustment.mask


def _validate_layer_tree_delete_items(
    settings,
    items: tuple[_LayerTreeDeleteItem, ...],
) -> None:
    """복수 삭제 전 모든 대상의 존재·Composite 보호 계약을 한 번에 검증합니다."""

    if not items:
        raise RuntimeError("삭제할 Layer 트리 선택 항목이 없습니다.")
    for item in items:
        resolved = _resolve_layer_tree_delete_item(settings, item)
        if resolved is None:
            raise RuntimeError("선택한 Layer 항목이 변경되어 삭제를 중단했습니다.")
        _layer_index, _adjustment_index, layer, _adjustment, _value = resolved
        if str(getattr(layer, "layer_role", "NORMAL")) == "COMPOSITE":
            raise RuntimeError("Composite Layer는 'Composite 해제'로만 삭제할 수 있습니다.")
        if not _layer_editable(settings, layer):
            raise RuntimeError("Composite 활성 중 잠긴 Layer 항목은 삭제할 수 없습니다.")


def _delete_requires_fresh_layer(
    settings,
    items: tuple[_LayerTreeDeleteItem, ...],
) -> bool:
    """선택 삭제가 마지막 쓰기 가능 Paint Layer를 없애는지 반환합니다."""

    selected_layers = {
        item.layer_uuid for item in items if item.kind == "LAYER"
    }
    if not selected_layers:
        return False
    if len(settings.layers) == 1:
        return str(settings.layers[0].layer_uuid) in selected_layers
    from .layer_shader import original_layer_is_read_only

    writable = {
        str(layer.layer_uuid)
        for layer in settings.layers
        if not original_layer_is_read_only(settings, layer)
    }
    return bool(writable and writable.issubset(selected_layers))


def _layer_tree_item_for_key(settings, item_key: str):
    """현재 트리 전체에서 UUID 기반 항목 키와 일치하는 행을 반환합니다."""

    return next(
        (
            item
            for item in _layer_tree_selection_items(
                settings,
                visible_only=False,
                editable_only=False,
            )
            if item.key == str(item_key)
        ),
        None,
    )


def _delete_active_fallback_key(
    settings,
    items: tuple[_LayerTreeDeleteItem, ...],
) -> str:
    """삭제 전 활성 항목이 사라질 때 계약에 맞는 소유자 또는 이웃 키를 계산합니다."""

    active = active_layer_tree_item(settings)
    active_key = active_layer_tree_item_key(settings)
    if active is None:
        return ""
    kind, layer, adjustment = active
    deleted_layers = {
        item.layer_uuid for item in items if item.kind == "LAYER"
    }
    deleted_keys = {item.key for item in items}
    layer_uuid = str(layer.layer_uuid)
    active_removed_by_parent = bool(
        kind == "ADJUSTMENT_MASK"
        and adjustment is not None
        and _layer_tree_item_key(
            "ADJUSTMENT",
            str(adjustment.adjustment_uuid),
        )
        in deleted_keys
    )
    if (
        layer_uuid not in deleted_layers
        and active_key not in deleted_keys
        and not active_removed_by_parent
    ):
        return active_key
    if layer_uuid not in deleted_layers:
        if kind == "ADJUSTMENT_MASK" and adjustment is not None:
            adjustment_key = _layer_tree_item_key(
                "ADJUSTMENT",
                str(adjustment.adjustment_uuid),
            )
            if adjustment_key not in deleted_keys:
                return adjustment_key
        return _layer_tree_item_key("LAYER", layer_uuid)
    layer_uuids = tuple(str(candidate.layer_uuid) for candidate in settings.layers)
    active_index = layer_uuids.index(layer_uuid)
    for candidate_uuid in layer_uuids[active_index + 1 :]:
        if candidate_uuid not in deleted_layers:
            return _layer_tree_item_key("LAYER", candidate_uuid)
    for candidate_uuid in reversed(layer_uuids[:active_index]):
        if candidate_uuid not in deleted_layers:
            return _layer_tree_item_key("LAYER", candidate_uuid)
    return ""


def _activate_layer_tree_key(context, item_key: str) -> bool:
    """삭제 뒤 살아 있는 UUID 트리 키를 실제 Paint 활성 대상으로 복구합니다."""

    item = _layer_tree_item_for_key(context.scene.projection_paint, item_key)
    if item is None:
        return False
    _activate_layer_tree_item(context, item)
    return True


def _projection_paint_layer_tree_context(context) -> bool:
    """Delete 키가 Projection Paint 레이어 트리 위에서 발생했는지 확인합니다."""

    scene = getattr(context, "scene", None)
    settings = getattr(scene, "projection_paint", None)
    window = getattr(context, "window", None)
    area = getattr(context, "area", None)
    region = getattr(context, "region", None)
    if not bool(
        settings is not None
        and window is not None
        and area is not None
        and str(getattr(area, "type", "")) == "VIEW_3D"
        and region is not None
        and str(getattr(region, "type", "")) == "UI"
        and _layer_ui_workspace_key(getattr(window, "workspace", None))
        == _layer_ui_workspace_key(getattr(settings, "paint_workspace", None))
    ):
        return False
    marker = getattr(context, "projection_paint_layer_tree", None)
    if marker is not None:
        try:
            if int(marker.as_pointer()) == int(settings.as_pointer()):
                return True
        except (AttributeError, ReferenceError, RuntimeError, TypeError):
            if marker is settings:
                return True
    identifier = str(
        getattr(
            getattr(getattr(context, "button_operator", None), "bl_rna", None),
            "identifier",
            "",
        )
        or ""
    )
    return identifier in {
        "PROJECTION_PAINT_OT_layer_select",
        "PROJECTION_PAINT_OT_layer_mask_select",
        "PROJECTION_PAINT_OT_layer_adjustment_select",
        "PROJECTION_PAINT_OT_adjustment_mask_select",
    }


class PP_OT_layer_adjustment_remove_hovered(bpy.types.Operator):
    """Projection Paint 레이어 트리의 선택 삭제만 Delete 키로 전달합니다."""

    bl_idname = "projection_paint.layer_adjustment_remove_hovered"
    bl_label = "Delete Selected Layer Items"
    bl_description = "Projection Paint 레이어 트리에서 선택한 항목을 삭제합니다"
    bl_options = {"INTERNAL"}

    @classmethod
    def poll(cls, context):
        """정확한 레이어 트리 문맥에 선택 항목이 있을 때만 Delete를 소비합니다."""

        settings = getattr(getattr(context, "scene", None), "projection_paint", None)
        return bool(
            settings is not None
            and layer_tree_selected_item_count(settings) > 0
            and _projection_paint_layer_tree_context(context)
        )

    def execute(self, context):
        """실행 순간에도 문맥을 확인하고 선택 삭제 Operator에 처리를 맡깁니다."""

        if not self.poll(context):
            return {"PASS_THROUGH"}
        return bpy.ops.projection_paint.layer_remove("EXEC_DEFAULT")

    def invoke(self, context, _event):
        """문맥이 달라졌으면 통과하고 유효하면 선택 삭제 확인 흐름을 시작합니다."""

        if not self.poll(context):
            return {"PASS_THROUGH"}
        return bpy.ops.projection_paint.layer_remove("INVOKE_DEFAULT")


class PP_OT_layer_adjustment_add(bpy.types.Operator):
    """선택한 논리 Paint Layer에 보조 UI 항목을 추가합니다."""

    bl_idname = "projection_paint.layer_adjustment_add"
    bl_label = "Add Layer Adjustment"
    bl_description = "선택한 Paint Layer에 색상 보정을 추가합니다"
    bl_options = {"REGISTER", "UNDO"}

    layer_uuid: StringProperty(options={"HIDDEN"})
    adjustment_type: EnumProperty(
        name="Adjustment",
        items=_ADJUSTMENT_ITEMS,
    )

    @classmethod
    def poll(cls, context):
        """하나 이상의 논리 Paint Layer가 있을 때만 추가를 허용합니다."""
        settings = getattr(getattr(context, "scene", None), "projection_paint", None)
        return bool(settings is not None and len(settings.layers) > 0)

    def execute(self, context):
        """대상 Layer를 선택하고 중복되지 않은 보조 항목을 추가합니다."""
        settings = context.scene.projection_paint
        layer = _find_layer(settings, self.layer_uuid)
        if layer is None or not _layer_editable(settings, layer):
            self.report({"WARNING"}, "보조 항목을 추가할 Paint Layer가 없습니다.")
            return {"CANCELLED"}
        adjustment_type = str(self.adjustment_type)
        if adjustment_type in _adjustment_types(layer):
            self.report({"INFO"}, "해당 보조 UI가 이미 추가되어 있습니다.")
            return {"CANCELLED"}
        adjustment = layer.adjustments.add()
        adjustment.adjustment_type = adjustment_type
        adjustment.adjustment_uuid = uuid.uuid4().hex
        added_index = len(layer.adjustments) - 1
        layer.active_adjustment_index = added_index
        settings.active_mask_uuid = ""
        settings.active_layer_index = next(
            index
            for index, candidate in enumerate(settings.layers)
            if str(candidate.layer_uuid) == str(layer.layer_uuid)
        )
        try:
            _reconcile_if_ready(context)
            if layer.image is not None:
                from .layer_shader import activate_layer_paint_image

                activate_layer_paint_image(context, str(layer.layer_uuid))
                layer.active_adjustment_index = added_index
        except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
            layer.adjustments.remove(len(layer.adjustments) - 1)
            layer.active_adjustment_index = min(
                layer.active_adjustment_index,
                len(layer.adjustments) - 1,
            )
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        _clear_layer_tree_selection(settings)
        adjustment.ui_selected = True
        settings.layer_selection_anchor_uuid = _layer_tree_item_key(
            "ADJUSTMENT",
            str(adjustment.adjustment_uuid),
        )
        return {"FINISHED"}


class PP_OT_layer_add(bpy.types.Operator):
    """현재 Scene의 논리 Paint Layer 목록 맨 위에 새 항목을 추가합니다."""

    bl_idname = "projection_paint.layer_add"
    bl_label = "Add Paint Layer"
    bl_description = "새 Paint Layer 항목을 추가합니다"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        """고유 UUID와 기본 이름을 가진 논리 Layer를 맨 위에 추가합니다."""
        settings = context.scene.projection_paint
        if str(settings.composite_state) in {"CREATING", "RECOMPOSITING", "RELEASING", "STALE"}:
            self.report({"ERROR"}, "Composite 처리 중이거나 손상된 상태에서는 Layer를 추가할 수 없습니다.")
            return {"CANCELLED"}
        layer_name = _next_layer_name(settings.layers)
        try:
            from .layer_shader import add_layer_transaction

            add_layer_transaction(context, layer_name, uuid.uuid4().hex)
        except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        active_index = int(settings.active_layer_index)
        if 0 <= active_index < len(settings.layers):
            active_layer = settings.layers[active_index]
            _clear_layer_tree_selection(settings)
            active_layer.ui_selected = True
            settings.layer_selection_anchor_uuid = str(active_layer.layer_uuid)
        return {"FINISHED"}


class PP_OT_composite_layer_create(bpy.types.Operator):
    """현재 보이는 Base Color Layer Stack을 하나의 Composite 작업 Layer로 베이크합니다."""

    bl_idname = "projection_paint.composite_layer_create"
    bl_label = "Create Composite Layer"
    bl_description = "현재 보이는 Layer 결과를 베이크하고 Composite 작업 Layer를 만듭니다"
    bl_options = {"REGISTER", "UNDO"}

    _job = None
    draw = draw_wrapped_confirmation

    @classmethod
    def poll(cls, context):
        """관리 Material과 최초 또는 재합성 가능한 Layer Stack이 준비됐을 때만 생성을 허용합니다."""
        from .texture_export import texture_export_running

        settings = getattr(getattr(context, "scene", None), "projection_paint", None)
        return bool(
            settings is not None
            and not texture_export_running()
            and settings.paint_material is not None
            and len(settings.layers) > 0
            and str(settings.composite_state) in {"NONE", "ACTIVE"}
        )

    def invoke(self, context, event):
        """고해상도 최초·재합성 Bake 전에 필요한 Image 메모리 경고를 표시합니다."""
        try:
            from .composite_layer import composite_memory_label, composite_recompose_summary

            _restore_layer_ui_pointer_selection(context, event)
            settings = context.scene.projection_paint
            if str(settings.composite_state) == "ACTIVE":
                detail = composite_recompose_summary(settings)
                return invoke_wrapped_confirmation(
                    self,
                    context,
                    title="Composite 다시 생성",
                    message=detail,
                    confirm_text="Composite 다시 생성",
                    icon="ERROR",
                )

            return invoke_wrapped_confirmation(
                self,
                context,
                title="Composite Layer 생성",
                message=(
                    "현재 보이는 Layer를 하나의 Composite Layer로 생성합니다. "
                    f"{composite_memory_label(context.scene.projection_paint)}"
                ),
                confirm_text="Composite Layer 생성",
                icon="STATUS_WARNING",
            )
        except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}

    def execute(self, context):
        """GUI는 Native Bake 시작 전 읽기 전용 Overlay를 열고 결과만 다음 Timer에 처리합니다."""
        _restore_layer_ui_pointer_selection(context)
        if bpy.app.background or context.window is None:
            try:
                from .composite_layer import create_composite_layer

                create_composite_layer(context)
            except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
                self.report({"ERROR"}, str(exc))
                return {"CANCELLED"}
            return {"FINISHED"}
        job = None
        timer = None
        timer_attached = False
        try:
            from .composite_layer import (
                invoke_native_bake,
                prepare_composite_image,
                prepare_native_bake,
                prepare_recomposite_native_source,
                start_composite_layer_creation,
                start_composite_native_progress,
            )
            from .composite_progress import attach_composite_progress_timer

            job = start_composite_layer_creation(context)
            start_composite_native_progress(context, job)
            timer = context.window_manager.event_timer_add(0.1, window=context.window)
            attach_composite_progress_timer(job.progress, timer)
            timer_attached = True
            prepare_composite_image(job)
            prepare_recomposite_native_source(job)
            if job.rolled_back:
                raise RuntimeError(job.error_message or "Composite UDIM Image 생성에 실패했습니다.")
            stage = (
                "FLATTEN_COLOR"
                if job.kind == "RECOMPOSITE" and not job.reuse_old_composite_image
                else "COLOR"
            )
            context.scene.projection_paint.status = "Composite Layer Native Bake를 시작하고 있습니다."
            context.scene.projection_paint.status_kind = "RUNNING"
            prepare_native_bake(context, job, stage)
            invoke_native_bake(context, job)
            self._job = job
            context.window_manager.modal_handler_add(self)
        except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
            if job is not None:
                from .composite_layer import (
                    _fail_composite_bake,
                    release_composite_create_job,
                )

                _fail_composite_bake(context, job, exc)
                if timer is not None and not timer_attached:
                    context.window_manager.event_timer_remove(timer)
                release_composite_create_job(job)
            elif timer is not None:
                context.window_manager.event_timer_remove(timer)
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        return {"RUNNING_MODAL"}

    def modal(self, context, event):
        """Native Bake 중에는 입력을 전달하고 종료 확인 뒤 다음 Timer에서만 결과를 처리합니다."""
        job = self._job
        if job is None:
            return {"CANCELLED"}
        if event.type != "TIMER":
            return {"PASS_THROUGH"}
        try:
            from .composite_layer import (
                _complete_composite_bake,
                _fail_composite_bake,
                _native_bake_is_running,
                finalize_native_bake,
                advance_composite_native_sequence,
                invoke_native_bake,
                mark_native_bake_job_stopped,
                prepare_native_bake,
                rollback_native_bake,
            )
            from .progress_overlay import request_progress_overlay_redraw

            if job.phase == "NATIVE_BAKING":
                if context.scene != job.scene:
                    if _native_bake_is_running():
                        job.abort_after_bake = True
                        return {"RUNNING_MODAL"}
                    if not job.native_bake_job_stopped:
                        job.native_bake_job_stopped = True
                        return {"RUNNING_MODAL"}
                    rollback_native_bake(
                        context,
                        job,
                        RuntimeError("Composite Native Bake 중 활성 Scene이 변경되었습니다."),
                    )
                elif _native_bake_is_running():
                    request_progress_overlay_redraw()
                    return {"RUNNING_MODAL"}
                elif not job.native_bake_job_stopped:
                    mark_native_bake_job_stopped(job)
                    request_progress_overlay_redraw()
                    return {"RUNNING_MODAL"}
                elif job.abort_after_bake:
                    rollback_native_bake(
                        context,
                        job,
                        RuntimeError("수명주기 중단 요청 뒤 Composite Native Bake를 롤백합니다."),
                    )
                elif job.native_bake_outcome == "CANCELLED":
                    rollback_native_bake(
                        context,
                        job,
                        RuntimeError("Composite Native Bake가 취소되었습니다."),
                    )
                elif job.native_bake_outcome != "COMPLETE":
                    rollback_native_bake(
                        context,
                        job,
                        RuntimeError("Composite Native Bake 결과 신호를 확인하지 못했습니다."),
                    )
                else:
                    completed_stage = finalize_native_bake(context, job)
                    context.scene.projection_paint.status = "Composite Layer 다음 순차 단계를 처리하고 있습니다."
                    context.scene.projection_paint.status_kind = "RUNNING"
                    advance_composite_native_sequence(context, job, completed_stage)
            elif job.phase == "RESULT":
                pass
            elif context.scene != job.scene:
                _fail_composite_bake(
                    context,
                    job,
                    RuntimeError("Composite 후처리 중 활성 Scene이 변경되었습니다."),
                )
            else:
                _fail_composite_bake(
                    context,
                    job,
                    RuntimeError("Composite Native Bake 상태가 유효하지 않습니다."),
                )
        except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
            if job.phase == "NATIVE_BAKING" and _native_bake_is_running():
                job.abort_after_bake = True
                return {"RUNNING_MODAL"}
            _fail_composite_bake(context, job, exc)
        if job.phase == "RESULT":
            from .composite_layer import release_composite_create_job

            release_composite_create_job(job)
            self._job = None
            if job.committed:
                if job.ui_restore_warning:
                    self.report({"WARNING"}, job.ui_restore_warning)
                else:
                    self.report({"INFO"}, "Composite Layer 생성 완료")
                return {"FINISHED"}
            self.report({"ERROR"}, job.error_message or "Composite Layer 생성에 실패했습니다.")
            return {"CANCELLED"}
        return {"RUNNING_MODAL"}


class PP_OT_composite_layer_release(bpy.types.Operator):
    """Composite 자체 제거 범위를 경고하고 Source Stack과 독립 Layer를 복구합니다."""

    bl_idname = "projection_paint.composite_layer_release"
    bl_label = "Release Composite Layer"
    bl_description = "Composite 자체를 삭제하고 원본 Source와 독립 일반 Layer를 유지합니다"
    bl_options = {"REGISTER", "UNDO"}

    draw = draw_wrapped_confirmation

    @classmethod
    def poll(cls, context):
        """검증 가능한 활성 Composite 세션에서만 해제를 허용합니다."""
        settings = getattr(getattr(context, "scene", None), "projection_paint", None)
        return bool(settings is not None and str(settings.composite_state) == "ACTIVE")

    def invoke(self, context, event):
        """Composite 직접 편집 결과의 삭제와 독립 일반 Layer 보존 계약을 안내합니다."""
        try:
            from .composite_layer import composite_release_summary

            composite_name, _compat_empty = composite_release_summary(
                context.scene.projection_paint
            )
            return invoke_wrapped_confirmation(
                self,
                context,
                title="Composite Layer 해제",
                message=(
                    f"{composite_name} 자체의 Paint·Clone·Mask·Adjustment 결과는 함께 제거됩니다. "
                    "별도로 추가한 일반 Layer는 유지되지만, 재합성 Source로 선택하면 "
                    "기존 규칙대로 Bake 후 소비됩니다."
                ),
                confirm_text="Composite Layer 해제",
                icon="ERROR",
            )
        except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}

    def execute(self, context):
        """원본 Shader Chain 복구 검증 뒤 동일 세션 Composite 자원만 삭제합니다."""
        try:
            from .composite_layer import release_composite_layer

            release_composite_layer(context)
        except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        return {"FINISHED"}


class PP_OT_layer_remove(bpy.types.Operator):
    """UI에서 선택한 Layer 트리 항목들을 UUID 기준 한 작업으로 제거합니다."""

    bl_idname = "projection_paint.layer_remove"
    bl_label = "Remove Selected Layer Items"
    bl_description = "선택한 Layer, Mask 또는 보정 항목을 모두 제거합니다"
    bl_options = {"REGISTER", "UNDO"}

    draw = draw_wrapped_confirmation

    reset_width: IntProperty(default=0, min=0, options={"HIDDEN", "SKIP_SAVE"})
    reset_height: IntProperty(default=0, min=0, options={"HIDDEN", "SKIP_SAVE"})
    delete_item_keys: StringProperty(default="", options={"HIDDEN", "SKIP_SAVE"})
    active_fallback_key: StringProperty(default="", options={"HIDDEN", "SKIP_SAVE"})

    @classmethod
    def poll(cls, context):
        """하나 이상의 Layer 트리 UI 선택이 있을 때만 빼기 버튼을 활성화합니다."""
        settings = getattr(getattr(context, "scene", None), "projection_paint", None)
        return bool(
            settings is not None
            and layer_tree_selected_item_count(settings) > 0
        )

    def _capture_delete_snapshot(self, settings) -> tuple[_LayerTreeDeleteItem, ...]:
        """현재 UI 선택을 정규화해 확인창 이후에도 쓸 UUID Snapshot으로 고정합니다."""

        items = _normalize_layer_tree_delete_items(
            _selected_layer_tree_delete_items(settings)
        )
        _validate_layer_tree_delete_items(settings, items)
        self.delete_item_keys = "\n".join(item.key for item in items)
        self.active_fallback_key = _delete_active_fallback_key(settings, items)
        return items

    def _delete_snapshot(self, settings) -> tuple[_LayerTreeDeleteItem, ...]:
        """Operator에 고정된 UUID 키를 현재 Scene의 삭제 Snapshot으로 다시 해석합니다."""

        keys = tuple(
            key for key in str(self.delete_item_keys or "").splitlines() if key
        )
        if not keys:
            return self._capture_delete_snapshot(settings)
        available = {
            item.key: item
            for item in (
                _layer_tree_delete_item(settings, selection_item)
                for selection_item in _layer_tree_selection_items(
                    settings,
                    visible_only=False,
                    editable_only=False,
                )
            )
        }
        if any(key not in available for key in keys):
            raise RuntimeError("확인 중 Layer 트리 대상이 변경되어 삭제를 중단했습니다.")
        items = tuple(available[key] for key in keys)
        _validate_layer_tree_delete_items(settings, items)
        return _normalize_layer_tree_delete_items(items)

    def _capture_reset_resolution(self, settings) -> tuple[int, int]:
        """마지막 Paint Layer 교체에 사용할 확정 해상도를 계산해 저장합니다."""

        from .material_setup import pending_overlay_resolution_change
        from .paint_session import _selected_overlay_resolution

        width, height = _selected_overlay_resolution(settings)
        change = pending_overlay_resolution_change(
            settings.paint_target,
            width,
            height,
            use_base_resolution=bool(settings.use_base_resolution),
        )
        desired = (
            change[1]
            if change is not None
            else tuple(int(value) for value in settings.overlay_image.size)
        )
        self.reset_width, self.reset_height = desired
        return int(desired[0]), int(desired[1])

    def invoke(self, context, event):
        """버튼 직전 선택을 복원·고정하고 Layer 포함 시 확인창을 정확히 한 번 엽니다."""

        _restore_layer_ui_pointer_selection(context, event)
        settings = context.scene.projection_paint
        try:
            items = self._capture_delete_snapshot(settings)
        except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
            self.report({"WARNING"}, str(exc))
            return {"CANCELLED"}
        selected_layers = tuple(item for item in items if item.kind == "LAYER")
        if not selected_layers:
            return self.execute(context)
        if (
            settings.paint_material is not None
            and _delete_requires_fresh_layer(settings, items)
        ):
            try:
                from .paint_session import _resolution_label

                desired = self._capture_reset_resolution(settings)
            except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
                self.report({"ERROR"}, str(exc))
                return {"CANCELLED"}
            return invoke_wrapped_confirmation(
                self,
                context,
                title="마지막 Paint Layer 새로 만들기",
                message=_last_layer_reset_message(
                    f"선택한 Layer {len(selected_layers)}개",
                    _resolution_label(desired),
                ),
                confirm_text=f"새 {_resolution_label(desired)} Layer 1 생성",
                icon="STATUS_WARNING",
            )
        return invoke_wrapped_confirmation(
            self,
            context,
            title="선택한 Layer 삭제",
            message=(
                f"선택한 Layer {len(selected_layers)}개와 그 하위 Mask·보정 자원을 삭제합니다. "
                "되돌리기는 Blender Undo 기록이 있을 때만 가능합니다."
            ),
            confirm_text="선택한 Layer 삭제",
            icon="ERROR",
        )

    def _remove_item(self, context, item: _LayerTreeDeleteItem) -> None:
        """검증된 단일 UUID 대상을 깊은 자식부터 실제 자원과 함께 제거합니다."""

        settings = context.scene.projection_paint
        resolved = _resolve_layer_tree_delete_item(settings, item)
        if resolved is None:
            raise RuntimeError("삭제 중 Layer 트리 대상이 변경되어 작업을 중단했습니다.")
        layer_index, adjustment_index, layer, adjustment, value = resolved
        if item.kind in {"LAYER_MASK", "ADJUSTMENT_MASK"}:
            if settings.paint_material is not None:
                from .layer_shader import remove_mask_resources

                remove_mask_resources(
                    context.scene,
                    layer,
                    value,
                    adjustment,
                )
            else:
                value.image = None
                value.mask_uuid = ""
            return
        if item.kind == "ADJUSTMENT":
            if not _remove_adjustment(
                layer,
                adjustment_index,
                context,
                reconcile_graph=False,
            ):
                raise RuntimeError("선택한 Adjustment를 삭제하지 못했습니다.")
            return
        if settings.paint_material is not None:
            from .layer_shader import remove_layer_resources

            remove_layer_resources(context, item.layer_uuid)
            return
        settings.layers.remove(layer_index)

    def execute(self, context):
        """고정 UUID 대상을 한 Undo 연산에서 정규화된 깊이·내림차순으로 삭제합니다."""

        settings = context.scene.projection_paint
        try:
            items = self._delete_snapshot(settings)
            _validate_layer_tree_delete_items(settings, items)
            reset_required = bool(
                settings.paint_material is not None
                and _delete_requires_fresh_layer(settings, items)
            )
            if reset_required:
                from .paint_session import (
                    _resolution_label,
                    _selected_overlay_resolution,
                )
                from .layer_reset import replace_with_fresh_layer

                width, height = (
                    (int(self.reset_width), int(self.reset_height))
                    if int(self.reset_width) > 0 and int(self.reset_height) > 0
                    else _selected_overlay_resolution(settings)
                )
                replace_with_fresh_layer(
                    context,
                    width,
                    height,
                    use_base_resolution=(
                        bool(settings.use_base_resolution)
                        and (int(self.reset_width) == 0 or int(self.reset_height) == 0)
                    ),
                )
                label = _resolution_label((width, height))
                settings.status = f"새 {label} Layer 1 생성 완료"
                settings.status_kind = "SUCCESS"
            else:
                fallback_key = str(self.active_fallback_key or "")
                if not fallback_key:
                    fallback_key = _delete_active_fallback_key(settings, items)
                for item in items:
                    self._remove_item(context, item)
                _reconcile_if_ready(context)
                if not _activate_layer_tree_key(context, fallback_key):
                    if len(settings.layers) > 0:
                        _activate_layer_tree_item(
                            context,
                            _layer_tree_selection_items(
                                settings,
                                visible_only=False,
                                editable_only=False,
                            )[0],
                        )
            _clear_layer_tree_selection(settings)
            settings.layer_selection_anchor_uuid = ""
            _clear_layer_ui_double_click_candidate(
                _layer_ui_window_key(getattr(context, "window", None))
            )
        except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
            self.report({"WARNING"}, str(exc))
            return {"CANCELLED"}
        return {"FINISHED"}


class PP_OT_layer_move(bpy.types.Operator):
    """선택한 논리 Paint Layer를 목록에서 한 칸 위나 아래로 이동합니다."""

    bl_idname = "projection_paint.layer_move"
    bl_label = "Move Paint Layer"
    bl_description = "선택한 Paint Layer의 순서를 한 칸 변경합니다"
    bl_options = {"REGISTER", "UNDO"}

    direction: EnumProperty(
        name="Direction",
        items=(
            ("UP", "Up", "선택한 Layer를 한 칸 위로 이동합니다"),
            ("DOWN", "Down", "선택한 Layer를 한 칸 아래로 이동합니다"),
        ),
        default="UP",
    )

    @classmethod
    def poll(cls, context):
        """유효한 활성 Layer와 이동 가능한 목록이 있을 때만 연산을 허용합니다."""
        settings = getattr(getattr(context, "scene", None), "projection_paint", None)
        return bool(
            settings is not None
            and len(settings.layers) > 1
            and 0 <= int(settings.active_layer_index) < len(settings.layers)
        )

    def execute(self, context):
        """활성 Layer를 지정 방향의 인접 Index로 이동하고 선택을 따라갑니다."""
        settings = context.scene.projection_paint
        if str(settings.composite_state) != "NONE":
            self.report({"WARNING"}, "Composite 활성·처리·손상 상태에서는 Layer 순서를 변경할 수 없습니다.")
            return {"CANCELLED"}
        source_index = int(settings.active_layer_index)
        offset = -1 if self.direction == "UP" else 1
        target_index = source_index + offset
        if not 0 <= target_index < len(settings.layers):
            return {"CANCELLED"}
        settings.layers.move(source_index, target_index)
        settings.active_layer_index = target_index
        try:
            _reconcile_if_ready(context)
        except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
            settings.layers.move(target_index, source_index)
            settings.active_layer_index = source_index
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        return {"FINISHED"}


CLASSES = (
    PP_OT_layer_ui_pointer_tracker,
    PP_OT_block_multiselect_paint_stroke,
    PP_OT_layer_select,
    PP_OT_layer_rename,
    PP_OT_layer_toggle,
    PP_OT_layer_adjustment_select,
    PP_OT_adjustment_toggle,
    PP_OT_layer_mask_add,
    PP_OT_adjustment_mask_add,
    PP_OT_layer_mask_select,
    PP_OT_adjustment_mask_select,
    PP_OT_layer_mask_remove,
    PP_OT_adjustment_mask_remove,
    PP_OT_original_layer_restore,
    PP_OT_layer_preview_toggle,
    PP_OT_mask_preview_toggle,
    PP_OT_layer_adjustment_remove,
    PP_OT_layer_adjustment_remove_hovered,
    PP_OT_layer_adjustment_add,
    PP_OT_layer_add,
    PP_OT_composite_layer_create,
    PP_OT_composite_layer_release,
    PP_OT_layer_remove,
    PP_OT_layer_move,
)


__all__ = [
    "CLASSES",
    "active_layer_tree_item_key",
    "clear_layer_ui_runtime_state",
    "disable_layer_ui_event_tracking",
    "enable_layer_ui_event_tracking",
    "layer_ui_event_tracker_heartbeat",
    "layer_tree_selected_item_count",
    "layer_tree_selection_count",
    "PP_OT_block_multiselect_paint_stroke",
    "PP_OT_layer_adjustment_remove",
    "PP_OT_layer_adjustment_remove_hovered",
    "PP_OT_layer_adjustment_select",
    "PP_OT_adjustment_toggle",
    "PP_OT_layer_mask_add",
    "PP_OT_adjustment_mask_add",
    "PP_OT_layer_mask_select",
    "PP_OT_adjustment_mask_select",
    "PP_OT_layer_mask_remove",
    "PP_OT_adjustment_mask_remove",
    "PP_OT_original_layer_restore",
    "PP_OT_layer_preview_toggle",
    "PP_OT_mask_preview_toggle",
    "PP_OT_layer_adjustment_add",
    "PP_OT_layer_add",
    "PP_OT_composite_layer_create",
    "PP_OT_composite_layer_release",
    "PP_OT_layer_rename",
    "PP_OT_layer_select",
    "PP_OT_layer_toggle",
    "PP_OT_layer_move",
    "PP_OT_layer_remove",
    "_last_layer_reset_message",
    "active_layer_adjustment",
    "adjustment_row_selected",
    "adjustment_mask_row_selected",
    "adjustment_ui",
    "draw_layer_list_context_menu",
    "layer_row_selected",
    "layer_mask_row_selected",
    "layer_preview_active",
    "mask_preview_active",
]
