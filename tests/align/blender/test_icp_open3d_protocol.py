"""Open3D 외부 worker의 protocol·전체점 audit 계약을 Blender에서 검증합니다."""

from __future__ import annotations

import builtins
import importlib
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace

import numpy as np


def _add_source_path() -> None:
    """애드온 소스 디렉터리를 Blender 테스트 모듈 경로에 추가합니다."""

    source_path = Path(__file__).resolve().parents[3]
    source_text = str(source_path)
    if source_text not in sys.path:
        sys.path.insert(0, source_text)


def _stage_audit(source_id_fingerprint: str, target_id_fingerprint: str) -> dict[str, str]:
    """동일 전체점 입력을 증명할 한 대응 거리 단계의 audit 값을 만듭니다."""

    return {
        "source_positions_fingerprint": "source-positions-fingerprint",
        "target_positions_fingerprint": "target-positions-fingerprint",
        "source_point_ids_fingerprint": source_id_fingerprint,
        "target_point_ids_fingerprint": target_id_fingerprint,
    }


def _open3d_result_metadata(backend_id: str) -> dict[str, object]:
    """Open3D 결과 schema 검증에 쓸 최소 유효 metadata fixture를 반환합니다."""

    from tools.align.alignment.icp_solver_backend import backend_algorithm_version
    from tools.align.constants import ICP_ALGORITHM_VERSION, ICP_JOB_PROTOCOL_VERSION

    source_ids = "source-point-ids-fingerprint"
    target_ids = "target-point-ids-fingerprint"
    stage = _stage_audit(source_ids, target_ids)
    return {
        "job_id": "open3d-protocol-job",
        "input_fingerprint": "open3d-input-fingerprint",
        "algorithm_version": ICP_ALGORITHM_VERSION,
        "backend_id": backend_id,
        "backend_algorithm_version": backend_algorithm_version(backend_id),
        "job_protocol_version": ICP_JOB_PROTOCOL_VERSION,
        "moving_model_id": "moving",
        "reference_model_ids": ["reference"],
        "quality": {"status": "REVIEW"},
        "level_results": [{"name": "fine"}],
        "parameter_fingerprint": "open3d-parameters-fingerprint",
        "model_revision_snapshot": [["moving", 1], ["reference", 2]],
        "model_state_snapshot": [
            ["moving", 1, "moving-display", "moving-alignment"],
            ["reference", 2, "reference-display", "reference-alignment"],
        ],
        "marker_signature": "open3d-marker-signature",
        "solver_stop_reason": "CONVERGED",
        "user_review_required": True,
        "full_input_audit": {
            "input_policy": "FULL_OVERLAP_NO_DOWNSAMPLING",
            "source_original_count": 7,
            "target_original_count": 9,
            "source_crop_count": 7,
            "target_crop_count": 9,
            "source_crop_point_ids_fingerprint": source_ids,
            "target_crop_point_ids_fingerprint": target_ids,
            "source_crop_positions_fingerprint": "source-positions-fingerprint",
            "target_crop_positions_fingerprint": "target-positions-fingerprint",
            "stage_input_fingerprints": {
                "coarse": dict(stage),
                "medium": dict(stage),
                "fine": dict(stage),
            },
            "downsampling": {
                "used": False,
                "voxel_down_sample": False,
                "point_budget_sampling": False,
            },
        },
        "worker_python_version": "3.12.4",
        "worker_numpy_version": "2.0.2",
        "open3d_version": "0.19.0",
        "platform_architecture": "AMD64",
        "local_origin": [0.0, 0.0, 0.0],
        "moving_diagonal": 1.0,
        "delta_translation": 0.0,
        "delta_rotation_degrees": 0.0,
        "scale_mode": "LOCKED",
        "scale_refine_status": "NOT_REQUESTED",
    }


def _expect_protocol_error(callback, description: str) -> None:
    """외부 metadata가 엄격한 protocol 오류로 거부되는지 확인합니다."""

    from tools.align.alignment.icp_backend import IcpJobProtocolError

    try:
        callback()
    except IcpJobProtocolError:
        return
    raise AssertionError(f"{description}가 Open3D protocol에 수용되었습니다.")


