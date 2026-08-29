"""Core unittest에서 사용할 최소 COLMAP Fixture 작성 도구를 제공합니다."""

from __future__ import annotations

import struct
from pathlib import Path
from typing import Iterable

from tools.projection_paint.core.colmap.model import (
    CameraRecord,
    ImagePoseRecord,
    Point3DRecord,
    TrackElement,
)

_CAMERA_MODEL_ID = {
    "SIMPLE_PINHOLE": 0,
    "PINHOLE": 1,
    "SIMPLE_RADIAL": 2,
}


def sample_cameras() -> tuple[CameraRecord, ...]:
    """공유 Intrinsics를 포함한 기본 Camera Fixture를 반환합니다."""
    return (
        CameraRecord(3, 1, "PINHOLE", 1920, 1080, (1000.0, 1001.0, 960.0, 540.0)),
        CameraRecord(8, 0, "SIMPLE_PINHOLE", 2048, 2048, (1200.0, 1024.0, 1024.0)),
    )


def sample_images() -> tuple[ImagePoseRecord, ...]:
    """불연속 IMAGE_ID와 공유 CAMERA_ID를 포함한 기본 Image Fixture를 반환합니다."""
    return (
        ImagePoseRecord(10, 3, (2.0, 0.0, 0.0, 0.0), (1.0, 2.0, 3.0), "nested/가 사진.jpg"),
        ImagePoseRecord(2, 3, (1.0, 0.0, 0.0, 0.0), (0.0, 0.0, 0.0), "image 2.png"),
        ImagePoseRecord(105, 8, (1.0, 0.0, 0.0, 0.0), (-1.0, 0.5, 4.0), "image105.exr"),
    )


def sample_points3d() -> tuple[Point3DRecord, ...]:
    """색상과 64비트 ID를 포함한 기본 Point3D Fixture를 반환합니다."""
    return (
        Point3DRecord(
            7,
            (1.0, 2.0, 3.0),
            (255, 64, 0),
            0.25,
            (TrackElement(2, 0),),
        ),
        Point3DRecord(
            (1 << 40) + 9,
            (-4.0, 5.5, 6.0),
            (12, 34, 56),
            1.5,
            (TrackElement(10, 3), TrackElement(105, 1)),
        ),
    )


def write_cameras_binary(path: Path, cameras: Iterable[CameraRecord]) -> None:
    """Camera Record를 공식 COLMAP little-endian Binary로 기록합니다."""
    items = tuple(cameras)
    with path.open("wb") as stream:
        stream.write(struct.pack("<Q", len(items)))
        for camera in items:
            stream.write(
                struct.pack(
                    "<IiQQ",
                    camera.camera_id,
                    camera.model_id,
                    camera.width,
                    camera.height,
                )
            )
            stream.write(struct.pack("<" + "d" * len(camera.params), *camera.params))


def write_images_binary(
    path: Path,
    images: Iterable[ImagePoseRecord],
    *,
    point_count: int = 2,
) -> None:
    """Image Pose와 더미 Point2D를 공식 COLMAP Binary로 기록합니다."""
    items = tuple(images)
    with path.open("wb") as stream:
        stream.write(struct.pack("<Q", len(items)))
        for image in items:
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
            stream.write(struct.pack("<Q", point_count))
            for index in range(point_count):
                stream.write(struct.pack("<ddQ", float(index), float(index + 1), (1 << 64) - 1))


def write_points3d_binary(
    path: Path,
    points: Iterable[Point3DRecord],
) -> None:
    """Point3D Record를 공식 COLMAP little-endian Binary로 기록합니다."""
    items = tuple(points)
    with path.open("wb") as stream:
        stream.write(struct.pack("<Q", len(items)))
        for point in items:
            stream.write(
                struct.pack(
                    "<QdddBBBd",
                    point.point3d_id,
                    *point.xyz,
                    *point.rgb,
                    point.error,
                )
            )
            stream.write(struct.pack("<Q", len(point.track)))
            for element in point.track:
                stream.write(
                    struct.pack(
                        "<II",
                        element.image_id,
                        element.point2d_index,
                    )
                )


def write_cameras_text(path: Path, cameras: Iterable[CameraRecord]) -> None:
    """Camera Record를 공식 COLMAP Text 형식으로 기록합니다."""
    with path.open("w", encoding="utf-8", newline="\n") as stream:
        stream.write("# Camera list\n")
        for camera in cameras:
            params = " ".join(format(value, ".17g") for value in camera.params)
            stream.write(
                f"{camera.camera_id} {camera.model_name} {camera.width} "
                f"{camera.height} {params}\n"
            )


def write_images_text(
    path: Path,
    images: Iterable[ImagePoseRecord],
    *,
    point_line: str = "1.0 2.0 -1 3.0 4.0 -1",
) -> None:
    """Image Pose와 Point2D 물리 행을 공식 COLMAP Text 형식으로 기록합니다."""
    with path.open("w", encoding="utf-8", newline="\n") as stream:
        stream.write("# Image list\n")
        for image in images:
            pose = " ".join(
                format(value, ".17g") for value in (*image.qvec, *image.tvec)
            )
            stream.write(
                f"{image.image_id} {pose} {image.camera_id} {image.image_name}\n"
            )
            stream.write(point_line + "\n")


def write_points3d_text(
    path: Path,
    points: Iterable[Point3DRecord],
) -> None:
    """Point3D Record를 공식 COLMAP Text 형식으로 기록합니다."""
    with path.open("w", encoding="utf-8", newline="\n") as stream:
        stream.write("# Point3D list\n")
        for point in points:
            values = (
                point.point3d_id,
                *point.xyz,
                *point.rgb,
                point.error,
                *(
                    value
                    for element in point.track
                    for value in (element.image_id, element.point2d_index)
                ),
            )
            stream.write(" ".join(str(value) for value in values) + "\n")


def write_layout(
    sparse: Path,
    input_format: str,
    cameras: Iterable[CameraRecord] | None = None,
    images: Iterable[ImagePoseRecord] | None = None,
    points3d: Iterable[Point3DRecord] | None = None,
) -> None:
    """Sparse Directory에 Camera, Image와 Point3D 세트를 기록합니다."""
    sparse.mkdir(parents=True, exist_ok=True)
    camera_items = tuple(cameras or sample_cameras())
    image_items = tuple(images or sample_images())
    point_items = tuple(points3d or sample_points3d())
    if input_format.upper() == "BINARY":
        write_cameras_binary(sparse / "cameras.bin", camera_items)
        write_images_binary(sparse / "images.bin", image_items)
        write_points3d_binary(sparse / "points3D.bin", point_items)
        return
    if input_format.upper() == "TEXT":
        write_cameras_text(sparse / "cameras.txt", camera_items)
        write_images_text(sparse / "images.txt", image_items)
        write_points3d_text(sparse / "points3D.txt", point_items)
        return
    raise ValueError(f"지원하지 않는 Fixture 형식입니다: {input_format}")


def write_image_files(images_root: Path, images: Iterable[ImagePoseRecord]) -> None:
    """Image Pose 이름에 대응하는 최소 더미 이미지 파일을 만듭니다."""
    for image in images:
        path = images_root.joinpath(*image.image_name.replace("\\", "/").split("/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(f"IMAGE:{image.image_id}".encode("utf-8"))
