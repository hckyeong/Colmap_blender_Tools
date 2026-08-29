"""Camera Review 순수 Python Core의 동작과 경로 보안을 검증합니다."""

from __future__ import annotations

import os
import tempfile
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path

from tools.modifier.core.camera_review import (
    build_review_index,
    navigate_next,
    navigate_previous,
    next_index,
    preferred_index_after_deletion,
    previous_index,
)
from tools.modifier.core.image_path_resolution import (
    AMBIGUOUS,
    EXACT,
    FALLBACK,
    MISSING,
    resolve_image_path,
)
from tools.modifier.core.model import CameraRecord, ImageRecord, Reconstruction


def _camera(
    camera_id: int,
    model_name: str = "PINHOLE",
    *,
    width: int = 1920,
    height: int = 1080,
) -> CameraRecord:
    model_ids = {
        "SIMPLE_PINHOLE": 0,
        "PINHOLE": 1,
        "OPENCV": 4,
    }
    params = (
        (1000.0, width / 2.0, height / 2.0)
        if model_name == "SIMPLE_PINHOLE"
        else (1000.0, 1000.0, width / 2.0, height / 2.0)
    )
    if model_name == "OPENCV":
        params += (0.0, 0.0, 0.0, 0.0)
    return CameraRecord(
        camera_id=camera_id,
        model_id=model_ids[model_name],
        model_name=model_name,
        width=width,
        height=height,
        params=params,
    )


def _image(image_id: int, camera_id: int, name: str) -> ImageRecord:
    return ImageRecord(
        image_id=image_id,
        qvec=(1.0, 0.0, 0.0, 0.0),
        tvec=(0.0, 0.0, 0.0),
        camera_id=camera_id,
        name=name,
        points2d=(),
    )


