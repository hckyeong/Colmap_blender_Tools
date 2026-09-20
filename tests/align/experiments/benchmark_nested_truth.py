"""제품을 수정하지 않고 동일 정답·독립 평가 표본으로 단일 및 중첩 등록을 비교합니다."""

import argparse
import json
from pathlib import Path
import sys
import traceback
from time import perf_counter
from types import SimpleNamespace

import numpy as np

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(ROOT / "tests/align"))
sys.path.insert(0, str(ROOT / "tools/align/alignment"))
import test_icp_cccore_partial_overlap as truth
import icp_cccore_contract as contract
import icp_cccore_solver as solver
from benchmark_nested_icp import run_child, write_json, file_hash
from prepare_nested_icp_samples import spatial_nested_order


def stage_counts(count, nested):
    """작은 최종 표본은 상한을 잘라 동일 크기의 중복 단계를 제거합니다."""
    if count < 3:
        raise ValueError("등록 표본이 세 점보다 적습니다.")
    return tuple(dict.fromkeys((min(5000, count), min(20000, count), count))) if nested else (count,)


def register_policy(runner, executable, points, model, policy, nested, adjust_scale, label, order):
    """원본 표본의 중첩 부분집합에 누적변환을 한 번 적용하고 매번 정확 겹침을 재계산합니다."""
    started = perf_counter()
    if "triangles_file" not in model:
        raise ValueError("이 검증 실행기는 삼각면 기준 모델만 지원합니다.")
    if not nested:
        order = np.arange(len(points))
    cumulative = np.eye(4)
    reports = []
    for count in stage_counts(len(points), nested):
        rows = np.sort(order[:count])
        data = runner.geometry(f"{label}-{count}", solver.transform_positions(points[rows], cumulative))
        stage = "SCALE_REFINE" if adjust_scale else "RIGID"
        ratio = solver.registration_overlap_ratio(runner, data, model, policy, stage=stage)
        _, scale = contract.matrix_sim3(cumulative, "실험 누적 변환")
        prefix = runner.directory / f"{label}-{count}"
        command = {"operation": "register", "stage": stage, "data": dict(data), "model": dict(model),
                   "parameters": {"adjust_scale": adjust_scale,
                       "min_scale": min(1.0, contract.CC_MIN_SCALE / scale) if adjust_scale else 1.0,
                       "max_scale": max(1.0, contract.CC_MAX_SCALE / scale) if adjust_scale else 1.0,
                       "overlap_ratio": ratio, "filter_out_farthest_points": True,
                       "max_iterations": 1000 if adjust_scale else 200,
                       "sampling_limit": max(count, model["point_count"] if "triangles_file" not in model else 1),
                       "max_threads": runner.parameters["max_threads"]}}
        write_json(prefix.with_suffix(".command.json"), command)
        measured = run_child(executable, prefix.with_suffix(".command.json"), runner.directory, prefix)
        write_json(prefix.with_suffix(".measured.json"), measured)
        result = measured["result"]
        if measured["time_limit_cancelled"] or measured["exit_code"] != 0 or not result or result.get("status") != "completed":
            raise RuntimeError(f"실험 등록 실패: {label}, 표본 {count}, 네이티브 응답: {result}")
        statistics = contract.validate_icp_statistics(result.get("icp_statistics"),
            mesh_registration="triangles_file" in model, kernel_seconds=result["kernel_seconds"],
            max_iterations=command["parameters"]["max_iterations"])
        contract.validate_icp_native_result(statistics, result.get("native_code"))
        step, _ = contract.matrix_sim3(result["matrix_4x4"], "실험 단계 변환")
        cumulative = step @ cumulative
        _, scale = contract.matrix_sim3(cumulative, "실험 누적 배율")
        low, high = (contract.CC_MIN_SCALE, contract.CC_MAX_SCALE) if adjust_scale else (1.0, 1.0)
        if not low - 5e-6 <= scale <= high + 5e-6:
            raise ValueError("누적 배율이 최초 허용 경계를 벗어났습니다.")
        np.save(prefix.with_suffix(".rows.npy"), rows, allow_pickle=False)
        reports.append({"sample_count": count, "overlap_ratio": ratio, "measured": measured,
                        "cumulative_matrix_4x4": cumulative.tolist()})
    return cumulative, {"seconds": perf_counter() - started, "steps": reports}


