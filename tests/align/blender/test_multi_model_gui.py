"""Blender GUI에서 Data Layers 실제 클릭·양방향 그룹 이동·표시 토글을 검증합니다."""

from __future__ import annotations

import sys
import traceback
from pathlib import Path

import bpy


_STAGE = "OPEN"
_SCENE_DATA_SNAPSHOT = None


def _add_source_path() -> None:
    """애드온 소스 디렉터리를 테스트 모듈 탐색 경로에 추가합니다."""
    source_path = Path(__file__).resolve().parents[3]
    source_text = str(source_path)
    if source_text not in sys.path:
        sys.path.insert(0, source_text)


def _scene_data_snapshot() -> tuple[frozenset[int], frozenset[int]]:
    """Object·Collection 생성 회귀를 검사할 현재 포인터 집합을 기록합니다."""
    return (
        frozenset(item.as_pointer() for item in bpy.data.objects),
        frozenset(item.as_pointer() for item in bpy.data.collections),
    )


def _main_window():
    """현재 활성화된 COLMAP Align 메인 Window를 반환합니다."""
    from tools.align.operators.workspace import find_main_window

    window = find_main_window(bpy.context.window_manager)
    assert window is not None, "COLMAP Align 메인 Window를 찾지 못했습니다."
    return window


def _window_region(area: bpy.types.Area):
    """오퍼레이터와 렌더러 override에 사용할 Area WINDOW Region을 반환합니다."""
    region = next((item for item in area.regions if item.type == "WINDOW"), None)
    assert region is not None, "VIEW_3D Area에 WINDOW Region이 없습니다."
    return region


def _select_data_layer_from_panel(
    window: bpy.types.Window,
    area: bpy.types.Area,
    model_id: str,
    *,
    extend_selection: bool = False,
    range_selection: bool = False,
) -> None:
    """Data Layers 행과 같은 Area에서 일반·Ctrl·Shift 선택을 실행합니다."""
    with bpy.context.temp_override(**_operator_override(window, area)):
        result = bpy.ops.colmap_align.select_data_layer(
            model_id=model_id,
            extend_selection=extend_selection,
            range_selection=range_selection,
        )
    assert "FINISHED" in result


def _toggle_data_layer_group_from_panel(
    window: bpy.types.Window,
    area: bpy.types.Area,
    model_id: str,
) -> None:
    """Data Layers 행의 더블클릭과 같은 그룹 전환 오퍼레이터를 실행합니다."""
    with bpy.context.temp_override(**_operator_override(window, area)):
        result = bpy.ops.colmap_align.toggle_data_layer_group(model_id=model_id)
    assert "FINISHED" in result


def _simulate_shortcut(window: bpy.types.Window, event_type: str) -> None:
    """공개 Window 이벤트 API로 세션 입력 가드에 키보드 입력을 보냅니다."""
    assert bpy.app.use_event_simulate is True, "--enable-event-simulate가 필요합니다."
    window.event_simulate(type=event_type, value="PRESS")
    window.event_simulate(type=event_type, value="RELEASE")


def _operator_override(window, moving_area):
    """Data Layers 오퍼레이터 호출에 사용할 오른쪽 Area override 인자를 반환합니다."""
    return {
        "window": window,
        "screen": window.screen,
        "area": moving_area,
        "region": _window_region(moving_area),
    }


def _assert_data_layer_row_operator_binding() -> None:
    """Data Layers 행의 선택·더블클릭 그룹 전환 연결 코드를 확인합니다."""
    from tools.align.ui import panels
    from tools.align.operators import model_group

    source_text = Path(panels.__file__).read_text(encoding="utf-8")
    operator_source = Path(model_group.__file__).read_text(encoding="utf-8")
    assert '"colmap_align.select_data_layer"' in source_text
    assert "operator.model_id = model.model_id" in source_text
    assert "_is_layer_double_click(context, event, self.model_id)" in operator_source


