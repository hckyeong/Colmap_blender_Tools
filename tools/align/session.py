"""COLMAP Align 작업공간 세션, 원래 WorkSpace 복귀와 사후 복구를 관리합니다."""

from __future__ import annotations

from typing import Any

import bpy

try:
    from ...shared.blender_runtime.workspace_factory import (
        source_workspace_from_metadata,
        workspace_matches_spec,
    )
except ImportError:  # Source Tree를 최상위 tools 패키지로 실행하는 기존 테스트를 지원합니다.
    from shared.blender_runtime.workspace_factory import (
        source_workspace_from_metadata,
        workspace_matches_spec,
    )

from .constants import (
    MODEL_COMMAND_TOGGLE_LAYER_GROUP,
    SESSION_ACTIVE,
    SESSION_ENTERING,
    SESSION_EXITING,
    SESSION_INACTIVE,
    WORKSPACE_OWNER_KEY,
    WORKSPACE_SOURCE_KEY,
)
from .status import set_workspace_status_text
from .state import RUNTIME
from .axis_settings import AXIS_SETTINGS_COMMAND, axis_edit_active_block_reason
from .constants import LAYER_TRANSFORM_COMMAND_COMMIT
from .viewport.registry import invalidate_registry, refresh_registry
from .viewport.renderer import request_redraw
from .workspace_contract import ALIGN_WORKSPACE_SPEC


def _window_manager() -> Any | None:
    """현재 Blender 컨텍스트에서 접근 가능한 WindowManager를 반환합니다."""
    return getattr(bpy.context, "window_manager", None)


def _window_is_live(window: Any) -> bool:
    """저장한 Window 참조가 현재 WindowManager에 계속 존재하는지 확인합니다."""
    window_manager = _window_manager()
    if window is None or window_manager is None:
        return False
    try:
        pointer = window.as_pointer()
    except (AttributeError, ReferenceError):
        return False
    return any(candidate.as_pointer() == pointer for candidate in window_manager.windows)


def _workspace_by_saved_identity(reference: Any, pointer: int, name: str) -> Any | None:
    """포인터를 우선하고 이름을 보조로 사용해 아직 유효한 WorkSpace를 찾습니다."""
    workspaces = list(bpy.data.workspaces)
    if reference is not None:
        try:
            reference_pointer = reference.as_pointer()
        except ReferenceError:
            reference_pointer = 0
        if reference_pointer and any(item.as_pointer() == reference_pointer for item in workspaces):
            return reference
    if pointer:
        for workspace in workspaces:
            if workspace.as_pointer() == pointer:
                return workspace
    if name:
        return bpy.data.workspaces.get(name)
    return None


def session_target_window() -> Any | None:
    """현재 세션이 보유한 살아 있는 대상 Window만 반환합니다."""
    window = RUNTIME.session.target_window_ref
    return window if _window_is_live(window) else None


def session_source_workspace() -> Any | None:
    """세션 진입 전에 저장한 원래 WorkSpace를 안전하게 다시 찾습니다."""
    session = RUNTIME.session
    return _workspace_by_saved_identity(
        session.source_workspace_ref,
        session.source_workspace_pointer,
        session.source_workspace_name,
    )


def session_align_workspace() -> Any | None:
    """세션에 저장한 포인터와 현행 Align 소유 계약이 모두 일치하는 WorkSpace만 반환합니다."""
    session = RUNTIME.session
    workspace = _workspace_by_saved_identity(
        session.align_workspace_ref,
        session.align_workspace_pointer,
        "",
    )
    return (
        workspace
        if workspace_matches_spec(workspace, ALIGN_WORKSPACE_SPEC, require_ready=False)
        else None
    )


def _set_session_message(message: str) -> None:
    """세션 메시지를 저장하고 COLMAP Align 작업공간의 하단 상태 표시줄에 알립니다."""
    RUNTIME.session.message = message
    set_workspace_status_text(session_align_workspace(), message)


def _clear_session_status_text() -> None:
    """세션 종료·파일 교체 전에 COLMAP Align 상태 표시줄 문구를 지웁니다."""
    set_workspace_status_text(session_align_workspace(), None)


def is_session_active_for_window(window: Any) -> bool:
    """주어진 Window에서만 COLMAP Align 입력 세션이 활성인지 판별합니다."""
    session = RUNTIME.session
    if not session.session_active or session.phase != SESSION_ACTIVE:
        return False
    if not session.is_target_window(window):
        return False
    try:
        return session.is_align_workspace(window.workspace)
    except ReferenceError:
        return False


