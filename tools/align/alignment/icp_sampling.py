"""원본 POINT3D_ID 기반의 결정적 ICP 복셀 샘플링을 제공합니다."""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from typing import Callable, Iterator, Mapping, Sequence

import numpy as np

from .icp_types import IcpCancelledError, IcpInputError, IcpPointCloud


_UINT64_MASK = np.uint64(0xFFFFFFFFFFFFFFFF)
_SPLITMIX_ADD = np.uint64(0x9E3779B97F4A7C15)
_SPLITMIX_MUL_1 = np.uint64(0xBF58476D1CE4E5B9)
_SPLITMIX_MUL_2 = np.uint64(0x94D049BB133111EB)
_DEFAULT_CHUNK_SIZE = 65_536


def _strict_positive_int(value: object, field_name: str) -> int:
    """bool·문자열 암묵 변환 없이 양의 정수를 검증합니다."""

    if isinstance(value, bool) or not isinstance(value, (int, np.integer)):
        raise IcpInputError(f"{field_name}은 bool이 아닌 정수여야 합니다.")
    normalized = int(value)
    if normalized < 1:
        raise IcpInputError(f"{field_name}은 1 이상이어야 합니다.")
    return normalized


def _strict_positive_float(value: object, field_name: str) -> float:
    """bool·문자열 암묵 변환 없이 유한한 양의 실수를 검증합니다."""

    if isinstance(value, bool) or not isinstance(value, (int, float, np.integer, np.floating)):
        raise IcpInputError(f"{field_name}은 bool·문자열이 아닌 실수여야 합니다.")
    normalized = float(value)
    if not isfinite(normalized) or normalized <= 0.0:
        raise IcpInputError(f"{field_name}은 유한한 양수여야 합니다.")
    return normalized


def _freeze_float64(value: object, shape: tuple[int | None, ...], field_name: str) -> np.ndarray:
    """샘플 결과에 쓸 유한 float64 배열만 복사해 읽기 전용으로 고정합니다."""

    array = np.array(value, dtype=np.float64, order="C", copy=True)
    if array.ndim != len(shape) or any(
        expected is not None and actual != expected
        for actual, expected in zip(array.shape, shape, strict=True)
    ):
        raise IcpInputError(f"{field_name} 배열 형상이 올바르지 않습니다.")
    if not np.isfinite(array).all():
        raise IcpInputError(f"{field_name} 배열에 유한하지 않은 값이 있습니다.")
    array.setflags(write=False)
    return array


def _freeze_uint64(value: object, shape: tuple[int | None, ...], field_name: str) -> np.ndarray:
    """샘플 결과에 쓸 uint64 배열만 복사해 읽기 전용으로 고정합니다."""

    source = np.asarray(value)
    if source.dtype.kind not in {"u", "i"} or (source.dtype.kind == "i" and np.any(source < 0)):
        raise IcpInputError(f"{field_name} 배열은 음수가 없는 정수 dtype이어야 합니다.")
    array = np.array(source, dtype=np.uint64, order="C", copy=True)
    if array.ndim != len(shape) or any(
        expected is not None and actual != expected
        for actual, expected in zip(array.shape, shape, strict=True)
    ):
        raise IcpInputError(f"{field_name} 배열 형상이 올바르지 않습니다.")
    array.setflags(write=False)
    return array


def _freeze_int64(value: object, shape: tuple[int | None, ...], field_name: str) -> np.ndarray:
    """샘플 결과에 쓸 int64 배열만 복사해 읽기 전용으로 고정합니다."""

    source = np.asarray(value)
    if source.dtype.kind not in {"u", "i"}:
        raise IcpInputError(f"{field_name} 배열은 정수 dtype이어야 합니다.")
    array = np.array(source, dtype=np.int64, order="C", copy=True)
    if array.ndim != len(shape) or any(
        expected is not None and actual != expected
        for actual, expected in zip(array.shape, shape, strict=True)
    ):
        raise IcpInputError(f"{field_name} 배열 형상이 올바르지 않습니다.")
    array.setflags(write=False)
    return array


def _raise_if_cancelled(cancel_probe: Callable[[], bool] | None) -> None:
    """chunk·탐색 반복 경계에서 worker 취소 요청을 전달합니다."""

    if cancel_probe is not None and bool(cancel_probe()):
        raise IcpCancelledError("사용자가 ICP 샘플 준비 취소를 요청했습니다.")


