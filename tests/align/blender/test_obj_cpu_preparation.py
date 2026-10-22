"""OBJ CPU 준비 재사용의 원본 순서·UV·미사용 정점·표시 제한 계약을 검증합니다."""
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from unittest.mock import patch

import bpy
import numpy as np

for directory in (Path(__file__).resolve().parents[3], Path(__file__).resolve().parent):
    sys.path.insert(0, str(directory))

from tools.align import mesh_importer
from tools.align.mesh_io import embedded_obj_store as store
from tools.align.mesh_io.obj_native_import import import_obj_to_staging
from tools.align.state import RuntimeState


def test_triangle_order_uv_material_and_unused_vertices():
    """삼각형 직접 경로가 Blender 삼각분할·UV seam·재질·미사용 원본 정점을 그대로 보존합니다."""
    mesh = bpy.data.meshes.new('CPU 준비 원본 보존')
    materials = []
    try:
        vertices = [(0, 0, 0), (2, 0, 0), (0, 2, 0), (0, 0, 2), (99, 88, 77)]
        mesh.from_pydata(vertices, [], [(2, 0, 1), (3, 1, 0), (1, 3, 2)])
        for name in ('첫 재질', '둘째 재질'):
            material = bpy.data.materials.new(name)
            materials.append(material)
            mesh.materials.append(material)
        mesh.polygons[1].material_index = 1
        layer = mesh.uv_layers.new()
        uv = (np.arange(len(mesh.loops) * 2, dtype=np.float32) * 0.125).reshape((-1, 2))
        layer.uv.foreach_set('vector', uv.reshape(-1))
        mesh.calc_loop_triangles()
        expected_triangles = np.array([tuple(t.vertices) for t in mesh.loop_triangles], dtype=np.uint32)
        expected_loops = np.array([tuple(t.loops) for t in mesh.loop_triangles], dtype=np.uint32)
        expected_polygons = np.array([t.polygon_index for t in mesh.loop_triangles], dtype=np.uint32)
        with patch.object(store, '_foreach_array', wraps=store._foreach_array) as reads:
            model = store.commit_embedded_obj_mesh_and_build_proxy(
                mesh, model_id='source', display_name='source', source_path='source.obj')
        attributes = [call.args[1] for call in reads.call_args_list]
        assert attributes.count('co') == 1
        assert not {'vertices', 'polygon_index', 'loops', 'uv', 'vertex_index', 'material_index'} & set(attributes)
        np.testing.assert_array_equal(model.vertices_float64, vertices)
        np.testing.assert_array_equal(model.triangles_uint32, expected_triangles)
        np.testing.assert_array_equal(model.triangle_loop_indices_uint32, expected_loops)
        np.testing.assert_array_equal(model.triangle_polygon_indices_uint32, expected_polygons)
        np.testing.assert_array_equal(model.texcoords_float64[:, :2], uv)
        np.testing.assert_array_equal(model.triangle_material_indices_int32, [0, 1, 0])
        np.testing.assert_array_equal(model.registration_vertex_indices, [0, 1, 2, 3])
        assert model.display_vertex_indices is model.registration_vertex_indices
        for field in model.__dataclass_fields__:
            value = getattr(model, field)
            if isinstance(value, np.ndarray):
                assert not value.flags.writeable and value.flags.c_contiguous, field
        exported = store.extract_embedded_obj_export_geometry(model.embedded_asset_uuid, expected_model_id='source', expected_revision=1)
        np.testing.assert_array_equal(exported.vertices_float64, vertices)
        np.testing.assert_array_equal(exported.loop_vertex_indices_uint32, expected_triangles.reshape(-1))
        np.testing.assert_array_equal(exported.texcoords_float64[:, :2], uv)
    finally:
        bpy.data.meshes.remove(mesh)
        for material in materials:
            bpy.data.materials.remove(material)


