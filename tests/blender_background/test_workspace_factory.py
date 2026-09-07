"""Blender Background와 GUI에서 공통 Workspace Factory 생명주기를 검증합니다."""

from __future__ import annotations

from pathlib import Path
import sys
import traceback

import bpy


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from shared.blender_runtime import (
    STATE_CREATING,
    WORKSPACE_STATE_KEY,
    WorkspaceFactoryError,
    WorkspaceSeedSpec,
    append_seed_workspace,
    delete_owned_workspace,
    mark_workspace_ready,
    pointer,
    screen_matches_workspace,
    source_workspace_from_metadata,
    workspace_matches_spec,
)


SPECS = (
    WorkspaceSeedSpec(
        tool_id="align",
        display_name="COLMAP Align Factory Test",
        seed_workspace_name="__COLMAP_ALIGN_SEED_5_2__",
        seed_path=ROOT / "tools/align/resources/workspaces/colmap_align_seed_5_2.blend",
    ),
    WorkspaceSeedSpec(
        tool_id="modifier",
        display_name="COLMAP Modifier Factory Test",
        seed_workspace_name="__COLMAP_MODIFIER_SEED_5_2__",
        seed_path=ROOT / "tools/modifier/resources/workspaces/colmap_modifier_seed_5_2.blend",
    ),
    WorkspaceSeedSpec(
        tool_id="projection_paint",
        display_name="Projection Paint Factory Test",
        seed_workspace_name="__PROJECTION_PAINT_SEED_5_2__",
        seed_path=ROOT / "tools/projection_paint/resources/workspaces/projection_paint_seed_5_2.blend",
    ),
)


def _assert_background_contract() -> None:
    """Background Context에서 Window 유효성 또는 CREATING ID 계약을 검증합니다."""

    context = bpy.context
    window = context.window
    before = {pointer(workspace) for workspace in bpy.data.workspaces}
    if window is None:
        try:
            append_seed_workspace(context, None, SPECS[0], None)
        except WorkspaceFactoryError:
            pass
        else:
            raise AssertionError("Window 없는 Background Context가 Factory에 허용되었습니다.")
        after = {pointer(workspace) for workspace in bpy.data.workspaces}
        if after != before:
            raise AssertionError("Window 유효성 차단 중 Workspace ID 집합이 변경되었습니다.")
        return
    source_workspace = window.workspace
    workspace = append_seed_workspace(context, window, SPECS[0], source_workspace)
    appended = [candidate for candidate in bpy.data.workspaces if pointer(candidate) not in before]
    if pointer(window.workspace) != pointer(source_workspace):
        raise AssertionError("Background Append 요청이 Source Workspace를 즉시 변경했습니다.")
    if len(appended) != 1 or appended[0] is not workspace:
        raise AssertionError("Background Append Pointer 차이가 정확히 하나가 아닙니다.")
    if workspace.get(WORKSPACE_STATE_KEY) != STATE_CREATING:
        raise AssertionError("Background Append 결과가 CREATING 상태가 아닙니다.")
    if not workspace_matches_spec(workspace, SPECS[0], require_ready=False):
        raise AssertionError("Background Append Workspace 소유 계약이 올바르지 않습니다.")
    if not screen_matches_workspace(workspace.screens[0], workspace):
        raise AssertionError("Background Seed Screen 소유 연결이 올바르지 않습니다.")


def main() -> None:
    """Background에서 Workspace Factory의 정적 생성 계약을 검사합니다."""

    _assert_background_contract()
    print("COLMAP Tools Workspace Factory Background 계약 검사 통과")


_GUI_PHASE = "APPEND"
_GUI_SPEC_INDEX = 0
_GUI_DELETE_INDEX = -1
_GUI_CREATED: list[tuple[object, object, WorkspaceSeedSpec]] = []
_GUI_WORKSPACE_POINTERS: set[int] = set()
_GUI_SCREEN_POINTERS: set[int] = set()


def _finish_gui_test(succeeded: bool) -> None:
    """GUI 검사 결과를 출력하고 Blender를 종료합니다."""

    print(f"GUI_TEST_RESULT={'PASS' if succeeded else 'FAIL'}")
    bpy.ops.wm.quit_blender()


