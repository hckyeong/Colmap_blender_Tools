from __future__ import annotations

import os
import inspect
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from tools.modifier.core.binary_io import read_frames_binary
from tools.modifier.core.camera_review import build_review_index
from tools.modifier.core.camera_models import BY_ID
from tools.modifier.core.discovery import discover_dataset
from tools.modifier.core.edit_replay import replay_history
from tools.modifier.core import indexes as indexes_module
from tools.modifier.core.integrity import validate_reconstruction
from tools.modifier.core.loader import load_reconstruction
from tools.modifier.core.model import (
    ImagePathManifest,
    Reconstruction,
    ValidatedImagePath,
)
from tools.modifier.core import paths as paths_module
from tools.modifier.core.paths import resolve_image_path
from tools.modifier.core.text_io import read_model_text
from tests.modifier.fixture_utils import (
    make_legacy_binary,
    make_legacy_text,
    make_legacy_text_multi_point,
    make_modern_binary,
    make_modern_text,
)


class CameraModelRegistryTests(unittest.TestCase):
    def test_current_colmap_models_are_registered(self):
        expected = {
            0: ("SIMPLE_PINHOLE", 3),
            1: ("PINHOLE", 4),
            11: ("RAD_TAN_THIN_PRISM_FISHEYE", 16),
            12: ("SIMPLE_DIVISION", 4),
            13: ("DIVISION", 5),
            14: ("SIMPLE_FISHEYE", 3),
            15: ("FISHEYE", 4),
            16: ("EUCM", 6),
            17: ("EQUIRECTANGULAR", 2),
        }
        for model_id, (name, count) in expected.items():
            self.assertEqual(BY_ID[model_id].name, name)
            self.assertEqual(BY_ID[model_id].num_params, count)
        self.assertTrue(BY_ID[0].exact_blender_projection)
        self.assertTrue(BY_ID[1].exact_blender_projection)
        self.assertFalse(BY_ID[12].exact_blender_projection)


