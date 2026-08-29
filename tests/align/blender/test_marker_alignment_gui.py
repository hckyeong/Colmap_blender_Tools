"""마커 Sim(3) 미리보기의 N 패널·듀얼 뷰 전환·취소 GUI 경계를 검증합니다."""

from __future__ import annotations

import inspect
from pathlib import Path
import sys
import traceback
from types import SimpleNamespace

import bpy
import numpy as np
from mathutils import Quaternion


_STAGE = "OPEN"
_SOURCE_WORKSPACE = None
_A_VIEW_BEFORE_PREVIEW = None
_P_VIEW_BEFORE_PREVIEW = None
_NORMAL_REFERENCE_DRAW_PASSES = None
_NORMAL_MOVING_DRAW_PASSES = None


def _add_source_path() -> None:
    """테스트가 현재 작업트리의 애드온 소스를 import하도록 경로를 추가합니다."""
    source_path = Path(__file__).resolve().parents[3]
    source_text = str(source_path)
    if source_text not in sys.path:
        sys.path.insert(0, source_text)


def _view_snapshot(area: bpy.types.Area) -> tuple[tuple[float, ...], float, tuple[float, ...], str]:
    """P 뷰 취소 복원 비교에 필요한 RegionView3D 값을 복사합니다."""
    region_3d = area.spaces.active.region_3d
    return (
        tuple(float(value) for value in region_3d.view_location),
        float(region_3d.view_distance),
        tuple(float(value) for value in region_3d.view_rotation),
        region_3d.view_perspective,
    )


def _assert_view_snapshot(area: bpy.types.Area, snapshot) -> None:
    """P 뷰의 위치·거리·회전·투영 방식이 미리보기 전 상태와 같은지 확인합니다."""
    location, distance, rotation, perspective = _view_snapshot(area)
    expected_location, expected_distance, expected_rotation, expected_perspective = snapshot
    np.testing.assert_allclose(location, expected_location, rtol=0.0, atol=1.0e-7)
    assert abs(distance - expected_distance) <= 1.0e-7
    assert Quaternion(rotation).rotation_difference(Quaternion(expected_rotation)).angle <= 1.0e-7
    assert perspective == expected_perspective


def _main_window() -> bpy.types.Window:
    """현재 COLMAP Align 전용 작업공간을 표시하는 메인 창을 반환합니다."""
    from tools.align.operators.workspace import find_main_window

    window = find_main_window(bpy.context.window_manager)
    assert window is not None, "COLMAP Align 메인 창을 찾지 못했습니다."
    return window


def _moving_area(window: bpy.types.Window) -> bpy.types.Area:
    """전용 듀얼 뷰의 오른쪽 P Area를 반환합니다."""
    from tools.align.viewport.registry import classify_areas

    _reference_area, moving_area = classify_areas(window.screen.areas)
    return moving_area


def _reference_area(window: bpy.types.Window) -> bpy.types.Area:
    """전용 듀얼 뷰의 왼쪽 A Area를 반환합니다."""
    from tools.align.viewport.registry import classify_areas

    reference_area, _moving_area_value = classify_areas(window.screen.areas)
    return reference_area


def _invoke_in_moving_area(
    window: bpy.types.Window,
    operator_name: str,
    **properties,
) -> set[str]:
    """오른쪽 P 뷰 WINDOW Region override에서 애드온 오퍼레이터를 실행합니다."""
    from tools.align.viewport.registry import find_window_region

    area = _moving_area(window)
    operator = getattr(bpy.ops.colmap_align, operator_name)
    with bpy.context.temp_override(
        window=window,
        screen=window.screen,
        area=area,
        region=find_window_region(area),
    ):
        return operator("EXEC_DEFAULT", **properties)


