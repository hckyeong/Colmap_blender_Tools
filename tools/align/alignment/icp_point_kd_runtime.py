"""점 기반 강체·Sim(3) 엔진을 기존 정렬 수명·미리보기·승인 계약에 연결합니다."""

from __future__ import annotations

import json
from contextlib import ExitStack
import os
from pathlib import Path
import subprocess
from time import monotonic
from typing import Any, Mapping

import numpy as np

from .icp_backend import (IcpBackendError, IcpJobHandle, IcpJobProtocolError, create_icp_job_directory,
                          cleanup_unstarted_job_directory, read_json_file, read_result_arrays, write_job_manifest)
from . import icp_point_kd_contract as contract
from .icp_point_kd_contract import PointKdRuntimeParameters
from .types import Sim3Transform


def bundled_point_kd_worker_path() -> str:
    """설치 패키지 또는 소스 전용 캐시에서 점 기반 실행 파일만 찾습니다."""
    align_root = Path(__file__).resolve().parents[1]
    bundled = align_root / "runtime" / "point_kd" / "colmap_point_kd_worker.exe"
    if bundled.is_file():
        return str(bundled)
    source_root = align_root.parent.parent
    development = source_root / ".point-kd-runtime" / "point_kd" / "colmap_point_kd_worker.exe"
    if (source_root / "build_tools").is_dir() and development.is_file():
        return str(development)
    if (source_root / "test_env" / "manage.py").is_file():
        development = source_root.parent / "runtime" / "point_kd" / "colmap_point_kd_worker.exe"
        if development.is_file():
            return str(development)
    return ""


