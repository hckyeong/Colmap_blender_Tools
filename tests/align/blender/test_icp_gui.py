"""ICP Marker Align UI phase, 잠금과 viewport 미리보기를 GUI Blender에서 검증합니다."""

from __future__ import annotations

import inspect
from pathlib import Path
import sys
import traceback

import bpy
import numpy as np


_STAGE = "OPEN"
_SOURCE_WORKSPACE = None
_REFERENCE_VIEW_SNAPSHOT = None
_MOVING_VIEW_SNAPSHOT = None


def _add_test_paths() -> None:
    """애드온 소스와 공용 GUI·ICP fixture 모듈을 import 경로에 추가합니다."""
    blender_test_path = Path(__file__).resolve().parent
    source_path = blender_test_path.parents[2]
    for path in (source_path, blender_test_path):
        path_text = str(path)
        if path_text not in sys.path:
            sys.path.insert(0, path_text)


def _apply_matrix(matrix: np.ndarray, positions: np.ndarray) -> np.ndarray:
    """column-vector 4×4 행렬을 N×3 검증 좌표에 한 번 적용합니다."""
    return positions @ matrix[:3, :3].T + matrix[:3, 3]


def _assert_dual_view_snapshots(window: bpy.types.Window) -> None:
    """ICP 상태 전이 뒤에도 A/P 뷰포트 시점이 바뀌지 않았는지 확인합니다."""
    from test_marker_alignment_gui import (
        _assert_view_snapshot,
        _moving_area,
        _reference_area,
    )

    assert _REFERENCE_VIEW_SNAPSHOT is not None
    assert _MOVING_VIEW_SNAPSHOT is not None
    _assert_view_snapshot(_reference_area(window), _REFERENCE_VIEW_SNAPSHOT)
    _assert_view_snapshot(_moving_area(window), _MOVING_VIEW_SNAPSHOT)


def _assert_icp_poll_timer_lifecycle() -> None:
    """같은 ICP 실행에서 poll timer가 한 번만 등록되고 lifecycle 해제로 사라지는지 확인합니다."""
    from tools.align.operators.marker_alignment import (
        _register_icp_poll_timer,
        stop_icp_poll_timer_for_lifecycle,
    )
    from tools.align.state import RUNTIME

    stop_icp_poll_timer_for_lifecycle()
    _register_icp_poll_timer()
    callback = RUNTIME.icp_timer
    assert callable(callback)
    assert bpy.app.timers.is_registered(callback)
    _register_icp_poll_timer()
    assert RUNTIME.icp_timer is callback
    assert bpy.app.timers.is_registered(callback)
    stop_icp_poll_timer_for_lifecycle()
    assert RUNTIME.icp_timer is None
    assert not bpy.app.timers.is_registered(callback)


def _assert_direct_align_ready(window: bpy.types.Window) -> None:
    """마커 미리보기 전에도 현재 A/P 선택으로 자동 Align이 활성화되는지 확인합니다."""

    from tools.align.operators.marker_alignment import (
        align_start_block_reason,
        direct_align_start_block_reason,
        marker_preview_start_block_reason,
    )
    from tools.align.ui.panels import COLMAPALIGN_PT_marker, marker_align_status_entries
    from tools.align.state import RUNTIME
    from test_marker_alignment_gui import _moving_area

    assert RUNTIME.model_catalog.selected_reference_model_ids
    assert RUNTIME.model_catalog.selected_moving_model_id is not None
    assert direct_align_start_block_reason() is None
    assert marker_preview_start_block_reason() is not None
    assert align_start_block_reason(bpy.context) is None
    assert "직접 Align · A 그룹 전체 2개를 기준으로 현재 P 위치를 초기값으로 사용합니다." in marker_align_status_entries(
        False,
        marker_preview_start_block_reason(),
        True,
    )
    panel_source = inspect.getsource(COLMAPALIGN_PT_marker.draw)
    assert "preview_active or direct_align_ready" in panel_source
    moving_area = _moving_area(window)
    with bpy.context.temp_override(
        window=window,
        screen=window.screen,
        area=moving_area,
        region=next(region for region in moving_area.regions if region.type == "WINDOW"),
    ):
        assert bpy.ops.colmap_align.align_marker_preview.poll() is True
    _assert_automatic_precise_align_uses_current_position_without_preview(window)


