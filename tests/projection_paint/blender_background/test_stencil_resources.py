"""Blender 5.2에서 Stencil 정렬과 Source Commit Token을 검증합니다."""

from __future__ import annotations

import inspect
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

import bpy


PROJECT_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PROJECT_ROOT))

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
from tools.projection_paint.blender import paint_runtime  # noqa: E402
from tools.projection_paint.blender.ownership import (  # noqa: E402
    PROJECT_STENCIL_DRAW_BRUSH_ROLE,
    is_owned,
    tag_owned,
)
from tools.projection_paint.blender.stencil_session import (  # noqa: E402
    PROJECT_STENCIL_BRUSH_NAME,
    PP_OT_project_stencil,
    PP_OT_reconnect_stencil,
    StencilSessionError,
    active_draw_brush_for_reconnect,
    active_stencil_session,
    bind_active_draw_brush,
    camera_frame_stencil,
    finish_stencil_session,
    project_current_stencil,
    reconnect_stencil_texture,
    resolve_stencil_connection,
    refresh_stencil,
    stage_stencil_source,
    start_stencil_session,
)
from tools import projection_paint as addon  # noqa: E402


def _new_target():
    """UV Target과 Stencil 연결 권위로 사용할 Scene Camera를 생성합니다."""
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
    camera_data = bpy.data.cameras.new("StencilCameraData")
    camera = bpy.data.objects.new("StencilCamera", camera_data)
    bpy.context.scene.collection.objects.link(camera)
    bpy.context.scene.camera = camera
    return target


def _new_texture_brush(name: str):
    """Background 테스트용 기본 Texture Paint Brush를 생성합니다."""

    return bpy.data.brushes.new(name=name, mode="TEXTURE_PAINT")


def _activate_local_brush(brush) -> None:
    """Background 테스트에서 생성한 Local Brush를 Blender 자산 API로 활성화합니다."""

    window = bpy.context.window
    area = next(item for item in window.screen.areas if item.type == "VIEW_3D")
    region = next(item for item in area.regions if item.type == "WINDOW")
    brush.asset_mark()
    with bpy.context.temp_override(
        window=window,
        screen=window.screen,
        area=area,
        region=region,
    ):
        result = bpy.ops.brush.asset_activate(
            asset_library_type="LOCAL",
            relative_asset_identifier=f"Brush/{brush.name}",
        )
    if result != {"FINISHED"}:
        raise AssertionError(f"Local Brush 활성화 실패: {result}")


