"""단일 형상 선택 UI와 마커 입력 거부 안내의 실제 오퍼레이터 경계를 검증합니다."""

from __future__ import annotations

from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path
import sys
from types import MethodType, SimpleNamespace
from unittest.mock import Mock, patch


def _add_test_paths():
    """실험 소스와 실제 UI·형상 회귀 자료를 테스트 경로에 추가합니다."""
    folder = Path(__file__).resolve().parent
    for path in (folder.parents[2], folder):
        if str(path) not in sys.path:
            sys.path.insert(0, str(path))


def _modal_operator():
    """네이티브 모달 등록만 생략하고 실제 보고 메서드를 사용하는 연산자를 만듭니다."""
    from tools.align.operators.marker_pick import COLMAPALIGN_OT_marker_pick_modal

    operator = SimpleNamespace(
        operation="CREATE", target_pair_id=0, report=Mock(), _space_navigation_active=False,
        _apply_pending_command=Mock(return_value=None),
    )
    operator._report_input_rejection = MethodType(COLMAPALIGN_OT_marker_pick_modal._report_input_rejection, operator)
    return operator


def test_geometry_icon_selection_is_exclusive_and_reclick_is_idempotent():
    """형상 아이콘 재클릭은 선택을 유지하고 다른 행·원본·정렬·Undo를 바꾸지 않습니다."""
    from test_geometry_axes_ui import _fixture, _Layout
    from tools.align.operators import model_group
    from tools.align.ui import panels

    with _fixture() as (runtime, context):
        catalog = runtime.model_catalog
        selected = catalog.selected_model_ids
        other = catalog.models["reference"]
        alignments = dict(runtime.model_repository.alignment_transforms)
        operator = model_group.COLMAPALIGN_OT_toggle_data_layer_geometry
        for geometry, points in (("TRIANGLE_MESH", False), ("TRIANGLE_MESH", False),
                                 ("COLMAP_POINT_CLOUD", True), ("COLMAP_POINT_CLOUD", True)):
            assert operator.execute(SimpleNamespace(model_id="moving", geometry_kind=geometry), context) == {"FINISHED"}
            record = catalog.models["moving"]
            assert record.show_points is points and record.show_surface is not points
            assert catalog.models["reference"] is other
            assert catalog.selected_model_ids == selected
            assert not runtime.undo_journal.can_undo
            for model_id, transform in alignments.items():
                assert runtime.model_repository.alignment_transforms[model_id] is transform
            layout = _Layout()
            panels._draw_data_layer_row(layout, record, catalog, importing=False, selection_locked=False)
            buttons = [item for item in layout.all_entries() if item[0] == "operator" and item[1] == "colmap_align.toggle_data_layer_geometry"]
            assert sum(bool(item[2]["depress"]) for item in buttons) == 1
        standalone = SimpleNamespace(model_id="standalone", geometry_kind="TRIANGLE_MESH")
        assert operator.execute(standalone, context) == {"FINISHED"}
        assert catalog.models["standalone"].show_surface and not catalog.models["standalone"].show_points
        before = catalog.models["moving"]
        assert model_group.COLMAPALIGN_OT_toggle_data_layer_visibility.execute(SimpleNamespace(model_id="moving"), context) == {"FINISHED"}
        after = catalog.models["moving"]
        assert not after.is_visible
        assert (before.show_points, before.show_surface) == (after.show_points, after.show_surface)


