"""자동 AABB·점유 그리드 기반 ICP 겹침 후보 판정을 제공합니다."""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from typing import Callable, Iterator, Mapping, Sequence

import numpy as np

from .icp_sampling import voxel_cell_keys
from .icp_types import IcpCancelledError, IcpInputError, IcpPointCloud
from .types import Sim3Transform


_DEFAULT_CHUNK_SIZE = 65_536


def _strict_finite_float(value: object, field_name: str, *, minimum: float = 0.0) -> float:
    """bool·문자열 암묵 변환 없이 유한한 실수 범위를 검증합니다."""

    if isinstance(value, bool) or not isinstance(value, (int, float, np.integer, np.floating)):
        raise IcpInputError(f"{field_name}은 bool·문자열이 아닌 실수여야 합니다.")
    normalized = float(value)
    if not isfinite(normalized) or normalized < minimum:
        raise IcpInputError(f"{field_name} 범위가 올바르지 않습니다.")
    return normalized


def _strict_positive_int(value: object, field_name: str) -> int:
    """bool·문자열 암묵 변환 없이 양의 정수 설정을 검증합니다."""

    if isinstance(value, bool) or not isinstance(value, (int, np.integer)):
        raise IcpInputError(f"{field_name}은 bool이 아닌 정수여야 합니다.")
    normalized = int(value)
    if normalized < 1:
        raise IcpInputError(f"{field_name}은 1 이상이어야 합니다.")
    return normalized


def _freeze_float64(value: object, shape: tuple[int | None, ...], field_name: str) -> np.ndarray:
    """겹침 계산 결과에 쓸 유한 float64 배열을 읽기 전용 복사본으로 고정합니다."""

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


def _raise_if_cancelled(cancel_probe: Callable[[], bool] | None) -> None:
    """후보 모델 경계에서 worker 취소 요청을 ICP 취소 오류로 전달합니다."""

    if cancel_probe is not None and bool(cancel_probe()):
        raise IcpCancelledError("사용자가 ICP 겹침 후보 계산 취소를 요청했습니다.")


@dataclass(frozen=True, slots=True)
class AxisAlignedBounds:
    """Project World에서 사용하는 유한한 axis-aligned 3차원 경계입니다."""

    minimum_float64: np.ndarray
    maximum_float64: np.ndarray

    def __post_init__(self) -> None:
        """최소·최대 경계의 형상·유한성·순서를 검증합니다."""

        minimum = _freeze_float64(self.minimum_float64, (3,), "AABB 최소")
        maximum = _freeze_float64(self.maximum_float64, (3,), "AABB 최대")
        if np.any(minimum > maximum):
            raise IcpInputError("AABB 최소값은 최대값보다 클 수 없습니다.")
        object.__setattr__(self, "minimum_float64", minimum)
        object.__setattr__(self, "maximum_float64", maximum)

    @property
    def diagonal(self) -> float:
        """경계 대각선 길이를 반환합니다."""

        return float(np.linalg.norm(self.maximum_float64 - self.minimum_float64))

    def expanded(self, distance: float) -> "AxisAlignedBounds":
        """양의 최대 대응 거리만큼 모든 방향으로 넓힌 AABB를 반환합니다."""

        amount = _strict_finite_float(distance, "AABB 확장 거리")
        return AxisAlignedBounds(
            minimum_float64=self.minimum_float64 - amount,
            maximum_float64=self.maximum_float64 + amount,
        )


