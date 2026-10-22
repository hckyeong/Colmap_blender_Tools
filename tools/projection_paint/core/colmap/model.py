"""Camera, Image와 Point3D용 COLMAP 불변 도메인 모델을 정의합니다."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Mapping


@dataclass(frozen=True, slots=True)
class ParserLimits:
    """외부 COLMAP 입력의 자원 사용 상한을 보관합니다."""

    max_cameras: int = 100_000
    max_images: int = 100_000
    max_points3d: int = 100_000_000
    max_track_elements: int = 1_000_000_000
    max_image_name_bytes: int = 1024 * 1024
    max_text_header_bytes: int = 1024 * 1024
    max_text_point_line_bytes: int = 1024 * 1024 * 1024
    max_source_file_bytes: int = 16 * 1024 * 1024 * 1024
    max_camera_dimension: int = 1_000_000
    cancel_check_interval: int = 256
    text_chunk_bytes: int = 64 * 1024

    def __post_init__(self) -> None:
        """모든 안전 상한이 양수인지 검증합니다."""
        for name, value in (
            ("max_cameras", self.max_cameras),
            ("max_images", self.max_images),
            ("max_points3d", self.max_points3d),
            ("max_track_elements", self.max_track_elements),
            ("max_image_name_bytes", self.max_image_name_bytes),
            ("max_text_header_bytes", self.max_text_header_bytes),
            ("max_text_point_line_bytes", self.max_text_point_line_bytes),
            ("max_source_file_bytes", self.max_source_file_bytes),
            ("max_camera_dimension", self.max_camera_dimension),
            ("cancel_check_interval", self.cancel_check_interval),
            ("text_chunk_bytes", self.text_chunk_bytes),
        ):
            if not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name}은 0보다 큰 정수여야 합니다.")


DEFAULT_LIMITS = ParserLimits()


@dataclass(frozen=True, slots=True)
class SourceFileIdentity:
    """열린 Source 파일의 변경 감지용 Identity를 보관합니다."""

    path: Path
    size: int
    mtime_ns: int
    device: int
    inode: int

    def same_file(self, other: SourceFileIdentity) -> bool:
        """경로와 파일 Identity가 모두 같은지 반환합니다."""
        return (
            self.path == other.path
            and self.size == other.size
            and self.mtime_ns == other.mtime_ns
            and self.device == other.device
            and self.inode == other.inode
        )


@dataclass(frozen=True, slots=True)
class CameraRecord:
    """COLMAP Camera Intrinsics 한 건을 보관합니다."""

    camera_id: int
    model_id: int
    model_name: str
    width: int
    height: int
    params: tuple[float, ...]


@dataclass(frozen=True, slots=True)
class ImagePoseRecord:
    """Point2D 없이 COLMAP Image Pose Header만 보관합니다."""

    image_id: int
    camera_id: int
    qvec: tuple[float, float, float, float]
    tvec: tuple[float, float, float]
    image_name: str


@dataclass(frozen=True, slots=True)
class TrackElement:
    """Point3D를 관측한 Image와 Point2D 인덱스를 보관합니다."""

    image_id: int
    point2d_index: int


@dataclass(frozen=True, slots=True)
class Point3DRecord:
    """COLMAP Point3D의 좌표, 색상, 오차와 Track을 보관합니다."""

    point3d_id: int
    xyz: tuple[float, float, float]
    rgb: tuple[int, int, int]
    error: float
    track: tuple[TrackElement, ...]


@dataclass(frozen=True, slots=True)
class ResolvedLayout:
    """Camera, Image와 Point3D로 확정된 Sparse 입력 Layout을 보관합니다."""

    model_directory: Path
    input_format: str
    cameras_path: Path
    images_path: Path
    points3d_path: Path

    @property
    def selected_files(self) -> tuple[Path, Path, Path]:
        """선택된 Camera, Image와 Point3D 파일을 반환합니다."""
        return self.cameras_path, self.images_path, self.points3d_path


@dataclass(frozen=True, slots=True)
class ColmapDataset:
    """검증된 Camera, Image와 Point3D 레코드 및 Source Identity를 보관합니다."""

    layout: ResolvedLayout
    cameras: Mapping[int, CameraRecord]
    images: Mapping[int, ImagePoseRecord]
    points3d: Mapping[int, Point3DRecord]
    source_identities: tuple[
        SourceFileIdentity,
        SourceFileIdentity,
        SourceFileIdentity,
    ]

    source_file_fingerprint: str = ""

    def __post_init__(self) -> None:
        """일반 매핑은 고정하고 공통 파서의 불변 점 보기는 복사 없이 보관합니다."""
        object.__setattr__(
            self,
            "cameras",
            MappingProxyType(dict(self.cameras)),
        )
        object.__setattr__(
            self,
            "images",
            MappingProxyType(dict(self.images)),
        )
        from .shared_reader import PointRecordMapping

        object.__setattr__(
            self,
            "points3d",
            self.points3d if isinstance(self.points3d, PointRecordMapping) else MappingProxyType(dict(self.points3d)),
        )


@dataclass(frozen=True, slots=True)
class ImagePathEntry:
    """IMAGE_ID 하나의 검증된 상대경로와 파일 Identity를 보관합니다."""

    image_id: int
    image_name: str
    normalized_relative_path: str
    resolved_path: Path
    path_status: str
    file_identity: SourceFileIdentity


@dataclass(frozen=True, slots=True)
class ImagePathManifest:
    """Images Root와 IMAGE_ID별 검증 경로를 불변 Mapping으로 보관합니다."""

    images_root: Path
    entries: Mapping[int, ImagePathEntry]

    def __post_init__(self) -> None:
        """Manifest Entry를 외부에서 변경할 수 없게 고정합니다."""
        object.__setattr__(
            self,
            "entries",
            MappingProxyType(dict(self.entries)),
        )


@dataclass(frozen=True, slots=True)
class FrameMapping:
    """연속 Frame과 불연속 IMAGE_ID의 양방향 Mapping을 보관합니다."""

    frame_start: int
    frame_end: int
    frame_to_image: Mapping[int, int]
    image_to_frame: Mapping[int, int]

    def __post_init__(self) -> None:
        """양방향 Frame Mapping을 외부에서 변경할 수 없게 고정합니다."""
        object.__setattr__(
            self,
            "frame_to_image",
            MappingProxyType(dict(self.frame_to_image)),
        )
        object.__setattr__(
            self,
            "image_to_frame",
            MappingProxyType(dict(self.image_to_frame)),
        )


@dataclass(frozen=True, slots=True)
class CameraDataPayload:
    """Blender Camera Data 한 개를 만들기 위한 Intrinsics를 보관합니다."""

    camera_id: int
    model_name: str
    width: int
    height: int
    fx: float
    fy: float
    cx: float
    cy: float


@dataclass(frozen=True, slots=True)
class CameraObjectPayload:
    """IMAGE_ID별 Blender Camera Object 생성 정보를 보관합니다."""

    image_id: int
    camera_id: int
    image_name: str
    qvec: tuple[float, float, float, float]
    tvec: tuple[float, float, float]
    frame: int


@dataclass(frozen=True, slots=True)
class MarkerPayload:
    """연속 Frame에 생성할 관리 Camera Marker 정보를 보관합니다."""

    frame: int
    image_id: int


@dataclass(frozen=True, slots=True)
class PointPayload:
    """Blender Point Mesh 한 Vertex의 표시 데이터를 보관합니다."""

    point3d_id: int
    xyz: tuple[float, float, float]
    rgb: tuple[int, int, int]
    error: float


@dataclass(frozen=True, slots=True)
class ImportStagingPayload:
    """Blender Scene 연결 전 완성된 Reconstruction 준비 데이터를 보관합니다."""

    dataset: ColmapDataset
    image_manifest: ImagePathManifest
    frame_mapping: FrameMapping
    source_fingerprint: str
    axis_matrix: tuple[tuple[float, float, float, float], ...]
    camera_data: tuple[CameraDataPayload, ...]
    camera_objects: tuple[CameraObjectPayload, ...]
    points: tuple[PointPayload, ...]
    markers: tuple[MarkerPayload, ...]
    warnings: tuple[str, ...] = field(default_factory=tuple)
