"""SINGLE·UDIM 공통 Paint Image 백엔드 계약을 검증합니다."""

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

from tools.projection_paint.blender.image_backend import (  # noqa: E402
    PaintImageContract,
    PaintImageContractError,
    SINGLE_BACKEND,
    UDIM_BACKEND,
    backend_from_layout,
    image_contract,
    managed_staging_paths,
    new_managed_paint_image,
    remove_managed_staging,
    validate_image_contract,
)


class ImageBackendTests(unittest.TestCase):
    """관리 이미지 Factory와 읽기 전용 계약 비교를 검증합니다."""

    def setUp(self):
        """각 테스트를 빈 Blender Factory 상태에서 시작합니다."""

        bpy.ops.wm.read_factory_settings(use_empty=True)

    def test_layout_kind_maps_without_counting_tiles(self):
        """입력 Layout 종류가 명시된 SINGLE·UDIM 백엔드로 매핑됩니다."""

        self.assertEqual(backend_from_layout("SINGLE_1001"), SINGLE_BACKEND)
        self.assertEqual(backend_from_layout("UV_ONLY"), SINGLE_BACKEND)
        self.assertEqual(backend_from_layout("EXISTING_UDIM"), UDIM_BACKEND)
        self.assertEqual(backend_from_layout("MULTI_MATERIAL_TILES"), UDIM_BACKEND)

    def test_single_factory_creates_file_packed_1001_contract(self):
        """SINGLE Factory가 FILE·Packed·1001 논리 타일과 요청 크기를 만듭니다."""

        image = new_managed_paint_image(
            SINGLE_BACKEND,
            "SinglePaint",
            (13, 7),
            (1001,),
            (0.1, 0.2, 0.3, 1.0),
        )
        expected = PaintImageContract(SINGLE_BACKEND, (1001,), ((13, 7),))

        self.assertEqual(str(image.source), "FILE")
        self.assertIsNotNone(image.packed_file)
        self.assertEqual(tuple(int(tile.number) for tile in image.tiles), (1001,))
        self.assertEqual(validate_image_contract(image, expected), expected)
        root, paths = managed_staging_paths(image)
        self.assertTrue(root.is_dir())
        self.assertEqual(tuple(path.name for path in paths), ("paint.png",))
        remove_managed_staging(image)
        self.assertFalse(root.exists())

    def test_udim_factory_keeps_tiled_1001_backend(self):
        """타일이 하나인 UDIM도 TILED·UDIM 백엔드로 유지됩니다."""

        image = new_managed_paint_image(
            UDIM_BACKEND,
            "OneTileUDIM",
            (8, 4),
            (1001,),
            (0.0, 0.0, 0.0, 0.0),
        )

        self.assertEqual(str(image.source), "TILED")
        self.assertEqual(len(tuple(image.packed_files)), 1)
        self.assertEqual(image_contract(image).backend, UDIM_BACKEND)

    def test_udim_factory_packs_every_tile(self):
        """다중 UDIM Factory가 모든 타일 픽셀과 Packed 파일을 생성합니다."""

        image = new_managed_paint_image(
            UDIM_BACKEND,
            "MultiTileUDIM",
            (8, 4),
            (1001, 1002),
            (0.0, 0.0, 0.0, 0.0),
        )
        expected = PaintImageContract(
            UDIM_BACKEND,
            (1001, 1002),
            ((8, 4), (8, 4)),
        )

        self.assertEqual(len(tuple(image.packed_files)), 2)
        self.assertEqual(validate_image_contract(image, expected), expected)

    def test_backend_mismatch_is_rejected_even_for_1001(self):
        """1001 한 장이어도 FILE과 TILED 백엔드 혼합을 거부합니다."""

        image = new_managed_paint_image(
            UDIM_BACKEND,
            "MismatchUDIM",
            (8, 8),
            (1001,),
            (0.0, 0.0, 0.0, 0.0),
        )
        with self.assertRaises(PaintImageContractError):
            validate_image_contract(
                image,
                PaintImageContract(SINGLE_BACKEND, (1001,), ((8, 8),)),
            )

    def test_invalid_single_request_does_not_create_image(self):
        """유효하지 않은 SINGLE 타일 요청은 Blender Image를 만들기 전에 실패합니다."""

        before = tuple(image.as_pointer() for image in bpy.data.images)
        with self.assertRaises(PaintImageContractError):
            new_managed_paint_image(
                SINGLE_BACKEND,
                "InvalidSingle",
                (8, 8),
                (1001, 1002),
                (0.0, 0.0, 0.0, 0.0),
            )
        self.assertEqual(before, tuple(image.as_pointer() for image in bpy.data.images))

    def test_single_factory_failure_removes_partial_staging(self):
        """SINGLE 생성 중 실패하면 부분 Image와 애드온 소유 Staging을 정리합니다."""

        staging_root = Path(tempfile.gettempdir()) / "blender_projection_paint_staging"
        before_roots = set(staging_root.iterdir()) if staging_root.is_dir() else set()
        before_images = tuple(image.as_pointer() for image in bpy.data.images)

        with self.assertRaises((TypeError, ValueError)):
            new_managed_paint_image(
                SINGLE_BACKEND,
                "BrokenSingle",
                (8, 8),
                (1001,),
                (0.0, 0.0, 0.0),
            )

        after_roots = set(staging_root.iterdir()) if staging_root.is_dir() else set()
        self.assertEqual(before_roots, after_roots)
        self.assertEqual(before_images, tuple(image.as_pointer() for image in bpy.data.images))


if __name__ == "__main__":
    unittest.main(argv=[__file__], verbosity=2)
