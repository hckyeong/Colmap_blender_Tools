"""Modifier와 Projection Painting이 공유할 COLMAP Dataset 순수 계약입니다."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


DATASET_SCHEMA_VERSION = 1
DATASET_OWNER_TOKEN = "colmap_dataset"

AXIS_ITEMS = (
    ("X", "X", "양의 X축"),
    ("-X", "-X", "음의 X축"),
    ("Y", "Y", "양의 Y축"),
    ("-Y", "-Y", "음의 Y축"),
    ("Z", "Z", "양의 Z축"),
    ("-Z", "-Z", "음의 Z축"),
)
AXIS_IDENTIFIERS = frozenset(item[0] for item in AXIS_ITEMS)
AXIS_PRESET_ITEMS = (
    (
        "REALITY_CAPTURE",
        "RealityCapture (RC)",
        "RealityCapture 좌표를 Blender 표시축에 맞춥니다",
    ),
    ("BLENDER", "Blender", "Blender의 Y Forward, Z Up 좌표를 그대로 표시합니다"),
    ("MAYA", "Maya", "Maya 기본 Y Up 좌표를 Blender 표시축에 맞춥니다"),
    ("HOUDINI", "Houdini", "Houdini 기본 Y Up 좌표를 Blender 표시축에 맞춥니다"),
    ("CUSTOM", "Custom", "Forward Axis와 Up Axis를 직접 설정합니다"),
)
AXIS_PRESET_IDENTIFIERS = frozenset(item[0] for item in AXIS_PRESET_ITEMS)


@dataclass(frozen=True, slots=True)
class DatasetImportMetadata:
    """성공한 Import가 공용 Scene 상태에 커밋할 검증 입력입니다."""

    dataset_uuid: str
    source_fingerprint: str
    dataset_root: str
    sparse_root: str
    model_directory: str
    images_directory: str
    input_format: str
    layout: str
    selected_files_json: str
    image_path_manifest_json: str
    camera_count: int
    image_count: int
    point3d_count: int
    source_schema_version: int
    axis_preset: str
    axis_forward: str
    axis_up: str
    point_size: float
    camera_frustum_size: float

    def __post_init__(self) -> None:
        """공용 Property에 기록하기 전에 필수 값과 수량을 검증합니다."""

        if not self.dataset_uuid.strip():
            raise ValueError("공용 Dataset UUID가 비어 있습니다.")
        if not self.source_fingerprint.strip():
            raise ValueError("공용 Dataset source fingerprint가 비어 있습니다.")
        for field_name in (
            "dataset_root",
            "sparse_root",
            "model_directory",
            "images_directory",
        ):
            value = str(getattr(self, field_name)).strip()
            if not value or not Path(value).is_absolute():
                raise ValueError(f"공용 Dataset {field_name} 경로가 절대경로가 아닙니다.")
        if self.input_format not in {"text", "binary"}:
            raise ValueError("공용 Dataset 입력 형식은 text 또는 binary여야 합니다.")
        if self.layout not in {"legacy", "modern"}:
            raise ValueError("공용 Dataset layout은 legacy 또는 modern이어야 합니다.")
        for field_name in ("camera_count", "image_count", "point3d_count"):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"공용 Dataset {field_name}는 0 이상의 정수여야 합니다.")
        if self.source_schema_version < 1:
            raise ValueError("공용 Dataset 원본 schema version이 올바르지 않습니다.")
        if self.axis_preset not in AXIS_PRESET_IDENTIFIERS:
            raise ValueError("공용 Dataset 표시축 프리셋이 올바르지 않습니다.")
        if self.axis_forward not in AXIS_IDENTIFIERS or self.axis_up not in AXIS_IDENTIFIERS:
            raise ValueError("공용 Dataset 표시축이 올바르지 않습니다.")
        if self.axis_forward.lstrip("-") == self.axis_up.lstrip("-"):
            raise ValueError("공용 Dataset Forward와 Up 축은 같을 수 없습니다.")
        if self.point_size <= 0.0:
            raise ValueError("공용 Dataset Point Scale은 0보다 커야 합니다.")
        if self.camera_frustum_size <= 0.0:
            raise ValueError("공용 Dataset Camera Frustum Size는 0보다 커야 합니다.")


@dataclass(frozen=True, slots=True)
class DatasetSnapshot:
    """도구가 직접 수정할 수 없는 한 Scene의 공용 Dataset Snapshot입니다."""

    dataset_uuid: str
    dataset_revision: int
    display_revision: int
    source_fingerprint: str
    dataset_root: str
    sparse_root: str
    model_directory: str
    images_directory: str
    input_format: str
    layout: str
    selected_files_json: str
    image_path_manifest_json: str
    camera_count: int
    image_count: int
    point3d_count: int
    schema_version: int
    source_schema_version: int
    axis_preset: str
    axis_forward: str
    axis_up: str
    point_size: float
    camera_frustum_size: float

    @property
    def available(self) -> bool:
        """활성 공용 Dataset이 완전한 최소 식별자를 가졌는지 반환합니다."""

        return bool(
            self.dataset_uuid
            and self.source_fingerprint
            and self.model_directory
            and self.schema_version == DATASET_SCHEMA_VERSION
        )


__all__ = [
    "AXIS_IDENTIFIERS",
    "AXIS_ITEMS",
    "AXIS_PRESET_IDENTIFIERS",
    "AXIS_PRESET_ITEMS",
    "DATASET_OWNER_TOKEN",
    "DATASET_SCHEMA_VERSION",
    "DatasetImportMetadata",
    "DatasetSnapshot",
]
