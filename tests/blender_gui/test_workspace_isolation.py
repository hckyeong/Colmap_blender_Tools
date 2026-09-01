"""Blender GUI에서 COLMAP 도구 간 자동 Workspace 종료와 격리를 검증합니다."""

from __future__ import annotations

import importlib.util
from importlib import import_module
import os
from pathlib import Path
import sys
import time
import traceback

import bpy


ROOT = Path(__file__).resolve().parents[2]
USE_INSTALLED = os.environ.get("COLMAP_TOOLS_USE_INSTALLED") == "1"
PACKAGE_NAME = os.environ.get(
    "COLMAP_TOOLS_INSTALLED_MODULE",
    "colmap_tools_workspace_isolation_test",
)
TIMEOUT_SECONDS = 90.0


def _load_extension_package():
    """Source Tree 또는 격리 설치본의 통합 Extension Package를 불러옵니다."""

    if USE_INSTALLED:
        return import_module(PACKAGE_NAME)
    spec = importlib.util.spec_from_file_location(
        PACKAGE_NAME,
        ROOT / "__init__.py",
        submodule_search_locations=[str(ROOT)],
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("통합 Extension Root를 불러올 수 없습니다.")
    module = importlib.util.module_from_spec(spec)
    sys.modules[PACKAGE_NAME] = module
    spec.loader.exec_module(module)
    return module


def _activate_extension():
    """Source Package를 등록하거나 격리 설치 Extension을 활성화합니다."""

    if not USE_INSTALLED:
        module = _load_extension_package()
        module.register()
        return module
    import addon_utils

    _loaded, enabled = addon_utils.check(PACKAGE_NAME)
    module = import_module(PACKAGE_NAME) if enabled else addon_utils.enable(
        PACKAGE_NAME,
        default_set=False,
        persistent=False,
    )
    if module is None:
        raise RuntimeError("격리 설치 COLMAP Tools Extension을 활성화하지 못했습니다.")
    return module


def _deactivate_extension(extension) -> None:
    """검증이 활성화한 Source 또는 설치 Extension을 안전하게 해제합니다."""

    if USE_INSTALLED:
        import addon_utils

        _loaded, enabled = addon_utils.check(PACKAGE_NAME)
        if enabled:
            addon_utils.disable(PACKAGE_NAME, default_set=False)
        return
    extension.unregister()


def _view_spaces(screen) -> tuple[object, ...]:
    """Screen의 모든 활성 3D View Space를 반환합니다."""

    return tuple(area.spaces.active for area in screen.areas if area.type == "VIEW_3D")


def _filter_names(space) -> tuple[str, ...]:
    """현재 Blender가 제공하는 Viewport Object 타입 필터 이름을 반환합니다."""

    return tuple(
        prop.identifier
        for prop in space.bl_rna.properties
        if prop.identifier.startswith("show_object_viewport_")
    )


def _set_all_filters(screen, enabled: bool) -> None:
    """테스트 Source Screen의 모든 Object 타입 필터를 지정 값으로 설정합니다."""

    spaces = _view_spaces(screen)
    if not spaces:
        raise AssertionError("Source Workspace에 3D Viewport가 없습니다.")
    for space in spaces:
        for name in _filter_names(space):
            setattr(space, name, bool(enabled))


def _filter_snapshot(screen) -> tuple[tuple[tuple[str, bool], ...], ...]:
    """사용자 Screen 필터 보존 여부를 비교할 불변 Snapshot을 반환합니다."""

    return tuple(
        tuple((name, bool(getattr(space, name))) for name in _filter_names(space))
        for space in _view_spaces(screen)
    )


class WorkspaceHandoffProbe:
    """종료 버튼 없이 세 도구를 전환하며 단일 활성 Workspace 계약을 검증합니다."""

    def __init__(self, extension) -> None:
        """통합 Extension 모듈과 단계별 검증 상태를 초기화합니다."""

        self.extension = extension
        self.started_at = time.monotonic()
        self.stage = "OPEN_ALIGN"
        self.source_workspace = bpy.context.window.workspace
        self.source_screen = bpy.context.window.screen
        _set_all_filters(self.source_screen, False)
        self.source_filters = _filter_snapshot(self.source_screen)
        self.align_workspace = None
        self.stale_align_pointer = 0
        self.modifier_workspace = None
        self.paint_workspace_pointer = 0
        self.align_session = import_module(f"{PACKAGE_NAME}.tools.align.session")
        self.align_state = import_module(f"{PACKAGE_NAME}.tools.align.state")
        self.modifier_session = import_module(
            f"{PACKAGE_NAME}.tools.modifier.blender.modifier_session"
        )
        self.paint_workspace = import_module(
            f"{PACKAGE_NAME}.tools.projection_paint.blender.paint_workspace"
        )
        self.paint_lifecycle = import_module(
            f"{PACKAGE_NAME}.tools.projection_paint.blender.lifecycle"
        )
        self.factory = import_module(
            f"{PACKAGE_NAME}.shared.blender_runtime.workspace_factory"
        )
        self.marker_session = import_module(
            f"{PACKAGE_NAME}.shared.blender_runtime.scene_marker_session"
        )
        self.workspace_handoff = import_module(
            f"{PACKAGE_NAME}.shared.blender_runtime.workspace_handoff"
        )
        excluded = {self.source_workspace.as_pointer()}
        self.external_after_align = self._ordinary_workspace("Modeling", excluded)
        excluded.add(self.external_after_align.as_pointer())
        self.external_after_modifier = self._ordinary_workspace("Scripting", excluded)
        excluded.add(self.external_after_modifier.as_pointer())
        self.external_after_paint = self._ordinary_workspace("UV Editing", excluded)

    def _ordinary_workspace(self, preferred_name: str, excluded: set[int]):
        """COLMAP 소유본이 아닌 테스트용 일반 Workspace를 하나 선택합니다."""

        preferred = bpy.data.workspaces.get(preferred_name)
        if (
            preferred is not None
            and preferred.as_pointer() not in excluded
            and not self.factory.owned_uuid_of(preferred)
        ):
            return preferred
        candidate = next(
            (
                workspace
                for workspace in bpy.data.workspaces
                if workspace.as_pointer() not in excluded
                and not self.factory.owned_uuid_of(workspace)
            ),
            None,
        )
        if candidate is None:
            raise AssertionError("일반 Workspace 전환 검증 대상을 찾지 못했습니다.")
        return candidate

    def _assert_source_unchanged(self) -> None:
        """사용자 Source Screen의 Viewport 필터가 진입 전 상태인지 검사합니다."""

        if _filter_snapshot(self.source_screen) != self.source_filters:
            raise AssertionError("사용자 Source Workspace의 Viewport 필터가 변경되었습니다.")

    def _assert_no_failed_workspace(self) -> None:
        """공용 소유 Workspace 중 FAILED 상태가 남지 않았는지 검사합니다."""

        failed = tuple(
            workspace.name
            for workspace in bpy.data.workspaces
            if self.factory.owned_uuid_of(workspace)
            and workspace.get(self.factory.WORKSPACE_STATE_KEY) == self.factory.STATE_FAILED
        )
        if failed:
            raise AssertionError(f"FAILED COLMAP Workspace가 남았습니다: {failed}")

    def _paint_ready(self, window) -> bool:
        """현재 Projection Paint Workspace의 화면과 READY 소유권을 검증합니다."""

        settings = bpy.context.scene.projection_paint
        workspace = getattr(settings, "paint_workspace", None)
        if workspace is None or window.workspace is not workspace:
            return False
        if str(workspace.get(self.paint_workspace.LAYOUT_STAGE_KEY, "")) != "COMPLETE":
            return False
        if not self.factory.workspace_matches_spec(
            workspace,
            self.paint_workspace.PAINT_WORKSPACE_SPEC,
        ):
            raise AssertionError("Projection Paint READY Workspace 계약이 다릅니다.")
        if self.paint_workspace_pointer == 0:
            self.paint_workspace_pointer = workspace.as_pointer()
        return True

    def _assert_align_panel_visible(self, window) -> None:
        """현재 Align P 뷰에서 최상위 N 패널의 표시 조건이 유지되는지 확인합니다."""

        align_panels = import_module(f"{PACKAGE_NAME}.tools.align.ui.panels")
        moving_area = sorted(window.screen.areas, key=lambda area: area.x)[-1]
        with bpy.context.temp_override(
            window=window,
            screen=window.screen,
            area=moving_area,
        ):
            if not align_panels.COLMAPALIGN_PT_main.poll(bpy.context):
                raise AssertionError("Align READY Workspace의 N 패널 UI가 표시되지 않았습니다.")

    def _finish(self) -> None:
        """성공 결과를 출력하고 Extension을 해제한 뒤 Process를 정상 종료합니다."""

        self._assert_source_unchanged()
        self._assert_no_failed_workspace()
        _deactivate_extension(self.extension)
        print("COLMAP_TOOLS_WORKSPACE_ISOLATION_GUI_OK")
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(0)

    def run(self) -> float | None:
        """Timer Tick마다 자동 종료와 후속 진입 완료 상태를 검증합니다."""

        if time.monotonic() - self.started_at > TIMEOUT_SECONDS:
            active_name = str(getattr(getattr(bpy.context, "window", None), "workspace", None))
            paint_settings = bpy.context.scene.projection_paint
            paint_workspace = getattr(paint_settings, "paint_workspace", None)
            paint_failures = tuple(
                (item.component, item.exception_type, item.message)
                for item in self.paint_lifecycle.lifecycle_registry().failures
            )
            raise TimeoutError(
                "Workspace 자동 전환 GUI 검증 시간 초과: "
                f"{self.stage}, active={active_name}, "
                f"align={self.align_state.RUNTIME.session.phase}, "
                f"modifier={self.modifier_session.SESSION.phase}, "
                f"modifier_status={self.modifier_session.SESSION.status_message!r}, "
                f"align_error={self.align_state.RUNTIME.workspace_setup_error!r}, "
                f"paint_state={paint_settings.paint_state!r}, "
                f"paint_status={paint_settings.status!r}, "
                f"paint_status_kind={paint_settings.status_kind!r}, "
                f"view_layer={getattr(bpy.context.window.view_layer, 'name', '')!r}, "
                f"layout_stage={getattr(paint_workspace, 'get', lambda *_: '')(self.paint_workspace.LAYOUT_STAGE_KEY, '')!r}, "
                f"paint_failures={paint_failures!r}"
            )
        window = bpy.context.window
        if window is None:
            raise AssertionError("검증할 Blender Window가 없습니다.")

        if self.stage == "OPEN_ALIGN":
            if bpy.ops.colmap_align.open_workspace("EXEC_DEFAULT") != {"FINISHED"}:
                raise AssertionError("Align 진입 요청에 실패했습니다.")
            self.stage = "WAIT_ALIGN"
            return 0.05

        if self.stage == "WAIT_ALIGN":
            if not self.align_session.is_session_active_for_window(window):
                return 0.05
            self.align_workspace = window.workspace
            if bpy.ops.colmap.open_modifier_workspace("EXEC_DEFAULT") != {"FINISHED"}:
                raise AssertionError("Align에서 Modifier 자동 전환 요청에 실패했습니다.")
            self.stage = "WAIT_MODIFIER"
            return 0.05

        if self.stage == "WAIT_MODIFIER":
            if self.modifier_session.SESSION.phase != self.modifier_session.SESSION_ACTIVE:
                return 0.05
            if self.align_state.RUNTIME.session.phase != "INACTIVE":
                raise AssertionError("Modifier 진입 전에 Align 세션이 종료되지 않았습니다.")
            if self.marker_session.scene_marker_session_owner(bpy.context.scene) != "modifier":
                raise AssertionError("Modifier 진입 뒤 Scene Marker 사용권을 획득하지 못했습니다.")
            self.modifier_workspace = window.workspace
            if self.modifier_session.session_source_workspace() is not self.source_workspace:
                raise AssertionError("Modifier Source가 사용자 Workspace가 아닙니다.")
            if bpy.ops.projection_paint.open_workspace("EXEC_DEFAULT") != {"FINISHED"}:
                raise AssertionError("Modifier에서 Paint 자동 전환 요청에 실패했습니다.")
            self.stage = "WAIT_PAINT"
            return 0.05

        if self.stage == "WAIT_PAINT":
            if not self._paint_ready(window):
                return 0.05
            if self.modifier_session.SESSION.phase != self.modifier_session.SESSION_INACTIVE:
                raise AssertionError("Paint 진입 전에 Modifier 세션이 종료되지 않았습니다.")
            if (
                self.marker_session.scene_marker_session_owner(bpy.context.scene)
                != "projection_paint"
            ):
                raise AssertionError("Paint 진입 전에 Modifier 사용권이 반납되지 않았습니다.")
            workspace = window.workspace
            source_name = str(workspace.get(self.factory.WORKSPACE_SOURCE_KEY, "") or "")
            if source_name != self.source_workspace.name:
                raise AssertionError("Paint Source가 사용자 Workspace가 아닙니다.")
            if bpy.ops.colmap_align.open_workspace("EXEC_DEFAULT") != {"FINISHED"}:
                raise AssertionError("Paint에서 Align 자동 전환 요청에 실패했습니다.")
            self.stage = "WAIT_ALIGN_AFTER_PAINT"
            return 0.05

        if self.stage == "WAIT_ALIGN_AFTER_PAINT":
            if not self.align_session.is_session_active_for_window(window):
                return 0.05
            if self.marker_session.scene_marker_session_owner(bpy.context.scene):
                raise AssertionError("Paint 종료 뒤 Scene Marker 사용권이 남았습니다.")
            paint_workspace = next(
                (
                    workspace
                    for workspace in bpy.data.workspaces
                    if workspace.as_pointer() == self.paint_workspace_pointer
                ),
                None,
            )
            if paint_workspace is None:
                raise AssertionError("Paint 종료 뒤 READY Workspace가 삭제되었습니다.")
            if bpy.context.scene.projection_paint.paint_workspace is not paint_workspace:
                raise AssertionError("Paint 종료 뒤 재사용 Pointer가 유지되지 않았습니다.")
            if window.workspace is not self.align_workspace:
                raise AssertionError("기존 Align Workspace를 재사용하지 않았습니다.")
            self._assert_no_failed_workspace()
            if bpy.ops.projection_paint.open_workspace("EXEC_DEFAULT") != {"FINISHED"}:
                raise AssertionError("Paint READY Workspace 재진입 요청에 실패했습니다.")
            self.stage = "WAIT_REUSED_PAINT"
            return 0.05

        if self.stage == "WAIT_REUSED_PAINT":
            if not self._paint_ready(window):
                return 0.05
            if (
                self.marker_session.scene_marker_session_owner(bpy.context.scene)
                != "projection_paint"
            ):
                raise AssertionError("Paint 재진입 뒤 Scene Marker 사용권을 획득하지 못했습니다.")
            if window.workspace.as_pointer() != self.paint_workspace_pointer:
                raise AssertionError("기존 Paint READY Workspace를 재사용하지 않았습니다.")
            if self.align_state.RUNTIME.session.phase != "INACTIVE":
                raise AssertionError("Paint 재진입 전에 Align 세션이 종료되지 않았습니다.")
            if bpy.ops.colmap_align.open_workspace("EXEC_DEFAULT") != {"FINISHED"}:
                raise AssertionError("재사용 Paint에서 Align 자동 전환 요청에 실패했습니다.")
            self.stage = "WAIT_ALIGN_AFTER_REUSED_PAINT"
            return 0.05

        if self.stage == "WAIT_ALIGN_AFTER_REUSED_PAINT":
            if not self.align_session.is_session_active_for_window(window):
                return 0.05
            if self.marker_session.scene_marker_session_owner(bpy.context.scene):
                raise AssertionError("Paint 재사용 종료 뒤 Scene Marker 사용권이 남았습니다.")
            if not any(
                workspace.as_pointer() == self.paint_workspace_pointer
                for workspace in bpy.data.workspaces
            ):
                raise AssertionError("Paint 재사용 종료 뒤 READY Workspace가 삭제되었습니다.")
            if window.workspace is not self.align_workspace:
                raise AssertionError("Paint 재사용 종료 뒤 기존 Align Workspace가 열리지 않았습니다.")
            window.workspace = self.external_after_align
            self.stage = "WAIT_EXTERNAL_AFTER_ALIGN"
            return 0.05

        if self.stage == "WAIT_EXTERNAL_AFTER_ALIGN":
            if (
                self.align_state.RUNTIME.session.phase != "INACTIVE"
                or window.workspace is not self.external_after_align
            ):
                return 0.05
            if not any(
                workspace.as_pointer() == self.align_workspace.as_pointer()
                for workspace in bpy.data.workspaces
            ):
                raise AssertionError("일반 Workspace 이동 뒤 READY Align Workspace가 삭제되었습니다.")
            window.workspace = self.align_workspace
            self.stage = "WAIT_MANUAL_ALIGN_PANEL"
            return 0.05

        if self.stage == "WAIT_MANUAL_ALIGN_PANEL":
            if window.workspace is not self.align_workspace:
                return 0.05
            if not self.factory.screen_matches_workspace(window.screen, window.workspace):
                return 0.05
            self._assert_align_panel_visible(window)
            self.stage = "WAIT_MANUAL_ALIGN_ACTIVE"
            return 0.05

        if self.stage == "WAIT_MANUAL_ALIGN_ACTIVE":
            if not self.align_session.is_session_active_for_window(window):
                return 0.05
            if window.workspace is not self.align_workspace:
                raise AssertionError("수동 전환 대상 Align Workspace가 활성화되지 않았습니다.")
            self._assert_align_panel_visible(window)
            window.workspace = self.external_after_align
            self.stage = "WAIT_EXTERNAL_AFTER_MANUAL_ALIGN"
            return 0.05

        if self.stage == "WAIT_EXTERNAL_AFTER_MANUAL_ALIGN":
            if (
                self.align_state.RUNTIME.session.phase != "INACTIVE"
                or window.workspace is not self.external_after_align
            ):
                return 0.05
            window.workspace = self.modifier_workspace
            self.stage = "WAIT_MANUAL_MODIFIER"
            return 0.05

        if self.stage == "WAIT_MANUAL_MODIFIER":
            if self.modifier_session.SESSION.phase != self.modifier_session.SESSION_ACTIVE:
                return 0.05
            if self.align_state.RUNTIME.session.phase != "INACTIVE":
                raise AssertionError("수동 Modifier 전환 전에 Align 세션이 종료되지 않았습니다.")
            if window.workspace is not self.modifier_workspace:
                raise AssertionError("수동 전환 대상 Modifier Workspace가 활성화되지 않았습니다.")
            if self.marker_session.scene_marker_session_owner(bpy.context.scene) != "modifier":
                raise AssertionError("수동 Modifier 전환 뒤 Scene Marker 사용권이 없습니다.")
            # Blender 파일 로드가 Message Bus를 비운 상태를 재현한 뒤 load_post 복구를 검사합니다.
            bpy.msgbus.clear_by_owner(self.workspace_handoff._MSGBUS_OWNER)
            self.workspace_handoff.remember_workspace_handoffs_after_load()
            window.workspace = self.align_workspace
            self.stage = "WAIT_DIRECT_ALIGN_AFTER_MODIFIER"
            return 0.05

        if self.stage == "WAIT_DIRECT_ALIGN_AFTER_MODIFIER":
            if not self.align_session.is_session_active_for_window(window):
                return 0.05
            if self.modifier_session.SESSION.phase != self.modifier_session.SESSION_INACTIVE:
                raise AssertionError("Modifier 직접 전환 뒤 이전 세션이 종료되지 않았습니다.")
            if window.workspace is not self.align_workspace:
                raise AssertionError("Modifier에서 직접 선택한 Align Workspace가 활성화되지 않았습니다.")
            self._assert_align_panel_visible(window)
            window.workspace = self.external_after_modifier
            self.stage = "WAIT_EXTERNAL_AFTER_MODIFIER"
            return 0.05

        if self.stage == "WAIT_EXTERNAL_AFTER_MODIFIER":
            if (
                self.align_state.RUNTIME.session.phase != "INACTIVE"
                or window.workspace is not self.external_after_modifier
            ):
                return 0.05
            if self.marker_session.scene_marker_session_owner(bpy.context.scene):
                raise AssertionError("일반 Workspace 이동 뒤 Modifier 사용권이 남았습니다.")
            if not any(
                workspace.as_pointer() == self.modifier_workspace.as_pointer()
                for workspace in bpy.data.workspaces
            ):
                raise AssertionError(
                    "일반 Workspace 이동 뒤 READY Modifier Workspace가 삭제되었습니다."
                )
            paint_workspace = next(
                (
                    workspace
                    for workspace in bpy.data.workspaces
                    if workspace.as_pointer() == self.paint_workspace_pointer
                ),
                None,
            )
            if paint_workspace is None:
                raise AssertionError("수동 재진입할 Paint Workspace가 사라졌습니다.")
            window.workspace = paint_workspace
            self.stage = "WAIT_MANUAL_PAINT"
            return 0.05

        if self.stage == "WAIT_MANUAL_PAINT":
            if not self._paint_ready(window):
                return 0.05
            if (
                self.marker_session.scene_marker_session_owner(bpy.context.scene)
                != "projection_paint"
            ):
                return 0.05
            window.workspace = self.external_after_paint
            self.stage = "WAIT_EXTERNAL_AFTER_PAINT"
            return 0.05

        if self.stage == "WAIT_EXTERNAL_AFTER_PAINT":
            settings = bpy.context.scene.projection_paint
            if (
                str(settings.status) != "Projection Paint Workspace 종료"
                or window.workspace is not self.external_after_paint
            ):
                return 0.05
            if self.marker_session.scene_marker_session_owner(bpy.context.scene):
                raise AssertionError("일반 Workspace 이동 뒤 Paint 사용권이 남았습니다.")
            if not any(
                workspace.as_pointer() == self.paint_workspace_pointer
                for workspace in bpy.data.workspaces
            ):
                raise AssertionError("일반 Workspace 이동 뒤 READY Paint Workspace가 삭제되었습니다.")
            if bpy.ops.colmap_align.open_workspace("EXEC_DEFAULT") != {"FINISHED"}:
                raise AssertionError("일반 Workspace에서 최종 Align 진입 요청에 실패했습니다.")
            self.stage = "WAIT_FINAL_ALIGN"
            return 0.05

        if self.stage == "WAIT_FINAL_ALIGN":
            if not self.align_session.is_session_active_for_window(window):
                return 0.05
            if self.modifier_session.SESSION.phase != self.modifier_session.SESSION_INACTIVE:
                raise AssertionError("최종 Align 진입 전에 Modifier 세션이 종료되지 않았습니다.")
            if bpy.ops.colmap_align.exit_session("EXEC_DEFAULT") != {"FINISHED"}:
                raise AssertionError("최종 Align 종료 요청에 실패했습니다.")
            self.stage = "PREPARE_STALE_ALIGN"
            return 0.05

        if self.stage == "PREPARE_STALE_ALIGN":
            if window.workspace is not self.external_after_paint:
                return 0.05
            self.stale_align_pointer = self.align_workspace.as_pointer()
            self.factory.mark_workspace_failed(self.align_workspace)
            if bpy.ops.colmap.open_modifier_workspace("EXEC_DEFAULT") != {"FINISHED"}:
                raise AssertionError("잔여 실패본 회귀용 Modifier 진입 요청에 실패했습니다.")
            self.stage = "WAIT_STALE_MODIFIER"
            return 0.05

        if self.stage == "WAIT_STALE_MODIFIER":
            if self.modifier_session.SESSION.phase != self.modifier_session.SESSION_ACTIVE:
                return 0.05
            if bpy.ops.colmap_align.open_workspace("EXEC_DEFAULT") != {"FINISHED"}:
                raise AssertionError("FAILED Align 자동 교체 요청에 실패했습니다.")
            self.stage = "WAIT_STALE_ALIGN_REPLACEMENT"
            return 0.05

        if self.stage == "WAIT_STALE_ALIGN_REPLACEMENT":
            if not self.align_session.is_session_active_for_window(window):
                return 0.05
            if any(
                workspace.as_pointer() == self.stale_align_pointer
                for workspace in bpy.data.workspaces
            ):
                raise AssertionError("FAILED Align Workspace 삭제 전에 새 Align이 시작되었습니다.")
            if self.modifier_session.SESSION.phase != self.modifier_session.SESSION_INACTIVE:
                raise AssertionError("FAILED Align 교체 전에 Modifier가 종료되지 않았습니다.")
            self._assert_no_failed_workspace()
            if bpy.ops.colmap_align.exit_session("EXEC_DEFAULT") != {"FINISHED"}:
                raise AssertionError("교체된 Align 종료 요청에 실패했습니다.")
            self.stage = "WAIT_SOURCE"
            return 0.05

        if self.stage == "WAIT_SOURCE":
            if window.workspace is not self.external_after_paint:
                return 0.05
            self._finish()
            return None

        raise AssertionError(f"알 수 없는 GUI 검증 단계입니다: {self.stage}")


def _run_probe() -> float | None:
    """검증 예외를 출력하고 Process 실패 코드로 전달합니다."""

    try:
        return PROBE.run()
    except Exception:
        traceback.print_exc()
        try:
            _deactivate_extension(EXTENSION)
        except Exception:
            traceback.print_exc()
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(1)


EXTENSION = _activate_extension()
PROBE = WorkspaceHandoffProbe(EXTENSION)
bpy.app.timers.register(_run_probe, first_interval=0.1)
