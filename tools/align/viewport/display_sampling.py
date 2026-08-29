"""POINT3D_ID 기준의 결정적 뷰포트 표시 샘플을 준비합니다."""

from __future__ import annotations

from dataclasses import dataclass
import math
import numbers
import operator
from time import perf_counter
from typing import Any

import numpy as np


# 이 값은 파일 탐색기와 직접 API가 공유하는 모델당 기본 표시 상한입니다.
DEFAULT_DISPLAY_POINT_LIMIT = 250_000
DEFAULT_DISPLAY_SAMPLE_CHUNK_SIZE = 1_000_000
DISPLAY_SAMPLE_POLICY_VERSION = 1
DISPLAY_SAMPLE_POLICY = "point_id_hash_v1"

# 정책 버전이 바뀌지 않는 한 이 고정 seed도 변경하지 않습니다.
DISPLAY_SAMPLE_FIXED_SEED = np.uint64(0xD1B54A32D192ED03)
_UINT64_MAX = int(np.iinfo(np.uint64).max)
_UINT32_INDEX_COUNT_LIMIT = int(np.iinfo(np.uint32).max) + 1


class DisplaySamplingError(ValueError):
    """표시 샘플 입력 또는 결과 계약이 올바르지 않을 때 발생합니다."""


class DisplaySamplingStateError(DisplaySamplingError):
    """완료 전 결과 접근처럼 빌더 상태가 맞지 않을 때 발생합니다."""


@dataclass(frozen=True, slots=True)
class DisplaySamplingProgress:
    """한 번의 증분 표시 샘플 처리 뒤 진행 상태를 보관합니다."""

    processed_count: int
    total_count: int
    display_count: int
    is_completed: bool
    is_cancelled: bool

    @property
    def fraction(self) -> float:
        """처리한 원본 점 비율을 0부터 1 사이 값으로 반환합니다."""
        if self.total_count <= 0:
            return 1.0 if self.is_completed else 0.0
        return min(1.0, max(0.0, self.processed_count / self.total_count))

    @property
    def completed(self) -> bool:
        """호환성을 위해 완료 상태를 짧은 이름으로 반환합니다."""
        return self.is_completed

    @property
    def cancelled(self) -> bool:
        """호환성을 위해 취소 상태를 짧은 이름으로 반환합니다."""
        return self.is_cancelled

    @property
    def done(self) -> bool:
        """완료 또는 취소로 더 이상 처리하지 않는 상태인지 반환합니다."""
        return self.is_completed or self.is_cancelled


def _positive_integer(value: Any, *, field_name: str) -> int:
    """bool을 제외한 양의 정수 설정값을 검증해 Python int로 반환합니다."""
    if isinstance(value, (bool, np.bool_)):
        raise DisplaySamplingError(f"{field_name}은 bool이 아닌 양의 정수여야 합니다.")
    try:
        normalized = operator.index(value)
    except TypeError as error:
        raise DisplaySamplingError(f"{field_name}은 양의 정수여야 합니다.") from error
    if normalized < 1:
        raise DisplaySamplingError(f"{field_name}은 1 이상이어야 합니다.")
    return int(normalized)


def normalize_display_point_limit(value: Any) -> int:
    """파일 탐색기와 ImportJob의 모델당 표시 점 상한을 검증해 반환합니다."""
    return _positive_integer(value, field_name="표시 점 상한")


