"""Blender Background에서 Paint Layer Shader Graph와 활성 Image를 검증합니다."""

from __future__ import annotations

from array import array
from itertools import combinations
import inspect
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

import bpy


ROOT = Path(__file__).resolve().parents[3] / "tools" / "projection_paint"
sys.path.insert(0, str(ROOT.parent))

from tools import projection_paint as addon  # noqa: E402
from tools.projection_paint.blender.layer_shader import (  # noqa: E402
    ADJUSTMENT_APPLY_MIX_NODE_ROLE,
    ADJUSTMENT_MASK_BW_NODE_ROLE,
    ADJUSTMENT_MASK_IMAGE_NODE_ROLE,
    ADJUSTMENT_NODE_ROLE,
    ADJUSTMENT_UUID_KEY,
    LAYER_FRAME_ROLE,
    LAYER_IMAGE_NODE_ROLE,
    LAYER_MIX_NODE_ROLE,
    LAYER_PREVIEW_UUID_KEY,
    LAYER_UUID_KEY,
    LayerShaderError,
    MASK_BW_NODE_ROLE,
    MASK_IMAGE_NODE_ROLE,
    MASK_PREVIEW_UUID_KEY,
    MASK_UUID_KEY,
    managed_node_rectangles,
    reconcile_layer_graph,
    validate_active_layer_graph,
    activate_layer_paint_image,
    activate_mask_paint_image,
    add_layer_transaction,
    validate_layer_graph,
)
from tools.projection_paint.blender.layer_reset import replace_with_fresh_layer  # noqa: E402
from tools.projection_paint.blender import layer_reset  # noqa: E402
from tools.projection_paint.blender import layer_shader  # noqa: E402
from tools.projection_paint.blender.image_backend import (  # noqa: E402
    SINGLE_BACKEND,
    UDIM_BACKEND,
    image_contract,
    new_managed_paint_image,
)
from tools.projection_paint.blender.material_setup import (  # noqa: E402
    prepare_active_material,
)
from tools.projection_paint.blender.overlay_durability import (  # noqa: E402
    authoritative_overlays,
    rebuild_overlay_registry,
)
from tools.projection_paint.blender.ownership import (  # noqa: E402
    OWNER_KEY,
    OWNER_TOKEN,
    RECORD_KIND_KEY,
)
from tools.projection_paint.blender.paint_state import (  # noqa: E402
    commit_paint_setup,
    validate_paint_contract,
)
from tools.projection_paint.blender.stencil_session import (  # noqa: E402
    stage_stencil_source,
    start_stencil_session,
)


def _new_target(name: str = "LayerTarget"):
    """UV와 한 면 및 Principled Material을 가진 Paint Target을 생성합니다."""
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
    return target


def _prepare_backend(name: str = "LayerTarget"):
    """단일 Overlay를 첫 Layer Shader Graph로 승격해 반환합니다."""
    scene = bpy.context.scene
    target = _new_target(name)
    setup = prepare_active_material(
        target,
        64,
        32,
        use_base_resolution=False,
    )
    commit_paint_setup(scene, setup)
    reconcile_layer_graph(
        scene,
        material=setup.material,
        overlay=setup.overlay,
    )
    setup.commit()
    return target, setup


def _validate_active_graph(material):
    """현재 Scene 상태에 맞는 활성 Layer 출력 Chain을 검증합니다."""
    validate_active_layer_graph(bpy.context.scene, material)


def _owned_nodes(material, role: str):
    """Material에서 지정한 애드온 역할의 Node만 반환합니다."""
    return tuple(
        node
        for node in material.node_tree.nodes
        if node.get(OWNER_KEY) == OWNER_TOKEN
        and node.get(RECORD_KIND_KEY) == role
    )


def _layer_mix(material, layer_uuid: str):
    """Layer UUID에 대응하는 유일한 Mix Node를 반환합니다."""
    matches = tuple(
        node
        for node in _owned_nodes(material, LAYER_MIX_NODE_ROLE)
        if str(node.get(LAYER_UUID_KEY, "")) == str(layer_uuid)
    )
    if len(matches) != 1:
        raise AssertionError(f"Layer Mix Node 수가 잘못되었습니다: {len(matches)}")
    return matches[0]


def _layer_image_node(material, layer_uuid: str):
    """Layer UUID에 연결된 관리 Image Texture 노드를 반환합니다."""
    return next(
        node
        for node in material.node_tree.nodes
        if node.get(LAYER_UUID_KEY, "") == str(layer_uuid)
        and node.get(RECORD_KIND_KEY, "") == LAYER_IMAGE_NODE_ROLE
    )


def _normal_layers(settings):
    """Original을 제외한 사용자가 편집하는 일반 Layer만 반환합니다."""
    return tuple(layer for layer in settings.layers if not layer.is_original_layer)


def _original_layer(settings):
    """실제 최하단 Original Layer를 반환합니다."""
    return next(layer for layer in settings.layers if layer.is_original_layer)


def _layer_uuids(settings):
    """현재 Stack 전체 Layer UUID를 UI 순서대로 반환합니다."""
    return tuple(str(layer.layer_uuid) for layer in settings.layers)


def _fill_image(image, color: tuple[float, float, float, float]) -> None:
    """Image 전체 Pixel을 지정한 선형 RGBA 값으로 채웁니다."""
    image.colorspace_settings.name = "Non-Color"
    pixel_count = int(image.size[0]) * int(image.size[1])
    image.pixels.foreach_set(array("f", color) * pixel_count)
    image.update()


def _render_center_color(target, material) -> tuple[float, float, float, float]:
    """Layer Stack 출력을 Emission으로 렌더링해 중앙 Pixel을 반환합니다."""
    scene = bpy.context.scene
    camera_data = bpy.data.cameras.new("LayerRenderCameraData")
    camera = bpy.data.objects.new("LayerRenderCamera", camera_data)
    scene.collection.objects.link(camera)
    camera.location = (0.75, 0.25, 5.0)
    camera_data.type = "ORTHO"
    camera_data.ortho_scale = 0.3
    scene.camera = camera
    scene.render.engine = "BLENDER_EEVEE"
    scene.render.resolution_x = 16
    scene.render.resolution_y = 16
    scene.render.resolution_percentage = 100
    scene.render.film_transparent = True
    tree = material.node_tree
    principled = next(node for node in tree.nodes if node.type == "BSDF_PRINCIPLED")
    final_color = principled.inputs["Base Color"].links[0].from_socket
    output = next(
        node
        for node in tree.nodes
        if node.type == "OUTPUT_MATERIAL" and bool(node.is_active_output)
    )
    emission = tree.nodes.new("ShaderNodeEmission")
    tree.links.new(final_color, emission.inputs["Color"])
    tree.links.new(emission.outputs["Emission"], output.inputs["Surface"])
    bpy.context.view_layer.objects.active = target
    scene.render.image_settings.file_format = "OPEN_EXR"
    scene.render.image_settings.color_mode = "RGBA"
    scene.render.image_settings.color_depth = "32"
    with tempfile.TemporaryDirectory() as directory:
        path = str(Path(directory) / "layer_result.exr")
        scene.render.filepath = path
        bpy.ops.render.render(write_still=True)
        result = bpy.data.images.load(path, check_existing=False)
        try:
            values = array("f", [0.0]) * len(result.pixels)
            result.pixels.foreach_get(values)
            center = (8 * 16 + 8) * 4
            return tuple(float(value) for value in values[center : center + 4])
        finally:
            bpy.data.images.remove(result)


def _prepare_layer_stencil_resume():
    """Camera View 복구 조건을 흉내 낼 Layer·Mask·Stencil 상태를 준비합니다."""
    target, setup = _prepare_backend("LayerStencilResumeTarget")
    scene = bpy.context.scene
    settings = scene.projection_paint
    layer = settings.layers[0]
    bpy.ops.projection_paint.layer_mask_add(layer_uuid=layer.layer_uuid)
    brush = bpy.data.brushes.new("Layer Stencil Resume Brush", mode="TEXTURE_PAINT")
    bpy.context.view_layer.objects.active = target
    target.select_set(True)
    bpy.ops.object.mode_set(mode="TEXTURE_PAINT")
    activate_mask_paint_image(
        bpy.context,
        str(layer.layer_uuid),
        str(layer.mask.mask_uuid),
    )
    stencil = start_stencil_session(
        bpy.context,
        dataset_uuid="layer-stencil-resume",
        brush_override=brush,
    )
    source = bpy.data.images.new("LayerStencilResumeSource", 64, 32, alpha=True)
    image_id = 701
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
    runtime = SimpleNamespace(
        scene=scene,
        entries={
            int(scene.frame_current): SimpleNamespace(image_id=image_id),
        },
        viewport_token=None,
    )
    return target, setup, layer, stencil, runtime


