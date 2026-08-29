"""ICP solver가 교체 가능한 최근접점 검색 인터페이스와 구현체를 제공합니다."""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from typing import Callable, Protocol, runtime_checkable

import numpy as np

from .icp_types import IcpCancelledError, IcpInputError


CancelProbe = Callable[[], bool] | None
"""반복 경계에서 취소를 확인하는 worker callback 타입입니다."""


def _freeze_positions(value: object, field_name: str) -> np.ndarray:
    """최근접점 검색에 쓸 유한 N×3 float64 입력을 읽기 전용 복사본으로 고정합니다."""

    array = np.array(value, dtype=np.float64, order="C", copy=True)
    if array.ndim != 2 or array.shape[1] != 3 or not np.isfinite(array).all():
        raise IcpInputError(f"{field_name} 좌표는 유한한 N×3 float64 배열이어야 합니다.")
    array.setflags(write=False)
    return array


def _validate_maximum_distance(value: float | None) -> float:
    """NN query의 선택적 최대 거리를 유한 양수 또는 무한대로 정규화합니다."""

    if value is None:
        return float("inf")
    if isinstance(value, bool) or not isinstance(value, (int, float, np.integer, np.floating)):
        raise IcpInputError("최근접점 최대 거리는 bool·문자열이 아닌 실수여야 합니다.")
    distance = float(value)
    if not (isfinite(distance) or np.isinf(distance)) or distance < 0.0:
        raise IcpInputError("최근접점 최대 거리는 0 이상이어야 합니다.")
    return distance


def _raise_if_cancelled(cancel_probe: CancelProbe) -> None:
    """취소 callback이 참이면 공통 ICP 취소 오류를 발생시킵니다."""

    if cancel_probe is not None and bool(cancel_probe()):
        raise IcpCancelledError("사용자가 ICP 계산 취소를 요청했습니다.")


@dataclass(frozen=True, slots=True)
class NearestNeighborQueryResult:
    """각 query의 target 인덱스와 float64 거리 결과를 보관합니다."""

    reference_indices_int64: np.ndarray
    distances_float64: np.ndarray

    def __post_init__(self) -> None:
        """유효/무효 query 표기를 포함한 최근접점 결과 배열을 검증합니다."""

        indices_source = np.asarray(self.reference_indices_int64)
        if indices_source.dtype.kind not in {"u", "i"}:
            raise IcpInputError("최근접점 target 인덱스는 정수 배열이어야 합니다.")
        indices = np.array(indices_source, dtype=np.int64, order="C", copy=True)
        distances = np.array(self.distances_float64, dtype=np.float64, order="C", copy=True)
        if indices.ndim != 1 or distances.shape != indices.shape:
            raise IcpInputError("최근접점 인덱스와 거리 배열 길이가 다릅니다.")
        valid = indices >= 0
        if np.any(indices < -1) or np.any(~np.isfinite(distances[valid])) or np.any(distances[valid] < 0.0):
            raise IcpInputError("최근접점 유효 결과 값이 올바르지 않습니다.")
        if np.any(valid == False) and not np.all(np.isinf(distances[~valid])):
            raise IcpInputError("최근접점 무효 결과 거리는 무한대여야 합니다.")
        indices.setflags(write=False)
        distances.setflags(write=False)
        object.__setattr__(self, "reference_indices_int64", indices)
        object.__setattr__(self, "distances_float64", distances)

    @property
    def query_count(self) -> int:
        """검색한 query 점 개수를 반환합니다."""

        return int(self.reference_indices_int64.size)

    @property
    def valid_mask(self) -> np.ndarray:
        """최대 거리 안에서 target을 찾은 query 마스크를 반환합니다."""

        mask = self.reference_indices_int64 >= 0
        mask.setflags(write=False)
        return mask


