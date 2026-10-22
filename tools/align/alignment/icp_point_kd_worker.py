"""Blender 밖에서 점 기반 강체·Sim(3) 정렬을 실행하고 결과를 공개합니다."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import struct
import subprocess
import sys
from time import monotonic, sleep
from typing import Any, Mapping

import numpy as np

if __package__:
    from . import icp_point_kd_contract as contract
    from .icp_point_kd_transfer import prepared_native_request
else:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import icp_point_kd_contract as contract
    from icp_point_kd_transfer import prepared_native_request


def parse_arguments(arguments: list[str] | None = None) -> argparse.Namespace:
    """환경 검사 또는 독립 작업의 명시적 경로를 해석합니다."""
    parser = argparse.ArgumentParser(prog="colmap_align_point_kd_worker")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--job-dir")
    group.add_argument("--check-environment", action="store_true")
    parser.add_argument("--native-worker", required=True)
    return parser.parse_args(arguments)


def worker_environment() -> dict[str, Any]:
    """실제로 실행 중인 Python·배열 라이브러리·프로세스 구조를 반환합니다."""
    return {"python_implementation": platform.python_implementation(),
            "worker_python_version": platform.python_version(), "worker_numpy_version": np.__version__,
            "platform_architecture": "64bit" if struct.calcsize("P") == 8 else "32bit"}


def check_environment(native_worker: str | Path) -> dict[str, Any]:
    """네이티브 실행 파일의 자체 검사와 전용 프로토콜을 실제로 확인합니다."""
    native = Path(native_worker)
    environment = worker_environment()
    if not native.is_absolute() or not native.is_file():
        raise contract.PointKdError("점 기반 정렬 실행 파일의 절대 경로가 올바르지 않습니다.")
    if environment["python_implementation"] != "CPython" or environment["platform_architecture"] != "64bit":
        raise contract.PointKdError("점 기반 정렬은 64비트 CPython이 필요합니다.")
    completed = subprocess.run([str(native), "--check-environment"], stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding="utf-8",
                               timeout=30, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    payload = json.loads(completed.stdout)
    if (completed.returncode or not isinstance(payload, dict)
            or payload.get("protocol_version") != contract.NATIVE_PROTOCOL_VERSION
            or payload.get("self_test") != "passed"):
        raise contract.PointKdError("점 기반 네이티브 환경 자체 검사에 실패했습니다.")
    return {**contract.identity({}), **environment, "ok": True, "native_environment": payload}


def native_request(manifest: Mapping[str, Any]) -> tuple[dict[str, Any], bool]:
    """전체점 입력의 방향과 파일·작업별 공유 배열 또는 상주 원본 묶음을 전달합니다."""
    source, target, method = contract.registration_direction(manifest)
    entries = {}
    for key, role in (("source", source), ("target", target)):
        entry = manifest["geometry"][role]
        entries[key] = {name: entry[name] for name in
                        ("positions_file", "positions_mapping", "point_count", "triangles_file", "triangles_mapping",
                         "triangle_count", "normals_file", "normals_mapping", "parts")
                        if name in entry}
    return {"protocol_version": contract.NATIVE_PROTOCOL_VERSION, **entries, "method": method,
            "parameters": {**manifest["parameters"], "marker_rms": manifest["marker_initial_rms"]},
            "cancel_file": "cancel.request", "result_file": "native-result.json",
            "progress_file": "native-progress.json"}, source == "reference"


def progress_payload(manifest: Mapping[str, Any], native: Mapping[str, Any], reverse: bool) -> dict[str, Any]:
    """네이티브 진행률과 원래 P 방향의 안정 변환을 작업 식별자로 감쌉니다."""
    if native.get("protocol_version") != contract.NATIVE_PROTOCOL_VERSION:
        raise contract.PointKdError("네이티브 진행률 프로토콜이 올바르지 않습니다.")
    value = contract.number(native.get("progress"), "네이티브 진행률")
    if value > 1:
        raise contract.PointKdError("네이티브 진행률은 1 이하여야 합니다.")
    iteration = contract.integer(native.get("iteration", 0), "진행 반복")
    maximum = manifest["parameters"]["max_iterations"]
    if iteration > maximum:
        raise contract.PointKdError("네이티브 진행 반복이 입력 상한을 벗어났습니다.")
    mode = manifest["parameters"].get("scale_mode", "FIXED")
    label = "배율 함께 보정" if mode == "SIM3" else "강체 정렬"
    event = {**contract.identity(manifest), "stage": "SIM3" if mode == "SIM3" else "RIGID", "state": "RUNNING",
             "progress": min(0.99, value), "iteration": iteration, "max_iterations": maximum,
             "message": f"점 기반 {label} · 반복 {iteration}/{maximum}",
             "objective_kind": contract.registration_direction(manifest)[2]}
    if native.get("delta_4x4") is not None:
        delta = contract.moving_delta(native["delta_4x4"], reverse, mode)
        event["stable_alignment_4x4"] = (delta @ contract.matrix(manifest["initial_alignment_4x4"])).tolist()
    for name in ("correspondence_count", "current_rms"):
        if native.get(name) is not None:
            event[name] = native[name]
    return event


def _windows_child_guard(process: subprocess.Popen[Any]) -> Any:
    """부모 작업이 강제 종료되어도 네이티브 자식이 남지 않도록 Windows 작업에 묶습니다."""
    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes

    class BasicLimit(ctypes.Structure):
        """Windows 작업의 기본 제한 레이아웃을 선언합니다."""
        _fields_ = [("process_time", ctypes.c_longlong), ("job_time", ctypes.c_longlong),
                    ("flags", wintypes.DWORD), ("minimum_working_set", ctypes.c_size_t),
                    ("maximum_working_set", ctypes.c_size_t), ("active_process_limit", wintypes.DWORD),
                    ("affinity", ctypes.c_size_t), ("priority_class", wintypes.DWORD),
                    ("scheduling_class", wintypes.DWORD)]

    class IoCounters(ctypes.Structure):
        """Windows 작업의 입출력 통계 레이아웃을 선언합니다."""
        _fields_ = [(name, ctypes.c_ulonglong) for name in
                    ("read_operations", "write_operations", "other_operations", "read_bytes", "write_bytes", "other_bytes")]

    class ExtendedLimit(ctypes.Structure):
        """프로세스 종료 제한을 설정할 확장 레이아웃을 선언합니다."""
        _fields_ = [("basic", BasicLimit), ("io", IoCounters), ("process_memory", ctypes.c_size_t),
                    ("job_memory", ctypes.c_size_t), ("peak_process_memory", ctypes.c_size_t),
                    ("peak_job_memory", ctypes.c_size_t)]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    job = kernel.CreateJobObjectW(None, None)
    limits = ExtendedLimit()
    limits.basic.flags = 0x00002000
    if (not job or not kernel.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits))
            or not kernel.AssignProcessToJobObject(job, wintypes.HANDLE(int(process._handle)))):
        if job:
            kernel.CloseHandle(job)
        if process.poll() is not None:
            return None
        raise contract.PointKdError("네이티브 자식 프로세스의 종료 수명을 보호하지 못했습니다.")
    return kernel, job


def run_native(root: Path, native_worker: Path, manifest: Mapping[str, Any], request: Mapping[str, Any], reverse: bool) -> dict[str, Any]:
    """격리 프로세스를 실행하며 취소와 실제 반복 진행을 공개하고 반드시 자식을 회수합니다."""
    request_path = contract.job_path(root, "native-request.json")
    contract.write_json(request_path, request)
    progress_path = contract.job_path(root, "native-progress.json")
    last_progress = None
    guard = None
    with contract.job_path(root, "native.log").open("xb") as stream:
        process = subprocess.Popen([str(native_worker), "--request", str(request_path)], cwd=str(root),
                                   stdin=subprocess.DEVNULL, stdout=stream, stderr=subprocess.STDOUT,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        try:
            guard = _windows_child_guard(process)
            while process.poll() is None:
                if progress_path.is_file():
                    event = contract.read_json(progress_path)
                    if event != last_progress:
                        contract.write_json(contract.job_path(root, "progress.json"), progress_payload(manifest, event, reverse))
                        last_progress = event
                sleep(0.03)
            if contract.job_path(root, "cancel.request").is_file():
                raise contract.PointKdCancelled("점 기반 정렬 작업을 취소했습니다.")
            result = contract.read_json(contract.job_path(root, "native-result.json"))
            validated = contract.validate_native_result(result, request)
            if process.returncode:
                raise contract.PointKdError(f"네이티브 정렬 프로세스가 비정상 종료했습니다: {process.returncode}")
            return validated
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            if guard is not None:
                guard[0].CloseHandle(guard[1])


def run_worker(root: Path, native_worker: Path) -> None:
    """상주 원본으로 선택한 강체·Sim(3) 결과와 실제 추가 배율을 원자 공개합니다."""
    started = monotonic()
    manifest = contract.validate_manifest(contract.read_json(contract.job_path(root, "manifest.json")), root)
    if not native_worker.is_absolute() or not native_worker.is_file():
        raise contract.PointKdError("점 기반 네이티브 실행 경로가 올바르지 않습니다.")
    for name in ("result.json", "result.npz", "native-result.json"):
        if contract.job_path(root, name).exists():
            raise contract.PointKdError("이미 계산한 점 기반 작업은 다시 실행할 수 없습니다.")
    request, reverse = native_request(manifest)
    contract.write_json(contract.job_path(root, "progress.json"), {
        **contract.identity(manifest), "stage": "PREPARING", "state": "RUNNING", "iteration": 0,
        "max_iterations": manifest["parameters"]["max_iterations"], "progress": 0.0,
        "message": "전체 유효점으로 검색 구조를 준비합니다.",
    })
    preparation_started = monotonic()
    with prepared_native_request(root, request) as prepared:
        preparation_seconds = monotonic() - preparation_started
        result = run_native(root, native_worker, manifest, prepared, reverse)
    mode = manifest["parameters"].get("scale_mode", "FIXED")
    delta = contract.moving_delta(result["delta_4x4"], reverse, mode)
    final = delta @ contract.matrix(manifest["initial_alignment_4x4"])
    reasons = []
    if result["status"] == "REVIEW":
        reasons.append(result["termination_reason"])
    if result["termination_reason"] in {"MAX_ITERATIONS", "RMS_WORSENED"} and result["termination_reason"] not in reasons:
        reasons.append(result["termination_reason"])
    if result["final_rms"] > result["initial_rms"] and "RMS_WORSENED" not in reasons:
        reasons.append("RMS_WORSENED")
    quality = "REVIEW" if reasons else "PASS"
    metadata = {**contract.identity(manifest), **worker_environment(), "status": "SUCCEEDED", "quality": quality,
                "reference_model_ids": manifest["reference_model_ids"], "marker_signature": manifest["marker_signature"],
                "parameter_fingerprint": manifest["parameter_fingerprint"], "parameters": manifest["parameters"],
                "model_revision_snapshot": manifest["model_revision_snapshot"], "model_state_snapshot": manifest["model_state_snapshot"],
                "geometry_mode": manifest["geometry_mode"], "geometry_input_fingerprint": manifest["geometry"]["input_fingerprint"],
                "input_transport": manifest.get("input_transport", "FILES_V1"),
                "geometry_provenance": manifest["geometry"]["provenance"], "reverse_registration": reverse,
                "query_role": "A" if reverse else "P", "target_role": "P" if reverse else "A", "method": request["method"],
                "input_policy": "ALL_VALID_POINTS", "downsampling_performed": False, "independent_evaluation_performed": False,
                "metric_kind": "ALIGNMENT_CORRESPONDENCE_RMS", "initial_rms": result["initial_rms"], "final_rms": result["final_rms"],
                "review_reasons": reasons, "user_review_required": quality == "REVIEW", "scale_mode": mode,
                "additional_scale": contract.uniform_scale(delta) if mode == "SIM3" else 1.0,
                "solver_stop_reason": result["termination_reason"], "iterations": result["iterations"], "native_result": result,
                "native_worker_sha256": hashlib.sha256(native_worker.read_bytes()).hexdigest(),
                "input_preparation_seconds": preparation_seconds, "elapsed_seconds": monotonic() - started}
    if contract.job_path(root, "cancel.request").is_file():
        raise contract.PointKdCancelled("결과를 기록하기 전에 점 기반 정렬을 취소했습니다.")
    temporary = contract.job_path(root, "result.npz.tmp")
    with temporary.open("xb") as stream:
        np.savez(stream, delta_4x4=delta, final_alignment_4x4=final)
    contract.replace_atomic(temporary, contract.job_path(root, "result.npz"))
    if contract.job_path(root, "cancel.request").is_file():
        contract.job_path(root, "result.npz").unlink()
        raise contract.PointKdCancelled("결과를 공개하기 전에 점 기반 정렬을 취소했습니다.")
    contract.write_json(contract.job_path(root, "result.json"), metadata)
    contract.write_json(contract.job_path(root, "progress.json"), {
        **contract.identity(manifest), "stage": "COMPLETED", "state": "SUCCEEDED", "progress": 1.0,
        "iteration": result["iterations"], "max_iterations": manifest["parameters"]["max_iterations"],
        "message": "점 기반 정렬 결과를 기록했습니다.", "stable_alignment_4x4": final.tolist(),
        "level_complete": True, "quality": quality, "objective_kind": request["method"],
    })


def main(arguments: list[str] | None = None) -> int:
    """작업 또는 환경 검사를 실행하고 실패·취소 종료 파일을 같은 식별자로 기록합니다."""
    root = None
    try:
        arguments = parse_arguments(arguments)
        if arguments.check_environment:
            print(json.dumps(check_environment(arguments.native_worker), ensure_ascii=True, allow_nan=False))
            return 0
        root = Path(arguments.job_dir).resolve(strict=True)
        run_worker(root, Path(arguments.native_worker))
        return 0
    except Exception as error:
        cancelled = isinstance(error, contract.PointKdCancelled)
        status = "CANCELLED" if cancelled else "FAILED"
        if root is not None:
            try:
                manifest = contract.read_json(contract.job_path(root, "manifest.json"))
                payload = {**contract.identity(manifest), "status": status, "state": status,
                           "stage": status, "progress": 1.0, "iteration": 0, "message": str(error),
                           "error_type": type(error).__name__, "level_complete": False}
                contract.write_json(contract.job_path(root, "error.json"), payload)
                contract.write_json(contract.job_path(root, "progress.json"), payload)
            except (OSError, ValueError):
                pass
        print(json.dumps({"status": status, "message": str(error)}, ensure_ascii=True), file=sys.stderr)
        return 0 if cancelled else 1


if __name__ == "__main__":
    raise SystemExit(main())
