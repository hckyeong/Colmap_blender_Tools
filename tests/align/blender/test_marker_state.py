"""마커 드래프트, 확정 이력과 세션 입력 라우팅을 Blender에서 검증합니다."""

from __future__ import annotations

import sys
from pathlib import Path


def _add_source_path() -> None:
    """애드온 소스 디렉터리를 테스트 모듈 탐색 경로에 추가합니다."""
    source_path = Path(__file__).resolve().parents[3]
    source_text = str(source_path)
    if source_text not in sys.path:
        sys.path.insert(0, source_text)


def _start_create_draft(selection, journal) -> None:
    """새 마커 쌍 드래프트와 해당 임시 Undo 범위를 시작합니다."""
    baseline = selection.snapshot()
    assert selection.begin_create_draft() is True
    assert journal.begin_scope(baseline) is True


def _select_draft_point(selection, journal, role: str, point_id: int, position) -> None:
    """성공한 드래프트 점 선택을 임시 Undo 이력 한 건으로 기록합니다."""
    before = selection.snapshot()
    assert selection.select_draft_point(role, point_id, position) is True
    after = selection.snapshot()
    label = "A점 선택" if role == "REFERENCE" else "P점 선택"
    assert journal.record_draft(before, after, label) is True


def _commit_draft(selection, journal, action: str = "마커 쌍 추가", kind: str = "PAIR_CREATE"):
    """준비된 드래프트를 확정 쌍으로 바꾸고 하나의 확정 명령으로 압축합니다."""
    pair = selection.commit_draft()
    assert pair is not None
    assert journal.commit_scope(selection.snapshot(), action, kind) is True
    return pair


def _create_pair(selection, journal, reference_id: int, moving_id: int):
    """A/P 드래프트를 만들고 확정한 테스트용 마커 쌍을 반환합니다."""
    from tools.align.constants import ROLE_MOVING, ROLE_REFERENCE

    _start_create_draft(selection, journal)
    _select_draft_point(selection, journal, ROLE_REFERENCE, reference_id, (float(reference_id), 0.0, 0.0))
    _select_draft_point(selection, journal, ROLE_MOVING, moving_id, (float(moving_id), 1.0, 0.0))
    return _commit_draft(selection, journal)


def test_draft_accepts_both_role_orders_and_requires_confirm() -> None:
    """A-first와 P-first 모두 완료 전에는 드래프트만 유지하는지 확인합니다."""
    from tools.align.constants import PHASE_CAPTURING, PHASE_IDLE, ROLE_MOVING, ROLE_REFERENCE
    from tools.align.state import MarkerSelectionState
    from tools.align.undo import UndoJournal

    selection = MarkerSelectionState()
    journal = UndoJournal()
    _start_create_draft(selection, journal)
    _select_draft_point(selection, journal, ROLE_MOVING, 11, (1.0, 2.0, 3.0))
    assert selection.phase == PHASE_CAPTURING
    assert selection.draft is not None
    assert selection.draft.reference is None
    assert selection.draft.moving is not None
    assert selection.can_confirm_draft() is False
    assert selection.commit_draft() is None
    assert selection.phase == PHASE_CAPTURING

    _select_draft_point(selection, journal, ROLE_REFERENCE, 7, (4.0, 5.0, 6.0))
    assert selection.can_confirm_draft() is True
    assert selection.completed_pairs == []
    first_pair = _commit_draft(selection, journal)
    assert first_pair.pair_id == 1
    assert selection.phase == PHASE_IDLE
    assert selection.completed_pairs == [first_pair]
    assert journal.undo_count == 1
    assert journal.draft_undo_count == 0

    _start_create_draft(selection, journal)
    _select_draft_point(selection, journal, ROLE_REFERENCE, 8, (7.0, 8.0, 9.0))
    _select_draft_point(selection, journal, ROLE_MOVING, 12, (10.0, 11.0, 12.0))
    second_pair = _commit_draft(selection, journal)
    assert second_pair.pair_id == 2
    assert [pair.pair_id for pair in selection.completed_pairs] == [1, 2]


