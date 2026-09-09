"""원본 형상을 보존하며 거리 기반 겹침 후보와 결정적 표본을 만듭니다."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from typing import Any, Callable

import numpy as np

if __package__:
    from .icp_cccore_contract import CcCoreCancelled, CcCoreGeometry, CcCoreInputError
else:
    from icp_cccore_contract import CcCoreCancelled, CcCoreGeometry, CcCoreInputError


@dataclass(frozen=True, slots=True)
class OverlapDomain:
    """후보 좌표·임시 면과 원본 인덱스의 대응을 보관합니다."""
    kind: str
    radius: float
    positions: np.ndarray
    source_indices: np.ndarray
    patches: np.ndarray | None
    patch_areas: np.ndarray | None
    metadata: dict[str, Any]


@dataclass(frozen=True, slots=True)
class CandidateSamples:
    """합격한 시작 좌표와 실제 거리·원본 출처·지문을 보관합니다."""
    positions: np.ndarray
    distances: np.ndarray
    source_indices: np.ndarray
    metadata: dict[str, Any]
    fingerprint: str


def _cancel(probe):
    """호출자의 취소 요청을 기존 취소 예외로 전달합니다."""
    if probe is not None and probe():
        raise CcCoreCancelled("CC 겹침 후보 처리가 취소되었습니다.")


def _positive_int(value, name):
    """예산과 표본 수가 양의 정수인지 확인합니다."""
    if isinstance(value, bool) or not isinstance(value, (int, np.integer)) or value < 1:
        raise CcCoreInputError(f"{name}은 양의 정수여야 합니다.")
    return int(value)


def _radius(value):
    """거리 반경을 유한한 양수로 검증합니다."""
    if isinstance(value, bool) or not np.isscalar(value):
        raise CcCoreInputError("CC 후보 반경은 유한한 양수여야 합니다.")
    try:
        value = float(value)
    except (ValueError, TypeError) as error:
        raise CcCoreInputError("CC 후보 반경을 읽지 못했습니다.") from error
    if not np.isfinite(value) or value <= 0:
        raise CcCoreInputError("CC 후보 반경은 유한한 양수여야 합니다.")
    return value


def _distances(points, query, cancel_probe, batch_size):
    """거리 질의를 제한된 묶음으로 실행하고 결과 계약을 검사합니다."""
    values = np.empty(len(points), dtype=np.float64)
    for start in range(0, len(points), batch_size):
        _cancel(cancel_probe)
        end = min(start + batch_size, len(points))
        result = np.asarray(query(points[start:end]), dtype=np.float64)
        if result.shape != (end - start,) or not np.isfinite(result).all() or np.any(result < 0):
            raise CcCoreInputError("CC 후보 거리 조회가 유한한 비음수 거리 배열을 반환하지 않았습니다.")
        values[start:end] = result
    _cancel(cancel_probe)
    return values


def _areas(patches):
    """임시 삼각형의 면적을 원본과 같은 좌표 단위로 계산합니다."""
    return np.linalg.norm(np.cross(patches[:, 1] - patches[:, 0], patches[:, 2] - patches[:, 0]), axis=1) * 0.5


def _spatial_metadata(points, bounds):
    """원본 경계 대비 후보 범위와 고정 공간 구간 점유율을 측정합니다."""
    lower, upper = np.asarray(bounds, dtype=np.float64)
    extent = upper - lower
    current_extent = np.ptp(points, axis=0)
    scaled = (points - lower) / np.where(extent > 0, extent, 1.0)
    cells = np.clip(np.floor(scaled * 4), 0, 3).astype(np.int64)
    codes = cells[:, 0] + 4 * cells[:, 1] + 16 * cells[:, 2]
    occupied, counts = np.unique(codes, return_counts=True)
    return {
        "original_bounds": [lower.tolist(), upper.tolist()],
        "original_extent": extent.tolist(), "candidate_extent": current_extent.tolist(),
        "extent_ratio": np.divide(current_extent, extent, out=np.zeros(3), where=extent > 0).tolist(),
        "spatial_grid_resolution": 4, "occupied_spatial_cells": int(len(occupied)),
        "spatial_cell_ids": occupied.tolist(), "spatial_cell_counts": counts.tolist(),
    }


def build_overlap_domain(geometry: CcCoreGeometry, model_bounds, radius, distance_query: Callable,
                         *, cancel_probe=None, batch_size=8192, max_subdivision_depth=8,
                         max_candidate_patches=250000) -> OverlapDomain:
    """보수적인 구 경계와 임시 사분할로 실제 겹침을 잃지 않는 후보를 구성합니다."""
    _cancel(cancel_probe)
    radius = _radius(radius)
    batch_size = _positive_int(batch_size, "CC 거리 조회 묶음 크기")
    cap = _positive_int(max_candidate_patches, "CC 추가 후보 조각 상한")
    if isinstance(max_subdivision_depth, bool) or not isinstance(max_subdivision_depth, int) or not 0 <= max_subdivision_depth <= 32:
        raise CcCoreInputError("CC 후보 분할 깊이는 0 이상 32 이하의 정수여야 합니다.")
    bounds = np.asarray(model_bounds, dtype=np.float64)
    points = np.asarray(geometry.positions, dtype=np.float64)
    if bounds.shape != (2, 3) or not np.isfinite(bounds).all() or np.any(bounds[0] > bounds[1]):
        raise CcCoreInputError("CC 대상 경계가 올바르지 않습니다.")
    if points.ndim != 2 or points.shape[1:] != (3,) or len(points) < 3 or not np.isfinite(points).all():
        raise CcCoreInputError("CC 후보 원본 좌표가 올바르지 않습니다.")
    original_bounds = np.array((points.min(axis=0), points.max(axis=0)))
    lower, upper = bounds[0] - radius, bounds[1] + radius
    metadata = {"radius": radius, "original_point_count": len(points), "provenance": dict(geometry.provenance),
                "max_subdivision_depth": max_subdivision_depth, "max_candidate_patches": cap,
                "distance_batch_size": batch_size}
    if geometry.kind == "COLMAP_POINT_CLOUD":
        # 같은 좌표는 최초 원본 점 ID만 남겨 중복 저장이 후보 밀도를 부풀리지 못하게 합니다.
        _, first_ids = np.unique(points, axis=0, return_index=True)
        first_ids.sort()
        _cancel(cancel_probe)
        unique_count = len(first_ids)
        ids = first_ids[np.all((points[first_ids] >= lower) & (points[first_ids] <= upper), axis=1)]
        distances = _distances(points[ids], distance_query, cancel_probe, batch_size)
        ids = ids[distances <= radius]
        if not len(ids):
            raise CcCoreInputError("CC 거리 반경 안에 겹침 후보 점이 없습니다.")
        selected = points[ids]
        metadata.update(_spatial_metadata(selected, original_bounds))
        metadata.update(candidate_point_count=len(ids), candidate_fraction=len(ids) / unique_count,
                        original_unique_point_count=unique_count,
                        original_duplicate_point_count=len(points) - unique_count)
        return OverlapDomain(geometry.kind, radius, selected, ids, None, None, metadata)
    if geometry.kind != "TRIANGLE_MESH" or geometry.triangles is None:
        raise CcCoreInputError("CC 겹침 후보 형상 종류가 올바르지 않습니다.")
    faces = np.asarray(geometry.triangles)
    if faces.ndim != 2 or faces.shape[1:] != (3,) or not np.issubdtype(faces.dtype, np.integer) or not len(faces) or np.any(faces < 0) or np.any(faces >= len(points)):
        raise CcCoreInputError("CC 후보 삼각면 인덱스가 올바르지 않습니다.")
    pending, pending_ids, original_area = [], [], 0.0
    initial_count = 0
    for start in range(0, len(faces), batch_size):
        _cancel(cancel_probe)
        patch = points[faces[start:start + batch_size]]
        area = _areas(patch)
        original_area += float(area.sum())
        keep = (area > 0) & np.all(patch.max(axis=1) >= lower, axis=1) & np.all(patch.min(axis=1) <= upper, axis=1)
        if np.any(keep):
            centers = patch[keep].mean(axis=1)
            radii = np.linalg.norm(patch[keep] - centers[:, None], axis=2).max(axis=1)
            near = _distances(centers, distance_query, cancel_probe, batch_size) - radii <= radius
            ids = np.arange(start, start + len(patch))[keep][near]
            pending.append(patch[keep][near])
            pending_ids.append(ids)
            initial_count += len(ids)
    if not np.isfinite(original_area) or original_area <= 0 or not initial_count:
        raise CcCoreInputError("CC 겹침 후보 면이 없거나 원본 면적이 퇴화했습니다.")
    patches, ids = np.concatenate(pending), np.concatenate(pending_ids)
    retained, retained_ids, retained_count, limited, additional_patches = [], [], 0, 0, 0
    for depth in range(max_subdivision_depth + 1):
        _cancel(cancel_probe)
        if not len(patches):
            break
        centers = patches.mean(axis=1)
        radii = np.linalg.norm(patches - centers[:, None], axis=2).max(axis=1)
        distances = _distances(centers, distance_query, cancel_probe, batch_size)
        inside = distances + radii <= radius
        mixed = (distances - radii <= radius) & ~inside
        retained.append(patches[inside])
        retained_ids.append(ids[inside])
        retained_count += int(inside.sum())
        patches, ids = patches[mixed], ids[mixed]
        # 원본 면 수는 제한하지 않고 사분할로 추가되는 조각 수만 예산에서 차감합니다.
        split_count = min(len(patches), max(0, (cap - additional_patches) // 3)) if depth < max_subdivision_depth else 0
        additional_patches += 3 * split_count
        if split_count < len(patches):
            retained.append(patches[split_count:])
            retained_ids.append(ids[split_count:])
            limited += len(patches) - split_count
            retained_count += len(patches) - split_count
        patches, ids = patches[:split_count], ids[:split_count]
        if not split_count:
            break
        a, b, c = patches[:, 0], patches[:, 1], patches[:, 2]
        ab, bc, ca = (a + b) * 0.5, (b + c) * 0.5, (c + a) * 0.5
        patches = np.stack((np.stack((a, ab, ca), axis=1), np.stack((ab, b, bc), axis=1),
                            np.stack((ca, bc, c), axis=1), np.stack((ab, bc, ca), axis=1)), axis=1).reshape(-1, 3, 3)
        ids = np.repeat(ids, 4)
    if not retained_count:
        raise CcCoreInputError("CC 거리 반경 안에 겹침 후보 면이 없습니다.")
    patches, ids = np.concatenate(retained), np.concatenate(retained_ids)
    areas = _areas(patches)
    valid = np.isfinite(areas) & (areas > 0)
    patches, ids, areas = patches[valid], ids[valid], areas[valid]
    if not len(patches) or not np.isfinite(areas.sum()):
        raise CcCoreInputError("CC 후보 면적이 수치적으로 퇴화했습니다.")
    metadata.update(_spatial_metadata(patches.reshape(-1, 3), original_bounds))
    metadata.update(original_face_count=len(faces), original_area=original_area, candidate_area=float(areas.sum()),
                    candidate_area_fraction=float(areas.sum() / original_area), candidate_patch_count=len(patches),
                    candidate_source_face_count=int(len(np.unique(ids))), limited_mixed_patch_count=limited,
                    initial_candidate_face_count=initial_count, additional_subdivision_patch_count=additional_patches)
    return OverlapDomain(geometry.kind, radius, points, ids, patches, areas, metadata)


def sample_overlap_domain(domain: OverlapDomain, count, seed, distance_query: Callable, *, radius=None,
                          cancel_probe=None, max_proposals_factor=20) -> CandidateSamples:
    """후보 안에서 실제 거리를 통과한 독립 표본을 유한한 제안 예산으로 확보합니다."""
    _cancel(cancel_probe)
    count = _positive_int(count, "CC 합격 표본 수")
    factor = _positive_int(max_proposals_factor, "CC 표본 제안 배수")
    radius = domain.radius if radius is None else _radius(radius)
    if radius > domain.radius:
        raise CcCoreInputError("CC 표본 반경은 후보 구성 반경을 초과할 수 없습니다.")
    rng = np.random.default_rng(seed)
    batch_size = domain.metadata["distance_batch_size"]
    proposals = 0
    if domain.kind == "COLMAP_POINT_CLOUD":
        distances = _distances(domain.positions, distance_query, cancel_probe, batch_size)
        accepted = np.flatnonzero(distances <= radius)
        proposals = len(distances)
        if len(accepted) > count:
            bounds = np.asarray(domain.metadata["original_bounds"])
            extent = bounds[1] - bounds[0]
            cells = np.clip(np.floor((domain.positions[accepted] - bounds[0]) / np.where(extent > 0, extent, 1) * 4), 0, 3).astype(np.int64)
            codes = cells[:, 0] + 4 * cells[:, 1] + 16 * cells[:, 2]
            order = np.lexsort((rng.random(len(accepted)), codes))
            # 각 공간 구간을 차례로 순회하여 밀집 구간의 독점을 막습니다.
            groups = np.split(order, np.flatnonzero(np.diff(codes[order])) + 1)
            ranked = np.concatenate([group for group in groups])
            ranks = np.concatenate([np.arange(len(group)) for group in groups])
            chosen = ranked[np.argsort(ranks, kind="stable")[:count]]
            accepted = accepted[chosen]
        points, source, values = domain.positions[accepted], domain.source_indices[accepted], distances[accepted]
    else:
        if domain.patches is None or domain.patch_areas is None:
            raise CcCoreInputError("CC 후보 표본의 삼각면 자료가 없습니다.")
        cumulative = np.cumsum(domain.patch_areas)
        point_parts, source_parts, distance_parts = [], [], []
        achieved, budget = 0, count * factor
        while achieved < count and proposals < budget:
            _cancel(cancel_probe)
            size = min(batch_size, budget - proposals, max(count - achieved, 256))
            chosen = np.searchsorted(cumulative, rng.random(size) * cumulative[-1], side="right")
            patch = domain.patches[chosen]
            uv = rng.random((size, 2))
            root = np.sqrt(uv[:, 0])
            weights = np.column_stack((1 - root, root * (1 - uv[:, 1]), root * uv[:, 1]))
            proposed = np.sum(patch * weights[:, :, None], axis=1)
            distances = _distances(proposed, distance_query, cancel_probe, batch_size)
            keep = np.flatnonzero(distances <= radius)[:count - achieved]
            point_parts.append(proposed[keep])
            source_parts.append(domain.source_indices[chosen[keep]])
            distance_parts.append(distances[keep])
            achieved += len(keep)
            proposals += size
        if achieved < count:
            raise CcCoreInputError(f"CC 겹침 합격 표본이 부족합니다: {achieved}/{count}, 제안 상한 {budget}개.")
        points, source, values = np.concatenate(point_parts), np.concatenate(source_parts), np.concatenate(distance_parts)
        if len(np.unique(points, axis=0)) != count:
            raise CcCoreInputError("CC 후보 면의 좌표 정밀도가 부족하여 서로 다른 합격 표본을 확보하지 못했습니다.")
    if len(points) < 3 or np.linalg.matrix_rank(points - points[0]) < 2:
        raise CcCoreInputError("CC 겹침 표본이 3개 미만이거나 한 점·직선으로 퇴화했습니다.")
    _cancel(cancel_probe)
    metadata = dict(domain.metadata)
    metadata.update(sample_count=len(points), requested_sample_count=count, proposal_count=proposals,
                    accepted_radius=radius, seed=int(seed), acceptance_fraction=len(points) / proposals,
                    sample_spatial_distribution=_spatial_metadata(points, domain.metadata["original_bounds"]))
    digest = hashlib.sha256()
    for array in (points, values, source):
        digest.update(np.ascontiguousarray(array).tobytes())
    return CandidateSamples(points, values, source, metadata, digest.hexdigest())
