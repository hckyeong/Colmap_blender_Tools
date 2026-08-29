"""Blender API와 분리된 COLMAP Export 진행 상태 계약입니다."""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum, auto


class ExportPhase(Enum):
    """Export Transaction의 재진입 가능한 실행 단계입니다."""

    CREATED = auto()
    PREPARE = auto()
    COPY_SOURCE = auto()
    WRITE_STAGING = auto()
    PARSE_STAGING = auto()
    VALIDATE_STAGING = auto()
    COMPARE_STAGING = auto()
    HASH_STAGING = auto()
    COMMIT = auto()
    BUILD_RESULT = auto()
    CLEANUP = auto()
    DONE = auto()
    FAILED = auto()


@dataclass(frozen=True)
class ExportProgressSnapshot:
    """한 시점의 Export 진행 정보를 불변 값으로 전달합니다."""

    phase: ExportPhase
    overall_fraction: float
    phase_fraction: float
    completed_units: int
    total_units: int
    message: str
    elapsed_seconds: float
    can_cancel: bool

    def __post_init__(self) -> None:
        if not isinstance(self.phase, ExportPhase):
            raise ValueError("Export 진행 단계가 유효하지 않습니다.")
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
            raise ValueError("Export 진행 메시지가 비어 있습니다.")
        if (
            not isinstance(self.elapsed_seconds, (int, float))
            or not math.isfinite(self.elapsed_seconds)
            or self.elapsed_seconds < 0.0
        ):
            raise ValueError("경과 시간은 0 이상의 유한한 수치여야 합니다.")
        if not isinstance(self.can_cancel, bool):
            raise ValueError("취소 가능 상태는 bool이어야 합니다.")
