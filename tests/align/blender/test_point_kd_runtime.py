"""새 강체 엔진의 미리보기·승인·출처 저장·취소·입력 변조 경계를 검증합니다."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
import sys
from time import monotonic, sleep
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np


def _paths() -> None:
    """소스와 기존 정렬 자료 생성 함수를 Blender 탐색 경로에 연결합니다."""
    folder = Path(__file__).resolve().parent
    for path in (folder.parents[2], folder):
        if str(path) not in sys.path:
            sys.path.insert(0, str(path))


def _runtime(*, reverse: bool = False, initial_scale: float = 1.0, additional_scale: float = 1.0):
    """기존 배율과 알려진 추가 배율·회전을 갖는 실제 A/P 저장소를 만듭니다."""
    from test_icp_runtime import _model
    from tools.align.alignment.types import Sim3Transform
    from tools.align.display_axes import identity_display_transform
    from tools.align.state import RuntimeState
    points = np.random.default_rng(9473).normal(size=(4000, 3))
    initial = np.diag((initial_scale, initial_scale, initial_scale, 1.0))
    angle = np.deg2rad(.7)
    delta = np.eye(4)
    delta[:3, :3] = ((np.cos(angle), -np.sin(angle), 0), (np.sin(angle), np.cos(angle), 0), (0, 0, 1))
    delta[:3, :3] *= additional_scale
    delta[:3, 3] = (.006, -.004, .005)
    expected = delta @ initial
    reference = points @ expected[:3, :3].T + expected[:3, 3]
    runtime = RuntimeState()
    runtime.session_active = True
    for model_id, positions in (("reference", reference[:3000] if reverse else reference), ("moving", points)):
        runtime.model_repository.add_model(_model(model_id, positions, 7), identity_display_transform())
        runtime.model_catalog.add_imported_model(model_id, model_id, select_after_import=model_id == "moving")
    runtime.model_repository.set_alignment_transform("moving", Sim3Transform(matrix_float64=initial))
    runtime.model_catalog.toggle_alignment_selection("reference", has_marker_draft=False, completed_pair_count=0)
    return runtime, expected


def _begin(runtime, *, sim3: bool = False):
    """실제 공개 시작 경계에 강체 또는 Sim(3) 선택을 전달합니다."""
    from tools.align.alignment.icp_runtime import begin_icp_job
    from tools.align.properties import bundled_point_kd_python_path
    with patch("tools.align.properties.configured_cc_cpu_thread_limit", return_value=2):
        return begin_icp_job(runtime, solver_backend="POINT_KD_ICP", scale_refine_enabled=sim3,
                             open3d_python_path=bundled_point_kd_python_path())


def _finish(runtime):
    """실제 외부 작업의 진행률을 검증하면서 완료 미리보기까지 처리합니다."""
    from tools.align.alignment.icp_runtime import invalidate_icp, poll_icp_job
    deadline = monotonic() + 60
    while runtime.icp_alignment.is_running:
        poll_icp_job(runtime)
        if monotonic() > deadline:
            invalidate_icp(runtime, "검증 시간 제한")
            raise AssertionError("점 기반 작업의 검증 시간 제한을 넘었습니다.")
        sleep(.02)
    assert runtime.icp_alignment.has_preview, runtime.icp_alignment.last_error
    return runtime.icp_alignment.result


def test_actual_preview_approve_undo_and_persistence() -> None:
    """양방향 실제 정렬의 원본 입력 재사용과 배율·승인 이력·최종 해제를 검사합니다."""
    from tools.align.alignment.icp_runtime import approve_icp_result, restore_icp_commit_snapshot
    from tools.align.project_persistence.codec import _decode_provenance
    for reverse in (False, True):
        runtime, expected = _runtime(reverse=reverse, initial_scale=2.5)
        before = runtime.model_repository.alignment_transform_for_model_id("moving")
        reference_before = runtime.model_repository.alignment_transform_for_model_id("reference")
        source = runtime.model_repository.models["moving"].points.positions.copy()
        handle = _begin(runtime)
        manifest = json.loads(handle.files.manifest_path.read_text(encoding='utf-8'))
        assert manifest['input_transport'] == 'WIN32_SHARED_LOCAL_V1'
        names = _mapping_names(manifest)
        assert not list(handle.files.job_directory.glob('*.bin'))
        result = _finish(runtime)
        _assert_mappings_available(names)
        assert result.metadata['input_transport'] == 'WIN32_SHARED_LOCAL_V1'
        assert not handle.files.job_directory.exists()
        assert result.solver_backend == "POINT_KD_ICP" and result.algorithm_version == "point-kd-icp-v3"
        assert result.metadata["reverse_registration"] is reverse
        assert result.metadata["independent_evaluation_performed"] is False
        assert np.isclose(result.icp_delta.uniform_scale, 1.0, rtol=0, atol=1e-12)
        np.testing.assert_allclose(result.final_alignment.matrix_float64, expected, atol=2e-6, rtol=0)
        np.testing.assert_array_equal(runtime.model_repository.models["moving"].points.positions, source)
        assert runtime.model_repository.alignment_transform_for_model_id("moving").fingerprint == before.fingerprint
        commit = approve_icp_result(runtime)
        provenance = runtime.model_catalog.models["moving"].alignment_provenance
        assert provenance.backend_id == "POINT_KD_ICP" and provenance.algorithm_version == "point-kd-icp-v3"
        assert provenance.icp_fitness is None
        assert provenance.solver_geometry_dependencies == (("moving", "MAIN"), ("reference", "MAIN"))
        assert _decode_provenance(provenance.to_mapping(), "moving", 8) == provenance
        assert json.loads(provenance.engine_metadata_json)["independent_evaluation_performed"] is False
        restore_icp_commit_snapshot(runtime, commit.before)
        assert runtime.model_repository.alignment_transform_for_model_id("moving").fingerprint == before.fingerprint
        restore_icp_commit_snapshot(runtime, commit.after)
        np.testing.assert_allclose(runtime.model_repository.alignment_transform_for_model_id("moving").matrix_float64, expected, atol=2e-6, rtol=0)
        assert runtime.model_repository.alignment_transform_for_model_id("reference").fingerprint == reference_before.fingerprint
        runtime.model_repository.clear()
        _assert_mappings_closed(names)


def test_marker_scale_discard_and_cancel() -> None:
    """마커 배율·확정 변환을 보존하고 취소 뒤에도 원본 입력은 다음 작업에 유지합니다."""
    from test_icp_runtime import _runtime_with_marker_preview
    from tools.align.alignment.icp_runtime import discard_icp_preview, invalidate_icp, poll_icp_job, request_icp_cancel
    runtime, marker = _runtime_with_marker_preview(count=60000)
    runtime.model_catalog.toggle_alignment_selection("reference", has_marker_draft=False, completed_pair_count=3)
    committed = runtime.model_repository.alignment_transform_for_model_id("moving")
    _begin(runtime)
    result = _finish(runtime)
    assert np.isclose(result.final_alignment.uniform_scale, marker.preview_alignment.uniform_scale)
    discard_icp_preview(runtime)
    assert runtime.marker_alignment.is_active
    assert runtime.model_repository.alignment_transform_for_model_id("moving").fingerprint == committed.fingerprint
    handle = _begin(runtime)
    manifest = json.loads(handle.files.manifest_path.read_text(encoding='utf-8'))
    names = _mapping_names(manifest)
    assert request_icp_cancel(runtime)
    deadline = monotonic() + 15
    while runtime.icp_alignment.is_running:
        poll_icp_job(runtime)
        assert monotonic() < deadline
        sleep(.02)
    assert not handle.is_running and not handle.files.job_directory.exists()
    _assert_mappings_available(names)
    assert runtime.marker_alignment.is_active
    assert runtime.model_repository.alignment_transform_for_model_id("moving").fingerprint == committed.fingerprint
    invalidate_icp(runtime, "검사 종료")
    runtime.model_repository.clear()
    _assert_mappings_closed(names)


def test_sim3_preview_discard_approve_undo_and_persistence() -> None:
    """양방향 Sim(3)의 원본 보존·미리보기·버리기·승인·복원·배율 저장을 검증합니다."""
    from tools.align.alignment.icp_runtime import approve_icp_result, restore_icp_commit_snapshot, discard_icp_preview
    from tools.align.project_persistence.codec import _decode_provenance
    from tools.align.ui.panels import _point_kd_result_entries
    for reverse in (False, True):
        runtime, expected = _runtime(reverse=reverse, initial_scale=2.5, additional_scale=.985)
        before = runtime.model_repository.alignment_transform_for_model_id('moving')
        original = runtime.model_repository.models['moving'].points.positions.copy()
        _begin(runtime, sim3=True)
        result = _finish(runtime)
        assert result.metadata['scale_mode'] == 'SIM3' and result.quality['status'] == 'PASS'
        assert abs(result.icp_delta.uniform_scale - .985) < 1e-7
        np.testing.assert_allclose(result.final_alignment.matrix_float64, expected, atol=2e-6, rtol=0)
        assert runtime.model_repository.alignment_transform_for_model_id('moving').fingerprint == before.fingerprint
        assert any('Sim(3)' in item for item in _point_kd_result_entries(result, runtime.icp_alignment))
        discard_icp_preview(runtime)
        assert runtime.model_repository.alignment_transform_for_model_id('moving').fingerprint == before.fingerprint
        _begin(runtime, sim3=True)
        _finish(runtime)
        commit = approve_icp_result(runtime)
        provenance = runtime.model_catalog.models['moving'].alignment_provenance
        assert _decode_provenance(provenance.to_mapping(), 'moving', 8) == provenance
        assert json.loads(provenance.engine_metadata_json)['scale_mode'] == 'SIM3'
        restore_icp_commit_snapshot(runtime, commit.before)
        assert runtime.model_repository.alignment_transform_for_model_id('moving').fingerprint == before.fingerprint
        restore_icp_commit_snapshot(runtime, commit.after)
        np.testing.assert_allclose(runtime.model_repository.alignment_transform_for_model_id('moving').matrix_float64, expected, atol=2e-6, rtol=0)
        np.testing.assert_array_equal(runtime.model_repository.models['moving'].points.positions, original)
        runtime.model_repository.clear()


def test_same_revision_geometry_change_rejects_approval() -> None:
    """미리보기 뒤 개정 번호가 같아도 불변 원본 교체를 감지해 승인을 거부합니다."""
    from tools.align.alignment.icp_runtime import approve_icp_result, IcpRuntimeStaleError
    runtime, _ = _runtime()
    _begin(runtime)
    _finish(runtime)
    model = runtime.model_repository.models["moving"]
    changed = model.points.positions.copy()
    changed[7, 0] += .01
    runtime.model_repository.models["moving"] = replace(model, points=replace(model.points, positions=changed))
    try:
        approve_icp_result(runtime)
    except IcpRuntimeStaleError:
        pass
    else:
        raise AssertionError("같은 revision의 원본 내용 변경을 승인했습니다.")


def test_sim3_scale_metadata_roundoff_and_corruption() -> None:
    """서로 다른 배열 런타임의 배율 반올림은 허용하고 실제 배율 변조는 거부합니다."""
    from tools.align.alignment.icp_runtime import finish_icp_job, discard_icp_preview, invalidate_icp, IcpRuntimeError
    from tools.align.alignment.icp_backend import read_json_file, write_json_atomically
    for difference, accepted in ((1e-14, True), (.01, False)):
        runtime, _ = _runtime(initial_scale=2.5, additional_scale=.99)
        handle = _begin(runtime, sim3=True)
        assert handle.process.wait(timeout=30) == 0
        payload = read_json_file(handle.files.result_path)
        payload['additional_scale'] += difference
        write_json_atomically(handle.files.result_path, payload)
        if accepted:
            finish_icp_job(runtime)
            assert runtime.icp_alignment.has_preview
            discard_icp_preview(runtime)
        else:
            try:
                finish_icp_job(runtime)
            except IcpRuntimeError:
                pass
            else:
                raise AssertionError('변조된 추가 배율을 승인했습니다.')
            assert not runtime.icp_alignment.has_preview
        invalidate_icp(runtime, '배율 결과 검증 종료')
        runtime.model_repository.clear()


def test_malformed_worker_results_never_preview() -> None:
    """평가 의미·엔진·강체성·전체점 수를 변조한 결과를 모두 실패로 처리합니다."""
    from tools.align.alignment.icp_runtime import finish_icp_job, invalidate_icp, IcpRuntimeError
    from tools.align.alignment.icp_backend import read_json_file, write_json_atomically
    for corruption in ("independent", "independent_type", "identity", "scale", "count", "native_type", "terminal", "terminal_quality"):
        runtime, _ = _runtime()
        before = runtime.model_repository.alignment_transform_for_model_id("moving")
        handle = _begin(runtime)
        assert handle.process.wait(timeout=30) == 0
        path = handle.files.progress_path if corruption.startswith("terminal") else handle.files.result_path
        payload = read_json_file(path)
        if corruption == "independent":
            payload["independent_evaluation_performed"] = True
        elif corruption == "independent_type":
            payload["independent_evaluation_performed"] = 0
        elif corruption == "identity":
            payload["backend_id"] = "CCCORELIB"
        elif corruption == "scale":
            payload["native_result"]["delta_4x4"][0][0] *= 1.01
        elif corruption == "count":
            payload["native_result"]["source_count"] -= 1
        elif corruption == "native_type":
            payload["native_result"] = []
        elif corruption == "terminal_quality":
            payload["quality"] = "REVIEW" if payload["quality"] == "PASS" else "PASS"
        else:
            payload["stage"] = "RIGID"
        write_json_atomically(path, payload)
        try:
            finish_icp_job(runtime)
        except IcpRuntimeError:
            pass
        else:
            raise AssertionError(f"변조한 점 기반 결과를 수락했습니다: {corruption}")
        assert not runtime.icp_alignment.has_preview
        assert runtime.model_repository.alignment_transform_for_model_id("moving").fingerprint == before.fingerprint
        invalidate_icp(runtime, "검사 종료")
        assert not handle.files.job_directory.exists()


def test_default_profiles_and_ui_quality_meaning() -> None:
    """새 기본 프로필과 명시적 구엔진을 구분하고 정렬 대응 RMS를 독립 평가로 표시하지 않습니다."""
    from tools.align import distribution_profile
    from tools.align.operators import marker_alignment
    from tools.align.ui import panels
    for profile, expected in (("POINT_KD", "POINT_KD_ICP"), ("FULL", "POINT_KD_ICP"),
                              ("CCCORELIB", "CCCORELIB"), ("SMALL_GICP", "SMALL_GICP")):
        with patch.object(distribution_profile, "DISTRIBUTION_PROFILE", profile):
            assert distribution_profile.default_icp_backend() == expected
            assert distribution_profile.icp_backend_enum_items()[0][0] == expected
    runtime, _ = _runtime()
    _begin(runtime)
    result = _finish(runtime)
    entries = panels._point_kd_result_entries(result, runtime.icp_alignment)
    assert any("정렬 대응 RMS" in item for item in entries)
    assert "독립 품질 평가 · 실행하지 않음" in entries
    assert not any("겹침 평가" in item or "CC 정밀" in item for item in entries)
    with patch.object(marker_alignment, "RUNTIME", runtime), patch.object(marker_alignment, "_AUTOMATIC_ICP_BACKEND", "POINT_KD_ICP"):
        assert "배율을 보존" in marker_alignment.automatic_alignment_description()


def test_public_align_dispatch_and_environment() -> None:
    """실제 Align 오퍼레이터의 기본 강체·명시적 Sim(3) 전달과 환경 검사를 확인합니다."""
    from tools.align.operators import marker_alignment
    from tools.align.properties import bundled_point_kd_python_path
    runtime, _ = _runtime()
    python = bundled_point_kd_python_path()
    with patch.object(marker_alignment, "RUNTIME", runtime), \
         patch.object(marker_alignment, "_AUTOMATIC_ICP_BACKEND", "POINT_KD_ICP"), \
         patch.object(marker_alignment, "restore_input_block_reason", return_value=None), \
         patch.object(marker_alignment, "begin_icp_job") as begin, \
         patch.object(marker_alignment, "_register_icp_poll_timer"), \
         patch.object(marker_alignment, "request_redraw"), patch.object(marker_alignment, "_report_status"), \
         patch.object(marker_alignment, "configured_icp_python_path", return_value=python):
        assert marker_alignment.COLMAPALIGN_OT_align_marker_preview.execute(SimpleNamespace(), SimpleNamespace()) == {"FINISHED"}
        assert begin.call_args.kwargs["solver_backend"] == "POINT_KD_ICP"
        assert begin.call_args.kwargs["scale_refine_enabled"] is False
        context = SimpleNamespace(scene=SimpleNamespace(colmap_align_settings=SimpleNamespace(icp_sim3_enabled=True)))
        assert marker_alignment.COLMAPALIGN_OT_align_marker_preview.execute(SimpleNamespace(), context) == {"FINISHED"}
        assert begin.call_args.kwargs["scale_refine_enabled"] is True
        assert marker_alignment.COLMAPALIGN_OT_validate_point_kd_environment.execute(SimpleNamespace(), SimpleNamespace()) == {"FINISHED"}


def _assert_mappings_closed(names) -> None:
    """원본과 작업의 마지막 소유권 해제 뒤 Windows 매핑이 사라지는지 검사합니다."""
    from multiprocessing.shared_memory import SharedMemory
    for name in names:
        try:
            memory = SharedMemory(name=name)
        except FileNotFoundError:
            continue
        memory.close()
        raise AssertionError('종료된 작업의 공유 입력이 남아 있습니다.')


def _mapping_names(manifest) -> list[str]:
    """역할별 상주 원본 목록에서 수명을 검증할 좌표 매핑 이름을 추출합니다."""
    return [part['positions_mapping']['name'] for role in ('moving', 'reference')
            for part in manifest['geometry'][role]['parts']]


def _assert_mappings_available(names) -> None:
    """작업 종료 뒤에도 등록 모델이 소유한 원본 입력을 재사용할 수 있는지 검사합니다."""
    from multiprocessing.shared_memory import SharedMemory
    for name in names:
        memory = SharedMemory(name=name)
        memory.close()


def test_launch_failure_releases_shared_inputs() -> None:
    """시작 실패 시 작업 파일은 해제하고 등록 원본은 다음 Align을 위해 유지합니다."""
    from tools.align.alignment import icp_point_kd_input as shared_input
    from tools.align.alignment import icp_point_kd_runtime as point_runtime
    from tools.align.alignment.icp_runtime import IcpRuntimeError
    runtime, _ = _runtime()
    allocate, create_directory = shared_input._allocate, point_runtime.create_icp_job_directory
    names, directories = [], []
    def track_allocation(owner, count, dtype):
        """실제 생성한 공유 입력의 이름만 수명 검사를 위해 보관합니다."""
        memory, descriptor = allocate(owner, count, dtype)
        names.append(descriptor['name'])
        return memory, descriptor
    def track_directory():
        """실제 작업 디렉터리의 실패 뒤 정리를 확인할 경로를 보관합니다."""
        files = create_directory()
        directories.append(files.job_directory)
        return files
    with patch.object(shared_input, '_allocate', side_effect=track_allocation), \
         patch.object(point_runtime, 'create_icp_job_directory', side_effect=track_directory), \
         patch.object(point_runtime, '_launch', side_effect=OSError('검증용 시작 실패')):
        try:
            _begin(runtime)
        except IcpRuntimeError:
            pass
        else:
            raise AssertionError('시작 실패가 정상 결과로 처리되었습니다.')
    assert len(names) == 0 and len(directories) == 1
    from tools.align.geometry_buffers import geometry_mapping_descriptor
    names = [geometry_mapping_descriptor(entry.positions)['name'] for entry in runtime.model_repository.alignment_inputs.values()]
    _assert_mappings_available(names)
    runtime.model_repository.clear()
    _assert_mappings_closed(names)
    assert not directories[0].exists()


def main() -> None:
    """실제 Blender 데이터 모델과 외부 엔진의 연결 회귀를 실행합니다."""
    _paths()
    tests = tuple((name, function) for name, function in globals().items() if name.startswith("test_") and callable(function))
    for name, function in tests:
        function()
        print(f"통과: {name}", flush=True)
    print(f"점 기반 정렬 런타임 검사 {len(tests)}개 통과", flush=True)


if __name__ == "__main__":
    main()
