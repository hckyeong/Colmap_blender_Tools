"""한 줄 데이터셋 UI와 축 팝업의 대상 고정·취소·적용 경계를 검증합니다."""

from contextlib import contextmanager, ExitStack
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import Mock, patch

import bpy


def _add_test_paths() -> None:
    """실험 소스와 기존 형상 fixture를 Blender 테스트 경로에 추가합니다."""
    test_dir = Path(__file__).resolve().parent
    for path in (test_dir.parents[2], test_dir):
        if str(path) not in sys.path:
            sys.path.insert(0, str(path))


class _Layout:
    """실제 패널 draw 호출의 행 계층·아이콘·오퍼레이터 속성을 기록합니다."""

    def __init__(self, parent=None):
        """부모 레이아웃과 독립적인 버튼·자식 목록을 준비합니다."""
        self.parent = parent
        self.children = []
        self.entries = []
        self.enabled = True
        self.active = True

    def row(self, **_kwargs):
        """부모 행에 연결된 자식 레이아웃을 기록합니다."""
        child = _Layout(self)
        self.children.append(child)
        return child

    def box(self):
        """팝업 형상 항목의 박스를 자식 레이아웃으로 기록합니다."""
        return self.row()

    def column(self, **_kwargs):
        """마커 안내와 오류 메시지의 세로 레이아웃을 기록합니다."""
        return self.row()

    def operator(self, identifier, **kwargs):
        """버튼의 아이콘과 추후 지정되는 model_id를 함께 보관합니다."""
        properties = SimpleNamespace()
        self.entries.append(("operator", identifier, kwargs, properties, self))
        return properties

    def label(self, **kwargs):
        """행 이름과 안내 메시지를 기록합니다."""
        self.entries.append(("label", kwargs))

    def prop(self, _data, name, **kwargs):
        """축과 회전 속성의 이름 및 표시 문구를 기록합니다."""
        self.entries.append(("prop", name, kwargs))

    def all_entries(self):
        """전체 자식 트리에서 기록한 UI 항목을 순서대로 반환합니다."""
        entries = list(self.entries)
        for child in self.children:
            entries.extend(child.all_entries())
        return entries


class _Dialog:
    """네이티브 창 호출만 대체해 실제 팝업 구현과 편집 서비스를 실행합니다."""

    def __init__(self, model_id):
        """팝업 RNA 기본값과 결과 보고 목록을 준비합니다."""
        self.model_id = model_id
        self.edit_token = ""
        self.target_name = ""
        self.is_standalone = False
        self.has_companion = False
        self.error_message = ""
        self.preview_enabled = True
        self.main_preset = "REALITY_CAPTURE"
        self.main_forward, self.main_up = "Z", "-Y"
        self.main_rotation = (0.0, 0.0, 0.0)
        self.companion_preset = "REALITY_CAPTURE_OBJ"
        self.companion_forward, self.companion_up = "Y", "Z"
        self.companion_rotation = (0.0, 0.0, 0.0)
        self.messages = []
        self.layout = _Layout()

    def report(self, level, message):
        """팝업에서 보고한 검증 오류를 보관합니다."""
        self.messages.append((level, message))

    def cancel(self, context):
        """취소 요청을 실제 오퍼레이터 취소 구현으로 전달합니다."""
        from tools.align.operators.axis_settings import COLMAPALIGN_OT_edit_geometry_axes
        COLMAPALIGN_OT_edit_geometry_axes.cancel(self, context)


@contextmanager
def _fixture():
    """두 COLMAP과 독립 OBJ를 격리 세션으로 구성하고 UI 외부 효과를 차단합니다."""
    from test_companion_display import _runtime_fixture
    from tools.align.operators import axis_settings, model_group
    from tools.align.ui import panels

    with _runtime_fixture() as runtime, ExitStack() as stack:
        runtime.session_active = True
        runtime.session.phase = "ACTIVE"
        context = SimpleNamespace(
            window=SimpleNamespace(as_pointer=lambda: 101),
            workspace=SimpleNamespace(as_pointer=lambda: 102),
            window_manager=SimpleNamespace(invoke_props_dialog=Mock(return_value={"RUNNING_MODAL"})),
        )
        runtime.session.target_window_pointer = 101
        runtime.session.align_workspace_pointer = 102
        for module in (axis_settings, panels):
            stack.enter_context(patch.object(module, "RUNTIME", runtime))
        for module in (axis_settings, model_group):
            stack.enter_context(patch.object(module, "request_redraw"))
            stack.enter_context(patch.object(module, "notify_project_state_changed"))
        stack.enter_context(patch.object(model_group, "_report_status"))
        stack.enter_context(patch.object(model_group, "restore_input_block_reason", return_value=None))
        try:
            yield runtime, context
        finally:
            if runtime.axis_settings.is_active:
                axis_settings.cancel_axis_edit(runtime)
            axis_settings.clear_axis_edit_dialogs()


