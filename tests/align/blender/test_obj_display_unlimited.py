"""OBJ의 25만 표시 상한 해제와 명시적 제한·내장 복원 호환을 검증합니다."""
from pathlib import Path
from tempfile import TemporaryDirectory
import sys

import numpy as np

for directory in (Path(__file__).resolve().parents[3], Path(__file__).resolve().parent):
    sys.path.insert(0, str(directory))

from tools.align.mesh_importer import create_mesh_import_job, run_mesh_import_job, MeshImportJobError
from tools.align.mesh_io.embedded_obj_store import remove_embedded_obj_mesh
from tools.align.project_persistence import encode_project_state, decode_project_state
from tools.align.project_persistence.runtime import build_project_state_snapshot, _restore_embedded_main, _restore_embedded_companion
from tools.align.state import RuntimeState


def _large_obj(path):
    """기존 상한을 넘는 250,002개 정점이 모두 면에 쓰이는 실제 OBJ를 만듭니다."""
    count = 83_334
    with path.open("w", encoding="utf-8") as output:
        for index in range(count):
            x, y = (index % 1000) * 2, (index // 1000) * 2
            output.write(f"v {x} {y} 0\nv {x + 1} {y} 0\nv {x} {y + 1} 0\n")
        for index in range(count):
            first = index * 3 + 1
            output.write(f"f {first} {first + 1} {first + 2}\n")
    return count * 3


def test_large_import_and_saved_limits():
    """기본 가져오기는 25만을 넘어 전부 표시하고 명시한 상한은 저장·복원 후에도 유지합니다."""
    with TemporaryDirectory() as directory:
        path = Path(directory) / "all_vertices.obj"
        count = _large_obj(path)
        for limit in (None, 7):
            runtime = RuntimeState()
            kwargs = {} if limit is None else {"display_vertex_limit": limit}
            job = run_mesh_import_job(runtime, path, prepare_gpu=False, **kwargs)
            model = job.staged_model
            expected = count if limit is None else limit
            try:
                assert model.vertex_count == count and len(model.registration_vertex_indices) == count
                assert len(model.display_vertex_indices) == expected
                if limit is None:
                    np.testing.assert_array_equal(model.display_vertex_indices, model.registration_vertex_indices)
                metadata = runtime.model_repository.source_metadata[model.model_id]
                assert metadata.display_point_limit == expected
                snapshot = decode_project_state(encode_project_state(build_project_state_snapshot(runtime)))
                restored = RuntimeState()
                _restore_embedded_main(restored, snapshot.models[0])
                result = restored.model_repository.models[model.model_id]
                np.testing.assert_array_equal(result.display_vertex_indices, model.display_vertex_indices)
                np.testing.assert_array_equal(result.vertices_float64, model.vertices_float64)
                np.testing.assert_array_equal(result.triangles_uint32, model.triangles_uint32)
            finally:
                remove_embedded_obj_mesh(model.embedded_asset_uuid, expected_model_id=model.model_id)


def test_companion_ignores_cloud_limit_after_restore():
    """부모 점군은 한 점으로 제한하더라도 연결 OBJ의 전체 표시 정점을 복원합니다."""
    from test_project_restore import _write_text_sparse_model, _run_import_until_terminal
    from tools.align.importer import create_import_job
    with TemporaryDirectory() as directory:
        root = Path(directory)
        _write_text_sparse_model(root / "sparse" / "0", point_offset=0.0)
        (root / "surface.obj").write_text("v 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n", encoding="utf-8")
        runtime = RuntimeState()
        job = create_import_job(root, input_format="TEXT", import_companion_obj=True, display_point_limit=1, prepare_gpu=False)
        assert _run_import_until_terminal(job, runtime) == "FINISHED", job.error_message
        model_id = job.staged_model.model_id
        mesh = runtime.model_repository.companion_meshes[model_id]
        try:
            assert len(mesh.display_vertex_indices) == 3
            assert runtime.model_repository.source_metadata[model_id].display_point_limit == 1
            snapshot = decode_project_state(encode_project_state(build_project_state_snapshot(runtime)))
            restored = RuntimeState()
            restored.model_repository.add_model(
                runtime.model_repository.models[model_id],
                runtime.model_repository.display_transforms[model_id],
                runtime.model_repository.source_metadata[model_id],
            )
            _restore_embedded_companion(restored, snapshot.models[0])
            np.testing.assert_array_equal(restored.model_repository.companion_meshes[model_id].display_vertex_indices, mesh.registration_vertex_indices)
        finally:
            remove_embedded_obj_mesh(mesh.embedded_asset_uuid, expected_model_id=model_id)


def test_invalid_explicit_limit_is_rejected():
    """제한 없음과 잘못된 입력을 구분하여 기존 양의 정수 검사 계약을 유지합니다."""
    for value in (0, -1, True, 2.5, "250000"):
        try:
            create_mesh_import_job("unused.obj", display_vertex_limit=value)
        except MeshImportJobError:
            pass
        else:
            raise AssertionError(f"잘못된 표시 상한이 허용되었습니다: {value!r}")


if __name__ == "__main__":
    for name, function in sorted(globals().copy().items()):
        if name.startswith("test_") and callable(function):
            function()
            print("통과:", name, flush=True)
