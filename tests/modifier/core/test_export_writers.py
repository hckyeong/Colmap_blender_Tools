from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from tools.modifier.core import binary_writer
from tools.modifier.core.binary_io import read_model_binary
from tools.modifier.core.binary_writer import (
    _write_model_binary_validated,
    write_model_binary,
)
from tools.modifier.core.model import (
    CameraRecord,
    ImageRecord,
    Point2DRecord,
    Point3DRecord,
    Reconstruction,
    TrackElement,
)
from tools.modifier.core.text_io import read_model_text
from tools.modifier.core.text_writer import (
    _write_model_text_validated,
    write_model_text,
)
from tests.modifier.fixture_utils import (
    make_legacy_binary,
    make_legacy_text,
    make_modern_binary,
    make_modern_text,
)


def _special_legacy_model() -> Reconstruction:
    camera_id = 4_000_000_000
    image_id = 3_000_000_000
    empty_image_id = 7
    point3d_id = (1 << 63) + 19
    camera = CameraRecord(
        camera_id=camera_id,
        model_id=4,
        model_name="OPENCV",
        width=6000,
        height=4000,
        params=(4200.5, 4199.25, 3000.125, 2000.875, 0.1, -0.02, 0.003, -0.004),
    )
    observed_image = ImageRecord(
        image_id=image_id,
        qvec=(1.0, -0.0, 0.125, -0.25),
        tvec=(1.0e-20, -2.0e20, 3.5),
        camera_id=camera_id,
        name="한글 촬영 image 01.jpg",
        points2d=(
            Point2DRecord(10.25, 20.5, None),
            Point2DRecord(30.75, 40.125, point3d_id),
        ),
    )
    empty_image = ImageRecord(
        image_id=empty_image_id,
        qvec=(1.0, 0.0, 0.0, 0.0),
        tvec=(0.0, 0.0, 0.0),
        camera_id=camera_id,
        name="empty observations.jpg",
        points2d=(),
    )
    point = Point3DRecord(
        point3d_id=point3d_id,
        xyz=(1.0 / 3.0, -2.5, 9.0e-100),
        rgb=(1, 127, 255),
        error=0.12345678901234566,
        track=(TrackElement(image_id, 1),),
    )
    # 삽입 순서도 writer의 결정적 출력에 포함되도록 ID 오름차순과 다르게 구성합니다.
    return Reconstruction(
        cameras={camera_id: camera},
        images={image_id: observed_image, empty_image_id: empty_image},
        points3d={point3d_id: point},
    )