def _open(model_id, context):
    """실제 invoke 경계로 편집 토큰을 생성하고 고정된 팝업을 반환합니다."""
    from tools.align.operators.axis_settings import COLMAPALIGN_OT_edit_geometry_axes
    dialog = _Dialog(model_id)
    assert COLMAPALIGN_OT_edit_geometry_axes.invoke(dialog, context, None) == {"RUNNING_MODAL"}
    return dialog


def test_dataset_rows_are_single_line_and_bind_each_icon_to_its_owner() -> None:
    """각 데이터셋 한 행과 점군·메시·톱니·눈 순서 및 비활성 조건을 확인합니다."""
    from tools.align.ui import panels

    with _fixture() as (runtime, _context):
        runtime.model_repository.companion_meshes.pop("reference")
        layout = _Layout()
        for model in runtime.model_catalog.ordered_models():
            panels._draw_data_layer_row(layout, model, runtime.model_catalog, importing=False, selection_locked=False)
        assert len(layout.children) == 3
        for row, model in zip(layout.children, runtime.model_catalog.ordered_models()):
            # 눈 버튼은 주 행에 있고 나머지 네 버튼은 한 행의 자식 열에 있습니다.
            children_buttons = [entry for child in row.children for entry in child.entries if entry[0] == "operator"]
            buttons = children_buttons + [entry for entry in row.entries if entry[0] == "operator"]
            assert [entry[1] for entry in buttons] == [
                "colmap_align.select_data_layer",
                "colmap_align.toggle_data_layer_geometry",
                "colmap_align.toggle_data_layer_geometry",
                "colmap_align.edit_geometry_axes",
                "colmap_align.toggle_data_layer_visibility",
            ]
            assert [entry[2]["icon"] for entry in buttons[1:]] == ["POINTCLOUD_DATA", "MESH_DATA", "PREFERENCES", "HIDE_OFF"]
            assert all(entry[3].model_id == model.model_id for entry in buttons)
            assert all(entry[2]["text"] == "" for entry in buttons[1:])
            assert buttons[1][4].enabled == (model.model_id != "standalone")
            assert buttons[2][4].enabled == (model.model_id != "reference")
        icons = bpy.types.UILayout.bl_rna.functions["operator"].parameters["icon"].enum_items
        assert "PREFERENCES" in icons and "POINTCLOUD_DATA" in icons and "MESH_DATA" in icons


def test_geometry_icons_preserve_selection_clear_double_click_and_support_obj() -> None:
    """선택 아이콘 재클릭이 형상·복수 선택·다른 행을 보존하는지 확인합니다."""
    from tools.align.operators import model_group

    with _fixture() as (runtime, _context):
        catalog = runtime.model_catalog
        catalog.toggle_alignment_selection("reference", has_marker_draft=False, completed_pair_count=0)
        selected = catalog.selected_model_ids
        original_reference = catalog.models["reference"]
        runtime.last_data_layer_click_model_id = "moving"
        runtime.last_data_layer_click_time = 10.0
        operator = model_group.COLMAPALIGN_OT_toggle_data_layer_geometry
        assert operator.execute(SimpleNamespace(model_id="moving", geometry_kind="COLMAP_POINT_CLOUD"), None) == {"FINISHED"}
        assert catalog.models["moving"].show_points
        assert catalog.models["reference"] is original_reference
        assert catalog.selected_model_ids == selected
        assert runtime.last_data_layer_click_model_id is None
        assert operator.execute(SimpleNamespace(model_id="standalone", geometry_kind="TRIANGLE_MESH"), None) == {"FINISHED"}
        assert catalog.models["standalone"].show_surface
        assert catalog.selected_model_ids == selected
        assert operator.execute(SimpleNamespace(model_id="standalone", geometry_kind="COLMAP_POINT_CLOUD"), None) == {"CANCELLED"}
        for operator_class, properties in (
            (operator, SimpleNamespace(model_id="moving", geometry_kind="COLMAP_POINT_CLOUD")),
            (model_group.COLMAPALIGN_OT_toggle_data_layer_visibility, SimpleNamespace(model_id="moving")),
        ):
            assert "moving" in operator_class.description(None, properties)


