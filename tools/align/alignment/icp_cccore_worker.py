"""Blender를 불러오지 않는 CPython·NumPy CC 작업 실행 진입점입니다."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import platform
import struct
import subprocess
import sys
from time import monotonic
from typing import Any, Mapping

# 직접 실행 시 이 디렉터리의 순수 모듈만 로드하여 애드온 초기화를 피합니다.
if __package__:
    from . import icp_cccore_contract as contract
    from . import icp_cccore_solver as solver
else:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import icp_cccore_contract as contract
    import icp_cccore_solver as solver


def parse_worker_arguments(arguments: list[str] | None = None) -> argparse.Namespace:
    """작업 실행 또는 환경 검사와 명시적 네이티브 실행 경로를 해석합니다."""
    parser = argparse.ArgumentParser(prog="colmap_align_cccore_worker")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--job-dir")
    group.add_argument("--check-environment", action="store_true")
    parser.add_argument("--native-worker", required=True)
    raw = list(sys.argv[1:] if arguments is None else arguments)
    if "--" in raw:
        raw = raw[raw.index("--") + 1:]
    return parser.parse_args(raw)


def worker_environment() -> dict[str, Any]:
    """순수 Python 실행 환경의 버전과 프로세스 구조를 반환합니다."""
    import numpy as np
    return {
        "python_implementation": platform.python_implementation(),
        "worker_python_version": platform.python_version(),
        "worker_numpy_version": np.__version__,
        "platform_architecture": "64bit" if struct.calcsize("P") == 8 else "32bit",
    }


def check_cccore_environment(native_worker: Path | str) -> dict[str, Any]:
    """실제 네이티브 실행 파일의 프로토콜과 고정 코어 버전을 검사합니다."""
    payload = {**worker_environment(), **contract.identity_payload({}), "errors": []}
    errors = payload["errors"]
    path = Path(native_worker)
    if payload["python_implementation"] != "CPython" or payload["platform_architecture"] != "64bit":
        errors.append("CC 계산 환경은 64비트 CPython이어야 합니다.")
    if not path.is_absolute() or not path.is_file():
        errors.append("CC 네이티브 실행 파일의 절대 경로가 올바르지 않습니다.")
    if not errors:
        try:
            completed = subprocess.run(
                [str(path), "--check-environment"], stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30.0,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                check=False, encoding="utf-8",
            )
            native = json.loads(completed.stdout)
            if not isinstance(native, dict):
                raise ValueError("CC 환경 결과가 객체가 아닙니다.")
            payload["native_environment"] = native
            payload["core_commit"] = native.get("core_commit", "")
            if completed.returncode != 0 or not payload["core_commit"] or native.get("status") != "completed" or native.get("self_test") != "passed":
                errors.append("CC 네이티브 환경 검사에 실패했습니다.")
            if native.get("protocol_version") != contract.CC_NATIVE_PROTOCOL_VERSION:
                errors.append("CC 네이티브 프로토콜 버전이 올바르지 않습니다.")
        except (OSError, ValueError, subprocess.TimeoutExpired) as error:
            errors.append(f"CC 네이티브 환경을 확인하지 못했습니다: {error}")
    payload["ok"] = not errors
    return payload


def run_cccore_worker(job_directory: Path | str, native_worker: Path | str) -> None:
    """완성된 NPZ 뒤에 결과 JSON과 종료 진행률을 원자적으로 공개합니다."""
    root = Path(job_directory).resolve(strict=True)
    if not root.is_dir():
        raise contract.CcCoreInputError("CC 작업 경로는 디렉터리여야 합니다.")
    manifest = contract.validate_manifest(contract.read_json_object(contract.job_path(root, "manifest.json")), root)
    if contract.job_path(root, "result.json").exists() or contract.job_path(root, "result.npz").exists():
        raise contract.CcCoreInputError("이미 결과가 기록된 CC 작업은 다시 실행할 수 없습니다.")
    environment = worker_environment()
    if environment["python_implementation"] != "CPython" or environment["platform_architecture"] != "64bit":
        raise contract.CcCoreEnvironmentError("CC 작업은 64비트 CPython에서 실행해야 합니다.")

    def progress_sink(event: Mapping[str, Any]) -> None:
        """진행률에 실행 환경을 덧붙여 하나의 원자적 파일로 기록합니다."""
        contract.write_json_atomic(contract.job_path(root, "progress.json"), {**event, **environment})

    result = solver.solve_cccore_job(root, native_worker, progress_sink=progress_sink)
    if contract.job_path(root, "cancel.request").is_file():
        raise contract.CcCoreCancelled("CC 결과 기록 전에 작업을 취소했습니다.")
    metadata = {**result.metadata, **environment}
    contract.write_arrays_atomic(contract.job_path(root, "result.npz"), result.result_arrays())
    if contract.job_path(root, "cancel.request").is_file():
        contract.job_path(root, "result.npz").unlink()
        raise contract.CcCoreCancelled("CC 결과 기록 전에 작업을 취소했습니다.")
    contract.write_json_atomic(contract.job_path(root, "result.json"), metadata)
    progress_sink({
        **contract.identity_payload(manifest), "stage": "COMPLETED", "state": "SUCCEEDED",
        "progress": 1.0, "message": "CC 정밀 정렬 결과를 기록했습니다.",
        "elapsed_seconds": metadata["elapsed_seconds"], "level_complete": True,
        "stable_alignment_4x4": result.final_alignment_4x4.tolist(),
        "quality": metadata["quality"],
    })


def write_worker_failure(job_directory: Path | None, error: Exception, elapsed: float) -> None:
    """취소와 계산 오류를 구분하고 동일한 식별자의 종료 파일을 기록합니다."""
    if job_directory is None:
        return
    try:
        manifest = contract.read_json_object(contract.job_path(job_directory, "manifest.json"))
    except contract.CcCoreError:
        manifest = {}
    cancelled = isinstance(error, contract.CcCoreCancelled)
    status = "CANCELLED" if cancelled else "FAILED"
    payload = {
        **contract.identity_payload(manifest), **worker_environment(),
        "status": status, "state": status, "kind": type(error).__name__,
        "error_type": type(error).__name__, "message": str(error) or "CC 계산에 실패했습니다.",
        "elapsed_seconds": elapsed,
    }
    contract.write_json_atomic(contract.job_path(job_directory, "error.json"), payload)
    contract.write_json_atomic(contract.job_path(job_directory, "progress.json"), {
        **payload, "stage": status, "progress": 1.0, "level_complete": False,
    })


def main(arguments: list[str] | None = None) -> int:
    """환경 검사 또는 작업 실행을 수행하고 구조화된 종료 상태를 남깁니다."""
    root = None
    started = monotonic()
    try:
        namespace = parse_worker_arguments(arguments)
        if namespace.check_environment:
            payload = check_cccore_environment(namespace.native_worker)
            print(json.dumps(payload, ensure_ascii=True, allow_nan=False, sort_keys=True))
            return 0 if payload["ok"] else 2
        root = Path(namespace.job_dir).resolve(strict=True)
        run_cccore_worker(root, namespace.native_worker)
        return 0
    except Exception as error:
        try:
            write_worker_failure(root, error, float(monotonic() - started))
        except (OSError, contract.CcCoreError):
            pass
        # 콘솔 코드페이지에 관계없이 JSON 이스케이프로 오류를 전달합니다.
        status = "CANCELLED" if isinstance(error, contract.CcCoreCancelled) else "FAILED"
        print(json.dumps({"status": status, "message": str(error)}, ensure_ascii=True), file=sys.stderr)
        return 0 if isinstance(error, contract.CcCoreCancelled) else 1


if __name__ == "__main__":
    raise SystemExit(main())
