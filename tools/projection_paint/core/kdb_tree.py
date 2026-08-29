"""고정 높이 KDB-tree 구축과 정확한 최근접 Point 검색을 제공합니다."""

from __future__ import annotations

from dataclasses import dataclass
from heapq import heappop, heappush
from math import ceil, isfinite, sqrt
from typing import Iterable


Point3 = tuple[float, float, float]


class KDBTreeError(ValueError):
    """KDB-tree 입력 또는 구조 불변식 위반을 나타냅니다."""


@dataclass(frozen=True, slots=True)
class KDBSourcePoint:
    """KDB-tree에 넣을 3차원 위치와 외부 Record 식별자를 보관합니다."""

    position: Point3
    record_index: int
    tie_break: int


@dataclass(frozen=True, slots=True)
class KDBRecord:
    """동일한 위치를 공유하는 한 외부 Record의 식별자를 보관합니다."""

    record_index: int
    tie_break: int


@dataclass(frozen=True, slots=True)
class KDBPointEntry:
    """한 고유 위치와 그 위치를 공유하는 정렬된 Record를 보관합니다."""

    position: Point3
    records: tuple[KDBRecord, ...]


@dataclass(frozen=True, slots=True)
class AxisAlignedRegion:
    """KDB Page가 담당하는 3차원 축 정렬 경계 상자를 보관합니다."""

    lower: Point3
    upper: Point3

    def contains(self, point: Point3, tolerance: float = 1.0e-12) -> bool:
        """Point가 허용 오차 안에서 Region 경계에 포함되는지 확인합니다."""

        return all(
            self.lower[axis] - tolerance
            <= point[axis]
            <= self.upper[axis] + tolerance
            for axis in range(3)
        )

    def minimum_distance_squared(self, point: Point3) -> float:
        """Point에서 Region까지 가능한 최소 제곱 거리를 반환합니다."""

        distance = 0.0
        for axis in range(3):
            if point[axis] < self.lower[axis]:
                delta = self.lower[axis] - point[axis]
            elif point[axis] > self.upper[axis]:
                delta = point[axis] - self.upper[axis]
            else:
                delta = 0.0
            distance += delta * delta
        return distance


@dataclass(frozen=True, slots=True)
class KDBPointPage:
    """KDB-tree의 동일 깊이 Leaf에 고유 Point 항목을 보관합니다."""

    entries: tuple[KDBPointEntry, ...]


@dataclass(frozen=True, slots=True)
class KDBRegionEntry:
    """한 비중첩 공간 Region과 그 Region을 담당하는 자식 Page를 연결합니다."""

    region: AxisAlignedRegion
    child: KDBPointPage | KDBRegionPage


@dataclass(frozen=True, slots=True)
class KDBRegionPage:
    """KDB-tree 내부 Page의 Region과 자식 Page 연결을 보관합니다."""

    entries: tuple[KDBRegionEntry, ...]


@dataclass(frozen=True, slots=True)
class KDBTree:
    """불변 Root와 Page 용량 및 Point 수를 가진 메모리 상주형 KDB-tree입니다."""

    root: KDBPointPage | KDBRegionPage
    root_region: AxisAlignedRegion
    height: int
    point_page_capacity: int
    region_page_capacity: int
    point_count: int
    unique_position_count: int


@dataclass(frozen=True, slots=True)
class KDBNearest:
    """정확한 최근접 검색으로 찾은 외부 Record와 거리를 보관합니다."""

    position: Point3
    record_index: int
    tie_break: int
    distance: float


def _normalized_point(value: Iterable[float]) -> Point3:
    """입력 좌표를 유한한 3차원 float 튜플로 검증해 반환합니다."""

    point = tuple(float(component) for component in value)
    if len(point) != 3 or not all(isfinite(component) for component in point):
        raise KDBTreeError("KDB-tree 좌표는 유한한 3차원 값이어야 합니다.")
    return point


def _normalized_sources(points: Iterable[KDBSourcePoint]) -> tuple[KDBSourcePoint, ...]:
    """입력 Point를 정규화하고 Record Index 중복을 거부합니다."""

    normalized = tuple(
        KDBSourcePoint(
            position=_normalized_point(point.position),
            record_index=int(point.record_index),
            tie_break=int(point.tie_break),
        )
        for point in points
    )
    if not normalized:
        raise KDBTreeError("KDB-tree를 구축할 Point가 없습니다.")
    indices = tuple(point.record_index for point in normalized)
    if len(set(indices)) != len(indices):
        raise KDBTreeError("KDB-tree Record Index가 중복되었습니다.")
    return normalized


def _point_entries(points: tuple[KDBSourcePoint, ...]) -> tuple[KDBPointEntry, ...]:
    """동일 위치 Point를 하나의 Entry로 묶고 Record를 결정적으로 정렬합니다."""

    grouped: dict[Point3, list[KDBRecord]] = {}
    for point in points:
        grouped.setdefault(point.position, []).append(
            KDBRecord(point.record_index, point.tie_break)
        )
    return tuple(
        KDBPointEntry(
            position=position,
            records=tuple(
                sorted(records, key=lambda record: (record.tie_break, record.record_index))
            ),
        )
        for position, records in sorted(grouped.items())
    )