def _assert_automatic_precise_align_uses_current_position_without_preview(window: bpy.types.Window) -> None:
    """Align이 UI 저장값과 무관하게 정밀 Refine 입력을 넘기는지 확인합니다."""

    import colmap_align.operators.marker_alignment as marker_operator_module
    from tools.align.alignment import create_icp_input_snapshot
    from tools.align.operators.marker_alignment import align_start_block_reason
    from tools.align.state import RUNTIME
    from test_marker_alignment_gui import _moving_area

    settings = bpy.context.scene.colmap_align_settings
    settings.icp_preset = "FAST"
    settings.icp_small_gicp_operation = "SMALL_GICP"
    settings.icp_scale_refine_enabled = False
    assert RUNTIME.marker_alignment.is_active is False
    assert align_start_block_reason(bpy.context) is None
    captured: dict[str, object] = {}
    original_begin_icp_job = marker_operator_module.begin_icp_job

    def _capture_begin_icp_job(runtime, **kwargs):
        """실제 worker 대신 Align이 전달한 초기값 계약을 불변 입력으로 검증합니다."""

        captured.update(kwargs)
        captured["input_data"] = create_icp_input_snapshot(
            runtime,
            preset=str(kwargs["preset"]),
            scale_refine_enabled=bool(kwargs["scale_refine_enabled"]),
        )
        return object()

    marker_operator_module.begin_icp_job = _capture_begin_icp_job
    try:
        with bpy.context.temp_override(
            window=window,
            screen=window.screen,
            area=_moving_area(window),
            region=next(region for region in _moving_area(window).regions if region.type == "WINDOW"),
        ):
            assert bpy.ops.colmap_align.align_marker_preview.poll() is True
            assert "FINISHED" in bpy.ops.colmap_align.align_marker_preview()
    finally:
        marker_operator_module.stop_icp_poll_timer_for_lifecycle()
        marker_operator_module.begin_icp_job = original_begin_icp_job
    assert captured["scale_refine_enabled"] is True
    assert captured["preset"] == "PRECISE"
    assert captured["solver_backend"] == "SMALL_GICP"
    assert "marker_alignment_required" not in captured
    assert captured["input_data"].seed.initialization_mode == "CURRENT_ALIGNMENT"
    assert captured["input_data"].seed.marker_result is None
    assert RUNTIME.marker_alignment.is_active is False
    settings.icp_preset = "BALANCED"


