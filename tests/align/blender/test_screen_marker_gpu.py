"""별도 GUI Blender에서 실제 정수 화면 선택·깊이·정밀 좌표·GPU 상태 복원을 검사합니다."""
from pathlib import Path
from types import SimpleNamespace
import json
import os
import sys
import traceback
from unittest.mock import patch
from tempfile import TemporaryDirectory

import bpy
import gpu
import numpy as np
from gpu_extras.batch import batch_for_shader

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from tools.align.viewport import picking, renderer, screen_pick


def gpu_state():
    """선택 패스가 보존해야 하는 현재 GPU 상태를 반환합니다."""
    return (gpu.state.blend_get(), gpu.state.depth_test_get(), gpu.state.depth_mask_get(),
            gpu.state.viewport_get(), gpu.state.scissor_get(), gpu.state.active_framebuffer_get())


def test_actual_gpu_surfaces():
    """얕은 깊이 차이·배면·변환·원근과 직교에서 독립 교차와 GPU 원본 ID를 비교합니다."""
    region = SimpleNamespace(width=1001, height=801)
    perspective = np.asarray(((1.5, 0, 0, 0), (0, 1.8, 0, 0), (0, 0, -1.000002, -.02000002), (0, 0, -1, 0)), dtype=np.float64)
    view = np.eye(4)
    vertices = np.asarray(((-2, -2, -.0123456789), (2, -2, -.0123456789), (0, 2, -.0123456789),
                           (-2, -2, .0123456789), (2, -2, .0123456789), (0, 2, .0123456789)), dtype=np.float64)
    triangles = np.asarray(((0, 1, 2), (3, 4, 5)), dtype=np.uint32)
    checked = 0
    for projection in (perspective, np.diag((.02, .02, -.0001, 1))):
        for flip in (False, True):
            faces = triangles[:, ::-1].copy() if flip else triangles
            batch = batch_for_shader(screen_pick._pick_shader(), "TRIS", {"pos": vertices.astype(np.float32)}, indices=faces)
            for scales in ((1, 1, 1), (-2, .7, 1.3), (3, 2, .2)):
                model_matrix = np.diag((*scales, 1.0))
                model_matrix[:3, 3] = (12.345678, -4.56789, -1000)
                for local_xy in ((0, 0), (.7, -.4)):
                    target = np.asarray((*local_xy, vertices[3, 2], 1))
                    clip = projection @ model_matrix @ target
                    mouse = tuple((clip[:2] / clip[3] + 1) * (region.width / 2, region.height / 2))
                    r3d = SimpleNamespace(view_matrix=view, perspective_matrix=projection)
                    before = gpu_state()
                    ids = screen_pick.screen_triangle_candidates(batch, model_matrix, region, r3d, mouse, 2, (vertices.min(axis=0), vertices.max(axis=0)))
                    assert gpu_state() == before, "GPU 상태가 복원되지 않았습니다."
                    assert 1 in ids and 0 not in ids, (flip, scales, ids)
                    origin, direction = picking._screen_view_ray(region, r3d, mouse)
                    inverse = np.linalg.inv(model_matrix)
                    local_origin = (inverse @ np.append(origin, 1))[:3]
                    local_direction = inverse[:3, :3] @ direction
                    local_direction /= np.linalg.norm(local_direction)
                    # 독립 평면 교차로 구한 좌표를 원본 삼각형 교차 결과와 대조합니다.
                    expected = local_origin + ((vertices[3, 2] - local_origin[2]) / local_direction[2]) * local_direction
                    actual = picking._ray_triangle_position_float64(local_origin, local_direction, vertices[faces[1]])
                    np.testing.assert_allclose(actual, expected, rtol=0, atol=1e-9)
                    np.testing.assert_allclose(actual, target[:3], rtol=0, atol=1e-7)
                    checked += 1
            assert not screen_pick.screen_triangle_candidates(batch, model_matrix, region, r3d, (0, 0), 2, (vertices.min(axis=0), vertices.max(axis=0)))
    return checked


