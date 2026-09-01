"""실제 설치본과 저장 프로젝트에서 가시 Layer Texture Export 상태 보존을 검증합니다."""

from __future__ import annotations

import importlib
import json
import os
from pathlib import Path
import shutil
from time import monotonic
import traceback

import bpy


PROJECT_ROOT = Path(__file__).resolve().parents[2]
RESULT_PATH = Path(
    os.environ.get(
        "COLMAP_TOOLS_VISIBLE_EXPORT_GUI_RESULT",
        str(PROJECT_ROOT / "build" / "visible_layer_texture_export_gui.json"),
    )
)
OUTPUT_DIRECTORY = PROJECT_ROOT / "build" / ".visible_layer_texture_export_gui"
INSTALLED_MODULE = os.environ.get(
    "COLMAP_TOOLS_INSTALLED_MODULE",
    "bl_ext.user_default.colmap_tools",
)
EXPECTED_VERSION = os.environ.get("COLMAP_TOOLS_EXPECTED_VERSION", "0.3.32")


def _pointer(value) -> int:
    """유효한 Blender RNA 객체의 Pointer를 정수로 반환합니다."""
    return int(value.as_pointer()) if value is not None else 0


def _state_signature(settings) -> tuple:
    """Export 전후 비교할 Layer Tree·선택·단독 보기 상태를 반환합니다."""
    return (
        tuple(
            (
                _pointer(layer),
                str(layer.layer_uuid),
                str(layer.layer_role),
                bool(layer.visible),
                bool(getattr(layer, "ui_selected", False)),
                _pointer(getattr(layer, "image", None)),
            )
            for layer in settings.layers
        ),
        str(settings.composite_state),
        str(getattr(settings, "active_composite_session_uuid", "") or ""),
        int(getattr(settings, "active_layer_index", -1)),
        str(getattr(settings, "active_mask_uuid", "") or ""),
        str(getattr(settings, "layer_preview_uuid", "") or ""),
        str(getattr(settings, "mask_preview_adjustment_uuid", "") or ""),
        bool(getattr(settings, "original_layer_preview", False)),
        _pointer(getattr(settings, "active_paint_image", None)),
        _pointer(getattr(settings, "overlay_image", None)),
    )


def _write_result(payload: dict) -> None:
    """GUI 검증 결과를 UTF-8 JSON으로 기록합니다."""
    RESULT_PATH.parent.mkdir(parents=True, exist_ok=True)
    RESULT_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _run() -> None:
    """설치본의 실데이터 Export와 상태 불변성을 한 번 검증하고 Blender를 종료합니다."""
    started_at = monotonic()
    try:
        addon = importlib.import_module(INSTALLED_MODULE)
        if str(getattr(addon, "__version__", "")) != EXPECTED_VERSION:
            raise RuntimeError(
                f"설치본 버전이 다릅니다: {getattr(addon, '__version__', '')}"
            )
        export_module = importlib.import_module(
            f"{INSTALLED_MODULE}.tools.projection_paint.blender.texture_export"
        )
        backend_module = importlib.import_module(
            f"{INSTALLED_MODULE}.tools.projection_paint.blender.image_backend"
        )
        settings = getattr(bpy.context.scene, "projection_paint", None)
        if settings is None or len(settings.layers) == 0:
            raise RuntimeError("실데이터 Projection Paint Layer가 없습니다.")
        before = _state_signature(settings)
        _settings, _graph_layers, visible_layers, source_image = (
            export_module._export_graph_inputs(bpy.context.scene)
        )
        contract = backend_module.image_contract(source_image)
        output_mode = "SINGLE" if contract.backend == backend_module.SINGLE_BACKEND else "UDIM"
        OUTPUT_DIRECTORY.mkdir(parents=True, exist_ok=False)
        paths = export_module.export_texture_sync(
            bpy.context,
            str(OUTPUT_DIRECTORY / "visible_layer_actual.png"),
            output_mode=output_mode,
            file_format="PNG",
            color_mode="RGBA",
            udim_naming="UDIM",
            atlas_max_size=8192,
            png_compression=15,
            jpeg_quality=90,
            overwrite=False,
        )
        if not paths or any(not path.is_file() or path.stat().st_size <= 0 for path in paths):
            raise RuntimeError("실데이터 Texture 출력 파일이 생성되지 않았습니다.")
        after = _state_signature(settings)
        if after != before:
            raise RuntimeError("Texture Export가 Layer Tree 또는 Paint 상태를 변경했습니다.")
        _write_result(
            {
                "success": True,
                "version": str(addon.__version__),
                "blend": str(bpy.data.filepath),
                "composite_state": str(settings.composite_state),
                "visible_layers": [str(layer.name) for layer in visible_layers],
                "tile_sizes": [list(size) for size in contract.tile_sizes],
                "backend": str(contract.backend),
                "output_count": len(paths),
                "output_bytes": [path.stat().st_size for path in paths],
                "state_preserved": True,
                "elapsed_seconds": monotonic() - started_at,
            }
        )
    except Exception as exc:
        _write_result(
            {
                "success": False,
                "error": str(exc),
                "traceback": traceback.format_exc(),
                "elapsed_seconds": monotonic() - started_at,
            }
        )
    finally:
        shutil.rmtree(OUTPUT_DIRECTORY, ignore_errors=True)
        bpy.ops.wm.quit_blender()


bpy.app.timers.register(_run, first_interval=1.0)