def test_mixed_polygons_keep_blender_tessellation():
    """Quad와 오목 N-gon은 원본 면을 유지하고 Blender가 만든 삼각형 순서와 UV 대응을 따릅니다."""
    from test_embedded_obj import _write_fixture
    with TemporaryDirectory() as directory:
        path = _write_fixture(Path(directory))
        staging = import_obj_to_staging(path)
        mesh = staging.mesh
        try:
            mesh.calc_loop_triangles()
            expected = [(tuple(t.vertices), tuple(t.loops), t.polygon_index) for t in mesh.loop_triangles]
            with patch.object(store, '_foreach_array', wraps=store._foreach_array) as reads:
                model = store.commit_embedded_obj_mesh_and_build_proxy(
                    mesh, model_id='mixed', display_name='mixed', source_path=path)
            assert {'vertices', 'polygon_index', 'loops'} <= {call.args[1] for call in reads.call_args_list}
            np.testing.assert_array_equal(model.triangles_uint32, [row[0] for row in expected])
            np.testing.assert_array_equal(model.triangle_loop_indices_uint32, [row[1] for row in expected])
            np.testing.assert_array_equal(model.triangle_polygon_indices_uint32, [row[2] for row in expected])
            np.testing.assert_array_equal(model.registration_vertex_indices, np.unique(model.triangles_uint32))
            assert model.original_polygon_count < model.face_count
        finally:
            staging.discard()


def test_full_display_reuses_model_and_explicit_limit_is_preserved():
    """전체 표시는 같은 검증 모델을 재사용하고 작은 명시적 상한은 기존 표본 결과를 유지합니다."""
    with TemporaryDirectory() as directory:
        path = Path(directory) / 'display.obj'
        path.write_text('v 0 0 0\nv 1 0 0\nv 0 1 0\nv 0 0 1\nf 3 1 2\nf 4 2 1\n', encoding='utf-8')
        for limit in (None, 4, 100, 2):
            runtime = RuntimeState()
            job = mesh_importer.create_mesh_import_job(path, display_vertex_limit=limit, prepare_gpu=False)
            while job.stage != mesh_importer.MESH_IMPORT_STAGE_PREPARE_DISPLAY_SAMPLE:
                job.step(runtime)
                assert not job.is_terminal, job.error_message
            original = job.staged_model
            try:
                if limit != 2:
                    with patch.object(mesh_importer, 'TriangleMeshModelData', side_effect=AssertionError('전체 형상 중복 검증')):
                        job.step(runtime)
                    assert job.staged_model is original
                else:
                    builder = mesh_importer.IncrementalDisplayIndexBuilder(
                        original.registration_vertex_indices.astype(np.uint64), max_points=2, _validated_unique=True)
                    while not builder.is_completed:
                        builder.step(max_records=1024, time_budget_seconds=0.025)
                    expected = original.registration_vertex_indices[builder.display_indices]
                    while job.stage == mesh_importer.MESH_IMPORT_STAGE_PREPARE_DISPLAY_SAMPLE:
                        job.step(runtime)
                    np.testing.assert_array_equal(job.staged_model.display_vertex_indices, expected)
                    assert job.staged_model.vertices_float64 is original.vertices_float64
                    assert job.staged_model.triangles_uint32 is original.triangles_uint32
                while not job.is_terminal:
                    job.step(runtime)
                assert job.stage == 'COMPLETED', job.error_message
                assert len(job.staged_model.display_vertex_indices) == (2 if limit == 2 else 4)
            finally:
                store.remove_embedded_obj_mesh(original.embedded_asset_uuid)


def test_corrupt_corner_indices_are_rejected_before_tessellation():
    """음수·범위 밖·중복 코너를 빠른 속성 경로에서도 삼각분할 전에 거부합니다."""
    for indices in ((0, 1, -1), (0, 1, 3), (0, 1, 1)):
        mesh = bpy.data.meshes.new('손상 코너 검사')
        try:
            mesh.from_pydata([(0, 0, 0), (1, 0, 0), (0, 1, 0)], [], [(0, 1, 2)])
            mesh.attributes['.corner_vert'].data.foreach_set('value', np.array(indices, dtype=np.int32))
            try:
                store._validated_geometry_arrays(mesh)
            except store.EmbeddedObjStoreError:
                pass
            else:
                raise AssertionError('손상된 코너를 허용했습니다.')
        finally:
            bpy.data.meshes.remove(mesh)


if __name__ == '__main__':
    for name, function in sorted(globals().copy().items()):
        if name.startswith('test_') and callable(function):
            function()
            print('통과:', name, flush=True)