@dataclass(frozen=True, slots=True)
class IcpOverlapCandidate:
    """한 A 모델의 AABB·점유 그리드 겹침 통계와 실제 공통 cell 하한을 보관합니다."""

    reference_model_id: str
    aabb_intersects: bool
    common_cell_count: int
    moving_cell_count: int
    reference_cell_count: int
    moving_coverage: float
    reference_coverage: float
    selected: bool
    effective_required_common_cells: int = 0

    def __post_init__(self) -> None:
        """겹침 후보 모델 ID·cell 수·coverage 범위를 검증합니다."""

        model_id = str(self.reference_model_id).strip()
        counts = {
            "common_cell_count": int(self.common_cell_count),
            "moving_cell_count": int(self.moving_cell_count),
            "reference_cell_count": int(self.reference_cell_count),
            "effective_required_common_cells": int(self.effective_required_common_cells),
        }
        coverages = {
            "moving_coverage": _strict_finite_float(self.moving_coverage, "ICP overlap moving coverage"),
            "reference_coverage": _strict_finite_float(self.reference_coverage, "ICP overlap reference coverage"),
        }
        if not model_id or any(value < 0 for value in counts.values()):
            raise IcpInputError("ICP overlap 후보 모델 또는 cell 수가 올바르지 않습니다.")
        if counts["common_cell_count"] > counts["moving_cell_count"] or counts["common_cell_count"] > counts["reference_cell_count"]:
            raise IcpInputError("ICP overlap 공통 cell 수가 각 점유 cell 수보다 클 수 없습니다.")
        if not all(isfinite(value) and 0.0 <= value <= 1.0 for value in coverages.values()):
            raise IcpInputError("ICP overlap coverage는 0 이상 1 이하여야 합니다.")
        if counts["moving_cell_count"]:
            expected = counts["common_cell_count"] / counts["moving_cell_count"]
            if not np.isclose(coverages["moving_coverage"], expected, rtol=0.0, atol=1.0e-12):
                raise IcpInputError("ICP overlap moving coverage가 cell 수와 다릅니다.")
        if counts["reference_cell_count"]:
            expected = counts["common_cell_count"] / counts["reference_cell_count"]
            if not np.isclose(coverages["reference_coverage"], expected, rtol=0.0, atol=1.0e-12):
                raise IcpInputError("ICP overlap reference coverage가 cell 수와 다릅니다.")
        object.__setattr__(self, "reference_model_id", model_id)
        for name, value in counts.items():
            object.__setattr__(self, name, value)
        for name, value in coverages.items():
            object.__setattr__(self, name, value)
        object.__setattr__(self, "aabb_intersects", bool(self.aabb_intersects))
        object.__setattr__(self, "selected", bool(self.selected))


def bounds_from_positions(positions_float64: object) -> AxisAlignedBounds:
    """N×3 유한 점 배열의 정확한 AABB를 계산합니다."""

    positions = _freeze_float64(positions_float64, (None, 3), "AABB 점 좌표")
    if positions.shape[0] == 0:
        raise IcpInputError("AABB 점군은 비어 있을 수 없습니다.")
    return AxisAlignedBounds(
        minimum_float64=np.min(positions, axis=0),
        maximum_float64=np.max(positions, axis=0),
    )


def _normalize_crop_bounds(
    crop_bounds: AxisAlignedBounds | None,
) -> AxisAlignedBounds | None:
    """선택적인 stream crop 경계를 올바른 AABB 또는 None으로 고정합니다."""

    if crop_bounds is not None and not isinstance(crop_bounds, AxisAlignedBounds):
        raise IcpInputError("점군 chunk crop 경계는 AxisAlignedBounds여야 합니다.")
    return crop_bounds


def _iter_cloud_position_chunks(
    cloud: IcpPointCloud,
    *,
    crop_bounds: AxisAlignedBounds | None,
    chunk_size: int,
    cancel_probe: Callable[[], bool] | None,
) -> Iterator[np.ndarray]:
    """read-only cloud/memmap을 고정 chunk로 순회하며 필요한 crop 점만 반환합니다."""

    if not isinstance(cloud, IcpPointCloud):
        raise IcpInputError("chunk AABB 계산에는 IcpPointCloud가 필요합니다.")
    normalized_crop = _normalize_crop_bounds(crop_bounds)
    normalized_chunk = _strict_positive_int(chunk_size, "ICP overlap chunk 크기")
    positions = cloud.positions_float64
    for start in range(0, cloud.count, normalized_chunk):
        _raise_if_cancelled(cancel_probe)
        chunk = positions[start : start + normalized_chunk]
        if normalized_crop is not None:
            mask = np.all(
                (chunk >= normalized_crop.minimum_float64[np.newaxis, :])
                & (chunk <= normalized_crop.maximum_float64[np.newaxis, :]),
                axis=1,
            )
            if not np.any(mask):
                continue
            chunk = chunk[mask]
        yield chunk


