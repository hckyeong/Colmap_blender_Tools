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
    """초기 거리 판정을 재사용하고 보수적인 구 경계와 사분할로 겹침 후보를 구성합니다."""
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
    pending, pending_ids, pending_inside, original_area = [], [], [], 0.0
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
            distances = _distances(centers, distance_query, cancel_probe, batch_size)
            near = distances - radii <= radius
            ids = np.arange(start, start + len(patch))[keep][near]
            pending.append(patch[keep][near])
            pending_ids.append(ids)
            pending_inside.append((distances + radii <= radius)[near])
            initial_count += len(ids)
    if not np.isfinite(original_area) or original_area <= 0 or not initial_count:
        raise CcCoreInputError("CC 겹침 후보 면이 없거나 원본 면적이 퇴화했습니다.")
    patches, ids = np.concatenate(pending), np.concatenate(pending_ids)
    initial_inside = np.concatenate(pending_inside)
    del pending_inside
    retained, retained_ids, retained_count, limited, additional_patches = [], [], 0, 0, 0
    for depth in range(max_subdivision_depth + 1):
        _cancel(cancel_probe)
        if not len(patches):
            break
        if depth == 0:
            # 초기 후보는 이미 근접 판정을 통과했으므로 내부 여부만 재사용합니다.
            inside = initial_inside
            mixed = ~inside
            del initial_inside
        else:
            centers = patches.mean(axis=1)
            radii = np.linalg.norm(patches - centers[:, None], axis=2).max(axis=1)
            distances = _distances(centers, distance_query, cancel_probe, batch_size)
            inside = distances + radii <= radius
            mixed = (distances - radii <= radius) & ~inside
        retained.append(patches[inside])
        retained_ids.append(ids[inside])
        retained_count += int(inside.sum())
        patches, ids = patches[mixed], ids[mixed]
        del inside, mixed
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


def _surface_interval(successes, total):
    """두 방향·두 반경·세 관측의 단측 오류 합을 1% 이하로 제한합니다."""
    import math
    observed = successes / total
    limit = math.log(2400.0) / total

    def divergence(value):
        """관측 비율과 가정 비율 사이의 이항 상대 엔트로피를 계산합니다."""
        if value <= 0:
            return 0.0 if observed == 0 else math.inf
        if value >= 1:
            return 0.0 if observed == 1 else math.inf
        return ((observed * math.log(observed / value) if observed else 0.0)
                + ((1 - observed) * math.log((1 - observed) / (1 - value)) if observed < 1 else 0.0))

    left, right = 0.0, observed
    for _ in range(64):
        middle = (left + right) * 0.5
        if divergence(middle) > limit:
            left = middle
        else:
            right = middle
    # 이분법의 바깥 경계와 외향 반올림으로 신뢰 구간의 수치적 축소를 피합니다.
    lower = float(np.nextafter(left, 0.0)) if observed else 0.0
    left, right = observed, 1.0
    for _ in range(64):
        middle = (left + right) * 0.5
        if divergence(middle) > limit:
            right = middle
        else:
            left = middle
    return (lower, float(np.nextafter(right, 1.0)) if observed < 1 else 1.0)


def _surface_rng(seed):
    """재현 가능한 표면 난수열의 씨드를 음이 아닌 정수로 검증합니다."""
    if isinstance(seed, (bool, np.bool_)) or not isinstance(seed, (int, np.integer)) or seed < 0:
        raise CcCoreInputError("CC 표면 표본 씨드는 음이 아닌 정수여야 합니다.")
    return np.random.default_rng(int(seed))


def _surface_radius(value):
    """넘파이 논리값도 반경으로 받아들이지 않도록 검사합니다."""
    if isinstance(value, np.bool_):
        raise CcCoreInputError("CC 표면 반경은 유한한 양수여야 합니다.")
    return _radius(value)


