"""독립 Blender 화면에서 실제 Ctrl·스트로크 입력과 원래 스텐실 복귀를 검증합니다."""

from __future__ import annotations

import json
from pathlib import Path
import sys
import time
import traceback

import bpy
from bpy_extras import view3d_utils
from mathutils import Vector

bpy.context.preferences.view.show_splash = False


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools import projection_paint as addon
from tools.projection_paint.blender import stencil_erase, stencil_session
from tools.projection_paint.blender.ownership import tag_owned, PROJECT_STENCIL_DRAW_BRUSH_ROLE
from tools.projection_paint.blender.paint_tool_bridge import active_tool_id

RESULT = ROOT / "build/verification/stencil-erase-20261006/gui.json"
STATE = {"step": 0, "start": time.monotonic(), "checks": []}


def viewport() -> dict:
    """검증 창에서 사용하는 3D 뷰의 실제 문맥을 반환합니다."""
    window = bpy.context.window
    area = next(a for a in window.screen.areas if a.type == "VIEW_3D")
    region = next(r for r in area.regions if r.type == "WINDOW")
    return dict(window=window, area=area, region=region)


def setup() -> None:
    """별도 검증 프로세스에 실제 이미지·UV 평면·카메라·연결 브러시를 만듭니다."""
    RESULT.parent.mkdir(parents=True, exist_ok=True)
    bpy.context.preferences.filepaths.temporary_directory = str(RESULT.parent)
    addon.register()
    bpy.context.window_manager.keyconfigs.update()
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)
    bpy.ops.mesh.primitive_plane_add(size=4)
    target = bpy.context.object
    image = bpy.data.images.new("지우개 검증 레이어", width=256, height=256, alpha=True)
    image.generated_color = (0.8, 0.2, 0.1, 1.0)
    material = bpy.data.materials.new("지우개 검증 재질")
    material.use_nodes = True
    node = material.node_tree.nodes.new("ShaderNodeTexImage")
    node.image = image
    material.node_tree.nodes.active = node
    target.data.materials.append(material)
    camera = bpy.data.objects.new("지우개 검증 카메라", bpy.data.cameras.new("검증 카메라"))
    bpy.context.scene.collection.objects.link(camera)
    camera.location = (0, 0, 5)
    camera.data.type = "ORTHO"
    camera.data.ortho_scale = 5
    scene = bpy.context.scene
    scene.camera = camera
    scene.render.resolution_x = 512
    scene.render.resolution_y = 512
    scene.tool_settings.image_paint.mode = "IMAGE"
    scene.tool_settings.image_paint.canvas = image
    settings = scene.projection_paint
    settings.paint_target = target
    settings.paint_workspace = bpy.context.workspace
    settings.paint_enabled = True
    settings.paint_state = "ACTIVE"
    settings.active_paint_image = image
    settings.overlay_image = image
    source = bpy.data.images.new("검증 스텐실 원본", width=16, height=16)
    source.generated_color = (1, 1, 1, 1)
    texture = bpy.data.textures.new("검증 스텐실 텍스처", type="IMAGE")
    texture.image = source
    tag_owned(texture, stencil_session.TEXTURE_ROLE)
    brush = bpy.data.brushes.new("검증 원래 스텐실", mode="TEXTURE_PAINT")
    brush.asset_mark()
    tag_owned(brush, PROJECT_STENCIL_DRAW_BRUSH_ROLE)
    brush.color = (0.21, 0.36, 0.54)
    brush.strength = 0.63
    brush.size = 113
    brush.texture = texture
    brush.texture_slot.map_mode = "STENCIL"
    settings.stencil_brush = brush
    settings.stencil_texture = texture
    settings.stencil_source_image = source
    settings.active_image_id = 1
    override = viewport()
    with bpy.context.temp_override(**override):
        bpy.ops.object.mode_set(mode="TEXTURE_PAINT")
        bpy.ops.wm.tool_set_by_id(name="builtin.brush")
        result = bpy.ops.brush.asset_activate(
            asset_library_type="LOCAL", relative_asset_identifier=f"Brush/{brush.name}",
        )
        assert result == {"FINISHED"}
    override["area"].spaces.active.region_3d.view_perspective = "CAMERA"
    override["area"].spaces.active.shading.type = "SOLID"
    with bpy.context.temp_override(**override):
        # 숨겨진 검증 창도 영역의 기본 입력 처리기가 초기화되도록 한 번 그립니다.
        bpy.ops.wm.redraw_timer(type="DRAW_WIN_SWAP", iterations=1)
    STATE.update(brush=brush, image=image, target=target, texture=texture,
                 original=brush_snapshot(brush))