def bounds_from_cloud(
    cloud: IcpPointCloud,
    *,
    crop_bounds: AxisAlignedBounds | None = None,
    chunk_size: int = _DEFAULT_CHUNK_SIZE,
    cancel_probe: Callable[[], bool] | None = None,
) -> AxisAlignedBounds:
    """대용량 read-only cloud의 전체 또는 crop AABB를 chunk 순회로 계산합니다."""

    minimum: np.ndarray | None = None
    maximum: np.ndarray | None = None
    for chunk in _iter_cloud_position_chunks(
        cloud,
        crop_bounds=crop_bounds,
        chunk_size=chunk_size,
        cancel_probe=cancel_probe,
    ):
        if chunk.size == 0:
            continue
        chunk_minimum = np.min(chunk, axis=0)
        chunk_maximum = np.max(chunk, axis=0)
        minimum = chunk_minimum if minimum is None else np.minimum(minimum, chunk_minimum)
        maximum = chunk_maximum if maximum is None else np.maximum(maximum, chunk_maximum)
    if minimum is None or maximum is None:
        raise IcpInputError("overlap crop 영역에 유효 점이 없습니다.")
    return AxisAlignedBounds(minimum_float64=minimum, maximum_float64=maximum)


def _cell_keys_for_chunk(chunk: np.ndarray, voxel_size: float, origin: np.ndarray) -> np.ndarray:
    """overlap 점유 계산용 한 chunk의 local-origin cell key를 반환합니다."""

    scaled = (chunk - origin[np.newaxis, :]) / voxel_size
    if not np.isfinite(scaled).all() or np.any(np.abs(scaled) > (2**62 - 1)):
        raise IcpInputError("ICP overlap cell 좌표가 int64 범위를 벗어났습니다.")
    return np.floor(scaled).astype(np.int64, copy=False)


def _stable_unique_keys(keys: np.ndarray) -> np.ndarray:
    """점유 cell key를 연속 배열로 정렬·중복 제거합니다."""

    if keys.shape[0] == 0:
        return np.empty((0, 3), dtype=np.int64)
    order = np.lexsort((keys[:, 2], keys[:, 1], keys[:, 0]))
    ordered = keys[order]
    first = np.empty(ordered.shape[0], dtype=bool)
    first[0] = True
    first[1:] = np.any(ordered[1:] != ordered[:-1], axis=1)
    return np.ascontiguousarray(ordered[first], dtype=np.int64)


def _merge_unique_keys(existing: np.ndarray, incoming: np.ndarray) -> np.ndarray:
    """chunk 단위 unique key를 연속 배열로 안정 병합합니다."""

    return _stable_unique_keys(np.concatenate((existing, incoming), axis=0))


def occupancy_cell_keys_for_cloud(
    cloud: IcpPointCloud,
    voxel_size: float,
    *,
    origin_float64: object,
    crop_bounds: AxisAlignedBounds | None = None,
    chunk_size: int = _DEFAULT_CHUNK_SIZE,
    maximum_cells: int | None = None,
    cancel_probe: Callable[[], bool] | None = None,
) -> tuple[np.ndarray, int]:
    """대용량 cloud의 점유 cell만 chunk 병합해 결과 cell 배열로 반환합니다."""

    normalized_voxel = _strict_finite_float(voxel_size, "ICP overlap 복셀 크기", minimum=np.finfo(np.float64).tiny)
    origin = _freeze_float64(origin_float64, (3,), "ICP overlap 원점")
    limit = None if maximum_cells is None else _strict_positive_int(maximum_cells, "ICP overlap 최대 cell 수")
    occupied = np.empty((0, 3), dtype=np.int64)
    for chunk in _iter_cloud_position_chunks(
        cloud,
        crop_bounds=crop_bounds,
        chunk_size=chunk_size,
        cancel_probe=cancel_probe,
    ):
        chunk_keys = _stable_unique_keys(_cell_keys_for_chunk(chunk, normalized_voxel, origin))
        occupied = _merge_unique_keys(occupied, chunk_keys)
        if limit is not None and occupied.shape[0] > limit:
            raise IcpInputError("ICP overlap 점유 cell 수가 메모리 안전 한도를 초과했습니다.")
    occupied.setflags(write=False)
    return occupied, int(occupied.shape[0])


