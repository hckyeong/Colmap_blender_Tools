"""실제 설치본의 Native Texture Export 응답성과 안전한 취소를 검증합니다."""

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
        "COLMAP_TOOLS_NATIVE_EXPORT_GUI_RESULT",
        str(PROJECT_ROOT / "build" / "native_texture_export_gui.json"),
    )
)
OUTPUT_DIRECTORY = PROJECT_ROOT / "build" / ".native_texture_export_gui"
INSTALLED_MODULE = os.environ.get(
    "COLMAP_TOOLS_INSTALLED_MODULE",
    "bl_ext.user_default.colmap_tools",
)
EXPECTED_VERSION = os.environ.get("COLMAP_TOOLS_EXPECTED_VERSION", "0.3.32")
RUN_MODE = os.environ.get("COLMAP_TOOLS_NATIVE_EXPORT_GUI_MODE", "SUCCESS").upper()
CANCEL_STAGE = os.environ.get("COLMAP_TOOLS_NATIVE_EXPORT_CANCEL_STAGE", "COLOR").upper()
TIMEOUT_SECONDS = float(os.environ.get("COLMAP_TOOLS_NATIVE_EXPORT_TIMEOUT", "180"))

_STATE: dict[str, object] = {
    "started_at": 0.0,
    "before": (),
    "before_images": (),
    "phase_events": [],
    "activity_labels": [],
    "responsive_bake_ticks": 0,
    "native_running_ticks": 0,
    "cancel_sent": False,
    "output_path": None,
    "export_module": None,
    "align_restore": "unknown",
}


def _pointer(value) -> int:
    """유효한 Blender RNA 객체 Pointer를 안전한 정수로 반환합니다."""
    try:
        return int(value.as_pointer()) if value is not None else 0
    except (AttributeError, ReferenceError, RuntimeError, TypeError, ValueError):
        return 0


def _state_signature(settings) -> tuple:
    """Export 전후에 보존되어야 할 Layer와 Paint 상태를 반환합니다."""
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


def _image_signature() -> tuple[int, ...]:
    """Export 전에 존재한 Image 데이터 블록 Pointer 집합을 반환합니다."""
    return tuple(sorted(_pointer(image) for image in bpy.data.images if _pointer(image)))


def _write_result(payload: dict) -> None:
    """GUI 검증 결과를 UTF-8 JSON으로 기록합니다."""
    RESULT_PATH.parent.mkdir(parents=True, exist_ok=True)
    RESULT_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _finish(payload: dict) -> None:
    """검증 결과를 기록하고 임시 출력 제거 뒤 Blender를 종료합니다."""
    try:
        _write_result(payload)
    finally:
        shutil.rmtree(OUTPUT_DIRECTORY, ignore_errors=True)
        bpy.ops.wm.quit_blender()


def _fail(exc: Exception) -> None:
    """예외와 현재 상태를 결과 파일에 기록한 뒤 Blender를 종료합니다."""
    _finish(
        {
            "success": False,
            "mode": RUN_MODE,
            "cancel_stage": CANCEL_STAGE,
            "error": str(exc),
            "traceback": traceback.format_exc(),
            "phase_events": list(_STATE["phase_events"]),
            "responsive_bake_ticks": int(_STATE["responsive_bake_ticks"]),
            "native_running_ticks": int(_STATE["native_running_ticks"]),
            "cancel_sent": bool(_STATE["cancel_sent"]),
            "elapsed_seconds": max(0.0, monotonic() - float(_STATE["started_at"] or 0.0)),
        }
    )


def _find_view3d_override() -> tuple[object, object, object]:
    """현재 Window에서 Operator Modal을 등록할 3D View 영역을 찾습니다."""
    window = bpy.context.window
    if window is None:
        raise RuntimeError("GUI Window가 없습니다.")
    for area in window.screen.areas:
        if area.type != "VIEW_3D":
            continue
        region = next((item for item in area.regions if item.type == "WINDOW"), None)
        if region is not None:
            return window, area, region
    raise RuntimeError("Texture Export를 실행할 3D View 영역이 없습니다.")


