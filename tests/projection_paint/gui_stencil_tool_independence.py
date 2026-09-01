"""실데이터 GUI에서 Camera Stencil과 Blender Paint 도구의 독립성을 검증합니다."""

from __future__ import annotations

import importlib.util
from importlib import import_module
import json
import os
from pathlib import Path
import sys
import time
import traceback

import bpy
from bpy_extras import view3d_utils
from bpy_extras.object_utils import world_to_camera_view
from mathutils import Vector


ROOT = Path(__file__).resolve().parents[2]
USE_INSTALLED = os.environ.get("COLMAP_TOOLS_USE_INSTALLED") == "1"
PACKAGE_NAME = os.environ.get(
    "COLMAP_TOOLS_INSTALLED_MODULE",
    "colmap_tools_stencil_tool_gui_test",
)
RESULT_PATH = Path(
    os.environ.get(
        "COLMAP_TOOLS_STENCIL_GUI_RESULT",
        str(ROOT / "build" / "stencil_tool_independence_gui.json"),
    )
)
TIMEOUT_SECONDS = 240.0
STATE = {
    "phase": "REGISTER",
    "started": time.monotonic(),
    "phase_started": time.monotonic(),
    "extension": None,
    "projection_workspace": None,
    "workspace_roundtrip": False,
    "paint_reentry_requested": False,
    "draw_before": None,
    "draw_after_reconnect": None,
    "bound": None,
    "connected_image_id": 0,
    "frame_debounce": None,
    "industry_navigation": None,
    "clone_snapshot": None,
    "initial_stencil_position": None,
    "initial_stencil_dimensions": None,
    "viewport_tracking": None,
    "camera_exit": None,
    "camera_return": None,
    "stroke": None,
    "tool_check_index": 0,
    "tool_checks": [],
}


TOOL_CHECKS = (
    ("builtin_brush.soften", "SOFTEN", "Blur"),
    ("builtin_brush.smear", "SMEAR", "Smear"),
    ("builtin_brush.clone", "CLONE", "Clone"),
    ("builtin_brush.fill", "FILL", "Fill"),
    ("builtin_brush.mask", "MASK", "Mask"),
)


