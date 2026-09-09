"""비대칭 곡면의 네 겹침 비율을 실제 CC 정렬과 사전 고정 허용오차로 검사합니다."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory

import numpy as np


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools" / "align" / "alignment"))
import icp_cccore_contract as contract
import icp_cccore_solver as solver


CASES = {"full": 1.0, "70": 0.7, "40": 0.4, "20": 0.2}
# 실제 계산 전에 고정하며 실패 시 실행 인자로 완화할 수 없습니다.
MAX_POSITION_RMS_DIAGONAL_FRACTION = 0.003
MAX_RELATIVE_SCALE_ERROR = 0.003
MAX_ROTATION_ERROR_DEGREES = 0.3


def curved_patch(x_start: float) -> tuple[np.ndarray, np.ndarray]:
    """같은 비대칭 물결 표면에서 가로 길이 2인 창을 동일 간격으로 잘라 만듭니다."""
    x, y = np.meshgrid(np.linspace(x_start, x_start + 2.0, 41), np.linspace(-1.0, 1.0, 31), indexing="ij")
    z = (0.23 * np.sin(2.3 * x + 0.7 * y) + 0.17 * np.cos(3.1 * y - 0.4 * x)
         + 0.13 * x * y + 0.09 * np.sin(4.7 * x - 1.3 * y))
    vertices = np.column_stack((x.ravel(), y.ravel(), z.ravel()))
    triangles = []
    for row in range(40):
        for column in range(30):
            index = row * 31 + column
            triangles.extend(((index, index + 31, index + 32), (index, index + 32, index + 1)))
    return vertices, np.asarray(triangles, dtype=np.uint32)


def known_transform(diagonal: float) -> np.ndarray:
    """작은 잔여 오차를 나타내는 배율 1.01·회전 0.35도·대각선의 0.002배 이동을 고정합니다."""
    angle = np.deg2rad(0.35)
    transform = np.eye(4)
    transform[:3, :3] = 1.01 * np.asarray(((np.cos(angle), -np.sin(angle), 0.0),
                                         (np.sin(angle), np.cos(angle), 0.0), (0.0, 0.0, 1.0)))
    direction = np.asarray((0.7, -0.3, 0.6))
    transform[:3, 3] = direction / np.linalg.norm(direction) * diagonal * 0.002
    return transform


def create_job(directory: Path, fraction: float, threads: int) -> tuple[dict, np.ndarray, np.ndarray, float]:
    """동일 표면의 서로 다른 창에 알려진 역변환을 적용하여 원본 메시 작업을 기록합니다."""
    truth, triangles = curved_patch(-1.0)
    reference, reference_triangles = curved_patch(-1.0 + 2.0 * (1.0 - fraction))
    diagonal = float(np.linalg.norm(np.ptp(truth, axis=0)))
    expected = known_transform(diagonal)
    moving = solver.transform_positions(truth, np.linalg.inv(expected))
    geometry = {}
    for role, positions, faces in (("moving", moving, triangles), ("reference", reference, reference_triangles)):
        positions_name, triangles_name = f"{role}.bin", f"{role}-triangles.bin"
        np.asarray(positions, dtype="<f8").tofile(directory / positions_name)
        np.asarray(faces, dtype="<u4").tofile(directory / triangles_name)
        geometry[role] = {"kind": "TRIANGLE_MESH", "positions_file": positions_name,
                          "point_count": len(positions), "triangles_file": triangles_name, "triangle_count": len(faces)}
    geometry.update(input_fingerprint=f"curved-overlap-{fraction}", provenance={
        "synthetic": True, "parameter_domain_overlap_fraction": fraction,
        "overlap_definition": "동일 가로 길이 창의 공유 구간 비율이며 곡면 넓이 비율과 구분합니다.",
    })
    manifest = {
        **contract.identity_payload({"job_id": directory.name, "moving_model_id": "P",
                                     "input_fingerprint": hashlib.sha256(f"curved-overlap-{fraction}".encode("ascii")).hexdigest()}),
        "parameter_fingerprint": "fixed-v4-partial-overlap", "reference_model_ids": ["A"],
        "model_revision_snapshot": [["P", 0], ["A", 0]], "model_state_snapshot": [],
        "marker_signature": "current-alignment", "marker_initial_rms": 0.0,
        "initial_alignment_4x4": np.eye(4).tolist(), "geometry": geometry,
        "geometry_mode": "MESH_TO_MESH", "reverse_registration": False,
        "parameters": {"cc_max_threads": threads},
    }
    contract.write_json_atomic(directory / "manifest.json", manifest)
    return manifest, expected, moving, diagonal


def input_hashes(directory: Path, manifest: dict) -> dict[str, str]:
    """원본 위치·삼각면 바이너리의 내용을 계산 전후 비교할 지문으로 고정합니다."""
    names = [entry[field] for role in ("moving", "reference")
             for entry in (manifest["geometry"][role],) for field in ("positions_file", "triangles_file")]
    return {name: hashlib.sha256((directory / name).read_bytes()).hexdigest() for name in names}


def measure_truth(result, expected: np.ndarray, moving: np.ndarray, diagonal: float) -> dict:
    """겹침 점만이 아닌 이동 원본 전체에서 알려진 변환의 위치·배율·회전 오차를 측정합니다."""
    delta = result.delta_4x4
    residual = solver.transform_positions(moving, delta) - solver.transform_positions(moving, expected)
    rms = float(np.sqrt(np.mean(np.sum(residual * residual, axis=1))))
    expected_scale = float(np.cbrt(np.linalg.det(expected[:3, :3])))
    actual_scale = float(np.cbrt(np.linalg.det(delta[:3, :3])))
    relative_rotation = (delta[:3, :3] / actual_scale) @ (expected[:3, :3] / expected_scale).T
    angle = float(np.rad2deg(np.arccos(np.clip((np.trace(relative_rotation) - 1.0) / 2.0, -1.0, 1.0))))
    return {"position_rms": rms, "position_rms_diagonal_fraction": rms / diagonal,
            "relative_scale_error": abs(actual_scale / expected_scale - 1.0),
            "rotation_error_degrees": angle, "diagonal": diagonal,
            "position_rms_limit": diagonal * MAX_POSITION_RMS_DIAGONAL_FRACTION}


def validate_result(result, manifest: dict, expected: np.ndarray, moving: np.ndarray, diagonal: float) -> dict:
    """원본 대상·표본 수·고정 반경·선택 지표와 사전 선언한 정답 허용오차를 함께 검사합니다."""
    metadata = result.metadata
    measured = measure_truth(result, expected, moving, diagonal)
    assert measured["position_rms_diagonal_fraction"] <= MAX_POSITION_RMS_DIAGONAL_FRACTION, measured
    assert measured["relative_scale_error"] <= MAX_RELATIVE_SCALE_ERROR, measured
    assert measured["rotation_error_degrees"] <= MAX_ROTATION_ERROR_DEGREES, measured
    np.testing.assert_allclose(result.final_alignment_4x4, result.delta_4x4, rtol=0.0, atol=1e-12)
    sampling = metadata["sampling_diagnostics"]
    assert sampling["registration_sample_count"] == 50000
    assert sampling["optimizer_seed"] != sampling["evaluation_seed"]
    policy = metadata["quality_policy"]
    tau = policy["correspondence_radius_normalized"]
    assert np.isclose(policy["candidate_radius_normalized"], 2.0 * tau, rtol=1e-12, atol=1e-12)
    assert tau <= 0.05 * policy["data_diagonal_normalized"]
    assert np.isclose(tau, max(0.01 * policy["data_diagonal_normalized"],
                              3.0 * policy["model_point_spacing_normalized"],
                              2.0 * policy["marker_initial_rms_normalized"]), rtol=1e-12, atol=1e-12)
    assert set(sampling["directions"]) == {"moving_to_reference", "reference_to_moving"}
    for direction in sampling["directions"].values():
        assert direction["evaluation_sample_count"] == 30000
        assert direction["evaluation"]["sample_count"] == 30000
        assert np.isclose(direction["evaluation"]["accepted_radius"], tau, rtol=1e-12, atol=1e-12)
        if "registration" in direction:
            assert direction["registration"]["sample_count"] == 50000
            assert direction["registration_sample_fingerprint"] != direction["evaluation_sample_fingerprint"]
            assert np.isclose(direction["registration"]["accepted_radius"], 2.0 * tau, rtol=1e-12, atol=1e-12)
    for metric in metadata["candidate_metrics"].values():
        assert metric["count"] == metric["evaluation_sample_count"] == 60000
        assert all(direction["count"] == 30000 for direction in metric["directions"].values())
        assert metric["correspondence_coverage"]["count"] == 30000
    for role in ("moving", "reference"):
        native_model = next(item for item in metadata["geometry_statistics"] if item["geometry_id"] == f"{role}-model")
        assert native_model["point_count"] == manifest["geometry"][role]["point_count"]
        assert native_model["triangle_count"] == manifest["geometry"][role]["triangle_count"]
    assert all(stage["data_point_count"] == 50000 for stage in metadata["registration_stages"])
    assert all(stage["filter_out_farthest_points"] is True for stage in metadata["registration_stages"])
    # 전체 참고 수치를 뒤집어도 같은 후보를 선택하는지 순수 선택 경계에서 확인합니다.
    changed_metrics = copy.deepcopy(metadata["candidate_metrics"])
    for name, metric in changed_metrics.items():
        replacement = 1e12 if name == metadata["best_stage"] else 0.0
        full = metric["full_reference_metrics"]
        for summary in (full, *full["directions"].values()):
            for field in tuple(summary):
                if field.startswith(("rms", "mean_rms", "trimmed_rms", "median", "p95")):
                    summary[field] = replacement
    selected = solver.select_candidate(changed_metrics, metadata["diagnostics"], policy)
    assert selected[0] == metadata["best_stage"]
    return measured


def run_case(name: str, native_worker: Path, threads: int) -> dict:
    """하나의 실제 작업을 완료·회수하고 실패 시에도 정답 오차를 보고서에 남깁니다."""
    with TemporaryDirectory(prefix=f"cc-overlap-{name}-") as temporary:
        directory = Path(temporary)
        manifest, expected, moving, diagonal = create_job(directory, CASES[name], threads)
        original_hashes = input_hashes(directory, manifest)
        result = solver.solve_cccore_job(directory, native_worker)
        report = {"case": name, "overlap_fraction": CASES[name], **measure_truth(result, expected, moving, diagonal),
                  "best_stage": result.metadata["best_stage"], "quality": result.metadata["quality"],
                  "overlap_mean_rms": result.metadata["best_candidate_metrics"]["overlap_mean_rms"],
                  "full_reference_rms": result.metadata["best_candidate_metrics"]["full_reference_metrics"]["rms"],
                  "elapsed_seconds": result.metadata["elapsed_seconds"],
                  "preparing_seconds": result.metadata["preparing_seconds"],
                  "registration_stages": result.metadata["registration_stages"],
                  "sampling_diagnostics": result.metadata["sampling_diagnostics"],
                  "native_process_count": result.metadata["native_process_count"],
                  "physical_native_command_count": result.metadata["physical_native_command_count"],
                  "session_statistics": result.metadata["session_statistics"],
                  "model_load_counts": {item["geometry_id"]: item["load_count"]
                      for item in result.metadata["geometry_statistics"] if item["geometry_id"].endswith("-model")},
                  "native_worker_sha256": result.metadata["native_worker_sha256"]}
        try:
            assert input_hashes(directory, manifest) == original_hashes
            validate_result(result, manifest, expected, moving, diagonal)
        except (AssertionError, ValueError) as error:
            report.update(passed=False, error=str(error) or "고정 계약 검사에 실패했습니다.")
        else:
            report["passed"] = True
        return report


def main() -> int:
    """선택한 사례를 한 번에 하나씩 실행하고 고정 허용오차와 결과를 JSON으로 저장합니다."""
    parser = argparse.ArgumentParser(description="비대칭 곡면 부분 겹침 실제 CC 검사")
    parser.add_argument("--case", choices=tuple(CASES), nargs="+", default=list(CASES))
    parser.add_argument("--native-worker", type=Path, default=ROOT / ".cccore-runtime" / "cccore" / "colmap_cccore_worker.exe")
    parser.add_argument("--threads", type=int, default=1)
    parser.add_argument("--output", type=Path, default=ROOT / ".artifacts" / "cccore-partial-overlap-report.json")
    args = parser.parse_args()
    if not args.native_worker.is_file() or not 1 <= args.threads <= 1024:
        parser.error("실제 네이티브 워커와 1~1024 범위 스레드 수가 필요합니다.")
    reports = []
    for name in dict.fromkeys(args.case):
        print(f"부분 겹침 {name} 검사 시작", flush=True)
        try:
            report = run_case(name, args.native_worker, args.threads)
        except Exception as error:
            report = {"case": name, "passed": False, "error": str(error)}
        reports.append(report)
        print(json.dumps(report, ensure_ascii=False, allow_nan=False), flush=True)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps({"tolerances": {
            "position_rms_diagonal_fraction": MAX_POSITION_RMS_DIAGONAL_FRACTION,
            "relative_scale_error": MAX_RELATIVE_SCALE_ERROR,
            "rotation_error_degrees": MAX_ROTATION_ERROR_DEGREES}, "cases": reports},
            ensure_ascii=False, allow_nan=False, indent=2), encoding="utf-8")
        if not report["passed"]:
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
