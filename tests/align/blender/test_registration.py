"""애드온 등록과 데이터블록 없는 오른쪽 P 뷰 N 패널 구성을 검증합니다."""

from __future__ import annotations

import inspect
import sys
from pathlib import Path
from types import SimpleNamespace

import bpy


def _add_source_path() -> None:
    """애드온 소스 디렉터리를 테스트 모듈 탐색 경로에 추가합니다."""
    source_path = Path(__file__).resolve().parents[3]
    source_text = str(source_path)
    if source_text not in sys.path:
        sys.path.insert(0, source_text)


def _get_panel_classes() -> tuple[type[bpy.types.Panel], ...]:
    """COLMAP Align 패널 모듈이 공개한 등록 대상 패널 클래스를 반환합니다."""
    from tools.align.ui import panels

    panel_classes = tuple(panels.PANEL_CLASSES)
    assert panel_classes, "COLMAP Align 패널 클래스가 비어 있습니다."
    return panel_classes


def _is_registered(panel_class: type[bpy.types.Panel]) -> bool:
    """패널 클래스가 현재 Blender 타입으로 등록됐는지 확인합니다."""
    return getattr(bpy.types, panel_class.__name__, None) is panel_class


class _RecordingLayout:
    """메뉴 callback이 생성한 UI 항목을 Blender 창 없이 기록합니다."""

    def __init__(self) -> None:
        """메뉴 호출 기록을 빈 목록으로 초기화합니다."""

        self.calls: list[tuple[str, tuple[object, ...], dict[str, object]]] = []

    def separator(self) -> None:
        """구분선 생성을 호출 기록에 추가합니다."""

        self.calls.append(("separator", (), {}))

    def menu(self, *args, **kwargs) -> None:
        """하위 메뉴 생성을 호출 기록에 추가합니다."""

        self.calls.append(("menu", args, kwargs))

    def operator(self, *args, **kwargs) -> None:
        """Operator 버튼 생성을 호출 기록에 추가합니다."""

        self.calls.append(("operator", args, kwargs))


def _legacy_data_is_absent() -> bool:
    """이전 버전의 정확한 Object·Collection 이름이 현재 Blender 데이터에 없는지 확인합니다."""
    from tools.align.constants import LEGACY_DEMO_COLLECTION_NAMES, LEGACY_DEMO_OBJECT_NAMES

    return all(
        bpy.data.collections.get(name) is None for name in LEGACY_DEMO_COLLECTION_NAMES
    ) and all(bpy.data.objects.get(name) is None for name in LEGACY_DEMO_OBJECT_NAMES)


def _create_legacy_owned_data() -> None:
    """등록과 파일 로드 마이그레이션 훅 검사용 이전 버전 데이터를 만듭니다."""
    from tools.align.constants import (
        LEGACY_DEMO_COLLECTION_NAMES,
        LEGACY_DEMO_OBJECT_NAMES,
        LEGACY_OWNED_DATA_KEY,
    )

    assert _legacy_data_is_absent()
    for name in LEGACY_DEMO_COLLECTION_NAMES:
        collection = bpy.data.collections.new(name)
        collection[LEGACY_OWNED_DATA_KEY] = True
        assert collection.get(LEGACY_OWNED_DATA_KEY) is True
    for name in LEGACY_DEMO_OBJECT_NAMES:
        object_data = bpy.data.objects.new(name, None)
        object_data[LEGACY_OWNED_DATA_KEY] = True
        assert object_data.get(LEGACY_OWNED_DATA_KEY) is True


def test_register_unregister_lifecycle() -> None:
    """등록·해제가 Scene Object·Collection을 만들지 않고 멱등적인지 확인합니다."""
    from tools.align import registration
    from tools.align.state import RUNTIME
    from tools.align.operators.view_framing import COLMAPALIGN_GGT_frame_all

    panel_classes = _get_panel_classes()
    registration.unregister()
    assert not any(_is_registered(panel_class) for panel_class in panel_classes)

    registration.register()
    assert all(_is_registered(panel_class) for panel_class in panel_classes)
    assert COLMAPALIGN_GGT_frame_all in registration.CLASSES
    assert registration._VIEW_MENU_REGISTERED is True
    addon_keyconfig = getattr(bpy.context.window_manager.keyconfigs, "addon", None)
    assert len(registration._ADDON_KEYMAPS) == (1 if addon_keyconfig is not None else 0)
    if registration._ADDON_KEYMAPS:
        _keymap, keymap_item = registration._ADDON_KEYMAPS[0]
        assert keymap_item.idname == "colmap_align.set_layer_transform_pivot"
        assert keymap_item.type == "LEFTMOUSE"
        assert keymap_item.value == "PRESS"
    assert len(RUNTIME.draw_handlers) == 2
    assert _legacy_data_is_absent()

    registration.register()
    assert all(_is_registered(panel_class) for panel_class in panel_classes)
    assert len(RUNTIME.draw_handlers) == 2

    registration.unregister()
    assert not any(_is_registered(panel_class) for panel_class in panel_classes)
    assert registration._VIEW_MENU_REGISTERED is False
    assert registration._ADDON_KEYMAPS == []
    assert RUNTIME.draw_handlers == []
    assert RUNTIME.viewport_registry.is_complete() is False
    assert _legacy_data_is_absent()


