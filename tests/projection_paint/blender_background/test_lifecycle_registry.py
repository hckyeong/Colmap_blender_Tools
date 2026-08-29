"""Blender Background에서 Lifecycle Epoch와 등록 대칭을 검증합니다."""

from __future__ import annotations

import inspect
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest import mock

import bpy


PROJECT_PARENT = Path(__file__).resolve().parents[3]
if str(PROJECT_PARENT) not in sys.path:
    sys.path.insert(0, str(PROJECT_PARENT))

from tools import projection_paint as addon
from tools.projection_paint.blender import layers as layers_module
from tools.projection_paint.blender import lifecycle as lifecycle_module
from tools.projection_paint.blender.lifecycle import lifecycle_registry
from tools.projection_paint.blender.camera_import import PP_OT_import_colmap
from tools.projection_paint.blender.camera_review import (
    PP_OT_review_background_toggle,
    PP_OT_review_look_through,
)
from tools.projection_paint.blender.stencil_session import (
    PP_OT_project_stencil,
)
from tools.projection_paint.blender.layers import (
    draw_layer_list_context_menu,
)
from tools.projection_paint.blender.panels import (
    PP_OT_open_paint_workspace,
    PP_PT_camera_review,
    PP_PT_colmap,
    PP_PT_layers,
    PP_PT_paint,
    PP_PT_status,
)
from shell.menu import COLMAP_MT_tools


