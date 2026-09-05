"""형상별 축 편집의 수학·작업 잠금·실패 복구·세션 이력을 검증합니다."""
from pathlib import Path
import sys
from contextlib import contextmanager, ExitStack
from unittest.mock import patch
import numpy as np


def _add_test_paths():
    """실험 소스와 공통 회귀 자료를 현재 Blender에 연결합니다."""
    folder = Path(__file__).resolve().parent
    for path in (folder.parents[2], folder):
        if str(path) not in sys.path:
            sys.path.insert(0, str(path))


@contextmanager
def _fixture():
    """실제 저장소와 Undo를 유지하면서 화면 알림만 격리합니다."""
    from test_companion_display import _runtime_fixture
    from tools.align import session
    from tools.align.project_persistence import runtime as persistence
    with _runtime_fixture() as runtime, ExitStack() as stack:
        runtime.session_active = True
        stack.enter_context(patch.object(session, "RUNTIME", runtime))
        stack.enter_context(patch.object(session, "request_redraw"))
        stack.enter_context(patch.object(session, "set_workspace_status_text"))
        stack.enter_context(patch.object(persistence, "notify_project_state_changed"))
        yield runtime


def _axes(angle=0.0):
    """원점에서 순서가 드러나는 비영 회전의 점군·메시 축을 구성합니다."""
    from tools.align.display_axes import create_model_display_transform
    return (
        create_model_display_transform("REALITY_CAPTURE", "Z", "-Y", rotation_xyz_degrees=(angle, 11, -23)),
        create_model_display_transform("REALITY_CAPTURE_OBJ", "Y", "Z", rotation_xyz_degrees=(-angle, 7, 19)),
    )


def _update(runtime, token, angle=32.0):
    """두 형상의 임시 축을 한 번에 갱신합니다."""
    from tools.align.axis_settings import update_axis_edit
    main, mesh = _axes(angle)
    update_axis_edit(runtime, token, main_transform=main, companion_transform=mesh)
    return main, mesh


def _expect_error(operation, message=None):
    """지정 경계가 예외를 반환하고 필요한 차단 사유를 포함하는지 확인합니다."""
    try:
        operation()
    except (ValueError, RuntimeError) as error:
        if message:
            assert message in str(error), str(error)
    else:
        raise AssertionError("실패해야 하는 요청이 성공했습니다.")


def test_rc_pair_extra_rotation_and_legacy_fingerprint():
    """RC 좌표 대응·고정축 회전 순서·기존 영 회전의 지문을 검증합니다."""
    from math import cos, sin, radians
    from bpy_extras.io_utils import axis_conversion
    from tools.align.display_axes import create_model_display_transform, _transform_fingerprint
    main = create_model_display_transform("REALITY_CAPTURE", "Z", "-Y")
    mesh = create_model_display_transform("REALITY_CAPTURE_OBJ", "Y", "Z")
    raw_mesh = np.asarray(((2, 3, 5), (-7, 11, 13), (17, -19, 23)), dtype=float)
    raw_points = np.column_stack((raw_mesh[:, 0], -raw_mesh[:, 2], raw_mesh[:, 1]))
    np.testing.assert_allclose(raw_points @ main.matrix_float64[:3, :3].T, raw_mesh @ mesh.matrix_float64[:3, :3].T)
    legacy = np.asarray(axis_conversion(from_forward="Z", from_up="-Y", to_forward="Y", to_up="Z").to_4x4(), dtype=np.float64)
    assert main.fingerprint == _transform_fingerprint("REALITY_CAPTURE", "Z", "-Y", legacy)
    x,y,z = map(radians, (31, -47, 59))
    rx = np.array(((1,0,0),(0,cos(x),-sin(x)),(0,sin(x),cos(x))))
    ry = np.array(((cos(y),0,sin(y)),(0,1,0),(-sin(y),0,cos(y))))
    rz = np.array(((cos(z),-sin(z),0),(sin(z),cos(z),0),(0,0,1)))
    extra = create_model_display_transform("REALITY_CAPTURE", "Z", "-Y", rotation_xyz_degrees=(31,-47,59))
    np.testing.assert_allclose(extra.matrix_float64[:3,:3], rz @ ry @ rx @ legacy[:3,:3], atol=1e-12)
    for forward in ("X","Y","Z","-X","-Y","-Z"):
        for up in ("X","Y","Z","-X","-Y","-Z"):
            if forward[-1] == up[-1]:
                _expect_error(lambda: create_model_display_transform("CUSTOM",forward,up))
                continue
            matrix = create_model_display_transform("CUSTOM",forward,up,rotation_xyz_degrees=(31,-47,59)).matrix_float64
            np.testing.assert_allclose(matrix[:3,:3].T @ matrix[:3,:3], np.eye(3), atol=1e-12)
            assert abs(np.linalg.det(matrix[:3,:3])-1) < 1e-12
    for values in ((float("nan"),0,0), (0,float("inf"),0), (1,2)):
        _expect_error(lambda: create_model_display_transform("REALITY_CAPTURE_OBJ","Y","Z",rotation_xyz_degrees=values))