def _candidate_face_mask(patches, areas, lower, upper, maximum, minimum):
    """재사용 배열로 세 정점을 직접 비교해 기존 경계 포함 후보 판정을 보존합니다."""
    maximum, minimum = maximum[:len(patches)], minimum[:len(patches)]
    eligible = areas > 0
    for axis in range(3):
        np.maximum(patches[:, 0, axis], patches[:, 1, axis], out=maximum)
        np.maximum(maximum, patches[:, 2, axis], out=maximum)
        np.minimum(patches[:, 0, axis], patches[:, 1, axis], out=minimum)
        np.minimum(minimum, patches[:, 2, axis], out=minimum)
        eligible &= maximum >= lower[axis]
        eligible &= minimum <= upper[axis]
    return eligible


class MeshSurfaceSampler:
    """원본 면적 누적 색인을 디스크에 보관하며 독립 표면 표본을 생성합니다."""

    def __init__(self, geometry, model_bounds, radius, directory, *, cancel_probe=None, batch_size=8192,
                 source_bounds=None):
        """같은 최종 형상의 선택적 범위를 재사용하며 제한된 묶음으로 면적 색인을 작성합니다."""
        import os
        import tempfile
        from pathlib import Path
        self._paths, self._indices = [], []
        self._closed = False
        self.cancel_probe = cancel_probe
        self.batch_size = min(8192, _positive_int(batch_size, "CC 표면 묶음 크기"))
        self.radius = _surface_radius(radius)
        self.points = np.asarray(geometry.positions)
        self.faces = np.asarray(geometry.triangles)
        bounds = np.asarray(model_bounds, dtype=np.float64)
        if geometry.kind != "TRIANGLE_MESH" or self.points.ndim != 2 or self.points.shape[1:] != (3,) or len(self.points) < 3:
            raise CcCoreInputError("CC 표면 샘플러에는 삼각 메시가 필요합니다.")
        if self.faces.ndim != 2 or self.faces.shape[1:] != (3,) or not len(self.faces) or not np.issubdtype(self.faces.dtype, np.integer):
            raise CcCoreInputError("CC 표면 삼각면 인덱스가 올바르지 않습니다.")
        if bounds.shape != (2, 3) or not np.isfinite(bounds).all() or np.any(bounds[0] > bounds[1]):
            raise CcCoreInputError("CC 대상 경계가 올바르지 않습니다.")
        if source_bounds is not None:
            source_bounds = np.asarray(source_bounds, dtype=np.float64)
            if (source_bounds.shape != (2, 3) or not np.isfinite(source_bounds).all()
                    or np.any(source_bounds[0] > source_bounds[1])):
                raise CcCoreInputError("CC 원본 표면 경계가 올바르지 않습니다.")
        lower, upper = bounds[0] - self.radius, bounds[1] + self.radius
        if not np.isfinite([lower, upper]).all():
            raise CcCoreInputError("CC 확장 대상 경계가 유한하지 않습니다.")
        original_lower, original_upper = (source_bounds.copy() if source_bounds is not None
                                          else (np.full(3, np.inf), np.full(3, -np.inf)))
        try:
            root = Path(directory).resolve(strict=True)
            for _ in range(2):
                descriptor, name = tempfile.mkstemp(prefix="cc_surface_", suffix=".bin", dir=root)
                self._paths.append(Path(name))
                os.close(descriptor)
                self._indices.append(np.memmap(name, dtype=np.float64, mode="w+", shape=(len(self.faces),)))
            for start in range(0, len(self.points), self.batch_size):
                _cancel(cancel_probe)
                vertices = self.points[start:start + self.batch_size]
                if not np.isfinite(vertices).all():
                    raise CcCoreInputError("CC 원본 표면 좌표가 유한하지 않습니다.")
                if source_bounds is None:
                    original_lower = np.minimum(original_lower, vertices.min(axis=0))
                    original_upper = np.maximum(original_upper, vertices.max(axis=0))
            totals = [0.0, 0.0]
            eligible_faces = 0
            maximum = np.empty(min(self.batch_size, len(self.faces)), dtype=np.float64)
            minimum = np.empty_like(maximum)
            for start in range(0, len(self.faces), self.batch_size):
                _cancel(cancel_probe)
                faces = self.faces[start:start + self.batch_size]
                if np.any(faces < 0) or np.any(faces >= len(self.points)):
                    raise CcCoreInputError("CC 표면 삼각면 인덱스가 범위를 벗어났습니다.")
                patches = np.asarray(self.points[faces], dtype=np.float64)
                areas = _areas(patches)
                if not np.isfinite(areas).all():
                    raise CcCoreInputError("CC 표면 면적이 유한하지 않습니다.")
                eligible = _candidate_face_mask(patches, areas, lower, upper, maximum, minimum)
                eligible_faces += int(eligible.sum())
                for index, weights in enumerate((areas, np.where(eligible, areas, 0.0))):
                    cumulative = np.cumsum(weights, dtype=np.float64) + totals[index]
                    increments = np.diff(np.concatenate(([totals[index]], cumulative)))
                    if not np.isfinite(cumulative).all() or np.any((weights > 0) & (increments <= 0)):
                        raise CcCoreInputError("CC 양수 면적이 누적 색인의 수치 정밀도에서 소실되었습니다.")
                    self._indices[index][start:start + len(faces)] = cumulative
                    totals[index] = float(cumulative[-1])
            if totals[0] <= 0 or totals[1] <= 0:
                raise CcCoreInputError("CC 원본 또는 후보 표면 면적이 없습니다.")
            for index in self._indices:
                index.flush()
            self.metadata = dict(kind="TRIANGLE_MESH", original_area=totals[0], eligible_area=totals[1], eligible_area_fraction=totals[1] / totals[0],
                                 original_bounds=[original_lower.tolist(), original_upper.tolist()], original_face_count=len(self.faces),
                                 eligible_face_count=eligible_faces, index_bytes=16 * len(self.faces), radius=self.radius,
                                 distance_batch_size=self.batch_size, provenance=dict(geometry.provenance), sampling_policy="MESH_SURFACE_AREA_REJECTION_V1")
        except OSError as error:
            failure = CcCoreInputError("CC 표면 면적 색인을 디스크에 작성하지 못했습니다.")
            self._close_after_error(failure)
            raise failure from error
        except BaseException as error:
            self._close_after_error(error)
            raise

    def __enter__(self):
        """열린 샘플러를 문맥 관리자에 제공합니다."""
        self._ensure_open()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        """정상 종료와 예외 종료 모두에서 자체 임시 색인을 제거합니다."""
        if exc_value is None:
            self.close()
        else:
            self._close_after_error(exc_value)
        return False

    def _close_after_error(self, error):
        """정리 실패를 부가 정보로 기록하여 원래 작업 예외를 보존합니다."""
        try:
            self.close()
        except CcCoreInputError as cleanup_error:
            if hasattr(error, "add_note"):
                error.add_note(str(cleanup_error))

    def close(self):
        """일부 정리가 실패해도 모든 자원을 처리하고 실패 자원은 재시도용으로 남깁니다."""
        self._closed = True
        remaining_indices, remaining_paths, failures = [], [], []
        for index in self._indices:
            try:
                index._mmap.close()
            except (OSError, ValueError, BufferError) as error:
                remaining_indices.append(index)
                failures.append(error)
        self._indices = remaining_indices
        for path in self._paths:
            try:
                path.unlink(missing_ok=True)
            except OSError as error:
                remaining_paths.append(path)
                failures.append(error)
        self._paths = remaining_paths
        if failures:
            raise CcCoreInputError(f"CC 표면 임시 색인 정리에 실패했습니다: {len(failures)}개 자원 오류.") from failures[0]

    def _ensure_open(self):
        """이미 해제한 샘플러의 사용과 취소를 검사합니다."""
        if self._closed:
            raise CcCoreInputError("CC 표면 샘플러가 이미 닫혔습니다.")
        _cancel(self.cancel_probe)

    def _draw(self, count, rng, *, full=False):
        """표본마다 세 난수를 사용하여 묶음 크기에 독립적인 면적 표본을 만듭니다."""
        self._ensure_open()
        cumulative = self._indices[0 if full else 1]
        random = rng.random((count, 3))
        area_positions = np.minimum(random[:, 0] * cumulative[-1], np.nextafter(cumulative[-1], 0.0))
        source = np.searchsorted(cumulative, area_positions, side="right")
        patches = np.asarray(self.points[self.faces[source]], dtype=np.float64)
        root = np.sqrt(random[:, 1])
        weights = np.column_stack((1 - root, root * (1 - random[:, 2]), root * random[:, 2]))
        points = np.sum(patches * weights[:, :, None], axis=1)
        if not np.isfinite(points).all():
            raise CcCoreInputError("CC 표면 표본 좌표가 유한하지 않습니다.")
        return points, source

    def sample_full(self, count, seed):
        """후보 필터 없이 원본 전체 면적에 비례하는 표본을 반환합니다."""
        self._ensure_open()
        count = _positive_int(count, "CC 전체 표면 표본 수")
        if count < 3:
            raise CcCoreInputError("CC 전체 표면 표본은 3개 이상이어야 합니다.")
        rng = _surface_rng(seed)
        result = np.empty((count, 3), dtype=np.float64)
        for start in range(0, count, self.batch_size):
            result[start:start + self.batch_size] = self._draw(min(self.batch_size, count - start), rng, full=True)[0]
        _cancel(self.cancel_probe)
        if len(np.unique(result, axis=0)) != count or count < 3 or np.linalg.matrix_rank(result - result[0]) < 2:
            raise CcCoreInputError("CC 전체 표면 표본에 중복 좌표가 있거나 공간적으로 퇴화했습니다.")
        _cancel(self.cancel_probe)
        return result

    def probe(self, distance_query, *, valid_radius, minimum_fraction, seed):
        """세 고정 관측에서 보정된 이항 구간으로 원본 대비 겹침을 판정합니다."""
        self._ensure_open()
        valid_radius = _surface_radius(valid_radius)
        if isinstance(minimum_fraction, (bool, np.bool_)) or not isinstance(minimum_fraction, (int, float, np.integer, np.floating)):
            raise CcCoreInputError("CC 최소 겹침 비율은 0과 1 사이의 수여야 합니다.")
        if valid_radius > self.radius or not np.isfinite(minimum_fraction) or not 0 < minimum_fraction < 1:
            raise CcCoreInputError("CC 탐색 반경 또는 최소 겹침 비율이 올바르지 않습니다.")
        rng = _surface_rng(seed)
        total = candidate = valid = 0
        fraction = self.metadata["eligible_area_fraction"]
        for target in (8192, 32768, 131072):
            while total < target:
                size = min(self.batch_size, target - total)
                points, _ = self._draw(size, rng)
                values = _distances(points, distance_query, self.cancel_probe, self.batch_size)
                candidate += int(np.count_nonzero(values <= self.radius))
                valid += int(np.count_nonzero(values <= valid_radius))
                total += size
            candidate_ci, valid_ci = _surface_interval(candidate, total), _surface_interval(valid, total)
            overlap_ci = [value * fraction for value in valid_ci]
            result = dict(estimated_overlap_fraction=fraction * valid / total, overlap_fraction_interval=overlap_ci,
                          valid_acceptance_fraction=valid / total, candidate_acceptance_fraction=candidate / total,
                          valid_acceptance_interval=list(valid_ci), candidate_acceptance_interval=list(candidate_ci),
                          probe_count=total, candidate_accepted_count=candidate, valid_accepted_count=valid,
                          confidence_family_error=0.01, confidence_method="Chernoff-KL", probe_seed=int(seed), decision="pass")
            if overlap_ci[0] >= minimum_fraction:
                return result
            if overlap_ci[1] < minimum_fraction:
                raise CcCoreInputError(f"CC 원본 면적 대비 겹침이 최소 비율보다 작습니다: {overlap_ci}.")
        raise CcCoreInputError(f"CC 겹침 신뢰 구간이 최소 비율을 걸쳐 판정할 수 없습니다: {overlap_ci}.")

    def sample(self, count, seed, distance_query, *, radius, max_proposals, acceptance_hint=None, progress=None):
        """고정된 면적 분포와 반경에서 독립 합격 표본을 정해진 예산까지 확보합니다."""
        self._ensure_open()
        count = _positive_int(count, "CC 합격 표본 수")
        if count < 3:
            raise CcCoreInputError("CC 합격 표면 표본은 3개 이상이어야 합니다.")
        budget = _positive_int(max_proposals, "CC 표본 제안 상한")
        radius = _surface_radius(radius)
        if radius > self.radius:
            raise CcCoreInputError("CC 표본 반경은 후보 반경보다 클 수 없습니다.")
        try:
            hint = float(acceptance_hint) if acceptance_hint is not None else 1.0
        except (TypeError, ValueError, OverflowError) as error:
            raise CcCoreInputError("CC 합격률 힌트가 유한한 확률이 아닙니다.") from error
        if isinstance(acceptance_hint, (bool, np.bool_)) or not np.isfinite(hint) or not 0 <= hint <= 1:
            raise CcCoreInputError("CC 합격률 힌트는 0 이상 1 이하의 유한한 수여야 합니다.")
        rng = _surface_rng(seed)
        points, values, sources = np.empty((count, 3)), np.empty(count), np.empty(count, dtype=np.int64)
        achieved = proposals = accepted_proposals = 0
        while achieved < count and proposals < budget:
            size = min(self.batch_size, budget - proposals)
            proposed, source = self._draw(size, rng)
            distances = _distances(proposed, distance_query, self.cancel_probe, self.batch_size)
            passed = np.flatnonzero(distances <= radius)
            accepted_proposals += len(passed)
            keep = passed[:count - achieved]
            end = achieved + len(keep)
            points[achieved:end], values[achieved:end], sources[achieved:end] = proposed[keep], distances[keep], source[keep]
            achieved, proposals = end, proposals + size
            if progress is not None:
                progress(dict(accepted_count=achieved, requested_count=count, proposal_count=proposals, max_proposals=budget))
        if achieved < count:
            raise CcCoreInputError(f"CC 겹침 합격 표본이 부족합니다: {achieved}/{count}, 제안 상한 {budget}개.")
        _cancel(self.cancel_probe)
        if len(np.unique(points, axis=0)) != count or count < 3 or np.linalg.matrix_rank(points - points[0]) < 2:
            raise CcCoreInputError("CC 겹침 표본에 중복 좌표가 있거나 공간적으로 퇴화했습니다.")
        metadata = dict(self.metadata)
        metadata.update(sample_count=count, accepted_count=count, requested_sample_count=count, proposal_count=proposals, accepted_radius=radius,
                        seed=int(seed), max_proposals=budget, acceptance_hint=hint, acceptance_fraction=count / proposals,
                        retained_sample_fraction=count / proposals, accepted_proposal_count=accepted_proposals,
                        proposal_acceptance_fraction=accepted_proposals / proposals,
                        sample_spatial_distribution=_spatial_metadata(points, self.metadata["original_bounds"]))
        digest = hashlib.sha256()
        for array in (points, values, sources):
            digest.update(np.ascontiguousarray(array).tobytes())
        _cancel(self.cancel_probe)
        return CandidateSamples(points, values, sources, metadata, digest.hexdigest())


def build_surface_sampler(geometry, model_bounds, radius, directory, *, cancel_probe=None, batch_size=8192,
                          source_bounds=None):
    """같은 최종 형상의 선택적 범위를 전달받아 전체·후보 면적 샘플러를 만듭니다."""
    return MeshSurfaceSampler(geometry, model_bounds, radius, directory, cancel_probe=cancel_probe,
                              batch_size=batch_size, source_bounds=source_bounds)
