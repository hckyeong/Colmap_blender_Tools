"""점 기반 작업의 방향·강체성·파일 경계와 실제 네이티브 조정을 검증합니다."""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools" / "align" / "alignment"))
import icp_point_kd_contract as contract
import icp_point_kd_worker as worker


def fixture_job(root: Path, moving_kind: str = "COLMAP_POINT_CLOUD", reference_kind: str = "COLMAP_POINT_CLOUD",
                *, reverse: bool = False, large_origin: bool = False, scale: float = 1.0) -> tuple[dict, np.ndarray]:
    """알려진 강체·배율 정답과 전체 좌표 파일을 갖는 비대칭 형상을 만듭니다."""
    vertices = np.array([(-1, -.7, -.5), (1.4, -.8, -.4), (.9, 1.2, -.3), (-.8, .7, -.2),
                         (-.4, -.5, 1.4), (.7, -.4, 1.1), (.8, .9, 1.3), (-.5, .8, 1.6)], dtype=float)
    triangles = np.array([(0, 2, 1), (0, 3, 2), (4, 5, 6), (4, 6, 7), (0, 1, 5), (0, 5, 4),
                          (1, 2, 6), (1, 6, 5), (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7)], dtype="<u4")
    for _ in range(3):
        expanded = vertices.tolist()
        midpoints = {}
        faces = []
        for a, b, c in triangles:
            mids = []
            for start, end in ((a, b), (b, c), (c, a)):
                key = tuple(sorted((int(start), int(end))))
                if key not in midpoints:
                    midpoints[key] = len(expanded)
                    expanded.append(((vertices[start] + vertices[end]) * .5).tolist())
                mids.append(midpoints[key])
            ab, bc, ca = mids
            faces.extend(((int(a), ab, ca), (ab, int(b), bc), (ca, bc, int(c)), (ab, bc, ca)))
        vertices = np.array(expanded, dtype="<f8")
        triangles = np.array(faces, dtype="<u4")
    same_kind = moving_kind == reference_kind
    points = np.random.default_rng(917).normal(size=(4000, 3)) if moving_kind == reference_kind == "COLMAP_POINT_CLOUD" else vertices
    angle = np.deg2rad(.5)
    expected = np.eye(4)
    expected[:3, :3] = ((np.cos(angle), -np.sin(angle), 0), (np.sin(angle), np.cos(angle), 0), (0, 0, 1))
    expected[:3, :3] *= scale
    expected[:3, 3] = (.003, -.002, .004)
    if large_origin:
        origin = np.array((1e7, -2e7, 3e7))
        points = points + origin
        expected[:3, 3] += origin - expected[:3, :3] @ origin
    geometry = {"input_fingerprint": "synthetic-geometry", "provenance": {"synthetic": True}}
    for role, kind in (("moving", moving_kind), ("reference", reference_kind)):
        source = points.copy()
        if same_kind and reverse and role == "moving":
            source = np.vstack((source, source[:3]))
        if role == "reference":
            source = source @ expected[:3, :3].T + expected[:3, 3]
        name = f"{role}.bin"
        np.asarray(source, dtype="<f8").tofile(root / name)
        entry = {"kind": kind, "positions_file": name, "point_count": len(source)}
        if kind == "TRIANGLE_MESH":
            name = f"{role}-triangles.bin"
            triangles.tofile(root / name)
            entry.update(triangles_file=name, triangle_count=len(triangles))
        geometry[role] = entry
    parameters = contract.PointKdRuntimeParameters(max_threads=1, maximum_correspondence_distance=.1)
    manifest = {"manifest_version": 1, **contract.identity({"job_id": root.name, "input_fingerprint": "synthetic", "moving_model_id": "moving"}),
                "reference_model_ids": ["reference"], "initial_alignment_4x4": np.eye(4).tolist(),
                "marker_signature": [], "marker_initial_rms": 0.0, "model_revision_snapshot": [["moving", 1], ["reference", 1]],
                "model_state_snapshot": [["moving", 1, "a", "b"], ["reference", 1, "c", "d"]],
                "geometry_mode": "POINT_TO_POINT" if same_kind and moving_kind == "COLMAP_POINT_CLOUD" else "MESH_TO_MESH",
                "parameter_fingerprint": parameters.fingerprint, "parameters": parameters.to_mapping(), "geometry": geometry}
    contract.write_json(root / "manifest.json", manifest)
    return manifest, expected