def test_name_double_click_keeps_group_transfer_and_not_icon_action() -> None:
    """이름의 더블클릭만 기존 A/P 그룹 이동으로 이어지는지 확인합니다."""
    from tools.align.operators import model_group

    with _fixture() as (_runtime, context), patch.object(model_group, "_commit_group_change", return_value={"FINISHED"}) as transfer:
        operator = SimpleNamespace(model_id="moving", range_selection=False, extend_selection=False)
        event = SimpleNamespace(value="DOUBLE_CLICK", shift=False, ctrl=False)
        result = model_group.COLMAPALIGN_OT_select_data_layer.invoke(operator, context, event)
        assert result == {"FINISHED"}
        transfer.assert_called_once_with(operator, "moving", allow_demote=True)


def test_popup_freezes_owner_keeps_selection_and_cancels_without_commit() -> None:
    """다른 선택 행의 톱니를 열어도 대상만 고정되고 취소하면 확정 축이 유지되는지 확인합니다."""
    from tools.align.operators import axis_settings

    with _fixture() as (runtime, context):
        selected = runtime.model_catalog.selected_model_ids
        baseline = runtime.model_repository.display_transforms["reference"]
        dialog = _open("reference", context)
        assert runtime.axis_settings.model_id == "reference"
        assert dialog.target_name == "reference" and dialog.has_companion
        assert runtime.model_catalog.selected_model_ids == selected
        context.window_manager.invoke_props_dialog.assert_called_once_with(dialog, width=460, confirm_text="적용")
        dialog.main_rotation = (0.0, 25.0, 0.0)
        axis_settings._update_preview(dialog, context)
        assert runtime.model_repository.display_transforms["reference"] is baseline
        assert runtime.axis_settings.main_transform.rotation_xyz_degrees == (0.0, 25.0, 0.0)
        dialog.cancel(context)
        assert runtime.model_repository.display_transforms["reference"] is baseline
        assert not runtime.axis_settings.is_active and not axis_settings._OPEN_DIALOGS
        assert runtime.model_catalog.selected_model_ids == selected
        axis_settings.notify_project_state_changed.assert_not_called()


def test_popup_manual_axes_validation_and_preview_toggle() -> None:
    """직접 축 변경·중복축 오류·미리보기 끄기가 마지막 유효값과 확정 상태를 보존하는지 확인합니다."""
    from tools.align.operators import axis_settings

    with _fixture() as (runtime, context):
        dialog = _open("moving", context)
        dialog.main_forward = "X"
        axis_settings._update_main_axes(dialog, context)
        assert dialog.main_preset == "CUSTOM"
        previous = runtime.axis_settings.main_transform
        dialog.main_up = "-X"
        axis_settings._update_main_axes(dialog, context)
        assert dialog.error_message
        assert runtime.axis_settings.main_transform is previous
        dialog.main_up = "Z"
        axis_settings._update_main_axes(dialog, context)
        assert not dialog.error_message
        dialog.preview_enabled = False
        axis_settings._update_preview(dialog, context)
        assert runtime.axis_settings.preview_enabled is False
        assert runtime.model_repository.display_transforms["moving"].rotation_xyz_degrees == (0.0, 0.0, 0.0)
        dialog.main_up = "X"
        axis_settings._update_main_axes(dialog, context)
        assert axis_settings.COLMAPALIGN_OT_edit_geometry_axes.execute(dialog, context) == {"CANCELLED"}
        assert not runtime.axis_settings.is_active
        axis_settings.notify_project_state_changed.assert_not_called()


