"""Blender ID와 Runtime 참조를 포함하지 않는 불변 COLMAP 전송 모델입니다."""

from __future__ import annotations

from dataclasses import dataclass, fields, is_dataclass
import hashlib
import json
import math
from pathlib import Path
from types import MappingProxyType
from typing import Mapping


TRANSFER_SCHEMA_VERSION = 1
_CAMERA_MODELS = {"SIMPLE_PINHOLE", "PINHOLE"}


def _finite_tuple(values, length: int, label: str) -> tuple[float, ...]:
    """입력 수열을 지정 길이의 유한한 실수 Tuple로 고정합니다."""

    result = tuple(float(value) for value in values)
    if len(result) != length or not all(math.isfinite(value) for value in result):
        raise ValueError(f"{label} 값이 올바르지 않습니다.")
    return result


def _absolute_path(value: str, label: str) -> str:
    """전송 경로를 정규화된 절대경로 문자열로 검증합니다."""

    path = Path(str(value)).expanduser()
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError(f"{label} 경로가 정규화된 절대경로가 아닙니다.")
    return str(path)


@dataclass(frozen=True, slots=True)
class CameraValue:
    """Camera intrinsics를 순수 값으로 고정합니다."""

    camera_id: int
    model_id: int
    model_name: str
    width: int
    height: int
    params: tuple[float, ...]

    def __post_init__(self) -> None:
        """지원 Camera 모델과 intrinsics 차원을 검증합니다."""

        if self.camera_id <= 0 or self.width <= 0 or self.height <= 0:
            raise ValueError("Camera ID 또는 해상도가 올바르지 않습니다.")
        expected = 3 if self.model_name == "SIMPLE_PINHOLE" else 4
        if self.model_name not in _CAMERA_MODELS or len(self.params) != expected:
            raise ValueError(f"지원하지 않는 Camera 모델입니다: {self.model_name}")
        object.__setattr__(self, "params", _finite_tuple(self.params, expected, "Camera params"))


@dataclass(frozen=True, slots=True)
class ImageValue:
    """Image pose와 원본 이미지 경로를 순수 값으로 고정합니다."""

    image_id: int
    camera_id: int
    name: str
    qvec: tuple[float, float, float, float]
    tvec: tuple[float, float, float]
    image_path: str

    def __post_init__(self) -> None:
        """Image 식별자·pose·절대경로를 검증합니다."""

        if self.image_id <= 0 or self.camera_id <= 0 or not self.name:
            raise ValueError("Image 식별자가 올바르지 않습니다.")
        object.__setattr__(self, "qvec", _finite_tuple(self.qvec, 4, "Image qvec"))
        object.__setattr__(self, "tvec", _finite_tuple(self.tvec, 3, "Image tvec"))
        object.__setattr__(self, "image_path", _absolute_path(self.image_path, "Image"))


@dataclass(frozen=True, slots=True)
class Point3DValue:
    """Point3D 좌표와 표시 속성을 순수 값으로 고정합니다."""

    point3d_id: int
    xyz: tuple[float, float, float]
    rgb: tuple[int, int, int]
    error: float

    def __post_init__(self) -> None:
        """Point3D 식별자·좌표·색상·오차를 검증합니다."""

        if self.point3d_id <= 0:
            raise ValueError("POINT3D_ID가 올바르지 않습니다.")
        object.__setattr__(self, "xyz", _finite_tuple(self.xyz, 3, "Point3D xyz"))
        rgb = tuple(int(value) for value in self.rgb)
        if len(rgb) != 3 or any(value < 0 or value > 255 for value in rgb):
            raise ValueError("Point3D RGB가 올바르지 않습니다.")
        if not math.isfinite(float(self.error)) or float(self.error) < 0.0:
            raise ValueError("Point3D error가 올바르지 않습니다.")
        object.__setattr__(self, "rgb", rgb)
        object.__setattr__(self, "error", float(self.error))


@dataclass(frozen=True, slots=True)
class TransferDisplayDefaults:
    """최초 Painting Snapshot에만 적용할 Modifier 표시 기본값입니다."""

    axis_preset: str
    axis_forward: str
    axis_up: str
    point_size: float
    camera_frustum_size: float

    def __post_init__(self) -> None:
        """표시 기본값의 축과 양수 크기를 검증합니다."""

        if not self.axis_preset or not self.axis_forward or not self.axis_up:
            raise ValueError("표시축 기본값이 비어 있습니다.")
        if self.axis_forward.lstrip("-") == self.axis_up.lstrip("-"):
            raise ValueError("Forward와 Up 축은 같을 수 없습니다.")
        if not math.isfinite(self.point_size) or self.point_size <= 0.0:
            raise ValueError("Point Scale은 0보다 커야 합니다.")
        if not math.isfinite(self.camera_frustum_size) or self.camera_frustum_size < 0.0:
            raise ValueError("Camera Frustum Size가 올바르지 않습니다.")