@dataclass(frozen=True, slots=True)
class RadiusNeighborQueryResult:
    """각 query의 반경 이웃 인덱스와 거리를 결정적 순서로 보관합니다."""

    indices_by_query: tuple[np.ndarray, ...]
    distances_by_query: tuple[np.ndarray, ...]

    def __post_init__(self) -> None:
        """이웃 배열 길이·dtype·거리 정렬을 읽기 전용 값으로 검증합니다."""

        indices_groups = tuple(self.indices_by_query)
        distances_groups = tuple(self.distances_by_query)
        if len(indices_groups) != len(distances_groups):
            raise IcpInputError("반경 이웃 인덱스와 거리 그룹 수가 다릅니다.")
        frozen_indices: list[np.ndarray] = []
        frozen_distances: list[np.ndarray] = []
        for indices_value, distances_value in zip(indices_groups, distances_groups, strict=True):
            indices_source = np.asarray(indices_value)
            if indices_source.dtype.kind not in {"u", "i"}:
                raise IcpInputError("반경 이웃 인덱스는 정수 배열이어야 합니다.")
            indices = np.array(indices_source, dtype=np.int64, order="C", copy=True)
            distances = np.array(distances_value, dtype=np.float64, order="C", copy=True)
            if (
                indices.ndim != 1
                or distances.shape != indices.shape
                or np.any(indices < 0)
                or not np.isfinite(distances).all()
                or np.any(distances < 0.0)
            ):
                raise IcpInputError("반경 이웃 배열 값이 올바르지 않습니다.")
            if indices.size > 1 and np.any(
                (distances[1:] < distances[:-1])
                | ((distances[1:] == distances[:-1]) & (indices[1:] < indices[:-1]))
            ):
                raise IcpInputError("반경 이웃은 거리·인덱스 순으로 정렬되어야 합니다.")
            indices.setflags(write=False)
            distances.setflags(write=False)
            frozen_indices.append(indices)
            frozen_distances.append(distances)
        object.__setattr__(self, "indices_by_query", tuple(frozen_indices))
        object.__setattr__(self, "distances_by_query", tuple(frozen_distances))

    @property
    def query_count(self) -> int:
        """반경 이웃을 요청한 점 개수를 반환합니다."""

        return len(self.indices_by_query)


@runtime_checkable
class NearestNeighborIndex(Protocol):
    """Blender 타입을 solver 밖으로 격리하는 최근접점 검색 Protocol입니다."""

    def build(
        self,
        reference_positions_float64: object,
        *,
        cancel_probe: CancelProbe = None,
    ) -> None:
        """한 해상도 레벨의 A 기준 점을 index에 준비합니다."""

    def query(
        self,
        query_positions_float64: object,
        *,
        maximum_distance: float | None = None,
        cancel_probe: CancelProbe = None,
    ) -> NearestNeighborQueryResult:
        """각 P query의 최근접 A target과 거리를 반환합니다."""

    def query_radius(
        self,
        query_positions_float64: object,
        *,
        radius: float,
        maximum_neighbors: int,
        cancel_probe: CancelProbe = None,
    ) -> RadiusNeighborQueryResult:
        """각 query의 반경 안 이웃을 거리·인덱스 순으로 반환합니다."""

    def close(self) -> None:
        """레벨 종료 후 index가 보관한 큰 기준 배열을 해제합니다."""