def _register_three_marker_pairs() -> None:
    """Demo_1 A와 선택 P Demo_3의 비공선 세 대응쌍을 런타임에 등록합니다."""
    from tools.align.constants import DEMO_MODEL_1_ID, DEMO_MODEL_3_ID, ROLE_MOVING, ROLE_REFERENCE
    from tools.align.properties import sync_marker_pair_list
    from tools.align.state import MarkerPair, RUNTIME, SelectedPoint

    reference_model = RUNTIME.model_repository.models[DEMO_MODEL_1_ID]
    moving_model = RUNTIME.model_repository.models[DEMO_MODEL_3_ID]
    state = RUNTIME.marker_selection
    state.clear()
    state.completed_pairs = []
    for pair_id, source_index in enumerate((0, 4, 8), start=1):
        reference_position = reference_model.points.positions[source_index]
        moving_position = moving_model.points.positions[source_index]
        state.completed_pairs.append(
            MarkerPair(
                pair_id=pair_id,
                reference=SelectedPoint(
                    ROLE_REFERENCE,
                    int(reference_model.points.point_ids[source_index]),
                    tuple(float(value) for value in reference_position),
                    model_id=DEMO_MODEL_1_ID,
                ),
                moving=SelectedPoint(
                    ROLE_MOVING,
                    int(moving_model.points.point_ids[source_index]),
                    tuple(float(value) for value in moving_position),
                    model_id=DEMO_MODEL_3_ID,
                ),
            )
        )
    state.active_pair_id = 1
    sync_marker_pair_list(bpy.context.window_manager)


def _assert_triangle_delta_contract() -> None:
    """P 대표 삼각형이 preview alignment가 아닌 delta를 정확히 한 번만 쓰는지 확인합니다."""
    from tools.align.alignment import create_sim3_transform, transform_positions
    from tools.align.viewport.renderer import preview_triangle_vertices

    reference_vertices = np.asarray(
        ((4.0, 1.0, 0.0), (6.0, 1.0, 0.0), (4.0, 3.0, 0.0)),
        dtype=np.float64,
    )
    moving_vertices = np.asarray(
        ((0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0)),
        dtype=np.float64,
    )
    delta_transform = create_sim3_transform(
        np.identity(3, dtype=np.float64),
        2.0,
        np.asarray((4.0, 1.0, 0.0), dtype=np.float64),
    )
    preview_alignment = create_sim3_transform(
        np.identity(3, dtype=np.float64),
        2.0,
        np.asarray((104.0, 1.0, 0.0), dtype=np.float64),
    )
    result = SimpleNamespace(
        delta_transform=delta_transform,
        preview_alignment=preview_alignment,
        representative_triangle=SimpleNamespace(
            reference_vertices=reference_vertices,
            moving_vertices=moving_vertices,
        ),
    )
    displayed_reference, displayed_moving = preview_triangle_vertices(result)
    np.testing.assert_allclose(displayed_reference, reference_vertices, rtol=0.0, atol=1.0e-6)
    np.testing.assert_allclose(
        displayed_moving,
        transform_positions(delta_transform, moving_vertices),
        rtol=0.0,
        atol=1.0e-6,
    )
    assert not np.allclose(
        displayed_moving,
        transform_positions(preview_alignment, moving_vertices),
        rtol=0.0,
        atol=1.0e-6,
    )


def _draw_pass_signature(draw_passes) -> tuple[tuple[object, ...], ...]:
    """렌더러의 점군 제출 계획을 역할·투명도·깊이 정책 비교용으로 정규화합니다."""
    return tuple(
        (
            draw_pass.model_ids,
            draw_pass.role,
            float(draw_pass.opacity),
            bool(draw_pass.depth_write),
            draw_pass.depth_test,
        )
        for draw_pass in draw_passes
    )


