from __future__ import annotations

import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.modifier.core import binary_io
from tools.modifier.core.binary_io import (
    read_cameras_binary,
    read_frames_binary,
    read_images_binary,
    read_points3d_binary,
    read_rigs_binary,
)


def _packed(format_string: str, *values: object) -> bytes:
    return struct.pack("<" + format_string, *values)


def _write(path: Path, *parts: bytes) -> Path:
    path.write_bytes(b"".join(parts))
    return path


def _camera_record(camera_id: int) -> bytes:
    return _packed("IiQQ", camera_id, 0, 640, 480) + _packed(
        "ddd", 500.0, 320.0, 240.0
    )


def _image_record(image_id: int, name: bytes, points: tuple[bytes, ...] = ()) -> bytes:
    return b"".join(
        (
            _packed(
                "IdddddddI",
                image_id,
                1.0,
                0.0,
                0.0,
                0.0,
                0.0,
                0.0,
                0.0,
                1,
            ),
            name,
            b"\0",
            _packed("Q", len(points)),
            *points,
        )
    )


def _point_record(point3d_id: int, tracks: tuple[bytes, ...] = ()) -> bytes:
    return b"".join(
        (
            _packed("QdddBBBd", point3d_id, 1.0, 2.0, 3.0, 10, 20, 30, 0.25),
            _packed("Q", len(tracks)),
            *tracks,
        )
    )


def _rig_record(rig_id: int) -> bytes:
    return _packed("IIiI", rig_id, 1, 0, 1)


def _frame_record(frame_id: int, data_ids: tuple[bytes, ...] = ()) -> bytes:
    return b"".join(
        (
            _packed(
                "IIddddddd",
                frame_id,
                1,
                1.0,
                0.0,
                0.0,
                0.0,
                0.0,
                0.0,
                0.0,
            ),
            _packed("I", len(data_ids)),
            *data_ids,
        )
    )


