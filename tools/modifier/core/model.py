"""COLMAP sparse reconstruction의 손실 없는 도메인 레코드입니다."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Mapping, Optional


@dataclass(frozen=True)
class CameraRecord:
    camera_id: int
    model_id: int
    model_name: str
    width: int
    height: int
    params: tuple[float, ...]


@dataclass(frozen=True)
class Point2DRecord:
    x: float
    y: float
    point3d_id: Optional[int]


@dataclass(frozen=True)
class ImageRecord:
    image_id: int
    qvec: tuple[float, float, float, float]
    tvec: tuple[float, float, float]
    camera_id: int
    name: str
    points2d: tuple[Point2DRecord, ...]


@dataclass(frozen=True)
class TrackElement:
    image_id: int
    point2d_idx: int


@dataclass(frozen=True)
class Point3DRecord:
    point3d_id: int
    xyz: tuple[float, float, float]
    rgb: tuple[int, int, int]
    error: float
    track: tuple[TrackElement, ...]


@dataclass(frozen=True)
class SensorRecord:
    sensor_type: int
    sensor_id: int
    has_pose: bool
    qvec: Optional[tuple[float, float, float, float]] = None
    tvec: Optional[tuple[float, float, float]] = None


@dataclass(frozen=True)
class RigRecord:
    rig_id: int
    ref_sensor_type: int
    ref_sensor_id: int
    sensors: tuple[SensorRecord, ...]


@dataclass(frozen=True)
class DataIdRecord:
    sensor_type: int
    sensor_id: int
    data_id: int


@dataclass(frozen=True)
class FrameRecord:
    frame_id: int
    rig_id: int
    qvec: tuple[float, float, float, float]
    tvec: tuple[float, float, float]
    data_ids: tuple[DataIdRecord, ...]


@dataclass
class Reconstruction:
    cameras: dict[int, CameraRecord] = field(default_factory=dict)
    images: dict[int, ImageRecord] = field(default_factory=dict)
    points3d: dict[int, Point3DRecord] = field(default_factory=dict)
    rigs: dict[int, RigRecord] = field(default_factory=dict)
    frames: dict[int, FrameRecord] = field(default_factory=dict)


@dataclass(frozen=True)
class ResolvedLayout:
    model_directory: Path
    input_format: str
    layout: str
    selected_files: tuple[Path, ...]


@dataclass(frozen=True)
class SourceDescriptor:
    dataset_root: Path
    sparse_root: Path
    model_directory: Path
    images_directory: Path
    input_format: str
    layout: str
    selected_files: tuple[Path, ...]
    source_fingerprint: str
    dataset_uuid: str
    schema_version: int


@dataclass(frozen=True, eq=False)
class ValidatedReconstructionToken:
    """검증된 Reconstruction 객체와 source revision을 함께 고정합니다."""

    reconstruction: Reconstruction = field(repr=False)
    dataset_uuid: str
    source_fingerprint: str
    validation_revision: int
    cameras: Mapping[int, CameraRecord] = field(repr=False)
    images: Mapping[int, ImageRecord] = field(repr=False)
    points3d: Mapping[int, Point3DRecord] = field(repr=False)
    rigs: Mapping[int, RigRecord] = field(repr=False)
    frames: Mapping[int, FrameRecord] = field(repr=False)


@dataclass(frozen=True)
class ValidatedImagePath:
    """Loader 경계에서 안전성과 존재 여부가 확인된 Image 경로입니다."""

    image_id: int
    image_name: str
    resolved_image_path: Path
    path_status: str


@dataclass(frozen=True)
class ImagePathManifest:
    """검증된 이미지 경로를 Dataset source token과 함께 고정합니다."""

    dataset_uuid: str
    source_fingerprint: str
    images_directory: Path
    entries: Mapping[int, ValidatedImagePath]

    def __post_init__(self) -> None:
        root = Path(self.images_directory)
        if not root.is_absolute():
            raise ValueError("이미지 경로 manifest의 Root가 절대경로가 아닙니다.")
        if ".." in root.parts:
            raise ValueError("이미지 경로 manifest의 Root가 정규화되지 않았습니다.")
        supplied = dict(self.entries)
        normalized: dict[int, ValidatedImagePath] = {}
        for image_id in sorted(supplied):
            entry = supplied[image_id]
            if image_id != entry.image_id:
                raise ValueError("이미지 경로 manifest의 IMAGE_ID key가 일치하지 않습니다.")
            if not entry.image_name:
                raise ValueError("이미지 경로 manifest의 Image 이름이 비어 있습니다.")
            if entry.path_status not in {"EXACT", "FALLBACK"}:
                raise ValueError("검증된 이미지 경로 manifest 상태가 올바르지 않습니다.")
            path = Path(entry.resolved_image_path)
            if not path.is_absolute():
                raise ValueError("검증된 Image 경로가 절대경로가 아닙니다.")
            if ".." in path.parts:
                raise ValueError(
                    "검증된 Image 경로가 Images Directory 밖을 가리킵니다."
                )
            try:
                path.relative_to(root)
            except ValueError as exc:
                raise ValueError(
                    "검증된 Image 경로가 Images Directory 밖을 가리킵니다."
                ) from exc
            normalized[image_id] = entry
        object.__setattr__(self, "images_directory", root)
        object.__setattr__(self, "entries", MappingProxyType(normalized))


@dataclass(frozen=True)
class LoadedReconstruction:
    reconstruction: Reconstruction
    source: SourceDescriptor
    warnings: tuple[str, ...]
    validation_token: Optional[ValidatedReconstructionToken] = field(
        default=None,
        compare=False,
        repr=False,
    )
    image_path_manifest: Optional[ImagePathManifest] = field(
        default=None,
        compare=False,
        repr=False,
    )
