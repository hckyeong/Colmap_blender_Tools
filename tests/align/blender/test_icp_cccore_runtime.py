"""실제 CC 작업의 미리보기·승인·취소·출처 저장과 이전 초기값을 검증합니다."""

from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path
import sys
from time import monotonic, sleep
from unittest.mock import patch

import numpy as np


def _paths() -> None:
    """소스와 실제 정렬 자료 생성 함수를 Blender 모듈 경로에 추가합니다."""
    folder = Path(__file__).resolve().parent
    for path in (folder.parents[2], folder):
        if str(path) not in sys.path:
            sys.path.insert(0, str(path))


def _python() -> str:
    """현재 작업트리에서 준비한 전용 CC Python 실행 파일을 선택합니다."""
    from tools.align.properties import bundled_cccore_python_path

    return bundled_cccore_python_path()


def _begin(runtime):
    """UI와 같은 공개 시작 경계에서 실제 CC 작업을 실행합니다."""
    from tools.align.alignment.icp_runtime import begin_icp_job
    return begin_icp_job(runtime, preset="PRECISE", scale_refine_enabled=True,
                         solver_backend="CCCORELIB", open3d_python_path=_python())


def _finish(runtime, timeout=120):
    """실제 파일 진행률과 작업 종료를 기존 공개 완료 경계까지 처리합니다."""
    from tools.align.alignment.icp_runtime import poll_icp_job
    deadline = monotonic() + timeout
    while runtime.icp_alignment.is_running:
        poll_icp_job(runtime)
        if monotonic() > deadline:
            from tools.align.alignment.icp_runtime import invalidate_icp
            invalidate_icp(runtime, "검증 시간 제한")
            raise AssertionError("CC 작업 시간 제한을 넘었습니다.")
        sleep(0.02)
    assert runtime.icp_alignment.has_preview, runtime.icp_alignment.last_error
    return runtime.icp_alignment.result


def _direct_runtime():
    """근접한 실제 점군 A/P와 소유 카탈로그를 구성합니다."""
    from test_icp_runtime import _model
    from tools.align.display_axes import identity_display_transform
    from tools.align.state import RuntimeState
    points = np.random.default_rng(763).normal(size=(96, 3))
    angle = np.deg2rad(3.0)
    rotation = np.asarray(((np.cos(angle), -np.sin(angle), 0),
                           (np.sin(angle), np.cos(angle), 0), (0, 0, 1)))
    expected = np.eye(4)
    expected[:3, :3] = 1.035 * rotation
    expected[:3, 3] = (0.04, -0.03, 0.02)
    reference = points @ expected[:3, :3].T + expected[:3, 3]
    runtime = RuntimeState()
    runtime.session_active = True
    for model_id, positions in (("reference", reference), ("moving", points)):
        runtime.model_repository.add_model(_model(model_id, positions, 7), identity_display_transform())
        runtime.model_catalog.add_imported_model(model_id, model_id, select_after_import=model_id == "moving")
    runtime.model_catalog.toggle_alignment_selection("reference", has_marker_draft=False, completed_pair_count=0)
    return runtime, expected