def combined_moving_overlap_coverage(
    moving_cloud: IcpPointCloud,
    reference_clouds: Sequence[IcpPointCloud],
    *,
    moving_crop_bounds: AxisAlignedBounds,
    reference_crop_bounds_by_model: Mapping[str, AxisAlignedBounds],
    occupancy_voxel_size: float,
    origin_float64: object,
    chunk_size: int = _DEFAULT_CHUNK_SIZE,
    maximum_occupancy_cells: int | None = None,
    cancel_probe: Callable[[], bool] | None = None,
) -> float:
    """선택 A들의 cell 합집합으로 P coverage를 계산해 다중 A 과소평가를 막습니다."""

    if not isinstance(moving_cloud, IcpPointCloud):
        raise IcpInputError("합성 overlap coverage에는 IcpPointCloud P가 필요합니다.")
    normalized_references = tuple(reference_clouds)
    if not normalized_references or not all(isinstance(cloud, IcpPointCloud) for cloud in normalized_references):
        raise IcpInputError("합성 overlap coverage에는 A 점군이 하나 이상 필요합니다.")
    if not isinstance(moving_crop_bounds, AxisAlignedBounds):
        raise IcpInputError("합성 overlap P crop 경계가 올바르지 않습니다.")
    moving_keys, moving_count = occupancy_cell_keys_for_cloud(
        moving_cloud,
        occupancy_voxel_size,
        origin_float64=origin_float64,
        crop_bounds=moving_crop_bounds,
        chunk_size=chunk_size,
        maximum_cells=maximum_occupancy_cells,
        cancel_probe=cancel_probe,
    )
    if moving_count == 0:
        return 0.0
    reference_union = np.empty((0, 3), dtype=np.int64)
    for cloud in sorted(normalized_references, key=lambda item: item.model_id):
        _raise_if_cancelled(cancel_probe)
        try:
            crop_bounds = reference_crop_bounds_by_model[cloud.model_id]
        except KeyError as error:
            raise IcpInputError("선택 A 모델의 overlap crop 경계가 없습니다.") from error
        if not isinstance(crop_bounds, AxisAlignedBounds):
            raise IcpInputError("선택 A 모델의 overlap crop 경계가 올바르지 않습니다.")
        reference_keys, _count = occupancy_cell_keys_for_cloud(
            cloud,
            occupancy_voxel_size,
            origin_float64=origin_float64,
            crop_bounds=crop_bounds,
            chunk_size=chunk_size,
            maximum_cells=maximum_occupancy_cells,
            cancel_probe=cancel_probe,
        )
        reference_union = _merge_unique_keys(reference_union, reference_keys)
        if maximum_occupancy_cells is not None and reference_union.shape[0] > maximum_occupancy_cells:
            raise IcpInputError("합성 A overlap cell 수가 메모리 안전 한도를 초과했습니다.")
    common_count = common_occupancy_cell_count(moving_keys, reference_union)
    return float(common_count / moving_count)


def transform_positions_for_overlap(
    positions_float64: object,
    transform: Sim3Transform | object,
) -> np.ndarray:
    """column-vector 4×4 변환을 적용한 새 Project World 점 배열을 반환합니다."""

    positions = _freeze_float64(positions_float64, (None, 3), "겹침 변환 입력")
    matrix = (
        transform.matrix_float64
        if isinstance(transform, Sim3Transform)
        else _freeze_float64(transform, (4, 4), "겹침 변환 행렬")
    )
    transformed = positions @ matrix[:3, :3].T + matrix[:3, 3]
    return _freeze_float64(transformed, (positions.shape[0], 3), "겹침 변환 결과")