def test_target_geometry_eye_group_and_selection_changes_are_blocked_during_input():
    """드래프트 대상의 표시·눈·A/P·선택 변경은 안내 후 상태와 이력 없이 거부합니다."""
    from test_geometry_axes_ui import _fixture
    from test_marker_geometry_state import _configure
    from tools.align.operators import model_group

    with _fixture() as (runtime, context):
        _configure(runtime, True, False)
        state = runtime.marker_selection
        assert state.begin_create_draft(runtime=runtime)
        before_catalog = runtime.model_catalog.snapshot()
        before_markers = state.snapshot()
        requests = (
            (model_group.COLMAPALIGN_OT_toggle_data_layer_geometry, SimpleNamespace(model_id="reference", geometry_kind="COLMAP_POINT_CLOUD")),
            (model_group.COLMAPALIGN_OT_toggle_data_layer_visibility, SimpleNamespace(model_id="moving")),
            (model_group.COLMAPALIGN_OT_toggle_data_layer_group, SimpleNamespace(model_id="reference")),
            (model_group.COLMAPALIGN_OT_select_data_layer, SimpleNamespace(model_id="standalone", range_selection=False, extend_selection=False)),
        )
        for operator, properties in requests:
            assert operator.execute(properties, context) == {"CANCELLED"}
            assert runtime.model_catalog.snapshot() == before_catalog
            assert state.snapshot() == before_markers
            assert not runtime.undo_journal.can_undo
        assert model_group._report_status.call_count >= len(requests)


def test_panel_shows_role_kind_lock_and_external_change_reason():
    """마커 패널은 보이는 연결 메시의 종류 고정과 외부 축 변경 이유를 표시합니다."""
    from test_geometry_axes_ui import _fixture, _Layout
    from test_marker_geometry_state import _configure, _mutate_capture
    from tools.align.ui import panels

    with _fixture() as (runtime, _context):
        _configure(runtime, True, False)
        assert runtime.marker_selection.begin_create_draft(runtime=runtime)
        layout = _Layout()
        panels._draw_marker_target_visibility(layout, runtime.model_catalog)
        labels = [item[1]["text"] for item in layout.all_entries() if item[0] == "label"]
        assert "A: 메시 고정 · P: 점군 고정" in labels
        assert not any("대상 점군을 켜세요" in text for text in labels)
        _mutate_capture(runtime, "axis")
        layout = _Layout()
        panels._draw_marker_target_visibility(layout, runtime.model_catalog)
        assert any("기준이 바뀌었습니다" in item[1]["text"] for item in layout.all_entries() if item[0] == "label")


def test_nearest_wrong_kind_pick_is_reported_without_fallback_or_undo():
    """최근접이 고정 종류와 다르면 다른 후보로 대체하지 않고 거부를 보고합니다."""
    from test_companion_display import _runtime_fixture
    from test_marker_geometry_state import _configure, _select
    from tools.align.constants import ROLE_REFERENCE
    from tools.align.operators import marker_pick

    with _runtime_fixture() as runtime, ExitStack() as stack:
        _configure(runtime)
        catalog, state = runtime.model_catalog, runtime.marker_selection
        assert catalog.promote_model_to_active("standalone", source="MANUAL", has_marker_draft=False, completed_pair_count=0)
        assert catalog.select_model("moving", has_marker_draft=False, completed_pair_count=0)
        assert state.begin_create_draft(runtime=runtime)
        assert _select(state, ROLE_REFERENCE, "standalone", True, source="MAIN")
        before = state.snapshot()
        operator = _modal_operator()
        context = SimpleNamespace(window=SimpleNamespace(), window_manager=SimpleNamespace())
        region = SimpleNamespace(x=0, y=0)
        area = SimpleNamespace(spaces=SimpleNamespace(active=SimpleNamespace(region_3d=SimpleNamespace())))
        picked = SimpleNamespace(point3d_id=12, position=(1, 0, 0), model_id="reference",
                                 point_kind="COLMAP_POINT", source_point_id=12, geometry_source="MAIN")
        for name, value in (("RUNTIME", runtime), ("is_session_active_for_window", Mock(return_value=True)),
                            ("refresh_registry", Mock(return_value=True)),
                            ("find_viewport_under_mouse", Mock(return_value=(area, region, ROLE_REFERENCE))),
                            ("pick_nearest_point", Mock(return_value=picked)),
                            ("request_redraw", Mock()), ("set_window_status_text", Mock())):
            stack.enter_context(patch.object(marker_pick, name, value))
        event = SimpleNamespace(type="LEFTMOUSE", value="PRESS", mouse_x=12, mouse_y=12)
        result = marker_pick.COLMAPALIGN_OT_marker_pick_modal.modal(operator, context, event)
        assert result == {"RUNNING_MODAL"}
        assert marker_pick.pick_nearest_point.call_count == 1
        assert operator.report.call_count == 1
        assert "A 마커는 메시" in operator.report.call_args.args[1]
        assert replace(state.snapshot(), message="") == replace(before, message="")
        assert not runtime.undo_journal.can_undo_draft