class BinaryParserContractTests(unittest.TestCase):
    def test_empty_files_return_empty_mappings(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            readers = (
                read_cameras_binary,
                read_images_binary,
                read_points3d_binary,
                read_rigs_binary,
                read_frames_binary,
            )
            for index, reader in enumerate(readers):
                with self.subTest(reader=reader.__name__):
                    path = _write(root / f"{index}.bin", _packed("Q", 0))
                    self.assertEqual(reader(path), {})

    def test_dictionary_order_unicode_name_sentinel_and_uint64_data_id_are_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            high_uint32_image_id = (1 << 31) + 7
            high_uint64_point_id = (1 << 40) + 99
            cameras = _write(
                root / "cameras.bin",
                _packed("Q", 2),
                _camera_record(10),
                _camera_record(2),
            )
            images = _write(
                root / "images.bin",
                _packed("Q", 1),
                _image_record(
                    high_uint32_image_id,
                    "고양이 frame 01.jpg".encode("utf-8"),
                    (
                        _packed("ddQ", 1.0, 2.0, high_uint64_point_id),
                        _packed("ddQ", 3.0, 4.0, (1 << 64) - 1),
                    ),
                ),
            )
            frames = _write(
                root / "frames.bin",
                _packed("Q", 1),
                _frame_record(1, (_packed("iIQ", 1, 7, (1 << 32) + 1),)),
            )
            points = _write(
                root / "points3D.bin",
                _packed("Q", 1),
                _point_record(
                    high_uint64_point_id,
                    (_packed("II", high_uint32_image_id, 0),),
                ),
            )

            self.assertEqual(list(read_cameras_binary(cameras)), [10, 2])
            image = read_images_binary(images)[high_uint32_image_id]
            self.assertEqual(image.name, "고양이 frame 01.jpg")
            self.assertEqual(image.points2d[0].point3d_id, high_uint64_point_id)
            self.assertIsNone(image.points2d[1].point3d_id)
            point = read_points3d_binary(points)[high_uint64_point_id]
            self.assertEqual(point.track[0].image_id, high_uint32_image_id)
            self.assertEqual(
                read_frames_binary(frames)[1].data_ids[0].data_id,
                (1 << 32) + 1,
            )

    def test_physical_empty_file_is_normalized_to_eof_value_error(self):
        with tempfile.TemporaryDirectory() as directory:
            path = _write(Path(directory) / "empty.bin")
            with self.assertRaisesRegex(ValueError, "Camera 개수"):
                read_cameras_binary(path)

    def test_each_fixed_header_rejects_early_eof_with_context(self):
        cases = (
            ("camera", read_cameras_binary, "Camera", _packed("Q", 1) + b"\0"),
            ("image", read_images_binary, "Image", _packed("Q", 1) + b"\0"),
            ("point", read_points3d_binary, "Point3D", _packed("Q", 1) + b"\0"),
            ("rig", read_rigs_binary, "Rig", _packed("Q", 1) + b"\0"),
            ("frame", read_frames_binary, "Frame", _packed("Q", 1) + b"\0"),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, reader, context, payload in cases:
                with self.subTest(reader=name):
                    path = _write(root / f"{name}.bin", payload)
                    with self.assertRaisesRegex(ValueError, context):
                        reader(path)

    def test_point2d_track_sensor_and_data_id_blocks_reject_early_eof(self):
        image_header = _packed(
            "IdddddddI",
            1,
            1.0,
            0.0,
            0.0,
            0.0,
            0.0,
            0.0,
            0.0,
            1,
        )
        point_header = _packed("QdddBBBd", 9, 1.0, 2.0, 3.0, 1, 2, 3, 0.1)
        frame_header = _packed(
            "IIddddddd",
            1,
            1,
            1.0,
            0.0,
            0.0,
            0.0,
            0.0,
            0.0,
            0.0,
        )
        cases = (
            (
                "point2d",
                read_images_binary,
                "Point2D",
                _packed("Q", 1)
                + image_header
                + b"a.jpg\0"
                + _packed("Q", 1)
                + b"\0",
            ),
            (
                "track",
                read_points3d_binary,
                "track",
                _packed("Q", 1) + point_header + _packed("Q", 1) + b"\0",
            ),
            (
                "sensor",
                read_rigs_binary,
                "sensor",
                _packed("Q", 1) + _packed("IIiI", 1, 2, 0, 1) + b"\0",
            ),
            (
                "data_id",
                read_frames_binary,
                "DATA_ID",
                _packed("Q", 1) + frame_header + _packed("I", 1) + b"\0",
            ),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, reader, context, payload in cases:
                with self.subTest(reader=name):
                    path = _write(root / f"{name}.bin", payload)
                    with self.assertRaisesRegex(ValueError, context):
                        reader(path)

    def test_missing_over_limit_and_invalid_utf8_image_names_are_rejected(self):
        header = _packed(
            "IdddddddI",
            3,
            1.0,
            0.0,
            0.0,
            0.0,
            0.0,
            0.0,
            0.0,
            1,
        )
        cases = (
            ("missing_null", b"name.jpg", "파일이 끝났습니다"),
            ("over_limit", b"a" * (1024 * 1024), "1 MiB"),
            (
                "null_at_limit",
                b"a" * (1024 * 1024) + b"\0" + _packed("Q", 0),
                "1 MiB",
            ),
            ("invalid_utf8", b"\xff\0" + _packed("Q", 0), "UTF-8"),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, suffix, message in cases:
                with self.subTest(name=name):
                    path = _write(
                        root / f"{name}.bin",
                        _packed("Q", 1),
                        header,
                        suffix,
                    )
                    with self.assertRaisesRegex(ValueError, message):
                        read_images_binary(path)

    def test_oversized_counts_are_rejected_before_allocation_or_iteration(self):
        image = _packed("Q", 1) + _image_record(1, b"a.jpg")[:-8] + _packed(
            "Q", (1 << 64) - 1
        )
        point = (
            _packed("Q", 1)
            + _packed("QdddBBBd", 1, 1.0, 2.0, 3.0, 1, 2, 3, 0.1)
            + _packed("Q", (1 << 64) - 1)
        )
        frame = (
            _packed("Q", 1)
            + _frame_record(1)[:-4]
            + _packed("I", (1 << 32) - 1)
        )
        cases = (
            ("images", read_images_binary, image),
            ("points", read_points3d_binary, point),
            ("frames", read_frames_binary, frame),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, reader, payload in cases:
                with self.subTest(reader=name):
                    path = _write(root / f"{name}.bin", payload)
                    with self.assertRaises(ValueError):
                        reader(path)

    def test_trailing_bytes_are_rejected_by_every_reader(self):
        cases = (
            read_cameras_binary,
            read_images_binary,
            read_points3d_binary,
            read_rigs_binary,
            read_frames_binary,
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for index, reader in enumerate(cases):
                with self.subTest(reader=reader.__name__):
                    path = _write(root / f"{index}.bin", _packed("Q", 0), b"x")
                    with self.assertRaisesRegex(ValueError, "해석되지 않은 데이터"):
                        reader(path)

    def test_duplicate_ids_are_rejected_by_every_reader(self):
        cases = (
            (
                "camera",
                read_cameras_binary,
                "CAMERA_ID",
                _packed("Q", 2) + _camera_record(1) + _camera_record(1),
            ),
            (
                "image",
                read_images_binary,
                "IMAGE_ID",
                _packed("Q", 2)
                + _image_record(1, b"a.jpg")
                + _image_record(1, b"b.jpg"),
            ),
            (
                "point",
                read_points3d_binary,
                "POINT3D_ID",
                _packed("Q", 2) + _point_record(1) + _point_record(1),
            ),
            (
                "rig",
                read_rigs_binary,
                "RIG_ID",
                _packed("Q", 2) + _rig_record(1) + _rig_record(1),
            ),
            (
                "frame",
                read_frames_binary,
                "FRAME_ID",
                _packed("Q", 2) + _frame_record(1) + _frame_record(1),
            ),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, reader, context, payload in cases:
                with self.subTest(reader=name):
                    path = _write(root / f"{name}.bin", payload)
                    with self.assertRaisesRegex(ValueError, context):
                        reader(path)

    def test_parser_does_not_recalculate_struct_layouts_on_success_path(self):
        with tempfile.TemporaryDirectory() as directory:
            path = _write(
                Path(directory) / "cameras.bin",
                _packed("Q", 1),
                _camera_record(1),
            )
            with patch.object(
                binary_io.struct,
                "calcsize",
                side_effect=AssertionError("레코드별 struct.calcsize()가 호출되었습니다."),
            ):
                self.assertEqual(read_cameras_binary(path)[1].width, 640)

    def test_file_mapping_is_released_after_success_and_record_creation_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            success = _write(
                root / "success.bin",
                _packed("Q", 1),
                _camera_record(1),
            )
            read_cameras_binary(success)
            moved_success = root / "success_moved.bin"
            success.replace(moved_success)
            moved_success.unlink()

            failing = _write(
                root / "failing.bin",
                _packed("Q", 1),
                _image_record(1, b"a.jpg", (_packed("ddQ", 1.0, 2.0, 3),)),
            )
            with (
                patch.object(
                    binary_io,
                    "Point2DRecord",
                    side_effect=RuntimeError("의도된 생성 실패"),
                ),
                self.assertRaisesRegex(RuntimeError, "의도된 생성 실패"),
            ):
                read_images_binary(failing)
            moved_failing = root / "failing_moved.bin"
            failing.replace(moved_failing)
            moved_failing.unlink()

    def test_every_truncated_offset_is_value_error_and_releases_file(self):
        sensor_with_pose = _packed("iIB", 1, 2, 1) + _packed(
            "ddddddd",
            1.0,
            0.0,
            0.0,
            0.0,
            1.0,
            2.0,
            3.0,
        )
        cases = (
            (
                "camera",
                read_cameras_binary,
                _packed("Q", 1) + _camera_record(1),
            ),
            (
                "image",
                read_images_binary,
                _packed("Q", 1)
                + _image_record(1, b"a.jpg", (_packed("ddQ", 1.0, 2.0, 3),)),
            ),
            (
                "point",
                read_points3d_binary,
                _packed("Q", 1)
                + _point_record(3, (_packed("II", 1, 0),)),
            ),
            (
                "rig",
                read_rigs_binary,
                _packed("Q", 1)
                + _packed("IIiI", 1, 2, 0, 1)
                + sensor_with_pose,
            ),
            (
                "frame",
                read_frames_binary,
                _packed("Q", 1)
                + _frame_record(1, (_packed("iIQ", 1, 2, 3),)),
            ),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, reader, payload in cases:
                valid = _write(root / f"{name}_valid.bin", payload)
                reader(valid)
                valid.unlink()
                for cut in range(len(payload)):
                    with self.subTest(reader=name, cut=cut):
                        path = _write(root / f"{name}.bin", payload[:cut])
                        with self.assertRaises(ValueError):
                            reader(path)
                        moved = root / f"{name}_moved.bin"
                        path.replace(moved)
                        moved.unlink()

    def test_invalid_sensor_pose_flag_releases_mapping(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = _write(
                root / "rigs.bin",
                _packed("Q", 1),
                _packed("IIiI", 1, 2, 0, 1),
                _packed("iIB", 1, 2, 2),
            )
            with self.assertRaisesRegex(ValueError, "HAS_POSE"):
                read_rigs_binary(path)
            moved = root / "rigs_moved.bin"
            path.replace(moved)
            moved.unlink()

    def test_bytes_fallback_and_last_allowed_string_terminator(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            name = b"a" * ((1024 * 1024) - 1)
            path = _write(
                root / "images.bin",
                _packed("Q", 1),
                _image_record(1, name),
            )
            with patch.object(
                binary_io.mmap,
                "mmap",
                side_effect=OSError("의도한 mmap 미지원"),
            ):
                image = read_images_binary(path)[1]
            self.assertEqual(len(image.name), len(name))
            moved = root / "images_moved.bin"
            path.replace(moved)
            moved.unlink()


if __name__ == "__main__":
    unittest.main()