def _write_result(payload: dict) -> None:
    """GUI 검증 결과를 UTF-8 JSON으로 기록합니다."""

    RESULT_PATH.parent.mkdir(parents=True, exist_ok=True)
    RESULT_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _load_extension_package():
    """현재 Source Tree 또는 실제 설치본의 통합 Extension Package를 불러옵니다."""

    if USE_INSTALLED:
        import addon_utils

        _loaded, enabled = addon_utils.check(PACKAGE_NAME)
        module = import_module(PACKAGE_NAME) if enabled else addon_utils.enable(
            PACKAGE_NAME,
            default_set=False,
            persistent=False,
        )
        if module is None:
            raise RuntimeError("설치된 COLMAP Tools Extension을 활성화하지 못했습니다.")
        return module

    spec = importlib.util.spec_from_file_location(
        PACKAGE_NAME,
        ROOT / "__init__.py",
        submodule_search_locations=[str(ROOT)],
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("통합 Extension Root를 불러올 수 없습니다.")
    module = importlib.util.module_from_spec(spec)
    sys.modules[PACKAGE_NAME] = module
    spec.loader.exec_module(module)
    return module


def _projection_workspace():
    """저장 파일의 Projection Paint 전용 Workspace를 반환합니다."""

    return next(
        (
            workspace
            for workspace in bpy.data.workspaces
            if workspace.get("colmap_tools_workspace_tool_id") == "projection_paint"
        ),
        None,
    )


def _viewport_override() -> dict:
    """현재 Window의 3D Viewport Operator Override를 반환합니다."""

    window = bpy.context.window
    if window is None:
        raise RuntimeError("검증에 사용할 Blender Window가 없습니다.")
    for area in window.screen.areas:
        if area.type != "VIEW_3D":
            continue
        region = next(
            (candidate for candidate in area.regions if candidate.type == "WINDOW"),
            None,
        )
        if region is not None:
            return {
                "window": window,
                "screen": window.screen,
                "area": area,
                "region": region,
            }
    raise RuntimeError("검증에 사용할 3D Viewport를 찾지 못했습니다.")


def _viewport_parts():
    """현재 Window의 3D Viewport와 Camera View 상태를 반환합니다."""

    override = _viewport_override()
    area = override["area"]
    return override, area.spaces.active, area.spaces.active.region_3d


def _ray_hit(region, rv3d, coordinate):
    """Viewport 좌표에서 현재 Scene으로 Ray를 발사합니다."""

    origin = view3d_utils.region_2d_to_origin_3d(region, rv3d, coordinate)
    direction = view3d_utils.region_2d_to_vector_3d(region, rv3d, coordinate)
    return bpy.context.scene.ray_cast(
        bpy.context.evaluated_depsgraph_get(),
        origin,
        direction,
    )


def _hit_uv(target, face_index: int, world_location: Vector) -> Vector:
    """삼각형 Ray Hit 위치의 활성 UV를 Barycentric 보간합니다."""

    polygon = target.data.polygons[face_index]
    if len(polygon.vertices) != 3:
        raise RuntimeError("실제 Stroke 검증은 삼각형 Polygon이 필요합니다.")
    local = target.matrix_world.inverted() @ world_location
    vertices = tuple(target.data.vertices[index].co for index in polygon.vertices)
    edge0 = vertices[1] - vertices[0]
    edge1 = vertices[2] - vertices[0]
    point = local - vertices[0]
    d00 = edge0.dot(edge0)
    d01 = edge0.dot(edge1)
    d11 = edge1.dot(edge1)
    d20 = point.dot(edge0)
    d21 = point.dot(edge1)
    denominator = d00 * d11 - d01 * d01
    if abs(denominator) <= 1.0e-20:
        raise RuntimeError("퇴화 삼각형에서는 UV를 계산할 수 없습니다.")
    weight1 = (d11 * d20 - d01 * d21) / denominator
    weight2 = (d00 * d21 - d01 * d20) / denominator
    weight0 = 1.0 - weight1 - weight2
    uv_data = target.data.uv_layers.active.data
    uvs = tuple(uv_data[index].uv for index in polygon.loop_indices)
    return uvs[0] * weight0 + uvs[1] * weight1 + uvs[2] * weight2


def _paint_point(target):
    """현재 Camera에서 보이는 활성 Material 삼각형의 Stroke 좌표와 UV를 찾습니다."""

    scene = bpy.context.scene
    camera = scene.camera
    if camera is None:
        raise RuntimeError("현재 Frame Camera가 없습니다.")
    override, _space, rv3d = _viewport_parts()
    region = override["region"]
    rv3d.view_perspective = "CAMERA"
    rv3d.update()
    center = Vector((region.width * 0.5, region.height * 0.5))
    polygons = target.data.polygons
    step = max(1, len(polygons) // 3000)
    candidates = []
    for polygon_index in range(0, len(polygons), step):
        polygon = polygons[polygon_index]
        if polygon.material_index != target.active_material_index:
            continue
        world = target.matrix_world @ polygon.center
        camera_point = world_to_camera_view(scene, camera, world)
        if (
            camera_point.z <= 0.0
            or not 0.02 < camera_point.x < 0.98
            or not 0.02 < camera_point.y < 0.98
        ):
            continue
        projected = view3d_utils.location_3d_to_region_2d(region, rv3d, world)
        if projected is not None:
            candidates.append(((projected - center).length_squared, projected))
    for _distance, projected in sorted(candidates, key=lambda item: item[0]):
        hit, location, _normal, face_index, hit_object, _matrix = _ray_hit(
            region,
            rv3d,
            projected,
        )
        if (
            hit
            and hit_object is target
            and face_index >= 0
            and target.data.polygons[face_index].material_index
            == target.active_material_index
        ):
            return projected, _hit_uv(target, face_index, location)
    raise RuntimeError("Camera에서 보이는 활성 Material 면을 찾지 못했습니다.")


def _pixel_window(image, uv: Vector, radius: int = 8) -> tuple[float, ...]:
    """UV 주변에서 Alpha가 가장 큰 RGBA Pixel을 반환합니다."""

    width, height = (int(value) for value in image.size)
    center_x = min(width - 1, max(0, int((uv.x % 1.0) * width)))
    center_y = min(height - 1, max(0, int((uv.y % 1.0) * height)))
    maximum = (-1.0, (0.0, 0.0, 0.0, 0.0))
    for y in range(max(0, center_y - radius), min(height, center_y + radius + 1)):
        minimum_x = max(0, center_x - radius)
        maximum_x = min(width, center_x + radius + 1)
        start = (y * width + minimum_x) * 4
        end = (y * width + maximum_x) * 4
        values = tuple(float(value) for value in image.pixels[start:end])
        for offset in range(0, len(values), 4):
            rgba = values[offset : offset + 4]
            if rgba[3] > maximum[0]:
                maximum = (rgba[3], rgba)
    return maximum[1]


def _stroke_camera_texture() -> dict:
    """현재 Camera Texture가 검게 변하지 않고 활성 Layer에 기록되는지 검증합니다."""

    scene = bpy.context.scene
    settings = scene.projection_paint
    target = settings.paint_target
    image = settings.active_paint_image
    brush = scene.tool_settings.image_paint.brush
    if target is None or image is None or brush is None:
        raise RuntimeError("Stroke 검증에 필요한 Target·Layer·Brush가 없습니다.")
    point, uv = _paint_point(target)
    before = _pixel_window(image, uv)
    brush.strength = 1.0
    brush.size = 96
    offsets = (-12.0, -6.0, 0.0, 6.0, 12.0)
    stroke = [
        {
            "name": "",
            "mouse": (float(point.x + offset), float(point.y)),
            "mouse_event": (float(point.x + offset), float(point.y)),
            "pressure": 1.0,
            "size": 96.0,
            "time": index * 0.05,
            "is_start": index == 0,
            "location": (0.0, 0.0, 0.0),
            "x_tilt": 0.0,
            "y_tilt": 0.0,
        }
        for index, offset in enumerate(offsets)
    ]
    override = _viewport_override()
    with bpy.context.temp_override(
        **override,
        active_object=target,
        object=target,
    ):
        result = bpy.ops.paint.image_paint(stroke=stroke)
    image.update()
    after = _pixel_window(image, uv)
    if result != {"FINISHED"}:
        raise RuntimeError(f"Texture Paint Stroke가 실패했습니다: {result}")
    if after == before:
        raise RuntimeError("Camera Texture Stroke 뒤 활성 Layer Pixel이 바뀌지 않았습니다.")
    if max(after[:3]) <= 0.01:
        raise RuntimeError("Camera Texture 대신 검은색이 활성 Layer에 기록되었습니다.")
    return {
        "point": [round(float(point.x), 3), round(float(point.y), 3)],
        "uv": [round(float(uv.x), 6), round(float(uv.y), 6)],
        "before": [round(float(value), 6) for value in before],
        "after": [round(float(value), 6) for value in after],
        "brush_color": [round(float(value), 6) for value in brush.color],
    }


def _paint_tool_snapshot() -> dict:
    """현재 Texture Paint Workspace Tool과 활성 Brush 식별값을 반환합니다."""

    scene = bpy.context.scene
    tool = bpy.context.workspace.tools.from_space_view3d_mode(
        "PAINT_TEXTURE",
        create=False,
    )
    brush = scene.tool_settings.image_paint.brush
    reference = scene.tool_settings.image_paint.brush_asset_reference
    return {
        "tool_id": str(getattr(tool, "idname", "") or ""),
        "tool_fallback": str(getattr(tool, "idname_fallback", "") or ""),
        "tool_brush_type": str(getattr(tool, "brush_type", "") or ""),
        "brush_pointer": int(brush.as_pointer()) if brush is not None else 0,
        "brush_name": str(getattr(brush, "name", "") or ""),
        "image_brush_type": str(getattr(brush, "image_brush_type", "") or ""),
        "asset_library_type": str(
            getattr(reference, "asset_library_type", "") or ""
        ),
        "relative_asset_identifier": str(
            getattr(reference, "relative_asset_identifier", "") or ""
        ),
    }


def _set_tool(tool_id: str) -> None:
    """현재 3D Viewport의 Texture Paint 도구를 지정 ID로 바꿉니다."""

    with bpy.context.temp_override(**_viewport_override()):
        result = bpy.ops.wm.tool_set_by_id(name=tool_id, space_type="VIEW_3D")
    if result != {"FINISHED"}:
        raise RuntimeError(f"Blender Paint 도구 전환에 실패했습니다: {tool_id} · {result}")


def _activate_essentials_draw_brush() -> None:
    """최초 Reconnect 입력으로 사용할 Blender Essentials Paint Hard를 활성화합니다."""

    with bpy.context.temp_override(**_viewport_override()):
        result = bpy.ops.brush.asset_activate(
            asset_library_type="ESSENTIALS",
            relative_asset_identifier=(
                "brushes/essentials_brushes-mesh_texture.blend/Brush/Paint Hard"
            ),
        )
    if result != {"FINISHED"}:
        raise RuntimeError(f"Blender Essentials Draw Brush 활성화에 실패했습니다: {result}")


def _run_reconnect() -> set[str]:
    """N 패널과 같은 3D Viewport 문맥에서 명시적 Reconnect를 실행합니다."""

    with bpy.context.temp_override(**_viewport_override()):
        return bpy.ops.projection_paint.reconnect_stencil()


def _advance_phase(phase: str) -> float:
    """검증 단계와 단계별 시작 시간을 갱신합니다."""

    STATE["phase"] = phase
    STATE["phase_started"] = time.monotonic()
    return 0.25


def _waited(seconds: float) -> bool:
    """현재 단계에서 지정한 실제 시간이 지났는지 반환합니다."""

    return time.monotonic() - float(STATE["phase_started"]) >= seconds


def _tick() -> float | None:
    """실제 Blender GUI Event Loop에서 도구 독립성 시나리오를 순차 실행합니다."""

    try:
        if time.monotonic() - float(STATE["started"]) > TIMEOUT_SECONDS:
            raise TimeoutError(f"GUI 검증 시간이 초과되었습니다: {STATE['phase']}")

        phase = str(STATE["phase"])
        if phase == "REGISTER":
            extension = _load_extension_package()
            if not USE_INSTALLED:
                extension.register()
            STATE["extension"] = extension
            workspace = _projection_workspace()
            if workspace is None:
                raise RuntimeError("저장 파일의 Projection Paint Workspace를 찾지 못했습니다.")
            STATE["projection_workspace"] = workspace
            bpy.context.window.workspace = workspace
            return _advance_phase("ENTER_PAINT")

        settings = bpy.context.scene.projection_paint
        if phase == "ENTER_PAINT":
            if not _waited(0.5):
                return 0.1
            if str(settings.paint_state) != "ACTIVE":
                result = bpy.ops.projection_paint.toggle_paint()
                if result != {"FINISHED"}:
                    raise RuntimeError(f"Painting Mode 진입 요청이 실패했습니다: {result}")
            return _advance_phase("WAIT_PAINT")

        if phase == "WAIT_PAINT":
            if str(settings.paint_state) != "ACTIVE":
                return 0.1
            if bpy.context.mode != "PAINT_TEXTURE":
                return 0.1
            _set_tool("builtin.brush")
            _activate_essentials_draw_brush()
            return _advance_phase("DRAW_READY")

        if phase == "DRAW_READY":
            if not _waited(0.75):
                return 0.1
            draw = _paint_tool_snapshot()
            if draw["tool_id"] != "builtin.brush":
                raise AssertionError(f"실제 Draw Workspace Tool이 준비되지 않았습니다: {draw}")
            if draw["image_brush_type"] != "DRAW":
                raise AssertionError(f"Draw Tool의 활성 Brush가 Draw가 아닙니다: {draw}")
            STATE["draw_before"] = draw
            result = _run_reconnect()
            if result != {"FINISHED"}:
                raise AssertionError(f"명시적 Draw Reconnect가 실패했습니다: {result}")
            return _advance_phase("CONNECTED")

        if phase == "CONNECTED":
            if not _waited(0.75):
                return 0.1
            stencil_session = import_module(
                f"{PACKAGE_NAME}.tools.projection_paint.blender.stencil_session"
            )

            connection = stencil_session.resolve_stencil_connection(
                bpy.context.scene,
                bpy.context,
            )
            if connection.state != "CONNECTED":
                raise AssertionError(
                    f"명시적 Reconnect 뒤 Stencil이 연결되지 않았습니다: {connection.state}"
                )
            STATE["bound"] = settings.stencil_brush
            connected = _paint_tool_snapshot()
            ownership = import_module(
                f"{PACKAGE_NAME}.tools.projection_paint.blender.ownership"
            )
            bound = STATE["bound"]
            if bound is None or connected["brush_pointer"] == STATE["draw_before"]["brush_pointer"]:
                raise AssertionError("최초 Reconnect가 프로젝트 전용 Draw Brush를 생성하지 않았습니다.")
            if not ownership.is_owned(
                bound,
                kind=ownership.PROJECT_STENCIL_DRAW_BRUSH_ROLE,
            ):
                raise AssertionError("연결 Brush가 프로젝트 전용 Draw Brush 소유권이 아닙니다.")
            if connected["asset_library_type"] != "LOCAL" or not bool(bound.is_editable):
                raise AssertionError(f"프로젝트 전용 Draw Brush가 편집 가능한 Local Asset이 아닙니다: {connected}")
            if any(abs(float(value) - 1.0) > 1.0e-6 for value in bound.color):
                raise AssertionError(f"Camera Texture용 Draw Brush 색상이 흰색이 아닙니다: {tuple(bound.color)}")
            STATE["draw_after_reconnect"] = connected
            STATE["connected_image_id"] = int(settings.active_image_id)
            camera_scene = import_module(
                f"{PACKAGE_NAME}.tools.projection_paint.blender.camera_scene"
            )
            camera_runtime = camera_scene.runtime_for_scene(bpy.context.scene)
            if camera_runtime is None:
                raise AssertionError("Frame Debounce를 검증할 Camera Runtime이 없습니다.")
            original_frame = int(bpy.context.scene.frame_current)
            original_image_id = int(settings.active_image_id)
            candidates = [
                entry
                for entry in sorted(
                    camera_runtime.entries_by_frame.values(),
                    key=lambda item: int(item.frame),
                )
                if int(entry.frame) != original_frame
                and int(entry.image_id) != original_image_id
            ]
            if not candidates:
                raise AssertionError("Frame Debounce를 검증할 다른 Camera Frame이 없습니다.")
            target_entry = candidates[min(19, len(candidates) - 1)]
            sequence = candidates[:20]
            while len(sequence) < 20:
                sequence.extend(candidates[: 20 - len(sequence)])
            sequence[-1] = target_entry
            STATE["frame_debounce"] = {
                "original_frame": original_frame,
                "original_image_id": original_image_id,
                "target_frame": int(target_entry.frame),
                "target_image_id": int(target_entry.image_id),
                "paint_image_pointer": int(settings.active_paint_image.as_pointer()),
                "brush_pointer": int(bound.as_pointer()),
                "tool_id": connected["tool_id"],
                "active_layer_index": int(settings.active_layer_index),
            }
            for entry in sequence:
                bpy.context.scene.frame_set(int(entry.frame))
            return _advance_phase("FRAME_DEBOUNCE_EARLY")

        if phase == "FRAME_DEBOUNCE_EARLY":
            if not _waited(0.15):
                return 0.02
            debounce = STATE["frame_debounce"]
            camera_scene = import_module(
                f"{PACKAGE_NAME}.tools.projection_paint.blender.camera_scene"
            )
            camera_runtime = camera_scene.runtime_for_scene(bpy.context.scene)
            if int(settings.active_image_id) != int(debounce["original_image_id"]):
                raise AssertionError("0.30초 대기 전에 중간 Source Image가 연결되었습니다.")
            if int(camera_runtime.pending_frame) != int(debounce["target_frame"]):
                raise AssertionError("빠른 이동의 최종 Frame이 Pending에 유지되지 않았습니다.")
            if int(camera_runtime.repair_attempts) != 0 or str(camera_runtime.error):
                raise AssertionError("정상 Frame 이동이 Camera Guard 복구 오류로 계산되었습니다.")
            return _advance_phase("FRAME_DEBOUNCE_FINAL")

        if phase == "FRAME_DEBOUNCE_FINAL":
            if not _waited(0.25):
                return 0.02
            debounce = STATE["frame_debounce"]
            camera_scene = import_module(
                f"{PACKAGE_NAME}.tools.projection_paint.blender.camera_scene"
            )
            camera_runtime = camera_scene.runtime_for_scene(bpy.context.scene)
            target_entry = camera_runtime.entries_by_frame[int(debounce["target_frame"])]
            target_camera = camera_runtime.objects[target_entry.camera_uuid]
            target_marker = camera_runtime.markers[target_entry.image_id]
            if camera_runtime.pending_frame is not None:
                raise AssertionError("최종 Source Commit 뒤 Pending Frame이 남았습니다.")
            if int(settings.active_image_id) != int(debounce["target_image_id"]):
                raise AssertionError("최종 Frame의 Source Image가 연결되지 않았습니다.")
            if bpy.context.scene.camera is not target_camera:
                raise AssertionError("최종 Frame Camera Pointer가 Manifest와 다릅니다.")
            if int(target_marker.frame) != int(debounce["target_frame"]):
                raise AssertionError("최종 Frame Camera Marker가 Manifest와 다릅니다.")
            if settings.stencil_texture.image is not settings.stencil_source_image:
                raise AssertionError("최종 Stencil Texture와 Source Image Pointer가 다릅니다.")
            current_tool = _paint_tool_snapshot()
            if current_tool["tool_id"] != debounce["tool_id"]:
                raise AssertionError("Frame Debounce가 Blender Paint Tool을 변경했습니다.")
            if int(current_tool["brush_pointer"]) != int(debounce["brush_pointer"]):
                raise AssertionError("Frame Debounce가 활성 Brush를 변경했습니다.")
            if int(settings.active_paint_image.as_pointer()) != int(
                debounce["paint_image_pointer"]
            ):
                raise AssertionError("Frame Debounce가 Paint Target Image를 변경했습니다.")
            if int(settings.active_layer_index) != int(debounce["active_layer_index"]):
                raise AssertionError("Frame Debounce가 활성 Layer를 변경했습니다.")
            debounce["final_status"] = str(settings.status)
            debounce["repair_attempts"] = int(camera_runtime.repair_attempts)
            override, _space, rv3d = _viewport_parts()
            region = override["region"]
            coordinate = (
                int(region.x + region.width // 2),
                int(region.y + region.height // 2),
            )
            STATE["industry_navigation"] = {
                "coordinate": coordinate,
                "before_camera_zoom": float(rv3d.view_camera_zoom),
                "tool_id": current_tool["tool_id"],
                "brush_pointer": int(current_tool["brush_pointer"]),
            }
            bpy.context.window.event_simulate(
                type="RIGHTMOUSE",
                value="PRESS",
                x=coordinate[0],
                y=coordinate[1],
                alt=True,
            )
            return _advance_phase("INDUSTRY_ZOOM_DRAG")

        if phase == "INDUSTRY_ZOOM_DRAG":
            if not _waited(0.12):
                return 0.02
            coordinate = STATE["industry_navigation"]["coordinate"]
            bpy.context.window.event_simulate(
                type="MOUSEMOVE",
                value="NOTHING",
                x=int(coordinate[0] + 120),
                y=int(coordinate[1] + 90),
                alt=True,
            )
            return _advance_phase("INDUSTRY_ZOOM_RELEASE")

        if phase == "INDUSTRY_ZOOM_RELEASE":
            if not _waited(0.12):
                return 0.02
            coordinate = STATE["industry_navigation"]["coordinate"]
            bpy.context.window.event_simulate(
                type="RIGHTMOUSE",
                value="RELEASE",
                x=int(coordinate[0] + 120),
                y=int(coordinate[1] + 90),
                alt=True,
            )
            return _advance_phase("INDUSTRY_ZOOM_VERIFY")

        if phase == "INDUSTRY_ZOOM_VERIFY":
            if not _waited(0.25):
                return 0.02
            navigation = STATE["industry_navigation"]
            _override, _space, rv3d = _viewport_parts()
            after_zoom = float(rv3d.view_camera_zoom)
            if abs(after_zoom - float(navigation["before_camera_zoom"])) <= 1.0e-3:
                raise AssertionError("Projection Paint의 Alt+우클릭 확대·축소가 작동하지 않았습니다.")
            current_tool = _paint_tool_snapshot()
            if current_tool["tool_id"] != navigation["tool_id"]:
                raise AssertionError("Alt+우클릭 탐색이 Blender Paint Tool을 변경했습니다.")
            if int(current_tool["brush_pointer"]) != int(navigation["brush_pointer"]):
                raise AssertionError("Alt+우클릭 탐색이 활성 Brush를 변경했습니다.")
            navigation["after_camera_zoom"] = after_zoom
            bound = STATE["bound"]
            _override, _space, rv3d = _viewport_parts()
            STATE["initial_stencil_position"] = tuple(float(value) for value in bound.stencil_pos)
            STATE["initial_stencil_dimensions"] = tuple(float(value) for value in bound.stencil_dimension)
            offset = tuple(float(value) for value in rv3d.view_camera_offset)
            rv3d.view_camera_offset = (offset[0] + 0.08, offset[1] - 0.06)
            rv3d.view_camera_zoom = min(600.0, float(rv3d.view_camera_zoom) + 24.0)
            return _advance_phase("VIEW_TRACK")

        if phase == "VIEW_TRACK":
            if not _waited(0.75):
                return 0.1
            bound = STATE["bound"]
            position = tuple(float(value) for value in bound.stencil_pos)
            dimensions = tuple(float(value) for value in bound.stencil_dimension)
            position_changed = max(
                abs(actual - before)
                for actual, before in zip(position, STATE["initial_stencil_position"], strict=True)
            ) > 1.0
            dimensions_changed = max(
                abs(actual - before)
                for actual, before in zip(dimensions, STATE["initial_stencil_dimensions"], strict=True)
            ) > 1.0
            if not position_changed or not dimensions_changed:
                raise AssertionError(
                    "Camera 이동·확대 뒤 Stencil 위치 또는 크기가 갱신되지 않았습니다: "
                    f"position={position}, dimensions={dimensions}"
                )
            STATE["viewport_tracking"] = {
                "position": list(position),
                "dimensions": list(dimensions),
            }
            _override, _space, rv3d = _viewport_parts()
            rv3d.view_perspective = "PERSP"
            return _advance_phase("VIEW_EXIT")

        if phase == "VIEW_EXIT":
            if not _waited(0.75):
                return 0.1
            bound = STATE["bound"]
            if bound.texture is not None:
                raise AssertionError("Camera View 이탈 뒤 Stencil Texture가 해제되지 않았습니다.")
            if not bool(settings.stencil_outside_camera_view):
                raise AssertionError("Camera View 이탈 상태가 기록되지 않았습니다.")
            if str(settings.paint_state) != "ACTIVE":
                raise AssertionError("Camera View 이탈이 Layer Paint 상태를 종료했습니다.")
            STATE["camera_exit"] = True
            _override, _space, rv3d = _viewport_parts()
            rv3d.view_perspective = "CAMERA"
            return _advance_phase("VIEW_RETURN")

        if phase == "VIEW_RETURN":
            if not _waited(0.75):
                return 0.1
            stencil_session = import_module(
                f"{PACKAGE_NAME}.tools.projection_paint.blender.stencil_session"
            )
            connection = stencil_session.resolve_stencil_connection(
                bpy.context.scene,
                bpy.context,
            )
            if connection.state != "CONNECTED":
                raise AssertionError(f"Camera View 복귀 뒤 Stencil 연결이 복구되지 않았습니다: {connection.state}")
            if scene_brush := bpy.context.scene.tool_settings.image_paint.brush:
                if scene_brush is not STATE["bound"]:
                    raise AssertionError("Camera View 복귀가 다른 Brush를 활성화했습니다.")
            STATE["camera_return"] = True
            STATE["stroke"] = _stroke_camera_texture()
            other_workspace = next(
                (
                    workspace
                    for workspace in bpy.data.workspaces
                    if workspace is not STATE["projection_workspace"]
                    and workspace.name == "Layout"
                ),
                None,
            )
            if other_workspace is None:
                raise AssertionError("Tool Timer 재등록을 검증할 Layout Workspace가 없습니다.")
            bpy.context.window.workspace = other_workspace
            return _advance_phase("WORKSPACE_AWAY")

        if phase == "WORKSPACE_AWAY":
            if not _waited(0.75):
                return 0.1
            bpy.context.window.workspace = STATE["projection_workspace"]
            return _advance_phase("WORKSPACE_RETURN")

        if phase == "WORKSPACE_RETURN":
            if (
                str(settings.paint_state) == "IDLE"
                and not bool(STATE["paint_reentry_requested"])
            ):
                result = bpy.ops.projection_paint.toggle_paint()
                if result != {"FINISHED"}:
                    raise RuntimeError(
                        f"작업공간 복귀 뒤 Painting Mode 재진입 요청이 실패했습니다: {result}"
                    )
                STATE["paint_reentry_requested"] = True
                return 0.1
            if str(settings.paint_state) == "ERROR":
                raise RuntimeError(
                    f"작업공간 복귀 뒤 Painting Mode가 오류 상태입니다: {settings.status}"
                )
            if str(settings.paint_state) != "ACTIVE" or bpy.context.mode != "PAINT_TEXTURE":
                return 0.1
            if not _waited(1.0):
                return 0.1
            STATE["workspace_roundtrip"] = True
            return _advance_phase("TOOL_SWITCH")

        if phase == "TOOL_SWITCH":
            index = int(STATE["tool_check_index"])
            if index >= len(TOOL_CHECKS):
                _set_tool("builtin.brush")
                return _advance_phase("DRAW_RETURNED")
            tool_id, _brush_type, _asset_name = TOOL_CHECKS[index]
            if tool_id == "builtin_brush.clone":
                bpy.context.scene.tool_settings.image_paint.use_clone_layer = True
            _set_tool(tool_id)
            return _advance_phase("TOOL_READY")

        if phase == "TOOL_READY":
            if not _waited(1.0):
                return 0.1
            index = int(STATE["tool_check_index"])
            tool_id, brush_type, asset_name = TOOL_CHECKS[index]
            snapshot = _paint_tool_snapshot()
            if snapshot["tool_id"] != tool_id or snapshot["tool_brush_type"] != brush_type:
                raise AssertionError(f"Workspace Tool이 유지되지 않았습니다: {snapshot}")
            if snapshot["image_brush_type"] != brush_type:
                raise AssertionError(
                    f"{tool_id}가 실제 {brush_type} Brush를 사용하지 않습니다: {snapshot}"
                )
            if snapshot["asset_library_type"] != "ESSENTIALS":
                raise AssertionError(f"Blender Essentials Brush가 아닙니다: {snapshot}")
            if not snapshot["relative_asset_identifier"].endswith(
                f"/Brush/{asset_name}"
            ):
                raise AssertionError(f"예상한 Blender Brush 자산이 아닙니다: {snapshot}")
            bound = STATE["bound"]
            if bound is not None and snapshot["brush_pointer"] == int(bound.as_pointer()):
                raise AssertionError("비Draw 도구 전환 뒤에도 Stencil Draw Brush가 활성 상태입니다.")
            stencil_session = import_module(
                f"{PACKAGE_NAME}.tools.projection_paint.blender.stencil_session"
            )
            connection = stencil_session.resolve_stencil_connection(
                bpy.context.scene,
                bpy.context,
            )
            if connection.state != "BRUSH_CHANGED":
                raise AssertionError(
                    f"비Draw 도구 전환 뒤 연결한 Draw Brush가 보존되지 않았습니다: {connection.state}"
                )
            if brush_type == "CLONE":
                if bool(bpy.context.scene.tool_settings.image_paint.use_clone_layer):
                    raise AssertionError(
                        "Workspace 복귀 뒤 Clone Tool Edge가 같은 활성 Layer 모드를 복구하지 못했습니다."
                    )
                STATE["clone_snapshot"] = snapshot
            STATE["tool_checks"].append(snapshot)
            STATE["tool_check_index"] = index + 1
            return _advance_phase("TOOL_SWITCH")

        if phase == "DRAW_RETURNED":
            if not _waited(1.25):
                return 0.1
            returned = _paint_tool_snapshot()
            if returned["tool_id"] != "builtin.brush":
                raise AssertionError(f"Draw 복귀가 유지되지 않았습니다: {returned}")
            if returned["image_brush_type"] != "DRAW":
                raise AssertionError(f"Draw 복귀 뒤 실제 Draw Brush가 아닙니다: {returned}")
            stencil_session = import_module(
                f"{PACKAGE_NAME}.tools.projection_paint.blender.stencil_session"
            )
            connection = stencil_session.resolve_stencil_connection(
                bpy.context.scene,
                bpy.context,
            )
            if connection.state != "CONNECTED":
                raise AssertionError(
                    "Draw 복귀 뒤 기존 Brush의 Stencil 연결이 유지되지 않았습니다: "
                    f"{connection.state}"
                )
            return _advance_phase("FINAL")

        if phase == "FINAL":
            if not _waited(0.75):
                return 0.1
            stencil_session = import_module(
                f"{PACKAGE_NAME}.tools.projection_paint.blender.stencil_session"
            )

            final_connection = stencil_session.resolve_stencil_connection(
                bpy.context.scene,
                bpy.context,
            )
            final_tool = _paint_tool_snapshot()
            if final_connection.state != "CONNECTED":
                raise AssertionError(
                    f"최종 수동 Reconnect 상태가 아닙니다: {final_connection.state}"
                )
            generated_test_brushes = [
                brush.name
                for brush in bpy.data.brushes
                if brush.name.startswith("COLMAP GUI")
            ]
            if generated_test_brushes:
                raise AssertionError(
                    f"검증 전용 가짜 Brush가 생성되었습니다: {generated_test_brushes}"
                )
            _write_result(
                {
                    "success": True,
                    "extension_version": str(getattr(STATE["extension"], "__version__", "")),
                    "draw_before": STATE["draw_before"],
                    "draw_after_reconnect": STATE["draw_after_reconnect"],
                    "clone_after_switch": STATE["clone_snapshot"],
                    "all_tool_checks": STATE["tool_checks"],
                    "draw_after_return": final_tool,
                    "connected_image_id": int(STATE["connected_image_id"]),
                    "frame_debounce": STATE["frame_debounce"],
                    "industry_navigation": STATE["industry_navigation"],
                    "final_stencil_state": final_connection.state,
                    "viewport_tracking": STATE["viewport_tracking"],
                    "camera_exit": STATE["camera_exit"],
                    "camera_return": STATE["camera_return"],
                    "stroke": STATE["stroke"],
                    "workspace_roundtrip": bool(STATE["workspace_roundtrip"]),
                    "clone_same_layer_mode": not bool(
                        bpy.context.scene.tool_settings.image_paint.use_clone_layer
                    ),
                    "file_saved": False,
                    "generated_test_brushes": generated_test_brushes,
                }
            )
            bpy.ops.wm.quit_blender()
            return None
        raise RuntimeError(f"알 수 없는 GUI 검증 단계입니다: {phase}")
    except Exception as exc:
        _write_result(
            {
                "success": False,
                "phase": str(STATE["phase"]),
                "error": str(exc),
                "traceback": traceback.format_exc(),
                "file_saved": False,
            }
        )
        bpy.ops.wm.quit_blender()
        return None


bpy.app.timers.register(_tick, first_interval=0.5)
