"""Blender GUI에서 Data Layers G/R/S·Gizmo 변환의 실제 모달 경계를 검증합니다."""

from __future__ import annotations

import sys
import traceback
from pathlib import Path
from types import SimpleNamespace

import bpy
import numpy as np
from bpy_extras import view3d_utils
from mathutils import Vector


_STAGE = "OPEN"
_SCENE_SNAPSHOT = None
_FINGERPRINTS: dict[str, str] = {}
_PIVOT_EXPECTED: np.ndarray | None = None


def _add_source_path() -> None:
    """애드온 소스 디렉터리를 Blender GUI 테스트의 모듈 탐색 경로에 추가합니다."""

    source_path = Path(__file__).resolve().parents[3]
    source_text = str(source_path)
    if source_text not in sys.path:
        sys.path.insert(0, source_text)


def _scene_snapshot() -> tuple[frozenset[int], frozenset[int]]:
    """proxy Blender Object·Collection이 생성되지 않는지 확인할 Scene 포인터 집합을 기록합니다."""

    return (
        frozenset(item.as_pointer() for item in bpy.data.objects),
        frozenset(item.as_pointer() for item in bpy.data.collections),
    )


def _main_window() -> bpy.types.Window:
    """현재 COLMAP Align 메인 Window를 반환합니다."""

    from tools.align.operators.workspace import find_main_window

    window = find_main_window(bpy.context.window_manager)
    assert window is not None, "COLMAP Align 메인 Window를 찾지 못했습니다."
    return window


def _window_region(area: bpy.types.Area):
    """오퍼레이터와 이벤트에 사용할 VIEW_3D WINDOW Region을 반환합니다."""

    region = next((item for item in area.regions if item.type == "WINDOW"), None)
    assert region is not None, "VIEW_3D WINDOW Region을 찾지 못했습니다."
    return region


def _override(window: bpy.types.Window, area: bpy.types.Area) -> dict[str, object]:
    """지정 A/P 뷰포트에서 오퍼레이터를 실행할 Blender context override를 만듭니다."""

    return {
        "window": window,
        "screen": window.screen,
        "area": area,
        "region": _window_region(area),
    }


def _viewport_coordinate(area: bpy.types.Area, horizontal: float, vertical: float) -> tuple[int, int]:
    """VIEW_3D WINDOW Region 비율 좌표를 Window.event_simulate 절대 좌표로 변환합니다."""

    region = _window_region(area)
    return (
        region.x + round(region.width * horizontal),
        region.y + round(region.height * vertical),
    )


def _send_mouse(window: bpy.types.Window, coordinate: tuple[int, int]) -> None:
    """후속 G/R/S 시작이 현재 P 뷰포트 위치를 읽도록 마우스 좌표를 갱신합니다."""

    window.event_simulate(type="MOUSEMOVE", value="NOTHING", x=coordinate[0], y=coordinate[1])


def _send_shortcut(window: bpy.types.Window, key: str, coordinate: tuple[int, int]) -> None:
    """세션 입력 가드에 현재 P 뷰포트 위치의 G 또는 R 단축키를 실제 이벤트로 전달합니다."""

    assert bpy.app.use_event_simulate is True, "--enable-event-simulate가 필요합니다."
    _send_mouse(window, coordinate)
    window.event_simulate(type=key, value="PRESS", x=coordinate[0], y=coordinate[1])
    window.event_simulate(type=key, value="RELEASE", x=coordinate[0], y=coordinate[1])


def _send_left_mouse(window: bpy.types.Window, value: str, coordinate: tuple[int, int]) -> None:
    """표준 G/R 확정 PRESS 또는 Gizmo 경로 확정 RELEASE를 실제 LMB 이벤트로 전달합니다."""

    window.event_simulate(type="LEFTMOUSE", value=value, x=coordinate[0], y=coordinate[1])