def native_worker_path() -> Path:
    """검증할 실제 새 엔진 실행 파일을 소스 캐시에서 찾습니다."""
    return ROOT / ".point-kd-runtime" / "point_kd" / "colmap_point_kd_worker.exe"


class PointKdContractTests(unittest.TestCase):
    """독립 작업의 수학·형식·경계 검증을 실행합니다."""

    def test_direction_and_inverse_preserve_existing_scale(self) -> None:
        """크기와 종류에 따른 양방향 결과가 동일한 P 강체 결과와 초기 배율을 보존합니다."""
        for moving_kind, reference_kind, moving_count, reference_count, source, method in (
            ("COLMAP_POINT_CLOUD", "COLMAP_POINT_CLOUD", 8, 9, "moving", "POINT_TO_POINT"),
            ("COLMAP_POINT_CLOUD", "COLMAP_POINT_CLOUD", 9, 8, "reference", "POINT_TO_POINT"),
            ("TRIANGLE_MESH", "TRIANGLE_MESH", 8, 8, "moving", "POINT_TO_PLANE"),
            ("TRIANGLE_MESH", "TRIANGLE_MESH", 9, 8, "reference", "POINT_TO_PLANE"),
            ("COLMAP_POINT_CLOUD", "TRIANGLE_MESH", 99, 8, "moving", "POINT_TO_PLANE"),
            ("TRIANGLE_MESH", "COLMAP_POINT_CLOUD", 8, 99, "reference", "POINT_TO_PLANE")):
            manifest = {"geometry": {"moving": {"kind": moving_kind, "point_count": moving_count},
                                     "reference": {"kind": reference_kind, "point_count": reference_count}}}
            self.assertEqual(contract.registration_direction(manifest)[::2], (source, method))
        initial = np.diag((2.5, 2.5, 2.5, 1.0))
        initial[:3, 3] = (20, 30, -5)
        native = np.eye(4)
        native[:3, 3] = (1, 2, 3)
        delta = contract.moving_delta(native, True)
        np.testing.assert_allclose((delta @ initial)[:3, :3], initial[:3, :3])
        np.testing.assert_allclose((delta @ initial)[:3, 3], (19, 28, -8))

    def test_scale_reflection_and_nonfinite_are_rejected(self) -> None:
        """네이티브 출력이 배율·반사·비유한 수치를 미리보기로 전달하지 못합니다."""
        for value in (np.diag((1.01, 1.01, 1.01, 1)), np.diag((-1, 1, 1, 1)), np.full((4, 4), np.nan)):
            with self.assertRaises(contract.PointKdError):
                contract.moving_delta(value, False)

    def test_sim3_matrix_modes_and_progress(self) -> None:
        """배율 허용은 명시적 모드에만 한정하고 역방향·전단·반사·모드 지문을 검사합니다."""
        matrix = np.diag((1.04, 1.04, 1.04, 1.0))
        matrix[:3, 3] = (2, -3, 4)
        np.testing.assert_allclose(contract.moving_delta(matrix, True, "SIM3") @ matrix, np.eye(4), atol=1e-14)
        for bad in (np.diag((1.04, 1.02, 1, 1)), np.diag((-1, 1, 1, 1)), np.diag((0, 0, 0, 1))):
            with self.assertRaises(contract.PointKdError):
                contract.moving_delta(bad, False, "SIM3")
        shear = matrix.copy()
        shear[0, 1] = .01
        with self.assertRaises(contract.PointKdError):
            contract.moving_delta(shear, False, "SIM3")
        fixed = contract.PointKdRuntimeParameters()
        sim3 = contract.PointKdRuntimeParameters(scale_mode="SIM3")
        self.assertNotEqual(fixed.fingerprint, sim3.fingerprint)
        for options in ({"scale_mode": "OTHER"}, {"scale_tolerance": 0}, {"scale_tolerance": float('nan')}):
            with self.assertRaises(contract.PointKdError):
                contract.PointKdRuntimeParameters(**options)
        with tempfile.TemporaryDirectory() as directory:
            manifest, _ = fixture_job(Path(directory))
            manifest["parameters"] = sim3.to_mapping()
            event = worker.progress_payload(manifest, {"protocol_version": contract.NATIVE_PROTOCOL_VERSION,
                "progress": .4, "delta_4x4": matrix.tolist()}, True)
            self.assertEqual(event["stage"], "SIM3")
            np.testing.assert_allclose(event["stable_alignment_4x4"], np.linalg.inv(matrix))

    @unittest.skipUnless(native_worker_path().is_file(), "점 기반 네이티브 빌드가 필요합니다.")
    def test_sim3_four_geometry_combinations_and_reverse(self) -> None:
        """네 종류의 형상과 역질의에서 실제 배율을 회복하고 모드·배율 변조를 거부합니다."""
        for moving, reference in (("COLMAP_POINT_CLOUD", "COLMAP_POINT_CLOUD"),
                ("COLMAP_POINT_CLOUD", "TRIANGLE_MESH"), ("TRIANGLE_MESH", "COLMAP_POINT_CLOUD"),
                ("TRIANGLE_MESH", "TRIANGLE_MESH")):
            for reverse in (False, True):
                with self.subTest(moving=moving, reference=reference, reverse=reverse), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    manifest, expected = fixture_job(root, moving, reference, reverse=reverse, scale=.985)
                    parameters = contract.PointKdRuntimeParameters(max_threads=2, scale_mode="SIM3", maximum_correspondence_distance=.1)
                    manifest.update(parameters=parameters.to_mapping(), parameter_fingerprint=parameters.fingerprint)
                    contract.write_json(root / 'manifest.json', manifest)
                    worker.run_worker(root, native_worker_path())
                    metadata = contract.read_json(root / 'result.json')
                    self.assertEqual(metadata['quality'], 'PASS')
                    self.assertEqual(metadata['scale_mode'], 'SIM3')
                    self.assertAlmostEqual(metadata['additional_scale'], .985, places=7)
                    with np.load(root / 'result.npz') as arrays:
                        np.testing.assert_allclose(arrays['delta_4x4'], expected, atol=2e-6, rtol=0)
                    request, _ = worker.native_request(manifest)
                    for field, value in (("scale_mode", "FIXED"), ("additional_scale", 2.0)):
                        damaged = {**metadata['native_result'], field: value}
                        with self.assertRaises(contract.PointKdError):
                            contract.validate_native_result(damaged, request)

    def test_manifest_rejects_truncation_foreign_backend_and_parameter_changes(self) -> None:
        """잘린 배열·다른 엔진·설정 변조를 실제 파일을 통해 거부합니다."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest, _ = fixture_job(root)
            contract.validate_manifest(manifest, root)
            for field, value in (("backend_id", "CCCORELIB"), ("algorithm_version", "cccore-icp-v7"),
                                  ("parameter_fingerprint", "changed")):
                changed = {**manifest, field: value}
                with self.assertRaises(contract.PointKdError):
                    contract.validate_manifest(changed, root)
            (root / "moving.bin").write_bytes(b"bad")
            with self.assertRaises(contract.PointKdError):
                contract.validate_manifest(manifest, root)

    def test_file_escape_duplicate_json_and_nonfinite_rejected(self) -> None:
        """상위 디렉터리·절대 경로·중복 키·비표준 수치를 입력에서 차단합니다."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for relative in ("../escape.bin", "nested/../inside.bin", "C:drive-relative.bin", "value.bin:stream",
                             "\\rooted.bin", str(root / "absolute.bin"), ""):
                with self.assertRaises(contract.PointKdError):
                    contract.job_path(root, relative)
            for content in ('{"backend_id":"POINT_KD_ICP","backend_id":"CCCORELIB"}', '{"value":NaN}'):
                (root / "bad.json").write_text(content, encoding="utf-8")
                with self.assertRaises(contract.PointKdError):
                    contract.read_json(root / "bad.json")

    def test_junction_escape_and_shared_reader_atomic_replacement(self) -> None:
        """링크 경로 이탈을 막고 관찰자가 읽기를 마친 뒤 원자 교체가 완료됩니다."""
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as outside:
            root = Path(directory)
            link = root / "outside"
            if os.name == "nt":
                import subprocess
                completed = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), outside],
                                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                self.assertEqual(completed.returncode, 0)
            else:
                link.symlink_to(outside, target_is_directory=True)
            with self.assertRaises(contract.PointKdError):
                contract.job_path(root, "outside/result.json")
            target = root / "progress.json"
            contract.write_json(target, {"iteration": 1})
            with contract._open_json_shared(target) as reader:
                self.assertEqual(json.load(reader), {"iteration": 1})
                from threading import Timer
                closer = Timer(.05, reader.close)
                closer.start()
                try:
                    contract.write_json(target, {"iteration": 2})
                finally:
                    closer.join()
            self.assertEqual(contract.read_json(target), {"iteration": 2})

    def test_temporary_share_conflict_and_iteration_cap(self) -> None:
        """일시적인 공유 오류를 재시도하고 네이티브의 30회 상한을 넘는 요청을 거부합니다."""
        with self.assertRaises(contract.PointKdError):
            contract.PointKdRuntimeParameters(max_iterations=31)
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "value.json"
            actual_replace = os.replace
            attempts = []
            def replace(source, destination):
                """처음 두 번의 공유 위반 뒤 실제 파일 교체를 수행합니다."""
                attempts.append(1)
                if len(attempts) < 3:
                    raise PermissionError("일시적인 파일 공유 충돌")
                actual_replace(source, destination)
            with patch.object(contract.os, "replace", side_effect=replace):
                contract.write_json(target, {"result": "완료"})
            self.assertEqual(len(attempts), 3)
            self.assertEqual(contract.read_json(target), {"result": "완료"})

    def test_progress_rejects_scale_and_wrong_identity(self) -> None:
        """진행 미리보기에서도 네이티브 프로토콜과 강체성을 검증합니다."""
        with tempfile.TemporaryDirectory() as directory:
            manifest, _ = fixture_job(Path(directory))
            for event in ({"protocol_version": "cccore-native-v1", "progress": .1},
                          {"protocol_version": contract.NATIVE_PROTOCOL_VERSION, "progress": .1,
                           "delta_4x4": np.diag((1.1, 1.1, 1.1, 1)).tolist()}):
                with self.assertRaises(contract.PointKdError):
                    worker.progress_payload(manifest, event, False)

    @unittest.skipUnless(native_worker_path().is_file(), "점 기반 네이티브 빌드가 필요합니다.")
    def test_actual_worker_four_geometry_combinations_and_reverse(self) -> None:
        """네 입력 조합의 실제 외부 정렬이 원래 P 방향과 전체점 감사 기록을 유지합니다."""
        for moving_kind, reference_kind in (("COLMAP_POINT_CLOUD", "COLMAP_POINT_CLOUD"),
                ("COLMAP_POINT_CLOUD", "TRIANGLE_MESH"), ("TRIANGLE_MESH", "COLMAP_POINT_CLOUD"),
                ("TRIANGLE_MESH", "TRIANGLE_MESH")):
            for reverse in (False, True):
                with self.subTest(moving=moving_kind, reference=reference_kind, reverse=reverse), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    manifest, expected = fixture_job(root, moving_kind, reference_kind, reverse=reverse)
                    worker.run_worker(root, native_worker_path())
                    metadata = contract.read_json(root / "result.json")
                    with np.load(root / "result.npz", allow_pickle=False) as arrays:
                        np.testing.assert_allclose(arrays["delta_4x4"], expected, atol=2e-5, rtol=0)
                    self.assertIs(metadata["independent_evaluation_performed"], False)
                    self.assertIs(metadata["downsampling_performed"], False)
                    self.assertEqual(metadata["backend_id"], contract.BACKEND_ID)
                    request, actual_reverse = worker.native_request(manifest)
                    self.assertEqual(metadata["reverse_registration"], actual_reverse)
                    self.assertEqual(metadata["native_result"]["source_count"], request["source"]["point_count"])

    @unittest.skipUnless(native_worker_path().is_file(), "점 기반 네이티브 빌드가 필요합니다.")
    def test_actual_large_coordinates_and_cancel(self) -> None:
        """큰 공통 좌표에서도 강체 정답을 회복하며 선행 취소는 결과를 공개하지 않습니다."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, expected = fixture_job(root, large_origin=True)
            worker.run_worker(root, native_worker_path())
            with np.load(root / "result.npz", allow_pickle=False) as arrays:
                np.testing.assert_allclose(arrays["delta_4x4"][:3, :3], expected[:3, :3], atol=1e-8, rtol=0)
                np.testing.assert_allclose(arrays["delta_4x4"][:3, 3], expected[:3, 3], atol=.2, rtol=0)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture_job(root)
            (root / "cancel.request").touch()
            with self.assertRaises(contract.PointKdCancelled):
                worker.run_worker(root, native_worker_path())
            self.assertFalse((root / "result.json").exists())


if __name__ == "__main__":
    unittest.main()
