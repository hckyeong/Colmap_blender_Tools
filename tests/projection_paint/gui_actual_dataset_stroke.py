"""실제 Flying squirrel 데이터로 Import부터 Texture Paint Stroke까지 검증합니다."""

from __future__ import annotations

import json
import math
from pathlib import Path
from time import perf_counter
import traceback

import bpy
from bpy_extras import view3d_utils
from bpy_extras.object_utils import world_to_camera_view
from mathutils import Vector


DATA_ROOT = Path(
    r"E:\DEV\DEV_RND\GSSplat\00_photogrammetry"
    r"\6_Flying_squirrel\rc\6_Flying_squirrel_workspace"
)
COLMAP_ROOT = DATA_ROOT / "final"
OBJ_PATH = DATA_ROOT / "obj_LOD0_LOD0.obj"
OUTPUT_ROOT = Path(r"C:\tmp\projection_paint_actual")
RESULT_PATH = OUTPUT_ROOT / "result.json"
BLEND_PATH = OUTPUT_ROOT / "flying_squirrel_stroke.blend"
OVERLAY_PATH = OUTPUT_ROOT / "paint_overlay.png"
SCREENSHOT_PATH = OUTPUT_ROOT / "viewport_stroke.png"

STATE = {
    "phase": 0,
    "started": perf_counter(),
    "result": {},
    "entry_reconnect_calls": 0,
    "entry_waits": 0,
}


def _write_result(success: bool, error: str = "") -> None:
    """현재 실제 데이터 테스트 결과를 UTF-8 JSON으로 기록합니다."""

    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    payload = dict(STATE["result"])
    payload["success"] = bool(success)
    payload["error"] = str(error)
    payload["elapsed_seconds"] = round(perf_counter() - STATE["started"], 3)
    RESULT_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _quit() -> None:
    """현재 테스트용 Blender Process를 종료합니다."""

    bpy.ops.wm.quit_blender()


def _fail(exc: Exception) -> None:
    """예외와 Traceback을 결과에 남기고 테스트 Process를 종료합니다."""

    STATE["result"]["traceback"] = traceback.format_exc()
    _write_result(False, str(exc))
    _quit()


def _clear_scene() -> None:
    """새 테스트 Scene의 기존 Object와 Marker를 제거합니다."""

    if bpy.context.mode != "OBJECT":
        bpy.ops.object.mode_set(mode="OBJECT")
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)
    for marker in tuple(bpy.context.scene.timeline_markers):
        bpy.context.scene.timeline_markers.remove(marker)


def _managed_stencil_brush_names() -> tuple[str, ...]:
    """애드온 소유 또는 이전 버전 Projection Brush 이름을 반환합니다."""

    return tuple(
        brush.name
        for brush in bpy.data.brushes
        if brush.name.startswith("Projection Stencil Brush")
        or brush.get("pp_record_kind") == "STENCIL_BRUSH"
    )


def _view_parts():
    """현재 Window의 첫 3D View Area와 Window Region을 반환합니다."""

    window = bpy.context.window
    if window is None:
        raise RuntimeError("테스트에 사용할 Blender Window가 없습니다.")
    area = next(
        (candidate for candidate in window.screen.areas if candidate.type == "VIEW_3D"),
        None,
    )
    if area is None:
        raise RuntimeError("Texture Paint용 3D View Area가 없습니다.")
    region = next(
        (candidate for candidate in area.regions if candidate.type == "WINDOW"),
        None,
    )
    if region is None:
        raise RuntimeError("Texture Paint용 Window Region이 없습니다.")
    return window, area, region, area.spaces.active, area.spaces.active.region_3d


def _ray_hit(scene, region, rv3d, coordinate):
    """Viewport 좌표에서 Scene으로 Ray를 발사해 첫 Hit를 반환합니다."""

    origin = view3d_utils.region_2d_to_origin_3d(region, rv3d, coordinate)
    direction = view3d_utils.region_2d_to_vector_3d(region, rv3d, coordinate)
    return scene.ray_cast(
        bpy.context.evaluated_depsgraph_get(),
        origin,
        direction,
    )


