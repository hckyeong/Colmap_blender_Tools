"""마커 Sim(3) 순수 계산과 불변 타입의 Blender 실행형 회귀를 검증합니다."""

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


def _rotation_z_90() -> np.ndarray:
    """예상값을 사람이 검토하기 쉬운 Z축 90도 proper rotation을 반환합니다."""

    return np.asarray(
        (
            (0.0, -1.0, 0.0),
            (1.0, 0.0, 0.0),
            (0.0, 0.0, 1.0),
        ),
        dtype=np.float64,
    )


def _moving_points() -> np.ndarray:
    """비공선성과 추가 N점 검증에 사용할 결정적 P 마커 점 집합을 반환합니다."""

    return np.asarray(
        (
            (-1.0, 0.0, 0.5),
            (1.0, 0.0, -0.5),
            (0.0, 2.0, 0.0),
            (0.0, -1.0, 1.0),
            (2.0, 1.0, 3.0),
        ),
        dtype=np.float64,
    )


def _reference_points(moving: np.ndarray) -> np.ndarray:
    """알려진 Sim(3)을 적용해 정답 A 마커 점 집합을 만듭니다."""

    rotation = _rotation_z_90()
    translation = np.asarray((3.0, -2.0, 5.0), dtype=np.float64)
    return 2.5 * (moving @ rotation.T) + translation


def _assert_close(left: np.ndarray, right: np.ndarray, *, tolerance: float = 1.0e-9) -> None:
    """행렬·점 배열 오차를 테스트 실패 문구에 포함해 비교합니다."""

    assert np.allclose(left, right, rtol=0.0, atol=tolerance), (left, right)


def test_exact_similarity_transform_uses_all_markers() -> None:
    """알려진 5개 대응점에서 P→A Sim(3)과 전체 residual을 복원하는지 확인합니다."""

    from tools.align.alignment import (
        compute_marker_residual_metrics,
        solve_similarity_transform,
        transform_positions,
    )

    moving = _moving_points()
    reference = _reference_points(moving)
    transform = solve_similarity_transform(reference, moving)
    expected_rotation = _rotation_z_90()
    _assert_close(transform.rotation_float64, expected_rotation)
    assert abs(transform.uniform_scale - 2.5) <= 1.0e-10
    _assert_close(transform.translation_float64, np.asarray((3.0, -2.0, 5.0)))
    transformed = transform_positions(transform, moving)
    _assert_close(transformed, reference)
    metrics = compute_marker_residual_metrics(
        reference,
        transformed,
        uniform_scale=transform.uniform_scale,
        rotation_determinant=float(np.linalg.det(transform.rotation_float64)),
    )
    assert metrics.count == moving.shape[0]
    assert metrics.residuals_float64.shape == (moving.shape[0],)
    assert metrics.maximum <= 1.0e-10
    assert metrics.normalized_rms <= 1.0e-10


def test_rigid_marker_transform_keeps_unit_delta_scale() -> None:
    """마커 스케일을 끄면 같은 대응점에서 회전·이동만 계산하고 delta scale을 1로 유지하는지 확인합니다."""

    from tools.align.alignment import solve_similarity_transform, transform_positions

    moving = _moving_points()
    reference = _reference_points(moving)
    transform = solve_similarity_transform(
        reference,
        moving,
        use_uniform_scale=False,
    )
    _assert_close(transform.rotation_float64, _rotation_z_90())
    assert np.isclose(transform.uniform_scale, 1.0, rtol=0.0, atol=1.0e-12)
    transformed = transform_positions(transform, moving)
    _assert_close(np.mean(transformed, axis=0), np.mean(reference, axis=0))
    assert not np.allclose(transformed, reference, rtol=0.0, atol=1.0e-6)


def test_marker_scale_has_no_increase_or_decrease_rate_limit() -> None:
    """유한한 양수인 마커 스케일은 크기와 무관하게 그대로 계산되는지 확인합니다."""

    from tools.align.alignment import solve_similarity_transform, transform_positions

    moving = _moving_points()
    for expected_scale in (1.0e-14, 1.0e14):
        reference = expected_scale * moving
        transform = solve_similarity_transform(
            reference,
            moving,
            use_uniform_scale=True,
        )
        assert np.isclose(
            transform.uniform_scale,
            expected_scale,
            rtol=1.0e-10,
            atol=0.0,
        )
        transformed = transform_positions(transform, moving)
        relative_error = float(np.linalg.norm(transformed - reference)) / float(
            np.linalg.norm(reference)
        )
        assert relative_error <= 1.0e-10