def test_real_direct_cccore_approve_undo_redo() -> None:
    """실제 점군 정렬을 승인하고 행렬·그룹·CC 출처를 함께 되돌립니다."""
    from tools.align.alignment.icp_runtime import approve_icp_result, restore_icp_commit_snapshot
    from tools.align.project_persistence.codec import _decode_provenance
    runtime, expected = _direct_runtime()
    before = runtime.model_repository.alignment_transform_for_model_id("moving").matrix_float64.copy()
    source_before = runtime.model_repository.models["moving"].points.positions.copy()
    handle = _begin(runtime)
    result = _finish(runtime)
    assert not handle.files.job_directory.exists()
    assert result.solver_backend == "CCCORELIB" and result.metadata["geometry_mode"] == "POINT_TO_POINT"
    assert result.algorithm_version == "cccore-icp-v3"
    requested_threads = min(1024, max(1, (os.cpu_count() or 1) - 1))
    assert result.metadata["parameters"]["max_threads"] == requested_threads
    assert result.metadata["requested_max_threads"] == requested_threads
    assert result.metadata["thread_backend"] == "oneTBB"
    assert result.metadata["tbb_version"] == "2022.0.0"
    assert result.metadata["distance_multithread_support"] is True
    assert result.metadata["octree_multithread_support"] is True
    assert result.metadata["effective_max_threads"] == min(
        requested_threads,
        result.metadata["hardware_thread_count"],
    )
    assert result.metadata["native_process_count"] == 1
    assert result.metadata["native_calls"] == result.metadata["logical_operations"]
    assert result.metadata["physical_native_command_count"] == len(result.metadata["physical_native_calls"])
    assert result.metadata["physical_native_command_count"] == 6
    assert result.metadata["exact_evaluation_cache"]["hit_count"] == 0
    assert tuple(call["operation"] for call in result.metadata["logical_operations"]) == (
        "distance", "distance", "register", "distance", "register", "distance",
    )
    assert tuple(call["stage"] for call in result.metadata["logical_operations"]) == (
        "PREPARING", "BASELINE", "RIGID", "RIGID_EVALUATION", "SCALE_REFINE", "REFINE_EVALUATION",
    )
    assert result.metadata["session_exit_reason"] == "closed"
    assert result.metadata["session_statistics"]["geometry_load_count"] == len(result.metadata["geometry_statistics"])
    assert result.metadata["session_statistics"]["geometry_cache_hits"] > 0
    for index, call in enumerate(result.metadata["logical_operations"], start=1):
        assert set(call) == {
            "operation", "index", "sequence", "physical_sequence", "source_sequence",
            "source_logical_index", "stage", "execution_source", "evaluation_key_sha256", "elapsed_seconds",
            "roundtrip_seconds", "protocol_overhead_seconds",
            "requested_max_threads", "effective_max_threads", "data_geometry_id", "model_geometry_id",
            "timings", "geometry_load_count", "geometry_cache_hits", "octree_build_count",
            "octree_cache_hits", "mesh_grid_build_count", "mesh_grid_cache_hits",
        }
        assert call["index"] == index and call["sequence"] == index
        assert call["execution_source"] == "native_session"
        assert call["physical_sequence"] == index and call["source_sequence"] == index
        assert call["requested_max_threads"] == requested_threads
        assert call["effective_max_threads"] == result.metadata["effective_max_threads"]
    np.testing.assert_allclose(result.final_alignment.matrix_float64, expected, atol=2e-5, rtol=0)
    np.testing.assert_array_equal(runtime.model_repository.alignment_transform_for_model_id("moving").matrix_float64, before)
    np.testing.assert_array_equal(runtime.model_repository.models["moving"].points.positions, source_before)
    commit = approve_icp_result(runtime)
    provenance = runtime.model_catalog.models["moving"].alignment_provenance
    assert provenance.backend_id == "CCCORELIB"
    assert provenance.solver_geometry_dependencies == (("moving", "MAIN"), ("reference", "MAIN"))
    audit = json.loads(provenance.engine_metadata_json)
    from tools.align.alignment.icp_cccore_runtime import CCCORE_PROVENANCE_METADATA_FIELDS
    assert set(audit) == set(CCCORE_PROVENANCE_METADATA_FIELDS)
    assert audit["geometry_input_fingerprint"] == result.metadata["geometry_input_fingerprint"]
    assert audit["core_commit"] and len(audit["native_worker_sha256"]) == 64
    for field in ("native_build_identity", "geometry_provenance", "baseline_metrics",
                  "best_candidate_metrics", "relative_improvement", "review_reasons",
                  "thread_backend", "tbb_version", "requested_max_threads",
                  "effective_max_threads", "hardware_thread_count",
                  "distance_multithread_support", "octree_multithread_support", "native_process_count",
                  "logical_operations", "native_calls", "physical_native_calls",
                  "physical_native_command_count", "exact_evaluation_cache",
                  "process_startup_seconds", "session_elapsed_seconds",
                  "session_exit_reason", "session_statistics", "geometry_statistics",
                  "session_protocol_statistics", "physical_roundtrip_seconds",
                  "protocol_overhead_seconds", "protocol_overhead_ratio_of_roundtrip",
                  "protocol_overhead_fraction_of_total_alignment"):
        assert audit[field] == result.metadata[field], field
    decoded = _decode_provenance(provenance.to_mapping(), "moving", 7)
    assert decoded == provenance
    assert runtime.undo_journal.undo_committed(lambda snapshot, kind: restore_icp_commit_snapshot(runtime, snapshot))
    np.testing.assert_array_equal(runtime.model_repository.alignment_transform_for_model_id("moving").matrix_float64, before)
    assert runtime.model_catalog.models["moving"].registration_status == "PENDING"
    assert runtime.undo_journal.redo_committed(lambda snapshot, kind: restore_icp_commit_snapshot(runtime, snapshot))
    np.testing.assert_allclose(runtime.model_repository.alignment_transform_for_model_id("moving").matrix_float64,
                               commit.after.alignment.matrix_float64, atol=1e-12)


