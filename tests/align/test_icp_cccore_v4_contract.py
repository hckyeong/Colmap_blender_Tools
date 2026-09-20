"""후보 선별과 등록 전 겹침 추정의 v4 파일 계약을 검증합니다."""

import copy
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools/align/alignment"))
import icp_cccore_contract as contract


def operation_summaries(mode, preparing_count=2):
    """실제 세션 계측을 가진 후보 준비·등록·평가 연산 목록을 만듭니다."""
    sequence = [("distance", "PREPARING")] * preparing_count + list(zip(
        contract.CC_NATIVE_OPERATION_SEQUENCES[mode], contract.CC_NATIVE_OPERATION_STAGES[mode],
    ))
    summaries = []
    for index, (operation, stage) in enumerate(sequence, 1):
        summaries.append({
            "operation": operation, "stage": stage, "index": index, "sequence": index,
            "physical_sequence": index, "source_sequence": index, "source_logical_index": index,
            "execution_source": "native_session",
            "evaluation_key_sha256": "a" * 64 if stage in {
                "BASELINE", "RIGID_EVALUATION", "REFINE_EVALUATION",
            } else "",
            "elapsed_seconds": 0.1, "roundtrip_seconds": 0.125, "protocol_overhead_seconds": 0.025,
            "requested_max_threads": 1, "effective_max_threads": 1,
            "data_geometry_id": f"data-{index}", "model_geometry_id": "model",
            "timings": {name: 0.0 for name in contract.CC_NATIVE_TIMING_FIELDS},
            **{name: 0 for name in contract.CC_NATIVE_COUNTER_FIELDS},
        })
    return summaries