def test_preview_cancel_noop_and_target_isolation():
    """미리보기·취소·무변경 적용이 확정값과 이력을 건드리지 않는지 검증합니다."""
    from tools.align.axis_settings import begin_axis_edit, cancel_axis_edit, commit_axis_edit, effective_axis_transform
    with _fixture() as runtime:
        repository = runtime.model_repository
        main_before = repository.display_transform_for_model_id("moving")
        mesh_before = repository.companion_display_transform_for_model_id("moving")
        other = repository.display_transform_for_model_id("reference")
        token = begin_axis_edit(runtime,"moving")
        main, mesh = _update(runtime,token)
        assert effective_axis_transform(runtime,"moving") is main
        assert effective_axis_transform(runtime,"moving",companion=True) is mesh
        assert repository.display_transform_for_model_id("moving") is main_before
        assert repository.companion_display_transform_for_model_id("moving") is mesh_before
        assert effective_axis_transform(runtime,"reference") is other
        assert not cancel_axis_edit(runtime,"다른 토큰")
        assert cancel_axis_edit(runtime,token)
        assert not runtime.undo_journal.can_undo
        token = begin_axis_edit(runtime,"moving")
        assert commit_axis_edit(runtime,token) is False
        assert runtime.undo_journal.undo_count == 0


def test_commit_undo_redo_preserves_raw_and_alignment_and_restores_quality():
    """두 축과 품질 상태를 단일 이력으로 복구하며 원본·공통 정렬은 보존합니다."""
    from tools.align.axis_settings import begin_axis_edit, commit_axis_edit
    from tools.align.alignment.types import AlignmentProvenanceRecord
    from tools.align import session
    with _fixture() as runtime:
        repository = runtime.model_repository
        before_main = repository.display_transform_for_model_id("moving")
        before_mesh = repository.companion_display_transform_for_model_id("moving")
        before_alignment = repository.alignment_transform_for_model_id("moving")
        raw = repository.models["moving"].points.positions.copy()
        mesh_raw = repository.companion_mesh_for_model_id("moving").vertices_float64.copy()
        quality = AlignmentProvenanceRecord(model_id="moving",method="ICP",icp_rmse=0.02,icp_fitness=0.95)
        runtime.model_catalog.set_alignment_provenance("moving",quality)
        selected = runtime.model_catalog.selected_model_ids
        token = begin_axis_edit(runtime,"moving")
        main, mesh = _update(runtime,token)
        assert not session.undo_session_state()
        assert commit_axis_edit(runtime,token)
        assert runtime.undo_journal.undo_count == 1
        assert repository.display_transform_for_model_id("moving") is main
        assert repository.companion_display_transform_for_model_id("moving") is mesh
        updated = runtime.model_catalog.models["moving"].alignment_provenance
        assert updated.method == "MANUAL" and updated.icp_rmse is None and updated.icp_fitness is None
        assert session.undo_session_state()
        assert repository.display_transform_for_model_id("moving") is before_main
        assert repository.companion_display_transform_for_model_id("moving") is before_mesh
        assert runtime.model_catalog.models["moving"].alignment_provenance is quality
        assert session.redo_session_state()
        assert repository.display_transform_for_model_id("moving") is main
        assert runtime.model_catalog.models["moving"].alignment_provenance is updated
        assert repository.alignment_transform_for_model_id("moving") is before_alignment
        assert runtime.model_catalog.selected_model_ids == selected
        np.testing.assert_array_equal(repository.models["moving"].points.positions,raw)
        np.testing.assert_array_equal(repository.companion_mesh_for_model_id("moving").vertices_float64,mesh_raw)


