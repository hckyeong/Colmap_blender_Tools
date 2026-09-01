"""Blender Background에서 실제 Original Layer 이전과 명시적 복원을 검증합니다."""

from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

import bpy


PROJECT_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PROJECT_ROOT))

from tools import projection_paint as addon  # noqa: E402
from tools.projection_paint.blender import layer_shader  # noqa: E402
from tools.projection_paint.blender.layer_shader import (  # noqa: E402
    LAYER_IMAGE_NODE_ROLE,
    LAYER_IMAGE_ROLE,
    LAYER_MIX_NODE_ROLE,
    LayerShaderError,
    ORIGINAL_GATE_NODE_ROLE,
    ORIGINAL_LAYER_SCHEMA_VERSION,
    _new_image,
    reconcile_layer_graph,
    remove_layer_resources,
    restore_original_layer,
    validate_active_layer_graph,
)
from tools.projection_paint.blender.material_setup import (  # noqa: E402
    prepare_active_material,
)
from tools.projection_paint.blender.ownership import (  # noqa: E402
    OWNER_KEY,
    OWNER_TOKEN,
    RECORD_KIND_KEY,
)
from tools.projection_paint.blender.paint_state import (  # noqa: E402
    commit_paint_setup,
)
from tools.projection_paint.blender.paint_session import (  # noqa: E402
    _paint_entry_has_foreign_state,
)


def _new_target(name: str):
    """테스트에 필요한 한 면 UV Mesh와 Principled Material을 만듭니다."""
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


def _prepare_backend(name: str):
    """구형 Base Color 입력에서 관리 Layer Stack을 처음 준비합니다."""
    scene = bpy.context.scene
    target = _new_target(name)
    setup = prepare_active_material(target, 32, 16, use_base_resolution=False)
    commit_paint_setup(scene, setup)
    reconcile_layer_graph(scene, material=setup.material, overlay=setup.overlay)
    setup.commit()
    return target, setup


def _prepare_legacy_gate_backend(name: str, *, material_socket: bool = False):
    """실패 주입용 구형 Gate 상태와 그 상태의 비교 기준을 준비합니다."""
    target, setup = _prepare_backend(name)
    settings = bpy.context.scene.projection_paint
    original = next(layer for layer in settings.layers if layer.is_original_layer)
    remove_layer_resources(bpy.context, str(original.layer_uuid))
    settings.original_layer_schema_version = 0
    settings.original_source_image = None
    settings.original_source_color = (0.8, 0.8, 0.8, 1.0)
    settings.original_source_node_uuid = ""
    settings.original_source_socket_identifier = ""
    tree = setup.material.node_tree
    source = tree.nodes.new(
        "ShaderNodeMixRGB" if material_socket else "ShaderNodeRGB"
    )
    if material_socket:
        source.blend_type = "ADD"
        source.inputs[0].default_value = 0.5
        source.inputs[1].default_value = (0.1, 0.2, 0.3, 1.0)
        source.inputs[2].default_value = (0.2, 0.1, 0.0, 1.0)
    else:
        source.outputs["Color"].default_value = (0.2, 0.4, 0.6, 1.0)
    gate = tree.nodes.new("ShaderNodeMixRGB")
    gate[OWNER_KEY] = OWNER_TOKEN
    gate[RECORD_KIND_KEY] = ORIGINAL_GATE_NODE_ROLE
    gate.inputs[0].default_value = 1.0
    gate.inputs[1].default_value = (0.0, 0.0, 0.0, 1.0)
    tree.links.new(source.outputs["Color"], gate.inputs[2])
    bottom = settings.layers[-1]
    bottom_mix = next(
        node
        for node in tree.nodes
        if node.get(OWNER_KEY) == OWNER_TOKEN
        and node.get(RECORD_KIND_KEY) == LAYER_MIX_NODE_ROLE
        and str(node.get("pp_layer_uuid", "")) == str(bottom.layer_uuid)
    )
    for link in tuple(bottom_mix.inputs[1].links):
        tree.links.remove(link)
    tree.links.new(gate.outputs["Color"], bottom_mix.inputs[1])
    return target, setup, gate


