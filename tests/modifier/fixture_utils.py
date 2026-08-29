"""외부 도구 없이 결정적인 COLMAP TXT/BIN fixture를 생성합니다."""

from __future__ import annotations

import struct
from pathlib import Path


def _write(path: Path, format_string: str, *values) -> None:
    with path.open("ab") as stream:
        stream.write(struct.pack("<" + format_string, *values))


def make_legacy_text(root: Path, tvec=(1.0, 2.0, 3.0)) -> tuple[Path, Path]:
    model = root / "sparse" / "0"
    images = root / "images"
    model.mkdir(parents=True)
    images.mkdir()
    (images / "frame 01.jpg").write_bytes(b"fixture")
    (model / "cameras.txt").write_text(
        "# cameras\n1 PINHOLE 1000 800 900 880 510 390\n", encoding="utf-8"
    )
    (model / "images.txt").write_text(
        f"# images\n7 1 0 0 0 {tvec[0]} {tvec[1]} {tvec[2]} 1 frame 01.jpg\n"
        "10 20 4294967301\n",
        encoding="utf-8",
    )
    (model / "points3D.txt").write_text(
        "# points\n4294967301 1 2 3 10 20 30 0.25 7 0\n", encoding="utf-8"
    )
    return model, images


def make_legacy_text_multi_point(root: Path) -> tuple[Path, Path]:
    """부분 선택 회귀 검증을 위한 3개 Point3D legacy TXT fixture를 만듭니다."""
    model = root / "sparse" / "0"
    images = root / "images"
    model.mkdir(parents=True)
    images.mkdir()
    (images / "frame 01.jpg").write_bytes(b"fixture")
    (model / "cameras.txt").write_text(
        "# cameras\n1 PINHOLE 1000 800 900 880 510 390\n", encoding="utf-8"
    )
    (model / "images.txt").write_text(
        "# images\n7 1 0 0 0 1 2 3 1 frame 01.jpg\n"
        "10 20 4294967301 30 40 4294967302 50 60 4294967303\n",
        encoding="utf-8",
    )
    (model / "points3D.txt").write_text(
        "# points\n"
        "4294967301 1 2 3 10 20 30 0.25 7 0\n"
        "4294967302 4 5 6 40 50 60 0.50 7 1\n"
        "4294967303 7 8 9 70 80 90 0.75 7 2\n",
        encoding="utf-8",
    )
    return model, images


def make_legacy_binary(root: Path) -> tuple[Path, Path]:
    model = root / "sparse" / "0"
    images = root / "images"
    model.mkdir(parents=True)
    images.mkdir()
    (images / "frame.jpg").write_bytes(b"fixture")

    cameras = model / "cameras.bin"
    cameras.write_bytes(b"")
    _write(cameras, "Q", 1)
    _write(cameras, "IiQQ", 1, 0, 640, 480)
    _write(cameras, "ddd", 500.0, 320.0, 240.0)

    image_file = model / "images.bin"
    image_file.write_bytes(b"")
    _write(image_file, "Q", 1)
    _write(image_file, "IdddddddI", 3, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1)
    with image_file.open("ab") as stream:
        stream.write(b"frame.jpg\0")
    _write(image_file, "Q", 2)
    _write(image_file, "ddQ", 1.0, 2.0, 99)
    _write(image_file, "ddQ", 3.0, 4.0, 0xFFFFFFFFFFFFFFFF)

    points = model / "points3D.bin"
    points.write_bytes(b"")
    _write(points, "Q", 1)
    _write(points, "QdddBBBd", 99, 5.0, 6.0, 7.0, 1, 2, 3, 0.5)
    _write(points, "Q", 1)
    _write(points, "II", 3, 0)
    return model, images


def make_modern_text(root: Path) -> tuple[Path, Path]:
    model, images = make_legacy_text(root, tvec=(0.0, 0.0, 0.0))
    (model / "rigs.txt").write_text(
        "# rigs\n"
        "1 2 CAMERA 1 IMU 7 1 1 0 0 0 0.1 0.2 0.3\n",
        encoding="utf-8",
    )
    (model / "frames.txt").write_text(
        "# frames\n"
        "1 1 1 0 0 0 0 0 0 2 CAMERA 1 7 IMU 7 4294967297\n",
        encoding="utf-8",
    )
    return model, images


def make_modern_binary(root: Path) -> tuple[Path, Path]:
    model, images = make_legacy_binary(root)
    rigs = model / "rigs.bin"
    rigs.write_bytes(b"")
    _write(rigs, "Q", 1)
    _write(rigs, "IIiI", 1, 2, 0, 1)
    _write(rigs, "iIB", 1, 7, 1)
    _write(rigs, "ddddddd", 1.0, 0.0, 0.0, 0.0, 0.1, 0.2, 0.3)

    frames = model / "frames.bin"
    frames.write_bytes(b"")
    _write(frames, "Q", 1)
    _write(frames, "IIddddddd", 1, 1, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
    _write(frames, "I", 2)
    _write(frames, "iIQ", 0, 1, 3)
    _write(frames, "iIQ", 1, 7, 4294967297)
    return model, images