class BruteForceNearestNeighborIndex:
    """작은 synthetic 테스트용 NumPy 정확 최근접점 검색 구현체입니다."""

    def __init__(self, *, query_chunk_size: int = 2_048) -> None:
        """테스트 입력을 위한 제한된 chunk 크기를 검증합니다."""

        if isinstance(query_chunk_size, bool) or not isinstance(query_chunk_size, (int, np.integer)):
            raise IcpInputError("BruteForce NN query chunk 크기는 bool이 아닌 정수여야 합니다.")
        self._query_chunk_size = int(query_chunk_size)
        if self._query_chunk_size < 1:
            raise IcpInputError("BruteForce NN query chunk 크기는 양수여야 합니다.")
        self._reference_positions: np.ndarray | None = None

    def build(
        self,
        reference_positions_float64: object,
        *,
        cancel_probe: CancelProbe = None,
    ) -> None:
        """기준 A 점을 read-only float64 배열로 복사해 준비합니다."""

        _raise_if_cancelled(cancel_probe)
        reference = _freeze_positions(reference_positions_float64, "BruteForce 기준")
        if reference.shape[0] == 0:
            raise IcpInputError("BruteForce 최근접점 기준 점군은 비어 있을 수 없습니다.")
        self._reference_positions = reference
        _raise_if_cancelled(cancel_probe)

    def query(
        self,
        query_positions_float64: object,
        *,
        maximum_distance: float | None = None,
        cancel_probe: CancelProbe = None,
    ) -> NearestNeighborQueryResult:
        """NumPy 거리 행렬을 chunk별로 계산해 최근접 target을 반환합니다."""

        reference = self._reference_positions
        if reference is None:
            raise IcpInputError("BruteForce index를 build하기 전에 query할 수 없습니다.")
        query = _freeze_positions(query_positions_float64, "BruteForce query")
        maximum = _validate_maximum_distance(maximum_distance)
        indices = np.full(query.shape[0], -1, dtype=np.int64)
        distances = np.full(query.shape[0], np.inf, dtype=np.float64)
        maximum_squared = maximum * maximum
        for start in range(0, query.shape[0], self._query_chunk_size):
            _raise_if_cancelled(cancel_probe)
            stop = min(start + self._query_chunk_size, query.shape[0])
            difference = query[start:stop, np.newaxis, :] - reference[np.newaxis, :, :]
            squared = np.einsum("ijk,ijk->ij", difference, difference, optimize=True)
            local_indices = np.argmin(squared, axis=1)
            local_squared = squared[np.arange(stop - start), local_indices]
            valid = local_squared <= maximum_squared
            indices[start:stop][valid] = local_indices[valid]
            distances[start:stop][valid] = np.sqrt(local_squared[valid])
        _raise_if_cancelled(cancel_probe)
        return NearestNeighborQueryResult(indices, distances)

    def query_radius(
        self,
        query_positions_float64: object,
        *,
        radius: float,
        maximum_neighbors: int,
        cancel_probe: CancelProbe = None,
    ) -> RadiusNeighborQueryResult:
        """테스트용 NumPy 거리 행렬에서 반경 이웃을 결정적으로 반환합니다."""

        reference = self._reference_positions
        if reference is None:
            raise IcpInputError("BruteForce index를 build하기 전에 query할 수 없습니다.")
        query = _freeze_positions(query_positions_float64, "BruteForce 반경 query")
        normalized_radius = _validate_maximum_distance(radius)
        if not np.isfinite(normalized_radius) or normalized_radius <= 0.0:
            raise IcpInputError("반경 이웃 검색 반경은 유한한 양수여야 합니다.")
        if isinstance(maximum_neighbors, bool) or not isinstance(maximum_neighbors, (int, np.integer)):
            raise IcpInputError("반경 이웃 최대 수는 bool이 아닌 정수여야 합니다.")
        normalized_maximum = int(maximum_neighbors)
        if normalized_maximum < 1:
            raise IcpInputError("반경 이웃 최대 수는 1 이상이어야 합니다.")
        groups_indices: list[np.ndarray] = []
        groups_distances: list[np.ndarray] = []
        radius_squared = normalized_radius * normalized_radius
        for start in range(0, query.shape[0], self._query_chunk_size):
            _raise_if_cancelled(cancel_probe)
            stop = min(start + self._query_chunk_size, query.shape[0])
            difference = query[start:stop, np.newaxis, :] - reference[np.newaxis, :, :]
            squared = np.einsum("ijk,ijk->ij", difference, difference, optimize=True)
            for row in squared:
                indices = np.flatnonzero(row <= radius_squared).astype(np.int64, copy=False)
                distances = np.sqrt(row[indices])
                order = np.lexsort((indices, distances))[:normalized_maximum]
                groups_indices.append(indices[order])
                groups_distances.append(distances[order])
        _raise_if_cancelled(cancel_probe)
        return RadiusNeighborQueryResult(tuple(groups_indices), tuple(groups_distances))

    def close(self) -> None:
        """기준 점 참조를 제거해 레벨 메모리를 해제합니다."""

        self._reference_positions = None


