"""Blender Background에서 Mask 활성 중 Stencil 연결 수명주기를 검증합니다."""

from __future__ import annotations

from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest import mock

import bpy


ROOT = Path(__file__).resolve().parents[3] / "tools" / "projection_paint"
sys.path.insert(0, str(ROOT.parent))

from tools import projection_paint as addon  # noqa: E402
from tools.projection_paint.blender import (  # noqa: E402
    overlay_migration,
    paint_runtime,
)
from tools.projection_paint.blender.paint_session import (  # noqa: E402
    active_paint_session,
    finish_paint_session,
    start_paint_session,
)
from tools.projection_paint.blender.stencil_session import (  # noqa: E402
    active_stencil_session,
    finish_stencil_session,
    reconnect_stencil_texture,
    stage_stencil_source,
    start_stencil_session,
)


def _new_target():
    """Mask Stencil 수명주기 테스트용 UV Paint Target을 생성합니다."""
    mesh = bpy.data.meshes.new("MaskStencilRuntimeTargetMesh")
    mesh.from_pydata(
        ((0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (1.0, 1.0, 0.0)),
        (),
        ((0, 1, 2),),
    )
    mesh.uv_layers.new(name="UVMap")
    target = bpy.data.objects.new("MaskStencilRuntimeTarget", mesh)
    bpy.context.scene.collection.objects.link(target)
    material = bpy.data.materials.new("MaskStencilRuntimeMaterial")
    material.use_nodes = True
    target.data.materials.append(material)
    return target


def _prepare_mask_stencil_runtime():
    """연결된 Stencil과 활성 Mask Image를 함께 가진 Runtime 대역을 준비합니다."""
    target = _new_target()
    start_paint_session(bpy.context, target, width=64, height=32)
    scene = bpy.context.scene
    settings = scene.projection_paint
    brush = bpy.data.brushes.new("Mask Stencil Runtime Brush", mode="TEXTURE_PAINT")
    stencil = start_stencil_session(
        bpy.context,
        dataset_uuid="mask-stencil-runtime",
        brush_override=brush,
    )
    source = bpy.data.images.new("MaskStencilRuntimeSource", 64, 32, alpha=True)
    image_id = 907
    stage_stencil_source(
        stencil,
        image=source,
        image_id=image_id,
        frame_corners=(
            (10.0, 20.0),
            (210.0, 20.0),
            (210.0, 120.0),
            (10.0, 120.0),
        ),
    )
    reconnect_stencil_texture(bpy.context, stencil)
    mask_image = bpy.data.images.new("MaskStencilRuntimeMask", 64, 32, alpha=True)
    settings.active_paint_image = mask_image
    settings.paint_mask_mode = True
    runtime = SimpleNamespace(
        scene=scene,
        entries={
            int(scene.frame_current): SimpleNamespace(image_id=image_id),
        },
        image_paths={},
        projection_workspace_active=True,
        viewport_retry_count=0,
        viewport_token=(True, "camera-view"),
    )
    return scene, settings, stencil, mask_image, runtime


class PaintRuntimeMaskStencilTests(unittest.TestCase):
    """Mask가 활성이어도 Camera View만 Stencil 연결을 제어하는지 검증합니다."""

    @classmethod
    def setUpClass(cls):
        """테스트 Process에서 Extension PropertyGroup을 등록합니다."""
        addon.register()

    @classmethod
    def tearDownClass(cls):
        """테스트 Process의 Extension 등록을 해제합니다."""
        addon.unregister()

    def setUp(self):
        """각 테스트를 독립적인 Factory Scene에서 시작합니다."""
        bpy.ops.wm.read_factory_settings(use_empty=True)

    def tearDown(self):
        """활성 Paint와 Stencil 자원을 다음 테스트 전에 정리합니다."""
        if active_stencil_session() is not None:
            finish_stencil_session(bpy.context)
        if active_paint_session() is not None:
            finish_paint_session(bpy.context)

    def test_mask_camera_exit_and_return_control_stencil_connection(self):
        """Mask 활성 Camera View 이탈·복귀가 Brush 연결과 상태만 전환합니다."""
        scene, settings, stencil, mask_image, runtime = _prepare_mask_stencil_runtime()
        scene_key = "mask-camera-view"
        with (
            mock.patch.dict(paint_runtime._RUNTIMES, {scene_key: runtime}, clear=True),
            mock.patch.object(
                paint_runtime,
                "_projection_runtime_is_active",
                return_value=True,
            ),
            mock.patch.object(
                paint_runtime,
                "stencil_view_token",
                return_value=(False, "perspective-view"),
            ),
        ):
            paint_runtime._camera_view_changed(scene_key)

        self.assertIsNone(stencil.brush.texture)
        self.assertTrue(settings.stencil_outside_camera_view)
        self.assertEqual(settings.paint_state, "FROZEN")
        self.assertIs(settings.active_paint_image, mask_image)
        self.assertTrue(settings.paint_mask_mode)

        with (
            mock.patch.dict(paint_runtime._RUNTIMES, {scene_key: runtime}, clear=True),
            mock.patch.object(
                paint_runtime,
                "_projection_runtime_is_active",
                return_value=True,
            ),
            mock.patch.object(
                paint_runtime,
                "stencil_view_token",
                return_value=(True, "camera-view"),
            ),
        ):
            paint_runtime._camera_view_changed(scene_key)

        self.assertIs(stencil.brush.texture, stencil.managed_texture)
        self.assertEqual(stencil.brush.texture_slot.map_mode, "STENCIL")
        self.assertFalse(settings.stencil_outside_camera_view)
        self.assertEqual(settings.paint_state, "ACTIVE")
        self.assertIs(settings.active_paint_image, mask_image)
        self.assertTrue(settings.paint_mask_mode)

    def test_mask_tracking_timer_disconnects_stencil_outside_camera_view(self):
        """Mask 활성 Timer 추적도 Perspective 전환에서 Stencil을 해제합니다."""
        scene, settings, stencil, mask_image, runtime = _prepare_mask_stencil_runtime()
        with (
            mock.patch.object(
                paint_runtime,
                "_projection_runtime_is_active",
                return_value=True,
            ),
            mock.patch.object(
                paint_runtime,
                "stencil_view_token",
                return_value=(False, "perspective-view"),
            ),
        ):
            delay = paint_runtime._track_runtime(runtime)

        self.assertEqual(delay, paint_runtime.OUTSIDE_CAMERA_VIEW_INTERVAL)
        self.assertIsNone(stencil.brush.texture)
        self.assertTrue(settings.stencil_outside_camera_view)
        self.assertEqual(settings.paint_state, "FROZEN")
        self.assertIs(settings.active_paint_image, mask_image)
        self.assertTrue(settings.paint_mask_mode)

    def test_start_tracking_subscribes_when_mask_is_active(self):
        """Mask 활성 Runtime도 기존 Message Bus와 단일 Timer를 구독합니다."""
        scene, settings, _stencil, mask_image, runtime = _prepare_mask_stencil_runtime()
        registry = SimpleNamespace(register_timer=mock.Mock())
        with (
            mock.patch.object(
                paint_runtime,
                "ensure_paint_runtime",
                return_value=runtime,
            ),
            mock.patch.object(
                paint_runtime,
                "_projection_runtime_is_active",
                return_value=True,
            ),
            mock.patch.object(
                paint_runtime,
                "stencil_view_token",
                return_value=(True, "camera-view"),
            ),
            mock.patch.object(paint_runtime, "_subscribe_camera_view") as subscribe,
            mock.patch(
                "tools.projection_paint.blender.lifecycle."
                "lifecycle_registry",
                return_value=registry,
            ),
        ):
            paint_runtime.start_viewport_tracking(scene)

        subscribe.assert_called_once_with(runtime)
        registry.register_timer.assert_called_once()
        self.assertTrue(runtime.projection_workspace_active)
        self.assertIs(settings.active_paint_image, mask_image)
        self.assertTrue(settings.paint_mask_mode)

    def test_mask_workspace_return_reconnects_with_existing_tracking(self):
        """Mask 활성 작업공간 복귀도 기존 Stencil 재연결과 Timer를 사용합니다."""
        scene, settings, _stencil, mask_image, runtime = _prepare_mask_stencil_runtime()
        runtime.projection_workspace_active = False
        scene_key = "mask-workspace-return"
        registry = SimpleNamespace(register_timer=mock.Mock())
        with (
            mock.patch.dict(paint_runtime._RUNTIMES, {scene_key: runtime}, clear=True),
            mock.patch.object(
                paint_runtime,
                "_projection_runtime_is_active",
                return_value=True,
            ),
            mock.patch.object(paint_runtime, "reconnect_stencil_texture") as reconnect,
            mock.patch.object(
                paint_runtime,
                "stencil_view_token",
                return_value=(True, "camera-view"),
            ),
            mock.patch(
                "tools.projection_paint.blender.lifecycle."
                "lifecycle_registry",
                return_value=registry,
            ),
        ):
            paint_runtime._paint_context_changed(scene_key)

        reconnect.assert_called_once()
        registry.register_timer.assert_called_once()
        self.assertTrue(runtime.projection_workspace_active)
        self.assertIs(settings.active_paint_image, mask_image)
        self.assertTrue(settings.paint_mask_mode)

    def test_mask_workspace_return_in_perspective_disconnects_stencil(self):
        """Mask 활성 Perspective 작업공간 복귀는 재연결하지 않고 즉시 해제합니다."""
        scene, settings, stencil, mask_image, runtime = _prepare_mask_stencil_runtime()
        runtime.projection_workspace_active = False
        scene_key = "mask-workspace-perspective"
        registry = SimpleNamespace(register_timer=mock.Mock())
        with (
            mock.patch.dict(paint_runtime._RUNTIMES, {scene_key: runtime}, clear=True),
            mock.patch.object(
                paint_runtime,
                "_projection_runtime_is_active",
                return_value=True,
            ),
            mock.patch.object(paint_runtime, "reconnect_stencil_texture") as reconnect,
            mock.patch.object(
                paint_runtime,
                "stencil_view_token",
                return_value=(False, "perspective-view"),
            ),
            mock.patch(
                "tools.projection_paint.blender.lifecycle."
                "lifecycle_registry",
                return_value=registry,
            ),
        ):
            paint_runtime._paint_context_changed(scene_key)

        reconnect.assert_not_called()
        registry.register_timer.assert_called_once()
        self.assertIsNone(stencil.brush.texture)
        self.assertTrue(settings.stencil_outside_camera_view)
        self.assertEqual(settings.paint_state, "FROZEN")
        self.assertIs(settings.active_paint_image, mask_image)
        self.assertTrue(settings.paint_mask_mode)

    def test_mask_timer_reentry_in_perspective_disconnects_stencil(self):
        """Mask 활성 Timer 재개도 Perspective에서 Stencil 연결을 남기지 않습니다."""
        scene, settings, stencil, mask_image, runtime = _prepare_mask_stencil_runtime()
        runtime.projection_workspace_active = False
        with (
            mock.patch.object(
                paint_runtime,
                "_projection_runtime_is_active",
                return_value=True,
            ),
            mock.patch.object(paint_runtime, "reconnect_stencil_texture") as reconnect,
            mock.patch.object(
                paint_runtime,
                "stencil_view_token",
                return_value=(False, "perspective-view"),
            ),
        ):
            delay = paint_runtime._track_runtime(runtime)

        reconnect.assert_not_called()
        self.assertEqual(delay, paint_runtime.OUTSIDE_CAMERA_VIEW_INTERVAL)
        self.assertIsNone(stencil.brush.texture)
        self.assertTrue(settings.stencil_outside_camera_view)
        self.assertEqual(settings.paint_state, "FROZEN")
        self.assertIs(settings.active_paint_image, mask_image)
        self.assertTrue(settings.paint_mask_mode)

    def test_mask_frame_source_refresh_updates_stencil_without_target_change(self):
        """Mask 활성 Frame Source 갱신도 Stencil만 갱신하고 선택 Image를 보존합니다."""
        scene, settings, _stencil, mask_image, runtime = _prepare_mask_stencil_runtime()
        with (
            mock.patch.object(
                paint_runtime,
                "ensure_paint_runtime",
                return_value=runtime,
            ),
            mock.patch.object(paint_runtime, "_stage_current_source", return_value=907),
            mock.patch.object(
                paint_runtime,
                "_projection_runtime_is_active",
                return_value=True,
            ),
            mock.patch.object(
                paint_runtime,
                "stencil_view_token",
                return_value=(True, "camera-view"),
            ),
            mock.patch.object(paint_runtime, "refresh_stencil", return_value=True) as refresh,
            mock.patch.object(paint_runtime, "start_viewport_tracking") as start_tracking,
        ):
            self.assertTrue(paint_runtime.refresh_frame_source(scene))

        refresh.assert_called_once()
        start_tracking.assert_called_once_with(scene)
        self.assertIs(settings.active_paint_image, mask_image)
        self.assertTrue(settings.paint_mask_mode)

    def test_mask_frame_source_refresh_outside_camera_disconnects_stencil(self):
        """Perspective View의 Mask Frame 갱신은 Stencil 연결을 유지하지 않습니다."""
        scene, settings, stencil, mask_image, runtime = _prepare_mask_stencil_runtime()
        with (
            mock.patch.object(
                paint_runtime,
                "ensure_paint_runtime",
                return_value=runtime,
            ),
            mock.patch.object(paint_runtime, "_stage_current_source", return_value=907),
            mock.patch.object(
                paint_runtime,
                "_projection_runtime_is_active",
                return_value=True,
            ),
            mock.patch.object(
                paint_runtime,
                "stencil_view_token",
                return_value=(False, "perspective-view"),
            ),
            mock.patch.object(paint_runtime, "refresh_stencil") as refresh,
            mock.patch.object(paint_runtime, "start_viewport_tracking") as start_tracking,
        ):
            self.assertTrue(paint_runtime.refresh_frame_source(scene))

        refresh.assert_not_called()
        start_tracking.assert_called_once_with(scene)
        self.assertIsNone(stencil.brush.texture)
        self.assertTrue(settings.stencil_outside_camera_view)
        self.assertEqual(settings.paint_state, "FROZEN")
        self.assertIs(settings.active_paint_image, mask_image)
        self.assertTrue(settings.paint_mask_mode)

    def test_mask_explicit_current_frame_reconnect_is_allowed(self):
        """Mask 활성 상태에서도 현재 Frame Reconnect는 Stencil 연결을 요청합니다."""
        scene, settings, _stencil, mask_image, runtime = _prepare_mask_stencil_runtime()
        with (
            mock.patch.object(
                paint_runtime,
                "ensure_paint_runtime",
                return_value=runtime,
            ),
            mock.patch.object(
                paint_runtime,
                "_projection_runtime_is_active",
                return_value=True,
            ),
            mock.patch.object(paint_runtime, "_stage_current_source", return_value=907),
            mock.patch.object(paint_runtime, "reconnect_stencil_texture") as reconnect,
            mock.patch.object(paint_runtime, "start_viewport_tracking") as start_tracking,
        ):
            self.assertEqual(paint_runtime.reconnect_current_frame(bpy.context), 907)

        reconnect.assert_called_once_with(bpy.context)
        start_tracking.assert_called_once_with(scene)
        self.assertIs(settings.active_paint_image, mask_image)
        self.assertTrue(settings.paint_mask_mode)

    def test_mask_explicit_nearest_camera_reconnect_is_allowed(self):
        """Mask 활성 상태에서도 최근접 Camera Reconnect는 Stencil 연결을 요청합니다."""
        scene, settings, _stencil, mask_image, runtime = _prepare_mask_stencil_runtime()
        image_id = 911
        candidate = SimpleNamespace(
            entry=SimpleNamespace(image_id=image_id, frame=9),
            distance=0.125,
            alignment=0.875,
        )
        runtime.image_paths = {image_id: Path(__file__)}
        with (
            mock.patch.object(
                paint_runtime,
                "ensure_paint_runtime",
                return_value=runtime,
            ),
            mock.patch.object(
                paint_runtime,
                "_projection_runtime_is_active",
                return_value=True,
            ),
            mock.patch.object(paint_runtime, "viewport_pose", return_value=object()),
            mock.patch.object(paint_runtime, "_capture_reconnect_snapshot", return_value=object()),
            mock.patch.object(paint_runtime, "_camera_index_for_paint", return_value=object()),
            mock.patch.object(paint_runtime, "select_best_camera", return_value=candidate),
            mock.patch.object(paint_runtime, "_camera_for_entry"),
            mock.patch.object(paint_runtime, "activate_managed_frame"),
            mock.patch.object(paint_runtime, "_stage_current_source", return_value=image_id),
            mock.patch.object(paint_runtime, "reconnect_stencil_texture") as reconnect,
            mock.patch.object(paint_runtime, "start_viewport_tracking") as start_tracking,
        ):
            self.assertIs(paint_runtime.reconnect_nearest_camera(bpy.context), candidate)

        reconnect.assert_called_once_with(bpy.context)
        start_tracking.assert_called_once_with(scene)
        self.assertIs(settings.active_paint_image, mask_image)
        self.assertTrue(settings.paint_mask_mode)

    def test_nearest_camera_overlay_recovery_failure_stops_before_snapshot(self):
        """Overlay 사전 복구 실패는 Camera Snapshot 전에 단일 원인으로 중지합니다."""
        _scene, _settings, _stencil, _mask_image, _runtime = (
            _prepare_mask_stencil_runtime()
        )
        with (
            mock.patch.object(
                overlay_migration,
                "repair_managed_overlay_if_needed",
                side_effect=overlay_migration.OverlayMigrationError(
                    "주입된 Overlay 복구 실패"
                ),
            ),
            mock.patch.object(paint_runtime, "ensure_paint_runtime") as ensure_runtime,
            mock.patch.object(
                paint_runtime,
                "_capture_reconnect_snapshot",
            ) as capture_snapshot,
        ):
            with self.assertRaises(paint_runtime.PaintRuntimeError) as caught:
                paint_runtime.reconnect_nearest_camera(bpy.context)

        message = str(caught.exception)
        self.assertIn("관리 Overlay UDIM 복구 실패", message)
        self.assertIn("주입된 Overlay 복구 실패", message)
        self.assertNotIn("최근접 Camera 재연결 실패", message)
        self.assertNotIn("Rollback 실패", message)
        ensure_runtime.assert_not_called()
        capture_snapshot.assert_not_called()

    def test_non_projection_runtime_keeps_mask_stencil_tracking_inert(self):
        """Object Mode 또는 비관리 Workspace에서는 Mask Runtime 추적을 시작하지 않습니다."""
        scene, settings, _stencil, mask_image, runtime = _prepare_mask_stencil_runtime()
        with (
            mock.patch.object(
                paint_runtime,
                "ensure_paint_runtime",
                return_value=runtime,
            ),
            mock.patch.object(
                paint_runtime,
                "_projection_runtime_is_active",
                return_value=False,
            ),
            mock.patch.object(paint_runtime, "_subscribe_camera_view") as subscribe,
        ):
            paint_runtime.start_viewport_tracking(scene)

        subscribe.assert_not_called()
        self.assertIs(settings.active_paint_image, mask_image)
        self.assertTrue(settings.paint_mask_mode)


if __name__ == "__main__":
    unittest.main(argv=[__file__], verbosity=2)
