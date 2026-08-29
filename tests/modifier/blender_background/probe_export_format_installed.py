"""Blender 설치본에서 Export Format UI와 TXT→BIN 변환을 검증합니다."""

from __future__ import annotations

import importlib
import os
import sys
import tempfile
from pathlib import Path

import bmesh
import bpy


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

USE_INSTALLED = os.environ.get("BLENDER_COLMAP_USE_INSTALLED") == "1"
PACKAGE_NAME = (
    "bl_ext.user_default.colmap_tools.tools.modifier"
    if USE_INSTALLED
    else "tools.modifier"
)
blender_colmap = importlib.import_module(PACKAGE_NAME)
binary_io = importlib.import_module(f"{PACKAGE_NAME}.core.binary_io")
ownership = importlib.import_module(f"{PACKAGE_NAME}.blender.ownership")


def assert_true(condition, message):
    if not condition:
        raise AssertionError(message)


def make_dataset(root: Path) -> Path:
    model = root / "sparse" / "0"
    images = root / "images"
    model.mkdir(parents=True)
    images.mkdir()
    (images / "frame.jpg").write_bytes(b"fixture")
    (model / "cameras.txt").write_text(
        "# cameras\n1 PINHOLE 64 64 50 50 32 32\n",
        encoding="utf-8",
    )
    (model / "images.txt").write_text(
        "# images\n"
        "7 1 0 0 0 0 0 0 1 frame.jpg\n"
        "32 32 100\n",
        encoding="utf-8",
    )
    (model / "points3D.txt").write_text(
        "# points\n100 0 0 2 255 96 64 0.1 7 0\n",
        encoding="utf-8",
    )
    return model


def remove_point(settings) -> None:
    assert_true(
        bpy.ops.colmap.toggle_edit_point_mode() == {"FINISHED"},
        settings.point_selection_status,
    )
    point_object = next(
        obj
        for obj in bpy.data.objects
        if obj.get(ownership.OWNER_KEY) == ownership.OWNER_TOKEN
        and obj.get(ownership.RECORD_KIND_KEY) == "POINT_MESH"
        and obj.get(ownership.DATASET_UUID_KEY) == settings.dataset_uuid
        and obj.users_collection
    )
    mesh = bmesh.from_edit_mesh(point_object.data)
    mesh.verts.ensure_lookup_table()
    for vertex in mesh.verts:
        vertex.select = False
    mesh.verts[0].select = True
    mesh.select_history.clear()
    mesh.select_history.add(mesh.verts[0])
    bmesh.update_edit_mesh(point_object.data)
    assert_true(
        bpy.ops.colmap.remove() == {"FINISHED"},
        settings.point_selection_status,
    )


if USE_INSTALLED:
    assert_true(
        hasattr(bpy.types.Scene, "colmap_import"),
        "설치된 Extension이 등록되지 않았습니다.",
    )
    assert_true(
        blender_colmap.__version__ == "0.17.4",
        "설치본 버전이 0.17.4가 아닙니다.",
    )
else:
    blender_colmap.register()

