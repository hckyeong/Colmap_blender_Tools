"""Data Layers 다중 선택과 비파괴 G/R/S 레이어 변환 계약을 검증합니다."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
import sys
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

import numpy as np


def _add_test_paths() -> None:
    """애드온 소스와 기존 Runtime fixture 경로를 Blender 테스트 목록에 추가합니다."""

    test_path = Path(__file__).resolve().parent
    source_path = test_path.parents[2]
    for path in (source_path, test_path):
        path_text = str(path)
        if path_text not in sys.path:
            sys.path.insert(0, path_text)


def _runtime_with_models():
    """두 A와 두 P를 가진 실제 Repository Runtime fixture를 구성합니다."""

    from test_icp_runtime import _runtime_with_marker_preview

    runtime, _marker_result = _runtime_with_marker_preview(include_far_reference=True)
    from tools.align.alignment import cancel_marker_preview

    cancel_marker_preview(runtime)
    runtime.marker_selection.completed_pairs.clear()
    runtime.model_catalog.select_model(
        "moving",
        has_marker_draft=False,
        completed_pair_count=0,
    )
    return runtime


def _select_many(catalog, *model_ids: str) -> None:
    """첫 일반 선택 뒤 Ctrl·Shift와 같은 토글 호출로 여러 레이어를 고릅니다."""

    assert model_ids
    assert catalog.select_model(
        model_ids[0],
        has_marker_draft=False,
        completed_pair_count=0,
    )
    for model_id in model_ids[1:]:
        assert catalog.toggle_alignment_selection(
            model_id,
            has_marker_draft=False,
            completed_pair_count=0,
        )


def test_data_layer_multiselection_is_catalog_ordered_and_role_derived() -> None:
    """A/P 선택은 클릭 순서와 무관하게 카탈로그 순서이며 단일 역할만 직접 Align에 노출되는지 확인합니다."""

    from tools.align.state import ModelCatalogState

    catalog = ModelCatalogState()
    catalog.reset_demo_catalog()
    _select_many(catalog, "demo_4", "demo_2", "demo_3", "demo_1")
    assert catalog.selected_model_ids == ("demo_1", "demo_2", "demo_3", "demo_4")
    assert catalog.selected_model_id == "demo_1"
    assert catalog.selected_reference_model_ids == ("demo_1", "demo_2")
    assert catalog.selected_moving_model_ids == ("demo_3", "demo_4")
    assert catalog.selected_reference_model_id is None
    assert catalog.selected_moving_model_id is None
    assert catalog.alignment_selection_is_complete is False

    assert catalog.select_model(
        "demo_3",
        has_marker_draft=False,
        completed_pair_count=0,
    )
    assert catalog.selected_model_ids == ("demo_3",)
    assert catalog.selected_model_id == "demo_3"
    assert catalog.selected_moving_model_id == "demo_3"
    assert catalog.selected_reference_model_id is None


def test_preview_uses_common_world_origin_pivot_and_left_composition() -> None:
    """여러 모델의 Project World 원점 평균을 pivot으로 쓰고 delta × baseline만 미리보기에 적용하는지 확인합니다."""

    from tools.align.alignment import (
        begin_layer_transform,
        create_translation_delta,
        effective_alignment_for_model,
        set_layer_transform_preview_delta,
    )
    from tools.align.constants import ROLE_REFERENCE

    runtime = _runtime_with_models()
    runtime.model_repository.set_alignment_transform(
        "reference_far",
        create_translation_delta((100.0, 0.0, 0.0)),
    )
    _select_many(runtime.model_catalog, "reference", "reference_far")
    baseline_reference = runtime.model_repository.alignment_transform_for_model_id("reference")
    baseline_far = runtime.model_repository.alignment_transform_for_model_id("reference_far")
    expected_pivot = np.asarray((50.0, 0.0, 0.0), dtype=np.float64)
    state = begin_layer_transform(
        runtime,
        role=ROLE_REFERENCE,
        model_ids=("reference", "reference_far"),
    )
    np.testing.assert_allclose(state.pivot_float64, expected_pivot, rtol=0.0, atol=1.0e-9)
    delta = create_translation_delta((2.0, -3.0, 4.0))
    set_layer_transform_preview_delta(runtime, delta)
    preview_reference = effective_alignment_for_model(runtime, "reference")
    preview_far = effective_alignment_for_model(runtime, "reference_far")
    np.testing.assert_allclose(
        preview_reference.matrix_float64,
        delta.matrix_float64 @ baseline_reference.matrix_float64,
        rtol=0.0,
        atol=1.0e-9,
    )
    np.testing.assert_allclose(
        preview_far.matrix_float64,
        delta.matrix_float64 @ baseline_far.matrix_float64,
        rtol=0.0,
        atol=1.0e-9,
    )
    assert runtime.model_repository.alignment_transform_for_model_id("reference") is baseline_reference
    assert runtime.model_repository.alignment_transform_for_model_id("reference_far") is baseline_far


def test_custom_pivot_tracks_picked_source_point_and_exact_selection() -> None:
    """더블클릭 피벗이 원본 점을 추적하고 정확히 같은 역할·선택에서만 재사용되는지 확인합니다."""

    from tools.align.alignment import (
        begin_layer_transform,
        cancel_layer_transform,
        create_translation_delta,
        layer_transform_pivot_for_selection,
        project_world_model_origin,
        project_world_source_position,
        set_layer_transform_custom_pivot,
    )
    from tools.align.constants import ROLE_MOVING, ROLE_REFERENCE

    runtime = _runtime_with_models()
    source_position = (1.25, -2.5, 3.75)
    expected = project_world_source_position(runtime.model_repository, "moving", source_position)
    pivot = set_layer_transform_custom_pivot(
        runtime,
        role=ROLE_MOVING,
        model_ids=("moving",),
        source_model_id="moving",
        source_position=source_position,
    )
    np.testing.assert_allclose(pivot, expected, rtol=0.0, atol=1.0e-12)
    np.testing.assert_allclose(
        layer_transform_pivot_for_selection(runtime, ROLE_MOVING, ("moving",)),
        expected,
        rtol=0.0,
        atol=1.0e-12,
    )
    reference_default = project_world_model_origin(runtime.model_repository, "reference")
    np.testing.assert_allclose(
        layer_transform_pivot_for_selection(runtime, ROLE_REFERENCE, ("reference",)),
        reference_default,
        rtol=0.0,
        atol=1.0e-12,
    )

    state = begin_layer_transform(runtime, role=ROLE_MOVING, model_ids=("moving",))
    np.testing.assert_allclose(state.pivot_float64, expected, rtol=0.0, atol=1.0e-12)
    assert cancel_layer_transform(runtime) is True
    np.testing.assert_allclose(
        layer_transform_pivot_for_selection(runtime, ROLE_MOVING, ("moving",)),
        expected,
        rtol=0.0,
        atol=1.0e-12,
    )

    runtime.model_repository.set_alignment_transform(
        "moving",
        create_translation_delta((7.0, 0.0, 0.0)),
    )
    moved_expected = project_world_source_position(runtime.model_repository, "moving", source_position)
    np.testing.assert_allclose(
        layer_transform_pivot_for_selection(runtime, ROLE_MOVING, ("moving",)),
        moved_expected,
        rtol=0.0,
        atol=1.0e-12,
    )
    runtime.layer_transform.clear()
    np.testing.assert_allclose(
        layer_transform_pivot_for_selection(runtime, ROLE_MOVING, ("moving",)),
        project_world_model_origin(runtime.model_repository, "moving"),
        rtol=0.0,
        atol=1.0e-12,
    )


@contextmanager
def _pivot_event_context(runtime, pick):
    """피벗 처리의 화면 경계만 대체하고 실제 선택·좌표 변환과 상태 갱신을 실행합니다."""

    from mathutils import Vector
    from tools.align.constants import ROLE_REFERENCE
    from tools.align.operators import layer_transform as operator

    region = SimpleNamespace(x=10, y=20)
    view = SimpleNamespace(view_location=Vector((11, 12, 13)), update=Mock())
    area = SimpleNamespace(spaces=SimpleNamespace(active=SimpleNamespace(region_3d=view)))
    event = SimpleNamespace(mouse_x=110, mouse_y=220)
    with (
        patch.object(operator, "RUNTIME", runtime),
        patch.object(operator, "_viewport_for_event", return_value=(area, region, ROLE_REFERENCE)),
        patch.object(operator, "layer_transform_start_block_reason", return_value=None),
        patch.object(operator, "pick_nearest_point_for_model_ids", pick),
        patch.object(operator, "session_align_workspace", return_value=None),
        patch.object(operator, "set_workspace_status_text") as status,
        patch.object(operator, "request_redraw"),
    ):
        yield operator, region, view, event, status


def test_surface_pivot_uses_visible_selection_and_preserves_source_precision() -> None:
    """화면 표면 선택 결과가 숨긴 레이어를 제외하고 정밀 원본 좌표와 전체 선택 피벗을 보존하는지 검사합니다."""

    from tools.align.alignment import create_translation_delta, project_world_source_position
    from tools.align.constants import ROLE_REFERENCE
    from tools.align.viewport.picking import PickResult

    runtime = _runtime_with_models()
    _select_many(runtime.model_catalog, "reference", "reference_far")
    runtime.model_catalog.set_model_visibility("reference_far", False)
    runtime.model_repository.set_alignment_transform("reference", create_translation_delta((7, -3, 2)))
    source = (1.123456789012, -2.234567890123, 3.345678901234)
    alignment = runtime.model_repository.alignment_transform_for_model_id("reference")
    undo_count = runtime.undo_journal.undo_count
    for kind in ("MESH_SURFACE", "COLMAP_POINT"):
        pick = Mock(return_value=PickResult("reference", 60206246, source, 0, 1, kind, 60206246))
        with _pivot_event_context(runtime, pick) as (operator, region, view, event, status):
            assert operator.set_layer_transform_pivot_from_event(None, event) is True
            pick.assert_called_once_with(("reference",), region, view, (100.0, 200.0), screen_surface=True)
            state = runtime.layer_transform
            assert state.custom_pivot_role == ROLE_REFERENCE
            assert state.custom_pivot_model_ids == ("reference", "reference_far")
            assert state.custom_pivot_source_model_id == "reference"
            np.testing.assert_array_equal(state.custom_pivot_source_position_float64, source)
            expected = project_world_source_position(runtime.model_repository, "reference", source)
            np.testing.assert_allclose(tuple(view.view_location), expected, rtol=0, atol=1e-6)
            view.update.assert_called_once()
            status.assert_called_once()
        assert runtime.model_repository.alignment_transform_for_model_id("reference") is alignment
        assert runtime.undo_journal.undo_count == undo_count


def test_surface_pivot_miss_or_gpu_error_preserves_previous_pivot() -> None:
    """표면 미적중과 GPU 오류가 기존 피벗·카메라를 옮기지 않고 상태 안내로 끝나는지 검사합니다."""

    from tools.align.alignment import set_layer_transform_custom_pivot
    from tools.align.constants import ROLE_REFERENCE
    from tools.align.model_repository import ModelRepositoryError

    runtime = _runtime_with_models()
    _select_many(runtime.model_catalog, "reference")
    source = (1.125, -2.25, 3.5)
    set_layer_transform_custom_pivot(runtime, role=ROLE_REFERENCE, model_ids=("reference",),
                                     source_model_id="reference", source_position=source)
    for error in (None, ModelRepositoryError("화면 표면 선택 실패 검사")):
        pick = Mock(return_value=None, side_effect=error)
        with _pivot_event_context(runtime, pick) as (operator, region, view, event, status):
            before = tuple(view.view_location)
            assert operator.set_layer_transform_pivot_from_event(None, event) is True
            assert tuple(view.view_location) == before
            view.update.assert_not_called()
            np.testing.assert_array_equal(runtime.layer_transform.custom_pivot_source_position_float64, source)
            assert runtime.layer_transform.custom_pivot_source_model_id == "reference"
            assert runtime.layer_transform.custom_pivot_model_ids == ("reference",)
            status.assert_called_once()
            if error is not None:
                assert runtime.layer_transform.message == str(error)


def test_commit_cancel_and_session_undo_are_atomic() -> None:
    """확정은 모든 모델·MANUAL provenance·세션 Undo를 한 경계로 기록하고 취소는 Repository를 바꾸지 않는지 확인합니다."""

    from tools.align.alignment import (
        begin_layer_transform,
        cancel_layer_transform,
        commit_layer_transform,
        create_translation_delta,
        restore_layer_transform_commit_snapshot,
        set_layer_transform_preview_delta,
    )
    from tools.align.constants import ROLE_REFERENCE

    runtime = _runtime_with_models()
    _select_many(runtime.model_catalog, "reference", "reference_far")
    original = {
        model_id: runtime.model_repository.alignment_transform_for_model_id(model_id)
        for model_id in ("reference", "reference_far")
    }
    begin_layer_transform(
        runtime,
        role=ROLE_REFERENCE,
        model_ids=("reference", "reference_far"),
    )
    set_layer_transform_preview_delta(runtime, create_translation_delta((1.0, 2.0, 3.0)))
    before, after = commit_layer_transform(runtime, action="레이어 이동")
    assert runtime.layer_transform.is_active is False
    assert runtime.undo_journal.undo_count == 1
    for model_id in ("reference", "reference_far"):
        current = runtime.model_repository.alignment_transform_for_model_id(model_id)
        assert current.fingerprint != original[model_id].fingerprint
        provenance = runtime.model_catalog.models[model_id].alignment_provenance
        assert provenance is not None and provenance.method == "MANUAL"
        assert provenance.result_fingerprint == current.fingerprint

    restore_layer_transform_commit_snapshot(runtime, before)
    for model_id in ("reference", "reference_far"):
        assert runtime.model_repository.alignment_transform_for_model_id(model_id).fingerprint == original[model_id].fingerprint
    restore_layer_transform_commit_snapshot(runtime, after)
    for model_id in ("reference", "reference_far"):
        assert runtime.model_repository.alignment_transform_for_model_id(model_id).fingerprint != original[model_id].fingerprint

    committed = runtime.model_repository.alignment_transform_for_model_id("reference")
    begin_layer_transform(runtime, role=ROLE_REFERENCE, model_ids=("reference", "reference_far"))
    set_layer_transform_preview_delta(runtime, create_translation_delta((9.0, 0.0, 0.0)))
    cancel_layer_transform(runtime)
    assert runtime.model_repository.alignment_transform_for_model_id("reference") is committed


def test_session_undo_redo_restores_only_layer_transform_snapshot() -> None:
    """세션 Undo·Redo는 확정 alignment·provenance만 복원하고 이후 선택은 유지하는지 확인합니다."""

    from tools.align.alignment import (
        begin_layer_transform,
        commit_layer_transform,
        create_translation_delta,
        set_layer_transform_preview_delta,
    )
    from tools.align.constants import ROLE_REFERENCE
    from tools.align.session import redo_session_state, undo_session_state
    from tools.align.state import RUNTIME

    runtime = _runtime_with_models()
    _select_many(runtime.model_catalog, "reference", "reference_far")
    original_fingerprint = runtime.model_repository.alignment_transform_for_model_id("reference").fingerprint
    original_provenance = runtime.model_catalog.models["reference"].alignment_provenance
    begin_layer_transform(runtime, role=ROLE_REFERENCE, model_ids=("reference", "reference_far"))
    set_layer_transform_preview_delta(runtime, create_translation_delta((6.0, 0.0, 0.0)))
    commit_layer_transform(runtime, action="세션 Undo 레이어 변환")
    committed_fingerprint = runtime.model_repository.alignment_transform_for_model_id("reference").fingerprint
    assert committed_fingerprint != original_fingerprint
    assert runtime.model_catalog.select_model(
        "moving",
        has_marker_draft=False,
        completed_pair_count=0,
    )

    fields = (
        "model_repository",
        "model_catalog",
        "marker_selection",
        "marker_alignment",
        "layer_transform",
        "icp_alignment",
        "merge_export",
        "project_restore",
        "undo_journal",
        "session",
        "modal_active",
        "project_persistence_dirty",
    )
    previous = {field: getattr(RUNTIME, field) for field in fields}
    try:
        for field in fields:
            setattr(RUNTIME, field, getattr(runtime, field))
        assert undo_session_state() is True
        assert RUNTIME.model_repository.alignment_transform_for_model_id("reference").fingerprint == original_fingerprint
        assert RUNTIME.model_catalog.selected_model_ids == ("moving",)
        assert RUNTIME.model_catalog.models["reference"].alignment_provenance == original_provenance
        assert redo_session_state() is True
        assert RUNTIME.model_repository.alignment_transform_for_model_id("reference").fingerprint == committed_fingerprint
        assert RUNTIME.model_catalog.selected_model_ids == ("moving",)
        provenance = RUNTIME.model_catalog.models["reference"].alignment_provenance
        assert provenance is not None and provenance.method == "MANUAL"
    finally:
        for field, value in previous.items():
            setattr(RUNTIME, field, value)


def test_invalid_scale_and_stale_baseline_leave_repository_unchanged() -> None:
    """음수·비유한 스케일과 stale baseline은 변환 확정 전에 거부하는지 확인합니다."""

    from tools.align.alignment import (
        LayerTransformError,
        begin_layer_transform,
        commit_layer_transform,
        create_uniform_scale_delta,
        create_translation_delta,
        set_layer_transform_preview_delta,
    )
    from tools.align.constants import ROLE_MOVING

    runtime = _runtime_with_models()
    original = runtime.model_repository.alignment_transform_for_model_id("moving")
    begin_layer_transform(runtime, role=ROLE_MOVING, model_ids=("moving",))
    try:
        create_uniform_scale_delta(-1.0, (0.0, 0.0, 0.0))
    except LayerTransformError:
        pass
    else:
        raise AssertionError("음수 균일 스케일이 허용되었습니다.")
    set_layer_transform_preview_delta(runtime, create_translation_delta((1.0, 0.0, 0.0)))
    runtime.model_repository.set_alignment_transform("moving", create_translation_delta((8.0, 0.0, 0.0)))
    try:
        commit_layer_transform(runtime, action="stale 확인")
    except LayerTransformError:
        pass
    else:
        raise AssertionError("stale baseline 확정이 허용되었습니다.")
    assert runtime.model_repository.alignment_transform_for_model_id("moving").fingerprint != original.fingerprint
    assert runtime.layer_transform.is_active is True


def test_gizmo_axes_use_distinct_project_world_directions() -> None:
    """X/Y/Z 기즈모가 공통 pivot에서는 만나되 각 Project World 축 방향으로 분리되는지 확인합니다."""

    from mathutils import Vector

    from tools.align.operators.layer_transform import _gizmo_matrix

    pivot = (3.0, -2.0, 5.0)
    expected_directions = {
        "X": Vector((1.0, 0.0, 0.0)),
        "Y": Vector((0.0, 1.0, 0.0)),
        "Z": Vector((0.0, 0.0, 1.0)),
    }
    directions = {}
    for axis, expected in expected_directions.items():
        matrix = _gizmo_matrix(pivot, axis)
        directions[axis] = matrix.to_3x3() @ Vector((0.0, 0.0, 1.0))
        np.testing.assert_allclose(tuple(matrix.translation), pivot, rtol=0.0, atol=1.0e-6)
        np.testing.assert_allclose(tuple(directions[axis]), tuple(expected), rtol=0.0, atol=1.0e-6)
    assert len({tuple(round(component, 9) for component in direction) for direction in directions.values()}) == 3


def test_gizmo_visibility_toggle_controls_poll_and_panel_order() -> None:
    """기즈모 토글이 poll을 제어하고 포인트 크기 바로 아래에 표시되는지 확인합니다."""

    import inspect

    from tools.align import registration
    from tools.align.operators import layer_transform as layer_transform_operator
    from tools.align.operators.layer_transform import (
        COLMAPALIGN_OT_set_layer_transform_pivot,
    )
    from tools.align.ui.panels import COLMAPALIGN_PT_viewport

    context = SimpleNamespace(
        area=object(),
        region=SimpleNamespace(type="WINDOW"),
        scene=SimpleNamespace(
            colmap_align_settings=SimpleNamespace(show_transform_gizmo=False),
        ),
        window=object(),
    )
    original_role_for_area = layer_transform_operator.role_for_area
    original_block_reason = layer_transform_operator.layer_transform_start_block_reason
    try:
        layer_transform_operator.role_for_area = lambda _window, _area: "MOVING"
        layer_transform_operator.layer_transform_start_block_reason = lambda _role: None
        assert layer_transform_operator._gizmo_poll(context) is False
        context.scene.colmap_align_settings.show_transform_gizmo = True
        assert layer_transform_operator._gizmo_poll(context) is True
    finally:
        layer_transform_operator.role_for_area = original_role_for_area
        layer_transform_operator.layer_transform_start_block_reason = original_block_reason

    panel_source = inspect.getsource(COLMAPALIGN_PT_viewport.draw)
    assert "point_size_row.use_property_split = False" in panel_source
    assert 'point_size_row.alignment = "EXPAND"' in panel_source
    assert 'gizmo_row.label(text="", icon="BLANK1")' in panel_source
    assert 'color_row.label(text="", icon="BLANK1")' in panel_source
    assert 'gizmo_row.alignment = "LEFT"' in panel_source
    assert 'color_row.alignment = "LEFT"' in panel_source
    assert panel_source.index('point_size_row.prop(settings, "point_size")') < panel_source.index(
        'gizmo_row.prop(settings, "show_transform_gizmo")'
    ) < panel_source.index('color_row.prop(settings, "use_colmap_rgb")')
    pivot_operator_source = inspect.getsource(COLMAPALIGN_OT_set_layer_transform_pivot.invoke)
    keymap_source = inspect.getsource(registration._register_addon_keymaps)
    assert "_is_viewport_pivot_double_click(context, event)" in pivot_operator_source
    assert "set_layer_transform_pivot_from_event(context, event)" in pivot_operator_source
    assert '"LEFTMOUSE"' in keymap_source
    assert '"PRESS"' in keymap_source
    assert "head=True" in keymap_source


def test_viewport_pivot_double_click_uses_blender_time_and_mouse_threshold() -> None:
    """피벗 더블클릭이 같은 Window·Area의 설정 시간과 마우스 거리 안에서만 성립하는지 확인합니다."""

    from tools.align.operators import layer_transform as layer_transform_operator
    from tools.align.state import RUNTIME

    class Pointer:
        """테스트용 RNA 포인터 값을 반환합니다."""

        def __init__(self, value: int) -> None:
            self.value = value

        def as_pointer(self) -> int:
            """지정된 정수 포인터를 반환합니다."""

            return self.value

    context = SimpleNamespace(
        window=Pointer(11),
        area=Pointer(22),
        preferences=SimpleNamespace(
            inputs=SimpleNamespace(
                mouse_double_click_time=350,
                drag_threshold_mouse=3,
            )
        ),
    )
    event = SimpleNamespace(value="PRESS", mouse_x=100, mouse_y=200)
    timestamps = iter((10.0, 10.2, 20.0, 20.2, 30.0, 30.2))
    original_monotonic = layer_transform_operator.monotonic
    RUNTIME.layer_transform.clear_pivot_click()
    try:
        layer_transform_operator.monotonic = lambda: next(timestamps)
        assert layer_transform_operator._is_viewport_pivot_double_click(context, event) is False
        event.mouse_x = 102
        event.mouse_y = 201
        assert layer_transform_operator._is_viewport_pivot_double_click(context, event) is True

        event.mouse_x = 100
        event.mouse_y = 200
        assert layer_transform_operator._is_viewport_pivot_double_click(context, event) is False
        event.mouse_x = 104
        assert layer_transform_operator._is_viewport_pivot_double_click(context, event) is False

        event.mouse_x = 100
        context.area = Pointer(33)
        assert layer_transform_operator._is_viewport_pivot_double_click(context, event) is False
        context.area = Pointer(22)
        assert layer_transform_operator._is_viewport_pivot_double_click(context, event) is False
    finally:
        layer_transform_operator.monotonic = original_monotonic
        RUNTIME.layer_transform.clear_pivot_click()


def test_committed_layer_transform_persists_but_preview_does_not_restore() -> None:
    """확정 MANUAL alignment는 기존 schema v1 snapshot으로 복원하고 진행 중 preview·선택은 버리는지 확인합니다."""

    from test_project_restore import (
        _drain_restore_timer,
        _origin_runtime,
        _remove_owned_project_texts,
        _write_text_sparse_model,
    )

    from tools.align import importer
    from tools.align.alignment import (
        begin_layer_transform,
        cancel_layer_transform,
        commit_layer_transform,
        create_translation_delta,
        set_layer_transform_preview_delta,
    )
    from tools.align.constants import ROLE_MOVING
    from tools.align.project_persistence.runtime import (
        build_project_state_snapshot,
        schedule_project_restore_if_available,
    )
    from tools.align.project_persistence.store import write_active_project_state
    from tools.align.state import RuntimeState

    _remove_owned_project_texts()
    with TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        first_source = root / "anchor" / "sparse"
        second_source = root / "moving" / "sparse"
        _write_text_sparse_model(first_source, point_offset=0.0)
        _write_text_sparse_model(second_source, point_offset=10.0)
        origin = _origin_runtime(first_source, second_source)
        begin_layer_transform(origin, role=ROLE_MOVING, model_ids=("model-p",))
        set_layer_transform_preview_delta(origin, create_translation_delta((2.0, 0.0, 0.0)))
        commit_layer_transform(origin, action="저장 전 레이어 변환")
        committed = origin.model_repository.alignment_transform_for_model_id("model-p")
        begin_layer_transform(origin, role=ROLE_MOVING, model_ids=("model-p",))
        set_layer_transform_preview_delta(origin, create_translation_delta((99.0, 0.0, 0.0)))
        snapshot = build_project_state_snapshot(origin)
        assert snapshot.models[1].alignment_matrix_float64 == tuple(
            tuple(float(value) for value in row) for row in committed.matrix_float64
        )
        write_active_project_state(snapshot)

        restored = RuntimeState()
        original_prepare_gpu = importer._prepare_gpu_batch_for_model
        try:
            importer._prepare_gpu_batch_for_model = lambda _model_id, _model: None
            assert schedule_project_restore_if_available(restored) is True
            _drain_restore_timer(restored)
        finally:
            importer._prepare_gpu_batch_for_model = original_prepare_gpu
            cancel_layer_transform(origin)

    restored_alignment = restored.model_repository.alignment_transform_for_model_id("model-p")
    np.testing.assert_allclose(
        restored_alignment.matrix_float64,
        committed.matrix_float64,
        rtol=0.0,
        atol=1.0e-12,
    )
    restored_provenance = restored.model_catalog.models["model-p"].alignment_provenance
    assert restored_provenance is not None and restored_provenance.method == "MANUAL"
    assert restored.layer_transform.is_active is False
    assert restored.model_catalog.selected_model_ids == ()
    _remove_owned_project_texts()


def test_preview_blocks_merge_export_start_until_cancelled() -> None:
    """진행 중 preview는 확정 alignment가 아니므로 구조 통합 내보내기를 시작하지 못하는지 확인합니다."""

    from tools.align.alignment import begin_layer_transform, cancel_layer_transform
    from tools.align.constants import ROLE_MOVING
    from tools.align.operators.export_model import merge_export_start_block_reason
    from tools.align.state import RUNTIME

    runtime = _runtime_with_models()
    begin_layer_transform(runtime, role=ROLE_MOVING, model_ids=("moving",))
    fields = (
        "model_repository",
        "model_catalog",
        "marker_selection",
        "marker_alignment",
        "layer_transform",
        "icp_alignment",
        "merge_export",
        "project_restore",
        "session",
        "modal_active",
    )
    previous = {field: getattr(RUNTIME, field) for field in fields}
    try:
        for field in fields:
            setattr(RUNTIME, field, getattr(runtime, field))
        assert merge_export_start_block_reason(require_output_path=False) == (
            "데이터 레이어 변환을 확정하거나 취소한 뒤 구조 통합 내보내기를 시작하십시오."
        )
        cancel_layer_transform(RUNTIME)
        assert merge_export_start_block_reason(require_output_path=False) is None
    finally:
        for field, value in previous.items():
            setattr(RUNTIME, field, value)


def run_all() -> None:
    """레이어 변환 순수 수학·선택·원자성 회귀를 Blender 프로세스에서 실행합니다."""

    _add_test_paths()
    test_data_layer_multiselection_is_catalog_ordered_and_role_derived()
    test_preview_uses_common_world_origin_pivot_and_left_composition()
    test_custom_pivot_tracks_picked_source_point_and_exact_selection()
    test_surface_pivot_uses_visible_selection_and_preserves_source_precision()
    test_surface_pivot_miss_or_gpu_error_preserves_previous_pivot()
    test_commit_cancel_and_session_undo_are_atomic()
    test_session_undo_redo_restores_only_layer_transform_snapshot()
    test_invalid_scale_and_stale_baseline_leave_repository_unchanged()
    test_gizmo_axes_use_distinct_project_world_directions()
    test_gizmo_visibility_toggle_controls_poll_and_panel_order()
    test_viewport_pivot_double_click_uses_blender_time_and_mouse_threshold()
    test_committed_layer_transform_persists_but_preview_does_not_restore()
    test_preview_blocks_merge_export_start_until_cancelled()
    print("test_layer_transform: 통과")


if __name__ == "__main__":
    run_all()
