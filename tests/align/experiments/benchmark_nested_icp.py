"""제품 정책을 바꾸지 않고 중첩 5천·2만·5만 표본의 연속 ICP 비용을 측정합니다."""

import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

import numpy as np

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(ROOT / "tools/align/alignment"))
from benchmark_fixed_domain_preparation import array_hash, read_json
from windows_paging import MemoryMonitor
import icp_cccore_contract as contract
import icp_cccore_solver as solver


def file_hash(path):
    """대형 모델을 추가 메모리에 적재하지 않고 파일 지문을 계산합니다."""
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def write_json(path, value):
    """배열을 명시적으로 변환한 유한 JSON 보고서를 UTF-8로 저장합니다."""
    path.write_text(json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2), encoding="utf-8")


def transformed(points, matrix):
    """매 단계 원본 표본에 누적 유사변환을 정확히 한 번 적용합니다."""
    return np.ascontiguousarray(points @ matrix[:3, :3].T + matrix[:3, 3], dtype="<f8")


def workspace_path(path, *, existing=True):
    """파일 복사와 실행 출력 경로를 실제 작업공간 안으로 제한합니다."""
    resolved = path.resolve(strict=existing)
    if not resolved.is_relative_to(ROOT.resolve()) or resolved == ROOT.resolve():
        raise ValueError("검증 경로는 프로젝트 작업공간 내부여야 합니다.")
    return resolved


def input_file(root, relative):
    """기준 명령의 파일이 기준 작업 폴더를 벗어나지 않는지 확인합니다."""
    return contract.job_path(root, relative)


def validate_inputs(args):
    """기준 등록 입력·중첩 원본 행·시드·정규화·배율 시작 행렬의 연결을 확인합니다."""
    baseline = workspace_path(args.baseline_job)
    request_path = input_file(baseline, "native/command-0001.accepted.json")
    request = read_json(request_path)
    source_report = read_json(baseline.parent / "result.json")
    selection = read_json(args.samples.parent / "selection-report.json")
    preserved = read_json(args.preserved_result)
    if request["operation"] != "register" or request["parameters"]["adjust_scale"] is not (args.stage == "scale_refine"):
        raise ValueError("기준 등록 명령과 선택한 강체·배율 단계가 다릅니다.")
    if source_report["stage"] != args.stage or source_report["input_fingerprint"] != selection["input_fingerprint"] or preserved["input_fingerprint"] != selection["input_fingerprint"]:
        raise ValueError("기준 실행·보존 결과·중첩 표본의 단계 또는 입력 지문이 다릅니다.")
    if source_report["normalization"] != selection["normalization"] or preserved["normalization"] != selection["normalization"]:
        raise ValueError("정규화가 동일하지 않습니다.")
    if source_report["seed_parameters"] != selection["seeds"]:
        raise ValueError("기준 실행과 중첩 선택의 원본 시드가 다릅니다.")
    initial = np.eye(4)
    if args.stage == "scale_refine":
        initial = np.asarray(next(stage for stage in preserved["registration_stages"] if stage["stage"] == "rigid")["matrix_4x4"], dtype=float)
    contract.matrix_sim3(initial, "검증 시작 변환")
    if not np.array_equal(initial, np.asarray(source_report["initial_native_matrix_4x4"])):
        raise ValueError("기준 입력에 적용된 보존 강체 변환이 다릅니다.")
    with np.load(args.samples, allow_pickle=False) as archive:
        stages = {count: (archive[f"registration_{count}"].copy(), archive[f"source_rows_{count}"].copy())
                  for count in (5000, 20000, 50000)}
    full = stages[50000][0]
    if array_hash(full) != selection["source_sample_fingerprints"]["registration"] or array_hash(full) != source_report["sample_fingerprint"]:
        raise ValueError("5만 등록 표본이 보존 원본과 다릅니다.")
    previous = np.empty(0, dtype=np.int64)
    for count, (points, rows) in stages.items():
        expected = selection["stages"][str(count)]
        if points.shape != (count, 3) or rows.shape != (count,) or rows.dtype.kind not in "iu":
            raise ValueError("중첩 표본의 크기 또는 원본 행번호 형식이 다릅니다.")
        if not np.isfinite(points).all() or np.any(rows < 0) or np.any(rows >= 50000) or np.any(np.diff(rows.astype(np.int64)) <= 0):
            raise ValueError("표본 좌표 또는 원본 행 순서가 올바르지 않습니다.")
        if not np.isin(previous, rows).all() or not np.array_equal(points, full[rows]):
            raise ValueError("표본이 원본에서 추출한 중첩 부분집합이 아닙니다.")
        if array_hash(points) != expected["positions_sha256"] or array_hash(rows) != expected["source_rows_sha256"]:
            raise ValueError("중첩 표본의 저장 지문이 다릅니다.")
        previous = rows
    original_data = np.fromfile(input_file(baseline, request["data"]["positions_file"]), dtype="<f8").reshape(-1, 3)
    if not np.array_equal(original_data, transformed(full, initial)):
        raise ValueError("보존 기준 명령의 실제 등록 좌표가 동일 표본·시작 행렬과 다릅니다.")
    if args.rigid_result is not None:
        if args.stage != "scale_refine":
            raise ValueError("새 강체 결과는 배율 보정 단계에만 지정할 수 있습니다.")
        rigid = read_json(workspace_path(args.rigid_result))
        if rigid.get("status") != "completed" or rigid.get("stage") != "rigid":
            raise ValueError("새 강체 결과가 완료된 강체 실험이 아닙니다.")
        for name in ("input_fingerprint", "normalization", "source_id", "seeds"):
            if rigid.get(name) != selection[name]:
                raise ValueError("새 강체 결과의 입력·정규화·표본 식별자가 다릅니다.")
        initial, scale = contract.matrix_sim3(rigid["original_registration_matrix_4x4"], "새 강체 시작 행렬")
        if not np.isclose(scale, 1.0, rtol=0, atol=5e-6):
            raise ValueError("새 강체 결과에 균일 배율 변경이 있습니다.")
    return baseline, request_path, request, source_report, selection, initial, stages


