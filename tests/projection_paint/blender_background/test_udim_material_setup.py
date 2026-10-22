"""단일 UDIM 관리 재질 전환과 원본 보존 계약을 검증합니다."""

from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest

import bpy

ROOT = Path(__file__).resolve().parents[3] / "tools" / "projection_paint"
PACKAGE_PARENT = ROOT.parent
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))

from tools import projection_paint as addon  # noqa: E402
from tools.projection_paint.blender.material_setup import (  # noqa: E402
    MANAGED_OVERLAY_LEGACY_VALID,
    OWNER_KEY,
    PaintSetupError,
    classify_managed_overlay,
    create_paint_material_preparation_job,
    pending_overlay_resolution_change,
    prepare_active_material,
    requires_udim_conversion,
    validate_material_contract,
)
from tools.projection_paint.blender.layer_reset import (  # noqa: E402
    replace_with_fresh_layer,
)
from tools.projection_paint.blender.layer_shader import (  # noqa: E402
    reconcile_layer_graph,
)
from tools.projection_paint.blender.paint_state import (  # noqa: E402
    commit_paint_setup,
)
from tools.projection_paint.blender.overlay_durability import (  # noqa: E402
    unregister_overlay,
)
from tools.projection_paint.blender.udim_setup import (  # noqa: E402
    UDIM_MANAGED_SLOT_KEY,
    new_tiled_paint_image,
)
from tools.projection_paint.blender.image_backend import (  # noqa: E402
    IMAGE_BACKEND_KEY,
    SINGLE_BACKEND,
    UDIM_BACKEND,
)


def _new_two_face_target(name: str):
    """서로 분리된 두 사각형과 활성 UV를 가진 Mesh를 만듭니다."""
    mesh = bpy.data.meshes.new(f"{name}Mesh")
    mesh.from_pydata(
        (
            (0.0, 0.0, 0.0),
            (1.0, 0.0, 0.0),
            (1.0, 1.0, 0.0),
            (0.0, 1.0, 0.0),
            (2.0, 0.0, 0.0),
            (3.0, 0.0, 0.0),
            (3.0, 1.0, 0.0),
            (2.0, 1.0, 0.0),
        ),
        (),
        ((0, 1, 2, 3), (4, 5, 6, 7)),
    )
    uv = mesh.uv_layers.new(name="UVMap")
    corners = ((0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0))
    for polygon in mesh.polygons:
        for loop_index, value in zip(polygon.loop_indices, corners):
            uv.data[loop_index].uv = value
    target = bpy.data.objects.new(name, mesh)
    bpy.context.scene.collection.objects.link(target)
    return target


def _new_texture_material(name: str, image) -> object:
    """직접 Base Color Image를 가진 단순 Principled 재질을 만듭니다."""
    material = bpy.data.materials.new(name)
    material.use_nodes = True
    tree = material.node_tree
    principled = next(node for node in tree.nodes if node.type == "BSDF_PRINCIPLED")
    image_node = tree.nodes.new("ShaderNodeTexImage")
    image_node.image = image
    tree.links.new(image_node.outputs["Color"], principled.inputs["Base Color"])
    return material


def _write_source_image(directory: str, tile_name: str):
    """테스트용 Source PNG를 쓰고 실제 경로를 가진 Image를 반환합니다."""
    image = bpy.data.images.new(f"Source_{tile_name}", 8, 8, alpha=True)
    path = Path(directory) / f"fish_{tile_name}_diffuse.png"
    image.save_render(str(path))
    image.filepath = str(path)
    return image


