"""Data Layers A/P 양방향 전환·선택·가시성과 공용 Undo 계약을 검증합니다."""

from __future__ import annotations

from dataclasses import replace
import inspect
import sys
from pathlib import Path
from types import SimpleNamespace


def _add_source_path() -> None:
    """애드온 소스 디렉터리를 테스트 모듈 탐색 경로에 추가합니다."""
    source_path = Path(__file__).resolve().parents[3]
    source_text = str(source_path)
    if source_text not in sys.path:
        sys.path.insert(0, source_text)


def _demo_catalog():
    """제품 Runtime과 분리해 테스트가 명시적으로 Demo 카탈로그를 구성합니다."""
    from tools.align.state import ModelCatalogState

    catalog = ModelCatalogState()
    catalog.reset_demo_catalog()
    return catalog


def _select(catalog, model_id: str, *, draft: bool = False, pairs: int = 0) -> bool:
    """반복되는 잠금 인자를 고정해 테스트 레이어 선택을 실행합니다."""
    return catalog.select_model(
        model_id,
        has_marker_draft=draft,
        completed_pair_count=pairs,
    )


def _toggle_alignment(catalog, model_id: str, *, draft: bool = False, pairs: int = 0) -> bool:
    """반복되는 잠금 인자를 고정해 Ctrl 개별 선택을 전환합니다."""

    return catalog.toggle_alignment_selection(
        model_id,
        has_marker_draft=draft,
        completed_pair_count=pairs,
    )


def _select_range(catalog, model_id: str, *, draft: bool = False, pairs: int = 0) -> bool:
    """반복되는 잠금 인자를 고정해 Shift 연속 범위 선택을 실행합니다."""

    return catalog.select_model_range(
        model_id,
        has_marker_draft=draft,
        completed_pair_count=pairs,
    )


def _transfer(catalog, model_id: str, *, draft: bool = False, pairs: int = 0) -> bool:
    """반복되는 수동 출처와 잠금 인자를 고정해 그룹 전환을 실행합니다."""
    from tools.align.constants import MODEL_ACTIVATION_SOURCE_MANUAL

    return catalog.transfer_model_group(
        model_id,
        source=MODEL_ACTIVATION_SOURCE_MANUAL,
        has_marker_draft=draft,
        completed_pair_count=pairs,
    )


def test_demo_catalog_starts_with_two_a_two_p_and_visible_layers() -> None:
    """초기 카탈로그가 A 둘·P 둘·단일 내부 Anchor와 전체 표시를 갖는지 확인합니다."""
    from tools.align.constants import (
        DEMO_MODEL_1_ID,
        DEMO_MODEL_2_ID,
        DEMO_MODEL_3_ID,
        DEMO_MODEL_4_ID,
    )
    from tools.align.state import ModelCatalogState

    catalog = _demo_catalog()
    assert tuple(model.model_id for model in catalog.ordered_models()) == (
        DEMO_MODEL_1_ID,
        DEMO_MODEL_2_ID,
        DEMO_MODEL_3_ID,
        DEMO_MODEL_4_ID,
    )
    assert tuple(model.model_id for model in catalog.active_models()) == (
        DEMO_MODEL_1_ID,
        DEMO_MODEL_2_ID,
    )
    assert tuple(model.model_id for model in catalog.inactive_models()) == (
        DEMO_MODEL_3_ID,
        DEMO_MODEL_4_ID,
    )
    assert tuple(model.model_id for model in catalog.active_models() if model.is_anchor) == (
        DEMO_MODEL_1_ID,
    )
    assert all(model.is_visible for model in catalog.ordered_models())
    assert catalog.selected_model_id is None