def transform_bounds(
    bounds: AxisAlignedBounds,
    transform: Sim3Transform | object,
) -> AxisAlignedBounds:
    """AABB의 여덟 모서리를 변환해 Project World의 올바른 새 AABB를 계산합니다."""

    if not isinstance(bounds, AxisAlignedBounds):
        raise IcpInputError("변환할 경계는 AxisAlignedBounds여야 합니다.")
    corners = np.asarray(
        [
            (x, y, z)
            for x in (bounds.minimum_float64[0], bounds.maximum_float64[0])
            for y in (bounds.minimum_float64[1], bounds.maximum_float64[1])
            for z in (bounds.minimum_float64[2], bounds.maximum_float64[2])
        ],
        dtype=np.float64,
    )
    return bounds_from_positions(transform_positions_for_overlap(corners, transform))


def bounds_intersection(
    left: AxisAlignedBounds,
    right: AxisAlignedBounds,
) -> AxisAlignedBounds | None:
    """닫힌 두 AABB의 교집합을 반환하고 만나지 않으면 None을 반환합니다."""

    if not isinstance(left, AxisAlignedBounds) or not isinstance(right, AxisAlignedBounds):
        raise IcpInputError("AABB 교집합에는 AxisAlignedBounds 두 개가 필요합니다.")
    minimum = np.maximum(left.minimum_float64, right.minimum_float64)
    maximum = np.minimum(left.maximum_float64, right.maximum_float64)
    if np.any(minimum > maximum):
        return None
    return AxisAlignedBounds(minimum_float64=minimum, maximum_float64=maximum)


def bounds_intersect(left: AxisAlignedBounds, right: AxisAlignedBounds) -> bool:
    """닫힌 두 AABB가 한 점·면·부피라도 공유하는지 반환합니다."""

    return bounds_intersection(left, right) is not None


def occupancy_cell_count(positions_float64: object, voxel_size: float, *, origin_float64: object) -> tuple[np.ndarray, int]:
    """공통 원점 기준 유일 점유 cell key와 그 개수를 반환합니다."""

    keys, _origin = voxel_cell_keys(
        positions_float64,
        voxel_size,
        origin_float64=origin_float64,
    )
    unique_keys = np.unique(keys, axis=0)
    unique_keys = np.array(unique_keys, dtype=np.int64, order="C", copy=True)
    unique_keys.setflags(write=False)
    return unique_keys, int(unique_keys.shape[0])


def common_occupancy_cell_count(left_keys: object, right_keys: object) -> int:
    """정렬된 unique cell key 두 집합의 공통 cell 수를 결정적으로 계산합니다."""

    left = np.asarray(left_keys, dtype=np.int64)
    right = np.asarray(right_keys, dtype=np.int64)
    if left.ndim != 2 or right.ndim != 2 or left.shape[1:] != (3,) or right.shape[1:] != (3,):
        raise IcpInputError("점유 cell key는 N×3 int64 배열이어야 합니다.")
    if left.shape[0] == 0 or right.shape[0] == 0:
        return 0
    left_structured = np.ascontiguousarray(left).view(
        [("x", np.int64), ("y", np.int64), ("z", np.int64)]
    ).reshape(-1)
    right_structured = np.ascontiguousarray(right).view(
        [("x", np.int64), ("y", np.int64), ("z", np.int64)]
    ).reshape(-1)
    return int(np.intersect1d(left_structured, right_structured, assume_unique=True).size)


