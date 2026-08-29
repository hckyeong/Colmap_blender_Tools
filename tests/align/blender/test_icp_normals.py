"""ICP PCA 노멀·각도 gate·블록 질의를 Blender 환경에서 검증합니다."""

from __future__ import annotations

from pathlib import Path
import sys

import numpy as np


def _add_source_path() -> None:
    """애드온 소스 디렉터리를 Blender 테스트 모듈 경로에 추가합니다."""

    source_path = Path(__file__).resolve().parents[3]
    source_text = str(source_path)
    if source_text not in sys.path:
        sys.path.insert(0, source_text)


def _plane_grid(side: int = 17) -> np.ndarray:
    """PCA 노멀 검증용 결정적 평면 격자 점군을 생성합니다."""

    values = np.linspace(-0.8, 0.8, side, dtype=np.float64)
    x, y = np.meshgrid(values, values, indexing="xy")
    return np.column_stack((x.ravel(), y.ravel(), np.zeros(x.size, dtype=np.float64)))


class _RecordingRadiusIndex:
    """노멀 추정의 radius query block 크기를 기록하는 테스트 index입니다."""

    def __init__(self) -> None:
        """작은 synthetic 점군용 내부 BruteForce index를 준비합니다."""

        from tools.align.alignment import BruteForceNearestNeighborIndex

        self._inner = BruteForceNearestNeighborIndex(query_chunk_size=64)
        self.radius_query_sizes: list[int] = []

    def build(self, reference_positions_float64: object, *, cancel_probe=None) -> None:
        """내부 index에 기준점을 전달합니다."""

        self._inner.build(reference_positions_float64, cancel_probe=cancel_probe)

    def query(self, query_positions_float64: object, *, maximum_distance=None, cancel_probe=None):
        """일반 최근접점 질의를 내부 index로 전달합니다."""

        return self._inner.query(
            query_positions_float64,
            maximum_distance=maximum_distance,
            cancel_probe=cancel_probe,
        )

    def query_radius(self, query_positions_float64: object, *, radius, maximum_neighbors, cancel_probe=None):
        """반경 질의 크기를 기록한 뒤 내부 index 결과를 반환합니다."""

        self.radius_query_sizes.append(int(np.asarray(query_positions_float64).shape[0]))
        return self._inner.query_radius(
            query_positions_float64,
            radius=radius,
            maximum_neighbors=maximum_neighbors,
            cancel_probe=cancel_probe,
        )

    def close(self) -> None:
        """내부 index가 보관한 테스트 배열을 해제합니다."""

        self._inner.close()


def test_pca_normals_are_valid_and_blocked() -> None:
    """평면 PCA 노멀의 단위 길이·방향과 고정 block 질의를 확인합니다."""

    from tools.align.alignment import estimate_sample_normals

    points = _plane_grid()
    index = _RecordingRadiusIndex()
    try:
        index.build(points)
        normals = estimate_sample_normals(
            points,
            index=index,
            radius=0.22,
            minimum_neighbors=8,
            maximum_neighbors=24,
            minimum_planarity=0.2,
            maximum_curvature=0.1,
            query_block_size=37,
        )
    finally:
        index.close()
    assert normals.valid_count >= int(points.shape[0] * 0.90)
    valid_normals = normals.normals_float64[normals.valid_mask]
    assert np.allclose(np.abs(valid_normals[:, 2]), 1.0, rtol=0.0, atol=1.0e-8)
    assert index.radius_query_sizes
    assert max(index.radius_query_sizes) <= 37
    assert sum(index.radius_query_sizes) == points.shape[0]


def test_normal_angle_gate_accepts_reversed_normals_only_in_absolute_mode() -> None:
    """부호가 반대인 노멀도 abs(dot) gate에서는 같은 면으로 통과하는지 확인합니다."""

    from tools.align.alignment import normal_angle_mask

    moving = np.asarray(((0.0, 0.0, 1.0), (0.0, 1.0, 0.0)), dtype=np.float64)
    reference = np.asarray(((0.0, 0.0, -1.0), (0.0, -1.0, 0.0)), dtype=np.float64)
    absolute = normal_angle_mask(
        moving,
        reference,
        maximum_angle_degrees=5.0,
        use_absolute_dot=True,
    )
    directional = normal_angle_mask(
        moving,
        reference,
        maximum_angle_degrees=5.0,
        use_absolute_dot=False,
    )
    assert np.array_equal(absolute, np.asarray((True, True), dtype=bool))
    assert np.array_equal(directional, np.asarray((False, False), dtype=bool))


def test_normal_cache_key_changes_with_sampling_settings() -> None:
    """노멀 cache key가 표본·반경·신뢰도 설정 변화를 구분하는지 확인합니다."""

    from tools.align.alignment import normal_cache_key

    base = normal_cache_key(
        model_id="A",
        model_revision=3,
        sample_fingerprint="sample-a",
        voxel_size=0.2,
        radius=0.6,
        minimum_neighbors=16,
        maximum_neighbors=28,
        minimum_planarity=0.05,
        maximum_curvature=0.25,
    )
    changed = normal_cache_key(
        model_id="A",
        model_revision=3,
        sample_fingerprint="sample-a",
        voxel_size=0.2,
        radius=0.8,
        minimum_neighbors=16,
        maximum_neighbors=28,
        minimum_planarity=0.05,
        maximum_curvature=0.25,
    )
    assert base != changed


def test_sparse_neighborhood_is_invalid_and_cancel_is_typed() -> None:
    """이웃 부족 노멀은 임의 축으로 채우지 않고 취소는 typed 오류로 끝나는지 확인합니다."""

    from tools.align.alignment import (
        BruteForceNearestNeighborIndex,
        IcpCancelledError,
        estimate_sample_normals,
    )

    sparse = np.asarray(
        ((0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0)),
        dtype=np.float64,
    )
    index = BruteForceNearestNeighborIndex()
    try:
        index.build(sparse)
        invalid = estimate_sample_normals(
            sparse,
            index=index,
            radius=0.01,
            minimum_neighbors=3,
            maximum_neighbors=8,
            minimum_planarity=0.0,
            maximum_curvature=1.0,
        )
        assert invalid.valid_count == 0
        assert np.array_equal(invalid.normals_float64, np.zeros((3, 3), dtype=np.float64))
        try:
            estimate_sample_normals(
                sparse,
                index=index,
                radius=1.5,
                minimum_neighbors=3,
                maximum_neighbors=8,
                minimum_planarity=0.0,
                maximum_curvature=1.0,
                cancel_probe=lambda: True,
            )
        except IcpCancelledError:
            pass
        else:
            raise AssertionError("PCA 노멀 취소 요청을 허용했습니다.")
    finally:
        index.close()


def run_all() -> None:
    """ICP 노멀 순수 회귀를 Blender 프로세스에서 순서대로 실행합니다."""

    _add_source_path()
    test_pca_normals_are_valid_and_blocked()
    test_normal_angle_gate_accepts_reversed_normals_only_in_absolute_mode()
    test_normal_cache_key_changes_with_sampling_settings()
    test_sparse_neighborhood_is_invalid_and_cancel_is_typed()
    print("test_icp_normals: 통과")


if __name__ == "__main__":
    run_all()
