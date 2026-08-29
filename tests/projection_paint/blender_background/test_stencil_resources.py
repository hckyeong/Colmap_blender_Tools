"""Blender 5.2에서 Stencil 정렬과 Source Commit Token을 검증합니다."""

from __future__ import annotations

from pathlib import Path
import sys
from types import SimpleNamespace
import unittest

import bpy


ROOT = Path(__file__).resolve().parents[3] / "tools" / "projection_paint"
sys.path.insert(0, str(ROOT.parent))

from tools.projection_paint.blender.frame_source import (  # noqa: E402
    FrameSourceCache,
)
from tools.projection_paint.blender.paint_session import (  # noqa: E402
    active_paint_session,
    finish_paint_session,
    start_paint_session,
)
from tools.projection_paint.blender.paint_runtime import (  # noqa: E402
    _frame_source_is_current,
)
from tools.projection_paint.blender.stencil_session import (  # noqa: E402
    _switch_active_brush,
    active_stencil_session,
    camera_frame_stencil,
    disconnect_stencil_for_camera_exit,
    finish_stencil_session,
    reconnect_stencil_texture,
    refresh_stencil,
    stage_stencil_source,
    start_stencil_session,
)
from tools import projection_paint as addon  # noqa: E402


def _new_target():
    """UV와 한 면을 가진 Paint Target을 생성합니다."""
    mesh = bpy.data.meshes.new("StencilTargetMesh")
    mesh.from_pydata(
        ((0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (1.0, 1.0, 0.0)),
        (),
        ((0, 1, 2),),
    )
    mesh.uv_layers.new(name="UVMap")
    target = bpy.data.objects.new("StencilTarget", mesh)
    bpy.context.scene.collection.objects.link(target)
    material = bpy.data.materials.new("StencilMaterial")
    material.use_nodes = True
    target.data.materials.append(material)
    return target


def _new_texture_brush(name: str):
    """Background 테스트용 기본 Texture Paint Brush를 생성합니다."""

    return bpy.data.brushes.new(name=name, mode="TEXTURE_PAINT")


class StencilResourceTests(unittest.TestCase):
    """Stencil Frame Fit과 늦은 Source Commit 차단을 검증합니다."""

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
        """테스트가 만든 Stencil과 Paint Session을 역순 정리합니다."""
        if active_stencil_session() is not None:
            finish_stencil_session(bpy.context)
        if active_paint_session() is not None:
            try:
                finish_paint_session(bpy.context)
            except Exception:
                pass

    def test_camera_frame_fit_returns_center_and_half_dimensions(self):
        """2대1 Camera Frame을 같은 종횡비 Source에 맞춥니다."""
        position, dimensions = camera_frame_stencil(
            ((10.0, 20.0), (210.0, 20.0), (210.0, 120.0), (10.0, 120.0)),
            200,
            100,
        )
        self.assertEqual(position, (110.0, 70.0))
        self.assertEqual(dimensions, (100.0, 50.0))

    def test_frame_transition_waits_until_stencil_source_matches(self):
        """Frame IMAGE_ID가 바뀐 전환 구간을 Stencil 추적에서 구분합니다."""

        runtime = SimpleNamespace(
            scene=SimpleNamespace(frame_current=2),
            entries={
                1: SimpleNamespace(image_id=10),
                2: SimpleNamespace(image_id=20),
            },
            stencil=SimpleNamespace(source_image_id=10),
        )
        self.assertFalse(_frame_source_is_current(runtime))
        runtime.stencil.source_image_id = 20
        self.assertTrue(_frame_source_is_current(runtime))

    def test_stencil_reconnect_restores_mapping_and_current_image(self):
        """Reconnect가 현재 Source와 STENCIL Mapping을 다시 연결합니다."""
        target = _new_target()
        paint = start_paint_session(bpy.context, target, width=200, height=100)
        brush = _new_texture_brush("ReconnectBrush")
        stencil = start_stencil_session(
            bpy.context,
            dataset_uuid="dataset",
            brush_override=brush,
        )
        source = bpy.data.images.new("Source", 200, 100, alpha=True)
        stage_stencil_source(
            stencil,
            image=source,
            image_id=17,
            frame_corners=(
                (10.0, 20.0),
                (210.0, 20.0),
                (210.0, 120.0),
                (10.0, 120.0),
            ),
        )
        stencil.brush.texture_slot.map_mode = "VIEW_PLANE"
        result = reconnect_stencil_texture(bpy.context, stencil)
        self.assertEqual(result, 17)
        self.assertEqual(stencil.brush.texture_slot.map_mode, "STENCIL")
        self.assertIs(stencil.managed_texture.image, source)
        self.assertIs(stencil.brush.texture, stencil.managed_texture)
        self.assertEqual(tuple(stencil.brush.stencil_pos), (110.0, 70.0))
        self.assertEqual(tuple(stencil.brush.stencil_dimension), (100.0, 50.0))

    def test_manual_mapping_change_is_not_automatically_reconnected(self):
        """수동 Mapping 해제는 Refresh가 복구하지 않고 Reconnect만 복구합니다."""

        target = _new_target()
        paint = start_paint_session(bpy.context, target, width=200, height=100)
        brush = _new_texture_brush("ManualBrush")
        stencil = start_stencil_session(
            bpy.context,
            dataset_uuid="dataset",
            brush_override=brush,
        )
        source = bpy.data.images.new("ManualSource", 200, 100, alpha=True)
        stage_stencil_source(
            stencil,
            image=source,
            image_id=21,
            frame_corners=(
                (10.0, 20.0),
                (210.0, 20.0),
                (210.0, 120.0),
                (10.0, 120.0),
            ),
        )
        reconnect_stencil_texture(bpy.context, stencil)
        stencil.brush.texture_slot.map_mode = "VIEW_PLANE"
        self.assertFalse(refresh_stencil(bpy.context, stencil))
        self.assertTrue(stencil.manual_disabled)
        self.assertEqual(stencil.brush.texture_slot.map_mode, "VIEW_PLANE")

    def test_camera_exit_disconnects_and_return_reconnects_stencil(self):
        """Camera View 이탈 연결 해제와 복귀 재연결 상태를 검증합니다."""

        target = _new_target()
        paint = start_paint_session(bpy.context, target, width=200, height=100)
        brush = _new_texture_brush("CameraExitBrush")
        stencil = start_stencil_session(
            bpy.context,
            dataset_uuid="dataset",
            brush_override=brush,
        )
        source = bpy.data.images.new("CameraExitSource", 200, 100, alpha=True)
        stage_stencil_source(
            stencil,
            image=source,
            image_id=31,
            frame_corners=(
                (10.0, 20.0),
                (210.0, 20.0),
                (210.0, 120.0),
                (10.0, 120.0),
            ),
        )
        reconnect_stencil_texture(bpy.context, stencil)
        self.assertTrue(disconnect_stencil_for_camera_exit(stencil))
        self.assertIsNone(stencil.brush.texture)
        self.assertIs(stencil.managed_texture.image, source)
        self.assertTrue(stencil.outside_camera_view)
        self.assertTrue(refresh_stencil(bpy.context, stencil))
        self.assertIs(stencil.brush.texture, stencil.managed_texture)
        self.assertFalse(stencil.outside_camera_view)

    def test_repeated_brush_switches_keep_following_active_brush(self):
        """연속 Brush 교체마다 같은 관리 Stencil Texture를 새 Brush가 승계합니다."""
        target = _new_target()
        start_paint_session(bpy.context, target, width=200, height=100)
        scene = bpy.context.scene
        first = _new_texture_brush("FirstFollowBrush")
        start_stencil_session(
            bpy.context,
            dataset_uuid="dataset",
            brush_override=first,
        )
        scene.projection_paint.stencil_follow_active_brush = True
        second = _new_texture_brush("SecondFollowBrush")
        _switch_active_brush(bpy.context, scene, second)
        self.assertIs(scene.projection_paint.stencil_brush, second)
        self.assertTrue(scene.projection_paint.stencil_follow_active_brush)
        third = _new_texture_brush("ThirdFollowBrush")
        _switch_active_brush(bpy.context, scene, third)
        self.assertIs(scene.projection_paint.stencil_brush, third)
        self.assertTrue(scene.projection_paint.stencil_follow_active_brush)

    def test_stencil_session_temporarily_disables_unified_color(self):
        """Stencil Session 동안 Unified Color를 끄고 종료 시 복구합니다."""

        target = _new_target()
        paint = start_paint_session(bpy.context, target, width=200, height=100)
        unified = bpy.context.scene.tool_settings.image_paint.unified_paint_settings
        unified.use_unified_color = True
        brush = _new_texture_brush("UnifiedColorBrush")
        original_color = tuple(brush.color)
        stencil = start_stencil_session(
            bpy.context,
            dataset_uuid="dataset",
            brush_override=brush,
        )
        self.assertFalse(unified.use_unified_color)
        self.assertIs(stencil.brush, brush)
        self.assertEqual(tuple(stencil.brush.color), (1.0, 1.0, 1.0))
        finish_stencil_session(bpy.context)
        self.assertTrue(unified.use_unified_color)
        self.assertEqual(tuple(brush.color), original_color)

    def test_session_uses_user_brush_directly_and_restores_texture_state(self):
        """Brush를 복제하지 않고 기존 Texture 상태를 종료 시 복구합니다."""

        target = _new_target()
        paint = start_paint_session(bpy.context, target, width=200, height=100)
        brush = _new_texture_brush("ExistingUserBrush")
        original_texture = bpy.data.textures.new("ExistingBrushTexture", type="IMAGE")
        brush.texture = original_texture
        brush.texture_slot.map_mode = "VIEW_PLANE"
        brush.color = (0.25, 0.5, 0.75)
        brush.stencil_pos = (31.0, 47.0)
        brush.stencil_dimension = (53.0, 29.0)
        original_brush_pointer = int(brush.as_pointer())
        original_brush_count = len(bpy.data.brushes)
        stencil = start_stencil_session(
            bpy.context,
            dataset_uuid="dataset",
            brush_override=brush,
        )
        source = bpy.data.images.new("DirectBrushSource", 200, 100, alpha=True)
        stage_stencil_source(
            stencil,
            image=source,
            image_id=41,
            frame_corners=(
                (10.0, 20.0),
                (210.0, 20.0),
                (210.0, 120.0),
                (10.0, 120.0),
            ),
        )
        reconnect_stencil_texture(bpy.context, stencil)
        self.assertIs(stencil.brush, brush)
        self.assertEqual(int(stencil.brush.as_pointer()), original_brush_pointer)
        self.assertEqual(len(bpy.data.brushes), original_brush_count)
        self.assertIs(brush.texture, stencil.managed_texture)
        managed_texture_name = stencil.managed_texture.name
        finish_stencil_session(bpy.context)
        self.assertIs(brush.texture, original_texture)
        self.assertEqual(brush.texture_slot.map_mode, "VIEW_PLANE")
        self.assertEqual(tuple(brush.color), (0.25, 0.5, 0.75))
        self.assertEqual(tuple(brush.stencil_pos), (31.0, 47.0))
        self.assertEqual(tuple(brush.stencil_dimension), (53.0, 29.0))
        self.assertIsNone(bpy.data.textures.get(managed_texture_name))

    def test_stale_source_request_never_commits(self):
        """느린 이전 Frame 요청이 최신 Stencil Source를 덮지 못합니다."""
        scene = bpy.context.scene
        cache = FrameSourceCache(
            scene=scene,
            session_epoch=1,
            dataset_revision=3,
            max_images=3,
        )
        image_a = bpy.data.images.new("SourceA", 8, 8)
        image_b = bpy.data.images.new("SourceB", 8, 8)
        request_a = cache.begin_request(frame=1, image_id=10)
        cache.add_borrowed(10, image_a)
        request_b = cache.begin_request(frame=2, image_id=20)
        cache.add_borrowed(20, image_b)
        committed = []
        self.assertFalse(
            cache.commit(
                request_a,
                image_a,
                current_frame=2,
                apply=lambda image, image_id: committed.append((image, image_id)),
            )
        )
        self.assertTrue(
            cache.commit(
                request_b,
                image_b,
                current_frame=2,
                apply=lambda image, image_id: committed.append((image, image_id)),
            )
        )
        self.assertEqual(committed, [(image_b, 20)])
        cache.close()

    def test_borrowed_lru_eviction_never_removes_user_images(self):
        """LRU 퇴거가 차용한 사용자 Image 데이터 블록을 제거하지 않습니다."""
        cache = FrameSourceCache(
            scene=bpy.context.scene,
            session_epoch=1,
            dataset_revision=1,
            max_images=3,
        )
        images = [bpy.data.images.new(f"Borrowed{index}", 4, 4) for index in range(4)]
        for index, image in enumerate(images):
            cache.add_borrowed(index, image)
        self.assertEqual(len(cache.records()), 3)
        self.assertTrue(all(bpy.data.images.get(image.name) is image for image in images))
        cache.close()
        self.assertTrue(all(bpy.data.images.get(image.name) is image for image in images))


if __name__ == "__main__":
    unittest.main(argv=[__file__])