def _cancel_align_restore() -> str:
    """별도 GUI 검증 프로세스의 Align 자동 복원을 중단해 Export만 측정합니다."""
    try:
        runtime_module = importlib.import_module(
            f"{INSTALLED_MODULE}.tools.align.project_persistence.runtime"
        )
        return "cancelled" if runtime_module.cancel_project_restore() else "inactive"
    except Exception as exc:
        return f"unavailable: {exc}"


def _record_job_state(job, export_module) -> None:
    """Modal Timer가 관측한 상태 전이와 활동형 진행 정보를 누적합니다."""
    phase = str(job.phase)
    events = _STATE["phase_events"]
    if not events or events[-1]["phase"] != phase:
        events.append(
            {
                "phase": phase,
                "seconds": monotonic() - float(_STATE["started_at"]),
            }
        )
    if phase not in {"COLOR_BAKING", "ALPHA_BAKING"}:
        return
    _STATE["responsive_bake_ticks"] = int(_STATE["responsive_bake_ticks"]) + 1
    if export_module.native_object_bake_running():
        _STATE["native_running_ticks"] = int(_STATE["native_running_ticks"]) + 1
    view = export_module._progress_view(job.progress)
    if not view.indeterminate or not view.activity_text:
        raise RuntimeError("Native Bake 진행창이 활동형 표시를 사용하지 않습니다.")
    labels = _STATE["activity_labels"]
    if not labels or labels[-1] != view.activity_text:
        labels.append(str(view.activity_text))


def _request_cancel_if_needed(job) -> None:
    """지정한 Native Bake 단계에서 실제 ESC 입력을 한 번 전송합니다."""
    if RUN_MODE != "CANCEL" or bool(_STATE["cancel_sent"]):
        return
    expected_phase = f"{CANCEL_STAGE}_BAKING"
    if str(job.phase) != expected_phase or int(_STATE["responsive_bake_ticks"]) < 2:
        return
    window = bpy.context.window
    if window is None or not hasattr(window, "event_simulate"):
        raise RuntimeError("현재 Blender가 GUI ESC 입력 전송을 지원하지 않습니다.")
    window.event_simulate(type="ESC", value="PRESS")
    _STATE["cancel_sent"] = True


def _validate_finished(settings) -> dict:
    """성공·취소 종료 뒤 출력과 Blender 상태 불변성을 검증합니다."""
    export_module = _STATE["export_module"]
    output_path = Path(_STATE["output_path"])
    if export_module.texture_export_running():
        raise RuntimeError("Texture Export 예약이 종료 뒤에도 남아 있습니다.")
    if export_module._EXPORT_JOBS or export_module._EXPORT_PROGRESS:
        raise RuntimeError("Texture Export Job 또는 진행 Overlay가 종료 뒤에도 남아 있습니다.")
    if _state_signature(settings) != _STATE["before"]:
        raise RuntimeError("Native Texture Export가 Layer 또는 Paint 상태를 변경했습니다.")
    if _image_signature() != _STATE["before_images"]:
        raise RuntimeError("Native Texture Export 임시 Image가 종료 뒤에도 남아 있습니다.")
    phases = [event["phase"] for event in _STATE["phase_events"]]
    for required in ("COLOR_BAKING", "ALPHA_BAKING") if RUN_MODE == "SUCCESS" else (f"{CANCEL_STAGE}_BAKING",):
        if required not in phases:
            raise RuntimeError(f"필수 Native Bake 단계가 관측되지 않았습니다: {required}")
    if int(_STATE["responsive_bake_ticks"]) < 2:
        raise RuntimeError("Native Bake 중 GUI Timer가 충분히 갱신되지 않았습니다.")
    if int(_STATE["native_running_ticks"]) < 1:
        raise RuntimeError("Blender OBJECT_BAKE Job 실행 상태를 관측하지 못했습니다.")
    if RUN_MODE == "SUCCESS":
        for required in ("COMBINING", "WRITING", "STAGING", "COMMITTING"):
            if required not in phases:
                raise RuntimeError(f"필수 Export 후처리 단계가 관측되지 않았습니다: {required}")
        if not output_path.is_file() or output_path.stat().st_size <= 0:
            raise RuntimeError("Native Texture Export 출력 파일이 생성되지 않았습니다.")
    else:
        if not bool(_STATE["cancel_sent"]):
            raise RuntimeError("취소 검증에서 ESC 입력을 전송하지 못했습니다.")
        if output_path.exists():
            raise RuntimeError("취소한 Native Texture Export가 출력 파일을 남겼습니다.")
    return {
        "success": True,
        "mode": RUN_MODE,
        "cancel_stage": CANCEL_STAGE,
        "version": EXPECTED_VERSION,
        "blend": str(bpy.data.filepath),
        "align_restore": str(_STATE["align_restore"]),
        "phase_events": list(_STATE["phase_events"]),
        "activity_labels": list(_STATE["activity_labels"]),
        "responsive_bake_ticks": int(_STATE["responsive_bake_ticks"]),
        "native_running_ticks": int(_STATE["native_running_ticks"]),
        "cancel_sent": bool(_STATE["cancel_sent"]),
        "output_bytes": output_path.stat().st_size if output_path.is_file() else 0,
        "state_preserved": True,
        "temporary_images_removed": True,
        "elapsed_seconds": monotonic() - float(_STATE["started_at"]),
    }