def _legacy_gate_state(setup):
    """구형 Gate와 기존 Layer·Image·Link가 변경되지 않았는지 비교할 값을 만듭니다."""
    settings = bpy.context.scene.projection_paint
    tree = setup.material.node_tree
    return {
        "layers": tuple(
            (
                layer.as_pointer(),
                str(layer.layer_uuid),
                layer.image.as_pointer() if layer.image is not None else 0,
                bool(layer.is_original_layer),
            )
            for layer in settings.layers
        ),
        "images": frozenset(image.as_pointer() for image in bpy.data.images),
        "nodes": frozenset(node.as_pointer() for node in tree.nodes),
        "links": frozenset(
            (
                link.from_node.as_pointer(),
                str(link.from_socket.identifier),
                link.to_node.as_pointer(),
                str(link.to_socket.identifier),
            )
            for link in tree.links
        ),
        "active_layer_index": int(settings.active_layer_index),
        "active_paint_image": (
            settings.active_paint_image.as_pointer()
            if settings.active_paint_image is not None
            else 0
        ),
        "layer_preview_uuid": str(settings.layer_preview_uuid or ""),
        "mask_preview_adjustment_uuid": str(
            settings.mask_preview_adjustment_uuid or ""
        ),
        "original_layer_preview": bool(settings.original_layer_preview),
    }