def evaluate_overlap_candidate(
    moving_positions_float64: object,
    reference_cloud: IcpPointCloud,
    *,
    maximum_distance: float,
    occupancy_voxel_size: float,
    minimum_common_cells: int = 32,
    minimum_moving_coverage: float = 0.01,
    chunk_size: int = _DEFAULT_CHUNK_SIZE,
    maximum_occupancy_cells: int | None = None,
    cancel_probe: Callable[[], bool] | None = None,
) -> IcpOverlapCandidate:
    """Project World P와 한 A 모델의 crop AABB·점유 cell 겹침을 평가합니다."""

    _raise_if_cancelled(cancel_probe)
    if isinstance(moving_positions_float64, IcpPointCloud):
        moving_cloud = moving_positions_float64
    else:
        moving = _freeze_float64(moving_positions_float64, (None, 3), "ICP overlap P 좌표")
        if moving.shape[0] == 0:
            raise IcpInputError("ICP overlap에는 비어 있지 않은 P 점군이 필요합니다.")
        moving_cloud = IcpPointCloud(
            model_id="__overlap_moving__",
            positions_float64=moving,
            point_ids_uint64=np.arange(moving.shape[0], dtype=np.uint64),
        )
    if not isinstance(reference_cloud, IcpPointCloud):
        raise IcpInputError("ICP overlap에는 비어 있지 않은 P와 IcpPointCloud A가 필요합니다.")
    maximum_distance = _strict_finite_float(maximum_distance, "ICP overlap 최대 거리")
    voxel_size = _strict_finite_float(
        occupancy_voxel_size,
        "ICP overlap 복셀 크기",
        minimum=np.finfo(np.float64).tiny,
    )
    required_common = _strict_positive_int(minimum_common_cells, "최소 overlap cell 수")
    required_coverage = _strict_finite_float(minimum_moving_coverage, "최소 moving coverage")
    normalized_chunk_size = _strict_positive_int(chunk_size, "ICP overlap chunk 크기")
    if required_coverage > 1.0:
        raise IcpInputError("ICP overlap 후보 기준이 올바르지 않습니다.")
    moving_bounds = bounds_from_cloud(
        moving_cloud,
        chunk_size=normalized_chunk_size,
        cancel_probe=cancel_probe,
    )
    reference_bounds = bounds_from_cloud(
        reference_cloud,
        chunk_size=normalized_chunk_size,
        cancel_probe=cancel_probe,
    )
    aabb_intersects = bounds_intersect(moving_bounds.expanded(maximum_distance), reference_bounds.expanded(maximum_distance))
    if not aabb_intersects:
        return IcpOverlapCandidate(
            reference_model_id=reference_cloud.model_id,
            aabb_intersects=False,
            common_cell_count=0,
            moving_cell_count=0,
            reference_cell_count=0,
            moving_coverage=0.0,
            reference_coverage=0.0,
            selected=False,
            effective_required_common_cells=0,
        )
    moving_crop = reference_bounds.expanded(maximum_distance)
    reference_crop = moving_bounds.expanded(maximum_distance)
    try:
        cropped_moving_bounds = bounds_from_cloud(
            moving_cloud,
            crop_bounds=moving_crop,
            chunk_size=normalized_chunk_size,
            cancel_probe=cancel_probe,
        )
        cropped_reference_bounds = bounds_from_cloud(
            reference_cloud,
            crop_bounds=reference_crop,
            chunk_size=normalized_chunk_size,
            cancel_probe=cancel_probe,
        )
    except IcpInputError:
        return IcpOverlapCandidate(
            reference_model_id=reference_cloud.model_id,
            aabb_intersects=True,
            common_cell_count=0,
            moving_cell_count=0,
            reference_cell_count=0,
            moving_coverage=0.0,
            reference_coverage=0.0,
            selected=False,
            effective_required_common_cells=0,
        )
    common_origin = np.minimum(
        moving_bounds.minimum_float64,
        cropped_reference_bounds.minimum_float64,
    )
    moving_keys, moving_count = occupancy_cell_keys_for_cloud(
        moving_cloud,
        voxel_size,
        origin_float64=common_origin,
        crop_bounds=None,
        chunk_size=normalized_chunk_size,
        maximum_cells=maximum_occupancy_cells,
        cancel_probe=cancel_probe,
    )
    reference_keys, reference_count = occupancy_cell_keys_for_cloud(
        reference_cloud,
        voxel_size,
        origin_float64=common_origin,
        crop_bounds=reference_crop,
        chunk_size=normalized_chunk_size,
        maximum_cells=maximum_occupancy_cells,
        cancel_probe=cancel_probe,
    )
    _raise_if_cancelled(cancel_probe)
    common_count = common_occupancy_cell_count(moving_keys, reference_keys)
    moving_coverage = common_count / moving_count if moving_count else 0.0
    reference_coverage = common_count / reference_count if reference_count else 0.0
    return IcpOverlapCandidate(
        reference_model_id=reference_cloud.model_id,
        aabb_intersects=True,
        common_cell_count=common_count,
        moving_cell_count=moving_count,
        reference_cell_count=reference_count,
        moving_coverage=moving_coverage,
        reference_coverage=reference_coverage,
        selected=common_count >= required_common and moving_coverage >= required_coverage,
        effective_required_common_cells=required_common,
    )


