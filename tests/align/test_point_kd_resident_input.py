"""불러오기 검증·불변 원본 재사용·외부 좌표 변환과 수명 경계를 검사합니다."""
from contextlib import ExitStack
import gc
from multiprocessing.shared_memory import SharedMemory
import os
from pathlib import Path
import tempfile
from types import MethodType, SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np

from test_icp_geometry_cache import _runtime, _snapshot, _transform
from tools.align.geometry_buffers import (immutable_geometry_array, immutable_geometry_fingerprint,
                                         geometry_mapping_descriptor)
from tools.align.alignment import icp_geometry as geometry
from tools.align.alignment.icp_prepared_input import prepare_alignment_input, registered_alignment_input
from tools.align.alignment.icp_point_kd_input import create_shared_geometry_snapshot
from tools.align.alignment.icp_point_kd_transfer import prepared_native_request
from tools.align.alignment import icp_point_kd_contract as contract


def prepare_fixture(runtime):
    """순수 저장소의 원본을 실제 등록과 같은 불변·최초 검사 경계로 준비합니다."""
    repository = runtime.model_repository
    repository.alignment_inputs = {}
    for slot, models in (('MAIN', repository.models), ('COMPANION', repository.companion_meshes)):
        for model_id, model in models.items():
            model.display_name = model_id
            if hasattr(model, 'vertices_float64'):
                model.vertices_float64 = immutable_geometry_array(model.vertices_float64, '<f8')
                model.triangles_uint32 = immutable_geometry_array(model.triangles_uint32, '<u4')
            else:
                model.points.positions = immutable_geometry_array(model.points.positions, '<f8')
            repository.alignment_inputs[(model_id, slot)] = prepare_alignment_input(model)
    repository.alignment_input_for = MethodType(registered_alignment_input, repository)
    return runtime


