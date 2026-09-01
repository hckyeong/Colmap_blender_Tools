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

    def test_projection_dataset_adapter_depends_on_shared_only(self):
        """PP Dataset Adapter가 Modifier를 import하지 않고 공용 Registry만 소비하는지 검사합니다."""

        path = ROOT / "blender" / "dataset_adapter.py"
        source = path.read_text(encoding="utf-8")
        self.assertIn("shared.colmap_dataset.blender.registry", source)
        self.assertNotIn("tools.modifier", source)
        self.assertNotIn("..modifier", source)

    def test_projection_import_operator_is_not_registered_or_drawn(self):
        """신규 PP 수명주기와 Panel에 별도 COLMAP Import 진입점이 없는지 검사합니다."""

        lifecycle = (ROOT / "blender" / "lifecycle.py").read_text(encoding="utf-8")
        panels = (ROOT / "blender" / "panels.py").read_text(encoding="utf-8")
        self.assertNotIn('".camera_import"', lifecycle)
        self.assertNotIn("projection_paint.import_colmap", panels)
        self.assertNotIn('prop(settings, "dataset_root")', panels)
        self.assertNotIn('prop(settings, "input_format")', panels)

    def test_composite_direct_edit_has_no_automatic_migration(self):
        """Composite 직접 편집 경로에 자동 이전 식별자나 실행 차단 상태가 없는지 검사합니다."""

        forbidden = (
            "ensure_composite_direct_edit_schema",
            "_migrate_all_direct_edit_schemas",
            "composite_direct_edit_schema_version",
            "composite_saved_clone_image",
            "composite_saved_use_clone_layer",
            "composite_saved_paint_clone_slot",
            "COMPOSITE_DERIVED",
            "composite_dependency_session_uuid",
        )
        offenders = []
        for path in _python_files():
            source = path.read_text(encoding="utf-8")
            offenders.extend(
                f"{path.relative_to(ROOT)}:{token}"
                for token in forbidden
                if token in source
            )
        self.assertFalse(offenders, f"자동 Composite 이전 코드가 남아 있습니다: {offenders}")

    def test_native_clone_rna_is_not_managed(self):
        """제품 코드가 Blender Native Clone Source·Offset·Slot을 읽거나 쓰지 않는지 검사합니다."""

        forbidden_attributes = {"clone_image", "clone_offset", "paint_clone_slot"}
        offenders = []
        for path in _python_files():
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Attribute) and node.attr in forbidden_attributes:
                    offenders.append(
                        f"{path.relative_to(ROOT)}:{node.lineno}:{node.attr}"
                    )
        self.assertFalse(offenders, f"금지된 Clone RNA 접근: {offenders}")

    def test_clone_layer_mode_has_one_false_edge_write(self):
        """Clone Tool Edge 함수에 use_clone_layer=False 쓰기 한 곳만 있는지 검사합니다."""

        accesses = []
        valid_writes = []
        for path in _python_files():
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Assign):
                    continue
                for target in node.targets:
                    if isinstance(target, ast.Attribute) and target.attr == "use_clone_layer":
                        accesses.append((path, node, target))
                        if (
                            path.name == "paint_tool_bridge.py"
                            and isinstance(node.value, ast.Constant)
                            and node.value.value is False
                        ):
                            valid_writes.append((path, node, target))
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Attribute)
                    and node.attr == "use_clone_layer"
                    and isinstance(node.ctx, ast.Load)
                ):
                    accesses.append((path, node, node))
        self.assertEqual(len(accesses), 1, f"use_clone_layer 접근 수가 다릅니다: {accesses}")
        self.assertEqual(len(valid_writes), 1)

    def test_tool_bridge_is_the_only_brush_asset_activation_site(self):
        """Brush 자산 활성화가 공용 Bridge 함수 한 곳에만 있는지 검사합니다."""

        offenders = []
        activations = []
        for path in _python_files():
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

            class ActivationVisitor(ast.NodeVisitor):
                """현재 함수 이름과 Brush asset_activate 호출 위치를 함께 수집합니다."""

                def __init__(self):
                    """빈 함수 Stack으로 정적 탐색기를 초기화합니다."""

                    self.functions = []

                def visit_FunctionDef(self, node):
                    """함수 범위에 진입해 내부 호출을 탐색합니다."""

                    self.functions.append(node.name)
                    self.generic_visit(node)
                    self.functions.pop()

                def visit_Call(self, node):
                    """brush.asset_activate 호출의 파일·함수·줄을 기록합니다."""

                    function = node.func
                    if (
                        isinstance(function, ast.Attribute)
                        and function.attr == "asset_activate"
                    ):
                        activations.append(
                            (path.name, self.functions[-1] if self.functions else "", node.lineno)
                        )
                    self.generic_visit(node)

            ActivationVisitor().visit(tree)
        offenders.extend(
            activation
            for activation in activations
            if activation[:2] != ("paint_tool_bridge.py", "activate_brush_asset")
        )
        self.assertEqual(len(activations), 1, f"Brush 자산 활성화 위치: {activations}")
        self.assertFalse(offenders, f"Tool Edge 밖 Brush 자산 활성화: {offenders}")

    def test_removed_native_compatibility_identifiers_are_absent(self):
        """삭제한 Clone·Brush 강제 동기화·Stencil 이전 식별자가 제품에 없는지 검사합니다."""

        forbidden = (
            "configure_composite_clone_source",
            "_CloneSnapshot",
            "_capture_clone_snapshot",
            "paint_tool_brush_token",
            "paint_tool_sync",
            "_track_runtime_paint_tool",
            "_paint_tool_tracking_tick",
            "start_paint_tool_tracking",
            "paint_tool_brush_tracking",
            "STENCIL_BINDING_SCHEMA_VERSION",
            "migrate_stencil_binding_state",
            "stencil_follow_active_brush",
            "stencil_binding_schema_version",
            "STENCIL_PERSISTENCE_SCHEMA_VERSION",
            "capture_stencil_persistence",
            "arm_stencil_persistence_restore",
            "restore_stencil_persistence",
            "persisted_stencil_",
            "stencil_persistence_restore_pending",
            "ensure_editable_brush",
            "restore_original_brush",
        )
        offenders = []
        for path in _python_files():
            source = path.read_text(encoding="utf-8")
            offenders.extend(
                f"{path.relative_to(ROOT)}:{token}"
                for token in forbidden
                if token in source
            )
            self.assertNotIn("wm.tool_set_by_id", source, str(path.relative_to(ROOT)))
        self.assertFalse(offenders, f"삭제 식별자가 남아 있습니다: {offenders}")


if __name__ == "__main__":
    unittest.main()
