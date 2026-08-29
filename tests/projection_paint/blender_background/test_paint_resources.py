"""Blender 5.2에서 Paint Material과 Overlay 내구성을 검증합니다."""

from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

import bpy


ROOT = Path(__file__).resolve().parents[3] / "tools" / "projection_paint"
sys.path.insert(0, str(ROOT.parent))

from tools.projection_paint.blender.material_setup import (  # noqa: E402
    MANAGED_OVERLAY_LEGACY_VALID,
    PaintSetupError,
    classify_managed_overlay,
    pending_overlay_resolution_change,
    prepare_active_material,
    resolve_overlay_size,
    setup_from_scene_state,
    validate_material_contract,
)
from tools.projection_paint.blender.image_backend import (  # noqa: E402
    IMAGE_BACKEND_KEY,
    UDIM_BACKEND,
    new_managed_paint_image,
)
from tools.projection_paint.blender.layer_shader import (  # noqa: E402
    LayerShaderError,
    reconcile_layer_graph,
    validate_active_layer_graph,
    activate_layer_paint_image,
    activate_mask_paint_image,
    validate_layer_graph,
)
from tools.projection_paint.blender.layer_reset import (  # noqa: E402
    replace_with_fresh_layer,
)
from tools.projection_paint.blender import layer_reset  # noqa: E402
from tools.projection_paint.blender import layer_shader  # noqa: E402
from tools.projection_paint.blender import overlay_durability as durability  # noqa: E402
from tools.projection_paint.blender.overlay_durability import (  # noqa: E402
    clear_runtime_registry,
    ensure_overlay_durable,
    mark_overlay_dirty,
    OverlayDurabilityError,
    recover_overlay_from_sidecar,
    registered_overlays,
    verify_registered_overlays,
)
from tools.projection_paint.blender.paint_session import (  # noqa: E402
    PaintSessionError,
    _capture_snapshot,
    _resolution_reset_message,
    _restore_snapshot,
    _selected_overlay_resolution,
    active_paint_session,
    create_paint_session_start_job,
    finish_paint_session,
    start_paint_session,
)
from tools.projection_paint.blender.paint_progress_session import (  # noqa: E402
    active_paint_progress_session,
)
from tools.projection_paint.blender.paint_state import (  # noqa: E402
    PaintStateError,
    commit_paint_setup,
    derive_paint_state,
)
from tools import projection_paint as addon  # noqa: E402
from tools.projection_paint.blender import paint_session  # noqa: E402
from tools.projection_paint.blender import overlay_migration  # noqa: E402
from tools.projection_paint.blender.ownership import UUID_KEY  # noqa: E402


