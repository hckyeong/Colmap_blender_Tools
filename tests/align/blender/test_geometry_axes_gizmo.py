"""축 팝업의 기즈모 중심·원점 이동과 원본 보존·실행 취소를 검증합니다."""
from itertools import product
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np


def _bootstrap():
    """제품 소스와 기존 격리 런타임을 테스트 경로에 연결합니다."""
    folder = Path(__file__).resolve().parent
    for path in (folder.parents[2], folder):
        sys.path.insert(0, str(path))


def _press(ui, dialog, context, name):
    """실제 버튼 실행 경계를 통과시키고 오류 없는 완료를 확인합니다."""
    button = SimpleNamespace(edit_token=dialog.edit_token, report=dialog.report)
    assert getattr(ui, name).execute(button, context) == {"FINISHED"}, dialog.error_message
    assert not dialog.error_message


def _bounds_center(runtime, model_id):
    """두 형상의 저장된 경계 코너를 독립적으로 월드에 투영해 중심을 구합니다."""
    repo = runtime.model_repository
    state = runtime.axis_settings
    alignment = repo.alignment_transform_for_model_id(model_id).matrix_float64
    points = []
    for model, transform in ((repo.models[model_id], state.main_transform),
                             (repo.companion_mesh_for_model_id(model_id), state.companion_transform)):
        if model is None:
            continue
        corners = np.array([(*p, 1.0) for p in product(*zip(model.bounds_min, model.bounds_max))])
        points.extend((corners @ (alignment @ transform.matrix_float64).T)[:, :3])
    return (np.min(points, axis=0) + np.max(points, axis=0)) * 0.5


def _pivot(runtime, role, ids):
    """실제 뷰포트 기즈모가 조회하는 확정 피벗을 반환합니다."""
    from tools.align.alignment.layer_transform import layer_transform_pivot_for_selection
    return layer_transform_pivot_for_selection(runtime, role, ids)


def test_center_changes_only_pivot_and_cancel_apply_undo_redo():
    """중심 지정이 즉시 확정되고 팝업 취소에도 남으며 별도 이력으로 복원됩니다."""
    from test_geometry_axes_ui import _fixture, _open
    from tools.align.operators import axis_settings as ui
    from tools.align import axis_settings as service, session

    for model_id, remove_companion in (("moving", False), ("reference", True), ("standalone", False)):
        with _fixture() as (runtime, context):
            repo = runtime.model_repository
            if remove_companion:
                repo.companion_meshes.pop(model_id)
            main = repo.display_transform_for_model_id(model_id)
            companion = repo.companion_display_transform_for_model_id(model_id) if repo.companion_mesh_for_model_id(model_id) is not None else None
            alignment = repo.alignment_transform_for_model_id(model_id)
            catalog = runtime.model_catalog.snapshot()
            result = runtime.icp_alignment.result = object()
            role, ids = service._axis_gizmo_scope(runtime, model_id)
            before_pivot = _pivot(runtime, role, ids).copy()
            dialog = _open(model_id, context)
            expected = _bounds_center(runtime, model_id)
            with patch.object(repo, "set_display_transform", side_effect=AssertionError("불필요한 축 갱신")), \
                 patch.object(repo, "set_companion_display_transform", side_effect=AssertionError("불필요한 연결 축 갱신")):
                _press(ui, dialog, context, "COLMAPALIGN_OT_center_axis_gizmo_on_bounds")
                np.testing.assert_allclose(_pivot(runtime, role, ids), expected, atol=1e-11)
                assert runtime.undo_journal.undo_count == 1
                dialog.cancel(context)
                np.testing.assert_allclose(_pivot(runtime, role, ids), expected, atol=1e-11)
            assert repo.display_transform_for_model_id(model_id) is main
            if companion is not None:
                assert repo.companion_display_transform_for_model_id(model_id) is companion
            assert repo.alignment_transform_for_model_id(model_id) is alignment
            assert runtime.model_catalog.snapshot() == catalog
            assert runtime.icp_alignment.result is result
            with patch.object(session, "RUNTIME", runtime), patch.object(session, "request_redraw"), patch.object(session, "set_workspace_status_text"):
                assert session.undo_session_state()
                np.testing.assert_array_equal(_pivot(runtime, role, ids), before_pivot)
                assert session.redo_session_state()
                np.testing.assert_allclose(_pivot(runtime, role, ids), expected, atol=1e-11)
            dialog = _open(model_id, context)
            assert ui.COLMAPALIGN_OT_edit_geometry_axes.execute(dialog, context) == {"FINISHED"}
            assert runtime.undo_journal.undo_count == 1