def _normalize_point_ids(value: Any, *, validate_unique: bool) -> np.ndarray:
    """POINT3D_ID 입력을 손실 없이 uint64 1차원 배열로 검증합니다."""
    raw_ids = np.asarray(value)
    if raw_ids.ndim != 1:
        raise DisplaySamplingError("POINT3D_ID는 1차원 배열이어야 합니다.")

    if raw_ids.dtype.kind == "u":
        point_ids = np.ascontiguousarray(raw_ids, dtype=np.uint64)
    elif raw_ids.dtype.kind == "i":
        if np.any(raw_ids < 0):
            raise DisplaySamplingError("POINT3D_ID는 음수일 수 없습니다.")
        point_ids = np.ascontiguousarray(raw_ids, dtype=np.uint64)
    elif raw_ids.dtype.kind == "O":
        point_ids = np.empty(raw_ids.size, dtype=np.uint64)
        for index, item in enumerate(raw_ids):
            if isinstance(item, (bool, np.bool_)) or not isinstance(item, numbers.Integral):
                raise DisplaySamplingError("POINT3D_ID는 uint64로 변환 가능한 정수여야 합니다.")
            integer_item = int(item)
            if integer_item < 0 or integer_item > _UINT64_MAX:
                raise DisplaySamplingError("POINT3D_ID가 uint64 범위를 벗어났습니다.")
            point_ids[index] = integer_item
    else:
        raise DisplaySamplingError("POINT3D_ID는 uint64로 변환 가능한 정수 배열이어야 합니다.")

    # 공개 API는 중복 ID를 명시적으로 거부해 hash score 동점 가능성을 없앱니다.
    if validate_unique and point_ids.size and np.unique(point_ids).size != point_ids.size:
        raise DisplaySamplingError("중복된 POINT3D_ID는 표시 샘플링에 사용할 수 없습니다.")
    return point_ids


def hash_point_ids(point_ids: np.ndarray) -> np.ndarray:
    """고정 uint64 혼합으로 POINT3D_ID별 충돌 없는 샘플 순위를 계산합니다."""
    source_ids = np.asarray(point_ids)
    if source_ids.ndim != 1 or source_ids.dtype != np.dtype(np.uint64):
        raise DisplaySamplingError("해시 입력 POINT3D_ID는 uint64 1차원 배열이어야 합니다.")
    # SplitMix64의 가역 혼합을 사용하므로 서로 다른 uint64 ID는 서로 다른 score를 가집니다.
    with np.errstate(over="ignore"):
        values = source_ids ^ DISPLAY_SAMPLE_FIXED_SEED
        values = values + np.uint64(0x9E3779B97F4A7C15)
        values = (values ^ (values >> np.uint64(30))) * np.uint64(0xBF58476D1CE4E5B9)
        values = (values ^ (values >> np.uint64(27))) * np.uint64(0x94D049BB133111EB)
        values = values ^ (values >> np.uint64(31))
    return np.ascontiguousarray(values, dtype=np.uint64)


def _index_dtype_for_count(point_count: int) -> np.dtype[np.unsignedinteger[Any]]:
    """원본 배열 인덱스 범위를 모두 표현할 최소 unsigned dtype을 반환합니다."""
    return np.dtype(np.uint32 if point_count <= _UINT32_INDEX_COUNT_LIMIT else np.uint64)


def _smallest_score_positions(scores: np.ndarray, limit: int) -> np.ndarray:
    """동점 없는 uint64 score에서 가장 작은 limit개 위치를 반환합니다."""
    if limit >= scores.size:
        return np.arange(scores.size, dtype=np.intp)
    return np.argpartition(scores, limit - 1)[:limit]


