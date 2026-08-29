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


_layer_ui_click_history: dict[int, tuple[str, float]] = {}
_LAYER_UI_POINTER_EVENT_MAX_AGE_SECONDS = 0.5
_LAYER_UI_DUPLICATE_PRESS_MAX_AGE_SECONDS = 0.02
_LAYER_UI_POINTER_POSITION_TOLERANCE_PX = 5.0
_LAYER_UI_TRACKER_RETRY_SECONDS = 0.1
_layer_ui_event_tracking_enabled = True
_layer_ui_pointer_events: dict[int, "_LayerUIPointerEvent"] = {}
_layer_ui_pointer_releases: set[int] = set()
_layer_ui_tracked_windows: set[int] = set()


@dataclass(frozen=True)
class _LayerUIPointerEvent:
    """실제 LEFTMOUSE 입력에서 보존한 modifier와 후보 선택 스냅샷입니다."""

    value: str
    ctrl: bool
    shift: bool
    mouse_x: int
    mouse_y: int
    recorded_at: float
    scene_key: int = 0
    selected_layer_uuids: tuple[str, ...] = ()
    selection_anchor_uuid: str = ""
    click_history: tuple[str, float] | None = None


def _layer_ui_window_key(window) -> int:
    """창별 실제 포인터 입력 스냅샷을 위한 안정 키를 반환합니다."""
    try:
        return int(window.as_pointer()) if window is not None else 0
    except ReferenceError:
        return 0


def _layer_ui_scene_key(scene) -> int:
    """후보 선택을 기록한 Scene을 다른 Scene과 구분하는 안정 키를 반환합니다."""
    try:
        as_pointer = getattr(scene, "as_pointer", None)
        return int(as_pointer()) if callable(as_pointer) else id(scene)
    except ReferenceError:
        return 0


def _tag_layer_ui_redraw() -> None:
    """Timer에서 후보 선택을 바꾼 뒤 모든 현재 Area를 다시 그리도록 요청합니다."""
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
    """확정되지 않은 입력만 폐기하고 현재 후보 선택은 유지합니다."""
    if _layer_ui_pointer_events.get(window_key) is not snapshot:
        return False
    _layer_ui_pointer_events.pop(window_key, None)
    _layer_ui_pointer_releases.discard(window_key)
    return True