def test_custom_pivot_origin_preserves_geometry_and_shared_motion():
    """기존 사용자 피벗과 경계 중심 모두 회전·비균일 배율·연결 축 차이 아래 원점에 도달합니다."""
    from test_geometry_axes_ui import _fixture, _open
    from tools.align.operators import axis_settings as ui
    from tools.align import axis_settings as service, session
    from tools.align.display_axes import create_model_display_transform
    from tools.align.alignment.types import create_sim3_transform
    from tools.align.alignment.layer_transform import set_layer_transform_custom_pivot

    for center_first in (False, True):
        with _fixture() as (runtime, context):
            repo = runtime.model_repository
            main = create_model_display_transform("CUSTOM", "X", "Z", rotation_xyz_degrees=(13, 29, -43),
                translation_xyz=(9, -8, 7), scale_xyz=(2, 3, 4), pivot_source=(2, 4, 6))
            companion = create_model_display_transform("BLENDER", "Y", "Z", rotation_xyz_degrees=(-71, 42, 16),
                translation_xyz=(-3, 1, 8), scale_xyz=(5, 0.5, 7), pivot_source=(8, -2, 5))
            repo.set_display_transform("moving", main)
            repo.set_companion_display_transform("moving", companion)
            rotation = create_model_display_transform("BLENDER", "Y", "Z", rotation_xyz_degrees=(14, 27, 39)).matrix_float64[:3, :3]
            alignment = create_sim3_transform(rotation, 2.0, np.array((4, 5, 6)))
            repo.set_alignment_transform("moving", alignment)
            role, ids = service._axis_gizmo_scope(runtime, "moving")
            old_pivot = set_layer_transform_custom_pivot(runtime, role=role, model_ids=ids,
                source_model_id="moving", source_position=(3.123456789, -4.5, 7.25))
            raw = repo.models["moving"].points.positions.copy()
            mesh = repo.companion_mesh_for_model_id("moving")
            vertices, triangles = mesh.vertices_float64.copy(), mesh.triangles_uint32.copy()
            untouched = repo.display_transform_for_model_id("standalone")
            dialog = _open("moving", context)
            dialog.preview_enabled = False
            ui._update_preview(dialog, context)
            if center_first:
                _press(ui, dialog, context, "COLMAPALIGN_OT_center_axis_gizmo_on_bounds")
                world = _bounds_center(runtime, "moving")
            else:
                world = old_pivot
            _press(ui, dialog, context, "COLMAPALIGN_OT_move_axis_gizmo_to_origin")
            state = runtime.axis_settings
            np.testing.assert_allclose(service.axis_gizmo_world_position(runtime, dialog.edit_token), 0, atol=1e-10)
            translation = np.eye(4)
            translation[:3, 3] = -world
            for before, after in ((main, state.main_transform), (companion, state.companion_transform)):
                np.testing.assert_allclose(alignment.matrix_float64 @ after.matrix_float64,
                    translation @ alignment.matrix_float64 @ before.matrix_float64, atol=1e-10)
            np.testing.assert_array_equal(repo.display_transform_for_model_id("moving").matrix_float64, state.main_transform.matrix_float64)
            np.testing.assert_allclose(_pivot(runtime, role, ids), 0, atol=1e-10)
            assert runtime.undo_journal.undo_count == (2 if center_first else 1)
            dialog.cancel(context)
            np.testing.assert_allclose(_pivot(runtime, role, ids), 0, atol=1e-10)
            assert repo.alignment_transform_for_model_id("moving") is alignment
            assert repo.display_transform_for_model_id("standalone") is untouched
            np.testing.assert_array_equal(repo.models["moving"].points.positions, raw)
            np.testing.assert_array_equal(mesh.vertices_float64, vertices)
            np.testing.assert_array_equal(mesh.triangles_uint32, triangles)
            with patch.object(session, "RUNTIME", runtime), patch.object(session, "request_redraw"), patch.object(session, "set_workspace_status_text"):
                assert session.undo_session_state()
                assert repo.display_transform_for_model_id("moving") is main
                assert repo.companion_display_transform_for_model_id("moving") is companion
                np.testing.assert_allclose(_pivot(runtime, role, ids), world, atol=1e-10)
                if center_first:
                    assert session.undo_session_state()
                    np.testing.assert_array_equal(_pivot(runtime, role, ids), old_pivot)
                    assert session.redo_session_state()
                    np.testing.assert_allclose(_pivot(runtime, role, ids), world, atol=1e-10)
                assert session.redo_session_state()
                np.testing.assert_allclose(_pivot(runtime, role, ids), 0, atol=1e-10)