def is_align_workspace_for_window(window: Any) -> bool:
    """세션 상태와 무관하게 현재 Window가 READY Align Workspace인지 판별합니다."""

    if window is None:
        return False
    try:
        return workspace_matches_spec(
            window.workspace,
            ALIGN_WORKSPACE_SPEC,
            require_ready=True,
        )
    except (AttributeError, ReferenceError, RuntimeError, TypeError):
        return False


def is_session_owned_by_window(window: Any) -> bool:
    """Workspace 탭이 바뀌어도 지정 Window가 현재 Align 세션 대상인지 반환합니다."""

    session = RUNTIME.session
    return bool(
        session.session_active
        and session.phase == SESSION_ACTIVE
        and session.is_target_window(window)
        and _window_is_live(window)
    )


def begin_session_entry(window: Any) -> None:
    """작업공간 전환 전에 원래 WorkSpace를 저장하고 ENTERING 상태로 전이합니다."""
    session = RUNTIME.session
    if session.phase != SESSION_INACTIVE:
        raise RuntimeError("COLMAP Align 세션이 이미 준비 또는 실행 중입니다.")
    if window is None or window.workspace is None:
        raise RuntimeError("COLMAP Align 세션을 시작할 현재 WorkSpace가 없습니다.")
    source_workspace = window.workspace
    if workspace_matches_spec(source_workspace, ALIGN_WORKSPACE_SPEC, require_ready=False):
        restored_source = source_workspace_from_metadata(source_workspace)
        if restored_source is None or restored_source == source_workspace:
            raise RuntimeError("원래 사용자 작업공간을 찾지 못해 COLMAP Align 세션을 시작할 수 없습니다.")
        source_workspace = restored_source
    elif source_workspace.get(WORKSPACE_OWNER_KEY, False):
        # Schema 이전 Align Workspace는 현행 소유 공간으로 재사용하지 않고 Source만 복구합니다.
        source_name = source_workspace.get(WORKSPACE_SOURCE_KEY, "")
        restored_source = bpy.data.workspaces.get(source_name) if source_name else None
        if restored_source is None or restored_source == source_workspace:
            raise RuntimeError("원래 사용자 작업공간을 찾지 못해 COLMAP Align 세션을 시작할 수 없습니다.")
        source_workspace = restored_source
    session.begin_entering(window, source_workspace)
    RUNTIME.undo_journal.clear()


def bind_align_workspace(workspace: Any) -> None:
    """Seed에서 추가하거나 재사용한 Align WorkSpace를 현재 진입 세션에 연결합니다."""
    if RUNTIME.session.phase != SESSION_ENTERING:
        raise RuntimeError("COLMAP Align 진입 세션이 없는 상태에서 작업공간을 연결할 수 없습니다.")
    RUNTIME.session.set_align_workspace(workspace)


def activate_session(window: Any) -> None:
    """작업공간 구성 성공 후 세션을 ACTIVE로 전환하고 입력 가드를 시작합니다."""
    session = RUNTIME.session
    if session.phase == SESSION_ACTIVE and is_session_active_for_window(window):
        return
    if session.phase != SESSION_ENTERING or not session.is_target_window(window):
        raise RuntimeError("COLMAP Align 세션 진입 상태가 올바르지 않습니다.")
    if not session.is_align_workspace(window.workspace):
        raise RuntimeError("COLMAP Align 작업공간이 활성 Window에 연결되지 않았습니다.")
    session.phase = SESSION_ACTIVE
    session.session_active = True
    session.exit_requested = False
    session.recovery_pending = False
    _set_session_message("COLMAP Align 세션이 활성화되었습니다. Ctrl+Z는 세션 이력만 되돌립니다.")
    from .operators.session_guard import start_input_guard

    start_input_guard(window)
    request_redraw()


def _clear_pending_marker_selection() -> None:
    """세션 종료 중인 미완성 A/P 선택과 대기 명령을 안전하게 비웁니다."""
    if RUNTIME.undo_journal.scope_active:
        RUNTIME.undo_journal.cancel_scope(RUNTIME.marker_selection)
    elif RUNTIME.marker_selection.has_draft:
        RUNTIME.marker_selection.cancel_draft()
    RUNTIME.clear_marker_command()


