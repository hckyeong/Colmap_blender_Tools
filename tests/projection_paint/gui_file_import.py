"""격리된 실제 UI에서 직접 입력·카메라 연결·OBJ·재입력·페인트 준비를 검증합니다."""

from __future__ import annotations

import json
from pathlib import Path
import sys
from time import perf_counter
import traceback

import bpy

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools import projection_paint as painter
from tools.projection_paint.blender.import_progress_session import active_import_progress_session
from tools.projection_paint.blender.snapshot_update import snapshot_is_ready
from tools.projection_paint.blender.camera_scene import projection_camera_session_is_active

OUTPUT = ROOT / "build" / "verification" / "painter-file-import-20261005"
STATE = {"phase": 0, "started": perf_counter(), "result": {}}


def _finish(success: bool, error: str = "") -> None:
    """실행 결과를 기록하고 테스트 전용 Blender만 종료합니다."""
    payload = dict(STATE["result"], success=success, error=error, elapsed_seconds=perf_counter() - STATE["started"])
    (OUTPUT / "gui.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    bpy.ops.wm.quit_blender()


def _step():
    """실제 작업공간 진입과 입력 타이머 완료를 기다려 순서대로 확인합니다."""
    try:
        if perf_counter() - STATE["started"] > 120:
            raise TimeoutError(f"UI 검증 대기 초과: {STATE['phase']}")
        if STATE["phase"] == 0:
            painter.register()
            assert bpy.ops.projection_paint.open_workspace() == {"FINISHED"}
            STATE["phase"] = 1
            return 0.3
        scene = bpy.context.scene
        settings = scene.projection_paint
        if STATE["phase"] == 1:
            if not projection_camera_session_is_active(scene):
                return 0.2
            settings.input_format = "TEXT"
            STATE["result"]["workspace"] = bpy.context.workspace.name
            STATE["import_started"] = perf_counter()
            assert bpy.ops.projection_paint.select_colmap_directory(
                directory=str(OUTPUT / "fixture"), target="ROOT", import_after_select=True,
            ) == {"FINISHED"}
            assert active_import_progress_session() is not None
            STATE["phase"] = 2
            return 0.2
        if STATE["phase"] == 2:
            if active_import_progress_session() is not None:
                return 0.2
            assert snapshot_is_ready(scene), settings.status
            assert settings.status_kind != "WARNING", settings.status
            assert settings.current_image_id == "1", settings.status
            assert scene.camera is not None and scene.camera.get("pp_owner") == "blender_projection_painting"
            STATE["result"]["modal_import_seconds"] = perf_counter() - STATE["import_started"]
            path = OUTPUT / "gui-model.obj"
            path.write_text("v 0 0 0\nv 1 0 0\nv 1 1 0\nv 0 1 0\nvt 0 0\nvt 1 0\nvt 1 1\nvt 0 1\nf 1/1 2/2 3/3 4/4\n", encoding="utf-8")
            assert bpy.ops.projection_paint.import_obj(filepath=str(path)) == {"FINISHED"}, settings.status
            model = next(obj for obj in scene.objects if obj.get("pp_record_kind") == "MODEL_OBJECT")
            settings.paint_target = model
            STATE["model_name"] = model.name
            STATE["phase"] = 3
            assert bpy.ops.projection_paint.import_colmap("INVOKE_DEFAULT") == {"RUNNING_MODAL"}
            return 0.2
        if STATE["phase"] == 3:
            if active_import_progress_session() is not None:
                return 0.2
            assert snapshot_is_ready(scene) and settings.paint_target.name == STATE["model_name"]
            assert settings.paint_target.visible_get(view_layer=bpy.context.view_layer)
            STATE["result"].update(ready=True, current_image=settings.current_image_id, points=settings.snapshot_point_count,
                                    model_preserved=True, model_visible=True, camera_connected=True)
            bpy.ops.wm.save_as_mainfile(filepath=str(OUTPUT / "gui-direct.blend"))
            _finish(True)
            return None
    except Exception:
        _finish(False, traceback.format_exc())
        return None


bpy.app.timers.register(_step, first_interval=0.3)