def validate_point_kd_environment(python_binary_path: str, native_worker_path: str | None = None) -> dict[str, Any]:
    """전용 Python과 실제 네이티브의 최소 계산·식별자·공유 입력 지원을 검사합니다."""
    python = Path(python_binary_path).expanduser()
    native = Path(native_worker_path or bundled_point_kd_worker_path())
    if not python.is_file() or not native.is_file():
        raise IcpBackendError("점 기반 정렬 실행 환경이 없습니다. 전용 실행 환경이 포함된 애드온을 설치하십시오.")
    script = Path(__file__).with_name("icp_point_kd_worker.py")
    try:
        completed = subprocess.run([str(python), "-I", str(script), "--check-environment", "--native-worker", str(native)],
                                   stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   encoding="utf-8", errors="replace", timeout=60,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if completed.returncode:
            raise IcpBackendError(f"점 기반 정렬 환경 검사 실패: {(completed.stderr or completed.stdout).strip()}")
        payload = json.loads(completed.stdout)
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        raise IcpBackendError("점 기반 정렬 환경 검사를 완료하지 못했습니다.") from error
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        raise IcpBackendError("점 기반 정렬 환경 검사가 성공 상태가 아닙니다.")
    if os.name == 'nt' and 'WIN32_SHARED_MEMORY_V1' not in payload.get('native_environment', {}).get('input_transports', []):
        raise IcpBackendError('점 기반 실행 파일이 공유 입력을 지원하지 않습니다. 같은 버전의 실행 환경을 설치하십시오.')
    if 'SIM3' not in payload.get('native_environment', {}).get('scale_modes', []):
        raise IcpBackendError('점 기반 실행 파일이 Sim(3) 정렬을 지원하지 않습니다. 같은 버전의 실행 환경을 설치하십시오.')
    for name in ("backend_id", "algorithm_version", "backend_algorithm_version", "job_protocol_version"):
        if payload.get(name) != contract.identity({})[name]:
            raise IcpBackendError(f"점 기반 정렬 환경의 {name} 식별자가 다릅니다.")
    return payload


def _write_geometry(files: Any, geometry: Any, prefix: str) -> dict[str, Any]:
    """전체 유효 좌표와 유효 삼각면을 원본과 분리한 작업 파일로 기록합니다."""
    positions = np.ascontiguousarray(geometry.positions_float64, dtype="<f8")
    positions_name = f"{prefix}_positions.bin"
    with (files.job_directory / positions_name).open("xb") as stream:
        positions.tofile(stream)
    entry = {"kind": geometry.geometry_kind, "positions_file": positions_name, "point_count": len(positions),
             "provenance": contract.plain(geometry.provenance)}
    if geometry.triangles_uint32 is not None:
        name = f"{prefix}_triangles.bin"
        triangles = np.ascontiguousarray(geometry.triangles_uint32, dtype="<u4")
        with (files.job_directory / name).open("xb") as stream:
            triangles.tofile(stream)
        entry.update(triangles_file=name, triangle_count=len(triangles))
    return entry


def _launch(files: Any, manifest: Mapping[str, Any], python_binary_path: str, native_worker_path: str) -> IcpJobHandle:
    """Blender를 차단하지 않는 전용 외부 조정기 프로세스를 시작합니다."""
    python = Path(python_binary_path).expanduser()
    native = Path(native_worker_path).expanduser()
    if not python.is_file() or not native.is_file():
        raise IcpBackendError("점 기반 정렬의 Python 또는 네이티브 실행 파일을 찾지 못했습니다.")
    python = python.resolve(strict=True)
    native = native.resolve(strict=True)
    stream = files.worker_log_path.open("wb")
    try:
        process = subprocess.Popen([str(python), "-I", str(Path(__file__).with_name("icp_point_kd_worker.py")),
                                    "--job-dir", str(files.job_directory), "--native-worker", str(native)],
                                   cwd=str(files.job_directory), stdin=subprocess.DEVNULL, stdout=stream,
                                   stderr=subprocess.STDOUT, shell=False,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except OSError as error:
        stream.close()
        raise IcpBackendError("점 기반 정렬 작업을 시작하지 못했습니다.") from error
    return IcpJobHandle(job_id=manifest["job_id"], files=files, process=process,
                        input_fingerprint=manifest["input_fingerprint"], backend_id=contract.BACKEND_ID,
                        backend_algorithm_version=contract.ALGORITHM_VERSION, _worker_log_handle=stream)


def begin_point_kd_job(runtime: Any, python_binary_path: str, native_worker_path: str | None = None,
                       runtime_parameters: PointKdRuntimeParameters | None = None, *,
                       scale_refine_enabled: bool = False) -> IcpJobHandle:
    """검증된 원본 공유 버퍼와 선택한 강체·Sim(3) 매개변수를 독립 계산에 전달합니다."""
    from . import icp_runtime as ir
    from .icp_geometry import create_cccore_geometry_snapshot, geometry_selection_block_reason
    from .runtime import create_icp_seed

    state = ir._icp_state(runtime)
    reason = geometry_selection_block_reason(runtime)
    if reason:
        raise ir.IcpRuntimeError(reason)
    state.phase = ir.ICP_ALIGNMENT_PHASE_PREPARING
    state.last_error = ""
    state.started_at = monotonic()
    files = None
    input_owner = ExitStack()
    try:
        seed = create_icp_seed(runtime)
        marker_rms = 0.0 if seed.marker_result is None else float(seed.marker_result.metrics.rms)
        contract.number(marker_rms, "마커 초기 RMS")
        references = ir._reference_ids_for_seed(runtime, seed)
        files = create_icp_job_directory()
        if os.name == 'nt':
            from .icp_point_kd_input import create_shared_geometry_snapshot
            geometry, manifest_geometry = create_shared_geometry_snapshot(runtime, input_owner,
                moving_model_id=seed.moving_model_id, reference_model_ids=references,
                initial_alignment_float64=seed.initial_alignment_float64)
            transport = ('WIN32_SHARED_LOCAL_V1' if 'parts' in manifest_geometry['moving']
                         else 'WIN32_SHARED_MEMORY_V1')
        else:
            snapshot = create_cccore_geometry_snapshot(runtime, moving_model_id=seed.moving_model_id,
                reference_model_ids=references, initial_alignment_float64=seed.initial_alignment_float64,
                point_kd_input=True)
            manifest_geometry = {'moving': _write_geometry(files, snapshot.moving, 'moving'),
                'reference': _write_geometry(files, snapshot.reference_combined, 'reference'),
                'input_fingerprint': snapshot.input_fingerprint, 'provenance': contract.plain(snapshot.provenance)}
            geometry = snapshot.without_arrays()
            del snapshot
            transport = 'FILES_V1'
        if runtime_parameters is None:
            from ..properties import configured_cc_cpu_thread_limit
            runtime_parameters = PointKdRuntimeParameters(max_threads=configured_cc_cpu_thread_limit(),
                scale_mode="SIM3" if scale_refine_enabled else "FIXED")
        parameters = runtime_parameters
        model_state = ir._model_state_snapshot(runtime, (seed.moving_model_id, *references))
        revisions = tuple(sorted((item[0], item[1]) for item in model_state))
        input_fingerprint = contract.fingerprint({"geometry": geometry.input_fingerprint,
            "parameters": parameters.fingerprint, "marker_signature": seed.marker_signature,
            "algorithm": contract.ALGORITHM_VERSION})
        manifest = {"manifest_version": 1, **contract.identity({"job_id": files.job_directory.name,
                    "input_fingerprint": input_fingerprint, "moving_model_id": seed.moving_model_id}),
                    "reference_model_ids": list(references), "initial_alignment_4x4": seed.initial_alignment_float64.tolist(),
                    "marker_signature": seed.marker_signature, "marker_initial_rms": marker_rms,
                    "model_revision_snapshot": contract.plain(revisions), "model_state_snapshot": contract.plain(model_state),
                    "parameter_fingerprint": parameters.fingerprint, "parameters": parameters.to_mapping(),
                    "geometry_mode": geometry.geometry_mode, "input_transport": transport, "geometry": manifest_geometry}
        contract.validate_manifest(contract.plain(manifest), files.job_directory)
        input_data = ir.IcpRuntimeInput(seed=seed, snapshot=geometry, parameters=parameters,
                    input_fingerprint=input_fingerprint, model_state_snapshot=model_state, reference_model_ids=references,
                    model_revision_snapshot=revisions, algorithm_version=contract.ALGORITHM_VERSION,
                    moving_cloud_frame="PROJECT_WORLD", solver_backend=contract.BACKEND_ID,
                    backend_algorithm_version=contract.ALGORITHM_VERSION)
        write_job_manifest(files, manifest)
        handle = _launch(files, manifest, python_binary_path, native_worker_path or bundled_point_kd_worker_path())
        handle._input_resource_owner = input_owner
    except Exception as error:
        input_owner.close()
        if files is not None:
            cleanup_unstarted_job_directory(files)
        state.clear(error_message=str(error))
        state.phase = ir.ICP_ALIGNMENT_PHASE_FAILED
        raise ir.IcpRuntimeError(str(error)) from error
    state.phase = ir.ICP_ALIGNMENT_PHASE_RUNNING
    state.job_handle = handle
    state.input_data = input_data
    state.progress = ir.IcpProgressState(stage="PREPARING", level_name="", iteration=0, progress=0,
                        message="점 기반 정렬 입력을 준비했습니다.", backend_id=contract.BACKEND_ID,
                        backend_algorithm_version=contract.ALGORITHM_VERSION)
    state.result = None
    state.working_alignment = None
    state.cancel_requested_at = None
    state.termination_requested_at = None
    return handle


def validate_payload(payload: Mapping[str, Any], handle: IcpJobHandle) -> None:
    """모든 작업 출력의 엔진·프로토콜·입력 식별자를 검증합니다."""
    expected = contract.identity({"job_id": handle.job_id, "input_fingerprint": handle.input_fingerprint})
    for name, value in expected.items():
        if name != "moving_model_id" and payload.get(name) != value:
            raise IcpJobProtocolError(f"점 기반 정렬 출력의 {name} 식별자가 현재 작업과 다릅니다.")


def read_result(handle: IcpJobHandle) -> tuple[dict[str, Any], dict[str, np.ndarray]] | None:
    """완료된 결과 메타데이터와 고정된 두 행렬을 함께 읽습니다."""
    if not handle.files.result_path.is_file() or not handle.files.result_array_path.is_file():
        return None
    metadata = read_json_file(handle.files.result_path)
    validate_payload(metadata, handle)
    arrays = read_result_arrays(handle.files.result_array_path)
    if set(arrays) != {"delta_4x4", "final_alignment_4x4"}:
        raise IcpJobProtocolError("점 기반 정렬 결과의 행렬 항목이 다릅니다.")
    return metadata, arrays


def _validate_alignment(input_data: Any, alignment: Sim3Transform) -> None:
    """강체 모드의 초기 배율 보존과 Sim(3) 모드의 양수 균일 배율을 검사합니다."""
    initial = Sim3Transform(matrix_float64=input_data.seed.initial_alignment_float64)
    mode = input_data.parameters.scale_mode
    if mode == "FIXED" and not np.isclose(alignment.uniform_scale, initial.uniform_scale, rtol=1e-9, atol=1e-9):
        raise IcpJobProtocolError("점 기반 강체 정렬이 초기 배율을 변경했습니다.")
    contract.moving_delta(alignment.matrix_float64 @ initial.inverse_float64, False, mode)


def progress_from_payload(payload: Mapping[str, Any], *, handle: IcpJobHandle, input_data: Any) -> Any:
    """실제 반복 진행과 검증된 P 방향 미리보기를 공통 상태로 변환합니다."""
    from . import icp_runtime as ir
    validate_payload(payload, handle)
    if payload.get("moving_model_id") != input_data.seed.moving_model_id or payload.get("stage") not in contract.PROGRESS_STAGES:
        raise IcpJobProtocolError("점 기반 진행률의 모델 또는 단계가 올바르지 않습니다.")
    value = contract.number(payload.get("progress"), "진행률")
    iteration = contract.integer(payload.get("iteration", 0), "반복 번호")
    if value > 1 or iteration > input_data.parameters.max_iterations:
        raise IcpJobProtocolError("점 기반 진행률 또는 반복 번호가 상한을 벗어났습니다.")
    if payload["stage"] == "COMPLETED" and (payload.get("state") != "SUCCEEDED"
            or payload.get("level_complete") is not True or payload.get("quality") not in {"PASS", "REVIEW"}):
        raise IcpJobProtocolError("점 기반 완료 진행률의 종료 상태가 올바르지 않습니다.")
    stable = None
    if payload.get("stable_alignment_4x4") is not None:
        stable = Sim3Transform(matrix_float64=payload["stable_alignment_4x4"])
        _validate_alignment(input_data, stable)
    return ir.IcpProgressState(stage=payload["stage"], level_name="", iteration=iteration, progress=value,
                message=str(payload.get("message", "")), stable_alignment=stable,
                max_iterations=input_data.parameters.max_iterations, level_complete=bool(payload.get("level_complete", False)),
                objective_kind=str(payload.get("objective_kind", "")), backend_id=contract.BACKEND_ID,
                backend_algorithm_version=contract.ALGORITHM_VERSION)


def current_geometry_matches(runtime: Any, input_data: Any, *, verify_content: bool = True) -> bool:
    """원본 전체 내용·선택·공통 변환이 작업 시점과 동일한지 검사합니다."""
    from .icp_geometry import geometry_snapshot_validation_message
    return geometry_snapshot_validation_message(runtime, input_data.snapshot,
                initial_alignment_float64=input_data.seed.initial_alignment_float64,
                verify_content=verify_content, point_kd_input=True) is None


def result_from_worker(runtime: Any, state: Any, metadata: Mapping[str, Any], arrays: Mapping[str, np.ndarray]) -> Any:
    """입력·배율 모드·실제 행렬·품질 의미를 재검증하고 승인 가능한 결과를 만듭니다."""
    from . import icp_runtime as ir
    from .icp_point_kd_worker import native_request
    input_data = state.input_data
    handle = state.job_handle
    if input_data is None or handle is None:
        raise IcpJobProtocolError("점 기반 결과에 연결된 작업 입력이 없습니다.")
    validate_payload(metadata, handle)
    manifest = contract.validate_manifest(read_json_file(handle.files.manifest_path), handle.files.job_directory)
    if metadata.get('input_transport', 'FILES_V1') != manifest.get('input_transport', 'FILES_V1'):
        raise IcpJobProtocolError('결과의 입력 전달 방식이 작업 입력과 다릅니다.')
    request, reverse = native_request(manifest)
    native = contract.validate_native_result(metadata.get("native_result", {}), request)
    reported_parameters = metadata.get("parameters")
    if not isinstance(reported_parameters, Mapping):
        raise IcpJobProtocolError("점 기반 결과의 매개변수가 객체가 아닙니다.")
    if contract.PointKdRuntimeParameters(**reported_parameters).fingerprint != input_data.parameters.fingerprint:
        raise IcpJobProtocolError("점 기반 결과의 매개변수가 작업 설정과 다릅니다.")
    for name in ("marker_signature", "reference_model_ids", "model_state_snapshot", "model_revision_snapshot", "parameter_fingerprint", "parameters", "geometry_mode"):
        if metadata.get(name) != manifest[name]:
            raise IcpJobProtocolError(f"점 기반 결과의 {name} 값이 작업 입력과 다릅니다.")
    if (metadata.get("geometry_input_fingerprint") != input_data.snapshot.input_fingerprint
            or metadata.get("geometry_provenance") != manifest["geometry"]["provenance"]):
        raise IcpJobProtocolError("점 기반 결과의 형상 출처가 입력과 다릅니다.")
    mode = input_data.parameters.scale_mode
    native_delta = contract.moving_delta(native["delta_4x4"], reverse, mode)
    expected = {"status": "SUCCEEDED", "reverse_registration": reverse, "method": request["method"],
                "input_policy": "ALL_VALID_POINTS", "independent_evaluation_performed": False,
                "downsampling_performed": False, "metric_kind": "ALIGNMENT_CORRESPONDENCE_RMS",
                "scale_mode": mode, "additional_scale": contract.uniform_scale(native_delta) if mode == "SIM3" else 1.0,
                "initial_rms": native["initial_rms"], "final_rms": native["final_rms"],
                "iterations": native["iterations"], "solver_stop_reason": native["termination_reason"]}
    for name, value in expected.items():
        actual = metadata.get(name)
        if name == "additional_scale" and mode == "SIM3":
            actual_scale = contract.number(actual, "결과 추가 배율")
            if not np.isclose(actual_scale, value, rtol=1e-10, atol=0):
                raise IcpJobProtocolError("점 기반 결과의 추가 배율이 네이티브 행렬과 다릅니다.")
            continue
        if (actual is not value if isinstance(value, bool) else actual != value):
            raise IcpJobProtocolError(f"점 기반 결과의 {name} 값이 네이티브 계약과 다릅니다.")
    for name in ("initial_rms", "final_rms", "additional_scale", "elapsed_seconds"):
        contract.number(metadata.get(name), f"결과 {name}")
    ir._validate_finite_json_values(dict(metadata), field_name="점 기반 결과")
    final = Sim3Transform(matrix_float64=arrays["final_alignment_4x4"])
    delta = Sim3Transform(matrix_float64=arrays["delta_4x4"])
    _validate_alignment(input_data, final)
    if (not np.allclose(delta.matrix_float64, native_delta, rtol=0, atol=1e-9)
            or not np.allclose(final.matrix_float64, native_delta @ input_data.seed.initial_alignment_float64, rtol=0, atol=1e-8)):
        raise IcpJobProtocolError("점 기반 최종 행렬이 원래 P 방향의 정렬 변환과 일치하지 않습니다.")
    reasons = metadata.get("review_reasons")
    if not isinstance(reasons, list) or any(not isinstance(item, str) for item in reasons):
        raise IcpJobProtocolError("점 기반 결과의 검토 사유가 올바르지 않습니다.")
    needs_review = native["status"] == "REVIEW" or native["termination_reason"] in {"MAX_ITERATIONS", "RMS_WORSENED"} or native["final_rms"] > native["initial_rms"]
    quality = "REVIEW" if needs_review else "PASS"
    if metadata.get("quality") != quality or metadata.get("user_review_required") is not needs_review:
        raise IcpJobProtocolError("점 기반 결과의 품질이 종료 사유와 다릅니다.")
    terminal = read_json_file(handle.files.progress_path)
    if terminal.get("quality") != quality or terminal.get("iteration") != native["iterations"]:
        raise IcpJobProtocolError("점 기반 완료 진행률의 품질 또는 반복 횟수가 최종 결과와 다릅니다.")
    if not ir._current_input_matches(runtime, input_data):
        raise ir.IcpRuntimeStaleError("점 기반 정렬 입력 형상 또는 마커가 변경되었습니다.")
    return ir.IcpRuntimeResult(moving_model_id=input_data.seed.moving_model_id,
        reference_model_ids=input_data.reference_model_ids, final_alignment=final, icp_delta=delta,
        quality={"status": quality, "rmse": native["final_rms"], "reasons": reasons,
                 "independent_evaluation_performed": False}, metadata=dict(metadata), input_fingerprint=input_data.input_fingerprint,
        parameter_fingerprint=input_data.parameters.fingerprint, marker_signature=input_data.seed.marker_signature,
        model_revision_snapshot=input_data.model_revision_snapshot, algorithm_version=contract.ALGORITHM_VERSION,
        solver_backend=contract.BACKEND_ID, backend_algorithm_version=contract.ALGORITHM_VERSION)


def provenance_fields(input_data: Any, result: Any) -> dict[str, Any]:
    """정렬 엔진·배율 모드·대응 RMS·입력 출처를 저장 가능한 감사 기록으로 보존합니다."""
    return {"solver_geometry_dependencies": input_data.snapshot.geometry_dependencies,
            "engine_metadata_json": json.dumps(contract.plain(result.metadata), ensure_ascii=False, sort_keys=True,
                                                separators=(",", ":"), allow_nan=False)}
