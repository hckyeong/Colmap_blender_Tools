"""Blender GUI에서 현재 Stencil Source의 Target 일괄 투영을 검증합니다."""

from __future__ import annotations

import json
from pathlib import Path
import traceback

import bpy


RESULT_PATH = Path(r"C:\tmp\projection_paint_project_stencil.json")
STATE = {"cleaned": False, "entered": False, "waits": 0}


def _write_result(payload: dict) -> None:
    """검증 결과를 UTF-8 JSON 파일로 기록합니다."""
    RESULT_PATH.parent.mkdir(parents=True, exist_ok=True)
    RESULT_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _run() -> float | None:
    """실제 Texture Paint 문맥에서 Project Stencil Operator를 실행합니다."""
    try:
        settings = bpy.context.scene.projection_paint
        if not STATE["cleaned"]:
            if bool(settings.paint_enabled):
                bpy.ops.projection_paint.toggle_paint()
                STATE["cleaned"] = True
                return 0.1
            STATE["cleaned"] = True
        if not STATE["entered"]:
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
        from bl_ext.user_default.colmap_tools.tools.projection_paint.blender.paint_runtime import (
            active_paint_runtime,
        )

        runtime = active_paint_runtime()
        if runtime is None or runtime.stencil.source_image is None:
            raise RuntimeError("현재 Stencil Source Image가 없습니다.")
        overlay = settings.overlay_image
        before_revision = int(overlay.get("pp_content_revision", 0))
        result = bpy.ops.projection_paint.project_stencil()
        _write_result(
            {
                "success": result == {"FINISHED"},
                "result": sorted(result),
                "source_image": runtime.stencil.source_image.name,
                "overlay_dirty": bool(overlay.is_dirty),
                "revision_incremented": (
                    int(overlay.get("pp_content_revision", 0))
                    == before_revision + 1
                ),
                "status": str(settings.status),
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
