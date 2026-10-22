"""작은 코너 UV의 무손실 값·모델 내부 공유·기존 호출과 표시 준비를 검증합니다."""
from dataclasses import replace
from pathlib import Path
import sys
from unittest.mock import patch

import bpy
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from tools.align.mesh_io import embedded_obj_store as store
from tools.align.mesh_io.texture_coordinates import MeshTextureCoordinates
from tools.align.mesh_io.types import ObjMaterialData
from tools.align.viewport import renderer


def make_model(name, offset=0.0, textured=True):
    """독립 내장 메시의 혼합 면과 UV 이음매·음수·범위 밖 좌표를 준비합니다."""
    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata([(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0), (0, 0, 1)], [], [(0, 1, 2, 3), (0, 4, 1)])
    values = np.array([[-0.0, 1.0], [1.0000001, -2.5], [4.125, .25], [.3, .6], [8.5, -7.0], [.5, .75], [2.25, 0]], dtype=np.float32)
    if offset:
        values += offset
    if textured:
        layer = mesh.uv_layers.new()
        layer.uv.foreach_set('vector', values.reshape(-1))
    model = store.commit_embedded_obj_mesh_and_build_proxy(mesh, model_id=name, display_name=name, source_path=name+'.obj')
    return model, values


def test_exact_uv_and_model_local_aliases():
    """각 모델의 UV가 원본과 비트 단위로 같고 인덱스 공유는 같은 모델 안에서만 일어납니다."""
    first, first_uv = make_model('compact-first')
    second, second_uv = make_model('compact-second', 3.25)
    try:
        for model, values in ((first, first_uv), (second, second_uv)):
            np.testing.assert_array_equal(model.texture_coordinates.view(np.uint32), values.view(np.uint32))
            expanded = model.texcoords_float64
            np.testing.assert_array_equal(expanded[:, :2].view(np.uint64), values.astype(np.float64).view(np.uint64))
            np.testing.assert_array_equal(expanded[:, 2], 0)
            assert not expanded.flags.writeable and not model.texture_coordinates.flags.writeable
            assert model.texcoord_component_counts_uint8.strides == (0,)
            assert model.texcoord_component_counts_uint8.base.nbytes == 1
            assert np.shares_memory(model.triangle_texcoord_indices_int32, model.triangle_loop_indices_uint32)
            np.testing.assert_array_equal(model.triangle_texcoord_indices_int32, model.triangle_loop_indices_uint32)
            assert not model.triangle_texcoord_indices_int32.flags.writeable
            assert not model.triangle_loop_indices_uint32.flags.writeable
        assert not np.shares_memory(first.texture_coordinates, second.texture_coordinates)
        assert not np.shares_memory(first.triangle_loop_indices_uint32, second.triangle_loop_indices_uint32)
        assert not np.shares_memory(first.texcoord_component_counts_uint8, second.texcoord_component_counts_uint8)
        store.remove_embedded_obj_mesh(first.embedded_asset_uuid)
        np.testing.assert_array_equal(second.texture_coordinates, second_uv)
    finally:
        for model in (first, second):
            store.remove_embedded_obj_mesh(model.embedded_asset_uuid)


def test_display_and_replace_do_not_expand_uv():
    """실제 표시 배열 준비와 모델 개정이 기존 큰 UV 사본을 요청하지 않는지 확인합니다."""
    model, values = make_model('compact-display')
    try:
        texture = Path(__file__)
        with patch.object(MeshTextureCoordinates, 'legacy_float64', side_effect=AssertionError('큰 UV 사본 생성')):
            updated = replace(model, geometry_revision=model.geometry_revision + 1,
                              materials=(ObjMaterialData('fixture', diffuse_texture_path=texture),),
                              triangle_material_indices_int32=np.zeros(model.face_count, dtype=np.int32))
            assert updated.uv_data is model.uv_data
            assert updated.has_complete_texture_coordinates
            solid, batches, ids = renderer._mesh_surface_arrays(updated)
            assert len(solid) == 0 and batches
            actual = np.concatenate([item[3] for item in batches])
            expected = values[model.triangle_loop_indices_uint32].reshape((-1, 2))
            np.testing.assert_array_equal(actual.view(np.uint32), expected.view(np.uint32))
            np.testing.assert_array_equal(np.concatenate([np.asarray(part) for part in ids[1:]]), np.arange(model.face_count))
    finally:
        store.remove_embedded_obj_mesh(model.embedded_asset_uuid)


def test_legacy_precision_and_partial_replacement():
    """기존 배정밀도·세 번째 UV 성분과 replace의 부분 인자 교체 의미를 보존합니다."""
    model, _ = make_model('compact-legacy')
    try:
        values = model.texcoords_float64.copy()
        values[:, 0] += 1e-12
        values[:, 2] = 0.123456789012345
        legacy = replace(model, texcoords_float64=values,
                         texcoord_component_counts_uint8=np.full(len(values), 3, dtype=np.uint8))
        assert legacy.texture_coordinates.dtype == np.float64
        np.testing.assert_array_equal(legacy.texcoords_float64, values)
        changed = replace(legacy, texcoords_float64=values * 2)
        np.testing.assert_array_equal(changed.texcoords_float64, values * 2)
        np.testing.assert_array_equal(changed.texcoord_component_counts_uint8, 3)
        changed_counts = replace(legacy, texcoord_component_counts_uint8=np.full(len(values), 2, dtype=np.uint8))
        np.testing.assert_array_equal(changed_counts.texcoords_float64, values)
    finally:
        store.remove_embedded_obj_mesh(model.embedded_asset_uuid)


def test_untextured_and_invalid_uv():
    """UV가 없는 메시와 잘못된 성분·비유한 입력의 기존 거부 동작을 유지합니다."""
    model, _ = make_model('compact-no-uv', textured=False)
    try:
        assert not model.has_texture_coordinates
        np.testing.assert_array_equal(model.triangle_texcoord_indices_int32, -1)
        assert model.texcoords_float64.shape == (0, 3)
        for values, counts in ((np.array([[np.nan, 0]], dtype=np.float32), np.array([2], dtype=np.uint8)),
                               (np.array([[0, 0]], dtype=np.float32), np.array([3], dtype=np.uint8))):
            try:
                MeshTextureCoordinates(values, counts)
            except ValueError:
                pass
            else:
                raise AssertionError('잘못된 UV 표현을 허용했습니다.')
    finally:
        store.remove_embedded_obj_mesh(model.embedded_asset_uuid)


if __name__ == '__main__':
    for name, function in sorted(globals().copy().items()):
        if name.startswith('test_') and callable(function):
            function()
            print('통과:', name, flush=True)