def run_child(executable, command, job, prefix, *, argv=None):
    """요청별 120초 후 정상 취소하고 10초 유예 뒤 직접 실행한 PID만 종료합니다."""
    memory = MemoryMonitor()
    started = time.perf_counter()
    cancelled_at = None
    forced = False
    with prefix.with_suffix(".stdout.log").open("wb") as stdout, prefix.with_suffix(".stderr.log").open("wb") as stderr:
        process = subprocess.Popen(argv or [str(executable), str(command), str(job), str(prefix), "cached"],
                                   stdout=stdout, stderr=stderr,
                                   creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        try:
            while process.poll() is None:
                memory.sample(process.pid)
                now = time.perf_counter()
                if cancelled_at is None and now - started >= 120:
                    (job / "cancel.request").write_text("중첩 ICP 검증 시간 상한", encoding="utf-8")
                    cancelled_at = now
                if cancelled_at is not None and now - cancelled_at >= 10:
                    process.kill()
                    forced = True
                    process.wait(timeout=10)
                    break
                time.sleep(0.1)
        finally:
            if process.poll() is None:
                (job / "cancel.request").write_text("검증 조정자 중단", encoding="utf-8")
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=10)
    result_path = prefix.with_suffix(".result.json")
    return {"wall_seconds": time.perf_counter() - started, "pid": process.pid, "exit_code": process.returncode,
            "time_limit_cancelled": cancelled_at is not None, "forced_termination": forced,
            "memory": memory.report(), "result": read_json(result_path) if result_path.exists() else None}