class ImportCoreTests(unittest.TestCase):
    def test_legacy_text_large_point_id_and_space_name(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model, images = make_legacy_text(root)
            loaded = load_reconstruction(root, model, images, "AUTO")
            self.assertEqual(loaded.source.input_format, "text")
            self.assertEqual(loaded.source.layout, "legacy")
            self.assertIn(4294967301, loaded.reconstruction.points3d)
            self.assertEqual(loaded.reconstruction.images[7].name, "frame 01.jpg")

    def test_legacy_binary_invalid_point_sentinel(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model, images = make_legacy_binary(root)
            loaded = load_reconstruction(root, model, images, "BINARY")
            points = loaded.reconstruction.images[3].points2d
            self.assertEqual(points[0].point3d_id, 99)
            self.assertIsNone(points[1].point3d_id)

    def test_modern_text_ref_sensor_is_separate_and_imu_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model, images = make_modern_text(root)
            loaded = load_reconstruction(root, model, images, "TEXT")
            rig = loaded.reconstruction.rigs[1]
            self.assertEqual((rig.ref_sensor_type, rig.ref_sensor_id), (0, 1))
            self.assertEqual(len(rig.sensors), 1)
            self.assertEqual((rig.sensors[0].sensor_type, rig.sensors[0].sensor_id), (1, 7))
            self.assertEqual(loaded.reconstruction.frames[1].data_ids[1].data_id, 4294967297)

    def test_modern_binary_uint64_data_id(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model, images = make_modern_binary(root)
            loaded = load_reconstruction(root, model, images, "BINARY")
            self.assertEqual(loaded.source.layout, "modern")
            self.assertEqual(loaded.reconstruction.frames[1].data_ids[1].data_id, 4294967297)
            direct = read_frames_binary(model / "frames.bin")
            self.assertEqual(direct[1].data_ids[1].data_id, 4294967297)

    def test_dataset_root_discovers_sparse_zero_and_images(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model, images = make_legacy_text(root)
            result = discover_dataset(root)
            self.assertEqual(result.sparse_directory, model.resolve())
            self.assertEqual(result.images_directory, images.resolve())

    def test_standard_dataset_discovery_defers_image_data_validation_to_loader(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model, images = make_legacy_text(root)
            (images / "frame 01.jpg").unlink()

            with (
                patch(
                    "tools.modifier.core.discovery.read_images_text",
                    side_effect=AssertionError("표준 경로에서 images parser를 호출했습니다."),
                ),
                patch(
                    "tools.modifier.core.discovery.validate_image_files",
                    side_effect=AssertionError("표준 경로에서 이미지 파일을 검사했습니다."),
                ),
            ):
                result = discover_dataset(root)

            self.assertEqual(result.sparse_directory, model.resolve())
            self.assertEqual(result.images_directory, images.resolve())
            with self.assertRaisesRegex(ValueError, "이미지 파일"):
                load_reconstruction(root, model, images, "TEXT")

    def test_standard_sparse_zero_is_prioritized_among_multiple_candidates(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model, _images = make_legacy_text(root)
            second = root / "sparse" / "1"
            second.mkdir()
            for name in ("cameras.txt", "images.txt", "points3D.txt"):
                source = root / "sparse" / "0" / name
                (second / name).write_bytes(source.read_bytes())
            result = discover_dataset(root)
            self.assertEqual(result.sparse_directory, model.resolve())
            self.assertEqual(len(result.sparse_candidates), 2)

    @unittest.skipUnless(hasattr(os, "symlink"), "symlink를 지원하는 환경에서만 실행합니다.")
    def test_discovery_does_not_follow_directory_symlink(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as outside:
            root = Path(directory)
            outside_root = Path(outside)
            make_legacy_text(outside_root)
            link = root / "sparse_link"
            try:
                os.symlink(outside_root / "sparse", link, target_is_directory=True)
            except OSError:
                self.skipTest("현재 계정에서 directory symlink 생성 권한이 없습니다.")
            result = discover_dataset(root)
            self.assertFalse(result.sparse_candidates)

    def test_missing_image_rejects_import(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model, images = make_legacy_text(root)
            (images / "frame 01.jpg").unlink()
            with self.assertRaisesRegex(ValueError, "이미지 파일"):
                load_reconstruction(root, model, images, "TEXT")

    def test_modern_frame_camera_sensor_must_match_image_camera(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model, images = make_modern_text(root)
            (model / "frames.txt").write_text(
                "1 1 1 0 0 0 0 0 0 1 CAMERA 2 7\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "sensor|Camera 레코드"):
                load_reconstruction(root, model, images, "TEXT")

    def test_modern_image_must_belong_to_a_frame(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model, images = make_modern_text(root)
            (model / "frames.txt").write_text(
                "1 1 1 0 0 0 0 0 0 1 IMU 7 4294967297\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "어떤 Frame"):
                load_reconstruction(root, model, images, "TEXT")

    def test_non_positive_pinhole_focal_length_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model, images = make_legacy_text(root)
            (model / "cameras.txt").write_text(
                "1 PINHOLE 1000 800 0 880 510 390\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "focal length"):
                load_reconstruction(root, model, images, "TEXT")

    def test_image_path_cannot_escape_images_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            images = Path(directory)
            with self.assertRaisesRegex(ValueError, "안전하지 않은"):
                resolve_image_path(images, "../outside.jpg")

    def test_valid_track_pairs_do_not_require_a_global_sort(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model_directory, _images = make_legacy_text_multi_point(root)
            reconstruction = read_model_text(model_directory, modern=False)
            original_sorted = sorted

            def reject_track_pair_sort(values, *args, **kwargs):
                materialized = list(values)
                if materialized and all(
                    isinstance(value, tuple)
                    and len(value) == 2
                    and all(isinstance(item, int) for item in value)
                    for value in materialized
                ):
                    raise AssertionError("정상 Track pair 전체를 정렬했습니다.")
                return original_sorted(materialized, *args, **kwargs)

            with patch(
                "tools.modifier.core.integrity.sorted",
                side_effect=reject_track_pair_sort,
                create=True,
            ):
                validate_reconstruction(reconstruction)

    def test_track_pair_mismatch_messages_keep_pair_order(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model_directory, _images = make_legacy_text_multi_point(root)
            reconstruction = read_model_text(model_directory, modern=False)
            image = reconstruction.images[7]
            swapped_points = (
                replace(image.points2d[0], point3d_id=image.points2d[1].point3d_id),
                replace(image.points2d[1], point3d_id=image.points2d[0].point3d_id),
                image.points2d[2],
            )
            invalid = Reconstruction(
                cameras=dict(reconstruction.cameras),
                images={7: replace(image, points2d=swapped_points)},
                points3d=dict(reconstruction.points3d),
            )

            with self.assertRaises(ValueError) as raised:
                validate_reconstruction(invalid)

            message = str(raised.exception)
            first = message.find("POINT2D_IDX 0")
            second = message.find("POINT2D_IDX 1")
            self.assertGreaterEqual(first, 0)
            self.assertGreater(second, first)

    def test_loader_validation_token_builds_initial_indexes_without_revalidation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model_directory, images_directory = make_legacy_text(root)
            loaded = load_reconstruction(
                root,
                model_directory,
                images_directory,
                "TEXT",
            )
            token = getattr(loaded, "validation_token", None)
            self.assertIsNotNone(token)
            self.assertIn(
                "validation_token",
                inspect.signature(replay_history).parameters,
            )

            with patch(
                "tools.modifier.core.indexes.validate_reconstruction",
                side_effect=AssertionError("Loader 검증 직후 모델을 다시 검사했습니다."),
            ):
                current, indexes = replay_history(
                    loaded.reconstruction,
                    (),
                    loaded.source.dataset_uuid,
                    loaded.source.source_fingerprint,
                    validation_token=token,
                )

            self.assertIs(current, loaded.reconstruction)
            self.assertEqual(
                indexes.point_to_images[4294967301],
                frozenset((7,)),
            )

    def test_validation_token_is_bound_to_model_and_source_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model_directory, images_directory = make_legacy_text(root)
            loaded = load_reconstruction(
                root,
                model_directory,
                images_directory,
                "TEXT",
            )
            builder = getattr(
                indexes_module,
                "build_relationship_indexes_validated",
                None,
            )
            self.assertIsNotNone(builder)
            token = getattr(loaded, "validation_token", None)
            self.assertIsNotNone(token)

            for model, dataset_uuid, source_fingerprint in (
                (
                    Reconstruction(),
                    loaded.source.dataset_uuid,
                    loaded.source.source_fingerprint,
                ),
                (
                    loaded.reconstruction,
                    "다른-dataset",
                    loaded.source.source_fingerprint,
                ),
                (
                    loaded.reconstruction,
                    loaded.source.dataset_uuid,
                    "다른-fingerprint",
                ),
            ):
                with self.subTest(
                    same_model=model is loaded.reconstruction,
                    dataset_uuid=dataset_uuid,
                    source_fingerprint=source_fingerprint,
                ):
                    with self.assertRaises(ValueError):
                        builder(
                            model,
                            token,
                            dataset_uuid=dataset_uuid,
                            source_fingerprint=source_fingerprint,
                            validation_revision=0,
                        )

    def test_loader_validation_token_rejects_reconstruction_mapping_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model_directory, images_directory = make_legacy_text(root)
            loaded = load_reconstruction(
                root,
                model_directory,
                images_directory,
                "TEXT",
            )
            token = loaded.validation_token
            self.assertIsNotNone(token)

            with self.assertRaises((AttributeError, TypeError)):
                loaded.reconstruction.images.clear()

            original_images = loaded.reconstruction.images
            loaded.reconstruction.images = {}
            try:
                with self.assertRaisesRegex(ValueError, "mapping"):
                    indexes_module.build_relationship_indexes_validated(
                        loaded.reconstruction,
                        token,
                        dataset_uuid=loaded.source.dataset_uuid,
                        source_fingerprint=loaded.source.source_fingerprint,
                        validation_revision=0,
                    )
            finally:
                loaded.reconstruction.images = original_images

    def test_loader_image_manifest_is_immutable_and_reused_by_camera_review(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model_directory, images_directory = make_legacy_text(root)
            loaded = load_reconstruction(
                root,
                model_directory,
                images_directory,
                "TEXT",
            )
            manifest = getattr(loaded, "image_path_manifest", None)
            self.assertIsNotNone(manifest)
            self.assertEqual(manifest.dataset_uuid, loaded.source.dataset_uuid)
            self.assertEqual(
                manifest.source_fingerprint,
                loaded.source.source_fingerprint,
            )
            self.assertEqual(
                manifest.images_directory,
                images_directory.resolve(),
            )
            self.assertEqual(
                manifest.entries[7].resolved_image_path,
                (images_directory / "frame 01.jpg").resolve(),
            )
            with self.assertRaises(TypeError):
                manifest.entries[7] = manifest.entries[7]

            with patch(
                "tools.modifier.core.camera_review.resolve_image_path",
                side_effect=AssertionError("검증 완료 이미지 경로를 다시 해석했습니다."),
            ):
                review = build_review_index(
                    loaded.reconstruction,
                    loaded.source.images_directory,
                    loaded.source.dataset_uuid,
                    loaded.source.source_fingerprint,
                    edit_revision=0,
                    frame_start=1,
                    image_path_manifest=manifest,
                )

            self.assertEqual(review.entries[0].path_status, "EXACT")
            self.assertEqual(
                review.entries[0].resolved_image_path,
                manifest.entries[7].resolved_image_path,
            )

    def test_camera_review_rejects_image_manifest_from_another_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model_directory, images_directory = make_legacy_text(root)
            loaded = load_reconstruction(
                root,
                model_directory,
                images_directory,
                "TEXT",
            )
            manifest = getattr(loaded, "image_path_manifest", None)
            self.assertIsNotNone(manifest)
            mismatched = replace(
                manifest,
                source_fingerprint="다른-fingerprint",
            )

            with self.assertRaises(ValueError):
                build_review_index(
                    loaded.reconstruction,
                    loaded.source.images_directory,
                    loaded.source.dataset_uuid,
                    loaded.source.source_fingerprint,
                    edit_revision=0,
                    frame_start=1,
                    image_path_manifest=mismatched,
                )

    def test_image_manifest_freshness_detects_file_removed_after_loading(self):
        validator = getattr(
            paths_module,
            "validate_image_path_manifest_current",
            None,
        )
        self.assertIsNotNone(validator)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model_directory, images_directory = make_legacy_text(root)
            loaded = load_reconstruction(
                root,
                model_directory,
                images_directory,
                "TEXT",
            )
            manifest = loaded.image_path_manifest
            self.assertIsNotNone(manifest)
            manifest.entries[7].resolved_image_path.unlink()

            with self.assertRaisesRegex(ValueError, "이미지 파일"):
                validator(manifest)

    @unittest.skipUnless(hasattr(os, "symlink"), "symlink를 지원하는 환경에서만 실행합니다.")
    def test_image_manifest_freshness_rejects_external_symlink_replacement(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            tempfile.TemporaryDirectory() as outside_directory,
        ):
            root = Path(directory)
            model_directory, images_directory = make_legacy_text(root)
            loaded = load_reconstruction(
                root,
                model_directory,
                images_directory,
                "TEXT",
            )
            manifest = loaded.image_path_manifest
            self.assertIsNotNone(manifest)
            image_path = manifest.entries[7].resolved_image_path
            image_path.unlink()
            outside = Path(outside_directory) / "outside.jpg"
            outside.write_bytes(b"outside")
            try:
                os.symlink(outside, image_path)
            except OSError:
                self.skipTest("현재 계정에서 파일 symlink 생성 권한이 없습니다.")

            with self.assertRaises(ValueError):
                paths_module.validate_image_path_manifest_current(manifest)

    def test_image_manifest_rejects_lexical_parent_escape(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            images_directory = root / "images"
            images_directory.mkdir()
            outside = root / "outside.png"
            outside.write_bytes(b"fixture")
            lexical_escape = images_directory / ".." / outside.name

            with self.assertRaisesRegex(ValueError, "밖"):
                ImagePathManifest(
                    dataset_uuid="dataset",
                    source_fingerprint="fingerprint",
                    images_directory=images_directory,
                    entries={
                        1: ValidatedImagePath(
                            image_id=1,
                            image_name="outside.png",
                            resolved_image_path=lexical_escape,
                            path_status="EXACT",
                        )
                    },
                )


if __name__ == "__main__":
    unittest.main()
