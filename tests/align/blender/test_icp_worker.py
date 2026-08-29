"""ICP worker 파일 계약·chunk 입력·구조화 실패 경계를 Blender에서 검증합니다."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Iterator

import numpy as np


def _add_source_path() -> None:
    """애드온 소스 디렉터리를 Blender 테스트 모듈 경로에 추가합니다."""

    source_path = Path(__file__).resolve().parents[3]
    source_text = str(source_path)
    if source_text not in sys.path:
        sys.path.insert(0, source_text)


def _worker_input_positions() -> np.ndarray:
    """worker manifest 복원에 사용할 최소 비퇴화 점군을 반환합니다."""

    return np.asarray(
        (
            (0.0, 0.0, 0.0),
            (1.0, 0.0, 0.0),
            (0.0, 1.0, 0.0),
            (0.0, 0.0, 1.0),
        ),
        dtype=np.float64,
    )


def _write_worker_input(files):
    """solver 대체 테스트가 사용할 완전한 worker 입력 파일 묶음을 기록합니다."""

    from tools.align.alignment import (
        IcpInputSnapshot,
        IcpParameters,
        IcpPointCloud,
        IcpPreset,
        identity_sim3_transform,
    )
    from tools.align.alignment.icp_backend import (
        build_manifest,
        write_array_atomically,
        write_job_manifest,
    )

    positions = _worker_input_positions()
    point_ids = np.asarray((101, 102, 103, 104), dtype=np.uint64)
    moving_cloud = IcpPointCloud(
        model_id="moving",
        positions_float64=positions,
        point_ids_uint64=point_ids,
    )
    reference_cloud = IcpPointCloud(
        model_id="reference",
        positions_float64=positions,
        point_ids_uint64=point_ids,
    )
    parameters = IcpParameters.for_preset(IcpPreset.FAST, scale_refine_enabled=False)
    marker_points = positions[:3]
    snapshot = IcpInputSnapshot(
        moving_model_id="moving",
        reference_model_ids=("reference",),
        moving_cloud=moving_cloud,
        reference_clouds=(reference_cloud,),
        initial_alignment=identity_sim3_transform(),
        parameters=parameters,
        marker_uniform_scale=1.0,
        marker_max_error=0.0,
        marker_rms=0.0,
        marker_reference_project=marker_points,
        marker_moving_project=marker_points,
        marker_signature="worker-test-marker-signature",
        model_revision_snapshot=(("moving", 1), ("reference", 1)),
    )
    write_array_atomically(
        files.job_directory / "moving_positions.npy",
        positions,
        dtype=np.dtype(np.float64),
        shape=(None, 3),
    )
    write_array_atomically(
        files.job_directory / "moving_point_ids.npy",
        point_ids,
        dtype=np.dtype(np.uint64),
        shape=(positions.shape[0],),
    )
    write_array_atomically(
        files.job_directory / "reference_positions.npy",
        positions,
        dtype=np.dtype(np.float64),
        shape=(None, 3),
    )
    write_array_atomically(
        files.job_directory / "reference_point_ids.npy",
        point_ids,
        dtype=np.dtype(np.uint64),
        shape=(positions.shape[0],),
    )
    manifest = build_manifest(
        job_id=files.job_directory.name,
        input_fingerprint=snapshot.input_fingerprint,
        payload={
            "algorithm_version": snapshot.algorithm_version,
            "parameters": parameters.to_mapping(),
            "moving_model_id": "moving",
            "reference_model_ids": ["reference"],
            "moving_cloud_frame": snapshot.moving_cloud_frame.value,
            "initial_alignment": snapshot.initial_alignment.matrix_float64.tolist(),
            "marker_uniform_scale": snapshot.marker_uniform_scale,
            "marker_max_error": snapshot.marker_max_error,
            "marker_rms": snapshot.marker_rms,
            "marker_reference_project": marker_points.tolist(),
            "marker_moving_project": marker_points.tolist(),
            "marker_signature": snapshot.marker_signature,
            "model_revision_snapshot": [list(item) for item in snapshot.model_revision_snapshot],
            "model_state_snapshot": [
                ["moving", 1, "moving-display", "moving-alignment"],
                ["reference", 1, "reference-display", "reference-alignment"],
            ],
            "arrays": {
                "moving": {
                    "model_id": "moving",
                    "positions_file": "moving_positions.npy",
                    "point_ids_file": "moving_point_ids.npy",
                },
                "references": [
                    {
                        "model_id": "reference",
                        "positions_file": "reference_positions.npy",
                        "point_ids_file": "reference_point_ids.npy",
                    }
                ],
            },
        },
    )
    write_job_manifest(files, manifest)
    return snapshot, manifest


@contextmanager
def _patched_solver(callback) -> Iterator[None]:
    """worker import 경계에서 solver 하나만 임시 대체합니다."""

    from tools.align.alignment import icp_solver

    original = icp_solver.solve_multiscale_icp
    icp_solver.solve_multiscale_icp = callback
    try:
        yield
    finally:
        icp_solver.solve_multiscale_icp = original


class _WorkerResult:
    """worker JSON·NPZ 분리만 검증할 최소 solver 결과 대체 객체입니다."""

    def __init__(self, snapshot, *, quality_status: str = "PASS") -> None:
        """입력 snapshot과 일치하는 identity 결과를 구성합니다."""

        from tools.align.alignment import identity_sim3_transform

        self.initial_alignment = snapshot.initial_alignment
        self.final_alignment = snapshot.initial_alignment
        self.icp_delta = identity_sim3_transform()
        self._snapshot = snapshot
        self._quality_status = quality_status
        self.quality = type("WorkerQuality", (), {"status": quality_status})()

    def to_mapping(self) -> dict[str, object]:
        """worker 메타데이터가 요구하는 JSON 안전 필드를 반환합니다."""

        return {
            "moving_model_id": self._snapshot.moving_model_id,
            "reference_model_ids": list(self._snapshot.reference_model_ids),
            "initial_alignment_4x4": self.initial_alignment.matrix_float64.tolist(),
            "final_alignment_4x4": self.final_alignment.matrix_float64.tolist(),
            "icp_delta_4x4": self.icp_delta.matrix_float64.tolist(),
            "quality": {"status": self._quality_status},
            "level_results": [],
            "input_fingerprint": self._snapshot.input_fingerprint,
            "parameter_fingerprint": self._snapshot.parameters.fingerprint,
            "model_revision_snapshot": [
                list(item) for item in self._snapshot.model_revision_snapshot
            ],
            "marker_signature": self._snapshot.marker_signature,
            "algorithm_version": self._snapshot.algorithm_version,
            "best_candidate": {
                "delta_4x4": self.icp_delta.matrix_float64.tolist(),
                "final_alignment_4x4": self.final_alignment.matrix_float64.tolist(),
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
                "correspondence_signature": "worker-test-correspondence",
                "evaluation_fingerprint": "worker-test-evaluation",
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
                "evaluation_fingerprint": "worker-test-evaluation",
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
                "evaluation_fingerprint": "worker-test-evaluation",
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
            "user_review_required": self._quality_status == "REVIEW",
        }


def _new_worker_files():
    """각 worker 분기 테스트에 독립 임시 작업 폴더를 생성합니다."""

    from tools.align.alignment.icp_backend import create_icp_job_directory

    return create_icp_job_directory()


def _cleanup_worker_files(files) -> None:
    """테스트가 만든 아직 시작하지 않은 전용 job 폴더를 정리합니다."""

    from tools.align.alignment.icp_backend import cleanup_unstarted_job_directory

    if files.job_directory.exists():
        cleanup_unstarted_job_directory(files)


def test_manifest_reservation_and_chunk_writer_contract() -> None:
    """예약 manifest 키와 chunk NPY 입력의 형식·원자 실패 정리를 검증합니다."""

    from tools.align.alignment.icp_backend import (
        IcpJobProtocolError,
        _chunk_size,
        build_manifest,
        read_array_file,
        write_point_ids_in_chunks_atomically,
        write_transformed_positions_atomically,
    )

    with TemporaryDirectory(prefix="colmap_align_icp_worker_test_") as directory:
        job_directory = Path(directory) / "job"
        job_directory.mkdir()
        for invalid_size in (True, 1.5, "32"):
            try:
                _chunk_size(invalid_size)
            except IcpJobProtocolError:
                pass
            else:
                raise AssertionError("ICP chunk 크기의 암묵 변환이 허용되었습니다.")
        try:
            build_manifest(
                job_id="worker-test",
                input_fingerprint="fingerprint",
                payload={"job_id": "forbidden"},
            )
        except IcpJobProtocolError:
            pass
        else:
            raise AssertionError("manifest 예약 키 덮어쓰기가 허용되었습니다.")
        try:
            build_manifest(
                job_id="worker-test",
                input_fingerprint="fingerprint",
                payload={"algorithm_version": "point-to-point-icp-v1"},
            )
        except IcpJobProtocolError:
            pass
        else:
            raise AssertionError("구 알고리즘 manifest가 Hybrid worker에 수용되었습니다.")

        positions = np.asarray(((1.0, 2.0, 3.0), (-2.0, 0.5, 4.0)), dtype=np.float64)
        matrix = np.eye(4, dtype=np.float64)
        matrix[:3, 3] = np.asarray((5.0, -1.0, 2.0), dtype=np.float64)
        write_transformed_positions_atomically(job_directory / "positions.npy", positions, matrix)
        transformed = read_array_file(
            job_directory,
            "positions.npy",
            dtype=np.dtype(np.float64),
            shape=(None, 3),
        )
        assert np.allclose(transformed, positions + matrix[:3, 3], rtol=0.0, atol=0.0)
        write_point_ids_in_chunks_atomically(
            job_directory / "point_ids.npy",
            np.asarray((41, 42), dtype=np.uint64),
        )

        overflow_matrix = np.eye(4, dtype=np.float64)
        overflow_matrix[0, 0] = 2.0
        with np.errstate(over="ignore"):
            try:
                write_transformed_positions_atomically(
                    job_directory / "overflow.npy",
                    np.asarray(((np.finfo(np.float64).max, 0.0, 0.0),), dtype=np.float64),
                    overflow_matrix,
                )
            except IcpJobProtocolError:
                pass
            else:
                raise AssertionError("유한하지 않은 변환 결과가 기록되었습니다.")
        assert not (job_directory / "overflow.npy").exists()
        assert not tuple(job_directory.glob(".overflow.npy.*.tmp"))

        for invalid_ids in (
            np.asarray((9, -1), dtype=np.int64),
            np.asarray((9, 9), dtype=np.uint64),
        ):
            try:
                write_point_ids_in_chunks_atomically(job_directory / "invalid_ids.npy", invalid_ids)
            except IcpJobProtocolError:
                pass
            else:
                raise AssertionError("잘못된 POINT3D_ID가 기록되었습니다.")
        assert not (job_directory / "invalid_ids.npy").exists()
        assert not tuple(job_directory.glob(".invalid_ids.npy.*.tmp"))


def test_unstarted_job_cleanup_removes_partial_chunk_files() -> None:
    """입력 준비 중 하나의 NPY 기록이 실패해도 job 디렉터리가 남지 않는지 검증합니다."""

    from tools.align.alignment.icp_backend import (
        cleanup_unstarted_job_directory,
        create_icp_job_directory,
        write_transformed_positions_atomically,
    )

    files = create_icp_job_directory()
    try:
        write_transformed_positions_atomically(
            files.job_directory / "partial.npy",
            np.asarray(((0.0, 0.0, 0.0),), dtype=np.float64),
            np.eye(4, dtype=np.float64),
        )
        try:
            write_transformed_positions_atomically(
                files.job_directory / "will_fail.npy",
                np.asarray(((np.nan, 0.0, 0.0),), dtype=np.float64),
                np.eye(4, dtype=np.float64),
            )
        except Exception:
            cleanup_unstarted_job_directory(files)
        else:
            raise AssertionError("유한하지 않은 입력이 거부되지 않았습니다.")
        assert not files.job_directory.exists()
    finally:
        if files.job_directory.exists():
            cleanup_unstarted_job_directory(files)


def test_atomic_replace_retries_short_windows_read_share_conflict() -> None:
    """progress 읽기와 겹친 짧은 Windows 공유 충돌이 worker 실패가 되지 않는지 검증합니다."""

    import colmap_align.alignment.icp_backend as backend_module

    with TemporaryDirectory(prefix="colmap_align_icp_replace_retry_") as directory:
        job_directory = Path(directory) / "job"
        job_directory.mkdir()
        target = job_directory / "progress.json"
        original_replace = backend_module.os.replace
        replace_calls = 0

        def _replace_once_denied(source, destination) -> None:
            """첫 원자 교체만 공유 충돌로 실패시키고 다음 호출은 실제 교체합니다."""

            nonlocal replace_calls
            replace_calls += 1
            if replace_calls == 1:
                raise PermissionError("테스트용 공유 충돌")
            original_replace(source, destination)

        backend_module.os.replace = _replace_once_denied
        try:
            backend_module.write_json_atomically(target, {"progress": 0.5})
        finally:
            backend_module.os.replace = original_replace
        assert replace_calls == 2
        assert backend_module.read_json_file(target) == {"progress": 0.5}


def test_json_reader_retries_short_windows_share_conflict() -> None:
    """원자 progress를 읽는 순간의 짧은 Windows 공유 충돌을 재시도합니다."""

    import colmap_align.alignment.icp_backend as backend_module

    with TemporaryDirectory(prefix="colmap_align_icp_read_retry_") as directory:
        target = Path(directory) / "progress.json"
        target.write_text('{"progress":0.5}', encoding="utf-8")
        original_read_text = Path.read_text
        read_calls = 0

        def _read_once_denied(path, *args, **kwargs):
            """첫 읽기만 공유 충돌로 실패시키고 다음 호출은 실제 JSON을 읽습니다."""

            nonlocal read_calls
            read_calls += 1
            if path == target and read_calls == 1:
                raise PermissionError("테스트용 읽기 공유 충돌")
            return original_read_text(path, *args, **kwargs)

        Path.read_text = _read_once_denied
        try:
            assert backend_module.read_json_file(target) == {"progress": 0.5}
        finally:
            Path.read_text = original_read_text
        assert read_calls == 2


def test_json_reader_rejects_nonfinite_numbers() -> None:
    """외부 JSON의 1e999·NaN·Infinity를 공통 protocol 오류로 거부하는지 검증합니다."""

    from tools.align.alignment.icp_backend import (
        IcpJobProtocolError,
        cleanup_unstarted_job_directory,
        create_icp_job_directory,
        read_json_file,
    )

    files = create_icp_job_directory()
    try:
        for raw_json in (b'{"value":1e999}', b'{"value":NaN}', b'{"value":Infinity}'):
            files.progress_path.write_bytes(raw_json)
            try:
                read_json_file(files.progress_path)
            except IcpJobProtocolError:
                pass
            else:
                raise AssertionError("비유한 외부 JSON 수치가 수용되었습니다.")
    finally:
        cleanup_unstarted_job_directory(files)
    assert not files.job_directory.exists()


def test_json_reader_rejects_invalid_utf8() -> None:
    """손상된 UTF-8 작업 JSON을 공통 protocol 오류로 거부합니다."""

    from tools.align.alignment.icp_backend import IcpJobProtocolError, read_json_file

    with TemporaryDirectory(prefix="colmap_align_icp_invalid_utf8_") as directory:
        target = Path(directory) / "progress.json"
        target.write_bytes(b"\xff")
        try:
            read_json_file(target)
        except IcpJobProtocolError:
            pass
        else:
            raise AssertionError("손상된 UTF-8 작업 JSON이 수용되었습니다.")


def test_manifest_job_id_must_match_job_directory() -> None:
    """시작 전 manifest 변조가 handle·worker 식별자를 바꾸지 못하는지 검증합니다."""

    from tools.align.alignment.icp_backend import (
        IcpJobProtocolError,
        launch_icp_worker,
        write_job_manifest,
    )
    from tools.align.alignment.icp_worker import input_snapshot_from_manifest

    files = _new_worker_files()
    try:
        snapshot, manifest = _write_worker_input(files)
        manifest["job_id"] = "altered-job-id"
        write_job_manifest(files, manifest)
        try:
            input_snapshot_from_manifest(files.job_directory)
        except IcpJobProtocolError:
            pass
        else:
            raise AssertionError("worker가 변조된 job_id manifest를 수용했습니다.")
        try:
            launch_icp_worker(
                files,
                blender_binary_path=Path(__file__),
                input_fingerprint=snapshot.input_fingerprint,
            )
        except IcpJobProtocolError:
            pass
        else:
            raise AssertionError("launcher가 변조된 job_id manifest를 수용했습니다.")
    finally:
        _cleanup_worker_files(files)


def test_worker_normal_result_keeps_matrices_in_npz_only() -> None:
    """정상 worker 결과에서 JSON 메타와 NPZ 행렬의 단일 소스 계약을 검증합니다."""

    from tools.align.alignment.icp_backend import (
        IcpJobProtocolError,
        read_job_progress,
        read_job_result,
        write_json_atomically,
    )
    from tools.align.alignment.icp_worker import main

    files = _new_worker_files()
    try:
        snapshot, _manifest = _write_worker_input(files)
        with _patched_solver(lambda input_snapshot, **_kwargs: _WorkerResult(input_snapshot)):
            assert main(["--job-dir", str(files.job_directory)]) == 0
        from tools.align.alignment.icp_backend import IcpJobHandle

        class _ExitedProcess:
            """완료된 결과 읽기만 위한 최소 process 대체 객체입니다."""

            def poll(self):
                """이미 정상 종료한 코드만 반환합니다."""

                return 0

        handle = IcpJobHandle(
            job_id=files.job_directory.name,
            files=files,
            process=_ExitedProcess(),
            input_fingerprint=snapshot.input_fingerprint,
        )
        result_data = read_job_result(handle)
        assert result_data is not None
        metadata, arrays = result_data
        assert not {"initial_alignment_4x4", "final_alignment_4x4", "icp_delta_4x4"}.intersection(metadata)
        assert set(arrays) == {"initial_alignment", "final_alignment", "icp_delta"}
        assert metadata["input_fingerprint"] == snapshot.input_fingerprint
        assert metadata["best_candidate_metrics"]["point_to_plane_rmse"] == 0.1
        assert metadata["density_sampling_metrics"]["shared_voxel_size"] == 0.1
        progress = read_job_progress(handle)
        assert progress is not None
        assert progress["algorithm_version"] == snapshot.algorithm_version
        assert progress["stage"] == "COMPLETED_PASS"
        assert "stable_alignment_4x4" in progress
        incomplete_metadata = dict(metadata)
        incomplete_metadata.pop("best_candidate_metrics")
        write_json_atomically(files.result_path, incomplete_metadata)
        try:
            read_job_result(handle)
        except IcpJobProtocolError:
            pass
        else:
            raise AssertionError("Hybrid 결과의 필수 후보 지표 누락이 수용되었습니다.")
    finally:
        _cleanup_worker_files(files)


def test_worker_records_review_terminal_with_best_candidate_progress() -> None:
    """유효하지만 검토가 필요한 결과도 REVIEW 완료와 미리보기 후보를 기록하는지 검증합니다."""

    from tools.align.alignment.icp_backend import IcpJobHandle, read_job_progress, read_job_result
    from tools.align.alignment.icp_worker import main

    class _ExitedProcess:
        """완료된 worker 파일을 읽기 위한 최소 종료 process를 제공합니다."""

        def poll(self):
            """정상 종료 코드를 반환합니다."""

            return 0

    files = _new_worker_files()
    try:
        snapshot, _manifest = _write_worker_input(files)
        with _patched_solver(
            lambda input_snapshot, **_kwargs: _WorkerResult(
                input_snapshot,
                quality_status="REVIEW",
            )
        ):
            assert main(["--job-dir", str(files.job_directory)]) == 0
        handle = IcpJobHandle(
            job_id=files.job_directory.name,
            files=files,
            process=_ExitedProcess(),
            input_fingerprint=snapshot.input_fingerprint,
        )
        metadata, _arrays = read_job_result(handle) or (None, None)
        assert metadata is not None
        assert metadata["quality"]["status"] == "REVIEW"
        assert metadata["user_review_required"] is True
        progress = read_job_progress(handle)
        assert progress is not None
        assert progress["stage"] == "COMPLETED_REVIEW"
        assert "stable_alignment_4x4" in progress
    finally:
        _cleanup_worker_files(files)


def test_worker_maps_typed_hybrid_progress_metrics() -> None:
    """core iteration 지표가 메시지 파싱 없이 고정 IPC 필드로 전달되는지 검증합니다."""

    from tools.align.alignment.icp_backend import IcpJobProtocolError
    from tools.align.alignment.icp_worker import _progress_payload

    progress = SimpleNamespace(
        stage="ICP_FINE_POINT_TO_PLANE",
        level_name="fine",
        iteration=3,
        max_iterations=12,
        progress=0.7,
        message="임의 메시지",
        level_complete=False,
        stable_alignment_float64=None,
        objective_kind="POINT_TO_PLANE",
        maximum_correspondence_distance=0.25,
        correspondence_count=82,
        point_to_point_rmse=0.04,
        point_to_plane_rmse=0.02,
        normal_valid_count=91,
        normal_angle_pass_count=76,
        normal_radius=0.11,
        reference_sample_count=512,
        moving_sample_count=256,
    )
    payload = _progress_payload(
        progress,
        job_id="typed-progress-job",
        input_fingerprint="typed-progress-fingerprint",
        moving_model_id="moving",
    )
    assert payload["objective_kind"] == "POINT_TO_PLANE"
    assert payload["maximum_correspondence_distance"] == 0.25
    assert payload["correspondence_count"] == 82
    assert payload["point_to_point_rmse"] == 0.04
    assert payload["point_to_plane_rmse"] == 0.02
    assert payload["normal_valid_count"] == 91
    assert payload["normal_angle_pass_count"] == 76
    assert payload["normal_radius"] == 0.11
    assert payload["reference_sample_count"] == 512
    assert payload["moving_sample_count"] == 256
    progress.correspondence_count = True
    try:
        _progress_payload(
            progress,
            job_id="typed-progress-job",
            input_fingerprint="typed-progress-fingerprint",
            moving_model_id="moving",
        )
    except IcpJobProtocolError:
        pass
    else:
        raise AssertionError("bool 대응점 수가 typed progress에 수용되었습니다.")


def test_worker_records_cancel_quality_and_failures_structurally() -> None:
    """취소·품질 거부·수치 실패·예기치 않은 실패가 모두 구조화 파일을 남기는지 검증합니다."""

    from tools.align.alignment import (
        IcpCancelledError,
        IcpNumericalError,
        IcpQualityRejectedError,
    )
    from tools.align.alignment.icp_backend import read_job_error
    from tools.align.alignment.icp_worker import main

    class _Quality:
        """품질 거부 사유 키 계약을 검증할 최소 quality 객체입니다."""

        def to_mapping(self) -> dict[str, object]:
            """전용 거부 사유와 구버전 검토 사유를 함께 반환합니다."""

            return {
                "rejection_reasons": ["겹침 비율이 최소 기준보다 낮습니다."],
                "review_reasons": ["구버전 사유"],
            }

    cases = (
        ("cancel", lambda _snapshot, **_kwargs: (_ for _ in ()).throw(IcpCancelledError("취소")), 0, "CANCELLED"),
        (
            "quality",
            lambda _snapshot, **_kwargs: (_ for _ in ()).throw(
                IcpQualityRejectedError("품질 거부", _Quality())
            ),
            0,
            "QUALITY_REJECTED",
        ),
        ("numerical", lambda _snapshot, **_kwargs: (_ for _ in ()).throw(IcpNumericalError("수치 실패")), 1, "FAILED"),
        ("unexpected", lambda _snapshot, **_kwargs: (_ for _ in ()).throw(RuntimeError("예기치 않은 실패")), 1, "FAILED"),
    )
    for _name, callback, expected_code, expected_status in cases:
        files = _new_worker_files()
        try:
            snapshot, _manifest = _write_worker_input(files)
            with _patched_solver(callback):
                assert main(["--job-dir", str(files.job_directory)]) == expected_code
            from tools.align.alignment.icp_backend import IcpJobHandle

            class _ExitedProcess:
                """error.json 읽기만 위한 종료 process 대체 객체입니다."""

                def poll(self):
                    """worker가 끝난 상태를 반환합니다."""

                    return expected_code

            handle = IcpJobHandle(
                job_id=files.job_directory.name,
                files=files,
                process=_ExitedProcess(),
                input_fingerprint=snapshot.input_fingerprint,
            )
            error = read_job_error(handle)
            assert error is not None
            assert error["status"] == expected_status
            assert error["job_id"] == files.job_directory.name
            assert error["input_fingerprint"] == snapshot.input_fingerprint
            if expected_status == "QUALITY_REJECTED":
                assert error["rejection_reasons"] == ["겹침 비율이 최소 기준보다 낮습니다."]
                assert error["review_reasons"] == error["rejection_reasons"]
        finally:
            _cleanup_worker_files(files)


def run_all() -> None:
    """ICP worker 단위·파일 계약 검사를 순서대로 실행합니다."""

    _add_source_path()
    test_manifest_reservation_and_chunk_writer_contract()
    test_unstarted_job_cleanup_removes_partial_chunk_files()
    test_atomic_replace_retries_short_windows_read_share_conflict()
    test_json_reader_retries_short_windows_share_conflict()
    test_json_reader_rejects_nonfinite_numbers()
    test_json_reader_rejects_invalid_utf8()
    test_manifest_job_id_must_match_job_directory()
    test_worker_normal_result_keeps_matrices_in_npz_only()
    test_worker_records_review_terminal_with_best_candidate_progress()
    test_worker_maps_typed_hybrid_progress_metrics()
    test_worker_records_cancel_quality_and_failures_structurally()
    print("test_icp_worker: 통과")


if __name__ == "__main__":
    run_all()
