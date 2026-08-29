"""COLMAP Modifier 전용 Workspace Session의 상태와 ID 수명을 관리합니다."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import bpy

try:
    from ....shared.blender_runtime.workspace_factory import (
        source_workspace_from_metadata,
        workspace_matches_spec,
    )
except ImportError:
    from shared.blender_runtime.workspace_factory import (
        source_workspace_from_metadata,
        workspace_matches_spec,
    )

from .modifier_session_constants import (
    CONTROL_PANEL_CATEGORY,
    MAIN_SCREEN_OWNER_KEY,
    WORKSPACE_NAME,
    WORKSPACE_OWNER_KEY,
    WORKSPACE_SOURCE_KEY,
    WORKSPACE_VISIBILITY_SNAPSHOT_KEY,
)


SESSION_INACTIVE = "INACTIVE"
SESSION_ENTERING = "ENTERING"
SESSION_ACTIVE = "ACTIVE"
SESSION_EXITING = "EXITING"


def _pointer(value: Any) -> int:
    """유효한 Blender RNA 객체의 런타임 포인터를 반환합니다."""

    if value is None:
        return 0
    try:
        return int(value.as_pointer())
    except (AttributeError, ReferenceError, RuntimeError):
        return 0


@dataclass
class ModifierWorkspaceSession:
    """현재 Window의 Modifier Workspace 전환 상태만 보관합니다."""

    generation: int = 0
    phase: str = SESSION_INACTIVE
    target_window_ref: Any = None
    target_window_pointer: int = 0
    source_workspace_ref: Any = None
    source_workspace_pointer: int = 0
    source_workspace_name: str = ""
    source_view_layer_ref: Any = None
    source_view_layer_pointer: int = 0
    source_view_layer_name: str = ""
    modifier_workspace_ref: Any = None
    modifier_workspace_pointer: int = 0
    modifier_workspace_name: str = ""
    control_screen_pointer: int = 0
    control_area_pointer: int = 0
    recovery_pending: bool = False
    exit_requested: bool = False
    status_message: str = ""
    visibility_view_layer_name: str = ""
    default_object_visibility: tuple[tuple[Any, int, str, str, bool], ...] = ()

    def begin_entering(
        self,
        window: Any,
        source_workspace: Any,
        source_view_layer: Any,
    ) -> None:
        """진입 대상 Window와 원래 Workspace·View Layer를 저장합니다."""

        self.generation += 1
        self.phase = SESSION_ENTERING
        self.target_window_ref = window
        self.target_window_pointer = _pointer(window)
        self.source_workspace_ref = source_workspace
        self.source_workspace_pointer = _pointer(source_workspace)
        self.source_workspace_name = str(source_workspace.name)
        self.source_view_layer_ref = source_view_layer
        self.source_view_layer_pointer = _pointer(source_view_layer)
        self.source_view_layer_name = str(source_view_layer.name)
        self.modifier_workspace_ref = None
        self.modifier_workspace_pointer = 0
        self.modifier_workspace_name = ""
        self.control_screen_pointer = 0
        self.control_area_pointer = 0
        self.recovery_pending = False
        self.exit_requested = False
        self.status_message = "COLMAP Modifier Workspace를 준비 중입니다."
        self.visibility_view_layer_name = ""
        self.default_object_visibility = ()

    def bind_modifier_workspace(self, workspace: Any) -> None:
        """현재 Session에 애드온 소유 Workspace를 연결합니다."""

        self.modifier_workspace_ref = workspace
        self.modifier_workspace_pointer = _pointer(workspace)
        self.modifier_workspace_name = str(workspace.name)

    def bind_control_area(self, window: Any, area: Any) -> None:
        """구성이 끝난 Screen과 Control Area 포인터를 갱신합니다."""

        self.control_screen_pointer = _pointer(window.screen)
        self.control_area_pointer = _pointer(area)

    def bind_default_object_visibility(
        self,
        view_layer_name: str,
        states: tuple[tuple[Any, int, str, str, bool], ...],
    ) -> None:
        """진입 전 기본 오브젝트의 View Layer 숨김 상태를 저장합니다."""

        self.visibility_view_layer_name = str(view_layer_name)
        self.default_object_visibility = tuple(states)

    def clear_default_object_visibility(self) -> None:
        """복원 완료된 기본 오브젝트 숨김 Snapshot을 폐기합니다."""

        self.visibility_view_layer_name = ""
        self.default_object_visibility = ()

    def clear(self) -> None:
        """파일 교체나 종료 뒤 재사용하면 안 되는 Blender 참조를 폐기합니다."""

        self.generation += 1
        self.phase = SESSION_INACTIVE
        self.target_window_ref = None
        self.target_window_pointer = 0
        self.source_workspace_ref = None
        self.source_workspace_pointer = 0
        self.source_workspace_name = ""
        self.source_view_layer_ref = None
        self.source_view_layer_pointer = 0
        self.source_view_layer_name = ""
        self.modifier_workspace_ref = None
        self.modifier_workspace_pointer = 0
        self.modifier_workspace_name = ""
        self.control_screen_pointer = 0
        self.control_area_pointer = 0
        self.recovery_pending = False
        self.exit_requested = False
        self.status_message = ""
        self.visibility_view_layer_name = ""
        self.default_object_visibility = ()


SESSION = ModifierWorkspaceSession()


def _window_manager():
    """현재 Blender WindowManager를 안전하게 반환합니다."""

    return getattr(bpy.context, "window_manager", None)


def window_is_live(window: Any) -> bool:
    """저장한 Window가 현재 WindowManager에 남아 있는지 확인합니다."""

    manager = _window_manager()
    pointer = _pointer(window)
    if manager is None or not pointer:
        return False
    return any(_pointer(candidate) == pointer for candidate in manager.windows)


def _workspace_by_identity(reference: Any, pointer: int, name: str):
    """저장 참조, 포인터와 이름 순서로 현재 Workspace를 다시 찾습니다."""

    try:
        if reference is not None and _pointer(reference) == pointer:
            return reference
    except ReferenceError:
        pass
    if pointer:
        for workspace in bpy.data.workspaces:
            if _pointer(workspace) == pointer:
                return workspace
    return bpy.data.workspaces.get(name) if name else None


def session_target_window():
    """현재 Session이 소유한 살아 있는 Window를 반환합니다."""

    return SESSION.target_window_ref if window_is_live(SESSION.target_window_ref) else None


def session_source_workspace():
    """현재 Blender 데이터에서 진입 전 Workspace를 다시 찾습니다."""

    workspace = _workspace_by_identity(
        SESSION.source_workspace_ref,
        SESSION.source_workspace_pointer,
        SESSION.source_workspace_name,
    )
    if is_owned_modifier_workspace(workspace) or is_legacy_modifier_workspace(workspace):
        return None
    return workspace


def session_source_view_layer():
    """현재 Scene에서 진입 전 일반 View Layer를 Pointer 우선으로 다시 찾습니다."""

    from .modifier_data_visibility import is_modifier_data_view_layer

    window = session_target_window()
    scene = getattr(window, "scene", None)
    if scene is None:
        return None
    view_layers = tuple(scene.view_layers)
    source_pointer = int(SESSION.source_view_layer_pointer)
    view_layer = next(
        (
            candidate
            for candidate in view_layers
            if source_pointer and _pointer(candidate) == source_pointer
        ),
        None,
    )
    if view_layer is None and SESSION.source_view_layer_name:
        view_layer = scene.view_layers.get(SESSION.source_view_layer_name)
    if view_layer is None or is_modifier_data_view_layer(view_layer):
        return None
    return view_layer


def session_modifier_workspace():
    """현재 Blender 데이터에서 Modifier Workspace를 다시 찾습니다."""

    workspace = _workspace_by_identity(
        SESSION.modifier_workspace_ref,
        SESSION.modifier_workspace_pointer,
        SESSION.modifier_workspace_name,
    )
    return workspace if is_owned_modifier_workspace(workspace) else None


def is_owned_modifier_workspace(workspace: Any) -> bool:
    """공통 Tool ID·UUID·Schema가 일치하는 Modifier Workspace인지 확인합니다."""

    try:
        from .modifier_workspace_contract import MODIFIER_WORKSPACE_SPEC

        return workspace_matches_spec(
            workspace,
            MODIFIER_WORKSPACE_SPEC,
            require_ready=False,
        )
    except (AttributeError, ReferenceError, RuntimeError):
        return False


def is_legacy_modifier_workspace(workspace: Any) -> bool:
    """구 소유 플래그만 있고 현행 메타데이터가 없는 Legacy Workspace인지 확인합니다."""

    try:
        return bool(
            workspace is not None
            and workspace.get(WORKSPACE_OWNER_KEY, False)
            and not is_owned_modifier_workspace(workspace)
        )
    except (AttributeError, ReferenceError, RuntimeError):
        return False


def is_session_active_for_window(window: Any) -> bool:
    """주어진 Window가 활성 Modifier Session의 정확한 대상인지 확인합니다."""

    if SESSION.phase != SESSION_ACTIVE or _pointer(window) != SESSION.target_window_pointer:
        return False
    try:
        return _pointer(window.workspace) == SESSION.modifier_workspace_pointer
    except (AttributeError, ReferenceError, RuntimeError):
        return False


def is_session_owned_by_window(window: Any) -> bool:
    """Workspace 탭이 바뀌어도 지정 Window가 현재 Modifier 세션 대상인지 반환합니다."""

    return bool(
        SESSION.phase != SESSION_INACTIVE
        and _pointer(window) == SESSION.target_window_pointer
        and window_is_live(window)
    )


def modifier_panel_poll(context: Any) -> bool:
    """전용 Workspace의 등록된 Control View에서만 패널 표시를 허용합니다."""

    window = getattr(context, "window", None)
    area = getattr(context, "area", None)
    if not is_session_active_for_window(window) or area is None:
        return False
    try:
        return bool(
            area.type == "VIEW_3D"
            and _pointer(window.screen) == SESSION.control_screen_pointer
            and window.screen.get(MAIN_SCREEN_OWNER_KEY, False)
            and _pointer(area) == SESSION.control_area_pointer
        )
    except (AttributeError, ReferenceError, RuntimeError):
        return False


def _source_view_layer_for_entry(window: Any) -> Any:
    """Modifier 진입 전 복원할 일반 View Layer를 확정합니다."""

    from .modifier_data_visibility import is_modifier_data_view_layer

    scene = getattr(window, "scene", None)
    if scene is None:
        raise RuntimeError("Modifier 진입 전 Scene을 찾을 수 없습니다.")
    current = getattr(window, "view_layer", None)
    if current is not None and not is_modifier_data_view_layer(current):
        return current
    existing = next(
        (
            view_layer
            for view_layer in scene.view_layers
            if not is_modifier_data_view_layer(view_layer)
        ),
        None,
    )
    if existing is not None:
        return existing
    try:
        return scene.view_layers.new("ViewLayer")
    except (AttributeError, ReferenceError, RuntimeError, TypeError) as error:
        raise RuntimeError(f"복귀용 일반 View Layer 생성에 실패했습니다: {error}") from error


def begin_session_entry(window: Any) -> None:
    """원래 Workspace·View Layer를 보존하고 Session을 ENTERING으로 전환합니다."""

    if SESSION.phase != SESSION_INACTIVE:
        raise RuntimeError("COLMAP Modifier Session이 이미 준비 또는 실행 중입니다.")
    if window is None or getattr(window, "workspace", None) is None:
        raise RuntimeError("COLMAP Modifier로 전환할 현재 Window가 없습니다.")
    source_workspace = window.workspace
    if is_owned_modifier_workspace(source_workspace):
        source_workspace = source_workspace_from_metadata(source_workspace)
        if source_workspace is None or is_owned_modifier_workspace(source_workspace):
            raise RuntimeError("진입 전 사용자 Workspace를 찾을 수 없습니다.")
    elif is_legacy_modifier_workspace(source_workspace):
        source_name = str(source_workspace.get(WORKSPACE_SOURCE_KEY, "") or "")
        source_workspace = bpy.data.workspaces.get(source_name)
        if (
            source_workspace is None
            or is_owned_modifier_workspace(source_workspace)
            or is_legacy_modifier_workspace(source_workspace)
        ):
            raise RuntimeError("Legacy Workspace의 진입 전 사용자 Workspace를 찾을 수 없습니다.")
    SESSION.begin_entering(
        window,
        source_workspace,
        _source_view_layer_for_entry(window),
    )


def bind_modifier_workspace(workspace: Any) -> None:
    """ENTERING Session에 검증된 Modifier Workspace를 연결합니다."""

    if SESSION.phase != SESSION_ENTERING:
        raise RuntimeError("COLMAP Modifier 진입 상태가 아닙니다.")
    if not is_owned_modifier_workspace(workspace):
        raise RuntimeError("애드온 소유 COLMAP Modifier Workspace가 아닙니다.")
    SESSION.bind_modifier_workspace(workspace)


def activate_session(window: Any, area: Any) -> None:
    """Workspace 구성이 끝난 Session을 ACTIVE로 확정합니다."""

    if SESSION.phase not in {SESSION_ENTERING, SESSION_ACTIVE}:
        raise RuntimeError("활성화할 COLMAP Modifier Session이 없습니다.")
    if _pointer(window) != SESSION.target_window_pointer:
        raise RuntimeError("COLMAP Modifier Session 대상 Window가 일치하지 않습니다.")
    if _pointer(window.workspace) != SESSION.modifier_workspace_pointer:
        raise RuntimeError("COLMAP Modifier Workspace가 대상 Window에 연결되지 않았습니다.")
    SESSION.bind_control_area(window, area)
    SESSION.phase = SESSION_ACTIVE
    SESSION.recovery_pending = False
    SESSION.exit_requested = False
    SESSION.status_message = "COLMAP Modifier Session이 활성화되었습니다."


def restore_source_workspace() -> bool:
    """Session 대상 Window를 진입 전 Workspace·View Layer로 복귀시킵니다."""

    window = session_target_window()
    source_workspace = session_source_workspace()
    if window is None or source_workspace is None:
        return False
    try:
        from .modifier_data_visibility import synchronize_modifier_data_visibility

        source_view_layer = session_source_view_layer()
        if source_view_layer is None:
            source_view_layer = _source_view_layer_for_entry(window)
        synchronize_modifier_data_visibility(
            window.scene,
            create_view_layer=True,
        )
        window.view_layer = source_view_layer
        window.workspace = source_workspace
        return True
    except (AttributeError, ReferenceError, RuntimeError, TypeError) as error:
        SESSION.status_message = f"진입 전 Workspace·View Layer 복귀 실패: {error}"
        return False


def abort_session_entry(message: str) -> None:
    """진입 실패를 기록하고 가능한 경우 원래 Workspace로 복귀합니다."""

    SESSION.status_message = str(message)
    restore_source_workspace()
    SESSION.clear()


def begin_session_exit() -> None:
    """정리 완료 전 자동 Workspace 복구를 막도록 EXITING으로 전환합니다."""

    if SESSION.phase != SESSION_ACTIVE:
        raise RuntimeError("종료할 COLMAP Modifier Session이 없습니다.")
    SESSION.phase = SESSION_EXITING
    SESSION.exit_requested = True
    SESSION.recovery_pending = False
    SESSION.status_message = "COLMAP Modifier Session을 종료하는 중입니다."


def complete_session_exit() -> None:
    """원래 Workspace 복귀 이후 모든 Session 참조를 초기화합니다."""

    SESSION.clear()


__all__ = [
    "CONTROL_PANEL_CATEGORY",
    "MAIN_SCREEN_OWNER_KEY",
    "ModifierWorkspaceSession",
    "SESSION",
    "SESSION_ACTIVE",
    "SESSION_ENTERING",
    "SESSION_EXITING",
    "SESSION_INACTIVE",
    "WORKSPACE_NAME",
    "WORKSPACE_OWNER_KEY",
    "WORKSPACE_SOURCE_KEY",
    "WORKSPACE_VISIBILITY_SNAPSHOT_KEY",
    "abort_session_entry",
    "activate_session",
    "begin_session_entry",
    "begin_session_exit",
    "bind_modifier_workspace",
    "complete_session_exit",
    "is_owned_modifier_workspace",
    "is_legacy_modifier_workspace",
    "is_session_active_for_window",
    "is_session_owned_by_window",
    "modifier_panel_poll",
    "restore_source_workspace",
    "session_modifier_workspace",
    "session_source_view_layer",
    "session_source_workspace",
    "session_target_window",
    "window_is_live",
]