def test_origin_with_default_pivot_multi_selection_and_unselected_owner():
    """복수 선택에서도 팝업 대상만 이동하고 미선택 대상의 기즈모는 단독 범위에 연결됩니다."""
    from test_geometry_axes_ui import _fixture, _open
    from tools.align.operators import axis_settings as ui
    from tools.align import axis_settings as service

    for model_id in ("moving", "standalone", "reference"):
        with _fixture() as (runtime, context):
            runtime.model_catalog.toggle_alignment_selection("standalone", has_marker_draft=False, completed_pair_count=0)
            role, ids = service._axis_gizmo_scope(runtime, model_id)
            before = dict(runtime.model_repository.display_transforms)
            selected = runtime.model_catalog.selected_model_ids
            dialog = _open(model_id, context)
            _press(ui, dialog, context, "COLMAPALIGN_OT_move_axis_gizmo_to_origin")
            assert ui.COLMAPALIGN_OT_edit_geometry_axes.execute(dialog, context) == {"FINISHED"}
            np.testing.assert_allclose(_pivot(runtime, role, ids), 0, atol=1e-11)
            assert runtime.model_catalog.selected_model_ids == selected
            for other in before:
                if other != model_id:
                    assert runtime.model_repository.display_transforms[other] is before[other]


def test_failed_commit_restores_pivot_and_expired_buttons_are_rejected():
    """각 즉시 실행의 이력 실패가 확정·후보를 복원하고 만료된 토큰 실행은 차단됩니다."""
    from test_geometry_axes_ui import _fixture, _open
    from tools.align.operators import axis_settings as ui
    from tools.align import axis_settings as service

    for name in ("COLMAPALIGN_OT_center_axis_gizmo_on_bounds", "COLMAPALIGN_OT_move_axis_gizmo_to_origin"):
        with _fixture() as (runtime, context):
            baseline = service._gizmo_snapshot(runtime)
            before = runtime.model_repository.display_transform_for_model_id("moving")
            dialog = _open("moving", context)
            original_snapshot = runtime.axis_settings.baseline
            button = SimpleNamespace(edit_token=dialog.edit_token, report=dialog.report)
            with patch.object(runtime.undo_journal, "record_committed", return_value=False):
                assert getattr(ui, name).execute(button, context) == {"CANCELLED"}
            assert service._gizmo_snapshot(runtime) == baseline
            assert runtime.model_repository.display_transform_for_model_id("moving") is before
            assert runtime.axis_settings.baseline is original_snapshot
            assert runtime.axis_settings.main_transform is before
            assert runtime.undo_journal.undo_count == 0
            dialog.cancel(context)
            assert getattr(ui, name).execute(button, context) == {"CANCELLED"}