def _assert_running_ui_and_viewport(window: bpy.types.Window) -> None:
    """실행 중 두 버튼·입력 잠금·양쪽 점군 Ghost·진행 로그를 확인합니다."""
    from tools.align.alignment import effective_alignment_for_model
    from tools.align.constants import ROLE_MOVING, ROLE_REFERENCE
    from tools.align.operators.marker_alignment import (
        is_icp_input_locked,
        is_icp_running,
    )
    from tools.align.state import RUNTIME
    from tools.align.ui.panels import marker_align_status_entries
    from tools.align.viewport.renderer import point_cloud_draw_passes_for_role
    from test_marker_alignment_gui import _moving_area

    assert is_icp_running() is True
    assert is_icp_input_locked() is True
    state = RUNTIME.icp_alignment
    assert state.input_data is not None
    assert state.working_alignment is not None
    assert effective_alignment_for_model(
        RUNTIME,
        state.input_data.seed.moving_model_id,
    ).fingerprint == state.working_alignment.fingerprint
    entries = marker_align_status_entries(True, None)
    assert entries == ("중간 해상도 Point-to-Plane을 계산하고 있습니다.",)
    _assert_dual_view_snapshots(window)

    reference_passes = point_cloud_draw_passes_for_role(ROLE_REFERENCE)
    moving_passes = point_cloud_draw_passes_for_role(ROLE_MOVING)
    assert len(reference_passes) == 2
    assert reference_passes[0].role == ROLE_REFERENCE
    assert reference_passes[1].role == ROLE_MOVING
    assert reference_passes[1].depth_write is False
    assert len(moving_passes) == 2
    assert moving_passes[0].role == ROLE_MOVING
    assert moving_passes[1].role == ROLE_REFERENCE
    assert moving_passes[1].depth_write is False

    with bpy.context.temp_override(
        window=window,
        screen=window.screen,
        area=_moving_area(window),
        region=next(region for region in _moving_area(window).regions if region.type == "WINDOW"),
    ):
        assert bpy.ops.colmap_align.toggle_marker_preview.poll() is False
        assert bpy.ops.colmap_align.align_marker_preview.poll() is False
        assert bpy.ops.colmap_align.cancel_icp.poll() is True
        assert bpy.ops.colmap_align.add_marker_pair.poll() is False
        assert bpy.ops.colmap_align.undo_marker_selection.poll() is False
        assert bpy.ops.colmap_align.select_data_layer.poll() is False
        assert bpy.ops.colmap_align.import_model.poll() is False

def _assert_result_preview_ui_and_viewport(
    window: bpy.types.Window,
    result,
    *,
    quality_status: str,
) -> None:
    """PASS·REVIEW 결과 로그·승인 버튼·candidate 삼각형 행렬을 확인합니다."""
    from tools.align.operators.marker_alignment import (
        is_icp_result_approval_available,
        is_icp_result_preview_active,
    )
    from tools.align.state import RUNTIME
    from tools.align.ui.panels import COLMAPALIGN_PT_marker, marker_align_status_entries
    from tools.align.viewport.renderer import (
        _marker_preview_triangle_result,
        preview_triangle_vertices,
    )
    from test_marker_alignment_gui import _moving_area

    assert is_icp_result_preview_active() is True
    assert is_icp_result_approval_available() is True
    entries = marker_align_status_entries(True, None)
    assert entries == (
        "Raw RMS · 0.05677",
        "Weighted RMS · 0.05234",
        "잔차 · 중앙값 0.03912 · P90 0.08765",
        "평가 대응점 · 4 · 낮은 가중치 1",
        "고립 후보 점 · P 2 · A 3",
        "거리 단위 · Project World 좌표 단위",
    )

    panel_source = inspect.getsource(COLMAPALIGN_PT_marker.draw)
    assert "marker_action_row.enabled = False" in panel_source
    assert "cancel_icp" in panel_source
    assert "discard_icp_preview" in panel_source
    assert "approve_icp_result" in panel_source
    assert "approve_action.enabled = is_icp_result_approval_available()" in panel_source
    assert "icp_preset" not in panel_source
    assert "icp_solver_backend" not in panel_source
    assert "configured_open3d_python_path" not in panel_source
    assert "Small GICP · 강체 정렬 2회 · Scale Locked" not in panel_source
    assert "외부 ICP Python · 설정됨" not in panel_source
    assert "marker_use_uniform_scale" in panel_source
    assert 'text="Marker Refine Scale"' in panel_source
    assert "marker_scale_action.use_property_split = False" in panel_source
    assert 'marker_scale_action.alignment = "LEFT"' in panel_source
    assert "toggle=True" not in panel_source
    assert "marker_action_row = preview_box.row()" in panel_source
    assert "align_action = preview_box.row()" in panel_source
    assert "icp_scale_refine_enabled" not in panel_source
    assert "icp_small_gicp_operation" not in panel_source
    assert "settings_column" not in panel_source
    assert "and not export_locked" in panel_source

    marker_result = RUNTIME.marker_alignment.result
    assert marker_result is not None
    assert _marker_preview_triangle_result() is None
    reference_vertices, moving_vertices = preview_triangle_vertices(marker_result)
    source_vertices = np.asarray(
        marker_result.representative_triangle.moving_vertices,
        dtype=np.float64,
    )
    relative = (
        result.final_alignment.matrix_float64
        @ np.linalg.inv(marker_result.base_alignment.matrix_float64)
    )
    expected_vertices = _apply_matrix(relative, source_vertices)
    np.testing.assert_allclose(moving_vertices, expected_vertices, rtol=0.0, atol=1.0e-6)
    assert not np.allclose(
        moving_vertices,
        _apply_matrix(relative, expected_vertices),
        rtol=0.0,
        atol=1.0e-6,
    )
    assert reference_vertices.shape == (3, 3)
    _assert_dual_view_snapshots(window)

    with bpy.context.temp_override(
        window=window,
        screen=window.screen,
        area=_moving_area(window),
        region=next(region for region in _moving_area(window).regions if region.type == "WINDOW"),
    ):
        assert bpy.ops.colmap_align.discard_icp_preview.poll() is True
        assert bpy.ops.colmap_align.approve_icp_result.poll() is True


