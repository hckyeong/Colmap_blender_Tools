"""같은 OBJ의 독립 불러오기·삭제 실패 복구·실제 저장 왕복을 검증합니다."""

from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from unittest.mock import patch

import bpy
import numpy as np


def _add_source_path() -> None:
    """소스와 공통 검사 도우미를 Blender 모듈 경로에 추가합니다."""
    for path in (Path(__file__).resolve().parents[3], Path(__file__).resolve().parent):
        if str(path) not in sys.path:
            sys.path.insert(0, str(path))


def _import_copies(runtime, path, count=3):
    """같은 원본을 독립 ID로 반복해서 가져옵니다."""
    from tools.align.mesh_importer import run_mesh_import_job

    models = []
    for index in range(count):
        job = run_mesh_import_job(
            runtime, path, model_id_factory=lambda: f"copy-{index}", prepare_gpu=False,
        )
        assert job.stage == "COMPLETED", job.error_message
        models.append(job.staged_model)
    return models


def _remove_meshes(models) -> None:
    """검사에서 생성한 내장 메시만 소유 ID를 확인하고 정리합니다."""
    from tools.align.mesh_io.embedded_obj_store import remove_embedded_obj_mesh

    for model in models:
        remove_embedded_obj_mesh(model.embedded_asset_uuid, expected_model_id=model.model_id)


def test_duplicate_obj_import_and_failed_commit_are_independent() -> None:
    """반복 가져오기의 이름·배열·자산과 등록 실패 후 기존 레이어 보존을 확인합니다."""
    from test_embedded_obj import _write_fixture
    from tools.align.mesh_importer import create_mesh_import_job
    from tools.align.mesh_io.embedded_obj_store import find_embedded_obj_mesh
    from tools.align.state import RuntimeState

    with TemporaryDirectory() as temporary:
        path = _write_fixture(Path(temporary))
        runtime = RuntimeState()
        models = _import_copies(runtime, path)
        repository = runtime.model_repository
        assert [model.display_name for model in models] == ["asymmetric", "asymmetric_2", "asymmetric_3"]
        assert len({model.embedded_asset_uuid for model in models}) == 3
        assert len({find_embedded_obj_mesh(model.embedded_asset_uuid).as_pointer() for model in models}) == 3
        for first, second in zip(models, models[1:]):
            assert np.array_equal(first.vertices_float64, second.vertices_float64)
            assert not np.shares_memory(first.vertices_float64, second.vertices_float64)
            assert not np.shares_memory(first.triangles_uint32, second.triangles_uint32)
        assert repository.model_id_for_source_path(path) == "copy-0"
        before_catalog = runtime.model_catalog.snapshot()
        before_meshes = {mesh.as_pointer() for mesh in bpy.data.meshes}
        job = create_mesh_import_job(path, model_id_factory=lambda: "failed-copy", prepare_gpu=False)
        with patch.object(type(runtime.model_catalog), "add_imported_model", side_effect=RuntimeError("등록 실패 검사")):
            while not job.is_terminal:
                job.step(runtime, time_budget_seconds=1.0)
        assert job.stage == "FAILED" and "등록 실패 검사" in job.error_message
        assert runtime.model_catalog.snapshot() == before_catalog
        assert tuple(repository.models) == ("copy-0", "copy-1", "copy-2")
        assert repository.model_id_for_source_path(path) == "copy-0"
        assert {mesh.as_pointer() for mesh in bpy.data.meshes} == before_meshes
        repository.remove_model("copy-1")
        assert repository.model_id_for_source_path(path) == "copy-0"
        repository.remove_model("copy-0")
        assert repository.model_id_for_source_path(path) == "copy-2"
        repository.remove_model("copy-2")
        assert not repository.contains_source_path(path)
        _remove_meshes(models)


def test_duplicate_layer_delete_failure_preserves_order_and_assets() -> None:
    """삭제 실패가 원래 등록 순서를 복구하고 정상 삭제가 다른 복사본을 보존하는지 확인합니다."""
    from test_embedded_obj import _write_fixture
    from test_project_restore import _remove_owned_project_texts
    from tools.align.mesh_io.embedded_obj_store import find_embedded_obj_mesh
    from tools.align.operators import model_group
    from tools.align.state import RUNTIME

    RUNTIME.clear_transient()
    with TemporaryDirectory() as temporary:
        path = _write_fixture(Path(temporary))
        models = _import_copies(RUNTIME, path)
        repository = RUNTIME.model_repository
        for model_id in ("copy-0", "copy-1"):
            RUNTIME.model_catalog.select_model(model_id, has_marker_draft=False, completed_pair_count=0)
            with patch.object(model_group, "sync_project_state_if_dirty", side_effect=RuntimeError("삭제 저장 실패 검사")):
                try:
                    model_group.delete_model_from_runtime(model_id)
                except RuntimeError as error:
                    assert str(error) == "삭제 저장 실패 검사"
                else:
                    raise AssertionError("삭제 실패를 전달하지 않았습니다.")
            assert tuple(repository.models) == ("copy-0", "copy-1", "copy-2")
            assert repository.model_id_for_source_path(path) == "copy-0"
            assert all(find_embedded_obj_mesh(model.embedded_asset_uuid) is not None for model in models)
        RUNTIME.model_catalog.select_model("copy-0", has_marker_draft=False, completed_pair_count=0)
        model_group.delete_model_from_runtime("copy-0")
        assert repository.model_id_for_source_path(path) == "copy-1"
        assert find_embedded_obj_mesh(models[0].embedded_asset_uuid, allow_missing=True) is None
        assert all(find_embedded_obj_mesh(model.embedded_asset_uuid) is not None for model in models[1:])
        _remove_meshes(models[1:])
    RUNTIME.clear_transient()
    _remove_owned_project_texts()