def test_draft_reselection_and_undo_redo_keep_other_role() -> None:
    """같은 역할 재선택과 드래프트 Undo/Redo가 반대 역할 후보를 보존하는지 확인합니다."""
    from tools.align.constants import ROLE_MOVING, ROLE_REFERENCE
    from tools.align.state import MarkerSelectionState
    from tools.align.undo import UndoJournal

    selection = MarkerSelectionState()
    journal = UndoJournal()
    _start_create_draft(selection, journal)
    _select_draft_point(selection, journal, ROLE_MOVING, 20, (2.0, 0.0, 0.0))
    _select_draft_point(selection, journal, ROLE_REFERENCE, 10, (1.0, 0.0, 0.0))
    _select_draft_point(selection, journal, ROLE_REFERENCE, 11, (1.1, 0.0, 0.0))
    assert selection.draft is not None
    assert selection.draft.reference.point3d_id == 11
    assert selection.draft.moving.point3d_id == 20
    assert journal.draft_undo_count == 3

    assert journal.undo_draft(selection) is True
    assert selection.draft is not None
    assert selection.draft.reference.point3d_id == 10
    assert selection.draft.moving.point3d_id == 20
    assert journal.redo_draft(selection) is True
    assert selection.draft.reference.point3d_id == 11

    before_same_point = selection.snapshot()
    assert selection.select_draft_point(ROLE_REFERENCE, 11, (1.1, 0.0, 0.0)) is False
    assert selection.snapshot() == before_same_point
    assert journal.draft_undo_count == 3


def test_cancel_scope_preserves_confirmed_redo_history() -> None:
    """드래프트 취소가 이전 확정 Redo를 비우거나 완료 쌍을 바꾸지 않는지 확인합니다."""
    from tools.align.constants import ROLE_REFERENCE
    from tools.align.state import MarkerSelectionState
    from tools.align.undo import UndoJournal

    selection = MarkerSelectionState()
    journal = UndoJournal()
    pair = _create_pair(selection, journal, 1, 2)
    assert journal.undo_committed(selection) is True
    assert selection.completed_pairs == []
    assert journal.redo_count == 1

    _start_create_draft(selection, journal)
    _select_draft_point(selection, journal, ROLE_REFERENCE, 3, (3.0, 0.0, 0.0))
    assert journal.cancel_scope(selection) is True
    assert selection.completed_pairs == []
    assert selection.has_draft is False
    assert journal.redo_count == 1
    assert journal.redo_committed(selection) is True
    assert selection.completed_pairs == [pair]


def test_edit_delete_and_stable_pair_ids() -> None:
    """편집·삭제 Undo와 중간 삭제 뒤 안정적인 쌍 ID를 확인합니다."""
    from tools.align.constants import ROLE_MOVING, ROLE_REFERENCE
    from tools.align.state import MarkerSelectionState
    from tools.align.undo import UndoJournal

    selection = MarkerSelectionState()
    journal = UndoJournal()
    first_pair = _create_pair(selection, journal, 1, 101)
    second_pair = _create_pair(selection, journal, 2, 102)
    assert [pair.pair_id for pair in selection.completed_pairs] == [1, 2]

    selection.set_active_pair_id(first_pair.pair_id)
    baseline = selection.snapshot()
    assert selection.begin_edit_draft(first_pair.pair_id) is True
    assert journal.begin_scope(baseline) is True
    assert selection.draft is not None
    assert selection.draft.reference == first_pair.reference
    assert selection.draft.moving == first_pair.moving
    assert selection.can_confirm_draft() is False
    before_self = selection.snapshot()
    assert selection.select_draft_point(ROLE_REFERENCE, first_pair.reference.point3d_id, first_pair.reference.local_position) is False
    assert selection.snapshot() == before_self
    assert selection.select_draft_point(ROLE_REFERENCE, second_pair.reference.point3d_id, second_pair.reference.local_position) is False
    assert "이미 다른 마커" in selection.message
    _select_draft_point(selection, journal, ROLE_REFERENCE, 3, (3.0, 0.0, 0.0))
    edited_pair = _commit_draft(selection, journal, "마커 쌍 편집", "PAIR_EDIT")
    assert edited_pair.pair_id == first_pair.pair_id
    assert edited_pair.moving == first_pair.moving
    assert edited_pair.reference.point3d_id == 3
    assert journal.undo_committed(selection) is True
    assert selection.get_pair(first_pair.pair_id) == first_pair
    assert journal.redo_committed(selection) is True
    assert selection.get_pair(first_pair.pair_id) == edited_pair

    selection.set_active_pair_id(first_pair.pair_id)
    before_delete = selection.snapshot()
    removed = selection.delete_active_pair()
    assert removed == edited_pair
    assert journal.record_committed(before_delete, selection.snapshot(), "마커 쌍 삭제", "PAIR_DELETE") is True
    assert [pair.pair_id for pair in selection.completed_pairs] == [2]
    assert journal.undo_committed(selection) is True
    assert [pair.pair_id for pair in selection.completed_pairs] == [1, 2]
    assert journal.redo_committed(selection) is True
    assert [pair.pair_id for pair in selection.completed_pairs] == [2]

    third_pair = _create_pair(selection, journal, 4, 104)
    assert third_pair.pair_id == 3
    assert [pair.pair_id for pair in selection.completed_pairs] == [2, 3]