def test_marker_initial_transform_is_applied_once() -> None:
    """기존 3점 초기 변환을 두 번 적용하지 않고 버리기로 마커 미리보기를 복원합니다."""
    from test_icp_runtime import _runtime_with_marker_preview
    from tools.align.alignment.icp_runtime import discard_icp_preview
    runtime, marker = _runtime_with_marker_preview(count=64)
    expected = marker.preview_alignment.matrix_float64.copy()
    _begin(runtime)
    result = _finish(runtime)
    np.testing.assert_allclose(result.final_alignment.matrix_float64, expected, atol=2e-5, rtol=0)
    discard_icp_preview(runtime)
    assert runtime.marker_alignment.result is marker
    np.testing.assert_array_equal(runtime.marker_alignment.result.preview_alignment.matrix_float64, expected)


def test_mesh_input_and_stale_axis_rejection() -> None:
    """실제 연결 메시 작업과 축 변경 후 승인 차단을 확인합니다."""
    from test_icp_cccore_geometry import _runtime, _surface
    from tools.align.alignment.icp_runtime import approve_icp_result, IcpRuntimeError
    from tools.align.display_axes import create_model_display_transform
    runtime = _runtime()
    runtime.session_active = True
    for model_id in ("기준", "이동"):
        _surface(runtime, model_id)
    runtime.model_catalog.toggle_alignment_selection("기준", has_marker_draft=False, completed_pair_count=0)
    before = runtime.model_repository.alignment_transform_for_model_id("이동").matrix_float64.copy()
    _begin(runtime)
    result = _finish(runtime)
    assert result.metadata["geometry_mode"] == "MESH_TO_MESH"
    assert runtime.icp_alignment.input_data.snapshot.geometry_dependencies == (("기준", "COMPANION"), ("이동", "COMPANION"))
    runtime.model_repository.set_companion_display_transform(
        "기준", create_model_display_transform("REALITY_CAPTURE_OBJ", "Y", "Z",
                                                rotation_xyz_degrees=(10, 0, 0)))
    try:
        approve_icp_result(runtime)
    except IcpRuntimeError:
        pass
    else:
        raise AssertionError("정밀 정렬에 사용한 메시 축 변경 후 승인이 차단되어야 합니다.")
    np.testing.assert_array_equal(runtime.model_repository.alignment_transform_for_model_id("이동").matrix_float64, before)


def test_progress_avoids_rehash_until_completion() -> None:
    """진행 확인은 원본 전체를 읽지 않고 완료와 승인 때 내용 지문을 검증합니다."""
    from tools.align.alignment import icp_geometry
    from tools.align.alignment.icp_runtime import poll_icp_job, approve_icp_result
    runtime, _expected = _direct_runtime()
    handle = _begin(runtime)
    with patch.object(icp_geometry, "_array_fingerprint", wraps=icp_geometry._array_fingerprint) as hash_array:
        with patch.object(handle.process, "poll", return_value=None):
            poll_icp_job(runtime)
            assert hash_array.call_count == 0, "진행 확인 중 전체 형상을 다시 읽었습니다."
        _finish(runtime)
        assert hash_array.call_count > 0, "완료 시 원본 내용 검증이 생략되었습니다."
        hash_array.reset_mock()
        approve_icp_result(runtime)
        assert hash_array.call_count > 0, "승인 시 원본 내용 검증이 생략되었습니다."


