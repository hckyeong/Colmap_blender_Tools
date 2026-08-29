"""코드 설명과 계층 분리 규칙을 정적 검사합니다."""

from __future__ import annotations

import ast
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2] / "tools" / "projection_paint"


def _python_files():
    """빌드 산출물을 제외한 Python 소스 경로를 반환합니다."""
    return tuple(
        path
        for path in ROOT.rglob("*.py")
        if "__pycache__" not in path.parts and "build" not in path.parts
    )


class ArchitectureTests(unittest.TestCase):
    """코드 설명과 Core 계층 분리를 검사합니다."""

    def test_every_function_has_one_line_docstring(self):
        """모든 함수와 메서드가 한 줄 기능 설명을 갖는지 검사합니다."""
        missing = []
        multiline = []
        for path in _python_files():
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                docstring = ast.get_docstring(node, clean=False)
                label = f"{path.relative_to(ROOT)}:{node.lineno}:{node.name}"
                if not docstring:
                    missing.append(label)
                elif "\n" in docstring.strip():
                    multiline.append(label)
        self.assertFalse(missing, f"기능 설명이 없는 함수: {missing}")
        self.assertFalse(multiline, f"한 줄이 아닌 함수 설명: {multiline}")

    def test_core_does_not_import_bpy(self):
        """순수 Core 계층이 Blender Python API를 가져오지 않는지 검사합니다."""
        offenders = []
        for path in (ROOT / "core").rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import) and any(
                    alias.name == "bpy" for alias in node.names
                ):
                    offenders.append(str(path.relative_to(ROOT)))
                if isinstance(node, ast.ImportFrom) and node.module == "bpy":
                    offenders.append(str(path.relative_to(ROOT)))
        self.assertFalse(offenders, f"Core의 bpy 의존: {sorted(set(offenders))}")

    def test_paint_authority_has_no_active_session_globals(self):
        """Paint·Stencil·Runtime 모듈에 전역 활성 권위 객체가 없는지 검사합니다."""
        forbidden = (
            "_ACTIVE_SESSION",
            "_ACTIVE_STENCIL",
            "_ACTIVE_RUNTIME",
            "abandon_paint_session",
            "복구할 Stencil Session이 없습니다",
        )
        offenders = []
        for name in ("paint_session.py", "stencil_session.py", "paint_runtime.py"):
            path = ROOT / "blender" / name
            source = path.read_text(encoding="utf-8")
            offenders.extend(
                f"{name}:{token}" for token in forbidden if token in source
            )
        self.assertFalse(offenders, f"Python 권위 세션이 남아 있습니다: {offenders}")


if __name__ == "__main__":
    unittest.main()
