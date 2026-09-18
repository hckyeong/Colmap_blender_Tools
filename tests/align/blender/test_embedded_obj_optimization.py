"""내장 OBJ 최적화의 형상 보존과 손상 거부·복원 원자성을 검증합니다."""

from itertools import combinations
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from unittest.mock import patch

import bpy
import numpy as np


def _assert_repeated_rejected(starts, totals, corners) -> None:
    """면 내부에 중복 정점이 있는 배열이 거부되는지 확인합니다."""

    from tools.align.mesh_io import embedded_obj_store as store

    try:
        store._validate_polygon_vertices(starts, totals, corners)
    except store.EmbeddedObjStoreError as error:
        assert "같은 정점" in str(error)
    else:
        raise AssertionError("반복 정점을 거부하지 않았습니다.")


def test_polygon_validation_matches_reference() -> None:
    """혼합 면·묶음 경계·비인접 중복 판정이 기존 면별 unique 결과와 같은지 확인합니다."""

    from tools.align.mesh_io import embedded_obj_store as store

    rng = np.random.default_rng(831)
    totals = rng.integers(3, 13, size=65_540, dtype=np.int64)
    starts = np.concatenate(([0], np.cumsum(totals[:-1])))
    corners = np.concatenate([rng.permutation(int(size)) for size in totals])
    assert all(len(np.unique(corners[start:start + size])) == size for start, size in zip(starts, totals))
    store._validate_polygon_vertices(starts, totals, corners)
    for face in (0, 65_535, 65_536, len(totals) - 1):
        index = starts[face] + totals[face] - 1
        saved = corners[index]
        corners[index] = corners[starts[face]]
        _assert_repeated_rejected(starts, totals, corners)
        corners[index] = saved
    for size in (3, 4, 5, 11):
        for first, second in combinations(range(size), 2):
            values = np.arange(size, dtype=np.int64)
            values[second] = values[first]
            _assert_repeated_rejected(np.array([0]), np.array([size]), values)
    large = np.arange(262_145, dtype=np.int64)
    store._validate_polygon_vertices(np.array([0]), np.array([len(large)]), large)
    large[-1] = large[0]
    _assert_repeated_rejected(np.array([0]), np.array([len(large)]), large)


def test_float_extraction_and_proxy_preserve_source() -> None:
    """float 버퍼 변경과 좌표 재사용 뒤에도 Polygon·Loop·UV·프록시 값이 정확히 같은지 확인합니다."""

    from test_embedded_obj import _write_fixture
    from tools.align.mesh_io import embedded_obj_store as store
    from tools.align.mesh_io.obj_native_import import import_obj_to_staging

    with TemporaryDirectory() as temporary:
        source = _write_fixture(Path(temporary))
        staging = import_obj_to_staging(source)
        mesh = staging.mesh
        try:
            mesh.vertices[0].co = (0.123456789, -12345.6789, 1e-20)
            mesh.uv_layers.active.data[0].uv = (0.123456789, -12345.6789)
            expected_vertices = np.empty((len(mesh.vertices), 3), dtype=np.float64)
            mesh.vertices.foreach_get("co", expected_vertices.ravel())
            expected_uv = np.empty((len(mesh.loops), 2), dtype=np.float64)
            mesh.uv_layers.active.data.foreach_get("uv", expected_uv.ravel())
            original_faces = tuple((face.loop_start, face.loop_total, face.material_index) for face in mesh.polygons)
            original_loops = tuple(loop.vertex_index for loop in mesh.loops)
            with patch.object(store, "_validated_geometry_vertices", wraps=store._validated_geometry_vertices) as validate:
                proxy = store.commit_embedded_obj_mesh_and_build_proxy(
                    mesh, model_id="precision", display_name="precision", source_path=source,
                )
                assert validate.call_count == 1
            assert np.array_equal(proxy.vertices_float64, expected_vertices)
            assert np.array_equal(proxy.texcoords_float64[:, :2], expected_uv)
            assert proxy.vertices_float64.dtype == np.float64 and proxy.vertices_float64.flags.c_contiguous
            assert tuple((face.loop_start, face.loop_total, face.material_index) for face in mesh.polygons) == original_faces
            assert tuple(loop.vertex_index for loop in mesh.loops) == original_loops
            mesh.calc_loop_triangles()
            assert np.array_equal(proxy.triangles_uint32, [tuple(tri.vertices) for tri in mesh.loop_triangles])
            assert np.array_equal(proxy.triangle_loop_indices_uint32, [tuple(tri.loops) for tri in mesh.loop_triangles])
            assert store.validate_embedded_obj_mesh(mesh) == mesh
            saved = mesh.loops[1].vertex_index
            mesh.loops[1].vertex_index = mesh.loops[0].vertex_index
            try:
                store.validate_embedded_obj_mesh(mesh)
            except store.EmbeddedObjStoreError as error:
                assert "같은 정점" in str(error)
            else:
                raise AssertionError("공개 전체 검증에서 손상 면을 허용했습니다.")
            finally:
                mesh.loops[1].vertex_index = saved
        finally:
            staging.discard()