def _assert_preview_point_cloud_contract() -> None:
    """GUI 미리보기에서 A는 유지되고 P에는 변환 P와 A Ghost가 함께 제출되는지 확인합니다."""
    from tools.align.constants import ROLE_MOVING, ROLE_REFERENCE
    from tools.align.viewport.renderer import (
        _PREVIEW_GHOST_OPACITY,
        point_cloud_draw_passes_for_role,
    )

    assert _NORMAL_REFERENCE_DRAW_PASSES is not None
    assert _NORMAL_MOVING_DRAW_PASSES is not None
    reference_passes = _draw_pass_signature(point_cloud_draw_passes_for_role(ROLE_REFERENCE))
    moving_passes = _draw_pass_signature(point_cloud_draw_passes_for_role(ROLE_MOVING))
    assert reference_passes == _NORMAL_REFERENCE_DRAW_PASSES
    assert moving_passes[0] == _NORMAL_MOVING_DRAW_PASSES[0]
    assert moving_passes[0][1:] == (ROLE_MOVING, 1.0, True, None)
    assert moving_passes[1] == (
        _NORMAL_REFERENCE_DRAW_PASSES[0][0],
        ROLE_REFERENCE,
        _PREVIEW_GHOST_OPACITY,
        False,
        "NONE",
    )


def _assert_preview_ui_and_locks(window: bpy.types.Window) -> None:
    """3점 마커 정렬 상태의 동적 문구·분리된 Align·위험 입력 잠금을 확인합니다."""
    from tools.align.operators.marker_alignment import marker_preview_button_label
    from tools.align.properties import marker_align_log_list, sync_marker_align_log
    from tools.align.state import RUNTIME
    from tools.align.ui.panels import (
        COLMAPALIGN_PT_marker,
        COLMAPALIGN_UL_marker_align_log,
        marker_align_status_entries,
        wrapped_marker_align_status_entries,
    )

    assert RUNTIME.marker_alignment.is_active is True
    assert RUNTIME.marker_alignment.result is not None
    assert marker_preview_button_label() == "3점 마커 정렬 취소"
    panel_source = inspect.getsource(COLMAPALIGN_PT_marker.draw)
    assert "toggle_marker_preview" in panel_source
    assert "align_marker_preview" in panel_source
    assert "cancel_icp" in panel_source
    assert "discard_icp_preview" in panel_source
    assert "approve_icp_result" in panel_source
    assert "align_action.enabled = align_ready and not icp_locked" in panel_source
    assert "icp_preset" not in panel_source
    assert "icp_scale_refine_enabled" not in panel_source
    assert "icp_small_gicp_operation" not in panel_source
    assert "settings_column" not in panel_source
    assert 'preview_box.label(text="Marker Align", icon="ORIENTATION_GIMBAL")' in panel_source
    assert "marker_action_row = preview_box.row()" in panel_source
    assert 'text="Marker Refine Scale"' in panel_source
    assert "marker_scale_action.use_property_split = False" in panel_source
    assert 'marker_scale_action.alignment = "LEFT"' in panel_source
    assert "toggle=True" not in panel_source
    assert "align_action = preview_box.row()" in panel_source
    assert "COLMAPALIGN_UL_marker_align_log" in panel_source
    assert "rows=max(1, min(6, len(log_entries)))" in panel_source
    log_draw_source = inspect.getsource(COLMAPALIGN_UL_marker_align_log.draw_item)
    assert "layout.scale_y = 0.8" in log_draw_source
    assert "icon=" not in log_draw_source
    assert "Marker Preview" not in panel_source
    assert "마커 결과 적용" not in panel_source
    status_entries = marker_align_status_entries(True, None)
    status_messages = tuple(status_entries)
    assert len(status_messages) == 1
    assert status_messages[0].startswith("RMS ·")
    wrapped_entries = wrapped_marker_align_status_entries(
        SimpleNamespace(
            region=SimpleNamespace(width=280),
            preferences=SimpleNamespace(system=SimpleNamespace(ui_scale=1.0)),
        ),
        True,
        None,
    )
    assert wrapped_entries == status_messages
    sync_marker_align_log(wrapped_entries, bpy.context.window_manager)
    log_list = marker_align_log_list(bpy.context.window_manager)
    assert log_list is not None
    assert len(log_list.items) == len(wrapped_entries)
    assert log_list.active_index == -1
    assert not hasattr(log_list.items[0], "icon_name")
    from tools.align.viewport.renderer import preview_triangle_vertices

    reference_vertices, moving_vertices = preview_triangle_vertices(RUNTIME.marker_alignment.result)
    assert reference_vertices.shape == (3, 3)
    assert moving_vertices.shape == (3, 3)
    _assert_preview_point_cloud_contract()
    with bpy.context.temp_override(
        window=window,
        screen=window.screen,
        area=_moving_area(window),
        region=next(region for region in _moving_area(window).regions if region.type == "WINDOW"),
    ):
        assert bpy.ops.colmap_align.add_marker_pair.poll() is False
        assert bpy.ops.colmap_align.undo_marker_selection.poll() is False
        assert bpy.ops.colmap_align.select_data_layer.poll() is False
        assert bpy.ops.colmap_align.import_model.poll() is False
        assert bpy.ops.colmap_align.align_marker_preview.poll() is True


