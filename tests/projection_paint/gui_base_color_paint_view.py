"""Blender GUI에서 Paint 뷰포트의 Base Color 표시와 배경 토글을 검증합니다."""

from __future__ import annotations

import json
from pathlib import Path
import sys
import traceback

import bpy


PROJECT_ROOT = Path(__file__).resolve().parents[2]
RESULT_PATH = Path(r"C:\tmp\projection_paint_base_color_view.json")
sys.path.insert(0, str(PROJECT_ROOT))

from tools import projection_paint as addon  # noqa: E402
from tools.projection_paint.blender.material_setup import (  # noqa: E402
    prepare_active_material,
)
from tools.projection_paint.blender.paint_session import (  # noqa: E402
    PaintSession,
    _capture_snapshot,
    _prepare_paint_view,
    _restore_snapshot,
)
from tools.projection_paint.blender.paint_workspace import (  # noqa: E402
    configure_paint_workspace,
    ensure_paint_workspace,
)


STATE = {"step": 0, "setup": None, "target": None}
COMBINED_BACKGROUND_COLOR = (0.18, 0.18, 0.18)


def _write_result(payload: dict) -> None:
    """검증 결과를 UTF-8 JSON 파일로 기록합니다."""
    RESULT_PATH.parent.mkdir(parents=True, exist_ok=True)
    RESULT_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _new_target() -> bpy.types.Object:
    """UV를 가진 Base Color 뷰포트 검증용 Mesh를 생성합니다."""
    mesh = bpy.data.meshes.new("PP_BASE_COLOR_VIEW_MESH")
    mesh.from_pydata(
        ((-1.0, -1.0, 0.0), (1.0, -1.0, 0.0), (1.0, 1.0, 0.0), (-1.0, 1.0, 0.0)),
        (),
        ((0, 1, 2, 3),),
    )
    mesh.uv_layers.new(name="UVMap")
    target = bpy.data.objects.new("PP_BASE_COLOR_VIEW_TARGET", mesh)
    bpy.context.scene.collection.objects.link(target)
    return target


def _same_color(left, right) -> bool:
    """두 Blender RGB 색상이 충분히 가까운지 판정합니다."""

    return len(left) == len(right) == 3 and all(
        abs(float(actual) - float(expected)) < 1e-6
        for actual, expected in zip(left, right)
    )


def _run() -> float | None:
    """Paint View의 Diffuse Color 표시와 Combined 회색 배경 승계를 검증합니다."""
    try:
        if STATE["step"] == 0:
            addon.register()
            target = _new_target()
            setup = prepare_active_material(target, 32, 32)
            settings = bpy.context.scene.projection_paint
            settings.paint_target = target
            settings.review_background_enabled = True
            bpy.context.view_layer.objects.active = target
            target.select_set(True)
            window = bpy.context.window
            layout_area = next(
                candidate
                for candidate in window.screen.areas
                if candidate.type == "VIEW_3D"
            )
            layout_region = next(
                candidate
                for candidate in layout_area.regions
                if candidate.type == "WINDOW"
            )
            layout_shading = layout_area.spaces.active.shading
            layout_shading.background_type = "VIEWPORT"
            layout_shading.background_color = COMBINED_BACKGROUND_COLOR
            with bpy.context.temp_override(
                window=window,
                screen=window.screen,
                area=layout_area,
                region=layout_region,
            ):
                _capture_snapshot(bpy.context, target)
            if settings.review_background_enabled:
                raise RuntimeError("Paint 진입 시 Show Background Image가 꺼지지 않았습니다.")
            ensure_paint_workspace(bpy.context)
            STATE["target"] = target
            STATE["setup"] = setup
            STATE["step"] = 1
            return 0.1
        if STATE["step"] == 1:
            if not configure_paint_workspace(bpy.context):
                return 0.1
            STATE["step"] = 2
            return 0.1
        window = bpy.context.window
        area = next(candidate for candidate in window.screen.areas if candidate.type == "VIEW_3D")
        shading = area.spaces.active.shading
        shading.type = "SOLID"
        shading.render_pass = "COMBINED"
        shading.background_type = "THEME"
        session = PaintSession(bpy.context.scene, STATE["setup"], "ACTIVE")
        _prepare_paint_view(bpy.context, session)
        paint_state = {
            "shading": shading.type,
            "render_pass": shading.render_pass,
            "background_type": shading.background_type,
            "background_color": tuple(shading.background_color),
            "background_enabled": bool(
                bpy.context.scene.projection_paint.review_background_enabled
            ),
        }
        if paint_state["shading"] != "MATERIAL":
            raise RuntimeError("Paint View가 Material Preview가 아닙니다.")
        if paint_state["render_pass"] != "DIFFUSE_COLOR":
            raise RuntimeError("Paint View가 Diffuse Color Pass가 아닙니다.")
        if paint_state["background_type"] != "VIEWPORT":
            raise RuntimeError("Paint View 배경 유형이 Layout Viewport와 다릅니다.")
        if not _same_color(
            paint_state["background_color"],
            COMBINED_BACKGROUND_COLOR,
        ):
            raise RuntimeError("Paint View 배경색이 Combined 회색 배경과 다릅니다.")
        if paint_state["background_enabled"]:
            raise RuntimeError("Paint View에서 Show Background Image가 활성화되었습니다.")
        _restore_snapshot(bpy.context, STATE["setup"].overlay)
        restored_background = bool(
            bpy.context.scene.projection_paint.review_background_enabled
        )
        if not restored_background:
            raise RuntimeError("Paint 종료 시 원래 Background 토글이 복구되지 않았습니다.")
        if shading.type != "SOLID" or shading.render_pass != "COMBINED":
            raise RuntimeError("Paint 종료 후 원래 Viewport 표시 상태가 복원되지 않았습니다.")
        _write_result(
            {
                "success": True,
                "paint_view": paint_state,
                "restored_background": restored_background,
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
