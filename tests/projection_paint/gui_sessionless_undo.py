"""실제 Paint 파일에서 Stroke Undo·Redo 뒤 세션리스 Stencil 복구를 검증합니다."""

from __future__ import annotations

import json
import math
from pathlib import Path
from time import perf_counter
import traceback

import bpy


RESULT_PATH = Path(r"C:\tmp\projection_paint_actual\undo_result.json")
STATE = {
    "phase": 0,
    "cycle": 0,
    "started": perf_counter(),
    "result": {},
    "reconnect_ms": [],
}


def _pointer(value) -> int:
    """삭제된 Blender ID Pointer를 안전하게 0으로 변환합니다."""
    try:
        return int(value.as_pointer()) if value is not None else 0
    except (AttributeError, ReferenceError, RuntimeError, TypeError, ValueError):
        return 0


def _view_parts():
    """현재 Window의 Texture Paint용 3D View 구성요소를 반환합니다."""
    window = bpy.context.window
    area = next(
        (candidate for candidate in window.screen.areas if candidate.type == "VIEW_3D"),
        None,
    )
    region = next(
        (candidate for candidate in getattr(area, "regions", ()) if candidate.type == "WINDOW"),
        None,
    )
    if window is None or area is None or region is None:
        raise RuntimeError("Texture Paint용 3D Viewport가 없습니다.")
    return window, area, region, area.spaces.active.region_3d


def _managed_counts() -> dict[str, int]:
    """세션리스 반복 검증용 관리 Texture·Brush·Source 수를 반환합니다."""
    return {
        "textures": sum(
            texture.get("pp_record_kind") == "STENCIL_TEXTURE"
            for texture in bpy.data.textures
        ),
        "brushes": sum(
            brush.get("pp_record_kind") == "STENCIL_BRUSH"
            for brush in bpy.data.brushes
        ),
        "sources": sum(
            image.get("pp_record_kind") == "SOURCE_OWNED"
            for image in bpy.data.images
        ),
    }


def _write_result(success: bool, error: str = "") -> None:
    """Undo·Redo GUI 결과를 UTF-8 JSON으로 기록합니다."""
    RESULT_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(STATE["result"])
    payload["success"] = bool(success)
    payload["error"] = str(error)
    payload["elapsed_seconds"] = round(perf_counter() - STATE["started"], 3)
    RESULT_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _quit() -> None:
    """현재 검증용 Blender Process를 종료합니다."""
    bpy.ops.wm.quit_blender()


def _fail(exc: Exception) -> None:
    """실패 원인과 Traceback을 기록하고 Blender를 종료합니다."""
    STATE["result"]["traceback"] = traceback.format_exc()
    _write_result(False, str(exc))
    _quit()


def _assert_reconnect() -> None:
    """Blender Pointer 상태만으로 현재 Frame Stencil이 복구되는지 확인합니다."""
    scene = bpy.context.scene
    settings = scene.projection_paint
    if not bool(settings.paint_enabled):
        raise RuntimeError("Undo·Redo 뒤 Paint 활성 의도가 사라졌습니다.")
    from bl_ext.user_default.colmap_tools.tools.projection_paint.blender.paint_session import (
        active_paint_session,
    )

    if active_paint_session(scene) is None:
        raise RuntimeError("Undo·Redo 뒤 Blender Paint 계약을 복원하지 못했습니다.")
    reconnect_started = perf_counter()
    result = bpy.ops.projection_paint.reconnect_stencil()
    STATE["reconnect_ms"].append((perf_counter() - reconnect_started) * 1000.0)
    if result != {"FINISHED"}:
        raise RuntimeError(f"Undo·Redo 뒤 Stencil Reconnect가 실패했습니다: {result}")
    brush = settings.stencil_brush
    texture = settings.stencil_texture
    source = settings.stencil_source_image
    if not all((_pointer(brush), _pointer(texture), _pointer(source))):
        raise RuntimeError("Undo·Redo 뒤 Brush·Texture·Source Pointer가 불완전합니다.")
    if brush.texture is not texture or brush.texture_slot.map_mode != "STENCIL":
        raise RuntimeError("Undo·Redo 뒤 현재 Brush의 STENCIL 연결이 복구되지 않았습니다.")


def _paint_stroke() -> None:
    """현재 Camera View에 실제 Texture Paint Stroke 한 획을 입력합니다."""
    scene = bpy.context.scene
    target = scene.projection_paint.paint_target
    brush = scene.tool_settings.image_paint.brush
    if target is None or brush is None or target.mode != "TEXTURE_PAINT":
        raise RuntimeError("실제 Stroke를 입력할 Texture Paint 상태가 아닙니다.")
    window, area, region, _rv3d = _view_parts()
    center = (region.width * 0.46, region.height * 0.49)
    stroke = [
        {
            "name": "",
            "mouse": (center[0] + offset, center[1]),
            "mouse_event": (center[0] + offset, center[1]),
            "pressure": 1.0,
            "size": 72.0,
            "time": index * 0.05,
            "is_start": index == 0,
            "location": (0.0, 0.0, 0.0),
            "x_tilt": 0.0,
            "y_tilt": 0.0,
        }
        for index, offset in enumerate((-8.0, -4.0, 0.0, 4.0, 8.0))
    ]
    with bpy.context.temp_override(
        window=window,
        screen=window.screen,
        area=area,
        region=region,
        active_object=target,
        object=target,
    ):
        result = bpy.ops.paint.image_paint(stroke=stroke)
    if result != {"FINISHED"}:
        raise RuntimeError(f"Undo 검증용 실제 Stroke가 실패했습니다: {result}")