class OriginalLayerTests(unittest.TestCase):
    """Original Layer가 일반 관리 Layer와 같은 수명 주기를 갖는지 검증합니다."""

    @classmethod
    def setUpClass(cls) -> None:
        """테스트에 필요한 Extension Property와 Operator를 등록합니다."""
        addon.register()

    @classmethod
    def tearDownClass(cls) -> None:
        """테스트 종료 뒤 등록한 Extension 타입을 해제합니다."""
        addon.unregister()

    def setUp(self) -> None:
        """각 테스트를 빈 Factory Scene에서 시작합니다."""
        bpy.ops.wm.read_factory_settings(use_empty=True)

    def test_original_layer_is_real_bottom_layer_without_gate(self) -> None:
        """최초 전환은 Gate 대신 편집 가능한 실제 하단 Original Layer를 만듭니다."""
        _target, setup = _prepare_backend("OriginalLayerReal")
        settings = bpy.context.scene.projection_paint
        originals = [layer for layer in settings.layers if layer.is_original_layer]
        self.assertEqual(len(originals), 1)
        self.assertEqual(
            settings.layers[-1].as_pointer(),
            originals[0].as_pointer(),
        )
        self.assertNotEqual(originals[0].image.as_pointer(), setup.overlay.as_pointer())
        self.assertFalse(
            any(
                node.get(OWNER_KEY) == OWNER_TOKEN
                and node.get(RECORD_KIND_KEY) == ORIGINAL_GATE_NODE_ROLE
                for node in setup.material.node_tree.nodes
            )
        )
        validate_active_layer_graph(bpy.context.scene, setup.material)

    def test_deleted_original_requires_explicit_restore(self) -> None:
        """Original 삭제 뒤 자동 재생성 없이 명시적 복원만 현재 Stack 계약을 사용합니다."""
        _target, setup = _prepare_backend("OriginalLayerRestore")
        settings = bpy.context.scene.projection_paint
        original = next(layer for layer in settings.layers if layer.is_original_layer)
        expected_size = tuple(int(value) for value in settings.layers[0].image.size)
        remove_layer_resources(bpy.context, str(original.layer_uuid))
        self.assertFalse(any(layer.is_original_layer for layer in settings.layers))
        reconcile_layer_graph(bpy.context.scene)
        self.assertFalse(any(layer.is_original_layer for layer in settings.layers))
        self.assertEqual(
            bpy.ops.projection_paint.original_layer_restore(),
            {"FINISHED"},
        )
        restored = next(layer for layer in settings.layers if layer.is_original_layer)
        self.assertTrue(restored.is_original_layer)
        self.assertEqual(tuple(int(value) for value in restored.image.size), expected_size)
        self.assertEqual(
            settings.layers[-1].as_pointer(),
            restored.as_pointer(),
        )
        self.assertIn("Original Layer 복원 완료", settings.status)
        self.assertIn("32 × 16", settings.status)
        self.assertEqual(settings.status_kind, "SUCCESS")
        validate_active_layer_graph(bpy.context.scene, setup.material)

    def test_deleting_only_original_replaces_it_with_plain_layer_one(self) -> None:
        """유일한 Original 삭제의 마지막 Layer 교체는 Original 표식 없이 Restore를 남깁니다."""
        _target, setup = _prepare_backend("OriginalLayerOnlyDelete")
        settings = bpy.context.scene.projection_paint
        original = next(layer for layer in settings.layers if layer.is_original_layer)
        original_uuid = str(original.layer_uuid)
        ordinary = next(layer for layer in settings.layers if not layer.is_original_layer)
        remove_layer_resources(bpy.context, str(ordinary.layer_uuid))
        self.assertEqual(len(settings.layers), 1)
        self.assertTrue(settings.layers[0].is_original_layer)
        self.assertEqual(
            bpy.ops.projection_paint.layer_select(layer_uuid=original_uuid),
            {"FINISHED"},
        )
        self.assertEqual(
            bpy.ops.projection_paint.layer_remove(),
            {"FINISHED"},
        )
        self.assertEqual(len(settings.layers), 1)
        self.assertEqual(settings.layers[0].name, "Layer 1")
        self.assertFalse(settings.layers[0].is_original_layer)
        replacement_size = tuple(int(value) for value in settings.layers[0].image.size)
        reconcile_layer_graph(bpy.context.scene)
        self.assertFalse(any(layer.is_original_layer for layer in settings.layers))
        restored = restore_original_layer(bpy.context)
        self.assertTrue(restored.is_original_layer)
        self.assertEqual(tuple(int(value) for value in restored.image.size), replacement_size)
        validate_active_layer_graph(bpy.context.scene, setup.material)

    def test_original_layer_uses_regular_layer_operators(self) -> None:
        """Original Layer는 선택·이름·Mask·보정·이동·삭제에서 일반 Layer 예외가 없습니다."""
        _target, setup = _prepare_backend("OriginalLayerOperators")
        settings = bpy.context.scene.projection_paint
        original = next(layer for layer in settings.layers if layer.is_original_layer)
        original_uuid = str(original.layer_uuid)
        self.assertEqual(
            bpy.ops.projection_paint.layer_select(layer_uuid=original_uuid),
            {"FINISHED"},
        )
        self.assertEqual(settings.active_paint_image.as_pointer(), original.image.as_pointer())
        self.assertEqual(
            bpy.ops.projection_paint.layer_rename(
                layer_uuid=original_uuid,
                new_name="Original Working",
            ),
            {"FINISHED"},
        )
        self.assertEqual(original.name, "Original Working")
        self.assertEqual(
            bpy.ops.projection_paint.layer_mask_add(layer_uuid=original_uuid),
            {"FINISHED"},
        )
        self.assertTrue(original.mask.mask_uuid)
        self.assertEqual(
            bpy.ops.projection_paint.layer_adjustment_add(
                layer_uuid=original_uuid,
                adjustment_type="BRIGHTNESS",
            ),
            {"FINISHED"},
        )
        self.assertEqual(len(original.adjustments), 1)
        self.assertEqual(
            bpy.ops.projection_paint.layer_move(direction="UP"),
            {"FINISHED"},
        )
        self.assertEqual(settings.layers[0].layer_uuid, original_uuid)
        self.assertEqual(
            bpy.ops.projection_paint.layer_move(direction="DOWN"),
            {"FINISHED"},
        )
        self.assertEqual(settings.layers[-1].layer_uuid, original_uuid)
        remove_layer_resources(bpy.context, original_uuid)
        self.assertFalse(any(layer.is_original_layer for layer in settings.layers))
        validate_active_layer_graph(bpy.context.scene, setup.material)

    def test_external_texture_source_file_is_not_modified(self) -> None:
        """외부 원본은 복제·패킹 없이 실제 Original Layer에서 직접 참조합니다."""
        target = _new_target("OriginalLayerExternal")
        material = target.data.materials[0]
        tree = material.node_tree
        principled = next(node for node in tree.nodes if node.type == "BSDF_PRINCIPLED")
        with tempfile.TemporaryDirectory(dir=str(PROJECT_ROOT)) as directory:
            bpy.ops.wm.save_as_mainfile(
                filepath=str(Path(directory) / "relative_source.blend"),
            )
            source = bpy.data.images.new("OriginalLayerExternalSource", 8, 8, alpha=True)
            external_path = str(Path(directory) / "external.png")
            pixels = [0.25, 0.5, 0.75, 1.0] * (8 * 8)
            source.pixels.foreach_set(pixels)
            source.update()
            source.filepath_raw = external_path
            source.file_format = "PNG"
            source.save()
            source_path = bpy.path.relpath(external_path)
            source.filepath = source_path
            self.assertEqual(source.source, "FILE")
            self.assertTrue(source_path.startswith("//"))
            source_node = tree.nodes.new("ShaderNodeTexImage")
            source_node.image = source
            tree.links.new(source_node.outputs["Color"], principled.inputs["Base Color"])
            setup = prepare_active_material(target, 4, 4, use_base_resolution=False)
            commit_paint_setup(bpy.context.scene, setup)
            setup.overlay.colorspace_settings.name = "Non-Color"
            setup.overlay.alpha_mode = "PREMUL"
            source.colorspace_settings.name = "sRGB"
            source.alpha_mode = "STRAIGHT"
            reconcile_layer_graph(
                bpy.context.scene,
                material=setup.material,
                overlay=setup.overlay,
            )
            setup.commit()
            settings = bpy.context.scene.projection_paint
            original = next(layer for layer in settings.layers if layer.is_original_layer)
            self.assertEqual(str(source.filepath), source_path)
            self.assertNotEqual(source.get(OWNER_KEY), OWNER_TOKEN)
            self.assertIs(settings.original_source_image, source)
            self.assertIs(original.image, source)
            self.assertIsNone(source.packed_file)
            self.assertEqual(tuple(int(value) for value in source.size), (8, 8))
            self.assertEqual(source.colorspace_settings.name, "sRGB")
            self.assertEqual(source.alpha_mode, "STRAIGHT")
            self.assertEqual(tuple(int(value) for value in setup.overlay.size), (4, 4))

            original_uuid = str(original.layer_uuid)
            self.assertEqual(
                bpy.ops.projection_paint.layer_select(layer_uuid=original_uuid),
                {"FINISHED"},
            )
            self.assertIsNone(settings.active_paint_image)
            self.assertIs(settings.overlay_image, setup.overlay)
            self.assertEqual(
                bpy.ops.projection_paint.layer_mask_add(layer_uuid=original_uuid),
                {"FINISHED"},
            )
            self.assertEqual(
                bpy.ops.projection_paint.layer_mask_select(
                    layer_uuid=original_uuid,
                    mask_uuid=str(original.mask.mask_uuid),
                ),
                {"FINISHED"},
            )
            self.assertTrue(settings.paint_mask_mode)
            self.assertIs(settings.active_paint_image, original.mask.image)
            self.assertIs(settings.overlay_image, setup.overlay)
            self.assertEqual(
                bpy.ops.projection_paint.layer_preview_toggle(
                    layer_uuid=original_uuid,
                ),
                {"FINISHED"},
            )
            self.assertEqual(settings.layer_preview_uuid, original_uuid)

            blend_path = str(Path(directory) / "external_reference.blend")
            self.assertEqual(
                bpy.ops.wm.save_as_mainfile(filepath=blend_path, check_existing=False),
                {"FINISHED"},
            )
            self.assertEqual(
                bpy.ops.wm.open_mainfile(filepath=blend_path),
                {"FINISHED"},
            )
            restored = bpy.context.scene.projection_paint
            restored_original = next(
                layer for layer in restored.layers if layer.is_original_layer
            )
            self.assertEqual(restored.layer_preview_uuid, restored_original.layer_uuid)
            self.assertIs(restored_original.image, restored.original_source_image)
            self.assertIsNone(restored_original.image.packed_file)
            self.assertEqual(
                Path(bpy.path.abspath(restored_original.image.filepath)).resolve(),
                Path(external_path).resolve(),
            )
            restored_pixel = tuple(float(value) for value in restored_original.image.pixels[:4])
            for actual, expected in zip(restored_pixel, (0.25, 0.5, 0.75, 1.0)):
                self.assertAlmostEqual(actual, expected, delta=1.0 / 255.0)
            self.assertTrue(restored.paint_mask_mode)
            self.assertIs(restored.active_paint_image, restored_original.mask.image)
            self.assertIsNot(restored.overlay_image, restored_original.image)
            self.assertFalse(
                _paint_entry_has_foreign_state(restored, restored.paint_target),
                "외부 Original 직접 참조를 다른 Paint Target 자원으로 오판했습니다.",
            )
            reconcile_layer_graph(bpy.context.scene)
            preview = next(
                node
                for node in restored.paint_material.node_tree.nodes
                if node.get(RECORD_KIND_KEY)
                == layer_shader.LAYER_SOLO_PREVIEW_MIX_NODE_ROLE
            )
            principled = next(
                node
                for node in restored.paint_material.node_tree.nodes
                if node.type == "BSDF_PRINCIPLED"
            )
            self.assertEqual(
                principled.inputs["Base Color"].links[0].from_node.as_pointer(),
                preview.as_pointer(),
            )
            validate_active_layer_graph(bpy.context.scene, restored.paint_material)

            writable = next(
                layer for layer in restored.layers if not layer.is_original_layer
            )
            self.assertEqual(
                bpy.ops.projection_paint.layer_select(
                    layer_uuid=str(writable.layer_uuid),
                ),
                {"FINISHED"},
            )
            self.assertEqual(
                bpy.ops.projection_paint.layer_remove(),
                {"FINISHED"},
            )
            self.assertEqual(len(restored.layers), 2)
            reset_original = next(
                layer for layer in restored.layers if layer.is_original_layer
            )
            reset_writable = next(
                layer for layer in restored.layers if not layer.is_original_layer
            )
            self.assertIs(reset_original.image, restored.original_source_image)
            self.assertIsNot(reset_writable.image, reset_original.image)

    def test_legacy_managed_original_migrates_to_external_reference(self) -> None:
        """기존 Schema 1 복제 Original을 Source 직접 참조로 한 번만 이관합니다."""
        target = _new_target("OriginalLayerExternalMigration")
        material = target.data.materials[0]
        tree = material.node_tree
        principled = next(node for node in tree.nodes if node.type == "BSDF_PRINCIPLED")
        with tempfile.TemporaryDirectory(dir=str(PROJECT_ROOT)) as directory:
            source = bpy.data.images.new("OriginalLayerMigrationSource", 8, 8, alpha=True)
            source.filepath_raw = str(Path(directory) / "migration_source.png")
            source.file_format = "PNG"
            source.save()
            source_node = tree.nodes.new("ShaderNodeTexImage")
            source_node.image = source
            tree.links.new(source_node.outputs["Color"], principled.inputs["Base Color"])
            setup = prepare_active_material(target, 4, 4, use_base_resolution=False)
            commit_paint_setup(bpy.context.scene, setup)
            reconcile_layer_graph(
                bpy.context.scene,
                material=setup.material,
                overlay=setup.overlay,
            )
            setup.commit()
            settings = bpy.context.scene.projection_paint
            original = next(layer for layer in settings.layers if layer.is_original_layer)
            legacy = _new_image(
                target,
                "LegacyOriginalClone",
                (4, 4),
                role=LAYER_IMAGE_ROLE,
                layer_uuid=str(original.layer_uuid),
                color=(0.0, 0.0, 0.0, 0.0),
                reference=setup.overlay,
            )
            legacy_name = str(legacy.name)
            original.image = legacy
            image_node = next(
                node
                for node in setup.material.node_tree.nodes
                if node.get(RECORD_KIND_KEY) == LAYER_IMAGE_NODE_ROLE
                and str(node.get("pp_layer_uuid", "")) == str(original.layer_uuid)
            )
            image_node.image = legacy
            settings.original_layer_schema_version = 1

            reconcile_layer_graph(bpy.context.scene)

            migrated = next(layer for layer in settings.layers if layer.is_original_layer)
            self.assertIs(migrated.image, source)
            self.assertEqual(
                int(settings.original_layer_schema_version),
                ORIGINAL_LAYER_SCHEMA_VERSION,
            )
            self.assertIsNone(bpy.data.images.get(legacy_name))

    def test_missing_external_original_stops_without_replacing_reference(self) -> None:
        """외부 원본 파일이 없으면 직접 참조를 유지하고 명확한 오류로 중단합니다."""
        target = _new_target("OriginalLayerMissingExternal")
        material = target.data.materials[0]
        tree = material.node_tree
        principled = next(node for node in tree.nodes if node.type == "BSDF_PRINCIPLED")
        with tempfile.TemporaryDirectory(dir=str(PROJECT_ROOT)) as directory:
            path = Path(directory) / "missing_source.png"
            source = bpy.data.images.new("OriginalLayerMissingSource", 8, 8, alpha=True)
            source.filepath_raw = str(path)
            source.file_format = "PNG"
            source.save()
            source_node = tree.nodes.new("ShaderNodeTexImage")
            source_node.image = source
            tree.links.new(source_node.outputs["Color"], principled.inputs["Base Color"])
            setup = prepare_active_material(target, 4, 4, use_base_resolution=False)
            commit_paint_setup(bpy.context.scene, setup)
            reconcile_layer_graph(
                bpy.context.scene,
                material=setup.material,
                overlay=setup.overlay,
            )
            setup.commit()
            settings = bpy.context.scene.projection_paint
            original = next(layer for layer in settings.layers if layer.is_original_layer)
            path.unlink()

            with self.assertRaisesRegex(LayerShaderError, "파일을 찾을 수 없습니다"):
                reconcile_layer_graph(bpy.context.scene)

            self.assertIs(original.image, source)
            self.assertIs(settings.original_source_image, source)

    def test_material_socket_provenance_restores_without_working_image_backup(self) -> None:
        """Material Socket Restore는 별도 대형 Image 없이 보존한 Node provenance를 다시 Bake합니다."""
        target, setup = _prepare_backend("OriginalLayerSocket")
        settings = bpy.context.scene.projection_paint
        current = next(layer for layer in settings.layers if layer.is_original_layer)
        remove_layer_resources(bpy.context, str(current.layer_uuid))
        settings.original_layer_schema_version = 0
        settings.original_source_image = None
        material = setup.material
        tree = material.node_tree
        source = tree.nodes.new("ShaderNodeMixRGB")
        source.blend_type = "ADD"
        source.inputs[0].default_value = 0.5
        source.inputs[1].default_value = (0.2, 0.1, 0.0, 1.0)
        source.inputs[2].default_value = (0.0, 0.3, 0.4, 1.0)
        gate = tree.nodes.new("ShaderNodeMixRGB")
        gate[OWNER_KEY] = OWNER_TOKEN
        gate[RECORD_KIND_KEY] = ORIGINAL_GATE_NODE_ROLE
        gate.inputs[0].default_value = 1.0
        gate.inputs[1].default_value = (0.0, 0.0, 0.0, 1.0)
        tree.links.new(source.outputs["Color"], gate.inputs[2])
        layer_mix = next(
            node
            for node in tree.nodes
            if node.get(OWNER_KEY) == OWNER_TOKEN
            and node.get(RECORD_KIND_KEY) == LAYER_MIX_NODE_ROLE
        )
        for link in tuple(layer_mix.inputs[1].links):
            tree.links.remove(link)
        tree.links.new(gate.outputs["Color"], layer_mix.inputs[1])
        bpy.context.view_layer.objects.active = target
        target.select_set(True)
        reconcile_layer_graph(bpy.context.scene)
        original = next(layer for layer in settings.layers if layer.is_original_layer)
        self.assertIsNone(settings.original_source_image)
        source_uuid = str(settings.original_source_node_uuid)
        self.assertTrue(source_uuid)
        self.assertTrue(settings.original_source_socket_identifier)
        expected = tuple(float(value) for value in original.image.pixels[:4])
        for iteration in range(2):
            original.image.pixels[0] = (iteration + 1) / 3.0
            original.image.update()
            remove_layer_resources(bpy.context, str(original.layer_uuid))
            original = restore_original_layer(bpy.context)
            actual = tuple(float(value) for value in original.image.pixels[:4])
            for result, source_value in zip(actual, expected):
                self.assertAlmostEqual(result, source_value, places=5)
            self.assertEqual(settings.original_source_node_uuid, source_uuid)

    def _assert_legacy_migration_failure_restores_state(
        self,
        patch_target: str,
        replacement,
        *,
        material_socket: bool = False,
    ) -> None:
        """이관 단계별 실패 뒤 구형 Gate 상태가 완전히 남는지 공통으로 검증합니다."""
        _target, setup, gate = _prepare_legacy_gate_backend(
            f"OriginalRollback{patch_target}",
            material_socket=material_socket,
        )
        settings = bpy.context.scene.projection_paint
        before = _legacy_gate_state(setup)
        with mock.patch.object(layer_shader, patch_target, replacement):
            with self.assertRaisesRegex(LayerShaderError, "주입된"):
                reconcile_layer_graph(bpy.context.scene)
        self.assertEqual(settings.original_layer_schema_version, 0)
        self.assertFalse(any(layer.is_original_layer for layer in settings.layers))
        self.assertEqual(gate.as_pointer(), next(
            node.as_pointer()
            for node in setup.material.node_tree.nodes
            if node.get(OWNER_KEY) == OWNER_TOKEN
            and node.get(RECORD_KIND_KEY) == ORIGINAL_GATE_NODE_ROLE
        ))
        self.assertEqual(_legacy_gate_state(setup), before)

    def test_migration_image_failure_rolls_back_legacy_gate(self) -> None:
        """Original Image 생성 실패는 Layer·Image·Gate Link를 남기지 않고 되돌립니다."""
        self._assert_legacy_migration_failure_restores_state(
            "_create_original_image",
            mock.Mock(side_effect=LayerShaderError("주입된 Image 오류")),
        )

    def test_migration_node_failure_rolls_back_legacy_gate(self) -> None:
        """Original Node 생성 뒤 실패해도 기존 Graph Link와 리소스를 복원합니다."""
        original_ensure = layer_shader._ensure_layer_nodes

        def fail_after_original_node(tree, layer, **kwargs):
            """Original Layer 노드 생성 뒤 롤백 경로를 검증하기 위해 실패시킵니다."""
            result = original_ensure(tree, layer, **kwargs)
            if layer.is_original_layer:
                raise LayerShaderError("주입된 Node 오류")
            return result

        self._assert_legacy_migration_failure_restores_state(
            "_ensure_layer_nodes",
            fail_after_original_node,
        )

    def test_migration_active_chain_failure_rolls_back_legacy_gate(self) -> None:
        """최종 활성 Chain 검증 실패는 이관 Commit 전에 구형 상태를 복원합니다."""
        self._assert_legacy_migration_failure_restores_state(
            "validate_active_layer_graph",
            mock.Mock(side_effect=LayerShaderError("주입된 활성 Chain 오류")),
        )

    def test_migration_source_tag_failure_rolls_back_legacy_gate(self) -> None:
        """Source provenance 태깅 실패는 Gate 제거 전 전체 이관을 되돌립니다."""
        self._assert_legacy_migration_failure_restores_state(
            "_tag_original_source_socket",
            mock.Mock(side_effect=LayerShaderError("주입된 Source 태그 오류")),
            material_socket=True,
        )

    def test_migration_paint_activation_failure_happens_before_commit(self) -> None:
        """Texture Paint 활성화 실패는 Gate 제거와 schema 확정 전에 전체 이관을 되돌립니다."""
        target, setup, _gate = _prepare_legacy_gate_backend("OriginalActivationRollback")
        before = _legacy_gate_state(setup)
        bpy.context.view_layer.objects.active = target
        target.select_set(True)
        bpy.ops.object.mode_set(mode="TEXTURE_PAINT")
        with mock.patch.object(
            layer_shader,
            "_activate_paint_image_node",
            side_effect=LayerShaderError("주입된 Paint 활성화 오류"),
        ):
            with self.assertRaisesRegex(LayerShaderError, "주입된 Paint 활성화 오류"):
                reconcile_layer_graph(bpy.context.scene)
        settings = bpy.context.scene.projection_paint
        self.assertEqual(settings.original_layer_schema_version, 0)
        self.assertFalse(any(layer.is_original_layer for layer in settings.layers))
        self.assertEqual(_legacy_gate_state(setup), before)

    def test_rollback_removes_only_new_source_provenance_tag(self) -> None:
        """Gate 제거 직전 실패는 새 provenance 태그만 지우고 기존 태그는 보존합니다."""
        for existing in (False, True):
            with self.subTest(existing=existing):
                bpy.ops.wm.read_factory_settings(use_empty=True)
                _target, setup, gate = _prepare_legacy_gate_backend(
                    f"OriginalSourceTagRollback{existing}",
                    material_socket=True,
                )
                source_node = gate.inputs[2].links[0].from_node
                previous = "existing-source-token" if existing else ""
                if existing:
                    source_node[layer_shader.ORIGINAL_SOURCE_NODE_UUID_KEY] = previous
                with mock.patch.object(
                    layer_shader,
                    "_remove_original_gates",
                    side_effect=LayerShaderError("주입된 Gate 제거 오류"),
                ):
                    with self.assertRaisesRegex(LayerShaderError, "주입된 Gate 제거 오류"):
                        reconcile_layer_graph(bpy.context.scene)
                if existing:
                    self.assertEqual(
                        source_node[layer_shader.ORIGINAL_SOURCE_NODE_UUID_KEY],
                        previous,
                    )
                else:
                    self.assertNotIn(
                        layer_shader.ORIGINAL_SOURCE_NODE_UUID_KEY,
                        source_node,
                    )

    def test_legacy_original_solo_becomes_regular_original_layer_preview(self) -> None:
        """구형 Original solo는 이관 뒤 실제 Original Layer의 일반 미리 보기로 이어집니다."""
        _target, setup, _gate = _prepare_legacy_gate_backend("OriginalLegacySolo")
        settings = bpy.context.scene.projection_paint
        settings.original_layer_preview = True
        layer_shader._apply_preview(bpy.context.scene, setup.material)
        reconcile_layer_graph(bpy.context.scene)
        original = next(layer for layer in settings.layers if layer.is_original_layer)
        self.assertFalse(settings.original_layer_preview)
        self.assertEqual(settings.layer_preview_uuid, original.layer_uuid)
        self.assertEqual(
            setup.material[layer_shader.LAYER_PREVIEW_UUID_KEY],
            original.layer_uuid,
        )
        self.assertNotIn(layer_shader.ORIGINAL_PREVIEW_KEY, setup.material)
        validate_active_layer_graph(bpy.context.scene, setup.material)

    def test_legacy_migration_is_blocked_without_mutation_during_composite_states(self) -> None:
        """CREATING·RELEASING·STALE에서는 구형 이관이 어떤 리소스도 바꾸지 않습니다."""
        for state in ("CREATING", "RELEASING", "STALE"):
            with self.subTest(state=state):
                bpy.ops.wm.read_factory_settings(use_empty=True)
                _target, setup, _gate = _prepare_legacy_gate_backend(
                    f"OriginalBlocked{state}"
                )
                settings = bpy.context.scene.projection_paint
                before = _legacy_gate_state(setup)
                settings.composite_state = state
                with self.assertRaises(LayerShaderError):
                    reconcile_layer_graph(bpy.context.scene)
                self.assertEqual(settings.original_layer_schema_version, 0)
                self.assertFalse(any(layer.is_original_layer for layer in settings.layers))
                self.assertEqual(_legacy_gate_state(setup), before)


if __name__ == "__main__":
    unittest.main(argv=[__file__], verbosity=2)
