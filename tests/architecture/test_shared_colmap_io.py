"""공통 COLMAP 구현과 기존 경로의 호환성 및 순수 경계를 검증합니다."""

import ast
from dataclasses import fields, is_dataclass, replace
from importlib import import_module
from pathlib import Path
import runpy
import subprocess
import sys
import tempfile
from types import ModuleType
import unittest
from unittest.mock import patch

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


class SharedColmapIOTests(unittest.TestCase):
    """파서 이동으로 공개 계약이나 기존 패치 지점이 바뀌지 않음을 확인합니다."""

    def test_shared_import_has_no_tool_or_blender_dependency(self):
        """새 프로세스에서 공통 계층만 가져오고 금지 의존성을 검사합니다."""
        result = subprocess.run([sys.executable, '-X', 'utf8', '-c',
            "import sys; sys.path.insert(0, sys.argv[1]); import shared.colmap_io; assert not any(n == 'tools' or n.startswith('tools.') or n in ('bpy', 'gpu') for n in sys.modules)", str(ROOT)],
            cwd=ROOT, capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(result.returncode, 0, result.stderr)
        for path in (ROOT / 'shared' / 'colmap_io').glob('*.py'):
            for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'))):
                names = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ''] if isinstance(node, ast.ImportFrom) else []
                self.assertFalse(any(n.split('.')[0] in {'bpy', 'gpu', 'tools', 'mathutils'} for n in names), str(path))
                if isinstance(node, ast.ImportFrom):
                    self.assertLessEqual(node.level, 1, str(path))

    def test_old_paths_preserve_objects_and_private_patch_targets(self):
        """재노출 심벌과 하위 모듈의 객체 동일성 및 기존 패치 전파를 검증합니다."""
        old = import_module('tools.align.colmap_io')
        shared = import_module('shared.colmap_io')
        for name in shared.__all__:
            self.assertIs(getattr(old, name), getattr(shared, name), name)
        for name in ('binary_reader', 'camera_models', 'discovery', 'errors', 'fingerprint', 'incremental_reader', 'text_reader', 'types', 'validation'):
            self.assertIs(import_module('tools.align.colmap_io.' + name), import_module('shared.colmap_io.' + name))
        original = shared.validate_model
        with patch('tools.align.colmap_io.validate_model') as validation:
            self.assertIs(shared.validate_model, validation)
        self.assertIs(shared.validate_model, original)
        for name in ('binary_writer', 'text_writer', 'projection'):
            self.assertTrue(import_module('tools.align.colmap_io.' + name).__name__.startswith('tools.align.'))

    def test_installed_package_namespace_keeps_identity(self):
        """설치 패키지 접두어 아래에서도 공통 객체와 기록기 경로를 보존합니다."""
        namespace = '_colmap_io_install_test'
        package = ModuleType(namespace)
        package.__path__ = [str(ROOT)]
        with patch.dict(sys.modules, {namespace: package}):
            old = import_module(namespace + '.tools.align.colmap_io')
            shared = import_module(namespace + '.shared.colmap_io')
            self.assertIs(old.ColmapModelData, shared.ColmapModelData)
            self.assertIs(import_module(namespace + '.tools.align.colmap_io.types'),
                          import_module(namespace + '.shared.colmap_io.types'))
            writer = import_module(namespace + '.tools.align.colmap_io.binary_writer')
            self.assertIs(writer.ColmapModelData, shared.ColmapModelData)

    def assert_model_value_equal(self, left, right):
        """모델 값의 자료형과 배열 불변성을 재귀적으로 비교합니다."""
        self.assertIs(type(left), type(right))
        if isinstance(left, np.ndarray):
            self.assertEqual(left.dtype, right.dtype)
            self.assertFalse(left.flags.writeable)
            self.assertFalse(right.flags.writeable)
            np.testing.assert_array_equal(left, right)
        elif is_dataclass(left):
            for field in fields(left):
                if field.name not in {'source_path', 'source_format', 'display_name'}:
                    self.assert_model_value_equal(getattr(left, field.name), getattr(right, field.name))
        elif hasattr(left, 'items'):
            self.assertEqual(set(left), set(right))
            for key in left:
                self.assert_model_value_equal(left[key], right[key])
        elif isinstance(left, tuple):
            self.assertEqual(len(left), len(right))
            for a, b in zip(left, right):
                self.assert_model_value_equal(a, b)
        else:
            self.assertEqual(left, right)

    def test_structure_profile_preserves_numeric_and_rig_checks(self):
        """빈 점군 허용과 관측 연결 생략이 수치·리그 검증을 생략하지 않음을 확인합니다."""
        shared = import_module('shared.colmap_io')
        fixture = runpy.run_path(str(ROOT / 'tests/align/blender/test_colmap_io.py'))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            fixture['_write_text_model'](path)
            model = shared.read_sparse_model(shared.discover_sparse_model(path))
            shared.validate_model_structure(model)
            # 누락된 관측 연결은 구조 검증만 통과하고 기존 엄격 검증에서 거부됩니다.
            broken_images = replace(model.images, point2d_point3d_ids=np.array([777, shared.INVALID_POINT3D_ID, 42], dtype=np.uint64))
            broken_links = replace(model, images=broken_images)
            shared.validate_model_structure(broken_links, allow_empty_points=True)
            with self.assertRaises(shared.ColmapValidationError):
                shared.validate_model(broken_links)
            (path / 'points3D.txt').write_text('# 빈 점군\n', encoding='utf-8')
            empty = shared.read_sparse_model(shared.discover_sparse_model(path))
            shared.validate_model_structure(empty, allow_empty_points=True)
            with self.assertRaises(shared.ColmapValidationError):
                shared.validate_model_structure(empty)
            with self.assertRaises(shared.ColmapValidationError):
                shared.validate_model(empty)
            invalid_camera = replace(empty.cameras[5], params_float64=np.array([np.nan, 500, 320, 240]))
            invalid_images = replace(empty.images, image_records={**empty.images.image_records, 7: replace(empty.images.image_records[7], cam_from_world=np.zeros(7))})
            malformed = (
                replace(empty, cameras={**empty.cameras, 5: invalid_camera}),
                replace(empty, images=invalid_images),
                replace(empty, rigs={9: replace(empty.rigs[9], reference_sensor=None)}),
                replace(empty, frames={11: replace(empty.frames[11], rig_id=888)}),
                replace(empty, bounds_min=np.array([np.nan, 0, 0])),
                replace(model, points=replace(model.points, positions=np.array([[np.nan, 2, 3]]))),
                replace(model, points=replace(model.points, point_ids=np.array([0], dtype=np.uint64))),
            )
            for candidate in malformed:
                with self.subTest(candidate=candidate), self.assertRaises(shared.ColmapValidationError):
                    shared.validate_model_structure(candidate, allow_empty_points=True)
            with self.assertRaises(ValueError):
                replace(empty.images, point2d_xy=np.zeros((3, 3)))
            with self.assertRaises(ValueError):
                replace(empty.points, track_point2d_indices=np.zeros(0, dtype=np.float64))

    def test_text_binary_roundtrip_large_noncontiguous_ids(self):
        """큰 비연속 식별자·리그·프레임을 양 형식과 기존 경로로 비교합니다."""
        shared = import_module('shared.colmap_io')
        old = import_module('tools.align.colmap_io')
        fixture = runpy.run_path(str(ROOT / 'tests/align/blender/test_colmap_io.py'))
        writer = import_module('tools.align.colmap_io.binary_writer').write_binary_model
        with tempfile.TemporaryDirectory() as directory:
            text_path = Path(directory) / 'text'
            binary_path = Path(directory) / 'binary'
            fixture['_write_text_model'](text_path)
            large_id = 2**63 + 117
            for name in ('images.txt', 'points3D.txt'):
                path = text_path / name
                path.write_text(path.read_text(encoding='utf-8').replace('42', str(large_id)), encoding='utf-8')
            points_path = text_path / 'points3D.txt'
            points_path.write_text('9001 4 5 6 40 50 60 0.25\n' + points_path.read_text(encoding='utf-8'), encoding='utf-8')
            with patch('tools.align.colmap_io.create_incremental_sparse_model_reader', wraps=shared.create_incremental_sparse_model_reader) as factory:
                text_model = old.load_sparse_model(text_path)
                self.assertEqual(factory.call_count, 1)
            self.assertEqual(set(map(int, text_model.points.point_ids)), {large_id, 9001})
            writer(text_model, binary_path)
            binary_model = shared.load_sparse_model(binary_path)
            self.assert_model_value_equal(text_model, binary_model)
            self.assert_model_value_equal(binary_model, old.load_sparse_model(binary_path))


if __name__ == '__main__':
    unittest.main()
