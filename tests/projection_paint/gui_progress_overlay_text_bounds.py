"""설치본의 공통 진행 안내창 문자열 경계와 실제 화면을 검증합니다."""

from __future__ import annotations

import importlib
import json
import os
from pathlib import Path
import traceback

import bpy


INSTALLED_MODULE = os.environ.get(
    "COLMAP_TOOLS_INSTALLED_MODULE",
    "bl_ext.user_default.colmap_tools",
)
EXPECTED_VERSION = os.environ.get("COLMAP_TOOLS_EXPECTED_VERSION", "0.3.32")
RESULT_PATH = Path(
    os.environ.get(
        "COLMAP_TOOLS_PROGRESS_OVERLAY_RESULT",
        str(Path.cwd() / "build" / "progress_overlay_text_bounds.json"),
    )
)
SCREENSHOT_PATH = Path(
    os.environ.get(
        "COLMAP_TOOLS_PROGRESS_OVERLAY_SCREENSHOT",
        str(Path.cwd() / "build" / "progress_overlay_text_bounds.png"),
    )
)

_STATE: dict[str, object] = {
    "module": None,
    "view": None,
    "handle": None,
    "lines": (),
    "step": 0,
}


def _write_result(payload: dict[str, object]) -> None:
    """GUI 검증 결과를 UTF-8 JSON으로 기록합니다."""
    RESULT_PATH.parent.mkdir(parents=True, exist_ok=True)
    RESULT_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _remove_handler() -> None:
    """검증용 Draw Handler를 중복 호출에도 안전하게 제거합니다."""
    module = _STATE.get("module")
    handle = _STATE.get("handle")
    if module is not None and handle is not None:
        module.remove_progress_overlay_draw_handler(handle)
    _STATE["handle"] = None


def _finish(payload: dict[str, object]) -> None:
    """검증 자원을 정리하고 결과 기록 뒤 Blender를 종료합니다."""
    _remove_handler()
    _write_result(payload)
    bpy.ops.wm.quit_blender()


def _fail(exc: Exception) -> None:
    """검증 예외와 Traceback을 기록하고 Blender를 종료합니다."""
    _finish(
        {
            "success": False,
            "error": str(exc),
            "traceback": traceback.format_exc(),
        }
    )


def _draw_overlay() -> None:
    """현재 3D Viewport에 검증용 Native Bake 안내창을 그립니다."""
    module = _STATE.get("module")
    view = _STATE.get("view")
    if module is not None and view is not None:
        module.draw_progress_overlay(view)


def _start() -> float | None:
    """설치본을 확인하고 긴 Device 정보의 픽셀 폭 검증을 시작합니다."""
    try:
        addon = importlib.import_module(INSTALLED_MODULE)
        if str(getattr(addon, "__version__", "")) != EXPECTED_VERSION:
            raise RuntimeError(
                f"설치본 버전이 다릅니다: {getattr(addon, '__version__', '')}"
            )
        module = importlib.import_module(f"{INSTALLED_MODULE}.tools.projection_paint.blender.progress_overlay")
        view = module.native_bake_progress_view(
            title="Final Texture Export",
            phase="Color Bake",
            elapsed_seconds=15.3,
            detail="가시 Layer Graph의 Color를 EMIT로 계산하고 Blender Image로 변환 중",
            device_detail="Cycles OPTIX GPU로 베이크합니다: NVIDIA GeForce RTX 4090",
            image_size=(8192, 8192),
            tile_count=1,
            sample_count=1,
        )
        maximum_width = float(module.OVERLAY_WIDTH - (module._OVERLAY_PADDING * 2))
        source_lines = (
            (view.title, 18),
            (f"{view.phase} · {view.activity_text}", 15),
            (view.detail_lines[0], 12),
            (view.detail_lines[1], 11),
            (view.detail_lines[2], 11),
        )
        measured = []
        for text, size in source_lines:
            fitted = module._fit_text_to_width(text, size, maximum_width)
            module.blf.size(0, size)
            width = float(module.blf.dimensions(0, fitted)[0])
            if width > maximum_width:
                raise RuntimeError(f"안내 문구가 경계를 초과합니다: {width} > {maximum_width}")
            measured.append(
                {
                    "source": text,
                    "display": fitted,
                    "width": width,
                    "maximum_width": maximum_width,
                }
            )
        _STATE["module"] = module
        _STATE["view"] = view
        _STATE["lines"] = tuple(measured)
        _STATE["handle"] = module.add_progress_overlay_draw_handler(_draw_overlay)
        module.request_progress_overlay_redraw()
        _STATE["step"] = 1
        return None
    except Exception as exc:
        _fail(exc)
        return None


def _capture() -> float | None:
    """두 번의 화면 갱신 뒤 안내창 Screenshot과 성공 결과를 기록합니다."""
    try:
        if int(_STATE["step"]) == 1:
            bpy.ops.wm.redraw_timer(type="DRAW_WIN_SWAP", iterations=2)
            _STATE["step"] = 2
            return 0.3
        SCREENSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
        result = bpy.ops.screen.screenshot(filepath=str(SCREENSHOT_PATH))
        if result != {"FINISHED"} or not SCREENSHOT_PATH.is_file():
            raise RuntimeError(f"진행 안내창 Screenshot을 저장하지 못했습니다: {result}")
        _finish(
            {
                "success": True,
                "version": EXPECTED_VERSION,
                "screenshot": str(SCREENSHOT_PATH),
                "lines": list(_STATE["lines"]),
            }
        )
        return None
    except Exception as exc:
        _fail(exc)
        return None


bpy.app.timers.register(_start, first_interval=1.0)
bpy.app.timers.register(_capture, first_interval=1.6)