def distance_child(config_path):
    """별도 프로세스에서 정식 v6 실행기를 재사용하여 단계별 실제 겹침 비율을 계산합니다."""
    config = read_json(config_path)
    job = workspace_path(Path(config["job"]))
    prefix = workspace_path(Path(config["prefix"]), existing=False)
    request = config["request"]
    parameters = contract.resolve_parameters({"cc_max_threads": request["parameters"]["max_threads"]})
    result = {"status": "error"}
    runner = None
    started = time.perf_counter()
    try:
        with solver.NativeRunner(job, workspace_path(Path(config["worker"])), parameters,
                session_identity={"job_id": job.name, "input_fingerprint": config["input_fingerprint"]}) as runner:
            ratio = solver.registration_overlap_ratio(runner, request["data"], request["model"],
                                                      config["quality_policy"], stage="RIGID")
            result.update(overlap_ratio=ratio, valid_count=int(round(ratio * request["data"]["point_count"])),
                          point_count=request["data"]["point_count"],
                          correspondence_radius_normalized=config["quality_policy"]["correspondence_radius_normalized"],
                          logical_operations=runner.logical_operations,
                          physical_native_calls=runner.physical_native_calls,
                          native_build_identity=runner.runtime_manifest)
        result.update(status="completed", session_statistics=runner.session_statistics,
                      geometry_statistics=runner.geometry_statistics)
    except contract.CcCoreCancelled as error:
        result.update(status="cancelled", message=str(error))
    except Exception as error:
        result.update(status="error", message=str(error), error_type=type(error).__name__)
    finally:
        if runner is not None:
            result.update(logical_operations=runner.logical_operations, physical_native_calls=runner.physical_native_calls)
        result["total_seconds"] = time.perf_counter() - started
        write_json(prefix.with_suffix(".result.json"), result)
    return 0 if result["status"] == "completed" else 1


def evaluate_stage_overlap(args, command, model_job, prefix, policy, fingerprint):
    """정확 거리 평가용 불변 입력을 복사하고 별도 실행기 프로세스의 시간·메모리를 감시합니다."""
    started = time.perf_counter()
    job = prefix.parent / (prefix.name + "-distance-job")
    job.mkdir()
    input_hashes = {}
    for entry, fields in ((command["data"], ("positions_file",)),
                         (command["model"], ("positions_file", "triangles_file"))):
        for field in fields:
            source = input_file(model_job, entry[field])
            target = input_file(job, entry[field])
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            digest = file_hash(source)
            if file_hash(target) != digest:
                raise ValueError("정확 거리 평가 입력 복사본이 원본과 다릅니다.")
            input_hashes[entry[field]] = digest
    config = {"worker": str(args.distance_worker), "job": str(job), "prefix": str(prefix),
              "request": command, "quality_policy": policy, "input_fingerprint": fingerprint}
    config_path = prefix.with_suffix(".config.json")
    write_json(config_path, config)
    measured = run_child(args.distance_worker, config_path, job, prefix,
        argv=[sys.executable, str(Path(__file__).resolve()), "--distance-config", str(config_path)])
    for name, digest in input_hashes.items():
        if file_hash(input_file(job, name)) != digest or file_hash(input_file(model_job, name)) != digest:
            raise ValueError("정확 거리 평가 중 표본 또는 모델이 변경되었습니다.")
    measured["total_with_input_copy_seconds"] = time.perf_counter() - started
    measured["input_sha256"] = input_hashes
    write_json(prefix.with_suffix(".measured.json"), measured)
    return measured