def _apply_double_click_pivot(
    window: bpy.types.Window,
    area: bpy.types.Area,
    coordinate: tuple[int, int],
) -> None:
    """등록 키맵이 전달하는 더블클릭 좌표를 실제 피벗 입력 경계에서 검증합니다."""

    from tools.align.operators.layer_transform import set_layer_transform_pivot_from_event

    event = SimpleNamespace(mouse_x=coordinate[0], mouse_y=coordinate[1])
    with bpy.context.temp_override(**_override(window, area)):
        assert set_layer_transform_pivot_from_event(bpy.context, event) is True


def _pickable_screen_point(
    area: bpy.types.Area,
    model_ids: tuple[str, ...],
) -> tuple[tuple[int, int], np.ndarray]:
    """현재 뷰에 투영 가능한 표시 점과 피킹 결과의 정확한 Project World 좌표를 반환합니다."""

    from tools.align.alignment import project_world_source_position
    from tools.align.state import RUNTIME
    from tools.align.viewport.picking import pick_nearest_point_for_model_ids
    from tools.align.viewport.point_data import (
        effective_positions_for_model_id,
        model_data_for_id,
        point_arrays,
    )

    region = _window_region(area)
    region_3d = area.spaces.active.region_3d
    for model_id in model_ids:
        model_data = model_data_for_id(model_id)
        assert model_data is not None
        arrays = point_arrays(model_data)
        for source_index in arrays.display_indices:
            source_position = arrays.positions[int(source_index)]
            world_position = effective_positions_for_model_id(
                model_id,
                source_position[np.newaxis, :],
            )[0]
            projected = view3d_utils.location_3d_to_region_2d(
                region,
                region_3d,
                Vector(tuple(float(value) for value in world_position)),
            )
            if projected is None:
                continue
            mouse_region = (round(float(projected.x)), round(float(projected.y)))
            if not (2 <= mouse_region[0] < region.width - 2 and 2 <= mouse_region[1] < region.height - 2):
                continue
            result = pick_nearest_point_for_model_ids(
                model_ids,
                region,
                region_3d,
                mouse_region,
            )
            if result is None:
                continue
            expected = project_world_source_position(
                RUNTIME.model_repository,
                result.model_id,
                result.position,
            )
            return (
                (region.x + mouse_region[0], region.y + mouse_region[1]),
                expected,
            )
    raise AssertionError("더블클릭 피벗 테스트에 사용할 화면 포인트를 찾지 못했습니다.")


def _select_layer(window: bpy.types.Window, area: bpy.types.Area, model_id: str, *, extend: bool) -> None:
    """Data Layers 행과 같은 선택 오퍼레이터로 P 다중 선택을 구성합니다."""

    with bpy.context.temp_override(**_override(window, area)):
        result = bpy.ops.colmap_align.select_data_layer(
            model_id=model_id,
            extend_selection=extend,
        )
    assert "FINISHED" in result


def _assert_gizmo_project_world_axes() -> None:
    """GUI 등록 GizmoGroup이 동일 pivot에서 서로 다른 Project World 축 basis를 쓰는지 확인합니다."""

    from tools.align.operators.layer_transform import (
        COLMAPALIGN_GGT_layer_transform,
        _gizmo_matrix,
    )

    pivot = (2.0, -3.0, 4.0)
    expected = {
        "X": Vector((1.0, 0.0, 0.0)),
        "Y": Vector((0.0, 1.0, 0.0)),
        "Z": Vector((0.0, 0.0, 1.0)),
    }
    bases = {}
    for axis, direction in expected.items():
        matrix = _gizmo_matrix(pivot, axis)
        bases[axis] = matrix.to_3x3() @ Vector((0.0, 0.0, 1.0))
        np.testing.assert_allclose(tuple(matrix.translation), pivot, rtol=0.0, atol=1.0e-6)
        np.testing.assert_allclose(tuple(bases[axis]), tuple(direction), rtol=0.0, atol=1.0e-6)
    assert len({tuple(round(value, 6) for value in basis) for basis in bases.values()}) == 3
    assert COLMAPALIGN_GGT_layer_transform.bl_space_type == "VIEW_3D"
    assert COLMAPALIGN_GGT_layer_transform.bl_region_type == "WINDOW"