def _capture_role_views_before_exit(window: Any) -> bool:
    """원래 작업공간 복귀 전에 전용 Screen의 사용자 A/P 시점을 보관합니다."""
    if not RUNTIME.session.is_align_workspace(getattr(window, "workspace", None)):
        return False
    try:
        from .operators.workspace import capture_role_view_states

        return capture_role_view_states(window)
    except (AttributeError, ReferenceError, RuntimeError):
        return False


def _restore_source_workspace(window: Any) -> bool:
    """세션 진입 전 WorkSpace가 유효할 때만 대상 Window를 그곳으로 복귀합니다."""
    source_workspace = session_source_workspace()
    if source_workspace is None:
        RUNTIME.workspace_setup_error = "원래 작업공간을 찾지 못해 자동 복귀할 수 없습니다."
        _set_session_message(RUNTIME.workspace_setup_error)
        return False
    _capture_role_views_before_exit(window)
    try:
        window.workspace = source_workspace
    except (AttributeError, ReferenceError, RuntimeError) as error:
        RUNTIME.workspace_setup_error = f"원래 작업공간 복귀에 실패했습니다: {error}"
        _set_session_message(RUNTIME.workspace_setup_error)
        return False
    return True


def _finish_session_exit() -> float | None:
    """모달 가드가 종료된 뒤 원래 WorkSpace 복귀와 런타임 세션 정리를 마칩니다."""
    session = RUNTIME.session
    if session.phase != SESSION_EXITING:
        RUNTIME.session_exit_task = None
        return None
    if RUNTIME.modal_active or session.input_guard_active:
        return 0.05
    window = session_target_window()
    if window is not None:
        _restore_source_workspace(window)
    else:
        RUNTIME.workspace_setup_error = "COLMAP Align 세션 Window가 사라져 작업공간을 복귀하지 못했습니다."
    invalidate_registry()
    _clear_session_status_text()
    session.clear()
    RUNTIME.session_exit_task = None
    request_redraw()
    return None


def _schedule_session_exit_finish() -> None:
    """모달 종료 뒤 한 번만 실행될 비동기 세션 종료 마무리를 예약합니다."""
    if RUNTIME.session_exit_task is not None:
        return
    RUNTIME.session_exit_task = _finish_session_exit
    if not bpy.app.timers.is_registered(_finish_session_exit):
        bpy.app.timers.register(_finish_session_exit, first_interval=0.05)


def request_session_exit(window: Any) -> bool:
    """종료 버튼 요청으로 입력을 비활성화하고 원래 WorkSpace 복귀를 예약합니다."""
    session = RUNTIME.session
    if not is_session_owned_by_window(window):
        return False
    from .alignment.icp_runtime import IcpRuntimeError

    try:
        _invalidate_icp_for_lifecycle("COLMAP Align 세션을 종료합니다.")
    except IcpRuntimeError as error:
        _set_session_message(f"ICP worker를 정리하지 못해 세션을 종료하지 않았습니다: {error}")
        request_redraw()
        return False
    from .operators.marker_alignment import discard_marker_preview_for_lifecycle
    from .alignment.layer_transform import cancel_layer_transform

    cancel_axis_edit_for_lifecycle()
    discard_marker_preview_for_lifecycle()
    cancel_layer_transform(RUNTIME, message="세션 종료로 데이터 레이어 변환을 취소했습니다.")
    session.phase = SESSION_EXITING
    session.session_active = False
    session.exit_requested = True
    session.recovery_pending = False
    _set_session_message("COLMAP Align 세션을 종료하고 원래 작업공간으로 돌아가는 중입니다.")
    _clear_pending_marker_selection()
    RUNTIME.model_catalog.clear_selected_model()
    RUNTIME.clear_data_layer_click()
    RUNTIME.undo_journal.clear()
    _schedule_session_exit_finish()
    request_redraw()
    return True


def _restore_committed_session_snapshot(snapshot: Any, kind: str) -> None:
    """공용 확정 명령의 종류에 따라 마커·데이터 레이어·정렬·축 스냅샷을 원자 복원합니다."""
    from .alignment.icp_runtime import (
        icp_commit_command_kind,
        restore_icp_commit_snapshot,
    )

    if kind == icp_commit_command_kind():
        restore_icp_commit_snapshot(RUNTIME, snapshot)
        return
    if kind == AXIS_SETTINGS_COMMAND:
        from .axis_settings import restore_axis_edit_snapshot

        restore_axis_edit_snapshot(RUNTIME, snapshot)
        return
    if kind == MODEL_COMMAND_TOGGLE_LAYER_GROUP:
        RUNTIME.model_catalog.restore_group_snapshot(snapshot)
        return
    if kind == LAYER_TRANSFORM_COMMAND_COMMIT:
        from .alignment.layer_transform import restore_layer_transform_commit_snapshot
        from .alignment.runtime import invalidate_marker_preview

        restore_layer_transform_commit_snapshot(RUNTIME, snapshot)
        invalidate_marker_preview(RUNTIME, "레이어 변환 Undo/Redo로 정렬 기준이 변경되었습니다.")
        _invalidate_icp_for_lifecycle("레이어 변환 Undo/Redo로 ICP 기준이 변경되었습니다.")
        return
    RUNTIME.marker_selection.restore_snapshot(snapshot)


