"""Edit Mode에서 실제 Align 작업공간 진입 시 Object Mode 전환을 검증합니다."""

from __future__ import annotations

from importlib import import_module
import os
import sys
import traceback
from pathlib import Path

import bpy


_STAGE = "OPEN"
_TARGET = None
_USE_INSTALLED = os.environ.get("COLMAP_TOOLS_USE_INSTALLED") == "1"
_ALIGN_PACKAGE = os.environ.get(
    "COLMAP_TOOLS_ALIGN_PACKAGE",
    "tools.align",
)


def _add_source_path() -> None:
    """통합 Source 패키지를 GUI 테스트 모듈 경로에 추가합니다."""

    if _USE_INSTALLED:
        return
    source = str(Path(__file__).resolve().parents[3])
    if source not in sys.path:
        sys.path.insert(0, source)


def _align_module(suffix: str):
    """Source 또는 실제 설치본에서 지정 Align 모듈을 불러옵니다."""

    module_name = _ALIGN_PACKAGE if not suffix else f"{_ALIGN_PACKAGE}.{suffix}"
    return import_module(module_name)


def _finish(succeeded: bool) -> None:
    """Align 등록과 테스트 프로세스를 정리하고 결과를 출력합니다."""

    registration = _align_module("registration")

    try:
        if registration.is_registered():
            registration.unregister()
    except Exception:
        traceback.print_exc()
        succeeded = False
    print(f"COLMAP_ALIGN_OBJECT_MODE_WORKSPACE_GUI={'PASS' if succeeded else 'FAIL'}")
    bpy.ops.wm.quit_blender()


def _run_step() -> float | None:
    """Edit Mode 진입 요청부터 Align 세션 활성화까지 단계적으로 검증합니다."""

    global _STAGE
    runtime = _align_module("state").RUNTIME

    try:
        if runtime.workspace_setup_error:
            raise AssertionError(runtime.workspace_setup_error)
        if _STAGE == "OPEN":
            assert bpy.context.mode == "EDIT_MESH"
            assert bpy.ops.colmap_align.open_workspace("EXEC_DEFAULT") == {"FINISHED"}
            _STAGE = "WAIT_READY"
            return 0.05
        if _STAGE == "WAIT_READY":
            if runtime.workspace_setup_task is not None or not runtime.session.session_active:
                return 0.05
            assert bpy.context.mode == "OBJECT"
            assert _TARGET is not None and _TARGET.mode == "OBJECT"
            _finish(True)
            return None
        raise AssertionError(f"알 수 없는 테스트 단계입니다: {_STAGE}")
    except Exception:
        traceback.print_exc()
        _finish(False)
        return None


def schedule_test() -> None:
    """숨김 GUI 이벤트 루프에서 실제 Align 진입 회귀를 예약합니다."""

    global _TARGET
    _add_source_path()
    if bpy.app.background:
        raise RuntimeError("Object Mode 작업공간 검사는 GUI Blender에서 실행해야 합니다.")
    registration = _align_module("registration")

    registration.register()
    bpy.ops.mesh.primitive_plane_add(size=2.0)
    _TARGET = bpy.context.active_object
    _TARGET.name = "COLMAP Align GUI Object Mode Target"
    assert bpy.ops.object.mode_set(mode="EDIT") == {"FINISHED"}
    bpy.app.timers.register(_run_step, first_interval=0.2)


if __name__ == "__main__":
    schedule_test()
