"""사용자 Blender 설치본만 로드해 COLMAP Align GUI 생명주기를 검증합니다."""

from __future__ import annotations

import importlib
import inspect
import os
from pathlib import Path
import sys
import traceback

import addon_utils
import bpy


_OPEN_STARTED = False
_CANCEL_REQUESTED = False
_EXIT_REQUESTED = False
_SOURCE_WORKSPACE_POINTER = 0
_SCENE_DATA_SNAPSHOT = None


def _assert_no_legacy_scene_data() -> None:
    """설치본 등록·진입·종료 중 legacy Object·Collection이 없는지 확인합니다."""
    from tools.align.constants import LEGACY_DEMO_COLLECTION_NAMES, LEGACY_DEMO_OBJECT_NAMES

    assert all(bpy.data.collections.get(name) is None for name in LEGACY_DEMO_COLLECTION_NAMES)
    assert all(bpy.data.objects.get(name) is None for name in LEGACY_DEMO_OBJECT_NAMES)


def _scene_data_snapshot() -> tuple[frozenset[tuple[str, int]], frozenset[tuple[str, int]]]:
    """현재 Scene Object·Collection의 이름과 포인터를 순서와 무관하게 기록합니다."""
    return (
        frozenset((object_data.name, object_data.as_pointer()) for object_data in bpy.data.objects),
        frozenset((collection.name, collection.as_pointer()) for collection in bpy.data.collections),
    )


def _assert_scene_data_snapshot_unchanged() -> None:
    """OPEN 직전 스냅샷과 비교해 설치본이 Scene 데이터를 변경하지 않았는지 확인합니다."""
    assert _SCENE_DATA_SNAPSHOT is not None, "OPEN 직전 Scene 데이터 스냅샷이 필요합니다."
    assert _scene_data_snapshot() == _SCENE_DATA_SNAPSHOT


def _enable_installed_addon() -> None:
    """factory startup에서도 사용자 scripts/addons의 설치본만 일시 활성화합니다."""
    addon_path = Path(bpy.utils.user_resource("SCRIPTS", path="addons")).resolve()
    assert addon_path.is_dir(), f"사용자 애드온 경로를 찾을 수 없습니다: {addon_path}"
    addon_text = str(addon_path)
    if addon_text not in sys.path:
        sys.path.insert(0, addon_text)
    _loaded, enabled = addon_utils.check("colmap_align")
    if not enabled:
        addon_utils.enable("colmap_align", default_set=False, persistent=False)


def _finish(succeeded: bool) -> None:
    """설치본을 해제하고 GUI 검증 결과를 출력한 뒤 Blender를 종료합니다."""
    try:
        _loaded, enabled = addon_utils.check("colmap_align")
        if enabled:
            addon_utils.disable("colmap_align", default_set=False)
    except Exception:
        traceback.print_exc()
        succeeded = False
    print(f"INSTALLED_GUI_TEST_RESULT={'PASS' if succeeded else 'FAIL'}")
    bpy.ops.wm.quit_blender()


def _installed_module_path() -> Path:
    """활성화한 사용자 설치본 모듈의 실제 파일 경로를 반환합니다."""
    module_path = getattr(importlib.import_module("colmap_align"), "__file__", "")
    assert module_path, "설치된 colmap_align 모듈을 찾지 못했습니다."
    return Path(module_path).resolve()