class UDIMMaterialSetupTests(unittest.TestCase):
    """지원 입력과 실패 시 단일 재질 Transaction을 검증합니다."""

    @classmethod
    def setUpClass(cls):
        """테스트에 필요한 Extension 속성과 Handler를 등록합니다."""
        addon.register()

    @classmethod
    def tearDownClass(cls):
        """테스트가 등록한 Extension 자원을 해제합니다."""
        addon.unregister()

    def setUp(self):
        """각 테스트를 빈 Factory Scene에서 시작합니다."""
        bpy.ops.wm.read_factory_settings(use_empty=True)

    def test_uv_only_target_uses_single_file_material(self):
        """Texture 없는 UV Mesh를 SINGLE FILE 관리 재질로 전환합니다."""
        target = _new_two_face_target("UVOnlyTarget")
        self.assertTrue(requires_udim_conversion(target))

        setup = prepare_active_material(
            target,
            16,
            8,
            use_base_resolution=False,
        )

        self.assertEqual(len(target.material_slots), 1)
        self.assertEqual(tuple(p.material_index for p in target.data.polygons), (0, 0))
        self.assertEqual(target.data.uv_layers.active.name, "PP_UDIM")
        self.assertEqual(tuple(int(tile.number) for tile in setup.overlay.tiles), (1001,))
        self.assertEqual(str(setup.overlay.source), "FILE")
        self.assertEqual(str(target[IMAGE_BACKEND_KEY]), SINGLE_BACKEND)
        self.assertEqual(str(target["pp_udim_kind"]), "UV_ONLY")
        validate_material_contract(setup)

    def test_initial_managed_material_sets_surface_defaults_once(self):
        """최초 관리 재질만 Roughness·IOR를 1로 설정하고 재진입 값을 보존합니다."""
        target = _new_two_face_target("ManagedSurfaceDefaultsTarget")
        source = bpy.data.images.new("ManagedSurfaceDefaultsSource", 8, 8, alpha=True)
        source_material = _new_texture_material(
            "ManagedSurfaceDefaultsMaterial",
            source,
        )
        source_principled = next(
            node
            for node in source_material.node_tree.nodes
            if node.type == "BSDF_PRINCIPLED"
        )
        source_principled.inputs["Roughness"].default_value = 0.25
        source_principled.inputs["IOR"].default_value = 1.45
        target.data.materials.append(source_material)

        first = prepare_active_material(target, 8, 8, use_base_resolution=False)
        self.assertEqual(str(first.overlay.source), "FILE")
        self.assertEqual(str(target[IMAGE_BACKEND_KEY]), SINGLE_BACKEND)
        first_principled = next(
            node
            for node in first.material.node_tree.nodes
            if node.type == "BSDF_PRINCIPLED"
        )
        self.assertEqual(first_principled.inputs["Roughness"].default_value, 1.0)
        self.assertEqual(first_principled.inputs["IOR"].default_value, 1.0)
        self.assertAlmostEqual(
            source_principled.inputs["Roughness"].default_value,
            0.25,
        )
        self.assertAlmostEqual(source_principled.inputs["IOR"].default_value, 1.45)
        first.commit()

        first_principled.inputs["Roughness"].default_value = 0.4
        first_principled.inputs["IOR"].default_value = 1.2
        reused = prepare_active_material(target, 8, 8, use_base_resolution=False)
        reused_principled = next(
            node
            for node in reused.material.node_tree.nodes
            if node.type == "BSDF_PRINCIPLED"
        )
        self.assertAlmostEqual(
            reused_principled.inputs["Roughness"].default_value,
            0.4,
        )
        self.assertAlmostEqual(reused_principled.inputs["IOR"].default_value, 1.2)

    def test_reentry_restores_managed_slot_without_creating_resources(self):
        """원본 슬롯을 선택한 재진입은 기존 UDIM 자원과 관리 슬롯을 재사용합니다."""
        target = _new_two_face_target("ManagedReentryTarget")
        source = bpy.data.images.new("ManagedReentrySource", 8, 8, alpha=True)
        source_material = _new_texture_material("ManagedReentrySourceMaterial", source)
        target.data.materials.append(source_material)
        first = prepare_active_material(target, 8, 8, use_base_resolution=False)
        first.commit()
        managed_slot = int(target[UDIM_MANAGED_SLOT_KEY])
        managed_uv = target.data.uv_layers["PP_UDIM"]
        counts = (
            len(bpy.data.materials),
            len(bpy.data.images),
            len(target.material_slots),
            len(target.data.uv_layers),
        )

        target.active_material_index = 0
        self.assertFalse(requires_udim_conversion(target))
        reused = prepare_active_material(target, 8, 8, use_base_resolution=False)

        self.assertIs(reused.material, first.material)
        self.assertIs(reused.overlay, first.overlay)
        self.assertEqual(
            int(target.data.uv_layers["PP_UDIM"].as_pointer()),
            int(managed_uv.as_pointer()),
        )
        self.assertEqual(int(target.active_material_index), managed_slot)
        self.assertEqual(
            (
                len(bpy.data.materials),
                len(bpy.data.images),
                len(target.material_slots),
                len(target.data.uv_layers),
            ),
            counts,
        )

    def test_resolution_query_uses_managed_material_without_switching_slot(self):
        """해상도 조회는 원본 슬롯 선택을 유지하며 관리 Overlay만 판정합니다."""
        target = _new_two_face_target("ManagedResolutionTarget")
        source = bpy.data.images.new("ManagedResolutionSource", 8, 8, alpha=True)
        target.data.materials.append(
            _new_texture_material("ManagedResolutionSourceMaterial", source)
        )
        setup = prepare_active_material(target, 8, 8, use_base_resolution=False)
        setup.commit()
        target.active_material_index = 0

        change = pending_overlay_resolution_change(
            target,
            16,
            16,
            use_base_resolution=False,
        )

        self.assertEqual(change, ((8, 8), (16, 16)))
        self.assertEqual(int(target.active_material_index), 0)

    def test_base_resolution_reentry_preserves_external_original_and_overlay(self):
        """외부 Original 이관 뒤에도 기본 해상도로 재진입하며 기존 이미지와 원본을 보존합니다."""
        target = _new_two_face_target("ExternalOriginalReentryTarget")
        with tempfile.TemporaryDirectory() as directory:
            source_path = Path(directory) / "original.png"
            generated = bpy.data.images.new("OriginalReentryFixture", 32, 16, alpha=True)
            generated.save_render(str(source_path))
            source = bpy.data.images.load(str(source_path), check_existing=False)
            source_bytes = source_path.read_bytes()
            target.data.materials.append(_new_texture_material("ExternalOriginalReentryMaterial", source))
            setup = prepare_active_material(target, 64, 64, use_base_resolution=True)
            commit_paint_setup(bpy.context.scene, setup)
            reconcile_layer_graph(bpy.context.scene, material=setup.material, overlay=setup.overlay)
            setup.commit()
            settings = bpy.context.scene.projection_paint
            self.assertIs(settings.original_source_image, source)
            self.assertNotIn("pp_is_original_layer", source)
            overlay_pointer = setup.overlay.as_pointer()
            layer_pointers = tuple(layer.image.as_pointer() for layer in settings.layers)
            self.assertIsNone(pending_overlay_resolution_change(target, 64, 64, use_base_resolution=True))
            reused = prepare_active_material(target, 64, 64, use_base_resolution=True)
            self.assertEqual(reused.overlay.as_pointer(), overlay_pointer)
            self.assertEqual(tuple(reused.overlay.size), (32, 16))
            self.assertEqual(tuple(layer.image.as_pointer() for layer in settings.layers), layer_pointers)
            self.assertEqual(source_path.read_bytes(), source_bytes)
            self.assertEqual(pending_overlay_resolution_change(target, 64, 64, use_base_resolution=False), ((32, 16), (64, 64)))

    def test_out_of_range_managed_slot_fails_without_creating_resources(self):
        """범위를 벗어난 관리 슬롯 기록은 최초 변환으로 떨어지지 않습니다."""
        target = _new_two_face_target("BrokenManagedSlotTarget")
        setup = prepare_active_material(target, 8, 8, use_base_resolution=False)
        setup.commit()
        target[UDIM_MANAGED_SLOT_KEY] = len(target.material_slots) + 10
        counts = (len(bpy.data.materials), len(bpy.data.images), len(target.material_slots))

        with self.assertRaisesRegex(PaintSetupError, "Material Slot이 삭제"):
            prepare_active_material(target, 8, 8, use_base_resolution=False)

        self.assertEqual(
            (len(bpy.data.materials), len(bpy.data.images), len(target.material_slots)),
            counts,
        )

    def test_replaced_managed_slot_material_fails_without_reconversion(self):
        """관리 슬롯의 재질 교체는 자원을 만들지 않고 손상 오류로 중단합니다."""
        target = _new_two_face_target("ReplacedManagedMaterialTarget")
        setup = prepare_active_material(target, 8, 8, use_base_resolution=False)
        setup.commit()
        replacement = bpy.data.materials.new("UnexpectedReplacement")
        replacement.use_nodes = True
        target.data.materials[int(target[UDIM_MANAGED_SLOT_KEY])] = replacement
        counts = (len(bpy.data.materials), len(bpy.data.images), len(target.material_slots))

        with self.assertRaisesRegex(PaintSetupError, "재질 또는 소유 태그"):
            prepare_active_material(target, 8, 8, use_base_resolution=False)

        self.assertEqual(
            (len(bpy.data.materials), len(bpy.data.images), len(target.material_slots)),
            counts,
        )

    def test_damaged_managed_owner_tag_fails_without_reconversion(self):
        """관리 재질 소유 태그 손상은 원본 분석이나 새 자원 생성으로 이어지지 않습니다."""
        target = _new_two_face_target("BrokenManagedOwnerTarget")
        setup = prepare_active_material(target, 8, 8, use_base_resolution=False)
        setup.commit()
        setup.material[OWNER_KEY] = "BROKEN"
        counts = (len(bpy.data.materials), len(bpy.data.images), len(target.material_slots))

        with self.assertRaisesRegex(PaintSetupError, "재질 또는 소유 태그"):
            prepare_active_material(target, 8, 8, use_base_resolution=False)

        self.assertEqual(
            (len(bpy.data.materials), len(bpy.data.images), len(target.material_slots)),
            counts,
        )

    def test_partial_udim_metadata_fails_without_creating_resources(self):
        """일부 UDIM 기록만 남은 Target은 새 변환을 시작하지 않습니다."""
        target = _new_two_face_target("PartialManagedMetadataTarget")
        target["pp_udim_kind"] = "UV_ONLY"
        counts = (len(bpy.data.materials), len(bpy.data.images), len(target.material_slots))

        with self.assertRaisesRegex(PaintSetupError, "일부만 남아 재변환"):
            prepare_active_material(target, 8, 8, use_base_resolution=False)

        self.assertEqual(
            (len(bpy.data.materials), len(bpy.data.images), len(target.material_slots)),
            counts,
        )
        self.assertNotIn("pp_target_uuid", target)

    def test_existing_udim_source_and_uv_are_borrowed_without_offset(self):
        """기존 단일 UDIM Source와 좌표를 유지하고 Layer만 같은 타일로 만듭니다."""
        target = _new_two_face_target("ExistingUDIMTarget")
        source = new_tiled_paint_image(
            "ExistingSource",
            (8, 8),
            (1001, 1002),
            (0.2, 0.4, 0.6, 1.0),
        )
        material = _new_texture_material("ExistingUDIMMaterial", source)
        target.data.materials.append(material)
        uv = target.data.uv_layers.active
        for loop_index in target.data.polygons[1].loop_indices:
            uv.data[loop_index].uv.x += 1.0
        original_values = tuple(tuple(item.uv) for item in uv.data)

        setup = prepare_active_material(target, 8, 8)

        self.assertIs(target.material_slots[0].material, material)
        self.assertIs(target.material_slots[1].material, setup.material)
        self.assertEqual(tuple(int(tile.number) for tile in source.tiles), (1001, 1002))
        self.assertEqual(tuple(int(tile.number) for tile in setup.overlay.tiles), (1001, 1002))
        self.assertEqual(str(setup.overlay.source), "TILED")
        self.assertEqual(str(target[IMAGE_BACKEND_KEY]), UDIM_BACKEND)
        self.assertEqual(
            tuple(tuple(item.uv) for item in target.data.uv_layers["UVMap"].data),
            original_values,
        )
        self.assertEqual(
            tuple(tuple(item.uv) for item in target.data.uv_layers["PP_UDIM"].data),
            original_values,
        )
        validate_material_contract(setup)

    def test_existing_udim_reset_resamples_every_original_tile(self):
        """기존 UDIM Original을 목표 해상도로 초기화할 때 모든 타일을 리샘플합니다."""
        target = _new_two_face_target("ExistingUDIMResetTarget")
        source = new_tiled_paint_image(
            "ExistingResetSource",
            (8, 8),
            (1001, 1002),
            (0.2, 0.4, 0.6, 1.0),
        )
        material = _new_texture_material("ExistingUDIMResetMaterial", source)
        target.data.materials.append(material)
        uv = target.data.uv_layers.active
        for loop_index in target.data.polygons[1].loop_indices:
            uv.data[loop_index].uv.x += 1.0
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

        replacement = replace_with_fresh_layer(
            bpy.context,
            4,
            4,
            use_base_resolution=False,
        )

        self.assertEqual(
            tuple(int(tile.number) for tile in replacement.tiles),
            (1001, 1002),
        )
        self.assertEqual(
            tuple(tuple(int(value) for value in tile.size) for tile in replacement.tiles),
            ((4, 4), (4, 4)),
        )
        self.assertIs(bpy.context.scene.projection_paint.original_source_image, source)

    def test_multi_material_reset_uses_consolidated_original_without_source_mutation(self):
        """다중 재질 reset은 통합 UDIM Original 하나를 만들고 원본 슬롯과 파일을 보존합니다."""
        with tempfile.TemporaryDirectory() as directory:
            target = _new_two_face_target("MultiMaterialResetTarget")
            sources = (
                _write_source_image(directory, "u1_v1"),
                _write_source_image(directory, "u2_v1"),
            )
            materials = tuple(
                _new_texture_material(f"MultiResetMaterial{index}", image)
                for index, image in enumerate(sources)
            )
            for material in materials:
                target.data.materials.append(material)
            target.data.polygons[0].material_index = 0
            target.data.polygons[1].material_index = 1
            slots_before = tuple(slot.material.as_pointer() for slot in target.material_slots)
            files_before = tuple((str(image.filepath), Path(image.filepath).read_bytes()) for image in sources)
            setup = prepare_active_material(target, 8, 8, use_base_resolution=False)
            commit_paint_setup(bpy.context.scene, setup)
            reconcile_layer_graph(bpy.context.scene, material=setup.material, overlay=setup.overlay)
            consolidated = bpy.context.scene.projection_paint.original_source_image

            replacement = replace_with_fresh_layer(bpy.context, 4, 4, use_base_resolution=False)

            settings = bpy.context.scene.projection_paint
            self.assertEqual(len(settings.layers), 1)
            self.assertTrue(settings.layers[0].is_original_layer)
            self.assertEqual(tuple(int(tile.number) for tile in replacement.tiles), (1001, 1002))
            self.assertEqual(tuple(tuple(tile.size) for tile in replacement.tiles), ((4, 4), (4, 4)))
            self.assertIs(settings.original_source_image, consolidated)
            self.assertEqual(tuple(slot.material.as_pointer() for slot in target.material_slots[:2]), slots_before)
            self.assertEqual(
                tuple((str(image.filepath), Path(image.filepath).read_bytes()) for image in sources),
                files_before,
            )

    def test_multi_material_rollback_restores_faces_uv_and_slots(self):
        """미확정 다중 재질 전환을 Rollback하면 원본 Mesh 상태를 복구합니다."""
        with tempfile.TemporaryDirectory() as directory:
            target = _new_two_face_target("RollbackTarget")
            sources = (
                _write_source_image(directory, "u1_v1"),
                _write_source_image(directory, "u2_v1"),
            )
            materials = tuple(
                _new_texture_material(f"SourceMaterial{index}", image)
                for index, image in enumerate(sources)
            )
            for material in materials:
                target.data.materials.append(material)
            target.data.polygons[0].material_index = 0
            target.data.polygons[1].material_index = 1
            original_paths = tuple(str(image.filepath) for image in sources)

            setup = prepare_active_material(target, 8, 8)
            managed_name = setup.material.name
            overlay_name = setup.overlay.name
            setup.rollback()

            self.assertEqual(len(target.material_slots), 2)
            self.assertEqual(tuple(p.material_index for p in target.data.polygons), (0, 1))
            self.assertEqual(target.data.uv_layers.active.name, "UVMap")
            self.assertIsNone(target.data.uv_layers.get("PP_UDIM"))
            self.assertIsNone(target.data.attributes.get("pp_original_material_index"))
            self.assertNotIn("pp_udim_tiles", target)
            self.assertEqual(tuple(str(image.filepath) for image in sources), original_paths)
            self.assertIsNone(bpy.data.materials.get(managed_name))
            self.assertIsNone(bpy.data.images.get(overlay_name))

    def test_incompatible_multi_material_fails_without_mutation(self):
        """Shader가 다른 다중 재질은 Blender ID를 만들기 전에 거부합니다."""
        with tempfile.TemporaryDirectory() as directory:
            target = _new_two_face_target("RejectedTarget")
            first = _new_texture_material(
                "FirstMaterial",
                _write_source_image(directory, "u1_v1"),
            )
            second = _new_texture_material(
                "SecondMaterial",
                _write_source_image(directory, "u2_v1"),
            )
            principled = next(
                node for node in second.node_tree.nodes if node.type == "BSDF_PRINCIPLED"
            )
            principled.inputs["Roughness"].default_value = 0.25
            target.data.materials.append(first)
            target.data.materials.append(second)
            target.data.polygons[0].material_index = 0
            target.data.polygons[1].material_index = 1
            image_count = len(bpy.data.images)
            material_count = len(bpy.data.materials)

            with self.assertRaises(PaintSetupError):
                prepare_active_material(target, 8, 8)

            self.assertEqual(len(bpy.data.images), image_count)
            self.assertEqual(len(bpy.data.materials), material_count)
            self.assertEqual(len(target.material_slots), 2)
            self.assertEqual(tuple(p.material_index for p in target.data.polygons), (0, 1))
            self.assertIsNone(target.data.uv_layers.get("PP_UDIM"))
            self.assertNotIn("pp_target_uuid", target)

    def test_progress_job_reports_real_monotonic_conversion_stages(self):
        """최초 UDIM 변환 Job이 실제 단계와 함께 단조 증가하는지 확인합니다."""

        target = _new_two_face_target("ProgressTarget")
        job = create_paint_material_preparation_job(
            target,
            16,
            8,
            use_base_resolution=False,
        )
        snapshots = [job.progress]
        while not job.done:
            snapshots.append(job.advance())

        self.assertIsNotNone(job.result)
        self.assertEqual(
            [snapshot.fraction for snapshot in snapshots],
            sorted(snapshot.fraction for snapshot in snapshots),
        )
        self.assertEqual(1.0, snapshots[-1].fraction)
        self.assertTrue(
            {
                "Analyzing materials and UV",
                "Creating paint image",
                "Creating PP_UDIM UV",
                "Applying single material",
                "Validating paint preparation",
            }.issubset({snapshot.phase for snapshot in snapshots})
        )
        validate_material_contract(job.result)
        job.result.rollback()

    def test_generated_source_stays_unchanged_for_single_backend(self):
        """GENERATED 일반 Source를 변경하지 않고 SINGLE Overlay를 준비합니다."""

        target = _new_two_face_target("GeneratedSingleTarget")
        source = bpy.data.images.new("GeneratedSingleSource", 8, 4, alpha=True)
        target.data.materials.append(_new_texture_material("GeneratedSingleMaterial", source))
        before = (
            str(source.source),
            str(source.filepath),
            str(source.filepath_raw),
            bool(source.is_dirty),
            int(source.as_pointer()),
        )

        setup = prepare_active_material(target, 8, 4, use_base_resolution=False)

        self.assertEqual(str(setup.overlay.source), "FILE")
        self.assertEqual(str(target[IMAGE_BACKEND_KEY]), SINGLE_BACKEND)
        self.assertEqual(
            before,
            (
                str(source.source),
                str(source.filepath),
                str(source.filepath_raw),
                bool(source.is_dirty),
                int(source.as_pointer()),
            ),
        )

    def test_file_source_maps_to_single_without_source_mutation(self):
        """일반 FILE Source를 변경하지 않고 SINGLE Overlay로 준비합니다."""

        with tempfile.TemporaryDirectory() as directory:
            generated = bpy.data.images.new("FileSourceWriter", 8, 4, alpha=True)
            path = Path(directory) / "single.png"
            generated.save_render(str(path))
            bpy.data.images.remove(generated)
            source = bpy.data.images.load(str(path), check_existing=False)
            target = _new_two_face_target("FileSingleTarget")
            target.data.materials.append(_new_texture_material("FileSingleMaterial", source))
            before = (
                str(source.source),
                str(source.filepath),
                str(source.filepath_raw),
                bool(source.is_dirty),
                path.read_bytes(),
            )

            setup = prepare_active_material(target, 8, 4, use_base_resolution=False)

            self.assertEqual(str(setup.overlay.source), "FILE")
            self.assertEqual(str(target[IMAGE_BACKEND_KEY]), SINGLE_BACKEND)
            self.assertEqual(
                before,
                (
                    str(source.source),
                    str(source.filepath),
                    str(source.filepath_raw),
                    bool(source.is_dirty),
                    path.read_bytes(),
                ),
            )

    def test_validate_rejects_explicit_backend_mix(self):
        """Target과 Overlay의 명시 백엔드가 섞이면 Material 계약을 거부합니다."""

        target = _new_two_face_target("MixedBackendTarget")
        setup = prepare_active_material(target, 8, 8, use_base_resolution=False)
        target[IMAGE_BACKEND_KEY] = UDIM_BACKEND

        with self.assertRaisesRegex(PaintSetupError, "백엔드"):
            validate_material_contract(setup)

    def test_legacy_tiled_1001_is_read_only_valid(self):
        """백엔드 키 없는 구형 TILED 1001 Stack을 픽셀 변환 없이 유효 분류합니다."""

        target = _new_two_face_target("LegacyTiledTarget")
        source = bpy.data.images.new("LegacySingleSource", 8, 8, alpha=True)
        target.data.materials.append(_new_texture_material("LegacySingleMaterial", source))
        setup = prepare_active_material(target, 8, 8, use_base_resolution=False)
        legacy_overlay = new_tiled_paint_image(
            "LegacyTiledOverlay",
            (8, 8),
            (1001,),
            (0.2, 0.3, 0.4, 1.0),
        )
        for key in setup.overlay.keys():
            legacy_overlay[key] = setup.overlay[key]
        unregister_overlay(setup.overlay)
        setup.image_node.image = legacy_overlay
        before = (int(legacy_overlay.as_pointer()), tuple(legacy_overlay.pixels[:16]))
        del target[IMAGE_BACKEND_KEY]

        classification = classify_managed_overlay(target, legacy_overlay)
        reused = prepare_active_material(target, 8, 8, use_base_resolution=False)

        self.assertEqual(classification.state, MANAGED_OVERLAY_LEGACY_VALID)
        self.assertIs(reused.overlay, legacy_overlay)
        self.assertEqual(
            before,
            (int(legacy_overlay.as_pointer()), tuple(legacy_overlay.pixels[:16])),
        )
        validate_material_contract(reused)

    def test_aborted_progress_job_rolls_back_partial_resources(self):
        """진행창 수명주기 중단이 생성 중인 Material과 Image를 모두 복구합니다."""

        target = _new_two_face_target("AbortProgressTarget")
        original_material_count = len(bpy.data.materials)
        original_image_count = len(bpy.data.images)
        job = create_paint_material_preparation_job(
            target,
            16,
            8,
            use_base_resolution=False,
        )
        while job.progress.completed_units < 6:
            job.advance()
        job.abort()

        self.assertEqual(original_material_count, len(bpy.data.materials))
        self.assertEqual(original_image_count, len(bpy.data.images))
        self.assertEqual(target.data.uv_layers.active.name, "UVMap")
        self.assertIsNone(target.data.uv_layers.get("PP_UDIM"))
        self.assertNotIn("pp_target_uuid", target)


if __name__ == "__main__":
    unittest.main(argv=[__file__], verbosity=2)
