"""실제 Paint 파일에서 KDB 최근접 Camera Reconnect를 GUI로 검증합니다."""

from __future__ import annotations

import json
import math
from pathlib import Path
from time import perf_counter
import traceback

import bpy
from mathutils import Vector


RESULT_PATH = Path(r"C:\tmp\projection_paint_actual\kdb_reconnect_result.json")
STATE = {
    "phase": 0,
    "started": perf_counter(),
    "result": {},
    "target_entry": None,
    "index": None,
    "pose": None,
}


def _pointer(value) -> int:
    """삭제된 Blender ID Pointer를 안전하게 0으로 변환합니다."""

    try:
        return int(value.as_pointer()) if value is not None else 0
    except (AttributeError, ReferenceError, RuntimeError, TypeError, ValueError):
        return 0


def _view_parts():
    """현재 Window의 3D Viewport와 Window Region을 반환합니다."""

    window = bpy.context.window
    area = next(
        (candidate for candidate in window.screen.areas if candidate.type == "VIEW_3D"),
        None,
    )
    region = next(
        (candidate for candidate in getattr(area, "regions", ()) if candidate.type == "WINDOW"),
        None,
    )
    if window is None or area is None or region is None:
        raise RuntimeError("KDB Reconnect에 사용할 3D Viewport가 없습니다.")
    return window, area, region, area.spaces.active, area.spaces.active.region_3d


def _override():
    """현재 3D Viewport를 Operator Context로 사용하는 임시 Override를 반환합니다."""

    window, area, region, _space, _rv3d = _view_parts()
    target = bpy.context.scene.projection_paint.paint_target
    return bpy.context.temp_override(
        window=window,
        screen=window.screen,
        area=area,
        region=region,
        active_object=target,
        object=target,
    )


def _managed_counts() -> dict[str, int]:
    """반복 Reconnect 전후의 관리 Texture·Brush·Source 수를 반환합니다."""

    return {
        "textures": sum(
            texture.get("pp_record_kind") == "STENCIL_TEXTURE"
            for texture in bpy.data.textures
        ),
        "brushes": sum(
            brush.get("pp_record_kind") == "STENCIL_BRUSH"
            for brush in bpy.data.brushes
        ),
        "sources": sum(
            image.get("pp_record_kind") == "SOURCE_OWNED"
            for image in bpy.data.images
        ),
    }


def _write_result(success: bool, error: str = "") -> None:
    """실제 KDB GUI 검증 결과를 UTF-8 JSON으로 기록합니다."""

    RESULT_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(STATE["result"])
    payload["success"] = bool(success)
    payload["error"] = str(error)
    payload["elapsed_seconds"] = round(perf_counter() - STATE["started"], 3)
    RESULT_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _fail(exc: Exception) -> None:
    """실패 원인과 Traceback을 기록하고 Blender를 종료합니다."""

    STATE["result"]["traceback"] = traceback.format_exc()
    _write_result(False, str(exc))
    bpy.ops.wm.quit_blender()


def _target_fixture():
    """현재 Camera에서 World 거리상 가장 먼 관리 Camera Entry와 Object를 반환합니다."""

    from bl_ext.user_default.colmap_tools.tools.projection_paint.blender.camera_scene import (
        runtime_for_scene,
        scene_manifest,
    )

    scene = bpy.context.scene
    runtime = runtime_for_scene(scene)
    manifest = scene_manifest(scene)
    if runtime is None or manifest is None or len(manifest.entries) != 305:
        raise RuntimeError("Camera 305개의 Runtime이 준비되지 않았습니다.")
    current_position = Vector(scene.camera.matrix_world.translation)
    entry = max(
        manifest.entries,
        key=lambda candidate: (
            Vector(runtime.objects[candidate.camera_uuid].matrix_world.translation)
            - current_position
        ).length_squared,
    )
    return runtime, entry, runtime.objects[entry.camera_uuid]


def _phase_prepare_free_view() -> float:
    """KDB Index를 구축하고 다른 관리 Camera 위치에 자유 Perspective 시점을 둡니다."""

    import bl_ext.user_default.colmap_tools as addon
    from bl_ext.user_default.colmap_tools.tools.projection_paint.blender.camera_spatial import (
        build_camera_kdb_index,
    )

    if addon.__version__ != "0.1.29":
        raise RuntimeError(f"설치된 Extension 버전이 다릅니다: {addon.__version__}")
    scene = bpy.context.scene
    settings = scene.projection_paint
    if not bool(settings.paint_enabled) or settings.paint_target is None:
        raise RuntimeError("실제 파일의 Paint 상태가 활성화되어 있지 않습니다.")
    runtime, entry, camera = _target_fixture()
    started = perf_counter()
    index = build_camera_kdb_index(runtime)
    build_ms = (perf_counter() - started) * 1000.0
    _window, area, _region, space, rv3d = _view_parts()
    forward = camera.matrix_world.to_3x3() @ Vector((0.0, 0.0, -1.0))
    rv3d.view_perspective = "PERSP"
    rv3d.view_rotation = camera.matrix_world.to_quaternion()
    rv3d.view_location = camera.matrix_world.translation
    rv3d.view_distance = 0.0
    space.camera = camera
    area.tag_redraw()
    STATE["target_entry"] = entry
    STATE["index"] = index
    STATE["result"].update(
        {
            "version": addon.__version__,
            "camera_count": len(runtime.manifest.entries),
            "initial_frame": int(scene.frame_current),
            "target_frame": int(entry.frame),
            "target_image_id": int(entry.image_id),
            "kdb_height": int(index.tree.height),
            "kdb_build_ms": round(build_ms, 4),
        }
    )
    STATE["phase"] = 1
    return 0.8