def test_start_failure_reports_visible_kind_reason_without_starting_undo():
    """표시 종류 불일치로 시작 실패하면 구체적 안내만 남기고 모달·Undo를 열지 않습니다."""
    from test_companion_display import _runtime_fixture
    from test_marker_geometry_state import _configure, _select
    from tools.align.constants import ROLE_REFERENCE, ROLE_MOVING
    from tools.align.operators import marker_pick

    with _runtime_fixture() as runtime, ExitStack() as stack:
        _configure(runtime)
        state = runtime.marker_selection
        assert state.begin_create_draft(runtime=runtime)
        assert _select(state, ROLE_REFERENCE, "reference", False)
        assert _select(state, ROLE_MOVING, "moving", False)
        assert state.commit_draft()
        assert runtime.model_catalog.set_geometry_visibility("reference", show_surface=True)
        before = state.snapshot()
        operator = _modal_operator()
        context = SimpleNamespace(window=SimpleNamespace(), window_manager=SimpleNamespace(event_timer_add=Mock(), modal_handler_add=Mock()))
        stack.enter_context(patch.object(marker_pick, "RUNTIME", runtime))
        stack.enter_context(patch.object(marker_pick, "request_redraw"))
        stack.enter_context(patch.object(marker_pick, "set_window_status_text"))
        assert marker_pick.COLMAPALIGN_OT_marker_pick_modal.invoke(operator, context, None) == {"CANCELLED"}
        assert "A 마커는 점군" in operator.report.call_args.args[1]
        assert replace(state.snapshot(), message="") == replace(before, message="")
        assert not runtime.modal_active and not runtime.undo_journal.scope_active
        context.window_manager.event_timer_add.assert_not_called()


def test_legacy_kind_conflict_blocks_cc_align_until_markers_are_cleared():
    """혼합 마커는 CC 입력도 막으며 마커를 비우면 직접 정렬을 허용합니다."""
    from test_companion_display import _runtime_fixture
    from tools.align.state import MarkerPair, SelectedPoint
    from tools.align.operators import marker_alignment

    with _runtime_fixture() as runtime, ExitStack() as stack:
        catalog = runtime.model_catalog
        assert catalog.toggle_alignment_selection("reference", has_marker_draft=False, completed_pair_count=0)
        runtime.marker_selection.completed_pairs = [
            MarkerPair(1, SelectedPoint("REFERENCE", 11, (0, 0, 0), "reference"), SelectedPoint("MOVING", 11, (0, 0, 0), "moving")),
            MarkerPair(2, SelectedPoint("REFERENCE", 0, (10.25, 0.25, 0), "reference", "MESH_SURFACE", 0, "COMPANION"), SelectedPoint("MOVING", 12, (1, 0, 0), "moving")),
        ]
        stack.enter_context(patch.object(marker_alignment, "RUNTIME", runtime))
        stack.enter_context(patch.object(marker_alignment, "restore_input_block_reason", return_value=None))
        assert "혼합" in marker_alignment.marker_preview_start_block_reason()
        assert "혼합" in marker_alignment.direct_align_start_block_reason()
        runtime.marker_selection.completed_pairs = []
        assert marker_alignment.direct_align_start_block_reason() is None


def run_all():
    """단일 형상 선택과 마커 입력 UI 경계의 회귀 검사를 모두 실행합니다."""
    _add_test_paths()
    tests = [value for name, value in globals().items() if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
    print(f"test_marker_geometry_ui: {len(tests)}개 통과")


if __name__ == "__main__":
    run_all()
