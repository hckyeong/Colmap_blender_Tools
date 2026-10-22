"""표시 GPU 배치 공유의 원본 번호·정밀 좌표와 32비트 정수 선택을 실제 GPU에서 검증합니다."""
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch
import json
import os
import sys
import traceback

import bpy
import gpu
import numpy as np
from gpu_extras.batch import batch_for_shader

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from tools.align.viewport import renderer, screen_pick, picking
from tools.align.mesh_io.types import ObjMaterialData, TriangleMeshModelData
from test_mesh_texture_chunks_gpu import test_pixel_equivalence
from test_screen_marker_gpu import test_actual_gpu_surfaces, test_public_marker_and_draw_failure, test_textured_original_order, gpu_state


def test_interleaved_material_ids():
    """공유 위치 배치의 혼합 재질·분할 경계·앞뒤 면 번호와 정밀 좌표를 보존합니다."""
    with TemporaryDirectory() as directory:
        root = Path(directory)
        image = bpy.data.images.new('공유 배치 검사', width=1, height=1)
        image.filepath_raw = str(root / 'texture.png')
        image.file_format = 'PNG'
        image.save()
        vertices = np.asarray([(3 * i + x, y, -.1234567890123) for i in range(8)
                               for x, y in ((0, 0), (2, 0), (0, 2))], dtype=np.float64)
        triangles = np.arange(24, dtype=np.uint32).reshape(-1, 3)
        uv_ids = triangles.astype(np.int32)
        uv_ids[6] = -1
        model = TriangleMeshModelData(
            model_id='교차재질', display_name='교차재질', source_path=root/'mesh.obj',
            vertices_float64=vertices, triangles_uint32=triangles,
            registration_vertex_indices=np.arange(24, dtype=np.uint32),
            display_vertex_indices=np.arange(24, dtype=np.uint32),
            bounds_min=vertices.min(axis=0), bounds_max=vertices.max(axis=0),
            texcoords_float64=np.zeros((24, 3)), texcoord_component_counts_uint8=np.full(24, 2, dtype=np.uint8),
            triangle_texcoord_indices_int32=uv_ids,
            triangle_material_indices_int32=np.asarray((0, 1, 0, 1, -1, 0, 1, 0), dtype=np.int32),
            materials=tuple(ObjMaterialData(str(i), diffuse_texture_path=root/'texture.png') for i in range(2)),
        )
        region = SimpleNamespace(width=2400, height=400)
        projection = np.diag((1/12, .5, -.1, 1))
        projection[0, 3] = -1
        view = SimpleNamespace(view_matrix=np.eye(4), perspective_matrix=projection)
        try:
            with patch.object(renderer, '_MESH_TEXTURE_BUFFER_MAX_BYTES', 192):
                renderer.prepare_gpu_batch_for_model(model.model_id, model)
            surface = renderer.RUNTIME.gpu_batches[renderer._mesh_batch_key(model)]
            shared = surface.pick_batch
            expected = (surface.solid_buffers, *(entry.shared_buffers for entry in surface.textured_batches))
            assert all(chunk.batch is buffers.pick_batch for chunk, buffers in zip(shared.chunks, expected, strict=True))
            assert all(buffers.pick_batch is not buffers.display_batch for buffers in expected)
            assert [list(chunk.triangle_ids) for chunk in shared.chunks] == [[4, 6], [0, 2], [5, 7], [1, 3]]
            for index in range(8):
                point = np.asarray((3 * index + .4, .6, vertices[0, 2]))
                clip = projection @ np.append(point, 1)
                mouse = tuple((clip[:2] + 1) * (region.width/2, region.height/2))
                before = gpu_state()
                candidates = screen_pick.screen_triangle_candidates(shared, np.eye(4), region, view, mouse, 8,
                                                                      (model.bounds_min, model.bounds_max))
                assert gpu_state() == before
                assert candidates == (index,), (index, candidates)
                origin, direction = picking._screen_view_ray(region, view, mouse)
                actual = picking._ray_triangle_position_float64(origin, direction, vertices[triangles[index]])
                np.testing.assert_allclose(actual, point, rtol=0, atol=1e-12)
            for direction_sign, nearest_id in ((1, 7), (-1, 0)):
                stacked = vertices.copy()
                stacked[:, 0] -= np.repeat(np.arange(8) * 3, 3)
                stacked[:, 2] += np.repeat(np.arange(8) * .001 * direction_sign, 3)
                layered = replace(model, vertices_float64=stacked, bounds_min=stacked.min(axis=0), bounds_max=stacked.max(axis=0))
                with patch.object(renderer, '_MESH_TEXTURE_BUFFER_MAX_BYTES', 192):
                    renderer.prepare_gpu_batch_for_model(model.model_id, layered)
                candidates = screen_pick.screen_triangle_candidates(
                    renderer.prepared_mesh_pick_batch(model.model_id, layered), np.eye(4), region, view,
                    (40, 260), 8, (layered.bounds_min, layered.bounds_max),
                )
                assert candidates == (nearest_id,), (direction_sign, candidates)
            # 같은 이름·리비전의 모델 교체도 새 공유 배치를 준비해야 합니다.
            replaced = replace(model)
            renderer.prepare_gpu_batch_for_model(model.model_id, replaced)
            assert renderer.prepared_mesh_pick_batch(model.model_id, replaced) is not shared
        finally:
            renderer.discard_gpu_batch_for_model(model.model_id)
            bpy.data.images.remove(image)
        assert not renderer._MESH_TEXTURE_RESOURCES


