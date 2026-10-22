"""제품 등록 경로와 분리하여 고정 원본 표면점의 정확 삼각면 거리를 비교합니다."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from time import perf_counter

import numpy as np


ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from tools.align.alignment import icp_cccore_contract as contract
from tools.align.alignment import icp_cccore_solver as solver

DEFAULT_ROOT = ROOT / "build/verification/point-kd-20261021"
DEFAULT_OLD = ROOT / "output/align_comparison_20261021/repeat_profile"
DIRECTIONS = ("moving_to_reference", "reference_to_moving")


def read_json(path):
    """평가 입력의 UTF-8 객체를 읽고 객체 이외의 자료는 거부합니다."""
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"객체 형식의 평가 입력이 필요합니다: {path}")
    return value


def array_sha256(array):
    """평가 표본의 형상·자료형·순서까지 포함하는 독립 지문을 계산합니다."""
    values = np.ascontiguousarray(array)
    digest = hashlib.sha256(f"{values.dtype.str}:{values.shape}".encode("ascii"))
    digest.update(memoryview(values).cast("B"))
    return digest.hexdigest()


def source_identity(path):
    """파일 전체 내용과 파일 시스템 식별값을 기록해 평가 중 원본 보존을 확인합니다."""
    path = Path(path).resolve(strict=True)
    stat = path.stat()
    return {"path": str(path), "bytes": stat.st_size, "mtime_ns": stat.st_mtime_ns,
            "sha256": solver.file_sha256(path)}


def load_mesh(directory, entry):
    """세계 좌표 메시를 읽기 전용으로 매핑하고 모든 좌표·인덱스의 유효성을 확인합니다."""
    positions_path = contract.job_path(directory, entry["positions_file"])
    triangles_path = contract.job_path(directory, entry["triangles_file"])
    count = contract.positive_integer(entry["point_count"], "원본 정점 수")
    faces = contract.positive_integer(entry["triangle_count"], "원본 삼각면 수")
    if entry["kind"] != "TRIANGLE_MESH" or positions_path.stat().st_size != count * 24 or triangles_path.stat().st_size != faces * 12:
        raise ValueError("평가 메시의 종류 또는 바이너리 크기가 원본 명세와 다릅니다.")
    positions = np.memmap(positions_path, mode="r", dtype="<f8", shape=(count, 3))
    triangles = np.memmap(triangles_path, mode="r", dtype="<u4", shape=(faces, 3))
    for start in range(0, count, 65_536):
        if not np.isfinite(positions[start:start + 65_536]).all():
            raise ValueError("평가 원본 좌표에 유한하지 않은 값이 있습니다.")
    for start in range(0, faces, 65_536):
        if np.any(triangles[start:start + 65_536] >= count):
            raise ValueError("평가 원본 삼각면 인덱스가 정점 범위를 벗어났습니다.")
    return contract.CcCoreGeometry("TRIANGLE_MESH", positions, triangles, {})


def sample_surface(positions, triangles, count, seed):
    """면적 누적분포와 제곱근 무게로 원본 표면의 균일 평가점과 요소 출처를 고정합니다."""
    if count < 3:
        raise ValueError("방향별 평가점이 최소 3개 필요합니다.")
    cumulative = np.empty(len(triangles), dtype=np.float64)
    for start in range(0, len(triangles), 65_536):
        corners = positions[triangles[start:start + 65_536]]
        cumulative[start:start + len(corners)] = np.linalg.norm(
            np.cross(corners[:, 1] - corners[:, 0], corners[:, 2] - corners[:, 0]), axis=1)
    if not np.isfinite(cumulative).all() or np.any(cumulative < 0):
        raise ValueError("원본 표면의 면적이 유한하지 않습니다.")
    np.cumsum(cumulative, out=cumulative)
    if not len(cumulative) or not np.isfinite(cumulative[-1]) or cumulative[-1] <= 0:
        raise ValueError("평가할 원본 표면의 면적이 양수가 아닙니다.")
    generator = np.random.default_rng(seed)
    face_ids = np.searchsorted(cumulative, generator.random(count) * cumulative[-1], side="right")
    radial, edge = np.sqrt(generator.random(count)), generator.random(count)
    barycentric = np.column_stack((1 - radial, radial * (1 - edge), radial * edge))
    corners = positions[triangles[face_ids]]
    points = np.sum(corners * barycentric[:, :, None], axis=1)
    return np.ascontiguousarray(points), face_ids.astype(np.uint64), barycentric


def common_original_moving_matrix(final_alignment, moving_id, reference_ids, provenance):
    """역할이 바뀐 시험도 최초 이동 메시를 변환하는 동등한 세계 행렬로 통일합니다."""
    matrix, _scale = contract.matrix_sim3(final_alignment, "후보 최종 정렬")
    original_moving = provenance["moving_model_id"]
    original_references = tuple(provenance["reference_model_ids"])
    if len(original_references) != 1:
        raise ValueError("이 비교 도구는 원본 메시 한 쌍만 지원합니다.")
    if moving_id == original_moving and tuple(reference_ids) == original_references:
        return matrix, "원래 이동 메시의 최종 세계 정렬을 직접 사용"
    if moving_id == original_references[0] and tuple(reference_ids) == (original_moving,):
        return contract.matrix_sim3(np.linalg.inv(matrix), "역할 전환 후보 역행렬")[0], "역할 전환 최종 정렬을 역으로 적용하여 원래 이동 메시 기준으로 통일"
    raise ValueError("후보가 원본 메시 쌍과 다른 소유자 또는 기준 목록을 참조합니다.")


def normalized_matrix(world_matrix, normalization):
    """세계 최종 행렬을 공통 정규화 좌표의 켤레변환으로 바꿉니다."""
    matrix = np.array(world_matrix, dtype=np.float64, copy=True)
    matrix[:3, 3] = (world_matrix[:3, :3] @ normalization.origin + world_matrix[:3, 3]
                     - normalization.origin) / normalization.unit
    return contract.matrix_sim3(matrix, "정규화 평가 행렬")[0]


def distance_statistics(distances):
    """후보 거리를 추가 제거하지 않고 고정된 분모의 오차와 꼬리 분포를 반환합니다."""
    values = np.asarray(distances, dtype=np.float64)
    if values.ndim != 1 or not len(values) or not np.isfinite(values).all() or np.any(values < 0):
        raise ValueError("고정 평가 거리에는 비어 있지 않은 유한 비음수 배열이 필요합니다.")
    return {"count": len(values), "rms": float(np.sqrt(np.mean(values * values))),
            "mean": float(np.mean(values)), "median": float(np.median(values)),
            "p95": float(np.quantile(values, .95)), "maximum": float(np.max(values))}


def fixed_spatial_distribution(points, selected, distances):
    """초기 원본 범위의 여덟 공간 구간별 전체·고정 겹침 개수와 후보 오차를 기록합니다."""
    midpoint = (points.min(axis=0) + points.max(axis=0)) * .5
    bins = np.sum((points >= midpoint).astype(np.uint8) * (1, 2, 4), axis=1)
    result = []
    for index in range(8):
        mask = bins == index
        selected_mask = mask & selected
        result.append({"bin": index, "total_count": int(mask.sum()),
                       "fixed_inlier_count": int(selected_mask.sum()),
                       "fixed_inlier_metrics": distance_statistics(distances[selected_mask]) if np.any(selected_mask) else None})
    return result


def summarize_distances(distances, samples, fixed_masks, radius):
    """초기에 고정한 겹침 부분과 전체 참고 구간의 양방향 오차를 분리합니다."""
    directions = {}
    for direction in DIRECTIONS:
        values, fixed = distances[direction], fixed_masks[direction]
        if np.count_nonzero(fixed) < 3:
            raise ValueError(f"초기 고정 겹침 평가점이 부족합니다: {direction}")
        directions[direction] = {"fixed_overlap": distance_statistics(values[fixed]),
            "all_surface_reference": distance_statistics(values),
            "fixed_inlier_count": int(fixed.sum()), "original_sample_count": len(values),
            "currently_within_radius_count_reference_only": int(np.count_nonzero(values <= radius)),
            "spatial_distribution": fixed_spatial_distribution(samples[direction], fixed, values)}
    return {"directions": directions,
        "fixed_overlap": distance_statistics(np.concatenate([distances[name][fixed_masks[name]] for name in DIRECTIONS])),
        "all_surface_reference": distance_statistics(np.concatenate([distances[name] for name in DIRECTIONS])),
        "mean_directional_fixed_overlap_rms": float(np.mean([directions[name]["fixed_overlap"]["rms"] for name in DIRECTIONS]))}


def accuracy_gate(candidate, existing, unit):
    """같은 고정 부분의 RMS와 95백분위 오차가 기존 결과보다 5퍼센트 넘게 나빠지는지 판정합니다."""
    checks = []
    for scope in ("symmetric", *DIRECTIONS):
        current = candidate["fixed_overlap"] if scope == "symmetric" else candidate["directions"][scope]["fixed_overlap"]
        reference = existing["fixed_overlap"] if scope == "symmetric" else existing["directions"][scope]["fixed_overlap"]
        for metric in ("rms", "p95"):
            limit = max(reference[metric] * 1.05, unit * 1e-12)
            checks.append({"scope": scope, "metric": metric, "existing_value": reference[metric],
                "candidate_value": current[metric], "maximum_value": limit,
                "relative_degradation": (current[metric] / reference[metric] - 1.) if reference[metric] else None,
                "passed": current[metric] <= limit})
    return {"relative_degradation_limit": .05, "absolute_zero_floor": unit * 1e-12,
            "passed": all(item["passed"] for item in checks), "checks": checks}


def registration_metrics(metadata):
    """엔진 내부 등록 지표를 독립 표면 평가와 섞지 않고 원래 의미로 기록합니다."""
    fields = ("backend_id", "metric_kind", "initial_rms", "final_rms", "iterations", "solver_stop_reason",
              "best_stage", "baseline_metrics", "best_candidate_metrics", "registration_direction")
    return {"comparison_scope": "엔진 내부 지표이며 서로 다른 엔진 사이의 직접 정확도 비교에 사용하지 않음",
            **{name: metadata[name] for name in fields if name in metadata}}


def verify_source_provenance(actual, expected):
    """초기 정렬과 역할 변경을 제외한 원본 내용·축·슬롯이 같은 메시 쌍인지 검증합니다."""
    if not isinstance(actual, dict):
        raise ValueError("후보 등록 결과에 원본 형상 출처가 없습니다.")
    fields = ("owner_model_id", "geometry_kind", "geometry_source", "geometry_revision", "source_path",
              "source_fingerprint", "positions_fingerprint", "triangles_fingerprint", "point_count", "triangle_count",
              "display_transform_fingerprint", "display_transform_float64")
    original = {source["owner_model_id"]: source for source in (expected["moving"], *expected["references"])}
    sources = (actual["moving"], *actual["references"])
    if len(sources) != len(original) or {source["owner_model_id"] for source in sources} != set(original):
        raise ValueError("후보 원본 소유자 목록이 평가 메시 쌍과 다릅니다.")
    for source in sources:
        reference = original[source["owner_model_id"]]
        if any(source[field] != reference[field] for field in fields):
            raise ValueError("후보 원본 내용·축·슬롯이 고정 평가 메시와 다릅니다.")


def load_candidates(options, provenance):
    """기존 확정 행렬과 존재하는 반복 시험의 모든 최종 후보를 원래 이동 기준으로 읽습니다."""
    raw = read_json(options.existing_raw)
    observed = read_json(options.existing_observed)["result"]
    if raw["input_fingerprint"] != observed["input_fingerprint"]:
        raise ValueError("기존 등록 지표와 관측된 최종 행렬의 입력 지문이 다릅니다.")
    verify_source_provenance(raw.get("geometry_provenance"), provenance)
    matrix, formula = common_original_moving_matrix(observed["final_alignment"]["matrix_float64"],
        observed["moving_model_id"], observed["reference_model_ids"], provenance)
    candidates = [{"name": "existing_cc", "matrix": matrix, "matrix_derivation": formula,
        "source": source_identity(options.existing_observed), "registration_metrics": registration_metrics(raw)}]
    missing = []
    for path in options.candidate:
        if not path.is_file():
            missing.append(str(path.resolve()))
            continue
        report = read_json(path)
        if not report.get("completed"):
            missing.append(str(path.resolve()))
            continue
        references = (report["reference_model_id"],)
        for index, run in enumerate(report["runs"]):
            verify_source_provenance(run["metadata"].get("geometry_provenance"), provenance)
            matrix, formula = common_original_moving_matrix(run["final_alignment"], report["moving_model_id"], references, provenance)
            candidates.append({"name": f"{report['scenario']}-{run['backend']}-{index}", "matrix": matrix,
                "matrix_derivation": formula, "source": source_identity(path), "run_index": index,
                "warmup": run["warmup"], "registration_metrics": registration_metrics(run["metadata"])})
    return candidates, missing, source_identity(options.existing_raw)


def evaluate_native_distances(runner, label, matrix, normalized, samples, normalization):
    """기존 BVH에만 정확 표면 거리를 질의하고 원본 모델을 바꾸지 않은 채 역방향 거리를 복원합니다."""
    delta = normalized_matrix(matrix, normalization)
    forward = runner.geometry(label + "-moving-query", solver.transform_positions(samples[DIRECTIONS[0]], delta), cache_scope="COMMAND")
    reference = runner.geometry("fixed-reference-surface", normalized["reference"].positions, normalized["reference"].triangles)
    moving = runner.geometry("fixed-moving-surface", normalized["moving"].positions, normalized["moving"].triangles)
    first = runner.call("distance", forward, reference, stage="BASELINE")["distances"] * normalization.unit
    inverse, scale = solver.inverse_surface_query(samples[DIRECTIONS[1]], delta)
    backward = runner.geometry(label + "-reference-query", inverse, cache_scope="COMMAND")
    second = runner.call("distance", backward, moving, stage="BASELINE")["distances"] * normalization.unit * scale
    return dict(zip(DIRECTIONS, (first, second)))


def run(options):
    """원본 표본과 초기 겹침 부분을 고정한 뒤 모든 후보를 동일한 네이티브 거리 세션에서 평가합니다."""
    started = perf_counter()
    output = options.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    manifest_path = options.geometry.resolve(strict=True)
    manifest = read_json(manifest_path)
    if not np.array_equal(manifest["initial_alignment_4x4"], np.eye(4)):
        raise ValueError("원본 세계 좌표의 초기 정렬이 단위 행렬이어야 합니다.")
    if any(not np.array_equal(source["applied_alignment_float64"], np.eye(4))
           for source in (manifest["provenance"]["moving"], *manifest["provenance"]["references"])):
        raise ValueError("평가 원본 메시 쌍의 기준 세계 정렬이 모두 단위 행렬이어야 합니다.")
    sources = [manifest_path]
    for role in ("moving", "reference"):
        sources.extend(contract.job_path(manifest_path.parent, manifest[role][field]) for field in ("positions_file", "triangles_file"))
    before = [source_identity(path) for path in sources]
    geometries = {role: load_mesh(manifest_path.parent, manifest[role]) for role in ("moving", "reference")}
    normalization = solver.make_normalization(geometries["moving"].positions, geometries["reference"].positions)
    diagonals = {role: float(np.linalg.norm(mesh.positions.max(axis=0) - mesh.positions.min(axis=0))) for role, mesh in geometries.items()}
    radius = min(diagonals.values()) * .01
    seeds = dict(zip(DIRECTIONS, (104729, 104730)))
    sample_data = {name: sample_surface(geometries[role].positions, geometries[role].triangles, options.sample_count, seeds[name])
                   for name, role in zip(DIRECTIONS, ("moving", "reference"))}
    samples = {name: values[0] for name, values in sample_data.items()}
    normalized_samples = {name: normalization.apply(points) for name, points in samples.items()}
    candidates, missing, old_identity = load_candidates(options, manifest["provenance"])
    report = {"scope": "INDEPENDENT_EVALUATION_ONLY", "completed": False, "units": "PROJECT_WORLD",
        "source_files": before, "existing_raw_registration_source": old_identity,
        "normalization": normalization.to_mapping(), "native_build_identity": solver.native_build_identity(options.worker),
        "distance_method": {"backend": "CCCORELIB_BVH_EXACT_POINT_TO_TRIANGLE",
            "coordinates": "기존 네이티브 계산기의 공통 정규화 단정도 좌표",
            "output": "세계 길이 단위로 복원한 비음수 표면 거리",
            "nearest_vertex_approximation_used": False, "production_registration_path_modified": False},
        "sampling": {"policy": "고정 원본 표면 면적 비례 표본", "count_per_direction": options.sample_count,
            "seeds": seeds, "point_sha256": {name: array_sha256(points) for name, points in samples.items()},
            "triangle_sha256": {name: array_sha256(values[1]) for name, values in sample_data.items()}},
        "fixed_subset_policy": {"rule": "초기 단위 정렬의 정확 표면 거리가 반경 이내인 원본 평가점만 한 번 선택",
            "radius": radius, "radius_formula": "0.01 * min(original_moving_bbox_diagonal, original_reference_bbox_diagonal)",
            "original_diagonals": diagonals, "candidate_filtering_performed": False},
        "candidate_inputs_pending": missing, "candidates": []}
    parameters = contract.resolve_parameters({"cc_max_threads": options.threads})
    archive = {name + "_points": values[0] for name, values in sample_data.items()}
    for name, values in sample_data.items():
        archive[name + "_triangle_ids"], archive[name + "_barycentric"] = values[1:]
    with TemporaryDirectory(prefix="native-evaluation-", dir=output) as temporary:
        directory = Path(temporary).resolve()
        if not directory.is_relative_to(output):
            raise ValueError("평가 임시 작업이 지정 출력 폴더를 벗어났습니다.")
        with solver.NativeRunner(directory, options.worker, parameters) as runner:
            normalized = {role: solver._clean_mesh_geometry(contract.CcCoreGeometry(mesh.kind,
                normalization.apply(mesh.positions), mesh.triangles, {}), role, native_precision=True,
                cleaner=runner.mesh_cleaner) for role, mesh in geometries.items()}
            report["native_evaluation_geometry"] = {role: {"point_count": len(mesh.positions),
                "triangle_count": len(mesh.triangles)} for role, mesh in normalized.items()}
            baseline_distances = evaluate_native_distances(runner, "baseline", np.eye(4), normalized, normalized_samples, normalization)
            fixed = {name: distances <= radius for name, distances in baseline_distances.items()}
            report["baseline"] = summarize_distances(baseline_distances, samples, fixed, radius)
            for name in DIRECTIONS:
                archive[name + "_fixed_inlier_mask"] = fixed[name]
                archive[name + "_baseline_distances"] = baseline_distances[name]
            report["fixed_subset_policy"]["mask_sha256"] = {name: array_sha256(mask) for name, mask in fixed.items()}
            exact_results = {array_sha256(np.eye(4)): baseline_distances}
            for index, candidate in enumerate(candidates):
                matrix = candidate.pop("matrix")
                key = array_sha256(matrix)
                began = perf_counter()
                reused = key in exact_results
                if not reused:
                    exact_results[key] = evaluate_native_distances(runner, f"candidate-{index}", matrix, normalized, normalized_samples, normalization)
                distances = exact_results[key]
                candidate.update(final_original_moving_world_matrix=matrix.tolist(),
                    independent_metrics=summarize_distances(distances, samples, fixed, radius),
                    evaluation_seconds=perf_counter() - began, identical_matrix_distance_reused=reused)
                report["candidates"].append(candidate)
                for name in DIRECTIONS:
                    archive[f"candidate_{index}_{name}_distances"] = distances[name]
            report["physical_native_calls"] = runner.physical_native_calls
        report["native_session_statistics"] = runner.session_statistics
    existing = report["candidates"][0]["independent_metrics"]
    for candidate in report["candidates"]:
        candidate["accuracy_gate_against_existing_cc"] = accuracy_gate(candidate["independent_metrics"], existing, normalization.unit)
    after = [source_identity(path) for path in sources]
    if before != after:
        raise ValueError("독립 평가 중 원본 메시 또는 명세 파일이 변경되었습니다.")
    archive_path = output / "fixed-evaluation-data.npz"
    contract.write_arrays_atomic(archive_path, archive)
    report.update(completed=True, source_files_unchanged=True,
                  evaluation_archive=source_identity(archive_path), total_seconds=perf_counter() - started)
    contract.write_json_atomic(output / "report.json", report)
    return report


def arguments():
    """원본 위치와 비교 결과 위치를 명시적인 명령행 인자로 받습니다."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--geometry", type=Path, default=DEFAULT_ROOT / "baseline/geometry.json")
    parser.add_argument("--existing-raw", type=Path, default=DEFAULT_OLD / "raw_result.json")
    parser.add_argument("--existing-observed", type=Path, default=DEFAULT_OLD / "blender_observed_result.json")
    parser.add_argument("--candidate", type=Path, action="append")
    parser.add_argument("--output", type=Path, default=DEFAULT_ROOT / "independent-evaluation")
    parser.add_argument("--worker", type=Path, default=ROOT / ".cccore-runtime/cccore/colmap_cccore_worker.exe")
    parser.add_argument("--sample-count", type=int, default=30_000)
    parser.add_argument("--threads", type=int, default=31)
    options = parser.parse_args()
    options.worker = options.worker.resolve(strict=True)
    if options.candidate is None:
        options.candidate = [DEFAULT_ROOT / f"paired/{scenario}/results.json" for scenario in ("aligned", "perturbed", "reversed")]
    return options


def main():
    """독립 비교를 실행하고 초기 오차와 후보별 통과 여부를 한국어로 표시합니다."""
    options = arguments()
    report = run(options)
    baseline = report["baseline"]["fixed_overlap"]
    print(f"초기 고정 겹침: {baseline['count']}점, RMS {baseline['rms']:.9g}, 95백분위 {baseline['p95']:.9g}")
    for candidate in report["candidates"]:
        metrics = candidate["independent_metrics"]["fixed_overlap"]
        passed = "통과" if candidate["accuracy_gate_against_existing_cc"]["passed"] else "실패"
        print(f"{candidate['name']}: RMS {metrics['rms']:.9g}, 95백분위 {metrics['p95']:.9g}, 정확도 기준 {passed}")
    print(f"평가 보고서: {options.output.resolve() / 'report.json'}")


if __name__ == "__main__":
    main()
