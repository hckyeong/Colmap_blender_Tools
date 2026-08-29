"""Flying squirrel Camera와 모델을 임포트한 사용자 테스트 Blend를 생성합니다."""

from __future__ import annotations

from pathlib import Path

import bpy


DATA_ROOT = Path(
    r"E:\DEV\DEV_RND\GSSplat\00_photogrammetry"
    r"\6_Flying_squirrel\rc\6_Flying_squirrel_workspace"
)
COLMAP_ROOT = DATA_ROOT / "final"
OBJ_PATH = DATA_ROOT / "obj_LOD0_LOD0.obj"
OUTPUT_PATH = Path(
    r"C:\Users\gksru\Desktop\Flying_Squirrel_ProjectionPainting_Test.blend"
)
ADDON_MODULE = "bl_ext.user_default.colmap_tools"


def _ensure_addon() -> None:
    """설치된 Projection Painting 애드온을 현재 Process에서 활성화합니다."""

    if hasattr(bpy.types.Scene, "projection_paint"):
        return
    result = bpy.ops.preferences.addon_enable(module=ADDON_MODULE)
    if result != {"FINISHED"}:
        raise RuntimeError(f"Projection Painting 활성화에 실패했습니다: {result}")


def _clear_scene() -> None:
    """현재 Scene의 기존 Object와 Timeline Marker를 제거합니다."""

    if bpy.context.mode != "OBJECT":
        bpy.ops.object.mode_set(mode="OBJECT")
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)
    for marker in tuple(bpy.context.scene.timeline_markers):
        bpy.context.scene.timeline_markers.remove(marker)


def _create_test_blend() -> None:
    """COLMAP Camera와 RC 모델을 임포트해 Paint 직전 상태로 저장합니다."""

    _ensure_addon()
    _clear_scene()
    scene = bpy.context.scene
    settings = scene.projection_paint
    settings.dataset_root = str(COLMAP_ROOT)
    settings.input_format = "TEXT"
    settings.frame_start = 1
    settings.camera_axis_preset = "REALITY_CAPTURE"
    result = bpy.ops.projection_paint.import_colmap()
    if result != {"FINISHED"}:
        raise RuntimeError(f"COLMAP Import가 실패했습니다: {result}, {settings.status}")
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
    settings.status = "임포트 완료: Enter Paint Mode를 눌러 테스트하십시오."
    settings.status_kind = "SUCCESS"
    settings.paint_state = "IDLE"
    scene.frame_set(1)
    scene["pp_desktop_test_ready"] = True
    scene["pp_desktop_test_camera_count"] = 305
    scene["pp_desktop_test_marker_count"] = 305
    scene["pp_desktop_test_point_count"] = 58853
    scene["pp_desktop_test_target"] = target.name
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    result = bpy.ops.wm.save_as_mainfile(filepath=str(OUTPUT_PATH))
    if result != {"FINISHED"}:
        raise RuntimeError(f"바탕화면 Blend 저장이 실패했습니다: {result}")
    print(f"PROJECTION_PAINT_DESKTOP_BLEND_OK:{OUTPUT_PATH}")


_create_test_blend()