def test_plain_ctrl_and_shift_selection_follow_windows_file_selection_rules() -> None:
    """일반·Ctrl·Shift 클릭이 단일·개별 토글·기준점 범위 선택 규칙을 지키는지 확인합니다."""
    from tools.align.constants import (
        DEMO_MODEL_1_ID,
        DEMO_MODEL_2_ID,
        DEMO_MODEL_3_ID,
        DEMO_MODEL_4_ID,
        MODEL_STATUS_PREVIEW,
    )
    from tools.align.state import ModelCatalogState

    catalog = _demo_catalog()
    assert _select(catalog, DEMO_MODEL_3_ID)
    assert catalog.selected_model_id == DEMO_MODEL_3_ID
    assert catalog.selected_inactive_model_id == DEMO_MODEL_3_ID
    assert catalog.alignment_selected_model_ids == ()
    assert _select(catalog, DEMO_MODEL_2_ID)
    assert catalog.selected_model_id == DEMO_MODEL_2_ID
    assert catalog.selected_inactive_model_id is None
    assert catalog.alignment_selected_model_ids == ()
    assert _select(catalog, DEMO_MODEL_3_ID)
    assert _toggle_alignment(catalog, DEMO_MODEL_2_ID)
    assert catalog.selected_model_ids == (DEMO_MODEL_2_ID, DEMO_MODEL_3_ID)
    assert catalog.selected_model_id == DEMO_MODEL_2_ID
    assert catalog.selected_reference_model_id == DEMO_MODEL_2_ID
    assert catalog.selected_moving_model_id == DEMO_MODEL_3_ID
    assert catalog.selected_inactive_model_id == DEMO_MODEL_3_ID
    assert catalog.alignment_selection_is_complete is True
    assert catalog.selection_anchor_model_id == DEMO_MODEL_2_ID

    assert _select_range(catalog, DEMO_MODEL_4_ID)
    assert catalog.selected_model_ids == (
        DEMO_MODEL_2_ID,
        DEMO_MODEL_3_ID,
        DEMO_MODEL_4_ID,
    )
    assert catalog.selected_model_id == DEMO_MODEL_4_ID
    assert catalog.selection_anchor_model_id == DEMO_MODEL_2_ID
    assert _select_range(catalog, DEMO_MODEL_3_ID)
    assert catalog.selected_model_ids == (DEMO_MODEL_2_ID, DEMO_MODEL_3_ID)
    assert catalog.selected_model_id == DEMO_MODEL_3_ID
    assert catalog.selection_anchor_model_id == DEMO_MODEL_2_ID

    assert _toggle_alignment(catalog, DEMO_MODEL_1_ID)
    assert catalog.selection_anchor_model_id == DEMO_MODEL_1_ID
    assert _select_range(catalog, DEMO_MODEL_3_ID)
    assert catalog.selected_model_ids == (
        DEMO_MODEL_1_ID,
        DEMO_MODEL_2_ID,
        DEMO_MODEL_3_ID,
    )
    assert catalog.selection_anchor_model_id == DEMO_MODEL_1_ID
    assert not _select(catalog, DEMO_MODEL_1_ID, draft=True)
    assert catalog.selected_model_id == DEMO_MODEL_3_ID
    assert not _select_range(catalog, DEMO_MODEL_4_ID, pairs=1)
    assert catalog.clear_alignment_selection() is True
    assert catalog.selected_model_id == DEMO_MODEL_3_ID
    assert catalog.selected_inactive_model_id == DEMO_MODEL_3_ID
    assert catalog.alignment_selected_model_ids == ()
    catalog.models[DEMO_MODEL_3_ID] = replace(
        catalog.models[DEMO_MODEL_3_ID],
        registration_status=MODEL_STATUS_PREVIEW,
    )
    assert not _select(catalog, DEMO_MODEL_1_ID)
    assert catalog.selected_model_id == DEMO_MODEL_3_ID


def test_align_selection_accepts_one_or_more_a_and_exactly_one_p() -> None:
    """직접 Align이 A 하나 이상과 P 하나만 선택된 조합을 완료로 판정하는지 확인합니다."""

    from tools.align.constants import (
        DEMO_MODEL_1_ID,
        DEMO_MODEL_2_ID,
        DEMO_MODEL_3_ID,
        DEMO_MODEL_4_ID,
    )

    catalog = _demo_catalog()
    assert _select(catalog, DEMO_MODEL_3_ID)
    assert catalog.alignment_selection_is_complete is False

    assert _toggle_alignment(catalog, DEMO_MODEL_1_ID)
    assert catalog.alignment_selection_is_complete is True
    assert catalog.alignment_selected_model_ids == (DEMO_MODEL_1_ID, DEMO_MODEL_3_ID)

    assert _toggle_alignment(catalog, DEMO_MODEL_2_ID)
    assert catalog.selected_reference_model_ids == (DEMO_MODEL_1_ID, DEMO_MODEL_2_ID)
    assert catalog.selected_reference_model_id is None
    assert catalog.alignment_selection_is_complete is True
    assert catalog.alignment_selected_model_ids == (
        DEMO_MODEL_1_ID,
        DEMO_MODEL_2_ID,
        DEMO_MODEL_3_ID,
    )

    assert _toggle_alignment(catalog, DEMO_MODEL_4_ID)
    assert catalog.selected_moving_model_ids == (DEMO_MODEL_3_ID, DEMO_MODEL_4_ID)
    assert catalog.alignment_selection_is_complete is False
    assert catalog.alignment_selected_model_ids == ()


