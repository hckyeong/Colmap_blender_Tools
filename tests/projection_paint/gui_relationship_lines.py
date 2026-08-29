"""Blender GUI에서 COLMAP Import 관계선 표시 해제를 검증합니다."""

from __future__ import annotations

import json
from pathlib import Path
import sys
import traceback

import bpy


PROJECT_PARENT = Path(__file__).resolve().parents[2]
if str(PROJECT_PARENT) not in sys.path:
    sys.path.insert(0, str(PROJECT_PARENT))

from tools.projection_paint.blender.camera_import import (
    _hide_camera_relationship_lines,
)


RESULT_PATH = Path(r"C:\tmp\projection_paint_relationship_lines.json")


def _write_result(payload: dict) -> None:
    """GUI 검증 결과를 UTF-8 JSON 파일로 기록합니다."""
    RESULT_PATH.parent.mkdir(parents=True, exist_ok=True)
    RESULT_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _run() -> None:
    """실제 3D Viewport의 관계선 Overlay가 꺼지는지 확인합니다."""
    try:
        window = bpy.context.window
        viewports = tuple(
            area for area in window.screen.areas if area.type == "VIEW_3D"
        )
        if not viewports:
            raise RuntimeError("검증할 3D Viewport가 없습니다.")
        for area in viewports:
            area.spaces.active.overlay.show_relationship_lines = True
        _hide_camera_relationship_lines(bpy.context)
        states = [
            bool(area.spaces.active.overlay.show_relationship_lines)
            for area in viewports
        ]
        if any(states):
            raise RuntimeError("COLMAP Import 완료 뒤 관계선 Overlay가 남아 있습니다.")
        _write_result(
            {
                "success": True,
                "viewport_count": len(viewports),
                "relationship_lines": states,
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


bpy.app.timers.register(_run, first_interval=0.5)