def _sync_after_committed_restore(kind: str) -> None:
    """Undo·Redo 뒤 UI를 맞추고 ICP 외 확정 변경만 persistence dirty로 기록합니다."""
    from .alignment.icp_runtime import icp_commit_command_kind
    from .project_persistence.runtime import notify_project_state_changed

    if kind == MODEL_COMMAND_TOGGLE_LAYER_GROUP:
        RUNTIME.model_catalog.message = RUNTIME.undo_journal.last_action
    elif kind in {LAYER_TRANSFORM_COMMAND_COMMIT, AXIS_SETTINGS_COMMAND}:
        RUNTIME.model_catalog.message = RUNTIME.undo_journal.last_action
    elif kind == icp_commit_command_kind():
        RUNTIME.model_catalog.message = RUNTIME.undo_journal.last_action
        _sync_marker_pair_list()
    else:
        RUNTIME.marker_selection.message = RUNTIME.undo_journal.last_action
        _sync_marker_pair_list()
    set_workspace_status_text(session_align_workspace(), RUNTIME.undo_journal.last_action)
    if kind != icp_commit_command_kind():
        notify_project_state_changed("확정 작업 Undo/Redo")
    request_redraw()


def can_redo_icp_commit_during_marker_preview() -> bool:
    """ICP 승인 Undo가 복원한 marker preview에서 승인 Redo만 허용합니다."""

    from .alignment.icp_runtime import icp_commit_command_kind
    from .operators.marker_alignment import is_marker_preview_active

    return bool(
        is_marker_preview_active()
        and not RUNTIME.undo_journal.scope_active
        and RUNTIME.undo_journal.next_redo_kind == icp_commit_command_kind()
    )


def _marker_preview_blocks_history(*, redo: bool = False) -> bool:
    """marker preview 중 일반 이력은 막되 ICP 승인 Redo 하나만 통과시킵니다."""
    from .operators.marker_alignment import is_marker_preview_active

    if not is_marker_preview_active():
        return False
    if redo and can_redo_icp_commit_during_marker_preview():
        return False
    message = "마커 정렬 미리보기 중에는 마커 Undo·Redo를 사용할 수 없습니다."
    RUNTIME.marker_selection.message = message
    set_workspace_status_text(session_align_workspace(), message)
    request_redraw()
    return True


def cancel_axis_edit_for_lifecycle() -> None:
    """세션·파일 수명이 끝나면 축 미리보기와 팝업 참조를 함께 폐기합니다."""
    from .axis_settings import cancel_axis_edit
    from .operators.axis_settings import clear_axis_edit_dialogs

    cancel_axis_edit(RUNTIME)
    clear_axis_edit_dialogs()


def _axis_edit_blocks_history() -> bool:
    """축 팝업에서 다른 확정 이력이 실행되어 편집 기준이 바뀌는 것을 막습니다."""
    reason = axis_edit_active_block_reason(RUNTIME)
    if reason is None:
        return False
    RUNTIME.model_catalog.message = reason
    set_workspace_status_text(session_align_workspace(), reason)
    request_redraw()
    return True


def undo_session_state() -> bool:
    """열린 드래프트를 우선하고 없으면 공용 확정 세션 명령을 되돌립니다."""
    if _axis_edit_blocks_history() or _marker_preview_blocks_history():
        return False
    journal = RUNTIME.undo_journal
    if journal.scope_active:
        changed = journal.undo_draft(RUNTIME.marker_selection)
        RUNTIME.marker_selection.message = RUNTIME.undo_journal.last_action
        _sync_marker_pair_list()
        set_workspace_status_text(session_align_workspace(), RUNTIME.undo_journal.last_action)
        request_redraw()
    else:
        changed = journal.undo_committed(_restore_committed_session_snapshot)
        _sync_after_committed_restore(journal.last_kind)
    return changed