class V4ContractTest(unittest.TestCase):
    """제한된 준비 접두부와 고정 후속 단계의 변조를 거부하는지 확인합니다."""

    def validate(self, summaries, mode="POINT_TO_POINT"):
        """단일 스레드 작업의 논리 연산 요약을 검증합니다."""
        return contract.validate_native_call_summaries(
            summaries, geometry_mode=mode, requested_max_threads=1, effective_max_threads=1,
        )

    def test_fixed_domain_rejects_nonfinite_and_malformed_bounds(self):
        """유한하지 않은 수치와 잘못된 경계 구조를 직렬화 전에 거부합니다."""
        valid = {"domain_id": "domain", "min": [-1, 0, 0], "max": [1, 0, 1]}
        self.assertIsNone(contract.validate_native_search_domain(None))
        self.assertEqual(contract.validate_native_search_domain(valid), valid)
        for value in (float("nan"), float("inf"), -float("inf"), True, "1"):
            changed = copy.deepcopy(valid)
            changed["max"][0] = value
            with self.subTest(value=value), self.assertRaises(contract.CcCoreInputError):
                contract.validate_native_search_domain(changed)

    def test_fixed_domain_identifier_matches_native_byte_contract(self):
        """식별자의 UTF-8 바이트 상한과 공백·제어문자·잘못된 유니코드를 검사합니다."""
        domain = {"domain_id": "a" * 128, "min": [0, 0, 0], "max": [1, 1, 1]}
        for identifier in ("a" * 128, "한" * 42, "범위-식별자"):
            self.assertEqual(contract.validate_native_search_domain({**domain, "domain_id": identifier})["domain_id"], identifier)
        for identifier in ("a" * 129, "한" * 43, "", "   ", "\t", "a\x00b", "a\x1fb", "a\x7fb", "\ud800"):
            with self.subTest(identifier=repr(identifier)), self.assertRaises(contract.CcCoreInputError):
                contract.validate_native_search_domain({**domain, "domain_id": identifier})

    def test_fixed_domain_coordinates_match_native_float32_range(self):
        """단정도 최대값 경계는 허용하고 초과 좌표와 매우 큰 정수는 거부합니다."""
        limit = contract.CC_NATIVE_COORDINATE_MAX
        valid = {"domain_id": "domain", "min": [-limit, 0, 0], "max": [limit, 1, 1]}
        self.assertEqual(contract.validate_native_search_domain(valid), valid)
        for name, value in (("min", -limit * 2), ("max", limit * 2), ("max", 10 ** 1000)):
            changed = copy.deepcopy(valid)
            changed[name][0] = value
            with self.subTest(name=name, value=value), self.assertRaises(contract.CcCoreInputError):
                contract.validate_native_search_domain(changed)

    def test_surface_budget_counts_each_stream_and_spacing(self):
        """양방향 저합격률·마지막 묶음·선행 간격 조회를 모두 예산에 넣습니다."""
        directions = (("TRIANGLE_MESH", 11303380, 50000, 30000),
                      ("TRIANGLE_MESH", 11303380, 0, 30000))
        self.assertEqual(contract.surface_preparing_operation_limit(directions, consumed_operations=2),
                         2 + 32 + 1221 + 733 + 733)
        self.assertEqual(contract.surface_preparing_operation_limit((("TRIANGLE_MESH", 3, 3, 3),)), 18)
        self.assertEqual(contract.surface_preparing_operation_limit((("COLMAP_POINT_CLOUD", 8193, 3, 3),),
                                                                   consumed_operations=2), 8)

    def test_fixed_domain_identity_tracks_geometry_transform_and_normalization(self):
        """같은 경계에서도 원본 지문·초기행렬·정규화 변경에 따라 격자 식별자가 바뀝니다."""
        import numpy as np
        import icp_cccore_solver as solver

        positions = np.array([[0., 0., 0.], [1., 0., 0.], [0., 1., 0.]])
        triangles = np.array([[0, 1, 2]], dtype=np.uint32)
        provenance = {"sources": [{"positions_fingerprint": "a" * 64,
                                   "triangles_fingerprint": "b" * 64,
                                   "applied_alignment_float64": np.eye(4).tolist()}]}
        moving = contract.CcCoreGeometry("TRIANGLE_MESH", positions, triangles, provenance)
        reference = contract.CcCoreGeometry("TRIANGLE_MESH", positions, triangles, {"sources": []})
        normalization = {"origin_project_world": [0., 0., 0.], "unit_project_world": 1.}
        baseline = solver.make_preparing_search_domain(moving, reference, normalization=normalization)
        self.assertEqual(baseline, solver.make_preparing_search_domain(moving, reference, normalization=normalization))
        for field in ("positions_fingerprint", "triangles_fingerprint", "applied_alignment_float64"):
            changed = copy.deepcopy(provenance)
            if "fingerprint" in field:
                changed["sources"][0][field] = "c" * 64
            else:
                changed["sources"][0][field][0][3] = 0.5
            geometry = contract.CcCoreGeometry(moving.kind, positions, triangles, changed)
            domain = solver.make_preparing_search_domain(geometry, reference, normalization=normalization)
            self.assertNotEqual(domain["domain_id"], baseline["domain_id"])
            self.assertEqual(domain["min"], baseline["min"])
            self.assertEqual(domain["max"], baseline["max"])
        for changed in ({**normalization, "unit_project_world": 2.},
                        {**normalization, "origin_project_world": [1., 0., 0.]}):
            self.assertNotEqual(solver.make_preparing_search_domain(moving, reference, normalization=changed)["domain_id"],
                                baseline["domain_id"])

    def test_prior_native_build_metadata_restores_without_current_runtime(self):
        """과거 네이티브 버전과 새 패치가 없는 승인 이력을 원래 메타데이터 그대로 복원합니다."""
        sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
        try:
            from tools.align.alignment.types import AlignmentProvenanceRecord
            from tools.align.project_persistence.codec import _decode_provenance
        finally:
            sys.path.pop(0)
        for version in ("cccore-native-v2", "cccore-native-v3", "cccore-native-v4"):
            metadata = {"native_build_identity": {"protocol_version": version,
                        "core_patch": "mesh-grid-reuse-v1", "native_worker_sha256": "a" * 64},
                        "quality": "PASS"}
            original = AlignmentProvenanceRecord(
                model_id="moving", method="ICP", backend_id="CCCORELIB",
                algorithm_version="cccore-icp-v5", icp_status="PASS",
                engine_metadata_json=json.dumps(metadata),
            )
            for schema in (7, 8):
                with self.subTest(version=version, schema=schema):
                    restored = _decode_provenance(original.to_mapping(), "moving", schema)
                    self.assertEqual(restored, original)
                    self.assertEqual(json.loads(restored.engine_metadata_json), metadata)

    def test_surface_budget_rejects_native_sequence_overflow(self):
        """후속 열 번과 종료 한 번을 남길 수 없는 준비 예산을 거부합니다."""
        directions = (("TRIANGLE_MESH", 3, 50000, 30000),)
        required = contract.surface_preparing_operation_limit(directions)
        self.assertEqual(contract.surface_preparing_operation_limit(
            directions, consumed_operations=contract.CC_MAX_PREPARING_OPERATIONS - required), 9988)
        with self.assertRaises(contract.CcCoreInputError):
            contract.surface_preparing_operation_limit(
                directions, consumed_operations=contract.CC_MAX_PREPARING_OPERATIONS - required + 1)
        for arguments in (((), 8192), ((("UNKNOWN", 3, 3, 3),), 8192), (directions, 0)):
            with self.subTest(arguments=arguments), self.assertRaises(contract.CcCoreInputError):
                contract.surface_preparing_operation_limit(*arguments)

    def test_surface_policy_preserves_sampling_with_native_v5(self):
        """표면 정책과 세 독립 시드를 고정하고 준비 범위 통신 버전을 확인합니다."""
        parameters = contract.resolve_parameters({})
        self.assertEqual(contract.CC_ALGORITHM_VERSION, "cccore-icp-v5")
        self.assertEqual(contract.CC_JOB_PROTOCOL_VERSION, "cccore-job-v4")
        self.assertEqual(contract.CC_NATIVE_PROTOCOL_VERSION, "cccore-native-v5")
        self.assertEqual(parameters["mesh_sampling_policy"], "MESH_SURFACE_AREA_REJECTION_V1")
        self.assertEqual(parameters["mesh_probe_counts"], (8192, 32768, 131072))
        self.assertEqual(contract.surface_proposal_limit(50000), 10000000)
        self.assertEqual(len({parameters[name] for name in ("probe_seed", "optimizer_seed", "evaluation_seed")}), 3)
        for key, value in (("mesh_sampling_policy", "OVERLAP_PATCHES_AREA_WEIGHTED_FIXED_SEED"),
                           ("probe_seed", 1729), ("mesh_min_overlap_fraction", 0.02),
                           ("mesh_proposal_budget_factor", 1), ("mesh_probe_counts", [8192])):
            with self.subTest(key=key), self.assertRaises(contract.CcCoreInputError):
                contract.resolve_parameters({f"cc_{key}": value})

    def test_large_project_budget_includes_original_mesh_scan(self):
        """실제 프로젝트의 양방향 원본 순회를 허용하고 조회 묶음은 키우지 않습니다."""
        limit = contract.preparing_operation_limit((11303380, 11303380), 8192)
        self.assertEqual(limit, 2760 + contract.CC_EXTRA_PREPARING_OPERATIONS)
        self.assertGreater(limit, 2048)
        self.assertLessEqual(limit + 10 + 1, contract.CC_MAX_NATIVE_OPERATIONS)
        values = operation_summaries("MESH_TO_MESH", 2780)
        self.assertEqual(len(self.validate(values, "MESH_TO_MESH")), len(values))

    def test_unsupported_input_budget_fails_before_distance_queries(self):
        """프로토콜 순번을 넘는 입력과 잘못된 묶음 크기를 원본 순회 전에 거부합니다."""
        with self.assertRaisesRegex(contract.CcCoreInputError, "입력 크기"):
            contract.preparing_operation_limit((100000000, 100000000), 8192)
        with self.assertRaises(contract.CcCoreInputError):
            contract.preparing_operation_limit((10,), 0)

    def test_preparing_prefix_and_registration_overlap_distance(self):
        """모든 형상 모드의 제한된 준비 뒤 등록 전 거리 두 회를 허용합니다."""
        for mode in contract.CC_NATIVE_OPERATION_SEQUENCES:
            for count in (1, 2, contract.CC_MAX_PREPARING_OPERATIONS):
                with self.subTest(mode=mode, count=count):
                    values = operation_summaries(mode, count)
                    self.assertEqual(len(self.validate(values, mode)), len(values))

    def test_invalid_preparing_count_and_changed_stage_are_rejected(self):
        """빈 준비·상한 초과·단계 중간의 준비 삽입·거리 생략을 거부합니다."""
        for count in (0, contract.CC_MAX_PREPARING_OPERATIONS + 1):
            with self.subTest(count=count), self.assertRaises(contract.CcCoreEnvironmentError):
                self.validate(operation_summaries("POINT_TO_POINT", count))
        valid = operation_summaries("POINT_TO_POINT")
        for mutation in ("stage", "operation", "missing"):
            changed = copy.deepcopy(valid)
            if mutation == "stage":
                changed[3]["stage"] = "PREPARING"
            elif mutation == "operation":
                changed[3]["operation"] = "register"
            else:
                del changed[3]
            with self.subTest(mutation=mutation), self.assertRaises(contract.CcCoreEnvironmentError):
                self.validate(changed)

    def test_overlap_probe_cannot_claim_evaluation_cache(self):
        """등록 전 거리 계산을 정확 평가 캐시나 평가 키로 위장할 수 없습니다."""
        values = operation_summaries("POINT_TO_POINT")
        values[3]["evaluation_key_sha256"] = "a" * 64
        with self.assertRaises(contract.CcCoreEnvironmentError):
            self.validate(values)
        values[3]["evaluation_key_sha256"] = ""
        values[3]["execution_source"] = "exact_evaluation_cache"
        with self.assertRaises(contract.CcCoreEnvironmentError):
            self.validate(values)

    def test_candidate_policy_ignores_saved_fixed_overlap(self):
        """구버전 고정 겹침값을 무시하고 후보 기반 정책과 새 표본수를 사용합니다."""
        defaults = contract.resolve_parameters({})
        self.assertEqual(defaults, contract.resolve_parameters({"cc_overlap_ratio": 0.9}))
        self.assertNotIn("overlap_ratio", defaults)
        self.assertEqual(defaults["overlap_mode"], "ESTIMATED_ON_CANDIDATES")
        self.assertEqual(defaults["point_sample_count"], 50000)
        self.assertEqual(defaults["mesh_sample_count"], 50000)
        self.assertEqual(defaults["evaluation_sample_count"], 30000)
        self.assertTrue(defaults["filter_out_farthest_points"])
        for settings in ({"cc_filter_out_farthest_points": False}, {"cc_candidate_max_depth": 9},
                         {"cc_point_sample_count": True}, {"cc_validation_distance_marker_factor": float("nan")}):
            with self.subTest(settings=settings), self.assertRaises(contract.CcCoreInputError):
                contract.resolve_parameters(settings)

    def test_prior_native_session_version_is_rejected(self):
        """이전 세션 버전을 새 작업의 성공 응답으로 수용하지 않습니다."""
        identity = {"session_id": "session", "job_id": "job", "input_fingerprint": "input"}
        for version in ("cccore-native-v2", "cccore-native-v3", "cccore-native-v4"):
            with self.subTest(version=version), self.assertRaises(contract.CcCoreEnvironmentError):
                contract.validate_native_session_identity({"protocol_version": version, **identity}, **identity)
        contract.validate_native_session_identity(
            {"protocol_version": "cccore-native-v5", **identity}, **identity,
        )


if __name__ == "__main__":
    unittest.main()