def _phase_reconnect() -> float:
    """자유 시점 Pose와 KDB 선택을 확인하고 실제 Reconnect 버튼을 실행합니다."""

    from bl_ext.user_default.colmap_tools.tools.projection_paint.blender.camera_spatial import (
        select_best_camera,
        viewport_pose,
    )
    from bl_ext.user_default.colmap_tools.tools.projection_paint.blender.paint_runtime import (
        ensure_paint_runtime,
    )
    from bl_ext.user_default.colmap_tools.tools.projection_paint.blender.camera_scene import (
        runtime_for_scene,
    )

    scene = bpy.context.scene
    settings = scene.projection_paint
    entry = STATE["target_entry"]
    index = STATE["index"]
    with _override():
        pose = viewport_pose(bpy.context)
        selected = select_best_camera(index, pose)
    origin_error = (
        Vector(pose.origin)
        - Vector(runtime_for_scene(scene).objects[entry.camera_uuid].matrix_world.translation)
    ).length
    if origin_error > 1.0e-3:
        raise RuntimeError(f"자유 View 원점이 목표 Camera 위치와 다릅니다: {origin_error}")
    if selected.entry.image_id != entry.image_id:
        raise RuntimeError(
            f"KDB 후보가 목표 Camera와 다릅니다: {selected.entry.image_id}"
        )
    runtime = ensure_paint_runtime(scene)
    runtime.camera_index = None
    counts_before = _managed_counts()
    with _override():
        result = bpy.ops.projection_paint.reconnect_stencil()
    if result != {"FINISHED"}:
        raise RuntimeError(f"실제 Reconnect Operator가 실패했습니다: {result}")
    if runtime.camera_index is None:
        raise RuntimeError("Reconnect가 Paint Runtime KDB Index를 자동 구축하지 않았습니다.")
    brush = settings.stencil_brush
    texture = settings.stencil_texture
    source = settings.stencil_source_image
    if int(scene.frame_current) != int(entry.frame):
        raise RuntimeError("Reconnect가 선택 Camera의 Marker Frame으로 이동하지 않았습니다.")
    if int(settings.active_image_id) != int(entry.image_id):
        raise RuntimeError("Reconnect가 선택 Camera의 IMAGE_ID Source를 연결하지 않았습니다.")
    if not all((_pointer(brush), _pointer(texture), _pointer(source))):
        raise RuntimeError("Reconnect 뒤 Stencil Blender Pointer가 불완전합니다.")
    if brush.texture is not texture or texture.image is not source:
        raise RuntimeError("Reconnect 뒤 Brush Texture와 Source Image 연결이 다릅니다.")
    if brush.texture_slot.map_mode != "STENCIL":
        raise RuntimeError("Reconnect 뒤 Brush Mapping이 STENCIL이 아닙니다.")
    baseline = _managed_counts()
    STATE["pose"] = pose
    STATE["result"].update(
        {
            "free_view_origin_error": origin_error,
            "selected_frame": int(scene.frame_current),
            "selected_image_id": int(settings.active_image_id),
            "selected_distance": round(float(selected.distance), 6),
            "selected_alignment": round(float(selected.alignment), 6),
            "counts_before_first_reconnect": counts_before,
            "baseline_counts": baseline,
        }
    )
    STATE["phase"] = 2
    return 0.8