class LifecycleRegistryTest(unittest.TestCase):
    """Lifecycle Registry의 중복·Epoch·예외 격리 계약을 검증합니다."""

    @staticmethod
    def _camera_review_toggle_keymap_items():
        """등록된 Camera Review 배경 토글 단축키 항목을 반환합니다."""

        keyconfig = bpy.context.window_manager.keyconfigs.addon
        return [
            item
            for keymap in keyconfig.keymaps
            for item in keymap.keymap_items
            if (
                keymap.name == "3D View"
                and keymap.space_type == "VIEW_3D"
                and keymap.region_type == "WINDOW"
                and item.idname == "projection_paint.review_background_toggle"
                and item.type == "F7"
                and item.value == "PRESS"
            )
        ]

    @staticmethod
    def _viewport_zoom_keymap_items(keymap_name="3D View"):
        """지정한 Keymap에 등록된 Industry Compatible View Zoom을 반환합니다."""

        keyconfig = bpy.context.window_manager.keyconfigs.addon
        return [
            item
            for keymap in keyconfig.keymaps
            for item in keymap.keymap_items
            if (
                keymap.name == keymap_name
                and keymap.space_type
                == ("VIEW_3D" if keymap_name == "3D View" else "EMPTY")
                and keymap.region_type == "WINDOW"
                and item.idname == "view3d.zoom"
                and item.type == "RIGHTMOUSE"
                and item.value == "PRESS"
                and item.alt
                and not item.ctrl
                and not item.shift
            )
        ]

    @classmethod
    def setUpClass(cls) -> None:
        """Extension을 테스트 Process에 한 번 등록합니다."""

        addon.register()

    @classmethod
    def tearDownClass(cls) -> None:
        """Extension과 모든 Callback을 테스트 Process에서 해제합니다."""

        addon.unregister()

    def test_register_is_idempotent(self) -> None:
        """중복 register가 Handler와 Property를 추가하지 않는지 확인합니다."""

        registry = lifecycle_registry()
        before = (
            len(bpy.app.handlers.load_pre),
            len(bpy.app.handlers.load_post),
            len(bpy.app.handlers.undo_pre),
        )
        addon.register()
        after = (
            len(bpy.app.handlers.load_pre),
            len(bpy.app.handlers.load_post),
            len(bpy.app.handlers.undo_pre),
        )
        self.assertEqual(before, after)
        self.assertTrue(registry.active)
        self.assertTrue(hasattr(bpy.types.Scene, "projection_paint"))
        draw_functions = tuple(
            getattr(
                bpy.types.UI_MT_button_context_menu.draw,
                "_draw_funcs",
                (),
            )
        )
        self.assertEqual(
            sum(
                callback is draw_layer_list_context_menu
                for callback in draw_functions
            ),
            1,
        )
        keyconfig = bpy.context.window_manager.keyconfigs.addon
        delete_items = [
            item
            for keymap in keyconfig.keymaps
            for item in keymap.keymap_items
            if (
                keymap.name == "Window"
                and item.idname
                == "projection_paint.layer_adjustment_remove_hovered"
                and item.type == "DEL"
                and item.value == "PRESS"
            )
        ]
        self.assertEqual(len(delete_items), 1)
        self.assertEqual(len(self._camera_review_toggle_keymap_items()), 1)
        self.assertEqual(len(self._viewport_zoom_keymap_items()), 1)
        self.assertEqual(len(self._viewport_zoom_keymap_items("Image Paint")), 1)

    def test_panel_default_open_states_match_ui_contract(self) -> None:
        """Status·Import·Camera Review·Paint Setup의 독립성과 기본 상태를 검증합니다."""

        self.assertIn("HIDE_HEADER", PP_PT_status.bl_options)
        self.assertNotIn(
            "DEFAULT_CLOSED",
            getattr(PP_PT_colmap, "bl_options", frozenset()),
        )
        self.assertIn("DEFAULT_CLOSED", PP_PT_camera_review.bl_options)
        self.assertIn("DEFAULT_CLOSED", PP_PT_paint.bl_options)
        self.assertIn("DEFAULT_CLOSED", PP_PT_layers.bl_options)
        self.assertEqual(PP_PT_layers.bl_parent_id, "PP_PT_paint")
        for panel in (
            PP_PT_colmap,
            PP_PT_camera_review,
            PP_PT_paint,
        ):
            self.assertFalse(str(getattr(panel, "bl_parent_id", "") or ""))
            self.assertEqual("Projection Paint", panel.bl_category)
        self.assertEqual("Import", PP_OT_import_colmap.bl_label)
        colmap_draw_source = inspect.getsource(PP_PT_colmap.draw)
        self.assertIn(
            'display_box.prop(settings, "camera_frustum_size")',
            colmap_draw_source,
        )
        self.assertNotIn("camera_display_box", colmap_draw_source)
        self.assertNotIn("display_box.separator()", colmap_draw_source)
        self.assertEqual("Look Through Camera", PP_OT_review_look_through.bl_label)
        self.assertEqual(
            "Toggle Background Image",
            PP_OT_review_background_toggle.bl_label,
        )
        self.assertEqual("Project Stencil to Target", PP_OT_project_stencil.bl_label)

    def test_projection_paint_entry_is_drawn_in_colmap_tools_menu(self) -> None:
        """COLMAP Tools 메뉴가 Paint 실행 대신 전용 작업공간을 여는지 검증합니다."""

        owner = SimpleNamespace(layout=mock.Mock())
        context = SimpleNamespace()

        COLMAP_MT_tools.draw(owner, context)

        owner.layout.operator.assert_any_call(
            "projection_paint.open_workspace",
            text="Projection Painting 열기",
            icon="TPAINT_HLT",
        )
        self.assertIn(
            'request_workspace_activation(context, "projection_paint")',
            inspect.getsource(PP_OT_open_paint_workspace.execute),
        )
        self.assertIn(
            "_paint_workspace_active(context)",
            inspect.getsource(PP_PT_paint.draw),
        )
        self.assertNotIn("COLMAP_MT_tools", inspect.getsource(lifecycle_module.register))

    def test_unregister_removes_viewport_keymaps_safely(self) -> None:
        """Viewport Keymap이 해제와 재등록 뒤에도 하나씩 유지되는지 검증합니다."""

        self.assertEqual(len(self._camera_review_toggle_keymap_items()), 1)
        self.assertEqual(len(self._viewport_zoom_keymap_items()), 1)
        self.assertEqual(len(self._viewport_zoom_keymap_items("Image Paint")), 1)
        addon.unregister()
        self.assertEqual(len(self._camera_review_toggle_keymap_items()), 0)
        self.assertEqual(len(self._viewport_zoom_keymap_items()), 0)
        self.assertEqual(len(self._viewport_zoom_keymap_items("Image Paint")), 0)
        addon.unregister()
        self.assertEqual(len(self._camera_review_toggle_keymap_items()), 0)
        self.assertEqual(len(self._viewport_zoom_keymap_items()), 0)
        self.assertEqual(len(self._viewport_zoom_keymap_items("Image Paint")), 0)
        addon.register()
        self.assertEqual(len(self._camera_review_toggle_keymap_items()), 1)
        self.assertEqual(len(self._viewport_zoom_keymap_items()), 1)
        self.assertEqual(len(self._viewport_zoom_keymap_items("Image Paint")), 1)

    def test_epoch_token_is_invalid_after_barrier(self) -> None:
        """중단 장벽 뒤 이전 Epoch Token이 Commit되지 않는지 확인합니다."""

        registry = lifecycle_registry()
        scene = bpy.context.scene
        token = registry.capture_token(scene, dataset_revision=1)
        self.assertTrue(
            registry.token_is_current(token, scene, dataset_revision=1)
        )
        registry.barrier("test")
        self.assertFalse(
            registry.token_is_current(token, scene, dataset_revision=1)
        )
        registry.rebuild(scene, "test_post")

    def test_handler_failure_is_deduplicated(self) -> None:
        """같은 Handler 예외가 한 번만 기록되고 밖으로 전파되지 않는지 확인합니다."""

        registry = lifecycle_registry()
        handlers = []

        def failing_handler(*_args) -> None:
            """예외 격리 검증을 위해 같은 오류를 발생시킵니다."""

            raise RuntimeError("격리 테스트")

        wrapper = registry.register_handler(
            handlers,
            failing_handler,
            component="failure_test",
            keep_after_load=False,
        )
        wrapper()
        wrapper()
        matches = [
            failure
            for failure in registry.failures
            if failure.component == "failure_test"
        ]
        self.assertEqual(1, len(matches))
        registry.unregister_handler(handlers, failing_handler)

    def test_timer_none_self_unregisters_registry(self) -> None:
        """None을 반환한 Timer가 Registry에서 자체 해제되는지 확인합니다."""

        registry = lifecycle_registry()
        calls = []

        def one_shot() -> None:
            """한 번 실행한 뒤 Timer 종료를 요청합니다."""

            calls.append(1)
            return None

        wrapper = registry.register_timer(
            one_shot,
            component="timer_test",
            first_interval=10.0,
        )
        self.assertIsNone(wrapper())
        self.assertEqual([1], calls)
        if bpy.app.timers.is_registered(wrapper):
            bpy.app.timers.unregister(wrapper)
        replacement = registry.register_timer(
            one_shot,
            component="timer_test",
            first_interval=10.0,
        )
        self.assertIsNot(wrapper, replacement)
        registry.unregister_timer(one_shot)

    def test_layer_ui_tracker_retries_restricted_context_and_stops_in_background(self) -> None:
        """등록 제한 Context는 재시도하고 Background는 모달 추적을 시작하지 않습니다."""
        original_bpy = layers_module.bpy
        try:
            layers_module.bpy = SimpleNamespace(
                app=SimpleNamespace(background=False),
                context=SimpleNamespace(
                    window_manager=SimpleNamespace(windows=()),
                ),
            )
            self.assertFalse(
                layers_module._layer_ui_event_tracker_context_ready(
                    layers_module.bpy.context,
                )
            )
            self.assertEqual(
                layers_module.layer_ui_event_tracker_heartbeat(),
                0.1,
            )
            layers_module.bpy = SimpleNamespace(
                app=SimpleNamespace(background=True),
            )
            self.assertIsNone(layers_module.layer_ui_event_tracker_heartbeat())
        finally:
            layers_module.bpy = original_bpy

    def test_layer_ui_tracker_rearms_from_rebuild_hook(self) -> None:
        """Barrier가 취소한 Tracker Timer는 다음 rebuild에서 Lifecycle 소유로 재등록됩니다."""
        registry = lifecycle_registry()
        tracker = layers_module.layer_ui_event_tracker_heartbeat
        scene = bpy.context.scene
        registry.rebuild(scene, "tracker_rearm_before_barrier")
        self.assertIn(tracker, registry._timers)
        registry.barrier("tracker_rearm_barrier")
        self.assertNotIn(tracker, registry._timers)
        registry.rebuild(scene, "tracker_rearm_after_barrier")
        self.assertIn(tracker, registry._timers)
        registry.unregister_timer(tracker)

    def test_layer_ui_tracker_registers_rebuild_hook_without_immediate_timer_call(self) -> None:
        """Tracker는 register 중 실행하지 않고 rebuild hook에서만 Timer를 arm합니다."""
        source = inspect.getsource(lifecycle_module.register)
        self.assertNotIn("layer_ui_event_tracker_heartbeat()", source)
        self.assertIn("LIFECYCLE.add_rebuild_hook(_rebuild_layer_ui_event_tracker)", source)
        self.assertNotIn("LIFECYCLE.register_timer(", source)


if __name__ == "__main__":
    unittest.main(argv=[__file__], verbosity=2)
