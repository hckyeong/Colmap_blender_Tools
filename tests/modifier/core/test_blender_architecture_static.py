from __future__ import annotations

import ast
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
PACKAGE_ROOT = REPOSITORY_ROOT / "tools" / "modifier"
BLENDER_ROOT = PACKAGE_ROOT / "blender"


def _relative_dependencies(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    dependencies: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom) or node.level != 1:
            continue
        candidates = (
            (node.module.split(".", 1)[0],)
            if node.module
            else tuple(alias.name.split(".", 1)[0] for alias in node.names)
        )
        for dependency in candidates:
            if (BLENDER_ROOT / f"{dependency}.py").exists():
                dependencies.add(dependency)
    return dependencies


class BlenderArchitectureStaticTests(unittest.TestCase):
    def test_modifier_workspace_preserves_window_and_undo_domains(self) -> None:
        """Workspace 전환이 새 Window나 Global Undo 초기화를 사용하지 않습니다."""

        sources = {
            path.name: path.read_text(encoding="utf-8")
            for path in (
                BLENDER_ROOT / "modifier_session_constants.py",
                BLENDER_ROOT / "modifier_session.py",
                BLENDER_ROOT / "modifier_workspace.py",
                BLENDER_ROOT / "modifier_viewport.py",
                BLENDER_ROOT / "operators_modifier_workspace.py",
            )
        }
        combined = "\n".join(sources.values())
        self.assertNotIn("bpy.ops.wm.window_new", combined)
        self.assertNotIn("bpy.ops.workspace.duplicate", combined)
        self.assertNotIn("undo_history_clear", combined)
        self.assertNotIn("bpy.ops.ed.undo", combined)
        self.assertIn('WORKSPACE_NAME = "COLMAP Modifier"', combined)
        self.assertIn("modifier_panel_poll", combined)
        self.assertIn("bpy.ops.screen.area_split", combined)
        self.assertIn('timeline_area.type = "DOPESHEET_EDITOR"', combined)
        self.assertIn('timeline_area.ui_type = "TIMELINE"', combined)
        self.assertIn("DEFAULT_STARTUP_OBJECTS", combined)
        self.assertIn('MODIFIER_VIEW_PROJECTION = "PERSP"', combined)
        self.assertIn("bpy.ops.view3d.view_selected", combined)

    def test_modifier_workspace_uses_independent_seed_append_contract(self) -> None:
        """Modifier Workspace가 고정 Seed와 공통 생명주기 API만 사용합니다."""

        source = "\n".join(
            (BLENDER_ROOT / name).read_text(encoding="utf-8")
            for name in ("modifier_workspace.py", "modifier_workspace_contract.py")
        )
        self.assertIn("append_seed_workspace(", source)
        self.assertIn("workspace_matches_spec(", source)
        self.assertIn("screen_matches_workspace(", source)
        self.assertIn("mark_workspace_ready(", source)
        self.assertIn("mark_workspace_failed(", source)
        self.assertIn("delete_owned_workspace(", source)
        self.assertIn('tool_id="modifier"', source)
        self.assertIn('seed_workspace_name="__COLMAP_MODIFIER_SEED_5_2__"', source)
        self.assertIn('workspace_schema=2', source)
        self.assertIn('seed_schema=1', source)
        seed_path = (
            PACKAGE_ROOT
            / "resources"
            / "workspaces"
            / "colmap_modifier_seed_5_2.blend"
        )
        self.assertTrue(seed_path.is_file() and seed_path.stat().st_size > 0)

    def test_modifier_viewport_normalizes_filters_before_activation(self) -> None:
        """모든 오브젝트 필터와 Local View 정규화가 ACTIVE 전 완료됩니다."""

        viewport_source = (BLENDER_ROOT / "modifier_viewport.py").read_text(
            encoding="utf-8"
        )
        workspace_source = (BLENDER_ROOT / "modifier_workspace.py").read_text(
            encoding="utf-8"
        )
        self.assertIn('startswith("show_object_viewport_")', viewport_source)
        self.assertIn("setattr(space, filter_name, True)", viewport_source)
        self.assertIn("bpy.ops.view3d.localview", viewport_source)
        normalize_position = workspace_source.index("normalize_modifier_viewport(window, area)")
        ready_position = workspace_source.index("mark_workspace_ready(workspace, window.screen")
        active_position = workspace_source.index("activate_session(window, area)")
        self.assertLess(normalize_position, ready_position)
        self.assertLess(ready_position, active_position)

    def test_modifier_legacy_workspace_has_safe_replacement_guard(self) -> None:
        """Legacy Workspace는 현행 소유로 재사용되지 않고 다중 창 사용 시 중단됩니다."""

        session_source = (BLENDER_ROOT / "modifier_session.py").read_text(
            encoding="utf-8"
        )
        workspace_source = (BLENDER_ROOT / "modifier_workspace.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("is_legacy_modifier_workspace", session_source)
        self.assertIn("not is_owned_modifier_workspace(workspace)", session_source)
        self.assertIn("legacy_workspaces", workspace_source)
        self.assertIn("stale_owned", workspace_source)
        self.assertIn("미완료 COLMAP Modifier Workspace를 다른 Blender Window가 사용 중", workspace_source)
        self.assertIn("delete_owned_workspace(", workspace_source)
        self.assertIn("다른 Blender Window가 사용 중이므로 교체할 수 없습니다", workspace_source)
        self.assertLess(
            workspace_source.index("if other_window is not None:"),
            workspace_source.index("bpy.ops.workspace.delete()"),
        )

    def test_modifier_public_name_keeps_persistent_internal_ids(self) -> None:
        """공개 명칭 변경이 저장·자동화 호환 식별자를 바꾸지 않습니다."""

        lifecycle = (BLENDER_ROOT / "lifecycle.py").read_text(encoding="utf-8")
        manifest = (REPOSITORY_ROOT / "blender_manifest.toml").read_text(
            encoding="utf-8"
        )
        self.assertIn("bpy.types.Scene.colmap_import", lifecycle)
        self.assertNotIn("bpy.types.Scene.colmap_modifier", lifecycle)
        self.assertIn('id = "colmap_tools"', manifest)
        self.assertIn('name = "COLMAP Tools"', manifest)

    def test_projection_paint_product_modules_are_removed(self) -> None:
        """분리 예정인 Projection Paint 구현이 Reconstruction 패키지에 남지 않습니다."""

        forbidden_files = {
            "operators_projection_layer_progress.py",
            "operators_projection_layers.py",
            "operators_projection_paint.py",
            "projection_brush.py",
            "projection_layer_bake_job.py",
            "projection_layer_flatten.py",
            "projection_layer_history.py",
            "projection_layer_images.py",
            "projection_layer_material.py",
            "projection_layer_paint_target.py",
            "projection_layer_session.py",
            "projection_layer_stroke_monitor.py",
            "projection_paint_session.py",
            "projection_pixel_history.py",
            "projection_undo_router.py",
            "texture_paint_navigation.py",
            "ui_projection_layers.py",
        }
        self.assertFalse(
            forbidden_files & {path.name for path in BLENDER_ROOT.glob("*.py")},
            "Projection Paint 제품 모듈이 Blender 패키지에 남아 있습니다.",
        )
        forbidden_tokens = (
            "projection_paint",
            "projection_layer",
            "projection_undo",
            "projection_brush",
            "toggle_projection_paint",
        )
        violations = []
        for path in sorted(BLENDER_ROOT.glob("*.py")):
            source = path.read_text(encoding="utf-8")
            if any(token in source for token in forbidden_tokens):
                violations.append(path.name)
        self.assertEqual([], violations)
        core_root = PACKAGE_ROOT / "core"
        self.assertFalse(tuple((core_root / "layers").glob("*.py")))
        self.assertFalse((core_root / "projection_pixel_history.py").exists())

    def test_timeline_frame_handler_only_uses_cached_o1_state(self) -> None:
        path = BLENDER_ROOT / "camera_review_timeline.py"
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        functions = {
            node.name: node
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        handler = functions["frame_change_post"]
        handler_names = {
            node.id for node in ast.walk(handler) if isinstance(node, ast.Name)
        }
        forbidden = {
            "ensure_runtime",
            "managed_camera_map",
            "update_working_set",
            "_load_image_datablock",
            "load_reconstruction",
            "source_fingerprint",
        }
        self.assertFalse(
            handler_names & forbidden,
            "frame handler가 Runtime 복원, 전체 scan, parse 또는 이미지 로드를 호출합니다.",
        )
        self.assertFalse(
            any(
                isinstance(node, (ast.For, ast.While, ast.ListComp, ast.SetComp))
                for node in ast.walk(handler)
            ),
            "frame handler에 전체 항목 순회가 있습니다.",
        )
        timer = functions["process_pending_timeline_updates"]
        timer_source = ast.get_source_segment(
            path.read_text(encoding="utf-8"),
            timer,
        )
        target_apply = functions["_apply_timeline_target"]
        target_apply_source = ast.get_source_segment(
            path.read_text(encoding="utf-8"),
            target_apply,
        )
        self.assertIn("_apply_timeline_target", timer_source)
        self.assertIn("update_working_set", target_apply_source)

    def test_service_modules_do_not_import_operator_modules(self) -> None:
        allowed = {
            "lifecycle.py",
            "operators_camera_edit_mode.py",
            "operators_point_edit_mode.py",
            "operators_point_selection.py",
        }
        violations: list[str] = []
        for path in sorted(BLENDER_ROOT.glob("*.py")):
            if path.name in allowed:
                continue
            source = path.read_text(encoding="utf-8")
            if ".operators_" in source:
                violations.append(path.name)
        self.assertEqual([], violations)

    def test_product_module_import_graph_has_no_cycle(self) -> None:
        graph = {
            path.stem: _relative_dependencies(path)
            for path in sorted(BLENDER_ROOT.glob("*.py"))
        }
        visiting: list[str] = []
        visited: set[str] = set()

        def visit(module: str) -> None:
            if module in visiting:
                cycle_start = visiting.index(module)
                cycle = " -> ".join((*visiting[cycle_start:], module))
                self.fail(f"Blender 제품 모듈 import 순환이 있습니다: {cycle}")
            if module in visited:
                return
            visiting.append(module)
            for dependency in sorted(graph[module]):
                visit(dependency)
            visiting.pop()
            visited.add(module)

        for module in sorted(graph):
            visit(module)


if __name__ == "__main__":
    unittest.main()