def test_unified_edit_requires_actual_change_and_tracks_undo_redo() -> None:
    """통합 편집이 한쪽 실제 변경과 원복·Undo·Redo 상태를 정확히 반영하는지 확인합니다."""
    from tools.align.constants import ROLE_MOVING
    from tools.align.state import MarkerSelectionState
    from tools.align.undo import UndoJournal

    selection = MarkerSelectionState()
    journal = UndoJournal()
    pair = _create_pair(selection, journal, 1, 2)
    baseline = selection.snapshot()
    assert selection.begin_edit_draft(pair.pair_id) is True
    assert journal.begin_scope(baseline) is True
    assert selection.can_confirm_draft() is False
    assert selection.commit_draft() is None
    assert selection.get_pair(pair.pair_id) == pair
    assert selection.has_draft is True
    _select_draft_point(selection, journal, ROLE_MOVING, 3, (3.0, 1.0, 0.0))
    assert selection.draft is not None
    assert selection.draft.reference == pair.reference
    assert selection.can_confirm_draft() is True

    assert journal.undo_draft(selection) is True
    assert selection.draft is not None
    assert selection.draft.moving == pair.moving
    assert selection.can_confirm_draft() is False
    assert journal.redo_draft(selection) is True
    assert selection.can_confirm_draft() is True

    _select_draft_point(
        selection,
        journal,
        ROLE_MOVING,
        pair.moving.point3d_id,
        pair.moving.local_position,
    )
    assert selection.can_confirm_draft() is False
    assert journal.undo_draft(selection) is True
    assert selection.can_confirm_draft() is True
    assert journal.redo_draft(selection) is True
    assert selection.can_confirm_draft() is False

    assert journal.undo_draft(selection) is True
    edited = _commit_draft(selection, journal, "마커 쌍 편집", "PAIR_EDIT")
    assert edited.pair_id == pair.pair_id
    assert edited.moving.point3d_id == 3
    assert edited.reference == pair.reference


def test_journal_restore_failure_is_atomic() -> None:
    """확정 스냅샷 복원 실패가 Undo 스택을 이동시키지 않는지 확인합니다."""
    from tools.align.undo import UndoJournal

    class FailingState:
        """스냅샷 복원을 의도적으로 실패시키는 최소 테스트 상태입니다."""

        def restore_snapshot(self, _snapshot) -> None:
            """UndoJournal의 예외 원자성을 검사하려고 오류를 발생시킵니다."""
            raise RuntimeError("복원 실패")

    journal = UndoJournal()
    assert journal.record_committed("before", "after", "테스트", "PAIR_CREATE") is True
    try:
        journal.undo_committed(FailingState())
    except RuntimeError:
        pass
    else:
        raise AssertionError("복원 실패가 호출자에게 전달되지 않았습니다.")
    assert journal.undo_count == 1
    assert journal.redo_count == 0


