"""실제 Stroke 결과 Blend를 재열어 Pack과 Camera 보호 상태를 검증합니다."""

from __future__ import annotations

import json
from pathlib import Path

import bpy


RESULT_PATH = Path(r"C:\tmp\projection_paint_actual\result.json")
POINTS_PATH = Path(
    r"E:\DEV\DEV_RND\GSSplat\00_photogrammetry"
    r"\6_Flying_squirrel\rc\6_Flying_squirrel_workspace"
    r"\final\sparse\0\points3D.txt"
)


def _maximum_rgba(image, uv: tuple[float, float], radius: int = 8) -> tuple[float, ...]:
    """UV 주변 Pixel Window에서 Alpha가 가장 큰 RGBA를 반환합니다."""

    width, height = (int(value) for value in image.size)
    center_x = min(width - 1, max(0, int((uv[0] % 1.0) * width)))
    center_y = min(height - 1, max(0, int((uv[1] % 1.0) * height)))
    minimum_x = max(0, center_x - radius)
    maximum_x = min(width, center_x + radius + 1)
    maximum = (0.0, 0.0, 0.0, 0.0)
    for y in range(max(0, center_y - radius), min(height, center_y + radius + 1)):
        start = (y * width + minimum_x) * 4
        end = (y * width + maximum_x) * 4
        values = tuple(float(value) for value in image.pixels[start:end])
        for index in range(0, len(values), 4):
            rgba = values[index : index + 4]
            if rgba[3] > maximum[3]:
                maximum = rgba
    return maximum


def _unsigned_point_id(low: int, high: int) -> int:
    """Blender signed int32 두 개를 원본 uint64 Point3D ID로 복원합니다."""
    return (int(low) & 0xFFFFFFFF) | ((int(high) & 0xFFFFFFFF) << 32)


def _verify_point_samples(point_mesh) -> None:
    """원본 Point3D와 저장된 Mesh Attribute의 세 표본을 수치 비교합니다."""
    from bl_ext.user_default.colmap_tools.tools.projection_paint.core.colmap.text_io import (
        parse_points3d_text,
    )

    source, _identity = parse_points3d_text(POINTS_PATH)
    ordered = tuple(source[point_id] for point_id in sorted(source))
    low = point_mesh.attributes["colmap_point3d_id_low"].data
    high = point_mesh.attributes["colmap_point3d_id_high"].data
    errors = point_mesh.attributes["colmap_reprojection_error"].data
    colors = point_mesh.color_attributes["colmap_color"].data
    for index in (0, len(ordered) // 2, len(ordered) - 1):
        expected = ordered[index]
        actual_id = _unsigned_point_id(low[index].value, high[index].value)
        if actual_id != expected.point3d_id:
            raise RuntimeError("Point3D 64비트 ID 표본이 원본과 다릅니다.")
        coordinates = tuple(float(value) for value in point_mesh.vertices[index].co)
        if max(abs(a - b) for a, b in zip(coordinates, expected.xyz)) > 1.0e-5:
            raise RuntimeError("Point3D 좌표 표본이 원본과 다릅니다.")
        if abs(float(errors[index].value) - expected.error) > 1.0e-5:
            raise RuntimeError("Point3D Error 표본이 원본과 다릅니다.")
        stored = tuple(float(value) for value in colors[index].color[:3])
        expected_color = tuple(value / 255.0 for value in expected.rgb)
        if max(abs(a - b) for a, b in zip(stored, expected_color)) > 0.01:
            raise RuntimeError("Point3D RGB 표본이 원본과 다릅니다.")


def _verify() -> None:
    """재열린 Scene의 실제 Stroke와 관리 Camera 계약을 검증합니다."""

    result = json.loads(RESULT_PATH.read_text(encoding="utf-8"))
    scene = bpy.context.scene
    overlay = bpy.data.images.get(str(result["overlay_name"]))
    if overlay is None or overlay.packed_file is None:
        raise RuntimeError("재열린 Blend에서 Packed Overlay를 찾지 못했습니다.")
    maximum_rgba = _maximum_rgba(overlay, tuple(result["stroke"]["uv"]))
    saved_alpha = float(result["stroke"]["after_rgba"][3])
    minimum_alpha = max(1.0 / 510.0, saved_alpha * 0.9)
    if maximum_rgba[3] < minimum_alpha:
        raise RuntimeError("재열린 Packed Overlay에서 실제 Stroke Pixel을 찾지 못했습니다.")
    if max(maximum_rgba[:3]) <= 0.01:
        raise RuntimeError("재열린 Packed Overlay의 Stencil Stroke RGB가 검은색입니다.")
    cameras = tuple(
        obj
        for obj in scene.objects
        if obj.get("pp_record_kind") == "CAMERA_OBJECT"
    )
    if len(cameras) != 305 or len(scene.timeline_markers) != 305:
        raise RuntimeError("재열린 Camera 또는 Marker 수가 305개가 아닙니다.")
    point_objects = tuple(
        obj
        for obj in scene.objects
        if obj.get("pp_record_kind") == "POINT_MESH"
    )
    if len(point_objects) != 1 or len(point_objects[0].data.vertices) != 58853:
        raise RuntimeError("재열린 Point Mesh가 58,853개 Point를 보존하지 못했습니다.")
    point_mesh = point_objects[0].data
    required_attributes = {
        "colmap_color",
        "colmap_reprojection_error",
        "colmap_source_index",
        "colmap_point3d_id_low",
        "colmap_point3d_id_high",
    }
    if not required_attributes.issubset(
        {attribute.name for attribute in point_mesh.attributes}
    ):
        raise RuntimeError("재열린 Point Mesh의 COLMAP Attribute가 불완전합니다.")
    _verify_point_samples(point_mesh)
    if any(obj.type in {"POINTCLOUD", "VOLUME"} for obj in scene.objects):
        raise RuntimeError("Camera/Image Import가 PointCloud 또는 Volume을 생성했습니다.")
    from bl_ext.user_default.colmap_tools.tools.projection_paint.blender.camera_scene import (
        _repair_timer,
        runtime_for_scene,
        scene_manifest,
    )

    manifest = scene_manifest(scene)
    runtime = runtime_for_scene(scene)
    if manifest is None or runtime is None:
        raise RuntimeError("재열기 뒤 Camera Runtime이 복구되지 않았습니다.")
    entry = manifest.entries[0]
    marker = runtime.markers[entry.image_id]
    marker.frame = entry.frame + 10
    _repair_timer()
    if marker.frame != entry.frame:
        raise RuntimeError("재열기 뒤 이동한 관리 Marker가 복구되지 않았습니다.")
    result.update(
        {
            "reopen_verified": True,
            "reopen_packed": True,
            "reopen_maximum_rgba": [
                round(value, 6) for value in maximum_rgba
            ],
            "reopen_camera_count": len(cameras),
            "reopen_marker_count": len(scene.timeline_markers),
            "reopen_marker_guard": True,
            "reopen_point_count": len(point_mesh.vertices),
            "reopen_point_samples_verified": True,
            "reopen_pointcloud_count": sum(
                obj.type == "POINTCLOUD" for obj in scene.objects
            ),
        }
    )
    RESULT_PATH.write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


_verify()