def test_mesh_only_commit_preserves_colmap_quality_and_results():
    """연결 메시만 바뀐 확정에서는 희소 정렬 품질과 계산 결과를 유지합니다."""
    from tools.align.axis_settings import begin_axis_edit, update_axis_edit, commit_axis_edit
    with _fixture() as runtime:
        quality = runtime.model_catalog.models["moving"].alignment_provenance
        result = object()
        runtime.icp_alignment.result = result
        main = runtime.model_repository.display_transform_for_model_id("moving")
        token = begin_axis_edit(runtime,"moving")
        update_axis_edit(runtime,token,main_transform=main,companion_transform=_axes(44)[1])
        assert commit_axis_edit(runtime,token)
        assert runtime.model_catalog.models["moving"].alignment_provenance is quality
        assert runtime.icp_alignment.result is result


def test_partial_store_and_journal_failures_roll_back():
    """연결 축·감사 기록·Undo 기록 실패가 부분 확정을 남기지 않는지 검사합니다."""
    from tools.align.axis_settings import begin_axis_edit, commit_axis_edit, cancel_axis_edit
    for target, name in (("repository","set_companion_display_transform"),("catalog","set_alignment_provenance"),("journal","record_committed")):
        with _fixture() as runtime:
            repo = runtime.model_repository
            before_main = repo.display_transform_for_model_id("moving")
            before_mesh = repo.companion_display_transform_for_model_id("moving")
            before_record = runtime.model_catalog.models["moving"]
            token = begin_axis_edit(runtime,"moving")
            _update(runtime,token)
            obj = {"repository":repo,"catalog":runtime.model_catalog,"journal":runtime.undo_journal}[target]
            with patch.object(obj,name,side_effect=RuntimeError("의도한 기록 실패")):
                _expect_error(lambda: commit_axis_edit(runtime,token),"의도한 기록 실패")
            assert repo.display_transform_for_model_id("moving") is before_main
            assert repo.companion_display_transform_for_model_id("moving") is before_mesh
            assert runtime.model_catalog.models["moving"] == before_record
            assert runtime.undo_journal.undo_count == 0
            cancel_axis_edit(runtime,token)


def test_stale_edit_cannot_overwrite_replaced_geometry_or_alignment():
    """창을 연 뒤 같은 ID의 형상 또는 정렬이 바뀌면 이전 기준 적용을 거부합니다."""
    from test_companion_display import _colmap
    from tools.align.axis_settings import begin_axis_edit, commit_axis_edit
    from tools.align.alignment import create_sim3_transform
    for change in ("geometry","alignment"):
        with _fixture() as runtime:
            token = begin_axis_edit(runtime,"moving")
            _update(runtime,token)
            if change == "geometry":
                runtime.model_repository.models["moving"] = _colmap("moving")
            else:
                runtime.model_repository.set_alignment_transform("moving",create_sim3_transform(np.eye(3),1,np.asarray((1,2,3))))
            _expect_error(lambda: commit_axis_edit(runtime,token),"편집 중")
            assert runtime.undo_journal.undo_count == 0


def test_axis_edit_rejects_busy_and_restore_states():
    """정렬·내보내기·복원·가져오기와 겹쳐 축 창을 열 수 없는지 검사합니다."""
    from types import SimpleNamespace
    from tools.align.axis_settings import begin_axis_edit
    from tools.align.constants import PROJECT_RESTORE_PHASE_RESTORING, PROJECT_RESTORE_PHASE_NEEDS_RELINK
    with _fixture() as runtime:
        for phase in (PROJECT_RESTORE_PHASE_RESTORING, PROJECT_RESTORE_PHASE_NEEDS_RELINK):
            runtime.project_restore.phase = phase
            _expect_error(lambda: begin_axis_edit(runtime,"moving"))
        runtime.project_restore.clear()
        for name, value in (("merge_export",SimpleNamespace(is_running=True)),("obj_export",SimpleNamespace(is_running=True)),("layer_transform",SimpleNamespace(is_active=True)),("icp_alignment",SimpleNamespace(is_running=True,has_preview=False))):
            with patch.object(runtime,name,value):
                _expect_error(lambda: begin_axis_edit(runtime,"moving"))
        runtime.modal_active = True
        _expect_error(lambda: begin_axis_edit(runtime,"moving"))