def test_commit_scope_failure_keeps_draft_for_atomic_retry() -> None:
    """확정 이력 압축 실패가 드래프트 범위나 기존 확정 이력을 바꾸지 않는지 확인합니다."""
    from tools.align.state import MarkerSelectionState
    from tools.align.undo import UndoJournal

    selection = MarkerSelectionState()
    journal = UndoJournal()
    baseline = selection.snapshot()
    assert journal.begin_scope(baseline) is True
    assert journal.commit_scope(baseline, "마커 쌍 추가", "PAIR_CREATE") is False
    assert journal.scope_active is True
    assert journal.undo_count == 0
    assert journal.redo_count == 0
    assert journal.cancel_scope(selection) is True
    assert selection.snapshot() == baseline


def test_draft_overlay_is_active_without_ui_list_selection() -> None:
    """새 드래프트 마커가 이전 UIList 선택값과 무관하게 강조되는지 확인합니다."""
    from tools.align.constants import ROLE_MOVING, ROLE_REFERENCE
    from tools.align.state import RUNTIME
    from tools.align.viewport.labels import _marker_labels
    from tools.align.viewport.renderer import _marker_entries

    selection = RUNTIME.marker_selection
    snapshot = selection.snapshot()
    try:
        selection.clear()
        assert selection.begin_create_draft() is True
        assert selection.select_draft_point(ROLE_REFERENCE, 1, (1.0, 0.0, 0.0)) is True
        assert selection.select_draft_point(ROLE_MOVING, 2, (2.0, 0.0, 0.0)) is True
        selection.active_pair_id = None
        assert _marker_entries(ROLE_REFERENCE)[-1][2] is True
        assert _marker_entries(ROLE_MOVING)[-1][2] is True
        assert _marker_labels(ROLE_REFERENCE)[-1][3] is True
        assert _marker_labels(ROLE_MOVING)[-1][3] is True
    finally:
        selection.restore_snapshot(snapshot)


def test_pair_colors_are_stable_distinct_and_shared_by_all_marker_layers() -> None:
    """쌍 색상이 24개를 넘어도 반복되지 않고 원·외곽선·라벨에서 같음을 확인합니다."""
    from math import dist

    from tools.align.constants import MARKER_PALETTE, marker_pair_color
    from tools.align.viewport.labels import _marker_label_color
    from tools.align.viewport.renderer import _marker_circle_layers

    supported_colors = [marker_pair_color(pair_id) for pair_id in range(1, len(MARKER_PALETTE) + 1)]
    generated_colors = [marker_pair_color(pair_id) for pair_id in range(1, len(MARKER_PALETTE) + 25)]
    assert supported_colors == list(MARKER_PALETTE)
    assert len(set(generated_colors)) == len(generated_colors)
    assert marker_pair_color(17) == marker_pair_color(17)
    assert all(0.0 <= channel <= 1.0 for color in generated_colors for channel in color)
    assert all(color[3] == 1.0 for color in generated_colors)
    assert all(
        dist(left[:3], right[:3]) >= 0.35
        for left, right in zip(supported_colors, supported_colors[1:])
    )
    assert all(
        dist(left[:3], right[:3]) >= 0.30
        for index, left in enumerate(supported_colors)
        for right in supported_colors[index + 1 :]
    )

    for pair_id, color in enumerate(supported_colors, start=1):
        outer, inner = _marker_circle_layers(pair_id, is_active=False)
        active_outer, active_inner = _marker_circle_layers(pair_id, is_active=True)
        assert outer[1] == inner[1] == _marker_label_color(pair_id) == color
        assert active_outer[1] == active_inner[1] == color
        assert active_outer[0] > outer[0]
        assert active_outer[2] > outer[2]
        assert active_inner == inner

    for invalid_pair_id in (0, -1):
        try:
            marker_pair_color(invalid_pair_id)
        except ValueError:
            pass
        else:
            raise AssertionError("0 이하 마커 쌍 ID를 거부하지 않았습니다.")


