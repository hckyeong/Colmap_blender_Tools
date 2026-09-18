"""정렬용 퇴화면 제외가 내장 원본·저장 복원·OBJ 왕복에 전파되지 않는지 검증합니다."""

from pathlib import Path
from tempfile import TemporaryDirectory
import sys

import bpy
import numpy as np

for path in (Path(__file__).resolve().parents[3], Path(__file__).resolve().parent):
    sys.path.insert(0, str(path))

from tools.align.alignment.icp_geometry import create_cccore_geometry_snapshot
from tools.align.mesh_importer import run_mesh_import_job
from tools.align.mesh_io.embedded_obj_store import find_embedded_obj_mesh
from tools.align.project_persistence.runtime import (
    build_project_state_snapshot, schedule_project_restore_if_available,
)
from tools.align.project_persistence.store import write_active_project_state
from tools.align.state import MarkerPair, RuntimeState, SelectedPoint


def _write_fixture(root: Path) -> Path:
    """UV·재질이 다른 정상 사각면과 서로 다른 정점 ID의 공선 삼각면을 기록합니다."""
    (root / "source.mtl").write_text(
        "newmtl surface\nKd 1 0 0\nnewmtl line\nKd 0 1 0\n", encoding="utf-8",
    )
    path = root / "source.obj"
    path.write_text(
        "mtllib source.mtl\n"
        "v 0 0 0\nv 2 0 0\nv 2 2 0\nv 0 2 0\n"
        "v 10 0 0\nv 11 0 0\nv 12 0 0\n"
        "vt 0 0\nvt 1 0\nvt 1 1\nvt 0 1\nvt .25 .25\nvt .5 .5\nvt .75 .75\n"
        "usemtl surface\nf 1/1 2/2 3/3 4/4\nusemtl line\nf 5/5 6/6 7/7\n",
        encoding="utf-8",
    )
    return path


def _mesh_values(mesh) -> tuple:
    """원본 정점·Polygon·Loop·UV·면별 재질을 Blender 참조 없이 값으로 기록합니다."""
    return (
        tuple(tuple(vertex.co) for vertex in mesh.vertices),
        tuple(tuple(polygon.vertices) for polygon in mesh.polygons),
        tuple(loop.vertex_index for loop in mesh.loops),
        tuple(tuple(item.uv) for item in mesh.uv_layers.active.data),
        tuple(polygon.material_index for polygon in mesh.polygons),
        tuple((str(material.get("colmap_align_source_name", material.name)),
               tuple(material.diffuse_color)) for material in mesh.materials),
    )


def _model_values(model) -> tuple:
    """원본 프록시 배열·원본 면 매핑·리비전·읽기 전용 상태를 값으로 기록합니다."""
    arrays = (model.vertices_float64, model.triangles_uint32, model.triangle_polygon_indices_uint32)
    return tuple((array.shape, array.tobytes(), array.flags.writeable) for array in arrays) + (model.geometry_revision,)


def _drain_restore(runtime) -> None:
    """예약된 실제 프로젝트 복원 타이머를 제한 횟수 안에 완료하고 등록을 정리합니다."""
    callback = runtime.project_restore_timer
    assert callable(callback)
    try:
        for _ in range(512):
            if callback() is None:
                assert runtime.project_restore.phase == "IDLE", runtime.project_restore
                return
        raise AssertionError("프로젝트 복원이 제한 횟수 안에 끝나지 않았습니다.")
    finally:
        if bpy.app.timers.is_registered(callback):
            bpy.app.timers.unregister(callback)


def _assert_alignment_filter(runtime) -> None:
    """공개 CC 입력만 두 정상 삼각면·네 정점으로 압축되고 원본 출처는 유지되는지 확인합니다."""
    snapshot = create_cccore_geometry_snapshot(
        runtime, moving_model_id="moving", reference_model_ids=("reference",),
        initial_alignment_float64=np.eye(4),
    )
    for geometry in (snapshot.moving, snapshot.references[0]):
        assert geometry.positions_float64.shape == (4, 3)
        assert geometry.triangles_uint32.shape == (2, 3)
        assert int(geometry.triangles_uint32.max()) == 3
        assert not geometry.positions_float64.flags.writeable
        assert not geometry.triangles_uint32.flags.writeable
        assert geometry.provenance.sources[0].point_count == 7
        assert geometry.provenance.sources[0].triangle_count == 3