class MathutilsKdTreeIndex:
    """Blender worker의 `mathutils.kdtree.KDTree`를 쓰는 실제 NN 구현체입니다."""

    def __init__(self, *, query_chunk_size: int = 2_048) -> None:
        """worker 취소 응답성을 위한 query chunk 크기를 검증합니다."""

        if isinstance(query_chunk_size, bool) or not isinstance(query_chunk_size, (int, np.integer)):
            raise IcpInputError("KD-tree query chunk 크기는 bool이 아닌 정수여야 합니다.")
        self._query_chunk_size = int(query_chunk_size)
        if self._query_chunk_size < 1:
            raise IcpInputError("KD-tree query chunk 크기는 양수여야 합니다.")
        self._tree: object | None = None
        self._reference_positions: np.ndarray | None = None

    def build(
        self,
        reference_positions_float64: object,
        *,
        cancel_probe: CancelProbe = None,
    ) -> None:
        """mathutils KDTree에 공통 local 좌표의 A 기준 점을 한 번 삽입합니다."""

        _raise_if_cancelled(cancel_probe)
        reference = _freeze_positions(reference_positions_float64, "KD-tree 기준")
        if reference.shape[0] == 0:
            raise IcpInputError("KD-tree 최근접점 기준 점군은 비어 있을 수 없습니다.")
        try:
            from mathutils.kdtree import KDTree
        except ImportError as error:
            raise IcpInputError("현재 Python 환경에서 mathutils.kdtree를 사용할 수 없습니다.") from error
        tree = KDTree(reference.shape[0])
        for index, position in enumerate(reference):
            if index % self._query_chunk_size == 0:
                _raise_if_cancelled(cancel_probe)
            tree.insert((float(position[0]), float(position[1]), float(position[2])), index)
        _raise_if_cancelled(cancel_probe)
        tree.balance()
        _raise_if_cancelled(cancel_probe)
        self._tree = tree
        self._reference_positions = reference

    def query(
        self,
        query_positions_float64: object,
        *,
        maximum_distance: float | None = None,
        cancel_probe: CancelProbe = None,
    ) -> NearestNeighborQueryResult:
        """KD-tree 결과의 거리를 NumPy float64로 재계산해 정밀도를 고정합니다."""

        tree = self._tree
        reference = self._reference_positions
        if tree is None or reference is None:
            raise IcpInputError("KD-tree index를 build하기 전에 query할 수 없습니다.")
        query = _freeze_positions(query_positions_float64, "KD-tree query")
        maximum = _validate_maximum_distance(maximum_distance)
        indices = np.full(query.shape[0], -1, dtype=np.int64)
        distances = np.full(query.shape[0], np.inf, dtype=np.float64)
        for start in range(0, query.shape[0], self._query_chunk_size):
            _raise_if_cancelled(cancel_probe)
            stop = min(start + self._query_chunk_size, query.shape[0])
            for index in range(start, stop):
                position = query[index]
                found = tree.find((float(position[0]), float(position[1]), float(position[2])))
                if found is None:
                    continue
                _coordinate, reference_index, _distance = found
                difference = position - reference[int(reference_index)]
                distance = float(np.sqrt(np.dot(difference, difference)))
                if distance <= maximum:
                    indices[index] = int(reference_index)
                    distances[index] = distance
        _raise_if_cancelled(cancel_probe)
        return NearestNeighborQueryResult(indices, distances)

    def query_radius(
        self,
        query_positions_float64: object,
        *,
        radius: float,
        maximum_neighbors: int,
        cancel_probe: CancelProbe = None,
    ) -> RadiusNeighborQueryResult:
        """KD-tree의 find_range 결과를 float64 거리·인덱스 순으로 고정합니다."""

        tree = self._tree
        reference = self._reference_positions
        if tree is None or reference is None:
            raise IcpInputError("KD-tree index를 build하기 전에 반경 query할 수 없습니다.")
        query = _freeze_positions(query_positions_float64, "KD-tree 반경 query")
        normalized_radius = _validate_maximum_distance(radius)
        if not np.isfinite(normalized_radius) or normalized_radius <= 0.0:
            raise IcpInputError("반경 이웃 검색 반경은 유한한 양수여야 합니다.")
        if isinstance(maximum_neighbors, bool) or not isinstance(maximum_neighbors, (int, np.integer)):
            raise IcpInputError("반경 이웃 최대 수는 bool이 아닌 정수여야 합니다.")
        normalized_maximum = int(maximum_neighbors)
        if normalized_maximum < 1:
            raise IcpInputError("반경 이웃 최대 수는 1 이상이어야 합니다.")
        groups_indices: list[np.ndarray] = []
        groups_distances: list[np.ndarray] = []
        for start in range(0, query.shape[0], self._query_chunk_size):
            _raise_if_cancelled(cancel_probe)
            stop = min(start + self._query_chunk_size, query.shape[0])
            for index in range(start, stop):
                position = query[index]
                found = tree.find_range(
                    (float(position[0]), float(position[1]), float(position[2])),
                    normalized_radius,
                )
                items: list[tuple[float, int]] = []
                for _coordinate, reference_index, _distance in found:
                    difference = position - reference[int(reference_index)]
                    distance = float(np.sqrt(np.dot(difference, difference)))
                    if distance <= normalized_radius:
                        items.append((distance, int(reference_index)))
                items.sort(key=lambda item: (item[0], item[1]))
                selected = items[:normalized_maximum]
                groups_indices.append(np.asarray([item[1] for item in selected], dtype=np.int64))
                groups_distances.append(np.asarray([item[0] for item in selected], dtype=np.float64))
        _raise_if_cancelled(cancel_probe)
        return RadiusNeighborQueryResult(tuple(groups_indices), tuple(groups_distances))

    def close(self) -> None:
        """KD-tree와 기준 배열 참조를 제거해 해상도 레벨 메모리를 해제합니다."""

        self._tree = None
        self._reference_positions = None


def default_nearest_neighbor_index(*, query_chunk_size: int = 2_048) -> NearestNeighborIndex:
    """실제 ICP 실행에는 Blender KD-tree만 생성하고 부재 시 명시적으로 실패합니다."""

    try:
        from mathutils.kdtree import KDTree  # noqa: F401
    except ImportError as error:
        raise IcpInputError(
            "실제 ICP 실행에는 Blender mathutils.kdtree.KDTree가 필요합니다. "
            "BruteForceNearestNeighborIndex는 테스트에서만 명시적으로 주입할 수 있습니다."
        ) from error
    return MathutilsKdTreeIndex(query_chunk_size=query_chunk_size)


__all__ = (
    "BruteForceNearestNeighborIndex",
    "CancelProbe",
    "MathutilsKdTreeIndex",
    "NearestNeighborIndex",
    "NearestNeighborQueryResult",
    "RadiusNeighborQueryResult",
    "default_nearest_neighbor_index",
)