def _finish(succeeded: bool) -> None:
    """테스트 애드온을 해제하고 성공 marker를 출력한 뒤 Blender를 종료합니다."""
    try:
        from tools.align import registration

        if registration.is_registered():
            registration.unregister()
    except Exception:
        traceback.print_exc()
        succeeded = False
    print(f"MARKER_ALIGNMENT_GUI_TEST_RESULT={'PASS' if succeeded else 'FAIL'}")
    bpy.ops.wm.quit_blender()


def _run_step() -> float | None:
    """듀얼 뷰 준비, preview·취소·Esc 취소와 종료를 단계별로 검증합니다."""
    global _A_VIEW_BEFORE_PREVIEW, _NORMAL_MOVING_DRAW_PASSES
    global _NORMAL_REFERENCE_DRAW_PASSES, _P_VIEW_BEFORE_PREVIEW, _STAGE
    try:
        from tools.align.constants import DEMO_MODEL_3_ID
        from tools.align.operators.workspace import _process_sidebar_activation
        from tools.align.state import RUNTIME

        if _STAGE == "OPEN":
            result = bpy.ops.colmap_align.open_workspace()
            assert "FINISHED" in result
            _STAGE = "WAIT_WORKSPACE"
            return 0.1
        if _STAGE == "WAIT_WORKSPACE":
            if RUNTIME.workspace_setup_task is not None or bpy.app.timers.is_registered(_process_sidebar_activation):
                return 0.1
            assert RUNTIME.session_active is True
            window = _main_window()
            assert "FINISHED" in _invoke_in_moving_area(
                window,
                "select_data_layer",
                model_id=DEMO_MODEL_3_ID,
            )
            _register_three_marker_pairs()
            from tools.align.constants import ROLE_MOVING, ROLE_REFERENCE
            from tools.align.viewport.renderer import point_cloud_draw_passes_for_role

            _NORMAL_REFERENCE_DRAW_PASSES = _draw_pass_signature(
                point_cloud_draw_passes_for_role(ROLE_REFERENCE)
            )
            _NORMAL_MOVING_DRAW_PASSES = _draw_pass_signature(
                point_cloud_draw_passes_for_role(ROLE_MOVING)
            )
            _A_VIEW_BEFORE_PREVIEW = _view_snapshot(_reference_area(window))
            _P_VIEW_BEFORE_PREVIEW = _view_snapshot(_moving_area(window))
            settings = bpy.context.scene.colmap_align_settings
            assert settings.marker_use_uniform_scale is True
            settings.marker_use_uniform_scale = False
            assert "FINISHED" in _invoke_in_moving_area(window, "toggle_marker_preview")
            assert np.isclose(
                RUNTIME.marker_alignment.result.delta_transform.uniform_scale,
                1.0,
                rtol=0.0,
                atol=1.0e-12,
            )
            _assert_view_snapshot(_reference_area(window), _A_VIEW_BEFORE_PREVIEW)
            _assert_view_snapshot(_moving_area(window), _P_VIEW_BEFORE_PREVIEW)
            _assert_triangle_delta_contract()
            _assert_preview_ui_and_locks(window)
            _STAGE = "BUTTON_CANCEL"
            return 0.1
        if _STAGE == "BUTTON_CANCEL":
            window = _main_window()
            assert "FINISHED" in _invoke_in_moving_area(window, "toggle_marker_preview")
            assert RUNTIME.marker_alignment.is_active is False
            _assert_view_snapshot(_moving_area(window), _P_VIEW_BEFORE_PREVIEW)
            from tools.align.constants import ROLE_MOVING, ROLE_REFERENCE
            from tools.align.viewport.renderer import point_cloud_draw_passes_for_role

            assert _draw_pass_signature(
                point_cloud_draw_passes_for_role(ROLE_REFERENCE)
            ) == _NORMAL_REFERENCE_DRAW_PASSES
            assert _draw_pass_signature(
                point_cloud_draw_passes_for_role(ROLE_MOVING)
            ) == _NORMAL_MOVING_DRAW_PASSES
            bpy.context.scene.colmap_align_settings.marker_use_uniform_scale = True
            assert "FINISHED" in _invoke_in_moving_area(window, "toggle_marker_preview")
            assert RUNTIME.marker_alignment.is_active is True
            assert not np.isclose(
                RUNTIME.marker_alignment.result.delta_transform.uniform_scale,
                1.0,
                rtol=0.0,
                atol=1.0e-6,
            )
            _assert_view_snapshot(_reference_area(window), _A_VIEW_BEFORE_PREVIEW)
            _assert_view_snapshot(_moving_area(window), _P_VIEW_BEFORE_PREVIEW)
            _STAGE = "ESC_CANCEL"
            window.event_simulate(type="ESC", value="PRESS")
            window.event_simulate(type="ESC", value="RELEASE")
            return 0.1
        if _STAGE == "ESC_CANCEL":
            window = _main_window()
            assert RUNTIME.marker_alignment.is_active is False
            _assert_view_snapshot(_moving_area(window), _P_VIEW_BEFORE_PREVIEW)
            assert "FINISHED" in _invoke_in_moving_area(window, "exit_session")
            _STAGE = "WAIT_EXIT"
            return 0.1
        if _STAGE == "WAIT_EXIT":
            if RUNTIME.session.phase != "INACTIVE":
                return 0.1
            assert _SOURCE_WORKSPACE is not None
            assert bpy.context.window_manager.windows[0].workspace.as_pointer() == _SOURCE_WORKSPACE.as_pointer()
            _finish(True)
            return None
        raise AssertionError(f"알 수 없는 marker GUI 테스트 단계입니다: {_STAGE}")
    except Exception:
        traceback.print_exc()
        _finish(False)
    return None


def schedule_gui_test() -> None:
    """GUI Blender에서 소스 애드온과 실제 Demo 저장소를 준비해 검증 타이머를 예약합니다."""
    global _SOURCE_WORKSPACE
    _add_source_path()
    if bpy.app.background:
        print("MARKER_ALIGNMENT_GUI_TEST_RESULT=PASS")
        return
    from tools.align import registration
    from tools.align.operators.demo_data import install_demo_fixture

    registration.register()
    install_demo_fixture(point_count=12)
    _SOURCE_WORKSPACE = bpy.context.window.workspace
    bpy.app.timers.register(_run_step, first_interval=0.2)


if __name__ == "__main__":
    try:
        schedule_gui_test()
    except Exception:
        traceback.print_exc()
        _finish(False)
