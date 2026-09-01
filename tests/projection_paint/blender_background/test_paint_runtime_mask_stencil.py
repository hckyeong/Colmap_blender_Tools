"""Blender Background에서 Mask 활성 중 Stencil 연결 수명주기를 검증합니다."""

from __future__ import annotations

from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest import mock

import bpy


ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

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
    StencilSessionError,
    active_stencil_session,
    bind_active_draw_brush,
    finish_stencil_session,
    reconnect_stencil_texture,
    stage_stencil_source,
    start_stencil_session,
)
from tools.projection_paint.blender.ownership import (  # noqa: E402
    PROJECT_STENCIL_DRAW_BRUSH_ROLE,
    tag_owned,
)


def _activate_and_bind_test_brush(scene, brush) -> None:
    """Background 테스트용 전용 Local Brush를 활성화해 Stencil 대상으로 연결합니다."""

    window = bpy.context.window
    area = next(item for item in window.screen.areas if item.type == "VIEW_3D")
    region = next(item for item in area.regions if item.type == "WINDOW")
    brush.asset_mark()
    with bpy.context.temp_override(window=window, screen=window.screen, area=area, region=region):
        result = bpy.ops.brush.asset_activate(
            asset_library_type="LOCAL",
            relative_asset_identifier=f"Brush/{brush.name}",
        )
    if result != {"FINISHED"}:
        raise AssertionError(f"Local Brush 활성화 실패: {result}")
    tag_owned(
        brush,
        PROJECT_STENCIL_DRAW_BRUSH_ROLE,
        dataset_uuid=str(scene.projection_paint.dataset_uuid or "mask-stencil-runtime"),
    )
    bind_active_draw_brush(bpy.context, scene)


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
    """Camera Source와 활성 Mask Image를 가진 연결 해제 Runtime 대역을 준비합니다."""
    target = _new_target()
    start_paint_session(bpy.context, target, width=64, height=32)
    scene = bpy.context.scene
    settings = scene.projection_paint
    bpy.context.view_layer.objects.active = target
    target.select_set(True)
    bpy.ops.object.mode_set(mode="TEXTURE_PAINT")
    camera_data = bpy.data.cameras.new("MaskStencilRuntimeCameraData")
    camera = bpy.data.objects.new("MaskStencilRuntimeCamera", camera_data)
    scene.collection.objects.link(camera)
    scene.camera = camera
    brush = bpy.data.brushes.new("Mask Stencil Runtime Brush", mode="TEXTURE_PAINT")
    stencil = start_stencil_session(
        bpy.context,
        dataset_uuid="mask-stencil-runtime",
    )
    _activate_and_bind_test_brush(scene, brush)
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
    settings.paint_enabled = True
    settings.paint_state = "ACTIVE"
    paint_runtime.disconnect_stencil(scene)
    runtime = SimpleNamespace(
        scene=scene,
        entries={
            int(scene.frame_current): SimpleNamespace(image_id=image_id),
        },
        image_paths={},
        projection_workspace_active=True,
        viewport_retry_count=0,
        viewport_token=(True, "camera-view"),
        tool_bridge=None,
        view_subscription_owner=None,
        scene_uuid="mask-runtime",
    )
    return scene, settings, stencil, mask_image, runtime


def _prepare_connected_stencil_runtime():
    """Camera View 추적 테스트용 연결된 일반 Layer Runtime을 준비합니다."""

    scene, settings, stencil, _mask_image, runtime = _prepare_mask_stencil_runtime()
    settings.paint_mask_mode = False
    settings.active_paint_image = settings.overlay_image
    reconnect_stencil_texture(bpy.context, stencil)
    runtime.viewport_token = (True, "이전-camera-view")
    return scene, settings, active_stencil_session(scene), runtime


