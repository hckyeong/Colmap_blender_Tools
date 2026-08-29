"""Flying Squirrel 실제 데이터로 Camera Review 프레임 연결을 검증합니다."""

from __future__ import annotations

import json
from pathlib import Path
import sys
from types import SimpleNamespace

import bpy


PROJECT_PARENT = Path(__file__).resolve().parents[2]
if str(PROJECT_PARENT) not in sys.path:
    sys.path.insert(0, str(PROJECT_PARENT))

from tools import projection_paint as addon
from tools.projection_paint.blender.camera_review import (
    look_through_current_camera,
)
from tools.projection_paint.blender.camera_scene import _repair_timer


DATASET_ROOT = Path(
    r"E:\DEV\DEV_RND\GSSplat\00_photogrammetry\6_Flying_squirrel"
    r"\rc\6_Flying_squirrel_workspace\rc_colmap"
)


def _review_snapshot(scene: bpy.types.Scene) -> dict[str, object]:
    """현재 Camera Review 이미지와 배경 슬롯 상태를 사전으로 반환합니다."""

    settings = scene.projection_paint
    camera_data = settings.review_background_camera_data
    slot = camera_data.background_images[settings.review_background_slot_index]
    return {
        "frame": int(scene.frame_current),
        "image_id": int(settings.review_background_image_id),
        "image_name": str(slot.image.name),
        "image_size": tuple(int(value) for value in slot.image.size),
        "camera_data_pointer": int(camera_data.as_pointer()),
        "slot_count": len(camera_data.background_images),
        "slot_pointer": int(slot.as_pointer()),
        "alpha": float(slot.alpha),
        "enabled": bool(slot.show_background_image),
    }


def main() -> None:
    """실제 Dataset Import 뒤 두 Frame의 배경 이미지 교체를 검증합니다."""

    bpy.ops.wm.read_factory_settings(use_empty=True)
    addon.register()
    scene = bpy.context.scene
    settings = scene.projection_paint
    settings.dataset_root = str(DATASET_ROOT)
    settings.input_format = "TEXT"
    result = bpy.ops.projection_paint.import_colmap("EXEC_DEFAULT")
    if result != {"FINISHED"}:
        raise RuntimeError(f"실제 Dataset Import가 실패했습니다: {result}")

    first = _review_snapshot(scene)
    import_resolution = (
        int(scene.render.resolution_x),
        int(scene.render.resolution_y),
    )
    if import_resolution != (9504, 6336):
        raise RuntimeError("Import 직후 Camera 원본 해상도가 적용되지 않았습니다.")
    if abs(float(first["alpha"]) - 0.5) > 1.0e-6:
        raise RuntimeError("Import 직후 Camera Background Alpha 기본값이 0.5가 아닙니다.")
    first_camera_data = settings.review_background_camera_data
    rv3d = SimpleNamespace(view_perspective="PERSP")
    space = SimpleNamespace(type="VIEW_3D", region_3d=rv3d, camera=None)
    redraws = []
    look_context = SimpleNamespace(
        scene=scene,
        area=SimpleNamespace(
            type="VIEW_3D",
            tag_redraw=lambda: redraws.append(True),
        ),
        space_data=space,
        window_manager=None,
    )
    look_image_id = look_through_current_camera(look_context)
    if (
        look_image_id != first["image_id"]
        or space.camera is not scene.camera
        or rv3d.view_perspective != "CAMERA"
        or not redraws
        or (scene.render.resolution_x, scene.render.resolution_y) != (9504, 6336)
    ):
        raise RuntimeError("Look Through Camera가 현재 Frame Camera를 표시하지 못했습니다.")
    settings.review_background_alpha = 0.35
    settings.review_background_enabled = False
    hidden = _review_snapshot(scene)
    settings.review_background_enabled = True
    scene.frame_set(scene.frame_current + 1)
    _repair_timer()
    second = _review_snapshot(scene)

    if first["image_id"] == second["image_id"]:
        raise RuntimeError("Frame 변경 뒤 Camera Review IMAGE_ID가 바뀌지 않았습니다.")
    if (
        first["camera_data_pointer"] == second["camera_data_pointer"]
        and first["slot_pointer"] != second["slot_pointer"]
    ):
        raise RuntimeError("공유 Camera Data에서 Review 슬롯이 재사용되지 않았습니다.")
    if (
        first["camera_data_pointer"] != second["camera_data_pointer"]
        and len(first_camera_data.background_images) != 0
    ):
        raise RuntimeError("이전 Camera Data의 Review 슬롯이 정리되지 않았습니다.")
    if hidden["enabled"]:
        raise RuntimeError("Camera Review 배경 토글이 슬롯을 숨기지 못했습니다.")
    if abs(float(second["alpha"]) - 0.35) > 1.0e-6:
        raise RuntimeError("Camera Review Alpha가 Frame 변경 뒤 유지되지 않았습니다.")

    print(
        "PP_CAMERA_REVIEW_ACTUAL "
        + json.dumps(
            {
                "status": str(settings.status),
                "import_resolution": import_resolution,
                "look_through_image_id": look_image_id,
                "first": first,
                "hidden": hidden,
                "second": second,
            },
            ensure_ascii=False,
        )
    )
    addon.unregister()


if __name__ == "__main__":
    main()