def test_representative_triangle_is_deterministic_across_input_order() -> None:
    """대표 삼각형 선택이 입력 배열 순서가 달라도 안정 pair_id 기준으로 같은지 확인합니다."""

    from tools.align.alignment import select_representative_triangle

    moving = _moving_points()
    reference = _reference_points(moving)
    pair_ids = (17, 4, 31, 8, 22)
    first = select_representative_triangle(reference, moving, pair_ids)
    permutation = np.asarray((2, 4, 0, 3, 1), dtype=np.int64)
    second = select_representative_triangle(
        reference[permutation],
        moving[permutation],
        tuple(pair_ids[index] for index in permutation),
    )
    assert first.pair_ids == second.pair_ids
    assert tuple(sorted(first.pair_ids)) == first.pair_ids
    assert len(first.pair_ids) == 3


def test_reflection_and_degenerate_inputs_are_not_accepted_as_valid_sim3() -> None:
    """반사에는 proper rotation만 반환하고 공선·중복·형상 오류는 거부하는지 확인합니다."""

    from tools.align.alignment import (
        MarkerDegeneracyError,
        MarkerInputError,
        solve_similarity_transform,
    )

    moving = _moving_points()
    reflected_reference = moving.copy()
    reflected_reference[:, 0] *= -1.0
    reflection_result = solve_similarity_transform(reflected_reference, moving)
    assert np.linalg.det(reflection_result.rotation_float64) > 0.999999999

    collinear = np.asarray(
        ((0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (2.0, 0.0, 0.0)),
        dtype=np.float64,
    )
    duplicate = np.zeros((3, 3), dtype=np.float64)
    for invalid in (collinear, duplicate):
        try:
            solve_similarity_transform(invalid, invalid)
        except MarkerDegeneracyError:
            pass
        else:
            raise AssertionError("퇴화한 마커 입력이 거부되지 않았습니다.")
    try:
        solve_similarity_transform(np.zeros((3, 2), dtype=np.float64), np.zeros((3, 2)))
    except MarkerInputError:
        pass
    else:
        raise AssertionError("N×3이 아닌 마커 입력이 거부되지 않았습니다.")


def test_frozen_transform_and_input_do_not_alias_caller_arrays() -> None:
    """불변 타입이 호출자 배열을 잠그거나 alias를 공유하지 않는지 확인합니다."""

    from tools.align.alignment import MarkerAlignmentInput, Sim3Transform

    matrix = np.identity(4, dtype=np.float64)
    reference = _reference_points(_moving_points())
    moving = _moving_points()
    transform = Sim3Transform(matrix_float64=matrix)
    input_data = MarkerAlignmentInput(
        moving_model_id="moving",
        pair_ids=(1, 2, 3, 4, 5),
        reference_project=reference,
        moving_project=moving,
        marker_signature="signature",
        source_revision_snapshot=(("reference", 1), ("moving", 1)),
        reference_model_ids=("reference",),
    )
    assert matrix.flags.writeable is True
    assert reference.flags.writeable is True
    assert moving.flags.writeable is True
    assert not np.shares_memory(matrix, transform.matrix_float64)
    assert not np.shares_memory(reference, input_data.reference_project)
    assert not np.shares_memory(moving, input_data.moving_project)
    matrix[0, 0] = 9.0
    reference[0, 0] += 100.0
    moving[0, 0] += 100.0
    assert transform.matrix_float64[0, 0] == 1.0
    assert input_data.reference_project[0, 0] != reference[0, 0]
    assert input_data.moving_project[0, 0] != moving[0, 0]
    assert transform.matrix_float64.flags.writeable is False
    assert input_data.reference_project.flags.writeable is False
    assert input_data.moving_project.flags.writeable is False


def run_all() -> None:
    """마커 Sim(3) 순수 계산 회귀를 Blender 프로세스에서 순서대로 실행합니다."""

    _add_source_path()
    test_exact_similarity_transform_uses_all_markers()
    test_rigid_marker_transform_keeps_unit_delta_scale()
    test_marker_scale_has_no_increase_or_decrease_rate_limit()
    test_representative_triangle_is_deterministic_across_input_order()
    test_reflection_and_degenerate_inputs_are_not_accepted_as_valid_sim3()
    test_frozen_transform_and_input_do_not_alias_caller_arrays()
    print("test_marker_sim3: 통과")


if __name__ == "__main__":
    run_all()