def run_case(args, name):
    """한 번 준비한 동일 표본으로 두 정책을 실행하고 제품의 독립 평가와 선택을 재사용합니다."""
    started = perf_counter()
    directory = args.output / name
    directory.mkdir()
    manifest, expected, original, diagonal = truth.create_job(directory, truth.CASES[name], args.threads,
        transverse_scale=.15 if name.startswith("thin") else 1.)
    original_hashes = truth.input_hashes(directory, manifest)
    geometries = [solver._clean_mesh_geometry(contract.load_geometry(directory, manifest["geometry"][role]), role)
                  for role in ("moving", "reference")]
    normalization = solver.make_normalization(*(item.positions for item in geometries))
    moving, reference = [solver._clean_mesh_geometry(contract.CcCoreGeometry(item.kind,
        normalization.apply(item.positions), item.triangles, item.provenance), "실험", native_precision=True)
        for item in geometries]
    parameters = contract.resolve_parameters(manifest["parameters"])
    reports = {}
    with solver.NativeRunner(directory, args.native_worker, parameters, session_identity=manifest) as runner:
        policy = solver.prepare_overlap_policy(runner, moving, reference, False, marker_initial_rms=0.)
        full = {}
        data, moving_overlap, reference_overlap, sampling = solver.prepare_candidate_samples(
            runner, moving, reference, policy, False, full_samples=full, normalization=normalization.to_mapping())
        # 이 정답 회귀는 실제 세 단계 정책을 검증하므로 작은 입력의 단일 단계 결과로 대체하지 않습니다.
        if len(data) != 50000:
            raise ValueError("부분겹침 정답 비교는 제품 기본 등록 표본 50,000개를 확보해야 합니다.")
        samples = [np.concatenate((subset, full.get(direction, np.empty((0, 3)))))
                   for subset, direction in ((moving_overlap, "moving_to_reference"), (reference_overlap, "reference_to_moving"))]
        counts = {"moving_to_reference": len(moving_overlap), "reference_to_moving": len(reference_overlap)}
        np.savez(directory / "fixed-samples.npz", registration=data,
                 moving_evaluation=samples[0], reference_evaluation=samples[1])
        preparation_seconds = perf_counter() - started
        model = runner.geometry("reference-model", reference.positions, reference.triangles)
        baseline = solver.evaluate_candidate(runner, "baseline", np.eye(4), moving, reference,
            *samples, normalization, policy, stage="BASELINE", overlap_sample_counts=counts)
        solver.assess_correspondence_coverage(baseline, policy)
        selection_started = perf_counter()
        order = spatial_nested_order(data, contract.CC_OPTIMIZER_SEED)
        selection_seconds = perf_counter() - selection_started
        np.save(directory / "nested-order.npy", order, allow_pickle=False)
        for nested in (False, True):
            label = "nested" if nested else "single"
            policy_started = perf_counter()
            rigid, rigid_report = register_policy(runner, args.trace_executable, data, model, policy, nested, False, label + "-rigid", order)
            refined, refine_report = register_policy(runner, args.trace_executable,
                solver.transform_positions(data, rigid), model, policy, nested, True, label + "-scale", order)
            deltas = {"baseline": np.eye(4), "rigid": rigid, "scale_refine": refined @ rigid}
            metrics = {"baseline": baseline}
            for candidate in ("rigid", "scale_refine"):
                metrics[candidate] = solver.evaluate_candidate(runner, label + "-" + candidate,
                    deltas[candidate], moving, reference, *samples, normalization, policy,
                    stage="RIGID_EVALUATION" if candidate == "rigid" else "REFINE_EVALUATION", overlap_sample_counts=counts)
            best = solver.select_candidate(metrics, {"additional_scale": 1.0}, policy)[0]
            world = normalization.world_delta(deltas[best])
            diagnostics = solver.delta_diagnostics(world, normalization)
            selected, quality, reasons, improvement, stop = solver.select_candidate(metrics, diagnostics, policy)
            measured = truth.measure_truth(SimpleNamespace(delta_4x4=world), expected, original, diagonal)
            candidate_truth = {key: truth.measure_truth(SimpleNamespace(delta_4x4=normalization.world_delta(value)),
                expected, original, diagonal) for key, value in deltas.items()}
            passed = (measured["position_rms_diagonal_fraction"] <= truth.MAX_POSITION_RMS_DIAGONAL_FRACTION
                      and measured["relative_scale_error"] <= truth.MAX_RELATIVE_SCALE_ERROR
                      and measured["rotation_error_degrees"] <= truth.MAX_ROTATION_ERROR_DEGREES)
            reports[label] = {"passed": bool(passed), "truth": measured, "best_stage": selected,
                "quality": quality, "reasons": reasons, "relative_improvement": improvement, "stop_reason": stop,
                "delta_4x4": world.tolist(), "candidate_truth": candidate_truth, "metrics": metrics,
                "rigid": rigid_report, "scale_refine": refine_report,
                "policy_seconds": perf_counter() - policy_started + (selection_seconds if nested else 0.)}
        runner.close()
        session_statistics = runner.session_statistics
    if truth.input_hashes(directory, manifest) != original_hashes:
        raise ValueError("실험 중 원본 입력이 변경되었습니다.")
    single_matrix = np.asarray(reports["single"]["delta_4x4"])
    nested_matrix = np.asarray(reports["nested"]["delta_4x4"])
    displacement = np.linalg.norm(solver.transform_positions(original, nested_matrix)
                                  - solver.transform_positions(original, single_matrix), axis=1)
    report = {"case": name, "scope": "EXPERIMENT_ONLY", "shared_preparation_seconds": preparation_seconds,
              "registration_sample_count": len(data),
              "policy_stage_counts": {"single": list(stage_counts(len(data), False)),
                                      "nested": list(stage_counts(len(data), True))},
              "policy_matrix_max_difference": float(np.max(np.abs(nested_matrix - single_matrix))),
              "policy_displacement_rms": float(np.sqrt(np.mean(displacement ** 2))),
              "policy_displacement_max": float(np.max(displacement)),
              "sampling": sampling, "selection_seconds": selection_seconds, "quality_policy": policy, "normalization": normalization.to_mapping(),
              "session_statistics": session_statistics, "policies": reports, "total_seconds": perf_counter() - started,
              "timing_note": "준비와 기준 평가는 공유하며 정책 시간에는 등록·겹침 재평가·최종 독립 평가를 포함합니다."}
    write_json(directory / "comparison.json", report)
    return report