def _start_bound_stencil(
    brush,
    *,
    dataset_uuid: str,
    source_fingerprint: str = "",
):
    """테스트에서 resource 준비와 명시적 Brush bind 단계를 분리해 실행합니다."""

    scene = bpy.context.scene
    target = scene.projection_paint.paint_target
    if bpy.context.mode != "PAINT_TEXTURE":
        bpy.context.view_layer.objects.active = target
        target.select_set(True)
        bpy.ops.object.mode_set(mode="TEXTURE_PAINT")
    stencil = start_stencil_session(
        bpy.context,
        dataset_uuid=dataset_uuid,
        source_fingerprint=source_fingerprint,
    )
    tag_owned(
        brush,
        PROJECT_STENCIL_DRAW_BRUSH_ROLE,
        dataset_uuid=dataset_uuid,
    )
    _activate_local_brush(brush)
    with mock.patch(
        "tools.projection_paint.blender.stencil_session.active_draw_brush_for_reconnect",
        return_value=(SimpleNamespace(idname="builtin.brush", brush_type="DRAW"), brush),
    ):
        bind_active_draw_brush(bpy.context, scene)
    return active_stencil_session(scene)


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
        stencil = _start_bound_stencil(brush, dataset_uuid="dataset")
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

    def test_stencil_reconnect_requires_scene_camera(self):
        """Scene Camera가 없으면 저장된 Frame 모서리가 있어도 Stencil을 연결하지 않습니다."""
        target = _new_target()
        start_paint_session(bpy.context, target, width=200, height=100)
        brush = _new_texture_brush("NoCameraBrush")
        stencil = _start_bound_stencil(brush, dataset_uuid="no-camera-dataset")
        source = bpy.data.images.new("NoCameraSource", 200, 100, alpha=True)
        stage_stencil_source(
            stencil,
            image=source,
            image_id=19,
            frame_corners=(
                (10.0, 20.0),
                (210.0, 20.0),
                (210.0, 120.0),
                (10.0, 120.0),
            ),
        )
        bpy.context.scene.camera = None

        with self.assertRaises(StencilSessionError) as caught:
            reconnect_stencil_texture(bpy.context, stencil)

        self.assertIn("Scene Camera", str(caught.exception))
        self.assertIsNone(brush.texture)

    def test_mask_mode_disables_stencil_action_operators(self):
        """Mask 활성 중에는 Reconnect와 Project Stencil 버튼을 모두 비활성화합니다."""
        target = _new_target()
        start_paint_session(bpy.context, target, width=64, height=32)
        settings = bpy.context.scene.projection_paint
        settings.paint_mask_mode = True

        with mock.patch(
            "tools.projection_paint.blender.snapshot_update.snapshot_is_ready",
            return_value=True,
        ):
            self.assertFalse(PP_OT_reconnect_stencil.poll(bpy.context))
            self.assertFalse(PP_OT_project_stencil.poll(bpy.context))

    def test_reconnect_poll_requires_actual_active_runtime(self):
        """ENTERING에서는 차단하고 ACTIVE와 실제 Runtime이 모두 있을 때만 허용합니다."""

        settings = bpy.context.scene.projection_paint
        settings.paint_mask_mode = False
        settings.paint_workspace = bpy.context.window.workspace
        with mock.patch(
            "tools.projection_paint.blender.snapshot_update.snapshot_is_ready",
            return_value=True,
        ), mock.patch(
            "tools.projection_paint.blender.paint_runtime.active_paint_runtime",
            return_value=object(),
        ):
            settings.paint_state = "ENTERING"
            self.assertFalse(PP_OT_reconnect_stencil.poll(bpy.context))
            settings.paint_state = "ACTIVE"
            self.assertTrue(PP_OT_reconnect_stencil.poll(bpy.context))

    def test_reconnect_poll_is_limited_to_projection_paint_workspace(self):
        """F6 재연결은 Projection Paint 작업공간에서만 입력을 소비합니다."""

        scene = bpy.context.scene
        settings = scene.projection_paint
        settings.paint_mask_mode = False
        settings.paint_state = "ACTIVE"
        settings.paint_workspace = bpy.context.window.workspace
        foreign_context = SimpleNamespace(
            scene=scene,
            window=SimpleNamespace(workspace=object()),
        )
        with mock.patch(
            "tools.projection_paint.blender.snapshot_update.snapshot_is_ready",
            return_value=True,
        ), mock.patch(
            "tools.projection_paint.blender.paint_runtime.active_paint_runtime",
            return_value=object(),
        ):
            self.assertTrue(PP_OT_reconnect_stencil.poll(bpy.context))
            self.assertFalse(PP_OT_reconnect_stencil.poll(foreign_context))

    def test_bind_authority_exists_only_in_nearest_camera_operator_transaction(self):
        """새 Brush bind 호출은 Operator가 권한을 전달하는 최근접 Camera Transaction 한 곳뿐입니다."""

        module_source = inspect.getsource(paint_runtime)
        transaction_source = inspect.getsource(paint_runtime.reconnect_nearest_camera)
        self.assertEqual(module_source.count("bind_active_draw_brush(context, scene)"), 1)
        self.assertEqual(
            transaction_source.count("bind_active_draw_brush(context, scene)"),
            1,
        )
        self.assertIn("if bind_active_brush:", transaction_source)

    def test_reconnect_rejects_non_draw_tool_and_brush_without_mutation(self):
        """Clone·Smear·Blur·Fill·Mask는 안내만 반환하고 Tool·Brush 값을 변경하지 않습니다."""

        cases = (
            ("CLONE", "DRAW"),
            ("SMEAR", "SMEAR"),
            ("SOFTEN", "SOFTEN"),
            ("FILL", "FILL"),
            ("MASK", "MASK"),
        )
        for tool_type, brush_type in cases:
            brush = SimpleNamespace(
                image_brush_type=brush_type,
                as_pointer=lambda: 101,
                marker=f"{tool_type}:{brush_type}",
            )
            tool = SimpleNamespace(brush_type=tool_type, marker=tool_type)
            scene = SimpleNamespace(
                tool_settings=SimpleNamespace(
                    image_paint=SimpleNamespace(brush=brush),
                )
            )
            context = SimpleNamespace(scene=scene)
            before = (tool.brush_type, tool.marker, brush.image_brush_type, brush.marker)
            with self.subTest(tool=tool_type, brush=brush_type), mock.patch(
                "tools.projection_paint.blender.stencil_session."
                "active_image_paint_tool",
                return_value=tool,
            ):
                with self.assertRaises(StencilSessionError) as caught:
                    active_draw_brush_for_reconnect(context, scene)
            self.assertIn("Draw Brush", str(caught.exception))
            self.assertEqual(
                (tool.brush_type, tool.marker, brush.image_brush_type, brush.marker),
                before,
            )

    def test_reconnect_accepts_matching_draw_tool_and_brush_without_changing_kind(self):
        """Workspace Tool과 활성 Brush가 모두 Draw면 같은 객체를 그대로 승인합니다."""

        brush = SimpleNamespace(image_brush_type="DRAW", as_pointer=lambda: 101)
        tool = SimpleNamespace(brush_type="DRAW")
        scene = SimpleNamespace(
            tool_settings=SimpleNamespace(image_paint=SimpleNamespace(brush=brush))
        )
        context = SimpleNamespace(scene=scene)
        with mock.patch(
            "tools.projection_paint.blender.stencil_session.active_image_paint_tool",
            return_value=tool,
        ):
            actual_tool, actual_brush = active_draw_brush_for_reconnect(context, scene)
        self.assertIs(actual_tool, tool)
        self.assertIs(actual_brush, brush)
        self.assertEqual(tool.brush_type, "DRAW")
        self.assertEqual(brush.image_brush_type, "DRAW")

    def test_reconnect_accepts_blender_generic_brush_tool_with_draw_brush(self):
        """Blender 5.2 범용 Brush Tool은 실제 Brush가 Draw일 때만 승인합니다."""

        brush = SimpleNamespace(image_brush_type="DRAW", as_pointer=lambda: 101)
        tool = SimpleNamespace(idname="builtin.brush", brush_type="ANY")
        scene = SimpleNamespace(
            tool_settings=SimpleNamespace(image_paint=SimpleNamespace(brush=brush))
        )
        context = SimpleNamespace(scene=scene)
        with mock.patch(
            "tools.projection_paint.blender.stencil_session.active_image_paint_tool",
            return_value=tool,
        ):
            actual_tool, actual_brush = active_draw_brush_for_reconnect(context, scene)
        self.assertIs(actual_tool, tool)
        self.assertIs(actual_brush, brush)

    def test_reconnect_rejects_clone_tool_even_when_stale_brush_is_draw(self):
        """Clone Tool이 오래된 Draw Brush를 가리켜도 새 Stencil 연결을 거부합니다."""

        brush = SimpleNamespace(image_brush_type="DRAW", as_pointer=lambda: 101)
        tool = SimpleNamespace(idname="builtin_brush.clone", brush_type="CLONE")
        scene = SimpleNamespace(
            tool_settings=SimpleNamespace(image_paint=SimpleNamespace(brush=brush))
        )
        context = SimpleNamespace(scene=scene)
        with mock.patch(
            "tools.projection_paint.blender.stencil_session.active_image_paint_tool",
            return_value=tool,
        ):
            with self.assertRaises(StencilSessionError):
                active_draw_brush_for_reconnect(context, scene)

    def test_explicit_bind_creates_project_brush_from_editable_draw(self):
        """최초 Reconnect는 편집 가능한 Draw도 원본을 보존한 전용 Brush로 복제합니다."""

        target = _new_target()
        start_paint_session(bpy.context, target, width=64, height=32)
        bpy.context.view_layer.objects.active = target
        target.select_set(True)
        bpy.ops.object.mode_set(mode="TEXTURE_PAINT")
        brush = _new_texture_brush("ExplicitEditableDraw")
        brush.image_brush_type = "DRAW"
        brush.color = (0.2, 0.3, 0.4)
        _activate_local_brush(brush)
        start_stencil_session(bpy.context, dataset_uuid="explicit-editable")
        original_count = len(bpy.data.brushes)
        tool = SimpleNamespace(brush_type="DRAW")
        with mock.patch(
            "tools.projection_paint.blender.stencil_session."
            "active_draw_brush_for_reconnect",
            return_value=(tool, brush),
        ):
            bound = bind_active_draw_brush(bpy.context)
        self.assertIsNot(bound, brush)
        self.assertIs(bpy.context.scene.projection_paint.stencil_brush, bound)
        self.assertIs(bpy.context.scene.tool_settings.image_paint.brush, bound)
        self.assertEqual(len(bpy.data.brushes), original_count + 1)
        self.assertEqual(tool.brush_type, "DRAW")
        self.assertEqual(bound.image_brush_type, "DRAW")
        self.assertTrue(is_owned(bound, kind=PROJECT_STENCIL_DRAW_BRUSH_ROLE))
        self.assertTrue(bound.name.startswith(PROJECT_STENCIL_BRUSH_NAME))
        self.assertIsNone(bound.library)
        self.assertTrue(bound.is_editable)
        self.assertIsNotNone(bound.asset_data)
        for actual, expected in zip(tuple(brush.color), (0.2, 0.3, 0.4)):
            self.assertAlmostEqual(actual, expected)
        self.assertEqual(tuple(bound.color), (1.0, 1.0, 1.0))

    def test_explicit_bind_creates_and_reuses_project_brush_from_essentials(self):
        """읽기 전용 Essentials Draw도 전용 Brush를 한 번 만들고 이후 같은 Pointer를 재사용합니다."""

        target = _new_target()
        start_paint_session(bpy.context, target, width=64, height=32)
        bpy.context.view_layer.objects.active = target
        target.select_set(True)
        bpy.ops.object.mode_set(mode="TEXTURE_PAINT")
        source = bpy.context.scene.tool_settings.image_paint.brush
        self.assertIsNotNone(source.library)
        self.assertEqual(source.image_brush_type, "DRAW")
        start_stencil_session(bpy.context, dataset_uuid="explicit-essentials")
        original_count = len(bpy.data.brushes)
        tool = SimpleNamespace(brush_type="DRAW")
        with mock.patch(
            "tools.projection_paint.blender.stencil_session."
            "active_draw_brush_for_reconnect",
            return_value=(tool, source),
        ):
            bound = bind_active_draw_brush(bpy.context)
        self.assertIsNot(bound, source)
        self.assertIs(bpy.context.scene.projection_paint.stencil_brush, bound)
        self.assertIs(bpy.context.scene.tool_settings.image_paint.brush, bound)
        self.assertEqual(len(bpy.data.brushes), original_count + 1)
        self.assertTrue(is_owned(bound, kind=PROJECT_STENCIL_DRAW_BRUSH_ROLE))
        self.assertEqual(tool.brush_type, "DRAW")
        self.assertEqual(bound.image_brush_type, "DRAW")
        bound.color = (0.0, 0.0, 0.0)
        with mock.patch(
            "tools.projection_paint.blender.stencil_session."
            "active_draw_brush_for_reconnect",
            return_value=(tool, bound),
        ):
            rebound = bind_active_draw_brush(bpy.context)
        self.assertIs(rebound, bound)
        self.assertEqual(len(bpy.data.brushes), original_count + 1)
        self.assertEqual(tuple(rebound.color), (1.0, 1.0, 1.0))

    def test_failed_first_reconnect_removes_created_project_brush(self):
        """최초 Reconnect 후속 실패 Rollback은 원래 Brush를 복구하고 새 전용 Brush를 제거합니다."""

        target = _new_target()
        start_paint_session(bpy.context, target, width=64, height=32)
        scene = bpy.context.scene
        bpy.context.view_layer.objects.active = target
        target.select_set(True)
        bpy.ops.object.mode_set(mode="TEXTURE_PAINT")
        start_stencil_session(bpy.context, dataset_uuid="rollback-project-brush")
        source = scene.tool_settings.image_paint.brush
        source_color = tuple(float(value) for value in source.color)
        tool = SimpleNamespace(idname="builtin.brush", brush_type="DRAW")
        window = bpy.context.window
        area = next(item for item in window.screen.areas if item.type == "VIEW_3D")
        region = next(item for item in area.regions if item.type == "WINDOW")
        with bpy.context.temp_override(
            window=window,
            screen=window.screen,
            area=area,
            region=region,
        ):
            snapshot = paint_runtime._capture_reconnect_snapshot(bpy.context)
            with mock.patch(
                "tools.projection_paint.blender.stencil_session."
                "active_draw_brush_for_reconnect",
                return_value=(tool, source),
            ):
                created = bind_active_draw_brush(bpy.context, scene)
            self.assertIsNot(created, source)
            created_name = created.name
            runtime = SimpleNamespace(
                scene=scene,
                cache=SimpleNamespace(invalidate=lambda: None),
            )
            rollback_error = paint_runtime._restore_reconnect_snapshot(
                runtime,
                bpy.context,
                snapshot,
            )

        self.assertEqual(rollback_error, "")
        self.assertIs(scene.tool_settings.image_paint.brush, source)
        self.assertEqual(tuple(float(value) for value in source.color), source_color)
        self.assertIsNone(scene.projection_paint.stencil_brush)
        self.assertIsNone(bpy.data.brushes.get(created_name))

    def test_resource_prepare_without_brush_never_activates_or_creates_one(self):
        """활성 Brush가 없어도 자원 준비가 성공하고 Brush를 만들거나 활성화하지 않습니다."""

        target = _new_target()
        start_paint_session(bpy.context, target, width=64, height=32)
        scene = bpy.context.scene
        unified = scene.tool_settings.image_paint.unified_paint_settings
        unified.use_unified_color = True
        original_count = len(bpy.data.brushes)
        self.assertIsNone(scene.tool_settings.image_paint.brush)

        stencil = start_stencil_session(
            bpy.context,
            dataset_uuid="resource-only-dataset",
        )

        self.assertIsNotNone(stencil.managed_texture)
        self.assertIsNone(scene.projection_paint.stencil_brush)
        self.assertIsNone(scene.tool_settings.image_paint.brush)
        self.assertEqual(len(bpy.data.brushes), original_count)
        self.assertTrue(unified.use_unified_color)

    def test_manual_mapping_change_is_not_automatically_reconnected(self):
        """수동 Mapping 해제는 Refresh가 복구하지 않고 Reconnect만 복구합니다."""

        target = _new_target()
        paint = start_paint_session(bpy.context, target, width=200, height=100)
        brush = _new_texture_brush("ManualBrush")
        stencil = _start_bound_stencil(brush, dataset_uuid="dataset")
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
        self.assertFalse(stencil.manual_disabled)
        self.assertEqual(stencil.brush.texture_slot.map_mode, "VIEW_PLANE")

    def test_project_stencil_requires_existing_connected_binding(self):
        """Project Stencil은 연결이 없으면 Frame 갱신이나 새 Brush 채택 전에 Reconnect를 안내합니다."""

        target = _new_target()
        start_paint_session(bpy.context, target, width=64, height=32)
        settings = bpy.context.scene.projection_paint
        settings.paint_enabled = True
        with mock.patch.object(paint_runtime, "reconnect_current_frame") as reconnect:
            with self.assertRaises(StencilSessionError) as caught:
                project_current_stencil(bpy.context)
        self.assertIn("Reconnect", str(caught.exception))
        reconnect.assert_not_called()
        self.assertIsNone(settings.stencil_brush)

    def test_paint_finish_keeps_direct_stencil_binding(self):
        """Paint 종료는 현재 Draw Brush의 직접 Stencil 연결을 해제하거나 복구하지 않습니다."""

        target = _new_target()
        paint = start_paint_session(bpy.context, target, width=200, height=100)
        brush = _new_texture_brush("CameraExitBrush")
        stencil = _start_bound_stencil(brush, dataset_uuid="dataset")
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
        bpy.context.view_layer.objects.active = target
        target.select_set(True)
        bpy.ops.object.mode_set(mode="TEXTURE_PAINT")
        bpy.context.scene.projection_paint.paint_enabled = True
        bpy.context.scene.projection_paint.paint_state = "ACTIVE"
        finish_stencil_session(bpy.context)
        self.assertIs(stencil.brush.texture, stencil.managed_texture)
        self.assertIs(stencil.managed_texture.image, source)
        self.assertEqual(bpy.context.scene.projection_paint.paint_state, "ACTIVE")

    def test_brush_switch_keeps_connected_draw_brush_unchanged(self):
        """다른 Tool Brush 전환은 연결한 Draw Brush Stencil을 제거하거나 새 Brush를 채택하지 않습니다."""
        target = _new_target()
        start_paint_session(bpy.context, target, width=200, height=100)
        scene = bpy.context.scene
        settings = scene.projection_paint
        first = _new_texture_brush("BoundDrawBrush")
        original_texture = bpy.data.textures.new("BoundOriginalTexture", type="IMAGE")
        first.texture = original_texture
        first.texture_slot.map_mode = "VIEW_PLANE"
        first.texture_slot.angle = 0.42
        first.color = (0.2, 0.3, 0.4)
        first.stencil_pos = (23.0, 31.0)
        first.stencil_dimension = (47.0, 59.0)
        _start_bound_stencil(first, dataset_uuid="dataset")
        second = _new_texture_brush("ExternalCloneBrush")
        second.image_brush_type = "CLONE"
        second_texture = bpy.data.textures.new("ExternalCloneTexture", type="IMAGE")
        second.texture = second_texture
        second.texture_slot.map_mode = "VIEW_PLANE"
        second.texture_slot.angle = 0.73
        second.color = (0.7, 0.6, 0.5)
        second.stencil_pos = (61.0, 67.0)
        second.stencil_dimension = (71.0, 79.0)
        second_state = (
            second.texture,
            second.texture_slot.map_mode,
            float(second.texture_slot.angle),
            tuple(second.color),
            tuple(second.stencil_pos),
            tuple(second.stencil_dimension),
        )
        _activate_local_brush(second)
        settings.paint_state = "ACTIVE"

        self.assertIs(settings.stencil_brush, first)
        self.assertIs(scene.tool_settings.image_paint.brush, second)
        self.assertIs(first.texture, original_texture)
        self.assertEqual(first.texture_slot.map_mode, "VIEW_PLANE")
        self.assertAlmostEqual(float(first.texture_slot.angle), 0.42)
        for actual, expected in zip(tuple(first.color), (1.0, 1.0, 1.0)):
            self.assertAlmostEqual(actual, expected)
        self.assertEqual(tuple(first.stencil_pos), (23.0, 31.0))
        self.assertEqual(tuple(first.stencil_dimension), (47.0, 59.0))
        self.assertEqual(
            (
                second.texture,
                second.texture_slot.map_mode,
                float(second.texture_slot.angle),
                tuple(second.color),
                tuple(second.stencil_pos),
                tuple(second.stencil_dimension),
            ),
            second_state,
        )
        status = resolve_stencil_connection(scene, bpy.context)
        self.assertEqual(status.state, "BRUSH_CHANGED")
        self.assertEqual(settings.paint_state, "ACTIVE")
        finish_stencil_session(bpy.context)
        self.assertIs(scene.tool_settings.image_paint.brush, second)
        self.assertEqual(
            (
                second.texture,
                second.texture_slot.map_mode,
                float(second.texture_slot.angle),
                tuple(second.color),
                tuple(second.stencil_pos),
                tuple(second.stencil_dimension),
            ),
            second_state,
        )

    def test_session_reuses_existing_project_brush_and_keeps_texture_state(self):
        """이미 생성된 프로젝트 전용 Brush는 종료 뒤에도 같은 Stencil 상태를 유지합니다."""

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
        stencil = _start_bound_stencil(brush, dataset_uuid="dataset")
        original_brush_count = len(bpy.data.brushes)
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
        finish_paint_session(bpy.context)
        self.assertIs(brush.texture, stencil.managed_texture)
        self.assertEqual(brush.texture_slot.map_mode, "STENCIL")
        self.assertEqual(tuple(brush.color), (1.0, 1.0, 1.0))
        self.assertIsNotNone(bpy.data.textures.get(managed_texture_name))

    def test_blender_saves_and_reloads_direct_stencil_brush_state(self):
        """별도 복구 필드 없이 Blender가 직접 연결한 Brush Stencil 상태를 저장·복원합니다."""

        target = _new_target()
        start_paint_session(bpy.context, target, width=200, height=100)
        brush = _new_texture_brush("SavedDirectStencilBrush")
        stencil = _start_bound_stencil(brush, dataset_uuid="saved-direct-dataset")
        source = bpy.data.images.new("SavedDirectStencilSource", 200, 100, alpha=True)
        stage_stencil_source(
            stencil,
            image=source,
            image_id=71,
            frame_corners=(
                (10.0, 20.0),
                (210.0, 20.0),
                (210.0, 120.0),
                (10.0, 120.0),
            ),
        )
        reconnect_stencil_texture(bpy.context, stencil)
        brush.stencil_pos = (143.0, 87.0)
        brush.stencil_dimension = (79.0, 43.0)
        brush.texture_slot.angle = 0.4375
        brush.color = (0.2, 0.4, 0.6)
        texture_name = stencil.managed_texture.name
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "direct_stencil_state.blend")
            bpy.ops.wm.save_as_mainfile(filepath=path)
            bpy.ops.wm.open_mainfile(filepath=path)

        settings = bpy.context.scene.projection_paint
        restored = bpy.data.brushes.get("SavedDirectStencilBrush")
        texture = bpy.data.textures.get(texture_name)
        self.assertIsNotNone(restored)
        self.assertIs(settings.stencil_brush, restored)
        self.assertIs(settings.stencil_texture, texture)
        self.assertIs(restored.texture, texture)
        self.assertEqual(restored.texture_slot.map_mode, "STENCIL")
        self.assertEqual(tuple(restored.stencil_pos), (143.0, 87.0))
        self.assertEqual(tuple(restored.stencil_dimension), (79.0, 43.0))
        self.assertAlmostEqual(float(restored.texture_slot.angle), 0.4375)
        for actual, expected in zip(restored.color, (0.2, 0.4, 0.6)):
            self.assertAlmostEqual(float(actual), expected, places=6)

    def test_created_project_brush_survives_save_and_reload(self):
        """Essentials에서 만든 프로젝트 전용 Brush와 Stencil 연결이 저장·재열기 뒤 유지됩니다."""

        target = _new_target()
        start_paint_session(bpy.context, target, width=200, height=100)
        scene = bpy.context.scene
        bpy.context.view_layer.objects.active = target
        target.select_set(True)
        bpy.ops.object.mode_set(mode="TEXTURE_PAINT")
        stencil = start_stencil_session(
            bpy.context,
            dataset_uuid="saved-created-project-brush",
        )
        source = scene.tool_settings.image_paint.brush
        tool = SimpleNamespace(idname="builtin.brush", brush_type="DRAW")
        with mock.patch(
            "tools.projection_paint.blender.stencil_session."
            "active_draw_brush_for_reconnect",
            return_value=(tool, source),
        ):
            created = bind_active_draw_brush(bpy.context, scene)
        image = bpy.data.images.new("SavedCreatedStencilSource", 200, 100, alpha=True)
        stage_stencil_source(
            stencil,
            image=image,
            image_id=81,
            frame_corners=(
                (10.0, 20.0),
                (210.0, 20.0),
                (210.0, 120.0),
                (10.0, 120.0),
            ),
        )
        reconnect_stencil_texture(bpy.context, active_stencil_session(scene))
        created_name = created.name
        texture_name = scene.projection_paint.stencil_texture.name
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "created_project_stencil.blend")
            bpy.ops.wm.save_as_mainfile(filepath=path)
            bpy.ops.wm.open_mainfile(filepath=path)

        restored_settings = bpy.context.scene.projection_paint
        restored = bpy.data.brushes.get(created_name)
        texture = bpy.data.textures.get(texture_name)
        self.assertIsNotNone(restored)
        self.assertIs(restored_settings.stencil_brush, restored)
        self.assertTrue(is_owned(restored, kind=PROJECT_STENCIL_DRAW_BRUSH_ROLE))
        self.assertIs(restored.texture, texture)
        self.assertEqual(restored.texture_slot.map_mode, "STENCIL")

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
