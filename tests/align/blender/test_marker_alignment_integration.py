"""실제 COLMAP ImportJob과 최소 3점 마커 미리보기의 통합 경계를 검증합니다."""

from __future__ import annotations

import sys
from pathlib import Path
from tempfile import TemporaryDirectory

import numpy as np


def _add_test_support_path() -> None:
    """공유 COLMAP fixture 도우미가 있는 Blender 테스트 폴더를 모듈 경로에 추가합니다."""
    test_directory = str(Path(__file__).resolve().parent)
    if test_directory not in sys.path:
        sys.path.insert(0, test_directory)


def _apply_delta_to_vertices(matrix_float64: np.ndarray, vertices_float64: np.ndarray) -> np.ndarray:
    """column-vector 4×4 delta 행렬을 P Project World 삼각형 정점에 정확히 한 번 적용합니다."""
    matrix = np.asarray(matrix_float64, dtype=np.float64)
    vertices = np.asarray(vertices_float64, dtype=np.float64)
    assert matrix.shape == (4, 4)
    assert vertices.shape == (3, 3)
    homogeneous = np.concatenate(
        (vertices, np.ones((vertices.shape[0], 1), dtype=np.float64)),
        axis=1,
    )
    transformed = (matrix @ homogeneous.T).T
    np.testing.assert_allclose(transformed[:, 3], 1.0, rtol=0.0, atol=1.0e-12)
    return transformed[:, :3]


def _matrix_copy(transform: object) -> np.ndarray:
    """불변 Sim(3) 객체의 4×4 행렬을 비교용 float64 복사본으로 반환합니다."""
    return np.array(transform.matrix_float64, dtype=np.float64, copy=True)


def test_imported_colmap_marker_preview_aligns_representative_triangle_and_cancels() -> None:
    """두 실제 text COLMAP 모델의 세 마커가 preview 삼각형에서만 일치하고 취소로 복원되는지 확인합니다."""
    _add_test_support_path()
    from marker_alignment_test_support import (
        add_source_path,
        add_three_completed_marker_pairs,
        blender_data_snapshot,
        import_known_sim3_models,
    )

    add_source_path()
    from tools.align.alignment import (
        begin_marker_preview,
        cancel_marker_preview,
        effective_alignment_for_model,
    )
    from tools.align.constants import (
        MARKER_ALIGNMENT_PHASE_IDLE,
        MARKER_ALIGNMENT_PHASE_PREVIEW,
    )
    from tools.align.state import RuntimeState

    runtime = RuntimeState()
    runtime.session_active = True
    data_blocks_before = blender_data_snapshot()
    with TemporaryDirectory() as temporary_directory:
        fixture = import_known_sim3_models(runtime, Path(temporary_directory))
        pair_ids = add_three_completed_marker_pairs(runtime, fixture)

        data_blocks_after_import = blender_data_snapshot()
        assert data_blocks_after_import == data_blocks_before
        committed_before = _matrix_copy(
            runtime.model_repository.alignment_transform_for_model_id(
                fixture.moving_model_id
            )
        )

        begin_marker_preview(runtime=runtime)

        preview_state = runtime.marker_alignment
        assert preview_state.phase == MARKER_ALIGNMENT_PHASE_PREVIEW
        assert preview_state.result is not None
        result = preview_state.result
        triangle = result.representative_triangle
        assert set(triangle.pair_ids) == set(pair_ids)
        assert tuple(triangle.reference_vertices.shape) == (3, 3)
        assert tuple(triangle.moving_vertices.shape) == (3, 3)

        transformed_moving_vertices = _apply_delta_to_vertices(
            result.delta_transform.matrix_float64,
            triangle.moving_vertices,
        )
        np.testing.assert_allclose(
            transformed_moving_vertices,
            triangle.reference_vertices,
            rtol=0.0,
            atol=1.0e-9,
        )
        np.testing.assert_allclose(
            _matrix_copy(
                runtime.model_repository.alignment_transform_for_model_id(
                    fixture.moving_model_id
                )
            ),
            committed_before,
            rtol=0.0,
            atol=0.0,
        )
        np.testing.assert_allclose(
            _matrix_copy(effective_alignment_for_model(runtime, fixture.moving_model_id)),
            _matrix_copy(result.preview_alignment),
            rtol=0.0,
            atol=1.0e-12,
        )
        assert blender_data_snapshot() == data_blocks_before

        cancel_marker_preview(runtime=runtime)

        assert runtime.marker_alignment.phase == MARKER_ALIGNMENT_PHASE_IDLE
        assert runtime.marker_alignment.result is None
        np.testing.assert_allclose(
            _matrix_copy(
                runtime.model_repository.alignment_transform_for_model_id(
                    fixture.moving_model_id
                )
            ),
            committed_before,
            rtol=0.0,
            atol=0.0,
        )
        np.testing.assert_allclose(
            _matrix_copy(effective_alignment_for_model(runtime, fixture.moving_model_id)),
            committed_before,
            rtol=0.0,
            atol=1.0e-12,
        )
        assert blender_data_snapshot() == data_blocks_before


def run_all() -> None:
    """실제 가져오기 기반 최소 3점 마커 정렬 통합 검사를 실행합니다."""
    test_imported_colmap_marker_preview_aligns_representative_triangle_and_cancels()
    print("test_marker_alignment_integration: 통과")


if __name__ == "__main__":
    run_all()