def _undo_or_redo(operator) -> None:
    """3D View Context에서 Blender Undo 또는 Redo Operator를 실행합니다."""
    window, area, region, _rv3d = _view_parts()
    with bpy.context.temp_override(
        window=window,
        screen=window.screen,
        area=area,
        region=region,
    ):
        result = operator()
    if result != {"FINISHED"}:
        raise RuntimeError(f"Undo·Redo Operator가 실패했습니다: {result}")


def _phase_stroke_and_undo() -> float:
    """Reconnect 뒤 실제 Stroke를 만들고 첫 Undo를 실행합니다."""
    _assert_reconnect()
    STATE["result"]["baseline_counts"] = _managed_counts()
    _paint_stroke()
    _undo_or_redo(bpy.ops.ed.undo)
    STATE["phase"] = 1
    return 0.8


def _phase_verify_undo() -> float:
    """각 Undo 뒤 Reconnect를 확인하고 첫 회에는 Camera View를 이동합니다."""
    _assert_reconnect()
    if int(STATE["cycle"]) == 0:
        settings = bpy.context.scene.projection_paint
        brush = settings.stencil_brush
        STATE["before_track_position"] = tuple(float(value) for value in brush.stencil_pos)
        STATE["before_track_dimensions"] = tuple(
            float(value) for value in brush.stencil_dimension
        )
        _window, _area, _region, rv3d = _view_parts()
        rv3d.view_camera_offset = (
            float(rv3d.view_camera_offset[0]) + 0.08,
            float(rv3d.view_camera_offset[1]) - 0.05,
        )
        rv3d.view_camera_zoom = float(rv3d.view_camera_zoom) + 18.0
        STATE["phase"] = 2
        return 0.8
    _undo_or_redo(bpy.ops.ed.redo)
    STATE["phase"] = 3
    return 0.8


def _phase_verify_tracking() -> float:
    """첫 Undo 뒤 Camera 이동·확대를 Stencil이 계속 추적하는지 확인합니다."""
    _assert_reconnect()
    brush = bpy.context.scene.projection_paint.stencil_brush
    position = tuple(float(value) for value in brush.stencil_pos)
    dimensions = tuple(float(value) for value in brush.stencil_dimension)
    if all(
        math.isclose(actual, before, abs_tol=1.0e-3)
        for actual, before in zip(position, STATE["before_track_position"], strict=True)
    ):
        raise RuntimeError("Undo 뒤 Camera 이동을 Stencil이 추적하지 못했습니다.")
    if all(
        math.isclose(actual, before, abs_tol=1.0e-3)
        for actual, before in zip(dimensions, STATE["before_track_dimensions"], strict=True)
    ):
        raise RuntimeError("Undo 뒤 Camera 확대를 Stencil이 추적하지 못했습니다.")
    STATE["result"]["undo_camera_tracking"] = True
    _undo_or_redo(bpy.ops.ed.redo)
    STATE["phase"] = 3
    return 0.8


def _phase_verify_redo() -> float:
    """각 Redo 뒤 Reconnect를 확인하고 20회가 될 때까지 반복합니다."""
    _assert_reconnect()
    STATE["cycle"] = int(STATE["cycle"]) + 1
    if int(STATE["cycle"]) < 20:
        _undo_or_redo(bpy.ops.ed.undo)
        STATE["phase"] = 1
        return 0.8
    counts = _managed_counts()
    baseline = STATE["result"]["baseline_counts"]
    if counts["textures"] != baseline["textures"]:
        raise RuntimeError("Undo·Redo 반복 뒤 관리 Stencil Texture 수가 증가했습니다.")
    if counts["brushes"] > max(1, baseline["brushes"]):
        raise RuntimeError("Undo·Redo 반복 뒤 임시 Brush 수가 증가했습니다.")
    if counts["sources"] > 3:
        raise RuntimeError("Undo·Redo 반복 뒤 Source Image Cache 상한을 초과했습니다.")
    reconnect_times = sorted(float(value) for value in STATE["reconnect_ms"])
    percentile_index = min(
        len(reconnect_times) - 1,
        max(0, math.ceil(len(reconnect_times) * 0.95) - 1),
    )
    STATE["result"].update(
        {
            "undo_count": 20,
            "redo_count": 20,
            "final_counts": counts,
            "paint_enabled": bool(bpy.context.scene.projection_paint.paint_enabled),
            "paint_state": str(bpy.context.scene.projection_paint.paint_state),
            "reconnect_after_each_transition": True,
            "reconnect_samples": len(reconnect_times),
            "reconnect_median_ms": round(
                reconnect_times[len(reconnect_times) // 2],
                3,
            ),
            "reconnect_p95_ms": round(reconnect_times[percentile_index], 3),
            "reconnect_max_ms": round(reconnect_times[-1], 3),
        }
    )
    _write_result(True)
    _quit()
    return None


def _run_phase():
    """Timer 단계별로 GUI Event Loop를 양보하며 Undo·Redo 검증을 실행합니다."""
    try:
        phase = int(STATE["phase"])
        if phase == 0:
            return _phase_stroke_and_undo()
        if phase == 1:
            return _phase_verify_undo()
        if phase == 2:
            return _phase_verify_tracking()
        if phase == 3:
            return _phase_verify_redo()
        raise RuntimeError(f"알 수 없는 Undo 검증 단계입니다: {phase}")
    except Exception as exc:
        _fail(exc)
        return None


RESULT_PATH.parent.mkdir(parents=True, exist_ok=True)
bpy.app.timers.register(_run_phase, first_interval=1.0)
