"""ICP 결정적 복셀 샘플과 다중 A quota를 Blender 환경에서 검증합니다."""

from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import gc

import numpy as np


def _add_source_path() -> None:
    """애드온 소스 디렉터리를 Blender 테스트 모듈 경로에 추가합니다."""

    source_path = Path(__file__).resolve().parents[3]
    source_text = str(source_path)
    if source_text not in sys.path:
        sys.path.insert(0, source_text)


def _cloud(model_id: str, count: int, offset: float = 0.0):
    """원본 순서와 무관한 복셀 샘플 테스트용 점군을 생성합니다."""

    from tools.align.alignment import IcpPointCloud

    values = np.arange(count, dtype=np.float64)
    positions = np.column_stack(
        (
            np.sin(values * 0.37) + offset,
            np.cos(values * 0.23) - offset,
            values * 0.041,
        )
    )
    point_ids = np.arange(count, dtype=np.uint64) * np.uint64(101) + np.uint64(17)
    return IcpPointCloud(model_id, positions, point_ids)


def test_voxel_representative_ids_are_deterministic_across_input_order() -> None:
    """POINT3D_ID hash 대표점 선택이 입력 배열 순서 변경에도 같은 ID 집합인지 확인합니다."""

    from tools.align.alignment import select_voxel_representatives

    cloud = _cloud("A", 400)
    first = select_voxel_representatives(
        cloud.positions_float64,
        cloud.point_ids_uint64,
        0.35,
    )
    permutation = np.random.default_rng(20_260_815).permutation(cloud.count)
    second = select_voxel_representatives(
        cloud.positions_float64[permutation],
        cloud.point_ids_uint64[permutation],
        0.35,
    )
    assert set(first.point_ids_uint64.tolist()) == set(second.point_ids_uint64.tolist())
    assert first.positions_float64.flags.writeable is False
    assert second.point_ids_uint64.flags.writeable is False


def test_budgeted_voxel_sample_uses_candidate_indices_and_fixed_budget() -> None:
    """crop 후보 인덱스 밖 점을 사용하지 않고 목표 예산 이하로 샘플링하는지 확인합니다."""

    from tools.align.alignment import build_budgeted_voxel_sample

    cloud = _cloud("A", 512)
    candidates = np.arange(80, 320, dtype=np.int64)
    sample = build_budgeted_voxel_sample(
        cloud,
        37,
        candidate_indices=candidates,
    )
    assert sample.count <= 37
    assert np.all(np.isin(sample.source_indices_int64, candidates))
    assert np.all(sample.source_indices_int64 >= 80)
    assert np.all(sample.source_indices_int64 < 320)


def test_duplicate_ids_and_cancelled_budget_search_are_rejected() -> None:
    """복셀 예산 탐색이 중복 ID를 즉시 거부하고 반복 경계 취소를 전달하는지 확인합니다."""

    from tools.align.alignment import (
        IcpCancelledError,
        IcpInputError,
        choose_voxel_size_for_budget,
    )

    cloud = _cloud("A", 128)
    duplicate_ids = cloud.point_ids_uint64.copy()
    duplicate_ids[3] = duplicate_ids[2]
    try:
        choose_voxel_size_for_budget(cloud.positions_float64, duplicate_ids, 20)
    except IcpInputError:
        pass
    else:
        raise AssertionError("중복 POINT3D_ID를 복셀 예산 탐색이 허용했습니다.")
    try:
        choose_voxel_size_for_budget(
            cloud.positions_float64,
            cloud.point_ids_uint64,
            20,
            cancel_probe=lambda: True,
        )
    except IcpCancelledError:
        pass
    else:
        raise AssertionError("복셀 예산 탐색이 취소 요청을 전달하지 않았습니다.")


def test_multi_reference_quota_uses_crop_capacity_and_overlap_weight() -> None:
    """다중 A quota가 crop 용량과 overlap cell 비율을 함께 반영하는지 확인합니다."""

    from tools.align.alignment import build_multi_reference_samples

    clouds = (_cloud("A_dense", 700), _cloud("A_sparse", 180, offset=10.0))
    candidates = {
        "A_dense": np.arange(30, 90, dtype=np.int64),
        "A_sparse": np.arange(10, 160, dtype=np.int64),
    }
    samples, quotas = build_multi_reference_samples(
        clouds,
        {"A_dense": 80, "A_sparse": 20},
        120,
        origin_float64=np.zeros(3, dtype=np.float64),
        candidate_indices_by_model=candidates,
        minimum_quota=10,
    )
    assert quotas["A_dense"] <= candidates["A_dense"].size
    assert quotas["A_sparse"] <= candidates["A_sparse"].size
    assert sum(quotas.values()) <= 120
    assert len(samples) == 2
    assert all(sample.count <= quotas[model_id] for sample, model_id in zip(samples, sorted(quotas), strict=True))


