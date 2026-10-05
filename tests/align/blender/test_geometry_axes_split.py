"""개별 원본 축·공통 변환·동시 표시와 저장 호환성을 실제 Blender에서 검증합니다."""

from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import patch

import bpy
import numpy as np


def _bootstrap():
    """제품 소스와 실제 런타임 시험 자료를 모듈 경로에 연결합니다."""
    folder = Path(__file__).resolve().parent
    for path in (folder.parents[2], folder):
        sys.path.insert(0, str(path))


def _assert_shared_delta(before_main, before_companion, main, companion):
    """모든 원본 좌표에 적용되는 변화량이 두 형상에서 동일한지 검증합니다."""
    delta = main.matrix_float64 @ np.linalg.inv(before_main.matrix_float64)
    np.testing.assert_allclose(companion.matrix_float64, delta @ before_companion.matrix_float64, atol=1e-10)


def test_shared_motion_preserves_native_axes_and_legacy_offsets():
    """기존 개별 회전·이동·비균일 배율과 RC 원본 축 차이를 공통 조작에서 유지합니다."""
    from test_geometry_axes_ui import _fixture, _open
    from tools.align.operators import axis_settings as ui
    from tools.align.display_axes import create_model_display_transform
    from tools.align.axis_edit_transform import dataset_world_center
    from tools.align.alignment.types import create_sim3_transform

    originals = (
        (create_model_display_transform("REALITY_CAPTURE", "Z", "-Y"), create_model_display_transform("REALITY_CAPTURE_OBJ", "Y", "Z")),
        (create_model_display_transform("CUSTOM", "X", "Z", rotation_xyz_degrees=(13, 29, -43),
             translation_xyz=(9, -8, 7), scale_xyz=(2, 3, 4), pivot_source=(2, 4, 6)),
         create_model_display_transform("BLENDER", "Y", "Z", rotation_xyz_degrees=(-71, 42, 16),
             translation_xyz=(-3, 1, 8), scale_xyz=(5, 0.5, 7), pivot_source=(8, -2, 5))),
    )
    for main, companion in originals:
        with _fixture() as (runtime, context):
            repository = runtime.model_repository
            repository.set_display_transform("moving", main)
            repository.set_companion_display_transform("moving", companion)
            rotation = create_model_display_transform("BLENDER", "Y", "Z", rotation_xyz_degrees=(14, 27, 39)).matrix_float64[:3, :3]
            alignment = create_sim3_transform(rotation, 2.0, np.array((4, 5, 6)))
            repository.set_alignment_transform("moving", alignment)
            center = dataset_world_center(runtime, "moving", main, companion)
            raw = repository.models["moving"].points.positions.copy()
            vertices = repository.companion_mesh_for_model_id("moving").vertices_float64.copy()
            dialog = _open("moving", context)
            assert runtime.axis_settings.main_transform is main
            assert runtime.axis_settings.companion_transform is companion
            dialog.dataset_rotation = (33, -17, 66)
            ui._update_preview(dialog, context)
            state = runtime.axis_settings
            _assert_shared_delta(main, companion, state.main_transform, state.companion_transform)
            common = ui._common_transform(dialog)
            np.testing.assert_allclose((alignment.matrix_float64 @ common.matrix_float64 @ np.append(common.pivot_source, 1))[:3], center, atol=1e-10)
            dialog.dataset_scale = (0.75, 2.5, 4.25)
            ui._update_dataset_scale(dialog, context)
            _assert_shared_delta(main, companion, state.main_transform, state.companion_transform)
            assert state.main_transform.forward_axis == main.forward_axis
            assert state.companion_transform.forward_axis == companion.forward_axis
            dialog.dataset_translation = (31, -12, 18)
            ui._update_preview(dialog, context)
            _assert_shared_delta(main, companion, state.main_transform, state.companion_transform)
            before = state.main_transform, state.companion_transform
            origin = SimpleNamespace(edit_token=dialog.edit_token, component="BOTH", report=dialog.report)
            assert ui.COLMAPALIGN_OT_center_geometry_at_origin.execute(origin, context) == {"FINISHED"}
            np.testing.assert_allclose(dataset_world_center(runtime, "moving", state.main_transform, state.companion_transform), 0, atol=1e-10)
            _assert_shared_delta(*before, state.main_transform, state.companion_transform)
            np.testing.assert_allclose(before[0].matrix_float64[:3, :3], state.main_transform.matrix_float64[:3, :3], atol=1e-12)
            np.testing.assert_array_equal(repository.models["moving"].points.positions, raw)
            np.testing.assert_array_equal(repository.companion_mesh_for_model_id("moving").vertices_float64, vertices)
            assert repository.alignment_transform_for_model_id("moving") is alignment
            dialog.cancel(context)
            assert repository.display_transform_for_model_id("moving") is main
            assert repository.companion_display_transform_for_model_id("moving") is companion


