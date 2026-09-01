"""Blender 5.2에서 Sessionless Pointer 복구와 UUID 분리를 검증합니다."""

from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest

import bpy


ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from tools import projection_paint as addon  # noqa: E402
from tools.projection_paint.blender.material_setup import (  # noqa: E402
    prepare_active_material,
)
from tools.projection_paint.blender.overlay_durability import (  # noqa: E402
    rebuild_overlay_registry,
)
from tools.projection_paint.blender.ownership import UUID_KEY  # noqa: E402
from tools.projection_paint.blender.paint_session import (  # noqa: E402
    finish_paint_session,
    start_paint_session,
)
from tools.projection_paint.blender.state_handlers import (  # noqa: E402
    undo_pre_handler,
)
from tools.projection_paint.blender.stencil_session import (  # noqa: E402
    bind_active_draw_brush,
    reconnect_stencil_texture,
    stage_stencil_source,
    start_stencil_session,
)


def _activate_and_bind_test_brush(scene, brush):
    """Background 테스트용 Draw에서 생성된 프로젝트 전용 Stencil Brush를 반환합니다."""

    window = bpy.context.window
    area = next(item for item in window.screen.areas if item.type == "VIEW_3D")
    region = next(item for item in area.regions if item.type == "WINDOW")
    brush.asset_mark()
    try:
        with bpy.context.temp_override(window=window, screen=window.screen, area=area, region=region):
            result = bpy.ops.brush.asset_activate(
                asset_library_type="LOCAL",
                relative_asset_identifier=f"Brush/{brush.name}",
            )
        if result != {"FINISHED"}:
            raise AssertionError(f"Local Brush 활성화 실패: {result}")
    finally:
        if brush.asset_data is not None:
            brush.asset_clear()
    return bind_active_draw_brush(bpy.context, scene)


def _new_target(name: str = "SessionlessTarget"):
    """UV Target과 Sessionless Stencil 권위 Camera를 생성합니다."""
    mesh = bpy.data.meshes.new(f"{name}Mesh")
    mesh.from_pydata(
        ((0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (1.0, 1.0, 0.0)),
        (),
        ((0, 1, 2),),
    )
    mesh.uv_layers.new(name="UVMap")
    target = bpy.data.objects.new(name, mesh)
    bpy.context.scene.collection.objects.link(target)
    material = bpy.data.materials.new(f"{name}Material")
    material.use_nodes = True
    target.data.materials.append(material)
    camera_data = bpy.data.cameras.new(f"{name}CameraData")
    camera = bpy.data.objects.new(f"{name}Camera", camera_data)
    bpy.context.scene.collection.objects.link(camera)
    bpy.context.scene.camera = camera
    return target


def _new_brush(name: str):
    """Background 테스트용 Texture Paint Brush를 생성합니다."""
    return bpy.data.brushes.new(name=name, mode="TEXTURE_PAINT")


class SessionlessStateTests(unittest.TestCase):
    """Python Runtime 폐기 뒤 Blender Pointer가 권위를 유지하는지 검증합니다."""

    @classmethod
    def setUpClass(cls):
        """Scene PointerProperty를 포함한 Extension을 테스트 Process에 등록합니다."""
        addon.register()

    @classmethod
    def tearDownClass(cls):
        """테스트 Process의 Extension 등록을 해제합니다."""
        addon.unregister()

    def setUp(self):
        """각 테스트를 빈 Factory Scene에서 시작합니다."""
        bpy.ops.wm.read_factory_settings(use_empty=True)

    def tearDown(self):
        """활성 Paint 상태를 다음 테스트 전에 정리합니다."""
        try:
            finish_paint_session(bpy.context)
        except Exception:
            pass

    def test_undo_pre_preserves_blender_stencil_authority(self):
        """Undo 중단 장벽이 Brush·Texture·Source Pointer를 폐기하지 않는지 확인합니다."""
        target = _new_target()
        start_paint_session(bpy.context, target, width=64, height=32)
        brush = _new_brush("UndoPointerBrush")
        stencil = start_stencil_session(
            bpy.context,
            dataset_uuid="dataset",
        )
        bpy.context.view_layer.objects.active = target
        target.select_set(True)
        bpy.ops.object.mode_set(mode="TEXTURE_PAINT")
        settings = bpy.context.scene.projection_paint
        bound = _activate_and_bind_test_brush(bpy.context.scene, brush)
        source = bpy.data.images.new("UndoPointerSource", 64, 32, alpha=True)
        stage_stencil_source(
            stencil,
            image=source,
            image_id=7,
            frame_corners=((0.0, 0.0), (64.0, 0.0), (64.0, 32.0), (0.0, 32.0)),
        )
        reconnect_stencil_texture(bpy.context)
        texture = settings.stencil_texture
        undo_pre_handler()
        self.assertIs(settings.stencil_texture, texture)
        self.assertIs(settings.stencil_source_image, source)
        self.assertIs(settings.stencil_brush, bound)
        self.assertIs(bound.texture, texture)
        self.assertEqual(reconnect_stencil_texture(bpy.context), 7)

    def test_overlay_uuid_repair_preserves_authoritative_image(self):
        """명시적 Repair가 권위 Overlay를 유지하고 복제본 UUID만 분리합니다."""
        target = _new_target("UUIDTarget")
        setup = prepare_active_material(target, 32, 32, use_base_resolution=False)
        settings = bpy.context.scene.projection_paint
        settings.paint_target = target
        settings.paint_material = setup.material
        settings.overlay_image = setup.overlay
        duplicate = setup.overlay.copy()
        original_uuid = str(setup.overlay[UUID_KEY])
        self.assertEqual(str(duplicate[UUID_KEY]), original_uuid)
        result = bpy.ops.projection_paint.repair_overlay_uuids()
        self.assertEqual(result, {"FINISHED"})
        self.assertEqual(str(setup.overlay[UUID_KEY]), original_uuid)
        self.assertNotEqual(str(duplicate[UUID_KEY]), original_uuid)
        self.assertEqual(len(rebuild_overlay_registry()), 2)

    def test_scene_overlay_pointer_survives_save_and_reload(self):
        """관리 Material과 Overlay Pointer가 .blend 저장·재열기 뒤 유지되는지 확인합니다."""
        target = _new_target("ReloadTarget")
        start_paint_session(bpy.context, target, width=32, height=32)
        finish_paint_session(bpy.context)
        overlay_name = bpy.context.scene.projection_paint.overlay_image.name
        material_name = bpy.context.scene.projection_paint.paint_material.name
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "sessionless_pointer.blend")
            bpy.ops.wm.save_as_mainfile(filepath=path)
            bpy.ops.wm.open_mainfile(filepath=path)
        settings = bpy.context.scene.projection_paint
        self.assertEqual(settings.overlay_image.name, overlay_name)
        self.assertEqual(settings.paint_material.name, material_name)

if __name__ == "__main__":
    unittest.main(argv=[__file__], verbosity=2)
