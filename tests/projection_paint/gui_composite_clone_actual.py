"""실제 Blender Clone 입력으로 Composite 직접 편집과 Undo를 검증합니다."""

from __future__ import annotations

from array import array
import addon_utils
import hashlib
from importlib import import_module
import json
import os
from pathlib import Path
from types import SimpleNamespace
import traceback

import bpy
from bpy_extras import view3d_utils
from mathutils import Vector


ROOT = Path(__file__).resolve().parents[2]
MODULE_NAME = os.environ.get(
    "COLMAP_TOOLS_INSTALLED_MODULE",
    "bl_ext.user_default.colmap_tools",
)
RESULT_PATH = Path(
    os.environ.get(
        "COLMAP_TOOLS_CLONE_GUI_RESULT",
        str(ROOT / "build" / "composite_clone_gui.json"),
    )
)
STATE: dict[str, object] = {
    "actions": [],
    "checks": [],
    "direct_clone_offset_writes": 0,
}


def _write_result(success: bool, error: str = "") -> None:
    """GUI 회귀 결과를 UTF-8 JSON으로 기록합니다."""
    payload = {
        "success": bool(success),
        "error": str(error),
        "extension_version": str(STATE.get("extension_version", "")),
        "checks": list(STATE["checks"]),
        "source_window": list(STATE.get("source_window", ())),
        "destination_window": list(STATE.get("destination_window", ())),
        "clone_offset_before": list(STATE.get("clone_offset_before", ())),
        "clone_offset_after_source": list(STATE.get("clone_offset_after_source", ())),
        "destination_before": list(STATE.get("destination_before", ())),
        "destination_after": list(STATE.get("destination_after", ())),
        "before_hash": str(STATE.get("before_hash", "")),
        "after_hash": str(STATE.get("after_hash", "")),
        "undo_hash": str(STATE.get("undo_hash", "")),
        "direct_clone_offset_writes": int(STATE["direct_clone_offset_writes"]),
        "generated_test_brushes": list(STATE.get("generated_test_brushes", ())),
        "file_saved": False,
    }
    if not success:
        payload["traceback"] = traceback.format_exc()
    RESULT_PATH.parent.mkdir(parents=True, exist_ok=True)
    RESULT_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _finish(success: bool, error: str = "") -> None:
    """결과를 기록하고 테스트용 Blender Process를 종료합니다."""
    _write_result(success, error)
    bpy.ops.wm.quit_blender()


def _enable_installed_extension():
    """실제 설치된 COLMAP Tools Extension을 활성화해 반환합니다."""
    _loaded, enabled = addon_utils.check(MODULE_NAME)
    module = import_module(MODULE_NAME) if enabled else addon_utils.enable(
        MODULE_NAME,
        default_set=False,
        persistent=False,
    )
    if module is None:
        raise RuntimeError("설치된 COLMAP Tools Extension을 활성화하지 못했습니다.")
    return module


def _clear_scene() -> None:
    """결정적 Plane Fixture를 위해 현재 Scene Object를 모두 제거합니다."""
    if bpy.context.mode != "OBJECT":
        bpy.ops.object.mode_set(mode="OBJECT")
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)


def _create_uv_plane():
    """UV 0~1이 XY -1~1에 대응하는 단일 Quad Plane을 생성합니다."""
    mesh = bpy.data.meshes.new("Composite Clone GUI Plane Mesh")
    mesh.from_pydata(
        ((-1.0, -1.0, 0.0), (1.0, -1.0, 0.0), (1.0, 1.0, 0.0), (-1.0, 1.0, 0.0)),
        (),
        ((0, 1, 2, 3),),
    )
    mesh.update()
    uv_layer = mesh.uv_layers.new(name="UVMap")
    for loop_index, uv in enumerate(((0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0))):
        uv_layer.uv[loop_index].vector = uv
    target = bpy.data.objects.new("Composite Clone GUI Plane", mesh)
    bpy.context.scene.collection.objects.link(target)
    material = bpy.data.materials.new("Composite Clone GUI Source")
    material.use_nodes = True
    target.data.materials.append(material)
    bpy.context.view_layer.objects.active = target
    target.select_set(True)
    return target