def test_raw_axis_only_changes_its_geometry_and_reset_scopes():
    """개별 축 변경과 축 초기화는 한 형상만, 공통 초기화는 이동·회전·배율만 바꿉니다."""
    from test_geometry_axes_ui import _fixture, _open
    from tools.align.operators import axis_settings as ui

    with _fixture() as (runtime, context):
        dialog = _open("moving", context)
        main = runtime.axis_settings.main_transform
        dialog.companion_preset = "MAYA"
        ui._update_companion_preset(dialog, context)
        assert runtime.axis_settings.main_transform is main
        assert runtime.axis_settings.companion_transform.preset_id == "MAYA"
        companion = runtime.axis_settings.companion_transform
        dialog.main_preset = "REALITY_CAPTURE"
        ui._update_main_preset(dialog, context)
        np.testing.assert_array_equal(runtime.axis_settings.companion_transform.matrix_float64, companion.matrix_float64)
        dialog.dataset_rotation, dialog.dataset_translation, dialog.dataset_scale = (17, 29, 43), (1, 2, 3), (2, 3, 4)
        ui._update_preview(dialog, context)
        reset = SimpleNamespace(edit_token=dialog.edit_token, component="COMPANION", report=dialog.report)
        assert ui.COLMAPALIGN_OT_reset_geometry_axes.execute(reset, context) == {"FINISHED"}
        assert dialog.dataset_rotation == (17, 29, 43)
        assert dialog.dataset_translation == (1, 2, 3)
        assert dialog.dataset_scale == (2, 3, 4)
        assert dialog.main_preset == "REALITY_CAPTURE" and dialog.companion_preset == "REALITY_CAPTURE_OBJ"
        assert ui.COLMAPALIGN_OT_reset_dataset_axis_transform.execute(reset, context) == {"FINISHED"}
        assert dialog.dataset_rotation == dialog.dataset_translation == (0, 0, 0)
        assert dialog.dataset_scale == (1, 1, 1)
        assert dialog.main_preset == "REALITY_CAPTURE" and dialog.companion_preset == "REALITY_CAPTURE_OBJ"
        dialog.cancel(context)


def test_popup_shows_existing_geometries_without_changing_saved_visibility():
    """전체 숨김과 다른 P 선택에서도 팝업 대상의 두 형상을 표시하고 종료 시 정확히 복원합니다."""
    from test_geometry_axes_ui import _fixture, _open
    from tools.align.operators import axis_settings as ui
    from tools.align.viewport import point_data, renderer

    for finish in ("CANCEL", "APPLY", "WORKSPACE"):
        with _fixture() as (runtime, context):
            catalog = runtime.model_catalog
            catalog.set_geometry_visibility("moving", show_surface=True)
            catalog.set_model_visibility("moving", False)
            before = catalog.snapshot()
            dialog = _open("moving", context)
            assert point_data.model_geometry_is_visible("moving", "COLMAP_POINT_CLOUD")
            assert point_data.model_geometry_is_visible("moving", "TRIANGLE_MESH")
            assert renderer.render_model_ids_for_role("MOVING") == ("moving",)
            dialog.preview_enabled = False
            ui._update_preview(dialog, context)
            assert point_data.model_geometry_is_visible("moving", "COLMAP_POINT_CLOUD")
            assert catalog.snapshot() == before
            if finish == "APPLY":
                assert ui.COLMAPALIGN_OT_edit_geometry_axes.execute(dialog, context) == {"FINISHED"}
            elif finish == "WORKSPACE":
                window = SimpleNamespace(as_pointer=lambda: 101, workspace=SimpleNamespace(as_pointer=lambda: 999))
                with patch.object(ui, "bpy", SimpleNamespace(context=SimpleNamespace(window_manager=SimpleNamespace(windows=[window])))):
                    assert ui._watch_axis_edit_dialogs() is None
            else:
                dialog.cancel(context)
            assert catalog.snapshot() == before
            assert not point_data.model_geometry_is_visible("moving", "COLMAP_POINT_CLOUD")
            assert not point_data.model_geometry_is_visible("moving", "TRIANGLE_MESH")
    with _fixture() as (runtime, context):
        selected = runtime.model_catalog.selected_model_ids
        dialog = _open("standalone", context)
        assert renderer.render_model_ids_for_role("MOVING") == ("standalone",)
        assert point_data.model_geometry_is_visible("standalone", "TRIANGLE_MESH")
        assert not point_data.model_geometry_is_visible("standalone", "COLMAP_POINT_CLOUD")
        dialog.cancel(context)
        assert runtime.model_catalog.selected_model_ids == selected
        runtime.model_repository.companion_meshes.pop("moving")
        dialog = _open("moving", context)
        assert point_data.model_geometry_is_visible("moving", "COLMAP_POINT_CLOUD")
        assert not point_data.model_geometry_is_visible("moving", "TRIANGLE_MESH")
        dialog.cancel(context)