def test_session_input_routing() -> None:
    """세션 단축키 분류가 Undo·Redo·차단 입력을 정확히 반환하는지 확인합니다."""
    from tools.align.constants import (
        INPUT_ACTION_BLOCK_UNDO_HISTORY,
        INPUT_ACTION_BLOCK_WORKSPACE_CYCLE,
        INPUT_ACTION_DELETE_MODEL,
        INPUT_ACTION_FRAME_ALL,
        INPUT_ACTION_REDO,
        INPUT_ACTION_UNDO,
    )
    from tools.align.operators.session_guard import route_session_input

    class FakeEvent:
        """필요한 키보드 수정자만 제공하는 경량 이벤트입니다."""

        def __init__(
            self,
            event_type,
            *,
            ctrl=False,
            shift=False,
            alt=False,
            oskey=False,
            value="PRESS",
        ):
            """테스트할 Blender Event 필드를 저장합니다."""
            self.type = event_type
            self.ctrl = ctrl
            self.shift = shift
            self.alt = alt
            self.oskey = oskey
            self.value = value

    assert route_session_input(FakeEvent("Z", ctrl=True)) == INPUT_ACTION_UNDO
    assert route_session_input(FakeEvent("Z", ctrl=True, shift=True)) == INPUT_ACTION_REDO
    assert route_session_input(FakeEvent("Y", ctrl=True)) == INPUT_ACTION_REDO
    assert route_session_input(FakeEvent("Z", ctrl=True, alt=True)) == INPUT_ACTION_BLOCK_UNDO_HISTORY
    assert route_session_input(FakeEvent("PAGE_UP", ctrl=True)) == INPUT_ACTION_BLOCK_WORKSPACE_CYCLE
    assert route_session_input(FakeEvent("DEL")) == INPUT_ACTION_DELETE_MODEL
    assert route_session_input(FakeEvent("DEL", shift=True)) == INPUT_ACTION_DELETE_MODEL
    assert route_session_input(FakeEvent("SPACE")) is None
    assert route_session_input(FakeEvent("Z", value="RELEASE")) is None
    assert route_session_input(FakeEvent("HOME")) == INPUT_ACTION_FRAME_ALL
    assert route_session_input(FakeEvent("HOME", shift=True)) is None
    assert route_session_input(FakeEvent("HOME", ctrl=True)) is None
    assert route_session_input(FakeEvent("HOME", alt=True)) is None
    assert route_session_input(FakeEvent("HOME", oskey=True)) is None


def test_stale_session_guard_home_finishes_and_passes_through() -> None:
    """오래된 세션 가드의 HOME을 정리한 뒤 Blender 기본 처리가 받도록 전달합니다."""
    from types import SimpleNamespace

    from tools.align.operators import session_guard
    from tools.align.state import RUNTIME

    finished = []
    guard = SimpleNamespace(
        _session_generation=RUNTIME.session.generation - 1,
        _finish=lambda _context: finished.append(True),
    )
    context = SimpleNamespace(window=object())
    event = SimpleNamespace(
        type="HOME",
        value="PRESS",
        ctrl=False,
        shift=False,
        alt=False,
        oskey=False,
    )
    result = session_guard.COLMAPALIGN_OT_session_input_guard.modal(guard, context, event)
    assert result == {"CANCELLED", "PASS_THROUGH"}
    assert finished == [True]


