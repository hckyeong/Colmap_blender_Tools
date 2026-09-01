"""실제 설치본의 Texture Export 단계별 실행 시간을 GUI에서 계측합니다."""

from __future__ import annotations

import importlib
import json
import os
from pathlib import Path
import shutil
from time import perf_counter
import traceback

import bpy


PROJECT_ROOT = Path(__file__).resolve().parents[2]
RESULT_PATH = PROJECT_ROOT / "build" / "texture_export_profile_8k.json"
OUTPUT_DIRECTORY = PROJECT_ROOT / "build" / ".texture_export_profile_8k"
INSTALLED_MODULE = os.environ.get(
    "COLMAP_TOOLS_INSTALLED_MODULE",
    "bl_ext.user_default.colmap_tools",
)
EVENTS: list[dict] = []
ORIGINALS: list[tuple[object, str, object]] = []


def _write_result(payload: dict) -> None:
    """계측 결과를 UTF-8 JSON으로 기록합니다."""
    RESULT_PATH.parent.mkdir(parents=True, exist_ok=True)
    RESULT_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _record_wrapper(module, name: str, label: str, detail=None) -> None:
    """지정 함수 호출 시간을 기록하는 임시 Wrapper를 설치합니다."""
    original = getattr(module, name)
    ORIGINALS.append((module, name, original))

    def measured(*args, **kwargs):
        """원본 결과와 예외를 보존하면서 경과 시간만 기록합니다."""
        started_at = perf_counter()
        try:
            return original(*args, **kwargs)
        finally:
            event = {
                "label": label,
                "seconds": perf_counter() - started_at,
            }
            if detail is not None:
                event.update(detail(args, kwargs))
            EVENTS.append(event)

    setattr(module, name, measured)


def _bake_detail(_args, kwargs) -> dict:
    """Color·Alpha Bake 채널 이름을 계측 Event에 추가합니다."""
    return {"channel": str(kwargs.get("channel", ""))}


def _restore_wrappers() -> None:
    """계측용 임시 Wrapper를 모두 원본 함수로 되돌립니다."""
    for module, name, original in reversed(ORIGINALS):
        setattr(module, name, original)
    ORIGINALS.clear()


def _cancel_align_restore() -> str:
    """별도 계측 프로세스의 Align 자동 복원을 중단해 Export 시간만 분리합니다."""
    try:
        runtime_module = importlib.import_module(
            f"{INSTALLED_MODULE}.tools.align.project_persistence.runtime"
        )
        cancelled = bool(runtime_module.cancel_project_restore())
        return "cancelled" if cancelled else "inactive"
    except Exception as exc:
        return f"unavailable: {exc}"


def _run() -> None:
    """실데이터 SINGLE Texture Export를 실행하고 단계별 시간을 확정합니다."""
    total_started_at = perf_counter()
    try:
        addon = importlib.import_module(INSTALLED_MODULE)
        export_module = importlib.import_module(
            f"{INSTALLED_MODULE}.tools.projection_paint.blender.texture_export"
        )
        composite_module = importlib.import_module(
            f"{INSTALLED_MODULE}.tools.projection_paint.blender.composite_layer"
        )
        backend_module = importlib.import_module(
            f"{INSTALLED_MODULE}.tools.projection_paint.blender.image_backend"
        )
        align_restore = _cancel_align_restore()
        settings = bpy.context.scene.projection_paint
        _settings, _graph_layers, visible_layers, source_image = (
            export_module._export_graph_inputs(bpy.context.scene)
        )
        contract = backend_module.image_contract(source_image)
        output_mode = (
            "SINGLE"
            if contract.backend == backend_module.SINGLE_BACKEND
            else "UDIM"
        )
        shutil.rmtree(OUTPUT_DIRECTORY, ignore_errors=True)
        OUTPUT_DIRECTORY.mkdir(parents=True, exist_ok=False)

        _record_wrapper(export_module, "_export_graph_inputs", "prepare_graph_inputs")
        _record_wrapper(
            export_module,
            "create_final_texture_export_image",
            "create_final_export_image",
        )
        _record_wrapper(composite_module, "_bake_texture_export_channel", "bake_channel", _bake_detail)
        _record_wrapper(composite_module, "combine_color_alpha_numpy", "combine_color_alpha")
        _record_wrapper(export_module, "save_single_image", "save_single_image")
        _record_wrapper(export_module, "save_image_tiles", "save_image_tiles")
        _record_wrapper(export_module, "commit_staged_files", "commit_staged_files")
        _record_wrapper(
            export_module,
            "remove_final_texture_export_image",
            "remove_final_export_image",
        )

        export_started_at = perf_counter()
        paths = export_module.export_texture_sync(
            bpy.context,
            str(OUTPUT_DIRECTORY / "profile.png"),
            output_mode=output_mode,
            file_format="PNG",
            color_mode="RGBA",
            udim_naming="UDIM",
            atlas_max_size=8192,
            png_compression=15,
            jpeg_quality=90,
            overwrite=False,
        )
        export_seconds = perf_counter() - export_started_at
        output_bytes = [path.stat().st_size for path in paths]
        _restore_wrappers()
        _write_result(
            {
                "success": True,
                "version": str(addon.__version__),
                "blend": str(bpy.data.filepath),
                "align_restore": align_restore,
                "backend": str(contract.backend),
                "tile_sizes": [list(size) for size in contract.tile_sizes],
                "visible_layers": [str(layer.name) for layer in visible_layers],
                "output_bytes": output_bytes,
                "export_seconds": export_seconds,
                "process_seconds": perf_counter() - total_started_at,
                "events": EVENTS,
            }
        )
    except Exception as exc:
        _restore_wrappers()
        _write_result(
            {
                "success": False,
                "error": str(exc),
                "traceback": traceback.format_exc(),
                "process_seconds": perf_counter() - total_started_at,
                "events": EVENTS,
            }
        )
    finally:
        shutil.rmtree(OUTPUT_DIRECTORY, ignore_errors=True)
        bpy.ops.wm.quit_blender()


bpy.app.timers.register(_run, first_interval=1.0)