def _assert_failed_result_ui(window: bpy.types.Window) -> None:
    """수치적으로 무효한 FAILED 결과에는 preview·승인이 남지 않는지 확인합니다."""
    from tools.align.operators.marker_alignment import is_icp_result_preview_active
    from tools.align.constants import ICP_ALIGNMENT_PHASE_FAILED
    from tools.align.state import RUNTIME
    from tools.align.ui.panels import marker_align_status_entries
    from test_marker_alignment_gui import _moving_area

    state = RUNTIME.icp_alignment
    state.clear(error_message="ICP 결과가 수치적으로 유효하지 않아 미리보기를 만들지 않았습니다.")
    state.phase = ICP_ALIGNMENT_PHASE_FAILED
    assert RUNTIME.marker_alignment.is_active is True
    assert is_icp_result_preview_active() is False
    assert any("수치적으로 유효하지" in entry for entry in marker_align_status_entries(True, None))
    _assert_dual_view_snapshots(window)
    with bpy.context.temp_override(
        window=window,
        screen=window.screen,
        area=_moving_area(window),
        region=next(region for region in _moving_area(window).regions if region.type == "WINDOW"),
    ):
        assert bpy.ops.colmap_align.discard_icp_preview.poll() is False
        assert bpy.ops.colmap_align.approve_icp_result.poll() is False
        assert bpy.ops.colmap_align.align_marker_preview.poll() is True


def _finish(succeeded: bool) -> None:
    """애드온을 해제하고 GUI runner가 읽을 성공 marker를 출력한 뒤 Blender를 종료합니다."""
    try:
        from tools.align import registration

        if registration.is_registered():
            registration.unregister()
    except Exception:
        traceback.print_exc()
        succeeded = False
    print(f"ICP_GUI_TEST_RESULT={'PASS' if succeeded else 'FAIL'}")
    bpy.ops.wm.quit_blender()


