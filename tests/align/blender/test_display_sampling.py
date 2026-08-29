"""POINT3D_ID 기반 결정적 뷰포트 표시 샘플링을 검증합니다."""

from __future__ import annotations

from pathlib import Path
import sys

import numpy as np


def _add_source_path() -> None:
    """애드온 소스 디렉터리를 Blender 테스트 모듈 탐색 경로에 추가합니다."""
    source_path = Path(__file__).resolve().parents[3]
    source_text = str(source_path)
    if source_text not in sys.path:
        sys.path.insert(0, source_text)


def _selected_point_ids(point_ids: np.ndarray, display_indices: np.ndarray) -> set[int]:
    """표시 원본 인덱스를 선택된 POINT3D_ID 집합으로 변환합니다."""
    return {int(point_ids[index]) for index in display_indices}


def _assert_sorted_unique_indices(indices: np.ndarray, point_count: int, limit: int) -> None:
    """표시 인덱스의 개수·정렬·고유성·원본 배열 범위를 함께 확인합니다."""
    assert np.issubdtype(indices.dtype, np.unsignedinteger)
    assert indices.size == limit
    assert np.all(indices < point_count)
    assert np.array_equal(indices, np.sort(indices))
    assert np.unique(indices).size == indices.size


def test_default_limit_and_under_or_equal_limit_return_all_indices() -> None:
    """기본 25만 상한과 N이 상한 이하인 전체 표시 계약을 확인합니다."""
    from tools.align.viewport.display_sampling import (
        DEFAULT_DISPLAY_POINT_LIMIT,
        build_display_indices,
    )

    assert DEFAULT_DISPLAY_POINT_LIMIT == 250_000
    under_point_ids = np.asarray((91, 4, 800, 17), dtype=np.uint64)
    equal_point_ids = np.arange(12, dtype=np.uint64) * np.uint64(101) + np.uint64(7)

    under_indices = build_display_indices(under_point_ids, max_points=10, chunk_size=2)
    equal_indices = build_display_indices(equal_point_ids, max_points=equal_point_ids.size, chunk_size=3)

    assert np.array_equal(under_indices, np.arange(under_point_ids.size, dtype=under_indices.dtype))
    assert np.array_equal(equal_indices, np.arange(equal_point_ids.size, dtype=equal_indices.dtype))


def test_over_limit_returns_exact_sorted_unique_indices_for_component_scale() -> None:
    """302,541개 입력에서 정확히 250,000개의 표시 원본 인덱스를 선택하는지 확인합니다."""
    from tools.align.viewport.display_sampling import build_display_indices

    point_count = 302_541
    point_ids = (
        np.arange(point_count, dtype=np.uint64) * np.uint64(1_000_003)
        + np.uint64(9_223_372_036_854_775)
    )

    indices = build_display_indices(point_ids, chunk_size=8_191)

    _assert_sorted_unique_indices(indices, point_count, 250_000)


def test_invalid_display_point_limit_inputs_are_rejected() -> None:
    """상한 0·bool·비정수 입력을 공개 동기 API가 명확히 거부하는지 확인합니다."""
    from tools.align.viewport.display_sampling import (
        DisplaySamplingError,
        build_display_indices,
    )

    point_ids = np.asarray((11, 22, 33), dtype=np.uint64)
    for invalid_limit in (0, False, True, 3.5):
        try:
            build_display_indices(point_ids, max_points=invalid_limit)
        except DisplaySamplingError:
            pass
        else:
            raise AssertionError(f"잘못된 표시 점 상한을 허용했습니다: {invalid_limit!r}")


def test_invalid_point_id_shapes_and_values_are_rejected() -> None:
    """2차원·음수·중복 POINT3D_ID 입력을 공개 동기 API가 거부하는지 확인합니다."""
    from tools.align.viewport.display_sampling import (
        DisplaySamplingError,
        build_display_indices,
    )

    invalid_point_id_arrays = (
        np.asarray(((1, 2), (3, 4)), dtype=np.uint64),
        np.asarray((1, -2, 3), dtype=np.int64),
        np.asarray((7, 8, 7), dtype=np.uint64),
    )
    for invalid_point_ids in invalid_point_id_arrays:
        try:
            build_display_indices(invalid_point_ids, max_points=2)
        except DisplaySamplingError:
            pass
        else:
            raise AssertionError("잘못된 POINT3D_ID 입력을 표시 샘플링이 허용했습니다.")


def test_selection_is_repeatable_and_independent_from_coordinates() -> None:
    """같은 ID 입력은 좌표 변환과 무관하게 항상 같은 표시 인덱스를 선택하는지 확인합니다."""
    from tools.align.viewport.display_sampling import build_display_indices

    point_ids = np.arange(211, dtype=np.uint64) * np.uint64(17) + np.uint64(2**63)
    source_positions = np.column_stack(
        (
            np.arange(point_ids.size, dtype=np.float64),
            np.full(point_ids.size, 3.0, dtype=np.float64),
            np.linspace(-5.0, 5.0, point_ids.size, dtype=np.float64),
        )
    )
    transformed_positions = (source_positions * np.asarray((3.0, -2.0, 0.5))) + np.asarray((10.0, 8.0, -4.0))
    assert not np.array_equal(source_positions, transformed_positions)

    first = build_display_indices(point_ids, max_points=71, chunk_size=31)
    second = build_display_indices(point_ids, max_points=71, chunk_size=31)

    assert np.array_equal(first, second)
    assert _selected_point_ids(point_ids, first) == _selected_point_ids(point_ids, second)