def test_layout_contains_only_native_axes_and_one_shared_transform():
    """각 형상 박스에는 원본 축만 있고 공통 변환 입력은 연결 유무와 무관하게 한 벌입니다."""
    from test_geometry_axes_ui import _fixture, _open
    from tools.align.operators import axis_settings as ui

    with _fixture() as (runtime, context):
        runtime.model_repository.companion_meshes.pop("reference")
        for model_id, count in (("moving", 2), ("reference", 1), ("standalone", 1)):
            dialog = _open(model_id, context)
            ui.COLMAPALIGN_OT_edit_geometry_axes.draw(dialog, context)
            assert len(dialog.layout.children) == count + 1
            for child in dialog.layout.children[:count]:
                names = [entry[1] for entry in child.all_entries() if entry[0] == "prop"]
                assert len(names) == 3 and all(name.endswith(("preset", "forward", "up")) for name in names)
            names = [entry[1] for entry in dialog.layout.all_entries() if entry[0] == "prop"]
            for prop in ("dataset_rotation", "dataset_translation", "dataset_scale"):
                assert names.count(prop) == 3
            assert not any(name in names for name in ("main_rotation", "companion_rotation", "main_scale", "companion_scale"))
            dialog.cancel(context)


def test_commit_undo_redo_persistence_and_noop_reopen():
    """통합 적용이 단일 이력으로 복원되고 저장 왕복과 무변경 재편집도 정확한 값을 유지합니다."""
    from test_geometry_axes_ui import _fixture, _open
    from tools.align.operators import axis_settings as ui
    from tools.align import session
    from tools.align.project_persistence.runtime import _project_axes_snapshot, _display_transform_from_axes
    from tools.align.project_persistence.codec import _axes_mapping, _decode_axes

    with _fixture() as (runtime, context):
        repository = runtime.model_repository
        before = repository.display_transform_for_model_id("moving"), repository.companion_display_transform_for_model_id("moving")
        dialog = _open("moving", context)
        dialog.dataset_rotation, dialog.dataset_scale = (11, 22, 33), (2, 3, 4)
        assert ui.COLMAPALIGN_OT_edit_geometry_axes.execute(dialog, context) == {"FINISHED"}
        assert runtime.undo_journal.undo_count == 1
        after = repository.display_transform_for_model_id("moving"), repository.companion_display_transform_for_model_id("moving")
        _assert_shared_delta(*before, *after)
        for transform in after:
            restored = _display_transform_from_axes(_decode_axes(_axes_mapping(_project_axes_snapshot(transform)), "axes"))
            assert restored.fingerprint == transform.fingerprint
            np.testing.assert_array_equal(restored.matrix_float64, transform.matrix_float64)
        with patch.object(session, "RUNTIME", runtime), patch.object(session, "request_redraw"), patch.object(session, "set_workspace_status_text"):
            assert session.undo_session_state()
            assert repository.display_transform_for_model_id("moving") is before[0]
            assert repository.companion_display_transform_for_model_id("moving") is before[1]
            assert session.redo_session_state()
        dialog = _open("moving", context)
        assert ui.COLMAPALIGN_OT_edit_geometry_axes.execute(dialog, context) == {"FINISHED"}
        assert runtime.undo_journal.undo_count == 1
        assert repository.companion_display_transform_for_model_id("moving") is after[1]
        dialog = _open("moving", context)
        dialog.dataset_rotation = (-19, 27, 73)
        ui._update_preview(dialog, context)
        _assert_shared_delta(*after, runtime.axis_settings.main_transform, runtime.axis_settings.companion_transform)
        dialog.cancel(context)