def _clear_pending_layer_ui_selection(window_key: int, snapshot) -> bool:
    """버튼이 소비하지 않은 RELEASE가 확정된 Scene 후보를 해제합니다."""
    if not _discard_pending_layer_ui_pointer_event(window_key, snapshot):
        return False
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
    for layer in settings.layers:
        layer.ui_selected = False
    settings.layer_selection_anchor_uuid = ""
    _layer_ui_click_history.pop(snapshot.scene_key, None)
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
    selected_layer_uuids: tuple[str, ...] = ()
    selection_anchor_uuid = ""
    click_history = None
    if value == "PRESS" and settings is not None:
        scene_key = _layer_ui_scene_key(scene)
        selected_layer_uuids = tuple(
            str(layer.layer_uuid)
            for layer in settings.layers
            if bool(getattr(layer, "ui_selected", False))
        )
        selection_anchor_uuid = str(
            getattr(settings, "layer_selection_anchor_uuid", "") or ""
        )
        click_history = _layer_ui_click_history.get(scene_key)
    _layer_ui_pointer_events[window_key] = _LayerUIPointerEvent(
        value=value,
        ctrl=ctrl,
        shift=shift,
        mouse_x=mouse_x,
        mouse_y=mouse_y,
        recorded_at=now,
        scene_key=scene_key,
        selected_layer_uuids=selected_layer_uuids,
        selection_anchor_uuid=selection_anchor_uuid,
        click_history=click_history,
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
        and event_value in {"RELEASE", "CLICK"}
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
    """레이어 관련 버튼이 소비한 PRESS의 후보 선택과 앵커를 복원합니다."""
    snapshot = _layer_ui_pointer_event(context, event)
    if snapshot is None or not snapshot.scene_key:
        return snapshot
    scene = getattr(context, "scene", None)
    if _layer_ui_scene_key(scene) != snapshot.scene_key:
        return snapshot
    settings = getattr(scene, "projection_paint", None)
    if settings is None:
        return snapshot
    selected_layer_uuids = set(snapshot.selected_layer_uuids)
    for layer in settings.layers:
        layer.ui_selected = str(layer.layer_uuid) in selected_layer_uuids
    settings.layer_selection_anchor_uuid = snapshot.selection_anchor_uuid
    if snapshot.click_history is None:
        _layer_ui_click_history.pop(snapshot.scene_key, None)
    else:
        _layer_ui_click_history[snapshot.scene_key] = snapshot.click_history
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
    except (AttributeError, RuntimeError):
        pass
    _layer_ui_pointer_events.clear()
    _layer_ui_pointer_releases.clear()
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
    for window in windows:
        window_key = _layer_ui_window_key(window)
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


def _remove_adjustment(layer, index: int, context=None) -> bool:
    """Layer 보조 항목을 제거하고 활성 Index를 남은 항목에 맞춥니다."""
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
    if context is not None:
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


def layer_row_selected(settings, layer_index: int) -> bool:
    """Composite 후보를 위한 지정 Layer의 단순 파란색 선택 상태를 반환합니다."""
    return bool(
        0 <= int(layer_index) < len(settings.layers)
        and bool(getattr(settings.layers[layer_index], "ui_selected", False))
    )


def _layer_ui_click_history_key(scene) -> int:
    """Scene별 일시 더블클릭 기록을 구분할 안정적인 키를 반환합니다."""
    as_pointer = getattr(scene, "as_pointer", None)
    return int(as_pointer()) if callable(as_pointer) else id(scene)


def _layer_double_click_interval_seconds(context) -> float:
    """사용자 Blender 환경설정의 더블클릭 허용 시간을 초 단위로 반환합니다."""
    preferences = getattr(context, "preferences", None)
    inputs = getattr(preferences, "inputs", None)
    interval_ms = int(getattr(inputs, "mouse_double_click_time", 350))
    return max(interval_ms, 1) / 1000.0


def _is_layer_ui_double_click(
    context,
    event,
    layer_uuid: str,
    *,
    ctrl: bool | None = None,
    shift: bool | None = None,
) -> bool:
    """명시 이벤트 또는 일반 버튼의 같은 Layer 연속 클릭을 더블클릭으로 판정합니다."""
    event_value = str(getattr(event, "value", "") or "")
    scene = context.scene
    history_key = _layer_ui_click_history_key(scene)
    if event_value == "DOUBLE_CLICK":
        _layer_ui_click_history.pop(history_key, None)
        return True
    if ctrl is None or shift is None:
        ctrl, shift = _layer_ui_modifiers(context, event)
    if event_value not in {"PRESS", "RELEASE", "CLICK"} or bool(ctrl) or bool(shift):
        _layer_ui_click_history.pop(history_key, None)
        return False
    now = time.monotonic()
    previous = _layer_ui_click_history.get(history_key)
    _layer_ui_click_history[history_key] = (str(layer_uuid), now)
    if previous is None:
        return False
    previous_uuid, previous_time = previous
    if (
        previous_uuid == str(layer_uuid)
        and now - previous_time <= _layer_double_click_interval_seconds(context)
    ):
        _layer_ui_click_history.pop(history_key, None)
        return True
    return False


def _toggle_layer_ui_selection(
    settings,
    layer_uuid: str,
    *,
    ctrl: bool,
    shift: bool,
) -> bool:
    """표시된 최상위 Layer만 일반·Ctrl·Shift Composite 후보로 선택합니다."""
    state = str(getattr(settings, "composite_state", "NONE"))
    session_uuid = str(getattr(settings, "active_composite_session_uuid", "") or "")

    def selectable(layer) -> bool:
        """현재 화면에서 Composite 후보로 선택할 수 있는 최상위 Layer인지 확인합니다."""
        role = str(getattr(layer, "layer_role", "NORMAL"))
        if state != "ACTIVE":
            return role == "NORMAL"
        if role == "COMPOSITE":
            return str(getattr(layer, "composite_session_uuid", "") or "") == session_uuid
        return (
            role == "NORMAL"
            and str(getattr(layer, "composite_source_session_uuid", "") or "")
            != session_uuid
        )

    candidates = tuple(
        (index, layer)
        for index, layer in enumerate(settings.layers)
        if selectable(layer)
    )
    target_position = next(
        (
            position
            for position, (_index, layer) in enumerate(candidates)
            if str(layer.layer_uuid) == str(layer_uuid)
        ),
        -1,
    )
    if target_position < 0:
        return False
    target_index, target = candidates[target_position]
    anchor_uuid = str(getattr(settings, "layer_selection_anchor_uuid", "") or "")
    anchor_position = next(
        (
            position
            for position, (_index, layer) in enumerate(candidates)
            if str(layer.layer_uuid) == anchor_uuid
        ),
        -1,
    )
    if shift:
        if anchor_position >= 0:
            start, end = sorted((anchor_position, target_position))
            if not ctrl:
                for layer in settings.layers:
                    layer.ui_selected = False
            for _index, layer in candidates[start : end + 1]:
                if selectable(layer):
                    layer.ui_selected = True
            return True
        if not ctrl:
            for layer in settings.layers:
                layer.ui_selected = False
        target.ui_selected = True
        settings.layer_selection_anchor_uuid = str(target.layer_uuid)
        return True
    if ctrl:
        target.ui_selected = not bool(target.ui_selected)
    else:
        for layer in settings.layers:
            layer.ui_selected = False
        target.ui_selected = True
    settings.layer_selection_anchor_uuid = str(target.layer_uuid)
    return True


def adjustment_row_selected(
    settings,
    layer_index: int,
    adjustment_index: int,
) -> bool:
    """활성 Layer의 지정 자식만 선택 상태인지 반환합니다."""
    if int(settings.active_layer_index) != int(layer_index):
        return False
    layer = settings.layers[layer_index]
    return not str(settings.active_mask_uuid or "") and bool(layer.expanded) and (
        int(layer.active_adjustment_index) == int(adjustment_index)
    )


def layer_mask_row_selected(settings, layer_index: int) -> bool:
    """활성 Layer의 자체 Mask만 선택 상태인지 반환합니다."""
    if int(settings.active_layer_index) != int(layer_index):
        return False
    layer = settings.layers[layer_index]
    return bool(
        layer.expanded
        and str(layer.mask.mask_uuid or "")
        and int(layer.active_adjustment_index) < 0
        and str(settings.active_mask_uuid) == str(layer.mask.mask_uuid)
    )


def adjustment_mask_row_selected(
    settings,
    layer_index: int,
    adjustment_index: int,
) -> bool:
    """활성 Adjustment의 자체 Mask만 선택 상태인지 반환합니다."""
    if int(settings.active_layer_index) != int(layer_index):
        return False
    layer = settings.layers[layer_index]
    if not 0 <= int(adjustment_index) < len(layer.adjustments):
        return False
    adjustment = layer.adjustments[adjustment_index]
    return bool(
        layer.expanded
        and adjustment.expanded
        and int(layer.active_adjustment_index) == int(adjustment_index)
        and str(adjustment.mask.mask_uuid or "")
        and str(settings.active_mask_uuid) == str(adjustment.mask.mask_uuid)
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
    """한 번 클릭 후보 선택과 더블클릭 활성 Paint Layer 선택을 분리합니다."""

    bl_idname = "projection_paint.layer_select"
    bl_label = "Select Paint Layer"
    bl_description = "한 번 클릭은 Composite 후보를, 더블클릭은 Paint Layer를 선택합니다"
    bl_options = {"INTERNAL"}

    layer_uuid: StringProperty(options={"HIDDEN"})

    @classmethod
    def poll(cls, context):
        """하나 이상의 논리 Paint Layer가 있을 때만 선택을 허용합니다."""
        settings = getattr(getattr(context, "scene", None), "projection_paint", None)
        return bool(settings is not None and len(settings.layers) > 0)

    def _activate_paint_layer(self, context):
        """더블클릭한 Layer만 실제 Paint Image와 활성 상태로 지정합니다."""
        settings = context.scene.projection_paint
        index = next(
            (
                layer_index
                for layer_index, layer in enumerate(settings.layers)
                if str(layer.layer_uuid) == str(self.layer_uuid)
            ),
            -1,
        )
        if index < 0:
            return {"CANCELLED"}
        layer = settings.layers[index]
        if not _layer_editable(settings, layer):
            self.report({"WARNING"}, "Composite 활성 중에는 기존 Layer를 선택할 수 없습니다.")
            return {"CANCELLED"}
        settings.active_layer_index = index
        layer.active_adjustment_index = -1
        settings.active_mask_uuid = ""
        if layer.image is not None:
            try:
                from .layer_shader import activate_layer_paint_image

                activate_layer_paint_image(context, str(layer.layer_uuid))
            except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
                self.report({"ERROR"}, str(exc))
                return {"CANCELLED"}
        return {"FINISHED"}

    def invoke(self, context, event):
        """Blender 클릭 이벤트 값으로 단순 후보 선택과 활성 선택을 구분합니다."""
        settings = context.scene.projection_paint
        layer = _find_layer(settings, self.layer_uuid)
        if layer is None or not _layer_editable(settings, layer):
            return {"CANCELLED"}
        ctrl, shift = _layer_ui_modifiers(context, event)
        if _is_layer_ui_double_click(
            context,
            event,
            str(layer.layer_uuid),
            ctrl=ctrl,
            shift=shift,
        ):
            return self._activate_paint_layer(context)
        if not _toggle_layer_ui_selection(
            settings,
            str(layer.layer_uuid),
            ctrl=ctrl,
            shift=shift,
        ):
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
    """Layer 행에서 클릭한 색상 보정 아이콘을 활성 항목으로 선택합니다."""

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
        """UUID로 찾은 색상 보정을 해당 Layer의 활성 항목으로 지정합니다."""
        settings = context.scene.projection_paint
        layer = _find_layer(settings, self.layer_uuid)
        if layer is None or not _layer_editable(settings, layer):
            return {"CANCELLED"}
        index = _find_adjustment_index(layer, self.adjustment_uuid)
        if index < 0 or not _layer_editable(settings, layer):
            return {"CANCELLED"}
        settings.active_layer_index = next(
            (
                layer_index
                for layer_index, candidate in enumerate(settings.layers)
                if str(candidate.layer_uuid) == str(layer.layer_uuid)
            ),
            int(settings.active_layer_index),
        )
        settings.active_mask_uuid = ""
        if layer.image is not None:
            try:
                from .layer_shader import activate_layer_paint_image

                activate_layer_paint_image(context, str(layer.layer_uuid))
            except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
                self.report({"ERROR"}, str(exc))
                return {"CANCELLED"}
        layer.active_adjustment_index = index
        return {"FINISHED"}


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
        return {"FINISHED"}


class PP_OT_layer_mask_select(bpy.types.Operator):
    """Layer Mask를 유일한 트리 선택과 활성 Paint Image로 지정합니다."""

    bl_idname = "projection_paint.layer_mask_select"
    bl_label = "Select Layer Mask"
    bl_options = {"INTERNAL"}

    layer_uuid: StringProperty(options={"HIDDEN"})
    mask_uuid: StringProperty(options={"HIDDEN"})

    def execute(self, context):
        """UUID가 일치하는 Layer Mask를 선택합니다."""
        settings = context.scene.projection_paint
        layer = _find_layer(settings, self.layer_uuid)
        if (
            layer is None
            or not _layer_editable(settings, layer)
            or str(layer.mask.mask_uuid) != str(self.mask_uuid)
        ):
            return {"CANCELLED"}
        settings.active_layer_index = next(
            index
            for index, candidate in enumerate(settings.layers)
            if str(candidate.layer_uuid) == str(layer.layer_uuid)
        )
        layer.active_adjustment_index = -1
        settings.active_mask_uuid = str(layer.mask.mask_uuid)
        if layer.mask.image is not None:
            from .layer_shader import activate_mask_paint_image

            activate_mask_paint_image(context, str(layer.layer_uuid), str(layer.mask.mask_uuid))
        return {"FINISHED"}


class PP_OT_adjustment_mask_select(bpy.types.Operator):
    """Adjustment Mask를 유일한 트리 선택과 활성 Paint Image로 지정합니다."""

    bl_idname = "projection_paint.adjustment_mask_select"
    bl_label = "Select Adjustment Mask"
    bl_options = {"INTERNAL"}

    layer_uuid: StringProperty(options={"HIDDEN"})
    adjustment_uuid: StringProperty(options={"HIDDEN"})
    mask_uuid: StringProperty(options={"HIDDEN"})

    def execute(self, context):
        """UUID가 일치하는 Adjustment Mask를 선택합니다."""
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
        settings.active_layer_index = next(
            layer_index
            for layer_index, candidate in enumerate(settings.layers)
            if str(candidate.layer_uuid) == str(layer.layer_uuid)
        )
        layer.active_adjustment_index = index
        settings.active_mask_uuid = str(adjustment.mask.mask_uuid)
        if adjustment.mask.image is not None:
            from .layer_shader import activate_mask_paint_image

            activate_mask_paint_image(
                context,
                str(layer.layer_uuid),
                str(adjustment.mask.mask_uuid),
            )
        return {"FINISHED"}


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
    """Mask 흑백 출력을 Material Base Color 단독 미리보기로 전환합니다."""

    bl_idname = "projection_paint.mask_preview_toggle"
    bl_label = "Toggle Mask Preview"
    bl_description = "이 Mask만 Base Color에 표시하거나 원래 Layer 합성으로 돌아갑니다"
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
    """Layer 색상 출력을 Material Base Color 단독 미리보기로 전환합니다."""

    bl_idname = "projection_paint.layer_preview_toggle"
    bl_label = "Toggle Layer Preview"
    bl_description = "이 Layer만 표시하거나 원래 Layer 합성으로 돌아갑니다"
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
        """지정 Layer의 단독 미리보기 상태를 전역 체크 그룹에서 토글합니다."""
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


class PP_OT_layer_adjustment_remove_hovered(bpy.types.Operator):
    """마우스 아래의 Layer 보조 아이콘을 Delete 키로 제거합니다."""

    bl_idname = "projection_paint.layer_adjustment_remove_hovered"
    bl_label = "Delete Hovered Layer Item"
    bl_description = "마우스 아래의 보정 또는 Mask 항목을 삭제합니다"
    bl_options = {"INTERNAL", "UNDO"}

    @classmethod
    def poll(cls, context):
        """마우스가 이 애드온의 Layer 보조 아이콘 위에 있을 때만 허용합니다."""
        return _context_adjustment(context) is not None

    def execute(self, context):
        """마우스 아래 아이콘을 찾아 해당 Layer에서 제거합니다."""
        hovered_adjustment = _context_adjustment(context)
        if hovered_adjustment is None:
            return {"CANCELLED"}
        layer, index = hovered_adjustment
        return (
            {"FINISHED"}
            if _remove_adjustment(layer, index, context)
            else {"CANCELLED"}
        )


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
    """Composite와 같은 세션의 의존 형제를 경고 후 삭제하고 Source Stack을 복구합니다."""

    bl_idname = "projection_paint.composite_layer_release"
    bl_label = "Release Composite Layer"
    bl_description = "Composite와 의존 형제를 삭제하고 Composite 이전 Layer Stack으로 복구합니다"
    bl_options = {"REGISTER", "UNDO"}

    draw = draw_wrapped_confirmation

    @classmethod
    def poll(cls, context):
        """검증 가능한 활성 Composite 세션에서만 해제를 허용합니다."""
        settings = getattr(getattr(context, "scene", None), "projection_paint", None)
        return bool(settings is not None and str(settings.composite_state) == "ACTIVE")

    def invoke(self, context, event):
        """삭제될 Composite와 의존 형제 이름을 포함한 확인 대화상자를 표시합니다."""
        try:
            from .composite_layer import composite_release_summary

            composite_name, dependent_names = composite_release_summary(
                context.scene.projection_paint
            )
            return invoke_wrapped_confirmation(
                self,
                context,
                title="Composite Layer 해제",
                message=(
                    f"{composite_name}와 이후 생성한 형제 Layer {len(dependent_names)}개를 삭제하고 "
                    "이전 Layer Stack을 복구합니다. 삭제된 형제 Layer의 페인트는 복구되지 않습니다."
                ),
                confirm_text="Composite 해제 및 의존 형제 Layer 삭제",
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
    """현재 선택한 Layer 트리 항목과 해당 소유 자원을 제거합니다."""

    bl_idname = "projection_paint.layer_remove"
    bl_label = "Remove Selected Layer Item"
    bl_description = "선택한 Layer, Mask 또는 보정 항목을 제거합니다"
    bl_options = {"REGISTER", "UNDO"}

    draw = draw_wrapped_confirmation

    reset_width: IntProperty(default=0, min=0, options={"HIDDEN", "SKIP_SAVE"})
    reset_height: IntProperty(default=0, min=0, options={"HIDDEN", "SKIP_SAVE"})

    @classmethod
    def poll(cls, context):
        """유효한 Layer 트리 항목 하나가 선택되었을 때만 버튼을 활성화합니다."""
        settings = getattr(getattr(context, "scene", None), "projection_paint", None)
        return bool(
            settings is not None
            and active_layer_tree_item(settings) is not None
        )

    def invoke(self, context, _event):
        """부모 Layer 삭제에만 확인창을 표시하고 자식은 즉시 제거합니다."""
        settings = context.scene.projection_paint
        selected = active_layer_tree_item(settings)
        if selected is None:
            return {"CANCELLED"}
        selected_kind, _layer, _adjustment = selected
        if selected_kind != "LAYER":
            return self.execute(context)
        if settings.paint_material is None:
            return self.execute(context)
        if len(settings.layers) == 1:
            try:
                from .material_setup import pending_overlay_resolution_change
                from .paint_session import (
                    _resolution_label,
                    _selected_overlay_resolution,
                )

                width, height = _selected_overlay_resolution(settings)
                change = pending_overlay_resolution_change(
                    settings.paint_target,
                    width,
                    height,
                    use_base_resolution=bool(settings.use_base_resolution),
                )
                desired = change[1] if change is not None else tuple(
                    int(value) for value in settings.overlay_image.size
                )
            except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
                self.report({"ERROR"}, str(exc))
                return {"CANCELLED"}
            self.reset_width, self.reset_height = desired
            layer_name = str(settings.layers[0].name)
            return invoke_wrapped_confirmation(
                self,
                context,
                title="마지막 Paint Layer 새로 만들기",
                message=_last_layer_reset_message(
                    layer_name,
                    _resolution_label(desired),
                ),
                confirm_text=f"새 {_resolution_label(desired)} Layer 1 생성",
                icon="STATUS_WARNING",
            )
        return context.window_manager.invoke_confirm(self, _event)

    def execute(self, context):
        """선택 종류에 따라 부모 전체 또는 해당 자식 하나만 제거합니다."""
        settings = context.scene.projection_paint
        selected = active_layer_tree_item(settings)
        if selected is None:
            self.report({"WARNING"}, "삭제할 Layer 항목이 없습니다.")
            return {"CANCELLED"}
        selected_kind, layer, adjustment = selected
        if str(getattr(layer, "layer_role", "NORMAL")) == "COMPOSITE":
            self.report({"WARNING"}, "Composite Layer는 'Composite 해제'로만 삭제할 수 있습니다.")
            return {"CANCELLED"}
        if not _layer_editable(settings, layer):
            self.report({"WARNING"}, "Composite 활성 중에는 기존 Layer를 삭제할 수 없습니다.")
            return {"CANCELLED"}
        try:
            if selected_kind == "LAYER_MASK":
                from .layer_shader import remove_mask_resources

                remove_mask_resources(context.scene, layer, layer.mask)
                _reconcile_if_ready(context)
                return {"FINISHED"}
            if selected_kind == "ADJUSTMENT_MASK":
                from .layer_shader import remove_mask_resources

                remove_mask_resources(
                    context.scene,
                    layer,
                    adjustment.mask,
                    adjustment,
                )
                _reconcile_if_ready(context)
                return {"FINISHED"}
            if selected_kind == "ADJUSTMENT":
                index = _find_adjustment_index(
                    layer,
                    str(adjustment.adjustment_uuid),
                )
                return (
                    {"FINISHED"}
                    if _remove_adjustment(layer, index, context)
                    else {"CANCELLED"}
                )
        except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        index = next(
            (
                layer_index
                for layer_index, candidate in enumerate(settings.layers)
                if str(candidate.layer_uuid) == str(layer.layer_uuid)
            ),
            -1,
        )
        if index < 0:
            self.report({"WARNING"}, "삭제할 Paint Layer가 없습니다.")
            return {"CANCELLED"}
        if settings.paint_material is not None and len(settings.layers) == 1:
            try:
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
            except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
                self.report({"ERROR"}, str(exc))
                return {"CANCELLED"}
            return {"FINISHED"}
        if settings.paint_material is not None:
            try:
                from .layer_shader import remove_layer_resources

                remove_layer_resources(
                    context,
                    str(settings.layers[index].layer_uuid),
                )
            except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
                self.report({"ERROR"}, str(exc))
                return {"CANCELLED"}
        else:
            settings.layers.remove(index)
            settings.active_layer_index = min(index, len(settings.layers) - 1)
            settings.active_mask_uuid = ""
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
    "disable_layer_ui_event_tracking",
    "enable_layer_ui_event_tracking",
    "layer_ui_event_tracker_heartbeat",
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