@dataclass(frozen=True, slots=True)
class ColmapTransferSnapshot:
    """Modifier 확정 상태를 한 시점에 고정한 값 전용 Snapshot입니다."""

    schema_version: int
    source_dataset_uuid: str
    source_revision: int
    source_fingerprint: str
    content_fingerprint: str
    model_directory: str
    images_directory: str
    cameras: tuple[CameraValue, ...]
    images: tuple[ImageValue, ...]
    points3d: tuple[Point3DValue, ...]
    display_defaults: TransferDisplayDefaults

    def __post_init__(self) -> None:
        """Snapshot 식별자·경로·중복·참조 무결성을 검증합니다."""

        if self.schema_version != TRANSFER_SCHEMA_VERSION:
            raise ValueError("지원하지 않는 COLMAP 전송 Schema입니다.")
        if not self.source_dataset_uuid or self.source_revision < 0:
            raise ValueError("Modifier Dataset 식별자 또는 Revision이 올바르지 않습니다.")
        if not self.source_fingerprint or not self.content_fingerprint:
            raise ValueError("Snapshot fingerprint가 비어 있습니다.")
        object.__setattr__(self, "model_directory", _absolute_path(self.model_directory, "Model"))
        object.__setattr__(self, "images_directory", _absolute_path(self.images_directory, "Images"))
        cameras = tuple(self.cameras)
        images = tuple(self.images)
        points = tuple(self.points3d)
        camera_ids = {item.camera_id for item in cameras}
        if len(camera_ids) != len(cameras) or not cameras:
            raise ValueError("Camera가 없거나 CAMERA_ID가 중복되었습니다.")
        image_ids = {item.image_id for item in images}
        if len(image_ids) != len(images) or not images:
            raise ValueError("Image가 없거나 IMAGE_ID가 중복되었습니다.")
        if any(item.camera_id not in camera_ids for item in images):
            raise ValueError("Image가 존재하지 않는 CAMERA_ID를 참조합니다.")
        point_ids = {item.point3d_id for item in points}
        if len(point_ids) != len(points):
            raise ValueError("POINT3D_ID가 중복되었습니다.")
        object.__setattr__(self, "cameras", cameras)
        object.__setattr__(self, "images", images)
        object.__setattr__(self, "points3d", points)

    @property
    def image_paths(self) -> Mapping[int, str]:
        """IMAGE_ID별 절대경로의 읽기 전용 Mapping을 반환합니다."""

        return MappingProxyType({item.image_id: item.image_path for item in self.images})