def test_session_undo_prefers_open_draft_scope() -> None:
    """드래프트가 열려 있으면 세션 Undo가 확정 쌍 대신 입력 이벤트만 되돌리는지 확인합니다."""
    from tools.align.constants import ROLE_MOVING
    from tools.align.session import undo_session_marker_state
    from tools.align.state import RUNTIME

    selection = RUNTIME.marker_selection
    journal = RUNTIME.undo_journal
    journal.clear()
    selection.clear()
    try:
        completed = _create_pair(selection, journal, 1, 2)
        _start_create_draft(selection, journal)
        _select_draft_point(selection, journal, ROLE_MOVING, 3, (3.0, 0.0, 0.0))
        assert undo_session_marker_state() is True
        assert selection.has_draft is True
        assert selection.draft is not None and selection.draft.moving is None
        assert selection.completed_pairs == [completed]
        assert undo_session_marker_state() is False
        assert selection.completed_pairs == [completed]
    finally:
        journal.clear()
        selection.clear()


def test_marker_command_boundary() -> None:
    """N 패널 명령의 단일 소비와 취소 우선순위를 검증합니다."""
    from tools.align.constants import (
        MARKER_COMMAND_CANCEL,
        MARKER_COMMAND_CONFIRM,
        MARKER_COMMAND_NONE,
    )
    from tools.align.state import RuntimeState

    runtime = RuntimeState()
    assert runtime.marker_command == MARKER_COMMAND_NONE
    assert runtime.request_marker_command(MARKER_COMMAND_CONFIRM) is True
    assert runtime.request_marker_command(MARKER_COMMAND_CONFIRM) is False
    assert runtime.request_marker_command(MARKER_COMMAND_CANCEL) is True
    assert runtime.take_marker_command() == MARKER_COMMAND_CANCEL
    assert runtime.take_marker_command() == MARKER_COMMAND_NONE
    try:
        runtime.request_marker_command("UNKNOWN")
    except ValueError:
        pass
    else:
        raise AssertionError("알 수 없는 마커 명령이 거부되지 않았습니다.")


def test_modal_pick_gate_requires_navigation_free_state_change() -> None:
    """모달 피킹 게이트가 탐색 chord와 상태 불변 후보를 소비 대상에서 제외하는지 확인합니다."""
    from tools.align.constants import ROLE_MOVING, ROLE_REFERENCE
    from tools.align.operators.marker_pick import _has_navigation_chord
    from tools.align.operators.navigation import (
        _event_follows_space_release,
        synchronize_space_navigation_state,
    )
    from tools.align.state import MarkerPair, MarkerSelectionState, SelectedPoint

    class FakeEvent:
        """탐색 수정자 판별에 필요한 최소 이벤트 필드를 제공합니다."""

        def __init__(
            self,
            *,
            event_type="LEFTMOUSE",
            value="PRESS",
            type_prev="",
            value_prev="",
            alt=False,
            ctrl=False,
            shift=False,
            oskey=False,
        ):
            """테스트할 수정자 상태를 저장합니다."""
            self.type = event_type
            self.value = value
            self.type_prev = type_prev
            self.value_prev = value_prev
            self.alt = alt
            self.ctrl = ctrl
            self.shift = shift
            self.oskey = oskey

    assert _has_navigation_chord(FakeEvent(), True) is True
    assert _has_navigation_chord(FakeEvent(alt=True), False) is True
    assert _has_navigation_chord(FakeEvent(), False) is False
    space_press = FakeEvent(event_type="SPACE", value="PRESS")
    space_release = FakeEvent(event_type="SPACE", value="RELEASE")
    consumed_release_followup = FakeEvent(type_prev="SPACE", value_prev="RELEASE")
    assert synchronize_space_navigation_state(space_press, False) is True
    assert synchronize_space_navigation_state(space_release, True) is False
    assert _event_follows_space_release(consumed_release_followup) is True
    assert synchronize_space_navigation_state(consumed_release_followup, True) is False
    assert synchronize_space_navigation_state(
        FakeEvent(event_type="WINDOW_DEACTIVATE"),
        True,
    ) is False

    selection = MarkerSelectionState()
    assert selection.begin_create_draft() is True
    before_first_pick = selection.snapshot()
    assert selection.can_select_draft_point(ROLE_REFERENCE, 1, (1.0, 0.0, 0.0)) is True
    assert selection.snapshot() == before_first_pick
    assert selection.select_draft_point(ROLE_REFERENCE, 1, (1.0, 0.0, 0.0)) is True

    before_reselect = selection.snapshot()
    assert selection.can_select_draft_point(ROLE_REFERENCE, 1, (1.0, 0.0, 0.0)) is False
    assert selection.snapshot() == before_reselect

    used_selection = MarkerSelectionState(
        completed_pairs=[
            MarkerPair(
                pair_id=1,
                reference=SelectedPoint(ROLE_REFERENCE, 2, (2.0, 0.0, 0.0)),
                moving=SelectedPoint(ROLE_MOVING, 3, (3.0, 0.0, 0.0)),
            )
        ]
    )
    assert used_selection.begin_create_draft() is True
    before_used_pick = used_selection.snapshot()
    assert used_selection.can_select_draft_point(ROLE_REFERENCE, 2, (2.0, 0.0, 0.0)) is False
    assert used_selection.snapshot() == before_used_pick