try:
    settings = bpy.context.scene.colmap_import
    assert_true(
        bpy.types.Operator.bl_rna_get_subclass_py(
            "COLMAP_OT_open_modifier_workspace"
        )
        is not None
        and bpy.types.Operator.bl_rna_get_subclass_py(
            "COLMAP_OT_exit_modifier_workspace"
        )
        is not None
        and hasattr(bpy.types, "COLMAP_PT_modifier")
        and bpy.types.COLMAP_PT_modifier.bl_category == "COLMAP Modifier",
        "설치본의 COLMAP Modifier Workspace 진입 UI가 등록되지 않았습니다.",
    )
    assert_true(
        not hasattr(bpy.types, "COLMAP_PT_projection_paint")
        and bpy.types.Operator.bl_rna_get_subclass_py(
            "COLMAP_OT_toggle_projection_paint"
        )
        is None
        and "projection_paint_target" not in settings.bl_rna.properties
        and "projection_layers" not in settings.bl_rna.properties,
        "Projection Paint UI, 연산자 또는 속성이 설치본에 남아 있습니다.",
    )
    assert_true(
        settings.axis_preset == "REALITY_CAPTURE"
        and (settings.axis_forward, settings.axis_up) == ("Z", "-Y"),
        "설치본 Display Axis 기본값이 RC Z/-Y가 아닙니다.",
    )
    assert_true(
        "camera_frustum_size" in settings.bl_rna.properties
        and abs(settings.camera_frustum_size - 1.0) < 1.0e-8,
        "설치본 Camera Frustum Size 속성 또는 기본값이 올바르지 않습니다.",
    )
    enum_property = (
        settings.bl_rna.properties["export_output_format"]
    )
    assert_true(
        tuple(item.identifier for item in enum_property.enum_items)
        == ("SOURCE", "TEXT", "BINARY"),
        "Export Format Enum 항목이 올바르지 않습니다.",
    )
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        model = make_dataset(root)
        settings.dataset_root = str(root)
        settings.input_format = "TEXT"
        settings.camera_frustum_size = 0.42
        assert_true(
            bpy.ops.colmap.import_model() == {"FINISHED"},
            settings.status,
        )
        managed_camera = next(
            obj
            for obj in bpy.data.objects
            if obj.get(ownership.OWNER_KEY) == ownership.OWNER_TOKEN
            and obj.get(ownership.RECORD_KIND_KEY) == "CAMERA"
            and obj.get(ownership.DATASET_UUID_KEY) == settings.dataset_uuid
        )
        assert_true(
            abs(managed_camera.data.display_size - 0.42) < 1.0e-6,
            "설치본 Import가 Camera Frustum Size를 적용하지 않았습니다.",
        )
        settings.camera_frustum_size = 0.63
        assert_true(
            abs(managed_camera.data.display_size - 0.63) < 1.0e-6,
            "설치본 Camera Frustum Size가 실시간 반영되지 않았습니다.",
        )
        assert_true(
            bpy.ops.colmap.review_filter_current() == {"FINISHED"}
            and settings.review_pending_camera_count == 1
            and settings.edit_revision == 0,
            "설치본 Filter Current Camera가 Pending 하나를 만들지 못했습니다.",
        )
        assert_true(
            bpy.ops.colmap.review_undo_last_filter() == {"FINISHED"}
            and settings.review_pending_camera_count == 0
            and settings.review_pending_filter_json == "{}"
            and settings.review_pending_filter_order_json == "[]"
            and settings.review_current_image_id == "7"
            and not managed_camera.hide_get()
            and settings.edit_revision == 0,
            "설치본 Undo Last Filter가 Pending Camera를 복원하지 못했습니다.",
        )
        remove_point(settings)
        assert_true(
            abs(managed_camera.data.display_size - 0.63) < 1.0e-6
            and abs(settings.camera_frustum_size - 0.63) < 1.0e-6,
            "설치본 Remove가 Camera Frustum Size를 기본값으로 되돌렸습니다.",
        )
        settings.export_backup_enabled = False
        settings.export_output_format = "BINARY"
        assert_true(
            bpy.ops.colmap.export() == {"FINISHED"},
            settings.export_status,
        )
        exported = binary_io.read_model_binary(model, modern=False)
        assert_true(
            not exported.points3d
            and settings.active_input_format == "binary"
            and "BIN" in settings.export_status,
            "설치본 TXT→BIN Export 결과가 올바르지 않습니다.",
        )
        assert_true(
            not list(model.glob("*.txt"))
            and tuple(
                path.name for path in sorted(model.glob("*.bin"))
            )
            == ("cameras.bin", "images.bin", "points3D.bin"),
            "설치본 Export 파일 집합이 BIN 3개로 정규화되지 않았습니다.",
        )
finally:
    if not USE_INSTALLED:
        blender_colmap.unregister()

print("BLENDER_COLMAP_EXPORT_FORMAT_INSTALLED_OK")
