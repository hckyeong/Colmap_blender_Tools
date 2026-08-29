"""Blender Background에서 최종 Texture Export 경로·저장·Atlas 불변성을 검증합니다."""

from __future__ import annotations

from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

import bpy


ROOT = Path(__file__).resolve().parents[3] / "tools" / "projection_paint"
if str(ROOT.parent) not in sys.path:
    sys.path.insert(0, str(ROOT.parent))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import projection_paint as addon  # noqa: E402
from tools.projection_paint.blender import texture_export as export_module  # noqa: E402
from tools.projection_paint.blender import composite_layer as composite_module  # noqa: E402
from tools.projection_paint.blender.texture_export import (  # noqa: E402
    TextureExportCommittedCleanupError,
    atlas_output_attribute,
    atlas_dimensions,
    blender_png_compression_to_oiio,
    build_export_path_plan,
    clear_texture_export_jobs,
    commit_staged_files,
    create_atlas,
    export_texture_sync,
    normalize_export_filepath,
    reserve_texture_export_job,
    save_image_tiles,
    save_single_image,
    texture_export_running,
    udim_to_uvtile,
)
from tools.projection_paint.blender.composite_layer import (  # noqa: E402
    CompositeLayerError,
    create_composite_layer,
    validate_composite_session,
)
from tools.projection_paint.blender.udim_setup import (  # noqa: E402
    new_tiled_paint_image,
)
from tests.blender_background.test_composite_layer import (  # noqa: E402
    _fill_image_tile,
    _prepare_backend,
)


