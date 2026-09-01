"""Blender Background에서 COLMAP Modifier Workspace 등록 계약을 검증합니다."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import bpy


PROJECT_PARENT = Path(__file__).resolve().parents[3]
if str(PROJECT_PARENT) not in sys.path:
    sys.path.insert(0, str(PROJECT_PARENT))

from tools import modifier as blender_colmap
from tools.modifier.blender.modifier_session import (
    CONTROL_PANEL_CATEGORY,
    MAIN_SCREEN_OWNER_KEY,
    SESSION,
    SESSION_ACTIVE,
    SESSION_INACTIVE,
    WORKSPACE_NAME,
    WORKSPACE_OWNER_KEY,
    WORKSPACE_SOURCE_KEY,
    WORKSPACE_VISIBILITY_SNAPSHOT_KEY,
    modifier_panel_poll,
)
from tools.modifier.blender.modifier_workspace import (
    TIMELINE_HEIGHT_FACTOR,
    _record_runtime_restore_error,
    clear_modifier_session_before_load,
    restore_modifier_session_after_load,
)
from tools.modifier.blender.modifier_viewport import (
    DEFAULT_STARTUP_OBJECTS,
    hide_default_startup_objects,
    restore_default_startup_objects,
)
from tools.modifier.blender.operators_modifier_workspace import (
    COLMAP_OT_exit_modifier_workspace,
    COLMAP_OT_open_modifier_workspace,
    COLMAP_OT_restore_modifier_workspace,
)
from tools.modifier.blender.panels import CLASSES as PANEL_CLASSES
from shell.menu import COLMAP_MT_tools, draw_window_menu


def assert_true(condition, message: str) -> None:
    """조건이 거짓이면 계약 위반 원인과 함께 실패합니다."""

    if not condition:
        raise AssertionError(message)


class _RecordingLayout:
    """Window와 COLMAP Tools 메뉴의 UI 호출을 기록합니다."""

    def __init__(self):
        self.calls: list[tuple[str, str, dict[str, object]]] = []

    def separator(self):
        """메뉴 구분선 호출을 기록 대상에서 제외합니다."""

    def operator(self, operator_id: str, **kwargs):
        """Operator ID와 표시 옵션을 기록합니다."""

        self.calls.append(("OPERATOR", operator_id, kwargs))
        return object()

    def menu(self, menu_id: str, **kwargs):
        """하위 메뉴 ID와 표시 옵션을 기록합니다."""

        self.calls.append(("MENU", menu_id, kwargs))
        return object()


blender_colmap.register()
try:
    blender_colmap.register()
    assert_true(
        blender_colmap.__version__ == "0.17.4",
        "COLMAP Modifier 버전이 0.17.4가 아닙니다.",
    )
    assert_true(
        (
            WORKSPACE_NAME,
            WORKSPACE_OWNER_KEY,
            WORKSPACE_SOURCE_KEY,
            WORKSPACE_VISIBILITY_SNAPSHOT_KEY,
            MAIN_SCREEN_OWNER_KEY,
            CONTROL_PANEL_CATEGORY,
        )
        == (
            "COLMAP Modifier",
            "colmap_modifier_owned",
            "colmap_modifier_source_workspace",
            "colmap_modifier_visibility_snapshot",
            "colmap_modifier_main_screen",
            "COLMAP Modifier",
        ),
        "Workspace 이름과 소유권 상수가 중앙 계약과 다릅니다.",
    )
    assert_true(
        TIMELINE_HEIGHT_FACTOR == 0.2,
        "하단 Timeline 높이 비율이 중앙 상수와 다릅니다.",
    )
    assert_true(
        tuple(panel.bl_idname for panel in PANEL_CLASSES)
        == (
            "COLMAP_PT_modifier",
            "COLMAP_PT_status",
            "COLMAP_PT_import",
            "COLMAP_PT_point_selection",
            "COLMAP_PT_camera_review",
            "COLMAP_PT_export",
        )
        and all(panel.bl_category == CONTROL_PANEL_CATEGORY for panel in PANEL_CLASSES),
        "Modifier 부모·하위 패널 등록 순서 또는 Category가 잘못되었습니다.",
    )
    assert_true(
        all(
            panel.bl_parent_id == "COLMAP_PT_modifier"
            for panel in PANEL_CLASSES[1:]
        ),
        "기능 패널이 COLMAP Modifier 부모 패널 아래에 연결되지 않았습니다.",
    )
    assert_true(
        not modifier_panel_poll(bpy.context)
        and not PANEL_CLASSES[0].poll(bpy.context),
        "활성 Session이 없는 일반 Context에서 Modifier 패널이 표시됩니다.",
    )
    assert_true(
        bool(COLMAP_OT_open_modifier_workspace.poll(bpy.context))
        == bool(bpy.context.window_manager and bpy.context.window),
        "Workspace 진입 Poll이 실제 Window Context 유효성과 일치하지 않습니다.",
    )
    operator_contract = {
        COLMAP_OT_open_modifier_workspace: (
            "colmap.open_modifier_workspace",
            "COLMAP Modifier 열기",
            {"REGISTER"},
        ),
        COLMAP_OT_restore_modifier_workspace: (
            "colmap.restore_modifier_workspace",
            "COLMAP Modifier Workspace 복원",
            {"INTERNAL"},
        ),
        COLMAP_OT_exit_modifier_workspace: (
            "colmap.exit_modifier_workspace",
            "COLMAP Modifier 종료",
            {"REGISTER"},
        ),
    }
    for operator, expected in operator_contract.items():
        assert_true(
            (operator.bl_idname, operator.bl_label, set(operator.bl_options)) == expected,
            f"{operator.__name__}의 ID, Label 또는 Undo 계약이 잘못되었습니다.",
        )
    assert_true(
        (COLMAP_MT_tools.bl_idname, COLMAP_MT_tools.bl_label)
        == ("COLMAP_MT_tools", "COLMAP Tools"),
        "COLMAP Tools 하위 메뉴의 공개 식별자 또는 이름이 잘못되었습니다.",
    )

    menu = type("WindowMenu", (), {"layout": _RecordingLayout()})()
    draw_window_menu(menu, None)
    assert_true(
        menu.layout.calls
        == [
            (
                "MENU",
                "COLMAP_MT_tools",
                {"text": "COLMAP Tools", "icon": "FILE_FOLDER"},
            )
        ],
        "Window 메뉴가 COLMAP Tools 하위 메뉴와 연결되지 않았습니다.",
    )
    tools_menu = type("ToolsMenu", (), {"layout": _RecordingLayout()})()
    COLMAP_MT_tools.draw(tools_menu, None)
    assert_true(
        tools_menu.layout.calls
        == [
            (
                "OPERATOR",
                "colmap_align.open_workspace",
                {"text": "COLMAP Align 열기", "icon": "ORIENTATION_GLOBAL"},
            ),
            (
                "OPERATOR",
                "colmap.open_modifier_workspace",
                {"text": "COLMAP Modifier 열기", "icon": "MODIFIER"},
            ),
            (
                "OPERATOR",
                "projection_paint.open_workspace",
                {"text": "Projection Painting 열기", "icon": "TPAINT_HLT"},
            ),
        ],
        "COLMAP Tools 메뉴가 세 독립 도구 Operator와 연결되지 않았습니다.",
    )
    assert_true(
        bpy.app.handlers.load_pre.count(clear_modifier_session_before_load) == 1
        and bpy.app.handlers.load_post.count(restore_modifier_session_after_load) == 1,
        "Modifier 파일 로드 Handler가 없거나 중복 등록되었습니다.",
    )
    assert_true(
        hasattr(bpy.types.Scene, "colmap_import")
        and not hasattr(bpy.types.Scene, "colmap_modifier"),
        "기존 .blend 호환 Scene.colmap_import 식별자가 유지되지 않았습니다.",
    )
    warning_settings = SimpleNamespace(warning_summary="기존 경고")
    with patch.object(SESSION, "phase", SESSION_ACTIVE):
        _record_runtime_restore_error(
            SimpleNamespace(colmap_import=warning_settings),
            ValueError("COLMAP source 파일이 변경되었습니다."),
        )
        workspace_remained_active = SESSION.phase == SESSION_ACTIVE
    warning = warning_settings.warning_summary.splitlines()[-1]
    assert_true(
        workspace_remained_active
        and "COLMAP source 파일이 변경되었습니다." in warning
        and warning_settings.warning_summary.splitlines() == ["기존 경고", warning]
        and warning_settings.status == warning
        and warning_settings.status_kind == "ERROR",
        "원본 변경 시 Workspace를 닫지 않고 Runtime 오류와 기존 경고를 보존하지 못했습니다.",
    )

    workspace = bpy.data.workspaces[0]
    fake_window = SimpleNamespace(
        scene=bpy.context.scene,
        view_layer=bpy.context.view_layer,
        workspace=workspace,
    )
    default_objects = {
        object_name: obj
        for object_name, object_type in DEFAULT_STARTUP_OBJECTS
        if (obj := bpy.context.view_layer.objects.get(object_name)) is not None
        and obj.type == object_type
    }
    original_hidden = {
        name: bool(obj.hide_get(view_layer=bpy.context.view_layer))
        for name, obj in default_objects.items()
    }
    hide_default_startup_objects(fake_window)
    assert_true(
        bool(default_objects)
        and all(obj.hide_get(view_layer=bpy.context.view_layer) for obj in default_objects.values())
        and WORKSPACE_VISIBILITY_SNAPSHOT_KEY in workspace,
        "기본 오브젝트 숨김 또는 저장 가능한 Snapshot 생성에 실패했습니다.",
    )
    SESSION.clear()
    hide_default_startup_objects(fake_window)
    restore_default_startup_objects(fake_window)
    assert_true(
        all(
            obj.hide_get(view_layer=bpy.context.view_layer) == original_hidden[name]
            for name, obj in default_objects.items()
        )
        and WORKSPACE_VISIBILITY_SNAPSHOT_KEY not in workspace,
        "파일 재로드를 모사한 기본 오브젝트 숨김 상태 복원에 실패했습니다.",
    )
finally:
    blender_colmap.unregister()

assert_true(
    SESSION.phase == SESSION_INACTIVE
    and clear_modifier_session_before_load not in bpy.app.handlers.load_pre
    and restore_modifier_session_after_load not in bpy.app.handlers.load_post,
    "Extension 해제 후 Modifier Session 또는 Handler가 남았습니다.",
)
assert_true(
    not hasattr(bpy.types.Scene, "colmap_import"),
    "Extension 해제 후 Scene Property가 남았습니다.",
)

print("BLENDER_COLMAP_MODIFIER_WORKSPACE_BACKGROUND_OK")
