"""Blender GUI에서 Paint Layer Shader Node 배치와 비중첩을 검증합니다."""

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
        "PP_LAYER_NODE_RESULT",
        r"C:\tmp\projection_paint_layer_nodes.json",
    )
)
SCREENSHOT_PATH = Path(
    os.environ.get(
        "PP_LAYER_NODE_SCREENSHOT",
        r"C:\tmp\projection_paint_layer_nodes.png",
    )
)
sys.path.insert(0, str(ROOT.parent))

STATE = {"step": 0}


def _write_result(payload: dict) -> None:
    """GUI 검증 결과를 UTF-8 JSON 파일로 기록합니다."""
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


def _new_target():
    """Shader GUI 검증용 UV Quad와 Principled Material을 생성합니다."""
    mesh = bpy.data.meshes.new("LayerNodeTargetMesh")
    mesh.from_pydata(
        (
            (-1.0, -1.0, 0.0),
            (1.0, -1.0, 0.0),
            (1.0, 1.0, 0.0),
            (-1.0, 1.0, 0.0),
        ),
        (),
        ((0, 1, 2, 3),),
    )
    mesh.uv_layers.new(name="UVMap")
    target = bpy.data.objects.new("LayerNodeTarget", mesh)
    bpy.context.scene.collection.objects.link(target)
    material = bpy.data.materials.new("LayerNodeMaterial")
    material.use_nodes = True
    target.data.materials.append(material)
    target.select_set(True)
    bpy.context.view_layer.objects.active = target
    return target


def _prepare_layer_graph() -> None:
    """세 Layer와 Mask·보정 Node를 실제 관리 Material에 구성합니다."""
    from tools.projection_paint.blender.layer_shader import (
        reconcile_layer_graph,
    )
    from tools.projection_paint.blender.material_setup import (
        prepare_active_material,
    )
    from tools.projection_paint.blender.paint_state import (
        commit_paint_setup,
    )

    target = _new_target()
    setup = prepare_active_material(
        target,
        256,
        256,
        use_base_resolution=False,
    )
    commit_paint_setup(bpy.context.scene, setup)
    reconcile_layer_graph(
        bpy.context.scene,
        material=setup.material,
        overlay=setup.overlay,
    )
    setup.commit()
    settings = bpy.context.scene.projection_paint
    top = settings.layers[0]
    bpy.ops.projection_paint.layer_mask_add(layer_uuid=top.layer_uuid)
    for adjustment_type in ("BRIGHTNESS", "EXPOSURE", "SATURATION"):
        bpy.ops.projection_paint.layer_adjustment_add(
            layer_uuid=top.layer_uuid,
            adjustment_type=adjustment_type,
        )
    bpy.ops.projection_paint.adjustment_mask_add(
        layer_uuid=top.layer_uuid,
        adjustment_uuid=top.adjustments[0].adjustment_uuid,
    )
    bpy.ops.projection_paint.layer_select(layer_uuid=top.layer_uuid)
    bpy.ops.projection_paint.layer_add()
    bpy.ops.projection_paint.layer_add()
    bpy.ops.projection_paint.layer_select(layer_uuid=top.layer_uuid)


def _open_shader_editor() -> tuple[object, object]:
    """현재 Window의 가장 큰 Area를 관리 Material Shader Editor로 전환합니다."""
    window = bpy.context.window
    area = max(
        (
            candidate
            for candidate in window.screen.areas
            if candidate.type not in {"TOPBAR", "STATUSBAR"}
        ),
        key=lambda candidate: candidate.width * candidate.height,
    )
    area.type = "NODE_EDITOR"
    area.ui_type = "ShaderNodeTree"
    space = area.spaces.active
    space.tree_type = "ShaderNodeTree"
    space.shader_type = "OBJECT"
    region = next(
        candidate
        for candidate in area.regions
        if candidate.type == "WINDOW"
    )
    return area, region


def _rectangles_overlap(first, second) -> bool:
    """두 관리 Frame 예약 사각형의 내부가 교차하는지 확인합니다."""
    return not (
        first[3] <= second[1]
        or second[3] <= first[1]
        or first[4] <= second[2]
        or second[4] <= first[2]
    )


def _run() -> float | None:
    """Layer Graph를 구성하고 Shader Editor 화면과 비중첩 결과를 기록합니다."""
    try:
        if STATE["step"] == 0:
            try:
                bpy.ops.wm.splash_close()
            except (AttributeError, RuntimeError):
                pass
            _ensure_addon()
            _prepare_layer_graph()
            _open_shader_editor()
            STATE["step"] = 1
            return 0.5
        if STATE["step"] == 1:
            area = next(
                candidate
                for candidate in bpy.context.window.screen.areas
                if candidate.type == "NODE_EDITOR"
            )
            region = next(
                candidate
                for candidate in area.regions
                if candidate.type == "WINDOW"
            )
            with bpy.context.temp_override(
                window=bpy.context.window,
                screen=bpy.context.window.screen,
                area=area,
                region=region,
                space_data=area.spaces.active,
            ):
                if not bpy.ops.node.view_all.poll():
                    raise RuntimeError("Shader Editor의 View All Context가 준비되지 않았습니다.")
                bpy.ops.node.view_all()
            STATE["step"] = 2
            return 0.8
        screenshot_result = bpy.ops.screen.screenshot(
            filepath=str(SCREENSHOT_PATH),
        )
        from tools.projection_paint.blender.layer_shader import (
            managed_node_rectangles,
            validate_layer_graph,
        )

        settings = bpy.context.scene.projection_paint
        material = settings.paint_material
        validate_layer_graph(
            material,
            tuple(str(layer.layer_uuid) for layer in settings.layers),
        )
        rectangles = managed_node_rectangles(material)
        overlaps = [
            (first[0], second[0])
            for index, first in enumerate(rectangles)
            for second in rectangles[index + 1 :]
            if _rectangles_overlap(first, second)
        ]
        payload = {
            "success": True,
            "screenshot_result": sorted(screenshot_result),
            "layer_names": [layer.name for layer in settings.layers],
            "layer_images": [layer.image.name for layer in settings.layers],
            "frame_count": len(rectangles),
            "overlaps": overlaps,
            "screenshot": str(SCREENSHOT_PATH),
        }
        if screenshot_result != {"FINISHED"}:
            raise RuntimeError(f"Shader Node 캡처에 실패했습니다: {screenshot_result}")
        if payload["frame_count"] != 3 or overlaps:
            raise RuntimeError(f"관리 Layer Frame 배치가 잘못되었습니다: {payload}")
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
