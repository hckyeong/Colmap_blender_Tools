"""Blender GUI에서 기존 Paint 이미지 해상도 교체 경고창을 검증합니다."""

from __future__ import annotations

import json
from pathlib import Path
import sys
import traceback

import bpy


ROOT = Path(__file__).resolve().parents[2] / "tools" / "projection_paint"
RESULT_PATH = Path(r"C:\tmp\projection_paint_resolution_warning.json")
sys.path.insert(0, str(ROOT.parent))

from tools import projection_paint as addon  # noqa: E402
from tools.projection_paint.blender.material_setup import (  # noqa: E402
    prepare_active_material,
)


def _write_result(payload: dict) -> None:
    """검증 결과를 UTF-8 JSON 파일로 기록합니다."""
    RESULT_PATH.parent.mkdir(parents=True, exist_ok=True)
    RESULT_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _new_target() -> bpy.types.Object:
    """UV를 가진 경고창 검증용 Mesh를 생성합니다."""
    mesh = bpy.data.meshes.new("PP_RESOLUTION_WARNING_MESH")
    mesh.from_pydata(
        ((-1.0, -1.0, 0.0), (1.0, -1.0, 0.0), (1.0, 1.0, 0.0), (-1.0, 1.0, 0.0)),
        (),
        ((0, 1, 2, 3),),
    )
    mesh.uv_layers.new(name="UVMap")
    target = bpy.data.objects.new("PP_RESOLUTION_WARNING_TARGET", mesh)
    bpy.context.scene.collection.objects.link(target)
    return target


def _quit() -> None:
    """검증 결과 기록 뒤 Blender를 종료합니다."""
    bpy.ops.wm.quit_blender()


def _run() -> None:
    """1K 기존 작업에서 2K 선택 시 확인창과 무변경 상태를 검증합니다."""
    try:
        addon.register()
        target = _new_target()
        setup = prepare_active_material(target, 1024, 1024)
        setup.commit()
        previous = setup.overlay
        previous_pointer = previous.as_pointer()
        settings = bpy.context.scene.projection_paint
        settings.paint_target = target
        settings.overlay_resolution = "RES_2K"
        bpy.context.view_layer.objects.active = target
        target.select_set(True)
        result = bpy.ops.projection_paint.toggle_paint("INVOKE_DEFAULT")
        if result != {"RUNNING_MODAL"}:
            raise RuntimeError(f"해상도 교체 경고창이 열리지 않았습니다: {result}")
        if setup.image_node.image is not previous:
            raise RuntimeError("확인 전 기존 Paint 이미지 연결이 변경되었습니다.")
        if previous.as_pointer() != previous_pointer:
            raise RuntimeError("확인 전 기존 Paint 이미지가 폐기되었습니다.")
        _write_result(
            {
                "success": True,
                "operator_result": sorted(result),
                "current_size": tuple(int(value) for value in previous.size),
                "selected_resolution": settings.overlay_resolution,
                "unchanged_before_confirmation": True,
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
    bpy.app.timers.register(_quit, first_interval=0.5)


bpy.app.timers.register(_run, first_interval=0.5)