def test_real_rna_shared_scale_lock_and_individual_axes():
    """실제 연산자 속성 래퍼에서도 공통 비율 잠금과 개별 축 콜백이 정확히 동작합니다."""
    from test_geometry_axes_ui import _fixture
    from tools.align.operators import axis_settings as ui

    with _fixture() as (runtime, _context):
        for cls in ui.CLASSES:
            bpy.utils.register_class(cls)
        try:
            properties = bpy.context.window_manager.operator_properties_last("colmap_align.edit_geometry_axes")
            properties.suspend_updates = True
            properties.model_id, properties.edit_token = "moving", ui.begin_axis_edit(runtime, "moving")
            properties.has_companion, properties.preview_enabled = True, True
            ui._set_dialog_values(properties, runtime.axis_settings.main_transform, runtime.axis_settings.companion_transform)
            properties.suspend_updates = False
            before = runtime.axis_settings.main_transform, runtime.axis_settings.companion_transform
            properties.dataset_scale = (2, 3, 4)
            properties.dataset_scale_locked = True
            properties.dataset_scale[1] = 6
            np.testing.assert_allclose(properties.dataset_scale, (4, 6, 8))
            properties.dataset_rotation = (12, 24, 36)
            assert not properties.error_message
            _assert_shared_delta(*before, runtime.axis_settings.main_transform, runtime.axis_settings.companion_transform)
            main = runtime.axis_settings.main_transform
            properties.companion_preset = "MAYA"
            np.testing.assert_array_equal(runtime.axis_settings.main_transform.matrix_float64, main.matrix_float64)
            assert runtime.axis_settings.companion_transform.preset_id == "MAYA"
            assert "main_rotation" not in properties.bl_rna.properties
        finally:
            ui.cancel_axis_edit(runtime)
            ui.clear_axis_edit_dialogs()
            for cls in reversed(ui.CLASSES):
                bpy.utils.unregister_class(cls)


def test_full_project_restore_and_legacy_basis_validation():
    """원본 기준의 실제 프로젝트 저장·복원과 구버전 지문 보존 및 잘못된 기준 거부를 검증합니다."""
    import test_geometry_axes_persistence as regression
    from test_geometry_axes_runtime import _expect_error
    from tools.align.axis_edit_transform import prepare_dataset_axis_edit, compose_dataset_geometry_transform
    from tools.align.display_axes import create_model_display_transform
    from tools.align.project_persistence.runtime import _project_axes_snapshot, _display_transform_from_axes
    from tools.align.project_persistence.codec import _axes_mapping, _decode_axes

    def set_transforms(runtime):
        """실제 저장 시험에 개별 RC 축과 공통 비균일 변환을 가진 두 형상을 구성합니다."""
        repository = runtime.model_repository
        main, companion = repository.display_transform_for_model_id("owner"), repository.companion_display_transform_for_model_id("owner")
        basis = prepare_dataset_axis_edit(runtime, "owner", main, companion)
        common = create_model_display_transform("BLENDER", "Y", "Z", rotation_xyz_degrees=(11, 22, 33),
            translation_xyz=(-2, -3, -4), scale_xyz=(2, 3, 4), pivot_source=basis.common.pivot_source)
        values = []
        for original, correction, setter in ((main, basis.main_basis, repository.set_display_transform),
                                             (companion, basis.companion_basis, repository.set_companion_display_transform)):
            value = compose_dataset_geometry_transform(common, original.preset_id, original.forward_axis, original.up_axis, correction)
            setter("owner", value)
            values.append(value)
        return tuple(values)

    with patch.object(regression, "_set_axes", set_transforms):
        regression.test_independent_axes_roundtrip_preserves_source_and_prepares_final_axes()
    for basis in (np.zeros((4, 4)), np.eye(3), np.full((4, 4), np.nan), np.diag((1, 1, 1, 2))):
        _expect_error(lambda: create_model_display_transform("BLENDER", "Y", "Z", source_basis=basis))
    basis = np.eye(4)
    basis[0, 1], basis[1, 3] = 0.75, 7.25
    value = create_model_display_transform("BLENDER", "Y", "Z", source_basis=basis)
    _expect_error(lambda: _axes_mapping(_project_axes_snapshot(value), 9))
    original = create_model_display_transform("CUSTOM", "X", "Z", rotation_xyz_degrees=(7, 12, 31))
    for version in (5, 6, 7, 8, 9):
        restored = _display_transform_from_axes(_decode_axes(_axes_mapping(_project_axes_snapshot(original), version), "axes", version))
        assert restored.fingerprint == original.fingerprint


def run_all():
    """개별 축·공통 변환·동시 표시와 저장·RNA 검사를 실행합니다."""
    _bootstrap()
    for name, function in tuple(globals().items()):
        if name.startswith("test_") and callable(function):
            function()
    print("test_geometry_axes_split: 통과")


if __name__ == "__main__":
    run_all()
