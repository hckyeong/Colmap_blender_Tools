"""저장된 내장 메시·마커로 동일 초기 입력을 만들고 CC 버전별 성능을 비교합니다."""

from __future__ import annotations

import argparse
import hashlib
from importlib import import_module
from importlib.util import module_from_spec, spec_from_file_location
import json
from pathlib import Path
import shutil
import subprocess
import sys
from time import monotonic
from types import SimpleNamespace

import numpy as np


def file_hash(path):
    """원본 파일이 변경되지 않았는지 확인할 SHA-256을 계산합니다."""
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def write_json(path, value):
    """비교 자료를 읽기 쉬운 UTF-8 JSON으로 기록합니다."""
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


def transformed(points, matrix):
    """마커 좌표에 기존 형상 어댑터와 같은 행벡터 변환을 적용합니다."""
    return np.asarray(points) @ matrix[:3, :3].T + matrix[:3, 3]


def extract_fixture(args):
    """외부 COLMAP을 복원하지 않고 내장 OBJ와 저장 마커에서 고정 입력을 추출합니다."""
    import bpy

    blend = Path(bpy.data.filepath).resolve(strict=True)
    before_hash = file_hash(blend)
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    if any(output.iterdir()):
        raise RuntimeError("고정 입력 출력 폴더에 기존 파일이 있습니다.")
    module_name = args.module
    if args.module_root:
        module_root = Path(args.module_root).resolve(strict=True)
        module_name = "overlap_fixture_installed"
        spec = spec_from_file_location(module_name, module_root / "__init__.py", submodule_search_locations=[str(module_root)])
        package = module_from_spec(spec)
        sys.modules[module_name] = package
        spec.loader.exec_module(package)
    prefix = module_name + ".tools.align."
    axes_module = import_module(prefix + "display_axes")
    embedded = import_module(prefix + "mesh_io.embedded_obj_store")
    adapter = import_module(prefix + "alignment.icp_geometry")
    marker_solver = import_module(prefix + "alignment.marker_sim3")
    types = import_module(prefix + "alignment.types")
    store = import_module(prefix + "project_persistence.store")
    text = store.active_project_text()
    if text is None:
        raise RuntimeError("활성 프로젝트 저장 자료를 찾지 못했습니다.")
    saved = json.loads(text.as_string())
    pairs = saved["markers"]["completed_pairs"]
    if len(pairs) != 3:
        raise RuntimeError("실데이터 비교 입력은 저장된 마커 3쌍이 필요합니다.")
    role_ids = {role: {pair[role]["model_id"] for pair in pairs} for role in ("moving", "reference")}
    if any(len(ids) != 1 for ids in role_ids.values()):
        raise RuntimeError("마커 쌍이 각각 동일한 이동·기준 모델을 가리키지 않습니다.")
    selected_ids = {role: next(iter(ids)) for role, ids in role_ids.items()}
    models = {model["model_id"]: model for model in saved["models"]}
    displays, alignments, proxies, records = {}, {}, {}, {}
    for model_id in selected_ids.values():
        model = models[model_id]
        axes = model["companion_axes"]
        display = axes_module.create_model_display_transform(
            axes["preset_id"], axes["axis_forward"], axes["axis_up"],
            rotation_xyz_degrees=axes["rotation_xyz_degrees"])
        if display.fingerprint != axes["display_transform_fingerprint"]:
            raise RuntimeError("저장된 표시 축 지문이 기존 축 변환 구현과 다릅니다.")
        displays[model_id] = display
        alignments[model_id] = types.Sim3Transform(np.asarray(model["alignment"]["matrix_float64"]))
        source = model["companion_source"]
        mesh = embedded.find_embedded_obj_mesh(source["embedded_obj_asset_uuid"])
        proxies[model_id] = embedded.build_triangle_proxy_from_embedded_mesh(
            mesh, model_id=model_id, display_name=model["display_name"],
            source_path=source["path_absolute"], geometry_only=True)
        records[model_id] = SimpleNamespace(model_id=model_id, display_name=model["display_name"])
    marker_world = {}
    for role, model_id in selected_ids.items():
        if any(pair[role]["geometry_source"] != "COMPANION" for pair in pairs):
            raise RuntimeError("저장 마커가 내장 동반 메시를 가리키지 않습니다.")
        marker_world[role] = transformed([pair[role]["local_position"] for pair in pairs],
                                        alignments[model_id].matrix_float64 @ displays[model_id].matrix_float64)
    marker_delta = marker_solver.solve_similarity_transform(
        marker_world["reference"], marker_world["moving"], use_uniform_scale=not args.rigid_markers)
    initial = marker_delta.matrix_float64 @ alignments[selected_ids["moving"]].matrix_float64
    residuals = np.linalg.norm(marker_world["reference"] - transformed(marker_world["moving"], marker_delta.matrix_float64), axis=1)
    marker_rms = float(np.sqrt(np.mean(residuals ** 2)))
    repository = SimpleNamespace(companion_sources={}, companion_display_transform_for_model_id=displays.__getitem__,
                                 alignment_transform_for_model_id=alignments.__getitem__)
    entries = {}
    for role, model_id in selected_ids.items():
        selected = adapter._SelectedGeometry(records[model_id], "TRIANGLE_MESH", "COMPANION", proxies[model_id])
        prepared = adapter._prepare_source(repository, selected, initial if role == "moving" else None,
                                           verify_content=True, validate_geometry=True)
        geometry = adapter._geometry_input(prepared)
        positions_name, triangles_name = f"{role}_positions.bin", f"{role}_triangles.bin"
        np.ascontiguousarray(geometry.positions_float64, dtype="<f8").tofile(output / positions_name)
        np.ascontiguousarray(geometry.triangles_uint32, dtype="<u4").tofile(output / triangles_name)
        entries[role] = {"kind": "TRIANGLE_MESH", "positions_file": positions_name,
                         "triangles_file": triangles_name, "point_count": len(geometry.positions_float64),
                         "triangle_count": len(geometry.triangles_uint32), "provenance": geometry.provenance.to_mapping(),
                         "positions_sha256": file_hash(output / positions_name),
                         "triangles_sha256": file_hash(output / triangles_name)}
    after_hash = file_hash(blend)
    if before_hash != after_hash:
        raise RuntimeError("실데이터 추출 중 원본 blend 파일의 지문이 변경되었습니다.")
    source = {"blend": str(blend), "blend_sha256_before": before_hash, "blend_sha256_after": after_hash,
              "saved_project_text": text.name, "installed_module": args.module, "installed_module_root": args.module_root,
              "moving_model_id": selected_ids["moving"], "reference_model_ids": [selected_ids["reference"]],
              "marker_count": len(pairs), "marker_initial_rms": marker_rms,
              "marker_residuals": residuals.tolist(), "marker_uniform_scale": not args.rigid_markers,
              "initial_alignment_4x4": initial.tolist(), "geometry": entries,
              "marker_signature": hashlib.sha256(json.dumps(pairs, sort_keys=True).encode("utf-8")).hexdigest()}
    write_json(output / "saved_project.json", saved)
    write_json(output / "source.json", source)
    print(json.dumps(source, ensure_ascii=False), flush=True)


