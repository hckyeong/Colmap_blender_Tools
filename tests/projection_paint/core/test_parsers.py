"""Camera, Image와 Point3D TXT/BIN Parser의 정상·손상 입력을 검증합니다."""

from __future__ import annotations

import math
import struct
import tempfile
import unittest
from pathlib import Path

from tools.projection_paint.core.colmap.binary_io import (
    parse_cameras_binary,
    parse_images_binary,
    parse_points3d_binary,
)
from tools.projection_paint.core.colmap.camera_import_job import load_camera_image_dataset
from tools.projection_paint.core.colmap.errors import ColmapError, ImportCancelled
from tools.projection_paint.core.colmap.layout import resolve_layout
from tools.projection_paint.core.colmap.model import CameraRecord, ImagePoseRecord, ParserLimits
from tools.projection_paint.core.colmap.text_io import (
    parse_cameras_text,
    parse_images_text,
    parse_points3d_text,
)

from support import (
    sample_cameras,
    sample_images,
    sample_points3d,
    write_cameras_binary,
    write_cameras_text,
    write_images_binary,
    write_images_text,
    write_layout,
    write_points3d_binary,
    write_points3d_text,
)


class ParserTests(unittest.TestCase):
    """Reconstruction Parser의 형식 동등성과 Fail-closed 동작을 검증합니다."""

    def test_text_binary_records_are_equal(self) -> None:
        """같은 의미의 TXT와 BIN이 같은 Reconstruction Record를 만듭니다."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_layout(root / "bin", "BINARY")
            write_layout(root / "txt", "TEXT")
            binary = load_camera_image_dataset(resolve_layout(root / "bin"))
            text = load_camera_image_dataset(resolve_layout(root / "txt"))
            self.assertEqual(dict(binary.cameras), dict(text.cameras))
            self.assertEqual(dict(binary.images), dict(text.images))
            self.assertEqual(dict(binary.points3d), dict(text.points3d))
            self.assertFalse(hasattr(next(iter(binary.images.values())), "points2d"))

    def test_point3d_parsers_preserve_track_and_uint64_id(self) -> None:
        """TXT와 BIN Point3D가 Track과 64비트 ID를 손실 없이 보존합니다."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            binary_path = root / "points3D.bin"
            text_path = root / "points3D.txt"
            write_points3d_binary(binary_path, sample_points3d())
            write_points3d_text(text_path, sample_points3d())
            binary, _binary_identity = parse_points3d_binary(binary_path)
            text, _text_identity = parse_points3d_text(text_path)
            self.assertEqual(binary, text)
            self.assertIn((1 << 40) + 9, binary)

    def test_binary_rejects_truncated_point3d_track(self) -> None:
        """Point3D Track Payload가 잘리면 범위 검사에서 거부합니다."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "points3D.bin"
            write_points3d_binary(path, sample_points3d())
            path.write_bytes(path.read_bytes()[:-1])
            with self.assertRaisesRegex(ColmapError, "범위|끝났습니다"):
                parse_points3d_binary(path)

    def test_binary_skips_large_point_payload(self) -> None:
        """Binary Point2D 수가 늘어도 Image Pose 결과 크기는 변하지 않습니다."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "images.bin"
            write_images_binary(path, sample_images(), point_count=10_000)
            records, _identity = parse_images_binary(path)
            self.assertEqual(tuple(sorted(records)), (2, 10, 105))
            self.assertFalse(hasattr(records[2], "points2d"))

    def test_binary_rejects_truncated_point_payload(self) -> None:
        """선언한 Point2D Payload보다 파일이 짧으면 거부합니다."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "images.bin"
            write_images_binary(path, sample_images()[:1], point_count=2)
            data = path.read_bytes()
            path.write_bytes(data[:-1])
            with self.assertRaisesRegex(ColmapError, "범위"):
                parse_images_binary(path)

    def test_binary_rejects_huge_point_count(self) -> None:
        """Point2D Count가 uint64 최대값이면 Offset 이동 전에 거부합니다."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "images.bin"
            image = sample_images()[0]
            with path.open("wb") as stream:
                stream.write(struct.pack("<Q", 1))
                stream.write(
                    struct.pack(
                        "<IdddddddI",
                        image.image_id,
                        *image.qvec,
                        *image.tvec,
                        image.camera_id,
                    )
                )
                stream.write(image.image_name.encode("utf-8") + b"\0")
                stream.write(struct.pack("<Q", (1 << 64) - 1))
            with self.assertRaisesRegex(ColmapError, "범위"):
                parse_images_binary(path)

    def test_binary_rejects_trailing_byte(self) -> None:
        """마지막 Record 뒤 예상하지 않은 Byte가 있으면 거부합니다."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cameras.bin"
            write_cameras_binary(path, sample_cameras())
            with path.open("ab") as stream:
                stream.write(b"\x00")
            with self.assertRaisesRegex(ColmapError, "잔여"):
                parse_cameras_binary(path)

    def test_binary_rejects_unterminated_name(self) -> None:
        """Binary Image 이름이 NUL 종료되지 않으면 길이 상한에서 거부합니다."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "images.bin"
            image = sample_images()[0]
            with path.open("wb") as stream:
                stream.write(struct.pack("<Q", 1))
                stream.write(
                    struct.pack(
                        "<IdddddddI",
                        image.image_id,
                        *image.qvec,
                        *image.tvec,
                        image.camera_id,
                    )
                )
                stream.write(b"A" * 32)
            limits = ParserLimits(max_image_name_bytes=16)
            with self.assertRaisesRegex(ColmapError, "NUL"):
                parse_images_binary(path, limits=limits)

    def test_binary_rejects_invalid_utf8_name(self) -> None:
        """Binary Image 이름의 잘못된 UTF-8을 거부합니다."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "images.bin"
            image = sample_images()[0]
            with path.open("wb") as stream:
                stream.write(struct.pack("<Q", 1))
                stream.write(
                    struct.pack(
                        "<IdddddddI",
                        image.image_id,
                        *image.qvec,
                        *image.tvec,
                        image.camera_id,
                    )
                )
                stream.write(b"\xff\0")
                stream.write(struct.pack("<Q", 0))
            with self.assertRaisesRegex(ColmapError, "UTF-8"):
                parse_images_binary(path)

    def test_duplicate_ids_are_rejected(self) -> None:
        """TXT와 BIN 모두 중복 CAMERA_ID와 IMAGE_ID를 거부합니다."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            camera_path = root / "cameras.txt"
            image_path = root / "images.bin"
            camera = sample_cameras()[0]
            write_cameras_text(camera_path, (camera, camera))
            image = sample_images()[0]
            write_images_binary(image_path, (image, image), point_count=0)
            with self.assertRaisesRegex(ColmapError, "중복 CAMERA_ID"):
                parse_cameras_text(camera_path)
            with self.assertRaisesRegex(ColmapError, "중복 IMAGE_ID"):
                parse_images_binary(image_path)

    def test_normalized_duplicate_image_names_are_rejected(self) -> None:
        """대소문자 정규화 뒤 충돌하는 이미지 이름을 거부합니다."""
        with tempfile.TemporaryDirectory() as directory:
            sparse = Path(directory)
            images = (
                ImagePoseRecord(1, 3, (1.0, 0.0, 0.0, 0.0), (0.0, 0.0, 0.0), "A.JPG"),
                ImagePoseRecord(2, 3, (1.0, 0.0, 0.0, 0.0), (0.0, 0.0, 0.0), "a.jpg"),
            )
            write_cameras_binary(sparse / "cameras.bin", sample_cameras())
            write_images_binary(sparse / "images.bin", images, point_count=0)
            write_points3d_binary(sparse / "points3D.bin", sample_points3d())
            with self.assertRaisesRegex(ColmapError, "이름이 충돌"):
                load_camera_image_dataset(resolve_layout(sparse))

    def test_text_point_line_is_not_numeric_converted(self) -> None:
        """Point2D 행은 Token 수만 맞으면 숫자 변환 없이 폐기됩니다."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "images.txt"
            write_images_text(path, sample_images()[:1], point_line="not numeric values")
            records, _identity = parse_images_text(path)
            self.assertEqual(tuple(records), (10,))

    def test_text_rejects_missing_or_malformed_point_line(self) -> None:
        """Image Header 뒤 Point2D 행 누락과 잘못된 Token 수를 거부합니다."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            missing = root / "missing.txt"
            image = sample_images()[0]
            pose = " ".join(str(value) for value in (*image.qvec, *image.tvec))
            missing.write_text(
                f"{image.image_id} {pose} {image.camera_id} {image.image_name}\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ColmapError, "누락"):
                parse_images_text(missing)
            malformed = root / "malformed.txt"
            write_images_text(malformed, (image,), point_line="1 2 3 4")
            with self.assertRaisesRegex(ColmapError, "3의 배수"):
                parse_images_text(malformed)

    def test_text_point_line_limit_and_cancel(self) -> None:
        """과대 Text Point2D 행과 Chunk 중간 취소를 각각 처리합니다."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "images.txt"
            write_images_text(path, sample_images()[:1], point_line="1 2 3 " * 100)
            limits = ParserLimits(
                max_text_point_line_bytes=32,
                text_chunk_bytes=8,
            )
            with self.assertRaisesRegex(ColmapError, "안전 상한"):
                parse_images_text(path, limits=limits)

            calls = 0

            def cancel_check() -> bool:
                """세 번째 취소 확인에서 중단을 요청합니다."""
                nonlocal calls
                calls += 1
                return calls >= 3

            with self.assertRaises(ImportCancelled):
                parse_images_text(path, cancel_check=cancel_check)

    def test_semantic_validation_rejects_invalid_values(self) -> None:
        """누락 참조, NaN·Inf, 0 Quaternion과 비지원 Model을 거부합니다."""
        cases = (
            (
                sample_cameras(),
                (ImagePoseRecord(1, 999, (1.0, 0.0, 0.0, 0.0), (0.0, 0.0, 0.0), "a.jpg"),),
                "존재하지 않는",
            ),
            (
                sample_cameras(),
                (ImagePoseRecord(1, 3, (0.0, 0.0, 0.0, 0.0), (0.0, 0.0, 0.0), "a.jpg"),),
                "Quaternion",
            ),
            (
                (CameraRecord(3, 1, "PINHOLE", 10, 10, (math.inf, 1.0, 1.0, 1.0)),),
                (ImagePoseRecord(1, 3, (1.0, 0.0, 0.0, 0.0), (0.0, 0.0, 0.0), "a.jpg"),),
                "NaN 또는 Inf",
            ),
            (
                (CameraRecord(3, 2, "SIMPLE_RADIAL", 10, 10, (1.0, 1.0, 1.0, 0.0)),),
                (ImagePoseRecord(1, 3, (1.0, 0.0, 0.0, 0.0), (0.0, 0.0, 0.0), "a.jpg"),),
                "지원하지 않는",
            ),
        )
        for cameras, images, message in cases:
            with self.subTest(message=message), tempfile.TemporaryDirectory() as directory:
                sparse = Path(directory)
                write_cameras_binary(sparse / "cameras.bin", cameras)
                write_images_binary(sparse / "images.bin", images, point_count=0)
                write_points3d_binary(sparse / "points3D.bin", sample_points3d())
                with self.assertRaisesRegex(ColmapError, message):
                    load_camera_image_dataset(resolve_layout(sparse))


if __name__ == "__main__":
    unittest.main()
