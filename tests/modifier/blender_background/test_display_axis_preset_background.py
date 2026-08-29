"""Blender 파일 로드에서 Display Axis 프리셋 이전을 검증합니다."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import bpy


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools import modifier as blender_colmap
from tools.modifier.blender.axis_display import display_axis_matrix
from tools.modifier.blender.ownership import (
    DATASET_UUID_KEY,
    OWNER_KEY,
    OWNER_TOKEN,
    RECORD_KIND_KEY,
)


def assert_true(condition, message):
    """조건이 거짓이면 검증 실패를 명확한 메시지로 보고합니다."""

    if not condition:
        raise AssertionError(message)


blender_colmap.register()
try:
    scene = bpy.context.scene
    settings = scene.colmap_import
    legacy_uuid = "legacy-display-axis"

    settings.dataset_uuid = legacy_uuid
    settings.axis_preset = "CUSTOM"
    settings.axis_forward = "Y"
    settings.axis_up = "Z"

    axis_root = bpy.data.objects.new("Legacy Axis Root", None)
    axis_root[OWNER_KEY] = OWNER_TOKEN
    axis_root[RECORD_KIND_KEY] = "AXIS_ROOT"
    axis_root[DATASET_UUID_KEY] = legacy_uuid
    axis_root["colmap_display_forward_axis"] = "Y"
    axis_root["colmap_display_up_axis"] = "Z"
    axis_root.matrix_world = display_axis_matrix("Y", "Z")
    scene.collection.objects.link(axis_root)

    for property_name in ("axis_preset", "axis_forward", "axis_up"):
        if property_name in settings.keys():
            del settings[property_name]

    with tempfile.TemporaryDirectory() as directory:
        blend_path = Path(directory) / "legacy_display_axis.blend"
        bpy.ops.wm.save_as_mainfile(filepath=str(blend_path))
        bpy.ops.wm.open_mainfile(filepath=str(blend_path))

        restored = bpy.context.scene.colmap_import
        assert_true(
            restored.axis_preset == "BLENDER"
            and (restored.axis_forward, restored.axis_up) == ("Y", "Z")
            and "axis_preset" in restored.keys(),
            "구형 `.blend`의 Y/Z 표시축을 Blender 프리셋으로 이전하지 못했습니다.",
        )
finally:
    blender_colmap.unregister()

print("BLENDER_COLMAP_DISPLAY_AXIS_PRESET_BACKGROUND_OK")