def _probe() -> float | None:
    """Native Export 실행 중 상태와 UI 응답성을 주기적으로 관측합니다."""
    try:
        export_module = _STATE["export_module"]
        jobs = tuple(export_module._EXPORT_JOBS.values())
        if jobs:
            job = jobs[0]
            _record_job_state(job, export_module)
            _request_cancel_if_needed(job)
        if export_module.texture_export_running():
            if monotonic() - float(_STATE["started_at"]) > TIMEOUT_SECONDS:
                raise RuntimeError("Native Texture Export GUI 검증 시간이 초과됐습니다.")
            return 0.05
        settings = bpy.context.scene.projection_paint
        _finish(_validate_finished(settings))
        return None
    except Exception as exc:
        _fail(exc)
        return None


def _start() -> None:
    """설치본 GUI Operator로 Native Texture Export를 시작합니다."""
    try:
        if RUN_MODE not in {"SUCCESS", "CANCEL"}:
            raise RuntimeError(f"지원하지 않는 GUI 검증 모드입니다: {RUN_MODE}")
        if CANCEL_STAGE not in {"COLOR", "ALPHA"}:
            raise RuntimeError(f"지원하지 않는 취소 단계입니다: {CANCEL_STAGE}")
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
        _STATE["align_restore"] = _cancel_align_restore()
        _settings, _graph_layers, _visible_layers, source_image = (
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
        output_path = OUTPUT_DIRECTORY / "native_export.png"
        _STATE["started_at"] = monotonic()
        _STATE["before"] = _state_signature(settings)
        _STATE["before_images"] = _image_signature()
        _STATE["output_path"] = output_path
        _STATE["export_module"] = export_module
        window, area, region = _find_view3d_override()
        with bpy.context.temp_override(window=window, area=area, region=region):
            result = bpy.ops.projection_paint.texture_export(
                "EXEC_DEFAULT",
                filepath=str(output_path),
                output_mode=output_mode,
                file_format="PNG",
                color_mode="RGBA",
                udim_naming="UDIM",
                atlas_max_size="8192",
                png_compression=15,
                jpeg_quality=90,
                overwrite=False,
            )
        if "RUNNING_MODAL" not in result:
            raise RuntimeError(f"Native Texture Export가 Modal로 시작되지 않았습니다: {result}")
        bpy.app.timers.register(_probe, first_interval=0.05)
    except Exception as exc:
        _fail(exc)


bpy.app.timers.register(_start, first_interval=1.0)