def empty_draw(shader):
    """거대한 번호 구간을 GPU 메모리 할당 없이 건너뛰는 검사 배치입니다."""
    return None


def test_large_integer_offsets():
    """단정도 정수 한계와 부호 비트 경계를 넘는 실제 정수 렌더 타깃 읽기를 검사합니다."""
    vertices = np.asarray(((-1, -1, -1), (1, -1, -1), (0, 1, -1)), dtype=np.float32)
    batch = batch_for_shader(screen_pick._pick_shader(), 'TRIS', {'pos': vertices})
    region = SimpleNamespace(width=100, height=100)
    view = SimpleNamespace(view_matrix=np.eye(4), perspective_matrix=np.diag((1, 1, -.1, 1)))
    for offset in (2**24 + 7, 2**31 + 17, 2**32 - 3):
        shared = screen_pick.SharedMeshPickBatch((
            screen_pick.MeshPickChunk(SimpleNamespace(draw=empty_draw), range(offset)),
            screen_pick.MeshPickChunk(batch, range(offset, offset+1)),
        ))
        result = screen_pick.screen_triangle_candidates(shared, np.eye(4), region, view, (50, 50), offset+1,
                                                        (vertices.min(axis=0), vertices.max(axis=0)))
        assert result == (offset,), (offset, result)


def main():
    """격리 GUI 문맥에서 공유·깊이·픽셀·실패 정리를 검증하고 지정 보고서에 기록합니다."""
    report = {'passed': False, 'gpu': gpu.platform.renderer_get()}
    try:
        test_pixel_equivalence()
        report['surface_cases'] = test_actual_gpu_surfaces()
        test_public_marker_and_draw_failure()
        with patch.object(renderer, '_MESH_TEXTURE_BUFFER_MAX_BYTES', 96):
            test_textured_original_order()
        test_interleaved_material_ids()
        test_large_integer_offsets()
        report.update(passed=True, interleaved_faces=8, layered_material_cases=2, large_integer_cases=3, pixel_equivalence_cases=2)
    except Exception:
        report['error'] = traceback.format_exc()
    destination = Path(sys.argv[sys.argv.index('--')+1]) if '--' in sys.argv else ROOT/'build/verification/obj-shared-gpu-20261021/gpu-tests.json'
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False), flush=True)
    os._exit(0 if report['passed'] else 1)


if __name__ == '__main__':
    bpy.app.timers.register(main, first_interval=1.0)