@unittest.skipUnless(os.name == 'nt', 'Windows 상주 원본 검사입니다.')
class ResidentInputTests(unittest.TestCase):
    """기존 세계좌표 스냅샷과 새 참조 입력의 수치·검사·수명을 비교합니다."""

    def test_original_immutable_alias_and_fingerprint(self):
        """외부 별칭 수정과 쓰기 플래그 재활성화를 막고 최초 지문을 재사용합니다."""
        raw = np.arange(18, dtype=np.float64).reshape(6, 3)
        array = immutable_geometry_array(raw, '<f8')
        expected = array.copy()
        raw[:] = 0
        np.testing.assert_array_equal(array, expected)
        with self.assertRaises(ValueError):
            array.setflags(write=True)
        self.assertIs(immutable_geometry_array(array, '<f8'), array)
        first = immutable_geometry_fingerprint(array, '<f8')
        with patch('tools.align.alignment.icp_array_hash.array_sha256_fingerprint', side_effect=AssertionError('재해시 금지')):
            self.assertEqual(immutable_geometry_fingerprint(array, '<f8'), first)

    def compare(self, runtime):
        """불러오기 이후 검사·해시 없이 만든 입력을 기존 전체 계산 결과와 바이트 비교합니다."""
        expected = _snapshot(runtime)
        prepare_fixture(runtime)
        with ExitStack() as owner, patch.object(geometry, '_validate_raw_geometry', side_effect=AssertionError('재검사 금지')), \
             patch.object(geometry, '_array_fingerprint', side_effect=AssertionError('재해시 금지')):
            metadata, actual = create_shared_geometry_snapshot(runtime, owner, moving_model_id='이동',
                reference_model_ids=runtime.model_catalog.reference_ids,
                initial_alignment_float64=runtime.model_repository.alignments['이동'].matrix_float64)
            self.assertEqual(metadata, expected.without_arrays())
            self.assertIsNone(geometry.geometry_snapshot_validation_message(runtime, metadata, point_kd_input=True))
            self.assertIsNone(geometry.geometry_snapshot_validation_message(runtime, metadata, point_kd_input=True, verify_content=False))
            with tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                for role in ('moving', 'reference'):
                    contract.validate_local_parts(actual[role], root)
                request = {'source': actual['moving'], 'target': actual['reference']}
                names = []
                with prepared_native_request(root, request) as prepared:
                    for role, source in (('source', expected.moving), ('target', expected.reference_combined)):
                        for field, array in (('positions', source.positions_float64), ('triangles', source.triangles_uint32)):
                            if array is None:
                                continue
                            descriptor = prepared[role][field + '_mapping']
                            names.append(descriptor['name'])
                            memory = SharedMemory(name=descriptor['name'])
                            try:
                                self.assertEqual(bytes(memory.buf[64:64 + array.nbytes]), array.tobytes())
                            finally:
                                memory.close()
                for name in names:
                    with self.assertRaises(FileNotFoundError):
                        SharedMemory(name=name)
            again = create_shared_geometry_snapshot(runtime, owner, moving_model_id='이동',
                reference_model_ids=runtime.model_catalog.reference_ids,
                initial_alignment_float64=runtime.model_repository.alignments['이동'].matrix_float64)[1]
            self.assertEqual(actual, again)
        return metadata

    def test_mesh_multiple_references_and_affine_transform(self):
        """여러 A의 연결·면 오프셋과 축별 배율·회전·이동을 보존합니다."""
        runtime = _runtime()
        repo = runtime.model_repository
        from test_icp_geometry_cache import _mesh
        repo.models['추가'] = _mesh('추가')
        runtime.model_catalog.models['추가'] = SimpleNamespace(model_id='추가', display_name='추가',
            geometry_kind='TRIANGLE_MESH', show_points=False, show_surface=True)
        runtime.model_catalog.reference_ids += ('추가',)
        matrix = np.diag((2., 3., 4., 1.))
        matrix[:3, 3] = (3., -9., 4.)
        repo.displays['추가'], repo.alignments['추가'] = _transform(matrix), _transform()
        self.compare(runtime)

    def test_all_point_mesh_combinations(self):
        """점군끼리·점군과 메시의 양방향에서 같은 좌표·면·출처를 유지합니다."""
        for names in ((), ('이동',), ('기준',), ('이동', '기준')):
            with self.subTest(names=names):
                runtime = _runtime()
                for model_id in names:
                    runtime.model_repository.models[model_id] = SimpleNamespace(model_id=model_id,
                        geometry_kind='COLMAP_POINT_CLOUD', geometry_revision=1, source_path='검증',
                        points=SimpleNamespace(positions=np.eye(3)))
                    record = runtime.model_catalog.models[model_id]
                    record.geometry_kind, record.show_points, record.show_surface = 'COLMAP_POINT_CLOUD', True, False
                self.compare(runtime)

    def test_companion_and_replacement_rejected(self):
        """연결 메시를 주 점군과 구분하고 같은 개정 번호의 원본 교체도 거부합니다."""
        runtime = _runtime()
        repo = runtime.model_repository
        repo.companion_meshes['이동'] = repo.models['이동']
        repo.models['이동'] = SimpleNamespace(model_id='이동', geometry_kind='COLMAP_POINT_CLOUD',
            geometry_revision=1, source_path='검증', points=SimpleNamespace(positions=np.eye(3)))
        runtime.model_catalog.models['이동'].geometry_kind = 'COLMAP_POINT_CLOUD'
        metadata = self.compare(runtime)
        repo.companion_meshes['이동'].vertices_float64 = repo.companion_meshes['이동'].vertices_float64.copy()
        self.assertIsNotNone(geometry.geometry_snapshot_validation_message(runtime, metadata, point_kd_input=True))

    def test_invalid_input_known_before_alignment(self):
        """유효면 없는 형상은 불러오기 검사 결과를 재사용해 시작 전에 거부합니다."""
        runtime = _runtime()
        runtime.model_repository.models['이동'].triangles_uint32[:] = 0
        prepare_fixture(runtime)
        with patch.object(geometry, '_validate_raw_geometry', side_effect=AssertionError('재검사 금지')):
            with self.assertRaisesRegex(geometry.CcCoreGeometryError, '퇴화'):
                runtime.model_repository.alignment_input_for('이동', 'MAIN')

    def test_job_holds_removed_source_and_releases_it(self):
        """모델 삭제 뒤에도 실행 중 입력은 유효하고 마지막 참조 해제 뒤 매핑이 사라집니다."""
        runtime = prepare_fixture(_runtime())
        repo = runtime.model_repository
        owner = ExitStack()
        _, actual = create_shared_geometry_snapshot(runtime, owner, moving_model_id='이동',
            reference_model_ids=('기준',), initial_alignment_float64=np.eye(4))
        names = [part['positions_mapping']['name'] for role in ('moving', 'reference') for part in actual[role]['parts']]
        repo.models.clear()
        repo.alignment_inputs.clear()
        gc.collect()
        for name in names:
            memory = SharedMemory(name=name)
            memory.close()
        owner.close()
        gc.collect()
        for name in names:
            with self.assertRaises(FileNotFoundError):
                SharedMemory(name=name)

    def test_cancel_before_transform_releases_work_buffers(self):
        """변환 전 취소를 외부 작업에 전달하고 원본은 다음 Align에 유지합니다."""
        runtime = prepare_fixture(_runtime())
        with ExitStack() as owner, tempfile.TemporaryDirectory() as folder:
            _, actual = create_shared_geometry_snapshot(runtime, owner, moving_model_id='이동',
                reference_model_ids=('기준',), initial_alignment_float64=np.eye(4))
            root = Path(folder)
            (root / 'cancel.request').touch()
            with self.assertRaises(contract.PointKdCancelled):
                with prepared_native_request(root, {'source': actual['moving'], 'target': actual['reference']}):
                    self.fail('취소된 입력을 계산에 전달했습니다.')


if __name__ == '__main__':
    unittest.main(verbosity=2)