class LayerShaderTests(unittest.TestCase):
    """Layer Image, Shader Chain, Mask 선택과 Node 배치를 검증합니다."""

    def test_original_duplicate_resamples_before_single_final_pack(self) -> None:
        """Original 복제는 목표 해상도 리샘플 뒤 최종 Pack을 한 번만 수행합니다."""
        source = inspect.getsource(layer_shader._duplicate_file_backed_original)
        self.assertEqual(source.count("image.pack()"), 1)
        self.assertNotIn("working.pack(", source)
        self.assertNotIn("working.save(", source)
        self.assertLess(source.index("working.scale("), source.index("image.pack()"))
        self.assertLess(source.index("temporary.scale("), source.index("image.pack()"))

    @classmethod
    def setUpClass(cls) -> None:
        """Layer PropertyGroup과 Shader 연산자를 포함한 Extension을 등록합니다."""
        addon.register()

    @classmethod
    def tearDownClass(cls) -> None:
        """테스트 Process에서 Extension 등록을 해제합니다."""
        addon.unregister()

    def setUp(self) -> None:
        """각 테스트를 빈 Factory Scene과 Runtime Registry로 시작합니다."""
        bpy.ops.wm.read_factory_settings(use_empty=True)

    def test_single_overlay_is_promoted_without_pixel_image_copy(self) -> None:
        """기존 Overlay 자체가 첫 Layer Image와 활성 Paint Image가 됩니다."""
        _target, setup = _prepare_backend()
        settings = bpy.context.scene.projection_paint
        self.assertEqual(len(settings.layers), 2)
        self.assertIs(settings.layers[0].image, setup.overlay)
        self.assertTrue(settings.layers[-1].is_original_layer)
        self.assertIs(settings.overlay_image, setup.overlay)
        self.assertIs(settings.active_paint_image, setup.overlay)
        self.assertEqual(
            len(_owned_nodes(setup.material, LAYER_FRAME_ROLE)),
            2,
        )
        self.assertEqual(
            len(_owned_nodes(setup.material, LAYER_IMAGE_NODE_ROLE)),
            2,
        )
        _validate_active_graph(setup.material)
        validate_paint_contract(bpy.context.scene)

    def test_single_stack_creates_file_packed_layer_and_masks(self) -> None:
        """SINGLE Target의 Layer·Layer Mask·Adjustment Mask가 모두 FILE·Packed입니다."""
        _target, _setup = _prepare_backend("SingleLayerContractTarget")
        settings = bpy.context.scene.projection_paint
        self.assertEqual(bpy.ops.projection_paint.layer_add(), {"FINISHED"})
        layer = settings.layers[0]
        self.assertEqual(
            bpy.ops.projection_paint.layer_mask_add(layer_uuid=layer.layer_uuid),
            {"FINISHED"},
        )
        self.assertEqual(
            bpy.ops.projection_paint.layer_adjustment_add(
                layer_uuid=layer.layer_uuid,
                adjustment_type="SATURATION",
            ),
            {"FINISHED"},
        )
        adjustment = layer.adjustments[0]
        self.assertEqual(
            bpy.ops.projection_paint.adjustment_mask_add(
                layer_uuid=layer.layer_uuid,
                adjustment_uuid=adjustment.adjustment_uuid,
            ),
            {"FINISHED"},
        )
        images = tuple(item.image for item in settings.layers) + (
            layer.mask.image,
            adjustment.mask.image,
        )
        for image in images:
            self.assertEqual(image_contract(image).backend, SINGLE_BACKEND)
            self.assertEqual(image.source, "FILE")
            self.assertIsNotNone(image.packed_file)

    def test_mixed_backend_reconcile_preserves_existing_stack(self) -> None:
        """SINGLE Stack에 UDIM Image가 섞이면 Pointer를 바꾸지 않고 재연결을 거부합니다."""
        target, setup = _prepare_backend("MixedLayerContractTarget")
        settings = bpy.context.scene.projection_paint
        layer = settings.layers[0]
        replacement = new_managed_paint_image(
            UDIM_BACKEND,
            "MixedBackendLayer",
            tuple(int(value) for value in layer.image.size),
            (1001,),
            (0.0, 0.0, 0.0, 0.0),
        )
        for key in layer.image.keys():
            replacement[key] = layer.image[key]
        layer.image = replacement
        before = tuple(item.image.as_pointer() for item in settings.layers)

        with self.assertRaisesRegex(LayerShaderError, "계약"):
            reconcile_layer_graph(
                bpy.context.scene,
                material=setup.material,
                overlay=replacement,
            )

        self.assertEqual(
            tuple(item.image.as_pointer() for item in settings.layers),
            before,
        )
        self.assertEqual(target.get("pp_image_backend"), SINGLE_BACKEND)

    def test_udim_target_creates_complete_layer_and_mask_tiles(self) -> None:
        """UDIM Target의 Layer와 Mask Image가 전체 TILED 타일 계약을 공유합니다."""
        target = _new_target("UdimLayerContractTarget")
        target["pp_image_backend"] = UDIM_BACKEND
        target["pp_udim_tiles"] = [1001, 1002]
        images = (
            layer_shader._new_image(
                target,
                "UdimLayer",
                (16, 8),
                role=layer_shader.LAYER_IMAGE_ROLE,
                layer_uuid="udim-layer",
                color=(0.0, 0.0, 0.0, 0.0),
            ),
            layer_shader._new_image(
                target,
                "UdimMask",
                (16, 8),
                role=layer_shader.MASK_IMAGE_ROLE,
                layer_uuid="udim-layer",
                mask_uuid="udim-mask",
                color=(1.0, 1.0, 1.0, 1.0),
            ),
        )
        for image in images:
            contract = image_contract(image)
            self.assertEqual(contract.backend, UDIM_BACKEND)
            self.assertEqual(contract.tile_numbers, (1001, 1002))
            self.assertEqual(contract.tile_sizes, ((16, 8), (16, 8)))
            self.assertEqual(image.source, "TILED")
            self.assertEqual(len(tuple(image.packed_files)), 2)

    def test_legacy_mask_item_migrates_without_image_or_uuid_loss(self) -> None:
        """구형 MASK Adjustment가 Layer Mask로 무손실 이전됩니다."""
        _target, setup = _prepare_backend("LegacyLayerMaskTarget")
        settings = bpy.context.scene.projection_paint
        layer = settings.layers[0]
        legacy_image = new_managed_paint_image(
            SINGLE_BACKEND,
            "LegacyMask",
            (64, 32),
            (1001,),
            (1.0, 1.0, 1.0, 1.0),
        )
        legacy_image[OWNER_KEY] = OWNER_TOKEN
        legacy_image[RECORD_KIND_KEY] = "MASK_PERSISTENT"
        legacy_image["pp_uuid"] = "legacy-mask-image"
        legacy = layer.adjustments.add()
        legacy.adjustment_type = "MASK"
        legacy.adjustment_uuid = "legacy-layer-mask"
        legacy.image = legacy_image
        legacy.enabled = False
        layer.active_adjustment_index = 0
        settings.active_paint_image = legacy_image
        settings.paint_mask_mode = True
        settings.mask_preview_adjustment_uuid = "legacy-layer-mask"

        reconcile_layer_graph(bpy.context.scene)

        self.assertEqual(len(layer.adjustments), 0)
        self.assertEqual(layer.mask.mask_uuid, "legacy-layer-mask")
        self.assertIs(layer.mask.image, legacy_image)
        self.assertFalse(layer.mask.enabled)
        self.assertEqual(settings.active_mask_uuid, "legacy-layer-mask")
        self.assertEqual(
            settings.mask_preview_adjustment_uuid,
            "legacy-layer-mask",
        )
        self.assertEqual(
            len(_owned_nodes(setup.material, MASK_IMAGE_NODE_ROLE)),
            1,
        )

    def test_reconcile_restores_mask_selection_from_active_image(self) -> None:
        """재연결이 활성 Mask Image를 트리의 유일 선택으로 복구합니다."""
        _target, _setup = _prepare_backend("MaskActiveImageSelectionTarget")
        scene = bpy.context.scene
        settings = scene.projection_paint
        layer = settings.layers[0]
        bpy.ops.projection_paint.layer_mask_add(layer_uuid=layer.layer_uuid)
        bpy.ops.projection_paint.layer_adjustment_add(
            layer_uuid=layer.layer_uuid,
            adjustment_type="SATURATION",
        )
        layer.active_adjustment_index = 0
        settings.active_mask_uuid = ""
        settings.active_paint_image = layer.mask.image
        settings.paint_mask_mode = True

        reconcile_layer_graph(scene)

        self.assertEqual(settings.active_layer_index, 0)
        self.assertEqual(layer.active_adjustment_index, -1)
        self.assertEqual(settings.active_mask_uuid, layer.mask.mask_uuid)
        self.assertIs(settings.active_paint_image, layer.mask.image)
        self.assertTrue(settings.paint_mask_mode)

    def test_legacy_mask_conflict_preserves_both_logical_sources(self) -> None:
        """새 Layer Mask와 다른 구형 Mask가 충돌하면 어느 쪽도 제거하지 않습니다."""
        _target, _setup = _prepare_backend("LegacyMaskConflictTarget")
        settings = bpy.context.scene.projection_paint
        layer = settings.layers[0]
        bpy.ops.projection_paint.layer_mask_add(layer_uuid=layer.layer_uuid)
        new_uuid = str(layer.mask.mask_uuid)
        new_image = layer.mask.image
        legacy = layer.adjustments.add()
        legacy.adjustment_type = "MASK"
        legacy.adjustment_uuid = "conflicting-legacy-mask"

        with self.assertRaises(LayerShaderError):
            reconcile_layer_graph(bpy.context.scene)

        self.assertEqual(layer.mask.mask_uuid, new_uuid)
        self.assertIs(layer.mask.image, new_image)
        self.assertEqual(len(layer.adjustments), 1)
        self.assertEqual(layer.adjustments[0].adjustment_type, "MASK")

    def test_adjustment_mask_uses_fixed_apply_mix_and_mask_factor(self) -> None:
        """Adjustment는 Mask 유무와 무관하게 고정 Apply Mix를 사용합니다."""
        _target, setup = _prepare_backend("AdjustmentMaskTarget")
        settings = bpy.context.scene.projection_paint
        layer = settings.layers[0]
        self.assertEqual(
            bpy.ops.projection_paint.layer_adjustment_add(
                layer_uuid=layer.layer_uuid,
                adjustment_type="BRIGHTNESS",
            ),
            {"FINISHED"},
        )
        adjustment = layer.adjustments[0]
        apply_mix = _owned_nodes(
            setup.material,
            ADJUSTMENT_APPLY_MIX_NODE_ROLE,
        )[0]
        self.assertEqual(len(apply_mix.inputs[0].links), 0)
        self.assertEqual(apply_mix.inputs[0].default_value, 1.0)
        self.assertEqual(
            bpy.ops.projection_paint.adjustment_mask_add(
                layer_uuid=layer.layer_uuid,
                adjustment_uuid=adjustment.adjustment_uuid,
            ),
            {"FINISHED"},
        )
        mask_uuid = str(adjustment.mask.mask_uuid)
        self.assertEqual(tuple(adjustment.mask.image.size), (64, 32))
        self.assertEqual(
            str(adjustment.mask.image.get(MASK_UUID_KEY, "")),
            mask_uuid,
        )
        self.assertEqual(
            len(_owned_nodes(setup.material, ADJUSTMENT_MASK_IMAGE_NODE_ROLE)),
            1,
        )
        self.assertEqual(
            len(_owned_nodes(setup.material, ADJUSTMENT_MASK_BW_NODE_ROLE)),
            1,
        )
        self.assertEqual(
            apply_mix.inputs[0].links[0].from_node.get(MASK_UUID_KEY),
            mask_uuid,
        )
        adjustment.mask.enabled = False
        self.assertEqual(len(apply_mix.inputs[0].links), 0)
        self.assertEqual(apply_mix.inputs[0].default_value, 1.0)
        adjustment.enabled = False
        self.assertEqual(apply_mix.inputs[0].default_value, 0.0)
        adjustment.enabled = True
        self.assertEqual(apply_mix.inputs[0].default_value, 1.0)
        _validate_active_graph(setup.material)

    def test_adjustment_mask_white_black_and_gray_render_expected_mix(self) -> None:
        """Adjustment Mask의 흰색·검은색·회색이 전효과·원본·부분효과로 렌더됩니다."""
        target, setup = _prepare_backend("AdjustmentMaskRenderTarget")
        settings = bpy.context.scene.projection_paint
        layer = settings.layers[0]
        _fill_image(layer.image, (0.2, 0.2, 0.2, 1.0))
        bpy.ops.projection_paint.layer_adjustment_add(
            layer_uuid=layer.layer_uuid,
            adjustment_type="EXPOSURE",
        )
        adjustment = layer.adjustments[0]
        adjustment.exposure = 1.0
        bpy.ops.projection_paint.adjustment_mask_add(
            layer_uuid=layer.layer_uuid,
            adjustment_uuid=adjustment.adjustment_uuid,
        )
        for factor, expected in ((1.0, 0.4), (0.0, 0.2), (0.5, 0.3)):
            _fill_image(adjustment.mask.image, (factor, factor, factor, 1.0))
            color = _render_center_color(target, setup.material)
            for channel in color[:3]:
                self.assertAlmostEqual(channel, expected, delta=0.03)

    def test_remove_button_on_adjustment_deletes_its_mask_and_resources(self) -> None:
        """선택 Adjustment 삭제가 자체 Mask Preview·Node·Image를 정리합니다."""
        _target, setup = _prepare_backend("AdjustmentMaskDeleteTarget")
        settings = bpy.context.scene.projection_paint
        layer = settings.layers[0]
        bpy.ops.projection_paint.layer_adjustment_add(
            layer_uuid=layer.layer_uuid,
            adjustment_type="BRIGHTNESS",
        )
        adjustment = layer.adjustments[0]
        bpy.ops.projection_paint.adjustment_mask_add(
            layer_uuid=layer.layer_uuid,
            adjustment_uuid=adjustment.adjustment_uuid,
        )
        adjustment_uuid = str(adjustment.adjustment_uuid)
        mask_uuid = str(adjustment.mask.mask_uuid)
        mask_name = adjustment.mask.image.name
        bpy.ops.projection_paint.mask_preview_toggle(
            layer_uuid=layer.layer_uuid,
            mask_uuid=mask_uuid,
        )
        bpy.ops.projection_paint.layer_adjustment_select(
            layer_uuid=layer.layer_uuid,
            adjustment_uuid=adjustment_uuid,
        )

        self.assertEqual(
            bpy.ops.projection_paint.layer_remove(),
            {"FINISHED"},
        )

        self.assertEqual(len(layer.adjustments), 0)
        self.assertEqual(settings.active_mask_uuid, "")
        self.assertEqual(settings.mask_preview_adjustment_uuid, "")
        self.assertFalse(settings.paint_mask_mode)
        self.assertIs(settings.active_paint_image, layer.image)
        self.assertIsNone(bpy.data.images.get(mask_name))
        self.assertEqual(
            len(_owned_nodes(setup.material, ADJUSTMENT_APPLY_MIX_NODE_ROLE)),
            0,
        )
        self.assertEqual(
            len(_owned_nodes(setup.material, ADJUSTMENT_MASK_IMAGE_NODE_ROLE)),
            0,
        )
        _validate_active_graph(setup.material)

    def test_remove_button_on_adjustment_mask_keeps_adjustment(self) -> None:
        """선택 Adjustment Mask 삭제가 부모 보정과 Layer를 그대로 유지합니다."""
        _target, setup = _prepare_backend("AdjustmentMaskOnlyDeleteTarget")
        settings = bpy.context.scene.projection_paint
        layer = settings.layers[0]
        bpy.ops.projection_paint.layer_adjustment_add(
            layer_uuid=layer.layer_uuid,
            adjustment_type="EXPOSURE",
        )
        adjustment = layer.adjustments[0]
        adjustment_uuid = str(adjustment.adjustment_uuid)
        bpy.ops.projection_paint.adjustment_mask_add(
            layer_uuid=layer.layer_uuid,
            adjustment_uuid=adjustment_uuid,
        )
        mask_name = adjustment.mask.image.name

        self.assertEqual(bpy.ops.projection_paint.layer_remove(), {"FINISHED"})

        self.assertEqual(len(settings.layers), 2)
        self.assertEqual(len(layer.adjustments), 1)
        self.assertEqual(str(layer.adjustments[0].adjustment_uuid), adjustment_uuid)
        self.assertFalse(str(layer.adjustments[0].mask.mask_uuid or ""))
        self.assertIsNone(bpy.data.images.get(mask_name))
        self.assertEqual(
            len(_owned_nodes(setup.material, ADJUSTMENT_NODE_ROLE)),
            1,
        )
        self.assertEqual(
            len(_owned_nodes(setup.material, ADJUSTMENT_MASK_IMAGE_NODE_ROLE)),
            0,
        )
        _validate_active_graph(setup.material)

    def test_adjustments_and_mask_create_owned_images_and_nodes(self) -> None:
        """Mask와 세 보정 UI가 실제 Image와 대응 Shader Node를 생성합니다."""
        _target, setup = _prepare_backend()
        settings = bpy.context.scene.projection_paint
        layer = settings.layers[0]
        self.assertEqual(
            bpy.ops.projection_paint.layer_mask_add(
                layer_uuid=layer.layer_uuid,
            ),
            {"FINISHED"},
        )
        for adjustment_type in ("BRIGHTNESS", "EXPOSURE", "SATURATION"):
            self.assertEqual(
                bpy.ops.projection_paint.layer_adjustment_add(
                    layer_uuid=layer.layer_uuid,
                    adjustment_type=adjustment_type,
                ),
                {"FINISHED"},
            )
        mask = layer.mask
        self.assertEqual(tuple(mask.image.size), (64, 32))
        self.assertEqual(mask.image.colorspace_settings.name, "Non-Color")
        self.assertEqual(tuple(mask.image.generated_color), (1.0, 1.0, 1.0, 1.0))
        self.assertEqual(
            str(mask.image.get(RECORD_KIND_KEY, "")),
            "MASK_PERSISTENT",
        )
        self.assertEqual(
            len(_owned_nodes(setup.material, MASK_IMAGE_NODE_ROLE)),
            1,
        )
        self.assertEqual(
            bpy.ops.projection_paint.layer_mask_select(
                layer_uuid=layer.layer_uuid,
                mask_uuid=mask.mask_uuid,
            ),
            {"FINISHED"},
        )
        self.assertIs(settings.active_paint_image, mask.image)
        self.assertTrue(settings.paint_mask_mode)
        layer_mix = _layer_mix(setup.material, layer.layer_uuid)
        self.assertEqual(
            layer_mix.inputs[0].links[0].from_node.type,
            "MATH",
        )
        mask.enabled = False
        self.assertEqual(
            layer_mix.inputs[0].links[0].from_node.type,
            "TEX_IMAGE",
        )
        mask.enabled = True
        adjustment_nodes = _owned_nodes(setup.material, ADJUSTMENT_NODE_ROLE)
        self.assertEqual(len(adjustment_nodes), 3)
        exposure = layer.adjustments[1]
        exposure.exposure = 2.0
        exposure_node = next(
            node
            for node in adjustment_nodes
            if str(node.get(ADJUSTMENT_UUID_KEY, ""))
            == str(exposure.adjustment_uuid)
        )
        self.assertEqual(tuple(exposure_node.inputs[2].default_value), (4.0, 4.0, 4.0, 1.0))
        hue_saturation = layer.adjustments[2]
        hue_saturation.hue = 0.75
        hue_saturation.saturation = 0.4
        hue_saturation_node = next(
            node
            for node in adjustment_nodes
            if str(node.get(ADJUSTMENT_UUID_KEY, ""))
            == str(hue_saturation.adjustment_uuid)
        )
        self.assertEqual(hue_saturation_node.type, "HUE_SAT")
        self.assertEqual(hue_saturation_node.label, "Hue/Saturation")
        self.assertAlmostEqual(
            hue_saturation_node.inputs["Hue"].default_value,
            0.75,
        )
        self.assertAlmostEqual(
            hue_saturation_node.inputs["Saturation"].default_value,
            0.4,
        )
        self.assertEqual(
            hue_saturation_node.inputs["Color"].links[0].from_node.as_pointer(),
            next(
                node
                for node in _owned_nodes(
                    setup.material,
                    ADJUSTMENT_APPLY_MIX_NODE_ROLE,
                )
                if str(node.get(ADJUSTMENT_UUID_KEY, ""))
                == str(exposure.adjustment_uuid)
            ).as_pointer(),
        )
        layer.adjustments[0].enabled = False
        brightness_node = next(
            node
            for node in adjustment_nodes
            if str(node.get(ADJUSTMENT_UUID_KEY, ""))
            == str(layer.adjustments[0].adjustment_uuid)
        )
        self.assertFalse(brightness_node.mute)
        brightness_apply = next(
            node
            for node in _owned_nodes(
                setup.material,
                ADJUSTMENT_APPLY_MIX_NODE_ROLE,
            )
            if str(node.get(ADJUSTMENT_UUID_KEY, ""))
            == str(layer.adjustments[0].adjustment_uuid)
        )
        self.assertEqual(brightness_apply.inputs[0].default_value, 0.0)
        _validate_active_graph(setup.material)

    def test_legacy_hue_and_saturation_merge_into_one_adjustment(self) -> None:
        """구버전 Hue와 Saturation을 값과 선택을 보존한 단일 항목으로 통합합니다."""
        _target, setup = _prepare_backend("HueSaturationMigrationTarget")
        settings = bpy.context.scene.projection_paint
        layer = settings.layers[0]
        hue = layer.adjustments.add()
        hue.adjustment_type = "HUE"
        hue.adjustment_uuid = "legacy-hue"
        hue["hue"] = 0.8
        hue["enabled"] = True
        saturation = layer.adjustments.add()
        saturation.adjustment_type = "SATURATION"
        saturation.adjustment_uuid = "legacy-saturation"
        saturation["saturation"] = 0.35
        saturation["enabled"] = True
        layer.active_adjustment_index = 0

        reconcile_layer_graph(bpy.context.scene)

        self.assertEqual(len(layer.adjustments), 1)
        merged = layer.adjustments[0]
        self.assertEqual(merged.adjustment_type, "SATURATION")
        self.assertEqual(merged.adjustment_uuid, "legacy-saturation")
        self.assertAlmostEqual(merged.hue, 0.8)
        self.assertAlmostEqual(merged.saturation, 0.35)
        self.assertTrue(merged.enabled)
        self.assertEqual(layer.active_adjustment_index, 0)
        nodes = _owned_nodes(setup.material, ADJUSTMENT_NODE_ROLE)
        self.assertEqual(len(nodes), 1)
        self.assertAlmostEqual(nodes[0].inputs["Hue"].default_value, 0.8)
        self.assertAlmostEqual(
            nodes[0].inputs["Saturation"].default_value,
            0.35,
        )
        _validate_active_graph(setup.material)

    def test_legacy_hue_only_becomes_unified_adjustment(self) -> None:
        """구버전 Hue 단일 항목이 UUID와 Hue 값을 유지한 통합 항목이 됩니다."""
        _target, setup = _prepare_backend("HueOnlyMigrationTarget")
        settings = bpy.context.scene.projection_paint
        layer = settings.layers[0]
        hue = layer.adjustments.add()
        hue.adjustment_type = "HUE"
        hue.adjustment_uuid = "legacy-hue-only"
        hue["hue"] = 0.2
        layer.active_adjustment_index = 0

        reconcile_layer_graph(bpy.context.scene)

        self.assertEqual(len(layer.adjustments), 1)
        merged = layer.adjustments[0]
        self.assertEqual(merged.adjustment_type, "SATURATION")
        self.assertEqual(merged.adjustment_uuid, "legacy-hue-only")
        self.assertAlmostEqual(merged.hue, 0.2)
        self.assertAlmostEqual(merged.saturation, 1.0)
        nodes = _owned_nodes(setup.material, ADJUSTMENT_NODE_ROLE)
        self.assertEqual(len(nodes), 1)
        self.assertAlmostEqual(nodes[0].inputs["Hue"].default_value, 0.2)
        self.assertAlmostEqual(
            nodes[0].inputs["Saturation"].default_value,
            1.0,
        )
        _validate_active_graph(setup.material)

    def test_mask_preview_replaces_and_restores_base_color(self) -> None:
        """Mask 체크가 흑백 출력만 Base Color에 연결하고 원래 합성을 복구합니다."""
        _target, setup = _prepare_backend("MaskPreviewTarget")
        settings = bpy.context.scene.projection_paint
        layer = settings.layers[0]
        self.assertEqual(
            bpy.ops.projection_paint.layer_mask_add(
                layer_uuid=layer.layer_uuid,
            ),
            {"FINISHED"},
        )
        mask_uuid = str(layer.mask.mask_uuid)
        preview = next(
            node
            for node in _owned_nodes(setup.material, MASK_BW_NODE_ROLE)
            if str(node.get(MASK_UUID_KEY, "")) == mask_uuid
        )
        principled = next(
            node
            for node in setup.material.node_tree.nodes
            if node.type == "BSDF_PRINCIPLED"
        )
        self.assertEqual(
            bpy.ops.projection_paint.mask_preview_toggle(
                layer_uuid=layer.layer_uuid,
                mask_uuid=mask_uuid,
            ),
            {"FINISHED"},
        )
        self.assertEqual(settings.mask_preview_adjustment_uuid, mask_uuid)
        self.assertEqual(settings.layer_preview_uuid, "")
        self.assertEqual(setup.material[MASK_PREVIEW_UUID_KEY], mask_uuid)
        self.assertEqual(
            principled.inputs["Base Color"].links[0].from_node.as_pointer(),
            preview.as_pointer(),
        )
        _validate_active_graph(setup.material)
        self.assertEqual(
            bpy.ops.projection_paint.mask_preview_toggle(
                layer_uuid=layer.layer_uuid,
                mask_uuid=mask_uuid,
            ),
            {"FINISHED"},
        )
        self.assertEqual(settings.mask_preview_adjustment_uuid, "")
        self.assertNotIn(MASK_PREVIEW_UUID_KEY, setup.material)
        self.assertEqual(
            principled.inputs["Base Color"].links[0].from_node.as_pointer(),
            _layer_mix(setup.material, layer.layer_uuid).as_pointer(),
        )
        _validate_active_graph(setup.material)

    def test_layer_and_mask_preview_are_globally_exclusive(self) -> None:
        """Layer와 Mask 체크가 하나만 활성화되고 해제 시 전체 합성을 복구합니다."""
        _target, setup = _prepare_backend("GlobalPreviewTarget")
        settings = bpy.context.scene.projection_paint
        bottom_layer_uuid = str(settings.layers[0].layer_uuid)
        self.assertEqual(
            bpy.ops.projection_paint.layer_mask_add(
                layer_uuid=bottom_layer_uuid,
            ),
            {"FINISHED"},
        )
        bottom_mask_uuid = str(settings.layers[0].mask.mask_uuid)
        self.assertEqual(bpy.ops.projection_paint.layer_add(), {"FINISHED"})
        top_layer_uuid = str(settings.layers[0].layer_uuid)
        top_mix = _layer_mix(setup.material, top_layer_uuid)
        top_preview_source = top_mix.inputs[2].links[0].from_socket
        principled = next(
            node
            for node in setup.material.node_tree.nodes
            if node.type == "BSDF_PRINCIPLED"
        )

        self.assertEqual(
            bpy.ops.projection_paint.layer_preview_toggle(
                layer_uuid=top_layer_uuid,
            ),
            {"FINISHED"},
        )
        self.assertEqual(settings.layer_preview_uuid, top_layer_uuid)
        self.assertEqual(settings.mask_preview_adjustment_uuid, "")
        self.assertEqual(
            setup.material[LAYER_PREVIEW_UUID_KEY],
            top_layer_uuid,
        )
        self.assertNotIn(MASK_PREVIEW_UUID_KEY, setup.material)
        self.assertEqual(
            principled.inputs["Base Color"].links[0].from_socket.as_pointer(),
            top_preview_source.as_pointer(),
        )
        _validate_active_graph(setup.material)

        self.assertEqual(
            bpy.ops.projection_paint.layer_preview_toggle(
                layer_uuid=top_layer_uuid,
            ),
            {"FINISHED"},
        )
        self.assertEqual(settings.layer_preview_uuid, "")
        self.assertEqual(
            principled.inputs["Base Color"].links[0].from_node.as_pointer(),
            top_mix.as_pointer(),
        )
        self.assertEqual(
            bpy.ops.projection_paint.layer_preview_toggle(
                layer_uuid=top_layer_uuid,
            ),
            {"FINISHED"},
        )

        self.assertEqual(
            bpy.ops.projection_paint.mask_preview_toggle(
                layer_uuid=bottom_layer_uuid,
                mask_uuid=bottom_mask_uuid,
            ),
            {"FINISHED"},
        )
        self.assertEqual(settings.layer_preview_uuid, "")
        self.assertEqual(settings.mask_preview_adjustment_uuid, bottom_mask_uuid)
        self.assertNotIn(LAYER_PREVIEW_UUID_KEY, setup.material)
        self.assertEqual(
            setup.material[MASK_PREVIEW_UUID_KEY],
            bottom_mask_uuid,
        )
        _validate_active_graph(setup.material)

        self.assertEqual(
            bpy.ops.projection_paint.mask_preview_toggle(
                layer_uuid=bottom_layer_uuid,
                mask_uuid=bottom_mask_uuid,
            ),
            {"FINISHED"},
        )
        self.assertEqual(settings.layer_preview_uuid, "")
        self.assertEqual(settings.mask_preview_adjustment_uuid, "")
        self.assertNotIn(LAYER_PREVIEW_UUID_KEY, setup.material)
        self.assertNotIn(MASK_PREVIEW_UUID_KEY, setup.material)
        self.assertEqual(
            principled.inputs["Base Color"].links[0].from_node.as_pointer(),
            top_mix.as_pointer(),
        )
        _validate_active_graph(setup.material)

    def test_original_default_visibility_and_solo_keep_paint_chain_intact(self) -> None:
        """실제 Original은 일반 Layer처럼 눈·단독 보기·복귀를 지원합니다."""
        _target, setup = _prepare_backend("OriginalDefaultLayerTarget")
        settings = bpy.context.scene.projection_paint
        original_layer = _original_layer(settings)
        original_uuid = str(original_layer.layer_uuid)
        normal_layer = _normal_layers(settings)[0]
        self.assertTrue(original_layer.name.startswith("Original Layer"))
        self.assertEqual(settings.layers[-1].layer_uuid, original_uuid)
        self.assertEqual(bpy.ops.projection_paint.layer_preview_toggle(layer_uuid=original_uuid), {"FINISHED"})
        self.assertEqual(settings.layer_preview_uuid, original_uuid)
        self.assertEqual(bpy.ops.projection_paint.layer_preview_toggle(layer_uuid=original_uuid), {"FINISHED"})
        self.assertEqual(settings.layer_preview_uuid, "")

        original_layer.visible = False
        original_mix = _layer_mix(setup.material, original_uuid)
        self.assertEqual(len(original_mix.inputs[0].links), 0)
        self.assertEqual(original_mix.inputs[0].default_value, 0.0)
        normal_mix = _layer_mix(setup.material, normal_layer.layer_uuid)
        self.assertEqual(
            normal_mix.inputs[1].links[0].from_node.as_pointer(),
            original_mix.as_pointer(),
        )
        original_layer.visible = True
        self.assertEqual(len(original_mix.inputs[0].links), 1)
        self.assertEqual(
            original_mix.inputs[0].links[0].from_node.as_pointer(),
            _layer_image_node(setup.material, original_uuid).as_pointer(),
        )
        _validate_active_graph(setup.material)

    def test_mask_preview_is_exclusive_and_deleted_mask_restores_stack(self) -> None:
        """다른 Mask 체크와 현재 Mask 삭제가 단일 미리보기와 합성을 유지합니다."""
        _target, setup = _prepare_backend("MaskPreviewDeleteTarget")
        settings = bpy.context.scene.projection_paint
        bottom = settings.layers[0]
        bpy.ops.projection_paint.layer_mask_add(layer_uuid=bottom.layer_uuid)
        bottom_layer_uuid = str(bottom.layer_uuid)
        bottom_uuid = str(bottom.mask.mask_uuid)
        bpy.ops.projection_paint.layer_add()
        top = settings.layers[0]
        bpy.ops.projection_paint.layer_mask_add(layer_uuid=top.layer_uuid)
        top_uuid = str(top.mask.mask_uuid)
        bpy.ops.projection_paint.mask_preview_toggle(
            layer_uuid=bottom_layer_uuid,
            mask_uuid=bottom_uuid,
        )
        self.assertEqual(settings.mask_preview_adjustment_uuid, bottom_uuid)
        bpy.ops.projection_paint.mask_preview_toggle(
            layer_uuid=top.layer_uuid,
            mask_uuid=top_uuid,
        )
        self.assertEqual(settings.mask_preview_adjustment_uuid, top_uuid)
        self.assertEqual(
            bpy.ops.projection_paint.layer_mask_remove(
                layer_uuid=top.layer_uuid,
            ),
            {"FINISHED"},
        )
        self.assertEqual(settings.mask_preview_adjustment_uuid, "")
        self.assertNotIn(MASK_PREVIEW_UUID_KEY, setup.material)
        _validate_active_graph(setup.material)

    def test_ui_top_layer_is_last_mix_before_principled(self) -> None:
        """UI 첫 Layer가 아래 Layer 결과를 받아 Principled 직전에 합성됩니다."""
        _target, setup = _prepare_backend()
        settings = bpy.context.scene.projection_paint
        original_layer = _original_layer(settings)
        original_uuid = str(original_layer.layer_uuid)
        original_name = str(original_layer.name)
        original_pointers = (
            _layer_image_node(setup.material, original_layer.layer_uuid).as_pointer(),
            _layer_mix(setup.material, original_layer.layer_uuid).as_pointer(),
        )
        bpy.ops.projection_paint.layer_add()
        bpy.ops.projection_paint.layer_add()
        self.assertEqual(
            [layer.name for layer in settings.layers],
            ["Layer 3", "Layer 2", "Layer 1", original_name],
        )
        top, middle, bottom = _normal_layers(settings)
        self.assertEqual(
            (
                _layer_image_node(setup.material, original_uuid).as_pointer(),
                _layer_mix(setup.material, original_uuid).as_pointer(),
            ),
            original_pointers,
        )
        top_mix = _layer_mix(setup.material, top.layer_uuid)
        middle_mix = _layer_mix(setup.material, middle.layer_uuid)
        bottom_mix = _layer_mix(setup.material, bottom.layer_uuid)
        self.assertEqual(
            top_mix.inputs[1].links[0].from_node.as_pointer(),
            middle_mix.as_pointer(),
        )
        self.assertEqual(
            middle_mix.inputs[1].links[0].from_node.as_pointer(),
            bottom_mix.as_pointer(),
        )
        self.assertEqual(
            bottom_mix.inputs[1].links[0].from_node.as_pointer(),
            _layer_mix(setup.material, original_uuid).as_pointer(),
        )
        principled = next(
            node
            for node in setup.material.node_tree.nodes
            if node.type == "BSDF_PRINCIPLED"
        )
        self.assertEqual(
            principled.inputs["Base Color"].links[0].from_node.as_pointer(),
            top_mix.as_pointer(),
        )
        settings.active_layer_index = 2
        self.assertEqual(
            bpy.ops.projection_paint.layer_move(direction="UP"),
            {"FINISHED"},
        )
        self.assertEqual(
            [layer.name for layer in settings.layers],
            ["Layer 3", "Layer 1", "Layer 2", original_name],
        )
        _validate_active_graph(setup.material)

    def test_layer_add_preserves_existing_preview_and_full_chain(self) -> None:
        """미리보기 중 Layer 추가가 기존 Preview와 전체 합성 Chain을 보존합니다."""
        _target, setup = _prepare_backend("LayerAddPreviewTarget")
        settings = bpy.context.scene.projection_paint
        previewed = settings.layers[0]
        previewed_uuid = str(previewed.layer_uuid)
        previewed_mix = _layer_mix(setup.material, previewed_uuid)
        preview_source = previewed_mix.inputs[2].links[0].from_socket
        principled = next(
            node
            for node in setup.material.node_tree.nodes
            if node.type == "BSDF_PRINCIPLED"
        )
        bpy.ops.projection_paint.layer_preview_toggle(layer_uuid=previewed_uuid)

        self.assertEqual(bpy.ops.projection_paint.layer_add(), {"FINISHED"})

        added = settings.layers[0]
        added_mix = _layer_mix(setup.material, added.layer_uuid)
        self.assertEqual(settings.layer_preview_uuid, previewed_uuid)
        self.assertEqual(
            principled.inputs["Base Color"].links[0].from_socket.as_pointer(),
            preview_source.as_pointer(),
        )
        self.assertEqual(
            added_mix.inputs[1].links[0].from_node.as_pointer(),
            previewed_mix.as_pointer(),
        )
        validate_layer_graph(
            setup.material,
            _layer_uuids(settings),
        )

    def test_object_mode_stale_paint_images_do_not_block_layer_add(self) -> None:
        """Object Mode의 기존 Paint Slot Cache가 새 Layer 추가를 방해하지 않습니다."""
        target, setup = _prepare_backend("ObjectModeStaleSlotsTarget")
        settings = bpy.context.scene.projection_paint
        bpy.context.view_layer.objects.active = target
        target.select_set(True)
        bpy.ops.object.mode_set(mode="TEXTURE_PAINT")
        self.assertGreater(len(tuple(setup.material.texture_paint_images)), 0)
        bpy.ops.object.mode_set(mode="OBJECT")
        self.assertEqual(target.mode, "OBJECT")

        self.assertEqual(bpy.ops.projection_paint.layer_add(), {"FINISHED"})

        added = settings.layers[0]
        self.assertIs(settings.active_paint_image, added.image)
        self.assertIs(settings.overlay_image, added.image)
        validate_layer_graph(
            setup.material,
            tuple(str(layer.layer_uuid) for layer in settings.layers),
        )

    def test_layer_add_activation_failure_rolls_back_all_resources(self) -> None:
        """새 Layer 활성화 실패가 논리·Image·Node·Link·Pointer를 모두 복구합니다."""
        _target, setup = _prepare_backend("LayerAddRollbackTarget")
        settings = bpy.context.scene.projection_paint
        tree = setup.material.node_tree
        snapshot = {
            "layers": tuple(str(layer.layer_uuid) for layer in settings.layers),
            "images": tuple(image.as_pointer() for image in bpy.data.images),
            "nodes": tuple(node.as_pointer() for node in tree.nodes),
            "links": tuple(
                sorted(
                    (link.from_socket.as_pointer(), link.to_socket.as_pointer())
                    for link in tree.links
                )
            ),
            "active_index": int(settings.active_layer_index),
            "overlay": settings.overlay_image.as_pointer(),
            "active_image": settings.active_paint_image.as_pointer(),
        }
        with mock.patch(
            "tools.projection_paint.blender.layer_shader."
            "_activate_paint_image_node",
            side_effect=LayerShaderError("주입된 활성화 실패"),
        ):
            with self.assertRaisesRegex(LayerShaderError, "주입된 활성화 실패"):
                add_layer_transaction(
                    bpy.context,
                    "Layer Rollback",
                    "rollback-layer",
                )

        self.assertEqual(
            tuple(str(layer.layer_uuid) for layer in settings.layers),
            snapshot["layers"],
        )
        self.assertEqual(
            tuple(image.as_pointer() for image in bpy.data.images),
            snapshot["images"],
        )
        self.assertEqual(tuple(node.as_pointer() for node in tree.nodes), snapshot["nodes"])
        self.assertEqual(
            tuple(
                sorted(
                    (link.from_socket.as_pointer(), link.to_socket.as_pointer())
                    for link in tree.links
                )
            ),
            snapshot["links"],
        )
        self.assertEqual(settings.active_layer_index, snapshot["active_index"])
        self.assertEqual(settings.overlay_image.as_pointer(), snapshot["overlay"])
        self.assertEqual(settings.active_paint_image.as_pointer(), snapshot["active_image"])

    def test_reconcile_removes_ghost_resources_without_replacing_logical_image(self) -> None:
        """명시 Reconcile이 고아 자원만 정리하고 논리 Layer Image를 유지합니다."""
        target, setup = _prepare_backend("GhostLayerResourceTarget")
        settings = bpy.context.scene.projection_paint
        logical_image = settings.layers[0].image
        ghost_image = bpy.data.images.new("GhostPaintImage", 64, 32, alpha=True)
        ghost_image[OWNER_KEY] = OWNER_TOKEN
        ghost_image[RECORD_KIND_KEY] = "OVERLAY_PERSISTENT"
        ghost_image["pp_uuid"] = "ghost-image"
        ghost_image["pp_target_uuid"] = str(target.get("pp_target_uuid", ""))
        ghost_image["pp_layer_uuid"] = "ghost-layer"
        ghost = setup.material.node_tree.nodes.new("ShaderNodeTexImage")
        ghost.image = ghost_image
        ghost[OWNER_KEY] = OWNER_TOKEN
        ghost[RECORD_KIND_KEY] = LAYER_IMAGE_NODE_ROLE
        ghost[LAYER_UUID_KEY] = "ghost-layer"
        settings.overlay_image = ghost_image
        settings.active_paint_image = ghost_image

        reconcile_layer_graph(bpy.context.scene)

        self.assertIs(settings.layers[0].image, logical_image)
        self.assertIs(settings.overlay_image, logical_image)
        self.assertIs(settings.active_paint_image, logical_image)
        self.assertIsNone(bpy.data.images.get("GhostPaintImage"))
        self.assertNotIn(ghost, tuple(setup.material.node_tree.nodes))

    def test_layer_and_mask_selection_activates_existing_nodes_only(self) -> None:
        """Layer와 Mask 선택은 기존 노드 활성화와 Pointer만 변경합니다."""
        _target, setup = _prepare_backend()
        settings = bpy.context.scene.projection_paint
        layer = settings.layers[0]
        canvas = bpy.context.scene.tool_settings.image_paint.canvas

        bpy.ops.projection_paint.layer_mask_add(layer_uuid=layer.layer_uuid)

        mask = layer.mask
        links = tuple(
            (link.from_socket.as_pointer(), link.to_socket.as_pointer())
            for link in setup.material.node_tree.links
        )
        mask_node = next(
            node
            for node in setup.material.node_tree.nodes
            if node.get(MASK_UUID_KEY, "") == str(mask.mask_uuid)
            and node.get(RECORD_KIND_KEY, "") == MASK_IMAGE_NODE_ROLE
        )
        self.assertEqual(
            setup.material.node_tree.nodes.active.as_pointer(),
            mask_node.as_pointer(),
        )
        self.assertIs(settings.active_paint_image, mask.image)

        self.assertEqual(
            bpy.ops.projection_paint.layer_select(
                layer_uuid=layer.layer_uuid,
            ),
            {"FINISHED"},
        )

        layer_node = _layer_image_node(setup.material, layer.layer_uuid)
        self.assertEqual(
            setup.material.node_tree.nodes.active.as_pointer(),
            layer_node.as_pointer(),
        )
        self.assertIs(layer_node.image, layer.image)
        self.assertIs(bpy.context.scene.tool_settings.image_paint.canvas, canvas)
        self.assertEqual(
            tuple(
                (link.from_socket.as_pointer(), link.to_socket.as_pointer())
                for link in setup.material.node_tree.links
            ),
            links,
        )
        self.assertFalse(settings.paint_mask_mode)
        self.assertIs(settings.active_paint_image, layer.image)

    def test_layer_and_mask_selection_outside_runtime_do_not_update_stencil(self) -> None:
        """실제 Projection Runtime 밖의 Layer·Mask 선택은 Stencil을 변경하지 않습니다."""
        _target, _setup = _prepare_backend("LayerViewPreserveTarget")
        scene = bpy.context.scene
        settings = scene.projection_paint
        layer = settings.layers[0]
        bpy.ops.projection_paint.layer_mask_add(layer_uuid=layer.layer_uuid)
        settings.paint_enabled = True
        context = SimpleNamespace(scene=scene)
        with (
            mock.patch(
                "tools.projection_paint.blender.paint_runtime."
                "reconnect_current_frame",
            ) as reconnect,
            mock.patch(
                "tools.projection_paint.blender.stencil_session."
                "disconnect_stencil",
            ) as disconnect,
        ):
            activate_layer_paint_image(context, str(layer.layer_uuid))
            activate_mask_paint_image(
                context,
                str(layer.layer_uuid),
                str(layer.mask.mask_uuid),
            )

        reconnect.assert_not_called()
        disconnect.assert_not_called()

    def test_layer_selection_in_camera_view_restores_projection_stencil(self) -> None:
        """Camera View의 Layer 선택은 검은 Brush를 흰 STENCIL 상태로 되돌립니다."""
        _target, setup, layer, stencil, runtime = _prepare_layer_stencil_resume()
        scene = bpy.context.scene
        settings = scene.projection_paint
        brush = stencil.brush
        brush.color = (0.0, 0.0, 0.0)
        brush.texture = None
        brush.texture_slot.map_mode = "VIEW_PLANE"
        with (
            mock.patch(
                "tools.projection_paint.blender.paint_runtime."
                "active_paint_runtime",
                return_value=runtime,
            ),
            mock.patch(
                "tools.projection_paint.blender.paint_runtime."
                "_projection_runtime_is_active",
                return_value=True,
            ),
            mock.patch(
                "tools.projection_paint.blender.paint_runtime."
                "stencil_view_token",
                return_value=(True, "camera-view"),
            ),
            mock.patch(
                "tools.projection_paint.blender.paint_runtime."
                "start_viewport_tracking",
            ) as start_tracking,
        ):
            self.assertIs(
                activate_layer_paint_image(bpy.context, str(layer.layer_uuid)),
                layer.image,
            )

        self.assertIs(settings.active_paint_image, layer.image)
        self.assertFalse(settings.paint_mask_mode)
        self.assertIs(
            setup.material.texture_paint_images[setup.material.paint_active_slot],
            layer.image,
        )
        self.assertIs(brush.texture, stencil.managed_texture)
        self.assertEqual(brush.texture_slot.map_mode, "STENCIL")
        self.assertEqual(tuple(brush.color), (1.0, 1.0, 1.0))
        self.assertFalse(settings.manual_stencil_disabled)
        self.assertFalse(settings.stencil_outside_camera_view)
        self.assertEqual(settings.paint_state, "ACTIVE")
        self.assertEqual(runtime.viewport_token, (True, "camera-view"))
        start_tracking.assert_called_once_with(scene)

    def test_layer_selection_outside_camera_view_keeps_stencil_unchanged(self) -> None:
        """Camera View가 아니면 Layer 선택이 Stencil을 강제로 복구하지 않습니다."""
        _target, setup, layer, stencil, runtime = _prepare_layer_stencil_resume()
        brush = stencil.brush
        brush.color = (0.0, 0.0, 0.0)
        brush.texture = None
        brush.texture_slot.map_mode = "VIEW_PLANE"
        with (
            mock.patch(
                "tools.projection_paint.blender.paint_runtime."
                "active_paint_runtime",
                return_value=runtime,
            ),
            mock.patch(
                "tools.projection_paint.blender.paint_runtime."
                "_projection_runtime_is_active",
                return_value=True,
            ),
            mock.patch(
                "tools.projection_paint.blender.paint_runtime."
                "stencil_view_token",
                return_value=(False, "perspective-view"),
            ),
            mock.patch(
                "tools.projection_paint.blender.paint_runtime."
                "reconnect_stencil_texture",
            ) as reconnect,
        ):
            self.assertIs(
                activate_layer_paint_image(bpy.context, str(layer.layer_uuid)),
                layer.image,
            )

        self.assertIs(
            setup.material.texture_paint_images[setup.material.paint_active_slot],
            layer.image,
        )
        self.assertEqual(tuple(brush.color), (0.0, 0.0, 0.0))
        self.assertIsNone(brush.texture)
        self.assertEqual(brush.texture_slot.map_mode, "VIEW_PLANE")
        reconnect.assert_not_called()

    def test_layer_selection_without_source_keeps_selection_successful(self) -> None:
        """Frame Source가 없으면 Layer 선택은 성공하고 Stencil 복구만 생략합니다."""
        _target, setup, layer, stencil, runtime = _prepare_layer_stencil_resume()
        settings = bpy.context.scene.projection_paint
        brush = stencil.brush
        settings.stencil_source_image = None
        settings.active_image_id = 0
        brush.color = (0.0, 0.0, 0.0)
        brush.texture = None
        brush.texture_slot.map_mode = "VIEW_PLANE"
        with (
            mock.patch(
                "tools.projection_paint.blender.paint_runtime."
                "active_paint_runtime",
                return_value=runtime,
            ),
            mock.patch(
                "tools.projection_paint.blender.paint_runtime."
                "_projection_runtime_is_active",
                return_value=True,
            ),
            mock.patch(
                "tools.projection_paint.blender.paint_runtime."
                "reconnect_stencil_texture",
            ) as reconnect,
        ):
            self.assertIs(
                activate_layer_paint_image(bpy.context, str(layer.layer_uuid)),
                layer.image,
            )

        self.assertIs(settings.active_paint_image, layer.image)
        self.assertFalse(settings.paint_mask_mode)
        self.assertIs(
            setup.material.texture_paint_images[setup.material.paint_active_slot],
            layer.image,
        )
        self.assertEqual(tuple(brush.color), (0.0, 0.0, 0.0))
        self.assertIsNone(brush.texture)
        self.assertEqual(brush.texture_slot.map_mode, "VIEW_PLANE")
        reconnect.assert_not_called()

    def test_layer_selection_keeps_target_when_stencil_restore_fails(self) -> None:
        """Stencil 복구 예외가 발생해도 확정된 Layer 선택과 Paint Slot을 유지합니다."""
        _target, setup, layer, stencil, runtime = _prepare_layer_stencil_resume()
        brush = stencil.brush
        brush.color = (0.0, 0.0, 0.0)
        brush.texture = None
        brush.texture_slot.map_mode = "VIEW_PLANE"
        with (
            mock.patch(
                "tools.projection_paint.blender.paint_runtime."
                "active_paint_runtime",
                return_value=runtime,
            ),
            mock.patch(
                "tools.projection_paint.blender.paint_runtime."
                "_projection_runtime_is_active",
                return_value=True,
            ),
            mock.patch(
                "tools.projection_paint.blender.paint_runtime."
                "stencil_view_token",
                return_value=(True, "camera-view"),
            ),
            mock.patch(
                "tools.projection_paint.blender.paint_runtime."
                "reconnect_stencil_texture",
                side_effect=RuntimeError("재연결 실패"),
            ) as reconnect,
        ):
            self.assertIs(
                activate_layer_paint_image(bpy.context, str(layer.layer_uuid)),
                layer.image,
            )

        self.assertIs(bpy.context.scene.projection_paint.active_paint_image, layer.image)
        self.assertIs(
            setup.material.texture_paint_images[setup.material.paint_active_slot],
            layer.image,
        )
        self.assertEqual(tuple(brush.color), (0.0, 0.0, 0.0))
        self.assertIsNone(brush.texture)
        self.assertEqual(brush.texture_slot.map_mode, "VIEW_PLANE")
        reconnect.assert_called_once()

    def test_paint_image_selection_100_times_changes_only_active_node(self) -> None:
        """Texture Paint에서 Layer와 두 Mask를 100회 선택해도 자원이 유지됩니다."""
        target, setup = _prepare_backend("LayerSelectionStressTarget")
        settings = bpy.context.scene.projection_paint
        layer = settings.layers[0]
        bpy.ops.projection_paint.layer_mask_add(layer_uuid=layer.layer_uuid)
        bpy.ops.projection_paint.layer_adjustment_add(
            layer_uuid=layer.layer_uuid,
            adjustment_type="BRIGHTNESS",
        )
        adjustment = layer.adjustments[0]
        bpy.ops.projection_paint.adjustment_mask_add(
            layer_uuid=layer.layer_uuid,
            adjustment_uuid=adjustment.adjustment_uuid,
        )
        tree = setup.material.node_tree
        links = tuple(
            sorted(
                (link.from_socket.as_pointer(), link.to_socket.as_pointer())
                for link in tree.links
            )
        )
        node_images = tuple(
            sorted(
                (node.as_pointer(), node.image.as_pointer())
                for node in tree.nodes
                if node.type == "TEX_IMAGE" and node.image is not None
            )
        )
        node_count = len(tree.nodes)
        image_count = len(bpy.data.images)
        bpy.context.view_layer.objects.active = target
        target.select_set(True)
        bpy.ops.object.mode_set(mode="TEXTURE_PAINT")
        mode = bpy.context.mode
        canvas = bpy.context.scene.tool_settings.image_paint.canvas
        brush = bpy.context.scene.tool_settings.image_paint.brush
        settings.paint_enabled = True
        try:
            for _index in range(100):
                activate_layer_paint_image(bpy.context, str(layer.layer_uuid))
                activate_mask_paint_image(
                    bpy.context,
                    str(layer.layer_uuid),
                    str(layer.mask.mask_uuid),
                )
                activate_mask_paint_image(
                    bpy.context,
                    str(layer.layer_uuid),
                    str(adjustment.mask.mask_uuid),
                )
            self.assertEqual(bpy.context.mode, mode)
            self.assertIs(bpy.context.scene.tool_settings.image_paint.canvas, canvas)
            self.assertIs(bpy.context.scene.tool_settings.image_paint.brush, brush)
            self.assertEqual(len(tree.nodes), node_count)
            self.assertEqual(len(bpy.data.images), image_count)
            self.assertEqual(
                tuple(
                    sorted(
                        (link.from_socket.as_pointer(), link.to_socket.as_pointer())
                        for link in tree.links
                    )
                ),
                links,
            )
            self.assertEqual(
                tuple(
                    sorted(
                        (node.as_pointer(), node.image.as_pointer())
                        for node in tree.nodes
                        if node.type == "TEX_IMAGE" and node.image is not None
                    )
                ),
                node_images,
            )
            self.assertIs(settings.active_paint_image, adjustment.mask.image)
            active_slot = int(setup.material.paint_active_slot)
            self.assertIs(
                tuple(setup.material.texture_paint_images)[active_slot],
                adjustment.mask.image,
            )
        finally:
            settings.paint_enabled = False
            bpy.ops.object.mode_set(mode="OBJECT")

    def test_remove_bottom_layer_preserves_base_input_and_owned_cleanup(self) -> None:
        """맨 아래 일반 Layer 삭제가 Original과 생존 Paint Image를 보존합니다."""
        _target, setup = _prepare_backend()
        settings = bpy.context.scene.projection_paint
        bpy.ops.projection_paint.layer_add()
        top, bottom = _normal_layers(settings)
        survivor_uuid = str(top.layer_uuid)
        original = _original_layer(settings)
        original_uuid = str(original.layer_uuid)
        original_image = original.image
        bottom_image_name = bottom.image.name
        self.assertEqual(
            bpy.ops.projection_paint.layer_select(
                layer_uuid=bottom.layer_uuid,
            ),
            {"FINISHED"},
        )
        self.assertEqual(
            bpy.ops.projection_paint.layer_remove(),
            {"FINISHED"},
        )
        self.assertEqual(len(settings.layers), 2)
        self.assertIsNone(bpy.data.images.get(bottom_image_name))
        survivor = settings.layers[0]
        self.assertIs(settings.overlay_image, original_image)
        self.assertIs(settings.active_paint_image, original_image)
        validate_layer_graph(setup.material, _layer_uuids(settings))
        survivor_mix = _layer_mix(setup.material, survivor_uuid)
        original_mix = _layer_mix(setup.material, original_uuid)
        self.assertEqual(len(survivor_mix.inputs[1].links), 1)
        self.assertEqual(
            survivor_mix.inputs[1].links[0].from_node.as_pointer(),
            original_mix.as_pointer(),
        )
        self.assertIs(_original_layer(settings).image, original_image)
        self.assertEqual(
            len(_owned_nodes(setup.material, LAYER_MIX_NODE_ROLE)),
            2,
        )

    def test_remove_only_layer_creates_fresh_layer_at_requested_resolution(self) -> None:
        """마지막 Layer 삭제가 생존 항목 없이 새 해상도 Layer 1을 만듭니다."""
        _target, setup = _prepare_backend("LastLayerResetTarget")
        settings = bpy.context.scene.projection_paint
        layer = _normal_layers(settings)[0]
        original_uuid = str(layer.layer_uuid)
        bpy.ops.projection_paint.layer_mask_add(layer_uuid=layer.layer_uuid)
        bpy.ops.projection_paint.layer_adjustment_add(
            layer_uuid=layer.layer_uuid,
            adjustment_type="BRIGHTNESS",
        )
        bpy.ops.projection_paint.adjustment_mask_add(
            layer_uuid=layer.layer_uuid,
            adjustment_uuid=layer.adjustments[0].adjustment_uuid,
        )
        previous_names = {
            layer.image.name,
            layer.mask.image.name,
            layer.adjustments[0].mask.image.name,
        }
        self.assertEqual(
            bpy.ops.projection_paint.layer_select(layer_uuid=layer.layer_uuid),
            {"FINISHED"},
        )

        self.assertEqual(bpy.ops.projection_paint.layer_remove(), {"FINISHED"})
        original = _original_layer(settings)
        original_uuid = str(original.layer_uuid)
        self.assertEqual(
            bpy.ops.projection_paint.layer_select(layer_uuid=original.layer_uuid),
            {"FINISHED"},
        )

        result = bpy.ops.projection_paint.layer_remove(
            reset_width=128,
            reset_height=64,
        )

        self.assertEqual(result, {"FINISHED"})
        self.assertEqual(len(settings.layers), 1)
        replacement = settings.layers[0]
        self.assertEqual(replacement.name, "Layer 1")
        self.assertEqual(str(replacement.layer_uuid), original_uuid)
        self.assertEqual(tuple(replacement.image.size), (128, 64))
        self.assertEqual(len(replacement.adjustments), 0)
        self.assertFalse(str(replacement.mask.mask_uuid or ""))
        self.assertTrue(
            all(bpy.data.images.get(name) is None for name in previous_names)
        )
        self.assertIs(settings.overlay_image, replacement.image)
        self.assertIs(settings.active_paint_image, replacement.image)
        validate_layer_graph(setup.material, (str(replacement.layer_uuid),))

    def test_remove_active_top_layer_rebuilds_principled_output(self) -> None:
        """활성 최상단 Layer 삭제 후 생존 최상단 Mix가 Principled에 연결됩니다."""
        _target, setup = _prepare_backend("LayerTopDeleteTarget")
        settings = bpy.context.scene.projection_paint
        bpy.ops.projection_paint.layer_add()
        top, bottom = _normal_layers(settings)
        bottom_name = str(bottom.name)
        bottom_uuid = str(bottom.layer_uuid)
        deleted_name = top.image.name
        self.assertIs(settings.overlay_image, top.image)
        self.assertEqual(bpy.ops.projection_paint.layer_remove(), {"FINISHED"})
        self.assertEqual(
            [layer.name for layer in settings.layers],
            [bottom_name, _original_layer(settings).name],
        )
        self.assertIsNone(bpy.data.images.get(deleted_name))
        self.assertIs(settings.overlay_image, settings.layers[0].image)
        self.assertIs(settings.active_paint_image, settings.layers[0].image)
        principled = next(
            node
            for node in setup.material.node_tree.nodes
            if node.type == "BSDF_PRINCIPLED"
        )
        self.assertEqual(
            principled.inputs["Base Color"].links[0].from_node.as_pointer(),
            _layer_mix(
                setup.material,
                settings.layers[0].layer_uuid,
            ).as_pointer(),
        )
        validate_layer_graph(
            setup.material,
            _layer_uuids(settings),
        )

    def test_remove_previewed_layer_clears_preview_and_restores_stack(self) -> None:
        """단독 보기 중인 Layer 삭제가 체크를 해제하고 생존 합성을 복구합니다."""
        _target, setup = _prepare_backend("LayerPreviewDeleteTarget")
        settings = bpy.context.scene.projection_paint
        self.assertEqual(bpy.ops.projection_paint.layer_add(), {"FINISHED"})
        deleted_uuid = str(settings.layers[0].layer_uuid)
        survivor_uuid = str(_normal_layers(settings)[1].layer_uuid)
        self.assertEqual(
            bpy.ops.projection_paint.layer_preview_toggle(
                layer_uuid=deleted_uuid,
            ),
            {"FINISHED"},
        )
        self.assertEqual(settings.layer_preview_uuid, deleted_uuid)
        self.assertEqual(bpy.ops.projection_paint.layer_remove(), {"FINISHED"})
        self.assertEqual(settings.layer_preview_uuid, "")
        self.assertEqual(settings.mask_preview_adjustment_uuid, "")
        self.assertNotIn(LAYER_PREVIEW_UUID_KEY, setup.material)
        principled = next(
            node
            for node in setup.material.node_tree.nodes
            if node.type == "BSDF_PRINCIPLED"
        )
        self.assertEqual(
            principled.inputs["Base Color"].links[0].from_node.as_pointer(),
            _layer_mix(setup.material, survivor_uuid).as_pointer(),
        )
        validate_layer_graph(setup.material, _layer_uuids(settings))

    def test_remove_middle_layer_rebuilds_lower_to_upper_chain(self) -> None:
        """세 Layer의 중간을 삭제하면 하위 Mix가 상위 Mix에 직접 연결됩니다."""
        _target, setup = _prepare_backend("LayerMiddleDeleteTarget")
        settings = bpy.context.scene.projection_paint
        bpy.ops.projection_paint.layer_add()
        bpy.ops.projection_paint.layer_add()
        top, middle, bottom = _normal_layers(settings)
        top_name = str(top.name)
        top_uuid = str(top.layer_uuid)
        bottom_name = str(bottom.name)
        bottom_uuid = str(bottom.layer_uuid)
        deleted_name = middle.image.name
        self.assertEqual(
            bpy.ops.projection_paint.layer_select(
                layer_uuid=middle.layer_uuid,
            ),
            {"FINISHED"},
        )
        self.assertEqual(bpy.ops.projection_paint.layer_remove(), {"FINISHED"})
        self.assertEqual(
            [layer.name for layer in settings.layers],
            [top_name, bottom_name, _original_layer(settings).name],
        )
        self.assertIsNone(bpy.data.images.get(deleted_name))
        top_mix = _layer_mix(setup.material, top_uuid)
        bottom_mix = _layer_mix(setup.material, bottom_uuid)
        self.assertEqual(
            top_mix.inputs[1].links[0].from_node.as_pointer(),
            bottom_mix.as_pointer(),
        )
        self.assertIs(settings.overlay_image, settings.layers[1].image)
        self.assertIs(settings.active_paint_image, settings.layers[1].image)
        validate_layer_graph(
            setup.material,
            _layer_uuids(settings),
        )

    def test_remove_button_with_active_layer_mask_deletes_only_mask(self) -> None:
        """활성 Layer Mask에서 빼기 버튼은 부모 Layer를 보존하고 Mask만 지웁니다."""
        _target, setup = _prepare_backend("LayerMaskDeleteTarget")
        settings = bpy.context.scene.projection_paint
        bpy.ops.projection_paint.layer_add()
        selected, survivor = _normal_layers(settings)
        selected_uuid = str(selected.layer_uuid)
        survivor_uuid = str(survivor.layer_uuid)
        self.assertEqual(
            bpy.ops.projection_paint.layer_mask_add(
                layer_uuid=selected.layer_uuid,
            ),
            {"FINISHED"},
        )
        mask_name = selected.mask.image.name
        self.assertTrue(settings.paint_mask_mode)
        self.assertIs(
            settings.active_paint_image,
            selected.mask.image,
        )
        self.assertEqual(bpy.ops.projection_paint.layer_remove(), {"FINISHED"})
        self.assertEqual(len(settings.layers), 3)
        self.assertEqual(str(settings.layers[0].layer_uuid), selected_uuid)
        self.assertFalse(str(settings.layers[0].mask.mask_uuid or ""))
        self.assertFalse(settings.paint_mask_mode)
        self.assertIs(settings.overlay_image, selected.image)
        self.assertIs(settings.active_paint_image, selected.image)
        self.assertIsNone(bpy.data.images.get(mask_name))
        validate_layer_graph(setup.material, _layer_uuids(settings))

    def test_remove_selected_parent_deletes_all_owned_children(self) -> None:
        """부모 Layer 선택 삭제가 자체 Mask와 보정·보정 Mask까지 함께 지웁니다."""
        _target, setup = _prepare_backend("LayerParentDeleteTarget")
        settings = bpy.context.scene.projection_paint
        bpy.ops.projection_paint.layer_add()
        deleted, survivor = _normal_layers(settings)
        survivor_uuid = str(survivor.layer_uuid)
        bpy.ops.projection_paint.layer_mask_add(layer_uuid=deleted.layer_uuid)
        bpy.ops.projection_paint.layer_adjustment_add(
            layer_uuid=deleted.layer_uuid,
            adjustment_type="BRIGHTNESS",
        )
        adjustment = deleted.adjustments[0]
        bpy.ops.projection_paint.adjustment_mask_add(
            layer_uuid=deleted.layer_uuid,
            adjustment_uuid=adjustment.adjustment_uuid,
        )
        deleted_images = {
            deleted.image.name,
            deleted.mask.image.name,
            adjustment.mask.image.name,
        }
        self.assertEqual(
            bpy.ops.projection_paint.layer_select(layer_uuid=deleted.layer_uuid),
            {"FINISHED"},
        )

        self.assertEqual(bpy.ops.projection_paint.layer_remove(), {"FINISHED"})

        self.assertEqual(len(settings.layers), 2)
        self.assertEqual(str(settings.layers[0].layer_uuid), survivor_uuid)
        self.assertTrue(
            all(bpy.data.images.get(name) is None for name in deleted_images)
        )
        validate_layer_graph(setup.material, _layer_uuids(settings))

    def test_last_layer_replaces_image_without_changing_node_or_canvas(self) -> None:
        """마지막 Layer 초기화는 UUID·노드·Mode·Brush·Canvas를 유지합니다."""
        _target, setup = _prepare_backend("LayerResetInvariantTarget")
        settings = bpy.context.scene.projection_paint
        layer = _normal_layers(settings)[0]
        image_node = _layer_image_node(setup.material, layer.layer_uuid)
        old_image = layer.image
        old_image_name = old_image.name
        canvas = bpy.context.scene.tool_settings.image_paint.canvas
        mode = bpy.context.mode
        brush = bpy.context.scene.tool_settings.image_paint.brush

        replacement = replace_with_fresh_layer(
            bpy.context,
            128,
            64,
            use_base_resolution=False,
        )

        self.assertEqual(str(layer.layer_uuid), str(settings.layers[0].layer_uuid))
        self.assertEqual(
            _layer_image_node(setup.material, layer.layer_uuid).as_pointer(),
            image_node.as_pointer(),
        )
        self.assertIs(layer.image, replacement)
        self.assertIs(image_node.image, replacement)
        self.assertIsNone(bpy.data.images.get(old_image_name))
        self.assertIs(bpy.context.scene.tool_settings.image_paint.canvas, canvas)
        self.assertEqual(bpy.context.mode, mode)
        self.assertIs(bpy.context.scene.tool_settings.image_paint.brush, brush)

    def test_last_layer_image_replacement_100_times_in_texture_paint(self) -> None:
        """Texture Paint 상태에서 Image만 100회 교체해도 노드와 자원 수가 유지됩니다."""
        target, setup = _prepare_backend("LayerResetStressTarget")
        settings = bpy.context.scene.projection_paint
        layer = _normal_layers(settings)[0]
        image_node = _layer_image_node(setup.material, layer.layer_uuid)
        node_pointer = image_node.as_pointer()
        bpy.context.view_layer.objects.active = target
        target.select_set(True)
        bpy.ops.object.mode_set(mode="TEXTURE_PAINT")
        mode = bpy.context.mode
        canvas = bpy.context.scene.tool_settings.image_paint.canvas
        brush = bpy.context.scene.tool_settings.image_paint.brush
        settings.paint_enabled = True
        try:
            node_count = None
            image_count = None
            for _index in range(100):
                replacement = replace_with_fresh_layer(
                    bpy.context,
                    64,
                    64,
                    use_base_resolution=False,
                )
                self.assertIs(layer.image, replacement)
                self.assertIs(image_node.image, replacement)
                if _index == 0:
                    node_count = len(setup.material.node_tree.nodes)
                    image_count = len(bpy.data.images)
            self.assertEqual(bpy.context.mode, mode)
            self.assertIs(bpy.context.scene.tool_settings.image_paint.canvas, canvas)
            self.assertIs(bpy.context.scene.tool_settings.image_paint.brush, brush)
            self.assertEqual(image_node.as_pointer(), node_pointer)
            self.assertEqual(len(setup.material.node_tree.nodes), node_count)
            self.assertEqual(len(bpy.data.images), image_count)
            validate_layer_graph(setup.material, _layer_uuids(settings))
        finally:
            settings.paint_enabled = False
            bpy.ops.object.mode_set(mode="OBJECT")

    def test_managed_frames_do_not_overlap_user_nodes_or_each_other(self) -> None:
        """관리 Frame 예약 사각형이 사용자 Node 아래에서 서로 교차하지 않습니다."""
        _target, setup = _prepare_backend()
        tree = setup.material.node_tree
        user_a = tree.nodes.new("ShaderNodeTexNoise")
        user_a.location = (-1200.0, -600.0)
        user_b = tree.nodes.new("ShaderNodeValToRGB")
        user_b.location = (900.0, -900.0)
        bpy.ops.projection_paint.layer_add()
        bpy.ops.projection_paint.layer_add()
        bpy.context.view_layer.update()
        reconcile_layer_graph(bpy.context.scene)
        rectangles = managed_node_rectangles(setup.material)
        self.assertEqual(len(rectangles), 4)
        for first, second in combinations(rectangles, 2):
            separated = (
                first[3] <= second[1]
                or second[3] <= first[1]
                or first[4] <= second[2]
                or second[4] <= first[2]
            )
            self.assertTrue(separated, f"관리 Frame이 겹쳤습니다: {first}, {second}")
        unmanaged = tuple(
            node
            for node in tree.nodes
            if node.get(OWNER_KEY) != OWNER_TOKEN
        )
        unmanaged_bottom = min(
            float(node.location_absolute.y)
            - (
                float(node.dimensions.y)
                if float(node.dimensions.y) > 1.0
                else 180.0
            )
            for node in unmanaged
        )
        self.assertLess(max(rectangle[4] for rectangle in rectangles), unmanaged_bottom)

    def test_all_layer_and_mask_images_are_save_authorities(self) -> None:
        """모든 Layer와 Mask Image가 Registry와 저장·재열기에서 유지됩니다."""
        _target, _setup = _prepare_backend()
        settings = bpy.context.scene.projection_paint
        bpy.ops.projection_paint.layer_add()
        layer = settings.layers[1]
        bpy.ops.projection_paint.layer_mask_add(layer_uuid=layer.layer_uuid)
        bpy.ops.projection_paint.layer_adjustment_add(
            layer_uuid=layer.layer_uuid,
            adjustment_type="BRIGHTNESS",
        )
        adjustment = layer.adjustments[0]
        bpy.ops.projection_paint.adjustment_mask_add(
            layer_uuid=layer.layer_uuid,
            adjustment_uuid=adjustment.adjustment_uuid,
        )
        settings.layers[0].visible = False
        names = {
            image.name
            for image in authoritative_overlays()
        }
        expected = {
            layer.image.name
            for layer in settings.layers
        }
        expected.add(layer.mask.image.name)
        expected.add(layer.adjustments[0].mask.image.name)
        self.assertEqual(names, expected)
        self.assertEqual(len(rebuild_overlay_registry()), 5)
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "layer_graph.blend")
            bpy.ops.wm.save_as_mainfile(filepath=path)
            bpy.ops.wm.open_mainfile(filepath=path)
        restored = bpy.context.scene.projection_paint
        self.assertEqual(len(restored.layers), 3)
        self.assertTrue(all(layer.image is not None for layer in restored.layers))
        self.assertIsNotNone(restored.layers[1].mask.image)
        self.assertIsNotNone(restored.layers[1].adjustments[0].mask.image)
        self.assertFalse(restored.layers[0].visible)

    def test_resolution_replacement_reuses_survivor_node(self) -> None:
        """전체 초기화가 생존 Layer 노드를 유지하고 Image만 교체합니다."""
        _target, setup = _prepare_backend("LayerResizeTarget")
        settings = bpy.context.scene.projection_paint
        bpy.ops.projection_paint.layer_add()
        for layer in tuple(settings.layers):
            bpy.ops.projection_paint.layer_mask_add(layer_uuid=layer.layer_uuid)
            bpy.ops.projection_paint.layer_adjustment_add(
                layer_uuid=layer.layer_uuid,
                adjustment_type="BRIGHTNESS",
            )
            bpy.ops.projection_paint.adjustment_mask_add(
                layer_uuid=layer.layer_uuid,
                adjustment_uuid=layer.adjustments[0].adjustment_uuid,
            )
        survivor = settings.layers[0]
        survivor_uuid = str(survivor.layer_uuid)
        survivor_node = _layer_image_node(setup.material, survivor_uuid)
        old_names = {
            image.name
            for layer in settings.layers
            for image in (
                layer.image,
                layer.mask.image,
                layer.adjustments[0].mask.image,
            )
        }

        replacement = replace_with_fresh_layer(
            bpy.context,
            128,
            64,
            use_base_resolution=False,
        )

        self.assertEqual(len(settings.layers), 1)
        self.assertEqual(str(settings.layers[0].layer_uuid), survivor_uuid)
        self.assertEqual(
            _layer_image_node(setup.material, survivor_uuid).as_pointer(),
            survivor_node.as_pointer(),
        )
        self.assertIs(settings.layers[0].image, replacement)
        self.assertIs(survivor_node.image, replacement)
        self.assertEqual(tuple(replacement.size), (128, 64))
        self.assertEqual(len(settings.layers[0].adjustments), 0)
        self.assertFalse(str(settings.layers[0].mask.mask_uuid or ""))
        self.assertTrue(
            all(bpy.data.images.get(name) is None for name in old_names)
        )
        self.assertIs(settings.overlay_image, replacement)
        self.assertIs(settings.active_paint_image, replacement)
        _validate_active_graph(setup.material)
    def test_resolution_replacement_image_creation_failure_is_non_destructive(self) -> None:
        """새 Image 생성 실패가 기존 Layer·노드·Image를 변경하지 않습니다."""
        _target, setup = _prepare_backend("LayerResizeFailureTarget")
        settings = bpy.context.scene.projection_paint
        layer = settings.layers[0]
        layer_uuid = str(layer.layer_uuid)
        old_image = layer.image
        image_node = _layer_image_node(setup.material, layer_uuid)
        active_node = setup.material.node_tree.nodes.active

        with mock.patch(
            "tools.projection_paint.blender.layer_reset._new_image",
            side_effect=LayerShaderError("강제 Image 생성 실패"),
        ):
            with self.assertRaisesRegex(
                LayerShaderError,
                "강제 Image 생성 실패",
            ):
                replace_with_fresh_layer(
                    bpy.context,
                    128,
                    64,
                    use_base_resolution=False,
                )

        self.assertEqual(len(settings.layers), 2)
        self.assertEqual(str(settings.layers[0].layer_uuid), layer_uuid)
        self.assertIs(settings.layers[0].image, old_image)
        self.assertIs(image_node.image, old_image)
        self.assertEqual(
            setup.material.node_tree.nodes.active.as_pointer(),
            active_node.as_pointer(),
        )
        self.assertIs(settings.active_paint_image, old_image)
        self.assertIs(bpy.data.images.get(old_image.name), old_image)
        _validate_active_graph(setup.material)

    def test_resolution_replacement_graph_failure_restores_full_stack(self) -> None:
        """적용 후 Graph 검증 실패는 Layer 자식·선택·Preview·활성 Image를 모두 복구합니다."""
        _target, setup = _prepare_backend("LayerResizeGraphRollbackTarget")
        settings = bpy.context.scene.projection_paint
        bpy.ops.projection_paint.layer_add()
        selected = settings.layers[1]
        bpy.ops.projection_paint.layer_mask_add(layer_uuid=selected.layer_uuid)
        bpy.ops.projection_paint.layer_adjustment_add(
            layer_uuid=selected.layer_uuid,
            adjustment_type="BRIGHTNESS",
        )
        adjustment = selected.adjustments[0]
        bpy.ops.projection_paint.adjustment_mask_add(
            layer_uuid=selected.layer_uuid,
            adjustment_uuid=adjustment.adjustment_uuid,
        )
        settings.active_layer_index = 1
        settings.active_mask_uuid = str(selected.mask.mask_uuid)
        settings.paint_mask_mode = True
        settings.active_paint_image = selected.mask.image
        settings.layer_preview_uuid = str(settings.layers[0].layer_uuid)
        before = (
            tuple(
                (
                    str(layer.layer_uuid),
                    layer.image.as_pointer(),
                    str(layer.mask.mask_uuid or ""),
                    layer.mask.image.as_pointer() if layer.mask.image else 0,
                    tuple(
                        (
                            str(item.adjustment_uuid),
                            str(item.mask.mask_uuid or ""),
                            item.mask.image.as_pointer() if item.mask.image else 0,
                        )
                        for item in layer.adjustments
                    ),
                )
                for layer in settings.layers
            ),
            int(settings.active_layer_index),
            str(settings.active_mask_uuid),
            bool(settings.paint_mask_mode),
            settings.active_paint_image.as_pointer(),
            str(settings.layer_preview_uuid),
        )

        with mock.patch.object(
            layer_reset,
            "validate_active_layer_graph",
            side_effect=LayerShaderError("주입된 초기화 Graph 오류"),
        ):
            with self.assertRaisesRegex(LayerShaderError, "주입된 초기화 Graph 오류"):
                replace_with_fresh_layer(
                    bpy.context,
                    128,
                    64,
                    use_base_resolution=False,
                )

        after = (
            tuple(
                (
                    str(layer.layer_uuid),
                    layer.image.as_pointer(),
                    str(layer.mask.mask_uuid or ""),
                    layer.mask.image.as_pointer() if layer.mask.image else 0,
                    tuple(
                        (
                            str(item.adjustment_uuid),
                            str(item.mask.mask_uuid or ""),
                            item.mask.image.as_pointer() if item.mask.image else 0,
                        )
                        for item in layer.adjustments
                    ),
                )
                for layer in settings.layers
            ),
            int(settings.active_layer_index),
            str(settings.active_mask_uuid),
            bool(settings.paint_mask_mode),
            settings.active_paint_image.as_pointer(),
            str(settings.layer_preview_uuid),
        )
        self.assertEqual(after, before)
        _validate_active_graph(setup.material)

    def test_two_layer_alpha_chain_renders_expected_linear_color(self) -> None:
        """불투명 파랑 위 50% 빨강 Layer가 선형 보라색으로 렌더링됩니다."""
        target, setup = _prepare_backend("LayerRenderTarget")
        settings = bpy.context.scene.projection_paint
        bpy.ops.projection_paint.layer_add()
        top, bottom = _normal_layers(settings)
        _fill_image(bottom.image, (0.0, 0.0, 1.0, 1.0))
        _fill_image(top.image, (1.0, 0.0, 0.0, 0.5))
        color = _render_center_color(target, setup.material)
        self.assertAlmostEqual(color[0], 0.5, delta=0.02)
        self.assertAlmostEqual(color[1], 0.0, delta=0.02)
        self.assertAlmostEqual(color[2], 0.5, delta=0.02)
        self.assertAlmostEqual(color[3], 1.0, delta=0.02)
        top.visible = False
        hidden_color = _render_center_color(target, setup.material)
        self.assertAlmostEqual(hidden_color[0], 0.0, delta=0.02)
        self.assertAlmostEqual(hidden_color[1], 0.0, delta=0.02)
        self.assertAlmostEqual(hidden_color[2], 1.0, delta=0.02)
        self.assertAlmostEqual(hidden_color[3], 1.0, delta=0.02)


if __name__ == "__main__":
    unittest.main(argv=[__file__], verbosity=2)
