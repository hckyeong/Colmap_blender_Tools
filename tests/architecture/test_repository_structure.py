"""통합 Repository의 패키지 경계와 배포 구조를 정적으로 검증합니다."""

from __future__ import annotations

import ast
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]
TOOL_NAMES = ("align", "modifier", "projection_paint")


def _python_files(base: Path) -> tuple[Path, ...]:
    """지정한 경계 아래의 Python Source를 결정적 순서로 반환합니다."""

    return tuple(sorted(base.rglob("*.py")))


def _syntax_tree(path: Path) -> ast.Module:
    """UTF-8 Python Source를 읽고 정적 구문 트리로 변환합니다."""

    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


class RepositoryStructureTests(unittest.TestCase):
    """독립 도구 세 개와 단일 Root Shell의 구조 계약을 검증합니다."""

    def test_required_package_boundaries_exist(self) -> None:
        """계획서에 정의한 Root, Shell, Tool, Shared 경계가 모두 존재해야 합니다."""

        required_paths = (
            ROOT / "__init__.py",
            ROOT / "blender_manifest.toml",
            ROOT / "shell" / "lifecycle.py",
            ROOT / "shell" / "menu.py",
            ROOT / "shared" / "contracts" / "__init__.py",
            ROOT / "shared" / "blender_runtime" / "__init__.py",
            ROOT / "shared" / "colmap_dataset" / "contracts.py",
            ROOT / "shared" / "colmap_dataset" / "blender" / "registry.py",
            *(ROOT / "tools" / name / "__init__.py" for name in TOOL_NAMES),
        )
        missing = [str(path.relative_to(ROOT)) for path in required_paths if not path.is_file()]
        self.assertEqual([], missing)

    def test_production_python_has_valid_syntax(self) -> None:
        """Shell, Shared와 세 도구의 모든 Python Source가 구문 분석되어야 합니다."""

        for package_dir in (ROOT / "shell", ROOT / "shared", ROOT / "tools"):
            for path in _python_files(package_dir):
                with self.subTest(path=path.relative_to(ROOT)):
                    _syntax_tree(path)

    def test_tools_do_not_import_each_other(self) -> None:
        """각 도구는 다른 도구 패키지를 직접 import하지 않아야 합니다."""

        violations: list[str] = []
        for tool_name in TOOL_NAMES:
            forbidden = {name for name in TOOL_NAMES if name != tool_name}
            for path in _python_files(ROOT / "tools" / tool_name):
                for node in ast.walk(_syntax_tree(path)):
                    imported_names: tuple[str, ...] = ()
                    if isinstance(node, ast.Import):
                        imported_names = tuple(alias.name for alias in node.names)
                    elif isinstance(node, ast.ImportFrom) and node.module:
                        imported_names = (node.module,)
                    for imported_name in imported_names:
                        if any(
                            imported_name == f"tools.{other}"
                            or imported_name.startswith(f"tools.{other}.")
                            for other in forbidden
                        ):
                            violations.append(
                                f"{path.relative_to(ROOT)}:{node.lineno} -> {imported_name}"
                            )
        self.assertEqual([], violations)

    def test_shared_colmap_dataset_does_not_import_tool_packages(self) -> None:
        """공용 Dataset 계층은 Modifier나 Projection Painting Runtime에 의존하지 않습니다."""

        violations: list[str] = []
        for path in _python_files(ROOT / "shared" / "colmap_dataset"):
            for node in ast.walk(_syntax_tree(path)):
                imported_names: tuple[str, ...] = ()
                if isinstance(node, ast.Import):
                    imported_names = tuple(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imported_names = (node.module,)
                for imported_name in imported_names:
                    if imported_name == "tools" or imported_name.startswith("tools."):
                        violations.append(
                            f"{path.relative_to(ROOT)}:{node.lineno} -> {imported_name}"
                        )
        self.assertEqual([], violations)

    def test_core_packages_do_not_import_bpy(self) -> None:
        """명시적인 순수 Core 패키지는 Blender Python API를 import하지 않아야 합니다."""

        violations: list[str] = []
        core_directories = (
            ROOT / "tools" / "modifier" / "core",
            ROOT / "tools" / "projection_paint" / "core",
        )
        for core_directory in core_directories:
            for path in _python_files(core_directory):
                for node in ast.walk(_syntax_tree(path)):
                    if isinstance(node, ast.Import) and any(
                        alias.name == "bpy" or alias.name.startswith("bpy.")
                        for alias in node.names
                    ):
                        violations.append(f"{path.relative_to(ROOT)}:{node.lineno}")
                    if isinstance(node, ast.ImportFrom) and (
                        node.module == "bpy" or (node.module or "").startswith("bpy.")
                    ):
                        violations.append(f"{path.relative_to(ROOT)}:{node.lineno}")
        self.assertEqual([], violations)

    def test_root_shell_does_not_import_tool_implementation_layers(self) -> None:
        """Root Shell은 각 도구의 Blender·Core·알고리듬 구현을 직접 참조하지 않아야 합니다."""

        forbidden_tokens = (".blender", ".core", ".alignment", ".colmap_io")
        violations: list[str] = []
        for path in _python_files(ROOT / "shell"):
            source = path.read_text(encoding="utf-8")
            for token in forbidden_tokens:
                if token in source:
                    violations.append(f"{path.relative_to(ROOT)} -> {token}")
        self.assertEqual([], violations)

    def test_runtime_sources_do_not_hardcode_bl_ext_namespace(self) -> None:
        """설치 Repository에 따라 달라지는 bl_ext 전체 경로를 Runtime에 고정하지 않아야 합니다."""

        violations = [
            str(path.relative_to(ROOT))
            for base in (ROOT / "shell", ROOT / "shared", ROOT / "tools")
            for path in _python_files(base)
            if "bl_ext." in path.read_text(encoding="utf-8")
        ]
        self.assertEqual([], violations)

    def test_root_shell_is_the_only_common_menu_owner(self) -> None:
        """공통 메뉴 Class는 Root Shell에서 정확히 한 번만 선언되어야 합니다."""

        owners: list[str] = []
        for package_dir in (ROOT / "shell", ROOT / "tools"):
            for path in _python_files(package_dir):
                for node in ast.walk(_syntax_tree(path)):
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
                        if isinstance(statement.value, ast.Constant) and statement.value.value == "COLMAP_MT_tools":
                            owners.append(path.relative_to(ROOT).as_posix())
        self.assertEqual(["shell/menu.py"], owners)

    def test_common_menu_lists_each_independent_tool_once(self) -> None:
        """공통 메뉴에는 세 독립 도구 진입 Operator가 각각 한 번만 있어야 합니다."""

        source = (ROOT / "shell" / "menu.py").read_text(encoding="utf-8")
        expected_operators = (
            "colmap_align.open_workspace",
            "colmap.open_modifier_workspace",
            "projection_paint.open_workspace",
        )
        for operator_id in expected_operators:
            self.assertEqual(1, source.count(f'layout.operator("{operator_id}"'))
        for sequential_token in ("1단계", "2단계", "3단계", "→"):
            self.assertNotIn(sequential_token, source)

    def test_manifest_excludes_non_runtime_sources(self) -> None:
        """Extension ZIP에서 원본 Repository, 문서와 테스트를 명시적으로 제외해야 합니다."""

        manifest = (ROOT / "blender_manifest.toml").read_text(encoding="utf-8")
        self.assertIn("paths_exclude_pattern", manifest)
        self.assertNotIn("\npaths = [", manifest)
        for excluded in (
            '"/.artifacts/"',
            '"/.small-gicp-runtime/"',
            '"/.small-gicp-runtime-cache/"',
            '"/.small-gicp-runtime-build/"',
            '"/tests/"',
            '"/docs/"',
            '"/build_tools/"',
            '"/COLMAP_Align/"',
            '"/COLMAP_Modifier/"',
            '"/COLMAP_Projection_Paint/"',
        ):
            self.assertIn(excluded, manifest)

    def test_workspace_seeds_are_packaged_without_runtime_duplication(self) -> None:
        """세 도구 Seed가 존재하고 Production Runtime이 활성 Workspace를 복제하지 않아야 합니다."""

        seed_paths = (
            ROOT
            / "tools"
            / "align"
            / "resources"
            / "workspaces"
            / "colmap_align_seed_5_2.blend",
            ROOT
            / "tools"
            / "modifier"
            / "resources"
            / "workspaces"
            / "colmap_modifier_seed_5_2.blend",
            ROOT
            / "tools"
            / "projection_paint"
            / "resources"
            / "workspaces"
            / "projection_paint_seed_5_2.blend",
        )
        for seed_path in seed_paths:
            with self.subTest(seed=seed_path.relative_to(ROOT)):
                self.assertTrue(seed_path.is_file())
                self.assertGreater(seed_path.stat().st_size, 0)

        violations = [
            str(path.relative_to(ROOT))
            for base in (ROOT / "shell", ROOT / "shared", ROOT / "tools")
            for path in _python_files(base)
            if "bpy.ops.workspace.duplicate()" in path.read_text(encoding="utf-8")
        ]
        self.assertEqual([], violations)

    def test_alignment_runtime_is_supplied_only_by_build_staging(self) -> None:
        """기본 점 기반 런타임과 구엔진 비교 프로필은 검증된 스테이징에서만 공급합니다."""

        self.assertFalse((ROOT / "tools" / "align" / "runtime").exists())
        self.assertTrue((ROOT / "build_tools" / "prepare_cccore_runtime.ps1").is_file())
        self.assertTrue((ROOT / "build_tools" / "cccore_distribution.py").is_file())
        self.assertTrue((ROOT / "build_tools" / "point_kd_distribution.py").is_file())
        self.assertTrue((ROOT / "build_tools" / "prepare_small_gicp_runtime.ps1").is_file())
        requirements = (
            ROOT / "build_tools" / "small_gicp_backend_requirements.txt"
        ).read_text(encoding="utf-8")
        self.assertEqual("numpy==1.26.4\nsmall-gicp==1.0.1\n", requirements)

        build_script = (ROOT / "build_tools" / "build_extension.ps1").read_text(
            encoding="utf-8"
        )
        for contract in (
            'AlignProfile = "PointKd"',
            "Add-PointKdEmbeddedRuntime",
            "tools/align/runtime/point_kd/colmap_point_kd_worker.exe",
            "Add-CcCoreEmbeddedRuntime",
            "tools/align/runtime/cccore/colmap_cccore_worker.exe",
            "Resolve-SmallGicpRuntimeSource",
            "Add-SmallGicpEmbeddedRuntime",
            "Test-SmallGicpRuntime",
            "synthetic_gicp_smoke.passed",
            "tools/align/runtime/python/python.exe",
        ):
            self.assertIn(contract, build_script)


if __name__ == "__main__":
    unittest.main()
