"""실제 저장 프로젝트에서 설치본 Modifier 빠른 Runtime 복원 시간을 측정합니다."""

from __future__ import annotations

import json
import os
import time
from importlib import import_module
from pathlib import Path

import bpy


MODULE_NAME = os.environ.get(
    "COLMAP_TOOLS_INSTALLED_MODULE",
    "bl_ext.user_default.colmap_tools",
)
RESULT_PATH = Path(
    os.environ.get(
        "COLMAP_TOOLS_FAST_RESTORE_RESULT",
        str(Path.cwd() / "build" / "modifier_fast_restore_actual.json"),
    )
).resolve()

_STARTED_AT = 0.0
_STATUSES: list[str] = []
_LAST_STATUS = ""
_RUNTIME_STATE = None
_LAST_PHASE = ""
_PHASE_STARTED_AT = 0.0
_PHASE_TIMINGS: list[dict[str, float | str]] = []


def _write_result(payload: dict) -> None:
    """실데이터 복원 결과를 UTF-8 JSON으로 기록합니다."""

    RESULT_PATH.parent.mkdir(parents=True, exist_ok=True)
    RESULT_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _finish_failure(error: Exception | str) -> None:
    """실패 원인과 수집한 상태를 기록한 뒤 Blender를 종료합니다."""

    _write_result(
        {
            "ok": False,
            "error": str(error),
            "statuses": tuple(_STATUSES),
        }
    )
    bpy.ops.wm.quit_blender()


def _poll_restore() -> float | None:
    """Modifier Runtime 준비 완료까지 상태와 경과 시간을 수집합니다."""

    global _LAST_PHASE, _LAST_STATUS, _PHASE_STARTED_AT
    try:
        scene = bpy.context.scene
        settings = scene.colmap_import
        restore_job = _RUNTIME_STATE.runtime_restore_job(scene)
        phase = str(getattr(getattr(restore_job, "progress", None), "phase", ""))
        now = time.perf_counter()
        if phase and phase != _LAST_PHASE:
            if _LAST_PHASE:
                _PHASE_TIMINGS.append(
                    {
                        "phase": _LAST_PHASE,
                        "elapsed_seconds": round(now - _PHASE_STARTED_AT, 6),
                    }
                )
            _LAST_PHASE = phase
            _PHASE_STARTED_AT = now
        status = str(settings.status or "")
        if status and status != _LAST_STATUS:
            _STATUSES.append(status)
            _LAST_STATUS = status
        if str(settings.status_kind) == "ERROR":
            raise RuntimeError(status or "Modifier Runtime 복원 오류")
        if not _RUNTIME_STATE.cached_runtime_matches_scene(scene):
            if time.perf_counter() - _STARTED_AT > 180.0:
                raise TimeoutError("Modifier Runtime 복원이 180초 안에 끝나지 않았습니다.")
            return 0.05

        state = _RUNTIME_STATE.cached_runtime(str(settings.dataset_uuid))
        if state is None:
            raise RuntimeError("복원 완료 뒤 Modifier Runtime Cache를 찾지 못했습니다.")
        if _LAST_PHASE:
            _PHASE_TIMINGS.append(
                {
                    "phase": _LAST_PHASE,
                    "elapsed_seconds": round(now - _PHASE_STARTED_AT, 6),
                }
            )
        forbidden = tuple(
            item for item in _STATUSES if "Reconstruction 무결성 검사" in item
        )
        payload = {
            "ok": not forbidden,
            "version": import_module(MODULE_NAME).__version__,
            "blend_file": bpy.data.filepath,
            "elapsed_seconds": round(time.perf_counter() - _STARTED_AT, 6),
            "status_count": len(_STATUSES),
            "statuses": tuple(_STATUSES),
            "forbidden_integrity_statuses": forbidden,
            "camera_count": len(state.current_reconstruction.cameras),
            "image_count": len(state.current_reconstruction.images),
            "point3d_count": len(state.current_reconstruction.points3d),
            "edit_revision": state.edit_revision,
            "history_operation_count": len(state.operations),
            "phase_timings": tuple(_PHASE_TIMINGS),
            "dataset_root": str(settings.dataset_root),
            "model_directory": str(settings.active_model_directory),
            "images_directory": str(settings.active_images_directory),
        }
        _write_result(payload)
        bpy.ops.wm.quit_blender()
    except Exception as exc:
        _finish_failure(exc)
    return None


def _start_restore() -> float | None:
    """설치본과 저장 Source를 확인하고 Modifier Workspace 복원을 시작합니다."""

    global _LAST_PHASE, _PHASE_STARTED_AT, _STARTED_AT, _RUNTIME_STATE
    try:
        module = import_module(MODULE_NAME)
        if module.__version__ != "0.3.33":
            raise RuntimeError(f"설치본 버전이 0.3.33이 아닙니다: {module.__version__}")
        settings = getattr(bpy.context.scene, "colmap_import", None)
        if settings is None or not str(settings.dataset_uuid or ""):
            raise RuntimeError("저장 프로젝트에 Modifier Dataset 정보가 없습니다.")
        _RUNTIME_STATE = import_module(
            f"{MODULE_NAME}.tools.modifier.blender.runtime_state"
        )
        _RUNTIME_STATE.clear_runtime()
        _STARTED_AT = time.perf_counter()
        _PHASE_STARTED_AT = _STARTED_AT
        _LAST_PHASE = ""
        result = bpy.ops.colmap.open_modifier_workspace()
        if "FINISHED" not in result:
            raise RuntimeError(f"Modifier Workspace 진입에 실패했습니다: {result}")
        bpy.app.timers.register(_poll_restore, first_interval=0.05)
    except Exception as exc:
        _finish_failure(exc)
    return None


bpy.app.timers.register(_start_restore, first_interval=1.0)