def test_reference_points_preserve_model_identity_and_allow_same_id_across_a_models() -> None:
    """A 그룹의 서로 다른 모델은 같은 점 ID를 독립 마커 후보로 사용할 수 있는지 확인합니다."""
    from tools.align.constants import (
        DEMO_MODEL_1_ID,
        DEMO_MODEL_2_ID,
        DEMO_MODEL_3_ID,
        ROLE_MOVING,
        ROLE_REFERENCE,
    )
    from tools.align.state import MarkerSelectionState

    selection = MarkerSelectionState()
    assert selection.begin_create_draft()
    assert selection.select_draft_point(
        ROLE_REFERENCE,
        7,
        (1.0, 2.0, 3.0),
        model_id=DEMO_MODEL_1_ID,
    )
    assert selection.select_draft_point(
        ROLE_MOVING,
        9,
        (4.0, 5.0, 6.0),
        model_id=DEMO_MODEL_3_ID,
    )
    first_pair = selection.commit_draft()
    assert first_pair is not None
    assert first_pair.reference.model_id == DEMO_MODEL_1_ID
    assert first_pair.moving.model_id == DEMO_MODEL_3_ID

    assert selection.begin_create_draft()
    assert not selection.can_select_draft_point(
        ROLE_REFERENCE,
        7,
        (1.0, 2.0, 3.0),
        model_id=DEMO_MODEL_1_ID,
    )
    assert selection.can_select_draft_point(
        ROLE_REFERENCE,
        7,
        (7.0, 8.0, 9.0),
        model_id=DEMO_MODEL_2_ID,
    )
    assert selection.select_draft_point(
        ROLE_REFERENCE,
        7,
        (7.0, 8.0, 9.0),
        model_id=DEMO_MODEL_2_ID,
    )
    assert selection.draft is not None
    assert selection.draft.reference is not None
    assert selection.draft.reference.model_id == DEMO_MODEL_2_ID


def run_all() -> None:
    """이 파일의 드래프트·확정 Undo와 입력 라우팅 검사를 순서대로 실행합니다."""
    _add_source_path()
    test_draft_accepts_both_role_orders_and_requires_confirm()
    test_draft_reselection_and_undo_redo_keep_other_role()
    test_cancel_scope_preserves_confirmed_redo_history()
    test_edit_delete_and_stable_pair_ids()
    test_unified_edit_requires_actual_change_and_tracks_undo_redo()
    test_journal_restore_failure_is_atomic()
    test_commit_scope_failure_keeps_draft_for_atomic_retry()
    test_draft_overlay_is_active_without_ui_list_selection()
    test_pair_colors_are_stable_distinct_and_shared_by_all_marker_layers()
    test_session_input_routing()
    test_stale_session_guard_home_finishes_and_passes_through()
    test_session_undo_prefers_open_draft_scope()
    test_marker_command_boundary()
    test_modal_pick_gate_requires_navigation_free_state_change()
    test_reference_points_preserve_model_identity_and_allow_same_id_across_a_models()
    print("test_marker_state: 통과")


if __name__ == "__main__":
    run_all()