class IncrementalDisplayIndexBuilder:
    """TIMER tick마다 POINT3D_ID 해시 Top-K를 누적하는 표시 인덱스 빌더입니다."""

    def __init__(
        self,
        point_ids: Any,
        *,
        max_points: int = DEFAULT_DISPLAY_POINT_LIMIT,
        chunk_size: int = DEFAULT_DISPLAY_SAMPLE_CHUNK_SIZE,
        _validated_unique: bool = False,
    ) -> None:
        """원본 POINT3D_ID와 고정 표시 상한으로 아직 시작하지 않은 빌더를 만듭니다."""
        self._max_points = _positive_integer(max_points, field_name="표시 점 상한")
        self._chunk_size = _positive_integer(chunk_size, field_name="표시 샘플 청크 크기")
        # ImportJob만 validate_model 직후 이 내부 경계를 사용해 전체 ID 정렬을 반복하지 않습니다.
        if not isinstance(_validated_unique, bool):
            raise DisplaySamplingError("내부 POINT3D_ID 고유성 검증 상태는 bool이어야 합니다.")
        self._point_ids: np.ndarray | None = _normalize_point_ids(
            point_ids,
            validate_unique=not _validated_unique,
        )
        self._total_count = int(self._point_ids.size)
        self._index_dtype = _index_dtype_for_count(self._total_count)
        self._processed_count = 0
        self._candidate_scores: np.ndarray | None = np.empty(0, dtype=np.uint64)
        self._candidate_indices: np.ndarray | None = np.empty(0, dtype=self._index_dtype)
        self._display_indices: np.ndarray | None = None
        self._is_completed = False
        self._is_cancelled = False

    @property
    def max_points(self) -> int:
        """이 빌더에 고정된 모델당 표시 점 상한을 반환합니다."""
        return self._max_points

    @property
    def chunk_size(self) -> int:
        """한 내부 해시 처리 블록의 최대 원본 점 수를 반환합니다."""
        return self._chunk_size

    @property
    def processed_count(self) -> int:
        """현재까지 해시 순위를 처리한 원본 점 수를 반환합니다."""
        return self._processed_count

    @property
    def total_count(self) -> int:
        """샘플 대상 원본 POINT3D_ID 전체 수를 반환합니다."""
        return self._total_count

    @property
    def is_completed(self) -> bool:
        """표시 인덱스가 확정됐는지 반환합니다."""
        return self._is_completed

    @property
    def is_cancelled(self) -> bool:
        """취소로 후보와 원본 참조를 해제했는지 반환합니다."""
        return self._is_cancelled

    @property
    def display_indices(self) -> np.ndarray:
        """완료된 뒤 원본 배열 인덱스 오름차순 표시 인덱스를 반환합니다."""
        if not self._is_completed or self._display_indices is None:
            raise DisplaySamplingStateError("표시 샘플링이 완료되기 전에는 display_indices에 접근할 수 없습니다.")
        return self._display_indices

    def _progress(self) -> DisplaySamplingProgress:
        """현재 내부 상태를 외부 TIMER 호출용 불변 진행 값으로 변환합니다."""
        if self._display_indices is not None:
            display_count = int(self._display_indices.size)
        elif self._candidate_indices is not None:
            display_count = int(self._candidate_indices.size)
        else:
            display_count = 0
        return DisplaySamplingProgress(
            processed_count=self._processed_count,
            total_count=self._total_count,
            display_count=display_count,
            is_completed=self._is_completed,
            is_cancelled=self._is_cancelled,
        )

    def _merge_chunk(self, scores: np.ndarray, indices: np.ndarray) -> None:
        """현재 청크의 후보와 이전 후보를 합쳐 score 기준 Top-K만 유지합니다."""
        candidate_scores = self._candidate_scores
        candidate_indices = self._candidate_indices
        if candidate_scores is None or candidate_indices is None:
            raise DisplaySamplingStateError("취소된 표시 샘플 빌더는 후보를 갱신할 수 없습니다.")
        if scores.size > self._max_points:
            chunk_positions = _smallest_score_positions(scores, self._max_points)
            scores = scores[chunk_positions]
            indices = indices[chunk_positions]
        if candidate_scores.size:
            scores = np.concatenate((candidate_scores, scores))
            indices = np.concatenate((candidate_indices, indices))
        if scores.size > self._max_points:
            candidate_positions = _smallest_score_positions(scores, self._max_points)
            scores = scores[candidate_positions]
            indices = indices[candidate_positions]
        self._candidate_scores = np.ascontiguousarray(scores, dtype=np.uint64)
        self._candidate_indices = np.ascontiguousarray(indices, dtype=self._index_dtype)

    def _complete(self) -> None:
        """누적 후보를 원본 인덱스 순서로 확정하고 임시 참조를 해제합니다."""
        expected_count = min(self._total_count, self._max_points)
        if self._total_count <= self._max_points:
            display_indices = np.arange(self._total_count, dtype=self._index_dtype)
        else:
            candidate_indices = self._candidate_indices
            if candidate_indices is None or candidate_indices.size != expected_count:
                raise DisplaySamplingError("표시 샘플 후보 수가 요청한 상한과 일치하지 않습니다.")
            display_indices = np.sort(candidate_indices).astype(self._index_dtype, copy=False)
        if display_indices.size != expected_count:
            raise DisplaySamplingError("표시 샘플 결과 수가 요청한 상한과 일치하지 않습니다.")
        if display_indices.size and (
            np.any(display_indices[1:] <= display_indices[:-1])
            or int(display_indices[-1]) >= self._total_count
        ):
            raise DisplaySamplingError("표시 샘플 결과 인덱스가 고유한 오름차순 범위를 만족하지 않습니다.")
        finalized_indices = np.ascontiguousarray(display_indices, dtype=self._index_dtype)
        finalized_indices.setflags(write=False)
        self._display_indices = finalized_indices
        self._candidate_scores = None
        self._candidate_indices = None
        self._point_ids = None
        self._processed_count = self._total_count
        self._is_completed = True

    def step(
        self,
        *,
        max_records: int,
        time_budget_seconds: float,
    ) -> DisplaySamplingProgress:
        """주어진 레코드·시간 예산 안에서 다음 POINT3D_ID 청크를 처리합니다."""
        record_limit = _positive_integer(max_records, field_name="표시 샘플 단계 레코드 수")
        try:
            budget_seconds = float(time_budget_seconds)
        except (TypeError, ValueError) as error:
            raise DisplaySamplingError("표시 샘플 시간 예산은 0 이상의 유한한 수여야 합니다.") from error
        if not math.isfinite(budget_seconds) or budget_seconds < 0.0:
            raise DisplaySamplingError("표시 샘플 시간 예산은 0 이상의 유한한 수여야 합니다.")
        if self._is_cancelled or self._is_completed:
            return self._progress()

        source_ids = self._point_ids
        if source_ids is None:
            raise DisplaySamplingStateError("표시 샘플 원본 POINT3D_ID 참조가 없습니다.")
        if self._total_count <= self._max_points:
            self._complete()
            return self._progress()

        started_at = perf_counter()
        processed_this_step = 0
        while self._processed_count < self._total_count and processed_this_step < record_limit:
            if processed_this_step and perf_counter() - started_at >= budget_seconds:
                break
            available_records = min(
                self._chunk_size,
                record_limit - processed_this_step,
                self._total_count - self._processed_count,
            )
            chunk_start = self._processed_count
            chunk_end = chunk_start + available_records
            scores = hash_point_ids(source_ids[chunk_start:chunk_end])
            indices = np.arange(chunk_start, chunk_end, dtype=self._index_dtype)
            self._merge_chunk(scores, indices)
            self._processed_count = chunk_end
            processed_this_step += available_records

        if self._processed_count == self._total_count:
            self._complete()
        return self._progress()

    def cancel(self) -> None:
        """진행 중 후보와 원본 POINT3D_ID 참조를 해제하고 이후 처리를 막습니다."""
        if self._is_completed or self._is_cancelled:
            return
        self._point_ids = None
        self._candidate_scores = None
        self._candidate_indices = None
        self._display_indices = None
        self._is_cancelled = True