def redo_session_state() -> bool:
    """열린 드래프트를 우선하고 없으면 공용 확정 세션 Redo 명령을 복원합니다."""
    if _axis_edit_blocks_history() or _marker_preview_blocks_history(redo=True):
        return False
    journal = RUNTIME.undo_journal
    if journal.scope_active:
        changed = journal.redo_draft(RUNTIME.marker_selection)
        RUNTIME.marker_selection.message = RUNTIME.undo_journal.last_action
        _sync_marker_pair_list()
        set_workspace_status_text(session_align_workspace(), RUNTIME.undo_journal.last_action)
        request_redraw()
    else:
        changed = journal.redo_committed(_restore_committed_session_snapshot)
        _sync_after_committed_restore(journal.last_kind)
    return changed


def _sync_marker_pair_list() -> None:
    """Undo 뒤 N 패널 UIList 미러를 안전하게 최신 확정 목록으로 맞춥니다."""
    from .properties import sync_marker_pair_list

    sync_marker_pair_list()


def undo_session_marker_state() -> bool:
    """이전 호출부 호환을 위해 공용 세션 Undo를 실행합니다."""
    return undo_session_state()


def redo_session_marker_state() -> bool:
    """이전 호출부 호환을 위해 공용 세션 Redo를 실행합니다."""
    return redo_session_state()


def abort_session_entry(error_message: str) -> None:
    """구성 실패 시 원래 WorkSpace로 안전하게 복귀한 뒤 세션을 INACTIVE로 정리합니다."""
    session = RUNTIME.session
    RUNTIME.workspace_setup_error = error_message
    _set_session_message(error_message)
    session.session_active = False
    session.phase = SESSION_EXITING
    session.exit_requested = True
    _clear_pending_marker_selection()
    RUNTIME.undo_journal.clear()
    window = session_target_window()
    if window is not None:
        _restore_source_workspace(window)
    else:
        RUNTIME.workspace_setup_error = f"{error_message} 원래 작업공간으로 복귀할 Window가 없습니다."
    invalidate_registry()
    _clear_session_status_text()
    session.clear()
    request_redraw()


def _recover_workspace_after_change() -> float | None:
    """작업공간 변경 알림 뒤 0초 타이머에서 대상 Window만 COLMAP Align으로 복귀합니다."""
    session = RUNTIME.session
    if not session.session_active or session.phase != SESSION_ACTIVE:
        session.recovery_pending = False
        return None
    window = session_target_window()
    align_workspace = session_align_workspace()
    if window is None or align_workspace is None:
        session.recovery_pending = False
        RUNTIME.workspace_setup_error = "COLMAP Align 작업공간 복구 대상을 찾지 못했습니다."
        _set_session_message(RUNTIME.workspace_setup_error)
        return None
    try:
        if not session.is_align_workspace(window.workspace):
            window.workspace = align_workspace
            invalidate_registry()
            return 0.05
        invalidate_registry()
        from .operators.workspace import configure_moving_sidebar, schedule_sidebar_activation

        if not configure_moving_sidebar(window):
            schedule_sidebar_activation()
        refresh_registry(window)
        if not session.input_guard_active:
            from .operators.session_guard import start_input_guard

            start_input_guard(window)
        _set_session_message("작업공간 전환을 감지하여 COLMAP Align으로 복귀했습니다.")
    except (AttributeError, ReferenceError, RuntimeError) as error:
        RUNTIME.workspace_setup_error = f"COLMAP Align 작업공간 복구에 실패했습니다: {error}"
        _set_session_message(RUNTIME.workspace_setup_error)
    finally:
        session.recovery_pending = False
        request_redraw()
    return None


def schedule_workspace_recovery() -> None:
    """MsgBus 알림에서 직접 전환하지 않고 0초 타이머로 사후 복구를 예약합니다."""
    session = RUNTIME.session
    if not session.session_active or session.phase != SESSION_ACTIVE:
        return
    window = session_target_window()
    if window is None or session.is_align_workspace(window.workspace):
        return
    session.recovery_pending = True
    _set_session_message("작업공간 변경을 감지했습니다. COLMAP Align으로 복귀합니다.")
    if not bpy.app.timers.is_registered(_recover_workspace_after_change):
        bpy.app.timers.register(_recover_workspace_after_change, first_interval=0.0)


