"""도구 전용 Workspace Seed의 Blender ID 생명주기를 안전하게 관리합니다."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

try:
    import bpy
except ModuleNotFoundError:  # Blender 밖에서는 계약 타입과 순수 함수만 사용할 수 있습니다.
    bpy = None


WORKSPACE_OWNER_KEY = "colmap_tools_workspace_owner"
WORKSPACE_TOOL_ID_KEY = "colmap_tools_workspace_tool_id"
WORKSPACE_UUID_KEY = "colmap_tools_workspace_uuid"
WORKSPACE_SCHEMA_KEY = "colmap_tools_workspace_schema"
WORKSPACE_SEED_SCHEMA_KEY = "colmap_tools_workspace_seed_schema"
WORKSPACE_SOURCE_KEY = "colmap_tools_source_workspace"
WORKSPACE_STATE_KEY = "colmap_tools_workspace_state"
SCREEN_OWNER_KEY = "colmap_tools_screen_owner"

STATE_CREATING = "CREATING"
STATE_READY = "READY"
STATE_FAILED = "FAILED"

_OWNER_VALUE = "colmap_tools"


class WorkspaceFactoryError(RuntimeError):
    """Workspace 생성·검증·정리 계약 위반을 나타냅니다."""


@dataclass(frozen=True, slots=True)
class WorkspaceSeedSpec:
    """도구별 Workspace Seed와 지원 Schema를 정의합니다."""

    tool_id: str
    display_name: str
    seed_workspace_name: str
    seed_path: Path
    workspace_schema: int = 2
    seed_schema: int = 1

    def __post_init__(self) -> None:
        """빈 식별자와 잘못된 Schema를 생성 시점에 차단합니다."""

        if not self.tool_id.strip():
            raise ValueError("Workspace 도구 ID는 비어 있을 수 없습니다.")
        if not self.display_name.strip():
            raise ValueError("Workspace 표시 이름은 비어 있을 수 없습니다.")
        if not self.seed_workspace_name.strip():
            raise ValueError("Seed 내부 Workspace 이름은 비어 있을 수 없습니다.")
        if self.workspace_schema < 1 or self.seed_schema < 1:
            raise ValueError("Workspace와 Seed Schema는 1 이상이어야 합니다.")
        object.__setattr__(self, "seed_path", Path(self.seed_path))


def pointer(value: Any) -> int:
    """Blender RNA 객체의 세션 내 Pointer를 정수로 반환합니다."""

    if value is None:
        return 0
    as_pointer = getattr(value, "as_pointer", None)
    if callable(as_pointer):
        try:
            return int(as_pointer())
        except (ReferenceError, RuntimeError, TypeError, ValueError):
            return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _property_value(data_block: Any, key: str, default: Any = None) -> Any:
    """유효하지 않은 Blender ID도 안전하게 Custom Property를 조회합니다."""

    try:
        return data_block.get(key, default)
    except (AttributeError, ReferenceError, RuntimeError):
        return default


def _require_blender() -> Any:
    """Blender Python 밖에서 Runtime API가 호출되면 명확히 중단합니다."""

    if bpy is None:
        raise WorkspaceFactoryError("Workspace Factory는 Blender Python에서만 실행할 수 있습니다.")
    return bpy


def _workspace_exists(workspace: Any) -> bool:
    """Workspace Pointer가 현재 Blender 데이터에 남아 있는지 확인합니다."""

    blender = _require_blender()
    target_pointer = pointer(workspace)
    return bool(target_pointer) and any(
        pointer(candidate) == target_pointer for candidate in blender.data.workspaces
    )


def _screen_belongs_to_workspace(screen: Any, workspace: Any) -> bool:
    """Screen이 해당 Workspace의 읽기 전용 Screen 집합에 속하는지 확인합니다."""

    screen_pointer = pointer(screen)
    if not screen_pointer:
        return False
    try:
        return any(pointer(candidate) == screen_pointer for candidate in workspace.screens)
    except (AttributeError, ReferenceError, RuntimeError):
        return False


def owned_uuid_of(workspace: Any) -> str:
    """공통 소유 Workspace의 유효한 UUID를 반환하고 아니면 빈 문자열을 반환합니다."""

    if _property_value(workspace, WORKSPACE_OWNER_KEY) != _OWNER_VALUE:
        return ""
    value = _property_value(workspace, WORKSPACE_UUID_KEY, "")
    return value if isinstance(value, str) and value.strip() else ""


def workspace_matches_spec(
    workspace: Any,
    spec: WorkspaceSeedSpec,
    require_ready: bool = True,
) -> bool:
    """Workspace 소유권과 도구·Schema·상태가 명세와 일치하는지 확인합니다."""

    if not owned_uuid_of(workspace):
        return False
    expected = (
        (_property_value(workspace, WORKSPACE_TOOL_ID_KEY), spec.tool_id),
        (_property_value(workspace, WORKSPACE_SCHEMA_KEY), spec.workspace_schema),
        (_property_value(workspace, WORKSPACE_SEED_SCHEMA_KEY), spec.seed_schema),
    )
    if any(actual != wanted for actual, wanted in expected):
        return False
    state = _property_value(workspace, WORKSPACE_STATE_KEY)
    return state == STATE_READY if require_ready else state in {
        STATE_CREATING,
        STATE_READY,
        STATE_FAILED,
    }


def screen_matches_workspace(screen: Any, workspace: Any) -> bool:
    """Screen 소유 UUID와 Workspace 연결 관계가 모두 유효한지 확인합니다."""

    workspace_uuid = owned_uuid_of(workspace)
    return bool(workspace_uuid) and _screen_belongs_to_workspace(
        screen, workspace
    ) and _property_value(screen, SCREEN_OWNER_KEY) == workspace_uuid


def _validate_seed_before_append(spec: WorkspaceSeedSpec) -> Path:
    """Seed 파일과 내부 Workspace ID를 Append 전에 읽기 전용으로 검증합니다."""

    blender = _require_blender()
    seed_path = spec.seed_path.expanduser().resolve()
    if not seed_path.is_file():
        raise WorkspaceFactoryError(f"Workspace Seed 파일이 없습니다: {seed_path}")
    if seed_path.suffix.lower() != ".blend":
        raise WorkspaceFactoryError(f"Workspace Seed는 .blend 파일이어야 합니다: {seed_path}")
    try:
        with blender.data.libraries.load(str(seed_path), link=False) as (data_from, _data_to):
            workspace_names = tuple(data_from.workspaces)
    except Exception as error:
        raise WorkspaceFactoryError(f"Workspace Seed를 읽을 수 없습니다: {seed_path}") from error
    if spec.seed_workspace_name not in workspace_names:
        raise WorkspaceFactoryError(
            f"Seed 내부 Workspace ID가 없습니다: {spec.seed_workspace_name}"
        )
    return seed_path


def workspace_is_used_by_other_window(workspace: Any, window: Any) -> bool:
    """현재 Window를 제외한 다른 Window가 Workspace를 사용하는지 확인합니다."""

    blender = _require_blender()
    target_pointer = pointer(workspace)
    current_pointer = pointer(window)
    if not target_pointer:
        return False
    try:
        windows = blender.context.window_manager.windows
    except (AttributeError, ReferenceError, RuntimeError):
        return False
    return any(
        pointer(candidate) != current_pointer
        and pointer(getattr(candidate, "workspace", None)) == target_pointer
        for candidate in windows
    )


def _restore_window_workspace(window: Any, workspace: Any) -> bool:
    """유효한 Workspace일 때만 Window를 원래 Workspace로 복귀시킵니다."""

    if workspace is None or not _workspace_exists(workspace):
        return False
    try:
        window.workspace = workspace
        return pointer(window.workspace) == pointer(workspace)
    except (AttributeError, ReferenceError, RuntimeError, TypeError):
        return False


def _schedule_workspace_restore(window: Any, workspace: Any) -> None:
    """Workspace 삭제 Event Tick 뒤 유효한 복귀 Workspace 전환을 예약합니다."""

    blender = _require_blender()

    def restore_after_delete() -> None:
        """삭제 Notifier 처리 뒤 Window의 복귀 Workspace를 적용합니다."""

        _restore_window_workspace(window, workspace)
        return None

    try:
        blender.app.timers.register(restore_after_delete, first_interval=0.01)
    except (AttributeError, ReferenceError, RuntimeError, TypeError):
        _restore_window_workspace(window, workspace)


def _delete_new_workspace_after_failure(
    context: Any,
    window: Any,
    workspace: Any,
    source_workspace: Any,
) -> bool:
    """이번 Append로 생긴 Workspace만 Pointer 근거로 실패 정리합니다."""

    blender = _require_blender()
    if not _workspace_exists(workspace) or workspace_is_used_by_other_window(workspace, window):
        return False
    if len(blender.data.workspaces) <= 1:
        return False
    try:
        if pointer(getattr(window, "workspace", None)) != pointer(workspace):
            _restore_window_workspace(window, source_workspace)
            return False
        with context.temp_override(window=window):
            result = blender.ops.workspace.delete()
        deleted = "FINISHED" in result
    except (AttributeError, ReferenceError, RuntimeError, TypeError):
        deleted = False
    if deleted:
        _schedule_workspace_restore(window, source_workspace)
    else:
        _restore_window_workspace(window, source_workspace)
    return deleted


def append_seed_workspace(
    context: Any,
    window: Any,
    spec: WorkspaceSeedSpec,
    source_workspace: Any,
    *,
    preserve_seed_ui: bool = False,
) -> Any:
    """검증된 Seed를 선택적으로 UI 설정까지 보존해 Append하고 CREATING 소유 상태로 전환합니다."""

    blender = _require_blender()
    if window is None or pointer(window) == 0:
        raise WorkspaceFactoryError("Workspace를 활성화할 Window가 유효하지 않습니다.")
    if source_workspace is None or not _workspace_exists(source_workspace):
        raise WorkspaceFactoryError("진입 전 Source Workspace가 유효하지 않습니다.")
    active_workspace = getattr(window, "workspace", None)
    if active_workspace is None or not _workspace_exists(active_workspace):
        raise WorkspaceFactoryError("Window의 활성 Workspace가 유효하지 않습니다.")
    if pointer(getattr(window, "screen", None)) == 0:
        raise WorkspaceFactoryError("Window의 활성 Screen이 유효하지 않습니다.")

    seed_path = _validate_seed_before_append(spec)
    before_pointers = {pointer(workspace) for workspace in blender.data.workspaces}
    appended_workspace = None
    try:
        if preserve_seed_ui:
            # append_activate는 영역 폭을 기본값으로 초기화하므로 저장된 UI가 필요하면 Library로 읽습니다.
            with blender.data.libraries.load(str(seed_path), link=False) as (_data_from, data_to):
                data_to.workspaces = [spec.seed_workspace_name]
        else:
            with context.temp_override(window=window):
                result = blender.ops.workspace.append_activate(
                    idname=spec.seed_workspace_name,
                    filepath=str(seed_path),
                )
            if "FINISHED" not in result:
                raise WorkspaceFactoryError(f"Workspace Seed Append가 완료되지 않았습니다: {result}")
        appended = [
            workspace
            for workspace in blender.data.workspaces
            if pointer(workspace) not in before_pointers
        ]
        if len(appended) != 1:
            raise WorkspaceFactoryError(
                f"Workspace Seed Append 결과는 정확히 하나여야 합니다: {len(appended)}개"
            )
        appended_workspace = appended[0]
        seed_tool_id = _property_value(appended_workspace, WORKSPACE_TOOL_ID_KEY)
        seed_schema = _property_value(appended_workspace, WORKSPACE_SEED_SCHEMA_KEY)

        workspace_uuid = str(uuid4())
        appended_workspace.name = spec.display_name
        appended_workspace[WORKSPACE_OWNER_KEY] = _OWNER_VALUE
        appended_workspace[WORKSPACE_TOOL_ID_KEY] = spec.tool_id
        appended_workspace[WORKSPACE_UUID_KEY] = workspace_uuid
        appended_workspace[WORKSPACE_SCHEMA_KEY] = spec.workspace_schema
        appended_workspace[WORKSPACE_SEED_SCHEMA_KEY] = spec.seed_schema
        appended_workspace[WORKSPACE_SOURCE_KEY] = source_workspace.name
        appended_workspace[WORKSPACE_STATE_KEY] = STATE_CREATING
        seed_screens = tuple(appended_workspace.screens)
        if len(seed_screens) != 1:
            raise WorkspaceFactoryError(
                f"Seed Workspace의 의존 Screen은 정확히 하나여야 합니다: {len(seed_screens)}개"
            )
        seed_screen = seed_screens[0]
        seed_screen[SCREEN_OWNER_KEY] = workspace_uuid
        if seed_tool_id != spec.tool_id:
            raise WorkspaceFactoryError("Seed의 Tool ID가 Workspace 명세와 다릅니다.")
        if seed_schema != spec.seed_schema:
            raise WorkspaceFactoryError("Seed Schema가 Workspace 명세와 다릅니다.")

        window.workspace = appended_workspace
        return appended_workspace
    except Exception as error:
        _restore_window_workspace(window, source_workspace)
        if appended_workspace is not None and _workspace_exists(appended_workspace):
            try:
                appended_workspace[WORKSPACE_STATE_KEY] = STATE_FAILED
            except (ReferenceError, RuntimeError, TypeError):
                pass
            _delete_new_workspace_after_failure(
                context,
                window,
                appended_workspace,
                source_workspace,
            )
        if isinstance(error, WorkspaceFactoryError):
            raise
        raise WorkspaceFactoryError("Workspace Seed Append 처리 중 오류가 발생했습니다.") from error


def mark_workspace_ready(workspace: Any, screen: Any, spec: WorkspaceSeedSpec) -> None:
    """소유권·Schema·Screen 연결을 검증한 Workspace를 READY로 확정합니다."""

    if not workspace_matches_spec(workspace, spec, require_ready=False):
        raise WorkspaceFactoryError("Workspace 소유권 또는 Schema가 명세와 다릅니다.")
    if _property_value(workspace, WORKSPACE_STATE_KEY) == STATE_FAILED:
        raise WorkspaceFactoryError("FAILED Workspace를 READY로 되돌릴 수 없습니다.")
    if not screen_matches_workspace(screen, workspace):
        raise WorkspaceFactoryError("Screen이 Workspace UUID와 올바르게 연결되지 않았습니다.")
    workspace[WORKSPACE_STATE_KEY] = STATE_READY


def mark_workspace_failed(workspace: Any) -> None:
    """유효한 공통 소유 Workspace를 FAILED 상태로 표시합니다."""

    if not owned_uuid_of(workspace):
        raise WorkspaceFactoryError("공통 소유권이 없는 Workspace는 실패 표시할 수 없습니다.")
    workspace[WORKSPACE_STATE_KEY] = STATE_FAILED


def source_workspace_from_metadata(workspace: Any) -> Any | None:
    """저장된 Source 이름과 일치하는 현재 Workspace를 보조 복구 대상으로 반환합니다."""

    blender = _require_blender()
    source_name = _property_value(workspace, WORKSPACE_SOURCE_KEY, "")
    if not isinstance(source_name, str) or not source_name:
        return None
    try:
        candidate = blender.data.workspaces.get(source_name)
    except (AttributeError, ReferenceError, RuntimeError):
        return None
    if candidate is None or pointer(candidate) == pointer(workspace):
        return None
    return candidate


def delete_owned_workspace(
    context: Any,
    window: Any,
    workspace: Any,
    spec: WorkspaceSeedSpec,
    restore_workspace: Any = None,
) -> bool:
    """명세와 일치하고 다른 Window가 쓰지 않는 소유 Workspace만 즉시 삭제합니다."""

    blender = _require_blender()
    if window is None or not workspace_matches_spec(workspace, spec, require_ready=False):
        return False
    if not _workspace_exists(workspace) or workspace_is_used_by_other_window(workspace, window):
        return False
    if len(blender.data.workspaces) <= 1:
        return False

    original_workspace = getattr(window, "workspace", None)
    restore_candidate = restore_workspace or source_workspace_from_metadata(workspace)
    if restore_candidate is workspace or not _workspace_exists(restore_candidate):
        restore_candidate = original_workspace if original_workspace is not workspace else None
    try:
        if pointer(getattr(window, "workspace", None)) != pointer(workspace):
            return False
        with context.temp_override(window=window):
            result = blender.ops.workspace.delete()
        deleted = "FINISHED" in result
    except (AttributeError, ReferenceError, RuntimeError, TypeError):
        deleted = False
    if deleted:
        if restore_candidate is not None:
            _schedule_workspace_restore(window, restore_candidate)
        return True
    if original_workspace is not None:
        _restore_window_workspace(window, original_workspace)
    return False


__all__ = [
    "SCREEN_OWNER_KEY",
    "STATE_CREATING",
    "STATE_FAILED",
    "STATE_READY",
    "WORKSPACE_OWNER_KEY",
    "WORKSPACE_SCHEMA_KEY",
    "WORKSPACE_SEED_SCHEMA_KEY",
    "WORKSPACE_SOURCE_KEY",
    "WORKSPACE_STATE_KEY",
    "WORKSPACE_TOOL_ID_KEY",
    "WORKSPACE_UUID_KEY",
    "WorkspaceFactoryError",
    "WorkspaceSeedSpec",
    "append_seed_workspace",
    "delete_owned_workspace",
    "mark_workspace_failed",
    "mark_workspace_ready",
    "owned_uuid_of",
    "pointer",
    "screen_matches_workspace",
    "source_workspace_from_metadata",
    "workspace_is_used_by_other_window",
    "workspace_matches_spec",
]