@dataclass(frozen=True, slots=True)
class VoxelSample:
    """복셀별 한 대표점과 원본 POINT3D_ID·인덱스를 보관합니다."""

    positions_float64: np.ndarray
    point_ids_uint64: np.ndarray
    source_indices_int64: np.ndarray
    voxel_size: float
    origin_float64: np.ndarray

    def __post_init__(self) -> None:
        """대표점·ID·복셀 메타데이터를 immutable 결과로 검증합니다."""

        positions = _freeze_float64(self.positions_float64, (None, 3), "복셀 샘플 좌표")
        ids = _freeze_uint64(self.point_ids_uint64, (positions.shape[0],), "복셀 샘플 POINT3D_ID")
        indices = _freeze_int64(self.source_indices_int64, (positions.shape[0],), "복셀 샘플 원본 인덱스")
        voxel_size = _strict_positive_float(self.voxel_size, "복셀 크기")
        origin = _freeze_float64(self.origin_float64, (3,), "복셀 샘플 원점")
        if positions.shape[0] == 0:
            raise IcpInputError("복셀 샘플은 비어 있을 수 없습니다.")
        if np.any(indices < 0) or np.unique(ids).size != ids.size:
            raise IcpInputError("복셀 샘플 ID 또는 원본 인덱스가 올바르지 않습니다.")
        object.__setattr__(self, "positions_float64", positions)
        object.__setattr__(self, "point_ids_uint64", ids)
        object.__setattr__(self, "source_indices_int64", indices)
        object.__setattr__(self, "voxel_size", voxel_size)
        object.__setattr__(self, "origin_float64", origin)

    @property
    def count(self) -> int:
        """대표점 개수를 반환합니다."""

        return int(self.positions_float64.shape[0])

    @classmethod
    def _from_verified_selection(
        cls,
        *,
        positions_float64: np.ndarray,
        point_ids_uint64: np.ndarray,
        source_indices_int64: np.ndarray,
        voxel_size: float,
        origin_float64: np.ndarray,
    ) -> "VoxelSample":
        """내부 sampler가 만든 읽기 전용 예산 규모 배열을 재복사 없이 결과로 감쌉니다."""

        if (
            positions_float64.dtype != np.dtype(np.float64)
            or positions_float64.ndim != 2
            or positions_float64.shape[1] != 3
            or not positions_float64.flags.c_contiguous
            or positions_float64.flags.writeable
            or point_ids_uint64.dtype != np.dtype(np.uint64)
            or point_ids_uint64.ndim != 1
            or not point_ids_uint64.flags.c_contiguous
            or point_ids_uint64.flags.writeable
            or source_indices_int64.dtype != np.dtype(np.int64)
            or source_indices_int64.ndim != 1
            or not source_indices_int64.flags.c_contiguous
            or source_indices_int64.flags.writeable
            or positions_float64.shape[0] == 0
            or positions_float64.shape[0] != point_ids_uint64.shape[0]
            or positions_float64.shape[0] != source_indices_int64.shape[0]
            or not np.isfinite(positions_float64).all()
            or np.any(source_indices_int64 < 0)
            or np.unique(point_ids_uint64).size != point_ids_uint64.size
        ):
            raise IcpInputError("내부 복셀 샘플 선택 결과가 올바르지 않습니다.")
        normalized_voxel = _strict_positive_float(voxel_size, "복셀 크기")
        normalized_origin = _freeze_float64(origin_float64, (3,), "복셀 샘플 원점")
        instance = object.__new__(cls)
        object.__setattr__(instance, "positions_float64", positions_float64)
        object.__setattr__(instance, "point_ids_uint64", point_ids_uint64)
        object.__setattr__(instance, "source_indices_int64", source_indices_int64)
        object.__setattr__(instance, "voxel_size", normalized_voxel)
        object.__setattr__(instance, "origin_float64", normalized_origin)
        return instance


def deterministic_point_hash(point_ids_uint64: object) -> np.ndarray:
    """POINT3D_ID마다 입력 순서와 무관한 SplitMix64 해시를 계산합니다."""

    source = np.asarray(point_ids_uint64)
    if source.ndim != 1 or source.dtype.kind not in {"u", "i"}:
        raise IcpInputError("POINT3D_ID 해시 입력은 1차원 정수 배열이어야 합니다.")
    if source.dtype.kind == "i" and np.any(source < 0):
        raise IcpInputError("POINT3D_ID 해시 입력은 음수일 수 없습니다.")
    values = np.array(source, dtype=np.uint64, order="C", copy=True)
    values = (values + _SPLITMIX_ADD) & _UINT64_MASK
    values = ((values ^ (values >> np.uint64(30))) * _SPLITMIX_MUL_1) & _UINT64_MASK
    values = ((values ^ (values >> np.uint64(27))) * _SPLITMIX_MUL_2) & _UINT64_MASK
    values = values ^ (values >> np.uint64(31))
    values.setflags(write=False)
    return values


def _validated_source_arrays(
    positions_float64: object,
    point_ids_uint64: object,
    *,
    validate_unique_ids: bool,
) -> tuple[np.ndarray, np.ndarray]:
    """공개 배열 API의 형상·유한성·ID 고유성을 한 번 검증합니다."""

    positions = np.asarray(positions_float64)
    point_ids = np.asarray(point_ids_uint64)
    if positions.dtype != np.dtype(np.float64):
        positions = np.asarray(positions, dtype=np.float64, order="C")
    if point_ids.dtype != np.dtype(np.uint64):
        if point_ids.dtype.kind not in {"u", "i"} or (point_ids.dtype.kind == "i" and np.any(point_ids < 0)):
            raise IcpInputError("복셀 입력 POINT3D_ID는 음수가 없는 정수 dtype이어야 합니다.")
        point_ids = np.asarray(point_ids, dtype=np.uint64, order="C")
    if positions.ndim != 2 or positions.shape[1] != 3 or point_ids.ndim != 1:
        raise IcpInputError("복셀 입력 좌표 또는 POINT3D_ID 배열 형상이 올바르지 않습니다.")
    if positions.shape[0] == 0 or point_ids.shape[0] != positions.shape[0]:
        raise IcpInputError("복셀 입력 점 수와 POINT3D_ID 수가 올바르지 않습니다.")
    if not np.isfinite(positions).all():
        raise IcpInputError("복셀 입력 좌표에 유한하지 않은 값이 있습니다.")
    if validate_unique_ids and np.unique(point_ids).size != point_ids.size:
        raise IcpInputError("복셀 입력 POINT3D_ID는 중복될 수 없습니다.")
    return positions, point_ids


def _normalize_candidate_indices(
    candidate_indices: object | None,
    point_count: int,
) -> np.ndarray | None:
    """선택적인 소규모 crop 인덱스를 검증해 읽기 전용으로 고정합니다."""

    if candidate_indices is None:
        return None
    indices = _freeze_int64(candidate_indices, (None,), "복셀 후보 인덱스")
    if indices.size == 0 or np.any(indices < 0) or np.any(indices >= point_count):
        raise IcpInputError("복셀 후보 인덱스 범위가 올바르지 않습니다.")
    if np.unique(indices).size != indices.size:
        raise IcpInputError("복셀 후보 인덱스는 중복될 수 없습니다.")
    return indices


def _normalize_candidate_bounds(
    candidate_bounds_float64: object | None,
) -> tuple[np.ndarray, np.ndarray] | None:
    """대용량 memmap crop용 닫힌 AABB를 작은 읽기 전용 배열로 검증합니다."""

    if candidate_bounds_float64 is None:
        return None
    try:
        minimum, maximum = candidate_bounds_float64  # type: ignore[misc]
    except (TypeError, ValueError) as error:
        raise IcpInputError("복셀 후보 AABB는 최소·최대 좌표 쌍이어야 합니다.") from error
    minimum_array = _freeze_float64(minimum, (3,), "복셀 후보 AABB 최소")
    maximum_array = _freeze_float64(maximum, (3,), "복셀 후보 AABB 최대")
    if np.any(minimum_array > maximum_array):
        raise IcpInputError("복셀 후보 AABB 최소값은 최대값보다 클 수 없습니다.")
    return minimum_array, maximum_array