class TextureExportBackgroundTests(unittest.TestCase):
    """작은 UDIM Fixture로 파일 결과와 Blender Image 불변성을 검사합니다."""

    @classmethod
    def setUpClass(cls) -> None:
        """Texture Export Operator를 포함한 Extension 클래스를 등록합니다."""
        addon.register()

    @classmethod
    def tearDownClass(cls) -> None:
        """테스트가 등록한 Extension 클래스와 Runtime을 해제합니다."""
        addon.unregister()

    def setUp(self) -> None:
        """각 테스트를 빈 Factory Scene과 고유 임시 폴더로 시작합니다."""
        bpy.ops.wm.read_factory_settings(use_empty=True)
        self.directory = Path(tempfile.mkdtemp(prefix="pp_texture_export_test_"))

    def tearDown(self) -> None:
        """테스트가 만든 외부 파일 묶음을 모두 제거합니다."""
        shutil.rmtree(self.directory, ignore_errors=True)

    def test_path_plan_normalizes_tokens_and_expands_both_udim_names(self) -> None:
        """파일 탐색기 기준 이름은 토큰을 정리하고 UDIM·UVTILE 파일을 결정적으로 확장합니다."""
        filepath = self.directory / "한글 texture.<UDIM>.jpeg"
        self.assertEqual(
            Path(normalize_export_filepath(str(filepath), "PNG")).name,
            "한글 texture.png",
        )
        udim = build_export_path_plan(
            str(filepath),
            file_format="PNG",
            output_mode="UDIM",
            udim_naming="UDIM",
            tile_numbers=(1011, 1001, 1002),
        )
        self.assertEqual(
            tuple(path.name for path in udim.output_paths),
            (
                "한글 texture.1001.png",
                "한글 texture.1002.png",
                "한글 texture.1011.png",
            ),
        )
        uvtile = build_export_path_plan(
            str(filepath),
            file_format="JPEG",
            output_mode="UDIM",
            udim_naming="UVTILE",
            tile_numbers=(1001, 1002, 1011),
        )
        self.assertEqual(
            tuple(path.name for path in uvtile.output_paths),
            (
                "한글 texture.u1_v1.jpg",
                "한글 texture.u2_v1.jpg",
                "한글 texture.u1_v2.jpg",
            ),
        )
        self.assertEqual(udim_to_uvtile(1012), "u2_v2")

    def test_single_path_plan_uses_exact_name_without_tile_token(self) -> None:
        """SINGLE 경로 계획은 논리 1001을 파일명에 붙이지 않고 한 장만 생성합니다."""
        plan = build_export_path_plan(
            str(self.directory / "single.<UDIM>.png"),
            file_format="PNG",
            output_mode="SINGLE",
            udim_naming="UDIM",
            tile_numbers=(1001,),
        )
        self.assertEqual(tuple(path.name for path in plan.output_paths), ("single.png",))

    def test_atlas_dimensions_preserve_grid_ratio(self) -> None:
        """Atlas Max Size는 4×1과 1×4 배치의 긴 변만 지정 크기에 맞춥니다."""
        self.assertEqual(atlas_dimensions((1001, 1002, 1003, 1004), 8192), (8192, 2048, 2048))
        self.assertEqual(atlas_dimensions((1001, 1011, 1021, 1031), 8192), (2048, 8192, 2048))

    def test_atlas_png_compression_maps_blender_percent_to_oiio_level(self) -> None:
        """Atlas PNG는 Blender 0~100 압축 의미를 OIIO png Level 0~9에 같은 방향으로 전달합니다."""
        self.assertEqual(blender_png_compression_to_oiio(0), 0)
        self.assertEqual(blender_png_compression_to_oiio(50), 5)
        self.assertEqual(blender_png_compression_to_oiio(100), 9)
        self.assertEqual(
            atlas_output_attribute(
                Path("atlas.png"),
                png_compression=67,
                jpeg_quality=21,
            ),
            ("png:compressionLevel", 6),
        )
        self.assertEqual(
            atlas_output_attribute(
                Path("atlas.jpg"),
                png_compression=67,
                jpeg_quality=82,
            ),
            ("CompressionQuality", 82),
        )

    def test_lifecycle_clear_releases_reserved_export_job(self) -> None:
        """Load·Undo·Redo Barrier 정리는 시작 전 예약된 Export Job과 Overlay 상태를 해제합니다."""
        reserve_texture_export_job()
        self.assertTrue(texture_export_running())
        clear_texture_export_jobs("test", 1)
        self.assertFalse(texture_export_running())

    def test_blender_udim_save_restores_path_format_pack_and_dirty(self) -> None:
        """Blender Save a Copy 뒤 Source의 경로·포맷·Pack·Dirty 계약이 그대로 유지됩니다."""
        image = new_tiled_paint_image(
            "TextureExportSaveSource",
            (8, 8),
            (1001, 1002),
            (0.2, 0.4, 0.6, 0.8),
        )
        image.filepath = "//원본 경로/source.<UDIM>.png"
        image.filepath_raw = "//원본 경로/source.<UDIM>.png"
        image.pack()
        value = float(image.pixels[0])
        image.pixels[0] = value
        image.update()
        before = (
            image.filepath,
            image.filepath_raw,
            image.file_format,
            image.is_dirty,
            tuple(item.as_pointer() for item in image.packed_files),
        )
        paths = save_image_tiles(
            bpy.context,
            image,
            self.directory / "tiles" / "paint.<UDIM>.png",
            file_format="PNG",
            color_mode="RGBA",
            png_compression=15,
            jpeg_quality=90,
        )
        self.assertEqual(tuple(path.name for path in paths), ("paint.1001.png", "paint.1002.png"))
        self.assertTrue(all(path.stat().st_size > 0 for path in paths))
        self.assertEqual(
            (
                image.filepath,
                image.filepath_raw,
                image.file_format,
                image.is_dirty,
                tuple(item.as_pointer() for item in image.packed_files),
            ),
            before,
        )

    def test_openimageio_atlas_places_high_v_row_at_top(self) -> None:
        """Atlas는 높은 V 타일을 위 행에 두고 빈 Cell을 투명하게 유지합니다."""
        image = new_tiled_paint_image(
            "TextureExportAtlasSource",
            (8, 8),
            (1001, 1002, 1011),
            (0.25, 0.5, 0.75, 0.5),
        )
        tile_paths = save_image_tiles(
            bpy.context,
            image,
            self.directory / "atlas_tiles" / "paint.<UDIM>.png",
            file_format="PNG",
            color_mode="RGBA",
            png_compression=15,
            jpeg_quality=90,
        )
        output = self.directory / "atlas.png"
        with patch.object(
            export_module,
            "atlas_output_attribute",
            wraps=atlas_output_attribute,
        ) as output_attribute:
            self.assertEqual(
                create_atlas(
                    tile_paths,
                    (1001, 1002, 1011),
                    output,
                    max_size=64,
                    color_mode="RGBA",
                    png_compression=63,
                    jpeg_quality=90,
                ),
                (64, 64),
            )
        output_attribute.assert_called_once_with(
            output,
            png_compression=63,
            jpeg_quality=90,
        )
        import OpenImageIO as oiio

        atlas = oiio.ImageBuf(str(output))
        self.assertEqual((atlas.spec().width, atlas.spec().height, atlas.spec().nchannels), (64, 64, 4))
        source_pixel = oiio.ImageBuf(str(tile_paths[2])).getpixel(4, 4)
        filled = atlas.getpixel(16, 16)
        self.assertLessEqual(
            max(abs(float(left) - float(right)) for left, right in zip(filled, source_pixel)),
            2.0 / 255.0,
        )
        self.assertLessEqual(abs(float(filled[3]) - 0.5), 2.0 / 255.0)
        empty = atlas.getpixel(48, 16)
        self.assertLessEqual(max(abs(float(value)) for value in empty), 1.0 / 255.0)

    def test_atomic_commit_replaces_whole_existing_set(self) -> None:
        """덮어쓰기 허용 시 기존 UDIM 묶음을 모두 새 Staging 파일로 교체합니다."""
        staging = self.directory / "staging"
        staging.mkdir()
        staged = (staging / "a.png", staging / "b.png")
        outputs = (self.directory / "paint.1001.png", self.directory / "paint.1002.png")
        for path, content in zip(staged, (b"new-a", b"new-b")):
            path.write_bytes(content)
        for path, content in zip(outputs, (b"old-a", b"old-b")):
            path.write_bytes(content)
        commit_staged_files(staged, outputs, overwrite=True)
        self.assertEqual(tuple(path.read_bytes() for path in outputs), (b"new-a", b"new-b"))

    def test_atomic_commit_failure_restores_whole_existing_set(self) -> None:
        """UDIM 중간 확정 실패는 이미 옮긴 새 파일을 제거하고 기존 묶음을 모두 복원합니다."""
        staging = self.directory / "rollback_staging"
        staging.mkdir()
        staged = (staging / "a.png", staging / "b.png")
        outputs = (self.directory / "rollback.1001.png", self.directory / "rollback.1002.png")
        for path, content in zip(staged, (b"new-a", b"new-b")):
            path.write_bytes(content)
        for path, content in zip(outputs, (b"old-a", b"old-b")):
            path.write_bytes(content)
        original_replace = export_module.os.replace

        def fail_second_staged(source, destination):
            """두 번째 새 타일의 목적 경로 확정만 강제로 실패시킵니다."""
            if Path(source) == staged[1] and Path(destination) == outputs[1]:
                raise OSError("강제 중간 확정 실패")
            return original_replace(source, destination)

        with patch.object(export_module.os, "replace", side_effect=fail_second_staged):
            with self.assertRaisesRegex(Exception, "확정에 실패"):
                commit_staged_files(staged, outputs, overwrite=True)
        self.assertEqual(tuple(path.read_bytes() for path in outputs), (b"old-a", b"old-b"))

    def test_backup_cleanup_failure_keeps_committed_outputs_and_old_backups(self) -> None:
        """확정 후 Backup 삭제 실패는 새 출력 성공과 보존된 이전 Backup을 명시적으로 반환합니다."""
        staging = self.directory / "cleanup_failure_staging"
        staging.mkdir()
        staged = (staging / "a.png", staging / "b.png")
        outputs = (self.directory / "cleanup.1001.png", self.directory / "cleanup.1002.png")
        for path, content in zip(staged, (b"new-a", b"new-b")):
            path.write_bytes(content)
        for path, content in zip(outputs, (b"old-a", b"old-b")):
            path.write_bytes(content)
        original_unlink = Path.unlink

        def fail_backup_unlink(path, *args, **kwargs):
            """확정된 이전 Backup 파일의 개별 정리만 강제로 실패시킵니다."""
            if path.parent.name == "backups":
                raise PermissionError("강제 Backup 정리 실패")
            return original_unlink(path, *args, **kwargs)

        with patch.object(Path, "unlink", new=fail_backup_unlink):
            result = commit_staged_files(staged, outputs, overwrite=True)
        self.assertEqual(result.output_paths, outputs)
        self.assertEqual(tuple(path.read_bytes() for path in outputs), (b"new-a", b"new-b"))
        self.assertEqual(
            tuple(path.read_bytes() for path in result.retained_backup_paths),
            (b"old-a", b"old-b"),
        )
        self.assertEqual(len(result.cleanup_errors), 2)

    def test_blender_udim_jpeg_is_rgb_and_uses_quality_setting(self) -> None:
        """JPEG UDIM은 Alpha 없는 3채널 파일로 기록하고 Source Image를 변경하지 않습니다."""
        image = new_tiled_paint_image(
            "TextureExportJpegSource",
            (8, 8),
            (1001,),
            (0.3, 0.6, 0.9, 0.25),
        )
        before = (image.filepath, image.filepath_raw, image.file_format, image.is_dirty)
        paths = save_image_tiles(
            bpy.context,
            image,
            self.directory / "jpeg_tiles" / "paint.<UDIM>.jpg",
            file_format="JPEG",
            color_mode="RGB",
            png_compression=15,
            jpeg_quality=72,
        )
        import OpenImageIO as oiio

        loaded = oiio.ImageBuf(str(paths[0]))
        self.assertEqual(loaded.spec().nchannels, 3)
        self.assertEqual((image.filepath, image.filepath_raw, image.file_format, image.is_dirty), before)

    def test_active_composite_exports_udim_without_changing_layer_graph(self) -> None:
        """활성 Composite 직접 경로는 실제 PNG를 만들고 Layer·Image·Shader Pointer를 보존합니다."""
        _target, setup = _prepare_backend(
            "TextureExportCompositeTarget",
            multi_udim=True,
            full_uv_coverage=True,
        )
        settings = bpy.context.scene.projection_paint
        for tile_number in (1001, 1002):
            _fill_image_tile(settings.layers[0].image, tile_number, (0.2, 0.4, 0.7, 1.0))
        create_composite_layer(bpy.context)
        session = validate_composite_session(bpy.context.scene)
        source = session.composite.image
        before = (
            tuple(layer.as_pointer() for layer in settings.layers),
            tuple(node.as_pointer() for node in setup.material.node_tree.nodes),
            source.as_pointer(),
            source.filepath,
            source.filepath_raw,
            source.file_format,
            source.is_dirty,
            tuple(item.as_pointer() for item in source.packed_files),
        )
        paths = export_texture_sync(
            bpy.context,
            str(self.directory / "final.png"),
            output_mode="UDIM",
            file_format="PNG",
            color_mode="RGBA",
            udim_naming="UDIM",
            atlas_max_size=2048,
            png_compression=15,
            jpeg_quality=90,
            overwrite=False,
        )
        self.assertEqual(tuple(path.name for path in paths), ("final.1001.png", "final.1002.png"))
        self.assertTrue(all(path.stat().st_size > 0 for path in paths))
        self.assertEqual(
            (
                tuple(layer.as_pointer() for layer in settings.layers),
                tuple(node.as_pointer() for node in setup.material.node_tree.nodes),
                source.as_pointer(),
                source.filepath,
                source.filepath_raw,
                source.file_format,
                source.is_dirty,
                tuple(item.as_pointer() for item in source.packed_files),
            ),
            before,
        )
        validate_composite_session(bpy.context.scene)

    def test_post_commit_temp_cleanup_failure_reports_committed_output_and_retained_backup(self) -> None:
        """임시 폴더 정리까지 실패하면 새 출력 확정과 이전 Backup 보존 상태를 구분해 보고합니다."""
        _target, _setup = _prepare_backend("TextureExportCommittedCleanupTarget")
        settings = bpy.context.scene.projection_paint
        create_composite_layer(bpy.context)
        output = self.directory / "committed_cleanup.png"
        output.write_bytes(b"old-texture")
        original_unlink = Path.unlink

        def fail_backup_unlink(path, *args, **kwargs):
            """이전 Texture Backup의 개별 삭제만 실패시켜 복구본을 유지합니다."""
            if path.parent.name == "backups":
                raise PermissionError("강제 Backup 보존")
            return original_unlink(path, *args, **kwargs)

        with patch.object(Path, "unlink", new=fail_backup_unlink), patch.object(
            export_module.shutil,
            "rmtree",
            side_effect=PermissionError("강제 임시 폴더 정리 실패"),
        ):
            with self.assertRaises(TextureExportCommittedCleanupError) as raised:
                export_texture_sync(
                    bpy.context,
                    str(self.directory / "committed_cleanup.png"),
                    output_mode="SINGLE",
                    file_format="PNG",
                    color_mode="RGBA",
                    udim_naming="UDIM",
                    atlas_max_size=2048,
                    png_compression=15,
                    jpeg_quality=90,
                    overwrite=True,
                )
        self.assertEqual(raised.exception.output_paths, (output,))
        self.assertNotEqual(output.read_bytes(), b"old-texture")
        backups = tuple(self.directory.glob(".pp_texture_export_*/staged/backups/*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), b"old-texture")
        validate_composite_session(bpy.context.scene)

    def test_visible_layer_above_composite_uses_temporary_bake_and_cleans_it(self) -> None:
        """Composite 위 가시 Layer는 임시 최종 RGBA Bake를 쓰고 Image·Node를 남기지 않습니다."""
        _target, setup = _prepare_backend(
            "TextureExportFinalBakeTarget",
            multi_udim=True,
            full_uv_coverage=True,
        )
        settings = bpy.context.scene.projection_paint
        _fill_image_tile(settings.layers[0].image, 1001, (0.1, 0.2, 0.4, 1.0))
        create_composite_layer(bpy.context)
        self.assertEqual(bpy.ops.projection_paint.layer_add(), {"FINISHED"})
        _fill_image_tile(settings.layers[0].image, 1001, (0.8, 0.2, 0.1, 0.5))
        before = (
            tuple(layer.as_pointer() for layer in settings.layers),
            tuple(node.as_pointer() for node in setup.material.node_tree.nodes),
            tuple(image.as_pointer() for image in bpy.data.images),
        )
        paths = export_texture_sync(
            bpy.context,
            str(self.directory / "final_atlas.png"),
            output_mode="ATLAS",
            file_format="PNG",
            color_mode="RGBA",
            udim_naming="UDIM",
            atlas_max_size=64,
            png_compression=15,
            jpeg_quality=90,
            overwrite=False,
        )
        self.assertEqual(tuple(path.name for path in paths), ("final_atlas.png",))
        self.assertTrue(paths[0].stat().st_size > 0)
        self.assertEqual(
            (
                tuple(layer.as_pointer() for layer in settings.layers),
                tuple(node.as_pointer() for node in setup.material.node_tree.nodes),
                tuple(image.as_pointer() for image in bpy.data.images),
            ),
            before,
        )
        validate_composite_session(bpy.context.scene)

    def test_temporary_final_bake_failure_restores_nodes_render_and_image_set(self) -> None:
        """최종 Bake 준비 뒤 오류가 나도 임시 Node·Image와 렌더 변경을 모두 정리합니다."""
        _target, setup = _prepare_backend("TextureExportBakeFailureTarget")
        settings = bpy.context.scene.projection_paint
        create_composite_layer(bpy.context)
        self.assertEqual(bpy.ops.projection_paint.layer_add(), {"FINISHED"})
        before = (
            tuple(node.as_pointer() for node in setup.material.node_tree.nodes),
            tuple(image.as_pointer() for image in bpy.data.images),
            bpy.context.scene.render.engine,
            bpy.context.scene.render.film_transparent,
            bpy.context.scene.render.bake.margin_type,
        )
        with patch.object(
            composite_module,
            "_set_composite_bake_samples",
            side_effect=CompositeLayerError("강제 Export Bake 준비 실패"),
        ):
            with self.assertRaisesRegex(CompositeLayerError, "강제 Export Bake"):
                composite_module.create_final_texture_export_image(bpy.context)
        self.assertEqual(
            (
                tuple(node.as_pointer() for node in setup.material.node_tree.nodes),
                tuple(image.as_pointer() for image in bpy.data.images),
                bpy.context.scene.render.engine,
                bpy.context.scene.render.film_transparent,
                bpy.context.scene.render.bake.margin_type,
            ),
            before,
        )
        validate_composite_session(bpy.context.scene)


if __name__ == "__main__":
    unittest.main(argv=[__file__], verbosity=2)