def _finish(succeeded: bool) -> None:
    """애드온을 해제하고 GUI 검증 결과를 출력한 뒤 Blender를 종료합니다."""
    try:
        from tools.align import registration

        registration.unregister()
    except Exception:
        traceback.print_exc()
        succeeded = False
    finally:
        print(f"MULTI_MODEL_GUI_TEST_RESULT={'PASS' if succeeded else 'FAIL'}")
        bpy.ops.wm.quit_blender()


def _run_step() -> float | None:
    """실제 N 패널 클릭과 렌더 결과를 단계적으로 검사합니다."""
    global _STAGE, _SCENE_DATA_SNAPSHOT
    try:
        from tools.align.constants import (
            DEMO_MODEL_1_ID,
            DEMO_MODEL_2_ID,
            DEMO_MODEL_3_ID,
            DEMO_MODEL_4_ID,
            MODEL_STATUS_PENDING,
            MODEL_STATUS_REGISTERED,
            ROLE_MOVING,
            ROLE_REFERENCE,
        )
        from tools.align.session import redo_session_state, undo_session_state
        from tools.align.state import RUNTIME
        from tools.align.viewport.picking import marker_source_model_id
        from tools.align.viewport.registry import classify_areas
        from tools.align.viewport.renderer import (
            ensure_gpu_batches,
            point_cloud_draw_passes_for_role,
            render_model_ids_for_role,
        )

        if RUNTIME.workspace_setup_task is not None:
            return 0.1
        if RUNTIME.workspace_setup_error:
            raise AssertionError(RUNTIME.workspace_setup_error)

        if _STAGE == "OPEN":
            _SCENE_DATA_SNAPSHOT = _scene_data_snapshot()
            assert "FINISHED" in bpy.ops.colmap_align.open_workspace()
            _STAGE = "VERIFY_OPEN"
            return 0.1

        if _STAGE == "EXIT":
            if RUNTIME.session.phase != "INACTIVE":
                return 0.1
            assert _scene_data_snapshot() == _SCENE_DATA_SNAPSHOT
            assert RUNTIME.last_data_layer_click_model_id is None
            _finish(True)
            return None

        window = _main_window()
        reference_area, moving_area = classify_areas(window.screen.areas)
        assert reference_area is not None and moving_area is not None

        if _STAGE == "VERIFY_OPEN":
            from tools.align.operators.workspace import _process_sidebar_activation

            if bpy.app.timers.is_registered(_process_sidebar_activation):
                return 0.1
            assert RUNTIME.model_catalog.model_count == 4
            assert tuple(model.model_id for model in RUNTIME.model_catalog.active_models()) == (
                DEMO_MODEL_1_ID,
                DEMO_MODEL_2_ID,
            )
            assert tuple(model.model_id for model in RUNTIME.model_catalog.inactive_models()) == (
                DEMO_MODEL_3_ID,
                DEMO_MODEL_4_ID,
            )
            assert RUNTIME.model_catalog.selected_model_id is None
            assert all(model.is_visible for model in RUNTIME.model_catalog.ordered_models())
            ensure_gpu_batches()
            assert render_model_ids_for_role(ROLE_REFERENCE) == (
                DEMO_MODEL_1_ID,
                DEMO_MODEL_2_ID,
            )
            assert render_model_ids_for_role(ROLE_MOVING) == ()
            assert _scene_data_snapshot() == _SCENE_DATA_SNAPSHOT
            assert bpy.app.use_event_simulate is True, "--enable-event-simulate가 필요합니다."
            _assert_data_layer_row_operator_binding()
            _simulate_shortcut(window, "DEL")
            _STAGE = "VERIFY_EMPTY_DELETE_BLOCKED"
            return 0.1

        if _STAGE == "VERIFY_EMPTY_DELETE_BLOCKED":
            assert RUNTIME.model_catalog.model_count == 4
            assert RUNTIME.model_catalog.selected_model_id is None
            assert RUNTIME.model_catalog.message == "삭제할 데이터 레이어를 먼저 선택하십시오."
            assert _scene_data_snapshot() == _SCENE_DATA_SNAPSHOT
            _select_data_layer_from_panel(window, moving_area, DEMO_MODEL_3_ID)
            _STAGE = "WAIT_P_SELECTED"
            return 0.1

        if _STAGE == "WAIT_P_SELECTED":
            assert RUNTIME.model_catalog.selected_model_id == DEMO_MODEL_3_ID
            assert RUNTIME.model_catalog.selected_inactive_model_id == DEMO_MODEL_3_ID
            assert render_model_ids_for_role(ROLE_MOVING) == (DEMO_MODEL_3_ID,)
            assert marker_source_model_id(ROLE_MOVING) == DEMO_MODEL_3_ID
            assert bpy.ops.colmap_align.add_marker_pair.poll() is True
            _select_data_layer_from_panel(
                window,
                moving_area,
                DEMO_MODEL_2_ID,
                extend_selection=True,
            )
            _STAGE = "WAIT_A_AND_P_SELECTED"
            return 0.1

        if _STAGE == "WAIT_A_AND_P_SELECTED":
            # 복수 선택의 활성 레이어는 마지막으로 클릭한 A 레이어입니다.
            assert RUNTIME.model_catalog.selected_model_id == DEMO_MODEL_2_ID
            assert RUNTIME.model_catalog.selected_reference_model_id == DEMO_MODEL_2_ID
            assert RUNTIME.model_catalog.selected_inactive_model_id == DEMO_MODEL_3_ID
            assert render_model_ids_for_role(ROLE_MOVING) == (DEMO_MODEL_3_ID,)
            for role in (ROLE_REFERENCE, ROLE_MOVING):
                passes = point_cloud_draw_passes_for_role(role)
                assert tuple(draw_pass.role for draw_pass in passes) == (
                    ROLE_REFERENCE,
                    ROLE_MOVING,
                )
                assert all(draw_pass.use_colmap_rgb is False for draw_pass in passes)
            _select_data_layer_from_panel(
                window,
                moving_area,
                DEMO_MODEL_4_ID,
                range_selection=True,
            )
            _STAGE = "WAIT_THREE_LAYERS_SELECTED"
            return 0.1

        if _STAGE == "WAIT_THREE_LAYERS_SELECTED":
            # 세 개 이상 선택해도 양쪽 뷰가 선택된 A/P 전체를 복수 데이터로 합성합니다.
            assert RUNTIME.model_catalog.selected_model_ids == (
                DEMO_MODEL_2_ID,
                DEMO_MODEL_3_ID,
                DEMO_MODEL_4_ID,
            )
            assert RUNTIME.model_catalog.selected_reference_model_id == DEMO_MODEL_2_ID
            assert RUNTIME.model_catalog.selected_moving_model_id is None
            for role in (ROLE_REFERENCE, ROLE_MOVING):
                passes = point_cloud_draw_passes_for_role(role)
                assert tuple((draw_pass.role, draw_pass.model_ids) for draw_pass in passes) == (
                    (ROLE_REFERENCE, (DEMO_MODEL_2_ID,)),
                    (ROLE_MOVING, (DEMO_MODEL_3_ID, DEMO_MODEL_4_ID)),
                )
            _select_data_layer_from_panel(window, moving_area, DEMO_MODEL_3_ID)
            assert RUNTIME.model_catalog.selected_reference_model_id is None
            # 단일 일반 클릭은 P 레이어 하나를 남겨 단일 P 파생 선택으로 만듭니다.
            assert RUNTIME.model_catalog.selected_moving_model_id == DEMO_MODEL_3_ID
            assert RUNTIME.model_catalog.selected_inactive_model_id == DEMO_MODEL_3_ID
            _toggle_data_layer_group_from_panel(window, moving_area, DEMO_MODEL_3_ID)
            _STAGE = "WAIT_P_TO_A"
            return 0.1

        if _STAGE == "WAIT_P_TO_A":
            assert RUNTIME.model_catalog.models[DEMO_MODEL_3_ID].registration_status == MODEL_STATUS_REGISTERED
            assert RUNTIME.model_catalog.selected_model_id == DEMO_MODEL_3_ID
            assert RUNTIME.model_catalog.selected_inactive_model_id is None
            assert render_model_ids_for_role(ROLE_REFERENCE) == (
                DEMO_MODEL_1_ID,
                DEMO_MODEL_2_ID,
                DEMO_MODEL_3_ID,
            )
            assert render_model_ids_for_role(ROLE_MOVING) == (DEMO_MODEL_4_ID,)
            assert RUNTIME.undo_journal.undo_count == 1
            _select_data_layer_from_panel(window, moving_area, DEMO_MODEL_3_ID)
            _STAGE = "WAIT_A_SELECTED"
            return 0.1

        if _STAGE == "WAIT_A_SELECTED":
            assert RUNTIME.model_catalog.selected_model_id == DEMO_MODEL_3_ID
            assert RUNTIME.model_catalog.models[DEMO_MODEL_3_ID].registration_status == MODEL_STATUS_REGISTERED
            _toggle_data_layer_group_from_panel(window, moving_area, DEMO_MODEL_3_ID)
            _STAGE = "WAIT_A_TO_P"
            return 0.1

        if _STAGE == "WAIT_A_TO_P":
            assert RUNTIME.model_catalog.models[DEMO_MODEL_3_ID].registration_status == MODEL_STATUS_PENDING
            assert RUNTIME.model_catalog.selected_model_id == DEMO_MODEL_3_ID
            assert render_model_ids_for_role(ROLE_REFERENCE) == (
                DEMO_MODEL_1_ID,
                DEMO_MODEL_2_ID,
            )
            assert render_model_ids_for_role(ROLE_MOVING) == (DEMO_MODEL_3_ID,)
            assert RUNTIME.undo_journal.undo_count == 2
            assert undo_session_state() is True
            assert RUNTIME.model_catalog.models[DEMO_MODEL_3_ID].registration_status == MODEL_STATUS_REGISTERED
            assert redo_session_state() is True
            assert RUNTIME.model_catalog.models[DEMO_MODEL_3_ID].registration_status == MODEL_STATUS_PENDING
            with bpy.context.temp_override(**_operator_override(window, moving_area)):
                assert "FINISHED" in bpy.ops.colmap_align.toggle_data_layer_visibility(
                    model_id=DEMO_MODEL_3_ID
                )
            _STAGE = "VERIFY_P_HIDDEN"
            return 0.1

        if _STAGE == "VERIFY_P_HIDDEN":
            assert RUNTIME.model_catalog.models[DEMO_MODEL_3_ID].is_visible is False
            assert render_model_ids_for_role(ROLE_MOVING) == ()
            assert marker_source_model_id(ROLE_MOVING) is None
            assert bpy.ops.colmap_align.add_marker_pair.poll() is False
            with bpy.context.temp_override(**_operator_override(window, moving_area)):
                assert "FINISHED" in bpy.ops.colmap_align.toggle_data_layer_visibility(
                    model_id=DEMO_MODEL_3_ID
                )
            _STAGE = "VERIFY_P_VISIBLE"
            return 0.1

        if _STAGE == "VERIFY_P_VISIBLE":
            assert RUNTIME.model_catalog.models[DEMO_MODEL_3_ID].is_visible is True
            assert render_model_ids_for_role(ROLE_MOVING) == (DEMO_MODEL_3_ID,)
            with bpy.context.temp_override(**_operator_override(window, moving_area)):
                assert "FINISHED" in bpy.ops.colmap_align.select_data_layer(
                    model_id=DEMO_MODEL_2_ID
                )
            _STAGE = "VERIFY_A_SELECTED"
            return 0.1

        if _STAGE == "VERIFY_A_SELECTED":
            assert RUNTIME.model_catalog.selected_model_id == DEMO_MODEL_2_ID
            # 단일 A 선택은 직접 Align 파생 A 선택에도 그대로 반영됩니다.
            assert RUNTIME.model_catalog.selected_reference_model_id == DEMO_MODEL_2_ID
            assert RUNTIME.model_catalog.selected_moving_model_id is None
            assert RUNTIME.model_catalog.selected_inactive_model_id is None
            assert render_model_ids_for_role(ROLE_MOVING) == ()
            with bpy.context.temp_override(**_operator_override(window, moving_area)):
                assert "FINISHED" in bpy.ops.colmap_align.toggle_data_layer_visibility(
                    model_id=DEMO_MODEL_2_ID
                )
            assert render_model_ids_for_role(ROLE_REFERENCE) == (DEMO_MODEL_1_ID,)
            with bpy.context.temp_override(**_operator_override(window, moving_area)):
                assert "FINISHED" in bpy.ops.colmap_align.toggle_data_layer_visibility(
                    model_id=DEMO_MODEL_2_ID
                )
                assert "FINISHED" in bpy.ops.colmap_align.select_data_layer(model_id=DEMO_MODEL_4_ID)
            assert render_model_ids_for_role(ROLE_REFERENCE) == (
                DEMO_MODEL_1_ID,
                DEMO_MODEL_2_ID,
            )
            assert RUNTIME.marker_selection.begin_create_draft() is True
            with bpy.context.temp_override(**_operator_override(window, moving_area)):
                assert "CANCELLED" in bpy.ops.colmap_align.toggle_data_layer_group(
                    model_id=DEMO_MODEL_4_ID
                )
            assert RUNTIME.model_catalog.models[DEMO_MODEL_4_ID].registration_status == MODEL_STATUS_PENDING
            assert RUNTIME.marker_selection.cancel_draft() is True
            assert _scene_data_snapshot() == _SCENE_DATA_SNAPSHOT
            _simulate_shortcut(window, "DEL")
            _STAGE = "CONFIRM_DELETE"
            return 0.2

        if _STAGE == "CONFIRM_DELETE":
            assert DEMO_MODEL_4_ID in RUNTIME.model_catalog.models
            _simulate_shortcut(window, "RET")
            _STAGE = "VERIFY_DELETE"
            return 0.2

        if _STAGE == "VERIFY_DELETE":
            assert DEMO_MODEL_4_ID not in RUNTIME.model_catalog.models
            assert DEMO_MODEL_4_ID not in RUNTIME.model_repository.models
            assert DEMO_MODEL_4_ID not in RUNTIME.model_repository.display_transforms
            assert DEMO_MODEL_4_ID not in RUNTIME.point_clouds
            # 삭제된 활성 레이어의 stale 선택은 다른 레이어로 임의 이동하지 않고 비웁니다.
            assert RUNTIME.model_catalog.selected_model_id is None
            assert RUNTIME.undo_journal.undo_count == 0
            assert RUNTIME.undo_journal.redo_count == 0
            assert _scene_data_snapshot() == _SCENE_DATA_SNAPSHOT
            assert "FINISHED" in bpy.ops.colmap_align.exit_session()
            _STAGE = "EXIT"
            return 0.1

        raise AssertionError(f"알 수 없는 Data Layers GUI 테스트 단계입니다: {_STAGE}")
    except Exception:
        traceback.print_exc()
        _finish(False)
        return None


def schedule_gui_test() -> None:
    """GUI 이벤트 루프가 준비된 뒤 Data Layers 검사를 시작합니다."""
    _add_source_path()
    if bpy.app.background:
        print("MULTI_MODEL_GUI_TEST_RESULT=PASS")
        return
    from tools.align import registration
    from tools.align.operators.demo_data import install_demo_fixture

    registration.register()
    install_demo_fixture()
    bpy.app.timers.register(_run_step, first_interval=0.2)


if __name__ == "__main__":
    schedule_gui_test()
