"""small_gicp backend 선택과 외부 worker 결과 계약을 검증합니다."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import sys

import numpy as np


def _add_test_paths() -> None:
    """애드온 소스와 기존 Open3D fixture 경로를 모듈 탐색 경로에 추가합니다."""

    test_path = Path(__file__).resolve().parent
    source_path = test_path.parents[2]
    for path in (source_path, test_path):
        path_text = str(path)
        if path_text not in sys.path:
            sys.path.insert(0, path_text)


def _expect_protocol_error(callback, description: str) -> None:
    """지정 호출이 worker protocol 오류로 거부되는지 확인합니다."""

    from tools.align.alignment.icp_backend import IcpJobProtocolError

    try:
        callback()
    except IcpJobProtocolError:
        return
    raise AssertionError(f"{description}가 small_gicp protocol에 수용되었습니다.")


def _rotation_z_matrix(angle_degrees: float) -> np.ndarray:
    """지정 각도의 Z축 proper rotation 행렬을 float64로 만듭니다."""

    angle = float(np.deg2rad(angle_degrees))
    cosine = float(np.cos(angle))
    sine = float(np.sin(angle))
    return np.asarray(
        (
            (cosine, -sine, 0.0),
            (sine, cosine, 0.0),
            (0.0, 0.0, 1.0),
        ),
        dtype=np.float64,
    )


def _distribution_metrics(count: int, rmse: float = 0.01) -> dict[str, object]:
    """Small GICP 분포 가중 RMS 결과 계약을 만족하는 테스트 지표를 만듭니다."""

    return {
        "point_to_point_rmse": rmse,
        "weighted_point_to_point_rmse": rmse,
        "residual_median": rmse,
        "residual_p90": rmse,
        "evaluation_correspondence_count": count,
        "low_weight_correspondence_count": 0,
        "isolation_candidate_correspondence_count": 0,
        "source_isolation_candidate_count": 0,
        "target_isolation_candidate_count": 0,
        "distance_unit": "PROJECT_WORLD_UNIT",
    }


def _external_result_metadata(input_data, job_id: str, diagnostics) -> dict[str, object]:
    """Small GICP Runtime 검증에 필요한 최소 외부 결과 metadata를 만듭니다."""

    source_count = int(input_data.moving_positions.shape[0])
    target_count = sum(int(value.shape[0]) for value in input_data.reference_positions)
    source_positions = "source-positions"
    source_ids = "source-point-ids"
    target_positions = "target-positions"
    target_ids = "target-point-ids"
    stage_audit = {
        "source_positions_fingerprint": source_positions,
        "target_positions_fingerprint": target_positions,
        "source_point_ids_fingerprint": source_ids,
        "target_point_ids_fingerprint": target_ids,
    }
    scale_mode = str(input_data.parameters.scale_mode.value).upper()
    scale_status = "ACCEPTED" if scale_mode == "REFINE" else "NOT_REQUESTED"
    return {
        "job_id": job_id,
        "input_fingerprint": input_data.input_fingerprint,
        "algorithm_version": input_data.algorithm_version,
        "backend_id": input_data.solver_backend,
        "backend_algorithm_version": input_data.backend_algorithm_version,
        "job_protocol_version": "icp-job-protocol-v1",
        "moving_model_id": input_data.seed.moving_model_id,
        "reference_model_ids": list(input_data.reference_model_ids),
        "quality": {
            "status": "PASS",
            "review_reasons": [],
            "scale_refine_status": scale_status,
            "scale_refine_reason": "",
        },
        "level_results": [{"level_name": "fine", "stop_reason": "CONVERGED"}],
        "parameter_fingerprint": input_data.parameters.fingerprint,
        "model_revision_snapshot": [list(item) for item in input_data.model_revision_snapshot],
        "model_state_snapshot": [list(item) for item in input_data.model_state_snapshot],
        "marker_signature": input_data.seed.marker_signature,
        "solver_stop_reason": "CONVERGED",
        "user_review_required": False,
        "full_input_audit": {
            "input_policy": "FULL_OVERLAP_NO_DOWNSAMPLING",
            "source_original_count": source_count,
            "target_original_count": target_count,
            "source_crop_count": source_count,
            "target_crop_count": target_count,
            "source_crop_point_ids_fingerprint": source_ids,
            "target_crop_point_ids_fingerprint": target_ids,
            "source_crop_positions_fingerprint": source_positions,
            "target_crop_positions_fingerprint": target_positions,
            "stage_input_fingerprints": {"fine": stage_audit},
            "downsampling": {
                "used": False,
                "voxel_down_sample": False,
                "point_budget_sampling": False,
            },
        },
        "worker_python_version": "3.12.11",
        "worker_numpy_version": "1.26.4",
        "small_gicp_version": "1.0.1",
        "platform_architecture": "64bit",
        "local_origin": diagnostics.local_origin_float64.tolist(),
        "moving_diagonal": 10.0,
        "delta_translation": diagnostics.translation_distance,
        "delta_rotation_degrees": diagnostics.rotation_degrees,
        "scale_mode": scale_mode,
        "scale_refine_status": scale_status,
        "baseline_metrics": _distribution_metrics(source_count),
        "best_candidate_metrics": _distribution_metrics(source_count),
        "confidence_preprocessing": {
            "enabled": True,
            "k": 24,
            "cache_hit_count": 0,
            "cache_miss_count": 2,
            "calculation_seconds": 0.01,
            "models": [],
        },
    }


def test_small_gicp_is_explicit_external_backend() -> None:
    """small_gicp가 Open3D와 구분되는 외부 CPython backend인지 확인합니다."""

    from tools.align.alignment.icp_solver_backend import (
        IcpSolverBackend,
        backend_algorithm_version,
        is_external_python_backend,
        is_open3d_backend,
    )

    backend = IcpSolverBackend.SMALL_GICP
    assert backend.value == "SMALL_GICP"
    assert backend_algorithm_version(backend) == "small-gicp-v6"
    assert is_external_python_backend(backend) is True
    assert is_open3d_backend(backend) is False


def test_bundled_small_gicp_python_uses_addon_relative_runtime_path() -> None:
    """Small GICP 내장 Python 경로가 실제 Align 모듈 위치를 기준으로 유지되는지 확인합니다."""

    from tools.align.properties import bundled_small_gicp_python_path

    python_path = Path(bundled_small_gicp_python_path())
    assert python_path.name == "python.exe"
    assert python_path.parent.name == "python"
    assert python_path.parent.parent.name == "runtime"
    from tools.align import properties

    assert python_path.parent.parent.parent == Path(properties.__file__).resolve().parent


def test_small_gicp_result_requires_own_library_version() -> None:
    """small_gicp 결과가 고정 library·backend 알고리즘 버전을 요구하는지 확인합니다."""

    from tools.align.alignment.icp_backend import validate_result_metadata_for_backend
    from tools.align.alignment.icp_solver_backend import (
        IcpSolverBackend,
        backend_algorithm_version,
    )
    from test_icp_open3d_protocol import _open3d_result_metadata

    backend = IcpSolverBackend.SMALL_GICP
    metadata = _open3d_result_metadata("OPEN3D_GICP")
    metadata["backend_id"] = backend.value
    metadata["backend_algorithm_version"] = backend_algorithm_version(backend)
    metadata.pop("open3d_version")
    metadata["small_gicp_version"] = "1.0.1"
    metadata["baseline_metrics"] = _distribution_metrics(8)
    metadata["best_candidate_metrics"] = _distribution_metrics(8)
    metadata["confidence_preprocessing"] = {
        "enabled": True,
        "k": 24,
        "models": [],
    }
    metadata["quality"]["scale_refine_status"] = "NOT_REQUESTED"
    metadata["quality"]["scale_refine_reason"] = ""
    validate_result_metadata_for_backend(metadata, backend)

    missing_version = dict(metadata)
    missing_version.pop("small_gicp_version")
    _expect_protocol_error(
        lambda: validate_result_metadata_for_backend(missing_version, backend),
        "small_gicp 버전이 없는 결과",
    )

    wrong_version = dict(metadata)
    wrong_version["small_gicp_version"] = "0.0.0"
    _expect_protocol_error(
        lambda: validate_result_metadata_for_backend(wrong_version, backend),
        "다른 small_gicp 버전 결과",
    )

    previous_algorithm = dict(metadata)
    previous_algorithm["backend_algorithm_version"] = "small-gicp-v4"
    _expect_protocol_error(
        lambda: validate_result_metadata_for_backend(previous_algorithm, backend),
        "이전 Small GICP 알고리즘 결과",
    )


def test_small_gicp_requires_external_python_before_creating_job() -> None:
    """small_gicp Python 경로가 없으면 임시 작업을 만들기 전에 거부하는지 확인합니다."""

    from tools.align.alignment import icp_runtime as runtime_module
    from tools.align.alignment.icp_runtime import IcpRuntimeError, begin_icp_job
    from test_icp_open3d_runtime import _runtime_fixture

    runtime = _runtime_fixture()
    create_called = False
    original_create = runtime_module.create_icp_job_directory

    def _unexpected_create():
        """외부 Python 경로 검증 전 작업 폴더 생성을 즉시 실패시킵니다."""

        nonlocal create_called
        create_called = True
        raise AssertionError("small_gicp Python 경로 확인 전에 작업 폴더를 만들었습니다.")

    runtime_module.create_icp_job_directory = _unexpected_create
    try:
        try:
            begin_icp_job(
                runtime,
                solver_backend="SMALL_GICP",
                scale_refine_enabled=True,
            )
        except IcpRuntimeError as error:
            assert "CPython 3.12" in str(error)
        else:
            raise AssertionError("small_gicp Python 경로 없이 작업을 시작했습니다.")
    finally:
        runtime_module.create_icp_job_directory = original_create
    assert create_called is False


def test_external_sim3_diagnostics_are_stable_across_rotation_ranges() -> None:
    """공통 Sim(3) 분해가 identity부터 180도 부근까지 안정적인 회전각을 반환하는지 확인합니다."""

    from tools.align.alignment.icp_external_contract import analyze_external_icp_delta

    cases = (
        (0.0, 1.0),
        (37.25, 1.0),
        (179.999999, 1.0),
    )
    for expected_angle, scale in cases:
        matrix = np.identity(4, dtype=np.float64)
        matrix[:3, :3] = scale * _rotation_z_matrix(expected_angle)
        diagnostics = analyze_external_icp_delta(
            matrix,
            np.asarray((1200.0, -850.0, 45.0), dtype=np.float64),
            field_name="회전 범위 fixture",
        )
        assert np.isclose(diagnostics.uniform_scale, scale, rtol=0.0, atol=1.0e-15)
        assert np.isclose(diagnostics.rotation_degrees, expected_angle, rtol=0.0, atol=1.0e-9)

    near_unit = np.identity(4, dtype=np.float64)
    near_unit[0, 0] = np.nextafter(1.0, 0.0)
    determinant_diagnostics = analyze_external_icp_delta(
        near_unit,
        np.zeros(3, dtype=np.float64),
        field_name="행렬식 경계 fixture",
    )
    determinant = float(np.linalg.det(near_unit[:3, :3]))
    assert determinant < 1.0
    assert abs(determinant - 0.9999999999999999) <= np.finfo(np.float64).eps
    assert determinant_diagnostics.rotation_degrees == 0.0


def test_refine_near_identity_rotation_uses_exact_npz_common_diagnostics() -> None:
    """실제 거짓 오류를 만든 Refine 행렬이 worker 보고값과 같은 공통 경로로 통과하는지 확인합니다."""

    from dataclasses import replace
    from types import SimpleNamespace

    from tools.align.alignment import create_icp_input_snapshot
    from tools.align.alignment.icp_external_contract import analyze_external_icp_delta
    from tools.align.alignment.icp_runtime import IcpJobProtocolError, _runtime_result_from_worker
    from tools.align.alignment.icp_solver_backend import backend_algorithm_version
    from tools.align.constants import ICP_ALIGNMENT_PHASE_RUNNING
    from test_icp_open3d_runtime import _runtime_fixture

    runtime = _runtime_fixture()
    input_data = replace(
        create_icp_input_snapshot(runtime, scale_refine_enabled=True),
        solver_backend="SMALL_GICP",
        backend_algorithm_version=backend_algorithm_version("SMALL_GICP"),
    )
    job_id = "small-gicp-near-identity-refine"
    state = runtime.icp_alignment
    state.phase = ICP_ALIGNMENT_PHASE_RUNNING
    state.input_data = input_data
    state.job_handle = SimpleNamespace(job_id=job_id)

    # 이 행렬은 SVD scale 분해 시 1.1197136397927998e-05도였지만
    # 기존 행렬식 scale + acos(trace) 경로에서는 1.1262047877356564e-05도가 됐습니다.
    icp_delta = np.identity(4, dtype=np.float64)
    icp_delta[:3, :3] = 1.05 * _rotation_z_matrix(1.1197136397927998e-05)
    local_origin = np.asarray((1200.0, -850.0, 45.0), dtype=np.float64)
    diagnostics = analyze_external_icp_delta(
        icp_delta,
        local_origin,
        field_name="실제 Refine 실패 fixture",
    )
    metadata = _external_result_metadata(input_data, job_id, diagnostics)
    metadata["moving_diagonal"] = 10_000.0
    initial_alignment = np.array(
        input_data.seed.initial_alignment_float64,
        dtype=np.float64,
        order="C",
        copy=True,
    )
    arrays = {
        "initial_alignment": initial_alignment,
        "final_alignment": np.ascontiguousarray(icp_delta @ initial_alignment),
        "icp_delta": icp_delta,
    }

    result = _runtime_result_from_worker(runtime, state, metadata, arrays)

    assert result.solver_backend == "SMALL_GICP"
    assert diagnostics.rotation_degrees == metadata["delta_rotation_degrees"]
    assert np.isclose(diagnostics.rotation_degrees, 1.1197136397927998e-05, atol=1.0e-15)

    inconsistent_metadata = dict(metadata)
    inconsistent_metadata["delta_rotation_degrees"] = diagnostics.rotation_degrees + 1.0e-4
    try:
        _runtime_result_from_worker(runtime, state, inconsistent_metadata, arrays)
    except IcpJobProtocolError as error:
        assert "외부 ICP 결과 추가 회전량" in str(error)
        assert "Open3D 결과" not in str(error)
    else:
        raise AssertionError("Small GICP의 불일치 회전 진단값이 Runtime에 수용됐습니다.")


def test_small_gicp_npz_motion_limits_still_block_unsafe_results() -> None:
    """Small GICP 안전 한도가 JSON이 아닌 NPZ delta의 실제 이동·회전을 계속 차단하는지 확인합니다."""

    from dataclasses import replace
    from types import SimpleNamespace

    from tools.align.alignment import create_icp_input_snapshot
    from tools.align.alignment.icp_external_contract import analyze_external_icp_delta
    from tools.align.alignment.icp_runtime import IcpJobProtocolError, _runtime_result_from_worker
    from tools.align.alignment.icp_solver_backend import backend_algorithm_version
    from tools.align.constants import ICP_ALIGNMENT_PHASE_RUNNING
    from test_icp_open3d_runtime import _runtime_fixture

    for case_name, icp_delta in (
        (
            "rotation",
            np.block(
                [
                    [_rotation_z_matrix(45.0), np.zeros((3, 1), dtype=np.float64)],
                    [np.zeros((1, 3), dtype=np.float64), np.ones((1, 1), dtype=np.float64)],
                ]
            ),
        ),
        (
            "translation",
            np.asarray(
                (
                    (1.0, 0.0, 0.0, 3.0),
                    (0.0, 1.0, 0.0, 0.0),
                    (0.0, 0.0, 1.0, 0.0),
                    (0.0, 0.0, 0.0, 1.0),
                ),
                dtype=np.float64,
            ),
        ),
    ):
        runtime = _runtime_fixture()
        input_data = replace(
            create_icp_input_snapshot(runtime, scale_refine_enabled=False),
            solver_backend="SMALL_GICP",
            backend_algorithm_version=backend_algorithm_version("SMALL_GICP"),
        )
        job_id = f"small-gicp-unsafe-{case_name}"
        state = runtime.icp_alignment
        state.phase = ICP_ALIGNMENT_PHASE_RUNNING
        state.input_data = input_data
        state.job_handle = SimpleNamespace(job_id=job_id)
        diagnostics = analyze_external_icp_delta(
            icp_delta,
            np.zeros(3, dtype=np.float64),
            field_name=f"{case_name} 안전 한도 fixture",
        )
        metadata = _external_result_metadata(input_data, job_id, diagnostics)
        if case_name == "translation":
            metadata["moving_diagonal"] = 10.0
        initial_alignment = np.array(
            input_data.seed.initial_alignment_float64,
            dtype=np.float64,
            order="C",
            copy=True,
        )
        arrays = {
            "initial_alignment": initial_alignment,
            "final_alignment": np.ascontiguousarray(icp_delta @ initial_alignment),
            "icp_delta": icp_delta,
        }
        try:
            _runtime_result_from_worker(runtime, state, metadata, arrays)
        except IcpJobProtocolError as error:
            assert "안전 한도" in str(error)
        else:
            raise AssertionError(f"NPZ {case_name} 안전 한도 초과 결과가 수용됐습니다.")


def test_small_gicp_runtime_accepts_only_bounded_refine_delta() -> None:
    """Blender runtime이 Small GICP의 ±5% Sim(3)만 수용하는지 확인합니다."""

    from dataclasses import replace

    from tools.align.alignment import create_icp_input_snapshot
    from tools.align.alignment.icp_runtime import (
        IcpJobProtocolError,
        _validate_external_delta_scale,
    )
    from tools.align.alignment.icp_solver_backend import backend_algorithm_version
    from test_icp_open3d_runtime import _runtime_fixture

    input_data = replace(
        create_icp_input_snapshot(_runtime_fixture(), scale_refine_enabled=True),
        solver_backend="SMALL_GICP",
        backend_algorithm_version=backend_algorithm_version("SMALL_GICP"),
    )
    _validate_external_delta_scale(
        input_data,
        1.03,
    )
    try:
        _validate_external_delta_scale(
            input_data,
            1.051,
        )
    except IcpJobProtocolError:
        pass
    else:
        raise AssertionError("Small GICP의 ±5% 밖 delta scale이 runtime에 수용됐습니다.")


def test_scale_refine_solver_changes_only_uniform_scale_and_translation() -> None:
    """Refine 내부 스케일 보정 수치 함수 자체는 회전을 추가하지 않는지 확인합니다."""

    from tools.align.alignment.icp_scale_refine import (
        solve_bounded_uniform_scale_matrix,
    )

    moving = np.asarray(
        ((0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 2.0, 0.0), (0.0, 0.0, 3.0)),
        dtype=np.float64,
    )
    expected_scale = 1.03
    expected_translation = np.asarray((0.2, -0.1, 0.05), dtype=np.float64)
    reference = expected_scale * moving + expected_translation
    matrix = solve_bounded_uniform_scale_matrix(moving, reference, (0.95, 1.05))
    np.testing.assert_allclose(matrix[:3, :3], expected_scale * np.identity(3), atol=1.0e-12)
    np.testing.assert_allclose(matrix[:3, 3], expected_translation, atol=1.0e-12)


def test_scale_refine_keeps_worse_rms_candidate_for_user_review() -> None:
    """RMS가 나빠진 유효 Refine 스케일 후보도 자동 폐기하지 않는지 확인합니다."""

    from tools.align.alignment import icp_small_gicp_solver as solver_module

    source = np.asarray(
        ((0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)),
        dtype=np.float64,
    )
    pairs = np.column_stack((np.arange(4), np.arange(4))).astype(np.int64)
    fixed_metrics = {
        "point_to_point_rmse": 0.01,
        "fitness": 1.0,
        "reciprocal_ratio": 1.0,
        "inlier_count": 4,
        "valid": True,
    }
    refined_metrics = {
        **fixed_metrics,
        "point_to_point_rmse": 0.02,
    }
    scale_step = np.identity(4, dtype=np.float64)
    scale_step[:3, :3] *= 1.01
    originals = (
        solver_module._scale_refine_skip_reason,
        solver_module._reciprocal_pairs,
        solver_module.solve_bounded_uniform_scale_matrix,
        solver_module._evaluate_on_full_crop,
    )
    solver_module._scale_refine_skip_reason = lambda *_args: None
    solver_module._reciprocal_pairs = lambda *_args, **_kwargs: pairs
    solver_module.solve_bounded_uniform_scale_matrix = lambda *_args: scale_step
    solver_module._evaluate_on_full_crop = lambda *_args, **_kwargs: refined_metrics
    try:
        delta, metrics, status, reason, iterations = solver_module._run_bounded_scale_refinement(
            SimpleNamespace(
                parameters={
                    "scale_mode": "REFINE",
                    "scale_refine_enabled": True,
                    "scale_refine_max_relative_change": 0.05,
                    "scale_refine_max_step": 0.02,
                    "scale_refine_max_iterations": 1,
                    "scale_refine_min_inliers": 3,
                    "scale_refine_min_rmse_improvement": 0.02,
                }
            ),
            object(),
            source,
            source,
            object(),
            np.zeros_like(source),
            np.ones(source.shape[0], dtype=np.float64),
            np.ones(source.shape[0], dtype=np.float64),
            np.zeros(source.shape[0], dtype=bool),
            np.zeros(source.shape[0], dtype=bool),
            np.identity(4, dtype=np.float64),
            fixed_metrics,
            {},
            1.0,
            minimum_correspondences=3,
            evaluation_fingerprint="fixture",
            local_origin=np.zeros(3, dtype=np.float64),
            num_threads=1,
            progress_sink=None,
            cancel_probe=None,
        )
    finally:
        (
            solver_module._scale_refine_skip_reason,
            solver_module._reciprocal_pairs,
            solver_module.solve_bounded_uniform_scale_matrix,
            solver_module._evaluate_on_full_crop,
        ) = originals
    assert status == "ACCEPTED"
    assert metrics is refined_metrics
    assert iterations == 1
    assert "후보를 유지" in reason
    assert np.isclose(np.linalg.det(delta[:3, :3]), 1.01 ** 3)


def run_all() -> None:
    """Blender 기본 환경에서 small_gicp backend protocol 회귀를 실행합니다."""

    _add_test_paths()
    test_small_gicp_is_explicit_external_backend()
    test_bundled_small_gicp_python_uses_addon_relative_runtime_path()
    test_small_gicp_result_requires_own_library_version()
    test_small_gicp_requires_external_python_before_creating_job()
    test_external_sim3_diagnostics_are_stable_across_rotation_ranges()
    test_refine_near_identity_rotation_uses_exact_npz_common_diagnostics()
    test_small_gicp_npz_motion_limits_still_block_unsafe_results()
    test_small_gicp_runtime_accepts_only_bounded_refine_delta()
    test_scale_refine_solver_changes_only_uniform_scale_and_translation()
    test_scale_refine_keeps_worse_rms_candidate_for_user_review()
    print("test_icp_small_gicp_backend: 통과")


if __name__ == "__main__":
    run_all()