def build_display_indices(
    point_ids: Any,
    *,
    max_points: int = DEFAULT_DISPLAY_POINT_LIMIT,
    chunk_size: int = DEFAULT_DISPLAY_SAMPLE_CHUNK_SIZE,
) -> np.ndarray:
    """POINT3D_ID 해시 순위로 정확한 표시 상한의 원본 인덱스를 동기 생성합니다."""
    builder = IncrementalDisplayIndexBuilder(
        point_ids,
        max_points=max_points,
        chunk_size=chunk_size,
    )
    while not builder.is_completed:
        builder.step(
            max_records=builder.chunk_size,
            time_budget_seconds=0.0,
        )
    return builder.display_indices


__all__ = (
    "DEFAULT_DISPLAY_POINT_LIMIT",
    "DEFAULT_DISPLAY_SAMPLE_CHUNK_SIZE",
    "DISPLAY_SAMPLE_FIXED_SEED",
    "DISPLAY_SAMPLE_POLICY",
    "DISPLAY_SAMPLE_POLICY_VERSION",
    "DisplaySamplingError",
    "DisplaySamplingProgress",
    "DisplaySamplingStateError",
    "IncrementalDisplayIndexBuilder",
    "build_display_indices",
    "hash_point_ids",
    "normalize_display_point_limit",
)