def _normalize_chunk_size(chunk_size: object) -> int:
    """원본 memmap 순회에 쓸 제한된 chunk 크기를 검증합니다."""

    return _strict_positive_int(chunk_size, "ICP 샘플 chunk 크기")


def _iter_candidate_chunks(
    positions: np.ndarray,
    point_ids: np.ndarray,
    *,
    candidate_indices: np.ndarray | None,
    candidate_bounds: tuple[np.ndarray, np.ndarray] | None,
    chunk_size: int,
    cancel_probe: Callable[[], bool] | None,
) -> Iterator[tuple[np.ndarray, np.ndarray, np.ndarray]]:
    """원본 배열을 고정 크기로 읽어 crop을 적용한 점·ID·원본 인덱스를 반환합니다."""

    if candidate_indices is not None:
        for start in range(0, candidate_indices.size, chunk_size):
            _raise_if_cancelled(cancel_probe)
            indices = candidate_indices[start : start + chunk_size]
            yield positions[indices], point_ids[indices], indices
        return
    for start in range(0, positions.shape[0], chunk_size):
        _raise_if_cancelled(cancel_probe)
        stop = min(start + chunk_size, positions.shape[0])
        chunk_positions = positions[start:stop]
        chunk_ids = point_ids[start:stop]
        chunk_indices = np.arange(start, stop, dtype=np.int64)
        if candidate_bounds is not None:
            minimum, maximum = candidate_bounds
            mask = np.all(
                (chunk_positions >= minimum[np.newaxis, :])
                & (chunk_positions <= maximum[np.newaxis, :]),
                axis=1,
            )
            if not np.any(mask):
                continue
            chunk_positions = chunk_positions[mask]
            chunk_ids = chunk_ids[mask]
            chunk_indices = chunk_indices[mask]
        yield chunk_positions, chunk_ids, chunk_indices


def _candidate_statistics(
    positions: np.ndarray,
    point_ids: np.ndarray,
    *,
    candidate_indices: np.ndarray | None,
    candidate_bounds: tuple[np.ndarray, np.ndarray] | None,
    chunk_size: int,
    cancel_probe: Callable[[], bool] | None,
) -> tuple[int, np.ndarray, np.ndarray]:
    """crop 후보의 개수와 AABB만 chunk 순회로 계산합니다."""

    count = 0
    minimum: np.ndarray | None = None
    maximum: np.ndarray | None = None
    for chunk_positions, _chunk_ids, _chunk_indices in _iter_candidate_chunks(
        positions,
        point_ids,
        candidate_indices=candidate_indices,
        candidate_bounds=candidate_bounds,
        chunk_size=chunk_size,
        cancel_probe=cancel_probe,
    ):
        if chunk_positions.size == 0:
            continue
        count += int(chunk_positions.shape[0])
        current_minimum = np.min(chunk_positions, axis=0)
        current_maximum = np.max(chunk_positions, axis=0)
        minimum = current_minimum if minimum is None else np.minimum(minimum, current_minimum)
        maximum = current_maximum if maximum is None else np.maximum(maximum, current_maximum)
    if count == 0 or minimum is None or maximum is None:
        raise IcpInputError("복셀 후보 영역에 유효 점이 없습니다.")
    return count, np.asarray(minimum, dtype=np.float64), np.asarray(maximum, dtype=np.float64)


def _cell_keys_for_chunk(
    positions: np.ndarray,
    voxel_size: float,
    origin: np.ndarray,
) -> np.ndarray:
    """한 chunk의 local-origin int64 복셀 key를 계산합니다."""

    scaled = (positions - origin[np.newaxis, :]) / voxel_size
    if not np.isfinite(scaled).all() or np.any(np.abs(scaled) > (2**62 - 1)):
        raise IcpInputError("복셀 cell 좌표가 int64 범위를 벗어났습니다.")
    return np.floor(scaled).astype(np.int64, copy=False)


def _stable_unique_keys(keys: np.ndarray) -> np.ndarray:
    """한 chunk의 int64 cell key를 연속 배열 상태로 정렬·중복 제거합니다."""

    if keys.ndim != 2 or keys.shape[1] != 3:
        raise IcpInputError("복셀 cell key 배열 형상이 올바르지 않습니다.")
    if keys.shape[0] == 0:
        return np.empty((0, 3), dtype=np.int64)
    order = np.lexsort((keys[:, 2], keys[:, 1], keys[:, 0]))
    ordered = keys[order]
    first = np.empty(ordered.shape[0], dtype=bool)
    first[0] = True
    first[1:] = np.any(ordered[1:] != ordered[:-1], axis=1)
    return np.ascontiguousarray(ordered[first], dtype=np.int64)


def _merge_unique_keys(
    existing: np.ndarray,
    incoming: np.ndarray,
    *,
    maximum_count: int,
) -> np.ndarray:
    """정렬된 점유 cell 배열과 chunk cell 배열을 예산 경계 안에서 안정 병합합니다."""

    combined = np.concatenate((existing, incoming), axis=0)
    merged = _stable_unique_keys(combined)
    if merged.shape[0] > maximum_count:
        return merged[: maximum_count + 1]
    return merged


