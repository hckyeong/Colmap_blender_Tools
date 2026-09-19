"""후보 선별과 등록 전 겹침 추정의 v4 파일 계약을 검증합니다."""

import copy
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
        for version in ("cccore-native-v2", "cccore-native-v3"):
            with self.subTest(version=version), self.assertRaises(contract.CcCoreEnvironmentError):
                contract.validate_native_session_identity({"protocol_version": version, **identity}, **identity)
        contract.validate_native_session_identity(
            {"protocol_version": "cccore-native-v4", **identity}, **identity,
        )


if __name__ == "__main__":
    unittest.main()