def _run_step() -> float | None:
    """workspace 준비부터 실행·결과·버리기·세션 종료를 타이머 단계로 검증합니다."""
    global _MOVING_VIEW_SNAPSHOT, _REFERENCE_VIEW_SNAPSHOT, _STAGE
    try:
        from tools.align.alignment import IcpProgressState, create_icp_input_snapshot
        from tools.align.constants import (
            DEMO_MODEL_1_ID,
            DEMO_MODEL_2_ID,
            DEMO_MODEL_3_ID,
            ICP_ALIGNMENT_PHASE_FAILED,
            ICP_ALIGNMENT_PHASE_IDLE,
            ICP_ALIGNMENT_PHASE_PREVIEW,
            ICP_ALIGNMENT_PHASE_RUNNING,
        )
        from tools.align.operators.workspace import _process_sidebar_activation
        from tools.align.state import RUNTIME
        from test_icp_integration import _preview_result_for_input
        from test_marker_alignment_gui import (
            _invoke_in_moving_area,
            _main_window,
            _moving_area,
            _reference_area,
            _register_three_marker_pairs,
            _view_snapshot,
        )

        if _STAGE == "OPEN":
            assert "FINISHED" in bpy.ops.colmap_align.open_workspace()
            _STAGE = "WAIT_WORKSPACE"
            return 0.1
        if _STAGE == "WAIT_WORKSPACE":
            if RUNTIME.workspace_setup_task is not None or bpy.app.timers.is_registered(
                _process_sidebar_activation
            ):
                return 0.1
            window = _main_window()
            assert "FINISHED" in _invoke_in_moving_area(
                window,
                "select_data_layer",
                model_id=DEMO_MODEL_3_ID,
            )
            assert "FINISHED" in _invoke_in_moving_area(
                window,
                "select_data_layer",
                model_id=DEMO_MODEL_1_ID,
                extend_selection=True,
            )
            _assert_direct_align_ready(window)
            assert "FINISHED" in _invoke_in_moving_area(
                window,
                "select_data_layer",
                model_id=DEMO_MODEL_2_ID,
                extend_selection=True,
            )
            assert RUNTIME.model_catalog.selected_reference_model_ids == (
                DEMO_MODEL_1_ID,
                DEMO_MODEL_2_ID,
            )
            assert RUNTIME.model_catalog.selected_reference_model_id is None
            _assert_direct_align_ready(window)
            assert "FINISHED" in _invoke_in_moving_area(
                window,
                "select_data_layer",
                model_id=DEMO_MODEL_3_ID,
            )
            _register_three_marker_pairs()
            settings = bpy.context.scene.colmap_align_settings
            assert settings.icp_preset == "BALANCED"
            assert settings.icp_scale_refine_enabled is False
            assert "FINISHED" in _invoke_in_moving_area(window, "toggle_marker_preview")
            assert RUNTIME.marker_alignment.is_active is True
            _REFERENCE_VIEW_SNAPSHOT = _view_snapshot(_reference_area(window))
            _MOVING_VIEW_SNAPSHOT = _view_snapshot(_moving_area(window))

            input_data = create_icp_input_snapshot(
                RUNTIME,
                preset=settings.icp_preset,
                scale_refine_enabled=settings.icp_scale_refine_enabled,
            )
            state = RUNTIME.icp_alignment
            state.phase = ICP_ALIGNMENT_PHASE_RUNNING
            state.input_data = input_data
            marker_result = RUNTIME.marker_alignment.result
            assert marker_result is not None
            marker_preview_alignment = marker_result.preview_alignment
            state.progress = IcpProgressState(
                stage="ICP_MEDIUM_POINT_TO_PLANE",
                level_name="medium",
                iteration=8,
                progress=0.46,
                message="중간 해상도 Point-to-Plane을 계산하고 있습니다.",
                stable_alignment=marker_preview_alignment,
                max_iterations=15,
                objective_kind="POINT_TO_PLANE",
                maximum_correspondence_distance=0.0672,
                correspondence_count=29_910,
                point_to_point_rmse=0.05677,
                point_to_plane_rmse=0.02092,
                normal_valid_count=37_042,
                normal_angle_pass_count=34_992,
                normal_radius=0.0310,
                moving_sample_count=143_208,
                reference_sample_count=281_744,
            )
            state.working_alignment = marker_preview_alignment
            _assert_icp_poll_timer_lifecycle()
            _assert_running_ui_and_viewport(window)
            _STAGE = "RESULT_PREVIEW"
            return 0.1
        if _STAGE == "RESULT_PREVIEW":
            window = _main_window()
            state = RUNTIME.icp_alignment
            assert state.input_data is not None
            result = _preview_result_for_input(
                RUNTIME,
                state.input_data,
                quality_status="REVIEW",
            )
            state.phase = ICP_ALIGNMENT_PHASE_PREVIEW
            state.progress = IcpProgressState(
                stage="COMPLETED_REVIEW",
                level_name="medium",
                iteration=4,
                progress=1.0,
                message="UI에 원문 완료 메시지를 표시하지 않습니다.",
            )
            state.result = result
            state.working_alignment = result.final_alignment
            _assert_result_preview_ui_and_viewport(window, result, quality_status="REVIEW")
            assert "FINISHED" in _invoke_in_moving_area(window, "discard_icp_preview")
            assert state.phase == ICP_ALIGNMENT_PHASE_IDLE
            assert state.result is None
            assert RUNTIME.marker_alignment.is_active is True
            _assert_dual_view_snapshots(window)
            _assert_failed_result_ui(window)
            assert state.phase == ICP_ALIGNMENT_PHASE_FAILED
            settings = bpy.context.scene.colmap_align_settings
            input_data = create_icp_input_snapshot(
                RUNTIME,
                preset=settings.icp_preset,
                scale_refine_enabled=settings.icp_scale_refine_enabled,
            )
            result = _preview_result_for_input(
                RUNTIME,
                input_data,
                quality_status="PASS",
            )
            state.phase = ICP_ALIGNMENT_PHASE_PREVIEW
            state.input_data = input_data
            state.result = result
            state.working_alignment = result.final_alignment
            _assert_result_preview_ui_and_viewport(window, result, quality_status="PASS")
            assert "FINISHED" in _invoke_in_moving_area(window, "discard_icp_preview")
            assert state.phase == ICP_ALIGNMENT_PHASE_IDLE
            assert RUNTIME.marker_alignment.is_active is True
            _assert_dual_view_snapshots(window)
            input_data = create_icp_input_snapshot(
                RUNTIME,
                preset=settings.icp_preset,
                scale_refine_enabled=settings.icp_scale_refine_enabled,
            )
            result = _preview_result_for_input(
                RUNTIME,
                input_data,
                quality_status="REVIEW",
            )
            state.phase = ICP_ALIGNMENT_PHASE_PREVIEW
            state.input_data = input_data
            state.result = result
            state.working_alignment = result.final_alignment
            _assert_result_preview_ui_and_viewport(window, result, quality_status="REVIEW")
            pair_list = bpy.context.window_manager.colmap_align_marker_pairs
            assert len(RUNTIME.marker_selection.completed_pairs) == 3
            assert len(pair_list.items) == 3
            assert "FINISHED" in _invoke_in_moving_area(window, "approve_icp_result")
            assert RUNTIME.marker_selection.completed_pairs == []
            assert len(pair_list.items) == 0
            assert RUNTIME.marker_alignment.is_active is False
            _assert_dual_view_snapshots(window)
            _STAGE = "EXIT"
            return 0.1
        if _STAGE == "EXIT":
            window = _main_window()
            if RUNTIME.marker_alignment.is_active:
                assert "FINISHED" in _invoke_in_moving_area(window, "toggle_marker_preview")
            assert "FINISHED" in _invoke_in_moving_area(window, "exit_session")
            _STAGE = "WAIT_EXIT"
            return 0.1
        if _STAGE == "WAIT_EXIT":
            if RUNTIME.session.phase != "INACTIVE":
                return 0.1
            _finish(True)
            return None
        raise AssertionError(f"알 수 없는 ICP GUI 테스트 단계입니다: {_STAGE}")
    except Exception:
        traceback.print_exc()
        _finish(False)
    return None


def schedule_gui_test() -> None:
    """GUI Blender에서 소스 애드온과 Demo fixture를 준비해 phase 검증을 예약합니다."""
    global _SOURCE_WORKSPACE
    _add_test_paths()
    if bpy.app.background:
        print("ICP_GUI_TEST_RESULT=PASS")
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
