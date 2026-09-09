"""CCCoreLib 작업을 기존 정렬 미리보기·승인 상태와 연결합니다."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import subprocess
from time import monotonic
from typing import Any, Mapping

import numpy as np

from .icp_backend import (
    IcpBackendError, IcpJobHandle, IcpJobProtocolError,
    create_icp_job_directory, cleanup_unstarted_job_directory,
    read_json_file, read_result_arrays, write_job_manifest,
)
from .icp_solver_backend import IcpSolverBackend
from .icp_cccore_contract import CC_DEFAULT_MAX_ITERATIONS, CC_DEFAULT_SCALE_REFINE_MAX_ITERATIONS
from .types import Sim3Transform

CC_BACKEND_ID = "CCCORELIB"
CC_ALGORITHM_VERSION = "cccore-icp-v4"
CC_JOB_PROTOCOL_VERSION = "cccore-job-v4"
CC_PROGRESS_STAGES = frozenset({
    "PREPARING", "BASELINE", "RIGID", "RIGID_EVALUATION", "SCALE_REFINE",
    "REFINE_EVALUATION", "FINALIZING", "COMPLETED", "FAILED", "CANCELLED",
})


def _plain(value: Any) -> Any:
    """불변 입력 메타데이터를 원자 JSON에 기록할 기본 값으로 변환합니다."""
    if hasattr(value, "to_mapping"):
        return _plain(value.to_mapping())
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain(item) for item in value]
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, np.generic):
        return value.item()
    return value


def _fingerprint(value: Any) -> str:
    """입력 계약과 설정을 결정적인 작은 지문으로 묶습니다."""
    encoded = json.dumps(_plain(value), sort_keys=True, ensure_ascii=False,
                         separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.blake2b(encoded, digest_size=20).hexdigest()


@dataclass(frozen=True, slots=True)
class CcCoreRuntimeParameters:
    """현재 자동 정밀 실행의 고정 설정과 ±10% 스케일 계약입니다."""

    scale_mode: str = "REFINE"
    scale_refine_max_relative_change: float = 0.10
    scale_refine_max_iterations: int = CC_DEFAULT_SCALE_REFINE_MAX_ITERATIONS
    cc_max_threads: int = 1

    def __post_init__(self) -> None:
        """작업 시작 때 고정한 스레드 상한이 지원 범위인지 검사합니다."""
        if isinstance(self.cc_max_threads, bool) or not isinstance(self.cc_max_threads, int):
            raise ValueError("CC CPU 스레드 상한은 정수여야 합니다.")
        if not 1 <= self.cc_max_threads <= 1024:
            raise ValueError("CC CPU 스레드 상한은 1 이상 1024 이하여야 합니다.")

    def to_mapping(self) -> dict[str, Any]:
        """부분 겹침에서의 수렴 예산과 고정 표본 정책을 계산 조정기에 전달합니다."""
        return {
            "cc_overlap_mode": "ESTIMATED_ON_CANDIDATES",
            "cc_point_input_policy": "OVERLAP_CANDIDATES_SPATIAL_SAMPLE",
            "cc_point_sample_count": 50000,
            "cc_filter_out_farthest_points": True,
            "cc_candidate_probe_batch_size": 8192,
            "cc_candidate_max_depth": 8,
            "cc_candidate_max_patch_points": 250000,
            "cc_candidate_proposal_limit": 20,
            "cc_validation_distance_diagonal_fraction": 0.01,
            "cc_validation_distance_spacing_factor": 3.0,
            "cc_validation_distance_marker_factor": 2.0,
            "cc_candidate_distance_factor": 2.0,
            "cc_validation_distance_max_diagonal_fraction": 0.05,
            "cc_max_iterations": CC_DEFAULT_MAX_ITERATIONS,
            "cc_scale_refine_max_iterations": self.scale_refine_max_iterations,
            "cc_mesh_sample_count": 50000,
            "cc_evaluation_sample_count": 30000,
            "cc_max_threads": self.cc_max_threads,
            "scale_mode": self.scale_mode,
            "scale_refine_max_relative_change": self.scale_refine_max_relative_change,
        }

    @property
    def fingerprint(self) -> str:
        """현재 정밀 매개변수와 코어 계약 버전의 지문을 반환합니다."""
        return _fingerprint({"algorithm": CC_ALGORITHM_VERSION, "parameters": self.to_mapping()})


def bundled_cccore_worker_path() -> str:
    """설치 패키지와 정식 소스 캐시 및 기존 실험 소스의 CC 실행 파일을 찾습니다."""
    align_root = Path(__file__).resolve().parents[1]
    bundled = align_root / "runtime" / "cccore" / "colmap_cccore_worker.exe"
    if bundled.is_file():
        return str(bundled)
    source_root = align_root.parent.parent
    development = source_root / ".cccore-runtime" / "cccore" / "colmap_cccore_worker.exe"
    if (source_root / "build_tools" / "cccore_distribution.py").is_file() and development.is_file():
        return str(development)
    if (source_root / "test_env" / "manage.py").is_file():
        development = source_root.parent / "runtime" / "cccore" / "colmap_cccore_worker.exe"
        if development.is_file():
            return str(development)
    return ""


def validate_cccore_environment(python_binary_path: str, native_worker_path: str | None = None) -> dict[str, Any]:
    """외부 Python과 CC 코어의 실제 최소 계산을 실행해 배포 환경을 검사합니다."""
    python = Path(python_binary_path).expanduser()
    native = Path(native_worker_path or bundled_cccore_worker_path())
    if not python.is_file() or not native.is_file():
        raise IcpBackendError("CC 정렬 실행 환경을 찾지 못했습니다. CC 실행 환경이 포함된 애드온을 설치하십시오.")
    worker = Path(__file__).with_name("icp_cccore_worker.py")
    try:
        checked = subprocess.run(
            [str(python), "-I", str(worker), "--check-environment", "--native-worker", str(native)],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", timeout=60,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise IcpBackendError("CC 정렬 엔진 환경 검사를 완료하지 못했습니다.") from error
    if checked.returncode:
        detail = (checked.stderr or checked.stdout).strip()
        raise IcpBackendError(f"CC 정렬 엔진 환경 검사 실패: {detail}")
    try:
        result = json.loads(checked.stdout)
    except (ValueError, TypeError) as error:
        raise IcpBackendError("CC 정렬 엔진 검사 결과 형식이 올바르지 않습니다.") from error
    if not isinstance(result, dict):
        raise IcpBackendError("CC 정렬 엔진 검사 결과가 객체가 아닙니다.")
    if not result.get("ok"):
        raise IcpBackendError("CC 정렬 엔진 환경 검사가 성공 상태를 보고하지 않았습니다.")
    expected = {
        "backend_id": CC_BACKEND_ID,
        "algorithm_version": CC_ALGORITHM_VERSION,
        "backend_algorithm_version": CC_ALGORITHM_VERSION,
        "job_protocol_version": CC_JOB_PROTOCOL_VERSION,
    }
    for name, value in expected.items():
        if result.get(name) != value:
            raise IcpBackendError(f"CC 정렬 환경의 {name} 식별자가 현재 계약과 다릅니다.")
    native = result.get("native_environment")
    if not isinstance(native, Mapping):
        raise IcpBackendError("CC 정렬 환경에 네이티브 병렬 실행 정보가 없습니다.")
    from .icp_cccore_contract import CcCoreError, validate_native_execution_metadata
    try:
        validate_native_execution_metadata(
            native,
            requested_max_threads=native.get("requested_max_threads"),
        )
    except (CcCoreError, ValueError, TypeError) as error:
        raise IcpBackendError(str(error)) from error
    return result


def _write_geometry(files: Any, geometry: Any, prefix: str) -> dict[str, Any]:
    """검증된 프로젝트 좌표와 삼각면을 외부 작업 전용 파일로 기록합니다."""
    positions_name = f"{prefix}_positions.bin"
    positions_path = files.job_directory / positions_name
    positions = np.ascontiguousarray(geometry.positions_float64, dtype="<f8")
    with positions_path.open("xb") as stream:
        positions.tofile(stream)
    entry = {
        "kind": geometry.geometry_kind,
        "positions_file": positions_name,
        "point_count": len(positions),
        "provenance": _plain(geometry.provenance),
    }
    if geometry.triangles_uint32 is not None:
        triangles_name = f"{prefix}_triangles.bin"
        triangles = np.ascontiguousarray(geometry.triangles_uint32, dtype="<u4")
        with (files.job_directory / triangles_name).open("xb") as stream:
            triangles.tofile(stream)
        entry.update(triangles_file=triangles_name, triangle_count=len(triangles))
    return entry


def _launch(files: Any, manifest: Mapping[str, Any], python_binary_path: str, native_worker_path: str) -> IcpJobHandle:
    """Blender와 독립된 계산 조정기를 숨김 프로세스로 시작합니다."""
    python = Path(python_binary_path).expanduser()
    native = Path(native_worker_path).expanduser()
    if not python.is_file() or not native.is_file():
        raise IcpBackendError("CC 정렬 실행 파일 또는 Python 런타임이 없습니다.")
    script = Path(__file__).with_name("icp_cccore_worker.py")
    log_stream = files.worker_log_path.open("wb")
    try:
        process = subprocess.Popen(
            [str(python), "-I", str(script), "--job-dir", str(files.job_directory),
             "--native-worker", str(native)],
            cwd=str(files.job_directory), stdin=subprocess.DEVNULL,
            stdout=log_stream, stderr=subprocess.STDOUT, shell=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except OSError as error:
        log_stream.close()
        raise IcpBackendError("CC 정렬 작업을 시작하지 못했습니다.") from error
    return IcpJobHandle(
        job_id=manifest["job_id"], files=files, process=process,
        input_fingerprint=manifest["input_fingerprint"], backend_id=CC_BACKEND_ID,
        backend_algorithm_version=CC_ALGORITHM_VERSION, _worker_log_handle=log_stream,
    )


def begin_cccore_job(
    runtime: Any,
    python_binary_path: str,
    native_worker_path: str | None = None,
    runtime_parameters: CcCoreRuntimeParameters | None = None,
) -> IcpJobHandle:
    """현재 선택 형상을 고정하고 CC 정렬을 기존 작업 상태에 연결합니다."""
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
    try:
        seed = create_icp_seed(runtime)
        marker_initial_rms = 0.0 if seed.marker_result is None else float(seed.marker_result.metrics.rms)
        if not np.isfinite(marker_initial_rms) or marker_initial_rms < 0.0:
            raise IcpJobProtocolError("마커 초기 정렬 잔차가 유효한 월드 거리 값이 아닙니다.")
        reference_ids = ir._reference_ids_for_seed(runtime, seed)
        geometry = create_cccore_geometry_snapshot(
            runtime, moving_model_id=seed.moving_model_id,
            reference_model_ids=reference_ids,
            initial_alignment_float64=seed.initial_alignment_float64,
        )
        if runtime_parameters is None:
            from ..properties import configured_cc_cpu_thread_limit
            runtime_parameters = CcCoreRuntimeParameters(
                cc_max_threads=configured_cc_cpu_thread_limit(),
            )
        parameters = runtime_parameters
        model_state = ir._model_state_snapshot(runtime, (seed.moving_model_id, *reference_ids))
        revisions = tuple(sorted((item[0], item[1]) for item in model_state))
        input_fingerprint = _fingerprint({
            "geometry": geometry.input_fingerprint, "parameters": parameters.fingerprint,
            "marker_signature": seed.marker_signature, "algorithm": CC_ALGORITHM_VERSION,
        })
        files = create_icp_job_directory()
        geometry_entries = {
            "moving": _write_geometry(files, geometry.moving, "moving"),
            "reference": _write_geometry(files, geometry.reference_combined, "reference"),
            "input_fingerprint": geometry.input_fingerprint,
            "provenance": _plain(geometry.provenance),
        }
        manifest = {
            "manifest_version": 1, "job_protocol_version": CC_JOB_PROTOCOL_VERSION,
            "job_id": files.job_directory.name, "input_fingerprint": input_fingerprint,
            "backend_id": CC_BACKEND_ID, "algorithm_version": CC_ALGORITHM_VERSION,
            "backend_algorithm_version": CC_ALGORITHM_VERSION,
            "moving_model_id": seed.moving_model_id, "reference_model_ids": list(reference_ids),
            "initial_alignment_4x4": seed.initial_alignment_float64.tolist(),
            "marker_signature": seed.marker_signature,
            "marker_initial_rms": marker_initial_rms,
            "model_revision_snapshot": _plain(revisions), "model_state_snapshot": _plain(model_state),
            "parameter_fingerprint": parameters.fingerprint, "parameters": parameters.to_mapping(),
            "geometry_mode": geometry.geometry_mode, "reverse_registration": geometry.reverse_registration,
            "geometry": geometry_entries,
        }
        input_data = ir.IcpRuntimeInput(
            seed=seed, snapshot=geometry.without_arrays(), parameters=parameters,
            input_fingerprint=input_fingerprint, model_state_snapshot=model_state,
            reference_model_ids=reference_ids, model_revision_snapshot=revisions,
            algorithm_version=CC_ALGORITHM_VERSION, moving_cloud_frame="PROJECT_WORLD",
            solver_backend=CC_BACKEND_ID, backend_algorithm_version=CC_ALGORITHM_VERSION,
        )
        write_job_manifest(files, manifest)
        handle = _launch(files, manifest, python_binary_path,
                         native_worker_path or bundled_cccore_worker_path())
    except Exception as error:
        if files is not None:
            cleanup_unstarted_job_directory(files)
        state.clear(error_message=str(error))
        state.phase = ir.ICP_ALIGNMENT_PHASE_FAILED
        raise ir.IcpRuntimeError(str(error)) from error
    state.phase = ir.ICP_ALIGNMENT_PHASE_RUNNING
    state.job_handle = handle
    state.input_data = input_data
    state.progress = ir.IcpProgressState(
        stage="PREPARING", level_name="", iteration=0, progress=0.0,
        message="CC 정렬 입력을 준비했습니다.", backend_id=CC_BACKEND_ID,
        backend_algorithm_version=CC_ALGORITHM_VERSION,
    )
    state.result = None
    state.working_alignment = None
    state.cancel_requested_at = None
    state.termination_requested_at = None
    return handle


def validate_cccore_payload(payload: Mapping[str, Any], handle: IcpJobHandle) -> None:
    """작업·입력·엔진·프로토콜 식별자가 현재 작업과 같은지 검사합니다."""
    expected = {
        "job_id": handle.job_id, "input_fingerprint": handle.input_fingerprint,
        "backend_id": CC_BACKEND_ID, "algorithm_version": CC_ALGORITHM_VERSION,
        "backend_algorithm_version": CC_ALGORITHM_VERSION,
        "job_protocol_version": CC_JOB_PROTOCOL_VERSION,
    }
    for name, value in expected.items():
        if payload.get(name) != value:
            raise IcpJobProtocolError(f"CC 정렬 결과의 {name} 식별자가 현재 작업과 다릅니다.")


def read_cccore_result(handle: IcpJobHandle) -> tuple[dict[str, Any], dict[str, np.ndarray]] | None:
    """완성된 CC 결과 JSON과 정확히 두 개의 변환 행렬을 읽습니다."""
    files = handle.files
    if not files.result_path.is_file() or not files.result_array_path.is_file():
        return None
    metadata = read_json_file(files.result_path)
    validate_cccore_payload(metadata, handle)
    arrays = read_result_arrays(files.result_array_path)
    if set(arrays) != {"delta_4x4", "final_alignment_4x4"}:
        raise IcpJobProtocolError("CC 정렬 결과 행렬 항목이 올바르지 않습니다.")
    if any(array.shape != (4, 4) for array in arrays.values()):
        raise IcpJobProtocolError("CC 정렬 결과 행렬 크기가 올바르지 않습니다.")
    return metadata, arrays


def progress_from_cccore(payload: Mapping[str, Any], *, handle: IcpJobHandle, input_data: Any) -> Any:
    """CC의 실제 계산 단계·반복 상한과 유효한 진행률을 기존 화면에 전달합니다."""
    from . import icp_runtime as ir

    validate_cccore_payload(payload, handle)
    if payload.get("moving_model_id") != input_data.seed.moving_model_id:
        raise IcpJobProtocolError("CC 진행률의 이동 모델이 입력과 다릅니다.")
    stage = payload.get("stage")
    value = payload.get("progress")
    if stage not in CC_PROGRESS_STAGES:
        raise IcpJobProtocolError("CC 진행 단계가 올바르지 않습니다.")
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not np.isfinite(value) or not 0 <= value <= 1:
        raise IcpJobProtocolError("CC 진행률이 올바르지 않습니다.")
    stable = None
    if payload.get("stable_alignment_4x4") is not None:
        stable = Sim3Transform(matrix_float64=np.asarray(payload["stable_alignment_4x4"], dtype=np.float64))
        _validate_scale(input_data, stable)
    return ir.IcpProgressState(
        stage=stage, level_name=str(payload.get("level_name", stage)),
        iteration=0, progress=float(value), message=str(payload.get("message", "")),
        stable_alignment=stable,
        max_iterations=(CC_DEFAULT_SCALE_REFINE_MAX_ITERATIONS if stage == "SCALE_REFINE" else CC_DEFAULT_MAX_ITERATIONS),
        level_complete=stage == "COMPLETED",
        backend_id=CC_BACKEND_ID, backend_algorithm_version=CC_ALGORITHM_VERSION,
    )


def _validate_scale(input_data: Any, final_alignment: Sim3Transform) -> None:
    """마커 초기 크기를 포함한 시작 행렬 대비 최종 ±10% 한도를 검사합니다."""
    initial = Sim3Transform(matrix_float64=input_data.seed.initial_alignment_float64)
    ratio = final_alignment.uniform_scale / initial.uniform_scale
    if not 0.9 - 1.0e-8 <= ratio <= 1.1 + 1.0e-8:
        raise IcpJobProtocolError("CC 정렬 결과가 ±10% 스케일 보정 범위를 벗어났습니다.")


def current_geometry_matches(runtime: Any, input_data: Any, *, verify_content: bool = True) -> bool:
    """진행 중에는 출처·축·행렬을, 완료·승인 시에는 원본 내용까지 검사합니다."""
    from .icp_geometry import geometry_snapshot_validation_message

    return geometry_snapshot_validation_message(
        runtime, input_data.snapshot,
        initial_alignment_float64=input_data.seed.initial_alignment_float64,
        verify_content=verify_content,
    ) is None


def _validate_overlap_result_metadata(metadata: Mapping[str, Any]) -> None:
    """고정 겹침 평가와 표본 진단의 수치·개수·출처 지문을 승인 전에 검사합니다."""
    from .icp_cccore_contract import finite_nonnegative_number, nonnegative_integer

    sampling = metadata.get("sampling_diagnostics")
    if not isinstance(sampling, Mapping):
        raise IcpJobProtocolError("CC 겹침 표본 진단이 없습니다.")
    total_registration = nonnegative_integer(sampling.get("registration_sample_count"), "CC 정합 표본 수")
    if not 3 <= total_registration <= 50000:
        raise IcpJobProtocolError("CC 정합 표본 수가 지원 범위를 벗어났습니다.")
    for name in ("optimizer_seed", "evaluation_seed"):
        nonnegative_integer(sampling.get(name), f"CC 표본 {name}")
    if sampling["optimizer_seed"] == sampling["evaluation_seed"]:
        raise IcpJobProtocolError("CC 정합과 독립 평가의 표본 시드가 같습니다.")
    for name in ("candidate_shortage", "spatial_distribution_warning"):
        if name in sampling and type(sampling[name]) is not bool:
            raise IcpJobProtocolError(f"CC 표본 진단의 {name} 값이 논리값이 아닙니다.")
    directions = sampling.get("directions")
    if not isinstance(directions, Mapping) or not directions or not set(directions) <= {
        "moving_to_reference", "reference_to_moving",
    }:
        raise IcpJobProtocolError("CC 겹침 평가 방향 진단이 올바르지 않습니다.")
    expected_directions = {"moving_to_reference", "reference_to_moving"} if metadata.get("geometry_mode") == "MESH_TO_MESH" else {
        "reference_to_moving" if metadata.get("geometry_mode") == "MESH_TO_POINT" else "moving_to_reference",
    }
    if set(directions) != expected_directions:
        raise IcpJobProtocolError("CC 겹침 평가 방향이 입력 형상 조합과 다릅니다.")
    evaluation_count = 0
    registration_count = 0
    for direction in directions.values():
        if not isinstance(direction, Mapping):
            raise IcpJobProtocolError("CC 평가 방향 진단이 객체가 아닙니다.")
        for name in ("domain", "evaluation"):
            if not isinstance(direction.get(name), Mapping):
                raise IcpJobProtocolError(f"CC 표본 진단의 {name} 값이 객체가 아닙니다.")
        count = nonnegative_integer(direction.get("evaluation_sample_count"), "CC 방향별 평가 표본 수")
        if not 3 <= count <= 30000:
            raise IcpJobProtocolError("CC 방향별 독립 평가 표본 수가 지원 범위를 벗어났습니다.")
        if nonnegative_integer(direction["evaluation"].get("sample_count"), "CC 독립 표본 진단 수") != count:
            raise IcpJobProtocolError("CC 독립 평가 표본 수가 채집 진단과 다릅니다.")
        evaluation_count += count
        nonnegative_integer(direction.get("full_reference_sample_count"), "CC 전체 참고 표본 수")
        fingerprints = ["evaluation_sample_fingerprint"]
        if "registration" in direction:
            if not isinstance(direction["registration"], Mapping):
                raise IcpJobProtocolError("CC 정합 표본 진단이 객체가 아닙니다.")
            registration_count += nonnegative_integer(direction["registration"].get("sample_count"), "CC 방향별 정합 표본 수")
            fingerprints.append("registration_sample_fingerprint")
        for name in fingerprints:
            value = direction.get(name)
            if not isinstance(value, str) or len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
                raise IcpJobProtocolError(f"CC 표본의 {name} 지문이 올바르지 않습니다.")
    if registration_count != sampling["registration_sample_count"]:
        raise IcpJobProtocolError("CC 정합 표본 합계가 방향별 진단과 다릅니다.")
    for name in ("baseline_metrics", "best_candidate_metrics"):
        metric = metadata.get(name)
        if not isinstance(metric, Mapping):
            raise IcpJobProtocolError("CC 고정 겹침 평가 통계가 없습니다.")
        count = nonnegative_integer(metric.get("evaluation_sample_count"), "CC 전체 평가 표본 수")
        if count != evaluation_count:
            raise IcpJobProtocolError("CC 평가 표본 수가 고정 방향별 표본과 다릅니다.")
        metric_directions = metric.get("directions")
        if not isinstance(metric_directions, Mapping) or set(metric_directions) != expected_directions:
            raise IcpJobProtocolError("CC 평가 통계의 방향이 독립 표본 방향과 다릅니다.")
        directional_rms = []
        for direction_name, summary in metric_directions.items():
            if not isinstance(summary, Mapping):
                raise IcpJobProtocolError("CC 방향별 평가 통계가 객체가 아닙니다.")
            directional_rms.append(finite_nonnegative_number(summary.get("rms"), "CC 방향별 겹침 RMS"))
            if nonnegative_integer(summary.get("count"), "CC 방향별 평가점 수") != directions[direction_name]["evaluation_sample_count"]:
                raise IcpJobProtocolError("CC 방향별 평가점 수가 고정 표본과 다릅니다.")
        overlap_mean = finite_nonnegative_number(metric.get("overlap_mean_rms"), "CC 겹침 평가 RMS")
        overlap_rms = finite_nonnegative_number(metric.get("overlap_rms"), "CC 겹침 합산 RMS")
        if not np.isclose(overlap_mean, np.mean(directional_rms), rtol=1e-12, atol=1e-12):
            raise IcpJobProtocolError("CC 겹침 평가 RMS가 방향별 평균과 다릅니다.")
        if not np.isclose(overlap_rms, finite_nonnegative_number(metric.get("rms"), "CC 고정 평가 RMS"), rtol=1e-12, atol=1e-12):
            raise IcpJobProtocolError("CC 겹침 합산 RMS가 고정 평가 통계와 다릅니다.")
        coverage = metric.get("correspondence_coverage")
        if not isinstance(coverage, Mapping):
            raise IcpJobProtocolError("CC 주방향 유효 대응 통계가 없습니다.")
        coverage_count = nonnegative_integer(coverage.get("count"), "CC 주방향 평가 표본 수")
        valid_count = nonnegative_integer(coverage.get("valid_count"), "CC 주방향 유효 대응 수")
        if valid_count > coverage_count or coverage_count > evaluation_count:
            raise IcpJobProtocolError("CC 주방향 유효 대응 수가 평가 표본 수를 초과했습니다.")
        full = metric.get("full_reference_metrics")
        if not isinstance(full, Mapping):
            raise IcpJobProtocolError("CC 전체 참고 평가 통계가 없습니다.")
        for field in ("rms", "median", "p95", "trimmed_rms_90", "mean_rms"):
            finite_nonnegative_number(full.get(field), f"CC 전체 참고 {field}")
        nonnegative_integer(full.get("count"), "CC 전체 참고 평가점 수")


def result_from_cccore(runtime: Any, state: Any, metadata: Mapping[str, Any], arrays: Mapping[str, np.ndarray]) -> Any:
    """CC 결과의 출처·스케일·합성 관계를 확인한 뒤 미리보기 값을 만듭니다."""
    from . import icp_runtime as ir

    input_data = state.input_data
    handle = state.job_handle
    if input_data is None or handle is None:
        raise ir.IcpRuntimeError("CC 결과 입력 상태가 없습니다.")
    validate_cccore_payload(metadata, handle)
    expected = {
        "moving_model_id": input_data.seed.moving_model_id,
        "reference_model_ids": list(input_data.reference_model_ids),
        "parameter_fingerprint": input_data.parameters.fingerprint,
        "marker_signature": input_data.seed.marker_signature,
        "model_revision_snapshot": _plain(input_data.model_revision_snapshot),
        "model_state_snapshot": _plain(input_data.model_state_snapshot),
        "geometry_mode": input_data.snapshot.geometry_mode,
        "reverse_registration": input_data.snapshot.reverse_registration,
    }
    for name, value in expected.items():
        if metadata.get(name) != value:
            raise ir.IcpRuntimeStaleError(f"CC 결과의 {name} 값이 현재 입력과 다릅니다.")
    from .icp_cccore_contract import (
        CcCoreError,
        validate_exact_evaluation_cache_statistics,
        validate_native_call_summaries,
        validate_native_execution_metadata,
        validate_native_geometry_statistics,
        validate_native_session_aggregates,
        validate_physical_native_call_summaries,
        validate_native_session_statistics,
        validate_session_protocol_statistics,
        finite_nonnegative_number,
    )
    try:
        _validate_overlap_result_metadata(metadata)
        marker_rms = finite_nonnegative_number(metadata.get("marker_initial_rms"), "CC 마커 초기 잔차")
        expected_marker_rms = 0.0 if input_data.seed.marker_result is None else float(input_data.seed.marker_result.metrics.rms)
        if marker_rms != expected_marker_rms:
            raise IcpJobProtocolError("CC 결과의 마커 초기 잔차가 작업 입력과 다릅니다.")
        native_identity = validate_native_execution_metadata(
            metadata,
            requested_max_threads=input_data.parameters.cc_max_threads,
        )
        if type(metadata.get("native_process_count")) is not int or metadata["native_process_count"] != 1:
            raise IcpJobProtocolError("CC 정렬 결과의 네이티브 프로세스 수가 1이 아닙니다.")
        operations = validate_native_call_summaries(
            metadata.get("logical_operations"),
            geometry_mode=input_data.snapshot.geometry_mode,
            requested_max_threads=native_identity["requested_max_threads"],
            effective_max_threads=native_identity["effective_max_threads"],
        )
        if metadata.get("native_calls") != metadata.get("logical_operations"):
            raise IcpJobProtocolError("CC 정렬 결과의 호환 호출 요약이 논리 연산과 다릅니다.")
        if len(operations) < 1:
            raise IcpJobProtocolError("CC 정렬 결과의 논리 연산이 비어 있습니다.")
        physical_operations = validate_physical_native_call_summaries(
            metadata.get("physical_native_calls"), operations,
            requested_max_threads=native_identity["requested_max_threads"],
            effective_max_threads=native_identity["effective_max_threads"],
        )
        total_alignment_seconds = finite_nonnegative_number(
            metadata.get("elapsed_seconds"), "CC 전체 정렬 시간",
        )
        session_protocol_statistics = validate_session_protocol_statistics(
            metadata.get("session_protocol_statistics"), physical_operations,
            total_alignment_seconds=total_alignment_seconds,
        )
        for name, value in session_protocol_statistics.items():
            top_level = finite_nonnegative_number(metadata.get(name), f"CC 최상위 프로토콜 {name}")
            if not np.isclose(top_level, value, rtol=1.0e-12, atol=1.0e-12):
                raise IcpJobProtocolError(f"CC 정렬 결과의 {name} 중복 기록이 세션 통계와 다릅니다.")
        physical_count = metadata.get("physical_native_command_count")
        if type(physical_count) is not int or physical_count != len(physical_operations):
            raise IcpJobProtocolError("CC 정렬 결과의 실제 네이티브 명령 수가 호출 요약과 다릅니다.")
        validate_exact_evaluation_cache_statistics(metadata.get("exact_evaluation_cache"), operations)
        session_statistics = validate_native_session_statistics(metadata.get("session_statistics"))
        for name in ("process_startup_seconds", "session_elapsed_seconds"):
            value = metadata.get(name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not np.isfinite(value) or value < 0:
                raise IcpJobProtocolError(f"CC 정렬 결과의 {name} 값이 올바르지 않습니다.")
        if metadata.get("session_exit_reason") != "closed":
            raise IcpJobProtocolError("CC 정렬 결과의 세션 종료 사유가 정상 close가 아닙니다.")
        geometry_statistics = validate_native_geometry_statistics(metadata.get("geometry_statistics"))
        validate_native_session_aggregates(
            session_statistics, geometry_statistics, physical_operations,
        )
    except CcCoreError as error:
        raise IcpJobProtocolError(str(error)) from error
    ir._validate_finite_json_values(metadata, field_name="CC 결과")
    if metadata.get("status") != "SUCCEEDED" or metadata.get("quality") not in {"PASS", "REVIEW"}:
        raise IcpJobProtocolError("CC 결과가 승인 가능한 완료 상태가 아닙니다.")
    if not isinstance(metadata.get("core_commit"), str) or not metadata["core_commit"]:
        raise IcpJobProtocolError("CC 결과의 코어 버전이 없습니다.")
    final = Sim3Transform(matrix_float64=arrays["final_alignment_4x4"])
    delta = Sim3Transform(matrix_float64=arrays["delta_4x4"])
    _validate_scale(input_data, final)
    if not np.allclose(final.matrix_float64, delta.matrix_float64 @ input_data.seed.initial_alignment_float64,
                       rtol=1.0e-12, atol=1.0e-9):
        raise IcpJobProtocolError("CC 최종 행렬이 추가 변환과 초기 행렬의 합성과 다릅니다.")
    if not ir._current_input_matches(runtime, input_data):
        raise ir.IcpRuntimeStaleError("CC 정렬 입력 형상 또는 마커가 변경되어 결과를 적용할 수 없습니다.")
    metrics = metadata.get("best_candidate_metrics")
    baseline = metadata.get("baseline_metrics")
    if not isinstance(metrics, Mapping) or not isinstance(baseline, Mapping):
        raise IcpJobProtocolError("CC 정렬 전후 평가가 없습니다.")
    for name in ("rms", "median", "p95", "trimmed_rms_90", "overlap_mean_rms", "overlap_rms",
                 "overlap_mean_rms_normalized", "overlap_rms_normalized"):
        for evaluation in (metrics, baseline):
            value = evaluation.get(name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not np.isfinite(value) or value < 0:
                raise IcpJobProtocolError(f"CC 평가의 {name} 값이 올바르지 않습니다.")
    reasons = metadata.get("review_reasons")
    if not isinstance(reasons, list) or any(not isinstance(reason, str) for reason in reasons):
        raise IcpJobProtocolError("CC 검토 사유가 올바르지 않습니다.")
    quality = {"status": metadata["quality"], "rmse": float(metrics["overlap_mean_rms"]), "reasons": list(reasons)}
    return ir.IcpRuntimeResult(
        moving_model_id=input_data.seed.moving_model_id,
        reference_model_ids=input_data.reference_model_ids,
        final_alignment=final, icp_delta=delta, quality=quality,
        metadata=dict(metadata), input_fingerprint=input_data.input_fingerprint,
        parameter_fingerprint=input_data.parameters.fingerprint, marker_signature=input_data.seed.marker_signature,
        model_revision_snapshot=input_data.model_revision_snapshot, algorithm_version=CC_ALGORITHM_VERSION,
        solver_backend=CC_BACKEND_ID, backend_algorithm_version=CC_ALGORITHM_VERSION,
    )


# 승인 기록에 보존하는 v4 감사 필드를 저장·복원 회귀와 공유합니다.
CCCORE_PROVENANCE_METADATA_FIELDS = (
    "sampling_diagnostics", "marker_initial_rms",
    "core_commit", "native_worker_sha256", "native_build_identity", "quality_policy",
    "geometry_mode", "reverse_registration", "normalization", "parameters",
    "diagnostics", "registration_stages", "best_stage", "solver_stop_reason",
    "geometry_provenance", "initial_alignment_4x4", "baseline_metrics",
    "best_candidate_metrics", "relative_improvement", "review_reasons",
    "quality", "user_review_required", "elapsed_seconds", "thread_backend",
    "tbb_version", "requested_max_threads", "effective_max_threads",
    "hardware_thread_count", "distance_multithread_support",
    "octree_multithread_support", "native_process_count", "logical_operations",
    "native_calls", "physical_native_calls", "physical_native_command_count",
    "exact_evaluation_cache", "process_startup_seconds", "session_elapsed_seconds",
    "session_exit_reason", "session_statistics", "geometry_statistics",
    "session_protocol_statistics", "physical_roundtrip_seconds",
    "protocol_overhead_seconds", "protocol_overhead_ratio_of_roundtrip",
    "protocol_overhead_fraction_of_total_alignment", "geometry_input_fingerprint",
)


def _compact_execution_audit(metadata: dict[str, Any]) -> None:
    """검증을 마친 반복 준비 기록은 개수·지문으로, 등록·평가 기록은 원문으로 보존합니다."""
    summary = {"version": 1, "policy": "PREPARATION_SUMMARY", "fields": {}}
    for name in ("logical_operations", "native_calls", "physical_native_calls", "geometry_statistics"):
        records = metadata.get(name)
        if not isinstance(records, list):
            continue
        if name == "geometry_statistics":
            retained = [item for item in records if item.get("cache_scope") == "SESSION"]
        else:
            retained = [item for item in records if item.get("stage") != "PREPARING"]
        encoded = json.dumps(records, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":"), allow_nan=False).encode("utf-8")
        summary["fields"][name] = {
            "source_count": len(records), "retained_count": len(retained),
            "source_sha256": hashlib.sha256(encoded).hexdigest(),
        }
        metadata[name] = retained
    # 세션 합계에는 생략된 준비 조회와 일회성 형상도 계속 포함됩니다.
    metadata["execution_audit_storage"] = summary


def cccore_provenance_fields(input_data: Any, result: Any) -> dict[str, Any]:
    """승인 순간의 CC 품질·출처와 요약된 실행 이력을 저장 한도 안에 보존합니다."""
    metadata = {
        name: _plain(result.metadata[name])
        for name in CCCORE_PROVENANCE_METADATA_FIELDS if name in result.metadata
    }
    metadata["geometry_input_fingerprint"] = input_data.snapshot.input_fingerprint
    _compact_execution_audit(metadata)
    return {
        "solver_geometry_dependencies": input_data.snapshot.geometry_dependencies,
        "engine_metadata_json": json.dumps(metadata, ensure_ascii=False, sort_keys=True,
                                           separators=(",", ":"), allow_nan=False),
    }
