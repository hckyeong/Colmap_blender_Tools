"""Open3D backend 선택과 명시 실패 경계를 Blender runtime에서 검증합니다."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import sys
from types import SimpleNamespace

import numpy as np


def _add_test_paths() -> None:
    """애드온 소스와 기존 runtime fixture 경로를 모듈 탐색 경로에 추가합니다."""

    test_path = Path(__file__).resolve().parent
    source_path = test_path.parents[2]
    for path in (source_path, test_path):
        path_text = str(path)
        if path_text not in sys.path:
            sys.path.insert(0, path_text)


def _runtime_fixture():
    """기존 실제 marker preview fixture를 Open3D backend 테스트에 재사용합니다."""

    from test_icp_runtime import _runtime_with_marker_preview

    runtime, _marker_result = _runtime_with_marker_preview(count=64)
    return runtime


def test_open3d_requires_explicit_python_before_creating_job() -> None:
    """Open3D Python 경로가 없으면 작업 폴더나 worker를 만들지 않는지 확인합니다."""

    from tools.align.alignment import icp_runtime as runtime_module
    from tools.align.alignment.icp_runtime import IcpRuntimeError, begin_icp_job

    runtime = _runtime_fixture()
    create_called = False
    original_create = runtime_module.create_icp_job_directory

    def _unexpected_create():
        """환경 검증 전에 작업 폴더를 만들면 테스트를 실패시킵니다."""

        nonlocal create_called
        create_called = True
        raise AssertionError("Open3D Python 경로 확인 전에 작업 폴더를 만들었습니다.")

    runtime_module.create_icp_job_directory = _unexpected_create
    try:
        try:
            begin_icp_job(runtime, solver_backend="OPEN3D_GICP")
        except IcpRuntimeError as error:
            assert "CPython 3.12" in str(error)
        else:
            raise AssertionError("Open3D Python 경로 없이 작업을 시작했습니다.")
    finally:
        runtime_module.create_icp_job_directory = original_create
    assert create_called is False


def test_open3d_rejects_scale_refine_before_creating_job() -> None:
    """rigid Open3D delta에 Scale Refine이 섞이면 실행 전에 거부하는지 확인합니다."""

    from tools.align.alignment import icp_runtime as runtime_module
    from tools.align.alignment.icp_runtime import IcpRuntimeError, begin_icp_job

    runtime = _runtime_fixture()
    create_called = False
    original_create = runtime_module.create_icp_job_directory

    def _unexpected_create():
        """Scale Refine 검증 전에 작업 폴더를 만들면 테스트를 실패시킵니다."""

        nonlocal create_called
        create_called = True
        raise AssertionError("Open3D Scale Refine 확인 전에 작업 폴더를 만들었습니다.")

    runtime_module.create_icp_job_directory = _unexpected_create
    try:
        try:
            begin_icp_job(
                runtime,
                solver_backend="OPEN3D_GICP",
                scale_refine_enabled=True,
                open3d_python_path=str(Path(__file__).resolve()),
            )
        except IcpRuntimeError as error:
            assert "Scale Refine" in str(error)
        else:
            raise AssertionError("Open3D backend가 Scale Refine 작업을 시작했습니다.")
    finally:
        runtime_module.create_icp_job_directory = original_create
    assert create_called is False


def test_open3d_launch_failure_does_not_fallback_to_custom_worker() -> None:
    """Open3D 시작 실패가 기존 Hybrid worker로 조용히 우회하지 않는지 확인합니다."""

    from tools.align.alignment import icp_runtime as runtime_module
    from tools.align.alignment.icp_backend import IcpBackendError
    from tools.align.alignment.icp_runtime import IcpRuntimeError, begin_icp_job
    from tools.align.constants import ICP_ALIGNMENT_PHASE_FAILED

    runtime = _runtime_fixture()
    open3d_calls = 0
    custom_calls = 0
    created_job_path: Path | None = None
    original_open3d_launch = runtime_module.launch_open3d_worker
    original_custom_launch = runtime_module.launch_icp_worker

    def _fail_open3d(files, _python_path, _input_fingerprint):
        """Open3D launcher가 환경 검사에 실패한 상황을 재현합니다."""

        nonlocal open3d_calls, created_job_path
        open3d_calls += 1
        created_job_path = files.job_directory
        raise IcpBackendError("의도한 Open3D 환경 검사 실패")

    def _unexpected_custom(*_args, **_kwargs):
        """숨은 Custom fallback 호출을 기록하고 즉시 실패시킵니다."""

        nonlocal custom_calls
        custom_calls += 1
        raise AssertionError("Open3D 실패 뒤 Custom worker를 호출했습니다.")

    runtime_module.launch_open3d_worker = _fail_open3d
    runtime_module.launch_icp_worker = _unexpected_custom
    try:
        try:
            begin_icp_job(
                runtime,
                solver_backend="OPEN3D_GICP",
                open3d_python_path=str(Path(__file__).resolve()),
            )
        except IcpRuntimeError as error:
            assert "의도한 Open3D 환경 검사 실패" in str(error)
        else:
            raise AssertionError("실패한 Open3D 작업이 정상 시작으로 처리됐습니다.")
    finally:
        runtime_module.launch_open3d_worker = original_open3d_launch
        runtime_module.launch_icp_worker = original_custom_launch

    assert open3d_calls == 1
    assert custom_calls == 0
    assert runtime.icp_alignment.phase == ICP_ALIGNMENT_PHASE_FAILED
    assert created_job_path is not None and not created_job_path.exists()


def test_open3d_result_normalizes_to_runtime_preview_candidate() -> None:
    """유효한 Open3D 결과가 기존 미리보기·승인용 runtime 결과로 변환되는지 확인합니다."""

    from tools.align.alignment import create_icp_input_snapshot
    from tools.align.alignment.icp_runtime import _runtime_result_from_worker
    from tools.align.alignment.icp_solver_backend import backend_algorithm_version
    from tools.align.constants import ICP_ALIGNMENT_PHASE_RUNNING, ICP_JOB_PROTOCOL_VERSION

    runtime = _runtime_fixture()
    backend_id = "OPEN3D_GICP"
    backend_version = backend_algorithm_version(backend_id)
    input_data = replace(
        create_icp_input_snapshot(runtime),
        moving_cloud_frame="INITIAL_PROJECT_WORLD",
        solver_backend=backend_id,
        backend_algorithm_version=backend_version,
    )
    job_id = "open3d-runtime-result"
    state = runtime.icp_alignment
    state.phase = ICP_ALIGNMENT_PHASE_RUNNING
    state.input_data = input_data
    state.job_handle = SimpleNamespace(job_id=job_id)
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
    metadata = {
        "job_id": job_id,
        "input_fingerprint": input_data.input_fingerprint,
        "algorithm_version": input_data.algorithm_version,
        "backend_id": backend_id,
        "backend_algorithm_version": backend_version,
        "job_protocol_version": ICP_JOB_PROTOCOL_VERSION,
        "moving_model_id": input_data.seed.moving_model_id,
        "reference_model_ids": list(input_data.reference_model_ids),
        "quality": {"status": "PASS", "review_reasons": []},
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
            "stage_input_fingerprints": {
                "coarse": dict(stage_audit),
                "medium": dict(stage_audit),
                "fine": dict(stage_audit),
            },
            "downsampling": {
                "used": False,
                "voxel_down_sample": False,
                "point_budget_sampling": False,
            },
        },
        "input_policy": "FULL_OVERLAP_NO_DOWNSAMPLING",
        "overlap_point_counts": {"moving": source_count, "reference": target_count},
        "worker_python_version": "3.12.11",
        "worker_numpy_version": "1.26.4",
        "open3d_version": "0.19.0",
        "platform_architecture": "64bit",
        "local_origin": [0.0, 0.0, 0.0],
        "moving_diagonal": 10.0,
        "delta_translation": 0.0,
        "delta_rotation_degrees": 0.0,
        "scale_mode": "LOCKED",
        "scale_refine_status": "NOT_REQUESTED",
    }
    identity = np.identity(4, dtype=np.float64)
    arrays = {
        "initial_alignment": np.array(
            input_data.seed.initial_alignment_float64,
            dtype=np.float64,
            order="C",
            copy=True,
        ),
        "final_alignment": np.array(
            input_data.seed.initial_alignment_float64,
            dtype=np.float64,
            order="C",
            copy=True,
        ),
        "icp_delta": identity,
    }

    result = _runtime_result_from_worker(runtime, state, metadata, arrays)
    assert result.solver_backend == backend_id
    assert result.backend_algorithm_version == backend_version
    assert result.quality_status == "PASS"
    np.testing.assert_allclose(
        result.final_alignment.matrix_float64,
        input_data.seed.initial_alignment_float64,
        rtol=0.0,
        atol=1.0e-12,
    )


def run_all() -> None:
    """Open3D 없는 기본 Blender 환경의 runtime 경계 테스트를 실행합니다."""

    _add_test_paths()
    test_open3d_requires_explicit_python_before_creating_job()
    test_open3d_rejects_scale_refine_before_creating_job()
    test_open3d_launch_failure_does_not_fallback_to_custom_worker()
    test_open3d_result_normalizes_to_runtime_preview_candidate()
    print("test_icp_open3d_runtime: 통과")


if __name__ == "__main__":
    run_all()