def test_window_entry_is_nested_under_colmap_tools() -> None:
    """최상위 Align 버튼 없이 COLMAP Tools 하위에 진입점이 배치되는지 확인합니다."""

    from tools.align import registration
    from shell.menu import COLMAP_MT_tools, draw_window_menu

    registration.unregister()
    registration.register()
    try:
        window_layout = _RecordingLayout()
        draw_window_menu(SimpleNamespace(layout=window_layout), None)
        assert window_layout.calls == [
            ("separator", (), {}),
            (
                "menu",
                ("COLMAP_MT_tools",),
                {
                    "text": "COLMAP Tools",
                    "icon": "FILE_FOLDER",
                },
            ),
        ]

        tools_layout = _RecordingLayout()
        COLMAP_MT_tools.draw(SimpleNamespace(layout=tools_layout), None)
        assert tools_layout.calls == [
            (
                "operator",
                ("colmap_align.open_workspace",),
                {
                    "text": "COLMAP Align 열기",
                    "icon": "ORIENTATION_GLOBAL",
                },
            ),
            (
                "operator",
                ("colmap.open_modifier_workspace",),
                {
                    "text": "COLMAP Modifier 열기",
                    "icon": "MODIFIER",
                },
            ),
            (
                "operator",
                ("projection_paint.open_workspace",),
                {
                    "text": "Projection Painting 열기",
                    "icon": "TPAINT_HLT",
                },
            ),
        ]
    finally:
        registration.unregister()


def test_root_shell_remains_only_menu_owner() -> None:
    """Align 등록 모듈은 공통 메뉴 Class나 Window Callback을 소유하지 않아야 합니다."""

    from tools.align import registration
    from shell.menu import COLMAP_MT_tools

    source = inspect.getsource(registration)
    assert "COLMAP_MT_tools" not in source
    assert COLMAP_MT_tools.bl_idname == "COLMAP_MT_tools"


def test_register_failure_after_view_menu_prepend_cleans_all_runtime_state() -> None:
    """View 메뉴 prepend 직후 등록 실패가 클래스·callback·handler를 모두 정리하는지 확인합니다."""
    from tools.align import registration
    from tools.align.state import RUNTIME

    panel_classes = _get_panel_classes()
    registration.unregister()
    original_prepend = registration._prepend_view_menu_once

    def fail_after_prepend() -> None:
        """실제 prepend를 먼저 수행한 뒤 등록 실패를 주입합니다."""
        original_prepend()
        raise RuntimeError("테스트용 View 메뉴 등록 실패")

    registration._prepend_view_menu_once = fail_after_prepend
    try:
        try:
            registration.register()
        except RuntimeError as error:
            assert "테스트용 View 메뉴 등록 실패" in str(error)
        else:
            raise AssertionError("View 메뉴 prepend 이후 등록 실패 주입이 동작하지 않았습니다.")
        assert registration._REGISTERED is False
        assert registration._VIEW_MENU_REGISTERED is False
        assert not any(_is_registered(panel_class) for panel_class in panel_classes)
        assert RUNTIME.draw_handlers == []
        assert not hasattr(bpy.types.Scene, "colmap_align_settings")
        assert not hasattr(bpy.types.WindowManager, "colmap_align_marker_pairs")
        assert not hasattr(bpy.types.WindowManager, "colmap_align_marker_align_log")
    finally:
        registration._prepend_view_menu_once = original_prepend
        registration.unregister()