def run(args):
    """불변 모델을 복사한 뒤 세 등록 단계를 순차 실행하고 적재를 포함한 총비용을 기록합니다."""
    started = time.perf_counter()
    executable = workspace_path(args.trace_executable)
    args.distance_worker = workspace_path(args.distance_worker)
    args.samples = workspace_path(args.samples)
    args.preserved_result = workspace_path(args.preserved_result)
    output = workspace_path(args.output, existing=False)
    if output.exists():
        raise ValueError("출력 폴더는 존재하지 않는 새 경로여야 합니다.")
    baseline, request_path, request, source_report, selection, initial, stages = validate_inputs(args)
    preserved = read_json(args.preserved_result)
    policy = preserved["quality_policy"]
    radius = contract.finite_nonnegative_number(policy.get("correspondence_radius_normalized"), "보존 대응 반경")
    if radius <= 0:
        raise ValueError("보존 대응 반경이 양수가 아닙니다.")
    output.mkdir(parents=True)
    report = {"scope": "EXPERIMENT_ONLY", "status": "running", "stage": args.stage, "mode": "cached",
              "input_fingerprint": selection["input_fingerprint"], "normalization": selection["normalization"],
              "source_id": selection["source_id"], "seeds": selection["seeds"],
              "selection_report": selection, "initial_native_matrix_4x4": initial.tolist(),
              "baseline_request": request, "baseline_request_sha256": file_hash(request_path),
              "source_files": {str(path): file_hash(path) for path in (args.samples, args.preserved_result, executable)},
              "runtime_dll_sha256": {path.name: file_hash(path) for path in executable.parent.glob("*.dll")},
              "stages": [], "overlap_evaluations": [], "quality_policy": policy,
              "initial_mode": "NESTED_RIGID" if args.rigid_result is not None else "PRESERVED_INDEPENDENT",
              "rigid_result_sha256": file_hash(args.rigid_result) if args.rigid_result is not None else None,
              "independent_quality_evaluation_required": True,
              "note": "전체 시간에는 파일 검증·복사·매 단계 모델 재적재·추가 격자 생성이 포함됩니다."}
    cumulative = np.eye(4)
    limit = 200 if args.stage == "rigid" else 1000
    try:
        job = output / "native-job"
        (job / "native").mkdir(parents=True)
        model_hashes = {}
        copy_started = time.perf_counter()
        for field in ("positions_file", "triangles_file"):
            source = input_file(baseline, request["model"][field])
            target = input_file(job, request["model"][field])
            target.parent.mkdir(parents=True, exist_ok=True)
            source_hash = file_hash(source)
            shutil.copyfile(source, target)
            if file_hash(target) != source_hash:
                raise ValueError("복사한 기준 모델이 원본과 다릅니다.")
            model_hashes[str(target.relative_to(job))] = source_hash
        report["model_copy_and_validation_seconds"] = time.perf_counter() - copy_started
        report["model_file_sha256"] = model_hashes
        for count, (points, rows) in stages.items():
            stage_started = time.perf_counter()
            prefix = output / f"stage-{count}"
            command = copy.deepcopy(request)
            _, current_scale = contract.matrix_sim3(cumulative, "검증 누적 변환")
            command["parameters"]["max_iterations"] = limit
            command["parameters"]["sampling_limit"] = count
            # 배율 단계는 전체 허용 경계를 유지하도록 누적 배율에 대한 잔여 경계를 전달합니다.
            if args.stage == "scale_refine":
                command["parameters"]["min_scale"] = min(1.0, request["parameters"]["min_scale"] / current_scale)
                command["parameters"]["max_scale"] = max(1.0, request["parameters"]["max_scale"] / current_scale)
            command["data"]["point_count"] = count
            command["data"]["positions_file"] = f"native/registration-{count}.bin"
            stage_input = transformed(points, cumulative @ initial)
            stage_input.tofile(input_file(job, command["data"]["positions_file"]))
            np.save(output / f"stage-{count}.source-rows.npy", rows, allow_pickle=False)
            overlap_prefix = output / f"overlap-{count}"
            overlap = evaluate_stage_overlap(args, command, job, overlap_prefix, policy, selection["input_fingerprint"])
            report["overlap_evaluations"].append(overlap)
            overlap_result = overlap["result"]
            if overlap["time_limit_cancelled"] or overlap["exit_code"] != 0 or not overlap_result or overlap_result.get("status") != "completed":
                report["status"] = "cancelled" if overlap["time_limit_cancelled"] else "error"
                break
            command["parameters"]["overlap_ratio"] = overlap_result["overlap_ratio"]
            write_json(prefix.with_suffix(".command.json"), command)
            measured = run_child(executable, prefix.with_suffix(".command.json"), job, prefix)
            for name, digest in model_hashes.items():
                if file_hash(input_file(job, name)) != digest:
                    raise ValueError("단계 사이 불변 모델 파일이 변경되었습니다.")
            measured.update(sample_count=count, max_iterations=limit, overlap_ratio=command["parameters"]["overlap_ratio"],
                            overlap_evaluation_seconds=overlap["total_with_input_copy_seconds"], source_rows_sha256=array_hash(rows),
                            input_positions_sha256=array_hash(stage_input), stage_total_seconds=time.perf_counter() - stage_started)
            report["stages"].append(measured)
            write_json(prefix.with_suffix(".measured.json"), measured)
            result = measured["result"]
            if measured["time_limit_cancelled"] or measured["exit_code"] != 0 or not result or result.get("status") != "completed":
                report["status"] = "cancelled" if measured["time_limit_cancelled"] else "error"
                break
            statistics = contract.validate_icp_statistics(result.get("icp_statistics"), mesh_registration=True,
                kernel_seconds=result["kernel_seconds"], max_iterations=limit)
            contract.validate_icp_native_result(statistics, result.get("native_code"))
            step, _ = contract.matrix_sim3(result["matrix_4x4"], "검증 단계 변환")
            cumulative = step @ cumulative
            _, resulting_scale = contract.matrix_sim3(cumulative, "검증 누적 결과")
            if not request["parameters"]["min_scale"] - 5e-6 <= resulting_scale <= request["parameters"]["max_scale"] + 5e-6:
                raise ValueError("세 단계 누적 배율이 기준 요청의 전체 경계를 벗어났습니다.")
            measured["cumulative_stage_matrix_4x4"] = cumulative.tolist()
            write_json(prefix.with_suffix(".measured.json"), measured)
        else:
            report["status"] = "completed"
            total_matrix = cumulative @ initial
            report["cumulative_stage_matrix_4x4"] = cumulative.tolist()
            report["original_registration_matrix_4x4"] = total_matrix.tolist()
            normalization = selection["normalization"]
            h = np.eye(4)
            h[:3, :3] /= normalization["unit_project_world"]
            h[:3, 3] = -np.asarray(normalization["origin_project_world"]) / normalization["unit_project_world"]
            report["delta_project_world_4x4"] = (np.linalg.inv(h) @ total_matrix @ h).tolist()
        for name, digest in model_hashes.items():
            if file_hash(input_file(job, name)) != digest or file_hash(input_file(baseline, name)) != digest:
                raise ValueError("등록 실행 중 복사 모델 또는 기준 원본이 변경되었습니다.")
    except Exception as error:
        report.update(status="error", error_type=type(error).__name__, message=str(error))
        raise
    finally:
        report["total_wall_seconds"] = time.perf_counter() - started
        report["registration_process_wall_seconds"] = sum(stage["wall_seconds"] for stage in report["stages"])
        report["registration_kernel_seconds"] = sum((stage.get("result") or {}).get("kernel_seconds", 0) for stage in report["stages"])
        report["distance_kernel_seconds"] = sum(call["timings"]["kernel_seconds"]
            for measured in report["overlap_evaluations"]
            for call in (measured.get("result") or {}).get("physical_native_calls", []))
        report["kernel_seconds"] = report["registration_kernel_seconds"] + report["distance_kernel_seconds"]
        report["overlap_evaluation_wall_seconds"] = sum(item["total_with_input_copy_seconds"] for item in report["overlap_evaluations"])
        report["peak_observed_working_set_bytes"] = max((stage["memory"]["observed_worker_tree_peak_working_set_bytes"] for stage in report["stages"] + report["overlap_evaluations"]), default=0)
        write_json(output / "result.json", report)
    return report


def main():
    """기준 강체 또는 배율 실행 하나와 중첩 표본을 명시하여 검증 실험을 시작합니다."""
    if len(sys.argv) == 3 and sys.argv[1] == "--distance-config":
        return distance_child(workspace_path(Path(sys.argv[2])))
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("trace-executable", "distance-worker", "baseline-job", "samples", "preserved-result", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--stage", choices=("rigid", "scale_refine"), required=True)
    parser.add_argument("--rigid-result", type=Path, help="연속 배율 후보의 시작점으로 사용할 새 중첩 강체 result.json")
    report = run(parser.parse_args())
    print(json.dumps({key: report[key] for key in ("stage", "status", "total_wall_seconds", "kernel_seconds")}, ensure_ascii=False))
    return 0 if report["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
