"""텍스처 버퍼 분할의 원본 면·UV·법선 보존과 실패 정리를 검증합니다."""
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import sys
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from tools.align.viewport import renderer
from tools.align.mesh_io.types import ObjMaterialData, TriangleMeshModelData
from test_mesh_normal_reuse import _make_model, _capture_shared_batches


def test_material_boundaries_and_original_arrays():
    """혼합 재질·누락 UV·마지막 부분 배치를 분할해도 모든 면과 원본 배열을 보존합니다."""
    with TemporaryDirectory() as directory:
        root = Path(directory)
        path = root / 'texture.png'
        path.write_bytes(b'texture')
        vertices = np.arange(90, dtype=np.float64).reshape(30, 3) / 7
        triangles = np.arange(30, dtype=np.uint32).reshape(10, 3)
        uvs = np.arange(90, dtype=np.float64).reshape(30, 3) / 101
        uv_ids = triangles.astype(np.int32)
        uv_ids[6] = -1
        materials = np.array([0, 1, 0, 1, 0, 0, 0, -1, 1, 0], dtype=np.int32)
        model = TriangleMeshModelData(
            model_id='분할검사', display_name='분할검사', source_path=root / 'mesh.obj',
            vertices_float64=vertices, triangles_uint32=triangles,
            registration_vertex_indices=np.arange(30, dtype=np.uint32),
            display_vertex_indices=np.arange(30, dtype=np.uint32),
            bounds_min=vertices.min(axis=0), bounds_max=vertices.max(axis=0),
            texcoords_float64=uvs, texcoord_component_counts_uint8=np.full(30, 3, dtype=np.uint8),
            triangle_texcoord_indices_int32=uv_ids, triangle_material_indices_int32=materials,
            materials=(ObjMaterialData('첫 재질', diffuse_texture_path=path), ObjMaterialData('같은 이미지', diffuse_texture_path=path)),
        )
        arrays = [model.vertices_float64, model.triangles_uint32, model.texcoords_float64,
                  model.triangle_texcoord_indices_int32, model.triangle_material_indices_int32]
        originals = [a.copy() for a in arrays]
        normals = np.ones((30, 3), dtype=np.float32)
        budget = 2 * 3 * 32 + 95
        with patch.object(renderer, '_MESH_TEXTURE_BUFFER_MAX_BYTES', budget):
            solid, chunks, original_ids = renderer._mesh_surface_arrays(model, normals=normals)
        np.testing.assert_array_equal(solid, triangles[[6, 7]])
        assert [list(ids) for ids in original_ids] == [[6, 7], [0, 2], [4, 5], [9], [1, 3], [8]]
        assert all(not ids.flags.writeable for ids in original_ids if isinstance(ids, np.ndarray))
        assert [len(c[1]) // 3 for c in chunks] == [2, 2, 1, 2, 1]
        for material_id in (0, 1):
            mask = (materials == material_id) & np.all(uv_ids >= 0, axis=1)
            grouped = [c for c in chunks if c[0] == material_id]
            np.testing.assert_array_equal(np.concatenate([c[1] for c in grouped]), vertices[triangles[mask]].reshape(-1, 3).astype(np.float32))
            np.testing.assert_array_equal(np.concatenate([c[2] for c in grouped]), normals[triangles[mask]].reshape(-1, 3))
            np.testing.assert_array_equal(np.concatenate([c[3] for c in grouped]), uvs[uv_ids[mask], :2].reshape(-1, 2).astype(np.float32))
        assert all(sum(a.nbytes for a in c[1:]) <= budget for c in chunks)
        for actual, expected in zip(arrays, originals, strict=True):
            np.testing.assert_array_equal(actual, expected)


def test_split_batches_and_pick_order():
    """UV 이음매를 보존하고 위치 버퍼를 선택에 공유하며 원본 번호를 복원합니다."""
    with TemporaryDirectory() as directory:
        model = _make_model(Path(directory), 2)
        captured = []
        def capture(shader, primitive, attributes, *, indices=None):
            """제출한 속성과 면 인덱스를 기록합니다."""
            batch = (shader, attributes, indices)
            captured.append(batch)
            return batch
        with (patch.dict(renderer.RUNTIME.gpu_batches, {}, clear=True),
              patch.object(renderer, '_MESH_TEXTURE_BUFFER_MAX_BYTES', 96),
              patch.object(renderer, '_point_shader', return_value='점'),
              patch.object(renderer, '_mesh_shader', return_value='면'),
              patch.object(renderer, '_mesh_texture_shader', return_value='텍스처'),
              patch.object(renderer, '_acquire_texture_resource'),
              patch.object(renderer, 'batch_for_shader', side_effect=capture),
              patch.object(renderer, '_shared_mesh_batches', side_effect=_capture_shared_batches(capture))):
            point = renderer.prepare_gpu_batch_for_model(model.model_id, model)
            surface = renderer.RUNTIME.gpu_batches[renderer._mesh_batch_key(model)]
            assert len(surface.textured_batches) == 2
            assert len(surface.pick_batch.chunks) == 2
            for chunk, entry in zip(surface.pick_batch.chunks, surface.textured_batches, strict=True):
                assert chunk.batch is entry.shared_buffers.pick_batch
                assert chunk.batch[2]['pos'] is entry.batch[1]['pos']
                assert isinstance(chunk.triangle_ids, range)
            assert surface.pick_batch.original_triangle_ids((1, 0)) == (0, 1)
            uv = np.concatenate([entry.batch[1]['uv'] for entry in surface.textured_batches])
            np.testing.assert_array_equal(uv, model.texcoords_float64[model.triangle_texcoord_indices_int32, :2].reshape(-1, 2).astype(np.float32))
            assert renderer.prepare_gpu_batch_for_model(model.model_id, model) is point
            assert len(captured) == 3


def test_split_failure_cleanup():
    """두 번째 텍스처 버퍼 생성 실패 시 부분 배치와 이미지 사용권을 확정하지 않습니다."""
    with TemporaryDirectory() as directory:
        model = _make_model(Path(directory), 2)
        calls = 0
        def fail_second_texture(shader, primitive, attributes, *, indices=None):
            """분할 중간의 GPU 메모리 할당 실패를 재현합니다."""
            nonlocal calls
            if shader == '텍스처':
                calls += 1
                if calls == 2:
                    raise RuntimeError('버퍼 할당 검사')
            return object()
        with (patch.dict(renderer.RUNTIME.gpu_batches, {}, clear=True),
              patch.object(renderer, '_MESH_TEXTURE_BUFFER_MAX_BYTES', 96),
              patch.object(renderer, '_point_shader', return_value='점'),
              patch.object(renderer, '_mesh_shader', return_value='면'),
              patch.object(renderer, '_mesh_texture_shader', return_value='텍스처'),
              patch.object(renderer, '_acquire_texture_resource'),
              patch.object(renderer, '_release_texture_resources_for_model') as release,
              patch.object(renderer, 'batch_for_shader', side_effect=fail_second_texture),
              patch.object(renderer, '_shared_mesh_batches', side_effect=_capture_shared_batches(fail_second_texture))):
            try:
                renderer.prepare_gpu_batch_for_model(model.model_id, model)
            except RuntimeError as error:
                assert str(error) == '버퍼 할당 검사'
            else:
                raise AssertionError('중간 실패가 전달되지 않았습니다.')
            assert not renderer.RUNTIME.gpu_batches
            assert release.call_count == 2


if __name__ == '__main__':
    for name, function in sorted(globals().copy().items()):
        if name.startswith('test_') and callable(function):
            function()
            print('통과:', name, flush=True)