def test_open3d_solver_module_import_is_lazy() -> None:
    """Blender 기본 환경에서 Open3D 모듈을 즉시 import하지 않는지 확인합니다."""

    module_name = "tools.align.alignment.icp_open3d_solver"
    sys.modules.pop(module_name, None)
    original_import = builtins.__import__

    def _guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
        """Open3D top-level import가 일어나면 테스트를 즉시 실패시킵니다."""

        if name == "open3d" or name.startswith("open3d."):
            raise AssertionError("Open3D solver가 Blender import 시점에 open3d를 불러왔습니다.")
        return original_import(name, globals, locals, fromlist, level)

    builtins.__import__ = _guarded_import
    try:
        module = importlib.import_module(module_name)
    finally:
        builtins.__import__ = original_import
    assert module is not None


def test_open3d_worker_retries_atomic_json_and_npz_replace() -> None:
    """진행 JSON과 결과 NPZ가 짧은 Windows 공유 충돌 뒤 정상 교체되는지 확인합니다."""

    from tools.align.alignment import icp_open3d_worker as worker

    with TemporaryDirectory(prefix="colmap_align_open3d_replace_retry_") as directory:
        root = Path(directory)
        progress_path = root / "progress.json"
        result_path = root / "result.npz"
        progress_path.write_text('{"progress":0.0}', encoding="utf-8")
        original_replace = worker.os.replace
        denied_targets: set[Path] = set()
        replace_calls = 0

        def _replace_once_denied(source, destination) -> None:
            """대상별 첫 교체만 공유 충돌로 실패시키고 다음 호출은 실제 교체합니다."""

            nonlocal replace_calls
            replace_calls += 1
            target = Path(destination)
            if target not in denied_targets:
                denied_targets.add(target)
                raise PermissionError("테스트용 Windows 공유 충돌")
            original_replace(source, destination)

        worker.os.replace = _replace_once_denied
        try:
            worker._write_json_atomically(progress_path, {"progress": 0.5})
            identity = np.identity(4, dtype=np.float64)
            worker._write_result_arrays_atomically(
                result_path,
                {
                    "initial_alignment": identity,
                    "final_alignment": identity,
                    "icp_delta": identity,
                },
            )
        finally:
            worker.os.replace = original_replace

        assert replace_calls == 4
        assert progress_path.read_text(encoding="utf-8") == '{"progress":0.5}'
        with np.load(result_path, allow_pickle=False) as arrays:
            assert set(arrays.files) == {"initial_alignment", "final_alignment", "icp_delta"}
            np.testing.assert_array_equal(arrays["final_alignment"], np.identity(4))


def test_open3d_worker_preserves_target_after_persistent_replace_denial() -> None:
    """지속적인 공유 거부는 숨기지 않고 기존 파일과 임시 파일 정리를 보장합니다."""

    from tools.align.alignment import icp_open3d_worker as worker

    with TemporaryDirectory(prefix="colmap_align_open3d_replace_failure_") as directory:
        root = Path(directory)
        target = root / "progress.json"
        original_payload = '{"progress":0.25}'
        target.write_text(original_payload, encoding="utf-8")
        original_replace = worker.os.replace
        original_monotonic = worker.monotonic
        original_sleep = worker.sleep
        clock_values = iter((0.0, 0.5, 1.0))
        sleep_calls = 0

        def _always_denied(_source, _destination) -> None:
            """제한 시간 만료까지 원자 교체를 계속 거부합니다."""

            raise PermissionError("테스트용 지속 공유 충돌")

        def _record_sleep(_seconds: float) -> None:
            """실제 대기 없이 재시도 횟수만 기록합니다."""

            nonlocal sleep_calls
            sleep_calls += 1

        worker.os.replace = _always_denied
        worker.monotonic = lambda: next(clock_values)
        worker.sleep = _record_sleep
        try:
            try:
                worker._write_json_atomically(target, {"progress": 0.75})
            except PermissionError:
                pass
            else:
                raise AssertionError("지속적인 원자 교체 거부가 오류 없이 무시되었습니다.")
        finally:
            worker.os.replace = original_replace
            worker.monotonic = original_monotonic
            worker.sleep = original_sleep

        assert sleep_calls == 1
        assert target.read_text(encoding="utf-8") == original_payload
        assert not list(root.glob(".progress.json.*.tmp"))


