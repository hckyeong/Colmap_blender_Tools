"""Blender Background에서 Paint 작업공간의 기하 안정화 계약을 검증합니다."""

from __future__ import annotations

from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import bpy


ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from shared.blender_runtime.workspace_factory import (  # noqa: E402
    STATE_CREATING,
    STATE_READY,
    WORKSPACE_STATE_KEY,
)
from tools.projection_paint.blender.paint_workspace import (  # noqa: E402
    LAYOUT_GEOMETRY_KEY,
    PAINT_WORKSPACE_SPEC,
    WORKSPACE_KIND,
    _area_geometry_is_stable,
    _configure_viewport_visibility_contract,
    ensure_paint_workspace,
    open_paint_workspace,
    paint_workspace_exit_settled,
    remove_paint_workspace,
    request_paint_workspace_exit,
)
from tools.projection_paint.blender.ownership import tag_owned  # noqa: E402


def _area(pointer: int, x: int, y: int, width: int, height: int) -> SimpleNamespace:
    """테스트용 Blender Area 기하 대체 객체를 만듭니다."""
    return SimpleNamespace(
        as_pointer=lambda: pointer,
        x=x,
        y=y,
        width=width,
        height=height,
    )


class _Workspace(dict):
    """Background에서 Workspace 소유권과 Pointer만 제공하는 테스트 대체 객체입니다."""

    def __init__(self, pointer: int) -> None:
        """고정 Pointer를 가진 빈 Workspace 대체 객체를 초기화합니다."""

        super().__init__()
        self._pointer = int(pointer)
        self.name = f"Workspace {pointer}"
        self.object_mode = "OBJECT"

    def as_pointer(self) -> int:
        """고정된 테스트 Pointer를 반환합니다."""

        return self._pointer