def test_group_transfer_is_bidirectional_and_leaves_only_transferred_layer_selected() -> None:
    """더블클릭 A↔P 전환은 모호한 다중 대상 없이 전환 레이어 하나만 남기는지 확인합니다."""
    from tools.align.constants import (
        DEMO_MODEL_1_ID,
        DEMO_MODEL_2_ID,
        DEMO_MODEL_3_ID,
        DEMO_MODEL_4_ID,
        MODEL_STATUS_PENDING,
        MODEL_STATUS_REGISTERED,
    )
    from tools.align.state import ModelCatalogState

    catalog = _demo_catalog()
    assert _select(catalog, DEMO_MODEL_3_ID)
    assert _toggle_alignment(catalog, DEMO_MODEL_4_ID)
    assert catalog.selected_model_ids == (DEMO_MODEL_3_ID, DEMO_MODEL_4_ID)
    assert _transfer(catalog, DEMO_MODEL_3_ID)
    assert catalog.models[DEMO_MODEL_3_ID].registration_status == MODEL_STATUS_REGISTERED
    assert catalog.selected_model_ids == (DEMO_MODEL_3_ID,)
    assert catalog.selected_model_id == DEMO_MODEL_3_ID
    assert _transfer(catalog, DEMO_MODEL_3_ID)
    assert catalog.models[DEMO_MODEL_3_ID].registration_status == MODEL_STATUS_PENDING
    assert catalog.selected_model_ids == (DEMO_MODEL_3_ID,)
    assert catalog.selected_model_id == DEMO_MODEL_3_ID

    assert _select(catalog, DEMO_MODEL_1_ID)
    assert _transfer(catalog, DEMO_MODEL_1_ID)
    assert catalog.models[DEMO_MODEL_1_ID].registration_status == MODEL_STATUS_PENDING
    assert catalog.models[DEMO_MODEL_1_ID].is_anchor is False
    assert catalog.models[DEMO_MODEL_2_ID].is_anchor is True
    assert catalog.selected_model_id == DEMO_MODEL_1_ID


def test_group_transfer_rejects_last_a_and_locked_state() -> None:
    """마지막 A와 열린 마커·완료 쌍·미리보기에서는 그룹 전환을 거부합니다."""
    from tools.align.constants import (
        DEMO_MODEL_1_ID,
        DEMO_MODEL_2_ID,
        DEMO_MODEL_3_ID,
        MODEL_STATUS_PREVIEW,
    )
    from tools.align.state import ModelCatalogState

    catalog = _demo_catalog()
    assert not _transfer(catalog, DEMO_MODEL_3_ID, draft=True)
    assert not _transfer(catalog, DEMO_MODEL_3_ID, pairs=1)
    catalog.models[DEMO_MODEL_3_ID] = replace(
        catalog.models[DEMO_MODEL_3_ID],
        registration_status=MODEL_STATUS_PREVIEW,
    )
    assert not _transfer(catalog, DEMO_MODEL_3_ID)

    catalog = _demo_catalog()
    assert _transfer(catalog, DEMO_MODEL_2_ID)
    before = catalog.snapshot()
    assert not _transfer(catalog, DEMO_MODEL_1_ID)
    assert catalog.snapshot() == before


def test_layer_visibility_is_independent_and_snapshot_safe() -> None:
    """각 레이어 표시 토글이 그룹·선택과 무관하며 스냅샷으로 정확히 복원되는지 확인합니다."""
    from tools.align.constants import DEMO_MODEL_1_ID, DEMO_MODEL_3_ID
    from tools.align.state import ModelCatalogState

    catalog = _demo_catalog()
    assert _select(catalog, DEMO_MODEL_3_ID)
    before = catalog.snapshot()
    assert catalog.toggle_model_visibility(DEMO_MODEL_1_ID)
    assert catalog.toggle_model_visibility(DEMO_MODEL_3_ID)
    assert catalog.models[DEMO_MODEL_1_ID].is_visible is False
    assert catalog.models[DEMO_MODEL_3_ID].is_visible is False
    assert catalog.selected_model_id == DEMO_MODEL_3_ID
    catalog.restore_snapshot(before)
    assert catalog.models[DEMO_MODEL_1_ID].is_visible is True
    assert catalog.models[DEMO_MODEL_3_ID].is_visible is True