def test_open3d_manifest_keeps_explicit_backend_protocol_identity() -> None:
    """Open3D manifest가 backend·알고리즘·파일 protocol 식별자를 보존하는지 확인합니다."""

    from tools.align.alignment.icp_backend import build_manifest
    from tools.align.alignment.icp_solver_backend import (
        IcpSolverBackend,
        backend_algorithm_version,
    )
    from tools.align.constants import ICP_ALGORITHM_VERSION, ICP_JOB_PROTOCOL_VERSION

    backend = IcpSolverBackend.OPEN3D_GICP
    manifest = build_manifest(
        job_id="open3d-manifest-job",
        input_fingerprint="open3d-manifest-input",
        payload={
            "algorithm_version": ICP_ALGORITHM_VERSION,
            "backend_id": backend.value,
            "backend_algorithm_version": backend_algorithm_version(backend),
            "job_protocol_version": ICP_JOB_PROTOCOL_VERSION,
        },
    )
    assert manifest["backend_id"] == backend.value
    assert manifest["backend_algorithm_version"] == backend_algorithm_version(backend)
    assert manifest["job_protocol_version"] == ICP_JOB_PROTOCOL_VERSION


def test_open3d_local_delta_progress_uses_project_world_alignment() -> None:
    """local origin delta를 Project World로 되돌린 뒤 marker와 합성하는지 확인합니다."""

    from tools.align.alignment import icp_open3d_solver as solver

    angle = float(np.deg2rad(8.0))
    delta_local = np.identity(4, dtype=np.float64)
    delta_local[:3, :3] = np.asarray(
        (
            (np.cos(angle), -np.sin(angle), 0.0),
            (np.sin(angle), np.cos(angle), 0.0),
            (0.0, 0.0, 1.0),
        ),
        dtype=np.float64,
    )
    delta_local[:3, 3] = np.asarray((0.3, -0.2, 0.1), dtype=np.float64)
    local_origin = np.asarray((1200.0, -850.0, 45.0), dtype=np.float64)
    delta_world = solver._local_delta_to_world(delta_local, local_origin)
    source_point = np.asarray(((1201.0, -849.0, 46.0),), dtype=np.float64)
    local_result = solver._transform_positions(source_point - local_origin, delta_local) + local_origin
    world_result = solver._transform_positions(source_point, delta_world)
    np.testing.assert_allclose(local_result, world_result, rtol=0.0, atol=1.0e-10)

    marker_initial = np.identity(4, dtype=np.float64)
    marker_initial[:3, :3] *= 1.25
    marker_initial[:3, 3] = np.asarray((4.0, -3.0, 2.0), dtype=np.float64)
    payloads: list[dict[str, object]] = []
    job = SimpleNamespace(
        job_id="direction-job",
        input_fingerprint="direction-input",
        algorithm_version="hybrid-point-to-plane-icp-v2",
        backend_id="OPEN3D_GICP",
        backend_algorithm_version="open3d-gicp-v1",
        job_protocol_version="icp-job-protocol-v1",
        initial_alignment=marker_initial,
    )
    solver._emit_progress(
        payloads.append,
        job,
        open3d_version="0.19.0",
        stage="OPEN3D_GICP_FINE_DISTANCE",
        progress=0.8,
        message="방향 검증",
        stable_delta=delta_world,
    )
    assert len(payloads) == 1
    np.testing.assert_allclose(
        np.asarray(payloads[0]["stable_alignment_4x4"], dtype=np.float64),
        delta_world @ marker_initial,
        rtol=0.0,
        atol=1.0e-10,
    )