def brush_snapshot(brush) -> tuple:
    """원래 브러시의 색·강도·배율과 스텐실 배치를 비교 가능한 값으로 읽습니다."""
    return (
        tuple(brush.color), float(brush.strength), int(brush.size), brush.blend,
        brush.curve_distance_falloff_preset, brush.texture_slot.map_mode,
        tuple(brush.stencil_pos), tuple(brush.stencil_dimension),
        float(brush.texture_slot.angle),
    )


def event(kind: str, value: str, **modifiers) -> None:
    """실제 창 이벤트 큐에 뷰 중앙의 키·마우스 입력을 전송합니다."""
    override = viewport()
    region = override["region"]
    point = view3d_utils.location_3d_to_region_2d(
        region, override["area"].spaces.active.region_3d, Vector((0, 0, 0)),
    )
    assert point is not None
    if kind in {"LEFT_CTRL", "RIGHT_CTRL"}:
        override["window"].event_simulate(
            type="MOUSEMOVE", value="NOTHING", x=round(region.x + point.x),
            y=round(region.y + point.y), **modifiers,
        )
    override["window"].event_simulate(
        type=kind, value=value, x=round(region.x + point.x),
        y=round(region.y + point.y), **modifiers,
    )


def assert_restored(label: str) -> None:
    """같은 도구·브러시·텍스처 포인터와 실제 연결 상태를 확인합니다."""
    with bpy.context.temp_override(**viewport()):
        assert bpy.context.scene.tool_settings.image_paint.brush is STATE["brush"]
        assert active_tool_id(bpy.context) == "builtin.brush"
        assert STATE["brush"].texture is STATE["texture"]
        assert brush_snapshot(STATE["brush"]) == STATE["original"]
        assert stencil_session.resolve_stencil_connection(bpy.context.scene).state == "CONNECTED"
        assert stencil_erase.active_stencil_erase(bpy.context.scene) is None
    STATE["checks"].append(label)


def assert_erase() -> None:
    """실제 Erase Soft 자산이 텍스처 없이 알파 지우기를 수행하는지 확인합니다."""
    brush = bpy.context.scene.tool_settings.image_paint.brush
    assert brush.name == "Erase Soft", brush.name
    assert brush.blend == "ERASE_ALPHA"
    assert brush.texture is None
    assert stencil_erase.active_stencil_erase(bpy.context.scene) is not None


def restart_paint() -> None:
    """작업공간 이탈로 끝난 세션 뒤 검증용 대상의 페인트 문맥을 다시 준비합니다."""
    with bpy.context.temp_override(**viewport()):
        target = STATE["target"]
        bpy.context.view_layer.objects.active = target
        target.select_set(True)
        settings = bpy.context.scene.projection_paint
        settings.paint_target = target
        settings.paint_enabled = True
        settings.paint_state = "ACTIVE"
        bpy.ops.object.mode_set(mode="TEXTURE_PAINT")
        bpy.ops.wm.tool_set_by_id(name="builtin.brush")


def finish(success: bool, error: str = "") -> None:
    """결과를 기록하고 사용자 프로젝트를 건드리지 않은 검증 프로세스만 종료합니다."""
    RESULT.parent.mkdir(parents=True, exist_ok=True)
    RESULT.write_text(json.dumps(dict(
        success=success, error=error, step=STATE["step"],
        checks=STATE["checks"], alpha=STATE.get("alpha"),
    ), ensure_ascii=False, indent=2), encoding="utf-8")
    bpy.ops.wm.quit_blender()