def _verify_installation() -> None:
    """설치본의 내장 Small GICP·UI·오퍼레이터·RGB 셰이더와 기본값을 확인합니다."""
    module_path = _installed_module_path()
    module_root = module_path.parent
    normalized_path = str(module_path).replace("/", "\\").lower()
    assert "scripts\\addons\\colmap_align" in normalized_path
    expected_root = os.environ.get("COLMAP_ALIGN_EXPECTED_ADDON_ROOT", "")
    if expected_root:
        normalized_root = str(Path(expected_root).resolve()).replace("/", "\\").lower()
        assert normalized_path.startswith(normalized_root + "\\")
    _loaded, enabled = addon_utils.check("colmap_align")
    assert enabled is True
    assert hasattr(bpy.types, "COLMAPALIGN_PT_main")
    assert hasattr(bpy.types, "COLMAPALIGN_UL_marker_pairs")
    assert hasattr(bpy.types.Scene, "colmap_align_settings")
    assert hasattr(bpy.types.WindowManager, "colmap_align_marker_pairs")
    settings = bpy.context.scene.colmap_align_settings
    assert settings.use_colmap_rgb is True
    assert settings.import_dataset_root == ""
    assert settings.import_last_directory == ""
    assert settings.import_input_format == "AUTO"
    assert settings.import_display_point_limit == 250_000
    assert settings.import_axis_preset == "REALITY_CAPTURE"
    assert settings.import_axis_forward == "Z"
    assert settings.import_axis_up == "-Y"
    assert settings.icp_preset == "BALANCED"
    assert settings.icp_solver_backend == "SMALL_GICP"
    assert settings.icp_small_gicp_operation == "SMALL_GICP"
    assert settings.icp_scale_refine_enabled is False
    operation_property = settings.bl_rna.properties.get("icp_small_gicp_operation")
    assert operation_property is not None
    assert tuple(
        (item.identifier, item.name) for item in operation_property.enum_items
    ) == (("SMALL_GICP", "Small GICP"), ("SCALE_REFINE", "Refine"))
    backend_property = settings.bl_rna.properties.get("icp_solver_backend")
    assert backend_property is not None
    assert tuple(item.identifier for item in backend_property.enum_items) == ("SMALL_GICP",)
    from tools.align.distribution_profile import DISTRIBUTION_PROFILE
    from tools.align.alignment.icp_solver_backend import backend_algorithm_version

    assert DISTRIBUTION_PROFILE == "SMALL_GICP"
    assert backend_algorithm_version("SMALL_GICP") == "small-gicp-v6"
    from tools.align.alignment.icp_backend import validate_small_gicp_environment
    from tools.align.properties import (
        COLMAPALIGN_AP_preferences,
        bundled_small_gicp_python_path,
        configured_icp_python_path,
    )

    bundled_python = Path(bundled_small_gicp_python_path()).resolve()
    assert bundled_python == (module_root / "runtime" / "python" / "python.exe").resolve()
    assert Path(configured_icp_python_path()).resolve() == bundled_python
    assert bundled_python.is_file()
    assert not (bundled_python.parent / "pyvenv.cfg").exists()
    assert not (bundled_python.parent / "Lib" / "site-packages" / "open3d").exists()
    assert COLMAPALIGN_AP_preferences.bl_rna.properties.get("open3d_python_path") is None
    environment = validate_small_gicp_environment(str(bundled_python))
    assert environment["worker_python_version"] == "3.12.10"
    assert environment["worker_numpy_version"] == "1.26.4"
    assert environment["small_gicp_version"] == "1.0.1"
    assert not (module_root / "optional_backends").exists()
    for excluded_name in (
        "icp_solver.py",
        "icp_worker.py",
        "icp_open3d_solver.py",
        "icp_open3d_worker.py",
    ):
        assert not (module_root / "alignment" / excluded_name).exists()
    for required_name in (
        "icp_external_contract.py",
        "icp_scale_refine.py",
        "icp_small_gicp_solver.py",
        "icp_small_gicp_worker.py",
    ):
        assert (module_root / "alignment" / required_name).is_file()
    assert settings.bl_rna.properties.get("use_demo_color") is None
    display_limit_property = settings.bl_rna.properties.get("import_display_point_limit")
    assert display_limit_property is not None
    assert display_limit_property.default == 250_000
    assert settings.bl_rna.properties.get("reference_visible") is None
    assert settings.bl_rna.properties.get("moving_visible") is None
    _assert_no_legacy_scene_data()

    required_operators = {
        "add_marker_pair": "COLMAP_ALIGN_OT_add_marker_pair",
        "edit_marker_pair": "COLMAP_ALIGN_OT_edit_marker_pair",
        "delete_marker_pair": "COLMAP_ALIGN_OT_delete_marker_pair",
        "confirm_marker_selection": "COLMAP_ALIGN_OT_confirm_marker_selection",
        "cancel_marker_selection": "COLMAP_ALIGN_OT_cancel_marker_selection",
        "undo_marker_selection": "COLMAP_ALIGN_OT_undo_marker_selection",
        "redo_marker_selection": "COLMAP_ALIGN_OT_redo_marker_selection",
        "select_data_layer": "COLMAP_ALIGN_OT_select_data_layer",
        "toggle_data_layer_group": "COLMAP_ALIGN_OT_toggle_data_layer_group",
        "activate_data_layer": "COLMAP_ALIGN_OT_activate_data_layer",
        "toggle_data_layer_visibility": "COLMAP_ALIGN_OT_toggle_data_layer_visibility",
        "delete_selected_model": "COLMAP_ALIGN_OT_delete_selected_model",
        "set_layer_transform_pivot": "COLMAP_ALIGN_OT_set_layer_transform_pivot",
        "import_model": "COLMAP_ALIGN_OT_import_model",
        "import_model_job": "COLMAP_ALIGN_OT_import_model_job",
        "cancel_import_model": "COLMAP_ALIGN_OT_cancel_import_model",
        "toggle_marker_preview": "COLMAP_ALIGN_OT_toggle_marker_preview",
        "align_marker_preview": "COLMAP_ALIGN_OT_align_marker_preview",
        "cancel_icp": "COLMAP_ALIGN_OT_cancel_icp",
        "discard_icp_preview": "COLMAP_ALIGN_OT_discard_icp_preview",
        "approve_icp_result": "COLMAP_ALIGN_OT_approve_icp_result",
        "exit_session": "COLMAP_ALIGN_OT_exit_session",
        "session_input_guard": "COLMAP_ALIGN_OT_session_input_guard",
    }
    for operator_name, identifier in required_operators.items():
        operator_type = getattr(bpy.ops.colmap_align, operator_name).get_rna_type()
        assert operator_type.identifier == identifier

    from tools.align.operators.workspace import COLMAPALIGN_OT_restore_workspace
    from tools.align.operators.import_model import COLMAPALIGN_OT_import_model
    from tools.align.ui.panels import (
        COLMAPALIGN_UL_marker_align_log,
        COLMAPALIGN_PT_main,
        COLMAPALIGN_PT_marker,
        COLMAPALIGN_PT_data_layers,
        COLMAPALIGN_PT_viewport,
    )

    assert "INTERNAL" in COLMAPALIGN_OT_restore_workspace.bl_options
    import_operator_source = inspect.getsource(COLMAPALIGN_OT_import_model)
    assert "display_point_limit" in import_operator_source
    assert "Point Limit" in import_operator_source
    draw_source = inspect.getsource(COLMAPALIGN_PT_main.draw)
    assert "colmap_align.exit_session" not in draw_source
    assert "restore_workspace" not in draw_source
    assert "RUNTIME.undo_journal.last_action" not in draw_source
    assert "RUNTIME.session.message" not in draw_source
    assert "탭/API 전환은 감지 후 이 작업공간으로 복귀합니다." not in draw_source
    layers_draw_source = inspect.getsource(COLMAPALIGN_PT_data_layers.draw)
    for text in (
        "A 고정",
        "P 이동",
        "현재 P로 선택",
        "P 대기",
        "승인 대기",
        "RADIOBUT_ON",
        "RADIOBUT_OFF",
    ):
        assert text not in layers_draw_source
    assert "active_models" in layers_draw_source
    assert "inactive_models" in layers_draw_source
    assert "PINNED" in layers_draw_source
    assert "toggle_data_layer_visibility" in layers_draw_source
    assert "HIDE_ON" in layers_draw_source
    assert "HIDE_OFF" in layers_draw_source
    assert "BLANK1" in layers_draw_source
    assert "layout.box()" not in layers_draw_source
    assert "depress=model.is_visible" not in layers_draw_source
    viewport_draw_source = inspect.getsource(COLMAPALIGN_PT_viewport.draw)
    assert "point_size_row.use_property_split = False" in viewport_draw_source
    assert 'point_size_row.alignment = "EXPAND"' in viewport_draw_source
    assert 'gizmo_row.label(text="", icon="BLANK1")' in viewport_draw_source
    assert 'color_row.label(text="", icon="BLANK1")' in viewport_draw_source
    assert 'gizmo_row.prop(settings, "show_transform_gizmo")' in viewport_draw_source
    assert 'color_row.prop(settings, "use_colmap_rgb")' in viewport_draw_source
    marker_draw_source = inspect.getsource(COLMAPALIGN_PT_marker.draw)
    assert "alignment_placeholder" not in marker_draw_source
    assert "Reference Group" not in marker_draw_source
    assert "Moving Model" not in marker_draw_source
    assert 'preview_box.label(text="Marker Align", icon="ORIENTATION_GIMBAL")' in marker_draw_source
    assert "marker_action_row = preview_box.row()" in marker_draw_source
    assert 'text="Marker Refine Scale"' in marker_draw_source
    assert "marker_scale_action.use_property_split = False" in marker_draw_source
    assert 'marker_scale_action.alignment = "LEFT"' in marker_draw_source
    assert "toggle=True" not in marker_draw_source
    assert "align_action = preview_box.row()" in marker_draw_source
    assert "COLMAPALIGN_UL_marker_align_log" in marker_draw_source
    assert "cancel_icp" in marker_draw_source
    assert "discard_icp_preview" in marker_draw_source
    assert "approve_icp_result" in marker_draw_source
    assert "icp_preset" not in marker_draw_source
    assert "icp_scale_refine_enabled" not in marker_draw_source
    assert "icp_small_gicp_operation" not in marker_draw_source
    assert "settings_column" not in marker_draw_source
    import colmap_align.operators.marker_alignment as marker_alignment_module

    assert marker_alignment_module._AUTOMATIC_ICP_PRESET == "PRECISE"
    assert marker_alignment_module._AUTOMATIC_ICP_BACKEND == "SMALL_GICP"
    align_execute_source = inspect.getsource(
        marker_alignment_module.COLMAPALIGN_OT_align_marker_preview.execute
    )
    assert "preset=_AUTOMATIC_ICP_PRESET" in align_execute_source
    assert "scale_refine_enabled=True" in align_execute_source
    assert "solver_backend=_AUTOMATIC_ICP_BACKEND" in align_execute_source
    assert 'preview_box.label(text="상태 로그")' in marker_draw_source
    assert 'preview_box.label(text="상태 로그", icon=' not in marker_draw_source
    log_draw_source = inspect.getsource(COLMAPALIGN_UL_marker_align_log.draw_item)
    assert "layout.scale_y = 0.8" in log_draw_source
    assert "icon=" not in log_draw_source
    assert getattr(bpy.types, "COLMAPALIGN_UL_marker_align_log", None) is COLMAPALIGN_UL_marker_align_log
    assert hasattr(bpy.types.WindowManager, "colmap_align_marker_align_log")

    from tools.align.viewport.renderer import (
        _POINT_FRAGMENT_SHADER_SOURCE,
        _point_shader,
    )

    assert "vertex_color.rgb * identifier_color.rgb" in _POINT_FRAGMENT_SHADER_SOURCE
    assert "mix(tinted_color, vertex_color, use_colmap_rgb)" in _POINT_FRAGMENT_SHADER_SOURCE
    assert _point_shader() is not None