def test_immediate_buttons_preserve_unapplied_axes_and_use_visible_geometry():
    """즉시 실행은 미적용 회전·배율을 확정하지 않고 미리보기 선택에 맞는 위치를 사용합니다."""
    from test_geometry_axes_ui import _fixture, _open
    from tools.align.operators import axis_settings as ui
    from tools.align import axis_settings as service

    for preview in (True, False):
        for center in (True, False):
            with _fixture() as (runtime, context):
                repo = runtime.model_repository
                before = repo.display_transform_for_model_id("moving")
                alignment = repo.alignment_transform_for_model_id("moving").matrix_float64
                dialog = _open("moving", context)
                dialog.dataset_rotation, dialog.dataset_scale = (17, 29, 43), (2, 3, 4)
                dialog.dataset_translation = (7, 11, 13)
                dialog.preview_enabled = preview
                ui._update_preview(dialog, context)
                draft = runtime.axis_settings.main_transform
                world = service.axis_gizmo_world_position(runtime, dialog.edit_token, displayed=True)
                if center:
                    _press(ui, dialog, context, "COLMAPALIGN_OT_center_axis_gizmo_on_bounds")
                    assert repo.display_transform_for_model_id("moving") is before
                    assert runtime.axis_settings.main_transform.fingerprint == draft.fingerprint
                else:
                    _press(ui, dialog, context, "COLMAPALIGN_OT_move_axis_gizmo_to_origin")
                    shift = np.eye(4)
                    shift[:3, 3] = -world
                    np.testing.assert_allclose(alignment @ repo.display_transform_for_model_id("moving").matrix_float64,
                        shift @ alignment @ before.matrix_float64, atol=1e-10)
                    np.testing.assert_allclose(alignment @ runtime.axis_settings.main_transform.matrix_float64,
                        shift @ alignment @ draft.matrix_float64, atol=1e-10)
                    np.testing.assert_allclose(service.axis_gizmo_world_position(runtime, dialog.edit_token, displayed=True), 0, atol=1e-10)
                confirmed = repo.display_transform_for_model_id("moving")
                np.testing.assert_array_equal(confirmed.matrix_float64[:3, :3], before.matrix_float64[:3, :3])
                assert runtime.undo_journal.undo_count == 1
                dialog.cancel(context)
                assert repo.display_transform_for_model_id("moving") is confirmed


def test_popup_gizmo_updates_immediately_without_enabling_drag():
    """팝업 중 기즈모 위치는 갱신되지만 선택·드래그 및 다른 역할의 노출은 차단됩니다."""
    from test_geometry_axes_ui import _fixture, _open
    from tools.align.operators import axis_settings as ui, layer_transform as gizmos
    from tools.align import axis_settings as service

    with _fixture() as (runtime, context):
        with patch.object(gizmos, "RUNTIME", runtime), patch.object(gizmos, "is_session_active_for_window", return_value=True), patch.object(gizmos, "role_for_area", return_value="MOVING") as role:
            context.area = object()
            context.region = SimpleNamespace(type="WINDOW")
            context.scene = SimpleNamespace(colmap_align_settings=SimpleNamespace(show_transform_gizmo=True))
            group = SimpleNamespace(_translate=[(a, SimpleNamespace()) for a in "XYZ"],
                _rotate=[(a, SimpleNamespace()) for a in "XYZ"], _scale=SimpleNamespace())
            dialog = _open("moving", context)
            assert gizmos._gizmo_poll(context)
            _press(ui, dialog, context, "COLMAPALIGN_OT_center_axis_gizmo_on_bounds")
            expected = service.axis_gizmo_world_position(runtime, dialog.edit_token, displayed=True)
            gizmos.COLMAPALIGN_GGT_layer_transform.draw_prepare(group, context)
            for item in [g for _,g in group._translate + group._rotate] + [group._scale]:
                assert item.hide_select
                np.testing.assert_allclose(item.matrix_basis.translation, expected, atol=1e-6)
            assert gizmos.layer_transform_start_block_reason("MOVING") is not None
            role.return_value = "REFERENCE"
            assert not gizmos._gizmo_poll(context)
            dialog.cancel(context)
            role.return_value = "MOVING"
            gizmos.COLMAPALIGN_GGT_layer_transform.draw_prepare(group, context)
            assert not group._scale.hide_select


def run_all():
    """기즈모 두 버튼의 수치·상태 보존 회귀를 Blender Python에서 실행합니다."""
    _bootstrap()
    for name, test in sorted(globals().items()):
        if name.startswith("test_") and callable(test):
            test()
            print("통과:", name, flush=True)


if __name__ == "__main__":
    run_all()
