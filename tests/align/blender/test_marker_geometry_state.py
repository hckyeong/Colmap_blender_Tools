"""마커 출처·역할별 종류 고정과 표시 기준 스냅샷의 원자성을 검증합니다."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import sys


def _add_test_paths() -> None:
    """실험 소스와 실제 형상 회귀 자료를 테스트 경로에 추가합니다."""
    folder = Path(__file__).resolve().parent
    for path in (folder.parents[2], folder):
        if str(path) not in sys.path:
            sys.path.insert(0, str(path))


def _select(selection, role, model_id, mesh, index=0, source=None):
    """실제 자료의 점 또는 같은 삼각형 내부의 서로 다른 표면 위치를 선택합니다."""
    from tools.align.constants import MARKER_POINT_KIND_COLMAP, MARKER_POINT_KIND_MESH_SURFACE
    point_id = 0 if mesh else 11 + index
    position = (10.25 + index * 0.25, 0.25, 0.0) if mesh else ((0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0))[index]
    arguments = dict(model_id=model_id, point_kind=MARKER_POINT_KIND_MESH_SURFACE if mesh else MARKER_POINT_KIND_COLMAP,
                     source_point_id=point_id, geometry_source=source or ("COMPANION" if mesh else "MAIN"))
    return selection.select_draft_point(role, point_id, position, **arguments)


def _configure(runtime, reference_mesh=False, moving_mesh=False):
    """A와 P의 표시 종류를 독립적으로 고정하고 P를 선택합니다."""
    catalog = runtime.model_catalog
    assert catalog.set_geometry_visibility("reference", show_surface=reference_mesh)
    assert catalog.set_geometry_visibility("moving", show_surface=moving_mesh)
    assert catalog.select_model("moving", has_marker_draft=False, completed_pair_count=0)


def _assert_marker_data_equal(actual, expected) -> None:
    """거부 안내 문구를 제외한 마커 후보와 확정 상태가 동일한지 확인합니다."""
    assert replace(actual, message="") == replace(expected, message="")


def test_all_role_kind_combinations_keep_every_pair_and_edit_consistent():
    """점군·메시의 네 A/P 조합에서 모든 쌍과 편집이 같은 역할 종류를 유지합니다."""
    from test_companion_display import _runtime_fixture
    from tools.align.constants import ROLE_REFERENCE, ROLE_MOVING, GEOMETRY_KIND_TRIANGLE_MESH, GEOMETRY_KIND_COLMAP_POINT_CLOUD

    for reference_mesh in (False, True):
        for moving_mesh in (False, True):
            with _runtime_fixture() as runtime:
                _configure(runtime, reference_mesh, moving_mesh)
                state = runtime.marker_selection
                for index in range(2):
                    assert state.begin_create_draft(runtime=runtime), state.message
                    assert _select(state, ROLE_MOVING, "moving", moving_mesh, index)
                    assert _select(state, ROLE_REFERENCE, "reference", reference_mesh, index)
                    assert state.can_confirm_draft()
                    assert state.commit_draft() is not None
                expected_a = GEOMETRY_KIND_TRIANGLE_MESH if reference_mesh else GEOMETRY_KIND_COLMAP_POINT_CLOUD
                expected_p = GEOMETRY_KIND_TRIANGLE_MESH if moving_mesh else GEOMETRY_KIND_COLMAP_POINT_CLOUD
                assert state.role_geometry_kind(ROLE_REFERENCE) == expected_a
                assert state.role_geometry_kind(ROLE_MOVING) == expected_p
                assert state.geometry_conflict_message() is None
                assert state.begin_edit_draft(1, runtime=runtime)
                before = state.snapshot()
                assert not _select(state, ROLE_REFERENCE, "reference", not reference_mesh, 2)
                _assert_marker_data_equal(state.snapshot(), before)
                assert _select(state, ROLE_REFERENCE, "reference", reference_mesh, 2)
                assert state.commit_draft() is not None
                assert len(state.completed_pairs) == 2


def test_mixed_visible_a_first_click_locks_kind_and_undo_restores_choice():
    """혼합 A 표시에서 P를 먼저 고른 뒤 첫 A 클릭과 Undo가 잠정 종류를 복원합니다."""
    from test_companion_display import _runtime_fixture
    from tools.align.constants import ROLE_REFERENCE, ROLE_MOVING

    with _runtime_fixture() as runtime:
        _configure(runtime)
        catalog, state, journal = runtime.model_catalog, runtime.marker_selection, runtime.undo_journal
        assert catalog.promote_model_to_active("standalone", source="MANUAL", has_marker_draft=False, completed_pair_count=0)
        assert catalog.select_model("moving", has_marker_draft=False, completed_pair_count=0)
        baseline = state.snapshot()
        assert state.begin_create_draft(runtime=runtime)
        assert journal.begin_scope(baseline)
        assert state.draft.reference_geometry_kind is None
        assert _select(state, ROLE_MOVING, "moving", False)
        assert state.draft.reference_geometry_kind is None
        before_a = state.snapshot()
        assert _select(state, ROLE_REFERENCE, "standalone", True, source="MAIN")
        assert journal.record_draft(before_a, state.snapshot(), "A점 선택")
        first_kind = state.draft.reference_geometry_kind
        assert first_kind == "TRIANGLE_MESH"
        before_reject = state.snapshot()
        history = journal.can_undo_draft
        assert not state.can_select_draft_point(ROLE_REFERENCE, 12, (1, 0, 0), model_id="reference")
        assert not _select(state, ROLE_REFERENCE, "reference", False, 1)
        _assert_marker_data_equal(state.snapshot(), before_reject)
        assert journal.can_undo_draft == history
        assert journal.undo_draft(state)
        assert state.draft.reference_geometry_kind is None
        assert journal.redo_draft(state)
        assert state.draft.reference_geometry_kind == first_kind
        assert catalog.models["reference"].show_points
        assert catalog.models["standalone"].show_surface
        assert journal.cancel_scope(state)
        assert not state.has_draft and state.role_geometry_kind(ROLE_REFERENCE) is None


def test_source_is_part_of_duplicate_identity_and_companion_requires_surface():
    """주 형상·연결 메시의 중복 키를 구분하고 연결 메시 정점 입력을 거부합니다."""
    from tools.align.state import MarkerSelectionState, MarkerPair, SelectedPoint
    from tools.align.constants import ROLE_REFERENCE, ROLE_MOVING

    a = SelectedPoint(ROLE_REFERENCE, 0, (1, 2, 3), "owner", "MESH_SURFACE", 0, "MAIN")
    p = SelectedPoint(ROLE_MOVING, 11, (1, 2, 3), "moving")
    state = MarkerSelectionState(completed_pairs=[MarkerPair(1, a, p)])
    assert state.is_point_used(ROLE_REFERENCE, 0, model_id="owner", point_kind="MESH_SURFACE", position=(1, 2, 3))
    assert not state.is_point_used(ROLE_REFERENCE, 0, model_id="owner", point_kind="MESH_SURFACE", position=(1, 2, 3), geometry_source="COMPANION")
    assert state.begin_create_draft()
    before = state.snapshot()
    assert not state.can_select_draft_point(ROLE_REFERENCE, 0, (1, 2, 3), model_id="owner", point_kind="MESH_VERTEX", geometry_source="COMPANION")
    assert not state.select_draft_point(ROLE_REFERENCE, 0, (1, 2, 3), model_id="owner", point_kind="MESH_VERTEX", geometry_source="COMPANION")
    _assert_marker_data_equal(state.snapshot(), before)
    assert state.select_draft_point(ROLE_REFERENCE, 0, (1, 2, 3), model_id="owner", point_kind="MESH_SURFACE", geometry_source="COMPANION")


def test_committed_kind_survives_partial_delete_and_releases_after_last_delete():
    """부분 삭제·마지막 삭제·Undo·Redo가 확정 목록에서 종류 고정을 유도합니다."""
    from tools.align.state import MarkerSelectionState
    from tools.align.undo import UndoJournal
    from tools.align.constants import ROLE_REFERENCE, ROLE_MOVING

    state, journal = MarkerSelectionState(), UndoJournal()
    for index in range(2):
        assert state.begin_create_draft()
        assert _select(state, ROLE_REFERENCE, "reference", True, index)
        assert _select(state, ROLE_MOVING, "moving", False, index)
        assert state.commit_draft()
    assert state.delete_active_pair()
    assert state.role_geometry_kind(ROLE_REFERENCE) == "TRIANGLE_MESH"
    before = state.snapshot()
    assert state.delete_active_pair()
    assert journal.record_committed(before, state.snapshot(), "마지막 마커 삭제", "PAIR_DELETE")
    assert state.role_geometry_kind(ROLE_REFERENCE) is None
    assert journal.undo_committed(state)
    assert state.role_geometry_kind(ROLE_REFERENCE) == "TRIANGLE_MESH"
    assert journal.redo_committed(state)
    assert state.role_geometry_kind(ROLE_REFERENCE) is None
    assert state.begin_create_draft()
    assert _select(state, ROLE_REFERENCE, "reference", False)
    assert state.cancel_draft()
    assert state.role_geometry_kind(ROLE_REFERENCE) is None


def test_legacy_mixed_kind_preserves_pairs_and_blocks_new_edit_and_confirm():
    """혼합 구버전 마커는 보존하며 추가·편집을 막고 삭제와 Undo를 허용합니다."""
    from tools.align.state import MarkerSelectionState, MarkerPair, SelectedPoint
    from tools.align.undo import UndoJournal
    from tools.align.constants import ROLE_REFERENCE, ROLE_MOVING

    point = SelectedPoint(ROLE_REFERENCE, 11, (0, 0, 0), "reference")
    mesh = SelectedPoint(ROLE_REFERENCE, 0, (10.25, 0.25, 0), "reference", "MESH_SURFACE", 0, "COMPANION")
    p1 = SelectedPoint(ROLE_MOVING, 11, (0, 0, 0), "moving")
    p2 = SelectedPoint(ROLE_MOVING, 12, (1, 0, 0), "moving")
    state = MarkerSelectionState(completed_pairs=[MarkerPair(1, point, p1), MarkerPair(2, mesh, p2)], active_pair_id=2)
    journal = UndoJournal()
    original = tuple(state.completed_pairs)
    assert "혼합" in state.geometry_conflict_message()
    assert state.role_geometry_kind(ROLE_REFERENCE) is None
    assert not state.begin_create_draft()
    assert not state.begin_edit_draft(2)
    assert not state.can_confirm_draft()
    assert state.commit_draft() is None
    assert tuple(state.completed_pairs) == original
    before = state.snapshot()
    assert state.delete_active_pair()
    assert journal.record_committed(before, state.snapshot(), "혼합 마커 삭제", "PAIR_DELETE")
    assert state.geometry_conflict_message() is None
    assert journal.undo_committed(state)
    assert "혼합" in state.geometry_conflict_message()
    state.clear()
    assert state.geometry_conflict_message() is None


def test_start_rejects_when_existing_kind_has_no_visible_candidate():
    """기존 고정 종류와 같은 보이는 후보가 없으면 데이터와 Undo를 바꾸지 않습니다."""
    from test_companion_display import _runtime_fixture
    from tools.align.constants import ROLE_REFERENCE, ROLE_MOVING

    with _runtime_fixture() as runtime:
        _configure(runtime)
        state = runtime.marker_selection
        assert state.begin_create_draft(runtime=runtime)
        assert _select(state, ROLE_REFERENCE, "reference", False)
        assert _select(state, ROLE_MOVING, "moving", False)
        pair = state.commit_draft()
        assert runtime.model_catalog.set_geometry_visibility("reference", show_surface=True)
        before = state.snapshot()
        assert not state.begin_create_draft(runtime=runtime)
        assert "A 마커는 점군" in state.message
        _assert_marker_data_equal(state.snapshot(), before)
        assert not state.begin_edit_draft(pair.pair_id, runtime=runtime)
        assert not runtime.undo_journal.scope_active
        assert runtime.model_catalog.models["reference"].show_surface


def _mutate_capture(runtime, category):
    """입력 스냅샷에서 감지해야 할 외부 변경을 한 종류씩 적용합니다."""
    from tools.align.display_axes import create_model_display_transform
    from tools.align.alignment import create_sim3_transform
    import numpy as np

    repository, catalog = runtime.model_repository, runtime.model_catalog
    if category == "display":
        catalog.set_geometry_visibility("reference", show_surface=False)
    elif category == "eye":
        catalog.set_model_visibility("reference", False)
    elif category == "membership":
        catalog.models["standalone"] = replace(catalog.models["standalone"], registration_status="REGISTERED")
    elif category == "selection":
        catalog.selected_moving_model_id = "standalone"
        catalog.selected_model_ids = ("standalone",)
    elif category == "axis":
        repository.set_companion_display_transform("reference", create_model_display_transform("CUSTOM", "Y", "Z", rotation_xyz_degrees=(10, 20, 30)))
    elif category == "alignment":
        repository.set_alignment_transform("moving", create_sim3_transform(np.identity(3), 1.0, np.asarray((2, 3, 4))))
    elif category == "source":
        old = repository.companion_meshes["reference"]
        repository.companion_meshes["reference"] = replace(old, geometry_revision=old.geometry_revision + 1)
    elif category == "fingerprint":
        old = repository.companion_sources["reference"]
        repository.companion_sources["reference"] = replace(old, source_fingerprint="b" * 64)
    else:
        raise AssertionError(category)


def test_capture_rejects_external_display_source_axes_alignment_and_membership_changes():
    """외부 변경 뒤 클릭·확정이 후보와 Undo를 보존하며 명시적으로 실패합니다."""
    from test_companion_display import _runtime_fixture
    from tools.align.constants import ROLE_REFERENCE, ROLE_MOVING

    for category in ("display", "eye", "membership", "selection", "axis", "alignment", "source", "fingerprint"):
        with _runtime_fixture() as runtime:
            _configure(runtime, True, False)
            state, journal = runtime.marker_selection, runtime.undo_journal
            baseline = state.snapshot()
            assert state.begin_create_draft(runtime=runtime)
            assert journal.begin_scope(baseline)
            assert _select(state, ROLE_REFERENCE, "reference", True)
            assert _select(state, ROLE_MOVING, "moving", False)
            before = state.snapshot()
            _mutate_capture(runtime, category)
            assert not state.can_select_draft_point(ROLE_MOVING, 12, (1, 0, 0), model_id="moving"), category
            assert not _select(state, ROLE_MOVING, "moving", False, 1), category
            assert not state.can_confirm_draft(), category
            assert state.commit_draft() is None, category
            _assert_marker_data_equal(state.snapshot(), before)
            assert journal.scope_active and not journal.can_undo_draft
            assert "기준이 바뀌었습니다" in state.message
            assert journal.cancel_scope(state)
            assert not state.has_draft


def test_failed_commit_restore_keeps_capture_validation_and_view_navigation_unlocked():
    """확정 직후 복구한 드래프트는 입력 지문을 유지하며 시점 변경을 허용합니다."""
    from test_companion_display import _runtime_fixture
    from tools.align.constants import ROLE_REFERENCE, ROLE_MOVING

    with _runtime_fixture() as runtime:
        _configure(runtime, True, False)
        state = runtime.marker_selection
        assert state.begin_create_draft(runtime=runtime)
        assert _select(state, ROLE_REFERENCE, "reference", True)
        assert _select(state, ROLE_MOVING, "moving", False)
        before = state.snapshot()
        assert state.commit_draft()
        state.restore_snapshot(before)
        runtime.role_view_states[101] = {"view_distance": 123.0}
        assert state.can_confirm_draft()
        assert state.draft_geometry_rejection_message() is None
        _mutate_capture(runtime, "alignment")
        assert not state.can_confirm_draft()
        assert state.cancel_draft()


def run_all() -> None:
    """역할 종류·출처·입력 스냅샷의 상태 회귀를 모두 실행합니다."""
    _add_test_paths()
    tests = [value for name, value in globals().items() if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
    print(f"test_marker_geometry_state: {len(tests)}개 통과")


if __name__ == "__main__":
    run_all()
