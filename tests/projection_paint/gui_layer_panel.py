"""Blender GUI에서 Paint Setup 하위 Layers 패널을 검증합니다."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import traceback

import bpy


ROOT = Path(__file__).resolve().parents[2] / "tools" / "projection_paint"
RESULT_PATH = Path(
    os.environ.get(
        "PP_LAYER_PANEL_RESULT",
        r"C:\tmp\projection_paint_layer_panel.json",
    )
)
SCREENSHOT_PATH = Path(
    os.environ.get(
        "PP_LAYER_PANEL_SCREENSHOT",
        r"C:\tmp\projection_paint_layer_panel.png",
    )
)
LAYER_SCREENSHOT_PATH = SCREENSHOT_PATH.with_name(
    f"{SCREENSHOT_PATH.stem}_layer{SCREENSHOT_PATH.suffix}"
)
sys.path.insert(0, str(ROOT.parent))

STATE = {"step": 0}


def _write_result(payload: dict) -> None:
    """검증 결과를 UTF-8 JSON 파일로 기록합니다."""
    RESULT_PATH.parent.mkdir(parents=True, exist_ok=True)
    RESULT_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _ensure_addon() -> None:
    """설치본이 비활성인 Factory 환경에서만 Source Extension을 등록합니다."""
    if hasattr(bpy.types.Scene, "projection_paint"):
        return
    import Blender_ProjectionPainting_addon as addon

    addon.register()


def _run() -> float | None:
    """Layers 패널을 열고 목록·추가·삭제 UI를 실제 Blender 화면에서 검증합니다."""
    try:
        if STATE["step"] == 0:
            _ensure_addon()
            bpy.ops.projection_paint.layer_add()
            bpy.ops.projection_paint.layer_add()
            bpy.ops.projection_paint.layer_add()
            active_layer = bpy.context.scene.projection_paint.layers[0]
            bpy.ops.projection_paint.layer_mask_add(
                layer_uuid=active_layer.layer_uuid,
            )
            for adjustment_type in ("BRIGHTNESS", "EXPOSURE", "SATURATION"):
                bpy.ops.projection_paint.layer_adjustment_add(
                    layer_uuid=active_layer.layer_uuid,
                    adjustment_type=adjustment_type,
                )
            bpy.ops.projection_paint.adjustment_mask_add(
                layer_uuid=active_layer.layer_uuid,
                adjustment_uuid=active_layer.adjustments[0].adjustment_uuid,
            )
            bpy.ops.projection_paint.adjustment_mask_select(
                layer_uuid=active_layer.layer_uuid,
                adjustment_uuid=active_layer.adjustments[0].adjustment_uuid,
                mask_uuid=active_layer.adjustments[0].mask.mask_uuid,
            )
            active_layer = bpy.context.scene.projection_paint.layers[0]
            active_layer.adjustments[0].brightness = 12.5
            active_layer.adjustments[1].exposure = 1.25
            active_layer.adjustments[2].hue = 0.65
            active_layer.adjustments[2].saturation = 0.85
            bpy.ops.projection_paint.layer_move(direction="DOWN")
            window = bpy.context.window
            area = next(
                candidate
                for candidate in window.screen.areas
                if candidate.type == "VIEW_3D"
            )
            region = next(
                candidate
                for candidate in area.regions
                if candidate.type == "WINDOW"
            )
            with bpy.context.temp_override(
                window=window,
                screen=window.screen,
                area=area,
                region=region,
            ):
                result = bpy.ops.wm.call_panel(
                    "INVOKE_DEFAULT",
                    name="PP_PT_layers",
                    keep_open=True,
                )
            if result != {"INTERFACE"}:
                raise RuntimeError(f"Layers 패널을 열지 못했습니다: {result}")
            STATE["step"] = 1
            return 0.3
        if STATE["step"] == 1:
            screenshot_result = bpy.ops.screen.screenshot(
                filepath=str(SCREENSHOT_PATH),
            )
            settings = bpy.context.scene.projection_paint
            from tools.projection_paint.blender.layers import (
                adjustment_mask_row_selected,
                adjustment_row_selected,
                layer_mask_row_selected,
                layer_row_selected,
            )

            active_index = int(settings.active_layer_index)
            active_layer = settings.layers[active_index]
            selection_count = int(layer_row_selected(settings, active_index))
            selection_count += sum(
                int(adjustment_row_selected(settings, active_index, index))
                for index in range(len(active_layer.adjustments))
            )
            selection_count += int(layer_mask_row_selected(settings, active_index))
            selection_count += sum(
                int(adjustment_mask_row_selected(settings, active_index, index))
                for index in range(len(active_layer.adjustments))
            )
            if screenshot_result != {"FINISHED"} or selection_count != 1:
                raise RuntimeError("Mask 단독 선택 화면 검증에 실패했습니다.")
            STATE["mask_screenshot_result"] = sorted(screenshot_result)
            STATE["mask_selection_count"] = selection_count
            bpy.ops.projection_paint.layer_select(
                layer_uuid=active_layer.layer_uuid,
            )
            STATE["step"] = 2
            return 0.3
        if STATE["step"] == 2:
            bpy.ops.wm.redraw_timer(type="DRAW_WIN_SWAP", iterations=2)
            STATE["step"] = 3
            return 0.3
        screenshot_result = bpy.ops.screen.screenshot(
            filepath=str(LAYER_SCREENSHOT_PATH),
        )
        settings = bpy.context.scene.projection_paint
        from tools.projection_paint.blender.layers import (
            adjustment_mask_row_selected,
            adjustment_row_selected,
            layer_mask_row_selected,
            layer_row_selected,
        )

        active_index = int(settings.active_layer_index)
        active_layer = settings.layers[active_index]
        layer_selection_count = int(layer_row_selected(settings, active_index))
        layer_selection_count += sum(
            int(adjustment_row_selected(settings, active_index, index))
            for index in range(len(active_layer.adjustments))
        )
        layer_selection_count += int(layer_mask_row_selected(settings, active_index))
        layer_selection_count += sum(
            int(adjustment_mask_row_selected(settings, active_index, index))
            for index in range(len(active_layer.adjustments))
        )
        payload = {
            "success": True,
            "mask_screenshot_result": STATE["mask_screenshot_result"],
            "layer_screenshot_result": sorted(screenshot_result),
            "mask_selection_count": STATE["mask_selection_count"],
            "layer_selection_count": layer_selection_count,
            "layer_names": [layer.name for layer in settings.layers],
            "active_adjustments": [
                adjustment.adjustment_type
                for adjustment in settings.layers[
                    settings.active_layer_index
                ].adjustments
            ],
            "active_adjustment_index": int(
                settings.layers[
                    settings.active_layer_index
                ].active_adjustment_index
            ),
            "active_layer_index": int(settings.active_layer_index),
            "mask_enabled": bool(
                settings.layers[
                    settings.active_layer_index
                ].mask.enabled
            ),
            "mask_screenshot": str(SCREENSHOT_PATH),
            "layer_screenshot": str(LAYER_SCREENSHOT_PATH),
        }
        if screenshot_result != {"FINISHED"}:
            raise RuntimeError(f"Layers 패널 캡처에 실패했습니다: {screenshot_result}")
        if payload["layer_names"] != ["Layer 2", "Layer 3", "Layer 1"]:
            raise RuntimeError(f"Layer 목록이 예상과 다릅니다: {payload['layer_names']}")
        if payload["active_adjustments"] != [
            "BRIGHTNESS",
            "EXPOSURE",
            "SATURATION",
        ]:
            raise RuntimeError(
                f"색상 보정 UI가 예상과 다릅니다: {payload['active_adjustments']}"
            )
        if payload["active_adjustment_index"] != -1:
            raise RuntimeError("Layer 선택 후 자식 선택이 해제되지 않았습니다.")
        if payload["mask_selection_count"] != 1:
            raise RuntimeError("Mask 선택 화면에 선택 항목이 둘 이상입니다.")
        if payload["layer_selection_count"] != 1:
            raise RuntimeError("Layer 선택 화면에 선택 항목이 둘 이상입니다.")
        if not payload["mask_enabled"]:
            raise RuntimeError("Mask Enabled 기본값이 활성화되지 않았습니다.")
        _write_result(payload)
    except Exception as exc:
        _write_result(
            {
                "success": False,
                "error": str(exc),
                "traceback": traceback.format_exc(),
            }
        )
    bpy.app.timers.register(_quit, first_interval=0.2)
    return None


def _quit() -> None:
    """GUI 검증 완료 후 Blender를 종료합니다."""
    bpy.ops.wm.quit_blender()


bpy.app.timers.register(_run, first_interval=0.5)
