"""관측 배열을 보관하지 않는 참고용 점 표본과 원본 메타데이터를 구성합니다."""

from __future__ import annotations

from array import array
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

import numpy as np

from .errors import ColmapValidationError
from .types import CameraRecord, FrameRecord, ImageTable, PointTable, RigRecord

SAMPLE_POLICY = "POINT_ID_PRIORITY_V1"
_BLOCK_SIZE = 4096


@dataclass(frozen=True, slots=True)
class ColmapReferencePreview:
    """전체 입력과 구별되는 참고 표본과 원본 전체의 수량·경계를 보관합니다."""

    cameras: Mapping[int, CameraRecord]
    images: ImageTable
    rigs: Mapping[int, RigRecord]
    frames: Mapping[int, FrameRecord]
    points: PointTable
    bounds_min: np.ndarray
    bounds_max: np.ndarray
    source_point_count: int
    source_track_count: int
    source_observation_count: int
    point_limit: int
    source_indices: np.ndarray
    source_path: Path
    source_format: str
    sample_policy: str = SAMPLE_POLICY

    def __post_init__(self):
        """참고 결과의 사전과 메타데이터 배열을 읽기 전용으로 고정합니다."""
        for name in ("cameras", "rigs", "frames"):
            object.__setattr__(self, name, MappingProxyType(dict(getattr(self, name))))
        for name in ("bounds_min", "bounds_max", "source_indices"):
            value = np.ascontiguousarray(getattr(self, name))
            value.setflags(write=False)
            object.__setattr__(self, name, value)


def point_priorities(ids: np.ndarray) -> np.ndarray:
    """원본 64비트 식별자만으로 순서와 입력 형식에 독립적인 표본 우선값을 계산합니다."""
    values = ids.astype(np.uint64, copy=True)
    values ^= values >> np.uint64(30)
    values *= np.uint64(0xBF58476D1CE4E5B9)
    values ^= values >> np.uint64(27)
    values *= np.uint64(0x94D049BB133111EB)
    values ^= values >> np.uint64(31)
    return values


class ReferencePointCollector:
    """전체 점을 검사하면서 한 블록과 지정 개수의 대표점만 누적합니다."""

    def __init__(self, limit: int):
        """표본 상한과 전체 식별자 검사·경계 누적용 저장소를 준비합니다."""
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100_000_000:
            raise ValueError("참고 점 상한은 1부터 100,000,000 사이의 정수여야 합니다.")
        self.limit = limit
        self.all_ids = array("Q")
        self.pending = []
        self.ids = np.empty(0, dtype=np.uint64)
        self.positions = np.empty((0, 3), dtype=np.float64)
        self.colors = np.empty((0, 3), dtype=np.uint8)
        self.errors = np.empty(0, dtype=np.float64)
        self.indices = np.empty(0, dtype=np.uint64)
        self.priorities = np.empty(0, dtype=np.uint64)
        self.bounds_min = np.full(3, np.inf, dtype=np.float64)
        self.bounds_max = np.full(3, -np.inf, dtype=np.float64)
        self.source_count = 0

    def add(self, point_id, xyz, rgb, error):
        """한 점을 작업 블록에 넣고 블록이 찼을 때만 배열 검사와 표본 선택을 수행합니다."""
        self.all_ids.append(point_id)
        self.pending.append((point_id, xyz, rgb, error, self.source_count))
        self.source_count += 1
        if len(self.pending) >= _BLOCK_SIZE:
            self._flush()

    def _flush(self):
        """블록 전체의 수치를 검사하고 고정 우선순위의 대표점만 유지합니다."""
        if not self.pending:
            return
        ids = np.asarray([row[0] for row in self.pending], dtype=np.uint64)
        xyz = np.asarray([row[1] for row in self.pending], dtype=np.float64)
        rgb = np.asarray([row[2] for row in self.pending], dtype=np.uint8)
        errors = np.asarray([row[3] for row in self.pending], dtype=np.float64)
        indices = np.asarray([row[4] for row in self.pending], dtype=np.uint64)
        invalid = (ids == 0) | (ids == np.iinfo(np.uint64).max)
        invalid |= ~np.isfinite(xyz).all(axis=1) | ~np.isfinite(errors) | (errors < 0)
        if invalid.any():
            point_id = int(ids[np.flatnonzero(invalid)[0]])
            raise ColmapValidationError(f"POINT3D_ID {point_id}의 식별자·좌표·재투영 오차가 올바르지 않습니다.")
        self.bounds_min = np.minimum(self.bounds_min, xyz.min(axis=0))
        self.bounds_max = np.maximum(self.bounds_max, xyz.max(axis=0))
        combined = (
            np.concatenate((self.ids, ids)),
            np.concatenate((self.positions, xyz)),
            np.concatenate((self.colors, rgb)),
            np.concatenate((self.errors, errors)),
            np.concatenate((self.indices, indices)),
            np.concatenate((self.priorities, point_priorities(ids))),
        )
        if len(combined[0]) > self.limit:
            priorities = combined[-1]
            threshold = np.partition(priorities, self.limit - 1)[self.limit - 1]
            lower = np.flatnonzero(priorities < threshold)
            equal = np.flatnonzero(priorities == threshold)
            equal = equal[np.argsort(combined[0][equal])[:self.limit - len(lower)]]
            selection = np.concatenate((lower, equal))
            combined = tuple(values[selection] for values in combined)
        self.ids, self.positions, self.colors, self.errors, self.indices, self.priorities = combined
        self.pending.clear()

    def result(self):
        """전체 식별자 중복을 확인하고 정렬된 참고 점 테이블을 반환합니다."""
        self._flush()
        all_ids = np.frombuffer(self.all_ids, dtype=np.uint64)
        if np.unique(all_ids).size != all_ids.size:
            raise ColmapValidationError("원본 POINT3D_ID에 중복 식별자가 있습니다.")
        # 전체 식별자는 중복 검사 뒤 해제하고 실제 원본 인덱스만 표본에 남깁니다.
        del all_ids
        self.all_ids = array("Q")
        order = np.argsort(self.ids)
        self.indices = self.indices[order]
        if self.source_count == 0:
            self.bounds_min = self.bounds_max = np.zeros(3, dtype=np.float64)
        count = len(order)
        return PointTable(
            point_ids=self.ids[order], positions=self.positions[order],
            colors=self.colors[order], errors=self.errors[order],
            track_offsets=np.zeros(count + 1, dtype=np.uint64),
            track_image_ids=np.empty(0, dtype=np.uint64),
            track_point2d_indices=np.empty(0, dtype=np.uint32),
            display_indices=np.arange(count, dtype=np.uint32),
        )
