"""UDIM 타일 번호와 파일명 분석을 검증합니다."""

import unittest

from tools.projection_paint.blender.udim_setup import tile_from_uvtile_name, udim_number


class UDIMSetupTests(unittest.TestCase):
    """UDIM 레이아웃 계산 규칙을 검증합니다."""

    def test_udim_number_uses_blender_tile_order(self):
        """UV 타일 좌표가 Blender UDIM 번호로 변환됩니다."""
        self.assertEqual(udim_number(1, 1), 1001)
        self.assertEqual(udim_number(2, 1), 1002)
        self.assertEqual(udim_number(1, 2), 1011)

    def test_uvtile_filename_maps_to_offsets(self):
        """u2_v3 파일명이 UDIM 번호와 오프셋으로 변환됩니다."""
        tile = tile_from_uvtile_name("E:/textures/fish_u2_v3_diffuse.jpg")
        self.assertIsNotNone(tile)
        self.assertEqual((tile.number, tile.offset_u, tile.offset_v), (1022, 1, 2))

    def test_non_uvtile_filename_is_not_converted(self):
        """일반 파일명은 UDIM 타일로 오인하지 않습니다."""
        self.assertIsNone(tile_from_uvtile_name("E:/textures/fish_diffuse.jpg"))