def test_reset_is_component_specific_and_apply_notifies_once() -> None:
    """개별 RC 초기화와 두 항목 맞춤을 구별하고 확정 시 저장 알림 한 번만 발생하는지 확인합니다."""
    from tools.align.operators import axis_settings

    with _fixture() as (runtime, context):
        selected = runtime.model_catalog.selected_model_ids
        dialog = _open("moving", context)
        dialog.main_rotation = (10.0, 20.0, 30.0)
        dialog.companion_rotation = (40.0, 50.0, 60.0)
        reset = SimpleNamespace(edit_token=dialog.edit_token, component="COMPANION", report=Mock())
        assert axis_settings.COLMAPALIGN_OT_reset_geometry_axes.execute(reset, context) == {"FINISHED"}
        assert dialog.main_rotation == (10.0, 20.0, 30.0)
        assert dialog.companion_rotation == (0.0, 0.0, 0.0)
        reset.component = "BOTH"
        assert axis_settings.COLMAPALIGN_OT_reset_geometry_axes.execute(reset, context) == {"FINISHED"}
        assert dialog.main_preset == "REALITY_CAPTURE" and dialog.companion_preset == "REALITY_CAPTURE_OBJ"
        assert dialog.main_rotation == dialog.companion_rotation == (0.0, 0.0, 0.0)
        dialog.companion_rotation = (0.0, 0.0, 90.0)
        assert axis_settings.COLMAPALIGN_OT_edit_geometry_axes.execute(dialog, context) == {"FINISHED"}
        assert not runtime.axis_settings.is_active
        assert runtime.model_repository.companion_display_transforms["moving"].rotation_xyz_degrees == (0.0, 0.0, 90.0)
        assert runtime.model_catalog.selected_model_ids == selected
        axis_settings.notify_project_state_changed.assert_called_once()


def test_standalone_popup_draws_only_mesh_and_noop_does_not_notify() -> None:
    """독립 OBJ 팝업에 메시 항목만 있고 무변경 적용은 저장 상태를 바꾸지 않는지 확인합니다."""
    from tools.align.operators import axis_settings

    with _fixture() as (_runtime, context):
        dialog = _open("standalone", context)
        axis_settings.COLMAPALIGN_OT_edit_geometry_axes.draw(dialog, context)
        entries = dialog.layout.all_entries()
        property_names = [entry[1] for entry in entries if entry[0] == "prop"]
        assert dialog.is_standalone and not dialog.has_companion
        assert "main_preset" in property_names and "companion_preset" not in property_names
        assert axis_settings.COLMAPALIGN_OT_edit_geometry_axes.execute(dialog, context) == {"FINISHED"}
        axis_settings.notify_project_state_changed.assert_not_called()


def test_axis_edit_blocks_selection_group_change_and_deletion() -> None:
    """팝업 미리보기 중 선택·그룹·삭제의 직접 실행 경계도 차단되는지 확인합니다."""
    from tools.align.operators import model_group

    with _fixture() as (runtime, context):
        dialog = _open("moving", context)
        before = runtime.model_catalog.snapshot()
        operator = SimpleNamespace(model_id="reference", range_selection=False, extend_selection=False)
        assert model_group.COLMAPALIGN_OT_select_data_layer.execute(operator, context) == {"CANCELLED"}
        assert model_group._commit_group_change(operator, "reference", allow_demote=True) == {"CANCELLED"}
        assert model_group.model_deletion_block_reason("moving") is not None
        assert runtime.model_catalog.snapshot() == before
        dialog.cancel(context)