def test_group_transfer_uses_one_shared_journal_entry() -> None:
    """양방향 그룹 이동 Undo·Redo가 하나의 공용 명령으로 카탈로그를 복원하는지 확인합니다."""
    from tools.align.constants import (
        DEMO_MODEL_2_ID,
        MODEL_COMMAND_TOGGLE_LAYER_GROUP,
        MODEL_STATUS_PENDING,
        MODEL_STATUS_REGISTERED,
    )
    from tools.align.state import ModelCatalogState
    from tools.align.undo import UndoJournal

    catalog = _demo_catalog()
    journal = UndoJournal()
    assert _select(catalog, DEMO_MODEL_2_ID)
    before = catalog.snapshot()
    assert _transfer(catalog, DEMO_MODEL_2_ID)
    after = catalog.snapshot()
    assert journal.record_committed(
        before,
        after,
        "Demo_2 → P 그룹",
        MODEL_COMMAND_TOGGLE_LAYER_GROUP,
    )
    assert journal.undo_committed(catalog)
    assert catalog.models[DEMO_MODEL_2_ID].registration_status == MODEL_STATUS_REGISTERED
    assert journal.redo_committed(catalog)
    assert catalog.models[DEMO_MODEL_2_ID].registration_status == MODEL_STATUS_PENDING


def test_group_undo_preserves_later_visibility_toggle() -> None:
    """그룹 이동 뒤 바꾼 독립 표시 상태가 그룹 Undo·Redo에 덮어쓰이지 않는지 확인합니다."""
    from tools.align.constants import (
        DEMO_MODEL_2_ID,
        MODEL_COMMAND_TOGGLE_LAYER_GROUP,
        MODEL_STATUS_PENDING,
        MODEL_STATUS_REGISTERED,
    )
    from tools.align.state import ModelCatalogState
    from tools.align.undo import UndoJournal

    catalog = _demo_catalog()
    journal = UndoJournal()
    before = catalog.snapshot()
    assert _transfer(catalog, DEMO_MODEL_2_ID)
    after = catalog.snapshot()
    assert journal.record_committed(
        before,
        after,
        "Demo_2 → P 그룹",
        MODEL_COMMAND_TOGGLE_LAYER_GROUP,
    )
    assert catalog.toggle_model_visibility(DEMO_MODEL_2_ID)

    def restore_group(snapshot, _kind: str) -> None:
        """세션과 동일하게 그룹 스냅샷만 복원합니다."""
        catalog.restore_group_snapshot(snapshot)

    assert journal.undo_committed(restore_group)
    assert catalog.models[DEMO_MODEL_2_ID].registration_status == MODEL_STATUS_REGISTERED
    assert catalog.models[DEMO_MODEL_2_ID].is_visible is False
    assert journal.redo_committed(restore_group)
    assert catalog.models[DEMO_MODEL_2_ID].registration_status == MODEL_STATUS_PENDING
    assert catalog.models[DEMO_MODEL_2_ID].is_visible is False