def find_overlap_candidates(
    moving_positions_float64: object,
    reference_clouds: Sequence[IcpPointCloud],
    *,
    maximum_distance: float,
    occupancy_voxel_size: float,
    minimum_common_cells: int = 32,
    minimum_moving_coverage: float = 0.01,
    chunk_size: int = _DEFAULT_CHUNK_SIZE,
    maximum_occupancy_cells: int | None = None,
    cancel_probe: Callable[[], bool] | None = None,
) -> tuple[IcpOverlapCandidate, ...]:
    """여러 A 모델을 ID 순서로 평가해 자동 겹침 후보 통계를 반환합니다."""

    clouds = tuple(reference_clouds)
    if not clouds or not all(isinstance(cloud, IcpPointCloud) for cloud in clouds):
        raise IcpInputError("ICP overlap 후보에는 A 점군이 하나 이상 필요합니다.")
    if len({cloud.model_id for cloud in clouds}) != len(clouds):
        raise IcpInputError("ICP overlap 후보 A 모델 ID가 중복됩니다.")
    candidates: list[IcpOverlapCandidate] = []
    for cloud in sorted(clouds, key=lambda cloud: cloud.model_id):
        _raise_if_cancelled(cancel_probe)
        candidates.append(
            evaluate_overlap_candidate(
                moving_positions_float64,
                cloud,
                maximum_distance=maximum_distance,
                occupancy_voxel_size=occupancy_voxel_size,
                minimum_common_cells=minimum_common_cells,
                minimum_moving_coverage=minimum_moving_coverage,
                chunk_size=chunk_size,
                maximum_occupancy_cells=maximum_occupancy_cells,
                cancel_probe=cancel_probe,
            )
        )
    return tuple(candidates)


def select_overlap_clouds(
    candidates: Sequence[IcpOverlapCandidate],
    clouds: Sequence[IcpPointCloud],
) -> tuple[IcpPointCloud, ...]:
    """후보 통계의 selected 플래그와 일치하는 A 점군만 ID 순서로 반환합니다."""

    candidate_by_id = {candidate.reference_model_id: candidate for candidate in candidates}
    if len(candidate_by_id) != len(tuple(candidates)):
        raise IcpInputError("ICP overlap 후보 모델 ID가 중복됩니다.")
    selected = tuple(
        cloud
        for cloud in sorted(tuple(clouds), key=lambda cloud: cloud.model_id)
        if candidate_by_id.get(cloud.model_id) is not None and candidate_by_id[cloud.model_id].selected
    )
    if not selected:
        raise IcpInputError("자동 ICP 겹침 후보가 없습니다.")
    return selected


def indices_within_expanded_bounds(
    positions_float64: object,
    bounds: AxisAlignedBounds,
    *,
    margin: float = 0.0,
) -> np.ndarray:
    """겹침 AABB와 여백 안에 있는 원본 점 인덱스를 읽기 전용 int64로 반환합니다."""

    if not isinstance(bounds, AxisAlignedBounds):
        raise IcpInputError("겹침 crop 경계는 AxisAlignedBounds여야 합니다.")
    positions = _freeze_float64(positions_float64, (None, 3), "겹침 crop 좌표")
    expanded = bounds.expanded(margin)
    mask = np.all(
        (positions >= expanded.minimum_float64[np.newaxis, :])
        & (positions <= expanded.maximum_float64[np.newaxis, :]),
        axis=1,
    )
    indices = np.flatnonzero(mask).astype(np.int64, copy=False)
    indices = np.array(indices, dtype=np.int64, order="C", copy=True)
    indices.setflags(write=False)
    return indices


__all__ = (
    "AxisAlignedBounds",
    "IcpOverlapCandidate",
    "bounds_from_positions",
    "bounds_intersect",
    "bounds_intersection",
    "common_occupancy_cell_count",
    "evaluate_overlap_candidate",
    "find_overlap_candidates",
    "indices_within_expanded_bounds",
    "occupancy_cell_count",
    "select_overlap_clouds",
    "transform_bounds",
    "transform_positions_for_overlap",
)