def _run_step() -> float | None:
    """설치본의 단일 창 진입·드래프트 취소·종료를 비동기로 검증합니다."""
    global _OPEN_STARTED, _CANCEL_REQUESTED, _EXIT_REQUESTED, _SCENE_DATA_SNAPSHOT
    try:
        _verify_installation()
        from tools.align.constants import (
            CONTROL_PANEL_CATEGORY,
            DEMO_MODEL_1_ID,
            DEMO_MODEL_2_ID,
            DEMO_MODEL_3_ID,
            DEMO_MODEL_4_ID,
            MODEL_STATUS_REGISTERED,
            ROLE_MOVING,
            ROLE_REFERENCE,
            VIEW_PRESET_VERSION,
            VIEW_PRESET_VERSION_KEY,
        )
        from tools.align.operators.workspace import (
            _process_sidebar_activation,
            find_main_window,
        )
        from tools.align.session import redo_session_state, undo_session_state
        from tools.align.state import RUNTIME
        from tools.align.ui.panels import COLMAPALIGN_PT_main
        from tools.align.viewport.registry import classify_areas, find_window_region, role_for_area
        from tools.align.viewport.renderer import (
            _active_role,
            point_cloud_draw_passes_for_role,
            render_model_ids_for_role,
        )

        if not _OPEN_STARTED:
            _SCENE_DATA_SNAPSHOT = _scene_data_snapshot()
            result = bpy.ops.colmap_align.open_workspace()
            assert "FINISHED" in result
            _OPEN_STARTED = True
            return 0.1
        if RUNTIME.workspace_setup_task is not None:
            return 0.1
        if bpy.app.timers.is_registered(_process_sidebar_activation):
            return 0.1
        if RUNTIME.workspace_setup_error:
            raise AssertionError(RUNTIME.workspace_setup_error)

        if _CANCEL_REQUESTED:
            if RUNTIME.modal_active:
                return 0.1
            main_window = find_main_window(bpy.context.window_manager)
            assert main_window is not None
            _reference_area, moving_area = classify_areas(main_window.screen.areas)
            with bpy.context.temp_override(
                window=main_window,
                screen=main_window.screen,
                area=moving_area,
                region=find_window_region(moving_area),
            ):
                result = bpy.ops.colmap_align.exit_session()
            assert "FINISHED" in result
            _CANCEL_REQUESTED = False
            _EXIT_REQUESTED = True
            return 0.1

        if _EXIT_REQUESTED:
            if RUNTIME.session.phase != "INACTIVE":
                return 0.1
            window = bpy.context.window_manager.windows[0]
            assert window.workspace.as_pointer() == _SOURCE_WORKSPACE_POINTER
            assert RUNTIME.session_active is False
            assert RUNTIME.undo_journal.undo_count == 0
            assert RUNTIME.undo_journal.redo_count == 0
            assert RUNTIME.modal_active is False
            assert RUNTIME.session.input_guard_active is False
            _assert_no_legacy_scene_data()
            _assert_scene_data_snapshot_unchanged()
            _finish(True)
            return None

        window_manager = bpy.context.window_manager
        main_window = find_main_window(window_manager)
        assert main_window is not None
        assert RUNTIME.session_active is True
        assert RUNTIME.session.input_guard_active is True
        assert RUNTIME.model_catalog.model_count == 4
        assert tuple(model.model_id for model in RUNTIME.model_catalog.active_models()) == (
            DEMO_MODEL_1_ID,
            DEMO_MODEL_2_ID,
        )
        assert tuple(model.model_id for model in RUNTIME.model_catalog.inactive_models()) == (
            DEMO_MODEL_3_ID,
            DEMO_MODEL_4_ID,
        )
        assert RUNTIME.model_catalog.selected_model is None
        assert set(RUNTIME.point_clouds) == {
            DEMO_MODEL_1_ID,
            DEMO_MODEL_2_ID,
            DEMO_MODEL_3_ID,
            DEMO_MODEL_4_ID,
        }
        _assert_scene_data_snapshot_unchanged()
        reference_area, moving_area = classify_areas(main_window.screen.areas)
        assert role_for_area(main_window, reference_area) == ROLE_REFERENCE
        assert role_for_area(main_window, moving_area) == ROLE_MOVING
        assert moving_area.spaces.active.show_region_ui
        with bpy.context.temp_override(
            window=main_window,
            screen=main_window.screen,
            area=reference_area,
            region=find_window_region(reference_area),
        ):
            assert _active_role() == ROLE_REFERENCE
            assert render_model_ids_for_role(ROLE_REFERENCE) == (
                DEMO_MODEL_1_ID,
                DEMO_MODEL_2_ID,
            )
        with bpy.context.temp_override(
            window=main_window,
            screen=main_window.screen,
            area=moving_area,
            region=find_window_region(moving_area),
        ):
            assert _active_role() == ROLE_MOVING
            assert render_model_ids_for_role(ROLE_MOVING) == ()
        _assert_no_legacy_scene_data()
        assert main_window.screen.get(VIEW_PRESET_VERSION_KEY) == VIEW_PRESET_VERSION
        ui_region = next(region for region in moving_area.regions if region.type == "UI")
        if not ui_region.is_property_readonly("active_panel_category"):
            assert ui_region.active_panel_category == CONTROL_PANEL_CATEGORY
        with bpy.context.temp_override(
            window=main_window,
            screen=main_window.screen,
            area=reference_area,
            region=find_window_region(reference_area),
        ):
            assert COLMAPALIGN_PT_main.poll(bpy.context) is False
        with bpy.context.temp_override(
            window=main_window,
            screen=main_window.screen,
            area=moving_area,
            region=find_window_region(moving_area),
        ):
            assert COLMAPALIGN_PT_main.poll(bpy.context) is True
            assert "FINISHED" in bpy.ops.colmap_align.select_data_layer(model_id=DEMO_MODEL_3_ID)
            assert RUNTIME.model_catalog.selected_model_id == DEMO_MODEL_3_ID
            assert RUNTIME.model_catalog.selected_inactive_model_id == DEMO_MODEL_3_ID
            assert render_model_ids_for_role(ROLE_MOVING) == (DEMO_MODEL_3_ID,)
            assert "FINISHED" in bpy.ops.colmap_align.select_data_layer(
                model_id=DEMO_MODEL_2_ID,
                extend_selection=True,
            )
            assert RUNTIME.model_catalog.selected_reference_model_id == DEMO_MODEL_2_ID
            assert RUNTIME.model_catalog.selected_inactive_model_id == DEMO_MODEL_3_ID
            for role in (ROLE_REFERENCE, ROLE_MOVING):
                passes = point_cloud_draw_passes_for_role(role)
                assert tuple(draw_pass.role for draw_pass in passes) == (
                    ROLE_REFERENCE,
                    ROLE_MOVING,
                )
                assert all(draw_pass.use_colmap_rgb is False for draw_pass in passes)
            assert "FINISHED" in bpy.ops.colmap_align.select_data_layer(model_id=DEMO_MODEL_3_ID)
            assert RUNTIME.model_catalog.selected_reference_model_id is None
            assert RUNTIME.model_catalog.selected_moving_model_id == DEMO_MODEL_3_ID
            assert RUNTIME.model_catalog.selected_inactive_model_id == DEMO_MODEL_3_ID
            assert "FINISHED" in bpy.ops.colmap_align.toggle_data_layer_group(model_id=DEMO_MODEL_3_ID)
            assert tuple(model.model_id for model in RUNTIME.model_catalog.active_models()) == (
                DEMO_MODEL_1_ID,
                DEMO_MODEL_2_ID,
                DEMO_MODEL_3_ID,
            )
            assert RUNTIME.model_catalog.selected_model_id == DEMO_MODEL_3_ID
            assert RUNTIME.model_catalog.selected_inactive_model is None
            assert undo_session_state() is True
            assert tuple(model.model_id for model in RUNTIME.model_catalog.active_models()) == (
                DEMO_MODEL_1_ID,
                DEMO_MODEL_2_ID,
            )
            assert RUNTIME.model_catalog.selected_inactive_model_id == DEMO_MODEL_3_ID
            assert redo_session_state() is True
            assert RUNTIME.model_catalog.models[DEMO_MODEL_3_ID].registration_status == MODEL_STATUS_REGISTERED
            assert "FINISHED" in bpy.ops.colmap_align.toggle_data_layer_group(model_id=DEMO_MODEL_3_ID)
            assert RUNTIME.model_catalog.selected_inactive_model_id == DEMO_MODEL_3_ID
            assert "FINISHED" in bpy.ops.colmap_align.toggle_data_layer_visibility(model_id=DEMO_MODEL_3_ID)
            assert render_model_ids_for_role(ROLE_MOVING) == ()
            assert "FINISHED" in bpy.ops.colmap_align.toggle_data_layer_visibility(model_id=DEMO_MODEL_3_ID)
            assert render_model_ids_for_role(ROLE_MOVING) == (DEMO_MODEL_3_ID,)
            assert bpy.ops.colmap_align.add_marker_pair.poll() is True
            result = bpy.ops.colmap_align.add_marker_pair("INVOKE_DEFAULT")
        assert "FINISHED" in result
        assert RUNTIME.modal_active is True
        assert RUNTIME.marker_selection.has_draft is True
        with bpy.context.temp_override(
            window=main_window,
            screen=main_window.screen,
            area=moving_area,
            region=find_window_region(moving_area),
        ):
            result = bpy.ops.colmap_align.cancel_marker_selection()
        assert "FINISHED" in result
        _CANCEL_REQUESTED = True
        return 0.1
    except Exception:
        traceback.print_exc()
        _finish(False)
    return None


def schedule_test() -> None:
    """GUI Blender에서 설치본 활성화 뒤 테스트 타이머를 예약합니다."""
    global _SOURCE_WORKSPACE_POINTER
    if bpy.app.background:
        raise RuntimeError("설치본 검증은 Blender GUI 컨텍스트가 필요합니다.")
    _enable_installed_addon()
    from tools.align.operators.demo_data import install_demo_fixture

    install_demo_fixture()
    _assert_no_legacy_scene_data()
    _SOURCE_WORKSPACE_POINTER = bpy.context.window.workspace.as_pointer()
    bpy.app.timers.register(_run_step, first_interval=0.3)


if __name__ == "__main__":
    try:
        schedule_test()
    except Exception:
        traceback.print_exc()
        _finish(False)