class ImagePathResolutionTests(unittest.TestCase):
    def test_exact_nested_and_windows_separator_paths_preserve_actual_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            nested = root / "set_a"
            nested.mkdir()
            image_path = nested / "frame 01.JPG"
            image_path.write_bytes(b"image")

            nested_result = resolve_image_path(root, "set_a/frame 01.JPG")
            windows_result = resolve_image_path(root, r"set_a\frame 01.JPG")

            self.assertEqual(nested_result.path_status, EXACT)
            self.assertEqual(nested_result.resolved_image_path, image_path.resolve())
            self.assertEqual(windows_result.path_status, EXACT)
            self.assertEqual(windows_result.resolved_image_path, image_path.resolve())

    def test_same_stem_fallback_is_limited_to_the_same_relative_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            nested = root / "nested"
            nested.mkdir()
            fallback = nested / "frame.png"
            fallback.write_bytes(b"image")
            elsewhere = root / "elsewhere"
            elsewhere.mkdir()
            (elsewhere / "frame.tif").write_bytes(b"other")

            result = resolve_image_path(root, "nested/frame.jpg")

            self.assertEqual(result.path_status, FALLBACK)
            self.assertEqual(result.resolved_image_path, fallback.resolve())

    def test_same_stem_zero_and_multiple_candidates_are_explicit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertEqual(
                resolve_image_path(root, "missing.jpg").path_status,
                MISSING,
            )

            (root / "frame.png").write_bytes(b"png")
            (root / "frame.tif").write_bytes(b"tif")
            ambiguous = resolve_image_path(root, "frame.jpg")

            self.assertEqual(ambiguous.path_status, AMBIGUOUS)
            self.assertIsNone(ambiguous.resolved_image_path)

    @unittest.skipUnless(os.name == "nt", "Windows 대소문자 정책은 Windows에서 검증합니다.")
    def test_windows_same_stem_comparison_is_case_insensitive(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            actual = root / "Frame.PNG"
            actual.write_bytes(b"image")

            result = resolve_image_path(root, "frame.jpg")

            self.assertEqual(result.path_status, FALLBACK)
            self.assertEqual(result.resolved_image_path, actual.resolve())

    def test_absolute_and_parent_traversal_paths_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            unsafe_names = (
                str((root.parent / "outside.jpg").resolve()),
                r"C:\outside.jpg",
                r"\outside.jpg",
                "../outside.jpg",
                r"nested\..\outside.jpg",
            )
            for image_name in unsafe_names:
                with self.subTest(image_name=image_name):
                    with self.assertRaises(ValueError):
                        resolve_image_path(root, image_name)

    @unittest.skipUnless(hasattr(os, "symlink"), "symlink를 지원하는 환경에서만 실행합니다.")
    def test_exact_symlink_escape_is_rejected(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            tempfile.TemporaryDirectory() as outside_directory,
        ):
            root = Path(directory)
            outside = Path(outside_directory) / "outside.jpg"
            outside.write_bytes(b"outside")
            link = root / "linked.jpg"
            try:
                os.symlink(outside, link)
            except OSError:
                self.skipTest("현재 계정에서 파일 symlink 생성 권한이 없습니다.")

            with self.assertRaises(ValueError):
                resolve_image_path(root, "linked.jpg")

    @unittest.skipUnless(hasattr(os, "symlink"), "symlink를 지원하는 환경에서만 실행합니다.")
    def test_fallback_symlink_candidate_escape_is_rejected(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            tempfile.TemporaryDirectory() as outside_directory,
        ):
            root = Path(directory)
            outside = Path(outside_directory) / "frame.png"
            outside.write_bytes(b"outside")
            link = root / "frame.png"
            try:
                os.symlink(outside, link)
            except OSError:
                self.skipTest("현재 계정에서 파일 symlink 생성 권한이 없습니다.")

            with self.assertRaises(ValueError):
                resolve_image_path(root, "frame.jpg")


class CameraReviewIndexTests(unittest.TestCase):
    def test_non_contiguous_ids_are_sorted_and_frame_map_is_consecutive(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("ten.jpg", "three.jpg", "forty.jpg"):
                (root / name).write_bytes(b"image")
            reconstruction = Reconstruction(
                cameras={
                    1: _camera(1),
                    2: _camera(2, "SIMPLE_PINHOLE"),
                    3: _camera(3, "OPENCV"),
                },
                images={
                    40: _image(40, 3, "forty.jpg"),
                    3: _image(3, 2, "three.jpg"),
                    10: _image(10, 1, "ten.jpg"),
                },
            )

            review = build_review_index(
                reconstruction,
                root,
                dataset_uuid="dataset",
                source_fingerprint="fingerprint",
                edit_revision=7,
                frame_start=-2,
            )

            self.assertEqual(tuple(entry.image_id for entry in review.entries), (3, 10, 40))
            self.assertEqual(dict(review.image_id_to_index), {3: 0, 10: 1, 40: 2})
            self.assertEqual(dict(review.frame_to_image_id), {-2: 3, -1: 10, 0: 40})
            self.assertEqual(
                tuple(entry.exact_projection_supported for entry in review.entries),
                (True, True, False),
            )
            self.assertEqual(
                tuple((entry.width, entry.height) for entry in review.entries),
                ((1920, 1080), (1920, 1080), (1920, 1080)),
            )
            self.assertEqual(review.entries[0].image_name, "three.jpg")

    def test_build_preserves_path_status_and_original_windows_image_name(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            nested = root / "nested"
            nested.mkdir()
            actual = nested / "frame.png"
            actual.write_bytes(b"image")
            original_name = r"nested\frame.jpg"
            reconstruction = Reconstruction(
                cameras={1: _camera(1)},
                images={9: _image(9, 1, original_name)},
            )

            review = build_review_index(
                reconstruction,
                root,
                "dataset",
                "fingerprint",
                1,
                100,
            )

            self.assertEqual(review.entries[0].image_name, original_name)
            self.assertEqual(review.entries[0].path_status, FALLBACK)
            self.assertEqual(review.entries[0].resolved_image_path, actual.resolve())

    def test_empty_reconstruction_builds_immutable_empty_mappings(self):
        with tempfile.TemporaryDirectory() as directory:
            review = build_review_index(
                Reconstruction(),
                Path(directory),
                "dataset",
                "fingerprint",
                0,
                1,
            )

            self.assertEqual(review.entries, ())
            self.assertEqual(dict(review.image_id_to_index), {})
            self.assertEqual(dict(review.frame_to_image_id), {})
            with self.assertRaises(TypeError):
                review.image_id_to_index[1] = 0
            with self.assertRaises(TypeError):
                review.frame_to_image_id[1] = 1

    def test_review_entry_and_index_are_frozen(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "frame.jpg").write_bytes(b"image")
            review = build_review_index(
                Reconstruction(
                    cameras={1: _camera(1)},
                    images={1: _image(1, 1, "frame.jpg")},
                ),
                root,
                "dataset",
                "fingerprint",
                0,
                1,
            )

            with self.assertRaises(FrozenInstanceError):
                review.entries[0].image_id = 2
            with self.assertRaises(FrozenInstanceError):
                review.edit_revision = 2

    def test_image_dict_key_and_record_id_mismatch_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            reconstruction = Reconstruction(
                cameras={1: _camera(1)},
                images={7: _image(8, 1, "frame.jpg")},
            )
            with self.assertRaises(ValueError):
                build_review_index(
                    reconstruction,
                    Path(directory),
                    "dataset",
                    "fingerprint",
                    0,
                    1,
                )

    def test_missing_camera_and_camera_key_mismatch_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaises(ValueError):
                build_review_index(
                    Reconstruction(
                        cameras={1: _camera(2)},
                        images={7: _image(7, 2, "frame.jpg")},
                    ),
                    root,
                    "dataset",
                    "fingerprint",
                    0,
                    1,
                )
            with self.assertRaises(ValueError):
                build_review_index(
                    Reconstruction(
                        cameras={1: _camera(1)},
                        images={7: _image(7, 2, "frame.jpg")},
                    ),
                    root,
                    "dataset",
                    "fingerprint",
                    0,
                    1,
                )


class CameraReviewNavigationTests(unittest.TestCase):
    def test_empty_and_single_item_navigation_are_safe_and_clamped(self):
        self.assertIsNone(previous_index(0, 0))
        self.assertIsNone(next_index(0, 0))
        self.assertEqual(previous_index(0, 1), 0)
        self.assertEqual(next_index(0, 1), 0)

    def test_first_previous_and_last_next_are_clamped(self):
        self.assertEqual(previous_index(0, 3), 0)
        self.assertEqual(previous_index(2, 3), 1)
        self.assertEqual(next_index(0, 3), 1)
        self.assertEqual(next_index(2, 3), 2)

    def test_navigation_accepts_current_image_id_or_index(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for image_id in (3, 10, 40):
                (root / f"{image_id}.jpg").write_bytes(b"image")
            review = build_review_index(
                Reconstruction(
                    cameras={1: _camera(1)},
                    images={
                        image_id: _image(image_id, 1, f"{image_id}.jpg")
                        for image_id in (40, 3, 10)
                    },
                ),
                root,
                "dataset",
                "fingerprint",
                0,
                1,
            )

            self.assertEqual(navigate_previous(review, current_image_id=10), 0)
            self.assertEqual(navigate_next(review, current_image_id=10), 2)
            self.assertEqual(navigate_previous(review, current_index=0), 0)
            self.assertEqual(navigate_next(review, current_index=2), 2)

    def test_preferred_position_after_middle_last_and_all_deletions(self):
        self.assertEqual(preferred_index_after_deletion(1, 2), 1)
        self.assertEqual(preferred_index_after_deletion(2, 2), 1)
        self.assertIsNone(preferred_index_after_deletion(0, 0))


if __name__ == "__main__":
    unittest.main()
