"""Blender 5.2 Combined Bake가 투명 Source RGBA를 기록하는지 검증합니다."""

from __future__ import annotations

from pathlib import Path
import sys
import unittest

import bpy


ROOT = Path(__file__).resolve().parents[3] / "tools" / "projection_paint"
if str(ROOT.parent) not in sys.path:
    sys.path.insert(0, str(ROOT.parent))

from tools.projection_paint.blender.udim_setup import (  # noqa: E402
    new_tiled_paint_image,
)


def _new_target(name: str, *, multi_udim: bool) -> object:
    """지정한 UDIM 수만큼 UV 삼각형을 가진 작은 Bake 대상을 생성합니다."""
    mesh = bpy.data.meshes.new(f"{name}Mesh")
    if multi_udim:
        vertices = (
            (0.0, 0.0, 0.0),
            (1.0, 0.0, 0.0),
            (1.0, 1.0, 0.0),
            (1.0, 0.0, 0.0),
            (2.0, 0.0, 0.0),
            (2.0, 1.0, 0.0),
        )
        faces = ((0, 1, 2), (3, 4, 5))
        uvs = (
            (0.0, 0.0),
            (1.0, 0.0),
            (1.0, 1.0),
            (1.0, 0.0),
            (2.0, 0.0),
            (2.0, 1.0),
        )
    else:
        vertices = ((0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (1.0, 1.0, 0.0))
        faces = ((0, 1, 2),)
        uvs = ((0.0, 0.0), (1.0, 0.0), (1.0, 1.0))
    mesh.from_pydata(vertices, (), faces)
    uv_layer = mesh.uv_layers.new(name="UVMap")
    for loop, uv in zip(uv_layer.data, uvs):
        loop.uv = uv
    target = bpy.data.objects.new(name, mesh)
    bpy.context.scene.collection.objects.link(target)
    material = bpy.data.materials.new(f"{name}Material")
    material.use_nodes = True
    target.data.materials.append(material)
    return target


def _set_bake_target(material: object, image: object) -> None:
    """Combined Bake가 지정 Image Texture만 대상으로 사용하도록 활성 Node를 설정합니다."""
    tree = material.node_tree
    target = tree.nodes.new("ShaderNodeTexImage")
    target.image = image
    for node in tree.nodes:
        node.select = False
    target.select = True
    tree.nodes.active = target


def _configure_transparent_emission(material: object, alpha: float) -> None:
    """반투명 Emission을 Transparent Shader 위에 합성하는 최소 Surface를 구성합니다."""
    tree = material.node_tree
    output = next(node for node in tree.nodes if node.type == "OUTPUT_MATERIAL")
    for link in tuple(output.inputs["Surface"].links):
        tree.links.remove(link)
    transparent = tree.nodes.new("ShaderNodeBsdfTransparent")
    emission = tree.nodes.new("ShaderNodeEmission")
    emission.inputs["Color"].default_value = (0.8, 0.2, 0.1, 1.0)
    mix = tree.nodes.new("ShaderNodeMixShader")
    mix.inputs[0].default_value = alpha
    tree.links.new(transparent.outputs[0], mix.inputs[1])
    tree.links.new(emission.outputs[0], mix.inputs[2])
    tree.links.new(mix.outputs[0], output.inputs["Surface"])


def _configure_emission_mix(
    material: object,
    *,
    foreground_image: object | None = None,
) -> None:
    """직접 또는 Bake Image의 Alpha를 써서 불투명 아래 색 위에 합성하는 Surface를 구성합니다."""
    tree = material.node_tree
    output = next(node for node in tree.nodes if node.type == "OUTPUT_MATERIAL")
    for link in tuple(output.inputs["Surface"].links):
        tree.links.remove(link)
    mix = tree.nodes.new("ShaderNodeMixRGB")
    mix.blend_type = "MIX"
    mix.inputs[1].default_value = (0.1, 0.4, 0.2, 1.0)
    if foreground_image is None:
        mix.inputs[0].default_value = 0.5
        mix.inputs[2].default_value = (0.8, 0.2, 0.1, 1.0)
    else:
        image = tree.nodes.new("ShaderNodeTexImage")
        image.image = foreground_image
        separate = tree.nodes.new("ShaderNodeSeparateColor")
        separate.mode = "RGB"
        combine = tree.nodes.new("ShaderNodeCombineColor")
        combine.mode = "RGB"
        minimum_alpha = tree.nodes.new("ShaderNodeMath")
        minimum_alpha.operation = "MAXIMUM"
        minimum_alpha.inputs[0].default_value = 0.000001
        tree.links.new(image.outputs["Color"], separate.inputs["Color"])
        tree.links.new(image.outputs["Alpha"], minimum_alpha.inputs[1])
        for source_socket, target_socket in zip(
            (separate.outputs["Red"], separate.outputs["Green"], separate.outputs["Blue"]),
            (combine.inputs["Red"], combine.inputs["Green"], combine.inputs["Blue"]),
        ):
            divide = tree.nodes.new("ShaderNodeMath")
            divide.operation = "DIVIDE"
            tree.links.new(source_socket, divide.inputs[0])
            tree.links.new(minimum_alpha.outputs[0], divide.inputs[1])
            tree.links.new(divide.outputs[0], target_socket)
        tree.links.new(image.outputs["Alpha"], mix.inputs[0])
        tree.links.new(combine.outputs["Color"], mix.inputs[2])
    emission = tree.nodes.new("ShaderNodeEmission")
    tree.links.new(mix.outputs["Color"], emission.inputs["Color"])
    tree.links.new(emission.outputs["Emission"], output.inputs["Surface"])


def _pixel(image: object, tile_number: int) -> tuple[float, float, float, float]:
    """UDIM 타일 중앙의 RGBA 픽셀을 읽고 활성 타일을 원래대로 돌립니다."""
    previous = image.tiles.active
    tile = image.tiles.get(tile_number)
    if tile is None:
        raise AssertionError(f"UDIM 타일 {tile_number}을 찾을 수 없습니다.")
    try:
        image.tiles.active = tile
        width, height = (int(value) for value in image.size)
        offset = ((height // 4) * width + ((width * 3) // 4)) * 4
        return tuple(image.pixels[offset : offset + 4])
    finally:
        image.tiles.active = previous


class CompositeRgbaBakePrototypeTests(unittest.TestCase):
    """Transparent + Combined Bake의 RGBA 동작을 실제 Blender 5.2에서 확인합니다."""

    def setUp(self) -> None:
        """각 프로토타입을 빈 Factory Scene에서 실행합니다."""
        bpy.ops.wm.read_factory_settings(use_empty=True)

    def _bake(self, *, multi_udim: bool) -> object:
        """반투명 Surface를 하나 또는 두 UDIM 타일의 Image에 Combined Bake합니다."""
        scene = bpy.context.scene
        target = _new_target("CompositeRgbaPrototype", multi_udim=multi_udim)
        material = target.active_material
        _configure_transparent_emission(material, 0.5)
        image = new_tiled_paint_image(
            "CompositeRgbaPrototypeImage",
            (32, 32),
            (1001, 1002) if multi_udim else (1001,),
            (0.0, 0.0, 0.0, 0.0),
        )
        _set_bake_target(material, image)
        scene.render.engine = "CYCLES"
        scene.render.film_transparent = True
        scene.view_settings.look = "None"
        scene.view_settings.view_transform = "Standard"
        scene.view_settings.exposure = 0.0
        scene.view_settings.gamma = 1.0
        scene.cycles.samples = 32
        bpy.context.view_layer.objects.active = target
        target.select_set(True)
        result = bpy.ops.object.bake(
            "EXEC_DEFAULT",
            type="COMBINED",
            target="IMAGE_TEXTURES",
            use_clear=True,
            margin=0,
        )
        self.assertIn("FINISHED", result)
        image.update()
        return image

    def test_combined_bake_records_transparent_rgba_for_single_tile(self) -> None:
        """투명 Shader 위 반투명 Emission이 단일 타일에 색과 Alpha를 함께 기록합니다."""
        image = self._bake(multi_udim=False)
        red, green, blue, alpha = _pixel(image, 1001)
        self.assertAlmostEqual(alpha, 0.5, delta=0.05)
        self.assertGreater(red, 0.0)
        self.assertGreater(green, 0.0)
        self.assertGreater(blue, 0.0)

    def test_combined_bake_records_transparent_rgba_for_all_udim_tiles(self) -> None:
        """동일 Surface가 두 UDIM 타일 모두에 색과 Alpha를 기록합니다."""
        image = self._bake(multi_udim=True)
        for tile_number in (1001, 1002):
            red, green, blue, alpha = _pixel(image, tile_number)
            self.assertAlmostEqual(alpha, 0.5, delta=0.05)
            self.assertGreater(red, 0.0)
            self.assertGreater(green, 0.0)
            self.assertGreater(blue, 0.0)

    def test_straight_alpha_bake_recomposites_without_double_alpha(self) -> None:
        """Combined Bake Image는 일반 Layer Mix에 다시 넣어도 반투명 Alpha가 이중 적용되지 않습니다."""
        baked = self._bake(multi_udim=False)
        target = bpy.context.view_layer.objects.active
        material = target.active_material
        reference = new_tiled_paint_image(
            "CompositeRgbaReference",
            (32, 32),
            (1001,),
            (0.0, 0.0, 0.0, 1.0),
        )
        _configure_emission_mix(material)
        _set_bake_target(material, reference)
        self.assertIn(
            "FINISHED",
            bpy.ops.object.bake(
                "EXEC_DEFAULT",
                type="EMIT",
                target="IMAGE_TEXTURES",
                use_clear=True,
                margin=0,
            ),
        )
        reference.update()
        recomposited = new_tiled_paint_image(
            "CompositeRgbaRecomposited",
            (32, 32),
            (1001,),
            (0.0, 0.0, 0.0, 1.0),
        )
        _configure_emission_mix(material, foreground_image=baked)
        _set_bake_target(material, recomposited)
        self.assertIn(
            "FINISHED",
            bpy.ops.object.bake(
                "EXEC_DEFAULT",
                type="EMIT",
                target="IMAGE_TEXTURES",
                use_clear=True,
                margin=0,
            ),
        )
        recomposited.update()
        for actual, expected in zip(
            _pixel(recomposited, 1001),
            _pixel(reference, 1001),
        ):
            self.assertAlmostEqual(actual, expected, delta=0.01)


if __name__ == "__main__":
    unittest.main(argv=[__file__], verbosity=2)