def test_reordered_point_ids_keep_the_same_selected_id_set() -> None:
    """원본 배열 순서가 달라도 선택되는 POINT3D_ID 집합이 같은지 확인합니다."""
    from tools.align.viewport.display_sampling import build_display_indices

    point_ids = (
        np.arange(1_003, dtype=np.uint64) * np.uint64(65_537)
        + np.uint64(19)
    )
    permutation = np.random.default_rng(20_260_815).permutation(point_ids.size)
    reordered_ids = point_ids[permutation]

    original_indices = build_display_indices(point_ids, max_points=127, chunk_size=29)
    reordered_indices = build_display_indices(reordered_ids, max_points=127, chunk_size=41)

    _assert_sorted_unique_indices(original_indices, point_ids.size, 127)
    _assert_sorted_unique_indices(reordered_indices, reordered_ids.size, 127)
    assert _selected_point_ids(point_ids, original_indices) == _selected_point_ids(
        reordered_ids,
        reordered_indices,
    )


def test_chunk_size_and_incremental_builder_match_synchronous_result() -> None:
    """청크 크기와 TIMER 증분 처리 방식이 동기 결과를 바꾸지 않는지 확인합니다."""
    from tools.align.viewport.display_sampling import (
        IncrementalDisplayIndexBuilder,
        build_display_indices,
    )

    point_ids = (
        np.arange(2_057, dtype=np.uint64) * np.uint64(4_294_967_291)
        + np.uint64(1_000_000_007)
    )
    synchronous = build_display_indices(point_ids, max_points=257, chunk_size=point_ids.size)
    small_chunk = build_display_indices(point_ids, max_points=257, chunk_size=17)
    builder = IncrementalDisplayIndexBuilder(point_ids, max_points=257, chunk_size=37)
    previous_processed_count = 0
    for _ in range(512):
        progress = builder.step(max_records=13, time_budget_seconds=1.0)
        assert progress.processed_count >= previous_processed_count
        assert progress.total_count == point_ids.size
        previous_processed_count = progress.processed_count
        if progress.is_completed:
            break
    else:
        raise AssertionError("증분 표시 샘플 빌더가 제한된 TIMER tick 안에 완료되지 않았습니다.")

    assert progress.display_count == 257
    assert progress.fraction == 1.0
    assert np.array_equal(synchronous, small_chunk)
    assert np.array_equal(synchronous, builder.display_indices)


def test_incremental_builder_rejects_result_access_before_completion() -> None:
    """증분 빌더가 완료 전에는 부분 결과 인덱스 접근을 거부하는지 확인합니다."""
    from tools.align.viewport.display_sampling import (
        DisplaySamplingStateError,
        IncrementalDisplayIndexBuilder,
    )

    point_ids = np.arange(64, dtype=np.uint64) + np.uint64(9_000)
    builder = IncrementalDisplayIndexBuilder(point_ids, max_points=3, chunk_size=8)
    try:
        _ = builder.display_indices
    except DisplaySamplingStateError:
        pass
    else:
        raise AssertionError("시작 전 표시 샘플 결과 접근을 허용했습니다.")

    progress = builder.step(max_records=1, time_budget_seconds=1.0)
    assert progress.is_completed is False
    try:
        _ = builder.display_indices
    except DisplaySamplingStateError:
        pass
    else:
        raise AssertionError("부분 처리 중 표시 샘플 결과 접근을 허용했습니다.")


def test_incremental_builder_cancel_rejects_result_access() -> None:
    """증분 샘플링 취소 뒤에는 결과 접근을 거부하고 취소 상태를 유지하는지 확인합니다."""
    from tools.align.viewport.display_sampling import (
        DisplaySamplingStateError,
        IncrementalDisplayIndexBuilder,
    )

    point_ids = np.arange(1_001, dtype=np.uint64) + np.uint64(50_000)
    builder = IncrementalDisplayIndexBuilder(point_ids, max_points=100, chunk_size=31)
    first_progress = builder.step(max_records=17, time_budget_seconds=1.0)
    assert first_progress.is_completed is False
    assert first_progress.is_cancelled is False

    builder.cancel()
    cancelled_progress = builder.step(max_records=17, time_budget_seconds=1.0)
    assert cancelled_progress.is_cancelled is True
    assert cancelled_progress.is_completed is False
    assert cancelled_progress.display_count == 0
    try:
        _ = builder.display_indices
    except DisplaySamplingStateError:
        pass
    else:
        raise AssertionError("취소된 표시 샘플 빌더에서 결과 인덱스 접근을 허용했습니다.")


def run_all() -> None:
    """결정적 표시 샘플링의 순수 경계 검사를 순서대로 실행합니다."""
    _add_source_path()
    test_default_limit_and_under_or_equal_limit_return_all_indices()
    test_over_limit_returns_exact_sorted_unique_indices_for_component_scale()
    test_invalid_display_point_limit_inputs_are_rejected()
    test_invalid_point_id_shapes_and_values_are_rejected()
    test_selection_is_repeatable_and_independent_from_coordinates()
    test_reordered_point_ids_keep_the_same_selected_id_set()
    test_chunk_size_and_incremental_builder_match_synchronous_result()
    test_incremental_builder_rejects_result_access_before_completion()
    test_incremental_builder_cancel_rejects_result_access()
    print("test_display_sampling: 통과")


if __name__ == "__main__":
    run_all()
