"""독립 Blender 창에서 실제 페인트 실행 취소·다시 실행 입력의 시점 보존을 검증합니다."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import runpy
import sys
import tempfile
import time
import traceback

import bpy
from bpy_extras import view3d_utils
from mathutils import Matrix, Vector


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
FIXTURE = runpy.run_path(str(ROOT / "tests/projection_paint/blender_background/test_camera_review_background.py"))
RESULT = ROOT / "build/verification/painter-undo-viewport-20261006/gui.json"
STATE = {"phase": "setup", "index": 0, "started": time.monotonic(), "checks": []}


def viewport() -> dict:
    """독립 검증 창의 3D 뷰포트 문맥을 반환합니다."""
    window = bpy.context.window
    area = next(item for item in window.screen.areas if item.type == "VIEW_3D")
    region = next(item for item in area.regions if item.type == "WINDOW")
    return dict(window=window, screen=window.screen, area=area, region=region)


def navigation() -> tuple:
    """현재 투영·회전·위치·거리와 카메라 확대·이동 상태를 읽습니다."""
    rv3d = viewport()["area"].spaces.active.region_3d
    return (rv3d.view_perspective, tuple(rv3d.view_rotation), tuple(rv3d.view_location),
            rv3d.view_distance, rv3d.view_camera_zoom, tuple(rv3d.view_camera_offset))


def pixels() -> str:
    """현재 관리 페인트 이미지의 픽셀을 실행 취소 검증용 지문으로 읽습니다."""
    import array
    image = bpy.data.images[STATE["image_name"]]
    values = array.array("f", [0.0]) * len(image.pixels)
    image.pixels.foreach_get(values)
    return hashlib.sha256(values.tobytes()).hexdigest()


def setup() -> None:
    """소형 촬영 이미지·카메라·페인트 대상을 만들어 실제 스트로크를 기록합니다."""
    from tools.projection_paint.blender.camera_import import CameraImportJob
    from tools.projection_paint.blender.camera_scene import activate_managed_frame
    from tools.projection_paint.blender.paint_session import start_paint_session

    FIXTURE["addon"].register()
    directory = tempfile.TemporaryDirectory()
    STATE["directory"] = directory
    for name, color in (("a.png", (1, 0, 0, 1)), ("b.png", (0, 1, 0, 1))):
        FIXTURE["_save_test_image"](Path(directory.name) / name, color)
    scene = bpy.context.scene
    FIXTURE["_activate_test_camera_session"](scene)
    CameraImportJob(scene, FIXTURE["_request"](Path(directory.name))).run_to_completion()
    target = FIXTURE["_new_paint_target"]()
    uv = target.data.uv_layers.active.data
    for loop, coord in zip(uv, ((0, 0), (1, 0), (1, 1)), strict=True):
        loop.uv = coord
    override = viewport()
    with bpy.context.temp_override(**override):
        activate_managed_frame(scene, int(scene.frame_current), bpy.context, refresh_source=False)
        target.matrix_world = scene.camera.matrix_world @ Matrix.Translation((0, 0, -3))
        start_paint_session(bpy.context, target, width=64, height=32)
        assert bpy.ops.object.mode_set(mode="TEXTURE_PAINT") == {"FINISHED"}
        scene.projection_paint.paint_enabled = True
        scene.projection_paint.paint_state = "ACTIVE"
        bpy.ops.wm.tool_set_by_id(name="builtin.brush")
        brush = bpy.data.brushes.new("시점 보존 검증 브러시", mode="TEXTURE_PAINT")
        brush.asset_mark()
        brush.color = (1, 1, 1)
        brush.strength = 1
        brush.size = 35
        assert bpy.ops.brush.asset_activate(
            asset_library_type="LOCAL", relative_asset_identifier=f"Brush/{brush.name}",
        ) == {"FINISHED"}
        STATE["image_name"] = scene.projection_paint.active_paint_image.name
        STATE["before_pixels"] = pixels()
        override["area"].spaces.active.region_3d.view_perspective = "CAMERA"
        bpy.ops.wm.redraw_timer(type="DRAW_WIN_SWAP", iterations=1)
        override["area"].spaces.active.region_3d.update()
        assert bpy.ops.ed.undo_push(message="페인트 검증 시작") == {"FINISHED"}
        point = view3d_utils.location_3d_to_region_2d(
            override["region"], override["area"].spaces.active.region_3d,
            target.matrix_world @ Vector((0.7, 0.3, 0)),
        )
        assert point is not None
        stroke = [dict(name="", mouse=(point.x + offset, point.y),
                       mouse_event=(point.x + offset, point.y), pressure=1.0, size=35.0,
                       time=index * 0.05, is_start=index == 0, location=(0, 0, 0),
                       x_tilt=0.0, y_tilt=0.0)
                  for index, offset in enumerate((-4, 0, 4))]
        with bpy.context.temp_override(**viewport()):
            assert bpy.ops.paint.image_paint(stroke=stroke, mode="NORMAL") == {"FINISHED"}
        STATE["painted_pixels"] = pixels()
        assert STATE["painted_pixels"] != STATE["before_pixels"], "검증 스트로크가 픽셀을 바꾸지 않았습니다."


def send_history_key(redo: bool) -> None:
    """검증 창의 이벤트 큐에 실제 실행 취소·다시 실행 단축키를 보냅니다."""
    override = viewport()
    region = override["region"]
    rv3d = override["area"].spaces.active.region_3d
    rv3d.view_perspective = ("PERSP", "ORTHO", "CAMERA")[STATE["index"]]
    rv3d.view_rotation = (0.9238795, 0, 0.3826834, 0)
    rv3d.view_location = (1.25, -2.5, 3.75)
    rv3d.view_distance = 17
    rv3d.view_camera_zoom = 23
    rv3d.view_camera_offset = (0.12, -0.08)
    with bpy.context.temp_override(**override):
        bpy.ops.wm.redraw_timer(type="DRAW_WIN_SWAP", iterations=1)
    STATE["before_navigation"] = navigation()
    point = dict(x=region.x + region.width // 2, y=region.y + region.height // 2)
    window = override["window"]
    window.event_simulate(type="MOUSEMOVE", value="NOTHING", **point)
    window.event_simulate(type="Z", value="PRESS", ctrl=True, shift=redo, **point)
    window.event_simulate(type="Z", value="RELEASE", ctrl=True, shift=redo, **point)


def finish(passed: bool, error: str = "") -> None:
    """검증 결과를 기록하고 사용자 프로젝트와 분리된 프로세스만 종료합니다."""
    RESULT.parent.mkdir(parents=True, exist_ok=True)
    RESULT.write_text(json.dumps(dict(passed=passed, error=error, checks=STATE["checks"]),
                                 ensure_ascii=False, indent=2), encoding="utf-8")
    STATE["directory"].cleanup()
    bpy.ops.wm.quit_blender()


def advance() -> float | None:
    """실제 단축키와 수명주기 타이머 완료 후 픽셀·탐색 상태를 순서대로 확인합니다."""
    try:
        assert time.monotonic() - STATE["started"] < 45, "시점 보존 검증 시간 초과"
        phase = STATE["phase"]
        if phase == "setup":
            setup()
            STATE["phase"] = "send_undo"
        elif phase in {"send_undo", "send_redo"}:
            send_history_key(phase == "send_redo")
            STATE["phase"] = "verify_redo" if phase == "send_redo" else "verify_undo"
        else:
            redo = phase == "verify_redo"
            expected = STATE["painted_pixels"] if redo else STATE["before_pixels"]
            assert pixels() == expected, "실제 단축키가 페인트 픽셀을 되돌리지 못했습니다."
            assert navigation() == STATE["before_navigation"], (navigation(), STATE["before_navigation"])
            from tools.projection_paint.blender.lifecycle import lifecycle_registry
            assert not lifecycle_registry().failures, lifecycle_registry().failures
            STATE["checks"].append(dict(operation="다시 실행" if redo else "실행 취소",
                                        perspective=navigation()[0], pixels_restored=True,
                                        navigation_preserved=True))
            if redo:
                STATE["index"] += 1
                if STATE["index"] == 3:
                    finish(True)
                    return None
                STATE["phase"] = "send_undo"
            else:
                STATE["phase"] = "send_redo"
        return 0.5
    except Exception:
        finish(False, traceback.format_exc())
        return None


assert not bpy.app.background, "실제 화면 검증이 필요합니다."
bpy.context.preferences.view.show_splash = False
bpy.context.preferences.view.smooth_view = 0
bpy.app.timers.register(advance, first_interval=0.5)
