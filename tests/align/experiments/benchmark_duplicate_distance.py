"""실제 계산 입력의 고정 면 구간에서 중복 조회 제거 전후를 순차 비교합니다."""

from __future__ import annotations

import argparse
import gc
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import statistics
import subprocess
import sys
import threading
from time import monotonic
from types import SimpleNamespace

import numpy as np

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "tools/align/alignment"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import icp_cccore_contract as contract
import icp_cccore_solver as solver
from windows_paging import MemoryMonitor


def write_json(path, value):
    """실험 결과를 유한한 수치만 허용하는 UTF-8 JSON으로 기록합니다."""
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


def array_hash(values):
    """배열 순서와 수치를 그대로 비교할 지문을 계산합니다."""
    return hashlib.sha256(np.ascontiguousarray(values).view(np.uint8)).hexdigest()


def prepare_fixture(args):
    """제품과 같은 정규화·퇴화면 정리를 거친 뒤 고정 면과 전체 대상을 저장합니다."""
    root = args.fixture.resolve(strict=True)
    manifest = contract.read_json_object(root / "manifest.json")
    input_files = [root / manifest["geometry"][role][key]
                   for role in ("moving", "reference") for key in ("positions_file", "triangles_file")]
    before = {path.name: solver.file_sha256(path) for path in input_files}
    world = [solver._clean_mesh_geometry(contract.load_geometry(root, manifest["geometry"][role]), role)
             for role in ("moving", "reference")]
    normalization = solver.make_normalization(*(geometry.positions for geometry in world))
    normalized = [solver._clean_mesh_geometry(contract.CcCoreGeometry(
        item.kind, normalization.apply(item.positions), item.triangles, item.provenance), role,
        native_precision=True) for item, role in zip(world, ("이동", "기준"))]
    parameters = contract.resolve_parameters({"cc_max_threads": args.threads})
    policy = solver.prepare_overlap_policy(SimpleNamespace(parameters=parameters), *normalized, False,
        marker_initial_rms=manifest.get("marker_initial_rms", 0.0) / normalization.unit)
    folder = args.output / "fixture"
    folder.mkdir()
    source, target = normalized
    if args.face_count > len(source.triangles):
        raise ValueError("고정 면 구간이 실제 계산 입력보다 큽니다.")
    arrays = {"source_positions": source.positions, "source_triangles": source.triangles[:args.face_count],
              "target_positions": target.positions, "target_triangles": target.triangles}
    for name, values in arrays.items():
        np.save(folder / f"{name}.npy", values, allow_pickle=False)
    after = {path.name: solver.file_sha256(path) for path in input_files}
    if before != after:
        raise AssertionError("실험 준비 중 원본 계산 입력이 변경되었습니다.")
    info = {"input_hashes": before, "normalized_hashes": {name: array_hash(values) for name, values in arrays.items()},
            "source_face_range_after_native_cleanup": [0, args.face_count],
            "source_total_face_count_after_native_cleanup": len(source.triangles),
            "target_full_face_count": len(target.triangles), "policy": policy,
            "normalization": normalization.to_mapping(), "parameters": parameters}
    write_json(folder / "info.json", info)
    return info