class ColmapExportWriterRoundTripTests(unittest.TestCase):
    def test_legacy_text_round_trip_preserves_special_values_and_lf(self):
        model = _special_legacy_model()
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            paths = write_model_text(output, model, modern=False)

            self.assertEqual(
                tuple(path.name for path in paths),
                ("cameras.txt", "images.txt", "points3D.txt"),
            )
            self.assertEqual(read_model_text(output, modern=False), model)
            self.assertNotIn(b"\r\n", (output / "images.txt").read_bytes())
            text = (output / "images.txt").read_text(encoding="utf-8")
            self.assertIn("한글 촬영 image 01.jpg", text)
            self.assertIn("10.25 20.5 -1", text)
            self.assertIn("empty observations.jpg\n\n", text)

    def test_legacy_binary_round_trip_preserves_special_values(self):
        model = _special_legacy_model()
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            paths = write_model_binary(output, model, modern=False)

            self.assertEqual(
                tuple(path.name for path in paths),
                ("cameras.bin", "images.bin", "points3D.bin"),
            )
            self.assertEqual(read_model_binary(output, modern=False), model)

    def test_modern_text_round_trip_preserves_rig_frame_and_uint64_data_id(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, _images = make_modern_text(root / "source")
            model = read_model_text(source, modern=True)
            output = root / "output"
            output.mkdir()
            (output / "user.bin").write_bytes(b"keep")

            paths = write_model_text(output, model, modern=True)

            self.assertEqual(
                tuple(path.name for path in paths),
                ("rigs.txt", "cameras.txt", "frames.txt", "images.txt", "points3D.txt"),
            )
            self.assertEqual(read_model_text(output, modern=True), model)
            self.assertEqual((output / "user.bin").read_bytes(), b"keep")
            self.assertEqual(
                read_model_text(output, modern=True).frames[1].data_ids[1].data_id,
                4_294_967_297,
            )

    def test_modern_binary_round_trip_preserves_rig_frame_and_uint64_data_id(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, _images = make_modern_binary(root / "source")
            model = read_model_binary(source, modern=True)
            output = root / "output"
            output.mkdir()
            (output / "user.txt").write_text("보존", encoding="utf-8")

            paths = write_model_binary(output, model, modern=True)

            self.assertEqual(
                tuple(path.name for path in paths),
                ("rigs.bin", "cameras.bin", "frames.bin", "images.bin", "points3D.bin"),
            )
            self.assertEqual(read_model_binary(output, modern=True), model)
            self.assertEqual((output / "user.txt").read_text(encoding="utf-8"), "보존")
            self.assertEqual(
                read_model_binary(output, modern=True).frames[1].data_ids[1].data_id,
                4_294_967_297,
            )

    def test_existing_legacy_fixtures_round_trip_in_both_formats(self):
        cases = (
            (make_legacy_text, read_model_text, write_model_text),
            (make_legacy_binary, read_model_binary, write_model_binary),
        )
        for fixture, reader, writer in cases:
            with self.subTest(fixture=fixture.__name__), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source, _images = fixture(root / "source")
                model = reader(source, modern=False)
                output = root / "output"
                output.mkdir()
                writer(output, model, modern=False)
                self.assertEqual(reader(output, modern=False), model)

    def test_same_model_produces_stable_bytes(self):
        model = _special_legacy_model()
        for writer, names in (
            (write_model_text, ("cameras.txt", "images.txt", "points3D.txt")),
            (write_model_binary, ("cameras.bin", "images.bin", "points3D.bin")),
        ):
            with self.subTest(writer=writer.__name__), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                first = root / "first"
                second = root / "second"
                first.mkdir()
                second.mkdir()
                writer(first, model, modern=False)
                writer(second, model, modern=False)
                for name in names:
                    self.assertEqual((first / name).read_bytes(), (second / name).read_bytes())

    def test_binary_writer_does_not_use_module_level_struct_pack(self):
        model = _special_legacy_model()
        with tempfile.TemporaryDirectory() as directory, patch.object(
            binary_writer.struct,
            "pack",
            side_effect=AssertionError("모듈 수준 struct.pack() 재호출"),
        ):
            output = Path(directory)
            write_model_binary(output, model, modern=False)
            self.assertEqual(read_model_binary(output, modern=False), model)


class ColmapExportWriterValidationTests(unittest.TestCase):
    def test_invalid_image_names_are_rejected_before_any_file_is_written(self):
        base = _special_legacy_model()
        image_id = next(iter(base.images))
        invalid_names = (
            "bad\0name.jpg",
            "bad\nname.jpg",
            "bad\rname.jpg",
            " leading.jpg",
            "trailing.jpg ",
            "broken\ud800.jpg",
        )
        for writer in (write_model_text, write_model_binary):
            for name in invalid_names:
                with (
                    self.subTest(writer=writer.__name__, name=repr(name)),
                    tempfile.TemporaryDirectory() as directory,
                ):
                    output = Path(directory)
                    images = dict(base.images)
                    images[image_id] = replace(images[image_id], name=name)
                    model = Reconstruction(
                        cameras=base.cameras,
                        images=images,
                        points3d=base.points3d,
                    )
                    with self.assertRaisesRegex(ValueError, "이름"):
                        writer(output, model, modern=False)
                    self.assertEqual(tuple(output.iterdir()), ())

    def test_uint32_camera_id_overflow_is_rejected_before_write(self):
        camera_id = 1 << 32
        model = Reconstruction(
            cameras={
                camera_id: CameraRecord(
                    camera_id,
                    0,
                    "SIMPLE_PINHOLE",
                    640,
                    480,
                    (500.0, 320.0, 240.0),
                )
            }
        )
        for writer in (write_model_text, write_model_binary):
            with self.subTest(writer=writer.__name__), tempfile.TemporaryDirectory() as directory:
                output = Path(directory)
                with self.assertRaisesRegex(ValueError, "허용 범위"):
                    writer(output, model, modern=False)
                self.assertEqual(tuple(output.iterdir()), ())

    def test_point3d_sentinel_id_is_rejected(self):
        model = _special_legacy_model()
        old_id = next(iter(model.points3d))
        sentinel = (1 << 64) - 1
        image_id = next(iter(model.images))
        images = dict(model.images)
        image = images[image_id]
        points2d = list(image.points2d)
        points2d[1] = replace(points2d[1], point3d_id=sentinel)
        images[image_id] = replace(image, points2d=tuple(points2d))
        point = model.points3d[old_id]
        points3d = {
            sentinel: replace(point, point3d_id=sentinel),
        }
        invalid = Reconstruction(model.cameras, images, points3d)

        for writer in (write_model_text, write_model_binary):
            with self.subTest(writer=writer.__name__), tempfile.TemporaryDirectory() as directory:
                with self.assertRaisesRegex(ValueError, "허용 범위"):
                    writer(Path(directory), invalid, modern=False)

        for writer in (
            _write_model_text_validated,
            _write_model_binary_validated,
        ):
            with self.subTest(writer=writer.__name__), tempfile.TemporaryDirectory() as directory:
                with self.assertRaisesRegex(ValueError, "허용 범위"):
                    writer(Path(directory), invalid, modern=False)

    def test_legacy_layout_rejects_rig_and_frame_loss(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, _images = make_modern_text(root / "source")
            model = read_model_text(source, modern=True)
            output = root / "output"
            output.mkdir()
            with self.assertRaisesRegex(ValueError, "legacy layout"):
                write_model_text(output, model, modern=False)
            self.assertEqual(tuple(output.iterdir()), ())

    def test_missing_output_directory_is_not_created_implicitly(self):
        model = _special_legacy_model()
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "missing"
            with self.assertRaisesRegex(ValueError, "존재하지 않습니다"):
                write_model_binary(output, model, modern=False)
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
