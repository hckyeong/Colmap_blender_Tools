"""GUI Blender에서 COLMAP Modifier Workspace 진입·복구·종료를 검증합니다."""

from __future__ import annotations

import importlib
import os
import sys
import time
from pathlib import Path

import bpy


PROJECT_PARENT = Path(__file__).resolve().parents[3]
if str(PROJECT_PARENT) not in sys.path:
    sys.path.insert(0, str(PROJECT_PARENT))

USE_INSTALLED = os.environ.get("BLENDER_COLMAP_USE_INSTALLED") == "1"
PACKAGE_NAME = (
    "bl_ext.user_default.colmap_tools.tools.modifier"
    if USE_INSTALLED
    else "tools.modifier"
)
blender_colmap = importlib.import_module(PACKAGE_NAME)
modifier_session = importlib.import_module(
    f"{PACKAGE_NAME}.blender.modifier_session"
)
panels = importlib.import_module(f"{PACKAGE_NAME}.blender.panels")
modifier_viewport = importlib.import_module(
    f"{PACKAGE_NAME}.blender.modifier_viewport"
)

MAIN_SCREEN_OWNER_KEY = modifier_session.MAIN_SCREEN_OWNER_KEY
SESSION = modifier_session.SESSION
SESSION_ACTIVE = modifier_session.SESSION_ACTIVE
WORKSPACE_NAME = modifier_session.WORKSPACE_NAME
WORKSPACE_OWNER_KEY = modifier_session.WORKSPACE_OWNER_KEY
WORKSPACE_VISIBILITY_SNAPSHOT_KEY = (
    modifier_session.WORKSPACE_VISIBILITY_SNAPSHOT_KEY
)
COLMAP_PT_modifier = panels.COLMAP_PT_modifier


_STAGE = "OPEN"
_STARTED_AT = time.monotonic()
_SOURCE_WORKSPACE = None
_SOURCE_SCREEN = None
_SOURCE_AREA_SIGNATURE = ()
_BASE_WINDOW_COUNT = 0
_BASE_WORKSPACE_COUNT = 0
PROBE_TIMEOUT_SECONDS = 20.0
_DEFAULT_OBJECT_HIDE_STATES = {}


def _area_signature(screen) -> tuple[tuple[str, int, int, int, int], ...]:
    """사용자 Screen 훼손 여부를 비교할 Area 구성을 반환합니다."""

    return tuple(
        sorted((area.type, area.x, area.y, area.width, area.height) for area in screen.areas)
    )


def _fail(message: str) -> None:
    """실패 원인을 출력하고 Blender를 오류 코드로 종료합니다."""

    print(f"COLMAP_MODIFIER_WORKSPACE_GUI_FAIL: {message}")
    bpy.ops.wm.quit_blender()
    raise AssertionError(message)


def _control_override(window):
    """전용 Control View의 UI Region Context를 반환합니다."""

    area = next(area for area in window.screen.areas if area.type == "VIEW_3D")
    region = next(region for region in area.regions if region.type == "UI")
    return {
        "window": window,
        "screen": window.screen,
        "area": area,
        "region": region,
    }