def _fill_asymmetric_pattern(image) -> None:
    """왼쪽 빨강·오른쪽 파랑의 비대칭 Source Pixel을 기록합니다."""
    width, height = (int(value) for value in image.size)
    pixels = array("f")
    for _y in range(height):
        for x in range(width):
            pixels.extend((1.0, 0.0, 0.0, 1.0) if x < width // 2 else (0.0, 0.0, 1.0, 1.0))
    image.pixels.foreach_set(pixels)
    image.update()


def _view_parts() -> tuple[object, object, object, object]:
    """현재 Window의 3D View Area·Window Region·Region3D를 반환합니다."""
    window = bpy.context.window
    if window is None:
        raise RuntimeError("GUI 회귀에 사용할 Blender Window가 없습니다.")
    area = next((candidate for candidate in window.screen.areas if candidate.type == "VIEW_3D"), None)
    if area is None:
        raise RuntimeError("GUI 회귀에 사용할 3D Viewport가 없습니다.")
    region = next((candidate for candidate in area.regions if candidate.type == "WINDOW"), None)
    if region is None:
        raise RuntimeError("GUI 회귀에 사용할 3D View Window Region이 없습니다.")
    return window, area, region, area.spaces.active.region_3d


def _viewport_override() -> dict[str, object]:
    """Texture Paint Operator에 전달할 실제 3D Viewport Context를 반환합니다."""
    window, area, region, _rv3d = _view_parts()
    return {
        "window": window,
        "screen": window.screen,
        "area": area,
        "region": region,
    }


def _window_coordinate(world: Vector) -> tuple[int, int]:
    """월드 위치를 Window.event_simulate용 정수 좌표로 변환합니다."""
    _window, _area, region, rv3d = _view_parts()
    projected = view3d_utils.location_3d_to_region_2d(region, rv3d, world)
    if projected is None:
        raise RuntimeError("Plane 검증 위치를 3D Viewport에 투영하지 못했습니다.")
    return (
        int(round(float(region.x + projected.x))),
        int(round(float(region.y + projected.y))),
    )


def _image_hash(image) -> str:
    """현재 Image 전체 Float Pixel의 SHA-256을 반환합니다."""
    values = array("f", [0.0]) * (int(image.size[0]) * int(image.size[1]) * 4)
    image.pixels.foreach_get(values)
    return hashlib.sha256(values.tobytes()).hexdigest()


def _sample_pixel(image, uv: tuple[float, float]) -> tuple[float, float, float, float]:
    """정규화 UV의 단일 RGBA Pixel을 반환합니다."""
    width, height = (int(value) for value in image.size)
    x = min(width - 1, max(0, int(float(uv[0]) * width)))
    y = min(height - 1, max(0, int(float(uv[1]) * height)))
    offset = (y * width + x) * 4
    return tuple(float(value) for value in image.pixels[offset : offset + 4])


def _queue(action, delay: float = 0.12) -> None:
    """실제 Event Loop 사이에 실행할 동작과 대기 시간을 추가합니다."""
    actions = STATE["actions"]
    assert isinstance(actions, list)
    actions.append((float(delay), action))


def _emit(event_type: str, value: str, coordinate: tuple[int, int], *, ctrl: bool = False) -> None:
    """실제 Blender Window에 마우스 이벤트 한 개를 주입합니다."""
    window = bpy.context.window
    if window is None:
        raise RuntimeError("입력을 보낼 Blender Window가 없습니다.")
    window.event_simulate(
        type=event_type,
        value=value,
        x=int(coordinate[0]),
        y=int(coordinate[1]),
        ctrl=bool(ctrl),
    )


def _record_source_result() -> None:
    """Ctrl+LMB 뒤 Clone Offset과 Paint Pixel 불변을 기록합니다."""
    image = STATE["composite_image"]
    image_paint = bpy.context.scene.tool_settings.image_paint
    STATE["clone_offset_after_source"] = tuple(float(value) for value in image_paint.clone_offset)
    if _image_hash(image) != STATE["before_hash"]:
        raise AssertionError("Clone Source 위치 지정만으로 Composite Pixel이 변경됐습니다.")
    STATE["checks"].append("Ctrl+LMB Source 지정은 Pixel을 변경하지 않음")


def _validate_stroke() -> None:
    """Clone Stroke가 Source 빨강을 파랑 Destination으로 복사했는지 확인합니다."""
    image = STATE["composite_image"]
    image.update()
    after_hash = _image_hash(image)
    after = _sample_pixel(image, (0.75, 0.5))
    STATE["after_hash"] = after_hash
    STATE["destination_after"] = after
    if after_hash == STATE["before_hash"]:
        raise AssertionError("실제 Clone Stroke 뒤 Composite Pixel Hash가 바뀌지 않았습니다.")
    if after[0] <= after[2] or after[0] < 0.25:
        raise AssertionError(f"Clone Destination에 Source 빨강이 복사되지 않았습니다: {after}")
    if _sample_pixel(image, (0.25, 0.5))[0] < 0.75:
        raise AssertionError("Clone Source Pixel이 예상한 빨강 패턴이 아닙니다.")
    STATE["checks"].append("실제 Clone Stroke가 Source 패턴을 Destination에 기록")


def _run_undo() -> None:
    """실제 Blender Undo Operator로 마지막 Clone Stroke를 되돌립니다."""
    with bpy.context.temp_override(**_viewport_override()):
        result = bpy.ops.ed.undo()
    if result != {"FINISHED"}:
        raise RuntimeError(f"Clone Stroke Undo가 실패했습니다: {result}")


def _validate_undo() -> None:
    """Undo 뒤 Composite 전체 Pixel Hash가 Stroke 전과 같은지 확인합니다."""
    image = STATE["composite_image"]
    image.update()
    undo_hash = _image_hash(image)
    STATE["undo_hash"] = undo_hash
    if undo_hash != STATE["before_hash"]:
        raise AssertionError("Undo 뒤 Composite Pixel Hash가 Stroke 전 값으로 복구되지 않았습니다.")
    STATE["checks"].append("Blender Undo가 Composite Pixel Hash를 복원")
    _finish(True)


def _prepare_clone_actions() -> None:
    """뷰 정렬이 끝난 뒤 실제 Clone 소스 지정과 Stroke 입력을 예약합니다."""
    source_window = _window_coordinate(Vector((-0.5, 0.0, 0.0)))
    destination_window = _window_coordinate(Vector((0.5, 0.0, 0.0)))
    STATE["source_window"] = source_window
    STATE["destination_window"] = destination_window
    _queue(lambda: _emit("MOUSEMOVE", "NOTHING", source_window), 0.2)
    _queue(lambda: _emit("LEFT_CTRL", "PRESS", source_window, ctrl=True), 0.12)
    _queue(lambda: _emit("LEFTMOUSE", "PRESS", source_window, ctrl=True), 0.15)
    _queue(lambda: _emit("LEFTMOUSE", "RELEASE", source_window, ctrl=True), 0.4)
    _queue(lambda: _emit("LEFT_CTRL", "RELEASE", source_window), 0.2)
    _queue(_record_source_result, 0.15)
    _queue(lambda: _emit("MOUSEMOVE", "NOTHING", destination_window), 0.2)
    _queue(lambda: _emit("LEFTMOUSE", "PRESS", destination_window), 0.15)
    _queue(lambda: _emit("MOUSEMOVE", "NOTHING", (destination_window[0] + 2, destination_window[1])), 0.15)
    _queue(lambda: _emit("LEFTMOUSE", "RELEASE", (destination_window[0] + 2, destination_window[1])), 0.8)
    _queue(_validate_stroke, 0.2)
    _queue(_run_undo, 0.8)
    _queue(_validate_undo, 0.1)


def _prepare_fixture() -> None:
    """Composite 직접 Paint Target과 실제 Essentials Clone 입력 좌표를 준비합니다."""
    if not bool(getattr(bpy.app, "use_event_simulate", False)):
        raise RuntimeError("--enable-event-simulate 옵션이 필요합니다.")
    if not hasattr(bpy.context.window, "event_simulate"):
        raise RuntimeError("Window.event_simulate API를 사용할 수 없습니다.")
    extension = _enable_installed_extension()
    STATE["extension_version"] = str(getattr(extension, "__version__", ""))
    layout_workspace = bpy.data.workspaces.get("Layout")
    if layout_workspace is not None:
        bpy.context.window.workspace = layout_workspace
    _clear_scene()
    target = _create_uv_plane()
    package = MODULE_NAME
    material_setup = import_module(f"{package}.tools.projection_paint.blender.material_setup")
    paint_state = import_module(f"{package}.tools.projection_paint.blender.paint_state")
    layer_shader = import_module(f"{package}.tools.projection_paint.blender.layer_shader")
    composite_layer = import_module(f"{package}.tools.projection_paint.blender.composite_layer")
    setup = material_setup.prepare_active_material(
        target,
        128,
        128,
        use_base_resolution=False,
    )
    paint_state.commit_paint_setup(bpy.context.scene, setup)
    layer_shader.reconcile_layer_graph(
        bpy.context.scene,
        material=setup.material,
        overlay=setup.overlay,
    )
    setup.commit()
    settings = bpy.context.scene.projection_paint
    editable = next(layer for layer in settings.layers if not bool(layer.is_original_layer))
    editable_uuid = str(editable.layer_uuid)
    _fill_asymmetric_pattern(editable.image)
    for layer in settings.layers:
        layer.ui_selected = str(layer.layer_uuid) == editable_uuid
        layer.visible = True
    composite_layer.create_composite_layer(bpy.context)
    composite = next(layer for layer in settings.layers if str(layer.layer_role) == "COMPOSITE")
    bpy.context.view_layer.objects.active = target
    target.select_set(True)
    if target.mode != "TEXTURE_PAINT":
        bpy.ops.object.mode_set(mode="TEXTURE_PAINT")
    layer_shader.activate_layer_paint_image(
        SimpleNamespace(scene=bpy.context.scene),
        str(composite.layer_uuid),
    )
    with bpy.context.temp_override(**_viewport_override()):
        bpy.ops.view3d.view_axis(type="TOP", align_active=False)
        bpy.ops.view3d.view_selected(use_all_regions=False)
        bpy.ops.wm.tool_set_by_id(name="builtin_brush.clone", space_type="VIEW_3D")
        result = bpy.ops.brush.asset_activate(
            asset_library_type="ESSENTIALS",
            relative_asset_identifier="brushes/essentials_brushes-mesh_texture.blend/Brush/Clone",
        )
    if result != {"FINISHED"}:
        raise RuntimeError(f"Blender Essentials Clone Brush 활성화에 실패했습니다: {result}")
    brush = bpy.context.scene.tool_settings.image_paint.brush
    tool = bpy.context.workspace.tools.from_space_view3d_mode("PAINT_TEXTURE", create=False)
    if str(getattr(tool, "idname", "")) != "builtin_brush.clone":
        raise AssertionError("실제 Blender Clone Workspace Tool이 활성화되지 않았습니다.")
    if brush is None or str(brush.image_brush_type) != "CLONE":
        raise AssertionError("실제 Blender Clone Brush가 활성화되지 않았습니다.")
    reference = bpy.context.scene.tool_settings.image_paint.brush_asset_reference
    if str(getattr(reference, "asset_library_type", "")) != "ESSENTIALS":
        raise AssertionError("Blender Essentials Clone Brush 자산이 아닙니다.")
    image_paint = bpy.context.scene.tool_settings.image_paint
    if bool(image_paint.use_clone_layer):
        raise AssertionError("직접 Clone Fixture에서 use_clone_layer가 켜져 있습니다.")
    if settings.active_paint_image is not composite.image:
        raise AssertionError("Composite Image가 실제 Paint Target이 아닙니다.")
    paint_images = tuple(settings.paint_material.texture_paint_images)
    active_slot = int(settings.paint_material.paint_active_slot)
    if active_slot < 0 or active_slot >= len(paint_images) or paint_images[active_slot] is not composite.image:
        raise AssertionError("Material Paint Active Slot이 Composite Image와 다릅니다.")
    brush.size = 48
    brush.strength = 1.0
    STATE["composite_image"] = composite.image
    STATE["clone_offset_before"] = tuple(float(value) for value in image_paint.clone_offset)
    STATE["before_hash"] = _image_hash(composite.image)
    STATE["destination_before"] = _sample_pixel(composite.image, (0.75, 0.5))
    STATE["generated_test_brushes"] = tuple(
        brush.name for brush in bpy.data.brushes if brush.name.startswith("COLMAP Clone GUI")
    )
    if STATE["generated_test_brushes"]:
        raise AssertionError("검증용 가짜 Clone Brush가 생성됐습니다.")
    STATE["checks"].extend(
        (
            "Composite Image가 실제 Paint Target",
            "Blender Essentials Clone Tool과 Brush 활성",
            "use_clone_layer 비활성",
        )
    )
    _queue(_prepare_clone_actions, 0.2)


def _advance() -> float | None:
    """실제 GUI Event Loop에서 준비·입력·검증을 한 단계씩 실행합니다."""
    try:
        actions = STATE["actions"]
        assert isinstance(actions, list)
        if not actions:
            _prepare_fixture()
            return 0.8
        delay, action = actions.pop(0)
        action()
        return float(delay)
    except Exception as exc:
        _finish(False, str(exc))
        return None


bpy.app.timers.register(_advance, first_interval=0.5)