def run_benchmark(args):
    """같은 고정 입력과 명시한 반복 상한으로 지정 버전의 독립 작업을 실행합니다."""
    fixture = Path(args.fixture).resolve(strict=True)
    source = json.loads((fixture / "source.json").read_text(encoding="utf-8"))
    solver_source = Path(args.solver_source).resolve(strict=True)
    sys.path.insert(0, str(solver_source))
    contract = import_module("icp_cccore_contract")
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    parameters = {"cc_mesh_sample_count": 50000, "cc_evaluation_sample_count": 30000,
                  "cc_max_iterations": args.max_iterations or contract.resolve_parameters({})["max_iterations"],
                  "cc_max_threads": args.threads}
    defaults = contract.resolve_parameters({})
    if "scale_refine_max_iterations" in defaults:
        parameters["cc_scale_refine_max_iterations"] = args.max_iterations or defaults["scale_refine_max_iterations"]
    entries = source["geometry"]
    for entry in entries.values():
        for key in ("positions_file", "triangles_file"):
            shutil.copyfile(fixture / entry[key], output / entry[key])
    manifest = {"manifest_version": 1, "job_id": output.name, "backend_id": contract.CC_BACKEND_ID,
                "algorithm_version": contract.CC_ALGORITHM_VERSION, "backend_algorithm_version": contract.CC_ALGORITHM_VERSION,
                "job_protocol_version": contract.CC_JOB_PROTOCOL_VERSION,
                "input_fingerprint": file_hash(fixture / "source.json"),
                "parameter_fingerprint": hashlib.sha256(json.dumps(parameters, sort_keys=True).encode()).hexdigest(),
                "moving_model_id": source["moving_model_id"], "reference_model_ids": source["reference_model_ids"],
                "initial_alignment_4x4": source["initial_alignment_4x4"], "marker_signature": source["marker_signature"],
                "marker_initial_rms": source["marker_initial_rms"], "model_revision_snapshot": [], "model_state_snapshot": [],
                "geometry_mode": "MESH_TO_MESH", "reverse_registration": False, "geometry": entries, "parameters": parameters}
    write_json(output / "manifest.json", manifest)
    contract.validate_manifest(manifest, output)
    command = [sys.executable, "-I", str(solver_source / "icp_cccore_worker.py"), "--job-dir", str(output),
               "--native-worker", str(Path(args.native_worker).resolve(strict=True))]
    started = monotonic()
    with (output / "benchmark-worker.log").open("w", encoding="utf-8") as log:
        completed = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, timeout=args.timeout,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    report = {"version": contract.CC_ALGORITHM_VERSION, "wall_seconds": monotonic() - started,
              "returncode": completed.returncode, "fixture_source_sha256": file_hash(fixture / "source.json"),
              "native_worker_sha256": file_hash(args.native_worker), "threads": args.threads,
              "max_iterations": parameters["cc_max_iterations"],
              "scale_refine_max_iterations": parameters.get("cc_scale_refine_max_iterations", parameters["cc_max_iterations"]),
              "blend_sha256_after": file_hash(source["blend"])}
    if report["blend_sha256_after"] != source["blend_sha256_before"]:
        raise RuntimeError("실데이터 비교 후 원본 blend 파일의 지문이 변경되었습니다.")
    for name in ("result", "error"):
        path = output / f"{name}.json"
        if path.is_file():
            report[name] = json.loads(path.read_text(encoding="utf-8"))
    write_json(output / "benchmark.json", report)
    print(json.dumps({key: value for key, value in report.items() if key not in ("result", "error")}, ensure_ascii=False), flush=True)
    if completed.returncode:
        raise RuntimeError(f"CC 비교 작업이 실패했습니다. 상세 기록: {output}")