def _run_probe():
    """Timer 단계별로 실제 Window 전환과 복구 결과를 검증합니다."""

    global _STAGE
    if time.monotonic() - _STARTED_AT > PROBE_TIMEOUT_SECONDS:
        area_summary = tuple(
            (
                area.type,
                getattr(area, "ui_type", ""),
                area.x,
                area.y,
                area.width,
                area.height,
            )
            for area in bpy.context.window.screen.areas
        )
        settings_status = getattr(
            getattr(bpy.context.scene, "colmap_import", None),
            "status",
            "",
        )
        _fail(
            f"{PROBE_TIMEOUT_SECONDS:.0f}초 안에 완료되지 않았습니다: "
            f"stage={_STAGE}, session={SESSION.phase}, "
            f"status={SESSION.status_message or settings_status}, areas={area_summary}"
        )

    window = bpy.context.window
    if window is None:
        _fail("현재 Blender Window가 없습니다.")

    if _STAGE == "OPEN":
        if COLMAP_PT_modifier.poll(bpy.context):
            _fail("일반 Workspace에서 Modifier 패널이 표시됩니다.")
        result = bpy.ops.colmap.open_modifier_workspace("EXEC_DEFAULT")
        if result != {"FINISHED"}:
            _fail(f"Workspace 진입 Operator 실패: {result}")
        _STAGE = "WAIT_ACTIVE"
        return 0.05

    if _STAGE == "WAIT_ACTIVE":
        if SESSION.phase != SESSION_ACTIVE:
            return 0.05
        if len(bpy.context.window_manager.windows) != _BASE_WINDOW_COUNT:
            _fail("COLMAP Modifier 진입 중 새 Blender Window가 생성되었습니다.")
        if len(bpy.data.workspaces) != _BASE_WORKSPACE_COUNT + 1:
            _fail("전용 Workspace가 정확히 하나 생성되지 않았습니다.")
        if window.workspace.name != WORKSPACE_NAME or not window.workspace.get(
            WORKSPACE_OWNER_KEY,
            False,
        ):
            _fail("전용 Workspace 이름 또는 소유권이 잘못되었습니다.")
        if not window.screen.get(MAIN_SCREEN_OWNER_KEY, False):
            _fail("전용 Screen 소유권이 기록되지 않았습니다.")
        view_areas = [area for area in window.screen.areas if area.type == "VIEW_3D"]
        timeline_areas = [
            area
            for area in window.screen.areas
            if area.type == "DOPESHEET_EDITOR"
            and getattr(area, "ui_type", "") == "TIMELINE"
        ]
        if (
            len(window.screen.areas) != 2
            or len(view_areas) != 1
            or len(timeline_areas) != 1
            or view_areas[0].y <= timeline_areas[0].y
            or timeline_areas[0].height >= view_areas[0].height
        ):
            _fail("전용 Layout이 상단 3D View와 하단 Timeline으로 구성되지 않았습니다.")
        for object_name, hidden in _DEFAULT_OBJECT_HIDE_STATES.items():
            obj = window.view_layer.objects.get(object_name)
            if obj is not None and not obj.hide_get(view_layer=window.view_layer):
                _fail(f"기본 오브젝트가 자동으로 숨겨지지 않았습니다: {object_name}")
        if not window.workspace.get(WORKSPACE_VISIBILITY_SNAPSHOT_KEY, ""):
            _fail("기본 오브젝트 숨김 Snapshot이 전용 Workspace에 저장되지 않았습니다.")
        region_3d = view_areas[0].spaces.active.region_3d
        if region_3d.view_perspective != "PERSP" or region_3d.view_rotation.angle < 0.1:
            _fail("전용 3D View가 45도 원근 시점으로 설정되지 않았습니다.")
        with bpy.context.temp_override(**_control_override(window)):
            if not COLMAP_PT_modifier.poll(bpy.context):
                _fail("전용 Control View에서 Modifier 패널이 표시되지 않습니다.")
        if _area_signature(_SOURCE_SCREEN) != _SOURCE_AREA_SIGNATURE:
            _fail("진입 전 사용자 Screen의 Area 구성이 변경되었습니다.")
        workspace_count = len(bpy.data.workspaces)
        result = bpy.ops.colmap.open_modifier_workspace("EXEC_DEFAULT")
        if result != {"FINISHED"} or len(bpy.data.workspaces) != workspace_count:
            _fail("중복 진입이 기존 Workspace를 멱등적으로 재사용하지 않았습니다.")
        window.workspace = _SOURCE_WORKSPACE
        _STAGE = "WAIT_RECOVERY"
        return 0.05

    if _STAGE == "WAIT_RECOVERY":
        if window.workspace.name != WORKSPACE_NAME or SESSION.recovery_pending:
            return 0.05
        with bpy.context.temp_override(**_control_override(window)):
            result = bpy.ops.colmap.exit_modifier_workspace("EXEC_DEFAULT")
        if result != {"FINISHED"}:
            _fail(f"Workspace 종료 Operator 실패: {result}")
        _STAGE = "WAIT_EXIT"
        return 0.05

    if _STAGE == "WAIT_EXIT":
        if window.workspace.as_pointer() != _SOURCE_WORKSPACE.as_pointer():
            return 0.05
        if COLMAP_PT_modifier.poll(bpy.context):
            _fail("종료 뒤 일반 Workspace에서 Modifier 패널이 표시됩니다.")
        if _area_signature(_SOURCE_SCREEN) != _SOURCE_AREA_SIGNATURE:
            _fail("종료 뒤 사용자 Screen의 Area 구성이 변경되었습니다.")
        for object_name, hidden in _DEFAULT_OBJECT_HIDE_STATES.items():
            obj = window.view_layer.objects.get(object_name)
            if obj is not None and obj.hide_get(view_layer=window.view_layer) != hidden:
                _fail(f"기본 오브젝트 숨김 상태가 복원되지 않았습니다: {object_name}")
        modifier_workspace = bpy.data.workspaces.get(WORKSPACE_NAME)
        if (
            modifier_workspace is not None
            and WORKSPACE_VISIBILITY_SNAPSHOT_KEY in modifier_workspace
        ):
            _fail("종료 뒤 기본 오브젝트 숨김 Snapshot이 제거되지 않았습니다.")
        print("COLMAP_MODIFIER_WORKSPACE_GUI_OK")
        blender_colmap.unregister()
        bpy.ops.wm.quit_blender()
        return None

    _fail(f"알 수 없는 단계입니다: {_STAGE}")
    return None


def main() -> None:
    """Extension을 등록하고 GUI 검증 Timer를 시작합니다."""

    global _BASE_WINDOW_COUNT, _BASE_WORKSPACE_COUNT
    global _DEFAULT_OBJECT_HIDE_STATES
    global _SOURCE_AREA_SIGNATURE, _SOURCE_SCREEN, _SOURCE_WORKSPACE
    blender_colmap.register()
    _SOURCE_WORKSPACE = bpy.context.window.workspace
    _SOURCE_SCREEN = bpy.context.window.screen
    _SOURCE_AREA_SIGNATURE = _area_signature(_SOURCE_SCREEN)
    _BASE_WINDOW_COUNT = len(bpy.context.window_manager.windows)
    _BASE_WORKSPACE_COUNT = len(bpy.data.workspaces)
    _DEFAULT_OBJECT_HIDE_STATES = {
        object_name: bool(obj.hide_get(view_layer=bpy.context.view_layer))
        for object_name, object_type in modifier_viewport.DEFAULT_STARTUP_OBJECTS
        if (obj := bpy.context.view_layer.objects.get(object_name)) is not None
        and obj.type == object_type
    }
    bpy.app.timers.register(_run_probe, first_interval=0.1)


main()