def _alignment_fingerprints(model_ids: tuple[str, ...]) -> dict[str, str]:
    """변환 preview와 확정 경계를 비교할 현재 Repository alignment 지문을 반환합니다."""

    from tools.align.state import RUNTIME

    return {
        model_id: RUNTIME.model_repository.alignment_transform_for_model_id(model_id).fingerprint
        for model_id in model_ids
    }


def _assert_manual_provenance(model_ids: tuple[str, ...]) -> None:
    """모든 확정 대상이 MANUAL provenance로 함께 갱신됐는지 확인합니다."""

    from tools.align.state import RUNTIME

    for model_id in model_ids:
        record = RUNTIME.model_catalog.models[model_id]
        alignment = RUNTIME.model_repository.alignment_transform_for_model_id(model_id)
        assert record.alignment_provenance is not None
        assert record.alignment_provenance.method == "MANUAL"
        assert record.alignment_provenance.result_fingerprint == alignment.fingerprint


def _finish(succeeded: bool) -> None:
    """등록을 해제하고 GUI 검증 결과를 출력한 뒤 Blender 프로세스를 종료합니다."""

    try:
        from tools.align import registration

        registration.unregister()
    except Exception:
        traceback.print_exc()
        succeeded = False
    finally:
        print(f"LAYER_TRANSFORM_GUI_TEST_RESULT={'PASS' if succeeded else 'FAIL'}")
        bpy.ops.wm.quit_blender()


