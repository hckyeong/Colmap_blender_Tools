"""사용자 작업공간을 보존하며 전용 Paint 화면 배치를 관리합니다."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import bpy

try:
    from ....shared.blender_runtime.workspace_factory import (
        WorkspaceFactoryError,
        WorkspaceSeedSpec,
        STATE_CREATING,
        STATE_READY,
        WORKSPACE_SOURCE_KEY,
        WORKSPACE_STATE_KEY,
        append_seed_workspace,
        delete_owned_workspace,
        mark_workspace_failed,
        mark_workspace_ready,
        owned_uuid_of as workspace_uuid_of,
        source_workspace_from_metadata,
        workspace_is_used_by_other_window,
        workspace_matches_spec,
    )
except ImportError:
    from shared.blender_runtime.workspace_factory import (
        WorkspaceFactoryError,
        WorkspaceSeedSpec,
        STATE_CREATING,
        STATE_READY,
        WORKSPACE_SOURCE_KEY,
        WORKSPACE_STATE_KEY,
        append_seed_workspace,
        delete_owned_workspace,
        mark_workspace_failed,
        mark_workspace_ready,
        owned_uuid_of as workspace_uuid_of,
        source_workspace_from_metadata,
        workspace_is_used_by_other_window,
        workspace_matches_spec,
    )

from .ownership import is_owned, owned_uuid_of, tag_owned

try:
    from ....shared.blender_runtime.scene_marker_session import (
        acquire_scene_marker_session,
        release_scene_marker_session,
        scene_marker_session_owned_by,
    )
except ImportError:
    from shared.blender_runtime.scene_marker_session import (
        acquire_scene_marker_session,
        release_scene_marker_session,
        scene_marker_session_owned_by,
    )


WORKSPACE_KIND = "PAINT_WORKSPACE"
WORKSPACE_NAME = "Projection Paint"
TIMELINE_RATIO = 0.16
RIGHT_PANEL_RATIO = 0.22
PROPERTIES_RATIO = 0.72
LAYOUT_STAGE_KEY = "pp_paint_layout_stage"
LAYOUT_GEOMETRY_KEY = "pp_paint_layout_geometry"
ORIGIN_WORKSPACE_KEY = "pp_paint_origin_workspace"
PAINT_WORKSPACE_SPEC = WorkspaceSeedSpec(
    tool_id="projection_paint",
    display_name=WORKSPACE_NAME,
    seed_workspace_name="__PROJECTION_PAINT_SEED_5_2__",
    seed_path=(
        Path(__file__).resolve().parents[1]
        / "resources"
        / "workspaces"
        / "projection_paint_seed_5_2.blend"
    ),
)
_MARKER_TOOL_ID = "projection_paint"


_PENDING_WORKSPACE_DELETIONS: set[str] = set()
_DEFERRED_WORKSPACE_DELETIONS: dict[str, "_DeferredWorkspaceDeletion"] = {}


@dataclass(slots=True)
class _DeferredWorkspaceDeletion:
    """비활성 Paint Workspace를 안전한 UI Tick에서 삭제할 상태를 보관합니다."""

    workspace_uuid: str
    window_pointer: int
    restore_workspace_pointer: int
    screen_pointers: tuple[int, ...]
    phase: str = "WAIT_IDLE"
    attempts: int = 0


class PaintWorkspaceError(RuntimeError):
    """전용 Paint 작업공간 생성 또는 화면 배치 오류를 나타냅니다."""


def _pointer(value: Any) -> int:
    """삭제된 Blender 구조체를 안전하게 0으로 변환합니다."""
    try:
        return int(value.as_pointer()) if value is not None else 0
    except (AttributeError, ReferenceError, RuntimeError, TypeError, ValueError):
        return 0


def _window_region(area: Any) -> Any | None:
    """화면 연산에 사용할 Area의 Window Region을 반환합니다."""
    return next(
        (
            region
            for region in getattr(area, "regions", ())
            if region.type == "WINDOW"
        ),
        None,
    )


def _operator_override(context: Any, window: Any, area: Any) -> dict[str, Any]:
    """화면 연산에 필요한 최소 Context Override를 구성합니다."""
    override = {
        "window": window,
        "workspace": window.workspace,
        "screen": window.screen,
        "area": area,
    }
    region = _window_region(area)
    if region is not None:
        override["region"] = region
    return override


def _stored_workspace(settings: Any) -> Any | None:
    """Scene Pointer가 가리키는 유효한 전용 Paint 작업공간을 반환합니다."""
    workspace = getattr(settings, "paint_workspace", None)
    if (
        _pointer(workspace) == 0
        or not is_owned(workspace, kind=WORKSPACE_KIND)
        or not workspace_matches_spec(
            workspace,
            PAINT_WORKSPACE_SPEC,
            require_ready=False,
        )
        or workspace.get(WORKSPACE_STATE_KEY) not in {STATE_CREATING, STATE_READY}
    ):
        return None
    return workspace


def _workspace_restore_source(window: Any, candidate: Any | None) -> Any:
    """교체할 Paint Workspace가 활성 상태여도 외부 Source Workspace를 찾습니다."""

    current = getattr(window, "workspace", None)
    if _pointer(current) == 0:
        raise PaintWorkspaceError("Paint 진입 전 Workspace를 찾을 수 없습니다.")
    if current is not candidate and not is_owned(current, kind=WORKSPACE_KIND):
        return current
    for workspace in (candidate, current):
        if _pointer(workspace) == 0:
            continue
        source = source_workspace_from_metadata(workspace)
        if source is None:
            try:
                origin_name = str(workspace.get(ORIGIN_WORKSPACE_KEY, "") or "")
            except (AttributeError, ReferenceError, RuntimeError, TypeError):
                origin_name = ""
            source = bpy.data.workspaces.get(origin_name)
        if (
            _pointer(source)
            and source is not workspace
            and not is_owned(source, kind=WORKSPACE_KIND)
        ):
            return source
    source = next(
        (
            workspace
            for workspace in bpy.data.workspaces
            if workspace is not candidate
            and not is_owned(workspace, kind=WORKSPACE_KIND)
        ),
        None,
    )
    if source is None:
        raise PaintWorkspaceError("Paint Workspace 교체 뒤 복귀할 외부 Workspace가 없습니다.")
    return source


def _workspace_in_use(workspace: Any) -> bool:
    """지정한 Workspace를 현재 사용 중인 Blender Window가 있는지 반환합니다."""

    return any(
        getattr(window, "workspace", None) is workspace
        for window in bpy.context.window_manager.windows
    )


def _clear_workspace_pointers(workspace: Any) -> None:
    """모든 Scene에서 삭제할 Paint Workspace Pointer만 해제합니다."""

    for scene in bpy.data.scenes:
        settings = getattr(scene, "projection_paint", None)
        if settings is None:
            continue
        try:
            if getattr(settings, "paint_workspace", None) is workspace:
                settings.paint_workspace = None
        except (AttributeError, ReferenceError, RuntimeError, TypeError):
            continue


def _prune_pending_workspace_deletions() -> None:
    """Blender Event Loop에서 이미 삭제된 Workspace의 대기 표식을 제거합니다."""

    existing = {
        owned_uuid_of(workspace)
        for workspace in bpy.data.workspaces
        if is_owned(workspace, kind=WORKSPACE_KIND)
    }
    _PENDING_WORKSPACE_DELETIONS.intersection_update(existing)


def _workspace_by_owned_uuid(workspace_uuid: str) -> Any | None:
    """소유 UUID와 일치하는 Paint Workspace를 현재 Main에서 찾습니다."""

    return next(
        (
            workspace
            for workspace in bpy.data.workspaces
            if is_owned(workspace, kind=WORKSPACE_KIND)
            and owned_uuid_of(workspace) == str(workspace_uuid)
        ),
        None,
    )


def _workspace_by_pointer(pointer: int) -> Any | None:
    """저장한 Pointer와 일치하는 현재 Workspace를 반환합니다."""

    return next(
        (
            workspace
            for workspace in bpy.data.workspaces
            if _pointer(workspace) == int(pointer)
        ),
        None,
    )


def _window_by_pointer(pointer: int) -> Any | None:
    """저장한 Pointer와 일치하는 현재 Window를 반환합니다."""

    return next(
        (
            window
            for window in bpy.context.window_manager.windows
            if _pointer(window) == int(pointer)
        ),
        None,
    )


def _screen_by_pointer(pointer: int) -> Any | None:
    """저장한 Pointer와 일치하는 현재 Screen을 반환합니다."""

    return next(
        (
            screen
            for screen in bpy.data.screens
            if _pointer(screen) == int(pointer)
        ),
        None,
    )


def _remove_unreferenced_workspace_screens(screen_pointers: tuple[int, ...]) -> bool:
    """공식 Workspace 삭제가 연결 Screen ID까지 제거했는지 검증합니다."""

    return all(_screen_by_pointer(pointer) is None for pointer in screen_pointers)


def _paint_workspace_cleanup_busy() -> bool:
    """Paint 진입·활성·종료 중에는 지연 작업공간 정리를 멈춥니다."""

    for scene in bpy.data.scenes:
        settings = getattr(scene, "projection_paint", None)
        if settings is None:
            continue
        if bool(getattr(settings, "paint_enabled", False)):
            return True
        if str(getattr(settings, "paint_state", "IDLE")) in {
            "ENTERING",
            "ACTIVE",
            "EXITING",
        }:
            return True
        workspace = getattr(settings, "paint_workspace", None)
        if _pointer(workspace) and is_owned(workspace, kind=WORKSPACE_KIND):
            return True
    return False


def _workspace_screen(workspace: Any, window: Any) -> Any | None:
    """활성 여부와 무관하게 대상 Workspace가 소유한 Screen을 반환합니다."""

    if getattr(window, "workspace", None) is workspace:
        return getattr(window, "screen", None)
    return next(iter(getattr(workspace, "screens", ())), None)


def _invoke_workspace_deletion(workspace: Any, window: Any) -> bool:
    """대상 Workspace의 비-탭 Region 문맥으로 Blender 삭제 Event를 등록합니다."""

    workspace_uuid = owned_uuid_of(workspace)
    screen = _workspace_screen(workspace, window)
    if not workspace_uuid or screen is None:
        return False
    area = next(
        (
            candidate
            for candidate in screen.areas
            if _window_region(candidate) is not None
        ),
        None,
    )
    if area is None:
        return False
    override = {
        "window": window,
        "workspace": workspace,
        "screen": screen,
        "area": area,
        "region": _window_region(area),
    }
    try:
        with bpy.context.temp_override(**override):
            result = bpy.ops.workspace.delete()
    except (AttributeError, ReferenceError, RuntimeError):
        return False
    if result != {"FINISHED"}:
        return False
    _PENDING_WORKSPACE_DELETIONS.add(workspace_uuid)
    return True


def _deferred_workspace_deletion_tick() -> float | None:
    """비활성 잔여 Workspace를 활성화·삭제·원위치 복구 순서로 정리합니다."""

    _prune_pending_workspace_deletions()
    if not _DEFERRED_WORKSPACE_DELETIONS:
        return None
    if _paint_workspace_cleanup_busy():
        return 0.05
    workspace_uuid = next(iter(_DEFERRED_WORKSPACE_DELETIONS))
    record = _DEFERRED_WORKSPACE_DELETIONS[workspace_uuid]
    workspace = _workspace_by_owned_uuid(workspace_uuid)
    window = _window_by_pointer(record.window_pointer)
    if window is None:
        window = getattr(bpy.context, "window", None)
    if workspace is None:
        if not _remove_unreferenced_workspace_screens(record.screen_pointers):
            record.attempts += 1
            if record.attempts < 200:
                return 0.05
            _DEFERRED_WORKSPACE_DELETIONS.pop(workspace_uuid, None)
            return None
        restore_workspace = _workspace_by_pointer(record.restore_workspace_pointer)
        if (
            _pointer(window)
            and restore_workspace is not None
            and getattr(window, "workspace", None) is not restore_workspace
        ):
            window.workspace = restore_workspace
            record.phase = "WAIT_RESTORE"
            return 0.05
        _DEFERRED_WORKSPACE_DELETIONS.pop(workspace_uuid, None)
        return 0.0 if _DEFERRED_WORKSPACE_DELETIONS else None
    if not _pointer(window):
        record.attempts += 1
        if record.attempts < 200:
            return 0.05
        _DEFERRED_WORKSPACE_DELETIONS.pop(workspace_uuid, None)
        return None
    if record.phase == "WAIT_IDLE":
        if _workspace_in_use(workspace):
            return 0.05
        window.workspace = workspace
        record.phase = "WAIT_ACTIVATE"
        return 0.05
    if record.phase == "WAIT_ACTIVATE":
        if getattr(window, "workspace", None) is not workspace:
            record.attempts += 1
            if record.attempts >= 200:
                _DEFERRED_WORKSPACE_DELETIONS.pop(workspace_uuid, None)
                return None
            return 0.05
        if not _invoke_workspace_deletion(workspace, window):
            _DEFERRED_WORKSPACE_DELETIONS.pop(workspace_uuid, None)
            return None
        record.phase = "WAIT_DELETE"
        record.attempts = 0
        return 0.05
    if record.phase == "WAIT_DELETE":
        record.attempts += 1
        if record.attempts >= 200:
            _DEFERRED_WORKSPACE_DELETIONS.pop(workspace_uuid, None)
            return None
        return 0.05
    if record.phase == "WAIT_RESTORE":
        restore_workspace = _workspace_by_pointer(record.restore_workspace_pointer)
        if restore_workspace is None or getattr(window, "workspace", None) is restore_workspace:
            _DEFERRED_WORKSPACE_DELETIONS.pop(workspace_uuid, None)
            return 0.0 if _DEFERRED_WORKSPACE_DELETIONS else None
        return 0.05
    _DEFERRED_WORKSPACE_DELETIONS.pop(workspace_uuid, None)
    return None


def _ensure_deferred_workspace_cleanup_timer() -> bool:
    """Lifecycle Registry가 소유하는 단일 지연 Workspace 정리 Timer를 등록합니다."""

    try:
        from .lifecycle import lifecycle_registry

        registry = lifecycle_registry()
        if not registry.active or registry.suspended:
            return False
        registry.register_timer(
            _deferred_workspace_deletion_tick,
            component="paint_workspace_cleanup",
            first_interval=0.05,
        )
    except (ImportError, AttributeError, RuntimeError, ValueError):
        return False
    return True


def _request_workspace_deletion(workspace: Any, window: Any | None = None) -> bool:
    """소유 Paint Workspace의 Screen 문맥으로 Blender 삭제 연산을 요청합니다."""

    workspace_uuid = owned_uuid_of(workspace)
    if not workspace_uuid:
        return False
    _prune_pending_workspace_deletions()
    if (
        workspace_uuid in _PENDING_WORKSPACE_DELETIONS
        or workspace_uuid in _DEFERRED_WORKSPACE_DELETIONS
    ):
        return True
    delete_window = window if _pointer(window) else getattr(bpy.context, "window", None)
    if (
        not _pointer(delete_window)
        or getattr(delete_window, "workspace", None) is not workspace
    ):
        delete_window = next(
            (
                candidate
                for candidate in bpy.context.window_manager.windows
                if getattr(candidate, "workspace", None) is workspace
            ),
            None,
        )
    if delete_window is not None:
        return _invoke_workspace_deletion(workspace, delete_window)
    delete_window = window if _pointer(window) else getattr(bpy.context, "window", None)
    if not _pointer(delete_window):
        return False
    restore_workspace = getattr(delete_window, "workspace", None)
    _DEFERRED_WORKSPACE_DELETIONS[workspace_uuid] = _DeferredWorkspaceDeletion(
        workspace_uuid=workspace_uuid,
        window_pointer=_pointer(delete_window),
        restore_workspace_pointer=_pointer(restore_workspace),
        screen_pointers=tuple(
            _pointer(screen) for screen in getattr(workspace, "screens", ())
        ),
    )
    if _ensure_deferred_workspace_cleanup_timer():
        return True
    _DEFERRED_WORKSPACE_DELETIONS.pop(workspace_uuid, None)
    return False


def remove_paint_workspace(
    scene: Any,
    *,
    window: Any | None = None,
    workspace: Any | None = None,
) -> bool:
    """원래 Workspace 복귀 뒤 애드온 소유 Paint Workspace를 안전하게 삭제합니다."""

    settings = getattr(scene, "projection_paint", None)
    candidate = workspace
    if candidate is None and settings is not None:
        candidate = getattr(settings, "paint_workspace", None)
    if _pointer(candidate) == 0:
        if settings is not None:
            try:
                settings.paint_workspace = None
            except (AttributeError, ReferenceError, RuntimeError, TypeError):
                pass
        return True
    if not is_owned(candidate, kind=WORKSPACE_KIND):
        if settings is not None and getattr(settings, "paint_workspace", None) is candidate:
            settings.paint_workspace = None
        return False

    if settings is not None and getattr(settings, "paint_workspace", None) is candidate:
        settings.paint_workspace = None
    _clear_workspace_pointers(candidate)
    active_window = window if _pointer(window) else getattr(bpy.context, "window", None)
    return _request_workspace_deletion(candidate, active_window)


def cleanup_unused_paint_workspaces() -> int:
    """Pointer와 Window가 사용하지 않는 애드온 Paint Workspace를 모두 정리합니다."""

    referenced = {
        _pointer(getattr(settings, "paint_workspace", None))
        for scene in bpy.data.scenes
        if (settings := getattr(scene, "projection_paint", None)) is not None
    }
    _prune_pending_workspace_deletions()
    removed = 0
    for workspace in tuple(bpy.data.workspaces):
        if not is_owned(workspace, kind=WORKSPACE_KIND):
            continue
        if _pointer(workspace) in referenced or _workspace_in_use(workspace):
            continue
        if (
            owned_uuid_of(workspace) in _PENDING_WORKSPACE_DELETIONS
            or owned_uuid_of(workspace) in _DEFERRED_WORKSPACE_DELETIONS
        ):
            continue
        if _request_workspace_deletion(workspace):
            removed += 1
    return removed


def pause_paint_workspace_cleanup(_reason: str, _epoch: int) -> None:
    """Load·Undo·Redo·해제 장벽에서 이전 Epoch의 지연 삭제 상태를 폐기합니다."""

    _DEFERRED_WORKSPACE_DELETIONS.clear()
    _PENDING_WORKSPACE_DELETIONS.clear()


def rebuild_paint_workspace_cleanup(
    _scene: Any,
    _reason: str,
    _epoch: int,
) -> None:
    """Rebuild 뒤 READY Workspace는 보존하고 손상된 소유 Workspace만 정리합니다."""

    for scene in bpy.data.scenes:
        settings = getattr(scene, "projection_paint", None)
        if settings is None or bool(getattr(settings, "paint_enabled", False)):
            continue
        workspace = getattr(settings, "paint_workspace", None)
        if (
            _pointer(workspace)
            and is_owned(workspace, kind=WORKSPACE_KIND)
            and not workspace_matches_spec(workspace, PAINT_WORKSPACE_SPEC)
        ):
            remove_paint_workspace(
                scene,
                window=getattr(bpy.context, "window", None),
                workspace=workspace,
            )
    cleanup_unused_paint_workspaces()


def ensure_paint_workspace(context: Any, *, reuse_existing: bool = False) -> Any:
    """도구 전용 Seed에서 Paint Workspace를 Append하거나 현행 구성을 재사용합니다."""
    window = getattr(context, "window", None)
    if window is None or _pointer(window) == 0:
        raise PaintWorkspaceError("Paint 작업공간을 표시할 Blender Window가 없습니다.")
    settings = context.scene.projection_paint
    workspace = _stored_workspace(settings)
    if (
        bool(reuse_existing)
        and workspace is not None
        and getattr(window, "workspace", None) is workspace
    ):
        return workspace
    previous_workspace = getattr(settings, "paint_workspace", None)
    if (
        _pointer(previous_workspace)
        and is_owned(previous_workspace, kind=WORKSPACE_KIND)
        and workspace_is_used_by_other_window(previous_workspace, window)
    ):
        raise PaintWorkspaceError(
            "기존 Projection Paint Workspace를 다른 Window가 사용 중이어서 교체할 수 없습니다."
        )
    origin_workspace = _workspace_restore_source(window, previous_workspace)
    cleanup_unused_paint_workspaces()

    created_workspace = None
    try:
        created_workspace = append_seed_workspace(
            context,
            window,
            PAINT_WORKSPACE_SPEC,
            origin_workspace,
        )
        tag_owned(
            created_workspace,
            WORKSPACE_KIND,
            owned_uuid=workspace_uuid_of(created_workspace),
        )
        created_workspace.object_mode = "TEXTURE_PAINT"
        created_workspace[LAYOUT_STAGE_KEY] = "RESET"
        created_workspace[LAYOUT_GEOMETRY_KEY] = ""
        created_workspace[ORIGIN_WORKSPACE_KEY] = str(
            getattr(origin_workspace, "name", "") or ""
        )
        settings.paint_workspace = created_workspace
        if window.workspace is not created_workspace:
            window.workspace = created_workspace
    except (AttributeError, ReferenceError, RuntimeError, TypeError, WorkspaceFactoryError) as exc:
        if created_workspace is not None:
            mark_workspace_failed(created_workspace)
            if getattr(window, "workspace", None) is created_workspace:
                window.workspace = origin_workspace
            delete_owned_workspace(
                context,
                window,
                created_workspace,
                PAINT_WORKSPACE_SPEC,
                restore_workspace=origin_workspace,
            )
        settings.paint_workspace = (
            previous_workspace if _pointer(previous_workspace) else None
        )
        raise PaintWorkspaceError(f"전용 Paint Workspace 생성 실패: {exc}") from exc

    if (
        _pointer(previous_workspace)
        and previous_workspace is not created_workspace
        and is_owned(previous_workspace, kind=WORKSPACE_KIND)
    ):
        remove_paint_workspace(
            context.scene,
            window=window,
            workspace=previous_workspace,
        )
    return created_workspace


def _show_projection_paint_sidebar(window: Any) -> bool:
    """전용 3D Viewport의 N패널과 Projection Paint 탭을 활성화합니다."""

    viewport = next(
        (area for area in window.screen.areas if area.type == "VIEW_3D"),
        None,
    )
    if viewport is None:
        return False
    space = getattr(getattr(viewport, "spaces", None), "active", None)
    if space is None or getattr(space, "type", "") != "VIEW_3D":
        return False
    try:
        space.show_region_ui = True
    except (AttributeError, ReferenceError, RuntimeError, TypeError, ValueError):
        return False
    ui_region = next(
        (region for region in viewport.regions if region.type == "UI"),
        None,
    )
    if ui_region is None:
        return False
    try:
        ui_region.active_panel_category = "Projection Paint"
    except (AttributeError, ReferenceError, RuntimeError, TypeError, ValueError):
        return False
    return bool(space.show_region_ui)


def _restore_workspace_after_open_failure(
    scene: Any,
    window: Any,
    workspace: Any,
) -> None:
    """작업공간 구성 실패 시 원래 Workspace로 복귀하고 소유본을 제거합니다."""

    try:
        mark_workspace_failed(workspace)
    except WorkspaceFactoryError:
        pass
    origin = bpy.data.workspaces.get(
        str(workspace.get(ORIGIN_WORKSPACE_KEY, "") or "")
    )
    if origin is None:
        origin = source_workspace_from_metadata(workspace)
    if origin is not None and getattr(window, "workspace", None) is workspace:
        window.workspace = origin
    remove_paint_workspace(scene, window=window, workspace=workspace)


def _schedule_open_workspace_configuration(
    scene: Any,
    window: Any,
    workspace: Any,
) -> None:
    """Event Tick마다 화면 배치를 구성하고 N패널을 활성화합니다."""

    scene_pointer = _pointer(scene)
    window_pointer = _pointer(window)
    workspace_pointer = _pointer(workspace)
    attempts = {"count": 0}

    def configure_on_next_tick() -> float | None:
        """현재 Pointer가 유지되는 동안 작업공간 구성 단계를 하나씩 진행합니다."""

        current_scene = next(
            (item for item in bpy.data.scenes if _pointer(item) == scene_pointer),
            None,
        )
        current_window = _window_by_pointer(window_pointer)
        current_workspace = _workspace_by_pointer(workspace_pointer)
        if current_scene is None or current_window is None or current_workspace is None:
            return None
        settings = getattr(current_scene, "projection_paint", None)
        if settings is None or getattr(settings, "paint_workspace", None) is not current_workspace:
            return None
        attempts["count"] += 1
        try:
            if getattr(current_window, "workspace", None) is not current_workspace:
                settings.status = "Workspace 전환으로 Projection Paint 준비를 종료합니다."
                settings.status_kind = "READY"
                return None
            if not configure_paint_workspace(bpy.context):
                return 0.025
            if not _show_projection_paint_sidebar(current_window):
                if attempts["count"] < 200:
                    return 0.025
                raise PaintWorkspaceError("Projection Paint N패널을 활성화하지 못했습니다.")
        except (PaintWorkspaceError, RuntimeError, ValueError) as exc:
            _restore_workspace_after_open_failure(
                current_scene,
                current_window,
                current_workspace,
            )
            settings.status = f"Projection Paint 작업공간 준비 실패: {exc}"
            settings.status_kind = "ERROR"
            settings.paint_state = "ERROR"
            return None
        settings.status = "Projection Paint 작업공간 준비 완료"
        settings.status_kind = "SUCCESS"
        if not bool(settings.paint_enabled):
            settings.paint_state = "IDLE"
        return None

    from .lifecycle import lifecycle_registry

    lifecycle_registry().register_timer(
        configure_on_next_tick,
        component="paint_workspace_open",
        first_interval=0.0,
    )


def open_paint_workspace(context: Any) -> Any:
    """Paint 연산 없이 전용 작업공간을 열고 설정용 N패널을 준비합니다."""

    window = getattr(context, "window", None)
    if window is None or _pointer(window) == 0:
        raise PaintWorkspaceError("Projection Paint 작업공간을 표시할 Window가 없습니다.")
    settings = context.scene.projection_paint
    if str(settings.paint_state) in {"ENTERING", "EXITING"}:
        raise PaintWorkspaceError("Paint 진입 또는 종료가 끝난 뒤 작업공간을 여십시오.")
    if not acquire_scene_marker_session(
        context.scene,
        _MARKER_TOOL_ID,
        window=window,
    ):
        raise PaintWorkspaceError(
            "현재 Scene의 Camera Marker를 다른 COLMAP 도구가 사용 중입니다."
        )
    try:
        from .camera_scene import attach_projection_markers, detach_projection_markers

        attach_projection_markers(context.scene)
        workspace = _stored_workspace(settings)
        if workspace is None:
            workspace = ensure_paint_workspace(context)
        elif getattr(window, "workspace", None) is not workspace:
            origin_workspace = _workspace_restore_source(window, workspace)
            workspace[WORKSPACE_SOURCE_KEY] = str(
                getattr(origin_workspace, "name", "") or ""
            )
            workspace[ORIGIN_WORKSPACE_KEY] = str(
                getattr(origin_workspace, "name", "") or ""
            )
            window.workspace = workspace
    except Exception:
        try:
            detach_projection_markers(context.scene)
        finally:
            release_scene_marker_session(context.scene, _MARKER_TOOL_ID)
        raise
    if not bool(settings.paint_enabled):
        workspace.object_mode = "OBJECT"
    settings.status = "Projection Paint 작업공간 준비 중"
    settings.status_kind = "RUNNING"
    _schedule_open_workspace_configuration(
        context.scene,
        window,
        workspace,
    )
    return workspace


def request_paint_workspace_exit(context: Any) -> None:
    """Paint 준비·세션을 종료하고 전용 Workspace를 보존한 채 Source로 복귀합니다."""

    scene = context.scene
    settings = scene.projection_paint
    window = getattr(context, "window", None)
    if window is None or _pointer(window) == 0:
        raise PaintWorkspaceError("Projection Paint를 종료할 Window가 없습니다.")
    from .paint_progress_session import (
        active_paint_progress_session,
        release_paint_progress_session,
    )

    progress_session = active_paint_progress_session()
    progress_aborted = False
    if progress_session is not None and progress_session.scene is scene:
        release_paint_progress_session(progress_session.job_id, reset_status=False)
        progress_aborted = True
    if bool(settings.paint_enabled) or str(settings.paint_state) in {
        "ENTERING",
        "ACTIVE",
        "EXITING",
    }:
        from .paint_session import finish_paint_session

        finish_paint_session(context)
    from .camera_scene import detach_projection_markers

    detach_projection_markers(scene)
    release_scene_marker_session(scene, _MARKER_TOOL_ID)

    workspace = _stored_workspace(settings)
    if workspace is None:
        current = getattr(window, "workspace", None)
        workspace = (
            current
            if workspace_matches_spec(current, PAINT_WORKSPACE_SPEC, require_ready=False)
            else None
        )
    if workspace is None:
        if progress_aborted:
            settings.paint_enabled = False
            settings.paint_state = "IDLE"
            settings.status = "COLMAP Workspace 전환으로 Paint 준비 종료"
            settings.status_kind = "READY"
        return
    source = source_workspace_from_metadata(workspace)
    if source is None:
        origin_name = str(workspace.get(ORIGIN_WORKSPACE_KEY, "") or "")
        source = bpy.data.workspaces.get(origin_name)
    if source is None or source is workspace:
        raise PaintWorkspaceError(
            "Projection Paint Workspace의 원래 Source Workspace를 찾지 못했습니다."
        )
    settings.paint_state = "EXITING"
    if getattr(window, "workspace", None) is workspace:
        window.workspace = source

    workspace_state = str(workspace.get(WORKSPACE_STATE_KEY, "") or "")
    if workspace_state != STATE_READY:
        if not remove_paint_workspace(scene, window=window, workspace=workspace):
            settings.paint_state = "ERROR"
            raise PaintWorkspaceError(
                "준비가 끝나지 않은 Projection Paint Workspace 삭제를 요청하지 못했습니다."
            )
    else:
        # Align·Modifier와 동일하게 정상 종료한 READY Workspace는 삭제하지 않습니다.
        # if not remove_paint_workspace(scene, window=window, workspace=workspace):
        #     settings.paint_state = "ERROR"
        #     raise PaintWorkspaceError("Projection Paint Workspace 삭제를 요청하지 못했습니다.")
        pass
    settings.paint_enabled = False
    settings.paint_state = "IDLE"
    settings.status = (
        "Projection Paint Workspace 종료"
        if workspace_state == STATE_READY
        else "준비 중이던 Projection Paint Workspace 종료"
    )
    settings.status_kind = "READY"


def paint_workspace_exit_settled(scene: Any, window: Any) -> bool:
    """Paint 상태 복구와 Source 복귀가 끝나 Workspace를 재사용할 수 있는지 반환합니다."""

    _prune_pending_workspace_deletions()
    from .camera_scene import projection_markers_detached

    settings = getattr(scene, "projection_paint", None)
    if settings is None:
        return bool(
            not _PENDING_WORKSPACE_DELETIONS
            and not _DEFERRED_WORKSPACE_DELETIONS
            and not scene_marker_session_owned_by(scene, _MARKER_TOOL_ID)
            and projection_markers_detached(scene)
        )
    return bool(
        not bool(getattr(settings, "paint_enabled", False))
        and str(getattr(settings, "paint_state", "IDLE"))
        not in {"ENTERING", "ACTIVE", "EXITING"}
        and not _PENDING_WORKSPACE_DELETIONS
        and not _DEFERRED_WORKSPACE_DELETIONS
        and not scene_marker_session_owned_by(scene, _MARKER_TOOL_ID)
        and projection_markers_detached(scene)
        and not workspace_matches_spec(
            getattr(window, "workspace", None),
            PAINT_WORKSPACE_SPEC,
            require_ready=False,
        )
    )


def _largest_area(screen: Any) -> Any:
    """Screen에서 면적이 가장 큰 Area를 반환합니다."""
    area = max(
        tuple(
            candidate
            for candidate in screen.areas
            if int(candidate.width) > 0 and int(candidate.height) > 0
        ),
        key=lambda candidate: int(candidate.width) * int(candidate.height),
        default=None,
    )
    if area is None:
        raise PaintWorkspaceError("Paint 화면에 사용할 Area가 없습니다.")
    return area


def _ready_areas(screen: Any, expected_count: int) -> tuple[Any, ...]:
    """기대 개수와 양수 크기를 모두 만족하는 안정화 후보 Area를 반환합니다."""
    areas = tuple(screen.areas)
    if len(areas) != int(expected_count):
        return ()
    if any(int(area.width) <= 0 or int(area.height) <= 0 for area in areas):
        return ()
    return areas


def _geometry_signature(areas: tuple[Any, ...]) -> str:
    """Area 위치와 크기를 Event Loop 간 비교 가능한 문자열로 변환합니다."""
    values = sorted(
        (
            _pointer(area),
            int(area.x),
            int(area.y),
            int(area.width),
            int(area.height),
        )
        for area in areas
    )
    return ";".join(",".join(str(value) for value in item) for item in values)


def _area_geometry_is_stable(workspace: Any, screen: Any, expected_count: int) -> bool:
    """모든 Area가 두 Event Loop 연속 같은 양수 기하인지 확인합니다."""
    areas = _ready_areas(screen, expected_count)
    if not areas:
        workspace[LAYOUT_GEOMETRY_KEY] = ""
        return False
    signature = _geometry_signature(areas)
    previous = str(workspace.get(LAYOUT_GEOMETRY_KEY, "") or "")
    workspace[LAYOUT_GEOMETRY_KEY] = signature
    return bool(previous and previous == signature)


def _advance_layout_stage(workspace: Any, stage: str) -> None:
    """다음 화면 구성 단계를 기록하고 기하 안정화 기록을 초기화합니다."""
    workspace[LAYOUT_STAGE_KEY] = str(stage)
    workspace[LAYOUT_GEOMETRY_KEY] = ""


def _close_other_areas(context: Any, window: Any) -> Any:
    """가장 큰 Area 하나만 남도록 전용 Screen의 나머지 Area를 닫습니다."""
    screen = window.screen
    keeper = _largest_area(screen)
    while len(screen.areas) > 1:
        candidate = min(
            (
                area
                for area in tuple(screen.areas)
                if _pointer(area) != _pointer(keeper)
            ),
            key=lambda area: int(area.width) * int(area.height),
            default=None,
        )
        if candidate is None:
            raise PaintWorkspaceError("Paint 화면 Area 정리 대상을 찾을 수 없습니다.")
        with context.temp_override(**_operator_override(context, window, candidate)):
            result = bpy.ops.screen.area_close()
        if result != {"FINISHED"}:
            raise PaintWorkspaceError("Paint 화면의 불필요한 Area를 닫지 못했습니다.")
    return next(iter(screen.areas))


def _split_right_panel(context: Any, window: Any, view_area: Any) -> None:
    """단일 Area에서 오른쪽 패널 열을 분할합니다."""
    screen = window.screen
    view_area.type = "VIEW_3D"
    with context.temp_override(**_operator_override(context, window, view_area)):
        result = bpy.ops.screen.area_split(
            direction="VERTICAL",
            factor=1.0 - RIGHT_PANEL_RATIO,
        )
    if result != {"FINISHED"} or len(screen.areas) != 2:
        raise PaintWorkspaceError("Paint 화면에 오른쪽 패널 영역을 만들지 못했습니다.")


def _split_timeline(context: Any, window: Any) -> None:
    """왼쪽 3D Viewport 아래에 Timeline 영역을 분할합니다."""
    screen = window.screen
    areas = _ready_areas(screen, 2)
    if not areas:
        raise PaintWorkspaceError("Timeline 분할 전 Area 기하가 준비되지 않았습니다.")
    left_area = min(areas, key=lambda area: (int(area.x), int(area.y)))
    with context.temp_override(**_operator_override(context, window, left_area)):
        result = bpy.ops.screen.area_split(
            direction="HORIZONTAL",
            factor=TIMELINE_RATIO,
        )
    if result != {"FINISHED"} or len(screen.areas) != 3:
        raise PaintWorkspaceError("Paint 화면에 Timeline Area를 만들지 못했습니다.")


def _split_properties(context: Any, window: Any) -> None:
    """오른쪽 패널 열을 Outliner와 Properties 영역으로 분할합니다."""
    screen = window.screen
    areas = _ready_areas(screen, 3)
    if not areas:
        raise PaintWorkspaceError("Properties 분할 전 Area 기하가 준비되지 않았습니다.")
    right_area = max(areas, key=lambda area: (int(area.x), int(area.y)))
    with context.temp_override(**_operator_override(context, window, right_area)):
        result = bpy.ops.screen.area_split(
            direction="HORIZONTAL",
            factor=PROPERTIES_RATIO,
        )
    if result != {"FINISHED"} or len(screen.areas) != 4:
        raise PaintWorkspaceError("Paint 화면에 Outliner와 Properties 영역을 만들지 못했습니다.")


def _paint_areas(window: Any) -> tuple[Any, Any, Any, Any]:
    """현재 네 Area를 위치 기준으로 Paint 편집기 역할에 대응시킵니다."""
    screen = window.screen
    areas = _ready_areas(screen, 4)
    if not areas:
        raise PaintWorkspaceError("Paint 편집기 Area 기하가 준비되지 않았습니다.")
    split_x = max(int(area.x) for area in areas)
    left_areas = sorted(
        (area for area in areas if int(area.x) < split_x),
        key=lambda area: int(area.y),
    )
    if len(left_areas) != 2:
        raise PaintWorkspaceError("3D Viewport와 Timeline 영역을 식별하지 못했습니다.")
    timeline_area, viewport_area = left_areas
    right_areas = sorted(
        (area for area in areas if int(area.x) >= split_x),
        key=lambda area: int(area.y),
    )
    if len(right_areas) != 2:
        raise PaintWorkspaceError("Outliner와 Properties 영역을 식별하지 못했습니다.")
    properties_area, outliner_area = right_areas

    timeline_area.type = "DOPESHEET_EDITOR"
    timeline_area.ui_type = "TIMELINE"
    viewport_area.type = "VIEW_3D"
    properties_area.type = "PROPERTIES"
    outliner_area.type = "OUTLINER"
    properties_area.spaces.active.context = "OBJECT"
    outliner_area.spaces.active.display_mode = "VIEW_LAYER"
    if int(timeline_area.height) >= int(viewport_area.height):
        raise PaintWorkspaceError("Timeline Area가 화면 하단의 작은 영역으로 생성되지 않았습니다.")
    if int(outliner_area.height) >= int(properties_area.height):
        raise PaintWorkspaceError("Outliner가 Properties 위의 작은 영역으로 생성되지 않았습니다.")
    return viewport_area, timeline_area, outliner_area, properties_area


def _disable_local_view(context: Any, window: Any, viewport_area: Any) -> None:
    """Paint Viewport가 Local View라면 소유 Screen 문맥에서 안전하게 해제합니다."""

    space = viewport_area.spaces.active
    if getattr(space, "local_view", None) is None:
        return
    with context.temp_override(**_operator_override(context, window, viewport_area)):
        result = bpy.ops.view3d.localview(frame_selected=False)
    if result != {"FINISHED"} or getattr(space, "local_view", None) is not None:
        raise PaintWorkspaceError("Projection Paint Viewport의 Local View를 해제하지 못했습니다.")


def _configure_viewport_visibility_contract(
    context: Any,
    window: Any,
    viewport_area: Any,
) -> None:
    """Paint 데이터가 필터로 차단되지 않도록 모든 Object 타입 표시를 활성화합니다."""

    space = viewport_area.spaces.active
    filter_names = tuple(
        prop.identifier
        for prop in space.bl_rna.properties
        if prop.identifier.startswith("show_object_viewport_")
    )
    if not filter_names:
        raise PaintWorkspaceError("현재 Blender에서 Viewport Object 표시 필터를 찾지 못했습니다.")
    for filter_name in filter_names:
        setattr(space, filter_name, True)
    if not all(bool(getattr(space, filter_name)) for filter_name in filter_names):
        raise PaintWorkspaceError("Projection Paint Viewport Object 필터를 활성화하지 못했습니다.")
    _disable_local_view(context, window, viewport_area)


def _finalize_workspace_contract(
    context: Any,
    window: Any,
    workspace: Any,
    viewport_area: Any,
) -> None:
    """Paint Layout과 Viewport 표시 계약을 검증한 뒤 Workspace를 READY로 표시합니다."""

    _configure_viewport_visibility_contract(context, window, viewport_area)
    try:
        mark_workspace_ready(
            workspace,
            window.screen,
            PAINT_WORKSPACE_SPEC,
        )
    except WorkspaceFactoryError as exc:
        raise PaintWorkspaceError(f"Projection Paint Workspace 검증 실패: {exc}") from exc


def configure_paint_workspace(context: Any) -> bool:
    """한 Event Loop에 한 번씩 Paint Screen 구성을 진행하고 완료 여부를 반환합니다."""
    window = getattr(context, "window", None)
    settings = context.scene.projection_paint
    workspace = _stored_workspace(settings)
    if (
        window is None
        or _pointer(window) == 0
        or workspace is None
        or window.workspace is not workspace
    ):
        raise PaintWorkspaceError("전용 Paint 작업공간 전환이 완료되지 않았습니다.")
    stage = str(workspace.get(LAYOUT_STAGE_KEY, "RESET") or "RESET")
    if stage == "RESET":
        view_area = _close_other_areas(context, window)
        _split_right_panel(context, window, view_area)
        _advance_layout_stage(workspace, "RIGHT_SPLIT")
        return False
    if stage == "RIGHT_SPLIT":
        if not _area_geometry_is_stable(workspace, window.screen, 2):
            return False
        _split_timeline(context, window)
        _advance_layout_stage(workspace, "TIMELINE_SPLIT")
        return False
    if stage == "TIMELINE_SPLIT":
        if not _area_geometry_is_stable(workspace, window.screen, 3):
            return False
        _split_properties(context, window)
        _advance_layout_stage(workspace, "EDITORS_SPLIT")
        return False
    if stage == "EDITORS_SPLIT":
        if not _area_geometry_is_stable(workspace, window.screen, 4):
            return False
        viewport_area, _timeline, _outliner, _properties = _paint_areas(window)
        _finalize_workspace_contract(
            context,
            window,
            workspace,
            viewport_area,
        )
        _advance_layout_stage(workspace, "COMPLETE")
        return True
    if stage == "COMPLETE":
        viewport_area, _timeline, _outliner, _properties = _paint_areas(window)
        _finalize_workspace_contract(
            context,
            window,
            workspace,
            viewport_area,
        )
        return True
    raise PaintWorkspaceError(f"지원하지 않는 Paint 화면 구성 단계입니다: {stage}")


__all__ = [
    "PaintWorkspaceError",
    "cleanup_unused_paint_workspaces",
    "configure_paint_workspace",
    "ensure_paint_workspace",
    "open_paint_workspace",
    "paint_workspace_exit_settled",
    "pause_paint_workspace_cleanup",
    "rebuild_paint_workspace_cleanup",
    "remove_paint_workspace",
    "request_paint_workspace_exit",
]
