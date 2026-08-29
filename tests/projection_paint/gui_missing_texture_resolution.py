"""Blender GUI에서 Texture가 없는 Target의 프리셋 해상도를 검증합니다."""

from __future__ import annotations

import json
from pathlib import Path
import traceback

import bpy


RESULT_PATH = Path(r"C:\tmp\projection_paint_missing_texture_resolution.json")
STATE = {"cleaned": False, "entered": False, "waits": 0}


def _write_result(payload: dict) -> None:
    """검증 결과를 UTF-8 JSON 파일로 기록합니다."""
    RESULT_PATH.parent.mkdir(parents=True, exist_ok=True)
    RESULT_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _new_target() -> bpy.types.Object:
    """UV는 있지만 Material과 Texture가 없는 검증용 Mesh를 생성합니다."""
    mesh = bpy.data.meshes.new("PP_RESOLUTION_TEST_MESH")
    mesh.from_pydata(
        ((-1.0, -1.0, 0.0), (1.0, -1.0, 0.0), (1.0, 1.0, 0.0), (-1.0, 1.0, 0.0)),
        (),
        ((0, 1, 2, 3),),
    )
    mesh.uv_layers.new(name="UVMap")
    target = bpy.data.objects.new("PP_RESOLUTION_TEST", mesh)
    bpy.context.scene.collection.objects.link(target)
    return target


def _run() -> float | None:
    """Texture 없는 Target으로 1K Paint Overlay 생성 결과를 확인합니다."""
    try:
        settings = bpy.context.scene.projection_paint
        if not STATE["cleaned"]:
            if bool(settings.paint_enabled):
                bpy.ops.projection_paint.toggle_paint()
                STATE["cleaned"] = True
                return 0.1
            STATE["cleaned"] = True
        if not STATE["entered"]:
            target = _new_target()
            settings.paint_target = target
            settings.overlay_resolution = "RES_1K"
            bpy.context.view_layer.objects.active = target
            target.select_set(True)
            result = bpy.ops.projection_paint.toggle_paint()
            if result != {"FINISHED"}:
                raise RuntimeError(f"Paint 진입이 실패했습니다: {result}")
            STATE["entered"] = True
            return 0.1
        if settings.paint_state != "ACTIVE":
            STATE["waits"] += 1
            if STATE["waits"] < 100:
                return 0.1
            raise RuntimeError("Paint 진입 완료 시간이 초과되었습니다.")
        overlay_size = tuple(int(value) for value in settings.overlay_image.size)
        if overlay_size != (1024, 1024):
            raise RuntimeError(f"선택한 1K와 Overlay 크기가 다릅니다: {overlay_size}")
        _write_result(
            {
                "success": True,
                "selection": settings.overlay_resolution,
                "overlay_size": overlay_size,
                "target": settings.paint_target.name,
            }
        )
    except Exception as exc:
        _write_result(
            {
                "success": False,
                "error": str(exc),
                "traceback": traceback.format_exc(),
            }
        )
    bpy.ops.wm.quit_blender()
    return None


bpy.app.timers.register(_run, first_interval=0.5)