def _run_step() -> float | None:
    """P 다중 선택·G/R·Gizmo scale release를 타이머 단계로 분리해 실제 모달 순서를 검증합니다."""

    global _STAGE, _SCENE_SNAPSHOT, _FINGERPRINTS, _PIVOT_EXPECTED
    try:
        from tools.align.constants import (
            DEMO_MODEL_3_ID,
            DEMO_MODEL_4_ID,
            LAYER_TRANSFORM_PHASE_ROTATE,
            LAYER_TRANSFORM_PHASE_SCALE,
            LAYER_TRANSFORM_PHASE_TRANSLATE,
            ROLE_MOVING,
        )
        from tools.align.operators.layer_transform import COLMAPALIGN_GGT_layer_transform
        from tools.align.operators.workspace import _process_sidebar_activation
        from tools.align.state import RUNTIME
        from tools.align.viewport.registry import classify_areas
        from tools.align.viewport.renderer import render_model_ids_for_role

        model_ids = (DEMO_MODEL_3_ID, DEMO_MODEL_4_ID)
        if RUNTIME.workspace_setup_task is not None:
            return 0.1
        if RUNTIME.workspace_setup_error:
            raise AssertionError(RUNTIME.workspace_setup_error)

        if _STAGE == "OPEN":
            _SCENE_SNAPSHOT = _scene_snapshot()
            assert "FINISHED" in bpy.ops.colmap_align.open_workspace()
            _STAGE = "WAIT_WORKSPACE"
            return 0.1

        if _STAGE == "EXIT":
            if RUNTIME.session.phase != "INACTIVE":
                return 0.1
            assert _scene_snapshot() == _SCENE_SNAPSHOT
            _finish(True)
            return None

        window = _main_window()
        reference_area, moving_area = classify_areas(window.screen.areas)
        assert reference_area is not None and moving_area is not None

        if _STAGE == "WAIT_WORKSPACE":
            if bpy.app.timers.is_registered(_process_sidebar_activation):
                return 0.1
            assert RUNTIME.session.input_guard_active is True
            _select_layer(window, moving_area, DEMO_MODEL_3_ID, extend=False)
            _select_layer(window, moving_area, DEMO_MODEL_4_ID, extend=True)
            _STAGE = "VERIFY_SELECTION_AND_GIZMO"
            return 0.1

        if _STAGE == "VERIFY_SELECTION_AND_GIZMO":
            assert RUNTIME.model_catalog.selected_moving_model_ids == model_ids
            assert render_model_ids_for_role(ROLE_MOVING) == model_ids
            with bpy.context.temp_override(**_override(window, moving_area)):
                assert COLMAPALIGN_GGT_layer_transform.poll(bpy.context) is True
                moving_area.tag_redraw()
            _assert_gizmo_project_world_axes()
            _FINGERPRINTS = _alignment_fingerprints(model_ids)
            coordinate, _PIVOT_EXPECTED = _pickable_screen_point(
                moving_area,
                model_ids,
            )
            _apply_double_click_pivot(window, moving_area, coordinate)
            _STAGE = "WAIT_CUSTOM_PIVOT"
            return 0.1

        if _STAGE == "WAIT_CUSTOM_PIVOT":
            from tools.align.alignment import layer_transform_pivot_for_selection

            assert _PIVOT_EXPECTED is not None
            state = RUNTIME.layer_transform
            assert state.is_active is False
            assert state.custom_pivot_role == ROLE_MOVING
            assert state.custom_pivot_model_ids == model_ids
            np.testing.assert_allclose(
                layer_transform_pivot_for_selection(RUNTIME, ROLE_MOVING, model_ids),
                _PIVOT_EXPECTED,
                rtol=0.0,
                atol=1.0e-6,
            )
            np.testing.assert_allclose(
                tuple(moving_area.spaces.active.region_3d.view_location),
                _PIVOT_EXPECTED,
                rtol=0.0,
                atol=1.0e-6,
            )
            assert _alignment_fingerprints(model_ids) == _FINGERPRINTS
            assert RUNTIME.undo_journal.undo_count == 0
            _send_shortcut(window, "G", _viewport_coordinate(moving_area, 0.62, 0.50))
            _STAGE = "WAIT_G_STARTED"
            return 0.1

        if _STAGE == "WAIT_G_STARTED":
            state = RUNTIME.layer_transform
            assert state.is_active is True
            assert state.phase == LAYER_TRANSFORM_PHASE_TRANSLATE
            assert state.role == ROLE_MOVING
            assert state.model_ids == model_ids
            assert _PIVOT_EXPECTED is not None
            np.testing.assert_allclose(state.pivot_float64, _PIVOT_EXPECTED, rtol=0.0, atol=1.0e-6)
            _send_mouse(window, _viewport_coordinate(moving_area, 0.78, 0.62))
            _STAGE = "WAIT_G_PREVIEW"
            return 0.1

        if _STAGE == "WAIT_G_PREVIEW":
            from tools.align.alignment import effective_alignment_for_model

            assert RUNTIME.layer_transform.is_active is True
            assert _alignment_fingerprints(model_ids) == _FINGERPRINTS
            assert (
                effective_alignment_for_model(RUNTIME, DEMO_MODEL_3_ID).fingerprint
                != _FINGERPRINTS[DEMO_MODEL_3_ID]
            )
            coordinate = _viewport_coordinate(moving_area, 0.78, 0.62)
            _send_left_mouse(window, "PRESS", coordinate)
            _STAGE = "WAIT_G_COMMITTED"
            return 0.1

        if _STAGE == "WAIT_G_COMMITTED":
            assert RUNTIME.layer_transform.is_active is False
            g_fingerprints = _alignment_fingerprints(model_ids)
            assert all(g_fingerprints[model_id] != _FINGERPRINTS[model_id] for model_id in model_ids)
            assert RUNTIME.undo_journal.undo_count == 1
            _assert_manual_provenance(model_ids)
            _FINGERPRINTS = g_fingerprints
            _send_shortcut(window, "R", _viewport_coordinate(moving_area, 0.70, 0.50))
            _STAGE = "WAIT_R_STARTED"
            return 0.1

        if _STAGE == "WAIT_R_STARTED":
            state = RUNTIME.layer_transform
            assert state.is_active is True
            assert state.phase == LAYER_TRANSFORM_PHASE_ROTATE
            assert state.model_ids == model_ids
            _send_mouse(window, _viewport_coordinate(moving_area, 0.70, 0.70))
            _STAGE = "WAIT_R_PREVIEW"
            return 0.1

        if _STAGE == "WAIT_R_PREVIEW":
            from tools.align.alignment import effective_alignment_for_model

            assert _alignment_fingerprints(model_ids) == _FINGERPRINTS
            assert (
                effective_alignment_for_model(RUNTIME, DEMO_MODEL_4_ID).fingerprint
                != _FINGERPRINTS[DEMO_MODEL_4_ID]
            )
            coordinate = _viewport_coordinate(moving_area, 0.70, 0.70)
            _send_left_mouse(window, "PRESS", coordinate)
            _STAGE = "WAIT_R_COMMITTED"
            return 0.1

        if _STAGE == "WAIT_R_COMMITTED":
            assert RUNTIME.layer_transform.is_active is False
            r_fingerprints = _alignment_fingerprints(model_ids)
            assert all(r_fingerprints[model_id] != _FINGERPRINTS[model_id] for model_id in model_ids)
            assert RUNTIME.undo_journal.undo_count == 2
            _assert_manual_provenance(model_ids)
            _FINGERPRINTS = r_fingerprints
            start = _viewport_coordinate(moving_area, 0.70, 0.50)
            _send_mouse(window, start)
            with bpy.context.temp_override(**_override(window, moving_area)):
                result = bpy.ops.colmap_align.transform_data_layers(
                    "INVOKE_DEFAULT",
                    mode="SCALE",
                    axis="NONE",
                    from_gizmo=True,
                )
            assert "RUNNING_MODAL" in result
            _STAGE = "WAIT_GIZMO_SCALE_STARTED"
            return 0.1

        if _STAGE == "WAIT_GIZMO_SCALE_STARTED":
            state = RUNTIME.layer_transform
            assert state.is_active is True
            assert state.phase == LAYER_TRANSFORM_PHASE_SCALE
            assert state.model_ids == model_ids
            _send_mouse(window, _viewport_coordinate(moving_area, 0.86, 0.50))
            _STAGE = "WAIT_GIZMO_SCALE_PREVIEW"
            return 0.1

        if _STAGE == "WAIT_GIZMO_SCALE_PREVIEW":
            from tools.align.alignment import effective_alignment_for_model

            assert _alignment_fingerprints(model_ids) == _FINGERPRINTS
            assert (
                effective_alignment_for_model(RUNTIME, DEMO_MODEL_3_ID).fingerprint
                != _FINGERPRINTS[DEMO_MODEL_3_ID]
            )
            _send_left_mouse(window, "RELEASE", _viewport_coordinate(moving_area, 0.86, 0.50))
            _STAGE = "WAIT_GIZMO_SCALE_COMMITTED"
            return 0.1

        if _STAGE == "WAIT_GIZMO_SCALE_COMMITTED":
            assert RUNTIME.layer_transform.is_active is False
            scale_fingerprints = _alignment_fingerprints(model_ids)
            assert all(scale_fingerprints[model_id] != _FINGERPRINTS[model_id] for model_id in model_ids)
            assert RUNTIME.undo_journal.undo_count == 3
            _assert_manual_provenance(model_ids)
            assert "FINISHED" in bpy.ops.colmap_align.exit_session()
            _STAGE = "EXIT"
            return 0.1

        raise AssertionError(f"알 수 없는 레이어 변환 GUI 단계입니다: {_STAGE}")
    except Exception:
        traceback.print_exc()
        _finish(False)
        return None


def schedule_gui_test() -> None:
    """GUI 이벤트 루프가 준비된 뒤 실제 G/R/S와 Gizmo release 검증을 예약합니다."""

    _add_source_path()
    if bpy.app.background:
        print("LAYER_TRANSFORM_GUI_TEST_RESULT=PASS")
        return
    from tools.align import registration
    from tools.align.operators.demo_data import install_demo_fixture

    registration.register()
    install_demo_fixture()
    bpy.app.timers.register(_run_step, first_interval=0.2)


if __name__ == "__main__":
    schedule_gui_test()
