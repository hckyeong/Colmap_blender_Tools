"""원본 입력을 재사용하여 점 KD 워커의 1·31 스레드 실행을 교차 측정합니다."""
from __future__ import annotations

import argparse
import copy
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import statistics
import subprocess
import time
from uuid import uuid4


ROOT = Path(__file__).resolve().parents[3]
DEFAULT_WORKER = ROOT / ".point-kd-runtime/point_kd/colmap_point_kd_worker.exe"
DEFAULT_REQUEST = ROOT / "build/verification/point-kd-20261021/baseline/native-strict-request.json"
PROTOCOL = "point-kd-native-v1"


def read_json(path: Path) -> dict:
    """UTF-8 서명 유무와 관계없이 저장한 JSON 객체를 읽습니다."""
    with path.open("r", encoding="utf-8-sig") as stream:
        value = json.load(stream)
    if not isinstance(value, dict):
        raise ValueError(f"JSON 객체가 필요합니다: {path}")
    return value


def write_json_exclusive(path: Path, value: dict) -> None:
    """기존 파일을 덮어쓰지 않고 새 UTF-8 JSON만 기록합니다."""
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")


def file_sha256(path: Path) -> str:
    """실제로 실행할 실행 파일의 SHA-256을 청크 단위로 계산합니다."""
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def input_metadata(request: dict, directory: Path) -> list[dict]:
    """작업 폴더 안의 원본 바이너리만 확인하고 읽기 전용 재사용 정보를 수집합니다."""
    inputs = []
    for side in ("source", "target"):
        descriptor = request[side]
        for field in ("positions_file", "normals_file", "triangles_file"):
            if field not in descriptor:
                continue
            path = (directory / descriptor[field]).resolve()
            if path == directory or not path.is_relative_to(directory) or not path.is_file():
                raise ValueError(f"작업 폴더 안의 유효 원본 파일이 필요합니다: {side}.{field}")
            stat = path.stat()
            inputs.append({"side": side, "field": field, "path": str(path), "size_bytes": stat.st_size, "mtime_ns": stat.st_mtime_ns})
    return inputs


def schedule(threads: list[int], warmups: int, repeats: int) -> list[dict]:
    """준비 실행 후 각 측정 회차에서 스레드 순서를 번갈아 배치합니다."""
    runs = []
    for phase, count in (("준비", warmups), ("측정", repeats)):
        for index in range(count):
            order = threads if index % 2 == 0 else list(reversed(threads))
            for thread_count in order:
                runs.append({"phase": phase, "round": index + 1, "threads": thread_count})
    return runs


