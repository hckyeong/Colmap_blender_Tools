"""UDIM Paint 이미지의 실제 타일 버퍼와 Pack을 검증합니다."""

from __future__ import annotations

import sys
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[3] / "tools" / "projection_paint"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.projection_paint.blender.udim_setup import new_tiled_paint_image


class UDIMImageTests(unittest.TestCase):
    """생성 UDIM 이미지가 페인트 가능한 픽셀 타일을 보유하는지 검증합니다."""

    def test_tiled_image_has_pixels_and_packed_tiles(self):
        """모든 타일이 픽셀 버퍼와 Packed 데이터를 가집니다."""
        image = new_tiled_paint_image(
            "test_udim_paint",
            (8, 8),
            (1001, 1002, 1011),
            (0.0, 0.0, 0.0, 0.0),
        )
        self.assertEqual([tile.number for tile in image.tiles], [1001, 1002, 1011])
        self.assertTrue(all(tuple(tile.size) == (8, 8) for tile in image.tiles))
        self.assertEqual(len(image.packed_files), 3)


if __name__ == "__main__":
    unittest.main(argv=[__file__], verbosity=2)
