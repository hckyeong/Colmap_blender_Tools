"""동일 실제 메시의 BVH 최초 생성과 재사용 검색을 두 실행 파일에서 교차 측정합니다."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import statistics
import subprocess
import time

import numpy as np


def read_json(path):
    """보존 입력과 실행 응답을 UTF-8 JSON으로 읽습니다."""
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path, value):
    """완성된 JSON만 보이도록 같은 폴더에서 원자적으로 교체합니다."""
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    temporary.replace(path)


def wait_response(process, path, timeout=90):
    """워커 종료와 응답 시간 상한을 함께 검사합니다."""
    deadline = time.perf_counter() + timeout
    while not path.is_file():
        if process.poll() is not None or time.perf_counter() >= deadline:
            raise RuntimeError(f"워커 응답 실패: {path}, 종료 코드={process.poll()}")
        time.sleep(0.005)
    return read_json(path)


def run_session(worker, preserved, output, threads):
    """새 세션에서 한 트리를 생성하고 같은 다섯 조회를 수행하여 모든 거리를 비교합니다."""
    output.mkdir(parents=True, exist_ok=False)
    (output / "native").mkdir()
    identity = dict(protocol_version="cccore-native-v7", session_id="bvh-build-comparison",
                    job_id=output.name, input_fingerprint="preserved-real-mesh")
    commands = []
    for sequence in range(1, 6):
        command = read_json(preserved / f"native/command-{sequence:04d}.accepted.json")
        command.update(identity)
        command["parameters"]["max_threads"] = threads
        for descriptor in (command["data"], command["model"]):
            for key in ("positions_file", "triangles_file"):
                if key not in descriptor:
                    continue
                source, target = preserved / descriptor[key], output / descriptor[key]
                if not source.resolve().is_relative_to(preserved.resolve()) or not target.resolve().is_relative_to(output.resolve()):
                    raise ValueError("보존 입력의 상대 경로가 작업 폴더를 벗어납니다.")
                if not target.exists():
                    shutil.copy2(source, target)
        commands.append(command)
    start = dict(identity, parameters={"max_threads": threads}, ready_file="native/session-ready.json",
                 result_file="native/session-result.json", cancel_file="cancel.request", command_directory="native")
    request = output / "native-session-request.json"
    write_json(request, start)
    with (output / "worker.log").open("w", encoding="utf-8") as log:
        process = subprocess.Popen([str(worker), "--session", str(request)], stdout=log,
                                   stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW)
        rows = []
        try:
            wait_response(process, output / start["ready_file"])
            for command in commands:
                sequence = command["sequence"]
                write_json(output / f"native/command-{sequence:04d}.request.json", command)
                response = wait_response(process, output / command["response_file"])
                if response["status"] != "completed":
                    raise RuntimeError(response)
                actual = np.fromfile(output / command["distances_file"], dtype="<f8")
                expected = np.fromfile(preserved / command["distances_file"], dtype="<f8")
                if actual.shape != expected.shape or not np.isfinite(actual).all():
                    raise AssertionError("거리 출력 개수 또는 유한성 위반")
                difference = float(np.max(np.abs(actual - expected)))
                if difference > 2e-7:
                    raise AssertionError(f"기존 거리 허용치 초과: {difference}")
                rows.append(dict(response=response, max_distance_difference=difference,
                                 exact_distances=bool(np.array_equal(actual, expected)),
                                 distance_sha256=hashlib.sha256(actual.tobytes()).hexdigest()))
            close = dict(identity, sequence=6, operation="close", stage="CLOSE",
                         response_file="native/command-0006.response.json", progress_file="native/command-0006.progress.json")
            write_json(output / "native/command-0006.request.json", close)
            session = wait_response(process, output / close["response_file"])
            if process.wait(timeout=15) != 0 or session["status"] != "closed":
                raise AssertionError("세션 정상 종료 실패")
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=15)
    report = dict(rows=rows, session=session,
                  build_seconds=sum(row["response"]["timings"]["bvh_build_seconds"] for row in rows),
                  search_seconds=sum(row["response"]["timings"]["kernel_seconds"] for row in rows),
                  exact_distances=all(row["exact_distances"] for row in rows))
    write_json(output / "result.json", report)
    return report


def main():
    """예열 한 회와 반복 세 회의 생성·검색 중앙값을 원문 및 실행 파일 지문과 기록합니다."""
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("baseline", "candidate", "preserved", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--threads", type=int, default=31)
    parser.add_argument("--repeats", type=int, default=3)
    args = parser.parse_args()
    if not 1 <= args.repeats <= 5 or not 1 <= args.threads <= 256:
        raise ValueError("반복 수 또는 스레드 상한이 범위를 벗어났습니다.")
    args.output = args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=False)
    workers = {name: getattr(args, name).resolve(strict=True) for name in ("baseline", "candidate")}
    results = {name: [] for name in workers}
    write_json(args.output / "configuration.json", dict(threads=args.threads, warmups=1, repeats=args.repeats,
        preserved=str(args.preserved.resolve()), workers={name: dict(path=str(path),
        sha256=hashlib.sha256(path.read_bytes()).hexdigest()) for name, path in workers.items()}))
    for run in range(args.repeats + 1):
        order = ("baseline", "candidate") if run % 2 == 0 else ("candidate", "baseline")
        for name in order:
            report = run_session(workers[name], args.preserved.resolve(strict=True), args.output / f"{name}-{run}", args.threads)
            results[name].append(report)
            print(json.dumps(dict(worker=name, run=run, build_seconds=report["build_seconds"],
                                  search_seconds=report["search_seconds"], exact_distances=report["exact_distances"])), flush=True)
    summary = {name: dict(build_seconds=statistics.median(item["build_seconds"] for item in series[1:]),
        search_seconds=statistics.median(item["search_seconds"] for item in series[1:]),
        exact_distances=all(item["exact_distances"] for item in series)) for name, series in results.items()}
    summary["build_time_reduction"] = 1 - summary["candidate"]["build_seconds"] / summary["baseline"]["build_seconds"]
    write_json(args.output / "comparison.json", summary)
    print(json.dumps(summary), flush=True)


if __name__ == "__main__":
    main()