def _region_for_points(entries: tuple[KDBPointEntry, ...]) -> AxisAlignedRegion:
    """Point Entry 집합을 모두 포함하는 최소 축 정렬 Region을 반환합니다."""

    if not entries:
        raise KDBTreeError("빈 Point 집합의 Region을 만들 수 없습니다.")
    return AxisAlignedRegion(
        lower=tuple(min(entry.position[axis] for entry in entries) for axis in range(3)),
        upper=tuple(max(entry.position[axis] for entry in entries) for axis in range(3)),
    )


def _tree_height(point_count: int, point_capacity: int, region_capacity: int) -> int:
    """모든 Point를 담는 최소 고정 Leaf 깊이를 계산합니다."""

    height = 0
    capacity = int(point_capacity)
    while capacity < point_count:
        capacity *= int(region_capacity)
        height += 1
    return height


def _widest_axis(entries: tuple[KDBPointEntry, ...]) -> int:
    """Point 분포 폭이 가장 큰 축을 결정적 순서로 선택합니다."""

    region = _region_for_points(entries)
    widths = tuple(region.upper[axis] - region.lower[axis] for axis in range(3))
    return max(range(3), key=lambda axis: (widths[axis], -axis))


def _balanced_chunks(
    entries: tuple[KDBPointEntry, ...],
    child_count: int,
) -> tuple[tuple[KDBPointEntry, ...], ...]:
    """Point를 선택 축과 Record 기준으로 균등한 연속 묶음으로 분할합니다."""

    axis = _widest_axis(entries)
    ordered = tuple(
        sorted(
            entries,
            key=lambda entry: (
                entry.position[axis],
                entry.position,
                entry.records[0].tie_break,
                entry.records[0].record_index,
            ),
        )
    )
    quotient, remainder = divmod(len(ordered), int(child_count))
    chunks: list[tuple[KDBPointEntry, ...]] = []
    offset = 0
    for child_index in range(int(child_count)):
        size = quotient + (1 if child_index < remainder else 0)
        chunks.append(ordered[offset : offset + size])
        offset += size
    return tuple(chunks)


def _build_page(
    entries: tuple[KDBPointEntry, ...],
    depth: int,
    height: int,
    point_capacity: int,
    region_capacity: int,
) -> KDBPointPage | KDBRegionPage:
    """정해진 Leaf 깊이와 Page 용량을 지키는 KDB Page를 재귀 구축합니다."""

    if depth == height:
        if len(entries) > point_capacity:
            raise KDBTreeError("KDB Point Page 용량을 초과했습니다.")
        return KDBPointPage(entries=tuple(sorted(entries, key=lambda entry: entry.position)))
    remaining_region_levels = height - depth - 1
    child_capacity = point_capacity * (region_capacity**remaining_region_levels)
    child_count = max(1, int(ceil(len(entries) / child_capacity)))
    if child_count > region_capacity:
        raise KDBTreeError("KDB Region Page 용량을 초과했습니다.")
    children = tuple(
        KDBRegionEntry(
            region=_region_for_points(chunk),
            child=_build_page(
                chunk,
                depth + 1,
                height,
                point_capacity,
                region_capacity,
            ),
        )
        for chunk in _balanced_chunks(entries, child_count)
    )
    return KDBRegionPage(entries=children)


def _regions_overlap_interior(left: AxisAlignedRegion, right: AxisAlignedRegion) -> bool:
    """두 Region의 내부 부피가 실제로 겹치는지 확인합니다."""

    return all(
        min(left.upper[axis], right.upper[axis])
        > max(left.lower[axis], right.lower[axis]) + 1.0e-12
        for axis in range(3)
    )


def _page_points(page: KDBPointPage | KDBRegionPage) -> tuple[KDBPointEntry, ...]:
    """Page 하위의 모든 고유 Point Entry를 결정적 순서로 반환합니다."""

    if isinstance(page, KDBPointPage):
        return page.entries
    return tuple(point for entry in page.entries for point in _page_points(entry.child))


def _validate_page(
    tree: KDBTree,
    page: KDBPointPage | KDBRegionPage,
    depth: int,
    leaf_depths: list[int],
    records: list[int],
) -> None:
    """한 Page 아래의 용량·영역·Leaf 깊이·Record 유일성을 검증합니다."""

    if isinstance(page, KDBPointPage):
        if not page.entries or len(page.entries) > tree.point_page_capacity:
            raise KDBTreeError("KDB Point Page 용량 불변식이 깨졌습니다.")
        leaf_depths.append(depth)
        records.extend(
            record.record_index for entry in page.entries for record in entry.records
        )
        return
    if not page.entries or len(page.entries) > tree.region_page_capacity:
        raise KDBTreeError("KDB Region Page 용량 불변식이 깨졌습니다.")
    for index, entry in enumerate(page.entries):
        child_points = _page_points(entry.child)
        if not child_points or any(
            not entry.region.contains(point.position) for point in child_points
        ):
            raise KDBTreeError("KDB Region이 자식 Point를 완전히 포함하지 않습니다.")
        for sibling in page.entries[index + 1 :]:
            if _regions_overlap_interior(entry.region, sibling.region):
                raise KDBTreeError("같은 KDB Region Page의 자식 영역이 겹칩니다.")
        _validate_page(tree, entry.child, depth + 1, leaf_depths, records)