def compare_evaluations(args):
    """새 작업이 고정한 동일 평가점에 이전·현재 행렬을 적용해 오차를 공정하게 비교합니다."""
    import numpy as np
    sys.path.insert(0, str(Path(args.solver_source).resolve(strict=True)))
    contract = import_module("icp_cccore_contract")
    solver = import_module("icp_cccore_solver")
    previous = Path(args.baseline_job).resolve(strict=True)
    current = Path(args.candidate_job).resolve(strict=True)
    metadata = contract.read_json_object(current / "result.json")
    old_metadata = contract.read_json_object(previous / "result.json")
    if metadata["geometry_mode"] != "MESH_TO_MESH":
        raise ValueError("이 비교는 양쪽 메시의 독립 평가 표본을 사용합니다.")
    if not np.array_equal(metadata["initial_alignment_4x4"], old_metadata["initial_alignment_4x4"]):
        raise ValueError("두 결과의 초기 정렬이 다릅니다.")
    manifest = contract.read_json_object(current / "manifest.json")
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    manifest["job_id"] = output.name
    normalization = solver.Normalization(np.asarray(metadata["normalization"]["origin_project_world"]),
                                         metadata["normalization"]["unit_project_world"])
    geometries = []
    for role in ("moving", "reference"):
        geometry = contract.load_geometry(current, manifest["geometry"][role])
        geometries.append(contract.CcCoreGeometry(geometry.kind, normalization.apply(geometry.positions),
                                                 geometry.triangles, geometry.provenance))
    sample_files = ("native/baseline-moving-evaluation-positions.bin",
                    "native/baseline-reference-inverse-evaluation-positions.bin")
    samples = [np.fromfile(contract.job_path(current, name), dtype="<f8").reshape(-1, 3) for name in sample_files]
    counts = {name: entry["evaluation_sample_count"] for name, entry in metadata["sampling_diagnostics"]["directions"].items()}
    deltas = {"initial": np.eye(4)}
    for name, path in (("v3", previous), ("v4", current)):
        with np.load(path / "result.npz", allow_pickle=False) as arrays:
            world, _scale = contract.matrix_sim3(arrays["delta_4x4"], "비교 결과 변환")
        delta = world.copy()
        delta[:3, 3] = (world[:3, :3] @ normalization.origin + world[:3, 3] - normalization.origin) / normalization.unit
        deltas[name] = delta
    parameters = contract.resolve_parameters(manifest["parameters"])
    runner = solver.NativeRunner(output, Path(args.native_worker).resolve(strict=True), parameters, session_identity=manifest)
    report = {"baseline_job": str(previous), "candidate_job": str(current), "evaluation_counts": counts, "metrics": {}}
    try:
        runner.start()
        for name, delta in deltas.items():
            report["metrics"][name] = solver.evaluate_candidate(
                runner, name, delta, *geometries, *samples, normalization,
                metadata["quality_policy"], stage="PREPARING", overlap_sample_counts=counts,
            )
        runner.close()
    except BaseException:
        runner.abort()
        raise
    write_json(output / "comparison.json", report)
    print(json.dumps({name: {"overlap_mean_rms": metric["overlap_mean_rms"],
                            "full_mean_rms": metric["full_reference_metrics"]["mean_rms"],
                            "valid_ratio": metric["correspondence_coverage"]["valid_ratio"]}
                      for name, metric in report["metrics"].items()}, ensure_ascii=False))