def _paint_point(target):
    """활성 Material의 보이는 삼각형에서 Stroke와 UV 검증 좌표를 찾습니다."""

    scene = bpy.context.scene
    camera = scene.camera
    if camera is None:
        raise RuntimeError("현재 Frame Camera가 없습니다.")
    _window, _area, region, _space, rv3d = _view_parts()
    rv3d.view_perspective = "CAMERA"
    rv3d.update()
    polygons = target.data.polygons
    step = max(1, len(polygons) // 3000)
    center = Vector((region.width * 0.5, region.height * 0.5))
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
        if projected is None:
            continue
        candidates.append(((projected - center).length_squared, projected))
    for _distance, projected in sorted(candidates, key=lambda item: item[0]):
        hit, location, _normal, face_index, hit_object, _matrix = _ray_hit(
            scene,
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
            uv = _hit_uv(target, face_index, location)
            return projected, uv
    raise RuntimeError("Camera에서 보이는 활성 Material 면을 찾지 못했습니다.")


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


def _pixel_window(image, uv: Vector, radius: int = 8) -> tuple[float, tuple[float, ...]]:
    """UV 주변 Pixel에서 가장 큰 Alpha와 해당 RGBA를 반환합니다."""

    width, height = (int(value) for value in image.size)
    center_x = min(width - 1, max(0, int((uv.x % 1.0) * width)))
    center_y = min(height - 1, max(0, int((uv.y % 1.0) * height)))
    maximum = (-1.0, (0.0, 0.0, 0.0, 0.0))
    minimum_x = max(0, center_x - radius)
    maximum_x = min(width, center_x + radius + 1)
    for y in range(max(0, center_y - radius), min(height, center_y + radius + 1)):
        start = (y * width + minimum_x) * 4
        end = (y * width + maximum_x) * 4
        values = tuple(float(value) for value in image.pixels[start:end])
        for offset in range(0, len(values), 4):
            rgba = values[offset : offset + 4]
            if rgba[3] > maximum[0]:
                maximum = (rgba[3], rgba)
    return maximum


def _sample_stencil_image(image, position, dimensions, point) -> dict:
    """Viewport Stroke 좌표에 대응하는 Stencil Image 표본을 반환합니다."""

    width, height = (int(value) for value in image.size)
    u = (float(point.x) - float(position[0])) / (2.0 * float(dimensions[0])) + 0.5
    v = (float(point.y) - float(position[1])) / (2.0 * float(dimensions[1])) + 0.5

    def sample(sample_u: float, sample_v: float) -> list[float]:
        """정규화 좌표의 단일 Image Pixel을 읽습니다."""

        x = min(width - 1, max(0, int(sample_u * width)))
        y = min(height - 1, max(0, int(sample_v * height)))
        start = (y * width + x) * 4
        return [round(float(value), 6) for value in image.pixels[start : start + 4]]

    return {
        "normalized": [round(u, 6), round(v, 6)],
        "direct": sample(u, v),
        "flip_y": sample(u, 1.0 - v),
        "flip_x": sample(1.0 - u, v),
        "flip_xy": sample(1.0 - u, 1.0 - v),
    }


def _stroke(target, overlay) -> dict:
    """Camera View의 보이는 면에 실제 Texture Paint Stroke를 입력합니다."""

    point, uv = _paint_point(target)
    before_alpha, before_rgba = _pixel_window(overlay, uv)
    window, area, region, _space, _rv3d = _view_parts()
    brush = bpy.context.scene.tool_settings.image_paint.brush
    if brush is None:
        raise RuntimeError("Texture Paint Brush가 없습니다.")
    unified = bpy.context.scene.tool_settings.image_paint.unified_paint_settings
    if hasattr(unified, "use_unified_strength"):
        unified.use_unified_strength = False
    if hasattr(unified, "use_unified_size"):
        unified.use_unified_size = False
    if hasattr(brush, "strength"):
        brush.strength = 1.0
    if hasattr(brush, "size"):
        brush.size = 96
    from bl_ext.user_default.colmap_tools.tools.projection_paint.blender.paint_runtime import (
        active_paint_runtime,
    )

    runtime = active_paint_runtime()
    if runtime is None or runtime.stencil.source_image is None:
        raise RuntimeError("Stroke Source Image가 없습니다.")
    source_samples = _sample_stencil_image(
        runtime.stencil.source_image,
        brush.stencil_pos,
        brush.stencil_dimension,
        point,
    )
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
    with bpy.context.temp_override(
        window=window,
        screen=window.screen,
        area=area,
        region=region,
        active_object=target,
        object=target,
    ):
        operator_result = bpy.ops.paint.image_paint(stroke=stroke)
    overlay.update()
    after_alpha, after_rgba = _pixel_window(overlay, uv)
    if operator_result != {"FINISHED"}:
        raise RuntimeError(f"Texture Paint Stroke Operator가 실패했습니다: {operator_result}")
    if after_rgba == before_rgba:
        raise RuntimeError("Stroke 뒤 검증 UV 주변 Overlay Pixel이 변경되지 않았습니다.")
    if max(after_rgba[:3]) <= 0.01:
        raise RuntimeError("Stencil Source RGB 대신 검은색이 Overlay에 기록되었습니다.")
    return {
        "mouse": [round(float(point.x), 3), round(float(point.y), 3)],
        "uv": [round(float(uv.x), 6), round(float(uv.y), 6)],
        "before_alpha": round(before_alpha, 6),
        "after_alpha": round(after_alpha, 6),
        "before_rgba": [round(value, 6) for value in before_rgba],
        "after_rgba": [round(value, 6) for value in after_rgba],
        "operator_result": sorted(operator_result),
        "brush_color": (
            [round(float(value), 6) for value in brush.color]
            if hasattr(brush, "color")
            else []
        ),
        "source_samples": source_samples,
    }


def _phase_import() -> float:
    """실제 COLMAP Camera와 OBJ를 Import하고 Paint Session에 진입합니다."""

    _clear_scene()
    scene = bpy.context.scene
    settings = scene.projection_paint
    settings.dataset_root = str(COLMAP_ROOT)
    settings.input_format = "TEXT"
    settings.frame_start = 1
    settings.camera_axis_preset = "REALITY_CAPTURE"
    camera_started = perf_counter()
    result = bpy.ops.projection_paint.import_colmap()
    if result != {"FINISHED"}:
        raise RuntimeError(f"COLMAP Import가 실패했습니다: {result}, {settings.status}")
    camera_seconds = perf_counter() - camera_started
    model_started = perf_counter()
    result = bpy.ops.wm.obj_import(
        filepath=str(OBJ_PATH),
        forward_axis="Y",
        up_axis="Z",
    )
    if result != {"FINISHED"}:
        raise RuntimeError(f"OBJ Import가 실패했습니다: {result}")
    imported_meshes = tuple(
        obj for obj in bpy.context.selected_objects if obj.type == "MESH"
    )
    if not imported_meshes:
        raise RuntimeError("OBJ Import 결과 Mesh가 없습니다.")
    target = max(imported_meshes, key=lambda obj: len(obj.data.polygons))
    from bl_ext.user_default.colmap_tools.tools.projection_paint.blender.model_import import (
        apply_model_axis,
    )

    apply_model_axis(
        scene,
        imported_meshes,
        preset="REALITY_CAPTURE",
        dataset_uuid=str(settings.dataset_uuid),
    )
    target.active_material_index = 0
    settings.paint_target = target
    from bl_ext.user_default.colmap_tools.tools.projection_paint.blender import paint_runtime

    original_reconnect = paint_runtime.reconnect_stencil_texture

    def counted_reconnect(*args, **kwargs):
        """초기 진입 Reconnect 호출 횟수를 기록하고 원래 함수를 실행합니다."""

        STATE["entry_reconnect_calls"] += 1
        return original_reconnect(*args, **kwargs)

    paint_runtime.reconnect_stencil_texture = counted_reconnect
    STATE["managed_brushes_before_entry"] = _managed_stencil_brush_names()
    result = bpy.ops.projection_paint.enter_paint()
    if result != {"FINISHED"}:
        raise RuntimeError(f"Paint 진입이 실패했습니다: {result}, {settings.status}")
    window, _area, _region, space, rv3d = _view_parts()
    from bl_ext.user_default.colmap_tools.tools.projection_paint.blender.paint_runtime import (
        active_paint_runtime,
    )

    runtime = active_paint_runtime()
    if runtime is None:
        raise RuntimeError("Paint 진입 직후 Frame Source Runtime이 없습니다.")
    managed_after_entry = _managed_stencil_brush_names()
    transient_names = tuple(
        name
        for name in managed_after_entry
        if name not in STATE["managed_brushes_before_entry"]
    )
    if len(transient_names) != 1:
        raise RuntimeError("Paint 진입용 임시 Brush가 정확히 한 개가 아닙니다.")
    if transient_names[0].startswith("Projection"):
        raise RuntimeError("Paint 진입 중 Projection 이름의 Brush가 생성되었습니다.")
    target_visible = target.visible_get(
        view_layer=window.view_layer,
        viewport=space,
    )
    STATE["result"].update(
        {
            "dataset_root": str(COLMAP_ROOT),
            "obj_path": str(OBJ_PATH),
            "camera_count": len(
                [
                    obj
                    for obj in scene.objects
                    if obj.get("pp_record_kind") == "CAMERA_OBJECT"
                ]
            ),
            "marker_count": len(scene.timeline_markers),
            "point_count": len(
                next(
                    obj
                    for obj in scene.objects
                    if obj.get("pp_record_kind") == "POINT_MESH"
                ).data.vertices
            ),
            "mesh_vertices": len(target.data.vertices),
            "mesh_polygons": len(target.data.polygons),
            "material_slots": len(target.material_slots),
            "uv_layers": len(target.data.uv_layers),
            "camera_import_seconds": round(camera_seconds, 3),
            "model_import_seconds": round(perf_counter() - model_started, 3),
            "target_name": target.name,
            "paint_workspace": window.workspace.name,
            "target_visible_during_operator": bool(target_visible),
            "camera_view_during_operator": rv3d.view_perspective,
            "reconnect_calls_during_operator": STATE["entry_reconnect_calls"],
            "active_user_brush": runtime.stencil.brush.name,
            "projection_brush_created": False,
            "transient_brush_count": len(transient_names),
        }
    )
    STATE["target_name"] = target.name
    STATE["phase"] = 1
    return 0.8


def _phase_view_move() -> float:
    """첫 Source 연결을 확인하고 Camera View를 이동·확대합니다."""

    from bl_ext.user_default.colmap_tools.tools.projection_paint.blender.paint_runtime import (
        active_paint_runtime,
    )

    runtime = active_paint_runtime()
    if (
        runtime is None
        or runtime.stencil.source_image_id is None
        or STATE["entry_reconnect_calls"] == 0
    ) and STATE["entry_waits"] < 20:
        STATE["entry_waits"] += 1
        return 0.25
    if runtime is None or runtime.stencil.source_image_id is None:
        from bl_ext.user_default.colmap_tools.tools.projection_paint.blender.lifecycle import (
            lifecycle_registry,
        )

        registry = lifecycle_registry()
        failures = [
            f"{item.component}:{item.exception_type}:{item.message}"
            for item in registry.failures
        ]
        settings = bpy.context.scene.projection_paint
        raise RuntimeError(
            "첫 Frame Source Stencil이 제한 시간 안에 연결되지 않았습니다. "
            f"status={settings.status!r}, paint_state={settings.paint_state!r}, "
            f"workspace={bpy.context.window.workspace.name!r}, "
            f"registry=(active={registry.active}, suspended={registry.suspended}, "
            f"epoch={registry.epoch}), failures={failures}"
        )
    window, _area, _region, space, rv3d = _view_parts()
    target = bpy.data.objects[STATE["target_name"]]
    STATE["result"].update(
        {
            "settled_workspace": window.workspace.name,
            "target_visible_after_workspace": bool(
                target.visible_get(
                    view_layer=window.view_layer,
                    viewport=space,
                )
            ),
            "camera_view_after_workspace": rv3d.view_perspective,
            "shading_after_workspace": space.shading.type,
            "stencil_mapping_after_workspace": (
                runtime.stencil.brush.texture_slot.map_mode
            ),
            "entry_reconnect_calls": STATE["entry_reconnect_calls"],
            "entry_waits": STATE["entry_waits"],
            "paint_status_after_workspace": (
                bpy.context.scene.projection_paint.status
            ),
            "stencil_connected_after_workspace": (
                runtime.stencil.brush.texture
                is runtime.stencil.managed_texture
            ),
        }
    )
    if STATE["entry_reconnect_calls"] != 1:
        raise RuntimeError(
            "Paint 진입 Reconnect가 정확히 한 번 실행되지 않았습니다: "
            f"{STATE['entry_reconnect_calls']}회"
        )
    if rv3d.view_perspective != "CAMERA":
        raise RuntimeError("작업공간 전환 뒤 초기 Camera View가 유지되지 않았습니다.")
    if space.shading.type not in {"MATERIAL", "RENDERED"}:
        raise RuntimeError("작업공간 전환 뒤 Material Preview가 적용되지 않았습니다.")
    if not STATE["result"]["target_visible_after_workspace"]:
        raise RuntimeError("작업공간 전환 뒤 Paint Target이 보이지 않습니다.")
    if not STATE["result"]["stencil_connected_after_workspace"]:
        raise RuntimeError("작업공간 전환 뒤 Stencil Texture가 연결되지 않았습니다.")
    brush = runtime.stencil.brush
    initial_position = tuple(float(value) for value in brush.stencil_pos)
    initial_dimensions = tuple(float(value) for value in brush.stencil_dimension)
    initial_offset = tuple(float(value) for value in rv3d.view_camera_offset)
    initial_zoom = float(rv3d.view_camera_zoom)
    STATE["initial_stencil_position"] = initial_position
    STATE["initial_stencil_dimensions"] = initial_dimensions
    STATE["result"].update(
        {
            "first_image_id": int(runtime.stencil.source_image_id),
            "first_source": str(runtime.stencil.source_image.name),
            "initial_stencil_position": list(initial_position),
            "initial_stencil_dimensions": list(initial_dimensions),
            "initial_camera_view_offset": list(initial_offset),
            "initial_camera_view_zoom": initial_zoom,
        }
    )
    rv3d.view_camera_offset = (initial_offset[0] + 0.12, initial_offset[1] - 0.08)
    rv3d.view_camera_zoom = min(600.0, initial_zoom + 30.0)
    STATE["phase"] = 2
    return 0.6


def _phase_verify_view_tracking() -> float:
    """이동·확대된 Camera Frame과 Stencil 정렬 결과가 일치하는지 확인합니다."""

    from bl_ext.user_default.colmap_tools.tools.projection_paint.blender.paint_runtime import (
        active_paint_runtime,
    )
    from bl_ext.user_default.colmap_tools.tools.projection_paint.blender.stencil_session import (
        camera_frame_stencil,
        project_camera_frame,
    )

    runtime = active_paint_runtime()
    if runtime is None or runtime.stencil.source_image is None:
        raise RuntimeError("Camera View 추적 검증용 Runtime이 없습니다.")
    _window, _area, _region, _space, rv3d = _view_parts()
    brush = runtime.stencil.brush
    position = tuple(float(value) for value in brush.stencil_pos)
    dimensions = tuple(float(value) for value in brush.stencil_dimension)
    expected_position, expected_dimensions = camera_frame_stencil(
        project_camera_frame(runtime.stencil, runtime.stencil.camera),
        int(runtime.stencil.source_image.size[0]),
        int(runtime.stencil.source_image.size[1]),
    )
    if not all(
        math.isclose(actual, expected, rel_tol=1.0e-5, abs_tol=1.0e-3)
        for actual, expected in zip(position, expected_position, strict=True)
    ):
        raise RuntimeError(
            f"Camera View 이동 뒤 Stencil 위치가 다릅니다: {position}, {expected_position}"
        )
    if not all(
        math.isclose(actual, expected, rel_tol=1.0e-5, abs_tol=1.0e-3)
        for actual, expected in zip(dimensions, expected_dimensions, strict=True)
    ):
        raise RuntimeError(
            "Camera View 확대 뒤 Stencil 크기가 다릅니다: "
            f"{dimensions}, {expected_dimensions}"
        )
    initial_position = STATE["initial_stencil_position"]
    initial_dimensions = STATE["initial_stencil_dimensions"]
    if max(abs(a - b) for a, b in zip(position, initial_position, strict=True)) < 1.0:
        raise RuntimeError("Camera View 이동 뒤 Stencil 위치가 갱신되지 않았습니다.")
    if max(abs(a - b) for a, b in zip(dimensions, initial_dimensions, strict=True)) < 1.0:
        raise RuntimeError("Camera View 확대 뒤 Stencil 크기가 갱신되지 않았습니다.")
    STATE["result"].update(
        {
            "tracked_camera_view_offset": [
                float(value) for value in rv3d.view_camera_offset
            ],
            "tracked_camera_view_zoom": float(rv3d.view_camera_zoom),
            "tracked_stencil_position": list(position),
            "tracked_stencil_dimensions": list(dimensions),
            "viewport_tracking_passed": True,
        }
    )
    rv3d.view_perspective = "PERSP"
    STATE["phase"] = 3
    return 0.7


def _phase_verify_camera_exit() -> float:
    """Camera View 이탈 시 Stencil 연결만 해제되는지 확인합니다."""

    from bl_ext.user_default.colmap_tools.tools.projection_paint.blender.paint_runtime import (
        active_paint_runtime,
    )

    runtime = active_paint_runtime()
    if runtime is None:
        raise RuntimeError("Camera View 이탈 검증용 Runtime이 없습니다.")
    stencil = runtime.stencil
    if stencil.brush.texture is not None:
        raise RuntimeError("Camera View 이탈 뒤 Stencil Texture가 연결되어 있습니다.")
    if stencil.managed_texture.image is not stencil.source_image:
        raise RuntimeError("Camera View 이탈 중 현재 Source Image가 유실되었습니다.")
    if not stencil.outside_camera_view or stencil.manual_disabled:
        raise RuntimeError("Camera View 이탈 상태가 수동 해제로 잘못 기록되었습니다.")
    if bpy.context.scene.projection_paint.paint_state != "FROZEN":
        raise RuntimeError("Camera View 이탈 상태가 FROZEN으로 표시되지 않았습니다.")
    STATE["result"]["camera_exit_disconnected"] = True
    _window, _area, _region, _space, rv3d = _view_parts()
    rv3d.view_perspective = "CAMERA"
    STATE["phase"] = 4
    return 0.7


def _phase_switch_brush() -> float:
    """Camera View 복귀 연결을 확인하고 다른 Brush Asset을 선택합니다."""

    from bl_ext.user_default.colmap_tools.tools.projection_paint.blender.paint_runtime import (
        active_paint_runtime,
    )

    runtime = active_paint_runtime()
    if runtime is None:
        raise RuntimeError("Brush 교체 검증용 Runtime이 없습니다.")
    stencil = runtime.stencil
    if stencil.brush.texture is not stencil.managed_texture:
        raise RuntimeError("Camera View 복귀 뒤 Stencil Texture가 재연결되지 않았습니다.")
    if stencil.outside_camera_view or stencil.manual_disabled:
        raise RuntimeError("Camera View 복귀 뒤 Stencil 활성 상태가 복구되지 않았습니다.")
    source_brush = stencil.brush
    if source_brush is None:
        raise RuntimeError("교체 검증에 사용할 원본 Brush가 없습니다.")
    alternate = source_brush.copy()
    alternate.name = "Paint Brush Switch Test"
    for key in tuple(alternate.keys()):
        if str(key).startswith("pp_"):
            del alternate[key]
    if alternate.asset_data is None:
        alternate.asset_mark()
    alternate.strength = 0.37
    window, area, region, _space, _rv3d = _view_parts()
    with bpy.context.temp_override(
        window=window,
        screen=window.screen,
        area=area,
        region=region,
    ):
        result = bpy.ops.brush.asset_activate(
            asset_library_type="LOCAL",
            relative_asset_identifier=f"Brush/{alternate.name}",
        )
    if result != {"FINISHED"}:
        raise RuntimeError(f"교체 검증용 Brush 활성화가 실패했습니다: {result}")
    if bpy.context.scene.tool_settings.image_paint.brush is not alternate:
        raise RuntimeError("교체 검증용 Brush가 활성화되지 않았습니다.")
    STATE["previous_brush_pointer"] = int(stencil.brush.as_pointer())
    STATE["alternate_brush_name"] = alternate.name
    STATE["phase"] = 5
    return 0.7


def _phase_verify_brush_switch() -> float:
    """새 활성 Brush에 같은 Stencil Texture가 직접 이어졌는지 확인합니다."""

    from bl_ext.user_default.colmap_tools.tools.projection_paint.blender.paint_runtime import (
        active_paint_runtime,
    )

    runtime = active_paint_runtime()
    if runtime is None:
        raise RuntimeError("Brush 교체 결과를 확인할 Runtime이 없습니다.")
    stencil = runtime.stencil
    brush = stencil.brush
    if int(brush.as_pointer()) == STATE["previous_brush_pointer"]:
        raise RuntimeError("Brush 교체 뒤 활성 Brush가 갱신되지 않았습니다.")
    if bpy.context.scene.tool_settings.image_paint.brush is not brush:
        raise RuntimeError("Brush 교체 뒤 선택한 Brush가 활성화되지 않았습니다.")
    if brush.texture is not stencil.managed_texture:
        raise RuntimeError("Brush 교체 뒤 Stencil Texture가 연결되지 않았습니다.")
    if brush.texture_slot.map_mode != "STENCIL":
        raise RuntimeError("Brush 교체 뒤 STENCIL Mapping이 적용되지 않았습니다.")
    if not math.isclose(float(brush.strength), 0.37, abs_tol=1.0e-6):
        raise RuntimeError("Brush 교체 뒤 선택한 Brush 설정이 보존되지 않았습니다.")
    if brush.name != STATE["alternate_brush_name"]:
        raise RuntimeError("교체한 사용자 Brush가 직접 사용되지 않았습니다.")
    if _managed_stencil_brush_names() != STATE["managed_brushes_before_entry"]:
        raise RuntimeError("Brush 교체 뒤 임시 Stencil Brush가 정리되지 않았습니다.")
    if stencil.manual_disabled or stencil.outside_camera_view:
        raise RuntimeError("Brush 교체가 Stencil 비활성 상태로 잘못 처리되었습니다.")
    STATE["result"].update(
        {
            "camera_return_reconnected": True,
            "brush_switch_reconnected": True,
            "switched_brush_strength": round(float(brush.strength), 6),
        }
    )
    bpy.context.scene.frame_set(2)
    STATE["phase"] = 6
    return 0.8


def _phase_manual_disable() -> float:
    """Frame 동기화를 확인하고 Camera View를 다시 이동·확대합니다."""

    from bl_ext.user_default.colmap_tools.tools.projection_paint.blender.paint_runtime import (
        active_paint_runtime,
    )

    runtime = active_paint_runtime()
    if runtime is None:
        raise RuntimeError("Frame Source Runtime이 종료되었습니다.")
    if runtime.stencil.source_image_id == STATE["result"]["first_image_id"]:
        raise RuntimeError("Frame 변경 뒤 Source IMAGE_ID가 갱신되지 않았습니다.")
    if runtime.scene.camera is not runtime.stencil.camera:
        raise RuntimeError("Frame 변경 뒤 Scene Camera와 Stencil Camera가 다릅니다.")
    _window, _area, _region, _space, rv3d = _view_parts()
    if rv3d.view_perspective != "CAMERA":
        raise RuntimeError("Frame 변경 뒤 Camera View로 돌아오지 않았습니다.")
    STATE["result"]["second_image_id"] = int(runtime.stencil.source_image_id)
    brush = runtime.stencil.brush
    STATE["post_frame_stencil_position"] = tuple(
        float(value) for value in brush.stencil_pos
    )
    STATE["post_frame_stencil_dimensions"] = tuple(
        float(value) for value in brush.stencil_dimension
    )
    offset = tuple(float(value) for value in rv3d.view_camera_offset)
    rv3d.view_camera_offset = (offset[0] - 0.08, offset[1] + 0.06)
    rv3d.view_camera_zoom = min(600.0, float(rv3d.view_camera_zoom) + 25.0)
    STATE["phase"] = 7
    return 0.8


def _phase_verify_post_frame_tracking() -> float:
    """Frame 변경 뒤에도 Camera View 추적 Timer가 유지되는지 확인합니다."""

    from bl_ext.user_default.colmap_tools.tools.projection_paint.blender.lifecycle import (
        lifecycle_registry,
    )
    from bl_ext.user_default.colmap_tools.tools.projection_paint.blender.paint_runtime import (
        active_paint_runtime,
    )

    runtime = active_paint_runtime()
    if runtime is None:
        raise RuntimeError("Frame 변경 뒤 추적 검증용 Runtime이 없습니다.")
    brush = runtime.stencil.brush
    position = tuple(float(value) for value in brush.stencil_pos)
    dimensions = tuple(float(value) for value in brush.stencil_dimension)
    if all(
        math.isclose(actual, before, abs_tol=1.0e-3)
        for actual, before in zip(
            position,
            STATE["post_frame_stencil_position"],
            strict=True,
        )
    ):
        raise RuntimeError("Frame 변경 뒤 Camera 이동을 Stencil이 추적하지 못했습니다.")
    if all(
        math.isclose(actual, before, abs_tol=1.0e-3)
        for actual, before in zip(
            dimensions,
            STATE["post_frame_stencil_dimensions"],
            strict=True,
        )
    ):
        raise RuntimeError("Frame 변경 뒤 Camera 확대를 Stencil이 추적하지 못했습니다.")
    failures = tuple(
        failure
        for failure in lifecycle_registry().failures
        if failure.component == "stencil_view_tracking"
    )
    if failures:
        raise RuntimeError(
            f"Frame 변경 뒤 Stencil 추적 오류가 기록되었습니다: {failures}"
        )
    STATE["result"]["post_frame_viewport_tracking_passed"] = True
    runtime.stencil.brush.texture_slot.map_mode = "VIEW_PLANE"
    bpy.context.scene.frame_set(3)
    STATE["phase"] = 8
    return 0.8


def _phase_stroke_save() -> None:
    """수동 해제 존중·Reconnect·Stroke·Pack·Blend 저장을 검증합니다."""

    from bl_ext.user_default.colmap_tools.tools.projection_paint.blender.paint_runtime import (
        active_paint_runtime,
    )

    runtime = active_paint_runtime()
    if runtime is None:
        raise RuntimeError("Stroke 직전 Frame Source Runtime이 없습니다.")
    if runtime.stencil.brush.texture_slot.map_mode != "VIEW_PLANE":
        raise RuntimeError("수동 Stencil 해제가 Frame 변경 중 자동 복구되었습니다.")
    if not runtime.stencil.manual_disabled:
        raise RuntimeError("수동 Stencil 해제 상태가 기록되지 않았습니다.")
    result = bpy.ops.projection_paint.reconnect_stencil()
    if result != {"FINISHED"}:
        raise RuntimeError(f"Stencil Reconnect가 실패했습니다: {result}")
    if runtime.stencil.brush.texture_slot.map_mode != "STENCIL":
        raise RuntimeError("Reconnect 뒤 STENCIL Mapping이 복구되지 않았습니다.")
    target = bpy.data.objects[STATE["target_name"]]
    from bl_ext.user_default.colmap_tools.tools.projection_paint.blender.paint_session import (
        active_paint_session,
    )

    paint = active_paint_session()
    if paint is None:
        raise RuntimeError("Stroke 직전 Paint Session이 없습니다.")
    overlay = paint.setup.overlay
    stroke_result = _stroke(target, overlay)
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    scene = bpy.context.scene
    scene.render.image_settings.file_format = "PNG"
    scene.render.image_settings.color_mode = "RGBA"
    overlay.save_render(str(OVERLAY_PATH), scene=scene)
    screenshot_saved = False
    try:
        bpy.ops.screen.screenshot(filepath=str(SCREENSHOT_PATH))
        screenshot_saved = SCREENSHOT_PATH.is_file()
    except (AttributeError, RuntimeError):
        screenshot_saved = False
    scene["pp_actual_overlay_name"] = overlay.name
    scene["pp_actual_stroke_after_rgba"] = stroke_result["after_rgba"]
    scene["pp_actual_camera_count"] = STATE["result"]["camera_count"]
    scene["pp_actual_marker_count"] = STATE["result"]["marker_count"]
    scene["pp_actual_point_count"] = STATE["result"]["point_count"]
    save_result = bpy.ops.wm.save_as_mainfile(filepath=str(BLEND_PATH))
    if save_result != {"FINISHED"}:
        raise RuntimeError(f"실제 테스트 Blend 저장이 실패했습니다: {save_result}")
    STATE["result"].update(
        {
            "third_image_id": int(runtime.stencil.source_image_id),
            "manual_disable_preserved": True,
            "reconnect_restored": True,
            "stroke": stroke_result,
            "overlay_name": overlay.name,
            "overlay_size": [int(value) for value in overlay.size],
            "overlay_dirty_after_stroke": bool(overlay.is_dirty),
            "overlay_packed_after_save": overlay.packed_file is not None,
            "blend_path": str(BLEND_PATH),
            "overlay_path": str(OVERLAY_PATH),
            "screenshot_path": str(SCREENSHOT_PATH) if screenshot_saved else "",
        }
    )
    _write_result(True)
    _quit()


def _run_phase():
    """Timer 단계별로 GUI Event Loop를 양보하며 실제 테스트를 실행합니다."""

    try:
        phase = int(STATE["phase"])
        if phase == 0:
            return _phase_import()
        if phase == 1:
            return _phase_view_move()
        if phase == 2:
            return _phase_verify_view_tracking()
        if phase == 3:
            return _phase_verify_camera_exit()
        if phase == 4:
            return _phase_switch_brush()
        if phase == 5:
            return _phase_verify_brush_switch()
        if phase == 6:
            return _phase_manual_disable()
        if phase == 7:
            return _phase_verify_post_frame_tracking()
        if phase == 8:
            _phase_stroke_save()
            return None
        raise RuntimeError(f"알 수 없는 테스트 단계입니다: {phase}")
    except Exception as exc:
        _fail(exc)
        return None


OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
bpy.app.timers.register(_run_phase, first_interval=0.3)
