"""이전 실데이터 보고서의 조건으로 설치된 CC 정렬과 시스템 페이징을 재측정합니다."""

from collections.abc import Mapping
import argparse
from datetime import datetime, timezone
import hashlib
from importlib import import_module
import json
import os
from pathlib import Path
import platform
import sys
import time

import addon_utils
import bpy
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from windows_paging import MemoryMonitor, PagingMonitor


def plain(value):
    """행렬과 불변 메타데이터를 UTF-8 JSON 자료형으로 변환합니다."""
    if isinstance(value, Mapping):
        return {str(key): plain(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [plain(item) for item in value]
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, np.generic):
        return value.item()
    return value


def file_hash(path):
    """계측 밖에서 읽기 전용 입력과 설치 바이너리의 SHA-256을 계산합니다."""
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def main():
    """독립 Blender 프로세스에서 복원·동일 조건 정렬·결과 비교를 수행합니다."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--module", default="bl_ext.colmap_benchmark.colmap_tools")
    parser.add_argument("--archive", required=True)
    args = parser.parse_args(sys.argv[sys.argv.index("--") + 1:])
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=False)
    baseline = json.loads(Path(args.baseline).read_text(encoding="utf-8-sig"))
    report = {"passed": False, "created_utc": datetime.now(timezone.utc).isoformat(),
              "baseline": args.baseline, "archive_sha256": file_hash(args.archive),
              "input_sha256": file_hash(bpy.data.filepath), "scenario": baseline["scenario"],
              "system": {"platform": platform.platform(), "processor": platform.processor(),
                         "logical_processors": os.cpu_count(), "blender": bpy.app.version_string}}
    memory = MemoryMonitor()
    paging = None
    runtime = None
    ir = None
    try:
        assert report["input_sha256"] == "2b07982399dffcd6f7dc45a74336d271b15d19c6e6a7405f18fd4c9d6a0a4adc"
        addon_utils.enable(args.module, default_set=True, persistent=True)
        package = import_module(args.module)
        package_root = Path(package.__file__).resolve().parent
        base = args.module + ".tools.align."
        state = import_module(base + "state")
        persistence = import_module(base + "project_persistence.runtime")
        ir = import_module(base + "alignment.icp_runtime")
        cc = import_module(base + "alignment.icp_cccore_runtime")
        properties = import_module(base + "properties")
        transforms = import_module(base + "alignment.types")
        python_path = properties.bundled_cccore_python_path()
        native_path = cc.bundled_cccore_worker_path()
        Path(python_path).resolve().relative_to(package_root)
        Path(native_path).resolve().relative_to(package_root)
        assert file_hash(native_path) == baseline["runtime"]["native_worker_sha256"]
        report["runtime"] = {"package": str(package_root), "python": python_path,
                             "native_worker": native_path, "native_worker_sha256": file_hash(native_path)}
        started = time.monotonic()
        runtime = state.RuntimeState()
        assert persistence.schedule_project_restore_if_available(runtime), runtime.project_restore.last_message
        while runtime.project_restore.phase == "RESTORING":
            if time.monotonic() - started > 300:
                raise TimeoutError("저장 데이터 복원 제한 시간을 넘었습니다.")
            persistence._restore_timer(runtime)
            time.sleep(.01)
        assert runtime.project_restore.phase == "IDLE", runtime.project_restore.last_message
        runtime.session_active = True
        report["restore_seconds"] = time.monotonic() - started
        runtime.marker_selection.clear()
        runtime.marker_alignment.clear()
        runtime.icp_alignment.clear()
        catalog = runtime.model_catalog
        repository = runtime.model_repository
        selection = baseline["selection"]
        moving = selection["moving_model_id"]
        reference = selection["reference_model_ids"][0]
        if catalog.models[moving].registration_status == "REGISTERED":
            assert catalog.transfer_model_group(moving, source="MANUAL", has_marker_draft=False, completed_pair_count=0), catalog.message
        catalog.clear_selected_model()
        assert catalog.select_model(moving, has_marker_draft=False, completed_pair_count=0), catalog.message
        assert catalog.toggle_alignment_selection(reference, has_marker_draft=False, completed_pair_count=0), catalog.message
        for model_id in (reference, moving):
            assert catalog.set_model_visibility(model_id, True)
            assert catalog.set_geometry_visibility(model_id, show_points=False, show_surface=True)
            target = np.asarray(baseline["alignment_before"][model_id], dtype=np.float64)
            saved = repository.alignment_transform_for_model_id(model_id).matrix_float64
            expected_saved = baseline["scenario"].get("alignment_before_perturbation", target) if model_id == moving else target
            np.testing.assert_allclose(saved, expected_saved, rtol=0, atol=1e-12)
            repository.set_alignment_transform(model_id, transforms.Sim3Transform(target))
        before = repository.alignment_transform_for_model_id(moving).matrix_float64.copy()
        requested = baseline["input_parameters"]["cc_max_threads"]
        bpy.context.preferences.addons[args.module].preferences.cc_cpu_thread_limit = requested
        assert properties.configured_cc_cpu_thread_limit() == requested
        report["blender_memory_after_restore"] = memory.read(os.getpid())
        paging = PagingMonitor()
        for _ in range(3):
            time.sleep(1)
            paging.sample("baseline")
        started = time.monotonic()
        handle = ir.begin_icp_job(runtime, solver_backend="CCCORELIB", open3d_python_path=python_path)
        report["input_preparation_and_launch_seconds"] = time.monotonic() - started
        report["input_parameters"] = plain(runtime.icp_alignment.input_data.parameters.to_mapping())
        assert report["input_parameters"] == baseline["input_parameters"]
        last_memory = last_notice = 0.0
        while runtime.icp_alignment.is_running:
            now = time.monotonic()
            if now - last_memory >= .1:
                memory.sample(handle.process.pid)
                last_memory = now
            if now - paging.last >= 1:
                paging.sample("alignment")
            ir.poll_icp_job(runtime)
            if now - last_notice >= 15:
                print(f"실데이터 정렬 진행: {now - started:.1f}초", flush=True)
                last_notice = now
            if now - started > 900:
                raise TimeoutError("정렬 측정이 15분 제한을 넘었습니다.")
            time.sleep(.02)
        report["total_alignment_seconds"] = time.monotonic() - started
        paging.sample("alignment")
        assert runtime.icp_alignment.has_preview, runtime.icp_alignment.last_error
        result = runtime.icp_alignment.result
        metadata = plain(result.metadata)
        np.testing.assert_allclose(repository.alignment_transform_for_model_id(moving).matrix_float64, before, rtol=0, atol=0)
        final = result.final_alignment.matrix_float64
        delta = result.icp_delta.matrix_float64
        old = baseline["result"]
        np.testing.assert_allclose(final, old["final_alignment"], rtol=0, atol=1e-9)
        np.testing.assert_allclose(delta, old["delta"], rtol=0, atol=1e-9)
        for key in ("geometry_input_fingerprint", "parameters", "best_stage", "quality", "native_process_count", "physical_native_command_count"):
            assert metadata[key] == old["metadata"][key], key
        metrics = ("rms", "mean_rms", "trimmed_rms_90", "median", "p95")
        difference = {key: abs(metadata["best_candidate_metrics"][key] - old["metadata"]["best_candidate_metrics"][key])
                      for key in metrics if key in metadata["best_candidate_metrics"]}
        assert max(difference.values(), default=0) <= 1e-9, difference
        meshes = [row for row in metadata["geometry_statistics"] if row["triangle_count"]]
        assert len(meshes) == 2 and all(row["load_count"] == 1 for row in meshes)
        report.update(passed=True, result={"metadata": metadata, "final_alignment": final, "delta": delta},
                      comparison={"matrix_max_abs_difference": float(np.max(np.abs(final - old["final_alignment"]))),
                                  "metrics_abs_difference": difference},
                      blender_memory_after_alignment=memory.read(os.getpid()))
        print(f"실데이터 정렬 검증 통과: {report['total_alignment_seconds']:.3f}초", flush=True)
    except Exception as error:
        report["error"] = f"{type(error).__name__}: {error}"
        raise
    finally:
        report["worker_memory"] = memory.report()
        if paging is not None:
            report["paging"] = paging.report()
            paging.close()
        (output / "result.json").write_text(json.dumps(plain(report), ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
        if runtime is not None and ir is not None:
            ir.invalidate_icp(runtime, "벤치마크 프로세스를 정리했습니다.")


if __name__ == "__main__":
    main()