def _chunk_representatives(
    keys: np.ndarray,
    hashes: np.ndarray,
    point_ids: np.ndarray,
    source_indices: np.ndarray,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """한 chunk의 cell별 최소 hash 대표 메타데이터만 연속 배열로 축약합니다."""

    order = np.lexsort(
        (
            source_indices,
            point_ids,
            hashes,
            keys[:, 2],
            keys[:, 1],
            keys[:, 0],
        )
    )
    ordered_keys = keys[order]
    first = np.empty(order.shape[0], dtype=bool)
    first[0] = True
    first[1:] = np.any(ordered_keys[1:] != ordered_keys[:-1], axis=1)
    selected = order[first]
    return (
        np.ascontiguousarray(keys[selected], dtype=np.int64),
        np.ascontiguousarray(hashes[selected], dtype=np.uint64),
        np.ascontiguousarray(point_ids[selected], dtype=np.uint64),
        np.ascontiguousarray(source_indices[selected], dtype=np.int64),
    )


def _merge_representatives(
    existing: tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray],
    incoming: tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray],
    *,
    maximum_count: int | None,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """두 cell 대표 배열을 hash·ID·원본 index 우선순위로 안정 병합합니다."""

    keys = np.concatenate((existing[0], incoming[0]), axis=0)
    hashes = np.concatenate((existing[1], incoming[1]), axis=0)
    point_ids = np.concatenate((existing[2], incoming[2]), axis=0)
    source_indices = np.concatenate((existing[3], incoming[3]), axis=0)
    order = np.lexsort(
        (
            source_indices,
            point_ids,
            hashes,
            keys[:, 2],
            keys[:, 1],
            keys[:, 0],
        )
    )
    ordered_keys = keys[order]
    first = np.empty(order.shape[0], dtype=bool)
    first[0] = True
    first[1:] = np.any(ordered_keys[1:] != ordered_keys[:-1], axis=1)
    selected = order[first]
    if maximum_count is not None and selected.size > maximum_count:
        raise IcpInputError("복셀 대표점 수가 목표 예산을 초과했습니다.")
    return (
        np.ascontiguousarray(keys[selected], dtype=np.int64),
        np.ascontiguousarray(hashes[selected], dtype=np.uint64),
        np.ascontiguousarray(point_ids[selected], dtype=np.uint64),
        np.ascontiguousarray(source_indices[selected], dtype=np.int64),
    )