def test_repeated_ui_clicks_toggle_p_to_a_and_a_to_p() -> None:
    """N 패널이 PRESS만 전달해도 설정 시간 내 같은 행 두 클릭이 양방향 전환되는지 확인합니다."""
    from tools.align.constants import (
        DEMO_MODEL_2_ID,
        DEMO_MODEL_3_ID,
        MODEL_STATUS_PENDING,
        MODEL_STATUS_REGISTERED,
    )
    from tools.align.operators import model_group
    from tools.align.state import MarkerSelectionState, ModelCatalogState, RUNTIME
    from tools.align.undo import UndoJournal

    class _FakeOperator:
        """Blender UI invoke와 execute 경계를 호출할 최소 오퍼레이터 대역입니다."""

        model_id = DEMO_MODEL_3_ID

        def report(self, _level, _message) -> None:
            """테스트에서는 Blender 보고 메시지를 상태 변경 없이 받습니다."""

        def execute(self, context):
            """실제 선택 오퍼레이터 execute 구현으로 호출을 전달합니다."""
            return model_group.COLMAPALIGN_OT_select_data_layer.execute(self, context)

    context = SimpleNamespace(
        preferences=SimpleNamespace(
            inputs=SimpleNamespace(mouse_double_click_time=350),
        ),
    )
    event = SimpleNamespace(value="PRESS", ctrl=False, shift=False)
    original_catalog = RUNTIME.model_catalog
    original_selection = RUNTIME.marker_selection
    original_journal = RUNTIME.undo_journal
    original_monotonic = model_group.monotonic
    try:
        RUNTIME.model_catalog = _demo_catalog()
        RUNTIME.marker_selection = MarkerSelectionState()
        RUNTIME.undo_journal = UndoJournal()
        RUNTIME.clear_data_layer_click()
        times = iter((10.0, 10.2, 20.0, 20.2))
        model_group.monotonic = lambda: next(times)
        operator = _FakeOperator()
        assert model_group.COLMAPALIGN_OT_select_data_layer.invoke(operator, context, event) == {"FINISHED"}
        assert RUNTIME.model_catalog.selected_model_id == DEMO_MODEL_3_ID
        assert model_group.COLMAPALIGN_OT_select_data_layer.invoke(operator, context, event) == {"FINISHED"}
        assert RUNTIME.model_catalog.models[DEMO_MODEL_3_ID].registration_status == MODEL_STATUS_REGISTERED

        operator.model_id = DEMO_MODEL_2_ID
        assert model_group.COLMAPALIGN_OT_select_data_layer.invoke(operator, context, event) == {"FINISHED"}
        assert RUNTIME.model_catalog.selected_model_id == DEMO_MODEL_2_ID
        assert model_group.COLMAPALIGN_OT_select_data_layer.invoke(operator, context, event) == {"FINISHED"}
        assert RUNTIME.model_catalog.models[DEMO_MODEL_2_ID].registration_status == MODEL_STATUS_PENDING
        assert RUNTIME.undo_journal.undo_count == 2
    finally:
        model_group.monotonic = original_monotonic
        RUNTIME.clear_data_layer_click()
        RUNTIME.model_catalog = original_catalog
        RUNTIME.marker_selection = original_selection
        RUNTIME.undo_journal = original_journal


def test_modifier_ui_click_routes_ctrl_toggle_and_shift_range_separately() -> None:
    """오퍼레이터가 Ctrl 개별 토글과 Shift 연속 범위를 서로 다른 상태 함수로 전달하는지 확인합니다."""

    from tools.align.constants import DEMO_MODEL_1_ID, DEMO_MODEL_2_ID, DEMO_MODEL_3_ID
    from tools.align.operators import model_group
    from tools.align.state import MarkerSelectionState, RUNTIME

    class _FakeOperator:
        """수정 키 invoke 경계를 호출할 최소 오퍼레이터 대역입니다."""

        model_id = DEMO_MODEL_2_ID
        extend_selection = False
        range_selection = False

        def report(self, _level, _message) -> None:
            """테스트에서는 Blender 보고 메시지를 상태 변경 없이 받습니다."""

        def execute(self, context):
            """실제 선택 오퍼레이터 execute 구현으로 호출을 전달합니다."""

            return model_group.COLMAPALIGN_OT_select_data_layer.execute(self, context)

    original_catalog = RUNTIME.model_catalog
    original_selection = RUNTIME.marker_selection
    try:
        RUNTIME.model_catalog = _demo_catalog()
        RUNTIME.marker_selection = MarkerSelectionState()
        assert _select(RUNTIME.model_catalog, DEMO_MODEL_3_ID)
        event = SimpleNamespace(value="PRESS", ctrl=True, shift=False)
        result = model_group.COLMAPALIGN_OT_select_data_layer.invoke(
            _FakeOperator(),
            SimpleNamespace(),
            event,
        )
        assert result == {"FINISHED"}
        assert RUNTIME.model_catalog.selected_model_ids == (DEMO_MODEL_2_ID, DEMO_MODEL_3_ID)
        assert RUNTIME.model_catalog.selected_model_id == DEMO_MODEL_2_ID
        assert RUNTIME.model_catalog.selected_reference_model_id == DEMO_MODEL_2_ID
        assert RUNTIME.model_catalog.selected_moving_model_id == DEMO_MODEL_3_ID
        assert RUNTIME.model_catalog.alignment_selection_is_complete is True

        _FakeOperator.model_id = DEMO_MODEL_1_ID
        event = SimpleNamespace(value="PRESS", ctrl=False, shift=True)
        result = model_group.COLMAPALIGN_OT_select_data_layer.invoke(
            _FakeOperator(),
            SimpleNamespace(),
            event,
        )
        assert result == {"FINISHED"}
        assert RUNTIME.model_catalog.selected_model_ids == (
            DEMO_MODEL_1_ID,
            DEMO_MODEL_2_ID,
        )
        assert RUNTIME.model_catalog.selected_model_id == DEMO_MODEL_1_ID
        assert RUNTIME.model_catalog.selection_anchor_model_id == DEMO_MODEL_2_ID
    finally:
        RUNTIME.clear_data_layer_click()
        RUNTIME.model_catalog = original_catalog
        RUNTIME.marker_selection = original_selection