def _new_target(name: str = "Target"):
    """UV와 한 면을 가진 로컬 Mesh Object를 생성합니다."""
    mesh = bpy.data.meshes.new(f"{name}Mesh")
    mesh.from_pydata(
        ((0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (1.0, 1.0, 0.0), (0.0, 1.0, 0.0)),
        (),
        ((0, 1, 2, 3),),
    )
    mesh.uv_layers.new(name="UVMap")
    target = bpy.data.objects.new(name, mesh)
    bpy.context.scene.collection.objects.link(target)
    return target


def _new_multi_material_target(name: str = "MultiMaterialTarget"):
    """재질 슬롯별 면을 가진 네 구역 Mesh Object를 생성합니다."""
    vertices = []
    faces = []
    for index in range(4):
        x = float(index)
        offset = len(vertices)
        vertices.extend(
            (
                (x, 0.0, 0.0),
                (x + 0.8, 0.0, 0.0),
                (x + 0.8, 0.8, 0.0),
                (x, 0.8, 0.0),
            )
        )
        faces.append(tuple(offset + corner for corner in range(4)))
    mesh = bpy.data.meshes.new(f"{name}Mesh")
    mesh.from_pydata(vertices, (), faces)
    mesh.uv_layers.new(name="UVMap")
    target = bpy.data.objects.new(name, mesh)
    bpy.context.scene.collection.objects.link(target)
    return target


def _new_textured_material(width: int = 64, height: int = 32):
    """직접 Base Color Image를 가진 Principled Material을 생성합니다."""
    material = bpy.data.materials.new("OriginalMaterial")
    material.use_nodes = True
    tree = material.node_tree
    principled = next(node for node in tree.nodes if node.type == "BSDF_PRINCIPLED")
    image = bpy.data.images.new("BaseImage", width, height, alpha=True)
    image_node = tree.nodes.new("ShaderNodeTexImage")
    image_node.image = image
    tree.links.new(image_node.outputs["Color"], principled.inputs["Base Color"])
    return material, image, image_node


def _prepare_recoverable_file_overlay(name: str, *, dirty: bool = False):
    """단일 Texture Target의 활성 관리 Layer를 pathless Packed FILE 상태로 만듭니다."""
    target = _new_target(name)
    material, source_image, _source_node = _new_textured_material(8, 4)
    target.data.materials.append(material)
    with tempfile.TemporaryDirectory(dir=str(ROOT)) as directory:
        source_path = Path(directory) / f"{name}_source.png"
        source_image.filepath_raw = str(source_path)
        source_image.file_format = "PNG"
        source_image.save()
        setup = prepare_active_material(target, 8, 4, use_base_resolution=False)
        commit_paint_setup(bpy.context.scene, setup)
        reconcile_layer_graph(
            bpy.context.scene,
            material=setup.material,
            overlay=setup.overlay,
        )
    settings = bpy.context.scene.projection_paint
    managed = settings.layers[0].image
    del target[IMAGE_BACKEND_KEY]
    managed.pixels[0:16] = (
        0.25,
        0.5,
        0.75,
        1.0,
        1.0,
        0.0,
        0.5,
        0.25,
        0.0,
        1.0,
        0.25,
        0.5,
        0.75,
        0.25,
        0.0,
        0.0,
    )
    managed.update()
    ensure_overlay_durable(managed, scene=bpy.context.scene, force=True)
    managed.source = "FILE"
    managed.filepath_raw = ""
    managed.filepath = ""
    if dirty:
        managed.pixels[0] = 0.375
        managed.update()
    settings.overlay_image = managed
    settings.active_paint_image = managed
    return target, setup.material, settings, managed


class PaintResourceTests(unittest.TestCase):
    """비파괴 Material과 Overlay 저장 계약을 검증합니다."""

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
        clear_runtime_registry()

    def tearDown(self):
        """활성 Paint Session을 테스트 종료 전에 정리합니다."""
        if active_paint_session() is not None:
            try:
                finish_paint_session(bpy.context)
            except Exception:
                pass

    def test_overlay_preserves_original_material_and_uses_base_size(self):
        """원본 Node를 보존하고 Base Image 해상도의 Overlay를 만듭니다."""
        target = _new_target()
        original, base, original_image_node = _new_textured_material()
        target.data.materials.append(original)
        original_link = original_image_node.outputs["Color"].links[0]
        setup = prepare_active_material(target, 2048, 2048)
        self.assertIsNot(setup.material, original)
        self.assertIs(target.active_material, setup.material)
        self.assertEqual(tuple(setup.overlay.size), tuple(base.size))
        self.assertEqual(
            original_image_node.outputs["Color"].links[0].as_pointer(),
            original_link.as_pointer(),
        )
        self.assertEqual(original_link.to_socket.name, "Base Color")
        self.assertEqual(setup.overlay.generated_color[3], 0.0)
        validate_material_contract(setup)

    def test_enter_paint_without_target_does_not_create_progress_session(self):
        """Paint Target 없는 진입은 진행 세션 생성 전에 취소되는지 검증합니다."""

        settings = bpy.context.scene.projection_paint
        settings.paint_target = None

        with self.assertRaisesRegex(RuntimeError, "유효한 Mesh Object"):
            bpy.ops.projection_paint.enter_paint("INVOKE_DEFAULT")

        self.assertIsNone(active_paint_progress_session())
        self.assertEqual("ERROR", settings.paint_state)
        self.assertIn("유효한 Mesh Object", settings.status)

    def test_modal_conversion_boundary_keeps_object_mode_until_handoff(self):
        """100% Paint 준비 단계까지는 Modal 안에서 Texture Paint Mode로 전환하지 않습니다."""

        target = _new_target("ModalBoundaryTarget")
        settings = bpy.context.scene.projection_paint
        settings.paint_target = target
        bpy.context.view_layer.objects.active = target
        target.select_set(True)
        job = create_paint_session_start_job(
            bpy.context,
            target,
            width=16,
            height=8,
            use_base_resolution=False,
        )
        while not job.conversion_ready:
            job.advance()

        self.assertEqual("OBJECT", target.mode)
        self.assertFalse(settings.paint_enabled)
        self.assertEqual("Paint image ready", job.progress.phase)
        job.abort()
        self.assertEqual("UVMap", target.data.uv_layers.active.name)
        self.assertIsNone(target.data.uv_layers.get("PP_UDIM"))

    def test_new_single_target_entry_ignores_previous_target_overlay(self):
        """신규 SINGLE Target 진입은 이전 Target의 Scene Overlay를 복구하지 않습니다."""
        previous = _new_target("PreviousManagedTarget")
        previous_setup = prepare_active_material(
            previous,
            8,
            4,
            use_base_resolution=False,
        )
        commit_paint_setup(bpy.context.scene, previous_setup)
        reconcile_layer_graph(
            bpy.context.scene,
            material=previous_setup.material,
            overlay=previous_setup.overlay,
        )
        previous_overlay = previous_setup.overlay

        target = _new_target("NewSingleTarget")
        material, _source, _node = _new_textured_material(8, 4)
        target.data.materials.append(material)
        settings = bpy.context.scene.projection_paint
        settings.paint_target = target
        bpy.context.view_layer.objects.active = target
        target.select_set(True)

        session = start_paint_session(
            bpy.context,
            target,
            width=8,
            height=4,
            use_base_resolution=False,
        )

        self.assertIs(session.setup.target, target)
        self.assertEqual(target["pp_udim_kind"], "SINGLE_1001")
        self.assertEqual(target[IMAGE_BACKEND_KEY], "SINGLE")
        self.assertEqual(settings.overlay_image.source, "FILE")
        self.assertIsNot(settings.overlay_image, previous_overlay)
        self.assertEqual(previous_overlay.source, "FILE")

    def test_new_target_entry_failure_restores_previous_scene_stack(self):
        """신규 Target Graph 연결 실패는 이전 Scene Layer와 포인터를 복원합니다."""
        previous = _new_target("PreviousRollbackTarget")
        previous_setup = prepare_active_material(
            previous,
            8,
            4,
            use_base_resolution=False,
        )
        commit_paint_setup(bpy.context.scene, previous_setup)
        reconcile_layer_graph(
            bpy.context.scene,
            material=previous_setup.material,
            overlay=previous_setup.overlay,
        )
        settings = bpy.context.scene.projection_paint
        previous_layers = tuple(
            (str(layer.layer_uuid), layer.image)
            for layer in settings.layers
        )

        target = _new_target("FailedNewTarget")
        material, _source, _node = _new_textured_material(8, 4)
        target.data.materials.append(material)
        settings.paint_target = target
        bpy.context.view_layer.objects.active = target
        target.select_set(True)

        with (
            mock.patch.object(
                layer_shader,
                "reconcile_layer_graph",
                side_effect=LayerShaderError("강제 Graph 실패"),
            ),
            self.assertRaisesRegex(LayerShaderError, "강제 Graph 실패"),
        ):
            start_paint_session(
                bpy.context,
                target,
                width=8,
                height=4,
                use_base_resolution=False,
            )

        self.assertIs(settings.paint_target, target)
        self.assertIs(settings.paint_material, previous_setup.material)
        self.assertIs(settings.overlay_image, previous_setup.overlay)
        self.assertEqual(len(settings.layers), len(previous_layers))
        self.assertEqual(
            tuple(str(layer.layer_uuid) for layer in settings.layers),
            tuple(layer_uuid for layer_uuid, _image in previous_layers),
        )
        for layer, (_layer_uuid, image) in zip(settings.layers, previous_layers):
            self.assertIs(layer.image, image)

    def test_new_target_entry_blocks_previous_active_composite(self):
        """이전 Target의 활성 Composite는 새 Target 전환 전에 명시 해제를 요구합니다."""
        previous = _new_target("PreviousCompositeTarget")
        previous_setup = prepare_active_material(
            previous,
            8,
            4,
            use_base_resolution=False,
        )
        commit_paint_setup(bpy.context.scene, previous_setup)
        reconcile_layer_graph(
            bpy.context.scene,
            material=previous_setup.material,
            overlay=previous_setup.overlay,
        )
        settings = bpy.context.scene.projection_paint
        settings.composite_state = "ACTIVE"
        settings.active_composite_session_uuid = "previous-composite"
        previous_layers = tuple(layer.image for layer in settings.layers)

        target = _new_target("BlockedNewTarget")
        settings.paint_target = target

        with self.assertRaisesRegex(PaintSessionError, "Composite를 먼저 해제"):
            start_paint_session(
                bpy.context,
                target,
                width=8,
                height=4,
                use_base_resolution=False,
            )

        self.assertIs(settings.paint_material, previous_setup.material)
        self.assertIs(settings.overlay_image, previous_setup.overlay)
        self.assertEqual(
            tuple(layer.image for layer in settings.layers),
            previous_layers,
        )

    def test_enter_paint_invoke_uses_modal_progress_for_resolution_reset(self):
        """관리 대상 해상도 초기화 invoke는 기존 중앙 진행창 Modal 경로를 선택합니다."""
        target = _new_target("ResetInvokeTarget")
        settings = type("Settings", (), {})()
        settings.paint_target = target
        settings.use_base_resolution = False
        settings.status = ""
        settings.status_kind = ""
        settings.paint_state = ""
        window_manager = mock.Mock()
        window_manager.event_timer_add.return_value = object()
        context = type("Context", (), {})()
        context.scene = type("Scene", (), {"projection_paint": settings})()
        context.window = object()
        context.window_manager = window_manager
        job = mock.Mock()
        session = type("ProgressSession", (), {"job_id": "reset-job"})()
        operator = type("EnterPaintOperator", (), {})()
        operator.replace_existing_overlay = True
        operator.job_id = ""
        operator.execute = mock.Mock(return_value={"FINISHED"})
        operator._start_error = mock.Mock(return_value={"CANCELLED"})

        with (
            mock.patch.object(paint_session, "_paint_progress_ui_available", return_value=True),
            mock.patch.object(paint_session, "requires_udim_conversion", return_value=False),
            mock.patch.object(paint_session, "_paint_entry_dimensions", return_value=(64, 64)),
            mock.patch.object(paint_session, "create_paint_session_start_job", return_value=job),
            mock.patch.object(paint_session, "create_paint_progress_session", return_value=session),
            mock.patch.object(paint_session, "add_progress_overlay_draw_handler", return_value=object()),
            mock.patch.object(paint_session, "attach_paint_progress_ui"),
            mock.patch.object(paint_session, "request_progress_overlay_redraw"),
        ):
            result = paint_session.PP_OT_enter_paint.invoke(operator, context, object())

        self.assertEqual(result, {"RUNNING_MODAL"})
        window_manager.modal_handler_add.assert_called_once_with(operator)

    def test_enter_paint_invoke_uses_modal_progress_for_overlay_recovery(self):
        """단일 FILE Overlay 복구가 필요한 재진입도 진행창 Modal 경로를 선택합니다."""
        target = _new_target("OverlayRecoveryInvokeTarget")
        settings = type("Settings", (), {})()
        settings.paint_target = target
        settings.overlay_image = object()
        settings.use_base_resolution = False
        settings.status = ""
        settings.status_kind = ""
        settings.paint_state = ""
        window_manager = mock.Mock()
        window_manager.event_timer_add.return_value = object()
        context = type("Context", (), {})()
        context.scene = type("Scene", (), {"projection_paint": settings})()
        context.window = object()
        context.window_manager = window_manager
        job = mock.Mock()
        session = type("ProgressSession", (), {"job_id": "recovery-job"})()
        operator = type("EnterPaintOperator", (), {})()
        operator.replace_existing_overlay = False
        operator.job_id = ""
        operator.execute = mock.Mock(return_value={"FINISHED"})
        operator._start_error = mock.Mock(return_value={"CANCELLED"})

        with (
            mock.patch.object(paint_session, "_paint_progress_ui_available", return_value=True),
            mock.patch.object(
                paint_session,
                "managed_overlay_recovery_required",
                return_value=True,
            ),
            mock.patch.object(paint_session, "requires_udim_conversion", return_value=False),
            mock.patch.object(paint_session, "_paint_entry_dimensions", return_value=(64, 64)),
            mock.patch.object(paint_session, "create_paint_session_start_job", return_value=job),
            mock.patch.object(paint_session, "create_paint_progress_session", return_value=session),
            mock.patch.object(paint_session, "add_progress_overlay_draw_handler", return_value=object()),
            mock.patch.object(paint_session, "attach_paint_progress_ui"),
            mock.patch.object(paint_session, "request_progress_overlay_redraw"),
        ):
            result = paint_session.PP_OT_enter_paint.invoke(operator, context, object())

        self.assertEqual(result, {"RUNNING_MODAL"})
        window_manager.modal_handler_add.assert_called_once_with(operator)
        operator.execute.assert_not_called()

    def test_enter_paint_invoke_keeps_sync_execute_for_managed_reentry(self):
        """변환과 해상도 변경이 없는 관리 대상 재진입은 기존 동기 execute를 유지합니다."""
        target = _new_target("ManagedReentryInvokeTarget")
        settings = type("Settings", (), {"paint_target": target})()
        context = type("Context", (), {})()
        context.scene = type("Scene", (), {"projection_paint": settings})()
        context.window = object()
        operator = type("EnterPaintOperator", (), {})()
        operator.replace_existing_overlay = False
        operator.execute = mock.Mock(return_value={"FINISHED"})
        operator._start_error = mock.Mock(return_value={"CANCELLED"})

        with (
            mock.patch.object(paint_session, "_paint_progress_ui_available", return_value=True),
            mock.patch.object(paint_session, "requires_udim_conversion", return_value=False),
        ):
            result = paint_session.PP_OT_enter_paint.invoke(operator, context, object())

        self.assertEqual(result, {"FINISHED"})
        operator.execute.assert_called_once_with(context)

    def test_multi_material_original_texture_remains_viewport_active(self):
        """네 Texture 재질을 보존하고 실제 면은 단일 UDIM 재질로 전환합니다."""
        with tempfile.TemporaryDirectory() as directory:
            target = _new_multi_material_target()
            originals = []
            base_images = []
            tile_names = ("u1_v1", "u2_v1", "u1_v2", "u2_v2")
            for index, tile_name in enumerate(tile_names):
                material, image, image_node = _new_textured_material(32, 32)
                material.name = f"OriginalMaterial{index}"
                image.name = f"BaseImage{index}"
                path = Path(directory) / f"fish_{tile_name}_diffuse.png"
                image.save_render(str(path))
                image.filepath = str(path)
                material.node_tree.nodes.active = image_node
                target.data.materials.append(material)
                originals.append(material)
                base_images.append(image)
            for polygon in target.data.polygons:
                polygon.material_index = polygon.index
            original_indices = tuple(
                int(polygon.material_index) for polygon in target.data.polygons
            )
            source_paths = tuple(str(image.filepath) for image in base_images)
            target.active_material_index = 0

            setup = prepare_active_material(target, 32, 32)
            commit_paint_setup(bpy.context.scene, setup)
            reconcile_layer_graph(
                bpy.context.scene,
                material=setup.material,
                overlay=setup.overlay,
            )
            activate_layer_paint_image(
                bpy.context,
                str(bpy.context.scene.projection_paint.layers[0].layer_uuid),
            )

            self.assertEqual(len(target.material_slots), 5)
            self.assertEqual(
                tuple(int(polygon.material_index) for polygon in target.data.polygons),
                (4, 4, 4, 4),
            )
            self.assertEqual(
                tuple(item.value for item in target.data.attributes["pp_original_material_index"].data),
                original_indices,
            )
            for index, original in enumerate(originals):
                self.assertIs(target.material_slots[index].material, original)
            self.assertIs(target.material_slots[4].material, setup.material)
            self.assertEqual(tuple(str(image.filepath) for image in base_images), source_paths)
            self.assertEqual(
                tuple(int(tile.number) for tile in setup.overlay.tiles),
                (1001, 1002, 1011, 1012),
            )
            paint_uv = target.data.uv_layers["PP_UDIM"]
            self.assertEqual(
                tuple(
                    tuple(round(float(value), 4) for value in paint_uv.data[index].uv)
                    for polygon in target.data.polygons
                    for index in (polygon.loop_indices[0],)
                ),
                ((0.0, 0.0), (1.0, 0.0), (0.0, 1.0), (1.0, 1.0)),
            )
            active_node = setup.material.node_tree.nodes.active
            self.assertIsNotNone(active_node)
            self.assertEqual(active_node.type, "TEX_IMAGE")
            self.assertIs(active_node.image, setup.overlay)
            self.assertEqual(active_node.as_pointer(), setup.image_node.as_pointer())
            original_layer = next(
                layer
                for layer in bpy.context.scene.projection_paint.layers
                if layer.is_original_layer
            )
            self.assertEqual(original_layer.name, "Original Layer · Texture")

    def test_material_paint_keeps_layer_stack_display_after_exit(self):
        """Paint 종료 뒤에도 원본 위 Layer Stack과 활성 Paint Image를 유지합니다."""
        target = _new_target("MaterialPaintTarget")
        material, base_image, image_node = _new_textured_material(32, 32)
        material.node_tree.nodes.active = image_node
        target.data.materials.append(material)
        image_paint = bpy.context.scene.tool_settings.image_paint
        original_mode = str(image_paint.mode)
        original_canvas_pointer = (
            image_paint.canvas.as_pointer()
            if image_paint.canvas is not None
            else 0
        )

        session = start_paint_session(bpy.context, target, width=32, height=32)

        self.assertEqual(image_paint.mode, original_mode)
        self.assertEqual(
            image_paint.canvas.as_pointer()
            if image_paint.canvas is not None
            else 0,
            original_canvas_pointer,
        )
        active_node = session.setup.material.node_tree.nodes.active
        self.assertIsNotNone(active_node)
        self.assertEqual(active_node.type, "TEX_IMAGE")
        self.assertIs(active_node.image, session.setup.overlay)
        finish_paint_session(bpy.context)
        self.assertEqual(image_paint.mode, original_mode)
        active_layer_node = session.setup.material.node_tree.nodes.active
        self.assertIsNotNone(active_layer_node)
        self.assertEqual(active_layer_node.type, "TEX_IMAGE")
        self.assertIs(active_layer_node.image, session.setup.overlay)
        self.assertIsNot(active_layer_node.image, base_image)

    def test_shared_data_material_slot_is_rejected_without_mutation(self):
        """공유 Mesh의 DATA Slot이 다른 Object를 바꾸지 않도록 진입을 거부합니다."""
        target = _new_target()
        sibling = bpy.data.objects.new("Sibling", target.data)
        bpy.context.scene.collection.objects.link(sibling)
        material, _base, _node = _new_textured_material()
        target.data.materials.append(material)
        self.assertEqual(target.material_slots[0].link, "DATA")
        with self.assertRaises(PaintSetupError):
            prepare_active_material(target)
        self.assertIs(target.active_material, material)
        self.assertIs(sibling.active_material, material)

    def test_custom_overlay_resolution_overrides_base_image_size(self):
        """UI Custom 선택 시 Base Image 크기 대신 입력 해상도를 사용합니다."""
        target = _new_target()
        material, _base, _node = _new_textured_material()
        target.data.materials.append(material)
        setup = prepare_active_material(
            target,
            96,
            48,
            use_base_resolution=False,
        )
        self.assertEqual(tuple(setup.overlay.size), (96, 48))

    def test_missing_base_image_uses_selected_preset_resolution(self):
        """Base Texture가 없으면 1K·2K·4K·8K 선택 해상도를 사용합니다."""
        settings = bpy.context.scene.projection_paint
        settings.overlay_resolution = "RES_4K"
        self.assertEqual(
            _selected_overlay_resolution(settings),
            (4096, 4096),
        )
        self.assertEqual(
            resolve_overlay_size(None, 4096, 4096, use_base_resolution=True),
            (4096, 4096),
        )
        target = _new_target()
        setup = prepare_active_material(
            target,
            1024,
            1024,
            use_base_resolution=True,
        )
        self.assertEqual(tuple(setup.overlay.size), (1024, 1024))

    def test_resolution_warning_names_every_discarded_layer_resource(self):
        """해상도 경고가 Layer·Mask·보정 삭제와 새 Layer 1을 명시합니다."""
        message = _resolution_reset_message((2048, 2048), (8192, 8192))
        self.assertIn("모든 Layer·Mask·보정 작업을 삭제", message)
        self.assertIn("새 8K Layer 1", message)
        self.assertIn("Blender Undo 기록", message)

    def test_start_session_resolution_change_resets_layer_stack(self):
        """Paint 진입 해상도 변경이 전체 Stack을 새 Layer 1로 교체합니다."""
        target = _new_target("ResolutionResetTarget")
        original = prepare_active_material(
            target,
            32,
            32,
            use_base_resolution=False,
        )
        commit_paint_setup(bpy.context.scene, original)
        reconcile_layer_graph(
            bpy.context.scene,
            material=original.material,
            overlay=original.overlay,
        )
        original.commit()
        settings = bpy.context.scene.projection_paint
        bpy.ops.projection_paint.layer_add()
        for layer in settings.layers:
            bpy.ops.projection_paint.layer_mask_add(layer_uuid=layer.layer_uuid)
            bpy.ops.projection_paint.layer_adjustment_add(
                layer_uuid=layer.layer_uuid,
                adjustment_type="BRIGHTNESS",
            )
        old_names = {
            image.name
            for layer in settings.layers
            for image in (layer.image, layer.mask.image)
        }

        session = start_paint_session(
            bpy.context,
            target,
            width=64,
            height=64,
            use_base_resolution=False,
            replace_existing_overlay=True,
        )

        self.assertEqual(len(settings.layers), 1)
        self.assertEqual(settings.layers[0].name, "Layer 1")
        self.assertEqual(len(settings.layers[0].adjustments), 0)
        self.assertFalse(str(settings.layers[0].mask.mask_uuid or ""))
        self.assertEqual(tuple(settings.layers[0].image.size), (64, 64))
        self.assertIs(session.setup.overlay, settings.layers[0].image)
        session.validate()
        remaining = sorted(name for name in old_names if bpy.data.images.get(name) is not None)
        self.assertEqual(remaining, [])
        validate_active_layer_graph(bpy.context.scene, settings.paint_material)

    def test_object_mode_mask_selection_survives_paint_reentry(self):
        """Object Mode에서 선택한 Mask가 재진입 뒤에도 활성 Paint Image로 유지됩니다."""
        target = _new_target("ObjectModeMaskReentryTarget")
        setup = prepare_active_material(
            target,
            32,
            32,
            use_base_resolution=False,
        )
        commit_paint_setup(bpy.context.scene, setup)
        reconcile_layer_graph(
            bpy.context.scene,
            material=setup.material,
            overlay=setup.overlay,
        )
        setup.commit()
        settings = bpy.context.scene.projection_paint
        layer = settings.layers[0]
        self.assertEqual(
            bpy.ops.projection_paint.layer_mask_add(layer_uuid=str(layer.layer_uuid)),
            {"FINISHED"},
        )
        mask = layer.mask
        activate_mask_paint_image(
            bpy.context,
            str(layer.layer_uuid),
            str(mask.mask_uuid),
        )

        session = start_paint_session(
            bpy.context,
            target,
            width=32,
            height=32,
            use_base_resolution=False,
        )

        self.assertTrue(settings.paint_mask_mode)
        self.assertEqual(str(settings.active_mask_uuid), str(mask.mask_uuid))
        self.assertIs(settings.active_paint_image, mask.image)
        self.assertIs(session.setup.overlay, layer.image)
        finish_paint_session(bpy.context)

    def test_resolution_change_rollback_restores_existing_overlay(self):
        """새 해상도 준비가 실패하면 기존 Overlay와 작업을 복원합니다."""
        target = _new_target()
        original = prepare_active_material(
            target,
            32,
            32,
            use_base_resolution=True,
        )
        original.commit()
        previous = original.overlay
        previous_name = previous.name
        self.assertEqual(
            pending_overlay_resolution_change(target, 64, 64),
            ((32, 32), (64, 64)),
        )
        replacement = prepare_active_material(
            target,
            64,
            64,
            replace_existing_overlay=True,
        )
        created = replacement.overlay
        created_name = created.name
        self.assertIsNot(created, previous)
        self.assertIs(replacement.image_node.image, created)
        replacement.rollback()
        self.assertIs(replacement.image_node.image, previous)
        self.assertIs(bpy.data.images.get(previous_name), previous)
        self.assertIsNone(bpy.data.images.get(created_name))

    def test_confirmed_resolution_change_discards_existing_overlay(self):
        """확정된 해상도 교체가 기존 Overlay를 제거하고 새 Image를 유지합니다."""
        target = _new_target()
        original = prepare_active_material(target, 32, 32)
        original.commit()
        previous = original.overlay
        previous_name = previous.name
        replacement = prepare_active_material(
            target,
            64,
            64,
            replace_existing_overlay=True,
        )
        created = replacement.overlay
        replacement.commit()
        self.assertIs(replacement.image_node.image, created)
        self.assertEqual(tuple(created.size), (64, 64))
        self.assertIsNone(bpy.data.images.get(previous_name))
        self.assertIsNone(
            pending_overlay_resolution_change(target, 64, 64),
        )

    def test_managed_base_texture_keeps_original_resolution(self):
        """원본 Base Image가 있으면 UI 프리셋 변경이 Overlay 교체를 요구하지 않습니다."""
        target = _new_target()
        material, _base, _node = _new_textured_material(64, 32)
        target.data.materials.append(material)
        setup = prepare_active_material(target, 2048, 2048)
        setup.commit()
        self.assertEqual(tuple(setup.overlay.size), (64, 32))
        self.assertIsNone(
            pending_overlay_resolution_change(target, 8192, 8192),
        )

    def test_layer_reset_uses_original_base_texture_resolution(self):
        """원본 해상도 옵션은 현재 UI 크기 대신 Base Texture 크기를 유지합니다."""
        target = _new_target("BaseResolutionResetTarget")
        material, base, _node = _new_textured_material(64, 32)
        target.data.materials.append(material)
        setup = prepare_active_material(
            target,
            8,
            8,
            use_base_resolution=False,
        )
        commit_paint_setup(bpy.context.scene, setup)
        reconcile_layer_graph(
            bpy.context.scene,
            material=setup.material,
            overlay=setup.overlay,
        )
        self.assertEqual(
            tuple(bpy.context.scene.projection_paint.layers[0].image.size),
            (8, 8),
        )

        replacement = replace_with_fresh_layer(
            bpy.context,
            8192,
            8192,
            use_base_resolution=True,
        )

        self.assertEqual(tuple(replacement.size), tuple(base.size))
        self.assertEqual(len(bpy.context.scene.projection_paint.layers), 1)
        self.assertTrue(bpy.context.scene.projection_paint.layers[0].is_original_layer)
        self.assertEqual(
            bpy.context.scene.projection_paint.layers[0].name,
            "Original Layer · Texture",
        )
        validate_active_layer_graph(bpy.context.scene, setup.material)

    def test_uv_only_resolution_reset_creates_one_transparent_layer(self):
        """최초 Image Texture가 없는 Target은 목표 해상도의 빈 Layer 1로 초기화합니다."""
        target = _new_target("UVOnlyResolutionResetTarget")
        setup = prepare_active_material(target, 32, 16, use_base_resolution=False)
        commit_paint_setup(bpy.context.scene, setup)
        reconcile_layer_graph(
            bpy.context.scene,
            material=setup.material,
            overlay=setup.overlay,
        )

        replacement = replace_with_fresh_layer(
            bpy.context,
            64,
            32,
            use_base_resolution=False,
        )

        settings = bpy.context.scene.projection_paint
        self.assertEqual(len(settings.layers), 1)
        self.assertEqual(settings.layers[0].name, "Layer 1")
        self.assertFalse(settings.layers[0].is_original_layer)
        self.assertEqual(tuple(replacement.size), (64, 32))
        self.assertEqual(tuple(float(value) for value in replacement.pixels[:4]), (0.0, 0.0, 0.0, 0.0))
        validate_active_layer_graph(bpy.context.scene, setup.material)

    def test_fixed_base_color_reset_creates_blank_layer(self):
        """고정 Base Color 재질은 Bake하지 않고 UV-only 투명 Layer 1로 초기화합니다."""
        target = _new_target("FixedBaseColorResetTarget")
        material = bpy.data.materials.new("FixedBaseColorMaterial")
        material.use_nodes = True
        principled = next(node for node in material.node_tree.nodes if node.type == "BSDF_PRINCIPLED")
        principled.inputs["Base Color"].default_value = (0.8, 0.2, 0.1, 1.0)
        target.data.materials.append(material)
        setup = prepare_active_material(target, 16, 8, use_base_resolution=False)
        commit_paint_setup(bpy.context.scene, setup)
        reconcile_layer_graph(bpy.context.scene, material=setup.material, overlay=setup.overlay)

        replacement = replace_with_fresh_layer(bpy.context, 32, 16, use_base_resolution=False)

        layer = bpy.context.scene.projection_paint.layers[0]
        self.assertEqual(str(target["pp_udim_kind"]), "UV_ONLY")
        self.assertEqual(layer.name, "Layer 1")
        self.assertFalse(layer.is_original_layer)
        self.assertEqual(tuple(replacement.size), (32, 16))
        self.assertEqual(tuple(replacement.pixels[:4]), (0.0, 0.0, 0.0, 0.0))

    def test_external_texture_graph_failure_preserves_source_and_stack(self):
        """Texture Original 적용 실패는 외부 Source와 기존 Stack을 모두 원상태로 유지합니다."""
        with tempfile.TemporaryDirectory(dir=str(ROOT)) as directory:
            target = _new_target("ExternalGraphRollbackTarget")
            material, source, _node = _new_textured_material(16, 8)
            path = Path(directory) / "external_source.png"
            source.filepath_raw = str(path)
            source.file_format = "PNG"
            source.save()
            target.data.materials.append(material)
            setup = prepare_active_material(target, 8, 8, use_base_resolution=False)
            commit_paint_setup(bpy.context.scene, setup)
            reconcile_layer_graph(bpy.context.scene, material=setup.material, overlay=setup.overlay)
            settings = bpy.context.scene.projection_paint
            stack_before = tuple((str(layer.layer_uuid), layer.image.as_pointer()) for layer in settings.layers)
            source_before = (
                str(source.filepath_raw),
                tuple(source.size),
                tuple(source.pixels[:16]),
                len(tuple(source.packed_files)),
                bool(source.is_dirty),
                path.read_bytes(),
            )

            with mock.patch.object(
                layer_reset,
                "validate_active_layer_graph",
                side_effect=LayerShaderError("주입된 외부 Graph 오류"),
            ):
                with self.assertRaisesRegex(LayerShaderError, "주입된 외부 Graph 오류"):
                    replace_with_fresh_layer(bpy.context, 32, 16, use_base_resolution=False)

            self.assertEqual(
                tuple((str(layer.layer_uuid), layer.image.as_pointer()) for layer in settings.layers),
                stack_before,
            )
            self.assertEqual(
                (
                    str(source.filepath_raw),
                    tuple(source.size),
                    tuple(source.pixels[:16]),
                    len(tuple(source.packed_files)),
                    bool(source.is_dirty),
                    path.read_bytes(),
                ),
                source_before,
            )

    def test_dirty_external_texture_reset_fails_without_source_mutation(self):
        """저장되지 않은 외부 Texture는 저장·Pack하지 않고 기존 Stack과 함께 보존합니다."""
        with tempfile.TemporaryDirectory(dir=str(ROOT)) as directory:
            target = _new_target("DirtyExternalResetTarget")
            material, source, _node = _new_textured_material(8, 8)
            path = Path(directory) / "dirty_external.png"
            source.filepath_raw = str(path)
            source.file_format = "PNG"
            source.save()
            target.data.materials.append(material)
            setup = prepare_active_material(target, 8, 8, use_base_resolution=False)
            commit_paint_setup(bpy.context.scene, setup)
            reconcile_layer_graph(bpy.context.scene, material=setup.material, overlay=setup.overlay)
            source.pixels[0] = 0.75
            settings = bpy.context.scene.projection_paint
            stack_before = tuple((str(layer.layer_uuid), layer.image.as_pointer()) for layer in settings.layers)
            source_before = (
                str(source.filepath_raw), tuple(source.size), tuple(source.pixels[:16]),
                len(tuple(source.packed_files)), bool(source.is_dirty), path.read_bytes(),
            )

            with self.assertRaisesRegex(LayerShaderError, "저장되지 않은 외부"):
                replace_with_fresh_layer(bpy.context, 16, 16, use_base_resolution=False)

            self.assertEqual(
                tuple((str(layer.layer_uuid), layer.image.as_pointer()) for layer in settings.layers),
                stack_before,
            )
            self.assertEqual(
                (
                    str(source.filepath_raw), tuple(source.size), tuple(source.pixels[:16]),
                    len(tuple(source.packed_files)), bool(source.is_dirty), path.read_bytes(),
                ),
                source_before,
            )

    def test_pathless_packed_texture_enters_and_resets_without_source_mutation(self):
        """경로 없는 일반 Packed Texture를 읽기 전용 원본으로 최초 진입과 reset에 사용합니다."""
        with tempfile.TemporaryDirectory(dir=str(ROOT)) as directory:
            target = _new_target("PackedSourceResetTarget")
            material, source, _node = _new_textured_material(8, 4)
            path = Path(directory) / "packed_source.png"
            source.filepath_raw = str(path)
            source.file_format = "PNG"
            source.save()
            source.pack()
            source.filepath_raw = ""
            source.filepath = ""
            target.data.materials.append(material)
            source_before = (
                str(source.filepath_raw), tuple(source.size), tuple(source.pixels[:16]),
                len(tuple(source.packed_files)), bool(source.is_dirty),
            )

            setup = prepare_active_material(target, 8, 4, use_base_resolution=False)
            commit_paint_setup(bpy.context.scene, setup)
            reconcile_layer_graph(bpy.context.scene, material=setup.material, overlay=setup.overlay)
            replacement = replace_with_fresh_layer(
                bpy.context,
                4,
                2,
                use_base_resolution=False,
            )

            settings = bpy.context.scene.projection_paint
            self.assertEqual(len(settings.layers), 1)
            self.assertTrue(settings.layers[0].is_original_layer)
            self.assertEqual(tuple(replacement.size), (4, 2))
            self.assertIs(settings.original_source_image, source)
            self.assertEqual(
                (
                    str(source.filepath_raw), tuple(source.size), tuple(source.pixels[:16]),
                    len(tuple(source.packed_files)), bool(source.is_dirty),
                ),
                source_before,
            )

    def test_missing_texture_provenance_keeps_existing_stack(self):
        """Texture Source 기록이 유실되면 빈 Layer로 폴백하지 않고 기존 Stack을 유지합니다."""
        target = _new_target("MissingOriginalSourceTarget")
        material, source, _node = _new_textured_material(16, 8)
        target.data.materials.append(material)
        with tempfile.TemporaryDirectory(dir=str(ROOT)) as directory:
            source_path = Path(directory) / "missing_original.png"
            source.filepath_raw = str(source_path)
            source.file_format = "PNG"
            source.save()
            setup = prepare_active_material(target, 16, 8, use_base_resolution=False)
            commit_paint_setup(bpy.context.scene, setup)
            reconcile_layer_graph(
                bpy.context.scene,
                material=setup.material,
                overlay=setup.overlay,
            )
            settings = bpy.context.scene.projection_paint
            before = tuple(
                (str(layer.layer_uuid), layer.image.as_pointer())
                for layer in settings.layers
            )
            settings.original_source_image = None
            source_path.unlink()

            with self.assertRaisesRegex(LayerShaderError, "Source 기록이 유실"):
                replace_with_fresh_layer(
                    bpy.context,
                    32,
                    16,
                    use_base_resolution=False,
                )

            self.assertEqual(
                tuple(
                    (str(layer.layer_uuid), layer.image.as_pointer())
                    for layer in settings.layers
                ),
                before,
            )
            validate_active_layer_graph(bpy.context.scene, setup.material)

    def test_resolution_reset_is_blocked_for_every_composite_state(self):
        """NONE 이외 Composite 상태에서는 Layer 초기화를 시작하지 않습니다."""
        target = _new_target("CompositeBlockedResolutionResetTarget")
        setup = prepare_active_material(target, 16, 8, use_base_resolution=False)
        commit_paint_setup(bpy.context.scene, setup)
        reconcile_layer_graph(
            bpy.context.scene,
            material=setup.material,
            overlay=setup.overlay,
        )
        settings = bpy.context.scene.projection_paint
        before = tuple(layer.image.as_pointer() for layer in settings.layers)

        for state in ("CREATING", "RECOMPOSITING", "ACTIVE", "RELEASING", "STALE"):
            settings.composite_state = state
            with self.assertRaisesRegex(LayerShaderError, "Composite를 먼저 해제"):
                replace_with_fresh_layer(
                    bpy.context,
                    32,
                    16,
                    use_base_resolution=False,
                )
            self.assertEqual(
                tuple(layer.image.as_pointer() for layer in settings.layers),
                before,
            )
        settings.composite_state = "NONE"

    def test_paint_snapshot_disables_and_restores_camera_background(self):
        """Paint 진입 중 Camera Background를 끄고 종료 시 원래 토글을 복구합니다."""
        target = _new_target()
        settings = bpy.context.scene.projection_paint
        settings.review_background_enabled = True
        _capture_snapshot(bpy.context, target)
        self.assertFalse(settings.review_background_enabled)
        _restore_snapshot(bpy.context, None)
        self.assertTrue(settings.review_background_enabled)

    def test_material_contract_rejects_active_uv_change(self):
        """세션 중 활성 UV가 바뀌면 기존 Paint Image 사용을 중지합니다."""
        target = _new_target()
        material, _base, _node = _new_textured_material()
        target.data.materials.append(material)
        setup = prepare_active_material(target)
        target.data.uv_layers.new(name="ChangedUV")
        target.data.uv_layers.active_index = len(target.data.uv_layers) - 1
        with self.assertRaises(PaintSetupError):
            validate_material_contract(setup)

    def test_legacy_file_overlay_is_adopted_as_single_without_replacement(self):
        """구형 FILE Stack은 전체 검증 뒤 같은 Image Pointer로 SINGLE을 채택합니다."""
        target, material, settings, source = _prepare_recoverable_file_overlay(
            "RecoverableFileOverlay"
        )
        old_pointer = source.as_pointer()
        expected_pixels = tuple(float(value) for value in source.pixels[:])
        classification = classify_managed_overlay(target, source)
        self.assertEqual(classification.state, MANAGED_OVERLAY_LEGACY_VALID)
        setup_from_scene_state(bpy.context.scene)

        result = overlay_migration.repair_managed_overlay_if_needed(bpy.context)

        self.assertFalse(result.changed)
        self.assertEqual(result.image.as_pointer(), old_pointer)
        self.assertEqual(result.image.source, "FILE")
        self.assertEqual(target[IMAGE_BACKEND_KEY], "SINGLE")
        self.assertIs(settings.layers[0].image, source)
        self.assertIs(settings.overlay_image, source)
        self.assertIs(settings.active_paint_image, source)
        for expected, actual in zip(expected_pixels, source.pixels[:]):
            self.assertAlmostEqual(float(actual), expected, delta=1.0 / 255.0)
        setup_from_scene_state(bpy.context.scene)
        validate_active_layer_graph(bpy.context.scene, material)

    def test_legacy_uv_only_file_stack_is_adopted_as_single(self):
        """구형 UV_ONLY FILE Stack도 픽셀 교체 없이 SINGLE 백엔드를 채택합니다."""
        target, material, settings, source = _prepare_recoverable_file_overlay(
            "LegacyUvOnlyFileOverlay"
        )
        target["pp_udim_kind"] = "UV_ONLY"
        old_pointer = source.as_pointer()

        result = overlay_migration.repair_managed_overlay_if_needed(bpy.context)

        self.assertFalse(result.changed)
        self.assertEqual(result.image.as_pointer(), old_pointer)
        self.assertEqual(target[IMAGE_BACKEND_KEY], "SINGLE")
        self.assertIs(settings.layers[0].image, source)
        validate_active_layer_graph(bpy.context.scene, material)

    def test_explicit_single_overlay_skips_legacy_migration(self):
        """명시 SINGLE FILE Overlay는 Tiled 자동 변환 없이 같은 Pointer를 유지합니다."""
        target = _new_target("ExplicitSingleMigrationTarget")
        material, _source, _node = _new_textured_material(8, 4)
        target.data.materials.append(material)
        setup = prepare_active_material(target, 8, 4, use_base_resolution=False)
        commit_paint_setup(bpy.context.scene, setup)
        reconcile_layer_graph(
            bpy.context.scene,
            material=setup.material,
            overlay=setup.overlay,
        )
        before = setup.overlay.as_pointer()

        result = overlay_migration.repair_managed_overlay_if_needed(bpy.context)

        self.assertFalse(result.changed)
        self.assertEqual(result.image.as_pointer(), before)
        self.assertEqual(result.image.source, "FILE")

    def test_legacy_tiled_overlay_skips_backend_conversion(self):
        """백엔드 기록 없는 TILED Overlay는 Image와 Target 메타데이터를 바꾸지 않습니다."""
        target = _new_target("LegacyTiledMigrationTarget")
        material, generated, image_node = _new_textured_material(8, 4)
        source = new_managed_paint_image(
            UDIM_BACKEND,
            "LegacyTiledSource",
            (8, 4),
            (1001,),
            (0.25, 0.5, 0.75, 1.0),
        )
        image_node.image = source
        bpy.data.images.remove(generated)
        target.data.materials.append(material)
        setup = prepare_active_material(target, 8, 4, use_base_resolution=False)
        commit_paint_setup(bpy.context.scene, setup)
        reconcile_layer_graph(
            bpy.context.scene,
            material=setup.material,
            overlay=setup.overlay,
        )
        del target[IMAGE_BACKEND_KEY]
        before = setup.overlay.as_pointer()

        result = overlay_migration.repair_managed_overlay_if_needed(bpy.context)

        self.assertFalse(result.changed)
        self.assertEqual(result.image.as_pointer(), before)
        self.assertEqual(result.image.source, "TILED")
        self.assertNotIn(IMAGE_BACKEND_KEY, target)

    def test_dirty_legacy_file_overlay_is_adopted_without_pixel_copy(self):
        """Dirty 구형 FILE Stack도 Pixel과 Dirty 상태를 바꾸지 않고 SINGLE로 채택합니다."""
        target, material, settings, source = _prepare_recoverable_file_overlay(
            "DirtyRecoverableFileOverlay",
            dirty=True,
        )
        expected = tuple(float(value) for value in source.pixels[:])

        result = overlay_migration.repair_managed_overlay_if_needed(bpy.context)

        self.assertFalse(result.changed)
        self.assertTrue(result.image.is_dirty)
        self.assertEqual(target[IMAGE_BACKEND_KEY], "SINGLE")
        self.assertIs(settings.layers[0].image, result.image)
        for before, after in zip(expected, result.image.pixels[:]):
            self.assertAlmostEqual(float(after), before, delta=1.0 / 255.0)
        validate_active_layer_graph(bpy.context.scene, material)

    def test_legacy_single_adoption_failure_restores_backend_and_references(self):
        """SINGLE 채택 후 Graph 검증 실패는 백엔드 기록과 모든 Pointer를 복원합니다."""
        target, material, settings, source = _prepare_recoverable_file_overlay(
            "RecoverableFileRollback"
        )
        old_pointer = source.as_pointer()
        image_pointers = {image.as_pointer() for image in bpy.data.images}

        with mock.patch.object(
            layer_shader,
            "validate_active_layer_graph",
            side_effect=LayerShaderError("주입된 SINGLE 검증 실패"),
        ):
            with self.assertRaisesRegex(
                LayerShaderError,
                "주입된 SINGLE 검증 실패",
            ):
                overlay_migration.repair_managed_overlay_if_needed(bpy.context)

        self.assertNotIn(IMAGE_BACKEND_KEY, target)
        self.assertEqual(source.as_pointer(), old_pointer)
        self.assertEqual(source.source, "FILE")
        self.assertIs(settings.layers[0].image, source)
        self.assertIs(settings.overlay_image, source)
        self.assertIs(settings.active_paint_image, source)
        self.assertEqual(
            {image.as_pointer() for image in bpy.data.images},
            image_pointers,
        )
        self.assertTrue(
            any(
                getattr(node, "image", None) is source
                for node in material.node_tree.nodes
            )
        )

    def test_legacy_file_overlay_with_unknown_user_is_not_adopted(self):
        """관리 범위 밖 Texture가 구형 FILE을 공유하면 SINGLE 채택을 중단합니다."""
        target, _material, settings, source = _prepare_recoverable_file_overlay(
            "SharedRecoverableFileOverlay"
        )
        unexpected = bpy.data.textures.new("UnexpectedOverlayUser", type="IMAGE")
        unexpected.image = source
        old_pointer = source.as_pointer()
        try:
            with self.assertRaisesRegex(
                overlay_migration.OverlayMigrationError,
                "관리 범위 밖",
            ):
                overlay_migration.repair_managed_overlay_if_needed(bpy.context)
        finally:
            bpy.data.textures.remove(unexpected)

        self.assertEqual(source.as_pointer(), old_pointer)
        self.assertEqual(source.source, "FILE")
        self.assertIs(settings.layers[0].image, source)
        self.assertNotIn(IMAGE_BACKEND_KEY, target)

    def test_overlay_pack_and_sidecar_are_verified(self):
        """Dirty Overlay를 Sidecar와 Packed 데이터로 함께 확정합니다."""
        target = _new_target()
        material, _base, _node = _new_textured_material()
        target.data.materials.append(material)
        setup = prepare_active_material(target)
        pixels = list(setup.overlay.pixels)
        pixels[:4] = (0.25, 0.5, 0.75, 1.0)
        setup.overlay.pixels[:] = pixels
        setup.overlay.update()
        mark_overlay_dirty(setup.overlay)
        with tempfile.TemporaryDirectory() as directory:
            record = ensure_overlay_durable(
                setup.overlay,
                scene=bpy.context.scene,
                recovery_root=directory,
            )
            self.assertIsNotNone(setup.overlay.packed_file)
            self.assertFalse(setup.overlay.is_dirty)
            self.assertFalse(record.recovery_path)
            verify_registered_overlays()
            self.assertEqual(len(registered_overlays()), 1)
            overlay_name = setup.overlay.name
            blend_path = str(Path(directory) / "packed_overlay.blend")
            bpy.ops.wm.save_as_mainfile(filepath=blend_path)
            bpy.ops.wm.open_mainfile(filepath=blend_path)
            restored = bpy.data.images.get(overlay_name)
            self.assertIsNotNone(restored)
            self.assertIsNotNone(restored.packed_file)
            restored_pixel = tuple(float(value) for value in restored.pixels[:4])
            self.assertAlmostEqual(restored_pixel[0], 0.25, delta=1.0 / 255.0)
            self.assertAlmostEqual(restored_pixel[1], 0.5, delta=1.0 / 255.0)
            self.assertAlmostEqual(restored_pixel[2], 0.75, delta=1.0 / 255.0)
            self.assertAlmostEqual(restored_pixel[3], 1.0, delta=1.0 / 255.0)

    def test_pack_failure_sidecar_checksum_rejects_corruption(self):
        """Pack 실패 Sidecar가 손상되면 Checksum 검증으로 복구를 거부합니다."""
        target = _new_target()
        material, _base, _node = _new_textured_material()
        target.data.materials.append(material)
        setup = prepare_active_material(target)
        pixels = list(setup.overlay.pixels)
        pixels[:4] = (0.1, 0.2, 0.3, 1.0)
        setup.overlay.pixels[:] = pixels
        setup.overlay.update()
        mark_overlay_dirty(setup.overlay)
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(
                durability,
                "_pack_image",
                side_effect=RuntimeError("강제 Pack 실패"),
            ):
                with self.assertRaises(OverlayDurabilityError):
                    ensure_overlay_durable(
                        setup.overlay,
                        scene=bpy.context.scene,
                        recovery_root=directory,
                    )
            sidecar = Path(setup.overlay[durability.RECOVERY_PATH_KEY])
            self.assertTrue(sidecar.is_file())
            self.assertTrue(setup.overlay[durability.RECOVERY_SHA256_KEY])
            sidecar.write_bytes(b"corrupted")
            with self.assertRaises(OverlayDurabilityError):
                recover_overlay_from_sidecar(setup.overlay, sidecar)

    def test_single_sidecar_restores_pixels_without_changing_image_path(self):
        """SINGLE Sidecar가 기존 Staging 경로를 유지하며 FILE Pixel을 복구합니다."""
        target = _new_target("SingleRecoveryTarget")
        setup = prepare_active_material(target, 8, 8, use_base_resolution=False)
        self.assertEqual(setup.overlay.source, "FILE")
        pixels = list(setup.overlay.pixels)
        pixels[:4] = (0.1, 0.2, 0.3, 1.0)
        setup.overlay.pixels[:] = pixels
        setup.overlay.update()
        mark_overlay_dirty(setup.overlay)
        original_filepath = str(setup.overlay.filepath)
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(
                durability,
                "_pack_image",
                side_effect=RuntimeError("강제 Pack 실패"),
            ):
                with self.assertRaises(OverlayDurabilityError):
                    ensure_overlay_durable(
                        setup.overlay,
                        scene=bpy.context.scene,
                        recovery_root=directory,
                    )
            self.assertEqual(
                Path(setup.overlay[durability.RECOVERY_PATH_KEY]).suffix,
                ".png",
            )
            changed = list(setup.overlay.pixels)
            changed[:4] = (0.9, 0.8, 0.7, 1.0)
            setup.overlay.pixels[:] = changed
            setup.overlay.update()

            recover_overlay_from_sidecar(setup.overlay)

            restored = tuple(float(value) for value in setup.overlay.pixels[:4])
            self.assertAlmostEqual(restored[0], 0.1, delta=1.0 / 255.0)
            self.assertAlmostEqual(restored[1], 0.2, delta=1.0 / 255.0)
            self.assertAlmostEqual(restored[2], 0.3, delta=1.0 / 255.0)
            self.assertAlmostEqual(restored[3], 1.0, delta=1.0 / 255.0)
            self.assertEqual(str(setup.overlay.filepath), original_filepath)

    def test_udim_manifest_restores_all_tiles_without_backend_conversion(self):
        """UDIM Manifest가 TILED 백엔드와 전체 타일을 유지하며 Pixel을 복구합니다."""
        target = _new_target("UdimRecoveryTarget")
        target[IMAGE_BACKEND_KEY] = "UDIM"
        target["pp_udim_tiles"] = [1001]
        image = layer_shader._new_image(
            target,
            "UdimRecoveryImage",
            (8, 8),
            role=layer_shader.LAYER_IMAGE_ROLE,
            layer_uuid="udim-recovery-layer",
            color=(0.0, 0.0, 0.0, 1.0),
        )
        expected = {}
        for index, tile in enumerate(image.tiles):
            image.tiles.active_index = index
            color = (0.1 + index * 0.2, 0.2, 0.3, 1.0)
            pixels = list(image.pixels)
            pixels[:4] = color
            image.pixels[:] = pixels
            image.update()
            expected[int(tile.number)] = color
        mark_overlay_dirty(image)
        original_filepath = str(image.filepath)
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(
                durability,
                "_pack_image",
                side_effect=RuntimeError("강제 Pack 실패"),
            ):
                with self.assertRaises(OverlayDurabilityError):
                    ensure_overlay_durable(
                        image,
                        scene=bpy.context.scene,
                        recovery_root=directory,
                    )
            self.assertEqual(
                Path(image[durability.RECOVERY_PATH_KEY]).suffixes,
                [".udim", ".json"],
            )
            for index, _tile in enumerate(image.tiles):
                image.tiles.active_index = index
                changed = list(image.pixels)
                changed[:4] = (0.9, 0.8, 0.7, 1.0)
                image.pixels[:] = changed
                image.update()

            recover_overlay_from_sidecar(image)

            self.assertEqual(image.source, "TILED")
            self.assertEqual(tuple(int(tile.number) for tile in image.tiles), (1001,))
            for index, tile in enumerate(image.tiles):
                image.tiles.active_index = index
                restored = tuple(float(value) for value in image.pixels[:4])
                for actual, value in zip(restored, expected[int(tile.number)]):
                    self.assertAlmostEqual(actual, value, delta=1.0 / 255.0)
            self.assertEqual(str(image.filepath), original_filepath)

    def test_blender_contract_fails_closed_after_uv_change(self):
        """활성 UV 변경 뒤 Blender 권위 계약이 STALE로 판정되는지 확인합니다."""
        target = _new_target()
        material, _base, _node = _new_textured_material()
        target.data.materials.append(material)
        session = start_paint_session(bpy.context, target, width=64, height=32)
        target.data.uv_layers.new(name="ChangedUV")
        target.data.uv_layers.active_index = len(target.data.uv_layers) - 1
        with self.assertRaises(PaintStateError):
            session.validate()
        self.assertEqual(derive_paint_state(bpy.context.scene)[0], "STALE")

    def test_blender_contract_fails_closed_after_material_slot_removal(self):
        """관리 Material Slot 삭제를 예외 누출 없이 STALE로 판정합니다."""
        target = _new_target()
        material, _base, _node = _new_textured_material()
        target.data.materials.append(material)
        session = start_paint_session(bpy.context, target, width=64, height=32)
        target.data.materials.clear()
        with self.assertRaises(PaintStateError):
            session.validate()
        self.assertEqual(derive_paint_state(bpy.context.scene)[0], "STALE")


if __name__ == "__main__":
    unittest.main(argv=[__file__])