def validate_kdb_tree(tree: KDBTree) -> None:
    """완성된 KDB-tree의 전체 구조 불변식을 검증합니다."""

    leaf_depths: list[int] = []
    records: list[int] = []
    _validate_page(tree, tree.root, 0, leaf_depths, records)
    if set(leaf_depths) != {tree.height}:
        raise KDBTreeError("KDB-tree의 Leaf 깊이가 균형을 이루지 않습니다.")
    if len(records) != tree.point_count or len(set(records)) != tree.point_count:
        raise KDBTreeError("KDB-tree Record가 누락되거나 중복되었습니다.")
    points = _page_points(tree.root)
    if len(points) != tree.unique_position_count:
        raise KDBTreeError("KDB-tree 고유 Point 수가 일치하지 않습니다.")
    if any(not tree.root_region.contains(entry.position) for entry in points):
        raise KDBTreeError("KDB-tree Root Region이 모든 Point를 포함하지 않습니다.")


def build_kdb_tree(
    points: Iterable[KDBSourcePoint],
    point_page_capacity: int = 16,
    region_page_capacity: int = 8,
) -> KDBTree:
    """입력 순서와 무관한 고정 높이 KDB-tree를 구축하고 검증합니다."""

    point_capacity = int(point_page_capacity)
    region_capacity = int(region_page_capacity)
    if point_capacity < 1 or region_capacity < 2:
        raise KDBTreeError("KDB Page 용량은 Point 1 이상, Region 2 이상이어야 합니다.")
    sources = _normalized_sources(points)
    entries = _point_entries(sources)
    height = _tree_height(len(entries), point_capacity, region_capacity)
    tree = KDBTree(
        root=_build_page(entries, 0, height, point_capacity, region_capacity),
        root_region=_region_for_points(entries),
        height=height,
        point_page_capacity=point_capacity,
        region_page_capacity=region_capacity,
        point_count=len(sources),
        unique_position_count=len(entries),
    )
    validate_kdb_tree(tree)
    return tree


def _distance_squared(left: Point3, right: Point3) -> float:
    """두 3차원 Point 사이의 제곱 거리를 반환합니다."""

    return sum((left[axis] - right[axis]) ** 2 for axis in range(3))


def _push_best(
    heap: list[tuple[float, int, int, KDBNearest]],
    candidate: KDBNearest,
    count: int,
) -> None:
    """가까운 Record 최대 요청 수만 남도록 최대 거리 Heap을 갱신합니다."""

    distance_squared = candidate.distance * candidate.distance
    item = (-distance_squared, -candidate.tie_break, -candidate.record_index, candidate)
    if len(heap) < count:
        heappush(heap, item)
        return
    worst_key = (-heap[0][0], -heap[0][1], -heap[0][2])
    candidate_key = (distance_squared, candidate.tie_break, candidate.record_index)
    if candidate_key < worst_key:
        heappop(heap)
        heappush(heap, item)


def find_nearest(tree: KDBTree, origin: Iterable[float], count: int = 8) -> tuple[KDBNearest, ...]:
    """Region 최소 거리 우선 탐색으로 정확한 최근접 Record를 반환합니다."""

    query = _normalized_point(origin)
    requested = max(1, min(int(count), tree.point_count))
    frontier: list[tuple[float, int, KDBPointPage | KDBRegionPage]] = []
    best: list[tuple[float, int, int, KDBNearest]] = []
    serial = 0
    heappush(frontier, (tree.root_region.minimum_distance_squared(query), serial, tree.root))
    while frontier:
        lower_bound, _page_serial, page = heappop(frontier)
        if len(best) == requested and lower_bound > -best[0][0] + 1.0e-12:
            break
        if isinstance(page, KDBRegionPage):
            for entry in page.entries:
                serial += 1
                heappush(
                    frontier,
                    (entry.region.minimum_distance_squared(query), serial, entry.child),
                )
            continue
        for point_entry in page.entries:
            distance = sqrt(_distance_squared(point_entry.position, query))
            for record in point_entry.records:
                _push_best(
                    best,
                    KDBNearest(
                        position=point_entry.position,
                        record_index=record.record_index,
                        tie_break=record.tie_break,
                        distance=distance,
                    ),
                    requested,
                )
    return tuple(
        sorted(
            (item[3] for item in best),
            key=lambda result: (result.distance, result.tie_break, result.record_index),
        )
    )


__all__ = [
    "AxisAlignedRegion",
    "KDBNearest",
    "KDBPointEntry",
    "KDBPointPage",
    "KDBRecord",
    "KDBRegionEntry",
    "KDBRegionPage",
    "KDBSourcePoint",
    "KDBTree",
    "KDBTreeError",
    "build_kdb_tree",
    "find_nearest",
    "validate_kdb_tree",
]
