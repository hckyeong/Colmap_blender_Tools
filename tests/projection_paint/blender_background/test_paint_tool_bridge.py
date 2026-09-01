"""Blender Texture Paint Tool Edge의 최소 Bridge 계약을 검증합니다."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import sys
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from tools.projection_paint.blender import paint_tool_bridge  # noqa: E402
from tools.projection_paint.blender.ownership import (  # noqa: E402
    OWNER_KEY,
    OWNER_TOKEN,
    PROJECT_STENCIL_DRAW_BRUSH_ROLE,
    RECORD_KIND_KEY,
)


class _Brush:
    """Pointer와 Brush 종류만 제공하는 활성 Brush 대역입니다."""

    def __init__(self, brush_type: str, pointer: int = 1):
        """Brush 종류와 고정 Pointer를 저장합니다."""

        self.image_brush_type = brush_type
        self._pointer = pointer
        self.asset_data = None
        self._properties = {}

    def as_pointer(self) -> int:
        """테스트용 고정 Blender Pointer를 반환합니다."""

        return self._pointer

    def get(self, key: str, default=None):
        """테스트 Brush의 ID Property 값을 반환합니다."""

        return self._properties.get(key, default)

    def __setitem__(self, key: str, value) -> None:
        """테스트 Brush에 ID Property 값을 기록합니다."""

        self._properties[key] = value


class _Tools:
    """테스트가 지정한 Texture Paint Tool을 반환하는 Workspace 대역입니다."""

    def __init__(self, tool_id: str):
        """초기 Tool ID를 저장합니다."""

        self.tool = SimpleNamespace(idname=tool_id)

    def from_space_view3d_mode(self, _mode: str, *, create: bool):
        """새 Tool 생성 없이 현재 Tool을 반환합니다."""

        if create:
            raise AssertionError("Tool 조회 중 새 Workspace Tool을 만들면 안 됩니다.")
        return self.tool


def _fixture(tool_id: str, brush_type: str):
    """Bridge 테스트에 필요한 Context·Scene·Tools를 생성합니다."""

    tools = _Tools(tool_id)
    image_paint = SimpleNamespace(
        brush=_Brush(brush_type),
        brush_asset_reference=SimpleNamespace(
            asset_library_type="CUSTOM",
            asset_library_identifier="User",
            relative_asset_identifier="brushes.blend/Brush/User Brush",
        ),
    )
    scene = SimpleNamespace(tool_settings=SimpleNamespace(image_paint=image_paint))
    scene.projection_paint = SimpleNamespace(stencil_brush=None)
    context = SimpleNamespace(workspace=SimpleNamespace(tools=tools))
    return context, scene, tools


class PaintToolBridgeTests(unittest.TestCase):
    """Tool ID Edge 외에는 Brush와 Clone 모드를 건드리지 않는지 검증합니다."""

    def test_each_supported_tool_has_matching_essentials_fallback(self) -> None:
        """Draw·Blur·Smear·Clone·Fill·Mask의 종류와 기본 자산명을 고정합니다."""

        expected = (
            ("builtin.brush", "DRAW", "Paint Hard"),
            ("builtin_brush.soften", "SOFTEN", "Blur"),
            ("builtin_brush.smear", "SMEAR", "Smear"),
            ("builtin_brush.clone", "CLONE", "Clone"),
            ("builtin_brush.fill", "FILL", "Fill"),
            ("builtin_brush.mask", "MASK", "Mask"),
        )
        for tool_id, brush_type, asset_name in expected:
            with self.subTest(tool_id=tool_id):
                spec = paint_tool_bridge.paint_tool_spec(tool_id)
                self.assertIsNotNone(spec)
                self.assertEqual(spec.brush_type, brush_type)
                self.assertEqual(spec.asset_name, asset_name)
                self.assertTrue(spec.essentials_identifier.endswith(f"/Brush/{asset_name}"))

    def test_baseline_and_same_tool_tick_are_noop(self) -> None:
        """진입 기준선과 동일 Tool ID Tick은 Brush와 Clone 설정을 바꾸지 않습니다."""

        context, scene, _tools = _fixture("builtin.brush", "DRAW")
        scene.tool_settings.image_paint.use_clone_layer = True
        brush = scene.tool_settings.image_paint.brush
        state = paint_tool_bridge.initialize_paint_tool_bridge(context, scene)
        self.assertFalse(paint_tool_bridge.detect_paint_tool_edge(context, scene, state))
        self.assertIs(scene.tool_settings.image_paint.brush, brush)
        self.assertTrue(scene.tool_settings.image_paint.use_clone_layer)

    def test_clone_tool_edge_uses_same_active_image_native_mode(self) -> None:
        """Clone Tool Edge는 활성 Brush를 보존하고 Paint Slot Clone 모드만 끕니다."""

        context, scene, tools = _fixture("builtin.brush", "DRAW")
        state = paint_tool_bridge.initialize_paint_tool_bridge(context, scene)
        clone_brush = _Brush("CLONE", pointer=2)
        scene.tool_settings.image_paint.brush = clone_brush
        scene.tool_settings.image_paint.use_clone_layer = True
        tools.tool.idname = "builtin_brush.clone"
        self.assertTrue(paint_tool_bridge.detect_paint_tool_edge(context, scene, state))
        self.assertFalse(scene.tool_settings.image_paint.use_clone_layer)
        self.assertIs(scene.tool_settings.image_paint.brush, clone_brush)

    def test_clone_tool_baseline_uses_same_active_image_native_mode(self) -> None:
        """Clone이 이미 활성인 작업공간 복귀도 같은 활성 Layer 모드를 확정합니다."""

        context, scene, _tools = _fixture("builtin_brush.clone", "CLONE")
        clone_brush = scene.tool_settings.image_paint.brush
        scene.tool_settings.image_paint.use_clone_layer = True
        state = paint_tool_bridge.initialize_paint_tool_bridge(context, scene)
        self.assertEqual(state.tool_id, "builtin_brush.clone")
        self.assertFalse(scene.tool_settings.image_paint.use_clone_layer)
        self.assertIs(scene.tool_settings.image_paint.brush, clone_brush)

    def test_compatible_user_brush_is_preserved(self) -> None:
        """Tool Edge에서 이미 호환되는 사용자 Brush는 절대 교체하지 않습니다."""

        context, scene, tools = _fixture("builtin.brush", "DRAW")
        state = paint_tool_bridge.initialize_paint_tool_bridge(context, scene)
        user_brush = _Brush("SMEAR", pointer=3)
        scene.tool_settings.image_paint.brush = user_brush
        tools.tool.idname = "builtin_brush.smear"
        self.assertTrue(paint_tool_bridge.detect_paint_tool_edge(context, scene, state))
        self.assertIs(scene.tool_settings.image_paint.brush, user_brush)
        self.assertIn("builtin_brush.smear", state.last_compatible_brushes)

    def test_unsupported_tool_is_not_intercepted(self) -> None:
        """지원하지 않는 Tool Edge는 Brush와 Clone 설정을 그대로 둡니다."""

        context, scene, tools = _fixture("builtin.brush", "DRAW")
        state = paint_tool_bridge.initialize_paint_tool_bridge(context, scene)
        brush = scene.tool_settings.image_paint.brush
        scene.tool_settings.image_paint.use_clone_layer = True
        tools.tool.idname = "builtin.select_box"
        self.assertFalse(paint_tool_bridge.detect_paint_tool_edge(context, scene, state))
        self.assertIs(scene.tool_settings.image_paint.brush, brush)
        self.assertTrue(scene.tool_settings.image_paint.use_clone_layer)

    def test_tool_edge_remembers_latest_compatible_user_brush(self) -> None:
        """다른 Tool로 떠나는 Edge에서 직전에 고른 호환 사용자 Brush를 기억합니다."""

        context, scene, tools = _fixture("builtin.brush", "DRAW")
        state = paint_tool_bridge.initialize_paint_tool_bridge(context, scene)
        latest_reference = scene.tool_settings.image_paint.brush_asset_reference
        latest_reference.relative_asset_identifier = "brushes.blend/Brush/Latest Draw"
        tools.tool.idname = "builtin.select_box"
        self.assertFalse(paint_tool_bridge.detect_paint_tool_edge(context, scene, state))
        remembered = state.last_compatible_brushes["builtin.brush"]
        self.assertEqual(
            remembered.relative_identifier,
            "brushes.blend/Brush/Latest Draw",
        )

    def test_draw_edge_prefers_saved_project_stencil_brush_to_essentials(self) -> None:
        """기억한 Draw가 없으면 저장된 프로젝트 전용 Brush를 Essentials보다 먼저 사용합니다."""

        context, scene, tools = _fixture("builtin_brush.clone", "CLONE")
        dedicated = _Brush("DRAW", pointer=8)
        dedicated.name = "Projection Paint Stencil Draw"
        dedicated.asset_data = object()
        dedicated[OWNER_KEY] = OWNER_TOKEN
        dedicated[RECORD_KIND_KEY] = PROJECT_STENCIL_DRAW_BRUSH_ROLE
        scene.projection_paint.stencil_brush = dedicated
        state = paint_tool_bridge.initialize_paint_tool_bridge(context, scene)
        tools.tool.idname = "builtin.brush"

        def activate(_context, target_scene, choice, **_kwargs):
            """요청 Asset을 기록하고 전용 Brush가 활성화된 상태를 재현합니다."""

            target_scene.tool_settings.image_paint.brush = dedicated
            target_scene.tool_settings.image_paint.brush_asset_reference = SimpleNamespace(
                asset_library_type=choice.library_type,
                asset_library_identifier=choice.library_identifier,
                relative_asset_identifier=choice.relative_identifier,
            )
            return dedicated

        with mock.patch.object(
            paint_tool_bridge,
            "activate_brush_asset",
            side_effect=activate,
        ) as activation:
            self.assertTrue(
                paint_tool_bridge.detect_paint_tool_edge(context, scene, state)
            )
        choice = activation.call_args.args[2]
        self.assertEqual(choice.library_type, "LOCAL")
        self.assertEqual(
            choice.relative_identifier,
            "Brush/Projection Paint Stencil Draw",
        )
        self.assertIs(scene.tool_settings.image_paint.brush, dedicated)


if __name__ == "__main__":
    unittest.main(argv=[__file__], verbosity=2)
