"""분리된 Blender 화면에서 실제 COLMAP 입력 시간과 탐색기 점 상한을 검증합니다."""

import argparse
import json
from pathlib import Path
import sys
from time import perf_counter
import traceback

import bpy


def arguments():
    """기준 소스·원본 자료와 측정 결과 위치를 명시적으로 읽습니다."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--dataset-root", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--point-limit", type=int, default=20_000)
    parser.add_argument("--browser", action="store_true")
    parser.add_argument("--repeats", type=int, default=3)
    return parser.parse_args(sys.argv[sys.argv.index("--") + 1:])


ARGS = arguments()
sys.path.insert(0, str(ARGS.source_root.resolve()))
ARGS.output_root.mkdir(parents=True, exist_ok=True)
from tools import projection_paint as painter
from tools.projection_paint.blender import camera_import
from tools.projection_paint.blender.import_progress_session import active_import_progress_session
from tools.projection_paint.blender.snapshot_update import snapshot_is_ready
from tools.projection_paint.blender.camera_scene import projection_camera_session_is_active

STATE = {"phase": "START", "started": perf_counter(), "runs": [], "calls": 0, "redraws": 0}
ORIGINAL_ADVANCE = camera_import._advance_import_session
ORIGINAL_REDRAW = camera_import.request_progress_overlay_redraw


def measured_advance(context, session):
    """검사 프로세스에서만 이벤트 처리 횟수와 가장 긴 작업 시간을 집계합니다."""
    started = perf_counter()
    ORIGINAL_ADVANCE(context, session)
    STATE["calls"] += 1
    STATE["longest_call"] = max(STATE.get("longest_call", 0), perf_counter() - started)


def measured_redraw():
    """검사 프로세스에서만 화면 갱신 요청 횟수를 집계합니다."""
    STATE["redraws"] += 1
    return ORIGINAL_REDRAW()


camera_import._advance_import_session = measured_advance
camera_import.request_progress_overlay_redraw = measured_redraw


def finish(success, error=""):
    """결과를 기록하고 사용자 Blender와 별개인 검사 프로세스만 종료합니다."""
    payload = {"success": success, "error": error, "source_root": str(ARGS.source_root), "dataset_root": str(ARGS.dataset_root), "runs": STATE["runs"], "browser_verified": STATE.get("browser_verified", False)}
    (ARGS.output_root / "gui.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    bpy.ops.wm.quit_blender()


def start_import():
    """준비된 작업공간에서 실제 가져오기 버튼과 같은 모달 실행을 시작합니다."""
    STATE.update(import_started=perf_counter(), calls=0, redraws=0, longest_call=0, phase="IMPORT")
    assert bpy.ops.projection_paint.import_colmap("INVOKE_DEFAULT") == {"RUNNING_MODAL"}


def step():
    """탐색기 취소·실제 입력·사진 연결·반복 입력을 실제 이벤트 루프에서 검증합니다."""
    try:
        if perf_counter() - STATE["started"] > 900:
            raise TimeoutError("화면 가져오기 측정 시간이 초과되었습니다.")
        if STATE["phase"] == "START":
            painter.register()
            assert bpy.ops.projection_paint.open_workspace() == {"FINISHED"}
            STATE["phase"] = "WORKSPACE"
            return 0.3
        scene = bpy.context.scene
        settings = scene.projection_paint
        if STATE["phase"] == "WORKSPACE":
            if not projection_camera_session_is_active(scene):
                return 0.1
            settings.dataset_root = str(ARGS.dataset_root)
            settings.input_format = "AUTO"
            if hasattr(settings, "import_point_limit"):
                settings.import_point_limit = ARGS.point_limit
            if ARGS.browser:
                assert bpy.ops.projection_paint.select_colmap_directory("INVOKE_DEFAULT", target="ROOT", import_after_select=True) == {"RUNNING_MODAL"}
                STATE["phase"] = "BROWSER"
                return 0.5
            start_import()
            return 0.1
        if STATE["phase"] == "BROWSER":
            for window in bpy.context.window_manager.windows:
                for area in window.screen.areas:
                    if area.type != "FILE_BROWSER":
                        continue
                    operator = area.spaces.active.active_operator
                    assert operator.point_limit == ARGS.point_limit
                    with bpy.context.temp_override(window=window, area=area):
                        bpy.ops.screen.screenshot(filepath=str((ARGS.output_root / "browser.png").resolve()))
                        operator.point_limit = 123
                        assert settings.import_point_limit == ARGS.point_limit
                        assert bpy.ops.file.cancel() == {"FINISHED"}
                    assert settings.import_point_limit == ARGS.point_limit
                    STATE["browser_verified"] = True
                    STATE["phase"] = "AFTER_BROWSER"
                    return 0.3
            return 0.1
        if STATE["phase"] == "AFTER_BROWSER":
            start_import()
            return 0.1
        if STATE["phase"] == "IMPORT":
            if active_import_progress_session() is not None:
                return 0.1
            assert snapshot_is_ready(scene), settings.status
            assert settings.status_kind != "WARNING", settings.status
            assert scene.camera is not None and scene.camera.get("pp_record_kind") == "CAMERA_OBJECT"
            STATE["runs"].append({"seconds": perf_counter() - STATE["import_started"], "points": settings.snapshot_point_count, "images": settings.snapshot_image_count, "calls": STATE["calls"], "redraws": STATE["redraws"], "longest_call_seconds": STATE["longest_call"]})
            (ARGS.output_root / "gui-progress.json").write_text(json.dumps(STATE["runs"], ensure_ascii=False, indent=2), encoding="utf-8")
            if len(STATE["runs"]) < ARGS.repeats:
                STATE["phase"] = "AFTER_BROWSER"
                return 0.3
            bpy.ops.screen.screenshot(filepath=str((ARGS.output_root / "import.png").resolve()))
            finish(True)
            return None
    except Exception:
        finish(False, traceback.format_exc())
        return None


bpy.app.timers.register(step, first_interval=0.3)