def test_corrupted_native_session_aggregate_is_rejected() -> None:
    """완료 파일의 상주 캐시 합계를 변조하면 공통 runtime 복원 경계에서 거부합니다."""
    from tools.align.alignment.icp_backend import IcpJobProtocolError
    from tools.align.alignment.icp_cccore_runtime import read_cccore_result, result_from_cccore
    from tools.align.alignment.icp_runtime import invalidate_icp

    runtime, _expected = _direct_runtime()
    handle = _begin(runtime)
    try:
        handle.process.wait(timeout=120)
        loaded = read_cccore_result(handle)
        assert loaded is not None
        metadata, arrays = loaded
        metadata["session_statistics"]["cache_resident_estimated_bytes"] += 1
        try:
            result_from_cccore(runtime, runtime.icp_alignment, metadata, arrays)
        except IcpJobProtocolError:
            pass
        else:
            raise AssertionError("손상된 세션 상주 캐시 합계를 runtime이 수용했습니다.")
        metadata["session_statistics"]["cache_resident_estimated_bytes"] -= 1
        metadata["protocol_overhead_seconds"] += 0.001
        try:
            result_from_cccore(runtime, runtime.icp_alignment, metadata, arrays)
        except IcpJobProtocolError:
            pass
        else:
            raise AssertionError("손상된 최상위 프로토콜 합계를 runtime이 수용했습니다.")
    finally:
        invalidate_icp(runtime, "손상 결과 회귀 검사 종료")


def test_actual_job_cancel_preserves_model() -> None:
    """실제 작업 취소가 원본·공통 행렬을 보존하고 프로세스를 회수하는지 확인합니다."""
    from tools.align.alignment.icp_runtime import request_icp_cancel, poll_icp_job
    runtime, _expected = _direct_runtime()
    original = runtime.model_repository.alignment_transform_for_model_id("moving").matrix_float64.copy()
    handle = _begin(runtime)
    assert request_icp_cancel(runtime)
    deadline = monotonic() + 20
    while runtime.icp_alignment.is_running:
        poll_icp_job(runtime)
        assert monotonic() < deadline, "취소한 프로세스가 종료되지 않았습니다."
        sleep(0.02)
    assert not handle.is_running
    assert not runtime.icp_alignment.has_preview
    np.testing.assert_array_equal(runtime.model_repository.alignment_transform_for_model_id("moving").matrix_float64, original)


def test_legacy_provenance_without_cc_fields_roundtrip() -> None:
    """스키마 8에서 이전 Small GICP와 CCCoreLib v1·v2 감사 기록을 그대로 복원합니다."""
    from tools.align.alignment.types import AlignmentProvenanceRecord
    from tools.align.project_persistence.codec import _decode_provenance
    original = AlignmentProvenanceRecord(model_id="old", method="ICP", backend_id="SMALL_GICP",
                                         algorithm_version="small-gicp-v6", icp_status="PASS", icp_rmse=0.25)
    decoded = _decode_provenance(original.to_mapping(), "old", 6)
    assert decoded == original and not decoded.engine_metadata_json and not decoded.solver_geometry_dependencies
    for version in ("cccore-icp-v1", "cccore-icp-v2"):
        legacy_cc = AlignmentProvenanceRecord(
            model_id="cc-old",
            method="ICP",
            backend_id="CCCORELIB",
            algorithm_version=version,
            input_fingerprint="legacy-input",
            result_fingerprint="legacy-result",
            icp_status="PASS",
            icp_rmse=0.125,
            solver_geometry_dependencies=(("cc-old", "MAIN"), ("reference", "COMPANION")),
            engine_metadata_json=json.dumps({"core_commit": "a" * 40, "quality": "PASS"}),
        )
        restored_cc = _decode_provenance(legacy_cc.to_mapping(), "cc-old", 8)
        assert restored_cc == legacy_cc
        assert restored_cc.algorithm_version == version


def main() -> None:
    """실제 네이티브 런타임이 연결된 Blender 정렬 회귀를 실행합니다."""
    _paths()
    tests = tuple((name, function) for name, function in globals().items()
                  if name.startswith("test_") and callable(function))
    for name, function in tests:
        function()
        print(f"통과: {name}", flush=True)
    print(f"CC 런타임 실제 정렬·승인 검사 {len(tests)}개 통과", flush=True)


if __name__ == "__main__":
    main()