def test_import_restore_validate_once_and_reject_late_corruption() -> None:
    """복원이 각 형상을 한 번 검사하고 사전 점검 이후 손상도 부분 확정 없이 거부하는지 확인합니다."""

    from test_embedded_obj import _write_fixture
    from test_project_restore import _drain_restore_timer, _remove_owned_project_texts
    from tools.align.mesh_importer import run_mesh_import_job
    from tools.align.mesh_io import embedded_obj_store as store
    from tools.align.project_persistence import runtime as persistence
    from tools.align.project_persistence.store import active_project_text, write_active_project_state
    from tools.align.state import RuntimeState

    _remove_owned_project_texts()
    meshes = []
    try:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            origin = RuntimeState()
            for model_id in ("first", "second"):
                path = _write_fixture(root, model_id + ".obj")
                with patch.object(store, "_validated_geometry_vertices", wraps=store._validated_geometry_vertices) as validate:
                    job = run_mesh_import_job(origin, path, model_id_factory=lambda: model_id, prepare_gpu=False)
                    assert job.stage == "COMPLETED", job.error_message
                    assert validate.call_count == 1
                meshes.append(store.find_embedded_obj_mesh(job.staged_model.embedded_asset_uuid))
                path.unlink()
            snapshot = persistence.build_project_state_snapshot(origin)
            write_active_project_state(snapshot)
            text_before = active_project_text().as_string()
            restored = RuntimeState()
            with patch.object(store, "_validated_geometry_vertices", wraps=store._validated_geometry_vertices) as validate:
                assert persistence.schedule_project_restore_if_available(restored)
                assert validate.call_count == 0
                _drain_restore_timer(restored)
                assert validate.call_count == 2
            assert restored.project_restore.phase == "IDLE", restored.project_restore.last_error
            for model_id, original in origin.model_repository.models.items():
                candidate = restored.model_repository.models[model_id]
                for field in original.__dataclass_fields__:
                    if isinstance(getattr(original, field), np.ndarray):
                        assert np.array_equal(getattr(original, field), getattr(candidate, field)), field
            for corruption in ("coordinate", "revision"):
                failed = RuntimeState()
                assert persistence.schedule_project_restore_if_available(failed)
                mesh = meshes[1]
                coordinate = tuple(mesh.vertices[0].co)
                revision = int(mesh["geometry_revision"])
                try:
                    if corruption == "coordinate":
                        mesh.vertices[0].co.x = float("nan")
                    else:
                        mesh["geometry_revision"] = revision + 1
                    _drain_restore_timer(failed)
                    assert failed.project_restore.phase == "FAILED"
                    assert not failed.model_repository.models and not failed.model_catalog.models
                    assert failed.project_restore.staging_runtime is None
                    assert active_project_text().as_string() == text_before
                    assert all(store.find_embedded_obj_mesh(item["asset_uuid"]) == item for item in meshes)
                finally:
                    mesh.vertices[0].co = coordinate
                    mesh["geometry_revision"] = revision
    finally:
        for mesh in meshes:
            store.remove_embedded_obj_mesh(mesh["asset_uuid"], expected_model_id=mesh["model_id"])
        _remove_owned_project_texts()


def run_all() -> None:
    """소스 경로를 등록하고 최적화 회귀 검증을 실행합니다."""

    for path in (Path(__file__).resolve().parents[3], Path(__file__).resolve().parent):
        if str(path) not in sys.path:
            sys.path.insert(0, str(path))
    test_polygon_validation_matches_reference()
    test_float_extraction_and_proxy_preserve_source()
    test_import_restore_validate_once_and_reject_late_corruption()
    print("내장 OBJ 최적화 검증: 통과")


if __name__ == "__main__":
    run_all()