def test_register_and_load_post_keep_runtime_catalog_empty() -> None:
    """등록과 load_post가 legacy만 정리하고 Demo·실제 모델을 자동 생성하지 않는지 확인합니다."""
    from tools.align import registration
    from tools.align.state import RUNTIME

    registration.unregister()
    _create_legacy_owned_data()
    registration.register()
    try:
        assert _legacy_data_is_absent()
        assert RUNTIME.model_catalog.model_count == 0
        assert RUNTIME.model_repository.models == {}
        assert RUNTIME.point_clouds == {}

        _create_legacy_owned_data()
        registration._on_load_post(None)
        assert _legacy_data_is_absent()
        assert RUNTIME.model_catalog.model_count == 0
        assert RUNTIME.model_repository.models == {}
        assert RUNTIME.point_clouds == {}
    finally:
        registration.unregister()
    assert _legacy_data_is_absent()


def test_immediate_unregister_completes_pending_legacy_migration() -> None:
    """지연 초기화 전 해제돼도 데이터 접근이 가능하면 legacy 정리를 마치는지 확인합니다."""
    from tools.align import registration

    registration.unregister()
    _create_legacy_owned_data()
    original_data_context_is_ready = registration._data_context_is_ready
    try:
        registration._data_context_is_ready = lambda: False
        registration.register()
    finally:
        registration._data_context_is_ready = original_data_context_is_ready
    registration.unregister()
    assert _legacy_data_is_absent()


def test_moving_sidebar_panel_hierarchy() -> None:
    """부모와 네 하위 패널이 오른쪽 P 뷰 N 패널용으로 등록됐는지 확인합니다."""
    from tools.align import registration

    panel_classes = _get_panel_classes()
    registration.register()
    try:
        root_panels = [
            panel_class
            for panel_class in panel_classes
            if panel_class.bl_label == "COLMAP Align" and not panel_class.bl_parent_id
        ]
        assert len(root_panels) == 1, "COLMAP Align 부모 패널이 하나여야 합니다."
        root_panel = root_panels[0]
        child_panels = [
            panel_class
            for panel_class in panel_classes
            if panel_class.bl_parent_id == root_panel.bl_idname
        ]

        assert [panel_class.bl_label for panel_class in sorted(child_panels, key=lambda panel: panel.bl_order)] == [
            "Data Layers",
            "Viewport",
            "Marker Align",
            "Merge & Export",
        ]
        for panel_class in (root_panel, *child_panels):
            assert panel_class.bl_space_type == "VIEW_3D"
            assert panel_class.bl_region_type == "UI"
            assert panel_class.bl_category == "COLMAP Align"
        assert root_panel.poll(bpy.context) is False, "일반 VIEW_3D에는 패널이 나타나면 안 됩니다."
    finally:
        registration.unregister()


