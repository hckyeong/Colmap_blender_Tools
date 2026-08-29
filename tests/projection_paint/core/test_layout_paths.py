"""Layout 탐색과 이미지 경로 신뢰 경계를 검증합니다."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.projection_paint.core.colmap.errors import ColmapError
from tools.projection_paint.core.colmap.image_paths import (
    AMBIGUOUS,
    EXACT,
    FALLBACK,
    ImagePathResolver,
    build_image_path_manifest,
    normalize_relative_image_path,
    resolve_image_path,
)
from tools.projection_paint.core.colmap.layout import resolve_layout
from tools.projection_paint.core.colmap.model import ImagePoseRecord

from support import sample_images, write_layout


class LayoutPathTests(unittest.TestCase):
    """입력 Pair 선택과 Canonical 이미지 경로 검증을 확인합니다."""

    def test_auto_prefers_complete_binary_pair(self) -> None:
        """BIN과 TXT Pair가 모두 있으면 Binary를 선택합니다."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_layout(root, "TEXT")
            write_layout(root, "BINARY")
            layout = resolve_layout(root, "AUTO")
            self.assertEqual(layout.input_format, "binary")
            self.assertEqual(
                {path.name for path in layout.selected_files},
                {"cameras.bin", "images.bin", "points3D.bin"},
            )

    def test_mixed_pair_is_rejected(self) -> None:
        """Camera와 Image 파일 형식이 섞인 Pair를 거부합니다."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "cameras.bin").write_bytes(b"")
            (root / "images.txt").write_text("", encoding="utf-8")
            with self.assertRaisesRegex(ColmapError, "완전한"):
                resolve_layout(root)

    def test_points3d_file_is_required(self) -> None:
        """points3D 파일이 없으면 불완전한 Reconstruction으로 거부합니다."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_layout(root, "BINARY")
            (root / "points3D.bin").unlink()
            with self.assertRaisesRegex(ColmapError, "완전한"):
                resolve_layout(root)

    def test_safe_nested_unicode_and_space_path(self) -> None:
        """한글·공백·중첩 상대경로를 정확한 파일로 해석합니다."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "중첩 폴더" / "가 사진.jpg"
            target.parent.mkdir()
            target.write_bytes(b"image")
            resolved, status = resolve_image_path(root, "중첩 폴더/가 사진.jpg")
            self.assertEqual(resolved, target.resolve())
            self.assertEqual(status, EXACT)

    def test_unsafe_windows_and_traversal_paths_are_rejected(self) -> None:
        """절대·순회·UNC·Device·ADS·예약 이름을 모두 거부합니다."""
        unsafe = (
            "../escape.jpg",
            "/absolute.jpg",
            "C:\\absolute.jpg",
            "\\\\server\\share\\a.jpg",
            "\\\\?\\C:\\a.jpg",
            "file.jpg:stream",
            "NUL.jpg",
            "folder/COM1.png",
        )
        for image_name in unsafe:
            with self.subTest(image_name=image_name):
                with self.assertRaises(ColmapError):
                    normalize_relative_image_path(image_name)

    def test_extension_fallback_requires_unique_candidate(self) -> None:
        """확장자 Fallback은 동일 Stem 후보가 하나일 때만 확정됩니다."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "image.jpg").write_bytes(b"jpg")
            resolved, status = resolve_image_path(
                root,
                "image.tif",
                allow_extension_fallback=True,
            )
            self.assertEqual(resolved, (root / "image.jpg").resolve())
            self.assertEqual(status, FALLBACK)
            (root / "image.png").write_bytes(b"png")
            path, status = resolve_image_path(
                root,
                "image.tif",
                allow_extension_fallback=True,
            )
            self.assertIsNone(path)
            self.assertEqual(status, AMBIGUOUS)

    def test_manifest_rejects_duplicate_canonical_file(self) -> None:
        """서로 다른 IMAGE_ID가 같은 Canonical 파일을 가리키면 거부합니다."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "same.jpg").write_bytes(b"image")
            images = (
                ImagePoseRecord(1, 3, (1.0, 0.0, 0.0, 0.0), (0.0, 0.0, 0.0), "same.jpg"),
                ImagePoseRecord(2, 3, (1.0, 0.0, 0.0, 0.0), (0.0, 0.0, 0.0), "SAME.jpg"),
            )
            with patch.object(
                ImagePathResolver,
                "resolve",
                side_effect=(
                    ((root / "same.jpg").resolve(), EXACT, "same.jpg"),
                    ((root / "same.jpg").resolve(), EXACT, "SAME.jpg"),
                ),
            ):
                with self.assertRaisesRegex(ColmapError, "같은 Canonical"):
                    build_image_path_manifest(root, images)

    def test_reparse_root_is_rejected(self) -> None:
        """Images Root가 Reparse Point로 판정되면 열기 전에 거부합니다."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch(
                "tools.projection_paint.core.colmap.image_paths._is_reparse",
                return_value=True,
            ):
                with self.assertRaisesRegex(ColmapError, "Reparse"):
                    ImagePathResolver(root)


if __name__ == "__main__":
    unittest.main()