def test_real_marker_preview_blocks_axis_edit_and_keeps_view_snapshot():
    """실제 마커 미리보기의 활성 속성과 시점 복구 값이 보호되는지 검사합니다."""
    from test_icp_runtime import _runtime_with_marker_preview
    from tools.align.axis_settings import begin_axis_edit
    runtime, result = _runtime_with_marker_preview()
    view = object()
    runtime.marker_alignment.p_view_snapshot = view
    assert runtime.marker_alignment.is_active
    _expect_error(lambda: begin_axis_edit(runtime,"moving"),"정렬")
    assert runtime.marker_alignment.result is result
    assert runtime.marker_alignment.p_view_snapshot is view
    assert not runtime.axis_settings.is_active


def test_axis_edit_blocks_calculation_and_mutation_entrypoints():
    """미리보기 중 실제 정렬·가져오기·출력·변환 진입점이 모두 잠기는지 검사합니다."""
    from tools.align.axis_settings import begin_axis_edit
    from tools.align.importer import begin_import_job
    from tools.align.mesh_importer import run_mesh_import_job
    from tools.align.alignment.runtime import create_marker_alignment_input, _build_icp_seed_from_current_alignment
    from tools.align.alignment.icp_runtime import begin_icp_job
    from tools.align.alignment.layer_transform import begin_layer_transform
    from tools.align.operators import export_model, export_obj, marker_alignment, import_model, layer_transform
    with _fixture() as runtime:
        begin_axis_edit(runtime,"moving")
        for operation in (
            lambda:begin_import_job(runtime,"unused"),lambda:run_mesh_import_job(runtime,"unused"),
            lambda:create_marker_alignment_input(runtime),lambda:_build_icp_seed_from_current_alignment(runtime),
            lambda:begin_icp_job(runtime),lambda:begin_layer_transform(runtime,role="MOVING"),
        ):
            _expect_error(operation,"축 설정")
        for module, call in (
            (export_model,lambda:export_model.merge_export_start_block_reason(require_output_path=False)),
            (export_obj,export_obj.obj_export_start_block_reason),
            (marker_alignment,marker_alignment.marker_preview_start_block_reason),
            (marker_alignment,marker_alignment.direct_align_start_block_reason),
            (import_model,import_model._import_preview_block_reason),
            (layer_transform,layer_transform.layer_transform_start_block_reason),
        ):
            with patch.object(module,"RUNTIME",runtime):
                assert "축 설정" in call()


def test_lifecycle_clears_preview_and_dialog_registry():
    """세션 수명 정리가 확정값을 바꾸지 않고 팝업 참조까지 비우는지 확인합니다."""
    from tools.align.axis_settings import begin_axis_edit
    from tools.align.operators import axis_settings as popup
    from tools.align import session
    with _fixture() as runtime:
        before = runtime.model_repository.display_transform_for_model_id("moving")
        token = begin_axis_edit(runtime,"moving")
        _update(runtime,token)
        popup._OPEN_DIALOGS[token] = object()
        session.cancel_axis_edit_for_lifecycle()
        assert not runtime.axis_settings.is_active and not popup._OPEN_DIALOGS
        assert runtime.model_repository.display_transform_for_model_id("moving") is before
        assert runtime.undo_journal.undo_count == 0



def test_mesh_marker_quality_follows_reference_axis_and_undo():
    """승인 후 마커가 비어 있어도 A 연결 축 변경이 종속 P 품질과 이력을 갱신합니다."""
    from tools.align.axis_settings import begin_axis_edit, update_axis_edit, commit_axis_edit
    from tools.align.alignment.types import AlignmentProvenanceRecord
    from tools.align import session

    with _fixture() as runtime:
        quality = AlignmentProvenanceRecord(
            model_id="moving", method="ICP", marker_rms=0.01, icp_rmse=0.02, icp_fitness=0.95,
            marker_geometry_dependencies=(("reference", "COMPANION"), ("moving", "COMPANION")),
        )
        runtime.model_catalog.set_alignment_provenance("moving", quality)
        reference_quality = runtime.model_catalog.models["reference"].alignment_provenance
        main = runtime.model_repository.display_transform_for_model_id("reference")
        old_mesh = runtime.model_repository.companion_display_transform_for_model_id("reference")
        runtime.icp_alignment.result = object()
        assert runtime.marker_selection.completed_pairs == []
        token = begin_axis_edit(runtime, "reference")
        update_axis_edit(runtime, token, main_transform=main, companion_transform=_axes(47)[1])
        assert commit_axis_edit(runtime, token)
        changed = runtime.model_catalog.models["moving"].alignment_provenance
        assert changed.method == "MANUAL" and changed.marker_rms is None and changed.icp_rmse is None
        assert changed.marker_geometry_dependencies == quality.marker_geometry_dependencies
        assert runtime.icp_alignment.result is None
        assert runtime.model_catalog.models["reference"].alignment_provenance is reference_quality
        assert session.undo_session_state()
        assert runtime.model_repository.companion_display_transform_for_model_id("reference") is old_mesh
        assert runtime.model_catalog.models["moving"].alignment_provenance is quality
        assert session.redo_session_state()
        assert runtime.model_catalog.models["moving"].alignment_provenance is changed


