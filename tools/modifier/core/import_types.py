"""Blender API와 분리된 COLMAP Import 입력·진행 상태 계약입니다."""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum, auto
from pathlib import Path


CAMERA_FRUSTUM_SIZE_MIN = 0.01
CAMERA_FRUSTUM_SIZE_MAX = 1000.0
DEFAULT_CAMERA_FRUSTUM_SIZE = 1.0


class ImportPhase(Enum):
    """Import Core Job의 재진입 가능한 실행 단계입니다."""

    CREATED = auto()
    DISCOVER = auto()
    HASH_SOURCE_BEFORE = auto()
    READ_MODEL = auto()
    VALIDATE_MODEL = auto()
    VALIDATE_IMAGES = auto()
    HASH_SOURCE_AFTER = auto()
    BUILD_RESULT = auto()
    DONE = auto()
    FAILED = auto()


@dataclass(frozen=True)
class ImportPlan:
    """Import 시작 시 RNA 입력을 복사해 두는 불변 plan입니다.

    표시 크기와 축 값은 Core가 해석하지 않지만 Blender staging 단계가 같은
    입력 snapshot을 재사용할 수 있도록 함께 보존합니다.
    """

    dataset_root: Path | str
    sparse_directory: Path | str | None = None
    images_directory: Path | str | None = None
    input_format: str = "AUTO"
    point_size: float = 0.05
    axis_forward: str = "Y"
    axis_up: str = "Z"
    matching_dataset_uuid: str | None = None
    matching_model_directory: Path | str | None = None
    camera_frustum_size: float = DEFAULT_CAMERA_FRUSTUM_SIZE

    def __post_init__(self) -> None:
        root = Path(self.dataset_root).expanduser().resolve()
        sparse = self._optional_path(self.sparse_directory)
        images = self._optional_path(self.images_directory)
        matching_model = self._optional_path(self.matching_model_directory)
        requested_format = str(self.input_format).upper()
        if requested_format not in {"AUTO", "BINARY", "TEXT"}:
            raise ValueError(f"지원하지 않는 Input Format입니다: {self.input_format}")
        if (
            isinstance(self.point_size, bool)
            or not isinstance(self.point_size, (int, float))
            or not math.isfinite(self.point_size)
            or self.point_size <= 0.0
        ):
            raise ValueError("Point Scale은 0보다 큰 유한한 수치여야 합니다.")
        if (
            isinstance(self.camera_frustum_size, bool)
            or not isinstance(self.camera_frustum_size, (int, float))
            or not math.isfinite(self.camera_frustum_size)
            or self.camera_frustum_size < CAMERA_FRUSTUM_SIZE_MIN
            or self.camera_frustum_size > CAMERA_FRUSTUM_SIZE_MAX
        ):
            raise ValueError(
                "Camera Frustum Size는 0.01 이상 1000.0 이하의 "
                "유한한 수치여야 합니다."
            )
        if not isinstance(self.axis_forward, str) or not self.axis_forward:
            raise ValueError("Forward Axis가 비어 있습니다.")
        if not isinstance(self.axis_up, str) or not self.axis_up:
            raise ValueError("Up Axis가 비어 있습니다.")
        if (
            self.matching_dataset_uuid is not None
            and (
                not isinstance(self.matching_dataset_uuid, str)
                or not self.matching_dataset_uuid
            )
        ):
            raise ValueError("기존 Dataset UUID가 비어 있습니다.")

        object.__setattr__(self, "dataset_root", root)
        object.__setattr__(self, "sparse_directory", sparse)
        object.__setattr__(self, "images_directory", images)
        object.__setattr__(self, "matching_model_directory", matching_model)
        object.__setattr__(self, "input_format", requested_format)
        object.__setattr__(self, "point_size", float(self.point_size))
        object.__setattr__(
            self,
            "camera_frustum_size",
            float(self.camera_frustum_size),
        )

    @staticmethod
    def _optional_path(value: Path | str | None) -> Path | None:
        if value is None or str(value).strip() == "":
            return None
        return Path(value).expanduser().resolve()


@dataclass(frozen=True)
class ImportProgressSnapshot:
    """한 시점의 Import Core 진행 정보를 불변 값으로 전달합니다."""

    phase: ImportPhase
    overall_fraction: float
    phase_fraction: float
    completed_units: int
    total_units: int
    message: str
    elapsed_seconds: float
    can_cancel: bool

    def __post_init__(self) -> None:
        if not isinstance(self.phase, ImportPhase):
            raise ValueError("Import 진행 단계가 유효하지 않습니다.")
        for name, value in (
            ("전체 진행률", self.overall_fraction),
            ("현재 단계 진행률", self.phase_fraction),
        ):
            if not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError(f"{name}은 유한한 수치여야 합니다.")
            if value < 0.0 or value > 1.0:
                raise ValueError(f"{name}은 0.0과 1.0 사이여야 합니다.")
        if (
            isinstance(self.completed_units, bool)
            or not isinstance(self.completed_units, int)
            or self.completed_units < 0
        ):
            raise ValueError("완료 작업량은 0 이상의 정수여야 합니다.")
        if (
            isinstance(self.total_units, bool)
            or not isinstance(self.total_units, int)
            or self.total_units < 0
        ):
            raise ValueError("전체 작업량은 0 이상의 정수여야 합니다.")
        if self.total_units and self.completed_units > self.total_units:
            raise ValueError("완료 작업량이 전체 작업량을 초과했습니다.")
        if not isinstance(self.message, str) or not self.message:
            raise ValueError("Import 진행 메시지가 비어 있습니다.")
        if (
            not isinstance(self.elapsed_seconds, (int, float))
            or not math.isfinite(self.elapsed_seconds)
            or self.elapsed_seconds < 0.0
        ):
            raise ValueError("경과 시간은 0 이상의 유한한 수치여야 합니다.")
        if not isinstance(self.can_cancel, bool):
            raise ValueError("취소 가능 상태는 bool이어야 합니다.")


__all__ = [
    "CAMERA_FRUSTUM_SIZE_MAX",
    "CAMERA_FRUSTUM_SIZE_MIN",
    "DEFAULT_CAMERA_FRUSTUM_SIZE",
    "ImportPhase",
    "ImportPlan",
    "ImportProgressSnapshot",
]