def test_data_layers_ui_has_group_selection_and_visibility_controls() -> None:
    """Data Layers 행이 고정 정렬·무배경 눈 토글을 사용하고 화면 맞춤은 없는지 확인합니다."""
    from tools.align.ui.panels import (
        COLMAPALIGN_PT_data_layers,
        COLMAPALIGN_PT_marker,
        COLMAPALIGN_PT_viewport,
    )

    layers_source = inspect.getsource(COLMAPALIGN_PT_data_layers.draw)
    viewport_source = inspect.getsource(COLMAPALIGN_PT_viewport.draw)
    marker_source = inspect.getsource(COLMAPALIGN_PT_marker.draw)
    for text in ("A 고정", "P 이동", "현재 P로 선택", "승인 대기", "RADIOBUT_ON"):
        assert text not in layers_source
    assert "selected_model_ids" in layers_source
    assert "toggle_data_layer_visibility" in layers_source
    assert "HIDE_OFF" in layers_source and "HIDE_ON" in layers_source
    assert "PINNED" in layers_source
    assert "BLANK1" in layers_source
    assert "layout.box()" not in layers_source
    assert "depress=is_selected" in layers_source
    assert "emboss=is_selected" in layers_source
    assert "depress=model.is_visible" not in layers_source
    assert "emboss=False" in layers_source
    assert "기준 A 화면 맞춤" not in viewport_source
    assert "이동 P 화면 맞춤" not in viewport_source
    assert "alignment_placeholder" not in marker_source


def test_data_layer_filename_and_geometry_share_one_control() -> None:
    """파일명과 짧은 형상 종류가 하나의 선택 버튼에 표시되는지 확인합니다."""

    from tools.align.ui.panels import COLMAPALIGN_PT_data_layers

    layers_source = inspect.getsource(COLMAPALIGN_PT_data_layers.draw)
    assert "text=model.display_name" not in layers_source
    assert "name_row.label(" not in layers_source
    assert '"OBJ"' in layers_source and '"COLMAP"' in layers_source
    assert 'text=f"{model.display_name} · {geometry_label}"' in layers_source


def run_all() -> None:
    """Data Layers 핵심 상태와 UI 정적 계약을 순서대로 검증합니다."""
    _add_source_path()
    test_demo_catalog_starts_with_two_a_two_p_and_visible_layers()
    test_plain_ctrl_and_shift_selection_follow_windows_file_selection_rules()
    test_align_selection_accepts_one_or_more_a_and_exactly_one_p()
    test_group_transfer_is_bidirectional_and_leaves_only_transferred_layer_selected()
    test_group_transfer_rejects_last_a_and_locked_state()
    test_layer_visibility_is_independent_and_snapshot_safe()
    test_group_transfer_uses_one_shared_journal_entry()
    test_group_undo_preserves_later_visibility_toggle()
    test_repeated_ui_clicks_toggle_p_to_a_and_a_to_p()
    test_modifier_ui_click_routes_ctrl_toggle_and_shift_range_separately()
    test_data_layers_ui_has_group_selection_and_visibility_controls()
    test_data_layer_filename_and_geometry_share_one_control()
    print("test_multi_model_ui_scaffold: 통과")


if __name__ == "__main__":
    run_all()