def load_overlap(path):
    """지정한 이전 또는 변경 모듈을 격리된 이름으로 불러옵니다."""
    spec = importlib.util.spec_from_file_location("measured_overlap", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def run_once(args):
    """새 프로세스·새 네이티브 세션에서 후보 준비와 선택적 표본 회귀를 계측합니다."""
    module = load_overlap(args.module.resolve(strict=True))
    info = json.loads((args.output / "fixture/info.json").read_text(encoding="utf-8"))
    arrays = {name: np.load(args.output / "fixture" / f"{name}.npy", allow_pickle=False)
              for name in ("source_positions", "source_triangles", "target_positions", "target_triangles")}
    before = {name: array_hash(values) for name, values in arrays.items()}
    source = contract.CcCoreGeometry("TRIANGLE_MESH", arrays["source_positions"], arrays["source_triangles"], {})
    target = arrays["target_positions"]
    bounds = (target.min(axis=0), target.max(axis=0))
    directory = args.output / args.run_name
    directory.mkdir()
    parameters = info["parameters"]
    memory, stopped = MemoryMonitor(), threading.Event()
    memory_errors = []

    def observe_memory():
        """계측 프로세스와 자식의 메모리를 종료 신호까지 관측합니다."""
        try:
            while not stopped.is_set():
                memory.sample(os.getpid())
                stopped.wait(0.1)
        except Exception as error:
            memory_errors.append(str(error))

    monitor = threading.Thread(target=observe_memory, daemon=True)
    monitor.start()
    try:
        with solver.NativeRunner(directory, args.native_worker.resolve(strict=True), parameters) as runner:
            model = runner.geometry("reference-model", target, arrays["target_triangles"])
            sizes = []

            def query(points):
                """제품과 같은 준비 명령으로 조회하고 실제 묶음 크기를 기록합니다."""
                if len(points) > 8192 or runner.counter >= contract.CC_MAX_PREPARING_OPERATIONS:
                    raise AssertionError("기존 조회 묶음 또는 준비 명령 상한을 초과했습니다.")
                sizes.append(len(points))
                data = runner.geometry(f"candidate-query-{runner.counter + 1}", points, cache_scope="COMMAND")
                return runner.call("distance", data, model, stage="PREPARING")["distances"]

            started = monotonic()
            domain = module.build_overlap_domain(source, bounds, info["policy"]["candidate_radius_normalized"], query,
                batch_size=parameters["candidate_probe_batch_size"], max_subdivision_depth=parameters["candidate_max_depth"],
                max_candidate_patches=parameters["candidate_max_patch_points"])
            seconds = monotonic() - started
            report = {"run": args.run_name, "domain_seconds": seconds, "query_calls": len(sizes),
                      "query_points": sum(sizes), "batch_sizes": list(sizes), "metadata": domain.metadata,
                      "domain_hashes": {name: array_hash(getattr(domain, name))
                                        for name in ("patches", "source_indices", "patch_areas")},
                      "bool_itemsize": np.dtype(np.bool_).itemsize,
                      "classification_array_bytes": domain.metadata["initial_candidate_face_count"] * np.dtype(np.bool_).itemsize,
                      "module_sha256": solver.file_sha256(args.module),
                      "native_sha256": runner.native_worker_sha256, "samples": {}}
            if args.samples:
                for label, count, seed in (("evaluation", parameters["evaluation_sample_count"], contract.CC_EVALUATION_SEED),
                                           ("optimizer", parameters["mesh_sample_count"], contract.CC_OPTIMIZER_SEED)):
                    sample = module.sample_overlap_domain(domain, count, seed, query,
                        radius=info["policy"]["correspondence_radius_normalized"],
                        max_proposals_factor=parameters["candidate_proposal_limit"])
                    report["samples"][label] = {"fingerprint": sample.fingerprint, "metadata": sample.metadata}
            report["all_query_calls"] = len(sizes)
            report["physical_calls"] = len(runner.physical_native_calls)
            stopped.set()
            monitor.join()
            memory.sample(os.getpid())
    finally:
        stopped.set()
        monitor.join()
    report["memory"] = memory.report()
    if memory_errors or report["memory"]["read_errors"] or not any(
        item["name"] == "colmap_cccore_worker.exe" for item in report["memory"]["processes"].values()
    ):
        raise AssertionError(f"메모리 관측이 완전하지 않습니다: {memory_errors}, {report['memory']}")
    report["memory_verified"] = True
    report["input_unchanged"] = before == {name: array_hash(values) for name, values in arrays.items()}
    if not report["input_unchanged"] or before != info["normalized_hashes"]:
        raise AssertionError("비교 입력 지문이 달라졌습니다.")
    write_json(directory / "result.json", report)
    print(json.dumps({"실행": args.run_name, "후보준비_초": seconds, "호출": report["query_calls"]}, ensure_ascii=False), flush=True)


def main():
    """예열 각 한 번과 순차 측정 세 번을 분리하고 결과 보존·속도 조건을 판정합니다."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--native-worker", type=Path, default=ROOT / ".cccore-runtime/cccore/colmap_cccore_worker.exe")
    parser.add_argument("--threads", type=int, default=31)
    parser.add_argument("--face-count", type=int, default=65536)
    parser.add_argument("--run-name")
    parser.add_argument("--module", type=Path)
    parser.add_argument("--samples", action="store_true")
    args = parser.parse_args()
    args.output = args.output.resolve()
    if args.run_name:
        run_once(args)
        return
    if not args.fixture or not args.baseline or args.face_count < 1 or not 1 <= args.threads <= 1024:
        parser.error("실제 입력, 기존 모듈, 양의 면 수와 1~1024 스레드가 필요합니다.")
    args.output.mkdir(parents=True, exist_ok=False)
    info = prepare_fixture(args)
    gc.collect()
    modules = {"baseline": args.baseline.resolve(strict=True), "candidate": ROOT / "tools/align/alignment/icp_cccore_overlap.py"}
    results = {}
    for index in range(4):
        for label, module in modules.items():
            name = f"{label}-{'warmup' if index == 0 else index}"
            command = [sys.executable, "-X", "utf8", str(Path(__file__).resolve()), "--output", str(args.output),
                       "--native-worker", str(args.native_worker.resolve()), "--run-name", name, "--module", str(module)]
            if index == 0:
                command.append("--samples")
            subprocess.run(command, check=True)
            results[name] = json.loads((args.output / name / "result.json").read_text(encoding="utf-8"))
    expected = results["baseline-warmup"]
    equality = all(row[key] == expected[key] for row in results.values() for key in ("metadata", "domain_hashes"))
    samples_equal = expected["samples"] == results["candidate-warmup"]["samples"]
    removed_points = expected["metadata"]["initial_candidate_face_count"]
    removed_calls = math.ceil(removed_points / 8192)
    counts_equal = all(results[f"baseline-{index}"]["query_points"] - results[f"candidate-{index}"]["query_points"] == removed_points
                       and results[f"baseline-{index}"]["query_calls"] - results[f"candidate-{index}"]["query_calls"] == removed_calls
                       for index in ("warmup", "1", "2", "3"))
    medians = {label: statistics.median(results[f"{label}-{index}"]["domain_seconds"] for index in range(1, 4)) for label in modules}
    report = {"fixture": info, "results": results, "domain_equal": equality, "samples_equal": samples_equal,
              "expected_removed_points": removed_points, "expected_removed_calls": removed_calls, "counts_equal": counts_equal,
              "median_seconds": medians, "reduction_percent": 100 * (1 - medians["candidate"] / medians["baseline"]),
              "passed": equality and samples_equal and counts_equal and medians["candidate"] < medians["baseline"]}
    write_json(args.output / "comparison.json", report)
    if not report["passed"]:
        raise AssertionError("결과 보존·조회 절감·준비 시간 개선 조건을 충족하지 못했습니다.")
    print(json.dumps({"중앙값_초": medians, "절감률": report["reduction_percent"]}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