class SnapshotContentFingerprintBuilder:
    """대용량 전송값의 기존 JSON SHA-256을 레코드 단위로 계산합니다."""

    def __init__(
        self,
        *,
        source_dataset_uuid: str,
        source_revision: int,
        source_fingerprint: str,
    ) -> None:
        """기존 직렬화 형식과 동일한 JSON 머리말로 증분 지문을 시작합니다."""

        self._hasher = hashlib.sha256()
        self._phase = "CAMERAS"
        self._first_record = True
        prefix = (
            '{"source_dataset_uuid":'
            f"{self._json(source_dataset_uuid)},"
            '"source_revision":'
            f"{self._json(int(source_revision))},"
            '"source_fingerprint":'
            f"{self._json(source_fingerprint)},"
            '"cameras":['
        )
        self._write(prefix)

    @staticmethod
    def _json(value: object) -> str:
        """기존 Snapshot 지문과 같은 JSON 옵션으로 값을 직렬화합니다."""

        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))

    def _write(self, value: str) -> None:
        """UTF-8 문자열 조각을 현재 SHA-256 상태에 추가합니다."""

        self._hasher.update(value.encode("utf-8"))

    def _append(self, item: object, expected_phase: str) -> None:
        """현재 종류의 dataclass 레코드 한 건을 기존 Tuple JSON 형식으로 추가합니다."""

        if self._phase != expected_phase:
            raise RuntimeError("Snapshot 지문 레코드 순서가 올바르지 않습니다.")
        if not self._first_record:
            self._write(",")
        payload = tuple(getattr(item, field.name) for field in fields(item))
        self._write(self._json(payload))
        self._first_record = False

    def add_camera(self, item: CameraValue) -> None:
        """Camera 전송값 한 건을 지문에 추가합니다."""

        self._append(item, "CAMERAS")

    def finish_cameras(self) -> None:
        """Camera 배열을 닫고 Image 배열 기록을 시작합니다."""

        if self._phase != "CAMERAS":
            raise RuntimeError("Snapshot Camera 지문 단계를 중복 종료했습니다.")
        self._write('],"images":[')
        self._phase = "IMAGES"
        self._first_record = True

    def add_image(self, item: ImageValue) -> None:
        """Image 전송값 한 건을 지문에 추가합니다."""

        self._append(item, "IMAGES")

    def finish_images(self) -> None:
        """Image 배열을 닫고 Point3D 배열 기록을 시작합니다."""

        if self._phase != "IMAGES":
            raise RuntimeError("Snapshot Image 지문 단계를 중복 종료했습니다.")
        self._write('],"points3d":[')
        self._phase = "POINTS"
        self._first_record = True

    def add_point3d(self, item: Point3DValue) -> None:
        """Point3D 전송값 한 건을 지문에 추가합니다."""

        self._append(item, "POINTS")

    def finish(self) -> str:
        """남은 빈 배열까지 닫고 완성된 SHA-256 문자열을 반환합니다."""

        if self._phase == "DONE":
            raise RuntimeError("완료된 Snapshot 지문을 다시 종료할 수 없습니다.")
        if self._phase == "CAMERAS":
            self.finish_cameras()
        if self._phase == "IMAGES":
            self.finish_images()
        if self._phase != "POINTS":
            raise RuntimeError("Snapshot 지문 완료 단계가 올바르지 않습니다.")
        self._write("]}")
        self._phase = "DONE"
        return self._hasher.hexdigest()


def snapshot_content_fingerprint(
    *,
    source_dataset_uuid: str,
    source_revision: int,
    source_fingerprint: str,
    cameras: tuple[CameraValue, ...],
    images: tuple[ImageValue, ...],
    points3d: tuple[Point3DValue, ...],
) -> str:
    """전송 값의 기존 JSON 형식을 증분 기록해 재현 가능한 SHA-256을 계산합니다."""

    builder = SnapshotContentFingerprintBuilder(
        source_dataset_uuid=source_dataset_uuid,
        source_revision=source_revision,
        source_fingerprint=source_fingerprint,
    )
    for item in cameras:
        builder.add_camera(item)
    builder.finish_cameras()
    for item in images:
        builder.add_image(item)
    builder.finish_images()
    for item in points3d:
        builder.add_point3d(item)
    return builder.finish()


def validate_snapshot(snapshot: ColmapTransferSnapshot) -> ColmapTransferSnapshot:
    """전송 Snapshot의 타입·금지 Runtime 값·내용 지문을 다시 검증합니다."""

    if not isinstance(snapshot, ColmapTransferSnapshot):
        raise TypeError("COLMAP 전송 Snapshot 타입이 올바르지 않습니다.")
    for value in (snapshot, *snapshot.cameras, *snapshot.images, *snapshot.points3d):
        if not is_dataclass(value):
            raise TypeError("COLMAP 전송 값은 dataclass여야 합니다.")
        for field in fields(value):
            candidate = getattr(value, field.name)
            if hasattr(candidate, "as_pointer") or candidate.__class__.__module__.startswith("bpy"):
                raise TypeError("COLMAP 전송 Snapshot에 Blender ID가 포함되었습니다.")
    expected = snapshot_content_fingerprint(
        source_dataset_uuid=snapshot.source_dataset_uuid,
        source_revision=snapshot.source_revision,
        source_fingerprint=snapshot.source_fingerprint,
        cameras=snapshot.cameras,
        images=snapshot.images,
        points3d=snapshot.points3d,
    )
    if snapshot.content_fingerprint != expected:
        raise ValueError("COLMAP 전송 Snapshot 내용 지문이 일치하지 않습니다.")
    return snapshot


__all__ = [
    "TRANSFER_SCHEMA_VERSION",
    "CameraValue",
    "ColmapTransferSnapshot",
    "ImageValue",
    "Point3DValue",
    "SnapshotContentFingerprintBuilder",
    "TransferDisplayDefaults",
    "snapshot_content_fingerprint",
    "validate_snapshot",
]