class PaintRuntimeMaskStencilTests(unittest.TestCase):
    """Mask 활성 중에는 Camera 상태와 무관하게 Stencil이 해제되는지 검증합니다."""

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

    def test_direct_stencil_reconnect_rejects_mask_mode(self):
        """저수준 재연결도 Mask 활성 상태에서는 Brush Texture를 연결하지 않습니다."""
        _scene, settings, stencil, mask_image, _runtime = _prepare_mask_stencil_runtime()

        with self.assertRaises(StencilSessionError) as caught:
            reconnect_stencil_texture(bpy.context, stencil)

        self.assertIn("Mask 레이어", str(caught.exception))
        self.assertIsNone(stencil.brush.texture)
        self.assertIs(settings.active_paint_image, mask_image)
        self.assertTrue(settings.paint_mask_mode)

    def test_projection_mask_does_not_change_native_mask_settings(self):
        """PP Mask 중 Blender 기본 MASK 종류와 Stencil Layer 사용값을 그대로 보존합니다."""

        scene, settings, stencil, mask_image, runtime = _prepare_mask_stencil_runtime()
        image_paint = scene.tool_settings.image_paint
        stencil.brush.image_brush_type = "MASK"
        image_paint.use_stencil_layer = True
        scene_key = "native-mask-invariant"
        with (
            mock.patch.dict(paint_runtime._RUNTIMES, {scene_key: runtime}, clear=True),
            mock.patch.object(
                paint_runtime,
                "_projection_runtime_is_active",
                return_value=True,
            ),
        ):
            paint_runtime._camera_view_changed(scene_key)

        self.assertEqual(stencil.brush.image_brush_type, "MASK")
        self.assertTrue(image_paint.use_stencil_layer)
        self.assertEqual(settings.stencil_state, "MASK_SUSPENDED")
        self.assertIs(settings.active_paint_image, mask_image)

    def test_mask_camera_exit_and_return_keep_stencil_disconnected(self):
        """Mask 활성 중에는 Camera View 이탈·복귀 모두 Brush 연결을 유지하지 않습니다."""
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
        self.assertFalse(settings.stencil_outside_camera_view)
        self.assertEqual(settings.stencil_state, "MASK_SUSPENDED")
        self.assertEqual(settings.paint_state, "ACTIVE")
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

        self.assertIsNone(stencil.brush.texture)
        self.assertFalse(settings.stencil_outside_camera_view)
        self.assertEqual(settings.stencil_state, "MASK_SUSPENDED")
        self.assertEqual(settings.paint_state, "ACTIVE")
        self.assertIs(settings.active_paint_image, mask_image)
        self.assertTrue(settings.paint_mask_mode)

    def test_mask_tracking_timer_idles_without_reconnecting(self):
        """Mask 활성 Timer는 저빈도로 유지하되 Stencil을 다시 연결하지 않습니다."""
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
        self.assertFalse(settings.stencil_outside_camera_view)
        self.assertEqual(settings.stencil_state, "MASK_SUSPENDED")
        self.assertEqual(settings.paint_state, "ACTIVE")
        self.assertIs(settings.active_paint_image, mask_image)
        self.assertTrue(settings.paint_mask_mode)

    def test_start_tracking_is_inert_when_mask_is_active(self):
        """Mask 활성 Runtime은 Camera Message Bus와 Stencil Timer를 시작하지 않습니다."""
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
        registry.register_timer.assert_not_called()
        self.assertTrue(runtime.projection_workspace_active)
        self.assertIs(settings.active_paint_image, mask_image)
        self.assertTrue(settings.paint_mask_mode)

    def test_mask_workspace_return_keeps_stencil_disconnected(self):
        """Mask 작업공간 복귀는 Tool Timer만 재개하고 Stencil Timer는 시작하지 않습니다."""
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

        reconnect.assert_not_called()
        registry.register_timer.assert_called_once_with(
            paint_runtime._paint_tool_edge_tick,
            component="paint_tool_edge_tracking",
            first_interval=0.0,
        )
        self.assertTrue(runtime.projection_workspace_active)
        self.assertIs(settings.active_paint_image, mask_image)
        self.assertTrue(settings.paint_mask_mode)

    def test_mask_workspace_return_in_perspective_disconnects_stencil(self):
        """Mask Perspective 복귀는 Tool Timer만 재개하고 Stencil을 연결하지 않습니다."""
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
        registry.register_timer.assert_called_once_with(
            paint_runtime._paint_tool_edge_tick,
            component="paint_tool_edge_tracking",
            first_interval=0.0,
        )
        self.assertIsNone(stencil.brush.texture)
        self.assertFalse(settings.stencil_outside_camera_view)
        self.assertEqual(settings.stencil_state, "MASK_SUSPENDED")
        self.assertEqual(settings.paint_state, "ACTIVE")
        self.assertIs(settings.active_paint_image, mask_image)
        self.assertTrue(settings.paint_mask_mode)

    def test_camera_view_pan_and_zoom_realign_only_the_connected_brush(self):
        """Camera View 이동·확대 Token 변경은 같은 Draw Brush 위치만 다시 맞춥니다."""

        _scene, _settings, _stencil, runtime = _prepare_connected_stencil_runtime()
        token = (True, "이동된-camera-view")
        with (
            mock.patch.object(
                paint_runtime,
                "_projection_runtime_is_active",
                return_value=True,
            ),
            mock.patch.object(
                paint_runtime,
                "stencil_view_token",
                return_value=token,
            ),
            mock.patch.object(paint_runtime, "align_stencil_to_camera") as align,
            mock.patch.object(paint_runtime, "disconnect_stencil") as disconnect,
            mock.patch.object(paint_runtime, "reconnect_stencil_texture") as reconnect,
        ):
            delay = paint_runtime._track_runtime(runtime)

        self.assertEqual(delay, paint_runtime.VIEWPORT_TRACK_INTERVAL)
        align.assert_called_once_with(bpy.context, runtime.scene)
        disconnect.assert_not_called()
        reconnect.assert_not_called()

    def test_camera_view_exit_disconnects_without_changing_paint_state(self):
        """Camera View 이탈은 Source와 Paint 상태를 유지하고 Brush Texture만 해제합니다."""

        scene, settings, stencil, runtime = _prepare_connected_stencil_runtime()
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
        self.assertEqual(settings.paint_state, "ACTIVE")
        self.assertIsNotNone(settings.stencil_source_image)
        self.assertIs(scene.projection_paint.stencil_brush, stencil.brush)

    def test_camera_view_exit_disconnects_while_frame_source_is_pending(self):
        """Frame Source 대기 중에도 Camera View 이탈을 먼저 처리합니다."""

        _scene, settings, stencil, runtime = _prepare_connected_stencil_runtime()
        with (
            mock.patch.object(
                paint_runtime,
                "_projection_runtime_is_active",
                return_value=True,
            ),
            mock.patch.object(
                paint_runtime,
                "_frame_source_is_current",
                return_value=False,
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
        self.assertEqual(settings.paint_state, "ACTIVE")
        self.assertIsNotNone(settings.stencil_source_image)

    def test_same_workspace_target_transition_keeps_view_timer_alive(self):
        """같은 작업공간의 일시적인 활성 Target 전환은 View Timer를 유지합니다."""

        _scene, settings, _stencil, runtime = _prepare_connected_stencil_runtime()
        settings.paint_workspace = bpy.context.window.workspace
        with mock.patch.object(
            paint_runtime,
            "_projection_runtime_is_active",
            return_value=False,
        ):
            delay = paint_runtime._track_runtime(runtime)

        self.assertEqual(delay, paint_runtime.OUTSIDE_CAMERA_VIEW_INTERVAL)
        self.assertFalse(runtime.projection_workspace_active)
        self.assertIsNone(runtime.viewport_token)

    def test_camera_view_return_restores_only_the_same_connected_brush(self):
        """Camera View 복귀는 다른 도구를 선택하지 않고 기존 Draw Brush 연결만 복구합니다."""

        scene, settings, stencil, runtime = _prepare_connected_stencil_runtime()
        paint_runtime.disconnect_stencil(scene)
        self.assertTrue(settings.stencil_outside_camera_view)
        with (
            mock.patch.object(
                paint_runtime,
                "_projection_runtime_is_active",
                return_value=True,
            ),
            mock.patch.object(
                paint_runtime,
                "stencil_view_token",
                return_value=(True, "camera-view-return"),
            ),
        ):
            delay = paint_runtime._track_runtime(runtime)

        self.assertEqual(delay, paint_runtime.VIEWPORT_TRACK_INTERVAL)
        self.assertIs(stencil.brush.texture, stencil.managed_texture)
        self.assertFalse(settings.stencil_outside_camera_view)
        self.assertIs(scene.tool_settings.image_paint.brush, stencil.brush)

    def test_start_tracking_registers_camera_view_timer_for_general_layer(self):
        """연결된 일반 Layer는 Message Bus와 Camera View 추적 Timer를 함께 시작합니다."""

        scene, _settings, _stencil, runtime = _prepare_connected_stencil_runtime()
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
            mock.patch.object(paint_runtime, "_subscribe_camera_view") as subscribe,
            mock.patch(
                "tools.projection_paint.blender.lifecycle.lifecycle_registry",
                return_value=registry,
            ),
        ):
            paint_runtime.start_viewport_tracking(scene)

        subscribe.assert_called_once_with(runtime)
        registry.register_timer.assert_called_once_with(
            paint_runtime._viewport_tracking_tick,
            component="stencil_view_tracking",
            first_interval=0.0,
        )
        self.assertIsNone(runtime.viewport_token)

    def test_general_layer_selection_rearms_view_tracking_without_reconnect(self):
        """일반 Layer 선택은 자동 재연결 없이 Camera View Timer만 다시 준비합니다."""

        scene, _settings, _stencil, runtime = _prepare_connected_stencil_runtime()
        with (
            mock.patch.object(
                paint_runtime,
                "active_paint_runtime",
                return_value=runtime,
            ),
            mock.patch.object(
                paint_runtime,
                "_projection_runtime_is_active",
                return_value=True,
            ),
            mock.patch.object(paint_runtime, "resolve_stencil_connection") as resolve,
            mock.patch.object(paint_runtime, "start_viewport_tracking") as start_tracking,
            mock.patch.object(
                paint_runtime,
                "reconnect_stencil_texture",
            ) as reconnect,
        ):
            restored = paint_runtime.resume_stencil_after_layer_selection(bpy.context)

        self.assertFalse(restored)
        resolve.assert_called_once_with(scene, bpy.context)
        start_tracking.assert_called_once_with(scene)
        reconnect.assert_not_called()

    def test_general_workspace_return_rearms_tool_and_stencil_timers(self):
        """일반 Layer 작업공간 복귀는 Tool Edge와 Camera View Timer를 모두 재등록합니다."""

        scene, _settings, _stencil, runtime = _prepare_connected_stencil_runtime()
        runtime.projection_workspace_active = False
        scene_key = "general-workspace-return"
        registry = SimpleNamespace(register_timer=mock.Mock())
        with (
            mock.patch.dict(paint_runtime._RUNTIMES, {scene_key: runtime}, clear=True),
            mock.patch.object(
                paint_runtime,
                "_projection_runtime_is_active",
                return_value=True,
            ),
            mock.patch.object(paint_runtime, "_subscribe_camera_view") as subscribe,
            mock.patch(
                "tools.projection_paint.blender.lifecycle.lifecycle_registry",
                return_value=registry,
            ),
        ):
            paint_runtime._paint_context_changed(scene_key)

        subscribe.assert_called_once_with(runtime)
        registry.register_timer.assert_has_calls(
            (
                mock.call(
                    paint_runtime._paint_tool_edge_tick,
                    component="paint_tool_edge_tracking",
                    first_interval=0.0,
                ),
                mock.call(
                    paint_runtime._viewport_tracking_tick,
                    component="stencil_view_tracking",
                    first_interval=0.0,
                ),
            )
        )
        self.assertEqual(registry.register_timer.call_count, 2)
        self.assertTrue(runtime.projection_workspace_active)

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
        self.assertFalse(settings.stencil_outside_camera_view)
        self.assertEqual(settings.stencil_state, "MASK_SUSPENDED")
        self.assertEqual(settings.paint_state, "ACTIVE")
        self.assertIs(settings.active_paint_image, mask_image)
        self.assertTrue(settings.paint_mask_mode)

    def test_mask_frame_source_refresh_updates_source_without_stencil(self):
        """Mask 활성 Frame 갱신은 Source만 바꾸고 Stencil 연결과 선택 Image를 보존합니다."""
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

        refresh.assert_not_called()
        start_tracking.assert_not_called()
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
        start_tracking.assert_not_called()
        self.assertIsNone(stencil.brush.texture)
        self.assertFalse(settings.stencil_outside_camera_view)
        self.assertEqual(settings.stencil_state, "MASK_SUSPENDED")
        self.assertEqual(settings.paint_state, "ACTIVE")
        self.assertIs(settings.active_paint_image, mask_image)
        self.assertTrue(settings.paint_mask_mode)

    def test_frame_source_failure_restores_previous_pointer_and_metadata(self):
        """Source Commit 중 예외가 나면 기존 Image·metadata·Render 상태를 보존합니다."""

        scene, settings, _stencil, runtime = _prepare_connected_stencil_runtime()
        old_source = settings.stencil_source_image
        old_texture = settings.stencil_texture
        old_texture_image = old_texture.image
        old_metadata = (
            int(settings.active_image_id),
            str(settings.current_image_id),
            str(settings.current_image_name),
            str(settings.stencil_frame_corners_json),
        )
        scene.render.resolution_x = 321
        scene.render.resolution_y = 123
        settings.render_resolution_overridden = True
        settings.original_render_width = 801
        settings.original_render_height = 601
        settings.applied_render_width = 321
        settings.applied_render_height = 123
        old_render = (
            int(scene.render.resolution_x),
            int(scene.render.resolution_y),
            bool(settings.render_resolution_overridden),
            int(settings.original_render_width),
            int(settings.original_render_height),
            int(settings.applied_render_width),
            int(settings.applied_render_height),
        )
        next_source = bpy.data.images.new(
            "MaskStencilRuntimeNextSource",
            64,
            32,
            alpha=True,
        )
        entry = SimpleNamespace(
            frame=int(scene.frame_current),
            image_id=908,
            image_name="next-source.png",
        )

        class CommitCache:
            """테스트 Image를 Commit Callback에 즉시 전달하는 결정적 Cache 대역입니다."""

            def __init__(self):
                """고정 Image와 Pin 기록을 준비합니다."""
                self.pinned: list[int] = []

            def begin_request(self, *, frame, image_id):
                """Frame과 IMAGE_ID를 담은 고정 Token을 반환합니다."""
                return (int(frame), int(image_id))

            def get(self, image_id):
                """요청한 최신 테스트 Image를 반환합니다."""
                return next_source if int(image_id) == 908 else None

            def commit(self, _token, image, *, current_frame, apply):
                """Commit Callback을 실행해 중간 실패를 재현합니다."""
                apply(image, 908)
                return True

            def pin(self, image_id):
                """Rollback이 다시 고정한 IMAGE_ID를 기록합니다."""
                self.pinned.append(int(image_id))

        runtime.entries = {int(scene.frame_current): entry}
        runtime.image_paths = {908: Path(__file__)}
        runtime.cache = CommitCache()

        def mutate_resolution(_runtime, _camera, _image):
            """Source Commit의 Render 변경을 의도적으로 재현합니다."""
            scene.render.resolution_x = 64
            scene.render.resolution_y = 32
            settings.applied_render_width = 64
            settings.applied_render_height = 32

        def fail_after_partial_bind(_context, _scene, image, image_id, camera):
            """Source Pointer와 metadata를 일부 쓴 뒤 의도한 예외를 발생시킵니다."""
            settings.stencil_source_image = image
            settings.active_image_id = int(image_id)
            settings.current_image_id = str(image_id)
            settings.current_image_name = str(image.name)
            settings.stencil_texture.image = image
            scene.camera = camera
            raise RuntimeError("의도한 Source Commit 실패")

        with (
            mock.patch.object(paint_runtime, "ensure_paint_runtime", return_value=runtime),
            mock.patch.object(
                paint_runtime,
                "_camera_for_entry",
                return_value=scene.camera,
            ),
            mock.patch.object(
                paint_runtime,
                "_apply_camera_resolution",
                side_effect=mutate_resolution,
            ),
            mock.patch.object(
                paint_runtime,
                "bind_stencil_source",
                side_effect=fail_after_partial_bind,
            ),
        ):
            self.assertFalse(
                paint_runtime.refresh_frame_source(
                    scene,
                    expected_frame=int(scene.frame_current),
                    expected_image_id=908,
                    request_is_current=lambda: True,
                )
            )

        self.assertIs(settings.stencil_source_image, old_source)
        self.assertIs(settings.stencil_texture, old_texture)
        self.assertIs(settings.stencil_texture.image, old_texture_image)
        self.assertEqual(
            (
                int(settings.active_image_id),
                str(settings.current_image_id),
                str(settings.current_image_name),
                str(settings.stencil_frame_corners_json),
            ),
            old_metadata,
        )
        self.assertEqual(
            (
                int(scene.render.resolution_x),
                int(scene.render.resolution_y),
                bool(settings.render_resolution_overridden),
                int(settings.original_render_width),
                int(settings.original_render_height),
                int(settings.applied_render_width),
                int(settings.applied_render_height),
            ),
            old_render,
        )
        self.assertEqual(runtime.cache.pinned[-1], old_metadata[0])

    def test_mask_explicit_current_frame_reconnect_is_blocked(self):
        """Mask 활성 상태의 현재 Frame Reconnect는 Source 변경 전에 거부됩니다."""
        scene, settings, _stencil, mask_image, runtime = _prepare_mask_stencil_runtime()
        with (
            mock.patch.object(paint_runtime, "ensure_paint_runtime") as ensure_runtime,
            mock.patch.object(paint_runtime, "_stage_current_source") as stage_source,
            mock.patch.object(paint_runtime, "reconnect_stencil_texture") as reconnect,
            mock.patch.object(paint_runtime, "start_viewport_tracking") as start_tracking,
        ):
            with self.assertRaises(paint_runtime.PaintRuntimeError) as caught:
                paint_runtime.reconnect_current_frame(bpy.context)

        self.assertIn("Mask 레이어", str(caught.exception))
        ensure_runtime.assert_not_called()
        stage_source.assert_not_called()
        reconnect.assert_not_called()
        start_tracking.assert_not_called()
        self.assertIs(settings.active_paint_image, mask_image)
        self.assertTrue(settings.paint_mask_mode)

    def test_mask_explicit_nearest_camera_reconnect_is_blocked(self):
        """Mask 활성 상태의 최근접 Camera Reconnect는 Camera 선택 전에 거부됩니다."""
        _scene, settings, _stencil, mask_image, _runtime = _prepare_mask_stencil_runtime()
        with (
            mock.patch.object(paint_runtime, "ensure_paint_runtime") as ensure_runtime,
            mock.patch.object(paint_runtime, "viewport_pose") as read_pose,
            mock.patch.object(paint_runtime, "select_best_camera") as select_camera,
            mock.patch.object(paint_runtime, "reconnect_stencil_texture") as reconnect,
            mock.patch.object(paint_runtime, "start_viewport_tracking") as start_tracking,
        ):
            with self.assertRaises(paint_runtime.PaintRuntimeError) as caught:
                paint_runtime.reconnect_nearest_camera(bpy.context)

        self.assertIn("Mask 레이어", str(caught.exception))
        ensure_runtime.assert_not_called()
        read_pose.assert_not_called()
        select_camera.assert_not_called()
        reconnect.assert_not_called()
        start_tracking.assert_not_called()
        self.assertIs(settings.active_paint_image, mask_image)
        self.assertTrue(settings.paint_mask_mode)

    def test_nearest_camera_adopts_brush_only_with_operator_permission(self):
        """최근접 Camera 내부 호출은 권한 없이 bind하지 않고 Operator 권한만 한 번 전달합니다."""

        scene, settings, _stencil, _mask_image, runtime = _prepare_mask_stencil_runtime()
        settings.paint_mask_mode = False
        candidate = SimpleNamespace(
            entry=SimpleNamespace(image_id=907, frame=3),
            distance=1.0,
            alignment=0.9,
        )
        runtime.image_paths = {907: Path(__file__)}
        for permitted, expected_calls in ((False, 0), (True, 1)):
            with (
                self.subTest(permitted=permitted),
                mock.patch(
                    "tools.projection_paint.blender.overlay_migration."
                    "repair_managed_overlay_if_needed"
                ),
                mock.patch.object(
                    paint_runtime,
                    "active_draw_brush_for_reconnect",
                ) as preflight,
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
                mock.patch.object(
                    paint_runtime,
                    "_camera_index_for_paint",
                    return_value=object(),
                ),
                mock.patch.object(
                    paint_runtime,
                    "_capture_reconnect_snapshot",
                    return_value=SimpleNamespace(
                        active_brush=scene.tool_settings.image_paint.brush,
                    ),
                ),
                mock.patch.object(
                    paint_runtime,
                    "select_best_camera",
                    return_value=candidate,
                ),
                mock.patch.object(paint_runtime, "_camera_for_entry"),
                mock.patch.object(paint_runtime, "activate_managed_frame"),
                mock.patch.object(
                    paint_runtime,
                    "_stage_current_source",
                    return_value=907,
                ),
                mock.patch.object(paint_runtime, "bind_active_draw_brush") as bind,
                mock.patch.object(paint_runtime, "reconnect_stencil_texture"),
                mock.patch.object(paint_runtime, "_validate_reconnect_invariants"),
                mock.patch.object(paint_runtime, "start_viewport_tracking"),
            ):
                self.assertIs(
                    paint_runtime.reconnect_nearest_camera(
                        bpy.context,
                        bind_active_brush=permitted,
                    ),
                    candidate,
                )
            self.assertEqual(preflight.call_count, expected_calls)
            self.assertEqual(bind.call_count, expected_calls)

    def test_nearest_camera_rolls_back_each_mutating_stage_failure(self):
        """Camera·Frame·Source·Bind·Reconnect·검증 단계 실패가 모두 전체 Rollback을 호출합니다."""

        _scene, settings, _stencil, _mask_image, runtime = _prepare_mask_stencil_runtime()
        settings.paint_mask_mode = False
        candidate = SimpleNamespace(
            entry=SimpleNamespace(image_id=907, frame=3),
            distance=1.0,
            alignment=0.9,
        )
        runtime.image_paths = {907: Path(__file__)}
        for failure_name in (
            "camera",
            "frame",
            "source",
            "bind",
            "reconnect",
            "validate",
        ):
            with (
                self.subTest(failure_name=failure_name),
                mock.patch(
                    "tools.projection_paint.blender.overlay_migration."
                    "repair_managed_overlay_if_needed"
                ),
                mock.patch.object(paint_runtime, "active_draw_brush_for_reconnect"),
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
                mock.patch.object(
                    paint_runtime,
                    "_camera_index_for_paint",
                    return_value=object(),
                ),
                mock.patch.object(
                    paint_runtime,
                    "_capture_reconnect_snapshot",
                    return_value=SimpleNamespace(
                        active_brush=_scene.tool_settings.image_paint.brush,
                    ),
                ),
                mock.patch.object(
                    paint_runtime,
                    "select_best_camera",
                    return_value=candidate,
                ),
                mock.patch.object(paint_runtime, "_camera_for_entry") as camera_stage,
                mock.patch.object(paint_runtime, "activate_managed_frame") as frame_stage,
                mock.patch.object(
                    paint_runtime,
                    "_stage_current_source",
                    return_value=907,
                ) as source_stage,
                mock.patch.object(paint_runtime, "bind_active_draw_brush") as bind_stage,
                mock.patch.object(
                    paint_runtime,
                    "reconnect_stencil_texture",
                ) as reconnect_stage,
                mock.patch.object(
                    paint_runtime,
                    "_validate_reconnect_invariants",
                ) as validate_stage,
                mock.patch.object(
                    paint_runtime,
                    "_restore_reconnect_snapshot",
                    return_value="",
                ) as rollback,
            ):
                stages = {
                    "camera": camera_stage,
                    "frame": frame_stage,
                    "source": source_stage,
                    "bind": bind_stage,
                    "reconnect": reconnect_stage,
                    "validate": validate_stage,
                }
                stages[failure_name].side_effect = RuntimeError(failure_name)
                with self.assertRaises(paint_runtime.PaintRuntimeError):
                    paint_runtime.reconnect_nearest_camera(
                        bpy.context,
                        bind_active_brush=True,
                    )
            rollback.assert_called_once()

    def test_reconnect_invariant_covers_paint_target_authority(self):
        """Reconnect 검증은 Canvas·활성 Paint Slot·Object·Material 변경을 차단합니다."""

        scene, settings, _stencil, _mask_image, _runtime = _prepare_mask_stencil_runtime()
        settings.paint_mask_mode = False
        target_snapshot = paint_runtime._capture_paint_target_snapshot(bpy.context)
        snapshot = SimpleNamespace(
            tool_id="builtin.brush",
            active_brush=scene.tool_settings.image_paint.brush,
            paint_target=target_snapshot,
        )
        with mock.patch(
            "tools.projection_paint.blender.paint_tool_bridge.active_tool_id",
            return_value="builtin.brush",
        ):
            paint_runtime._validate_reconnect_invariants(
                bpy.context,
                scene,
                snapshot,
                snapshot.active_brush,
            )
            replacement = bpy.data.images.new("ReconnectCanvasInvariant", 4, 4)
            scene.tool_settings.image_paint.canvas = replacement
            with self.assertRaises(paint_runtime.PaintRuntimeError):
                paint_runtime._validate_reconnect_invariants(
                    bpy.context,
                    scene,
                    snapshot,
                    snapshot.active_brush,
                )
            scene.tool_settings.image_paint.canvas = target_snapshot.canvas

    def test_nearest_camera_overlay_recovery_failure_rolls_back_snapshot(self):
        """Overlay 복구 실패는 먼저 잡은 Reconnect Snapshot으로 전체 상태를 되돌립니다."""
        _scene, settings, _stencil, _mask_image, runtime = _prepare_mask_stencil_runtime()
        settings.paint_mask_mode = False
        candidate = SimpleNamespace(
            entry=SimpleNamespace(image_id=907, frame=3),
            distance=1.0,
            alignment=0.9,
        )
        runtime.image_paths = {907: Path(__file__)}
        snapshot = SimpleNamespace(
            active_brush=_scene.tool_settings.image_paint.brush,
        )
        with (
            mock.patch.object(
                overlay_migration,
                "repair_managed_overlay_if_needed",
                side_effect=overlay_migration.OverlayMigrationError(
                    "주입된 Overlay 복구 실패"
                ),
            ),
            mock.patch.object(paint_runtime, "ensure_paint_runtime", return_value=runtime),
            mock.patch.object(
                paint_runtime,
                "_projection_runtime_is_active",
                return_value=True,
            ),
            mock.patch.object(paint_runtime, "viewport_pose", return_value=object()),
            mock.patch.object(paint_runtime, "_camera_index_for_paint", return_value=object()),
            mock.patch.object(
                paint_runtime,
                "_capture_reconnect_snapshot",
                return_value=snapshot,
            ) as capture_snapshot,
            mock.patch.object(
                paint_runtime,
                "select_best_camera",
                return_value=candidate,
            ),
            mock.patch.object(paint_runtime, "_camera_for_entry"),
            mock.patch.object(paint_runtime, "activate_managed_frame"),
            mock.patch.object(paint_runtime, "_stage_current_source", return_value=907),
            mock.patch.object(paint_runtime, "reconnect_stencil_texture"),
            mock.patch.object(paint_runtime, "_validate_reconnect_invariants"),
            mock.patch.object(paint_runtime, "start_viewport_tracking"),
            mock.patch.object(
                paint_runtime,
                "_restore_reconnect_snapshot",
                return_value="",
            ) as rollback,
        ):
            with self.assertRaises(paint_runtime.PaintRuntimeError) as caught:
                paint_runtime.reconnect_nearest_camera(bpy.context)

        message = str(caught.exception)
        self.assertIn("최근접 Camera 재연결 실패", message)
        self.assertIn("주입된 Overlay 복구 실패", message)
        self.assertNotIn("Rollback 실패", message)
        capture_snapshot.assert_called_once()
        rollback.assert_called_once_with(runtime, bpy.context, snapshot)

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