def test_open3d_result_requires_matching_backend_and_version_identity() -> None:
    """Open3D 결과의 backend·버전·환경 식별자 누락과 불일치를 거부하는지 확인합니다."""

    from tools.align.alignment.icp_backend import validate_result_metadata_for_backend
    from tools.align.alignment.icp_solver_backend import IcpSolverBackend
    from tools.align.constants import ICP_JOB_PROTOCOL_VERSION

    backend = IcpSolverBackend.OPEN3D_GICP
    metadata = _open3d_result_metadata(backend.value)
    validate_result_metadata_for_backend(metadata, backend.value)

    required_fields = (
        "backend_id",
        "backend_algorithm_version",
        "job_protocol_version",
        "worker_python_version",
        "worker_numpy_version",
        "open3d_version",
        "platform_architecture",
        "parameter_fingerprint",
    )
    for field_name in required_fields:
        malformed = dict(metadata)
        malformed.pop(field_name)
        _expect_protocol_error(
            lambda malformed=malformed: validate_result_metadata_for_backend(
                malformed,
                backend.value,
            ),
            f"Open3D 결과 필수 필드 {field_name}",
        )

    wrong_backend = dict(metadata)
    wrong_backend["backend_id"] = IcpSolverBackend.CUSTOM_HYBRID.value
    _expect_protocol_error(
        lambda: validate_result_metadata_for_backend(wrong_backend, backend.value),
        "다른 backend가 기록한 Open3D 결과",
    )

    wrong_protocol = dict(metadata)
    wrong_protocol["job_protocol_version"] = ICP_JOB_PROTOCOL_VERSION + "-wrong"
    _expect_protocol_error(
        lambda: validate_result_metadata_for_backend(wrong_protocol, backend.value),
        "다른 job protocol 버전",
    )

    wrong_review_flag = _open3d_result_metadata(backend.value)
    wrong_review_flag["quality"] = {"status": "PASS"}
    wrong_review_flag["user_review_required"] = True
    _expect_protocol_error(
        lambda: validate_result_metadata_for_backend(wrong_review_flag, backend.value),
        "PASS 결과의 잘못된 사용자 검토 플래그",
    )


def test_open3d_full_input_audit_rejects_downsampling_or_stage_changes() -> None:
    """전체 희소점 audit에서 다운샘플링과 단계별 입력 변경을 거부하는지 확인합니다."""

    from tools.align.alignment.icp_backend import validate_result_metadata_for_backend
    from tools.align.alignment.icp_solver_backend import IcpSolverBackend

    backend = IcpSolverBackend.OPEN3D_ROBUST_POINT_TO_PLANE
    metadata = _open3d_result_metadata(backend.value)
    validate_result_metadata_for_backend(metadata, backend.value)

    changed_stage = _open3d_result_metadata(backend.value)
    changed_stage["full_input_audit"]["stage_input_fingerprints"]["fine"][
        "source_point_ids_fingerprint"
    ] = "different-source-point-ids"
    _expect_protocol_error(
        lambda: validate_result_metadata_for_backend(changed_stage, backend.value),
        "fine 단계에서 바뀐 source POINT3D_ID 집합",
    )

    changed_count = _open3d_result_metadata(backend.value)
    changed_count["full_input_audit"]["source_crop_count"] = 8
    _expect_protocol_error(
        lambda: validate_result_metadata_for_backend(changed_count, backend.value),
        "원본 점 수를 넘는 overlap source 점 수",
    )

    downsampled = _open3d_result_metadata(backend.value)
    downsampled["full_input_audit"]["downsampling"]["used"] = True
    _expect_protocol_error(
        lambda: validate_result_metadata_for_backend(downsampled, backend.value),
        "Open3D 입력 다운샘플링",
    )


def run_all() -> None:
    """Open3D가 없는 기본 Blender 환경용 protocol 테스트를 순서대로 실행합니다."""

    _add_source_path()
    test_open3d_solver_module_import_is_lazy()
    test_open3d_worker_retries_atomic_json_and_npz_replace()
    test_open3d_worker_preserves_target_after_persistent_replace_denial()
    test_open3d_manifest_keeps_explicit_backend_protocol_identity()
    test_open3d_local_delta_progress_uses_project_world_alignment()
    test_open3d_result_requires_matching_backend_and_version_identity()
    test_open3d_full_input_audit_rejects_downsampling_or_stage_changes()
    print("test_icp_open3d_protocol: 통과")


if __name__ == "__main__":
    run_all()