def test_mesh_axis_dependency_rolls_back_all_related_provenances():
    """종속 모델 품질 기록 실패가 축과 모든 감사 기록을 부분 변경 없이 복원합니다."""
    from tools.align.axis_settings import begin_axis_edit, update_axis_edit, commit_axis_edit, cancel_axis_edit
    from tools.align.alignment.types import AlignmentProvenanceRecord

    with _fixture() as runtime:
        quality = AlignmentProvenanceRecord(
            model_id="moving", method="ICP", icp_rmse=0.02,
            marker_geometry_dependencies=(("reference", "COMPANION"),),
        )
        runtime.model_catalog.set_alignment_provenance("moving", quality)
        original_setter = runtime.model_catalog.set_alignment_provenance
        before_catalog = dict(runtime.model_catalog.models)
        old_main = runtime.model_repository.display_transform_for_model_id("reference")
        old_mesh = runtime.model_repository.companion_display_transform_for_model_id("reference")
        token = begin_axis_edit(runtime, "reference")
        update_axis_edit(runtime, token, main_transform=old_main, companion_transform=_axes(57)[1])

        def fail_related(model_id, record):
            """주 형상 기록 이후 종속 기록에서만 실패를 주입합니다."""
            if model_id == "moving":
                raise RuntimeError("의도한 종속 기록 실패")
            return original_setter(model_id, record)

        with patch.object(runtime.model_catalog, "set_alignment_provenance", side_effect=fail_related):
            _expect_error(lambda: commit_axis_edit(runtime, token), "의도한 종속")
        assert runtime.model_catalog.models == before_catalog
        assert runtime.model_repository.companion_display_transform_for_model_id("reference") is old_mesh
        assert runtime.model_repository.display_transform_for_model_id("reference") is old_main
        assert runtime.undo_journal.undo_count == 0
        cancel_axis_edit(runtime, token)


def test_main_marker_dependency_ignores_companion_only_axis_change():
    """같은 소유자의 주 점군만 참조한 품질에는 연결 메시 축 변경을 전파하지 않습니다."""
    from tools.align.axis_settings import begin_axis_edit, update_axis_edit, commit_axis_edit
    from tools.align.alignment.types import AlignmentProvenanceRecord

    with _fixture() as runtime:
        quality = AlignmentProvenanceRecord(
            model_id="moving", method="ICP", icp_rmse=0.02,
            marker_geometry_dependencies=(("reference", "MAIN"), ("moving", "MAIN")),
        )
        runtime.model_catalog.set_alignment_provenance("moving", quality)
        main = runtime.model_repository.display_transform_for_model_id("reference")
        result = runtime.icp_alignment.result = object()
        token = begin_axis_edit(runtime, "reference")
        update_axis_edit(runtime, token, main_transform=main, companion_transform=_axes(41)[1])
        assert commit_axis_edit(runtime, token)
        assert runtime.model_catalog.models["moving"].alignment_provenance is quality
        assert runtime.icp_alignment.result is result


def run_all():
    """축 편집의 필수 계약 검사를 실제 Blender Python에서 실행합니다."""
    _add_test_paths()
    tests = [value for name,value in sorted(globals().items()) if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
        print("통과:",test.__name__,flush=True)
    print("축 편집 런타임 검사",len(tests),"개 통과",flush=True)


if __name__ == "__main__":
    run_all()