def run_once(worker: Path, original: dict, directory: Path, prefix: str, item: dict, timeout: float) -> dict:
    """고유한 요청·출력 이름으로 실제 워커 한 번을 실행하고 세부 결과를 보존합니다."""
    suffix = f"{prefix}-{'warmup' if item['phase'] == '준비' else 'measure'}-{item['round']:02d}-t{item['threads']}"
    paths = {"request": directory / f"{suffix}-request.json", "result": directory / f"{suffix}-result.json", "progress": directory / f"{suffix}-progress.json", "cancel": directory / f"{suffix}.cancel"}
    if any(path.exists() for path in paths.values()):
        raise FileExistsError("시험 경로가 이미 존재합니다. 기존 결과는 덮어쓰지 않습니다.")
    request = copy.deepcopy(original)
    request.setdefault("parameters", {})["max_threads"] = item["threads"]
    for field in ("result", "progress", "cancel"):
        request[f"{field}_file"] = paths[field].name
    write_json_exclusive(paths["request"], request)
    record = {**item, "paths": {name: str(path) for name, path in paths.items()}}
    started = time.perf_counter()
    try:
        completed = subprocess.run(
            [str(worker), "--request", str(paths["request"])],
            capture_output=True, encoding="utf-8", errors="strict", timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        record["wall_seconds"] = time.perf_counter() - started
        record["returncode"] = completed.returncode
        if completed.stderr:
            record["stderr"] = completed.stderr
        result = read_json(paths["result"]) if paths["result"].is_file() else json.loads(completed.stdout)
        if result.get("protocol_version") != PROTOCOL:
            raise ValueError("실행 결과의 네이티브 프로토콜이 다릅니다.")
        matrix = result.get("delta_4x4")
        if not isinstance(matrix, list) or len(matrix) != 4 or any(not isinstance(row, list) or len(row) != 4 or any(not isinstance(value, (int, float)) or not math.isfinite(value) for value in row) for row in matrix):
            raise ValueError("실행 결과에 유한한 4×4 행렬이 없습니다.")
        record["native_result"] = result
        record["native_timing_seconds"] = result.get("timing_seconds", {})
        record["delta_4x4"] = matrix
        for field in ("status", "iterations", "termination_reason", "initial_rms", "final_rms", "initial_objective_rms", "final_objective_rms", "correspondence_count"):
            record[field] = result.get(field)
        record["accepted"] = completed.returncode == 0 and result.get("status") in ("SUCCESS", "REVIEW")
        if not record["accepted"]:
            record["error"] = result.get("message", "네이티브 정렬이 정상 결과를 반환하지 않았습니다.")
    except Exception as error:
        record["wall_seconds"] = time.perf_counter() - started
        record["accepted"] = False
        record["error"] = str(error)
    return record


def compare_results(first: dict, second: dict) -> dict:
    """두 실행의 행렬·회전·이동·종료 상태와 오차 차이를 수치로 비교합니다."""
    a, b = first["delta_4x4"], second["delta_4x4"]
    differences = [abs(a[row][column] - b[row][column]) for row in range(4) for column in range(4)]
    rotation_trace = sum(a[row][column] * b[row][column] for row in range(3) for column in range(3))
    cosine = max(-1.0, min(1.0, (rotation_trace - 1.0) / 2.0))
    value = {
        "first_threads": first["threads"], "second_threads": second["threads"],
        "first_round": first["round"], "second_round": second["round"],
        "matrix_exact_match": a == b,
        "maximum_matrix_absolute_difference": max(differences),
        "matrix_frobenius_difference": math.sqrt(sum(number * number for number in differences)),
        "translation_difference": math.sqrt(sum((a[row][3] - b[row][3]) ** 2 for row in range(3))),
        "rotation_difference_radians": 0.0 if a == b else math.acos(cosine),
        "iterations_equal": first["iterations"] == second["iterations"],
        "termination_equal": first["termination_reason"] == second["termination_reason"],
        "status_equal": first["status"] == second["status"],
        "correspondence_count_equal": first["correspondence_count"] == second["correspondence_count"],
    }
    for field in ("final_rms", "final_objective_rms"):
        if isinstance(first[field], (int, float)) and isinstance(second[field], (int, float)):
            value[f"{field}_absolute_difference"] = abs(first[field] - second[field])
    return value


def summarize(runs: list[dict], threads: list[int]) -> dict:
    """준비 실행을 제외한 시간 분포와 회차별 두 스레드 결과 차이를 정리합니다."""
    measured = [run for run in runs if run["phase"] == "측정" and run.get("accepted")]
    summaries = {}
    for count in threads:
        selected = [run for run in measured if run["threads"] == count]
        if not selected:
            continue
        walls = [run["wall_seconds"] for run in selected]
        stages = sorted({stage for run in selected for stage in run["native_timing_seconds"]})
        summaries[str(count)] = {
            "measured_runs": len(selected), "wall_seconds": walls,
            "wall_median_seconds": statistics.median(walls), "wall_mean_seconds": statistics.mean(walls),
            "wall_min_seconds": min(walls), "wall_max_seconds": max(walls),
            "native_stage_median_seconds": {stage: statistics.median(run["native_timing_seconds"][stage] for run in selected if stage in run["native_timing_seconds"]) for stage in stages},
            "repeat_differences": [compare_results(selected[0], run) for run in selected[1:]],
        }
    cross = []
    for index in sorted({run["round"] for run in measured}):
        pair = [next((run for run in measured if run["round"] == index and run["threads"] == count), None) for count in threads]
        if all(run is not None for run in pair):
            cross.append(compare_results(pair[0], pair[1]))
    result = {"by_threads": summaries, "cross_thread_differences": cross}
    if all(str(count) in summaries for count in threads):
        denominator = summaries[str(threads[1])]["wall_median_seconds"]
        result["median_wall_speedup_first_over_second"] = summaries[str(threads[0])]["wall_median_seconds"] / denominator if denominator else None
    return result


def main() -> int:
    """인수를 검증하고 고유한 시험 보고서에 모든 실행과 비교 결과를 기록합니다."""
    parser = argparse.ArgumentParser(description="점 KD 워커의 1·31 스레드를 준비 1회·측정 3회씩 교차 비교합니다. 원본 입력과 기존 결과를 덮어쓰지 않습니다.", add_help=False)
    parser.add_argument("-h", "--help", action="help", help="도움말을 표시하고 종료합니다.")
    parser.add_argument("--worker", type=Path, default=DEFAULT_WORKER, help="실행할 네이티브 워커 경로")
    parser.add_argument("--request", type=Path, default=DEFAULT_REQUEST, help="읽기 전용으로 재사용할 기준 요청 JSON")
    parser.add_argument("--threads", type=int, nargs=2, default=[1, 31], metavar=("단일", "병렬"), help="비교할 두 CPU 스레드 수, 기본값 1 31")
    parser.add_argument("--warmups", type=int, default=1, help="각 스레드의 준비 실행 횟수, 기본값 1")
    parser.add_argument("--repeats", type=int, default=3, help="각 스레드의 실제 측정 횟수, 기본값 3")
    parser.add_argument("--timeout", type=float, default=600.0, help="한 번의 실행 제한 시간(초), 기본값 600")
    parser.add_argument("--report", type=Path, help="새 보고서 경로, 생략하면 기준 폴더의 고유 이름 사용")
    args = parser.parse_args()
    if args.warmups < 0 or args.repeats < 1 or args.timeout <= 0 or not math.isfinite(args.timeout) or len(set(args.threads)) != 2 or any(count < 1 or count > 1024 for count in args.threads):
        parser.error("준비 횟수는 0 이상, 측정 횟수는 1 이상, 시간은 양수, 서로 다른 스레드 수는 1~1024여야 합니다.")
    worker, request_path = args.worker.resolve(), args.request.resolve()
    if not worker.is_file() or not request_path.is_file():
        parser.error("실행 파일과 기준 요청 JSON이 모두 존재해야 합니다.")
    original = read_json(request_path)
    if original.get("protocol_version") != PROTOCOL:
        parser.error("기준 요청의 네이티브 프로토콜이 다릅니다.")
    directory = request_path.parent
    inputs_before = input_metadata(original, directory)
    prefix = f"native-thread-benchmark-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid4().hex}"
    report_path = args.report.resolve() if args.report else directory / f"{prefix}-report.json"
    if report_path.exists() or not report_path.parent.is_dir():
        parser.error("보고서는 기존 파일과 겹치지 않는 존재하는 폴더에 저장해야 합니다.")
    order = schedule(args.threads, args.warmups, args.repeats)
    digest = file_sha256(worker)
    report = {
        "schema": "POINT_KD_THREAD_BENCHMARK_V1", "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "worker": str(worker), "worker_sha256": digest,
        "baseline_request": str(request_path), "baseline_request_sha256": file_sha256(request_path),
        "baseline_parameters": original.get("parameters", {}),
        "source": original.get("source"), "target": original.get("target"), "method": original.get("method"),
        "threads": args.threads, "warmups_per_thread": args.warmups, "measurements_per_thread": args.repeats,
        "order": order, "read_only_reused_inputs": inputs_before, "runs": [], "success": False,
    }
    started = time.perf_counter()
    try:
        for item in order:
            record = run_once(worker, original, directory, prefix, item, args.timeout)
            report["runs"].append(record)
            print(f"{item['phase']} {item['round']}회 · 스레드 {item['threads']}개 · {record['wall_seconds']:.6f}초 · {record.get('status', '실패')}/{record.get('termination_reason', record.get('error', ''))}", flush=True)
            if not record["accepted"]:
                raise RuntimeError(record["error"])
        report["summary"] = summarize(report["runs"], args.threads)
        report["worker_sha256_after"] = file_sha256(worker)
        report["inputs_unchanged"] = input_metadata(original, directory) == inputs_before
        report["baseline_request_unchanged"] = file_sha256(request_path) == report["baseline_request_sha256"]
        if report["worker_sha256_after"] != digest or not report["inputs_unchanged"] or not report["baseline_request_unchanged"]:
            raise RuntimeError("측정 중 실행 파일 또는 기준 입력이 변경되었습니다.")
        report["success"] = True
    except Exception as error:
        report["error"] = str(error)
        report["summary"] = summarize(report["runs"], args.threads)
    finally:
        report["total_wall_seconds"] = time.perf_counter() - started
        write_json_exclusive(report_path, report)
    print(f"보고서: {report_path}", flush=True)
    return 0 if report["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