def test_public_marker_and_draw_failure():
    """공개 마커 함수의 원본 좌표 저장과 실제 그리기 실패 후 GPU 상태 보존을 확인합니다."""
    from test_companion_marker_alignment import _display_runtime
    from tools.align.mesh_io.types import TriangleMeshModelData
    from tools.align.state import RuntimeState
    from tools.align.display_axes import identity_display_transform
    from tools.align.model_repository import ModelRepositoryError
    runtime = RuntimeState()
    runtime.session_active = True
    vertices = np.asarray(((-1, -1, -3.123456789012), (1, -1, -3.123456789012), (0, 1, -3.123456789012)))
    model = TriangleMeshModelData(model_id="정밀검사", display_name="정밀검사", source_path=ROOT / "fixture.obj", vertices_float64=vertices,
                                  triangles_uint32=np.asarray(((0, 1, 2),), dtype=np.uint32), registration_vertex_indices=np.arange(3, dtype=np.uint32),
                                  display_vertex_indices=np.arange(3, dtype=np.uint32), bounds_min=vertices.min(axis=0), bounds_max=vertices.max(axis=0))
    runtime.model_repository.add_model(model, identity_display_transform())
    runtime.model_catalog.add_imported_model(model.model_id, model.display_name, select_after_import=True)
    runtime.model_catalog.set_geometry_visibility(model.model_id, show_points=False, show_surface=True)
    region = SimpleNamespace(width=1000, height=800)
    view = SimpleNamespace(view_matrix=np.eye(4), perspective_matrix=np.diag((1, 1, -.1, 1)))
    with _display_runtime(runtime):
        renderer.prepare_gpu_batch_for_model(model.model_id, model)
        picking.clear_mesh_surface_pick_cache()
        hit = picking.pick_nearest_point("REFERENCE", region, view, (500, 400))
        assert hit.point_kind == "MESH_SURFACE" and hit.source_point_id == 0 and hit.geometry_source == "MAIN"
        np.testing.assert_allclose(hit.position, (0, 0, vertices[0, 2]), rtol=0, atol=1e-12)
        assert not picking._MESH_SURFACE_BVH_CACHE
        before = gpu_state()
        with patch.object(renderer, "prepared_mesh_pick_batch", return_value=SimpleNamespace(draw=fail_draw)):
            try:
                picking.pick_nearest_point("REFERENCE", region, view, (500, 400))
            except ModelRepositoryError as error:
                assert "그리기 실패 검사" in str(error)
            else:
                raise AssertionError("그리기 실패가 전달되지 않았습니다.")
        assert gpu_state() == before
        renderer.discard_gpu_batch_for_model(model.model_id)
        picking.clear_mesh_surface_pick_cache()


def fail_draw(shader):
    """실제 프레임버퍼 바인딩 뒤 오류를 발생시켜 상태 정리 경로를 검사합니다."""
    raise RuntimeError("그리기 실패 검사")


def test_textured_original_order():
    """재질별 표시 순서와 무관하게 실제 GPU 선택이 원본 두 번째 삼각형 ID를 반환합니다."""
    from test_mesh_normal_reuse import _make_model
    from test_companion_marker_alignment import _display_runtime
    from tools.align.state import RuntimeState
    from tools.align.display_axes import identity_display_transform
    for textured_count in (1, 2):
        with TemporaryDirectory() as directory:
            model = _make_model(Path(directory), textured_count)
            image = bpy.data.images.new("마커 검사 텍스처", width=1, height=1)
            image.filepath_raw = str(Path(directory) / "texture.png")
            image.file_format = "PNG"
            image.save()
            runtime = RuntimeState()
            runtime.session_active = True
            runtime.model_repository.add_model(model, identity_display_transform())
            runtime.model_catalog.add_imported_model(model.model_id, model.display_name, select_after_import=True)
            runtime.model_catalog.set_geometry_visibility(model.model_id, show_points=False, show_surface=True)
            view = np.asarray(((1, 0, 0, -.5), (0, 0, 1, -.5), (0, -1, 0, -5), (0, 0, 0, 1)), dtype=np.float64)
            projection = np.diag((.5, .5, -.1, 1)) @ view
            region = SimpleNamespace(width=1000, height=800)
            r3d = SimpleNamespace(view_matrix=view, perspective_matrix=projection)
            target = np.asarray((.2, 0, .6))
            clip = projection @ np.append(target, 1)
            mouse = tuple((clip[:2] + 1) * (region.width / 2, region.height / 2))
            with _display_runtime(runtime):
                renderer.prepare_gpu_batch_for_model(model.model_id, model)
                hit = picking.pick_nearest_point("REFERENCE", region, r3d, mouse)
                assert hit.point_kind == "MESH_SURFACE" and hit.source_point_id == 1
                np.testing.assert_allclose(hit.position, target, rtol=0, atol=1e-12)
                renderer.discard_gpu_batch_for_model(model.model_id)
                picking.clear_mesh_surface_pick_cache()
            bpy.data.images.remove(image)


def main():
    """실제 GPU 검사를 별도 프로세스에서 실행하고 원본 장면을 저장하지 않고 종료합니다."""
    report = {"passed": False, "gpu": gpu.platform.renderer_get()}
    try:
        report["surface_cases"] = test_actual_gpu_surfaces()
        test_public_marker_and_draw_failure()
        test_textured_original_order()
        report["textured_cases"] = 2
        report["passed"] = True
    except Exception:
        report["error"] = traceback.format_exc()
    (ROOT / "build/verification/screen-marker-20261021/gpu-tests.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False), flush=True)
    sys.stdout.flush()
    os._exit(0 if report["passed"] else 1)


if __name__ == "__main__":
    bpy.app.timers.register(main, first_interval=1)
