"""같은 저장 프로젝트에서 공개 마커 경로와 기존 BVH의 시간·원본 좌표를 비교합니다."""
from pathlib import Path
from types import SimpleNamespace
import json
import logging
import os
import sys
import time
import traceback
from unittest.mock import patch

import bpy
import gpu
import numpy as np
from mathutils import Matrix

ROOT = Path(__file__).resolve().parents[3]
OUT = ROOT / "build/verification/screen-marker-20261021"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from screen_marker_oracle import nearest_original_triangle
from tools.align import state
from tools.align.project_persistence import runtime as persistence
from tools.align.viewport import picking, point_data, renderer, screen_pick


def write_report(report):
    """중간 결과도 남겨 실패 지점과 전체 측정의 완료 여부를 구분합니다."""
    (OUT / "benchmark.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


def draw_existing_display_batch(batch, model_matrix, view):
    """클릭 전에 이미 화면에 표시된 배치의 GPU 업로드를 일반 표시 셰이더로 완료합니다."""
    offscreen = gpu.types.GPUOffScreen(8, 8)
    try:
        with offscreen.bind():
            shader = renderer._mesh_shader()
            shader.bind()
            shader.uniform_float("ModelViewProjectionMatrix", Matrix((view.perspective_matrix @ model_matrix).tolist()))
            shader.uniform_float("NormalMatrix", Matrix.Identity(3))
            shader.uniform_float("mesh_color", (1, 1, 1, 1))
            shader.uniform_float("opacity", 1.0)
            batch.draw(shader)
            gpu.state.active_framebuffer_get().read_color(0, 0, 1, 1, 4, 0, "FLOAT")
    finally:
        offscreen.free()


def run_benchmark():
    """표시 준비가 끝난 동일 모델에서 첫 선택과 반복 선택 및 화면별 정확도를 측정합니다."""
    logging.disable(logging.INFO)
    bpy.context.preferences.use_preferences_save = False
    report = {"completed": False, "blender": bpy.app.version_string, "gpu": gpu.platform.renderer_get(),
              "file": bpy.data.filepath, "models": [], "errors": []}
    try:
        runtime = state.RUNTIME
        started = time.perf_counter()
        assert persistence.schedule_project_restore_if_available(runtime), runtime.project_restore.last_message
        deadline = time.monotonic() + 300
        while runtime.project_restore.phase == "RESTORING":
            assert time.monotonic() < deadline, "복원 시간 초과"
            persistence._restore_timer(runtime)
            time.sleep(.01)
        assert runtime.project_restore.phase == "IDLE", runtime.project_restore.last_message
        runtime.session_active = True
        report["restore_seconds"] = time.perf_counter() - started
        views = json.loads((OUT / "views.json").read_text(encoding="utf-8"))
        baseline_state = (len(bpy.data.objects), len(bpy.data.meshes), bpy.data.is_dirty, repr(runtime.marker_selection.snapshot()))
        for mid, model in sorted(runtime.model_repository.models.items(), key=lambda item: len(item[1].vertices_float64)):
            runtime.model_catalog.set_geometry_visibility(mid, show_points=False, show_surface=True)
            if not any(mid in picking.marker_pick_model_ids(role) for role in ("REFERENCE", "MOVING")):
                runtime.model_catalog._set_selected_model_ids((mid,), active_model_id=mid)
            role = next(role for role in ("REFERENCE", "MOVING") if mid in picking.marker_pick_model_ids(role))
            saved_view = next(view for view in views if view["role"] == role)
            region = SimpleNamespace(width=saved_view["width"], height=saved_view["height"])
            view = SimpleNamespace(view_matrix=np.asarray(saved_view["view_matrix"]), perspective_matrix=np.asarray(saved_view["perspective_matrix"]))
            matrix = point_data.effective_model_matrix_for_id(mid)
            batch = renderer.RUNTIME.gpu_batches[renderer._mesh_batch_key(model)]
            assert renderer.prepared_mesh_pick_batch(mid, model) is batch.solid_batch
            samples = []
            rng = np.random.default_rng(20261021)
            indices = np.unique(np.concatenate((np.linspace(0, len(model.triangles_uint32) - 1, 100, dtype=int), rng.integers(0, len(model.triangles_uint32), 900))))
            for index in indices:
                local = model.vertices_float64[model.triangles_uint32[index]].mean(axis=0)
                clip = view.perspective_matrix @ matrix @ np.append(local, 1)
                if clip[3] <= 0:
                    continue
                ndc = clip[:3] / clip[3]
                if np.any(np.abs(ndc[:2]) > .98) or abs(ndc[2]) >= 1:
                    continue
                mouse = tuple((ndc[:2] + 1) * (region.width / 2, region.height / 2))
                samples.append(mouse)
            assert samples, "화면 안의 검사 지점이 없습니다."
            mouse = samples[len(samples) // 2]
            origin, direction = picking._screen_view_ray(region, view, mouse)
            result = {"model": model.display_name, "vertices": len(model.vertices_float64), "triangles": len(model.triangles_uint32),
                      "matrix": matrix.tolist(), "role": role, "cold_gpu_seconds": [], "cold_bvh_seconds": [], "hot_gpu_seconds": [], "hot_bvh_seconds": [], "accuracy": []}
            report["models"].append(result)
            start = time.perf_counter()
            draw_existing_display_batch(batch.solid_batch, matrix, view)
            result["existing_display_first_draw_seconds"] = time.perf_counter() - start
            for trial in range(3):
                picking.clear_mesh_surface_pick_cache()
                # 첫 클릭에는 셰이더 생성도 포함하되 기존 화면 배치는 그대로 재사용합니다.
                start = time.perf_counter()
                hit = picking.pick_nearest_point(role, region, view, mouse)
                result["cold_gpu_seconds"].append(time.perf_counter() - start)
                assert hit is not None and hit.point_kind == "MESH_SURFACE"
                assert not picking._MESH_SURFACE_BVH_CACHE
                start = time.perf_counter()
                old = picking._pick_model_mesh_surface_from_ray(mid, model, region, view, origin, direction)
                result["cold_bvh_seconds"].append(time.perf_counter() - start)
                assert old is not None and old.source_point_id == hit.source_point_id
                np.testing.assert_array_equal(old.position, hit.position)
                for _ in range(20):
                    start = time.perf_counter()
                    again = picking.pick_nearest_point(role, region, view, mouse)
                    result["hot_gpu_seconds"].append(time.perf_counter() - start)
                    start = time.perf_counter()
                    previous = picking._pick_model_mesh_surface_from_ray(mid, model, region, view, origin, direction)
                    result["hot_bvh_seconds"].append(time.perf_counter() - start)
                    assert again == hit and previous == old
                write_report(report)
            for mouse in samples:
                origin, direction = picking._screen_view_ray(region, view, mouse)
                old = picking._pick_model_mesh_surface_from_ray(mid, model, region, view, origin, direction)
                new = picking.pick_nearest_point(role, region, view, mouse)
                error = float(np.linalg.norm(np.asarray(old.position) - new.position)) if old and new else None
                row = {"mouse": mouse, "old_id": old.source_point_id if old else None, "new_id": new.source_point_id if new else None,
                       "kind": new.point_kind if new else None, "coordinate_error": error}
                result["accuracy"].append(row)
            mismatches = [row for row in result["accuracy"] if row["old_id"] != row["new_id"] or (row["coordinate_error"] is not None and row["coordinate_error"] > 1e-9)]
            result["mismatches"] = mismatches
            for row in mismatches:
                mouse = row["mouse"]
                row["candidates"] = screen_pick.screen_triangle_candidates(batch.pick_batch, matrix, region, view, mouse, len(model.triangles_uint32), (model.bounds_min, model.bounds_max))
                if row["old_id"] is not None:
                    tri = model.vertices_float64[model.triangles_uint32[row["old_id"]]]
                    clip = np.column_stack((tri, np.ones(3))) @ (view.perspective_matrix @ matrix).T
                    row["old_triangle_screen"] = ((clip[:, :2] / clip[:, 3:4] + 1) * (region.width / 2, region.height / 2)).tolist()
            result["independent_checks"] = []
            check_rows = list(mismatches) + [result["accuracy"][0], result["accuracy"][len(samples) // 2]]
            for row in check_rows:
                origin, direction = picking._screen_view_ray(region, view, row["mouse"])
                local_origin, local_direction = picking._ray_in_source_local(mid, origin, direction)
                exact = nearest_original_triangle(model.vertices_float64, model.triangles_uint32, local_origin, local_direction)
                hit = picking.pick_nearest_point(role, region, view, row["mouse"])
                check = {"mouse": row["mouse"], "full_scan_id": exact[1] if exact else None, "new_id": hit.source_point_id if hit else None,
                         "coordinate_error": float(np.linalg.norm(exact[2] - hit.position)) if exact and hit else None}
                result["independent_checks"].append(check)
                if row in mismatches:
                    row["independent"] = check
            print(json.dumps({"model": model.display_name, "samples": len(samples), "mismatches": len(mismatches),
                              "cold_gpu": result["cold_gpu_seconds"], "cold_bvh": result["cold_bvh_seconds"]}, ensure_ascii=False), flush=True)
            write_report(report)
        final_state = (len(bpy.data.objects), len(bpy.data.meshes), bpy.data.is_dirty, repr(runtime.marker_selection.snapshot()))
        report["scene_and_markers_unchanged"] = final_state == baseline_state
        report["completed"] = True
        report["passed"] = report["scene_and_markers_unchanged"] and all(
            check["full_scan_id"] == check["new_id"] and check["coordinate_error"] is not None and check["coordinate_error"] < 1e-8
            for model in report["models"] for check in model["independent_checks"]
        )
    except Exception:
        report["errors"].append(traceback.format_exc())
        print(report["errors"][-1], flush=True)
    finally:
        write_report(report)
        # 독립 검사 프로세스가 사용자의 종료 복구 파일을 덮어쓰지 않도록 저장 없이 끝냅니다.
        sys.stdout.flush()
        os._exit(0 if report.get("passed") else 1)


if __name__ == "__main__":
    bpy.app.timers.register(run_benchmark, first_interval=2)
