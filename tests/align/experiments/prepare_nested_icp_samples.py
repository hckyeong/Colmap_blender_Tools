"""보존 등록 표본에서 공간 분포를 유지하는 중첩 실험 입력만 준비합니다."""

import argparse
from collections import deque
import hashlib
import json
from pathlib import Path
import sys
from time import perf_counter

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from benchmark_fixed_domain_preparation import array_hash, read_json


def spatial_nested_order(points, seed):
    """최장축 중위수 분할의 너비우선 대표로 재현 가능한 원본 행 우선순위를 만듭니다."""
    points = np.asarray(points)
    if points.ndim != 2 or points.shape[1] != 3 or not np.isfinite(points).all():
        raise ValueError("등록 표본은 유한한 N×3 좌표여야 합니다.")
    count = len(points)
    priorities = np.empty(count, dtype=np.int64)
    generator = np.random.Generator(np.random.PCG64(seed))
    priorities[generator.permutation(count)] = np.arange(count)
    queue = deque([np.arange(count, dtype=np.int64)])
    selected = np.zeros(count, dtype=bool)
    order = []
    while queue:
        level = [queue.popleft() for _index in range(len(queue))]
        # 마지막 깊이의 일부만 선택해도 왼쪽 공간부터 편향되지 않도록 노드 순서를 섞습니다.
        for node_index in generator.permutation(len(level)):
            indices = level[node_index]
            remaining = indices[~selected[indices]]
            if remaining.size:
                representative = int(remaining[np.argmin(priorities[remaining])])
                selected[representative] = True
                order.append(representative)
            if len(indices) > 1:
                axis = int(np.argmax(np.ptp(points[indices], axis=0)))
                # 같은 좌표는 원본 행번호로 정렬하여 플랫폼과 부모 순서에 의한 흔들림을 막습니다.
                ordered = indices[np.lexsort((indices, points[indices, axis]))]
                middle = len(ordered) // 2
                queue.extend((ordered[:middle], ordered[middle:]))
    return np.asarray(order, dtype=np.int64)


def distribution_summary(points, source_min, source_span):
    """공통 16칸 공간 격자의 점유와 표본 경계를 비교 가능한 감사 값으로 기록합니다."""
    cell = np.minimum(15, np.floor((points - source_min) / np.where(source_span > 0, source_span, 1) * 16)).astype(np.int64)
    return {"count": len(points), "minimum": points.min(axis=0).tolist(),
            "maximum": points.max(axis=0).tolist(), "mean": points.mean(axis=0).tolist(),
            "occupied_cells_16": len(np.unique(cell, axis=0))}


def prepare(samples_path, manifest_path, preserved_result_path, output):
    """등록 배열만 선택하고 입력 지문·원본 행 ID·시드와 독립 평가 지문을 보존합니다."""
    started = perf_counter()
    preparation = read_json(samples_path.parent / "result.json")
    manifest = read_json(manifest_path)
    preserved = read_json(preserved_result_path)
    if preparation["input_fingerprint"] != manifest["input_fingerprint"] or preserved["input_fingerprint"] != manifest["input_fingerprint"]:
        raise ValueError("보존 준비·작업·결과의 입력 지문이 다릅니다.")
    if preparation["geometry_fingerprint"] != manifest["geometry"]["input_fingerprint"]:
        raise ValueError("보존 준비와 작업의 형상 지문이 다릅니다.")
    if preparation["initial_alignment_4x4"] != manifest["initial_alignment_4x4"] or preparation["normalization"] != preserved["normalization"]:
        raise ValueError("초기 변환 또는 정규화가 보존 결과와 다릅니다.")
    with np.load(samples_path, allow_pickle=False) as arrays:
        points = arrays["registration"].copy()
        fingerprints = {name: array_hash(arrays[name]) for name in arrays.files}
    if points.shape != (50000, 3) or fingerprints != preparation["sample_position_fingerprints"]:
        raise ValueError("보존 표본 개수 또는 배열 지문이 다릅니다.")
    seed = preparation["parameters"]["optimizer_seed"]
    for name in ("optimizer_seed", "probe_seed", "evaluation_seed"):
        if preparation["parameters"][name] != preserved["parameters"][name]:
            raise ValueError("보존 준비·결과의 표본 시드가 다릅니다.")
    selection_started = perf_counter()
    order = spatial_nested_order(points, seed)
    selection_seconds = perf_counter() - selection_started
    minimum = points.min(axis=0)
    span = np.ptp(points, axis=0)
    arrays = {"selection_priority_source_rows": order}
    stages = {}
    previous = np.empty(0, dtype=np.int64)
    for count in (5000, 20000, 50000):
        # 선택 우선순위와 등록 입력 순서는 구분하며 실제 입력은 항상 원본 행 순서를 유지합니다.
        rows = np.sort(order[:count])
        if not np.isin(previous, rows).all():
            raise ValueError("단계별 등록 표본이 중첩되지 않았습니다.")
        selected = points[rows]
        arrays[f"registration_{count}"] = selected
        arrays[f"source_rows_{count}"] = rows
        stages[str(count)] = {"positions_sha256": array_hash(selected), "source_rows_sha256": array_hash(rows),
                              "distribution": distribution_summary(selected, minimum, span)}
        previous = rows
    if not np.array_equal(arrays["registration_50000"], points):
        raise ValueError("최종 5만 표본이 보존 원본과 다릅니다.")
    report = {"scope": "EXPERIMENT_INPUT_ONLY", "policy": "LONGEST_AXIS_MEDIAN_BREADTH_FIRST_V1",
              "input_fingerprint": manifest["input_fingerprint"], "normalization": preparation["normalization"],
              "initial_alignment_4x4": manifest["initial_alignment_4x4"], "optimizer_seed": seed,
              "seed_generator": "PCG64",
              "seeds": {name: preparation["parameters"][name] for name in ("optimizer_seed", "probe_seed", "evaluation_seed")},
              "source_sample_fingerprints": fingerprints,
              "source_id": {"kind": "PRESERVED_REGISTRATION_ARRAY_ROW", "array_sha256": fingerprints["registration"],
                            "note": "원본 삼각면 ID가 저장되지 않아 배열 지문과 원본 행번호를 사용합니다."},
              "evaluation_policy": "UNCHANGED_EXTERNAL_INDEPENDENT_ARRAYS",
              "source_files": {str(path.resolve()): hashlib.sha256(path.read_bytes()).hexdigest()
                               for path in (samples_path, manifest_path, preserved_result_path)},
              "selection_seconds": selection_seconds, "preparation_seconds": perf_counter() - started,
              "stages": stages,
              "note": "네이티브 계산·제품 기본정책 변경은 수행하지 않습니다. 평가점은 선택 입력에 포함하지 않습니다."}
    output.mkdir(parents=True, exist_ok=False)
    np.savez(output / "nested-registration-samples.npz", **arrays)
    (output / "selection-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main():
    """보존 입력 세 경로와 새 출력 폴더를 받아 중첩 등록 표본을 저장합니다."""
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("samples", "manifest", "preserved-result", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    report = prepare(args.samples, args.manifest, args.preserved_result, args.output)
    print(json.dumps(report["stages"], ensure_ascii=False))


if __name__ == "__main__":
    main()
