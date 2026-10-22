"""독립 표면 평가의 고정 분모·표본 분포·좌표 변환과 실제 BVH 거리 계약을 확인합니다."""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

import numpy as np


sys.path.insert(0, str(Path(__file__).resolve().parent))
import evaluate_point_kd_comparison as evaluation


def _provenance():
    """같은 원본 내용과 축을 가진 두 역할의 최소 감사 기록을 만듭니다."""
    sources = []
    for owner in ("이동", "기준"):
        sources.append({"owner_model_id": owner, "geometry_kind": "TRIANGLE_MESH", "geometry_source": "MAIN",
            "geometry_revision": 1, "source_path": owner + ".obj", "source_fingerprint": "",
            "positions_fingerprint": owner + "좌표", "triangles_fingerprint": owner + "면",
            "point_count": 3, "triangle_count": 1, "display_transform_fingerprint": "동일 축",
            "display_transform_float64": np.eye(4).tolist()})
    return {"moving_model_id": "이동", "reference_model_ids": ["기준"], "moving": sources[0], "references": [sources[1]]}


class IndependentPointKdComparisonTests(unittest.TestCase):
    """등록 지표에 의존하지 않는 동일 원본 평가의 수학적 불변 조건을 검사합니다."""

    def test_surface_sampling_is_deterministic_area_weighted_and_barycentric(self):
        """면적이 네 배인 면의 표본 비율과 면 내부 균일 무게를 고정 시드로 확인합니다."""
        positions = np.asarray(((0, 0, 0), (1, 0, 0), (0, 1, 0),
                                (10, 0, 0), (12, 0, 0), (10, 2, 0)), dtype=np.float64)
        triangles = np.asarray(((0, 1, 2), (3, 4, 5)), dtype=np.uint32)
        first = evaluation.sample_surface(positions, triangles, 30_000, 104729)
        second = evaluation.sample_surface(positions, triangles, 30_000, 104729)
        for left, right in zip(first, second):
            np.testing.assert_array_equal(left, right)
        self.assertAlmostEqual(float(np.mean(first[1] == 1)), .8, delta=.012)
        np.testing.assert_allclose(first[2].mean(axis=0), np.full(3, 1 / 3), atol=.006)
        np.testing.assert_allclose(first[2].sum(axis=1), 1., atol=1e-15)
        reconstructed = np.sum(positions[triangles[first[1]]] * first[2][:, :, None], axis=1)
        np.testing.assert_array_equal(first[0], reconstructed)
        self.assertTrue(np.all(first[2] >= 0))

    def test_reversed_candidate_is_an_inverse_of_the_original_moving_final_matrix(self):
        """역할 반전 후보를 원래 이동 메시의 최종 세계 행렬로 정확히 역변환합니다."""
        final = np.eye(4)
        final[:3, :3] *= 1.02
        final[:3, 3] = (1., 2., 3.)
        matrix, _formula = evaluation.common_original_moving_matrix(final, "기준", ("이동",), _provenance())
        np.testing.assert_allclose(matrix @ final, np.eye(4), atol=1e-14)
        direct, _formula = evaluation.common_original_moving_matrix(final, "이동", ("기준",), _provenance())
        np.testing.assert_array_equal(direct, final)
        with self.assertRaisesRegex(ValueError, "다른 소유자"):
            evaluation.common_original_moving_matrix(final, "기준", ("다른 메시",), _provenance())

    def test_normalized_world_transform_is_a_conjugation_without_reapplying_initial_alignment(self):
        """큰 세계 원점을 가진 최종 행렬도 정규화 전후의 점 변환이 일치합니다."""
        normalization = evaluation.solver.Normalization(np.asarray((3000., -2000., 500.)), 835.)
        matrix = np.eye(4)
        angle = np.deg2rad(20.)
        matrix[:3, :3] = 1.02 * np.asarray(((np.cos(angle), -np.sin(angle), 0),
                                           (np.sin(angle), np.cos(angle), 0), (0, 0, 1)))
        matrix[:3, 3] = (.6, -.4, .2)
        points = np.asarray(((3001., -2002., 503.), (3002., -2001., 501.)))
        native = evaluation.normalized_matrix(matrix, normalization)
        expected = normalization.apply(evaluation.solver.transform_positions(points, matrix))
        actual = evaluation.solver.transform_positions(normalization.apply(points), native)
        np.testing.assert_allclose(actual, expected, rtol=0, atol=1e-15)

    def test_fixed_subset_keeps_points_that_move_outside_the_acceptance_radius(self):
        """나빠진 후보가 초기 겹침점을 반경 밖으로 밀어도 평가 분모에서 삭제되지 않습니다."""
        points = np.asarray(((0., 0., 0.), (1., 0., 0.), (0., 1., 0.), (1., 1., 0.)))
        samples = {name: points for name in evaluation.DIRECTIONS}
        masks = {name: np.asarray((True, True, True, False)) for name in evaluation.DIRECTIONS}
        baseline = {name: np.asarray((.1, .2, .3, 100.)) for name in evaluation.DIRECTIONS}
        candidate = {name: np.asarray((10., 20., 30., .1)) for name in evaluation.DIRECTIONS}
        before = evaluation.summarize_distances(baseline, samples, masks, 1.)
        after = evaluation.summarize_distances(candidate, samples, masks, 1.)
        self.assertEqual(before["fixed_overlap"]["count"], 6)
        self.assertEqual(after["fixed_overlap"]["count"], 6)
        self.assertAlmostEqual(after["fixed_overlap"]["rms"], before["fixed_overlap"]["rms"] * 100.)
        self.assertEqual(after["directions"]["moving_to_reference"]["currently_within_radius_count_reference_only"], 1)
        self.assertFalse(evaluation.accuracy_gate(after, before, 1.)["passed"])
        self.assertEqual(sum(item["fixed_inlier_count"] for item in after["directions"]["moving_to_reference"]["spatial_distribution"]), 3)

    def test_accuracy_gate_checks_rms_and_p95_in_each_direction(self):
        """전체·각 방향의 RMS와 95백분위 중 어느 하나라도 5퍼센트를 넘으면 거부합니다."""
        reference = {"fixed_overlap": {"rms": 1., "p95": 2.}, "directions": {
            name: {"fixed_overlap": {"rms": 1., "p95": 2.}} for name in evaluation.DIRECTIONS}}
        self.assertTrue(evaluation.accuracy_gate(reference, reference, 100.)["passed"])
        for direction in (None, *evaluation.DIRECTIONS):
            for metric in ("rms", "p95"):
                candidate = deepcopy(reference)
                scope = candidate if direction is None else candidate["directions"][direction]
                scope["fixed_overlap"][metric] *= 1.051
                self.assertFalse(evaluation.accuracy_gate(candidate, reference, 100.)["passed"])

    def test_provenance_accepts_role_change_and_rejects_content_or_axis_change(self):
        """역할 교환은 허용하면서 원본 내용·축·슬롯이 바뀐 후보의 비교를 거부합니다."""
        provenance = _provenance()
        reversed_provenance = deepcopy(provenance)
        reversed_provenance["moving"], reversed_provenance["references"] = provenance["references"][0], [provenance["moving"]]
        evaluation.verify_source_provenance(reversed_provenance, provenance)
        for field in ("positions_fingerprint", "triangles_fingerprint", "geometry_source", "display_transform_fingerprint"):
            changed = deepcopy(provenance)
            changed["moving"][field] = "변경"
            with self.assertRaisesRegex(ValueError, "원본 내용"):
                evaluation.verify_source_provenance(changed, provenance)

    @unittest.skipUnless((evaluation.ROOT / ".cccore-runtime/cccore/colmap_cccore_worker.exe").is_file(), "실제 네이티브 평가기가 필요합니다.")
    def test_native_bvh_evaluates_both_directions_and_restores_uniform_scale(self):
        """실제 삼각면 거리기로 양방향의 알려진 평면 간격과 역질의 배율 복원을 검증합니다."""
        vertices = np.asarray(((0., 0., 0.), (4., 0., 0.), (0., 4., 0.)))
        triangles = np.asarray(((0, 1, 2),), dtype=np.uint32)
        mesh = evaluation.contract.CcCoreGeometry("TRIANGLE_MESH", vertices, triangles, {})
        points = np.asarray(((.2, .2, 0.), (.4, .2, 0.), (.2, .4, 0.)))
        samples = {name: points for name in evaluation.DIRECTIONS}
        matrix = np.diag((2., 2., 2., 1.))
        matrix[2, 3] = 2.
        parameters = evaluation.contract.resolve_parameters({"cc_max_threads": 2})
        worker = (evaluation.ROOT / ".cccore-runtime/cccore/colmap_cccore_worker.exe").resolve()
        with TemporaryDirectory(prefix="comparison-native-test-") as temporary:
            with evaluation.solver.NativeRunner(Path(temporary), worker, parameters) as runner:
                distances = evaluation.evaluate_native_distances(runner, "analytic", matrix,
                    {"moving": mesh, "reference": mesh}, samples, evaluation.solver.Normalization(np.zeros(3), 1.))
                for values in distances.values():
                    np.testing.assert_allclose(values, np.full(3, 2.), rtol=0, atol=1e-6)
                self.assertEqual([item["operation"] for item in runner.physical_native_calls], ["distance", "distance"])
            self.assertEqual(runner.session_statistics["bvh_build_count"], 2)

    @unittest.skipUnless((evaluation.ROOT / ".cccore-runtime/cccore/colmap_cccore_worker.exe").is_file(), "실제 네이티브 평가기가 필요합니다.")
    def test_complete_report_keeps_samples_and_separates_registration_metrics(self):
        """작은 실제 메시로 파일 입력부터 원본 보존·후보 개선·표본 저장까지 전체 경로를 검증합니다."""
        with TemporaryDirectory(prefix="comparison-report-test-") as temporary:
            directory = Path(temporary)
            provenance = _provenance()
            for source in (provenance["moving"], *provenance["references"]):
                source["applied_alignment_float64"] = np.eye(4).tolist()
            manifest = {"initial_alignment_4x4": np.eye(4).tolist(), "provenance": provenance}
            vertices = np.asarray(((0., 0., 0.), (4., 0., 0.), (0., 4., 0.)))
            for role, offset in (("moving", .01), ("reference", 0.)):
                points = vertices.copy()
                points[:, 2] += offset
                points.astype("<f8").tofile(directory / (role + "-positions.bin"))
                np.asarray(((0, 1, 2),), dtype="<u4").tofile(directory / (role + "-triangles.bin"))
                manifest[role] = {"kind": "TRIANGLE_MESH", "point_count": 3, "triangle_count": 1,
                    "positions_file": role + "-positions.bin", "triangles_file": role + "-triangles.bin"}
            (directory / "geometry.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
            raw = {"input_fingerprint": "same-input", "geometry_provenance": provenance, "metric_kind": "기존 등록 지표"}
            (directory / "raw.json").write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
            observed = {"result": {"input_fingerprint": "same-input", "moving_model_id": "이동", "reference_model_ids": ["기준"],
                                   "final_alignment": {"matrix_float64": np.eye(4).tolist()}}}
            (directory / "observed.json").write_text(json.dumps(observed, ensure_ascii=False), encoding="utf-8")
            final = np.eye(4)
            final[2, 3] = -.01
            candidate = {"completed": True, "scenario": "aligned", "moving_model_id": "이동", "reference_model_id": "기준",
                "runs": [{"backend": "POINT_KD", "final_alignment": final.tolist(), "warmup": False,
                          "metadata": {"geometry_provenance": provenance, "initial_rms": 99., "final_rms": 88.}}]}
            (directory / "candidate.json").write_text(json.dumps(candidate, ensure_ascii=False), encoding="utf-8")
            options = SimpleNamespace(output=directory / "evaluation", geometry=directory / "geometry.json",
                existing_raw=directory / "raw.json", existing_observed=directory / "observed.json",
                candidate=[directory / "candidate.json"], sample_count=24, threads=2,
                worker=(evaluation.ROOT / ".cccore-runtime/cccore/colmap_cccore_worker.exe").resolve())
            report = evaluation.run(options)
            self.assertTrue(report["completed"])
            self.assertTrue(report["source_files_unchanged"])
            self.assertAlmostEqual(report["baseline"]["fixed_overlap"]["rms"], .01, delta=1e-7)
            chosen = report["candidates"][1]
            self.assertLess(chosen["independent_metrics"]["fixed_overlap"]["rms"], 1e-6)
            self.assertEqual(chosen["registration_metrics"]["final_rms"], 88.)
            self.assertTrue(chosen["accuracy_gate_against_existing_cc"]["passed"])
            self.assertTrue((options.output / "report.json").is_file())
            with np.load(options.output / "fixed-evaluation-data.npz", allow_pickle=False) as archive:
                for direction in evaluation.DIRECTIONS:
                    self.assertEqual(int(archive[direction + "_fixed_inlier_mask"].sum()), 24)
                    self.assertEqual(archive[direction + "_points"].shape, (24, 3))
            self.assertEqual(list(options.output.glob("native-evaluation-*")), [])


if __name__ == "__main__":
    unittest.main()
