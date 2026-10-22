"""연결된 스텐실에서 Ctrl 입력 동안 기본 부드러운 지우개를 임시 사용합니다."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import bpy
from bpy.props import BoolProperty

from .paint_tool_bridge import (
    ESSENTIALS_TEXTURE_BRUSH_LIBRARY,
    BrushAssetChoice,
    activate_brush_asset,
    active_brush_choice,
    active_tool_id,
)
from .viewport_navigation import projection_paint_viewport_active


@dataclass(slots=True)
class StencilEraseHold:
    """한 번의 Ctrl 입력에 속한 원래 브러시와 뷰 문맥을 보관합니다."""

    scene: Any
    window: Any
    workspace: Any
    screen: Any
    area: Any
    region: Any
    view_layer: Any
    target: Any
    original_brush: Any
    original_choice: BrushAssetChoice
    original_tool: str
    erase_brush: Any = None
    stroke_active: bool = False
    control_keys: set[str] = field(default_factory=set)


_HOLDS: dict[int, StencilEraseHold] = {}
_CTRL_KEYS = {"LEFT_CTRL", "RIGHT_CTRL"}
_MODIFIER_KEYS = _CTRL_KEYS | {"LEFT_SHIFT", "RIGHT_SHIFT", "LEFT_ALT", "RIGHT_ALT", "OSKEY"}
_POINTER_EVENTS = {
    "LEFTMOUSE", "RIGHTMOUSE", "MIDDLEMOUSE", "MOUSEMOVE", "INBETWEEN_MOUSEMOVE",
    "WHEELUPMOUSE", "WHEELDOWNMOUSE", "WHEELINMOUSE", "WHEELOUTMOUSE",
    "TRACKPADPAN", "TRACKPADZOOM", "MOUSEROTATE", "PEN", "ERASER", "TIMER",
}


def active_stencil_erase(scene: Any) -> StencilEraseHold | None:
    """해당 장면에 속한 현재 Ctrl 임시 지우개를 반환합니다."""
    return next((hold for hold in _HOLDS.values() if hold.scene is scene), None)


def _hold_override(hold: StencilEraseHold) -> dict[str, Any]:
    """입력을 시작한 장면·작업공간·뷰 문맥으로 브러시 복구를 제한합니다."""
    return dict(
        window=hold.window, screen=hold.screen, area=hold.area, region=hold.region,
        workspace=hold.workspace, scene=hold.scene, view_layer=hold.view_layer,
        active_object=hold.target, object=hold.target,
    )


def _restore_hold(hold: StencilEraseHold) -> None:
    """원래 도구와 동일한 브러시 자산을 복구한 뒤 임시 입력 상태를 제거합니다."""
    with bpy.context.temp_override(**_hold_override(hold)):
        if active_tool_id(bpy.context) != hold.original_tool:
            bpy.ops.wm.tool_set_by_id(name=hold.original_tool)
        restored = activate_brush_asset(
            bpy.context, hold.scene, hold.original_choice,
            expected_tool_id=hold.original_tool, expected_brush_type="DRAW",
        )
        if restored is not hold.original_brush:
            raise RuntimeError("Ctrl 지우개 종료 뒤 원래 브러시를 복구하지 못했습니다.")
    _HOLDS.pop(hold.window.as_pointer(), None)


def restore_stencil_erase(scene: Any = None) -> None:
    """종료·문맥 전환 시 지정한 장면 또는 모든 임시 지우개를 복구합니다."""
    for hold in tuple(_HOLDS.values()):
        if scene is None or hold.scene is scene:
            _restore_hold(hold)


def clear_stencil_erase(_reason: str = "", _epoch: int = 0) -> None:
    """저장·수명주기 중단 전에 임시 브러시 선택을 원상 복구합니다."""
    restore_stencil_erase()


def _hold_is_current(hold: StencilEraseHold) -> bool:
    """현재 창·모드·카메라·레이어가 입력 시작 조건을 유지하는지 확인합니다."""
    settings = hold.scene.projection_paint
    return bool(
        hold.window.scene is hold.scene
        and hold.window.workspace is hold.workspace
        and hold.window.view_layer == hold.view_layer
        and hold.area.type == "VIEW_3D"
        and hold.area.spaces.active.region_3d.view_perspective == "CAMERA"
        and settings.paint_enabled
        and not settings.paint_mask_mode
        and not settings.manual_stencil_disabled
        and not settings.stencil_outside_camera_view
        and settings.paint_target is hold.target
        and hold.target.mode == "TEXTURE_PAINT"
        and hold.view_layer.objects.active is hold.target
        and settings.stencil_brush is hold.original_brush
        and hold.original_brush.texture is settings.stencil_texture
    )


def reconcile_stencil_erase(scene: Any) -> bool:
    """입력 조건이 깨진 지우개를 복구하고 유효한 임시 전환 여부를 반환합니다."""
    hold = active_stencil_erase(scene)
    if hold is None:
        return False
    if not _hold_is_current(hold):
        _restore_hold(hold)
        return False
    return True


def stencil_erase_allowed(context: Any) -> bool:
    """실제로 스텐실이 연결된 전용 페인트 뷰의 Draw 도구만 허용합니다."""
    if not projection_paint_viewport_active(context) or context.mode != "PAINT_TEXTURE":
        return False
    settings = context.scene.projection_paint
    if (
        not settings.paint_enabled or settings.paint_mask_mode
        or context.area.spaces.active.region_3d.view_perspective != "CAMERA"
        or active_tool_id(context) != "builtin.brush"
        or settings.paint_target is not context.view_layer.objects.active
    ):
        return False
    from .stencil_session import resolve_stencil_connection

    return resolve_stencil_connection(context.scene, context).state == "CONNECTED"


class PP_OT_hold_stencil_erase(bpy.types.Operator):
    """Ctrl을 누른 동안 지우개를 선택하고 기본 스트로크 앞에서 해제를 감지합니다."""

    bl_idname = "projection_paint.hold_stencil_erase"
    bl_label = "스텐실 임시 부드러운 지우개"
    bl_options = {"INTERNAL"}

    resume: BoolProperty(default=False, options={"HIDDEN", "SKIP_SAVE"})

    @classmethod
    def poll(cls, context: Any) -> bool:
        """새 입력은 연결 상태를 검사하고 스트로크 관찰자는 기존 전환만 받습니다."""
        return active_stencil_erase(context.scene) is not None or stencil_erase_allowed(context)

    def invoke(self, context: Any, event: Any) -> set[str]:
        """처음 Ctrl을 누르면 원래 자산을 보관하고 기본 Erase Soft를 활성화합니다."""
        hold = active_stencil_erase(context.scene)
        if self.resume:
            if hold is None:
                return {"CANCELLED"}
        else:
            if hold is not None or event.type not in _CTRL_KEYS or event.alt or event.shift or event.oskey:
                return {"PASS_THROUGH"}
            choice = active_brush_choice(context.scene)
            if choice is None:
                return {"PASS_THROUGH"}
            hold = StencilEraseHold(
                context.scene, context.window, context.workspace, context.screen,
                context.area, context.region, context.view_layer,
                context.scene.projection_paint.paint_target,
                context.scene.tool_settings.image_paint.brush, choice,
                active_tool_id(context),
            )
            _HOLDS[context.window.as_pointer()] = hold
            hold.control_keys.add(event.type)
            try:
                hold.erase_brush = activate_brush_asset(
                    context, context.scene,
                    BrushAssetChoice("ESSENTIALS", "", f"{ESSENTIALS_TEXTURE_BRUSH_LIBRARY}/Brush/Erase Soft"),
                    expected_tool_id="builtin.brush", expected_brush_type="DRAW",
                )
                if hold.erase_brush.blend != "ERASE_ALPHA" or hold.erase_brush.texture is not None:
                    raise RuntimeError("기본 Erase Soft 브러시가 올바른 지우개 자산이 아닙니다.")
            except Exception:
                _restore_hold(hold)
                raise
        self._hold = hold
        context.window_manager.modal_handler_add(self)
        return {"RUNNING_MODAL"}

    def modal(self, context: Any, event: Any) -> set[str]:
        """Ctrl 해제·문맥 이탈을 복구하며 지우기 입력만 기본 반전 없이 전달합니다."""
        hold = self._hold
        if active_stencil_erase(hold.scene) is not hold:
            return {"FINISHED", "PASS_THROUGH"}
        if not _hold_is_current(hold) or event.type in {"WINDOW_DEACTIVATE", "ESC"}:
            _restore_hold(hold)
            return {"FINISHED", "PASS_THROUGH"}
        if event.type in _CTRL_KEYS:
            if event.value == "PRESS":
                hold.control_keys.add(event.type)
            elif event.value == "RELEASE":
                hold.control_keys.discard(event.type)
                if not hold.control_keys:
                    _restore_hold(hold)
                    return {"FINISHED", "PASS_THROUGH"}
            return {"PASS_THROUGH"}
        if event.value == "PRESS" and event.type not in _MODIFIER_KEYS | _POINTER_EVENTS:
            _restore_hold(hold)
            return {"FINISHED", "PASS_THROUGH"}
        if event.type == "LEFTMOUSE" and event.value == "RELEASE":
            hold.stroke_active = False
        if (
            event.type == "LEFTMOUSE" and event.value == "PRESS"
            and not hold.stroke_active and not event.alt and not event.shift
            and context.area == hold.area and context.region == hold.region
        ):
            if not (
                hold.region.x <= event.mouse_x < hold.region.x + hold.region.width
                and hold.region.y <= event.mouse_y < hold.region.y + hold.region.height
            ):
                _restore_hold(hold)
                return {"FINISHED", "PASS_THROUGH"}
            from .layers import PP_OT_block_multiselect_paint_stroke

            if PP_OT_block_multiselect_paint_stroke.poll(context):
                bpy.ops.projection_paint.block_multiselect_paint_stroke("EXEC_DEFAULT")
                return {"RUNNING_MODAL"}
            result = bpy.ops.paint.image_paint("INVOKE_DEFAULT", mode="NORMAL")
            if result == {"RUNNING_MODAL"}:
                hold.stroke_active = True
                # 기본 스트로크가 키 입력을 소비하므로 새 관찰자를 그 앞에 연결합니다.
                bpy.ops.projection_paint.hold_stencil_erase("INVOKE_DEFAULT", resume=True)
                return {"FINISHED"}
            return {"RUNNING_MODAL"}
        return {"PASS_THROUGH"}

    def cancel(self, _context: Any) -> None:
        """Blender가 입력 처리를 취소해도 자신이 선택한 임시 지우개를 복구합니다."""
        hold = getattr(self, "_hold", None)
        if hold is not None and active_stencil_erase(hold.scene) is hold:
            _restore_hold(hold)


CLASSES = (PP_OT_hold_stencil_erase,)
