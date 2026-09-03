"""Blender 없이 Modifier 입력 Guard의 정적 계약을 검증합니다."""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace
import unittest


ROOT = Path(__file__).resolve().parents[3]
GUARD_PATH = ROOT / "tools" / "modifier" / "blender" / "modifier_input_guard.py"
SOURCE = GUARD_PATH.read_text(encoding="utf-8")
TREE = ast.parse(SOURCE)


def _attribute_name(node: ast.AST) -> str:
    """중첩 Attribute 호출을 점으로 연결한 이름으로 변환합니다."""

    parts: list[str] = []
    current = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        parts.append(current.id)
    return ".".join(reversed(parts))


def _function_source(name: str) -> str:
    """지정한 최상위 함수의 원본 Source 구간을 반환합니다."""

    node = next(
        candidate
        for candidate in TREE.body
        if isinstance(candidate, (ast.FunctionDef, ast.AsyncFunctionDef))
        and candidate.name == name
    )
    return ast.get_source_segment(SOURCE, node) or ""


class ModifierInputGuardStaticTests(unittest.TestCase):
    """입력 Guard가 Modifier 경계와 소비 계약을 지키는지 검사합니다."""

    def test_never_calls_blender_global_undo_or_history(self):
        """제품 Guard는 Blender Global Undo 계열 Operator를 호출하지 않아야 합니다."""

        called = {
            _attribute_name(node.func)
            for node in ast.walk(TREE)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        self.assertFalse(
            called
            & {
                "bpy.ops.ed.undo",
                "bpy.ops.ed.redo",
                "bpy.ops.ed.undo_history",
            }
        )

    def test_does_not_import_align_or_share_align_runtime(self):
        """Modifier Guard는 Align 패키지나 Runtime을 참조하지 않아야 합니다."""

        imported_modules: list[str] = []
        for node in ast.walk(TREE):
            if isinstance(node, ast.Import):
                imported_modules.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported_modules.append(node.module or "")
        self.assertFalse(any("align" in name.lower() for name in imported_modules))
        self.assertNotIn("tools.align", SOURCE)

    def test_routes_exact_ctrl_z_shift_z_y_and_alt_z_paths(self):
        """PRESS Router에는 네 Modifier 단축키와 엄격한 보조키 분기가 있어야 합니다."""

        router = _function_source("route_modifier_input")
        for token in (
            '"PRESS"',
            'event_type == "Z" and alt and not shift',
            'event_type == "Z" and shift and not alt',
            'event_type == "Y" and not shift and not alt',
            'event_type == "Z" and not shift and not alt',
            'getattr(event, "hyper", False)',
            "ACTION_BLOCK_UNDO_HISTORY",
            "ACTION_UNDO",
            "ACTION_REDO",
        ):
            self.assertIn(token, router)
        self.assertIn('getattr(event, "is_repeat", False)', SOURCE)

    def test_router_behavior_is_strict_without_blender(self):
        """분류 함수만 분리 실행해 PRESS와 보조키 조합을 정확히 확인합니다."""

        route_node = next(
            node
            for node in TREE.body
            if isinstance(node, ast.FunctionDef) and node.name == "route_modifier_input"
        )
        namespace = {
            "Any": object,
            "ACTION_UNDO": "UNDO",
            "ACTION_REDO": "REDO",
            "ACTION_BLOCK_UNDO_HISTORY": "BLOCK_UNDO_HISTORY",
        }
        isolated_tree = ast.Module(body=[route_node], type_ignores=[])
        ast.fix_missing_locations(isolated_tree)
        exec(compile(isolated_tree, str(GUARD_PATH), "exec"), namespace)
        route = namespace["route_modifier_input"]

        def event(event_type: str, **modifiers: bool) -> SimpleNamespace:
            """Router 검증에 필요한 최소 Blender Event 대역을 만듭니다."""

            values = {
                "type": event_type,
                "value": "PRESS",
                "ctrl": True,
                "shift": False,
                "alt": False,
                "oskey": False,
                "hyper": False,
            }
            values.update(modifiers)
            return SimpleNamespace(**values)

        self.assertEqual(route(event("Z")), "UNDO")
        self.assertEqual(route(event("Z", shift=True)), "REDO")
        self.assertEqual(route(event("Y")), "REDO")
        self.assertEqual(route(event("Z", alt=True)), "BLOCK_UNDO_HISTORY")
        self.assertIsNone(route(event("Z", value="RELEASE")))
        self.assertIsNone(route(event("Z", ctrl=False)))
        self.assertIsNone(route(event("Z", shift=True, alt=True)))
        self.assertIsNone(route(event("Z", oskey=True)))
        self.assertIsNone(route(event("Z", hyper=True)))

    def test_empty_and_locked_shortcuts_are_consumed(self):
        """빈 Journal과 작업 잠금도 전역 입력으로 넘기지 않아야 합니다."""

        event_router = _function_source("route_modifier_input_event")
        self.assertIn("_modifier_input_lock_reason", event_router)
        self.assertGreaterEqual(event_router.count('{"RUNNING_MODAL"}'), 3)
        self.assertIn("실행 취소할 Modifier 편집 기록이 없습니다.", SOURCE)
        self.assertIn("다시 실행할 Modifier 편집 기록이 없습니다.", SOURCE)
        self.assertIn("Import 진행 중", SOURCE)
        self.assertIn("Export 진행 중", SOURCE)
        self.assertIn("Runtime 복원 중", SOURCE)
        self.assertIn("편집 Transaction", SOURCE)

    def test_calls_only_modifier_undo_and_redo_operators(self):
        """실행 경로는 Modifier 전용 Undo·Redo Operator에 연결되어야 합니다."""

        self.assertIn('bpy.ops.colmap.undo_last_edit("EXEC_DEFAULT")', SOURCE)
        self.assertIn('bpy.ops.colmap.redo_last_edit("EXEC_DEFAULT")', SOURCE)

    def test_exposes_start_stop_and_rearm_lifecycle(self):
        """Lifecycle 통합에 필요한 시작·정지·재연결 함수를 공개해야 합니다."""

        function_names = {
            node.name
            for node in TREE.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        self.assertTrue(
            {
                "start_modifier_input_guard",
                "stop_modifier_input_guard",
                "rearm_modifier_input_guard",
            }.issubset(function_names)
        )
        self.assertIn("_GUARD_TIMER", SOURCE)
        self.assertIn("_GUARD_WINDOW_POINTER", SOURCE)

    def test_all_functions_have_korean_documentation(self):
        """새 함수와 Method에는 기능을 설명하는 한국어 문서화 문자열이 있어야 합니다."""

        functions = [
            node
            for node in ast.walk(TREE)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]
        for function in functions:
            docstring = ast.get_docstring(function) or ""
            self.assertTrue(docstring, function.name)
            self.assertRegex(docstring, "[가-힣]", function.name)


if __name__ == "__main__":
    unittest.main()
