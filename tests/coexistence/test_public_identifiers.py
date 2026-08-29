"""세 독립 도구가 공개하는 Blender 식별자의 공존 계약을 검증합니다."""

from __future__ import annotations

import ast
from collections import defaultdict
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]


def _declared_bl_idnames() -> dict[str, list[str]]:
    """Shell과 Tool Class가 문자열로 선언한 Blender 식별자를 수집합니다."""

    declarations: dict[str, list[str]] = defaultdict(list)
    for base in (ROOT / "shell", ROOT / "tools"):
        for path in sorted(base.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.ClassDef):
                    continue
                for statement in node.body:
                    if not isinstance(statement, ast.Assign):
                        continue
                    if not any(
                        isinstance(target, ast.Name) and target.id == "bl_idname"
                        for target in statement.targets
                    ):
                        continue
                    if isinstance(statement.value, ast.Constant) and isinstance(statement.value.value, str):
                        declarations[statement.value.value].append(
                            f"{path.relative_to(ROOT)}:{statement.lineno}"
                        )
    return declarations


class PublicIdentifierTests(unittest.TestCase):
    """등록 시 충돌 가능한 공개 식별자와 Scene 속성을 검사합니다."""

    def test_blender_class_identifiers_are_unique(self) -> None:
        """문자열로 선언한 모든 bl_idname은 Repository에서 유일해야 합니다."""

        duplicates = {
            identifier: locations
            for identifier, locations in _declared_bl_idnames().items()
            if len(locations) > 1
        }
        self.assertEqual({}, duplicates)

    def test_scene_property_names_remain_tool_specific(self) -> None:
        """기존 파일 호환을 위한 세 Scene 속성명이 서로 달라야 합니다."""

        expected = {
            "align": "colmap_align_settings",
            "modifier": "colmap_import",
            "projection_paint": "projection_paint",
        }
        sources = {
            "align": ROOT / "tools" / "align" / "registration.py",
            "modifier": ROOT / "tools" / "modifier" / "blender" / "lifecycle.py",
            "projection_paint": ROOT / "tools" / "projection_paint" / "blender" / "lifecycle.py",
        }
        self.assertEqual(len(expected), len(set(expected.values())))
        for tool_name, property_name in expected.items():
            with self.subTest(tool=tool_name):
                self.assertIn(property_name, sources[tool_name].read_text(encoding="utf-8"))

    def test_owner_keys_and_tokens_are_unique_across_tools(self) -> None:
        """Blender 데이터 소유권 식별 문자열은 다른 도구와 충돌하지 않아야 합니다."""

        owners: dict[str, list[str]] = defaultdict(list)
        for tool_name in ("align", "modifier", "projection_paint"):
            for path in sorted((ROOT / "tools" / tool_name).rglob("*.py")):
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
                for node in tree.body:
                    if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Constant):
                        continue
                    if not isinstance(node.value.value, str):
                        continue
                    names = [target.id for target in node.targets if isinstance(target, ast.Name)]
                    if any(name.endswith(("OWNER_KEY", "OWNER_TOKEN")) for name in names):
                        owners[node.value.value].append(tool_name)
        cross_tool_duplicates = {
            value: tool_names
            for value, tool_names in owners.items()
            if len(set(tool_names)) > 1
        }
        self.assertEqual({}, cross_tool_duplicates)


if __name__ == "__main__":
    unittest.main()
