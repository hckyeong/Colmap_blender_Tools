"""출처면 노말의 실제 바이너리 기록과 불변 캐시 계약을 검증합니다."""
from pathlib import Path
import sys
import tempfile
import unittest

import numpy as np

sys.path.insert(0,str(Path(__file__).resolve().parents[2] / 'tools/align/alignment'))
import icp_cccore_contract as contract
import icp_cccore_solver as solver


class NormalGeometryTest(unittest.TestCase):
    """계산 워커 실행과 독립적인 실제 파일 입력 경계를 검사합니다."""

    def setUp(self):
        """실제 기록 메서드에 필요한 파일 세션 상태를 격리합니다."""
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / 'native').mkdir()
        self.runner = object.__new__(solver.NativeRunner)
        self.runner.directory = self.root
        self.runner._cancel_path = self.root / 'cancel.request'
        self.runner.cancel_probe = None
        self.runner.files = {}
        self.runner.normal_fingerprints = {}
        self.positions = np.array([[0.,0.,0.],[1.,0.,0.],[0.,1.,0.]])
        self.normals = np.tile([0.,0.,1.],(3,1))

    def test_normals_binary_roundtrip_and_optional_absence(self):
        """단위 노말이 원래 순서와 float64 정밀도로 기록되고 없는 입력은 생략됩니다."""
        entry = self.runner.geometry('mesh-samples',self.positions,normals=self.normals)
        actual = np.fromfile(self.root / entry['normals_file'],dtype='<f8').reshape(-1,3)
        np.testing.assert_array_equal(actual,self.normals)
        self.assertEqual((self.root / entry['normals_file']).stat().st_size,72)
        self.assertEqual(self.runner.geometry('mesh-samples',self.positions,normals=self.normals.copy()),entry)
        plain = self.runner.geometry('cloud',self.positions)
        self.assertNotIn('normals_file',plain)

    def test_invalid_normals_do_not_write_partial_geometry(self):
        """모양·유한성·단위성 위반은 좌표 파일을 남기기 전에 거부됩니다."""
        cases = [self.normals[:2],np.ones(9),np.zeros((3,3)),self.normals*2,
                 np.array([[float('nan'),0.,1.]]*3)]
        for index, normals in enumerate(cases):
            with self.subTest(index=index):
                with self.assertRaises(contract.CcCoreInputError):
                    self.runner.geometry(f'invalid-{index}',self.positions,normals=normals)
        self.assertEqual(list((self.root / 'native').iterdir()),[])

    def test_cached_normals_cannot_change_appear_or_disappear(self):
        """한 ID의 노말 수정·추가·삭제를 거부하고 기록된 원본을 보존합니다."""
        entry = self.runner.geometry('mesh',self.positions,normals=self.normals)
        before = (self.root / entry['normals_file']).read_bytes()
        for normals in (-self.normals,None,self.normals[:2]):
            with self.subTest(normals=normals):
                with self.assertRaises(contract.CcCoreInputError):
                    self.runner.geometry('mesh',self.positions,normals=normals)
        self.runner.geometry('cloud',self.positions)
        with self.assertRaises(contract.CcCoreInputError):
            self.runner.geometry('cloud',self.positions,normals=self.normals)
        self.assertEqual((self.root / entry['normals_file']).read_bytes(),before)


if __name__ == '__main__':
    unittest.main()