def main() -> None:
    """실제 OBJ 가져오기·정렬 입력·blend 저장 복원·OBJ 재가져오기를 왕복 검증합니다."""
    from test_obj_export import _finish_job
    from test_project_restore_roundtrip import _remove_owned_project_texts
    from tools.align.mesh_io import read_obj
    from tools.align.mesh_io.obj_native_import import import_obj_to_staging
    from tools.align.operators.export_obj import ObjExportJob, build_obj_export_snapshots

    _remove_owned_project_texts()
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        source = _write_fixture(root)
        runtime = RuntimeState()
        originals = {}
        assets = {}
        for model_id in ("reference", "moving"):
            job = run_mesh_import_job(
                runtime, source, model_id_factory=lambda model_id=model_id: model_id,
                axis_preset="BLENDER", axis_forward="Y", axis_up="Z", prepare_gpu=False,
            )
            assert job.stage == "COMPLETED", job.error_message
            model = runtime.model_repository.models[model_id]
            mesh = find_embedded_obj_mesh(model.embedded_asset_uuid)
            assert tuple(polygon.loop_total for polygon in mesh.polygons) == (4, 3)
            assert len(mesh.loops) == 7 and len(mesh.uv_layers.active.data) == 7
            assert model.triangles_uint32.shape == (3, 3)
            originals[model_id] = (_mesh_values(mesh), _model_values(model))
            assets[model_id] = model.embedded_asset_uuid
        marker_points = []
        for role, model_id in (("REFERENCE", "reference"), ("MOVING", "moving")):
            model = runtime.model_repository.models[model_id]
            position = tuple(model.vertices_float64[model.triangles_uint32[1]].mean(axis=0))
            marker_points.append(SelectedPoint(role, 1, position, model_id, "MESH_SURFACE", 1, "MAIN"))
        runtime.marker_selection.completed_pairs.append(MarkerPair(1, *marker_points))
        runtime.marker_selection.next_pair_id = 2
        _assert_alignment_filter(runtime)
        for model_id in assets:
            assert _mesh_values(find_embedded_obj_mesh(assets[model_id])) == originals[model_id][0]
            assert _model_values(runtime.model_repository.models[model_id]) == originals[model_id][1]
        assert runtime.marker_selection.completed_pairs[0].moving.source_point_id == 1

        write_active_project_state(build_project_state_snapshot(runtime))
        blend = root / "preserved.blend"
        assert bpy.ops.wm.save_as_mainfile(filepath=str(blend), check_existing=False) == {"FINISHED"}
        source.unlink()
        assert bpy.ops.wm.open_mainfile(filepath=str(blend), load_ui=False) == {"FINISHED"}
        restored = RuntimeState()
        assert schedule_project_restore_if_available(restored)
        _drain_restore(restored)
        for model_id in assets:
            assert _mesh_values(find_embedded_obj_mesh(assets[model_id])) == originals[model_id][0]
            assert _model_values(restored.model_repository.models[model_id]) == originals[model_id][1]
        assert restored.marker_selection.completed_pairs[0].moving.source_point_id == 1
        assert restored.marker_selection.completed_pairs[0].moving.local_position == marker_points[1].local_position
        assert restored.model_catalog.select_model("moving", has_marker_draft=False, completed_pair_count=0)
        _assert_alignment_filter(restored)

        export_snapshot = build_obj_export_snapshots(restored)[0]
        assert export_snapshot.face_count == 2
        np.testing.assert_array_equal(export_snapshot.polygon_loop_totals_uint32, (4, 3))
        job = ObjExportJob(root / "aligned_obj", (export_snapshot,),
                           overwrite_existing_output=False, job_id="degenerate-preserved")
        _finish_job(job)
        result = job.finalize()
        exported = result.output_root / export_snapshot.output_filename
        parsed = read_obj(exported, preserve_polygons=True)
        assert parsed.polygon_count == 2
        np.testing.assert_array_equal(parsed.polygon_loop_totals_uint32, (4, 3))
        np.testing.assert_array_equal(parsed.loop_vertex_indices_uint32, export_snapshot.loop_vertex_indices_uint32)
        np.testing.assert_array_equal(parsed.loop_texcoord_indices_int32, export_snapshot.loop_texcoord_indices_int32)
        np.testing.assert_array_equal(parsed.texcoords_float64, export_snapshot.texcoords_float64)
        staging = import_obj_to_staging(exported)
        try:
            actual = _mesh_values(staging.mesh)
            expected = originals["reference"][0]
            assert actual[:5] == expected[:5]
            assert tuple(color for _name, color in actual[5]) == tuple(color for _name, color in expected[5])
        finally:
            staging.discard()
        for model_id in assets:
            assert _mesh_values(find_embedded_obj_mesh(assets[model_id])) == originals[model_id][0]
            assert _model_values(restored.model_repository.models[model_id]) == originals[model_id][1]
        _remove_owned_project_texts()
    print("퇴화면 원본 보존 검사 통과: 정렬 입력만 필터·압축, 실제 blend 복원 및 OBJ 파서·네이티브 왕복")


if __name__ == "__main__":
    main()