def _run_gui_factory_test() -> float | None:
    """세 Seed의 생성·삭제와 소유권, Align Seed의 기본 패널 닫힘을 검증합니다."""

    global _GUI_PHASE, _GUI_SPEC_INDEX, _GUI_DELETE_INDEX
    try:
        context = bpy.context
        window = context.window
        if window is None:
            raise AssertionError("GUI Factory 검사에 활성 Window가 없습니다.")
        if _GUI_PHASE == "APPEND":
            spec = SPECS[_GUI_SPEC_INDEX]
            source_workspace = window.workspace
            workspace = append_seed_workspace(
                context, window, spec, source_workspace,
                preserve_seed_ui=spec.tool_id == "align",
            )
            if workspace.get(WORKSPACE_STATE_KEY) != STATE_CREATING:
                raise AssertionError("Append 직후 Workspace 상태가 CREATING이 아닙니다.")
            if source_workspace_from_metadata(workspace) is not source_workspace:
                raise AssertionError("Source Workspace 메타데이터 복구가 올바르지 않습니다.")
            _GUI_CREATED.append((workspace, source_workspace, spec))
            _GUI_PHASE = "VERIFY_APPEND"
            return 0.2
        if _GUI_PHASE == "VERIFY_APPEND":
            workspace, _source_workspace, spec = _GUI_CREATED[-1]
            if pointer(window.workspace) != pointer(workspace):
                raise AssertionError("다음 Tick에 Append Workspace가 활성화되지 않았습니다.")
            if not screen_matches_workspace(window.screen, workspace):
                raise AssertionError("활성 Screen의 Workspace UUID 연결이 올바르지 않습니다.")
            if spec.tool_id == "align":
                area = next(area for area in window.screen.areas if area.type == "VIEW_3D")
                if area.spaces.active.show_region_ui:
                    raise AssertionError("Align Seed의 패널이 기본 닫힘 상태가 아닙니다.")
            mark_workspace_ready(workspace, window.screen, spec)
            if not workspace_matches_spec(workspace, spec):
                raise AssertionError("READY Workspace가 최종 명세와 일치하지 않습니다.")
            _GUI_WORKSPACE_POINTERS.add(pointer(workspace))
            _GUI_SCREEN_POINTERS.add(pointer(window.screen))
            _GUI_SPEC_INDEX += 1
            if _GUI_SPEC_INDEX < len(SPECS):
                _GUI_PHASE = "APPEND"
                return 0.1
            if (
                len(_GUI_WORKSPACE_POINTERS) != len(SPECS)
                or len(_GUI_SCREEN_POINTERS) != len(SPECS)
            ):
                raise AssertionError("세 도구 Workspace 또는 Screen Pointer가 독립적이지 않습니다.")
            _GUI_DELETE_INDEX = len(_GUI_CREATED) - 1
            _GUI_PHASE = "WAIT_BEFORE_DELETE"
            return 1.0
        if _GUI_PHASE == "WAIT_BEFORE_DELETE":
            _GUI_PHASE = "DELETE"
            return 0.1
        if _GUI_PHASE == "DELETE":
            workspace, source_workspace, spec = _GUI_CREATED[_GUI_DELETE_INDEX]
            if pointer(window.workspace) != pointer(workspace):
                raise AssertionError("삭제 대상 Workspace가 현재 Window에서 활성 상태가 아닙니다.")
            if not delete_owned_workspace(
                context,
                window,
                workspace,
                spec,
                restore_workspace=source_workspace,
            ):
                raise AssertionError(f"소유 Workspace를 삭제하지 못했습니다: {spec.tool_id}")
            _GUI_PHASE = "VERIFY_DELETE"
            return 0.2
        if _GUI_PHASE == "VERIFY_DELETE":
            _workspace, source_workspace, _spec = _GUI_CREATED[_GUI_DELETE_INDEX]
            if pointer(window.workspace) != pointer(source_workspace):
                raise AssertionError("삭제 뒤 Source Workspace가 복원되지 않았습니다.")
            _GUI_DELETE_INDEX -= 1
            if _GUI_DELETE_INDEX >= 0:
                _GUI_PHASE = "DELETE"
                return 0.1
            remaining_screen_pointers = {pointer(screen) for screen in bpy.data.screens}
            if _GUI_SCREEN_POINTERS & remaining_screen_pointers:
                raise AssertionError("삭제한 Workspace의 Seed Screen이 고아 ID로 남았습니다.")
            remaining_workspace_pointers = {
                pointer(workspace) for workspace in bpy.data.workspaces
            }
            if _GUI_WORKSPACE_POINTERS & remaining_workspace_pointers:
                raise AssertionError("삭제한 소유 Workspace가 고아 ID로 남았습니다.")
            print("COLMAP Tools Workspace Factory GUI 검사 통과")
            _finish_gui_test(True)
            return None
        raise AssertionError(f"알 수 없는 GUI 검사 단계입니다: {_GUI_PHASE}")
    except Exception:
        traceback.print_exc()
        _finish_gui_test(False)
    return None


def schedule_test() -> None:
    """Background에서는 즉시, GUI에서는 첫 이벤트 Tick 뒤 검사를 실행합니다."""

    if bpy.app.background:
        main()
        return
    bpy.app.timers.register(_run_gui_factory_test, first_interval=0.2)


if __name__ == "__main__":
    schedule_test()
