"""검증된 Camera, Image와 Point3D 의미 데이터의 Fingerprint를 계산합니다."""

from __future__ import annotations

import hashlib
import struct
from collections.abc import Generator

from .io_common import consume_generator
from .model import ColmapDataset

SCHEMA_TAG = "PP_COLMAP_RECONSTRUCTION"
SCHEMA_VERSION = 2


def _write_tag(digest: object, value: str) -> None:
    """SHA-256에 길이 포함 UTF-8 Type Tag를 기록합니다."""
    encoded = value.encode("utf-8", errors="strict")
    digest.update(struct.pack("<Q", len(encoded)))
    digest.update(encoded)


def _write_u32(digest: object, value: int) -> None:
    """SHA-256에 부호 없는 32-bit 정수를 기록합니다."""
    digest.update(struct.pack("<I", value))


def _write_u64(digest: object, value: int) -> None:
    """SHA-256에 부호 없는 64-bit 정수를 기록합니다."""
    digest.update(struct.pack("<Q", value))


def _write_i32(digest: object, value: int) -> None:
    """SHA-256에 부호 있는 32-bit 정수를 기록합니다."""
    digest.update(struct.pack("<i", value))


def _write_f64(digest: object, value: float) -> None:
    """SHA-256에 IEEE-754 64-bit 실수를 기록합니다."""
    digest.update(struct.pack("<d", value))


def semantic_fingerprint_steps(
    dataset: ColmapDataset,
) -> Generator[tuple[int, int], None, str]:
    """의미 Fingerprint를 레코드 단위로 계산해 처리량을 전달합니다."""
    digest = hashlib.sha256()
    _write_tag(digest, SCHEMA_TAG)
    _write_u32(digest, SCHEMA_VERSION)
    _write_tag(digest, dataset.layout.input_format)

    total = len(dataset.cameras) + len(dataset.images) + len(dataset.points3d)
    completed = 0
    yield completed, total
    _write_u64(digest, len(dataset.cameras))
    for camera_id in sorted(dataset.cameras):
        camera = dataset.cameras[camera_id]
        _write_tag(digest, "CAMERA")
        _write_u32(digest, camera.camera_id)
        _write_i32(digest, camera.model_id)
        _write_tag(digest, camera.model_name)
        _write_u64(digest, camera.width)
        _write_u64(digest, camera.height)
        _write_u32(digest, len(camera.params))
        for parameter in camera.params:
            _write_f64(digest, parameter)
        completed += 1
        if completed % 256 == 0:
            yield completed, total

    _write_u64(digest, len(dataset.images))
    for image_id in sorted(dataset.images):
        image = dataset.images[image_id]
        _write_tag(digest, "IMAGE")
        _write_u32(digest, image.image_id)
        for value in image.qvec:
            _write_f64(digest, value)
        for value in image.tvec:
            _write_f64(digest, value)
        _write_u32(digest, image.camera_id)
        _write_tag(digest, image.image_name)
        completed += 1
        if completed % 256 == 0:
            yield completed, total

    _write_u64(digest, len(dataset.points3d))
    for point3d_id in sorted(dataset.points3d):
        point = dataset.points3d[point3d_id]
        _write_tag(digest, "POINT3D")
        _write_u64(digest, point.point3d_id)
        for value in point.xyz:
            _write_f64(digest, value)
        for component in point.rgb:
            digest.update(struct.pack("<B", component))
        _write_f64(digest, point.error)
        completed += 1
        if completed % 256 == 0:
            yield completed, total
    return digest.hexdigest()


def semantic_fingerprint(dataset: ColmapDataset) -> str:
    """증분 의미 Fingerprint 계산을 동기 완료해 SHA-256을 반환합니다."""

    return consume_generator(semantic_fingerprint_steps(dataset))