def test_marker_ui_mirror_and_default_display_setting() -> None:
    """마커 목록 미러, ICP 기본값과 COLMAP RGB 기본 ON을 확인합니다."""
    from tools.align import registration
    from tools.align.operators.marker_pick import (
        COLMAPALIGN_OT_edit_marker_pair,
        COLMAPALIGN_OT_marker_pick_modal,
        COLMAPALIGN_OT_redo_marker_selection,
        COLMAPALIGN_OT_undo_marker_selection,
    )
    from tools.align.operators.workspace import COLMAPALIGN_OT_restore_workspace
    from tools.align.properties import marker_align_log_list, marker_pair_list
    from tools.align.ui.panels import (
        COLMAPALIGN_UL_marker_align_log,
        COLMAPALIGN_UL_marker_pairs,
        _marker_align_log_unit_limit,
        _text_display_units,
        wrap_marker_align_log_message,
    )

    registration.register()
    try:
        assert getattr(bpy.types, "COLMAPALIGN_UL_marker_pairs", None) is COLMAPALIGN_UL_marker_pairs
        assert (
            getattr(bpy.types, "COLMAPALIGN_UL_marker_align_log", None)
            is COLMAPALIGN_UL_marker_align_log
        )
        assert hasattr(bpy.types.WindowManager, "colmap_align_marker_pairs")
        assert hasattr(bpy.types.WindowManager, "colmap_align_marker_align_log")
        assert marker_pair_list(bpy.context.window_manager) is not None
        log_list = marker_align_log_list(bpy.context.window_manager)
        assert log_list is not None
        assert not hasattr(log_list.items.add(), "icon_name")
        log_list.items.clear()
        wrapped_lines = wrap_marker_align_log_message(
            "ICP 결과 미리보기 중에는 결과 버리기 또는 정렬 승인을 선택하십시오.",
            18,
        )
        assert len(wrapped_lines) >= 2
        assert all(_text_display_units(line) <= 18 for line in wrapped_lines)
        scaled_limit = _marker_align_log_unit_limit(
            SimpleNamespace(
                region=SimpleNamespace(width=280),
                preferences=SimpleNamespace(system=SimpleNamespace(ui_scale=2.0)),
            )
        )
        assert scaled_limit == 13
        scaled_lines = wrap_marker_align_log_message(
            "오차 검증 · RMS 0.42214 · 최대 0.538876",
            scaled_limit,
        )
        assert all(_text_display_units(line) <= scaled_limit for line in scaled_lines)
        settings = bpy.context.scene.colmap_align_settings
        assert settings.show_transform_gizmo is True
        assert settings.marker_use_uniform_scale is True
        assert settings.bl_rna.properties["marker_use_uniform_scale"].name == "Marker Refine Scale"
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
        assert tuple(item.identifier for item in backend_property.enum_items) == (
            "CUSTOM_HYBRID",
            "SMALL_GICP",
        )
        icp_preset_property = settings.bl_rna.properties.get("icp_preset")
        assert icp_preset_property is not None
        assert tuple(item.identifier for item in icp_preset_property.enum_items) == (
            "FAST",
            "BALANCED",
            "PRECISE",
        )
        assert tuple(item.name for item in icp_preset_property.enum_items) == (
            "빠름",
            "균형",
            "정밀",
        )
        settings.import_axis_preset = "CUSTOM"
        settings.import_axis_forward = "X"
        settings.import_axis_up = "-X"
        assert settings.import_axis_forward.lstrip("-") != settings.import_axis_up.lstrip("-")
        assert settings.bl_rna.properties.get("use_demo_color") is None
        display_limit_property = settings.bl_rna.properties.get("import_display_point_limit")
        assert display_limit_property is not None
        assert display_limit_property.default == 250_000
        assert display_limit_property.is_hidden is True
        assert settings.bl_rna.properties.get("reference_visible") is None
        assert settings.bl_rna.properties.get("moving_visible") is None
        assert "INTERNAL" in COLMAPALIGN_OT_restore_workspace.bl_options
        assert COLMAPALIGN_OT_edit_marker_pair.bl_label == "마커 편집"
        assert COLMAPALIGN_OT_edit_marker_pair.bl_rna.properties.get("edit_target") is None
        assert COLMAPALIGN_OT_marker_pick_modal.bl_rna.properties.get("edit_target") is None
        assert COLMAPALIGN_OT_undo_marker_selection.bl_label == "되돌리기"
        assert COLMAPALIGN_OT_redo_marker_selection.bl_label == "다시 실행"
    finally:
        registration.unregister()


def test_status_bar_feedback_and_top_info_removal() -> None:
    """세션 안내가 N 패널 고정 INFO 행 대신 Blender 상태 표시줄을 사용하는지 확인합니다."""
    from tools.align import registration
    from tools.align.status import set_workspace_status_text
    from tools.align.ui.panels import COLMAPALIGN_PT_main

    registration.register()
    try:
        draw_source = inspect.getsource(COLMAPALIGN_PT_main.draw)
        assert "colmap_align.exit_session" not in draw_source
        assert "RUNTIME.undo_journal.last_action" not in draw_source
        assert "RUNTIME.session.message" not in draw_source
        assert "탭/API 전환은 감지 후 이 작업공간으로 복귀합니다." not in draw_source
        assert "다중 모델 UI 골격" not in draw_source
        assert "Demo Models" not in draw_source
        workspace = bpy.context.workspace
        assert workspace is not None
        assert set_workspace_status_text(workspace, "COLMAP Align 상태 표시줄 테스트") is True
        assert set_workspace_status_text(workspace, None) is True
    finally:
        registration.unregister()


def run_all() -> None:
    """이 파일의 애드온 등록 및 패널 검사를 순서대로 실행합니다."""
    _add_source_path()
    test_register_unregister_lifecycle()
    test_window_entry_is_nested_under_colmap_tools()
    test_root_shell_remains_only_menu_owner()
    test_register_failure_after_view_menu_prepend_cleans_all_runtime_state()
    test_register_and_load_post_keep_runtime_catalog_empty()
    test_immediate_unregister_completes_pending_legacy_migration()
    test_moving_sidebar_panel_hierarchy()
    test_marker_ui_mirror_and_default_display_setting()
    test_status_bar_feedback_and_top_info_removal()
    print("test_registration: 통과")


if __name__ == "__main__":
    run_all()