def voxel_cell_keys(
    positions_float64: object,
    voxel_size: float,
    *,
    origin_float64: object | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    """공개 소규모 배열 API에서 결정적 int64 복셀 cell key를 계산합니다."""

    positions = np.asarray(positions_float64, dtype=np.float64)
    if positions.ndim != 2 or positions.shape[1] != 3 or positions.shape[0] == 0 or not np.isfinite(positions).all():
        raise IcpInputError("복셀 입력 좌표는 비어 있지 않은 유한 float64 N×3이어야 합니다.")
    voxel = _strict_positive_float(voxel_size, "복셀 크기")
    origin = np.min(positions, axis=0) if origin_float64 is None else _freeze_float64(origin_float64, (3,), "복셀 원점")
    keys = _cell_keys_for_chunk(positions, voxel, np.asarray(origin, dtype=np.float64))
    frozen_keys = _freeze_int64(keys, (positions.shape[0], 3), "복셀 cell key")
    frozen_origin = _freeze_float64(origin, (3,), "복셀 원점")
    return frozen_keys, frozen_origin


def _minimum_voxel_size(minimum: np.ndarray, maximum: np.ndarray) -> float:
    """후보 AABB에서 안정적으로 사용할 수 있는 최소 양의 복셀 크기를 구합니다."""

    diagonal = float(np.linalg.norm(maximum - minimum))
    magnitude = max(1.0, float(np.max(np.abs(np.concatenate((minimum, maximum))))))
    return max(diagonal * 1.0e-12, np.spacing(magnitude) * 16.0)


def _count_voxels_capped(
    positions: np.ndarray,
    point_ids: np.ndarray,
    voxel_size: float,
    origin: np.ndarray,
    *,
    candidate_indices: np.ndarray | None,
    candidate_bounds: tuple[np.ndarray, np.ndarray] | None,
    chunk_size: int,
    maximum_count: int,
    cancel_probe: Callable[[], bool] | None,
) -> int:
    """연속 cell 배열을 budget+1까지만 병합해 원본 크기 메모리 사용을 막습니다."""

    occupied = np.empty((0, 3), dtype=np.int64)
    for chunk_positions, _chunk_ids, _chunk_indices in _iter_candidate_chunks(
        positions,
        point_ids,
        candidate_indices=candidate_indices,
        candidate_bounds=candidate_bounds,
        chunk_size=chunk_size,
        cancel_probe=cancel_probe,
    ):
        keys = _cell_keys_for_chunk(chunk_positions, voxel_size, origin)
        chunk_unique = _stable_unique_keys(keys)
        occupied = _merge_unique_keys(
            occupied,
            chunk_unique,
            maximum_count=maximum_count,
        )
        if occupied.shape[0] > maximum_count:
            return maximum_count + 1
    return int(occupied.shape[0])


def _choose_voxel_size_trusted(
    positions: np.ndarray,
    point_ids: np.ndarray,
    budget: int,
    *,
    origin_float64: object | None,
    candidate_indices: np.ndarray | None,
    candidate_bounds: tuple[np.ndarray, np.ndarray] | None,
    max_search_iterations: int,
    chunk_size: int,
    cancel_probe: Callable[[], bool] | None,
) -> tuple[float, np.ndarray]:
    """검증된 원본을 여러 번 chunk 순회해 예산 이하 최소 복셀과 공통 원점을 찾습니다."""

    candidate_count, minimum, maximum = _candidate_statistics(
        positions,
        point_ids,
        candidate_indices=candidate_indices,
        candidate_bounds=candidate_bounds,
        chunk_size=chunk_size,
        cancel_probe=cancel_probe,
    )
    origin = minimum if origin_float64 is None else _freeze_float64(origin_float64, (3,), "복셀 원점")
    lower = _minimum_voxel_size(minimum, maximum)
    if candidate_count <= budget:
        return lower, origin
    upper = max(float(np.linalg.norm(maximum - minimum)), lower * 2.0)
    upper_count = _count_voxels_capped(
        positions,
        point_ids,
        upper,
        origin,
        candidate_indices=candidate_indices,
        candidate_bounds=candidate_bounds,
        chunk_size=chunk_size,
        maximum_count=budget,
        cancel_probe=cancel_probe,
    )
    for _iteration in range(max_search_iterations):
        if upper_count <= budget:
            break
        _raise_if_cancelled(cancel_probe)
        upper *= 2.0
        upper_count = _count_voxels_capped(
            positions,
            point_ids,
            upper,
            origin,
            candidate_indices=candidate_indices,
            candidate_bounds=candidate_bounds,
            chunk_size=chunk_size,
            maximum_count=budget,
            cancel_probe=cancel_probe,
        )
    if upper_count > budget:
        raise IcpInputError("복셀 크기를 늘려도 샘플 예산을 만족하지 못했습니다.")
    best = upper
    for _iteration in range(max_search_iterations):
        _raise_if_cancelled(cancel_probe)
        middle = (lower + upper) * 0.5
        count = _count_voxels_capped(
            positions,
            point_ids,
            middle,
            origin,
            candidate_indices=candidate_indices,
            candidate_bounds=candidate_bounds,
            chunk_size=chunk_size,
            maximum_count=budget,
            cancel_probe=cancel_probe,
        )
        if count <= budget:
            best = middle
            upper = middle
        else:
            lower = middle
    return float(best), origin


def _select_voxel_representatives_trusted(
    positions: np.ndarray,
    point_ids: np.ndarray,
    voxel_size: float,
    origin: np.ndarray,
    *,
    candidate_indices: np.ndarray | None,
    candidate_bounds: tuple[np.ndarray, np.ndarray] | None,
    chunk_size: int,
    maximum_count: int | None,
    cancel_probe: Callable[[], bool] | None,
) -> VoxelSample:
    """cell별 대표 메타데이터만 연속 배열로 병합해 원본 점군 복사를 피합니다."""

    representatives = (
        np.empty((0, 3), dtype=np.int64),
        np.empty((0,), dtype=np.uint64),
        np.empty((0,), dtype=np.uint64),
        np.empty((0,), dtype=np.int64),
    )
    for chunk_positions, chunk_ids, chunk_indices in _iter_candidate_chunks(
        positions,
        point_ids,
        candidate_indices=candidate_indices,
        candidate_bounds=candidate_bounds,
        chunk_size=chunk_size,
        cancel_probe=cancel_probe,
    ):
        keys = _cell_keys_for_chunk(chunk_positions, voxel_size, origin)
        hashes = deterministic_point_hash(chunk_ids)
        representatives = _merge_representatives(
            representatives,
            _chunk_representatives(keys, hashes, chunk_ids, chunk_indices),
            maximum_count=maximum_count,
        )
    if representatives[0].shape[0] == 0:
        raise IcpInputError("복셀 후보 영역에 유효 대표점이 없습니다.")
    keys, _hashes, ids_result, source_indices = representatives
    stable_order = np.lexsort((ids_result, keys[:, 2], keys[:, 1], keys[:, 0]))
    ids_result = np.ascontiguousarray(ids_result[stable_order], dtype=np.uint64)
    source_indices = np.ascontiguousarray(source_indices[stable_order], dtype=np.int64)
    positions_result = np.ascontiguousarray(positions[source_indices], dtype=np.float64)
    positions_result.setflags(write=False)
    ids_result.setflags(write=False)
    source_indices.setflags(write=False)
    return VoxelSample._from_verified_selection(
        positions_float64=positions_result,
        point_ids_uint64=ids_result,
        source_indices_int64=source_indices,
        voxel_size=voxel_size,
        origin_float64=origin,
    )


def select_voxel_representatives(
    positions_float64: object,
    point_ids_uint64: object,
    voxel_size: float,
    *,
    origin_float64: object | None = None,
    candidate_indices: object | None = None,
    candidate_bounds_float64: object | None = None,
    chunk_size: int = _DEFAULT_CHUNK_SIZE,
    cancel_probe: Callable[[], bool] | None = None,
) -> VoxelSample:
    """공개 배열 API에서 각 cell의 결정적 대표점을 chunk 순회로 선택합니다."""

    positions, point_ids = _validated_source_arrays(
        positions_float64,
        point_ids_uint64,
        validate_unique_ids=True,
    )
    normalized_indices = _normalize_candidate_indices(candidate_indices, positions.shape[0])
    normalized_bounds = _normalize_candidate_bounds(candidate_bounds_float64)
    if normalized_indices is not None and normalized_bounds is not None:
        raise IcpInputError("복셀 후보 인덱스와 후보 AABB는 동시에 지정할 수 없습니다.")
    normalized_chunk_size = _normalize_chunk_size(chunk_size)
    _count, minimum, _maximum = _candidate_statistics(
        positions,
        point_ids,
        candidate_indices=normalized_indices,
        candidate_bounds=normalized_bounds,
        chunk_size=normalized_chunk_size,
        cancel_probe=cancel_probe,
    )
    origin = minimum if origin_float64 is None else _freeze_float64(origin_float64, (3,), "복셀 원점")
    return _select_voxel_representatives_trusted(
        positions,
        point_ids,
        _strict_positive_float(voxel_size, "복셀 크기"),
        origin,
        candidate_indices=normalized_indices,
        candidate_bounds=normalized_bounds,
        chunk_size=normalized_chunk_size,
        maximum_count=None,
        cancel_probe=cancel_probe,
    )


def choose_voxel_size_for_budget(
    positions_float64: object,
    point_ids_uint64: object,
    budget: int,
    *,
    origin_float64: object | None = None,
    candidate_indices: object | None = None,
    candidate_bounds_float64: object | None = None,
    max_search_iterations: int = 20,
    chunk_size: int = _DEFAULT_CHUNK_SIZE,
    cancel_probe: Callable[[], bool] | None = None,
) -> float:
    """공개 배열 API에서 예산 이하가 되는 최소 결정적 복셀 크기를 찾습니다."""

    positions, point_ids = _validated_source_arrays(
        positions_float64,
        point_ids_uint64,
        validate_unique_ids=True,
    )
    normalized_indices = _normalize_candidate_indices(candidate_indices, positions.shape[0])
    normalized_bounds = _normalize_candidate_bounds(candidate_bounds_float64)
    if normalized_indices is not None and normalized_bounds is not None:
        raise IcpInputError("복셀 후보 인덱스와 후보 AABB는 동시에 지정할 수 없습니다.")
    voxel_size, _origin = _choose_voxel_size_trusted(
        positions,
        point_ids,
        _strict_positive_int(budget, "복셀 샘플 예산"),
        origin_float64=origin_float64,
        candidate_indices=normalized_indices,
        candidate_bounds=normalized_bounds,
        max_search_iterations=_strict_positive_int(max_search_iterations, "복셀 탐색 반복 횟수"),
        chunk_size=_normalize_chunk_size(chunk_size),
        cancel_probe=cancel_probe,
    )
    return voxel_size


def build_budgeted_voxel_sample(
    cloud: IcpPointCloud,
    budget: int,
    *,
    origin_float64: object | None = None,
    candidate_indices: object | None = None,
    candidate_bounds_float64: object | None = None,
    chunk_size: int = _DEFAULT_CHUNK_SIZE,
    cancel_probe: Callable[[], bool] | None = None,
) -> VoxelSample:
    """검증된 원본 점군에서 예산 이하의 결정적 복셀 대표점만 생성합니다."""

    if not isinstance(cloud, IcpPointCloud):
        raise IcpInputError("ICP 복셀 샘플에는 IcpPointCloud가 필요합니다.")
    normalized_budget = _strict_positive_int(budget, "복셀 샘플 예산")
    normalized_indices = _normalize_candidate_indices(candidate_indices, cloud.count)
    normalized_bounds = _normalize_candidate_bounds(candidate_bounds_float64)
    if normalized_indices is not None and normalized_bounds is not None:
        raise IcpInputError("복셀 후보 인덱스와 후보 AABB는 동시에 지정할 수 없습니다.")
    normalized_chunk_size = _normalize_chunk_size(chunk_size)
    voxel_size, origin = _choose_voxel_size_trusted(
        cloud.positions_float64,
        cloud.point_ids_uint64,
        normalized_budget,
        origin_float64=origin_float64,
        candidate_indices=normalized_indices,
        candidate_bounds=normalized_bounds,
        max_search_iterations=20,
        chunk_size=normalized_chunk_size,
        cancel_probe=cancel_probe,
    )
    return _select_voxel_representatives_trusted(
        cloud.positions_float64,
        cloud.point_ids_uint64,
        voxel_size,
        origin,
        candidate_indices=normalized_indices,
        candidate_bounds=normalized_bounds,
        chunk_size=normalized_chunk_size,
        maximum_count=normalized_budget,
        cancel_probe=cancel_probe,
    )


def allocate_multi_reference_quotas(
    reference_counts: Mapping[str, int],
    overlap_cell_counts: Mapping[str, int],
    total_budget: int,
    *,
    minimum_quota: int = 3,
) -> dict[str, int]:
    """A 모델별 최소 quota와 overlap 비례 잔여 예산을 결정적으로 배분합니다."""

    if not reference_counts:
        raise IcpInputError("다중 A quota에는 기준 모델이 하나 이상 필요합니다.")
    normalized_budget = _strict_positive_int(total_budget, "다중 A quota 예산")
    normalized_minimum = _strict_positive_int(minimum_quota, "다중 A 최소 quota")
    model_ids = tuple(sorted(str(model_id).strip() for model_id in reference_counts))
    if any(not model_id for model_id in model_ids) or len(set(model_ids)) != len(model_ids):
        raise IcpInputError("다중 A quota 모델 ID가 올바르지 않습니다.")
    capacities: dict[str, int] = {}
    weights: dict[str, int] = {}
    for model_id in model_ids:
        capacity = _strict_positive_int(reference_counts[model_id], "다중 A 후보 점 수")
        raw_overlap = overlap_cell_counts.get(model_id, 0)
        if isinstance(raw_overlap, bool) or not isinstance(raw_overlap, (int, np.integer)):
            raise IcpInputError("다중 A overlap cell 수는 정수여야 합니다.")
        overlap = int(raw_overlap)
        if overlap < 0:
            raise IcpInputError("다중 A overlap cell 수는 음수일 수 없습니다.")
        capacities[model_id] = capacity
        weights[model_id] = overlap
    active_ids = tuple(model_id for model_id in model_ids if weights[model_id] > 0)
    if not active_ids:
        raise IcpInputError("다중 A quota에 배분할 겹침 후보가 없습니다.")
    mandatory = sum(min(capacities[model_id], normalized_minimum) for model_id in active_ids)
    if mandatory > normalized_budget:
        raise IcpInputError("다중 A 최소 quota 합이 전체 예산을 초과합니다.")
    quotas = {model_id: 0 for model_id in model_ids}
    for model_id in active_ids:
        quotas[model_id] = min(capacities[model_id], normalized_minimum)
    remaining = normalized_budget - sum(quotas.values())
    while remaining > 0:
        available = tuple(model_id for model_id in active_ids if quotas[model_id] < capacities[model_id])
        if not available:
            break
        total_weight = sum(weights[model_id] for model_id in available)
        distributed = 0
        for model_id in available:
            share = max(1, int(remaining * weights[model_id] / total_weight))
            addition = min(share, capacities[model_id] - quotas[model_id], remaining)
            quotas[model_id] += addition
            remaining -= addition
            distributed += addition
            if remaining == 0:
                break
        if distributed == 0:
            break
    return quotas


def build_multi_reference_samples(
    clouds: Sequence[IcpPointCloud],
    overlap_cell_counts: Mapping[str, int],
    total_budget: int,
    *,
    origin_float64: object | None = None,
    minimum_quota: int = 3,
    candidate_indices_by_model: Mapping[str, object] | None = None,
    candidate_bounds_by_model: Mapping[str, object] | None = None,
    chunk_size: int = _DEFAULT_CHUNK_SIZE,
    cancel_probe: Callable[[], bool] | None = None,
) -> tuple[tuple[VoxelSample, ...], dict[str, int]]:
    """선택된 여러 A 모델을 crop 용량·overlap 비율로 quota 배분해 샘플링합니다."""

    normalized_clouds = tuple(clouds)
    if not normalized_clouds or not all(isinstance(cloud, IcpPointCloud) for cloud in normalized_clouds):
        raise IcpInputError("다중 A 샘플에는 IcpPointCloud가 하나 이상 필요합니다.")
    if len({cloud.model_id for cloud in normalized_clouds}) != len(normalized_clouds):
        raise IcpInputError("다중 A 샘플 모델 ID가 중복됩니다.")
    if candidate_indices_by_model is not None and candidate_bounds_by_model is not None:
        raise IcpInputError("다중 A 후보 인덱스와 후보 AABB는 동시에 지정할 수 없습니다.")
    normalized_chunk_size = _normalize_chunk_size(chunk_size)
    by_id = {cloud.model_id: cloud for cloud in normalized_clouds}
    normalized_indices: dict[str, np.ndarray | None] = {}
    normalized_bounds: dict[str, tuple[np.ndarray, np.ndarray] | None] = {}
    candidate_counts: dict[str, int] = {}
    for model_id, cloud in by_id.items():
        indices = _normalize_candidate_indices(
            None if candidate_indices_by_model is None else candidate_indices_by_model.get(model_id),
            cloud.count,
        )
        bounds = _normalize_candidate_bounds(
            None if candidate_bounds_by_model is None else candidate_bounds_by_model.get(model_id)
        )
        normalized_indices[model_id] = indices
        normalized_bounds[model_id] = bounds
        if indices is not None:
            candidate_counts[model_id] = int(indices.size)
        elif bounds is None:
            candidate_counts[model_id] = cloud.count
        else:
            candidate_counts[model_id] = _candidate_statistics(
                cloud.positions_float64,
                cloud.point_ids_uint64,
                candidate_indices=None,
                candidate_bounds=bounds,
                chunk_size=normalized_chunk_size,
                cancel_probe=cancel_probe,
            )[0]
    quotas = allocate_multi_reference_quotas(
        candidate_counts,
        overlap_cell_counts,
        total_budget,
        minimum_quota=minimum_quota,
    )
    samples: list[VoxelSample] = []
    for model_id, quota in sorted(quotas.items()):
        if quota <= 0:
            continue
        _raise_if_cancelled(cancel_probe)
        samples.append(
            build_budgeted_voxel_sample(
                by_id[model_id],
                quota,
                origin_float64=origin_float64,
                candidate_indices=normalized_indices[model_id],
                candidate_bounds_float64=normalized_bounds[model_id],
                chunk_size=normalized_chunk_size,
                cancel_probe=cancel_probe,
            )
        )
    return tuple(samples), quotas


def choose_shared_voxel_size(
    moving_cloud: IcpPointCloud,
    reference_clouds: Sequence[IcpPointCloud],
    *,
    moving_budget: int,
    reference_budget: int,
    origin_float64: object,
    moving_candidate_bounds_float64: object | None = None,
    reference_candidate_bounds_by_model: Mapping[str, object] | None = None,
    chunk_size: int = _DEFAULT_CHUNK_SIZE,
    cancel_probe: Callable[[], bool] | None = None,
) -> float:
    """P와 선택 A가 같은 grid를 쓰도록 가장 보수적인 공통 voxel 크기를 고릅니다."""

    if not isinstance(moving_cloud, IcpPointCloud):
        raise IcpInputError("공통 voxel 선택에는 IcpPointCloud P가 필요합니다.")
    clouds = tuple(reference_clouds)
    if not clouds or not all(isinstance(cloud, IcpPointCloud) for cloud in clouds):
        raise IcpInputError("공통 voxel 선택에는 기준 A 점군이 하나 이상 필요합니다.")
    normalized_origin = _freeze_float64(origin_float64, (3,), "공통 voxel 원점")
    normalized_moving_budget = _strict_positive_int(moving_budget, "공통 voxel P 예산")
    normalized_reference_budget = _strict_positive_int(reference_budget, "공통 voxel A 예산")
    normalized_chunk_size = _normalize_chunk_size(chunk_size)
    moving_voxel = choose_voxel_size_for_budget(
        moving_cloud.positions_float64,
        moving_cloud.point_ids_uint64,
        normalized_moving_budget,
        origin_float64=normalized_origin,
        candidate_bounds_float64=moving_candidate_bounds_float64,
        chunk_size=normalized_chunk_size,
        cancel_probe=cancel_probe,
    )
    per_reference_budget = max(3, normalized_reference_budget // len(clouds))
    reference_voxels: list[float] = []
    for cloud in clouds:
        _raise_if_cancelled(cancel_probe)
        bounds = (
            None
            if reference_candidate_bounds_by_model is None
            else reference_candidate_bounds_by_model.get(cloud.model_id)
        )
        reference_voxels.append(
            choose_voxel_size_for_budget(
                cloud.positions_float64,
                cloud.point_ids_uint64,
                per_reference_budget,
                origin_float64=normalized_origin,
                candidate_bounds_float64=bounds,
                chunk_size=normalized_chunk_size,
                cancel_probe=cancel_probe,
            )
        )
    return float(max((moving_voxel, *reference_voxels)))


def allocate_macro_cell_quotas(
    macro_cell_counts: Mapping[tuple[int, int, int], int],
    total_budget: int,
    *,
    minimum_quota: int = 1,
) -> dict[tuple[int, int, int], int]:
    """점유 macro-cell마다 최소 quota 뒤 용량 비례 잔여 예산을 배분합니다."""

    normalized_budget = _strict_positive_int(total_budget, "macro-cell 전체 예산")
    normalized_minimum = _strict_positive_int(minimum_quota, "macro-cell 최소 quota")
    if not macro_cell_counts:
        raise IcpInputError("macro-cell quota에는 점유 cell이 하나 이상 필요합니다.")
    normalized: dict[tuple[int, int, int], int] = {}
    for raw_key, raw_count in macro_cell_counts.items():
        if not isinstance(raw_key, tuple) or len(raw_key) != 3:
            raise IcpInputError("macro-cell key는 3개 정수 좌표 튜플이어야 합니다.")
        if any(isinstance(item, bool) or not isinstance(item, (int, np.integer)) for item in raw_key):
            raise IcpInputError("macro-cell key는 bool이 아닌 정수여야 합니다.")
        # overlap 원점 밖의 점에도 안정적으로 대응하려고 음수 cell도 허용합니다.
        signed_key = tuple(int(item) for item in raw_key)
        count = _strict_positive_int(raw_count, "macro-cell 표본 수")
        normalized[signed_key] = count
    ordered_keys = tuple(sorted(normalized))
    mandatory = sum(min(normalized[key], normalized_minimum) for key in ordered_keys)
    if mandatory > normalized_budget:
        raise IcpInputError("macro-cell 최소 quota 합이 전체 예산을 초과합니다.")
    quotas = {key: min(normalized[key], normalized_minimum) for key in ordered_keys}
    remaining = normalized_budget - sum(quotas.values())
    while remaining > 0:
        available = tuple(key for key in ordered_keys if quotas[key] < normalized[key])
        if not available:
            break
        total_capacity = sum(normalized[key] - quotas[key] for key in available)
        distributed = 0
        for key in available:
            capacity = normalized[key] - quotas[key]
            addition = min(
                capacity,
                remaining,
                max(1, int(np.floor(remaining * capacity / max(total_capacity, 1)))),
            )
            quotas[key] += addition
            remaining -= addition
            distributed += addition
            if remaining == 0:
                break
        if distributed == 0:
            break
    return quotas


def _macro_cell_keys(
    positions: np.ndarray,
    *,
    minimum: np.ndarray,
    maximum: np.ndarray,
    divisions: int,
) -> np.ndarray:
    """overlap AABB 안 샘플 좌표를 고정 분할 macro-cell key로 변환합니다."""

    extent = maximum - minimum
    safe_extent = np.where(extent > np.finfo(np.float64).eps, extent, 1.0)
    scaled = (positions - minimum[np.newaxis, :]) / safe_extent[np.newaxis, :]
    keys = np.floor(np.clip(scaled, 0.0, 1.0 - np.finfo(np.float64).eps) * divisions).astype(np.int64)
    return np.ascontiguousarray(keys, dtype=np.int64)


def build_uniform_density_sample(
    cloud: IcpPointCloud,
    budget: int,
    *,
    shared_voxel_size: float,
    shared_origin_float64: object,
    overlap_bounds_float64: object,
    macro_cell_divisions: int,
    candidate_bounds_float64: object | None = None,
    chunk_size: int = _DEFAULT_CHUNK_SIZE,
    cancel_probe: Callable[[], bool] | None = None,
) -> VoxelSample:
    """공통 voxel 대표점에 간단한 macro-cell quota를 적용한 균일 표본을 만듭니다."""

    if not isinstance(cloud, IcpPointCloud):
        raise IcpInputError("균일 밀도 샘플에는 IcpPointCloud가 필요합니다.")
    normalized_budget = _strict_positive_int(budget, "균일 밀도 샘플 예산")
    normalized_voxel = _strict_positive_float(shared_voxel_size, "공통 voxel 크기")
    normalized_origin = _freeze_float64(shared_origin_float64, (3,), "공통 voxel 원점")
    normalized_divisions = _strict_positive_int(macro_cell_divisions, "macro-cell 분할 수")
    bounds = _normalize_candidate_bounds(overlap_bounds_float64)
    if bounds is None:
        raise IcpInputError("균일 밀도 샘플에는 overlap AABB가 필요합니다.")
    candidate_bounds = _normalize_candidate_bounds(candidate_bounds_float64)
    sample = _select_voxel_representatives_trusted(
        cloud.positions_float64,
        cloud.point_ids_uint64,
        normalized_voxel,
        normalized_origin,
        candidate_indices=None,
        candidate_bounds=candidate_bounds,
        chunk_size=_normalize_chunk_size(chunk_size),
        maximum_count=None,
        cancel_probe=cancel_probe,
    )
    if sample.count <= normalized_budget:
        return sample
    macro_keys = _macro_cell_keys(
        sample.positions_float64,
        minimum=bounds[0],
        maximum=bounds[1],
        divisions=normalized_divisions,
    )
    counts: dict[tuple[int, int, int], int] = {}
    for key in macro_keys:
        tuple_key = (int(key[0]), int(key[1]), int(key[2]))
        counts[tuple_key] = counts.get(tuple_key, 0) + 1
    quotas = allocate_macro_cell_quotas(counts, normalized_budget)
    # 대표점의 POINT3D_ID 정렬을 tie-break로 사용해 입력 순서에 영향을 받지 않게 합니다.
    order = np.lexsort(
        (
            sample.point_ids_uint64,
            macro_keys[:, 2],
            macro_keys[:, 1],
            macro_keys[:, 0],
        )
    )
    used = {key: 0 for key in quotas}
    selected: list[int] = []
    for source_index in order.tolist():
        key_values = macro_keys[source_index]
        key = (int(key_values[0]), int(key_values[1]), int(key_values[2]))
        if used[key] >= quotas[key]:
            continue
        used[key] += 1
        selected.append(source_index)
    selected_indices = np.asarray(selected, dtype=np.int64)
    positions = np.ascontiguousarray(sample.positions_float64[selected_indices], dtype=np.float64)
    point_ids = np.ascontiguousarray(sample.point_ids_uint64[selected_indices], dtype=np.uint64)
    source_indices = np.ascontiguousarray(sample.source_indices_int64[selected_indices], dtype=np.int64)
    positions.setflags(write=False)
    point_ids.setflags(write=False)
    source_indices.setflags(write=False)
    return VoxelSample._from_verified_selection(
        positions_float64=positions,
        point_ids_uint64=point_ids,
        source_indices_int64=source_indices,
        voxel_size=normalized_voxel,
        origin_float64=normalized_origin,
    )


__all__ = (
    "VoxelSample",
    "allocate_macro_cell_quotas",
    "allocate_multi_reference_quotas",
    "build_budgeted_voxel_sample",
    "build_multi_reference_samples",
    "build_uniform_density_sample",
    "choose_shared_voxel_size",
    "choose_voxel_size_for_budget",
    "deterministic_point_hash",
    "select_voxel_representatives",
    "voxel_cell_keys",
)
