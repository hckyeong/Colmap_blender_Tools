"""COLMAP Align Workspace Seed·소유권 계약을 Blender 실행 없이 검증합니다."""

from __future__ import annotations

import ast
from pathlib import Path
import unittest

from shared.blender_runtime.workspace_factory import (
    SCREEN_OWNER_KEY,
    STATE_READY,
    WORKSPACE_OWNER_KEY,
    WORKSPACE_SCHEMA_KEY,
    WORKSPACE_SEED_SCHEMA_KEY,
    WORKSPACE_STATE_KEY,
    WORKSPACE_TOOL_ID_KEY,
    WORKSPACE_UUID_KEY,
    WorkspaceSeedSpec,
    screen_matches_workspace,
    workspace_matches_spec,
)


_ROOT = Path(__file__).resolve().parents[2]
_WORKSPACE_SOURCE = _ROOT / "tools" / "align" / "operators" / "workspace.py"
_CONSTANTS_SOURCE = _ROOT / "tools" / "align" / "constants.py"


class _FakeId(dict):
    """Custom Property와 Blender Pointer를 최소 형태로 표현합니다."""

    def __init__(self, pointer_value: int, **properties: object) -> None:
        """테스트 Pointer와 Custom Property를 초기화합니다."""
        super().__init__(properties)
        self._pointer_value = pointer_value
        self.screens: tuple[_FakeId, ...] = ()

    def as_pointer(self) -> int:
        """고정된 테스트 Pointer를 반환합니다."""
        return self._pointer_value


def _function_source(path: Path, function_name: str) -> str:
    """지정한 함수의 정적 Source Segment를 반환합니다."""
    source = path.read_text(encoding="utf-8")
    module = ast.parse(source)
    function = next(
        node
        for node in module.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == function_name
    )
    return ast.get_source_segment(source, function) or ""


class AlignWorkspaceSeedContractTests(unittest.TestCase):
    """Align Production Source의 Seed Append와 UUID 소유권 계약을 검증합니다."""

    def test_production_workspace_uses_seed_append_without_duplicate(self) -> None:
        """활성 Workspace 복제 호출을 금지하고 Seed Append 호출을 고정합니다."""
        source = _WORKSPACE_SOURCE.read_text(encoding="utf-8")
        self.assertNotIn("workspace.duplicate", source)
        self.assertIn("append_seed_workspace(", source)
        self.assertIn("mark_workspace_ready(", source)
        self.assertIn("mark_workspace_failed(", source)

    def test_seed_identity_and_absolute_path_contract_are_declared(self) -> None:
        """Align Tool ID·Seed ID·Schema와 절대 경로 계약을 고정합니다."""
        source = _CONSTANTS_SOURCE.read_text(encoding="utf-8")
        self.assertIn('WORKSPACE_TOOL_ID = "align"', source)
        self.assertIn('WORKSPACE_SEED_NAME = "__COLMAP_ALIGN_SEED_5_2__"', source)
        self.assertIn("WORKSPACE_SCHEMA_VERSION = 2", source)
        self.assertIn("WORKSPACE_SEED_SCHEMA_VERSION = 1", source)
        self.assertIn('Path(__file__).resolve().parent', source)
        self.assertIn('colmap_align_seed_5_2.blend', source)

    def test_align_filter_policy_requires_owned_workspace_and_screen(self) -> None:
        """모든 Object 필터를 끄기 전 Workspace와 Screen UUID 검사를 강제합니다."""
        source = _function_source(_WORKSPACE_SOURCE, "hide_scene_objects_in_align_viewports")
        filter_write = source.index("setattr(space, filter_name, False)")
        self.assertLess(source.index("workspace_matches_spec("), filter_write)
        self.assertLess(source.index("screen_matches_workspace("), filter_write)

    def test_legacy_boolean_alone_never_matches_current_spec(self) -> None:
        """구버전 Boolean만 가진 Workspace를 현행 Align 공간으로 재사용하지 않습니다."""
        legacy = _FakeId(10, colmap_align_owned=True)
        spec = WorkspaceSeedSpec(
            tool_id="align",
            display_name="COLMAP Align",
            seed_workspace_name="__COLMAP_ALIGN_SEED_5_2__",
            seed_path=Path("seed.blend"),
        )
        self.assertFalse(workspace_matches_spec(legacy, spec, require_ready=False))

    def test_screen_uuid_must_match_current_workspace(self) -> None:
        """Align Screen 필터 적용의 기준인 Workspace–Screen UUID 일치를 검증합니다."""
        spec = WorkspaceSeedSpec(
            tool_id="align",
            display_name="COLMAP Align",
            seed_workspace_name="__COLMAP_ALIGN_SEED_5_2__",
            seed_path=Path("seed.blend"),
        )
        workspace = _FakeId(
            20,
            **{
                WORKSPACE_OWNER_KEY: "colmap_tools",
                WORKSPACE_TOOL_ID_KEY: "align",
                WORKSPACE_UUID_KEY: "align-uuid",
                WORKSPACE_SCHEMA_KEY: 2,
                WORKSPACE_SEED_SCHEMA_KEY: 1,
                WORKSPACE_STATE_KEY: STATE_READY,
            },
        )
        owned_screen = _FakeId(21, **{SCREEN_OWNER_KEY: "align-uuid"})
        foreign_screen = _FakeId(22, **{SCREEN_OWNER_KEY: "modifier-uuid"})
        workspace.screens = (owned_screen, foreign_screen)
        self.assertTrue(workspace_matches_spec(workspace, spec))
        self.assertTrue(screen_matches_workspace(owned_screen, workspace))
        self.assertFalse(screen_matches_workspace(foreign_screen, workspace))

    def test_legacy_workspace_in_other_window_has_explicit_safe_failure(self) -> None:
        """다른 Window이 사용 중인 Legacy Workspace를 변경하지 않는 진단을 고정합니다."""
        source = _function_source(_WORKSPACE_SOURCE, "_delete_legacy_workspace")
        self.assertIn("_workspace_is_used_by_other_window", source)
        self.assertIn("다른 Blender Window가 사용 중입니다", source)
        self.assertLess(source.index("_workspace_is_used_by_other_window"), source.index("bpy.ops.workspace.delete"))


if __name__ == "__main__":
    unittest.main()