def test_duplicate_obj_blend_roundtrip_and_export_without_source() -> None:
    """원본 파일이 없어도 복사본별 변환·표시·이름을 복원하고 별도 OBJ로 내보냅니다."""
    from test_embedded_obj import _write_fixture
    from test_obj_export import _finish_job
    from test_project_restore import _drain_restore_timer, _remove_owned_project_texts
    from tools.align.alignment.types import Sim3Transform
    from tools.align.constants import MODEL_ACTIVATION_SOURCE_MANUAL
    from tools.align.mesh_io import read_obj
    from tools.align.operators.export_obj import ObjExportJob, build_obj_export_snapshots
    from tools.align.project_persistence.runtime import build_project_state_snapshot, schedule_project_restore_if_available
    from tools.align.project_persistence.store import write_active_project_state
    from tools.align.state import RuntimeState

    _remove_owned_project_texts()
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        path = _write_fixture(root)
        runtime = RuntimeState()
        models = _import_copies(runtime, path)
        matrices = {}
        for index, model in enumerate(models):
            matrix = np.eye(4, dtype=np.float64)
            matrix[:3, 3] = (index * 10.0, index * 2.0, 0.0)
            matrices[model.model_id] = matrix
            runtime.model_repository.set_alignment_transform(model.model_id, Sim3Transform(matrix_float64=matrix))
            if index:
                assert runtime.model_catalog.transfer_model_group(
                    model.model_id, source=MODEL_ACTIVATION_SOURCE_MANUAL,
                    has_marker_draft=False, completed_pair_count=0,
                )
        runtime.model_catalog.set_model_visibility("copy-1", False)
        write_active_project_state(build_project_state_snapshot(runtime))
        blend_path = root / "duplicate_sources.blend"
        assert bpy.ops.wm.save_as_mainfile(filepath=str(blend_path), check_existing=False) == {"FINISHED"}
        path.unlink()
        runtime.clear_transient()
        assert bpy.ops.wm.open_mainfile(filepath=str(blend_path), load_ui=False) == {"FINISHED"}
        restored = RuntimeState()
        assert schedule_project_restore_if_available(restored)
        _drain_restore_timer(restored)
        assert restored.project_restore.phase == "IDLE", restored.project_restore.last_error
        assert len(restored.model_repository.models) == 3
        for original in models:
            model = restored.model_repository.models[original.model_id]
            assert model.display_name == original.display_name
            assert model.embedded_asset_uuid == original.embedded_asset_uuid
            assert np.array_equal(model.vertices_float64, original.vertices_float64)
            assert np.array_equal(restored.model_repository.alignment_transform_for_model_id(model.model_id).matrix_float64, matrices[model.model_id])
        assert restored.model_catalog.models["copy-0"].is_visible
        assert not restored.model_catalog.models["copy-1"].is_visible
        assert restored.model_catalog.models["copy-2"].is_visible
        snapshots = build_obj_export_snapshots(restored)
        assert {item.output_filename for item in snapshots} == {"asymmetric.obj", "asymmetric_2.obj", "asymmetric_3.obj"}
        job = ObjExportJob(root / "aligned_obj", snapshots, overwrite_existing_output=False, job_id="duplicate-export")
        _finish_job(job)
        result = job.finalize()
        for snapshot in snapshots:
            exported = read_obj(result.output_root / snapshot.output_filename, preserve_polygons=True)
            assert tuple(exported.polygon_loop_totals_uint32) == (3, 4, 5)
        _remove_meshes(models)
    _remove_owned_project_texts()


def run_all() -> None:
    """동일 원본의 독립 레이어 사용에 대한 Blender 회귀 검사를 실행합니다."""
    _add_source_path()
    test_duplicate_obj_import_and_failed_commit_are_independent()
    test_duplicate_layer_delete_failure_preserves_order_and_assets()
    test_duplicate_obj_blend_roundtrip_and_export_without_source()
    print("test_duplicate_sources: 3개 검사 통과")


if __name__ == "__main__":
    run_all()
