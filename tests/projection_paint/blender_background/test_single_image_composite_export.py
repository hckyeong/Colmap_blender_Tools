"""SINGLE Paint Image의 Composite·Alpha·Export 계약을 검증합니다."""

from __future__ import annotations

from pathlib import Path
import shutil
import sys
import tempfile
import unittest

import bpy


ROOT = Path(__file__).resolve().parents[3] / "tools" / "projection_paint"
if str(ROOT.parent) not in sys.path:
    sys.path.insert(0, str(ROOT.parent))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import projection_paint as addon  # noqa: E402
from tools.projection_paint.blender.composite_layer import (  # noqa: E402
    combine_color_alpha_numpy,
    composite_memory_estimate,
    create_composite_layer,
    validate_composite_session,
)
from tools.projection_paint.blender.image_backend import (  # noqa: E402
    SINGLE_BACKEND,
    image_contract,
    new_managed_paint_image,
)
from tools.projection_paint.blender.texture_export import (  # noqa: E402
    TextureExportError,
    export_texture_sync,
    save_single_image,
)
from tests.blender_background.test_composite_layer import (  # noqa: E402
    _fill_image,
    _prepare_backend,
)


class SingleImageCompositeExportTests(unittest.TestCase):
    """FILE source 한 장이 Composite와 Export에서 유지되는지 검사합니다."""

    @classmethod
    def setUpClass(cls) -> None:
        """테스트에 필요한 Extension 클래스를 등록합니다."""
        addon.register()

    @classmethod
    def tearDownClass(cls) -> None:
        """테스트가 등록한 Extension 클래스를 해제합니다."""
        addon.unregister()

    def setUp(self) -> None:
        """빈 Scene과 고유 임시 출력 폴더를 준비합니다."""
        bpy.ops.wm.read_factory_settings(use_empty=True)
        self.directory = Path(tempfile.mkdtemp(prefix="pp_single_backend_test_"))

    def tearDown(self) -> None:
        """테스트가 생성한 외부 출력 파일을 제거합니다."""
        shutil.rmtree(self.directory, ignore_errors=True)

    def test_single_alpha_combines_one_pixel_buffer(self) -> None:
        """SINGLE Color·Alpha는 타일 전환 없이 한 Pixel Buffer에서 결합됩니다."""
        color = new_managed_paint_image(
            SINGLE_BACKEND,
            "SingleColor",
            (4, 4),
            (1001,),
            (0.2, 0.4, 0.6, 1.0),
        )
        alpha = new_managed_paint_image(
            SINGLE_BACKEND,
            "SingleAlpha",
            (4, 4),
            (1001,),
            (0.25, 0.25, 0.25, 1.0),
        )
        combine_color_alpha_numpy(color, alpha)
        pixels = tuple(float(value) for value in color.pixels[:8])
        self.assertAlmostEqual(pixels[3], 0.25, delta=1.0 / 255.0)
        self.assertAlmostEqual(pixels[7], 0.25, delta=1.0 / 255.0)

    def test_single_png_and_jpeg_save_exactly_one_file(self) -> None:
        """SINGLE Save a Copy는 PNG와 JPEG 모두 토큰 없는 한 파일만 기록합니다."""
        image = new_managed_paint_image(
            SINGLE_BACKEND,
            "SingleSave",
            (8, 8),
            (1001,),
            (0.3, 0.6, 0.9, 0.5),
        )
        for file_format, color_mode, name in (
            ("PNG", "RGBA", "single.png"),
            ("JPEG", "RGB", "single.jpg"),
        ):
            paths = save_single_image(
                bpy.context,
                image,
                self.directory / file_format.lower() / name,
                file_format=file_format,
                color_mode=color_mode,
                png_compression=15,
                jpeg_quality=80,
            )
            self.assertEqual(tuple(path.name for path in paths), (name,))
            self.assertGreater(paths[0].stat().st_size, 0)

    def test_single_composite_recompose_and_final_export_keep_backend(self) -> None:
        """SINGLE Composite·재합성·최종 Bake Export가 FILE 한 장 계약을 유지합니다."""
        _target, _setup = _prepare_backend("SingleCompositeExportTarget")
        settings = bpy.context.scene.projection_paint
        settings.composite_include_alpha = True
        _fill_image(settings.layers[0].image, (0.2, 0.4, 0.7, 0.6))
        first = create_composite_layer(bpy.context)
        self.assertEqual(image_contract(first).backend, SINGLE_BACKEND)
        self.assertEqual(bpy.ops.projection_paint.layer_add(), {"FINISHED"})
        _fill_image(settings.layers[0].image, (0.8, 0.1, 0.2, 0.5))
        dependent_pointer = settings.layers[0].as_pointer()
        for layer in settings.layers:
            layer.ui_selected = (
                str(layer.layer_role) == "COMPOSITE"
                or layer.as_pointer() == dependent_pointer
            )
        second = create_composite_layer(bpy.context)
        self.assertEqual(image_contract(second).backend, SINGLE_BACKEND)
        self.assertEqual(
            image_contract(validate_composite_session(bpy.context.scene).composite.image).backend,
            SINGLE_BACKEND,
        )
        paths = export_texture_sync(
            bpy.context,
            str(self.directory / "final.png"),
            output_mode="SINGLE",
            file_format="PNG",
            color_mode="RGBA",
            udim_naming="UDIM",
            atlas_max_size=64,
            png_compression=15,
            jpeg_quality=90,
            overwrite=False,
        )
        self.assertEqual(tuple(path.name for path in paths), ("final.png",))
        self.assertGreater(paths[0].stat().st_size, 0)

    def test_single_export_rejects_backend_change_without_output(self) -> None:
        """SINGLE을 UDIM으로 요청하면 출력과 임시 폴더 없이 실패 폐쇄합니다."""
        _target, _setup = _prepare_backend("SingleExportMismatchTarget")
        settings = bpy.context.scene.projection_paint
        create_composite_layer(bpy.context)
        expected_memory = sum(
            width * height * 4
            for width, height in image_contract(settings.overlay_image).tile_sizes
        )
        self.assertEqual(composite_memory_estimate(settings), expected_memory)
        with self.assertRaisesRegex(TextureExportError, "SINGLE 출력"):
            export_texture_sync(
                bpy.context,
                str(self.directory / "mismatch.png"),
                output_mode="UDIM",
                file_format="PNG",
                color_mode="RGBA",
                udim_naming="UDIM",
                atlas_max_size=64,
                png_compression=15,
                jpeg_quality=90,
                overwrite=False,
            )
        self.assertFalse((self.directory / "mismatch.png").exists())
        self.assertEqual(tuple(self.directory.glob(".pp_texture_export_*")), ())


if __name__ == "__main__":
    unittest.main(argv=[__file__], verbosity=2)