def test_delete_failure_restores_companion_current_axes_and_source_metadata() -> None:
    """축 편집 뒤 삭제가 실패해도 최초 출처와 연결 메시·현재 축이 정확히 복원되는지 확인합니다."""
    from tools.align.display_axes import create_model_display_transform
    from tools.align.model_repository import ImportedModelSourceMetadata
    from tools.align.operators import model_group

    with _fixture() as (runtime, _context):
        repository = runtime.model_repository
        model = repository.models["moving"]
        imported = repository.display_transforms["moving"]
        metadata = ImportedModelSourceMetadata(
            selected_root_absolute=str(model.source_path), sparse_path_absolute=str(model.source_path),
            source_format="text", source_fingerprint="b" * 64, display_point_limit=100,
            axis_preset=imported.preset_id, axis_forward=imported.forward_axis,
            axis_up=imported.up_axis, display_transform_fingerprint=imported.fingerprint,
        )
        repository.source_metadata["moving"] = metadata
        current = create_model_display_transform("MAYA", "-Z", "Y", rotation_xyz_degrees=(20.0, 0.0, 0.0))
        repository.set_display_transform("moving", current)
        companion = repository.companion_meshes["moving"]
        companion_source = repository.companion_sources["moving"]
        companion_axis = repository.companion_display_transforms["moving"]
        before = runtime.model_catalog.snapshot()
        with patch.object(model_group, "_owned_embedded_assets_for_model", return_value=()), \
             patch.object(runtime.marker_selection, "remove_pairs_for_model", side_effect=RuntimeError("삭제 실패 재현")):
            try:
                model_group.delete_model_from_runtime("moving")
            except RuntimeError as error:
                assert "삭제 실패 재현" in str(error)
            else:
                raise AssertionError("삭제 오류가 발생하지 않았습니다.")
        assert repository.models["moving"] is model
        assert repository.display_transforms["moving"] is current
        assert repository.source_metadata["moving"] is metadata
        assert repository.companion_meshes["moving"] is companion
        assert repository.companion_sources["moving"] is companion_source
        assert repository.companion_display_transforms["moving"] is companion_axis
        assert runtime.model_catalog.snapshot() == before


def test_hidden_points_hint_is_limited_to_current_marker_target() -> None:
    """다른 숨긴 행 대신 현재 P 대상의 전체 숨김에만 표시 안내를 추가합니다."""
    from tools.align.ui import panels

    with _fixture() as (runtime, _context):
        catalog = runtime.model_catalog
        catalog.set_model_visibility("reference", False)
        layout = _Layout()
        panels._draw_marker_target_visibility(layout, catalog)
        assert not any("마커를 지정하려면" in entry[1]["text"] for entry in layout.entries)
        catalog.set_model_visibility("moving", False)
        layout = _Layout()
        panels._draw_marker_target_visibility(layout, catalog)
        hints = [entry[1]["text"] for entry in layout.entries if "마커를 지정하려면" in entry[1]["text"]]
        assert len(hints) == 1
        assert "moving" in hints[0] and "reference" not in hints[0]


def test_dialog_watch_cancels_closed_window_workspace_exit_and_removed_target() -> None:
    """소유 창·작업공간·대상이 사라지면 편집과 팝업 참조가 정리되는지 확인합니다."""
    from tools.align.operators import axis_settings

    for reason in ("WINDOW", "WORKSPACE", "MODEL"):
        with _fixture() as (runtime, context):
            _open("moving", context)
            owner = SimpleNamespace(as_pointer=lambda: 101, workspace=SimpleNamespace(as_pointer=lambda: 102))
            windows = [owner]
            if reason == "WINDOW":
                windows = []
            elif reason == "WORKSPACE":
                owner.workspace = SimpleNamespace(as_pointer=lambda: 999)
            else:
                runtime.model_catalog.models.pop("moving")
            fake_context = SimpleNamespace(window_manager=SimpleNamespace(windows=windows))
            with patch.object(axis_settings, "bpy", SimpleNamespace(context=fake_context)):
                assert axis_settings._watch_axis_edit_dialogs() is None
            assert not runtime.axis_settings.is_active and not axis_settings._OPEN_DIALOGS


def test_missing_companion_draws_disabled_message() -> None:
    """연결 메시가 없는 COLMAP 팝업은 비활성 메시 안내를 표시하는지 확인합니다."""
    from tools.align.operators import axis_settings

    with _fixture() as (runtime, context):
        runtime.model_repository.companion_meshes.pop("moving")
        dialog = _open("moving", context)
        axis_settings.COLMAPALIGN_OT_edit_geometry_axes.draw(dialog, context)
        disabled = [child for child in dialog.layout.children if not child.enabled]
        assert len(disabled) == 1
        assert any(entry[0] == "label" and entry[1]["text"] == "연결된 메시가 없습니다." for entry in disabled[0].entries)
        dialog.cancel(context)
        assert not bpy.app.timers.is_registered(axis_settings._watch_axis_edit_dialogs)


def run_all() -> None:
    """축 편집 UI의 행·콜백·원자 적용 및 취소 회귀를 실행합니다."""
    _add_test_paths()
    for name, function in tuple(globals().items()):
        if name.startswith("test_") and callable(function):
            function()
    print("test_geometry_axes_ui: 통과")


if __name__ == "__main__":
    run_all()