def advance() -> float | None:
    """화면 갱신 사이에 실제 입력 전환과 지우기 결과를 순서대로 검증합니다."""
    try:
        assert time.monotonic() - STATE["start"] < 60, "검증 시간 초과"
        step = STATE["step"]
        if step == 0:
            setup()
        elif step == 1:
            assert_restored("초기 스텐실 연결")
            event("MOUSEMOVE", "NOTHING")
        elif step == 2:
            with bpy.context.temp_override(**viewport()):
                assert stencil_erase.stencil_erase_allowed(bpy.context), (
                    bpy.context.mode, bpy.context.scene.projection_paint.paint_enabled,
                    bpy.context.scene.projection_paint.paint_state,
                    stencil_session.resolve_stencil_connection(bpy.context.scene).state,
                    active_tool_id(bpy.context),
                )
            event("LEFT_CTRL", "PRESS", ctrl=True)
        elif step == 3:
            assert_erase()
            event("LEFTMOUSE", "PRESS", ctrl=True)
        elif step == 4:
            assert_erase()
            event("MOUSEMOVE", "NOTHING", ctrl=True)
        elif step == 5:
            event("LEFTMOUSE", "RELEASE", ctrl=True)
        elif step == 6:
            assert_erase()
            image = STATE["image"]
            image.update()
            STATE["alpha"] = min(image.pixels[3::4])
            assert STATE["alpha"] < 0.5, STATE["alpha"]
            STATE["checks"].append("Ctrl 클릭에서 알파 감소")
            event("LEFT_CTRL", "RELEASE", ctrl=False)
        elif step == 7:
            assert_restored("왼쪽 Ctrl 해제 복구")
            event("RIGHT_CTRL", "PRESS", ctrl=True)
        elif step == 8:
            assert_erase()
            event("LEFT_CTRL", "PRESS", ctrl=True)
        elif step == 9:
            # Blender는 양쪽 Ctrl 중 하나를 놓아도 보조키 플래그를 끌 수 있습니다.
            event("RIGHT_CTRL", "RELEASE", ctrl=False)
        elif step == 10:
            assert_erase()
            event("LEFT_CTRL", "RELEASE", ctrl=False)
        elif step == 11:
            assert_restored("양쪽 Ctrl 중 마지막 해제 복구")
            event("LEFT_CTRL", "PRESS", ctrl=True)
        elif step == 12:
            assert_erase()
            event("LEFTMOUSE", "PRESS", ctrl=True)
        elif step == 13:
            event("LEFT_CTRL", "RELEASE", ctrl=False)
        elif step == 14:
            assert_restored("스트로크 도중 Ctrl 해제 즉시 복구")
            event("LEFTMOUSE", "RELEASE", ctrl=False)
        elif step == 15:
            bpy.context.scene.projection_paint.manual_stencil_disabled = True
            event("LEFT_CTRL", "PRESS", ctrl=True)
        elif step == 16:
            assert bpy.context.scene.tool_settings.image_paint.brush is STATE["brush"]
            assert stencil_erase.active_stencil_erase(bpy.context.scene) is None
            STATE["checks"].append("수동 연결 해제에서 Ctrl 무시")
            event("LEFT_CTRL", "RELEASE", ctrl=False)
            bpy.context.scene.projection_paint.manual_stencil_disabled = False
        elif step == 17:
            event("LEFT_CTRL", "PRESS", ctrl=True)
        elif step == 18:
            assert_erase()
            stencil_erase.restore_stencil_erase(bpy.context.scene)
            assert_restored("페인트 종료 복구")
            event("LEFT_CTRL", "RELEASE", ctrl=False)
        elif step == 19:
            bpy.context.scene.projection_paint.paint_mask_mode = True
            event("LEFT_CTRL", "PRESS", ctrl=True)
        elif step == 20:
            assert stencil_erase.active_stencil_erase(bpy.context.scene) is None
            assert bpy.context.scene.tool_settings.image_paint.brush is STATE["brush"]
            STATE["checks"].append("마스크 편집에서 Ctrl 무시")
            event("LEFT_CTRL", "RELEASE", ctrl=False)
            bpy.context.scene.projection_paint.paint_mask_mode = False
        elif step == 21:
            with bpy.context.temp_override(**viewport()):
                bpy.ops.wm.tool_set_by_id(name="builtin_brush.clone")
                bpy.ops.brush.asset_activate(
                    asset_library_type="ESSENTIALS",
                    relative_asset_identifier="brushes/essentials_brushes-mesh_texture.blend/Brush/Clone",
                )
            STATE["clone"] = bpy.context.scene.tool_settings.image_paint.brush
            event("LEFT_CTRL", "PRESS", ctrl=True)
        elif step == 22:
            assert stencil_erase.active_stencil_erase(bpy.context.scene) is None
            assert bpy.context.scene.tool_settings.image_paint.brush is STATE["clone"]
            STATE["checks"].append("Clone의 Ctrl 입력 유지")
            event("LEFT_CTRL", "RELEASE", ctrl=False)
            with bpy.context.temp_override(**viewport()):
                bpy.ops.wm.tool_set_by_id(name="builtin.brush")
                bpy.ops.brush.asset_activate(
                    asset_library_type="LOCAL",
                    relative_asset_identifier=f"Brush/{STATE['brush'].name}",
                )
        elif step == 23:
            event("LEFT_CTRL", "PRESS", ctrl=True)
        elif step == 24:
            assert_erase()
            STATE["workspace"] = bpy.context.window.workspace
            STATE["other"] = next(w for w in bpy.data.workspaces if w is not STATE["workspace"])
            bpy.context.window.workspace = STATE["other"]
        elif step == 25:
            assert bpy.context.window.workspace is STATE["other"]
            stencil_erase.reconcile_stencil_erase(bpy.context.scene)
            assert bpy.context.window.workspace is STATE["other"]
            assert bpy.context.scene.tool_settings.image_paint.brush is STATE["brush"]
            bpy.context.window.workspace = STATE["workspace"]
        elif step == 26:
            assert_restored("작업공간 이탈 복구와 현재 화면 보존")
            event("LEFT_CTRL", "RELEASE", ctrl=False)
            restart_paint()
        elif step == 27:
            with bpy.context.temp_override(**viewport()):
                assert stencil_erase.stencil_erase_allowed(bpy.context), (
                    bpy.context.mode, active_tool_id(bpy.context),
                    bpy.context.scene.projection_paint.paint_enabled,
                    bpy.context.area.spaces.active.region_3d.view_perspective,
                    stencil_session.resolve_stencil_connection(bpy.context.scene).state,
                    bpy.context.view_layer.objects.active,
                )
            event("LEFT_CTRL", "PRESS", ctrl=True)
        elif step == 28:
            assert_erase()
            with bpy.context.temp_override(**viewport()):
                bpy.ops.object.mode_set(mode="OBJECT")
                stencil_erase.reconcile_stencil_erase(bpy.context.scene)
            assert bpy.context.scene.tool_settings.image_paint.brush is STATE["brush"]
            assert STATE["target"].mode == "OBJECT"
            STATE["checks"].append("외부 모드 변경 복구")
            restart_paint()
            event("LEFT_CTRL", "RELEASE", ctrl=False)
        elif step == 29:
            event("LEFT_CTRL", "PRESS", ctrl=True)
        elif step == 30:
            assert_erase()
            stencil_erase.clear_stencil_erase("save_pre")
            assert_restored("저장 직전 복구")
            event("LEFT_CTRL", "RELEASE", ctrl=False)
        elif step == 31:
            event("LEFT_CTRL", "PRESS", ctrl=True)
        elif step == 32:
            assert_erase()
            from tools.projection_paint.blender.lifecycle import lifecycle_registry

            lifecycle_registry().barrier("undo_pre")
            assert_restored("실행 취소 장벽 복구")
            lifecycle_registry().rebuild(bpy.context.scene, "undo_post")
            event("LEFT_CTRL", "RELEASE", ctrl=False)
        elif step == 33:
            event("LEFT_CTRL", "PRESS", ctrl=True)
        elif step == 34:
            assert_erase()
            event("WINDOW_DEACTIVATE", "NOTHING", ctrl=True)
        elif step == 35:
            assert_restored("창 비활성화 복구")
            event("LEFT_CTRL", "RELEASE", ctrl=False)
        elif step == 36:
            event("LEFT_CTRL", "PRESS", ctrl=True)
        else:
            assert_erase()
            addon.unregister()
            assert bpy.context.scene.tool_settings.image_paint.brush is STATE["brush"]
            assert stencil_erase.active_stencil_erase(bpy.context.scene) is None
            STATE["checks"].append("Ctrl 유지 중 확장 해제 복구")
            assert not hasattr(bpy.types, "PROJECTION_PAINT_OT_hold_stencil_erase")
            finish(True)
            return None
        STATE["step"] += 1
        return 0.25
    except Exception:
        finish(False, traceback.format_exc())
        return None


assert not bpy.app.background, "실제 화면 검증이 필요합니다."
bpy.app.timers.register(advance, first_interval=0.5)
