"""독립 점 KD 워커의 실제 실행과 수치·입력 보존 계약을 검증합니다."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from multiprocessing.shared_memory import SharedMemory
import struct
from uuid import uuid4
from pathlib import Path
import subprocess
import tempfile
import time
import unittest

import numpy as np


ROOT = Path(__file__).resolve().parents[3]
WORKER = Path(os.environ.get("COLMAP_POINT_KD_WORKER", ROOT / ".point-kd-runtime/build/bin/colmap_point_kd_worker.exe"))
REPORT: Path | None = None
MEASUREMENTS: list[dict] = []


def rotation_xyz(x: float, y: float, z: float) -> np.ndarray:
    """알려진 강체 변환 검증에 사용할 세 축 회전을 구성합니다."""
    cx, sx, cy, sy, cz, sz = np.cos(x), np.sin(x), np.cos(y), np.sin(y), np.cos(z), np.sin(z)
    return np.array([[cz, -sz, 0], [sz, cz, 0], [0, 0, 1]]) @ np.array([[cy, 0, sy], [0, 1, 0], [-sy, 0, cy]]) @ np.array([[1, 0, 0], [0, cx, -sx], [0, sx, cx]])


def fixture(count: int = 1700) -> tuple[np.ndarray, np.ndarray]:
    """비대칭 타원체의 좌표와 해석적 표면 법선을 생성합니다."""
    rng = np.random.default_rng(881407)
    points = rng.normal(size=(count, 3))
    points /= np.linalg.norm(points, axis=1)[:, None]
    points *= np.array([1.7, 0.9, 0.6])
    normals = points / np.array([1.7, 0.9, 0.6]) ** 2
    normals /= np.linalg.norm(normals, axis=1)[:, None]
    return points, normals


@unittest.skipUnless(WORKER.is_file(), "점 KD 네이티브 워커 빌드가 필요합니다.")
class PointKdWorkerTests(unittest.TestCase):
    """실행 파일을 직접 호출하여 Python 모의 구현 없는 계약을 검사합니다."""

    def setUp(self) -> None:
        """한글 경로의 독립 작업 폴더와 공통 요청을 준비합니다."""
        self.temp = tempfile.TemporaryDirectory(prefix="점정렬_검증_")
        self.directory = Path(self.temp.name)
        self.request = {"protocol_version": "point-kd-native-v1", "method": "POINT_TO_POINT", "parameters": {"max_threads": 2, "maximum_correspondence_distance": 0.16}, "result_file": "result.json", "progress_file": "progress.json", "cancel_file": "cancel"}

    def tearDown(self) -> None:
        """검증용 바이너리와 임시 결과를 정리합니다."""
        self.temp.cleanup()

    def cloud(self, name: str, points: np.ndarray, normals: np.ndarray | None = None, triangles: np.ndarray | None = None) -> dict:
        """명시된 자료형으로 입력을 저장하고 네이티브 설명자를 만듭니다."""
        position_name = name + "_positions.bin"
        np.asarray(points, dtype="<f8").tofile(self.directory / position_name)
        value = {"positions_file": position_name, "point_count": len(points)}
        if normals is not None:
            normal_name = name + "_normals.bin"
            np.asarray(normals, dtype="<f8").tofile(self.directory / normal_name)
            value["normals_file"] = normal_name
        if triangles is not None:
            triangle_name = name + "_triangles.bin"
            np.asarray(triangles, dtype="<u4").tofile(self.directory / triangle_name)
            value.update(triangles_file=triangle_name, triangle_count=len(triangles))
        return value

    def run_job(self) -> dict:
        """입력 해시를 보존하면서 실제 워커를 실행하고 유한 결과를 읽습니다."""
        path = self.directory / "request.json"
        path.write_text(json.dumps(self.request, ensure_ascii=False), encoding="utf-8")
        hashes = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in self.directory.glob("*.bin")}
        result = subprocess.run([str(WORKER), "--request", str(path)], capture_output=True, encoding="utf-8", timeout=40)
        self.assertIn(result.returncode, (0, 2), result.stderr)
        output = json.loads((self.directory / "result.json").read_text(encoding="utf-8"))
        self.assertEqual(output["protocol_version"], "point-kd-native-v1")
        self.assertEqual(output["engine"], "POINT_KD")
        self.assertTrue(np.isfinite(np.asarray(output["delta_4x4"])).all())
        self.assertNotIn("NaN", json.dumps(output))
        self.assertEqual(hashes, {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in self.directory.glob("*.bin")})
        return output

    def shared_cloud(self, descriptor: dict) -> tuple[dict, list]:
        """기존 파일 입력과 같은 바이트를 작업별 공유 메모리에 담아 독립 전달합니다."""
        value, memories = dict(descriptor), []
        for field in ('positions', 'triangles', 'normals'):
            if field + '_file' not in value:
                continue
            data = (self.directory / value.pop(field + '_file')).read_bytes()
            token = uuid4().hex
            memory = SharedMemory(name='Local\\ColmapPointKd_' + token, create=True, size=64 + len(data))
            self.addCleanup(memory.close)
            struct.pack_into('<8sQ32s16x', memory.buf, 0, b'CPKDIPC1', len(data), token.encode('ascii'))
            memory.buf[64:64 + len(data)] = data
            value[field + '_mapping'] = {'name': memory.name, 'byte_count': len(data)}
            memories.append(memory)
        return value, memories

    @unittest.skipUnless(os.name == 'nt', 'Windows 공유 입력 검사입니다.')
    def test_shared_inputs_equal_file_results_and_remain_unchanged(self):
        """좌표·법선·면 공유 입력이 두 풀이에서 파일 입력과 완전히 같은 결과를 냅니다."""
        for method in ('POINT_TO_POINT', 'POINT_TO_PLANE'):
            target, normals = fixture(400)
            source = target @ rotation_xyz(.003, -.002, .001).T + [.002, -.003, .001]
            # 면 법선 경로도 별도 실행하여 두 배열 전달 방식의 결과를 비교합니다.
            for explicit_normals in (True, False):
                self.request['method'] = method
                triangles = np.arange(399, dtype=np.uint32).reshape(-1, 3)
                for role, positions in (('source', source), ('target', target)):
                    self.request[role] = self.cloud(role, positions,
                        normals=normals if explicit_normals else None,
                        triangles=None if explicit_normals else triangles)
                expected = self.run_job()
                self.assertEqual(expected['status'], 'SUCCESS', expected)
                memories = []
                for role in ('source', 'target'):
                    self.request[role], added = self.shared_cloud(self.request[role])
                    memories.extend(added)
                hashes = [hashlib.sha256(memory.buf).digest() for memory in memories]
                actual = self.run_job()
                for field in ('delta_4x4', 'iteration_stats', 'final_rms', 'final_objective_rms', 'status', 'termination_reason'):
                    self.assertEqual(actual[field], expected[field])
                self.assertEqual(hashes, [hashlib.sha256(memory.buf).digest() for memory in memories])

    @unittest.skipUnless(os.name == 'nt', 'Windows 공유 입력 검사입니다.')
    def test_invalid_shared_headers_and_descriptors_fail(self):
        """표식·길이·토큰·예약영역·이름·중복 설명자 오류를 실제 네이티브에서 거부합니다."""
        points, _ = fixture(100)
        target = self.cloud('target', points)
        self.request['target'] = target
        for corruption in ('magic', 'size', 'token', 'reserved', 'name', 'descriptor_size', 'duplicate', 'closed'):
            with self.subTest(corruption=corruption):
                source = self.cloud('source', points)
                entry, memories = self.shared_cloud(source)
                memory = memories[0]
                if corruption in ('magic', 'size', 'token', 'reserved'):
                    offset = {'magic': 0, 'size': 8, 'token': 16, 'reserved': 48}[corruption]
                    memory.buf[offset] ^= 1
                elif corruption == 'name':
                    entry['positions_mapping']['name'] = 'Global\\외부입력'
                elif corruption == 'descriptor_size':
                    entry['positions_mapping']['byte_count'] += 8
                elif corruption == 'duplicate':
                    entry['positions_file'] = source['positions_file']
                else:
                    memory.close()
                self.request['source'] = entry
                self.assertEqual(self.run_job()['status'], 'FAIL')

    @unittest.skipUnless(os.name == 'nt', 'Windows 공유 입력 검사입니다.')
    def test_truncated_shared_mapping_fails(self):
        """요청 배열보다 작은 실제 매핑의 경계를 넘는 읽기를 거부합니다."""
        points, _ = fixture(100)
        self.request['target'] = self.cloud('target', points)
        token = uuid4().hex
        memory = SharedMemory(name='Local\\ColmapPointKd_' + token, create=True, size=64)
        self.addCleanup(memory.close)
        size = 10000 * 24
        struct.pack_into('<8sQ32s16x', memory.buf, 0, b'CPKDIPC1', size, token.encode('ascii'))
        self.request['source'] = {'point_count': 10000, 'positions_mapping': {'name': memory.name, 'byte_count': size}}
        self.assertEqual(self.run_job()['status'], 'FAIL')

    def test_sim3_known_scale_rotation_translation_and_threads(self):
        """점대점·점평면의 알려진 배율 정답과 스레드 수 독립성을 원본 보존과 함께 검사합니다."""
        for method in ('POINT_TO_POINT', 'POINT_TO_PLANE'):
            for scale in (.96, 1.04):
                target, normals = fixture()
                rotation = rotation_xyz(.009, -.012, .008)
                source = scale * (target @ rotation.T) + [.012, -.008, .006]
                self.request.update(method=method,
                    source=self.cloud('source', source, normals @ rotation.T),
                    target=self.cloud('target', target, normals))
                self.request['parameters'].update(scale_mode='SIM3', max_threads=1, scale_tolerance=1e-6)
                result = self.run_job()
                self.assertEqual(result['status'], 'SUCCESS', result)
                self.assertEqual(result['termination_policy'], 'SIM3_RELATIVE_DELTA_V1')
                delta = np.asarray(result['delta_4x4'])
                error = np.max(np.linalg.norm(source @ delta[:3, :3].T + delta[:3, 3] - target, axis=1))
                self.assertLess(error, 2e-7)
                self.assertAlmostEqual(result['additional_scale'], 1 / scale, places=7)
                self.assertLess(result['iteration_stats'][-1]['log_scale_change'], 1e-6)
                self.assertEqual(result['target_kd_build_count'], 1)
                self.request['parameters']['max_threads'] = 4
                parallel = self.run_job()
                self.assertEqual(result['delta_4x4'], parallel['delta_4x4'])
                self.assertEqual(result['iteration_stats'], parallel['iteration_stats'])

    def test_sim3_large_origin_and_degenerate_plane(self):
        """큰 좌표의 정밀도 한계 검토를 유지하면서 정확한 배율과 평면 퇴화 실패를 검사합니다."""
        target, normals = fixture()
        origin = np.array([1e6, -2e6, 3e6])
        source = 1.02 * target + origin
        target = target + origin
        self.request.update(method='POINT_TO_PLANE',
            source=self.cloud('source', source, normals), target=self.cloud('target', target, normals))
        self.request['parameters']['scale_mode'] = 'SIM3'
        result = self.run_job()
        self.assertEqual(result['status'], 'REVIEW', result)
        self.assertEqual(result['termination_reason'], 'OBJECTIVE_INCREASE')
        self.assertGreater(result['rejected_update_objective']['after'], result['rejected_update_objective']['before'])
        delta = np.asarray(result['delta_4x4'])
        self.assertLess(np.max(np.linalg.norm(source @ delta[:3, :3].T + delta[:3, 3] - target, axis=1)), 2e-7)
        points = np.random.default_rng(18).normal(size=(100, 3))
        points[:, 2] = 0
        normals = np.tile([0., 0., 1.], (100, 1))
        self.request.update(source=self.cloud('source', points + [0, 0, .001], normals),
                            target=self.cloud('target', points, normals))
        result = self.run_job()
        self.assertEqual(result['status'], 'FAIL')
        self.assertIn('7자유도', result['message'])

    def test_sim3_scale_increment_prevents_premature_convergence(self):
        """오차 조건이 느슨해도 배율 증분이 큰 동안 성공 종료하지 않습니다."""
        target, normals = fixture()
        self.request.update(method='POINT_TO_PLANE', source=self.cloud('source', target * 1.04, normals),
                            target=self.cloud('target', target, normals))
        self.request['parameters'].update(scale_mode='SIM3', relative_rms_tolerance=1.0, scale_tolerance=1e-8)
        result = self.run_job()
        self.assertEqual(result['status'], 'SUCCESS', result)
        self.assertEqual(result['iteration_stats'][0]['convergence_branch'], 'SCALE_NOT_STABLE')
        self.assertLess(result['iteration_stats'][-1]['log_scale_change'], 1e-8)

    def known_transform(self, method: str, count: int = 1700, threads: int = 2) -> dict:
        """알려진 회전·이동의 역변환을 엄격한 좌표 오차로 검사합니다."""
        target, normals = fixture(count)
        rotation = rotation_xyz(0.009, -0.012, 0.008)
        translation = np.array([0.012, -0.008, 0.006])
        source = target @ rotation.T + translation
        self.request["source"] = self.cloud("source", source, normals @ rotation.T)
        self.request["target"] = self.cloud("target", target, normals)
        self.request["method"] = method
        self.request["parameters"]["max_threads"] = threads
        output = self.run_job()
        self.assertEqual(output["status"], "SUCCESS", output)
        delta = np.asarray(output["delta_4x4"])
        error = np.max(np.linalg.norm(source @ delta[:3, :3].T + delta[:3, 3] - target, axis=1))
        self.assertLess(error, 1e-8, (error, output))
        MEASUREMENTS.append({"method": method, "point_count": count, "threads": threads, "maximum_coordinate_error": float(error), "final_rms": output["final_rms"], "iterations": output["iterations"]})
        self.assertAlmostEqual(np.linalg.det(delta[:3, :3]), 1.0, places=12)
        self.assertEqual(output["target_kd_build_count"], 1)
        self.assertEqual(output["source_count"], len(source))
        self.assertEqual(output["target_count"], len(target))
        self.assertEqual(output["trimmed_count"], int(len(source) * 0.05))
        self.assertEqual(output["termination_reason"], "CONVERGED")
        self.assertEqual(output["iteration_stats"][-1]["previous_correspondence_count"], output["iteration_stats"][-1]["correspondence_count"])
        for update in output["iteration_stats"]:
            self.assertLessEqual(update["fixed_objective_after"], update["fixed_objective_before"] + 1e-22)
        return output

    def test_environment(self) -> None:
        """독립 프로토콜과 실제 자체검사 성공을 확인합니다."""
        output = json.loads(subprocess.check_output([str(WORKER), "--check-environment"], encoding="utf-8"))
        self.assertEqual(output["protocol_version"], "point-kd-native-v1")
        self.assertEqual(output["self_test"], "passed")
        self.assertEqual(output["search"], "EXACT_NEAREST_ELIGIBLE")

    def test_known_point_to_point(self) -> None:
        """점대점의 알려진 변환 정확도를 검사합니다."""
        self.known_transform("POINT_TO_POINT")

    def test_known_point_to_plane(self) -> None:
        """점평면의 알려진 변환과 6자유도 풀이를 검사합니다."""
        self.known_transform("POINT_TO_PLANE")

    def test_thread_determinism(self) -> None:
        """여러 고정 블록을 1·4 스레드에서 동일하게 합산하는지 검사합니다."""
        for method in ("POINT_TO_POINT", "POINT_TO_PLANE"):
            one = self.known_transform(method, count=8201, threads=1)
            many = self.known_transform(method, count=8201, threads=4)
            self.assertEqual(one["delta_4x4"], many["delta_4x4"])
            self.assertEqual(one["iteration_stats"], many["iteration_stats"])

    def test_large_coordinate_origin(self) -> None:
        """큰 공통 좌표 원점에서도 중심 정규화가 정확도를 유지하는지 검사합니다."""
        target, _ = fixture()
        target += [1e8, -2e8, 3e8]
        source = target + [0.01, -0.008, 0.006]
        self.request["source"] = self.cloud("source", source)
        self.request["target"] = self.cloud("target", target)
        output = self.run_job()
        self.assertEqual(output["status"], "SUCCESS", output)
        delta = np.asarray(output["delta_4x4"])
        self.assertLess(np.max(np.linalg.norm(source @ delta[:3, :3].T + delta[:3, 3] - target, axis=1)), 2e-7)

    def test_rank_deficient_plane_fails(self) -> None:
        """평면 하나의 관측 불가능한 자유도를 조용히 우회하지 않는지 검사합니다."""
        x, y = np.meshgrid(np.linspace(-1, 1, 18), np.linspace(-1, 1, 18))
        points = np.column_stack((x.ravel(), y.ravel(), np.zeros(x.size)))
        normals = np.tile([0.0, 0.0, 1.0], (len(points), 1))
        self.request["source"] = self.cloud("source", points + [0, 0, 0.01], normals)
        self.request["target"] = self.cloud("target", points, normals)
        self.request["method"] = "POINT_TO_PLANE"
        output = self.run_job()
        self.assertEqual(output["status"], "FAIL")
        self.assertIn("6자유도", output["message"])

    def test_missing_target_normals_fails(self) -> None:
        """점평면 대상 법선 누락을 점대점으로 우회하지 않는지 검사합니다."""
        points, _ = fixture(100)
        self.request["source"] = self.cloud("source", points)
        self.request["target"] = self.cloud("target", points)
        self.request["method"] = "POINT_TO_PLANE"
        self.assertEqual(self.run_job()["status"], "FAIL")

    def test_radius_has_no_silent_widening(self) -> None:
        """반경 바깥의 점을 자동 확장으로 대응시키지 않는지 검사합니다."""
        points, _ = fixture(100)
        self.request["source"] = self.cloud("source", points + [10, 0, 0])
        self.request["target"] = self.cloud("target", points)
        self.assertEqual(self.run_job()["status"], "FAIL")

    def test_correspondence_collapse_requires_review(self) -> None:
        """갱신 후 법선 경계를 넘어 대응 절반이 사라지면 검토로 중단하는지 검사합니다."""
        source, _ = fixture(400)
        rotation = rotation_xyz(0, 0, 0.02)
        target = source @ rotation.T
        source_normals = np.tile([1.0, 0, 0], (len(source), 1))
        target_normals = source_normals.copy()
        angle = np.deg2rad(-44.5)
        target_normals[:360] = [np.cos(angle), np.sin(angle), 0]
        self.request["source"] = self.cloud("source", source, source_normals)
        self.request["target"] = self.cloud("target", target, target_normals)
        self.request["parameters"]["maximum_correspondence_distance"] = 0.05
        output = self.run_job()
        self.assertEqual(output["status"], "REVIEW", output)
        self.assertEqual(output["termination_reason"], "CORRESPONDENCE_COLLAPSE")
        last = output["iteration_stats"][-1]
        self.assertLessEqual(last["correspondence_count"], last["previous_correspondence_count"] // 2)

    def test_plane_objective_rms_separate_from_euclidean_rms(self) -> None:
        """접선 방향 오차가 남아도 수렴 변화량은 점평면 목적함수로 계산하는지 검사합니다."""
        target, normals = fixture(600)
        tangent = np.cross(normals, np.tile([0.0, 0, 1.0], (len(target), 1)))
        tangent /= np.linalg.norm(tangent, axis=1)[:, None]
        source = target + tangent * 0.001
        self.request["source"] = self.cloud("source", source)
        self.request["target"] = self.cloud("target", target, normals)
        self.request["method"] = "POINT_TO_PLANE"
        output = self.run_job()
        self.assertEqual(output["status"], "SUCCESS", output)
        self.assertGreater(output["final_rms"], 0.0005)
        self.assertLess(output["final_objective_rms"], 1e-12)
        self.assertEqual(output["point_to_plane_rms"], output["final_objective_rms"])
        first = output["iteration_stats"][0]
        expected = abs(output["initial_objective_rms"] - first["objective_rms"]) / (output["initial_objective_rms"] + 1e-9)
        self.assertAlmostEqual(first["relative_rms_change"], expected, places=12)
        self.assertEqual(output["termination_policy"], "ARTEC_RELATIVE_DELTA_V1")

    def test_error_branch_accepts_changed_correspondence_count(self) -> None:
        """대응 수가 달라도 오차 조건 하나로 수렴하며 변환 조건을 평가하지 않는지 검사합니다."""
        source, _ = fixture(400)
        target = source @ rotation_xyz(0, 0, 0.02).T
        source_normals = np.tile([1.0, 0, 0], (len(source), 1))
        target_normals = source_normals.copy()
        angle = np.deg2rad(-44.5)
        target_normals[:80] = [np.cos(angle), np.sin(angle), 0]
        self.request["source"] = self.cloud("source", source, source_normals)
        self.request["target"] = self.cloud("target", target, target_normals)
        self.request["parameters"].update(maximum_correspondence_distance=0.05,
            relative_rms_tolerance=1, translation_tolerance=0, rotation_tolerance=0)
        output = self.run_job()
        self.assertEqual(output["status"], "SUCCESS", output)
        self.assertEqual(output["iterations"], 1)
        last = output["iteration_stats"][-1]
        self.assertNotEqual(last["previous_correspondence_count"], last["correspondence_count"])
        self.assertEqual(last["convergence_branch"], "ERROR_STABLE")
        self.assertIsNone(last["relative_translation_change"])
        self.assertIsNone(last["relative_rotation_change"])

    def test_iteration_limit_precedes_convergence(self) -> None:
        """반복 상한에서는 오차가 영이어도 수렴 검사보다 상한 종료를 우선하는지 검사합니다."""
        points, _ = fixture(100)
        self.request["source"] = self.cloud("source", points)
        self.request["target"] = self.cloud("target", points)
        self.request["parameters"]["max_iterations"] = 1
        output = self.run_job()
        self.assertEqual(output["termination_reason"], "MAX_ITERATIONS")
        self.assertEqual(output["status"], "REVIEW")
        self.assertEqual(output["iteration_stats"][-1]["convergence_branch"], "NOT_CHECKED_MAX_ITERATIONS")

    def test_default_radius_bound(self) -> None:
        """희소 입력의 자동 반경이 대각선 상한을 넘으면 명시적으로 실패하는지 검사합니다."""
        points, _ = fixture(20)
        self.request["parameters"].pop("maximum_correspondence_distance")
        self.request["source"] = self.cloud("source", points)
        self.request["target"] = self.cloud("target", points)
        output = self.run_job()
        self.assertEqual(output["status"], "FAIL")
        self.assertIn("5%", output["message"])

    def test_nonfinite_input_rejected_without_modification(self) -> None:
        """비유한 원본점을 입력 실패로 거부하고 바이너리를 보존하는지 검사합니다."""
        points, _ = fixture(400)
        source = np.vstack((points, [[np.nan, 0, 0]]))
        self.request["source"] = self.cloud("source", source)
        self.request["target"] = self.cloud("target", points)
        output = self.run_job()
        self.assertEqual(output["status"], "FAIL", output)
        self.assertIn("비유한 좌표", output["message"])

    def test_normal_gate_searches_next_eligible(self) -> None:
        """더 가까운 부적격 표면을 건너뛰어 적격 원본점을 찾는지 검사합니다."""
        target, normals = fixture(900)
        source = target + [0.002, -0.001, 0.001]
        # 오답 위치는 더 가깝지만 수직 법선을 주어 정확 검색에서 제외합니다.
        perpendicular = np.cross(normals, np.tile([1.0, 0, 0], (len(normals), 1)))
        perpendicular /= np.linalg.norm(perpendicular, axis=1)[:, None]
        combined = np.vstack((source, target))
        combined_normals = np.vstack((perpendicular, normals))
        self.request["source"] = self.cloud("source", source, normals)
        self.request["target"] = self.cloud("target", combined, combined_normals)
        output = self.run_job()
        self.assertEqual(output["status"], "SUCCESS", output)
        self.assertLess(output["final_rms"], 1e-10)
        self.assertGreater(output["initial_rms"], 1e-4)
        self.assertEqual(output["normal_policy"], "BOTH_VALID_ABS_DOT")

    def test_reversed_normals_allowed(self) -> None:
        """법선 부호가 반대여도 절대 내적 조건을 적용하는지 검사합니다."""
        points, normals = fixture(100)
        self.request["source"] = self.cloud("source", points, normals)
        self.request["target"] = self.cloud("target", points, -normals)
        self.assertEqual(self.run_job()["status"], "SUCCESS")

    def test_preexisting_cancel(self) -> None:
        """입력 적재 전 취소를 정상 취소 결과로 반환하는지 검사합니다."""
        points, _ = fixture(100)
        self.request["source"] = self.cloud("source", points)
        self.request["target"] = self.cloud("target", points)
        (self.directory / "cancel").touch()
        self.assertEqual(self.run_job()["status"], "CANCELLED")

    def test_binary_length_rejected(self) -> None:
        """원본점 수와 바이너리 길이 불일치를 거부하는지 검사합니다."""
        points, _ = fixture(100)
        self.request["source"] = self.cloud("source", points)
        self.request["target"] = self.cloud("target", points)
        self.request["source"]["point_count"] += 1
        self.assertEqual(self.run_job()["status"], "FAIL")

    def test_external_path_rejected(self) -> None:
        """작업 폴더 밖의 원본 파일을 읽지 않는지 검사합니다."""
        points, _ = fixture(100)
        self.request["source"] = self.cloud("source", points)
        self.request["target"] = self.cloud("target", points)
        self.request["source"]["positions_file"] = "../외부.bin"
        output = self.run_job()
        self.assertEqual(output["status"], "FAIL")
        self.assertIn("외부", output["message"])

    def test_transformed_triangle_normals(self) -> None:
        """비균일 변환 후 삼각형 면적가중 법선을 명시한 법선 입력과 대조합니다."""
        longitude, latitude = 20, 12
        points = []
        triangles = []
        for row in range(latitude):
            theta = (row + 0.5) * np.pi / latitude
            for column in range(longitude):
                phi = column * 2 * np.pi / longitude
                points.append([np.sin(theta) * np.cos(phi), np.sin(theta) * np.sin(phi), np.cos(theta)])
                if row + 1 < latitude:
                    a, b = row * longitude + column, row * longitude + (column + 1) % longitude
                    triangles.extend([[a, b, a + longitude], [b, b + longitude, a + longitude]])
        target = np.asarray(points) * [2.3, 0.7, 1.4] + [3.0, -2.0, 4.0]
        triangles = np.asarray(triangles, dtype=np.uint32)
        rotation = rotation_xyz(0.002, -0.003, 0.001)
        source = target @ rotation.T + [0.002, -0.003, 0.001]
        self.request["method"] = "POINT_TO_PLANE"
        self.request["source"] = self.cloud("source", source, triangles=triangles)
        self.request["target"] = self.cloud("target", target, triangles=triangles)
        generated = self.run_job()
        self.assertEqual(generated["status"], "SUCCESS", generated)
        self.assertEqual(generated["normal_generation_policy"], "TRANSFORMED_SURFACE_AREA_WEIGHTED")
        self.assertEqual(generated["valid_normal_count"], {"source": len(source), "target": len(target)})
        for name, vertices in (("source", source), ("target", target)):
            normals = np.zeros_like(vertices)
            for a, b, c in triangles:
                normal = np.cross(vertices[b] - vertices[a], vertices[c] - vertices[a])
                normals[a] += normal
                normals[b] += normal
                normals[c] += normal
            normals /= np.linalg.norm(normals, axis=1)[:, None]
            self.request[name] = self.cloud(name, vertices, normals=normals)
        explicit = self.run_job()
        self.assertEqual(explicit["status"], "SUCCESS", explicit)
        np.testing.assert_allclose(generated["delta_4x4"], explicit["delta_4x4"], atol=1e-12, rtol=0)
        self.assertEqual(len(generated["iteration_stats"]), len(explicit["iteration_stats"]))
        for first, second in zip(generated["iteration_stats"], explicit["iteration_stats"]):
            self.assertAlmostEqual(first["fixed_objective_before"], second["fixed_objective_before"], delta=1e-14)

    def test_cancel_during_kd_build(self) -> None:
        """실행 중인 KD 생성이 취소 요청 후 2초 안에 종료되는지 검사합니다."""
        rng = np.random.default_rng(18419)
        target = rng.normal(size=(500_000, 3))
        self.request["source"] = self.cloud("source", target[:1000])
        self.request["target"] = self.cloud("target", target)
        path = self.directory / "request.json"
        path.write_text(json.dumps(self.request), encoding="utf-8")
        process = subprocess.Popen([str(WORKER), "--request", str(path)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        deadline = time.monotonic() + 10
        observed = False
        try:
            while process.poll() is None and time.monotonic() < deadline:
                try:
                    progress = json.loads((self.directory / "progress.json").read_text(encoding="utf-8"))
                    if progress["stage"] == "KD_BUILD":
                        observed = True
                        break
                except (FileNotFoundError, json.JSONDecodeError, PermissionError):
                    pass
                time.sleep(0.001)
            self.assertTrue(observed, "취소할 KD 생성 단계를 관측하지 못했습니다.")
            started = time.monotonic()
            (self.directory / "cancel").touch()
            stdout, stderr = process.communicate(timeout=2)
            latency = time.monotonic() - started
            self.assertLess(latency, 2)
            self.assertEqual(process.returncode, 0, stderr.decode("utf-8"))
            output = json.loads(stdout.decode("utf-8"))
            self.assertEqual(output["status"], "CANCELLED", output)
            MEASUREMENTS.append({"stage": "KD_BUILD", "cancel_latency_seconds": latency})
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()

    def test_output_input_collision_does_not_overwrite(self) -> None:
        """출력 경로가 원본 입력과 같으면 원본을 덮어쓰지 않는지 검사합니다."""
        points, _ = fixture(100)
        self.request["source"] = self.cloud("source", points)
        self.request["target"] = self.cloud("target", points)
        self.request["result_file"] = self.request["source"]["positions_file"]
        input_path = self.directory / self.request["result_file"]
        before = input_path.read_bytes()
        request_path = self.directory / "request.json"
        request_path.write_text(json.dumps(self.request), encoding="utf-8")
        process = subprocess.run([str(WORKER), "--request", str(request_path)], capture_output=True, encoding="utf-8", timeout=10)
        self.assertEqual(process.returncode, 2)
        self.assertEqual(json.loads(process.stdout)["status"], "FAIL")
        self.assertEqual(before, input_path.read_bytes())

    @unittest.skipUnless(os.name == "nt", "Windows 파일 공유 검사입니다.")
    def test_transient_progress_reader_lock(self) -> None:
        """진행 파일의 짧은 읽기 잠금이 해제되면 원자 교체를 재시도하는지 검사합니다."""
        import ctypes
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateFileW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
        kernel.CreateFileW.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        points, _ = fixture(300)
        self.request["source"] = self.cloud("source", points)
        self.request["target"] = self.cloud("target", points)
        request_path = self.directory / "request.json"
        request_path.write_text(json.dumps(self.request), encoding="utf-8")
        progress_path = self.directory / "progress.json"
        progress_path.write_text("{}", encoding="utf-8")
        handle = kernel.CreateFileW(str(progress_path), 0x80000000, 0x00000003, None, 3, 0x80, None)
        self.assertNotEqual(handle, ctypes.c_void_p(-1).value)
        process = subprocess.Popen([str(WORKER), "--request", str(request_path)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            time.sleep(0.08)
        finally:
            kernel.CloseHandle(handle)
        try:
            stdout, stderr = process.communicate(timeout=10)
            self.assertEqual(process.returncode, 0, stderr.decode("utf-8"))
            output = json.loads(stdout.decode("utf-8"))
            self.assertEqual(output["status"], "SUCCESS", output)
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="독립 점 KD 워커 검증")
    parser.add_argument("--worker", type=Path)
    parser.add_argument("--report", type=Path)
    args, remaining = parser.parse_known_args()
    if args.worker:
        WORKER = args.worker.resolve()
        PointKdWorkerTests.__unittest_skip__ = not WORKER.is_file()
    REPORT = args.report
    started = time.monotonic()
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(PointKdWorkerTests)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if REPORT:
        REPORT.parent.mkdir(parents=True, exist_ok=True)
        REPORT.write_text(json.dumps({"worker": str(WORKER), "tests": result.testsRun, "failures": len(result.failures), "errors": len(result.errors), "skipped": len(result.skipped), "success": result.wasSuccessful(), "duration_seconds": time.monotonic() - started, "measurements": MEASUREMENTS}, ensure_ascii=False, indent=2), encoding="utf-8")
    raise SystemExit(0 if result.wasSuccessful() else 1)
