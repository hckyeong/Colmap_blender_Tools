"""ICP 메인 런타임의 worker 수명주기·품질 거부·승인 Undo 경계를 검증합니다."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from time import sleep
import subprocess
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace

import numpy as np


def _add_test_paths() -> None:
    """애드온과 마커 fixture 테스트 경로를 Blender 모듈 탐색 경로에 추가합니다."""

    test_path = Path(__file__).resolve().parent
    source_path = test_path.parents[2]
    for path in (source_path, test_path):
        path_text = str(path)
        if path_text not in sys.path:
            sys.path.insert(0, path_text)


def _point_table(positions: np.ndarray):
    """독립 ICP fixture에 필요한 유효한 COLMAP PointTable을 생성합니다."""

    from tools.align.colmap_io.types import PointTable

    count = int(positions.shape[0])
    return PointTable(
        point_ids=np.arange(101, 101 + count, dtype=np.uint64),
        positions=positions,
        colors=np.full((count, 3), 127, dtype=np.uint8),
        errors=np.zeros(count, dtype=np.float64),
        track_offsets=np.zeros(count + 1, dtype=np.uint64),
        track_image_ids=np.empty(0, dtype=np.uint64),
        track_point2d_indices=np.empty(0, dtype=np.uint32),
        display_indices=np.arange(count, dtype=np.uint32),
    )


def _model(model_id: str, positions: np.ndarray, revision: int):
    """Repository에 직접 넣을 최소 실제 ColmapModelData를 만듭니다."""

    from tools.align.colmap_io.types import ColmapModelData, ImageTable

    return ColmapModelData(
        model_id=model_id,
        display_name=model_id,
        source_path=Path.cwd() / "icp_runtime_fixture" / model_id,
        source_format="text",
        cameras={},
        images=ImageTable(
            image_records={},
            point2d_offsets=np.asarray((0,), dtype=np.uint64),
            point2d_xy=np.empty((0, 2), dtype=np.float64),
            point2d_point3d_ids=np.empty(0, dtype=np.uint64),
        ),
        rigs={},
        frames={},
        points=_point_table(positions),
        bounds_min=np.min(positions, axis=0),
        bounds_max=np.max(positions, axis=0),
        geometry_revision=revision,
    )


def _moving_positions(count: int) -> np.ndarray:
    """처음 세 마커가 비공선이고 overlap 복셀을 충분히 공유하는 격자 점군을 생성합니다."""

    if count < 4:
        raise ValueError("ICP runtime fixture 점 수는 4 이상이어야 합니다.")
    side = int(np.ceil(count ** (1.0 / 3.0)))
    coordinates = np.linspace(-2.0, 2.0, num=side, dtype=np.float64)
    grid = np.stack(
        np.meshgrid(coordinates, coordinates, coordinates, indexing="ij"),
        axis=-1,
    ).reshape(-1, 3)
    positions = np.array(grid[:count], dtype=np.float64, copy=True, order="C")
    positions[:4] = np.asarray(
        (
            (0.0, 0.0, 0.0),
            (1.5, 0.2, 0.1),
            (0.3, 1.7, 0.5),
            (-0.4, 0.6, 1.8),
        ),
        dtype=np.float64,
    )
    return positions


def _reference_positions(moving_positions: np.ndarray) -> np.ndarray:
    """마커와 ICP가 회복할 알려진 P→A Sim(3) 점군을 계산합니다."""

    angle = float(np.deg2rad(23.0))
    rotation = np.asarray(
        (
            (np.cos(angle), -np.sin(angle), 0.0),
            (np.sin(angle), np.cos(angle), 0.0),
            (0.0, 0.0, 1.0),
        ),
        dtype=np.float64,
    )
    return 1.25 * (moving_positions @ rotation.T) + np.asarray(
        (4.0, -3.0, 1.5),
        dtype=np.float64,
    )


def _runtime_with_marker_preview(*, count: int = 16, include_far_reference: bool = False):
    """A/P 등록·3개 완성 마커·유효 marker preview를 가진 Runtime을 생성합니다."""

    from tools.align.alignment import begin_marker_preview
    from tools.align.constants import ROLE_MOVING, ROLE_REFERENCE
    from tools.align.display_axes import identity_display_transform
    from tools.align.state import MarkerPair, RuntimeState, SelectedPoint

    moving_positions = _moving_positions(count)
    reference_positions = _reference_positions(moving_positions)
    runtime = RuntimeState()
    runtime.session_active = True
    runtime.model_repository.add_model(
        _model("reference", reference_positions, revision=7),
        identity_display_transform(),
    )
    if include_far_reference:
        runtime.model_repository.add_model(
            _model("reference_far", reference_positions + np.asarray((500.0, 0.0, 0.0)), revision=8),
            identity_display_transform(),
        )
    runtime.model_repository.add_model(
        _model("moving", moving_positions, revision=11),
        identity_display_transform(),
    )
    runtime.model_catalog.add_imported_model("reference", "reference", select_after_import=False)
    if include_far_reference:
        runtime.model_catalog.add_imported_model(
            "reference_far",
            "reference_far",
            select_after_import=False,
        )
        runtime.model_catalog.promote_model_for_icp_commit("reference_far")
    runtime.model_catalog.add_imported_model("moving", "moving", select_after_import=True)
    point_ids = runtime.model_repository.models["moving"].points.point_ids
    runtime.marker_selection.completed_pairs = [
        MarkerPair(
            pair_id=index + 1,
            reference=SelectedPoint(
                ROLE_REFERENCE,
                int(point_ids[index]),
                tuple(float(value) for value in reference_positions[index]),
                model_id="reference",
            ),
            moving=SelectedPoint(
                ROLE_MOVING,
                int(point_ids[index]),
                tuple(float(value) for value in moving_positions[index]),
                model_id="moving",
            ),
        )
        for index in range(3)
    ]
    marker_result = begin_marker_preview(runtime)
    return runtime, marker_result


def _preview_result_for_input(input_data):
    """승인·Undo 테스트 전용으로 marker 결과와 일치하는 typed ICP 결과를 생성합니다."""

    from tools.align.alignment import IcpRuntimeResult, identity_sim3_transform

    final_alignment = input_data.seed.marker_result.preview_alignment
    return IcpRuntimeResult(
        moving_model_id=input_data.seed.moving_model_id,
        reference_model_ids=input_data.reference_model_ids,
        final_alignment=final_alignment,
        icp_delta=identity_sim3_transform(),
        quality={"status": "PASS"},
        metadata={"fixture": "runtime"},
        input_fingerprint=input_data.input_fingerprint,
        parameter_fingerprint=input_data.parameters.fingerprint,
        marker_signature=input_data.seed.marker_signature,
        model_revision_snapshot=input_data.model_revision_snapshot,
        algorithm_version=input_data.algorithm_version,
    )


class _ExitedProcess:
    """결과 파일 처리만 검증할 정상 종료 process 대체 객체입니다."""

    def __init__(self, return_code: int = 0) -> None:
        """정상 또는 실패 종료 코드를 고정합니다."""

        self.return_code = return_code

    def poll(self) -> int:
        """항상 이미 종료한 코드를 반환합니다."""

        return self.return_code

    def wait(self, timeout=None) -> int:
        """대기 없이 이미 종료한 코드를 반환합니다."""

        return self.return_code


class _ControllableProcess:
    """cancel·terminate·kill lifecycle을 시간 대기 없이 검증하는 process 대체 객체입니다."""

    def __init__(self, *, stop_on_terminate: bool, stop_on_kill: bool) -> None:
        """종료 신호별로 실제 종료 여부를 결정합니다."""

        self.return_code: int | None = None
        self.stop_on_terminate = stop_on_terminate
        self.stop_on_kill = stop_on_kill
        self.terminate_count = 0
        self.kill_count = 0

    def poll(self) -> int | None:
        """현재 종료 코드 또는 실행 중 상태를 반환합니다."""

        return self.return_code

    def wait(self, timeout=None) -> int:
        """실행 중이면 즉시 timeout을 흉내 내고 종료되면 코드를 반환합니다."""

        if self.return_code is None:
            raise subprocess.TimeoutExpired("icp-test-worker", timeout)
        return self.return_code

    def terminate(self) -> None:
        """grace 종료 요청을 기록하고 설정에 따라 worker를 멈춥니다."""

        self.terminate_count += 1
        if self.stop_on_terminate:
            self.return_code = -15

    def kill(self) -> None:
        """강제 종료 요청을 기록하고 설정에 따라 worker를 멈춥니다."""

        self.kill_count += 1
        if self.stop_on_kill:
            self.return_code = -9


def _stopped_worker_runtime(*, return_code: int):
    """손상된 종료 출력 회귀용 Runtime·입력·worker handle을 한 묶음으로 만듭니다."""

    from tools.align.alignment import create_icp_input_snapshot
    from tools.align.alignment.icp_backend import IcpJobHandle, create_icp_job_directory
    from tools.align.constants import ICP_ALIGNMENT_PHASE_RUNNING

    runtime, _marker_result = _runtime_with_marker_preview()
    input_data = create_icp_input_snapshot(runtime)
    files = create_icp_job_directory()
    handle = IcpJobHandle(
        job_id=files.job_directory.name,
        files=files,
        process=_ExitedProcess(return_code),
        input_fingerprint=input_data.input_fingerprint,
    )
    state = runtime.icp_alignment
    state.phase = ICP_ALIGNMENT_PHASE_RUNNING
    state.job_handle = handle
    state.input_data = input_data
    return runtime, files, handle


def _assert_terminal_output_failure(runtime, files, handle) -> None:
    """손상된 종료 출력이 handle을 해제하고 진단 폴더를 보존하는지 검증합니다."""

    from tools.align.constants import ICP_ALIGNMENT_PHASE_FAILED

    state = runtime.icp_alignment
    assert state.phase == ICP_ALIGNMENT_PHASE_FAILED
    assert state.job_handle is None
    assert state.result is None
    assert state.diagnostic_handles == [handle]
    assert files.job_directory.exists()


def _valid_progress_payload(handle, input_data) -> dict[str, object]:
    """진행률 형식 검증에 사용할 최소 유효 worker JSON을 만듭니다."""

    return {
        "job_id": handle.job_id,
        "input_fingerprint": input_data.input_fingerprint,
        "moving_model_id": input_data.seed.moving_model_id,
        "algorithm_version": input_data.algorithm_version,
        "stage": "ICP_FINE_POINT_TO_PLANE",
        "level_name": "fine",
        "iteration": 0,
        "max_iterations": 0,
        "progress": 0.5,
        "message": "진행 중",
        "level_complete": False,
    }


def _evaluation_metrics_mapping() -> dict[str, object]:
    """PASS·REVIEW runtime 결과에 공통으로 넣을 고정 평가 지표를 만듭니다."""

    return {
        "point_to_point_rmse": 0.08,
        "point_to_plane_rmse": 0.05,
        "fitness": 0.9,
        "reciprocal_ratio": 0.85,
        "inlier_count": 8,
        "query_count": 10,
        "maximum_correspondence_distance": 0.4,
        "normal_valid_ratio": 0.92,
        "normal_angle_pass_ratio": 0.88,
        "evaluation_fingerprint": "runtime-hybrid-evaluation",
    }


def _hybrid_result_metadata(handle, input_data, *, quality_status: str) -> dict[str, object]:
    """v2 runtime 완료 처리 테스트용 완전한 Hybrid 결과 JSON을 만듭니다."""

    from tools.align.alignment import identity_sim3_transform

    final_alignment = input_data.seed.initial_alignment_float64
    delta = identity_sim3_transform().matrix_float64
    candidate_metrics = _evaluation_metrics_mapping()
    baseline_metrics = dict(candidate_metrics)
    baseline_metrics["point_to_point_rmse"] = 0.1
    baseline_metrics["point_to_plane_rmse"] = 0.06
    return {
        "job_id": handle.job_id,
        "input_fingerprint": input_data.input_fingerprint,
        "algorithm_version": input_data.algorithm_version,
        "moving_model_id": input_data.seed.moving_model_id,
        "reference_model_ids": list(input_data.reference_model_ids),
        "quality": {"status": quality_status},
        "level_results": [{"name": "fine"}],
        "parameter_fingerprint": input_data.parameters.fingerprint,
        "model_revision_snapshot": [list(item) for item in input_data.model_revision_snapshot],
        "model_state_snapshot": [list(item) for item in input_data.model_state_snapshot],
        "marker_signature": input_data.seed.marker_signature,
        "best_candidate": {
            "delta_4x4": delta.tolist(),
            "final_alignment_4x4": final_alignment.tolist(),
            "level_name": "fine",
            "iteration": 3,
            "objective_kind": "POINT_TO_PLANE",
            "point_to_plane_rmse": candidate_metrics["point_to_plane_rmse"],
            "point_to_point_rmse": candidate_metrics["point_to_point_rmse"],
            "fitness": candidate_metrics["fitness"],
            "inlier_count": candidate_metrics["inlier_count"],
            "reciprocal_ratio": candidate_metrics["reciprocal_ratio"],
            "normal_valid_ratio": candidate_metrics["normal_valid_ratio"],
            "normal_angle_pass_ratio": candidate_metrics["normal_angle_pass_ratio"],
            "maximum_correspondence_distance": candidate_metrics["maximum_correspondence_distance"],
            "correspondence_signature": "runtime-hybrid-correspondence",
            "evaluation_fingerprint": candidate_metrics["evaluation_fingerprint"],
        },
        "baseline_metrics": baseline_metrics,
        "best_candidate_metrics": candidate_metrics,
        "relative_improvement": 1.0 / 6.0,
        "best_level_name": "fine",
        "best_iteration": 3,
        "solver_stop_reason": "CONVERGED",
        "point_to_plane_rmse": candidate_metrics["point_to_plane_rmse"],
        "normal_valid_ratio": candidate_metrics["normal_valid_ratio"],
        "normal_angle_pass_ratio": candidate_metrics["normal_angle_pass_ratio"],
        "density_sampling_metrics": {
            "shared_voxel_size": 0.05,
            "macro_cell_divisions": 4,
            "macro_cell_count": 64,
            "occupied_macro_cell_count": 11,
            "minimum_samples_per_macro_cell": 1,
            "median_samples_per_macro_cell": 2.0,
            "maximum_samples_per_macro_cell": 4,
        },
        "user_review_required": quality_status == "REVIEW",
    }


def _core_result_for_hybrid_metadata(input_data, *, quality_status: str):
    """runtime mapping 검증 경계용 최소 typed core 결과 대체 객체를 만듭니다."""

    from tools.align.alignment import identity_sim3_transform

    class _Quality:
        """runtime UI 상태에 필요한 품질 상태만 제공합니다."""

        status = quality_status

        def to_mapping(self) -> dict[str, object]:
            """최소 품질 mapping을 반환합니다."""

            return {"status": quality_status}

    return SimpleNamespace(
        moving_model_id=input_data.seed.moving_model_id,
        reference_model_ids=input_data.reference_model_ids,
        initial_alignment=input_data.seed.marker_result.preview_alignment,
        final_alignment=input_data.seed.marker_result.preview_alignment,
        icp_delta=identity_sim3_transform(),
        quality=_Quality(),
        model_revision_snapshot=input_data.model_revision_snapshot,
        algorithm_version=input_data.algorithm_version,
    )


def test_direct_input_does_not_mutate_source_and_rejects_negative_ids() -> None:
    """UI/test용 직접 snapshot도 원본 배열을 불변으로 유지하고 음수 ID를 거부하는지 검증합니다."""

    from tools.align.alignment import create_icp_input_snapshot
    from tools.align.alignment.icp_runtime import IcpRuntimeError, _validated_point_ids_view

    runtime, _marker_result = _runtime_with_marker_preview()
    source_positions = runtime.model_repository.models["moving"].points.positions
    source_ids = runtime.model_repository.models["moving"].points.point_ids
    source_positions_writable = source_positions.flags.writeable
    source_ids_writable = source_ids.flags.writeable
    input_data = create_icp_input_snapshot(runtime)
    assert source_positions.flags.writeable is source_positions_writable
    assert source_ids.flags.writeable is source_ids_writable
    assert not np.shares_memory(input_data.moving_positions, source_positions)
    assert not np.shares_memory(input_data.moving_point_ids, source_ids)
    try:
        _validated_point_ids_view(
            np.asarray((11, -1), dtype=np.int64),
            expected_count=2,
            field_name="음수 ID",
        )
    except IcpRuntimeError:
        pass
    else:
        raise AssertionError("signed 음수 POINT3D_ID가 uint64로 감싸졌습니다.")


def test_quality_rejection_keeps_marker_preview_and_lifecycle_cleans_diagnostics() -> None:
    """QUALITY_REJECTED는 ICP preview 없이 marker preview를 보존하고 나중에 job을 지우는지 검증합니다."""

    from tools.align.alignment import create_icp_input_snapshot, finish_icp_job, invalidate_icp
    from tools.align.alignment.icp_backend import IcpJobHandle, create_icp_job_directory, write_json_atomically
    from tools.align.alignment.icp_runtime import IcpRuntimeError
    from tools.align.constants import ICP_ALIGNMENT_PHASE_FAILED, ICP_ALIGNMENT_PHASE_RUNNING

    runtime, marker_result = _runtime_with_marker_preview()
    input_data = create_icp_input_snapshot(runtime)
    files = create_icp_job_directory()
    handle = IcpJobHandle(
        job_id=files.job_directory.name,
        files=files,
        process=_ExitedProcess(0),
        input_fingerprint=input_data.input_fingerprint,
    )
    write_json_atomically(
        files.error_path,
        {
            "job_id": handle.job_id,
            "input_fingerprint": input_data.input_fingerprint,
            "status": "QUALITY_REJECTED",
            "message": "최종 품질 기준을 충족하지 못했습니다.",
            "rejection_reasons": ["겹침 비율이 낮습니다."],
        },
    )
    state = runtime.icp_alignment
    state.phase = ICP_ALIGNMENT_PHASE_RUNNING
    state.job_handle = handle
    state.input_data = input_data
    try:
        finish_icp_job(runtime)
    except IcpRuntimeError as error:
        assert "ICP 품질 거부" in str(error)
        assert "겹침 비율" in str(error)
    else:
        raise AssertionError("품질 거부 결과가 ICP preview로 승격되었습니다.")
    assert state.phase == ICP_ALIGNMENT_PHASE_FAILED
    assert state.result is None
    assert runtime.marker_alignment.result is marker_result
    assert files.job_directory.exists()
    assert state.diagnostic_handles == [handle]
    invalidate_icp(runtime, "품질 거부 진단을 정리합니다.")
    assert not files.job_directory.exists()
    assert not state.diagnostic_handles


def test_corrupt_terminal_error_json_finalizes_failed_state() -> None:
    """비정상 종료 worker의 손상 error.json이 UI 잠금 없이 FAILED로 정규화되는지 검증합니다."""

    from tools.align.alignment import finish_icp_job, invalidate_icp
    from tools.align.alignment.icp_runtime import IcpRuntimeError

    runtime, files, handle = _stopped_worker_runtime(return_code=7)
    files.error_path.write_bytes(b"{")
    try:
        try:
            finish_icp_job(runtime)
        except IcpRuntimeError as error:
            assert "오류 파일" in str(error)
        else:
            raise AssertionError("손상된 error.json이 성공 처리되었습니다.")
        _assert_terminal_output_failure(runtime, files, handle)
    finally:
        invalidate_icp(runtime, "손상된 error.json 진단을 정리합니다.")
    assert not files.job_directory.exists()


def test_corrupt_terminal_result_json_finalizes_failed_state() -> None:
    """정상 종료 worker의 손상 result.json이 preview 없이 FAILED로 정리되는지 검증합니다."""

    from tools.align.alignment import finish_icp_job, invalidate_icp
    from tools.align.alignment.icp_runtime import IcpRuntimeError

    runtime, files, handle = _stopped_worker_runtime(return_code=0)
    files.result_path.write_bytes(b"{")
    files.result_array_path.write_bytes(b"not-used-because-json-is-corrupt")
    try:
        try:
            finish_icp_job(runtime)
        except IcpRuntimeError as error:
            assert "결과 파일" in str(error)
        else:
            raise AssertionError("손상된 result.json이 성공 처리되었습니다.")
        _assert_terminal_output_failure(runtime, files, handle)
    finally:
        invalidate_icp(runtime, "손상된 result.json 진단을 정리합니다.")
    assert not files.job_directory.exists()


def test_nonfinite_result_metadata_finalizes_failed_state() -> None:
    """result.json의 1e999가 완료 처리에서 FAILED·진단 보존으로 정리되는지 검증합니다."""

    from tools.align.alignment import finish_icp_job, invalidate_icp
    from tools.align.alignment.icp_runtime import IcpRuntimeError

    runtime, files, handle = _stopped_worker_runtime(return_code=0)
    files.result_path.write_bytes(b'{"model_revision_snapshot":[["moving",1e999]]}')
    files.result_array_path.write_bytes(b"not-used-because-json-is-nonfinite")
    try:
        try:
            finish_icp_job(runtime)
        except IcpRuntimeError as error:
            assert "결과 파일" in str(error)
        else:
            raise AssertionError("비유한 result.json이 성공 처리되었습니다.")
        _assert_terminal_output_failure(runtime, files, handle)
    finally:
        invalidate_icp(runtime, "비유한 result.json 진단을 정리합니다.")
    assert not files.job_directory.exists()


def test_corrupt_terminal_result_npz_finalizes_failed_state() -> None:
    """정상 종료 worker의 손상 result.npz가 preview 없이 FAILED로 정리되는지 검증합니다."""

    from tools.align.alignment import finish_icp_job, invalidate_icp
    from tools.align.alignment.icp_backend import write_json_atomically
    from tools.align.alignment.icp_runtime import IcpRuntimeError

    runtime, files, handle = _stopped_worker_runtime(return_code=0)
    write_json_atomically(files.result_path, {"format": "intentionally-incomplete"})
    files.result_array_path.write_bytes(b"not-a-npz")
    try:
        try:
            finish_icp_job(runtime)
        except IcpRuntimeError as error:
            assert "결과 파일" in str(error)
        else:
            raise AssertionError("손상된 result.npz가 성공 처리되었습니다.")
        _assert_terminal_output_failure(runtime, files, handle)
    finally:
        invalidate_icp(runtime, "손상된 result.npz 진단을 정리합니다.")
    assert not files.job_directory.exists()


def test_corrupt_terminal_progress_finalizes_failed_state() -> None:
    """종료 worker의 손상 progress.json이 poll 경계에서 FAILED로 정리되는지 검증합니다."""

    from tools.align.alignment import invalidate_icp, poll_icp_job

    runtime, files, handle = _stopped_worker_runtime(return_code=0)
    files.progress_path.write_bytes(b"{")
    try:
        assert poll_icp_job(runtime) is None
        _assert_terminal_output_failure(runtime, files, handle)
    finally:
        invalidate_icp(runtime, "손상된 progress.json 진단을 정리합니다.")
    assert not files.job_directory.exists()


def test_progress_payload_rejects_implicit_or_nonfinite_numbers() -> None:
    """진행률 JSON의 bool·문자열·비유한·범위 밖 수치를 protocol 오류로 거부하는지 검증합니다."""

    from tools.align.alignment import invalidate_icp
    from tools.align.alignment.icp_backend import IcpJobProtocolError
    from tools.align.alignment.icp_runtime import _maximum_progress_iteration, _progress_from_payload

    runtime, files, handle = _stopped_worker_runtime(return_code=0)
    input_data = runtime.icp_alignment.input_data
    assert input_data is not None
    invalid_cases = (
        ("algorithm_version", "point-to-point-icp-v1"),
        ("iteration", float("inf")),
        ("iteration", "1"),
        ("iteration", True),
        ("iteration", -1),
        ("iteration", _maximum_progress_iteration(input_data) + 1),
        ("progress", "x"),
        ("progress", float("nan")),
        ("progress", float("inf")),
        ("progress", True),
        ("level_complete", "false"),
    )
    try:
        best_candidate_payload = _valid_progress_payload(handle, input_data)
        best_candidate_payload["stable_alignment_4x4"] = (
            input_data.seed.initial_alignment_float64.tolist()
        )
        best_candidate_progress = _progress_from_payload(
            best_candidate_payload,
            handle=handle,
            input_data=input_data,
        )
        assert best_candidate_progress.stable_alignment is not None
        assert best_candidate_progress.level_complete is False

        for field_name, invalid_value in invalid_cases:
            payload = _valid_progress_payload(handle, input_data)
            payload[field_name] = invalid_value
            try:
                _progress_from_payload(payload, handle=handle, input_data=input_data)
            except IcpJobProtocolError:
                pass
            else:
                raise AssertionError(f"진행률 {field_name}의 잘못된 값이 수용되었습니다.")

        payload = _valid_progress_payload(handle, input_data)
        payload["stable_alignment_4x4"] = [
            [10**400, 0, 0, 0],
            [0, 1, 0, 0],
            [0, 0, 1, 0],
            [0, 0, 0, 1],
        ]
        try:
            _progress_from_payload(payload, handle=handle, input_data=input_data)
        except IcpJobProtocolError:
            pass
        else:
            raise AssertionError("stable alignment의 과대 정수가 수용되었습니다.")
    finally:
        invalidate_icp(runtime, "진행률 수치 형식 진단을 정리합니다.")
    assert not files.job_directory.exists()


def test_runtime_reads_typed_hybrid_progress_metrics() -> None:
    """IPC의 Hybrid 실행 지표가 메시지 해석 없이 typed 상태로 전달되는지 검증합니다."""

    from tools.align.alignment import invalidate_icp
    from tools.align.alignment.icp_backend import IcpJobProtocolError
    from tools.align.alignment.icp_runtime import _progress_from_payload

    runtime, files, handle = _stopped_worker_runtime(return_code=0)
    input_data = runtime.icp_alignment.input_data
    assert input_data is not None
    payload = _valid_progress_payload(handle, input_data)
    payload.update(
        {
            "objective_kind": "POINT_TO_PLANE",
            "maximum_correspondence_distance": 0.25,
            "correspondence_count": 82,
            "point_to_point_rmse": 0.04,
            "point_to_plane_rmse": 0.02,
            "normal_valid_count": 91,
            "normal_angle_pass_count": 76,
            "normal_radius": 0.11,
            "reference_sample_count": 512,
            "moving_sample_count": 256,
        }
    )
    try:
        progress = _progress_from_payload(payload, handle=handle, input_data=input_data)
        assert progress.objective_kind == "POINT_TO_PLANE"
        assert progress.maximum_correspondence_distance == 0.25
        assert progress.correspondence_count == 82
        assert progress.point_to_point_rmse == 0.04
        assert progress.point_to_plane_rmse == 0.02
        assert progress.normal_valid_count == 91
        assert progress.normal_angle_pass_count == 76
        assert progress.normal_radius == 0.11
        assert progress.reference_sample_count == 512
        assert progress.moving_sample_count == 256
        for key, invalid_value in (
            ("objective_kind", "UNKNOWN"),
            ("maximum_correspondence_distance", "0.25"),
            ("correspondence_count", True),
            ("point_to_point_rmse", float("nan")),
            ("normal_radius", -0.1),
        ):
            malformed = dict(payload)
            malformed[key] = invalid_value
            try:
                _progress_from_payload(malformed, handle=handle, input_data=input_data)
            except IcpJobProtocolError:
                pass
            else:
                raise AssertionError(f"typed progress {key}의 잘못된 값이 수용되었습니다.")
        malformed = dict(payload)
        malformed["normal_angle_pass_count"] = malformed["normal_valid_count"] + 1
        try:
            _progress_from_payload(malformed, handle=handle, input_data=input_data)
        except IcpJobProtocolError:
            pass
        else:
            raise AssertionError("노멀 유효 수보다 큰 angle pass 수가 수용되었습니다.")
    finally:
        invalidate_icp(runtime, "typed progress 테스트 작업을 정리합니다.")
    assert not files.job_directory.exists()


def test_poll_timer_finalizes_nonfinite_progress_without_exception() -> None:
    """timer가 1e999 진행률 JSON을 FAILED·진단 보존 상태로 정리하는지 검증합니다."""

    import tools.align.operators.marker_alignment as marker_operator_module
    from tools.align.alignment import invalidate_icp

    runtime, files, handle = _stopped_worker_runtime(return_code=0)
    input_data = runtime.icp_alignment.input_data
    assert input_data is not None
    files.progress_path.write_text(
        (
            "{"
            f'\"job_id\":\"{handle.job_id}\",'
            f'\"input_fingerprint\":\"{input_data.input_fingerprint}\",'
            f'\"moving_model_id\":\"{input_data.seed.moving_model_id}\",'
            f'\"algorithm_version\":\"{input_data.algorithm_version}\",'
            '\"stage\":\"ICP_FINE_POINT_TO_PLANE\",\"iteration\":1e999,\"max_iterations\":0,\"progress\":0.5'
            "}"
        ),
        encoding="utf-8",
    )
    original_runtime = marker_operator_module.RUNTIME
    original_redraw = marker_operator_module.request_redraw
    original_status = marker_operator_module.set_workspace_status_text
    original_workspace = marker_operator_module.session_align_workspace
    try:
        marker_operator_module.RUNTIME = runtime
        runtime.icp_timer = lambda: None
        marker_operator_module.request_redraw = lambda: None
        marker_operator_module.set_workspace_status_text = lambda _workspace, _message: None
        marker_operator_module.session_align_workspace = lambda: None
        assert marker_operator_module._poll_icp_job_timer() is None
        _assert_terminal_output_failure(runtime, files, handle)
        assert runtime.icp_timer is None
    finally:
        marker_operator_module.RUNTIME = original_runtime
        marker_operator_module.request_redraw = original_redraw
        marker_operator_module.set_workspace_status_text = original_status
        marker_operator_module.session_align_workspace = original_workspace
        invalidate_icp(runtime, "비유한 진행률 JSON 진단을 정리합니다.")
    assert not files.job_directory.exists()


def test_missing_terminal_progress_finalizes_failed_state() -> None:
    """결과가 있어도 종료 worker의 progress.json이 없으면 preview를 만들지 않는지 검증합니다."""

    import tools.align.alignment.icp_runtime as icp_runtime_module
    from tools.align.alignment import finish_icp_job, invalidate_icp
    from tools.align.alignment.icp_runtime import IcpRuntimeError

    runtime, files, handle = _stopped_worker_runtime(return_code=0)
    original_read_result = icp_runtime_module.read_job_result
    icp_runtime_module.read_job_result = lambda _handle: ({}, {})
    try:
        try:
            finish_icp_job(runtime)
        except IcpRuntimeError as error:
            assert "진행률 파일" in str(error)
        else:
            raise AssertionError("진행률 파일 없이 ICP preview가 생성되었습니다.")
        _assert_terminal_output_failure(runtime, files, handle)
    finally:
        icp_runtime_module.read_job_result = original_read_result
        invalidate_icp(runtime, "누락된 progress.json 진단을 정리합니다.")
    assert not files.job_directory.exists()


def test_finish_normalizes_core_worker_error_to_failed_state() -> None:
    """완료 처리의 core IcpWorkerError가 UI 밖으로 새지 않고 FAILED로 정리되는지 검증합니다."""

    import tools.align.alignment.icp_runtime as icp_runtime_module
    from tools.align.alignment import finish_icp_job, invalidate_icp
    from tools.align.alignment import IcpWorkerError
    from tools.align.alignment.icp_runtime import IcpRuntimeError

    runtime, files, handle = _stopped_worker_runtime(return_code=0)
    original_read_result = icp_runtime_module.read_job_result
    original_read_progress = icp_runtime_module._read_terminal_progress
    original_result_from_worker = icp_runtime_module._runtime_result_from_worker
    try:
        icp_runtime_module.read_job_result = lambda _handle: ({}, {})
        icp_runtime_module._read_terminal_progress = lambda _state, _handle, _input: None

        def _raise_worker_error(_runtime, _state, _metadata, _arrays):
            """core 경계의 구조화 worker 오류를 완료 처리까지 전달합니다."""

            raise IcpWorkerError("테스트용 core worker 오류")

        icp_runtime_module._runtime_result_from_worker = _raise_worker_error
        try:
            finish_icp_job(runtime)
        except IcpRuntimeError as error:
            assert "테스트용 core worker 오류" in str(error)
        else:
            raise AssertionError("core worker 오류가 FAILED로 정리되지 않았습니다.")
        _assert_terminal_output_failure(runtime, files, handle)
    finally:
        icp_runtime_module.read_job_result = original_read_result
        icp_runtime_module._read_terminal_progress = original_read_progress
        icp_runtime_module._runtime_result_from_worker = original_result_from_worker
        invalidate_icp(runtime, "core worker 오류 진단을 정리합니다.")
    assert not files.job_directory.exists()


def test_pass_and_review_results_both_enter_approvable_preview() -> None:
    """PASS·REVIEW 결과가 같은 비파괴 preview와 세부 진단값으로 승격되는지 검증합니다."""

    from tools.align.alignment import (
        IcpAlignmentResult,
        create_icp_input_snapshot,
        discard_icp_preview,
        finish_icp_job,
    )
    from tools.align.alignment.icp_backend import (
        IcpJobHandle,
        create_icp_job_directory,
        write_json_atomically,
        write_result_arrays_atomically,
    )
    from tools.align.alignment.icp_runtime import (
        is_icp_result_approvable,
    )
    from tools.align.constants import (
        ICP_ALIGNMENT_PHASE_PREVIEW,
        ICP_ALIGNMENT_PHASE_RUNNING,
        ICP_STAGE_COMPLETED_PASS,
        ICP_STAGE_COMPLETED_REVIEW,
    )

    original_from_mapping = IcpAlignmentResult.__dict__["from_mapping"]
    try:
        for quality_status, expected_stage in (
            ("PASS", ICP_STAGE_COMPLETED_PASS),
            ("REVIEW", ICP_STAGE_COMPLETED_REVIEW),
        ):
            runtime, _marker_result = _runtime_with_marker_preview()
            input_data = create_icp_input_snapshot(runtime)
            files = create_icp_job_directory()
            handle = IcpJobHandle(
                job_id=files.job_directory.name,
                files=files,
                process=_ExitedProcess(0),
                input_fingerprint=input_data.input_fingerprint,
            )
            metadata = _hybrid_result_metadata(
                handle,
                input_data,
                quality_status=quality_status,
            )
            write_json_atomically(files.result_path, metadata)
            write_result_arrays_atomically(
                files.result_array_path,
                {
                    "initial_alignment": input_data.seed.initial_alignment_float64,
                    "final_alignment": input_data.seed.initial_alignment_float64,
                    "icp_delta": np.eye(4, dtype=np.float64),
                },
            )
            write_json_atomically(
                files.progress_path,
                {
                    **_valid_progress_payload(handle, input_data),
                    "stage": expected_stage,
                    "progress": 1.0,
                    "level_complete": True,
                    "stable_alignment_4x4": input_data.seed.initial_alignment_float64.tolist(),
                },
            )
            state = runtime.icp_alignment
            state.phase = ICP_ALIGNMENT_PHASE_RUNNING
            state.job_handle = handle
            state.input_data = input_data
            IcpAlignmentResult.from_mapping = classmethod(
                lambda _class, _mapping, _input=input_data, _status=quality_status: _core_result_for_hybrid_metadata(
                    _input,
                    quality_status=_status,
                )
            )
            result = finish_icp_job(runtime)
            assert state.phase == ICP_ALIGNMENT_PHASE_PREVIEW
            assert state.has_preview is True
            assert is_icp_result_approvable(result) is True
            assert result.quality_status == quality_status
            assert result.user_review_required is (quality_status == "REVIEW")
            assert result.baseline_metrics is not None
            assert result.best_candidate_metrics is not None
            assert result.best_candidate is not None
            assert result.point_to_plane_rmse == 0.05
            assert result.normal_valid_ratio == 0.92
            assert result.normal_angle_pass_ratio == 0.88
            assert result.best_level_name == "fine"
            assert result.best_iteration == 3
            assert result.solver_stop_reason == "CONVERGED"
            assert result.density_sampling_metrics is not None
            assert state.progress is not None
            assert state.progress.stage == expected_stage
            assert not files.job_directory.exists()
            discard_icp_preview(runtime)
    finally:
        IcpAlignmentResult.from_mapping = original_from_mapping


def test_poll_timer_handles_corrupt_terminal_output_without_exception() -> None:
    """N 패널 poll timer가 손상된 종료 출력에도 예외 없이 해제되는지 검증합니다."""

    import tools.align.operators.marker_alignment as marker_operator_module
    from tools.align.alignment import invalidate_icp

    runtime, files, handle = _stopped_worker_runtime(return_code=9)
    files.error_path.write_bytes(b"{")
    original_runtime = marker_operator_module.RUNTIME
    original_redraw = marker_operator_module.request_redraw
    original_status = marker_operator_module.set_workspace_status_text
    original_workspace = marker_operator_module.session_align_workspace
    redraw_count = 0
    try:
        marker_operator_module.RUNTIME = runtime
        runtime.icp_timer = lambda: None

        def _request_redraw() -> None:
            """timer가 종료 뒤 redraw를 요청했는지 기록합니다."""

            nonlocal redraw_count
            redraw_count += 1

        marker_operator_module.request_redraw = _request_redraw
        marker_operator_module.set_workspace_status_text = lambda _workspace, _message: None
        marker_operator_module.session_align_workspace = lambda: None
        assert marker_operator_module._poll_icp_job_timer() is None
        _assert_terminal_output_failure(runtime, files, handle)
        assert runtime.icp_timer is None
        assert redraw_count == 1
    finally:
        marker_operator_module.RUNTIME = original_runtime
        marker_operator_module.request_redraw = original_redraw
        marker_operator_module.set_workspace_status_text = original_status
        marker_operator_module.session_align_workspace = original_workspace
        invalidate_icp(runtime, "timer 손상 출력 진단을 정리합니다.")
    assert not files.job_directory.exists()


def test_approve_undo_redo_and_restore_rollback_are_atomic() -> None:
    """ICP 승인·Undo·marker preview 중 ICP Redo 예외·복원 rollback을 검증합니다."""

    from tools.align.alignment import (
        approve_icp_result,
        create_icp_input_snapshot,
        icp_commit_command_kind,
        restore_icp_commit_snapshot,
    )
    from tools.align.constants import ICP_ALIGNMENT_PHASE_PREVIEW, MODEL_STATUS_PENDING, MODEL_STATUS_REGISTERED
    import tools.align.operators.marker_alignment as marker_operator_module
    import tools.align.session as session_module
    from tools.align.session import can_redo_icp_commit_during_marker_preview

    runtime, marker_result = _runtime_with_marker_preview()
    marker_selection_before = runtime.marker_selection.snapshot()
    assert len(marker_selection_before.completed_pairs) == 3
    input_data = create_icp_input_snapshot(runtime)
    result = _preview_result_for_input(input_data)
    base_alignment = runtime.model_repository.alignment_transform_for_model_id("moving")
    state = runtime.icp_alignment
    state.phase = ICP_ALIGNMENT_PHASE_PREVIEW
    state.input_data = input_data
    state.result = result
    state.working_alignment = result.final_alignment
    commit = approve_icp_result(runtime)
    assert runtime.model_repository.alignment_transform_for_model_id("moving").fingerprint == result.final_alignment.fingerprint
    assert runtime.model_catalog.models["moving"].registration_status == MODEL_STATUS_REGISTERED
    assert runtime.marker_alignment.result is None
    assert runtime.marker_selection.completed_pairs == []
    assert runtime.marker_selection.next_pair_id == 1
    assert commit.before.marker_selection == marker_selection_before
    assert commit.after.marker_selection == runtime.marker_selection.snapshot()
    assert runtime.undo_journal.undo_count == 1
    assert runtime.undo_journal.last_kind == icp_commit_command_kind()

    assert runtime.undo_journal.undo_committed(
        lambda snapshot, kind: restore_icp_commit_snapshot(runtime, snapshot)
    ) is True
    assert runtime.model_repository.alignment_transform_for_model_id("moving").fingerprint == base_alignment.fingerprint
    assert runtime.model_catalog.models["moving"].registration_status == MODEL_STATUS_PENDING
    assert runtime.marker_alignment.result is marker_result
    assert runtime.marker_selection.snapshot() == marker_selection_before
    assert runtime.undo_journal.next_redo_kind == icp_commit_command_kind()
    original_runtime = session_module.RUNTIME
    original_marker_runtime = marker_operator_module.RUNTIME
    session_module.RUNTIME = runtime
    marker_operator_module.RUNTIME = runtime
    try:
        assert can_redo_icp_commit_during_marker_preview() is True
    finally:
        session_module.RUNTIME = original_runtime
        marker_operator_module.RUNTIME = original_marker_runtime
    assert runtime.undo_journal.redo_committed(
        lambda snapshot, kind: restore_icp_commit_snapshot(runtime, snapshot)
    ) is True
    assert runtime.model_repository.alignment_transform_for_model_id("moving").fingerprint == result.final_alignment.fingerprint
    assert runtime.model_catalog.models["moving"].registration_status == MODEL_STATUS_REGISTERED
    assert runtime.marker_alignment.result is None
    assert runtime.marker_selection.completed_pairs == []

    original_restore = runtime.model_catalog.restore_snapshot
    restore_calls = 0

    def _fail_once(snapshot) -> None:
        """첫 catalog 복원만 실패시켜 Repository rollback 경계를 강제합니다."""

        nonlocal restore_calls
        restore_calls += 1
        if restore_calls == 1:
            raise RuntimeError("의도한 catalog 복원 실패")
        original_restore(snapshot)

    runtime.model_catalog.restore_snapshot = _fail_once
    try:
        try:
            restore_icp_commit_snapshot(runtime, commit.before)
        except RuntimeError as error:
            assert "의도한" in str(error)
        else:
            raise AssertionError("부분 ICP 복원이 rollback 없이 성공했습니다.")
    finally:
        runtime.model_catalog.restore_snapshot = original_restore
    assert restore_calls == 2
    assert runtime.model_repository.alignment_transform_for_model_id("moving").fingerprint == result.final_alignment.fingerprint
    assert runtime.model_catalog.models["moving"].registration_status == MODEL_STATUS_REGISTERED
    assert runtime.marker_alignment.result is None
    assert runtime.marker_selection.completed_pairs == []


def test_runtime_accepts_ordered_overlapping_reference_subset() -> None:
    """비겹침 A가 제외된 solver 결과가 시작 A 목록의 순서 보존 부분집합으로 허용되는지 검증합니다."""

    from tools.align.alignment import (
        IcpAlignmentResult,
        IcpQualityStatus,
        create_icp_input_snapshot,
        identity_sim3_transform,
    )
    from tools.align.alignment.icp_runtime import (
        IcpRuntimeError,
        IcpRuntimeInput,
        _model_state_snapshot,
        _runtime_result_from_worker,
    )
    from tools.align.alignment.icp_backend import IcpJobProtocolError
    from tools.align.constants import ICP_ALIGNMENT_PHASE_RUNNING

    runtime, _marker_result = _runtime_with_marker_preview(include_far_reference=True)
    input_data = create_icp_input_snapshot(runtime)
    extended_model_state = _model_state_snapshot(
        runtime,
        ("moving", "reference", "reference_far"),
    )
    extended_revisions = tuple(
        (model_id, revision)
        for model_id, revision, _display, _alignment in extended_model_state
    )
    expanded_input = replace(
        input_data,
        reference_model_ids=("reference", "reference_far"),
        model_revision_snapshot=extended_revisions,
        model_state_snapshot=extended_model_state,
    )

    class _Quality:
        """부분 A 결과를 runtime 검증에 통과시킬 최소 quality 표현입니다."""

        status = IcpQualityStatus.PASS

        def to_mapping(self) -> dict[str, object]:
            """UI가 소비할 최소 품질 사전을 반환합니다."""

            return {"status": "PASS"}

    def _core_result(reference_ids: tuple[str, ...]):
        """결과 모델 ID만 달리하는 최소 core 결과 객체를 반환합니다."""

        return SimpleNamespace(
            moving_model_id="moving",
            reference_model_ids=reference_ids,
            initial_alignment=expanded_input.seed.marker_result.preview_alignment,
            final_alignment=expanded_input.seed.marker_result.preview_alignment,
            icp_delta=identity_sim3_transform(),
            quality=_Quality(),
            model_revision_snapshot=extended_revisions,
            algorithm_version=expanded_input.algorithm_version,
        )

    state = runtime.icp_alignment
    state.phase = ICP_ALIGNMENT_PHASE_RUNNING
    state.input_data = expanded_input
    state.job_handle = SimpleNamespace(job_id="partial-reference-job")
    arrays = {
        "initial_alignment": expanded_input.seed.initial_alignment_float64,
        "final_alignment": expanded_input.seed.initial_alignment_float64,
        "icp_delta": identity_sim3_transform().matrix_float64,
    }

    def _metadata(reference_ids: tuple[str, ...]) -> dict[str, object]:
        """runtime strict metadata 검증에 필요한 worker JSON 표현을 반환합니다."""

        return {
            "job_id": "partial-reference-job",
            "input_fingerprint": expanded_input.input_fingerprint,
            "marker_signature": expanded_input.seed.marker_signature,
            "algorithm_version": expanded_input.algorithm_version,
            "parameter_fingerprint": expanded_input.parameters.fingerprint,
            "moving_model_id": "moving",
            "reference_model_ids": list(reference_ids),
            "model_revision_snapshot": [list(item) for item in extended_revisions],
            "model_state_snapshot": [list(item) for item in extended_model_state],
            "quality": {"status": "PASS"},
            "level_results": [],
            "best_candidate": {
                "delta_4x4": identity_sim3_transform().matrix_float64.tolist(),
                "final_alignment_4x4": expanded_input.seed.initial_alignment_float64.tolist(),
                "level_name": "fine",
                "iteration": 1,
                "objective_kind": "POINT_TO_PLANE",
                "point_to_plane_rmse": 0.1,
                "point_to_point_rmse": 0.1,
                "fitness": 1.0,
                "inlier_count": 4,
                "reciprocal_ratio": 1.0,
                "normal_valid_ratio": 1.0,
                "normal_angle_pass_ratio": 1.0,
                "maximum_correspondence_distance": 1.0,
                "correspondence_signature": "subset-correspondence",
                "evaluation_fingerprint": "subset-evaluation",
            },
            "baseline_metrics": {
                "point_to_point_rmse": 0.1,
                "point_to_plane_rmse": 0.1,
                "fitness": 1.0,
                "reciprocal_ratio": 1.0,
                "inlier_count": 4,
                "query_count": 4,
                "maximum_correspondence_distance": 1.0,
                "normal_valid_ratio": 1.0,
                "normal_angle_pass_ratio": 1.0,
                "evaluation_fingerprint": "subset-evaluation",
            },
            "best_candidate_metrics": {
                "point_to_point_rmse": 0.1,
                "point_to_plane_rmse": 0.1,
                "fitness": 1.0,
                "reciprocal_ratio": 1.0,
                "inlier_count": 4,
                "query_count": 4,
                "maximum_correspondence_distance": 1.0,
                "normal_valid_ratio": 1.0,
                "normal_angle_pass_ratio": 1.0,
                "evaluation_fingerprint": "subset-evaluation",
            },
            "relative_improvement": 0.0,
            "best_level_name": "fine",
            "best_iteration": 1,
            "solver_stop_reason": "CONVERGED",
            "point_to_plane_rmse": 0.1,
            "normal_valid_ratio": 1.0,
            "normal_angle_pass_ratio": 1.0,
            "density_sampling_metrics": {
                "shared_voxel_size": 0.1,
                "macro_cell_divisions": 1,
                "macro_cell_count": 1,
                "occupied_macro_cell_count": 1,
                "minimum_samples_per_macro_cell": 4,
                "median_samples_per_macro_cell": 4.0,
                "maximum_samples_per_macro_cell": 4,
            },
            "user_review_required": False,
        }

    original_from_mapping = IcpAlignmentResult.__dict__["from_mapping"]
    try:
        IcpAlignmentResult.from_mapping = classmethod(
            lambda _class, _mapping: _core_result(("reference",))
        )
        result = _runtime_result_from_worker(runtime, state, _metadata(("reference",)), arrays)
        assert result.reference_model_ids == ("reference",)
        IcpAlignmentResult.from_mapping = classmethod(
            lambda _class, _mapping: _core_result(("reference_far", "reference"))
        )
        try:
            _runtime_result_from_worker(
                runtime,
                state,
                _metadata(("reference_far", "reference")),
                arrays,
            )
        except IcpRuntimeError:
            pass
        else:
            raise AssertionError("순서가 바뀐 A 부분집합 결과가 수용되었습니다.")

        for field_name, invalid_value in (
            ("model_revision_snapshot", float("inf")),
            ("model_state_snapshot", float("inf")),
            ("model_state_snapshot", "7"),
        ):
            malformed_metadata = _metadata(("reference",))
            malformed_metadata[field_name][0][1] = invalid_value
            try:
                _runtime_result_from_worker(runtime, state, malformed_metadata, arrays)
            except IcpJobProtocolError:
                pass
            else:
                raise AssertionError(f"결과 {field_name}의 잘못된 revision이 수용되었습니다.")
    finally:
        IcpAlignmentResult.from_mapping = original_from_mapping


def test_invalidate_running_worker_preserves_live_handle_until_kill_or_error() -> None:
    """lifecycle 종료가 cancel·terminate·kill·job 정리 순서를 지키고 live handle을 잃지 않는지 검증합니다."""

    from tools.align.alignment import invalidate_icp
    from tools.align.alignment.icp_backend import IcpJobHandle, create_icp_job_directory
    from tools.align.alignment.icp_runtime import IcpRuntimeError
    from tools.align.constants import ICP_ALIGNMENT_PHASE_IDLE, ICP_ALIGNMENT_PHASE_RUNNING

    runtime, _marker_result = _runtime_with_marker_preview()
    files = create_icp_job_directory()
    process = _ControllableProcess(stop_on_terminate=True, stop_on_kill=True)
    handle = IcpJobHandle(
        job_id=files.job_directory.name,
        files=files,
        process=process,
        input_fingerprint="lifecycle-fingerprint",
    )
    state = runtime.icp_alignment
    state.phase = ICP_ALIGNMENT_PHASE_RUNNING
    state.job_handle = handle
    invalidate_icp(runtime, "테스트 lifecycle 종료")
    assert process.terminate_count == 1
    assert process.kill_count == 0
    assert not files.job_directory.exists()
    assert state.phase == ICP_ALIGNMENT_PHASE_IDLE
    assert state.job_handle is None

    files = create_icp_job_directory()
    process = _ControllableProcess(stop_on_terminate=False, stop_on_kill=False)
    handle = IcpJobHandle(
        job_id=files.job_directory.name,
        files=files,
        process=process,
        input_fingerprint="live-handle-fingerprint",
    )
    state.phase = ICP_ALIGNMENT_PHASE_RUNNING
    state.job_handle = handle
    try:
        invalidate_icp(runtime, "종료 실패 보존")
    except IcpRuntimeError:
        pass
    else:
        raise AssertionError("살아 있는 worker handle이 유실되었습니다.")
    assert state.job_handle is handle
    assert files.job_directory.exists()
    assert process.terminate_count == 1
    assert process.kill_count == 1
    process.return_code = -9
    invalidate_icp(runtime, "테스트 종료 후 정리")
    assert not files.job_directory.exists()
    assert state.phase == ICP_ALIGNMENT_PHASE_IDLE


def test_lifecycle_stops_poll_timer_only_after_worker_invalidation() -> None:
    """load·unregister·clear 경계가 worker 종료 뒤 timer·marker를 정리하는지 spy로 검증합니다."""

    import tools.align.alignment.icp_runtime as icp_runtime_module
    import tools.align.operators.marker_alignment as marker_operator_module
    import tools.align.operators.session_guard as session_guard_module
    import tools.align.session as session_module
    from tools.align.state import RuntimeState

    events: list[str] = []
    original_invalidate = icp_runtime_module.invalidate_icp
    original_stop_timer = marker_operator_module.stop_icp_poll_timer_for_lifecycle
    original_discard = marker_operator_module.discard_marker_preview_for_lifecycle
    original_stop_guard = session_guard_module.stop_input_guard_timer
    original_cancel_recovery = session_module.cancel_workspace_recovery
    original_cancel_exit = session_module.cancel_session_exit_finish
    original_request_redraw = session_module.request_redraw
    original_session_runtime = session_module.RUNTIME

    def _invalidate(_runtime, _reason: str) -> None:
        """worker 종료 완료 지점을 기록하는 대체 함수입니다."""

        events.append("worker")

    def _stop_timer() -> None:
        """poll timer 해제 지점을 기록하는 대체 함수입니다."""

        events.append("timer")

    try:
        icp_runtime_module.invalidate_icp = _invalidate
        marker_operator_module.stop_icp_poll_timer_for_lifecycle = _stop_timer
        marker_operator_module.discard_marker_preview_for_lifecycle = lambda: events.append("marker") or False
        session_guard_module.stop_input_guard_timer = lambda: events.append("guard")
        session_module.cancel_workspace_recovery = lambda: events.append("cancel-recovery")
        session_module.cancel_session_exit_finish = lambda: events.append("cancel-exit")
        session_module.request_redraw = lambda: events.append("redraw")

        session_runtime = RuntimeState()
        session_module.RUNTIME = session_runtime
        session_module.clear_session_for_file_load()
        assert events.index("worker") < events.index("timer") < events.index("marker")

        events.clear()
        session_module.restore_session_for_unregister()
        assert events.index("worker") < events.index("timer") < events.index("marker")

        events.clear()
        transient_runtime = RuntimeState()
        transient_runtime.icp_timer = lambda: None
        transient_runtime.clear_transient()
        assert events == ["worker", "timer"]
    finally:
        icp_runtime_module.invalidate_icp = original_invalidate
        marker_operator_module.stop_icp_poll_timer_for_lifecycle = original_stop_timer
        marker_operator_module.discard_marker_preview_for_lifecycle = original_discard
        session_guard_module.stop_input_guard_timer = original_stop_guard
        session_module.cancel_workspace_recovery = original_cancel_recovery
        session_module.cancel_session_exit_finish = original_cancel_exit
        session_module.request_redraw = original_request_redraw
        session_module.RUNTIME = original_session_runtime


def test_actual_hidden_worker_uses_lightweight_runtime_input_and_cleans_job_directory() -> None:
    """실제 Blender worker가 chunk job을 읽어 preview까지 전이하고 Windows job 폴더를 지우는지 검증합니다."""

    import bpy

    from tools.align.alignment import (
        begin_icp_job,
        discard_icp_preview,
        invalidate_icp,
        poll_icp_job,
    )
    from tools.align.constants import ICP_ALIGNMENT_PHASE_FAILED, ICP_ALIGNMENT_PHASE_PREVIEW

    runtime, _marker_result = _runtime_with_marker_preview(count=256)
    state = runtime.icp_alignment
    try:
        handle = begin_icp_job(
            runtime,
            preset="FAST",
            scale_refine_enabled=False,
            blender_binary_path=bpy.app.binary_path,
        )
        job_directory = handle.files.job_directory
        assert state.input_data is not None
        assert state.input_data.snapshot is None
        for _index in range(400):
            poll_icp_job(runtime)
            if state.phase in {ICP_ALIGNMENT_PHASE_PREVIEW, ICP_ALIGNMENT_PHASE_FAILED}:
                break
            sleep(0.05)
        assert state.phase == ICP_ALIGNMENT_PHASE_PREVIEW, state.last_error
        assert state.result is not None
        assert not job_directory.exists()
        discard_icp_preview(runtime)
    finally:
        if state.has_preview:
            discard_icp_preview(runtime)
        if state.job_handle is not None or state.diagnostic_handles:
            invalidate_icp(runtime, "hidden worker 테스트 작업을 정리합니다.")


def run_all() -> None:
    """ICP runtime의 독립·실제 worker 회귀 검사를 순서대로 실행합니다."""

    _add_test_paths()
    test_direct_input_does_not_mutate_source_and_rejects_negative_ids()
    test_quality_rejection_keeps_marker_preview_and_lifecycle_cleans_diagnostics()
    test_corrupt_terminal_error_json_finalizes_failed_state()
    test_corrupt_terminal_result_json_finalizes_failed_state()
    test_nonfinite_result_metadata_finalizes_failed_state()
    test_corrupt_terminal_result_npz_finalizes_failed_state()
    test_corrupt_terminal_progress_finalizes_failed_state()
    test_progress_payload_rejects_implicit_or_nonfinite_numbers()
    test_runtime_reads_typed_hybrid_progress_metrics()
    test_poll_timer_finalizes_nonfinite_progress_without_exception()
    test_missing_terminal_progress_finalizes_failed_state()
    test_finish_normalizes_core_worker_error_to_failed_state()
    test_pass_and_review_results_both_enter_approvable_preview()
    test_poll_timer_handles_corrupt_terminal_output_without_exception()
    test_approve_undo_redo_and_restore_rollback_are_atomic()
    test_runtime_accepts_ordered_overlapping_reference_subset()
    test_invalidate_running_worker_preserves_live_handle_until_kill_or_error()
    test_lifecycle_stops_poll_timer_only_after_worker_invalidation()
    test_actual_hidden_worker_uses_lightweight_runtime_input_and_cleans_job_directory()
    print("test_icp_runtime: 통과")


if __name__ == "__main__":
    run_all()