class PaintWorkspaceGeometryTests(unittest.TestCase):
    """Paint 화면 분할 사이의 Area 기하 안정화 판정을 검증합니다."""

    def test_zero_sized_area_waits_for_two_stable_valid_ticks(self) -> None:
        """0×0 Area는 기다리고 유효한 동일 기하가 두 번 관찰된 뒤 진행합니다."""
        workspace = {LAYOUT_GEOMETRY_KEY: ""}
        areas = [_area(1, 0, 0, 800, 600), _area(2, 800, 0, 0, 600)]
        screen = SimpleNamespace(areas=areas)

        self.assertFalse(_area_geometry_is_stable(workspace, screen, 2))
        self.assertEqual(workspace[LAYOUT_GEOMETRY_KEY], "")
        areas[1].width = 200
        self.assertFalse(_area_geometry_is_stable(workspace, screen, 2))
        self.assertTrue(_area_geometry_is_stable(workspace, screen, 2))

    def test_owned_workspace_is_removed_and_pointer_is_cleared(self) -> None:
        """사용하지 않는 애드온 Paint Workspace만 삭제 요청하고 Pointer를 해제합니다."""

        workspace = _Workspace(101)
        tag_owned(workspace, WORKSPACE_KIND)
        settings = SimpleNamespace(paint_workspace=workspace)
        scene = SimpleNamespace(projection_paint=settings)

        with patch(
            "tools.projection_paint.blender.paint_workspace."
            "_request_workspace_deletion",
            return_value=True,
        ) as request_delete:
            self.assertTrue(remove_paint_workspace(scene, workspace=workspace))
        self.assertIsNone(settings.paint_workspace)
        request_delete.assert_called_once()

    def test_user_workspace_is_never_deleted(self) -> None:
        """이름이 비슷해도 소유 태그가 없는 사용자 Workspace는 보존합니다."""

        workspace = _Workspace(202)
        settings = SimpleNamespace(paint_workspace=workspace)
        scene = SimpleNamespace(projection_paint=settings)

        with patch(
            "tools.projection_paint.blender.paint_workspace."
            "_request_workspace_deletion",
        ) as request_delete:
            self.assertFalse(remove_paint_workspace(scene, workspace=workspace))
        self.assertIsNone(settings.paint_workspace)
        request_delete.assert_not_called()

    def test_existing_current_workspace_is_reused_for_paint_entry(self) -> None:
        """설정 단계의 현재 소유 Workspace는 Paint 진입에서 다시 Append하지 않습니다."""

        workspace = _Workspace(303)
        tag_owned(workspace, WORKSPACE_KIND)
        window = SimpleNamespace(
            workspace=workspace,
            as_pointer=lambda: 404,
        )
        settings = SimpleNamespace(paint_workspace=workspace)
        context = SimpleNamespace(
            window=window,
            scene=SimpleNamespace(projection_paint=settings),
        )

        with patch(
            "tools.projection_paint.blender.paint_workspace."
            "_stored_workspace",
            return_value=workspace,
        ):
            result = ensure_paint_workspace(context, reuse_existing=True)

        self.assertIs(result, workspace)
        self.assertIs(window.workspace, workspace)

    def test_normal_exit_keeps_ready_workspace_for_reentry(self) -> None:
        """정상 종료는 Source로 복귀하되 READY Paint Workspace와 Pointer를 유지합니다."""

        source = _Workspace(401)
        workspace = _Workspace(402)
        tag_owned(workspace, WORKSPACE_KIND)
        workspace[WORKSPACE_STATE_KEY] = STATE_READY
        window = SimpleNamespace(
            workspace=workspace,
            as_pointer=lambda: 403,
        )
        settings = SimpleNamespace(
            paint_workspace=workspace,
            paint_enabled=False,
            paint_state="IDLE",
            status="",
            status_kind="",
        )
        scene = SimpleNamespace(projection_paint=settings)
        context = SimpleNamespace(window=window, scene=scene)

        with (
            patch(
                "tools.projection_paint.blender.paint_workspace._stored_workspace",
                return_value=workspace,
            ),
            patch(
                "tools.projection_paint.blender.paint_workspace."
                "source_workspace_from_metadata",
                return_value=source,
            ),
            patch(
                "tools.projection_paint.blender.paint_workspace.remove_paint_workspace",
            ) as remove_workspace,
            patch(
                "tools.projection_paint.blender.paint_workspace.workspace_matches_spec",
                return_value=False,
            ),
            patch(
                "tools.projection_paint.blender.camera_scene."
                "detach_projection_markers",
            ) as detach_markers,
            patch(
                "tools.projection_paint.blender.paint_workspace."
                "release_scene_marker_session",
            ) as release_markers,
        ):
            request_paint_workspace_exit(context)
            settled = paint_workspace_exit_settled(scene, window)

        self.assertIs(window.workspace, source)
        self.assertIs(settings.paint_workspace, workspace)
        self.assertEqual(settings.paint_state, "IDLE")
        self.assertTrue(settled)
        detach_markers.assert_called_once_with(scene)
        release_markers.assert_called_once_with(scene, "projection_paint")
        remove_workspace.assert_not_called()

    def test_incomplete_exit_removes_creating_workspace(self) -> None:
        """준비 중 이탈은 CREATING Paint Workspace를 보존하지 않고 정리합니다."""

        source = _Workspace(411)
        workspace = _Workspace(412)
        tag_owned(workspace, WORKSPACE_KIND)
        workspace[WORKSPACE_STATE_KEY] = STATE_CREATING
        window = SimpleNamespace(
            workspace=workspace,
            as_pointer=lambda: 413,
        )
        settings = SimpleNamespace(
            paint_workspace=workspace,
            paint_enabled=False,
            paint_state="IDLE",
            status="",
            status_kind="",
        )
        scene = SimpleNamespace(projection_paint=settings)
        context = SimpleNamespace(window=window, scene=scene)

        with (
            patch(
                "tools.projection_paint.blender.paint_workspace._stored_workspace",
                return_value=workspace,
            ),
            patch(
                "tools.projection_paint.blender.paint_workspace."
                "source_workspace_from_metadata",
                return_value=source,
            ),
            patch(
                "tools.projection_paint.blender.paint_workspace.remove_paint_workspace",
                return_value=True,
            ) as remove_workspace,
        ):
            request_paint_workspace_exit(context)

        self.assertIs(window.workspace, source)
        self.assertEqual(settings.paint_state, "IDLE")
        self.assertEqual(settings.status, "준비 중이던 Projection Paint Workspace 종료")
        remove_workspace.assert_called_once_with(
            scene,
            window=window,
            workspace=workspace,
        )

    def test_new_workspace_is_appended_from_projection_paint_seed(self) -> None:
        """신규 Paint Workspace는 현재 화면 복제 없이 도구 Seed에서 Append합니다."""

        origin = _Workspace(601)
        created = _Workspace(602)
        window = SimpleNamespace(
            workspace=origin,
            screen=SimpleNamespace(areas=()),
            as_pointer=lambda: 603,
        )
        settings = SimpleNamespace(paint_workspace=None)
        context = SimpleNamespace(
            window=window,
            scene=SimpleNamespace(projection_paint=settings),
        )
        with (
            patch(
                "tools.projection_paint.blender.paint_workspace."
                "cleanup_unused_paint_workspaces",
                return_value=0,
            ),
            patch(
                "tools.projection_paint.blender.paint_workspace."
                "append_seed_workspace",
                return_value=created,
            ) as append_seed,
            patch(
                "tools.projection_paint.blender.paint_workspace.workspace_uuid_of",
                return_value="workspace-uuid",
            ),
        ):
            result = ensure_paint_workspace(context)

        self.assertIs(result, created)
        self.assertIs(settings.paint_workspace, created)
        self.assertIs(window.workspace, created)
        append_seed.assert_called_once_with(
            context,
            window,
            PAINT_WORKSPACE_SPEC,
            origin,
        )

    def test_viewport_contract_enables_every_object_filter(self) -> None:
        """Paint Viewport 계약은 상속된 모든 Object 타입 필터를 활성화합니다."""

        properties = (
            SimpleNamespace(identifier="show_object_viewport_mesh"),
            SimpleNamespace(identifier="show_object_viewport_camera"),
            SimpleNamespace(identifier="show_object_viewport_empty"),
        )
        space = SimpleNamespace(
            bl_rna=SimpleNamespace(properties=properties),
            show_object_viewport_mesh=False,
            show_object_viewport_camera=False,
            show_object_viewport_empty=False,
            local_view=None,
        )
        area = SimpleNamespace(spaces=SimpleNamespace(active=space))

        _configure_viewport_visibility_contract(
            SimpleNamespace(),
            SimpleNamespace(),
            area,
        )

        self.assertTrue(space.show_object_viewport_mesh)
        self.assertTrue(space.show_object_viewport_camera)
        self.assertTrue(space.show_object_viewport_empty)

    def test_production_workspace_does_not_duplicate_active_workspace(self) -> None:
        """Projection Paint Production Source에는 활성 Workspace 복제 호출이 없습니다."""

        source_path = (
            Path(__file__).resolve().parents[3]
            / "tools"
            / "projection_paint"
            / "blender"
            / "paint_workspace.py"
        )
        source = source_path.read_text(encoding="utf-8")
        self.assertNotIn("bpy.ops.workspace.duplicate()", source)
        self.assertIn("append_seed_workspace", source)

    def test_open_workspace_starts_layout_without_enabling_paint(self) -> None:
        """Window 진입은 작업공간 구성만 예약하고 Paint 상태를 켜지 않습니다."""

        workspace = SimpleNamespace(object_mode="TEXTURE_PAINT")
        settings = SimpleNamespace(
            paint_workspace=None,
            paint_enabled=False,
            paint_state="IDLE",
            status="",
            status_kind="",
        )
        window = SimpleNamespace(as_pointer=lambda: 505, workspace=object())
        context = SimpleNamespace(
            window=window,
            scene=SimpleNamespace(projection_paint=settings),
        )

        with (
            patch(
                "tools.projection_paint.blender.paint_workspace."
                "_stored_workspace",
                return_value=None,
            ),
            patch(
                "tools.projection_paint.blender.paint_workspace."
                "ensure_paint_workspace",
                return_value=workspace,
            ),
            patch(
                "tools.projection_paint.blender.paint_workspace."
                "_schedule_open_workspace_configuration",
            ) as schedule,
            patch(
                "tools.projection_paint.blender.paint_workspace."
                "acquire_scene_marker_session",
                return_value=object(),
            ),
            patch(
                "tools.projection_paint.blender.camera_scene."
                "attach_projection_markers",
            ),
        ):
            result = open_paint_workspace(context)

        self.assertIs(result, workspace)
        self.assertFalse(settings.paint_enabled)
        self.assertEqual("OBJECT", workspace.object_mode)
        self.assertEqual("RUNNING", settings.status_kind)
        schedule.assert_called_once_with(context.scene, window, workspace)


if __name__ == "__main__":
    unittest.main(argv=[__file__], verbosity=2)