def main():
    """명시한 사례만 순차 실행하며 실패 및 진단 사례도 같은 고정 기준으로 기록합니다."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", choices=tuple(truth.CASES), nargs="+", default=list(truth.DEFAULT_CASES))
    parser.add_argument("--native-worker", type=Path, required=True)
    parser.add_argument("--trace-executable", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--threads", type=int, default=1)
    args = parser.parse_args()
    if args.output.exists() or not 1 <= args.threads <= 1024:
        parser.error("새 출력 폴더와 1~1024 범위 스레드 수가 필요합니다.")
    for key in ("native_worker", "trace_executable"):
        value = getattr(args, key).resolve(strict=True)
        if not value.is_file():
            parser.error("실행 파일 경로가 필요합니다.")
        setattr(args, key, value)
    args.output = args.output.resolve()
    args.output.mkdir(parents=True)
    reports = []
    binaries = {args.native_worker, args.trace_executable}
    for executable in tuple(binaries):
        binaries.update(executable.parent.glob("*.dll"))
    identity = {str(path): file_hash(path) for path in sorted(binaries)}
    for name in dict.fromkeys(args.case):
        try:
            report = run_case(args, name)
        except Exception as error:
            report = {"case": name, "status": "execution_error", "error": str(error),
                      "error_type": type(error).__name__, "traceback": traceback.format_exc(), "passed": False}
        if any(file_hash(Path(path)) != digest for path, digest in identity.items()):
            raise RuntimeError("실험 중 실행 파일 또는 라이브러리가 변경되었습니다.")
        reports.append(report)
        write_json(args.output / "report.json", {"executables": identity, "cases": reports})
        print(json.dumps({"case": name, "error": report.get("error"),
            "passed": {key: value["passed"] for key, value in report.get("policies", {}).items()}}, ensure_ascii=False), flush=True)
    return 0 if all(report.get("policies") and all(item["passed"] for item in report["policies"].values()) for report in reports) else 1


if __name__ == "__main__":
    raise SystemExit(main())