def test_large_readonly_memmap_keeps_only_budgeted_voxel_result() -> None:
    """대형 read-only memmap도 원본 복사 없이 chunk 순회해 동일한 예산 샘플을 만드는지 확인합니다."""

    from tools.align.alignment import IcpPointCloud, build_budgeted_voxel_sample

    point_count = 300_000
    with tempfile.TemporaryDirectory(prefix="colmap_align_icp_memmap_") as temporary:
        root = Path(temporary)
        positions_path = root / "positions.npy"
        ids_path = root / "point_ids.npy"
        positions_writer = np.lib.format.open_memmap(
            positions_path,
            mode="w+",
            dtype=np.float64,
            shape=(point_count, 3),
        )
        ids_writer = np.lib.format.open_memmap(
            ids_path,
            mode="w+",
            dtype=np.uint64,
            shape=(point_count,),
        )
        for start in range(0, point_count, 25_000):
            """원본 생성도 작은 chunk만 사용해 대형 일반 ndarray를 만들지 않습니다."""

            stop = min(start + 25_000, point_count)
            values = np.arange(start, stop, dtype=np.float64)
            positions_writer[start:stop] = np.column_stack(
                (
                    np.sin(values * 0.001),
                    np.cos(values * 0.0017),
                    np.mod(values, 701.0) * 0.002,
                )
            )
            ids_writer[start:stop] = np.arange(start, stop, dtype=np.uint64) + np.uint64(50_000)
        del positions_writer
        del ids_writer
        positions = np.load(positions_path, mmap_mode="r")
        point_ids = np.load(ids_path, mmap_mode="r")
        cloud = IcpPointCloud.from_verified_arrays(
            model_id="memmap",
            positions_float64=positions,
            point_ids_uint64=point_ids,
        )
        assert cloud.positions_float64 is positions
        assert cloud.point_ids_uint64 is point_ids
        first = build_budgeted_voxel_sample(cloud, 257, chunk_size=25_000)
        second = build_budgeted_voxel_sample(cloud, 257, chunk_size=25_000)
        assert first.count <= 257
        assert np.array_equal(first.point_ids_uint64, second.point_ids_uint64)
        assert first.positions_float64.flags.writeable is False
        # Windows는 memmap 참조가 남아 있으면 TemporaryDirectory 정리를 거부하므로 명시 해제합니다.
        del first
        del second
        del cloud
        positions._mmap.close()
        point_ids._mmap.close()
        del positions
        del point_ids
        gc.collect()


def test_uniform_density_sample_preserves_sparse_macro_cell_with_shared_origin() -> None:
    """공통 voxel 원점과 macro-cell quota가 희소 영역도 표본에 남기는지 확인합니다."""

    from tools.align.alignment import IcpPointCloud, build_uniform_density_sample

    generator = np.random.default_rng(20_260_816)
    dense = np.column_stack(
        (
            generator.uniform(0.0, 0.8, size=96),
            generator.uniform(-0.4, 0.4, size=96),
            generator.uniform(-0.4, 0.4, size=96),
        )
    )
    sparse = np.asarray(
        (
            (9.2, -0.2, -0.2),
            (9.4, 0.2, -0.1),
            (9.6, -0.1, 0.2),
            (9.8, 0.1, 0.1),
        ),
        dtype=np.float64,
    )
    positions = np.vstack((dense, sparse))
    point_ids = np.arange(positions.shape[0], dtype=np.uint64) + np.uint64(900)
    cloud = IcpPointCloud("A_macro", positions, point_ids)
    origin = np.asarray((0.0, -1.0, -1.0), dtype=np.float64)
    bounds = (
        np.asarray((0.0, -1.0, -1.0), dtype=np.float64),
        np.asarray((10.0, 1.0, 1.0), dtype=np.float64),
    )
    sample = build_uniform_density_sample(
        cloud,
        12,
        shared_voxel_size=0.001,
        shared_origin_float64=origin,
        overlap_bounds_float64=bounds,
        candidate_bounds_float64=bounds,
        macro_cell_divisions=2,
    )
    assert sample.count == 12
    assert np.array_equal(sample.origin_float64, origin)
    assert np.count_nonzero(sample.positions_float64[:, 0] >= 9.0) >= 1


def test_macro_cell_quota_has_deterministic_capacity_boundaries() -> None:
    """macro-cell quota가 용량을 넘지 않고 같은 입력에서 같은 결과를 내는지 확인합니다."""

    from tools.align.alignment import allocate_macro_cell_quotas

    counts = {(0, 0, 0): 100, (1, 0, 0): 2, (1, 1, 0): 5}
    first = allocate_macro_cell_quotas(counts, 11, minimum_quota=1)
    second = allocate_macro_cell_quotas(counts, 11, minimum_quota=1)
    assert first == second
    assert sum(first.values()) == 11
    assert all(1 <= first[key] <= counts[key] for key in counts)


def run_all() -> None:
    """ICP 샘플링 순수 회귀를 Blender 프로세스에서 순서대로 실행합니다."""

    _add_source_path()
    test_voxel_representative_ids_are_deterministic_across_input_order()
    test_budgeted_voxel_sample_uses_candidate_indices_and_fixed_budget()
    test_duplicate_ids_and_cancelled_budget_search_are_rejected()
    test_multi_reference_quota_uses_crop_capacity_and_overlap_weight()
    test_large_readonly_memmap_keeps_only_budgeted_voxel_result()
    test_uniform_density_sample_preserves_sparse_macro_cell_with_shared_origin()
    test_macro_cell_quota_has_deterministic_capacity_boundaries()
    print("test_icp_sampling: 통과")


if __name__ == "__main__":
    run_all()