def cancel_workspace_recovery() -> None:
    """파일 로드·등록 해제 전에 예약된 작업공간 사후 복구를 제거합니다."""
    if bpy.app.timers.is_registered(_recover_workspace_after_change):
        bpy.app.timers.unregister(_recover_workspace_after_change)
    RUNTIME.session.recovery_pending = False


def cancel_session_exit_finish() -> None:
    """등록 해제·파일 로드 전에 대기 중인 세션 종료 타이머를 제거합니다."""
    if bpy.app.timers.is_registered(_finish_session_exit):
        bpy.app.timers.unregister(_finish_session_exit)
    RUNTIME.session_exit_task = None


def _invalidate_icp_for_lifecycle(reason: str) -> None:
    """worker 종료·job 정리 뒤에만 ICP poll timer를 해제합니다."""

    from .alignment.icp_runtime import invalidate_icp
    from .operators.marker_alignment import stop_icp_poll_timer_for_lifecycle

    invalidate_icp(RUNTIME, reason)
    stop_icp_poll_timer_for_lifecycle()


def _invalidate_merge_export_for_lifecycle(reason: str) -> None:
    """worker 후보 출력 정리 뒤에만 구조 통합 poll timer를 해제합니다."""

    from .operators.export_model import invalidate_merge_export_for_lifecycle

    invalidate_merge_export_for_lifecycle(reason)


def _invalidate_obj_export_for_lifecycle(reason: str) -> None:
    """후보 출력과 파일 handle을 정리한 뒤 OBJ Export timer를 해제합니다."""

    from .operators.export_obj import invalidate_obj_export_for_lifecycle

    invalidate_obj_export_for_lifecycle(reason)


def clear_session_for_file_load() -> None:
    """새 파일에 이전 Runtime을 넘기지 않도록 복원·세션·worker 참조를 모두 비웁니다."""
    from .project_persistence.runtime import cancel_project_restore_for_file_load

    cancel_project_restore_for_file_load()
    cancel_workspace_recovery()
    cancel_session_exit_finish()
    _invalidate_merge_export_for_lifecycle("새 Blender 파일을 불러와 구조 통합 작업을 정리했습니다.")
    _invalidate_obj_export_for_lifecycle("새 Blender 파일을 불러와 OBJ 내보내기 작업을 정리했습니다.")
    _invalidate_icp_for_lifecycle("새 Blender 파일을 불러와 ICP 작업을 정리했습니다.")
    from .operators.marker_alignment import discard_marker_preview_for_lifecycle
    from .operators.session_guard import stop_input_guard_timer
    from .alignment.layer_transform import cancel_layer_transform

    cancel_axis_edit_for_lifecycle()
    discard_marker_preview_for_lifecycle()
    cancel_layer_transform(RUNTIME, message="파일 교체로 데이터 레이어 변환을 취소했습니다.")
    stop_input_guard_timer()
    RUNTIME.session.session_active = False
    _clear_pending_marker_selection()
    RUNTIME.undo_journal.clear()
    RUNTIME.role_view_states.clear()
    _clear_session_status_text()
    RUNTIME.session.clear()


def restore_session_for_unregister() -> None:
    """애드온 해제 시 원래 WorkSpace가 유효하면 최선으로 복귀하고 세션을 정리합니다."""
    cancel_workspace_recovery()
    cancel_session_exit_finish()
    _invalidate_merge_export_for_lifecycle("COLMAP Align 애드온을 해제해 구조 통합 작업을 정리했습니다.")
    _invalidate_obj_export_for_lifecycle("COLMAP Align 애드온을 해제해 OBJ 내보내기 작업을 정리했습니다.")
    _invalidate_icp_for_lifecycle("COLMAP Align 애드온을 해제해 ICP 작업을 정리했습니다.")
    from .operators.marker_alignment import discard_marker_preview_for_lifecycle
    from .operators.session_guard import stop_input_guard_timer
    from .alignment.layer_transform import cancel_layer_transform

    cancel_axis_edit_for_lifecycle()
    discard_marker_preview_for_lifecycle()
    cancel_layer_transform(RUNTIME, message="애드온 해제로 데이터 레이어 변환을 취소했습니다.")
    stop_input_guard_timer()
    session = RUNTIME.session
    session.session_active = False
    _clear_pending_marker_selection()
    RUNTIME.undo_journal.clear()
    window = session_target_window()
    if window is not None and session.phase in {SESSION_ENTERING, SESSION_ACTIVE, SESSION_EXITING}:
        _restore_source_workspace(window)
    _clear_session_status_text()
    session.clear()
    invalidate_registry()
    request_redraw()