def main():
    """Blender 추출과 외부 Python 비교 실행의 명시적 인자를 해석합니다."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("extract", "run", "compare"))
    parser.add_argument("--output", required=True)
    parser.add_argument("--module", default="bl_ext.user_default.colmap_tools")
    parser.add_argument("--module-root")
    parser.add_argument("--rigid-markers", action="store_true")
    parser.add_argument("--fixture")
    parser.add_argument("--solver-source")
    parser.add_argument("--baseline-job")
    parser.add_argument("--candidate-job")
    parser.add_argument("--native-worker")
    parser.add_argument("--threads", type=int, default=16)
    parser.add_argument("--max-iterations", type=int)
    parser.add_argument("--timeout", type=int, default=900)
    arguments = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else sys.argv[1:]
    args = parser.parse_args(arguments)
    if args.action == "extract":
        extract_fixture(args)
    elif args.action == "compare":
        if not all((args.baseline_job, args.candidate_job, args.solver_source, args.native_worker)):
            parser.error("동일 표본 비교에는 두 작업과 솔버·네이티브 경로가 필요합니다.")
        compare_evaluations(args)
    else:
        if not all((args.fixture, args.solver_source, args.native_worker)):
            parser.error("비교 실행에는 입력 자료·솔버 경로·네이티브 워커 경로가 필요합니다.")
        run_benchmark(args)


if __name__ == "__main__":
    main()