def _phase_repeat_and_ortho_guard() -> None:
    """반복 검색 성능·자원 수와 Orthographic 실패 전 무변경 계약을 확인합니다."""

    from bl_ext.user_default.colmap_tools.tools.projection_paint.blender.camera_spatial import (
        select_best_camera,
    )
    from bl_ext.user_default.colmap_tools.tools.projection_paint.blender.camera_scene import (
        runtime_for_scene,
    )
    from bl_ext.user_default.colmap_tools.tools.projection_paint.blender import paint_runtime

    scene = bpy.context.scene
    settings = scene.projection_paint
    samples = []
    for _index in range(100):
        started = perf_counter()
        select_best_camera(STATE["index"], STATE["pose"])
        samples.append((perf_counter() - started) * 1000.0)
    for _index in range(20):
        with _override():
            if bpy.ops.projection_paint.reconnect_stencil() != {"FINISHED"}:
                raise RuntimeError("반복 Reconnect가 실패했습니다.")
    final_counts = _managed_counts()
    baseline = STATE["result"]["baseline_counts"]
    if final_counts != baseline:
        raise RuntimeError(f"반복 Reconnect 뒤 관리 자원 수가 바뀌었습니다: {final_counts}")
    frame_before = int(scene.frame_current)
    image_before = int(settings.active_image_id)
    runtime = runtime_for_scene(scene)
    rollback_entry = max(
        (
            entry
            for entry in runtime.manifest.entries
            if int(entry.frame) != frame_before
        ),
        key=lambda entry: (
            Vector(runtime.objects[entry.camera_uuid].matrix_world.translation)
            - Vector(scene.camera.matrix_world.translation)
        ).length_squared,
    )
    rollback_camera = runtime.objects[rollback_entry.camera_uuid]
    _window, area, _region, space, rv3d = _view_parts()
    rv3d.view_perspective = "PERSP"
    rv3d.view_rotation = rollback_camera.matrix_world.to_quaternion()
    rv3d.view_location = rollback_camera.matrix_world.translation
    rv3d.view_distance = 0.0
    space.camera = rollback_camera
    area.tag_redraw()
    view_location_before = tuple(float(value) for value in rv3d.view_location)
    original_reconnect = paint_runtime.reconnect_stencil_texture
    injected = {"pending": True}

    def fail_once(*args, **kwargs):
        """첫 Stencil 연결만 실패시키고 Rollback 재연결은 원래 함수로 위임합니다."""

        if injected["pending"]:
            injected["pending"] = False
            raise RuntimeError("KDB Rollback 검증 실패 주입")
        return original_reconnect(*args, **kwargs)

    paint_runtime.reconnect_stencil_texture = fail_once
    try:
        with _override():
            paint_runtime.reconnect_nearest_camera(bpy.context)
    except paint_runtime.PaintRuntimeError as exc:
        if "Rollback 검증 실패 주입" not in str(exc):
            raise
    else:
        raise RuntimeError("강제 연결 실패가 Reconnect Transaction에서 전파되지 않았습니다.")
    finally:
        paint_runtime.reconnect_stencil_texture = original_reconnect
    if int(scene.frame_current) != frame_before or int(settings.active_image_id) != image_before:
        raise RuntimeError("Reconnect 실패 뒤 원래 Frame과 IMAGE_ID가 복구되지 않았습니다.")
    if str(rv3d.view_perspective) != "PERSP" or max(
        abs(float(actual) - expected)
        for actual, expected in zip(rv3d.view_location, view_location_before)
    ) > 1.0e-5:
        raise RuntimeError("Reconnect 실패 뒤 원래 자유 View가 복구되지 않았습니다.")
    if settings.stencil_brush.texture is not settings.stencil_texture:
        raise RuntimeError("Reconnect 실패 뒤 원래 Stencil Texture가 복구되지 않았습니다.")
    rv3d.view_perspective = "ORTHO"
    try:
        with _override():
            ortho_result = bpy.ops.projection_paint.reconnect_stencil()
    except RuntimeError as exc:
        if "Orthographic View" not in str(exc):
            raise
        ortho_result = {"CANCELLED"}
    if ortho_result != {"CANCELLED"}:
        raise RuntimeError("Orthographic View에서 Reconnect가 차단되지 않았습니다.")
    if int(scene.frame_current) != frame_before or int(settings.active_image_id) != image_before:
        raise RuntimeError("Orthographic 차단 전에 Scene 상태가 변경되었습니다.")
    ordered = sorted(samples)
    p95_index = min(len(ordered) - 1, max(0, math.ceil(len(ordered) * 0.95) - 1))
    STATE["result"].update(
        {
            "query_samples": len(ordered),
            "query_median_ms": round(ordered[len(ordered) // 2], 6),
            "query_p95_ms": round(ordered[p95_index], 6),
            "query_max_ms": round(ordered[-1], 6),
            "repeat_count": 20,
            "final_counts": final_counts,
            "rollback_verified": True,
            "rollback_frame": frame_before,
            "rollback_image_id": image_before,
            "counts_after_rollback": _managed_counts(),
            "orthographic_guard": True,
            "status": str(settings.status),
        }
    )
    _write_result(True)
    bpy.ops.wm.quit_blender()
    return None


def _run_phase():
    """GUI Event Loop를 양보하며 KDB 실제 검증 단계를 순서대로 실행합니다."""

    try:
        phase = int(STATE["phase"])
        if phase == 0:
            return _phase_prepare_free_view()
        if phase == 1:
            return _phase_reconnect()
        if phase == 2:
            return _phase_repeat_and_ortho_guard()
        raise RuntimeError(f"알 수 없는 KDB GUI 검증 단계입니다: {phase}")
    except Exception as exc:
        _fail(exc)
        return None


RESULT_PATH.parent.mkdir(parents=True, exist_ok=True)
bpy.app.timers.register(_run_phase, first_interval=1.0)
