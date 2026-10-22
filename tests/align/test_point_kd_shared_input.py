"""공유 입력의 좌표 동등성·원본 검증·해제 경계와 설명자 거부를 검사합니다."""
from contextlib import ExitStack
from multiprocessing.shared_memory import SharedMemory
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np

from test_icp_geometry_cache import _runtime, _snapshot, _mesh, _transform
from tools.align.alignment import icp_point_kd_input as shared_input
from tools.align.alignment import icp_geometry as geometry
from tools.align.alignment import icp_point_kd_contract as contract
from tools.align.alignment import icp_backend as backend


@unittest.skipUnless(os.name == 'nt', 'Windows 공유 입력 검사입니다.')
class SharedInputTests(unittest.TestCase):
    """기존 불변 스냅샷을 독립 정답으로 사용하여 전달 경계를 확인합니다."""

    def compare(self, runtime):
        """공개 준비 결과를 기존 값과 바이트 단위로 대조하고 모든 버퍼 해제를 확인합니다."""
        expected = _snapshot(runtime)
        names = []
        with ExitStack() as owner:
            metadata, actual = shared_input.create_shared_geometry_snapshot(runtime, owner,
                moving_model_id='이동', reference_model_ids=runtime.model_catalog.reference_ids,
                initial_alignment_float64=runtime.model_repository.alignments['이동'].matrix_float64)
            self.assertEqual(metadata, expected.without_arrays())
            for role, source in (('moving', expected.moving), ('reference', expected.reference_combined)):
                self.assertEqual(actual[role]['provenance'], contract.plain(source.provenance))
                for field, values in (('positions', source.positions_float64), ('triangles', source.triangles_uint32)):
                    if values is None:
                        self.assertNotIn(field + '_mapping', actual[role])
                        continue
                    entry = actual[role][field + '_mapping']
                    self.assertEqual(contract.validate_input_buffer(actual[role], field, values.nbytes, Path('.')),
                                     'WIN32_SHARED_MEMORY_V1')
                    names.append(entry['name'])
                    memory = SharedMemory(name=entry['name'])
                    try:
                        self.assertEqual(bytes(memory.buf[64:64 + values.nbytes]), values.tobytes())
                    finally:
                        memory.close()
            self.assertIsNone(geometry.geometry_snapshot_validation_message(runtime, metadata, point_kd_input=True))
        for name in names:
            with self.assertRaises(FileNotFoundError):
                SharedMemory(name=name)
        return metadata

    def test_mesh_transforms_multiple_references_and_original_mutation(self):
        """축별 배율·회전·여러 기준의 면 오프셋을 보존하고 이후 원본 변경을 감지합니다."""
        runtime = _runtime()
        repo, catalog = runtime.model_repository, runtime.model_catalog
        display = np.diag((2., 3., 4., 1.))
        angle = .173
        alignment = np.eye(4)
        alignment[:3, :3] = ((np.cos(angle), -np.sin(angle), 0), (np.sin(angle), np.cos(angle), 0), (0, 0, 1))
        alignment[:3, 3] = (1.23, -4.56, 7.89)
        repo.displays['이동'], repo.alignments['이동'] = _transform(display), _transform(alignment)
        repo.models['추가'] = _mesh('추가')
        repo.displays['추가'], repo.alignments['추가'] = _transform(display), _transform(alignment)
        catalog.models['추가'] = SimpleNamespace(model_id='추가', display_name='추가', geometry_kind='TRIANGLE_MESH', show_surface=True, show_points=False)
        catalog.reference_ids = ('기준', '추가')
        original = repo.models['이동'].vertices_float64.copy()
        metadata = self.compare(runtime)
        np.testing.assert_array_equal(repo.models['이동'].vertices_float64, original)
        self.assertTrue(repo.models['이동'].vertices_float64.flags.writeable)
        repo.models['이동'].vertices_float64[0, 0] += .001
        self.assertIsNotNone(geometry.geometry_snapshot_validation_message(runtime, metadata, point_kd_input=True))

    def test_point_cloud_and_mixed_directions(self):
        """점군끼리와 메시·점군 양방향 입력이 기존 선택·지문·좌표를 보존합니다."""
        for point_ids in (('이동',), ('기준',), ('이동', '기준')):
            with self.subTest(point_ids=point_ids):
                runtime = _runtime()
                for name in point_ids:
                    runtime.model_repository.models[name] = SimpleNamespace(model_id=name, geometry_kind='COLMAP_POINT_CLOUD',
                        geometry_revision=1, source_path='검증', points=SimpleNamespace(positions=np.eye(3)))
                    record = runtime.model_catalog.models[name]
                    record.geometry_kind, record.show_points, record.show_surface = 'COLMAP_POINT_CLOUD', True, False
                self.compare(runtime)

    def test_companion_mesh(self):
        """연결 메시의 실제 슬롯과 주 점군 출처를 기존 방식대로 구분합니다."""
        runtime = _runtime()
        repo = runtime.model_repository
        repo.companion_meshes['이동'] = repo.models['이동']
        repo.models['이동'] = SimpleNamespace(model_id='이동', geometry_kind='COLMAP_POINT_CLOUD', geometry_revision=1,
            source_path='검증', points=SimpleNamespace(positions=np.eye(3)))
        runtime.model_catalog.models['이동'].geometry_kind = 'COLMAP_POINT_CLOUD'
        self.compare(runtime)

    def test_failure_releases_previously_allocated_buffers(self):
        """일부 버퍼 생성 후 준비가 실패해도 작업 소유자가 모든 매핑을 해제합니다."""
        runtime = _runtime()
        allocate, names = shared_input._allocate, []
        def fail_third(owner, count, dtype):
            """기준 버퍼 생성 실패를 재현하고 이미 생성한 입력 이름을 보관합니다."""
            if len(names) == 2:
                raise OSError('검증용 할당 실패')
            memory, descriptor = allocate(owner, count, dtype)
            names.append(descriptor['name'])
            return memory, descriptor
        with self.assertRaisesRegex(OSError, '할당 실패'):
            with ExitStack() as owner, patch.object(shared_input, '_allocate', side_effect=fail_third):
                shared_input.create_shared_geometry_snapshot(runtime, owner, moving_model_id='이동',
                    reference_model_ids=('기준',), initial_alignment_float64=np.eye(4))
        self.assertEqual(len(names), 2)
        for name in names:
            with self.assertRaises(FileNotFoundError):
                SharedMemory(name=name)

    def test_invalid_descriptor_rejected(self):
        """외부 이름·길이·중복 전달 설명자를 공유 메모리를 열기 전에 거부합니다."""
        valid = {'name': 'Local\\ColmapPointKd_' + 'a' * 32, 'byte_count': 72}
        for change in ({'name': 'Global\\임의'}, {'byte_count': 71}, {'byte_count': True}, {'extra': 1}):
            with self.subTest(change=change), self.assertRaises(contract.PointKdError):
                contract.validate_input_buffer({'positions_mapping': {**valid, **change}}, 'positions', 72, Path('.'))
        with self.assertRaises(contract.PointKdError):
            contract.validate_input_buffer({'positions_mapping': valid, 'positions_file': '입력.bin'}, 'positions', 72, Path('.'))

    def test_cleanup_waits_for_exit_and_releases_when_diagnostics_retained(self):
        """실행 중 해제를 금지하고 진단 보존 시에도 입력 자원은 한 번만 닫습니다."""
        owner = ExitStack()
        closed = []
        owner.callback(closed.append, True)
        handle = SimpleNamespace(cleaned=False, is_running=True, _input_resource_owner=owner, _worker_log_handle=None)
        with self.assertRaises(backend.IcpBackendError):
            backend.cleanup_job_directory(handle, keep_failure_diagnostics=True)
        self.assertEqual(closed, [])
        handle.is_running = False
        backend.cleanup_job_directory(handle, keep_failure_diagnostics=True)
        backend.cleanup_job_directory(handle, keep_failure_diagnostics=True)
        self.assertEqual(closed, [True])


if __name__ == '__main__':
    unittest.main(verbosity=2)
