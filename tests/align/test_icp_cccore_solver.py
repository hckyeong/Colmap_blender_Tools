"""CC coordinator의 순수 수학·파일 계약과 실제 네이티브 네 형상 조합을 검증합니다."""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import zipfile

import numpy as np

MODULE_ROOT = Path(__file__).resolve().parents[2] / "tools" / "align" / "alignment"
sys.path.insert(0, str(MODULE_ROOT))
import icp_cccore_contract as contract
import icp_cccore_solver as solver
import icp_cccore_worker as worker

MANAGE_MODULE_PATH = Path(__file__).resolve().parents[2] / "build_tools" / "cccore_distribution.py"
MANAGE_MODULE_SPEC = importlib.util.spec_from_file_location("cccore_test_env_manage", MANAGE_MODULE_PATH)
if MANAGE_MODULE_SPEC is None or MANAGE_MODULE_SPEC.loader is None:
    raise RuntimeError("CC 패키징 계약 모듈을 불러오지 못했습니다.")
package_manage = importlib.util.module_from_spec(MANAGE_MODULE_SPEC)
MANAGE_MODULE_SPEC.loader.exec_module(package_manage)


def box_mesh() -> tuple[np.ndarray, np.ndarray]:
    """세 축과 면 기울기가 다른 닫힌 합성 메시를 만듭니다."""
    vertices = np.array([
        (-1.0, -0.7, -0.5), (1.4, -0.8, -0.4), (0.9, 1.2, -0.3), (-0.8, 0.7, -0.2),
        (-0.4, -0.5, 1.4), (0.7, -0.4, 1.1), (0.8, 0.9, 1.3), (-0.5, 0.8, 1.6),
    ], dtype=np.float64)
    triangles = np.array([
        (0, 2, 1), (0, 3, 2), (4, 5, 6), (4, 6, 7),
        (0, 1, 5), (0, 5, 4), (1, 2, 6), (1, 6, 5),
        (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7),
    ], dtype=np.uint32)
    return vertices, triangles


def known_delta(scale: float = 1.035) -> np.ndarray:
    """작은 회전·이동과 알려진 추가 배율의 검증용 변환을 만듭니다."""
    angle = np.radians(2.0)
    matrix = np.eye(4)
    matrix[:3, :3] = scale * np.array((
        (np.cos(angle), -np.sin(angle), 0.0),
        (np.sin(angle), np.cos(angle), 0.0),
        (0.0, 0.0, 1.0),
    ))
    matrix[:3, 3] = (0.015, -0.01, 0.02)
    return matrix


def make_job(
    directory: Path, moving_kind: str, reference_kind: str, *, scale: float = 1.035, offset: float = 0.0
) -> tuple[dict, np.ndarray]:
    """실제 작업과 같은 바이너리·메타데이터를 가진 합성 입력을 기록합니다."""
    vertices, triangles = box_mesh()
    sample = solver.sample_triangle_surface(vertices, triangles, 4000, 9761)
    delta = known_delta(scale)
    delta[:3, 3] += offset - delta[:3, :3] @ np.full(3, offset)
    moving = (vertices if moving_kind == "TRIANGLE_MESH" else sample) + offset
    reference = solver.transform_positions((vertices if reference_kind == "TRIANGLE_MESH" else sample) + offset, delta)
    geometry = {}
    for role, positions, kind in (("moving", moving, moving_kind), ("reference", reference, reference_kind)):
        position_name = f"{role}.bin"
        np.asarray(positions, dtype="<f8").tofile(directory / position_name)
        entry = {"kind": kind, "positions_file": position_name, "point_count": len(positions)}
        if kind == "TRIANGLE_MESH":
            triangle_name = f"{role}-triangles.bin"
            np.asarray(triangles, dtype="<u4").tofile(directory / triangle_name)
            entry.update(triangles_file=triangle_name, triangle_count=len(triangles))
        geometry[role] = entry
    geometry["provenance"] = {"synthetic": True}
    geometry["input_fingerprint"] = "synthetic-geometry"
    initial = np.eye(4)
    initial[:3, :3] *= 2.0
    initial[:3, 3] = (8.0, 3.0, -2.0)
    mode = contract.GEOMETRY_MODES[(moving_kind, reference_kind)]
    manifest = {
        **contract.identity_payload({"job_id": directory.name, "moving_model_id": "P", "input_fingerprint": "synthetic"}),
        "parameter_fingerprint": "synthetic-parameters",
        "reference_model_ids": ["A"], "model_revision_snapshot": [["P", 0], ["A", 0]],
        "model_state_snapshot": [], "marker_signature": "direct",
        "initial_alignment_4x4": initial.tolist(), "geometry": geometry,
        "geometry_mode": mode, "reverse_registration": mode == "MESH_TO_POINT",
        "parameters": {},
    }
    contract.write_json_atomic(directory / "manifest.json", manifest)
    return manifest, delta


def simple_metrics(mean_rms: float, p95: float | None = None) -> dict:
    """품질 선택 규칙을 독립적으로 검증할 최소 거리 통계를 만듭니다."""
    tail = mean_rms if p95 is None else p95
    direction = {
        "rms_normalized": mean_rms, "p95_normalized": tail,
        "spatial_bins": [{"bin": 0, "count": 30, "rms_normalized": mean_rms}],
    }
    return {"mean_rms_normalized": mean_rms, "directions": {"moving_to_reference": direction}}


def native_thread_metadata(*, requested: int = 8, effective: int = 6, hardware: int = 6) -> dict:
    """순수 계약 검사에 사용할 유효한 oneTBB 네이티브 실행 정보를 만듭니다."""
    return {
        "thread_backend": "oneTBB",
        "tbb_version": "2022.0.0",
        "requested_max_threads": requested,
        "effective_max_threads": effective,
        "hardware_thread_count": hardware,
        "distance_multithread_support": True,
        "octree_multithread_support": True,
        "core_commit": "a" * 40,
        "core_patch": "bounded-scale-v1",
        "elapsed_seconds": 0.125,
    }


def native_call_summaries(
    geometry_mode: str,
    *,
    requested: int = 8,
    effective: int = 6,
    preparing_count: int = 2,
) -> list[dict]:
    """제한된 준비 접두부와 형상별 v4 등록·평가 골격의 계측 목록을 만듭니다."""
    operations = ("distance",) * preparing_count + contract.CC_NATIVE_OPERATION_SEQUENCES[geometry_mode]
    stages = ("PREPARING",) * preparing_count + contract.CC_NATIVE_OPERATION_STAGES[geometry_mode]
    timings = {name: 0.0 for name in contract.CC_NATIVE_TIMING_FIELDS}
    counters = {name: 0 for name in contract.CC_NATIVE_COUNTER_FIELDS}
    return [{
        "operation": operation,
        "index": index,
        "sequence": index,
        "physical_sequence": index,
        "source_sequence": index,
        "source_logical_index": index,
        "stage": stages[index - 1],
        "execution_source": "native_session",
        "evaluation_key_sha256": "d" * 64 if operation == "distance" and
        stages[index - 1] in {"BASELINE", "RIGID_EVALUATION", "REFINE_EVALUATION"} else "",
        "elapsed_seconds": index / 100.0,
        "roundtrip_seconds": index / 100.0 + 0.002,
        "protocol_overhead_seconds": 0.002,
        "requested_max_threads": requested,
        "effective_max_threads": effective,
        "data_geometry_id": f"data-{index}",
        "model_geometry_id": "model",
        "timings": dict(timings),
        **counters,
    } for index, operation in enumerate(operations, start=1)]


def physical_native_call_summaries(logical_operations: list[dict]) -> list[dict]:
    """논리 목록의 실제 세션 실행 항목만 물리 명령 계약으로 변환합니다."""
    result = []
    for logical in logical_operations:
        if logical["execution_source"] != "native_session":
            continue
        sequence = len(result) + 1
        result.append({
            **{
                key: value for key, value in logical.items()
                if key not in {"index", "sequence", "physical_sequence", "source_sequence", "source_logical_index"}
            },
            "index": sequence,
            "sequence": sequence,
            "logical_index": logical["index"],
        })
    return result


def make_native_runtime(directory: Path) -> tuple[Path, dict]:
    """등록 안전·격자 메모리·재사용 패치 지문을 포함하는 최소 v4 실행 환경을 만듭니다."""
    directory.mkdir(parents=True, exist_ok=True)
    binary_contents = {
        "colmap_cccore_worker.exe": b"worker-v4",
        "CCCoreLib.dll": b"cccore-v4",
        "tbb12.dll": b"tbb-v4",
    }
    binary_hashes = {}
    for filename, contents in binary_contents.items():
        path = directory / filename
        path.write_bytes(contents)
        binary_hashes[filename] = solver.file_sha256(path)
    thread_metadata = native_thread_metadata(requested=4, effective=4, hardware=8)
    safety = {
        "id": "farthest-mesh-correspondence-v1", "file": "patches/farthest_mesh_correspondence.patch",
        "sha256": "b" * 64, "original_sha256": "c" * 64, "patched_sha256": "d" * 64,
    }
    grid = {
        "id": "mesh-grid-memory-budget-v1", "file": "patches/mesh_grid_memory_budget.patch",
        "sha256": "e" * 64, "files": [{"path": "src/DistanceComputationTools.cpp",
                                        "original_sha256": "f" * 64, "patched_sha256": "a" * 64}],
    }
    reuse = {
        "id": "mesh-grid-reuse-v1", "file": "patches/mesh_grid_reuse.patch", "sha256": "1" * 64,
        "files": [{"path": name, "original_sha256": "a" * 64, "patched_sha256": "2" * 64}
                  for name in ("include/DistanceComputationTools.h", "src/DistanceComputationTools.cpp",
                               "include/GridAndMeshIntersection.h", "src/GridAndMeshIntersection.cpp")],
    }
    thread_metadata["core_patch"] += "+multithread-state-race-v1+" + safety["id"] + "+" + grid["id"] + "+" + reuse["id"]
    manifest = {
        "protocol_version": contract.CC_NATIVE_PROTOCOL_VERSION,
        "platform": "windows-x64",
        "core_commit": thread_metadata["core_commit"],
        "core_patch": thread_metadata["core_patch"],
        "dependencies": {"tbb": {"version": contract.CC_TBB_VERSION},
                         "cccore": {"registration_safety_patch": safety, "mesh_grid_memory_patch": grid,
                                    "mesh_grid_reuse_patch": reuse}},
        "mesh_grid_reuse_patch": reuse["id"],
        "mesh_grid_reuse_patch_sha256": reuse["sha256"],
        "mesh_grid_reuse_source_sha256": {item["path"]: item["patched_sha256"] for item in reuse["files"]},
        "mesh_grid_memory_patch": grid["id"],
        "mesh_grid_memory_patch_sha256": grid["sha256"],
        "mesh_grid_source_sha256": {item["path"]: item["patched_sha256"] for item in grid["files"]},
        "registration_safety_patch": safety["id"],
        "registration_safety_patch_sha256": safety["sha256"],
        "patched_registration_sha256": safety["patched_sha256"],
        "build_options": {
            "CCCORELIB_SHARED": True,
            "CCCORELIB_USE_QT_CONCURRENT": False,
            "CCCORELIB_USE_TBB": True,
            "CCCORELIB_USE_CGAL": False,
        },
        "coordinate_type": "float32",
        "matrix_scalar_type": "float64",
        "binary_sha256": binary_hashes,
        **{key: value for key, value in thread_metadata.items() if key != "elapsed_seconds"},
        "self_test": {
            "protocol_version": contract.CC_NATIVE_PROTOCOL_VERSION,
            "status": "completed",
            "self_test": "passed",
            "coordinate_type": "float32",
            "matrix_scalar_type": "float64",
            "library_path": "CCCoreLib.dll",
            "tbb_library_path": "tbb12.dll",
            **thread_metadata,
        },
    }
    contract.write_json_atomic(directory / "runtime_manifest.json", manifest)
    return directory / "colmap_cccore_worker.exe", manifest


class MeshCleanupTests(unittest.TestCase):
    """원본 보존과 두 정밀도 단계의 계산 전용 메시 정리를 검증합니다."""

    def test_clean_mesh_is_unchanged(self) -> None:
        """정상 메시의 배열·면 순서·배정도 좌표가 그대로 유지되는지 확인합니다."""
        positions, triangles = box_mesh()
        for native in (False, True):
            result = solver.prepare_mesh_alignment_geometry(positions, triangles, native_precision=native)
            self.assertIs(result[0], positions)
            self.assertIs(result[1], triangles)

    def test_native_rounding_removes_only_collapsed_face(self) -> None:
        """배정도에서 유효하지만 단정도에서 겹치는 작은 면만 제외합니다."""
        positions = np.asarray(((0, 0, 0), (1, 0, 0), (0, 1, 0),
                                (0.5, 0.5, 0.5), (0.5 + 1e-9, 0.5, 0.5),
                                (0.5, 0.5 + 1e-9, 0.5)), dtype=np.float64)
        triangles = np.asarray(((3, 4, 5), (0, 1, 2)), dtype=np.uint32)
        original = positions.copy(), triangles.copy()
        world = solver.prepare_mesh_alignment_geometry(positions, triangles)
        self.assertEqual(len(world[1]), 2)
        native = solver.prepare_mesh_alignment_geometry(*world, native_precision=True)
        np.testing.assert_array_equal(native[0], positions[:3])
        np.testing.assert_array_equal(native[1], ((0, 1, 2),))
        np.testing.assert_array_equal(positions, original[0])
        np.testing.assert_array_equal(triangles, original[1])

    def test_remote_degenerate_vertices_do_not_affect_normalization(self) -> None:
        """퇴화면 전용 외딴 정점을 제거해 공통 원점·단위와 원본 출처를 보존합니다."""
        positions, triangles = box_mesh()
        expanded = np.vstack((positions, ((1e12, 0, 0), (1e12 + 1, 0, 0), (1e12 + 2, 0, 0))))
        faces = np.vstack((triangles, ((8, 9, 10),))).astype(np.uint32)
        provenance = {"point_count": len(expanded), "triangle_count": len(faces)}
        cleaned = solver._clean_mesh_geometry(contract.CcCoreGeometry("TRIANGLE_MESH", expanded, faces, provenance), "검사")
        self.assertIs(cleaned.provenance, provenance)
        reference = positions + 0.1
        expected = solver.make_normalization(positions, reference)
        actual = solver.make_normalization(cleaned.positions, reference)
        np.testing.assert_array_equal(actual.origin, expected.origin)
        self.assertEqual(actual.unit, expected.unit)
        np.testing.assert_array_equal(cleaned.triangles, triangles)

    def test_degenerate_and_invalid_inputs_are_distinguished(self) -> None:
        """전체 퇴화와 잘못된 좌표·인덱스 형식을 엄격히 거부합니다."""
        positions = np.asarray(((0, 0, 0), (1, 0, 0), (2, 0, 0)), dtype=np.float64)
        triangles = np.asarray(((0, 1, 2),), dtype=np.uint32)
        for native in (False, True):
            with self.assertRaisesRegex(ValueError, "퇴화"):
                solver.prepare_mesh_alignment_geometry(positions, triangles, native_precision=native)
        with self.assertRaisesRegex(contract.CcCoreInputError, "CC 검사 메시.*퇴화"):
            solver._clean_mesh_geometry(contract.CcCoreGeometry("TRIANGLE_MESH", positions, triangles, {}), "검사")
        for invalid_positions, invalid_faces in (
            (positions, np.asarray(((0, 1, 3),))),
            (positions, np.asarray(((0, 1, -1),))),
            (positions, triangles.astype(float)),
            (positions[:, :2], triangles),
            (np.full((3, 3), np.nan), triangles),
        ):
            with self.subTest(shape=invalid_positions.shape, faces=invalid_faces), self.assertRaises(ValueError):
                solver.prepare_mesh_alignment_geometry(invalid_positions, invalid_faces)

    def test_block_boundary_keeps_face_order_and_compacts_vertices(self) -> None:
        """블록 경계를 넘는 면의 순서를 유지하고 미참조 정점만 압축합니다."""
        positions = np.asarray(((9, 9, 9), (0, 0, 0), (1, 0, 0), (0, 1, 0)), dtype=float)
        triangles = np.tile(np.asarray((1, 2, 3), dtype=np.uint32), (65_538, 1))
        triangles[65_535] = (1, 1, 2)
        triangles[-1] = (3, 2, 1)
        result_positions, result_faces = solver.prepare_mesh_alignment_geometry(positions, triangles)
        np.testing.assert_array_equal(result_positions, positions[1:])
        self.assertEqual(len(result_faces), len(triangles) - 1)
        np.testing.assert_array_equal(result_faces[-1], (2, 1, 0))


class PureContractTests(unittest.TestCase):
    """외부 라이브러리 계산을 대체하지 않는 순수 계약 회귀입니다."""

    def test_surface_sampling_follows_area_and_uses_distinct_seed(self) -> None:
        """면적비 1:4가 표본 비율에 반영되고 두 평가 시드가 구분되는지 확인합니다."""
        vertices = np.array(((0, 0, 0), (1, 0, 0), (0, 1, 0), (10, 0, 0), (12, 0, 0), (10, 2, 0)), dtype=float)
        faces = np.array(((0, 1, 2), (3, 4, 5)), dtype=np.uint32)
        first = solver.sample_triangle_surface(vertices, faces, 30000, contract.CC_OPTIMIZER_SEED)
        repeated = solver.sample_triangle_surface(vertices, faces, 30000, contract.CC_OPTIMIZER_SEED)
        evaluation = solver.sample_triangle_surface(vertices, faces, 30000, contract.CC_EVALUATION_SEED)
        np.testing.assert_array_equal(first, repeated)
        self.assertAlmostEqual(float(np.mean(first[:, 0] < 5.0)), 0.2, delta=0.012)
        self.assertFalse(np.array_equal(first, evaluation))
        self.assertTrue(np.all(first[:, 2] == 0.0))

    def test_v4_protocol_and_thread_limit_contract(self) -> None:
        """표본 정책 v5·통신 v4와 1..1024 범위의 스레드 요청만 허용합니다."""
        self.assertEqual(contract.CC_ALGORITHM_VERSION, "cccore-icp-v5")
        self.assertEqual(contract.CC_JOB_PROTOCOL_VERSION, "cccore-job-v4")
        self.assertEqual(contract.CC_NATIVE_PROTOCOL_VERSION, "cccore-native-v4")
        self.assertEqual(contract.resolve_parameters({"cc_max_threads": 1024})["max_threads"], 1024)
        for value in (0, -1, 1025, True, 1.5):
            with self.subTest(value=value), self.assertRaises(contract.CcCoreInputError):
                contract.resolve_parameters({"cc_max_threads": value})

    def test_native_thread_metadata_is_strict_and_stable_between_calls(self) -> None:
        """각 호출의 요청·실제값과 oneTBB·코어 식별 불일치를 모두 거부합니다."""
        valid = native_thread_metadata()
        first = contract.validate_native_execution_metadata(valid, requested_max_threads=8)
        self.assertEqual(first["effective_max_threads"], 6)
        self.assertEqual(first["thread_backend"], "oneTBB")
        expected = {key: value for key, value in first.items() if key != "elapsed_seconds"}
        contract.validate_native_execution_metadata(valid, requested_max_threads=8, expected_identity=expected)
        mutations = {
            "requested_max_threads": 7,
            "effective_max_threads": 5,
            "thread_backend": "serial",
            "tbb_version": "2021.11.0",
            "distance_multithread_support": False,
            "octree_multithread_support": False,
            "core_commit": "invalid",
            "elapsed_seconds": float("nan"),
        }
        for field, value in mutations.items():
            with self.subTest(field=field):
                changed = {**valid, field: value}
                with self.assertRaises(contract.CcCoreEnvironmentError):
                    contract.validate_native_execution_metadata(changed, requested_max_threads=8)
        changed_hardware = native_thread_metadata(effective=7, hardware=7)
        with self.assertRaisesRegex(contract.CcCoreEnvironmentError, "순차 호출"):
            contract.validate_native_execution_metadata(
                changed_hardware,
                requested_max_threads=8,
                expected_identity=expected,
            )
        summaries = native_call_summaries("POINT_TO_POINT")
        self.assertEqual(
            contract.validate_native_call_summaries(
                summaries,
                geometry_mode="POINT_TO_POINT",
                requested_max_threads=8,
                effective_max_threads=6,
            )[0]["operation"],
            "distance",
        )
        for field, value in (
            ("index", 2), ("requested_max_threads", 7), ("elapsed_seconds", float("inf")),
            ("roundtrip_seconds", float("nan")), ("protocol_overhead_seconds", 0.003),
        ):
            with self.subTest(summary_field=field):
                changed = copy.deepcopy(summaries)
                changed[0][field] = value
                with self.assertRaises(contract.CcCoreEnvironmentError):
                    contract.validate_native_call_summaries(
                        changed,
                        geometry_mode="POINT_TO_POINT",
                        requested_max_threads=8,
                        effective_max_threads=6,
                    )

    def test_native_call_sequence_matches_each_geometry_mode(self) -> None:
        """네 형상 모드의 호출 목록을 정확히 허용하고 누락·순서 변경을 거부합니다."""
        for geometry_mode in contract.CC_NATIVE_OPERATION_SEQUENCES:
            with self.subTest(geometry_mode=geometry_mode):
                summaries = native_call_summaries(geometry_mode)
                validated = contract.validate_native_call_summaries(
                    summaries,
                    geometry_mode=geometry_mode,
                    requested_max_threads=8,
                    effective_max_threads=6,
                )
                self.assertEqual(
                    tuple(item["operation"] for item in validated[2:]),
                    contract.CC_NATIVE_OPERATION_SEQUENCES[geometry_mode],
                )
                with self.assertRaisesRegex(contract.CcCoreEnvironmentError, "작업 순서"):
                    contract.validate_native_call_summaries(
                        summaries[:-1],
                        geometry_mode=geometry_mode,
                        requested_max_threads=8,
                        effective_max_threads=6,
                    )
                reordered = copy.deepcopy(summaries)
                register_index = next(
                    index for index, item in enumerate(reordered) if item["operation"] == "register"
                )
                reordered[0]["operation"], reordered[register_index]["operation"] = (
                    reordered[register_index]["operation"], reordered[0]["operation"]
                )
                with self.assertRaisesRegex(contract.CcCoreEnvironmentError, "작업 순서"):
                    contract.validate_native_call_summaries(
                        reordered,
                        geometry_mode=geometry_mode,
                        requested_max_threads=8,
                        effective_max_threads=6,
                    )

    def test_logical_and_physical_calls_validate_exact_cache_links(self) -> None:
        """캐시 적중 논리 연산이 앞선 물리 거리 명령의 동일 키만 참조하는지 확인합니다."""
        logical = native_call_summaries("POINT_TO_MESH")
        baseline_index = next(index for index, item in enumerate(logical) if item["stage"] == "BASELINE")
        cached_index = next(index for index, item in enumerate(logical) if item["stage"] == "RIGID_EVALUATION")
        evaluation_key = "b" * 64
        logical[baseline_index]["evaluation_key_sha256"] = evaluation_key
        logical[cached_index].update({
            "physical_sequence": None,
            "source_sequence": baseline_index + 1,
            "source_logical_index": baseline_index + 1,
            "execution_source": "exact_evaluation_cache",
            "evaluation_key_sha256": evaluation_key,
            "elapsed_seconds": 0.0,
            "roundtrip_seconds": logical[baseline_index]["roundtrip_seconds"],
            "protocol_overhead_seconds": logical[baseline_index]["protocol_overhead_seconds"],
        })
        next_sequence = 1
        for item in logical:
            if item["execution_source"] != "native_session":
                continue
            item["physical_sequence"] = next_sequence
            item["source_sequence"] = next_sequence
            item["source_logical_index"] = item["index"]
            next_sequence += 1
        validated = contract.validate_native_call_summaries(
            logical, geometry_mode="POINT_TO_MESH",
            requested_max_threads=8, effective_max_threads=6,
        )
        physical = physical_native_call_summaries(logical)
        self.assertEqual(len(contract.validate_physical_native_call_summaries(
            physical, validated, requested_max_threads=8, effective_max_threads=6,
        )), len(logical) - 1)
        self.assertEqual(contract.validate_exact_evaluation_cache_statistics(
            {"policy": "BIT_EXACT_MATRIX_AND_EVALUATION_CONTRACT", "entry_count": 2,
             "hit_count": 1, "miss_count": 2},
            validated,
        )["hit_count"], 1)
        for timing_field in ("roundtrip_seconds", "protocol_overhead_seconds"):
            with self.subTest(timing_field=timing_field):
                changed = copy.deepcopy(logical)
                changed[cached_index][timing_field] += 0.001
                with self.assertRaises(contract.CcCoreEnvironmentError):
                    contract.validate_native_call_summaries(
                        changed, geometry_mode="POINT_TO_MESH",
                        requested_max_threads=8, effective_max_threads=6,
                    )
        changed = copy.deepcopy(logical)
        changed[cached_index]["evaluation_key_sha256"] = "c" * 64
        with self.assertRaises(contract.CcCoreEnvironmentError):
            contract.validate_native_call_summaries(
                changed, geometry_mode="POINT_TO_MESH",
                requested_max_threads=8, effective_max_threads=6,
            )

    def test_exact_evaluation_cache_requires_bit_identical_matrix(self) -> None:
        """유효한 후보도 float64 행렬 한 비트가 다르면 정확 캐시 미스로 처리합니다."""
        samples = np.array(((0.0, 0.0, 0.0), (1.0, 2.0, 3.0), (-1.0, 4.0, 2.0)))
        cache = solver.ExactEvaluationCache({"contract": "test"}, samples, samples + 1.0)
        identity = np.identity(4, dtype=np.float64)
        cached, original_key, _, _ = cache.lookup("moving_to_reference", identity)
        self.assertIsNone(cached)
        cache.store(
            "moving_to_reference", identity, np.array((0.1, 0.2, 0.3)),
            source_logical_index=1, source_sequence=1,
        )
        cached, repeated_key, logical_index, sequence = cache.lookup("moving_to_reference", identity.copy())
        np.testing.assert_array_equal(cached, (0.1, 0.2, 0.3))
        self.assertEqual((original_key, logical_index, sequence), (repeated_key, 1, 1))
        changed = identity.copy()
        changed[0, 3] = np.nextafter(0.0, 1.0)
        missed, changed_key, _, _ = cache.lookup("moving_to_reference", changed)
        self.assertIsNone(missed)
        self.assertNotEqual(changed_key, original_key)
        self.assertEqual(cache.statistics(), {
            "policy": "BIT_EXACT_MATRIX_AND_EVALUATION_CONTRACT",
            "entry_count": 1, "hit_count": 1, "miss_count": 2,
        })

    def test_native_session_aggregate_contract_rejects_corruption(self) -> None:
        """상주 메모리와 지오메트리·물리 명령 합계가 다른 종료 통계를 거부합니다."""
        geometries = [{
            "geometry_id": "model", "point_count": 8, "triangle_count": 0,
            "load_count": 1, "cache_hits": 2, "binary_read_seconds": 0.01,
            "conversion_validation_seconds": 0.01, "mesh_load_seconds": 0.0,
            "estimated_bytes": 100, "cache_scope": "SESSION", "released_after_command": False,
        }, {
            "geometry_id": "query", "point_count": 8, "triangle_count": 0,
            "load_count": 1, "cache_hits": 1, "binary_read_seconds": 0.01,
            "conversion_validation_seconds": 0.01, "mesh_load_seconds": 0.0,
            "estimated_bytes": 20, "cache_scope": "COMMAND", "released_after_command": True,
        }]
        physical = physical_native_call_summaries(native_call_summaries("POINT_TO_MESH"))
        totals = {
            "geometry_load_count": 2, "geometry_cache_hits": 3,
            "octree_build_count": 2, "octree_cache_hits": 1,
            "mesh_grid_build_count": 1, "mesh_grid_cache_hits": 0,
        }
        for name, total in totals.items():
            physical[0][name] = total
        session = {
            **totals, "peak_working_set_bytes": 1000,
            "cache_peak_estimated_bytes": 120,
            "cache_resident_estimated_bytes": 100,
        }
        contract.validate_native_session_aggregates(session, geometries, physical)
        mutations = (
            ("resident", "cache_resident_estimated_bytes", 101, None),
            ("peak", "cache_peak_estimated_bytes", 99, None),
            ("geometry_hits", "geometry_cache_hits", 4, None),
            ("physical_octree", None, None, "octree_build_count"),
        )
        for label, session_field, value, physical_field in mutations:
            with self.subTest(label=label):
                changed_session = copy.deepcopy(session)
                changed_physical = copy.deepcopy(physical)
                if session_field is not None:
                    changed_session[session_field] = value
                if physical_field is not None:
                    changed_physical[0][physical_field] += 1
                with self.assertRaises(contract.CcCoreEnvironmentError):
                    contract.validate_native_session_aggregates(
                        changed_session, geometries, changed_physical,
                    )

    def test_session_protocol_statistics_rejects_inconsistent_sums_and_ratios(self) -> None:
        """물리 명령 왕복 합계와 전체 정렬 대비 비율의 변조를 모두 거부합니다."""
        logical = native_call_summaries("POINT_TO_POINT")
        validated_logical = contract.validate_native_call_summaries(
            logical, geometry_mode="POINT_TO_POINT", requested_max_threads=8, effective_max_threads=6,
        )
        physical = contract.validate_physical_native_call_summaries(
            physical_native_call_summaries(logical), validated_logical,
            requested_max_threads=8, effective_max_threads=6,
        )
        roundtrip = math.fsum(item["roundtrip_seconds"] for item in physical)
        overhead = math.fsum(item["protocol_overhead_seconds"] for item in physical)
        total = 2.0
        statistics = {
            "physical_roundtrip_seconds": roundtrip,
            "protocol_overhead_seconds": overhead,
            "protocol_overhead_ratio_of_roundtrip": overhead / roundtrip,
            "protocol_overhead_fraction_of_total_alignment": overhead / total,
        }
        self.assertEqual(
            contract.validate_session_protocol_statistics(
                statistics, physical, total_alignment_seconds=total,
            ),
            statistics,
        )
        for field in statistics:
            with self.subTest(field=field):
                changed = dict(statistics)
                changed[field] += 0.01
                with self.assertRaises(contract.CcCoreEnvironmentError):
                    contract.validate_session_protocol_statistics(
                        changed, physical, total_alignment_seconds=total,
                    )
        zero_roundtrip = copy.deepcopy(logical)
        zero_roundtrip[0]["roundtrip_seconds"] = 0.0
        zero_roundtrip[0]["protocol_overhead_seconds"] = 0.0
        with self.assertRaisesRegex(contract.CcCoreEnvironmentError, "왕복 시간"):
            contract.validate_native_call_summaries(
                zero_roundtrip, geometry_mode="POINT_TO_POINT",
                requested_max_threads=8, effective_max_threads=6,
            )
        zero_total = dict(statistics)
        zero_total["protocol_overhead_fraction_of_total_alignment"] = 0.0
        with self.assertRaisesRegex(contract.CcCoreEnvironmentError, "전체 정렬 시간이 0"):
            contract.validate_session_protocol_statistics(
                zero_total, physical, total_alignment_seconds=0.0,
            )
        short_total = roundtrip - 0.01
        short_statistics = dict(statistics)
        short_statistics["protocol_overhead_fraction_of_total_alignment"] = overhead / short_total
        with self.assertRaisesRegex(contract.CcCoreEnvironmentError, "왕복 합계"):
            contract.validate_session_protocol_statistics(
                short_statistics, physical, total_alignment_seconds=short_total,
            )
        changed_physical = [dict(item) for item in physical]
        changed_physical[0]["protocol_overhead_seconds"] += 0.001
        with self.assertRaises(contract.CcCoreEnvironmentError):
            contract.validate_physical_native_call_summaries(
                changed_physical, validated_logical,
                requested_max_threads=8, effective_max_threads=6,
            )

    def test_wait_for_response_rechecks_cancelled_session_after_process_exit(self) -> None:
        """첫 종료 조회 뒤 공개된 취소 결과를 재조회해 환경 오류로 오분류하지 않습니다."""
        class ExitedProcess:
            """종료 코드 2를 반환하는 최소 프로세스 대역입니다."""

            returncode = 2

            @staticmethod
            def poll() -> int:
                """네이티브 취소 종료 코드를 반환합니다."""
                return 2

            @staticmethod
            def wait(timeout: float | None = None) -> int:
                """종료된 프로세스의 코드를 즉시 반환합니다."""
                del timeout
                return 2

        with tempfile.TemporaryDirectory() as temporary:
            runner = object.__new__(solver.NativeRunner)
            runner.directory = Path(temporary)
            runner.process = ExitedProcess()
            runner.cancel_probe = None
            runner.progress_sink = None
            response_path = runner.directory / "response.json"
            progress_path = runner.directory / "progress.json"
            with mock.patch.object(
                runner, "_session_failure", side_effect=(None, {"status": "cancelled"}),
            ) as failure_probe:
                with self.assertRaises(contract.CcCoreCancelled):
                    runner._wait_for_response(
                        response_path, progress_path, sequence=1,
                        operation="distance", stage="BASELINE",
                    )
            self.assertEqual(failure_probe.call_count, 2)

    def test_wait_for_response_accepts_atomic_response_published_before_normal_exit(self) -> None:
        """close 응답 공개 직후 exit 0을 봐도 응답 경로를 재확인해 정상 결과를 읽습니다."""
        class AppearingResponse:
            """poll 종료 관측과 함께 원자 공개되는 응답 파일을 재현합니다."""

            def __init__(self) -> None:
                """응답이 아직 공개되지 않은 상태로 시작합니다."""
                self.check_count = 0
                self.published = False

            def is_file(self) -> bool:
                """현재 응답 공개 상태를 반환합니다."""
                self.check_count += 1
                return self.published

        class ExitedProcess:
            """응답을 공개한 뒤 정상 종료한 close 세션 프로세스를 대행합니다."""

            returncode = 0

            def __init__(self, response: AppearingResponse) -> None:
                """poll 때 공개할 응답을 보관합니다."""
                self.response = response

            def poll(self) -> int:
                """응답 공개 뒤 정상 종료 코드를 반환합니다."""
                self.response.published = True
                return 0

        class MissingProgress:
            """진행 파일이 없는 close 종료 경계를 대행합니다."""

            @staticmethod
            def is_file() -> bool:
                """진행 파일이 없음을 반환합니다."""
                return False

        runner = object.__new__(solver.NativeRunner)
        runner.directory = Path(tempfile.gettempdir())
        runner.cancel_probe = None
        runner.progress_sink = None
        response_path = AppearingResponse()
        runner.process = ExitedProcess(response_path)
        with (
            mock.patch.object(
                runner, "_session_failure", side_effect=(None, {"status": "closed"}),
            ) as failure_probe,
            mock.patch.object(contract, "read_json_object", return_value={"status": "closed"}),
        ):
            result = runner._wait_for_response(
                response_path, MissingProgress(), sequence=1,
                operation="close", stage="CLOSE",
            )
        self.assertEqual(result, {"status": "closed"})
        self.assertEqual(response_path.check_count, 2)
        self.assertEqual(failure_probe.call_count, 2)

    def test_start_and_close_classify_verified_cancelled_session(self) -> None:
        """ready 대기와 close 사전 검사도 검증된 취소 종료를 같은 상태로 분류합니다."""
        class ExitedProcess:
            """시작·종료 경계에서 취소 코드로 끝난 프로세스를 대행합니다."""

            pid = 12345
            returncode = 2

            @staticmethod
            def poll() -> int:
                """네이티브 취소 종료 코드를 반환합니다."""
                return 2

            @staticmethod
            def wait(timeout: float | None = None) -> int:
                """종료된 프로세스의 코드를 즉시 반환합니다."""
                del timeout
                return 2

        class Lifetime:
            """단위 검사에서 Job Object 닫기만 대행합니다."""

            @staticmethod
            def close() -> None:
                """테스트용 수명 객체를 닫습니다."""

        with tempfile.TemporaryDirectory() as temporary:
            runner = object.__new__(solver.NativeRunner)
            runner.directory = Path(temporary)
            runner.executable = Path(temporary) / "worker.exe"
            runner.parameters = {"max_threads": 1}
            runner.session_id = "session"
            runner.job_id = "job"
            runner.input_fingerprint = "input"
            runner.process = None
            runner.closed = False
            runner.cancel_probe = None
            runner.lifetime = None
            runner.native_process_count = 0
            process = ExitedProcess()
            with (
                mock.patch.object(runner, "_validate_runtime_unchanged"),
                mock.patch.object(
                    runner, "_session_failure", side_effect=(None, {"status": "cancelled"}),
                ) as failure_probe,
                mock.patch.object(solver.subprocess, "Popen", return_value=process),
                mock.patch.object(solver, "_ChildLifetime", return_value=Lifetime()),
            ):
                with self.assertRaises(contract.CcCoreCancelled):
                    runner.start()
            self.assertEqual(failure_probe.call_count, 2)

        runner = object.__new__(solver.NativeRunner)
        runner.closed = False
        runner.process = ExitedProcess()
        runner.lifetime = None
        with mock.patch.object(
            runner, "_session_failure", return_value={"status": "cancelled"},
        ) as failure_probe:
            with self.assertRaises(contract.CcCoreCancelled):
                runner.close()
        self.assertEqual(failure_probe.call_count, 1)

    def test_start_rechecks_cancelled_session_after_valid_ready_and_exit(self) -> None:
        """ready 검증 직후 종료된 세션도 둘째 종료 조회의 취소 상태를 우선합니다."""
        class ExitedProcess:
            """ready 공개 직후 취소 코드로 종료한 프로세스를 대행합니다."""

            pid = 12345
            returncode = 2

            @staticmethod
            def poll() -> int:
                """네이티브 취소 종료 코드를 반환합니다."""
                return 2

            @staticmethod
            def wait(timeout: float | None = None) -> int:
                """종료된 프로세스의 코드를 즉시 반환합니다."""
                del timeout
                return 2

        class Lifetime:
            """단위 검사에서 Job Object 닫기만 대행합니다."""

            @staticmethod
            def close() -> None:
                """테스트용 수명 객체를 닫습니다."""

        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            (directory / "native").mkdir()
            ready_path = directory / "native" / "session-ready.json"
            runner = object.__new__(solver.NativeRunner)
            runner.directory = directory
            runner.executable = directory / "worker.exe"
            runner.parameters = {"max_threads": 1}
            runner.session_id = "session"
            runner.job_id = "job"
            runner.input_fingerprint = "input"
            runner.process = None
            runner.closed = False
            runner.cancel_probe = None
            runner.lifetime = None
            runner.native_process_count = 0
            process = ExitedProcess()
            ready = {"status": "ready", "process_id": process.pid}

            def popen_with_ready(*arguments, **keywords):
                """세션 시작 호출 시 ready 파일을 원자 공개된 상태로 만듭니다."""
                del arguments, keywords
                ready_path.touch()
                return process

            with (
                mock.patch.object(runner, "_validate_runtime_unchanged"),
                mock.patch.object(
                    runner, "_session_failure", side_effect=(None, {"status": "cancelled"}),
                ) as failure_probe,
                mock.patch.object(runner, "_accept_native_identity"),
                mock.patch.object(solver.subprocess, "Popen", side_effect=popen_with_ready),
                mock.patch.object(solver, "_ChildLifetime", return_value=Lifetime()),
                mock.patch.object(contract, "read_json_object", return_value=ready),
                mock.patch.object(contract, "validate_native_session_identity"),
                mock.patch.object(contract, "validate_native_execution_metadata", return_value={}),
            ):
                with self.assertRaises(contract.CcCoreCancelled):
                    runner.start()
            self.assertEqual(failure_probe.call_count, 2)

    def test_evaluation_distance_requires_exact_cache_key(self) -> None:
        """독립 평가만 키를 필수로 하고 준비·겹침 추정·등록의 평가 키를 거부합니다."""
        summaries = native_call_summaries("POINT_TO_POINT")
        validated = contract.validate_native_call_summaries(
            summaries, geometry_mode="POINT_TO_POINT",
            requested_max_threads=8, effective_max_threads=6,
        )
        self.assertTrue(all(
            bool(item["evaluation_key_sha256"])
            for item in validated if item["stage"] in {"BASELINE", "RIGID_EVALUATION", "REFINE_EVALUATION"}
        ))
        for index, item in enumerate(summaries):
            with self.subTest(index=index):
                changed = copy.deepcopy(summaries)
                changed[index]["evaluation_key_sha256"] = "" if item["evaluation_key_sha256"] else "e" * 64
                with self.assertRaises(contract.CcCoreEnvironmentError):
                    contract.validate_native_call_summaries(
                        changed, geometry_mode="POINT_TO_POINT",
                        requested_max_threads=8, effective_max_threads=6,
                    )

    def test_native_runner_requires_manifest_dlls_and_matching_binary_hashes(self) -> None:
        """시작 시 Manifest·필수 DLL 누락과 세 필수 바이너리 해시 불일치를 거부합니다."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            executable, _manifest = make_native_runtime(root / "runtime")
            (root / "job").mkdir()
            runner = solver.NativeRunner(root / "job", executable, contract.resolve_parameters({"cc_max_threads": 4}))
            self.assertEqual(runner.runtime_manifest["native_worker_sha256"], solver.file_sha256(executable))
            self.assertEqual(
                runner.runtime_manifest["runtime_manifest_sha256"],
                solver.file_sha256(executable.with_name("runtime_manifest.json")),
            )
            self.assertEqual(runner.runtime_manifest["cccore_dll_sha256"], solver.file_sha256(executable.with_name("CCCoreLib.dll")))
            self.assertEqual(runner.runtime_manifest["tbb_dll_sha256"], solver.file_sha256(executable.with_name("tbb12.dll")))
        for field, invalid_path in (
            ("library_path", r"E:\DEV\DEV_RND\cccore\CCCoreLib.dll"),
            ("tbb_library_path", r"C:\Users\builder\tbb12.dll"),
        ):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                executable, manifest = make_native_runtime(root / "runtime")
                (root / "job").mkdir()
                manifest["self_test"][field] = invalid_path
                contract.write_json_atomic(executable.with_name("runtime_manifest.json"), manifest)
                with self.assertRaisesRegex(contract.CcCoreEnvironmentError, "DLL 상대 식별자"):
                    solver.NativeRunner(root / "job", executable, contract.resolve_parameters({"cc_max_threads": 4}))
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            clean_archive = root / "clean.zip"
            with zipfile.ZipFile(clean_archive, "w") as archive:
                archive.writestr("runtime_manifest.json", '{"library_path":"CCCoreLib.dll"}')
            package_manage.validate_package_archive(clean_archive)
            nested_archive = root / "nested.zip"
            with zipfile.ZipFile(nested_archive, "w") as archive:
                archive.writestr("leak.txt", str(package_manage.ROOT.parent))
            leaking_archive = root / "leaking.zip"
            with zipfile.ZipFile(leaking_archive, "w") as archive:
                archive.write(nested_archive, "cccore_rebuild_sources.zip")
            with self.assertRaisesRegex(RuntimeError, "호스트 절대 경로"):
                package_manage.validate_package_archive(leaking_archive)
        failure_cases = (
            ("Manifest 누락", "runtime_manifest.json", "missing"),
            ("CCCoreLib DLL 누락", "CCCoreLib.dll", "missing"),
            ("oneTBB DLL 누락", "tbb12.dll", "missing"),
            ("워커 해시 불일치", "colmap_cccore_worker.exe", "changed"),
            ("CCCoreLib 해시 불일치", "CCCoreLib.dll", "changed"),
            ("oneTBB 해시 불일치", "tbb12.dll", "changed"),
        )
        for label, filename, mutation in failure_cases:
            with self.subTest(case=label), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                executable, _manifest = make_native_runtime(root / "runtime")
                (root / "job").mkdir()
                target = executable.with_name(filename)
                if mutation == "missing":
                    target.unlink()
                else:
                    target.write_bytes(target.read_bytes() + b"-tampered")
                with self.assertRaises(contract.CcCoreEnvironmentError):
                    solver.NativeRunner(root / "job", executable, contract.resolve_parameters({"cc_max_threads": 4}))

    def test_native_runner_rechecks_worker_and_manifest_before_call(self) -> None:
        """초기화 뒤 워커 또는 Manifest가 바뀌면 네이티브 프로세스를 시작하기 전에 거부합니다."""
        for filename in ("colmap_cccore_worker.exe", "runtime_manifest.json"):
            with self.subTest(filename=filename), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                executable, _manifest = make_native_runtime(root / "runtime")
                (root / "job").mkdir()
                runner = solver.NativeRunner(
                    root / "job",
                    executable,
                    contract.resolve_parameters({"cc_max_threads": 4}),
                )
                target = executable.with_name(filename)
                target.write_bytes(target.read_bytes() + b" ")
                with self.assertRaises(contract.CcCoreEnvironmentError):
                    runner.call("distance", {"point_count": 1}, {"point_count": 1})

    def test_native_identity_requires_mesh_grid_patch_and_matching_sources(self) -> None:
        """구형 실행 환경과 격자 패치·최종 원문 지문 불일치를 실행 전에 거부합니다."""
        cases = ("missing_id", "missing_hash", "missing_sources", "missing_dependency", "wrong_id",
                 "missing_core_patch", "wrong_patch_hash", "wrong_source_hash", "wrong_source_path",
                 "duplicate_source", "invalid_original_hash", "invalid_patched_hash")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temporary:
                executable, manifest = make_native_runtime(Path(temporary) / "runtime")
                identity = solver.native_build_identity(executable)
                self.assertEqual(identity["mesh_grid_memory_patch"], "mesh-grid-memory-budget-v1")
                self.assertEqual(identity["mesh_grid_memory_patch_sha256"], "e" * 64)
                self.assertEqual(identity["mesh_grid_source_sha256"], {"src/DistanceComputationTools.cpp": "a" * 64})
                dependency = manifest["dependencies"]["cccore"]
                grid = dependency["mesh_grid_memory_patch"]
                if case == "missing_id":
                    manifest.pop("mesh_grid_memory_patch")
                elif case == "missing_hash":
                    manifest.pop("mesh_grid_memory_patch_sha256")
                elif case == "missing_sources":
                    manifest.pop("mesh_grid_source_sha256")
                elif case == "missing_dependency":
                    dependency.pop("mesh_grid_memory_patch")
                elif case == "wrong_id":
                    grid["id"] = manifest["mesh_grid_memory_patch"] = "unrecognized-patch"
                elif case == "missing_core_patch":
                    manifest["core_patch"] = manifest["core_patch"].replace("+mesh-grid-memory-budget-v1", "")
                elif case == "wrong_patch_hash":
                    manifest["mesh_grid_memory_patch_sha256"] = "0" * 64
                elif case == "wrong_source_hash":
                    manifest["mesh_grid_source_sha256"]["src/DistanceComputationTools.cpp"] = "0" * 64
                elif case == "wrong_source_path":
                    grid["files"][0]["path"] = "src/Other.cpp"
                elif case == "duplicate_source":
                    grid["files"].append(dict(grid["files"][0]))
                elif case == "invalid_original_hash":
                    grid["files"][0]["original_sha256"] = "invalid"
                elif case == "invalid_patched_hash":
                    grid["files"][0]["patched_sha256"] = "invalid"
                contract.write_json_atomic(executable.with_name("runtime_manifest.json"), manifest)
                with self.assertRaises(contract.CcCoreEnvironmentError):
                    solver.native_build_identity(executable)

    def test_native_identity_requires_complete_mesh_grid_reuse_patch(self) -> None:
        """재사용 패치 누락·원문 연결 오류·최종 지문 변조를 실행 전에 거부합니다."""
        for case in ("missing", "hash", "source", "chain", "files", "core"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temporary:
                executable, manifest = make_native_runtime(Path(temporary) / "runtime")
                self.assertEqual(solver.native_build_identity(executable)["mesh_grid_reuse_patch"], "mesh-grid-reuse-v1")
                reuse = manifest["dependencies"]["cccore"]["mesh_grid_reuse_patch"]
                if case == "missing":
                    manifest.pop("mesh_grid_reuse_patch")
                elif case == "hash":
                    manifest["mesh_grid_reuse_patch_sha256"] = "0" * 64
                elif case == "source":
                    manifest["mesh_grid_reuse_source_sha256"]["src/DistanceComputationTools.cpp"] = "0" * 64
                elif case == "chain":
                    reuse["files"][1]["original_sha256"] = "0" * 64
                elif case == "files":
                    reuse["files"].pop()
                elif case == "core":
                    manifest["core_patch"] = manifest["core_patch"].replace("+mesh-grid-reuse-v1", "")
                contract.write_json_atomic(executable.with_name("runtime_manifest.json"), manifest)
                with self.assertRaises(contract.CcCoreEnvironmentError):
                    solver.native_build_identity(executable)

    def test_native_runner_requires_manifest_fields(self) -> None:
        """시작 시 필수 Manifest 필드가 빠진 v4 런타임을 거부합니다."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            executable, manifest = make_native_runtime(root / "runtime")
            (root / "job").mkdir()
            manifest.pop("core_commit")
            contract.write_json_atomic(executable.with_name("runtime_manifest.json"), manifest)
            with self.assertRaisesRegex(contract.CcCoreEnvironmentError, "필수 필드"):
                solver.NativeRunner(root / "job", executable, contract.resolve_parameters({"cc_max_threads": 4}))

    def test_normalization_conjugation_and_reverse_composition(self) -> None:
        """큰 전역 좌표에서도 H 복원과 역방향 두 단계 순서를 확인합니다."""
        vertices, _faces = box_mesh()
        world = vertices + 1.0e9
        normalization = solver.make_normalization(world, world + 0.1)
        forward = known_delta()
        rigid = known_delta(1.0)
        reverse_rigid = np.linalg.inv(rigid)
        reverse_total = np.linalg.inv(forward)
        reverse_refine = reverse_total @ np.linalg.inv(reverse_rigid)
        restored = np.linalg.inv(reverse_refine @ reverse_rigid)
        np.testing.assert_allclose(restored, forward, atol=1.0e-12)
        expected = solver.transform_positions(normalization.apply(world), forward)
        actual_world = solver.transform_positions(world, normalization.world_delta(forward))
        np.testing.assert_allclose(normalization.apply(actual_world), expected, atol=1.0e-7)

    def test_native_projection_rejects_shear_and_reflection(self) -> None:
        """작은 반올림만 보정하고 전단이나 반사는 거부하는지 확인합니다."""
        delta = known_delta()
        delta[0, 0] += 1.0e-9
        normalized, correction = solver.normalize_native_sim3(delta)
        self.assertGreater(correction, 0.0)
        contract.matrix_sim3(normalized, "검사", tolerance=1.0e-10)
        for bad in (np.diag((-1.0, 1.0, 1.0, 1.0)), np.diag((1.0, 1.001, 1.0, 1.0))):
            with self.assertRaises(contract.CcCoreNumericalError):
                solver.normalize_native_sim3(bad)

    def test_quality_requires_mean_improvement_and_keeps_better_rigid(self) -> None:
        """평균 오차 감소 없는 자동 승인과 더 나쁜 배율 결과 채택을 막습니다."""
        diagnostics = {"additional_scale": 1.0}
        best, quality, reasons, _improvement, _stop = solver.select_candidate({
            "baseline": simple_metrics(0.1), "rigid": simple_metrics(0.1), "scale_refine": simple_metrics(0.1),
        }, diagnostics)
        self.assertEqual((best, quality), ("baseline", "REVIEW"))
        self.assertTrue(reasons)
        best, quality, reasons, _improvement, _stop = solver.select_candidate({
            "baseline": simple_metrics(0.1), "rigid": simple_metrics(0.03), "scale_refine": simple_metrics(0.04),
        }, diagnostics)
        self.assertEqual((best, quality), ("rigid", "REVIEW"))
        self.assertTrue(any("배율" in reason for reason in reasons))

    def test_quality_catches_tail_and_spatial_regression(self) -> None:
        """평균 RMS가 줄어도 꼬리 또는 특정 공간의 악화를 검토로 표시합니다."""
        metrics = {
            "baseline": simple_metrics(0.1, 0.12),
            "rigid": simple_metrics(0.07, 0.17),
            "scale_refine": simple_metrics(0.05, 0.16),
        }
        metrics["scale_refine"]["directions"]["moving_to_reference"]["spatial_bins"][0]["rms_normalized"] = 0.2
        _best, quality, reasons, _improvement, _stop = solver.select_candidate(metrics, {"additional_scale": 1.03})
        self.assertEqual(quality, "REVIEW")
        self.assertTrue(any("상위" in reason for reason in reasons))
        self.assertTrue(any("공간" in reason for reason in reasons))

    def test_candidate_selection_rejects_directional_coverage_collapse(self) -> None:
        """오차가 작아도 어느 평가 방향에서든 대응 수나 공간 분포를 잃은 후보는 제외합니다."""
        policy = {
            "minimum_coverage_retention": 0.9, "minimum_correspondence_count": 30,
            "minimum_valid_correspondence_ratio": 0.2, "minimum_pass_correspondence_ratio": 0.9,
            "minimum_covered_spatial_bins": 3, "minimum_spatial_span_ratio": 0.25,
        }
        coverage = {
            "valid_count": 100, "count": 100, "valid_ratio": 1.0,
            "covered_spatial_bins": 8, "occupied_spatial_bins": 8,
            "spatial_span_ratios": [1.0, 1.0, 1.0],
        }
        for direction in ("moving_to_reference", "reference_to_moving"):
            for field, value in (("valid_count", 89), ("covered_spatial_bins", 2),
                                 ("spatial_span_ratios", [1.0, 1.0, 0.1])):
                metrics = {"baseline": simple_metrics(0.1), "rigid": simple_metrics(0.05),
                           "scale_refine": simple_metrics(0.001)}
                for entry in metrics.values():
                    entry["directions"]["reference_to_moving"] = copy.deepcopy(entry["directions"]["moving_to_reference"])
                    entry["directional_coverage"] = {name: copy.deepcopy(coverage) for name in entry["directions"]}
                metrics["scale_refine"]["directional_coverage"][direction][field] = value
                with self.subTest(direction=direction, field=field):
                    best, quality, reasons, _, _ = solver.select_candidate(metrics, {"additional_scale": 1.0}, policy)
                    self.assertEqual((best, quality), ("rigid", "REVIEW"))
                    self.assertTrue(any("제외" in reason for reason in reasons))

    def test_full_reference_error_does_not_override_overlap_selection(self) -> None:
        """고정 겹침 오차가 같은 후보의 전체 참고 오차를 바꿔도 선택·승인은 같습니다."""
        metrics = {"baseline": simple_metrics(0.1), "rigid": simple_metrics(0.05),
                   "scale_refine": simple_metrics(0.02)}
        expected = solver.select_candidate(metrics, {"additional_scale": 1.03})
        metrics["baseline"]["full_reference_metrics"] = simple_metrics(0.0001)
        metrics["rigid"]["full_reference_metrics"] = simple_metrics(1.0)
        metrics["scale_refine"]["full_reference_metrics"] = simple_metrics(1000000.0)
        self.assertEqual(solver.select_candidate(metrics, {"additional_scale": 1.03}), expected)
        self.assertEqual(expected[:2], ("scale_refine", "PASS"))

    def test_file_contract_rejects_traversal_and_wrong_binary_size(self) -> None:
        """경로 이탈과 배열 크기 불일치를 계산 전에 거부하는지 확인합니다."""
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            manifest, _delta = make_job(directory, "COLMAP_POINT_CLOUD", "TRIANGLE_MESH")
            contract.validate_manifest(manifest, directory)
            for bad in ("../outside.bin", "..\\outside.bin", "C:\\outside.bin", "/outside.bin", "file:stream"):
                with self.assertRaises(contract.CcCoreInputError):
                    contract.job_path(directory, bad)
            (directory / "moving.bin").write_bytes(b"short")
            with self.assertRaises(contract.CcCoreInputError):
                contract.load_geometry(directory, manifest["geometry"]["moving"])

    def test_manifest_enforces_geometry_direction_and_positive_sim3(self) -> None:
        """메시 역방향 계약과 시작 배율의 양수 조건을 검증합니다."""
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            manifest, _delta = make_job(directory, "TRIANGLE_MESH", "COLMAP_POINT_CLOUD")
            bad = copy.deepcopy(manifest)
            bad["reverse_registration"] = False
            with self.assertRaises(contract.CcCoreInputError):
                contract.validate_manifest(bad, directory)
            bad = copy.deepcopy(manifest)
            bad["initial_alignment_4x4"][0][0] = -2.0
            with self.assertRaises(contract.CcCoreNumericalError):
                contract.validate_manifest(bad, directory)

    def test_coverage_rejects_missing_and_degenerate_correspondences(self) -> None:
        """원본 공간을 덮지 않는 거리 대응을 순수 분포 계산으로 거부합니다."""
        axis = np.linspace(-1.0, 1.0, 5)
        points = np.stack(np.meshgrid(axis, axis, axis, indexing="ij"), axis=-1).reshape(-1, 3)
        policy = {
            **solver.QUALITY_POLICY, "correspondence_radius_normalized": 0.05,
            "minimum_valid_correspondence_ratio": 0.2, "minimum_pass_correspondence_ratio": 0.765,
            "minimum_spatial_span_ratio": 0.25, "minimum_covered_spatial_bins": 3,
            "minimum_bin_valid_ratio": 0.2,
        }
        for distances, reason in (
            (np.ones(len(points)), "대응점"),
            (np.abs(points[:, 2]), "접촉"),
        ):
            with self.subTest(reason=reason):
                coverage = solver.correspondence_coverage(points, distances, policy)
                with self.assertRaisesRegex(contract.CcCoreNumericalError, reason):
                    solver.assess_correspondence_coverage({"correspondence_coverage": coverage}, policy)

    @unittest.skipUnless(os.name == "nt", "Windows 공유 읽기 회귀입니다.")
    def test_json_reader_and_atomic_writer_coordinate(self) -> None:
        """짧은 읽기 핸들이 닫히면 원자 교체가 완료되고 두 JSON이 온전히 유지되는지 확인합니다."""
        from threading import Timer
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "progress.json"
            contract.write_json_atomic(path, {"단계": "이전"})
            with contract._open_json_shared(path) as stream:
                previous = json.load(stream)
                release = Timer(0.15, stream.close)
                release.start()
                try:
                    contract.write_json_atomic(path, {"단계": "다음"})
                finally:
                    release.join()
            self.assertEqual(previous["단계"], "이전")
            self.assertEqual(contract.read_json_object(path)["단계"], "다음")

    @unittest.skipUnless(os.name == "nt", "Windows 공유 잠금 회귀입니다.")
    def test_json_reader_retries_short_lock_and_rejects_persistent_lock(self) -> None:
        """실제 150밀리초 공유 잠금은 기다리고 800밀리초 잠금은 제한 시간에 실패합니다."""
        import ctypes
        from ctypes import wintypes
        from threading import Timer
        from time import monotonic
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateFileW.argtypes = (
            wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
            wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
        )
        kernel.CreateFileW.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        for seconds in (0.15, 0.8):
            with self.subTest(seconds=seconds), tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / "progress.json"
                contract.write_json_atomic(path, {"단계": "진행"})
                handle = kernel.CreateFileW(str(path), 0x80000000, 0, None, 3, 0x80, None)
                self.assertNotEqual(handle, ctypes.c_void_p(-1).value)
                release = Timer(seconds, kernel.CloseHandle, args=(handle,))
                release.start()
                started = monotonic()
                try:
                    if seconds < 0.24:
                        self.assertEqual(contract.read_json_object(path)["단계"], "진행")
                        self.assertGreater(monotonic() - started, 0.1)
                    else:
                        with self.assertRaises(contract.CcCoreInputError):
                            contract.read_json_object(path)
                        self.assertLess(monotonic() - started, 0.7)
                finally:
                    release.join()

    def test_worker_import_does_not_load_blender(self) -> None:
        """직접 worker 부트스트랩이 Blender 의존성을 불러오지 않는지 확인합니다."""
        self.assertNotIn("bpy", sys.modules)
        self.assertNotIn("align.alignment", sys.modules)

    def test_atomic_result_contains_only_declared_arrays(self) -> None:
        """UTF-8 결과와 두 행렬 NPZ를 손실 없이 읽을 수 있는지 확인합니다."""
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            contract.write_json_atomic(directory / "result.json", {"message": "정렬 완료"})
            self.assertEqual(contract.read_json_object(directory / "result.json")["message"], "정렬 완료")
            contract.write_arrays_atomic(directory / "result.npz", {"delta_4x4": np.eye(4), "final_alignment_4x4": np.eye(4)})
            with np.load(directory / "result.npz", allow_pickle=False) as arrays:
                self.assertEqual(set(arrays.files), {"delta_4x4", "final_alignment_4x4"})


def native_child_pids(parent_pid: int) -> list[int]:
    """Windows 프로세스 스냅샷에서 해당 작업의 네이티브 자식만 찾습니다."""
    import ctypes
    from ctypes import wintypes

    class ProcessEntry(ctypes.Structure):
        """Windows 프로세스 스냅샷 항목의 메모리 배열입니다."""
        _fields_ = [
            ("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260),
        ]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateToolhelp32Snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel.Process32FirstW.argtypes = (wintypes.HANDLE, ctypes.POINTER(ProcessEntry))
    kernel.Process32NextW.argtypes = (wintypes.HANDLE, ctypes.POINTER(ProcessEntry))
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    handle = kernel.CreateToolhelp32Snapshot(2, 0)
    if handle == ctypes.c_void_p(-1).value:
        raise OSError("프로세스 스냅샷을 만들지 못했습니다.")
    entry = ProcessEntry()
    entry.dwSize = ctypes.sizeof(entry)
    pids = []
    try:
        available = kernel.Process32FirstW(handle, ctypes.byref(entry))
        while available:
            if entry.th32ParentProcessID == parent_pid and entry.szExeFile.lower() == "colmap_cccore_worker.exe":
                pids.append(int(entry.th32ProcessID))
            available = kernel.Process32NextW(handle, ctypes.byref(entry))
    finally:
        kernel.CloseHandle(handle)
    return pids


def pid_is_running(pid: int) -> bool:
    """종료된 네이티브 프로세스의 운영체제 핸들이 신호 상태인지 확인합니다."""
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    handle = kernel.OpenProcess(0x00100000, False, pid)
    if not handle:
        return False
    try:
        return kernel.WaitForSingleObject(handle, 0) == 258
    finally:
        kernel.CloseHandle(handle)


NATIVE_WORKER = os.environ.get("CCCORE_NATIVE_WORKER", "")


@unittest.skipUnless(NATIVE_WORKER and Path(NATIVE_WORKER).is_file(), "실제 CC 실행 경로가 지정되지 않았습니다.")
class NativeIntegrationTests(unittest.TestCase):
    """실제 CC 바이너리로 coordinator 전체 흐름을 검증합니다."""

    def test_overlap_center_repacking_preserves_native_boundary_and_samples(self) -> None:
        """경계상 중심의 묶음 재구성이 실제 거리·후보 순서·결정적 표본을 바꾸지 않는지 확인합니다."""
        from dataclasses import replace
        import icp_cccore_overlap as overlap

        vertices, triangles = box_mesh()
        faces = vertices[triangles]
        centers = faces.mean(axis=1)
        patches = centers[:, None] + (faces - centers[:, None]) * 0.02
        patches += np.array((0.0, 0.0, 0.025))
        # 외딴 면을 사이에 넣어 초기 경계 상자 필터가 재조회와 다른 묶음을 만들게 합니다.
        interleaved = np.stack((patches, patches + 20.0), axis=1).reshape(-1, 3, 3)
        geometry = contract.CcCoreGeometry(
            "TRIANGLE_MESH", interleaved.reshape(-1, 3),
            np.arange(interleaved.size // 3, dtype=np.uint32).reshape(-1, 3), {},
        )
        parameters = contract.resolve_parameters({"cc_max_threads": 1})
        with tempfile.TemporaryDirectory() as temporary:
            with solver.NativeRunner(Path(temporary), NATIVE_WORKER, parameters) as runner:
                model = runner.geometry("repacking-model", vertices, triangles)
                query_index = 0

                def query(points):
                    """각 중심·표본 묶음의 실제 CCCore 거리를 독립 형상으로 조회합니다."""
                    nonlocal query_index
                    query_index += 1
                    data = runner.geometry(f"repacking-query-{query_index}", points)
                    return runner.call("distance", data, model, stage="PREPARING")["distances"]

                patch_centers = patches.mean(axis=1)
                radii = np.linalg.norm(patches - patch_centers[:, None], axis=2).max(axis=1)
                distances = query(patch_centers)
                repacked = np.concatenate([query(patch_centers[start:start + 3])
                                           for start in range(0, len(patches), 3)])
                np.testing.assert_array_equal(repacked, distances)
                threshold = float(distances[0] + radii[0])
                bounds = np.array((vertices.min(axis=0), vertices.max(axis=0)))
                for radius in (np.nextafter(threshold, 0.0), threshold, np.nextafter(threshold, np.inf)):
                    with self.subTest(radius=radius):
                        domains = [overlap.build_overlap_domain(
                            geometry, bounds, radius, query, batch_size=batch_size,
                            max_subdivision_depth=1, max_candidate_patches=18,
                        ) for batch_size in (5, 8192)]
                        first, second = domains
                        first_face_count = int(np.count_nonzero(first.source_indices == 0))
                        self.assertEqual(first_face_count, 1 if radius >= threshold else 4)
                        self.assertLess(first.metadata["initial_candidate_face_count"], len(interleaved))
                        np.testing.assert_array_equal(first.patches, second.patches)
                        np.testing.assert_array_equal(first.source_indices, second.source_indices)
                        np.testing.assert_array_equal(first.patch_areas, second.patch_areas)
                        self.assertEqual(
                            {key: value for key, value in first.metadata.items() if key != "distance_batch_size"},
                            {key: value for key, value in second.metadata.items() if key != "distance_batch_size"},
                        )
                        # 표본 제안 묶음은 고정하여 후보 구성 단계의 재구성만 비교합니다.
                        samples = [overlap.sample_overlap_domain(
                            replace(domain, metadata={**domain.metadata, "distance_batch_size": 8192}),
                            32, contract.CC_OPTIMIZER_SEED, query,
                        ) for domain in domains]
                        np.testing.assert_array_equal(samples[0].positions, samples[1].positions)
                        np.testing.assert_array_equal(samples[0].distances, samples[1].distances)
                        np.testing.assert_array_equal(samples[0].source_indices, samples[1].source_indices)
                        self.assertEqual(samples[0].fingerprint, samples[1].fingerprint)

    def test_mesh_cleanup_before_native_registration(self) -> None:
        """극단 외딴 퇴화면과 단정도 재퇴화면을 제외하고 원본 파일 보존 및 실제 정렬을 검증합니다."""
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            manifest, expected = make_job(directory, "TRIANGLE_MESH", "TRIANGLE_MESH")
            for role in ("moving", "reference"):
                entry = manifest["geometry"][role]
                positions = np.fromfile(directory / entry["positions_file"], dtype="<f8").reshape(-1, 3)
                triangles = np.fromfile(directory / entry["triangles_file"], dtype="<u4").reshape(-1, 3)
                extra = np.asarray(((1e20, 0, 0), (1e20 + 32768, 0, 0), (1e20 + 65536, 0, 0),
                                    (0.5, 0.5, 0.5), (0.5 + 1e-12, 0.5, 0.5),
                                    (0.5, 0.5 + 1e-12, 0.5)))
                positions = np.vstack((positions, extra))
                triangles = np.vstack((triangles, ((8, 9, 10), (11, 12, 13)))).astype("<u4")
                positions.astype("<f8").tofile(directory / entry["positions_file"])
                triangles.tofile(directory / entry["triangles_file"])
                entry.update(point_count=len(positions), triangle_count=len(triangles))
            contract.write_json_atomic(directory / "manifest.json", manifest)
            source_paths = [directory / "manifest.json"]
            for role in ("moving", "reference"):
                entry = manifest["geometry"][role]
                source_paths.extend(directory / entry[key] for key in ("positions_file", "triangles_file"))
            source_hashes = {path: hashlib.sha256(path.read_bytes()).digest() for path in source_paths}
            worker.run_cccore_worker(directory, NATIVE_WORKER)
            self.assertEqual(source_hashes, {path: hashlib.sha256(path.read_bytes()).digest() for path in source_paths})
            with np.load(directory / "result.npz", allow_pickle=False) as arrays:
                np.testing.assert_allclose(arrays["delta_4x4"], expected, atol=8.0e-4)
            result = contract.read_json_object(directory / "result.json")
            self.assertEqual(result["quality"], "PASS")
            model_stats = [item for item in result["geometry_statistics"] if item["triangle_count"]]
            self.assertTrue(model_stats)
            self.assertTrue(all(item["point_count"] == 8 and item["triangle_count"] == 12 for item in model_stats))

    def test_inverse_surface_query_matches_transformed_mesh_distances(self) -> None:
        """역변환 질의·배율 복원 거리가 기존 후보별 변형 메시 거리와 허용오차 안에서 같은지 확인합니다."""
        vertices, triangles = box_mesh()
        rng = np.random.default_rng(1729)
        queries = rng.uniform(np.min(vertices, axis=0) - .2, np.max(vertices, axis=0) + .2, size=(4096, 3))
        parameters = contract.resolve_parameters({"cc_max_threads": min(4, os.cpu_count() or 1)})
        with tempfile.TemporaryDirectory() as temporary:
            with solver.NativeRunner(Path(temporary), NATIVE_WORKER, parameters) as runner:
                original_model = runner.geometry("moving-model", vertices, triangles)
                for index, scale in enumerate((.9, 1.0, 1.1), start=1):
                    delta = known_delta(scale)
                    old_query = runner.geometry(f"old-query-{index}", queries)
                    old_model = runner.geometry(
                        f"old-transformed-model-{index}", solver.transform_positions(vertices, delta), triangles,
                    )
                    old_distances = runner.call(
                        "distance", old_query, old_model, stage="INVERSE_EQUIVALENCE",
                    )["distances"]
                    inverse_query, restored_scale = solver.inverse_surface_query(queries, delta)
                    new_query = runner.geometry(f"inverse-query-{index}", inverse_query)
                    new_distances = runner.call(
                        "distance", new_query, original_model, stage="INVERSE_EQUIVALENCE",
                    )["distances"] * restored_scale
                    np.testing.assert_allclose(new_distances, old_distances, rtol=0.0, atol=2.0e-6)

    def test_identity_mesh_job_reuses_exact_candidate_evaluations(self) -> None:
        """동일 메시의 항등 후보 네 평가를 캐시하면서 준비·등록 전 거리는 실제 실행합니다."""
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            _manifest, _expected = make_job(directory, "TRIANGLE_MESH", "TRIANGLE_MESH")
            (directory / "reference.bin").write_bytes((directory / "moving.bin").read_bytes())
            result = solver.solve_cccore_job(directory, NATIVE_WORKER)
            np.testing.assert_allclose(result.delta_4x4, np.identity(4), atol=2.0e-6, rtol=0.0)
            logical = result.metadata["logical_operations"]
            preparing_count = sum(item["stage"] == "PREPARING" for item in logical)
            self.assertGreaterEqual(preparing_count, 1)
            self.assertLessEqual(preparing_count, contract.CC_MAX_PREPARING_OPERATIONS)
            validated = contract.validate_native_call_summaries(
                logical, geometry_mode="MESH_TO_MESH",
                requested_max_threads=result.metadata["requested_max_threads"],
                effective_max_threads=result.metadata["effective_max_threads"],
            )
            self.assertEqual(len(logical), preparing_count + 10)
            self.assertEqual(result.metadata["physical_native_command_count"], preparing_count + 6)
            contract.validate_physical_native_call_summaries(
                result.metadata["physical_native_calls"], validated,
                requested_max_threads=result.metadata["requested_max_threads"],
                effective_max_threads=result.metadata["effective_max_threads"],
            )
            self.assertEqual(result.metadata["exact_evaluation_cache"], {
                "policy": "BIT_EXACT_MATRIX_AND_EVALUATION_CONTRACT",
                "entry_count": 2, "hit_count": 4, "miss_count": 2,
            })
            cached = [
                item for item in result.metadata["logical_operations"]
                if item["execution_source"] == "exact_evaluation_cache"
            ]
            self.assertEqual(
                [(item["index"], item["source_logical_index"], item["source_sequence"]) for item in cached],
                [(preparing_count + index, preparing_count + origin, preparing_count + origin)
                 for index, origin in ((5, 1), (6, 2), (9, 1), (10, 2))],
            )
            by_logical = {item["logical_index"]: item for item in result.metadata["physical_native_calls"]}
            for item in cached:
                source = by_logical[item["source_logical_index"]]
                self.assertEqual(item["roundtrip_seconds"], source["roundtrip_seconds"])
                self.assertEqual(item["protocol_overhead_seconds"], source["protocol_overhead_seconds"])
            contract.validate_session_protocol_statistics(
                result.metadata["session_protocol_statistics"],
                tuple(result.metadata["physical_native_calls"]),
                total_alignment_seconds=result.metadata["elapsed_seconds"],
            )

    def test_all_four_geometry_combinations(self) -> None:
        """네 조합의 정밀도와 유한한 후보 준비·고정 등록 골격·단일 합성을 확인합니다."""
        for moving_kind, reference_kind in contract.GEOMETRY_MODES:
            with self.subTest(moving=moving_kind, reference=reference_kind), tempfile.TemporaryDirectory() as temporary:
                directory = Path(temporary)
                manifest, expected = make_job(directory, moving_kind, reference_kind)
                worker.run_cccore_worker(directory, NATIVE_WORKER)
                result = contract.read_json_object(directory / "result.json")
                progress = contract.read_json_object(directory / "progress.json")
                with np.load(directory / "result.npz", allow_pickle=False) as arrays:
                    delta = arrays["delta_4x4"]
                    final = arrays["final_alignment_4x4"]
                np.testing.assert_allclose(delta, expected, atol=8.0e-4)
                np.testing.assert_allclose(final, delta @ np.asarray(manifest["initial_alignment_4x4"]), atol=1.0e-12)
                self.assertGreater(result["relative_improvement"], 0.96)
                self.assertEqual(result["reverse_registration"], moving_kind == "TRIANGLE_MESH" and reference_kind == "COLMAP_POINT_CLOUD")
                self.assertEqual(result["best_stage"], "scale_refine")
                self.assertEqual(result["quality"], "PASS")
                self.assertEqual(progress["stage"], "COMPLETED")
                np.testing.assert_array_equal(progress["stable_alignment_4x4"], final)
                self.assertEqual(len(result["native_worker_sha256"]), 64)
                self.assertEqual(result["native_process_count"], 1)
                self.assertEqual(result["native_calls"], result["logical_operations"])
                self.assertEqual(result["physical_native_command_count"], len(result["physical_native_calls"]))
                self.assertLessEqual(result["physical_native_command_count"], len(result["logical_operations"]))
                self.assertEqual(result["physical_native_command_count"], len(result["logical_operations"]))
                self.assertEqual(result["exact_evaluation_cache"]["hit_count"], 0)
                self.assertEqual(
                    [item["logical_index"] for item in result["physical_native_calls"]],
                    [
                        item["index"] for item in result["logical_operations"]
                        if item["execution_source"] == "native_session"
                    ],
                )
                self.assertEqual(
                    tuple(item["operation"] for item in result["logical_operations"] if item["stage"] != "PREPARING"),
                    contract.CC_NATIVE_OPERATION_SEQUENCES[result["geometry_mode"]],
                )
                self.assertEqual(
                    tuple(item["stage"] for item in result["logical_operations"] if item["stage"] != "PREPARING"),
                    contract.CC_NATIVE_OPERATION_STAGES[result["geometry_mode"]],
                )
                validated = contract.validate_native_call_summaries(
                    result["logical_operations"], geometry_mode=result["geometry_mode"],
                    requested_max_threads=result["requested_max_threads"],
                    effective_max_threads=result["effective_max_threads"],
                )
                contract.validate_physical_native_call_summaries(
                    result["physical_native_calls"], validated,
                    requested_max_threads=result["requested_max_threads"],
                    effective_max_threads=result["effective_max_threads"],
                )
                self.assertEqual(result["session_exit_reason"], "closed")
                self.assertEqual(result["session_statistics"]["geometry_load_count"], len(result["geometry_statistics"]))
                self.assertGreater(result["session_statistics"]["geometry_cache_hits"], 0)
                self.assertEqual(
                    result["session_protocol_statistics"],
                    {name: result[name] for name in (
                        "physical_roundtrip_seconds", "protocol_overhead_seconds",
                        "protocol_overhead_ratio_of_roundtrip",
                        "protocol_overhead_fraction_of_total_alignment",
                    )},
                )
                contract.validate_session_protocol_statistics(
                    result["session_protocol_statistics"], tuple(result["physical_native_calls"]),
                    total_alignment_seconds=result["elapsed_seconds"],
                )
                self.assertEqual(
                    result["exact_evaluation_cache"]["hit_count"],
                    sum(
                        item["execution_source"] == "exact_evaluation_cache"
                        for item in result["logical_operations"]
                    ),
                )
                self.assertTrue(all(
                    item["released_after_command"] is (item["cache_scope"] == "COMMAND")
                    for item in result["geometry_statistics"]
                ))
                self.assertEqual(
                    result["session_statistics"]["cache_resident_estimated_bytes"],
                    sum(
                        item["estimated_bytes"] for item in result["geometry_statistics"]
                        if item["cache_scope"] == "SESSION"
                    ),
                )
                if result["geometry_mode"] == "POINT_TO_POINT":
                    by_id = {item["geometry_id"]: item for item in result["geometry_statistics"]}
                    self.assertEqual(by_id["spacing-query"]["cache_scope"], "COMMAND")
                    self.assertEqual(by_id["spacing-model"]["cache_scope"], "COMMAND")
                    self.assertTrue(by_id["spacing-query"]["released_after_command"])
                    self.assertTrue(by_id["spacing-model"]["released_after_command"])
                if moving_kind == "TRIANGLE_MESH":
                    self.assertTrue((directory / "native/moving-model-positions.bin").is_file())
                    for label in ("baseline", "rigid", "scale-refine"):
                        self.assertFalse((directory / f"native/{label}-moving-model-positions.bin").exists())
                print(json.dumps({
                    "조합": result["geometry_mode"], "시간": result["elapsed_seconds"],
                    "개선율": result["relative_improvement"], "배율": result["diagnostics"]["additional_scale"],
                }, ensure_ascii=True))

    def test_single_and_parallel_solver_results_are_equivalent(self) -> None:
        """동일 입력의 1스레드와 멀티스레드 결과가 정한 수치·품질 기준 안에서 같은지 확인합니다."""
        parallel_threads = min(4, os.cpu_count() or 1)
        if parallel_threads < 2:
            self.skipTest("멀티스레드 비교에 사용할 논리 프로세서가 없습니다.")
        results = []
        for requested_threads in (1, parallel_threads):
            with tempfile.TemporaryDirectory() as temporary:
                directory = Path(temporary)
                manifest, _expected = make_job(directory, "TRIANGLE_MESH", "TRIANGLE_MESH")
                manifest["parameters"] = {"cc_max_threads": requested_threads}
                contract.write_json_atomic(directory / "manifest.json", manifest)
                result = solver.solve_cccore_job(directory, NATIVE_WORKER)
                self.assertEqual(result.metadata["requested_max_threads"], requested_threads)
                self.assertEqual(result.metadata["thread_backend"], "oneTBB")
                self.assertEqual(result.metadata["tbb_version"], "2022.0.0")
                self.assertTrue(result.metadata["native_calls"])
                results.append(result)
        single, parallel = results
        single_scale = single.metadata["diagnostics"]["additional_scale"]
        parallel_scale = parallel.metadata["diagnostics"]["additional_scale"]
        self.assertLessEqual(abs(single_scale - parallel_scale), 1.0e-5)
        vertices, _triangles = box_mesh()
        difference = solver.transform_positions(vertices, single.delta_4x4) - solver.transform_positions(
            vertices,
            parallel.delta_4x4,
        )
        normalized_rms = float(np.sqrt(np.mean(np.sum(difference * difference, axis=1)))) / float(
            np.linalg.norm(np.ptp(vertices, axis=0))
        )
        self.assertLessEqual(normalized_rms, 1.0e-4)
        single_rms = float(single.metadata["best_candidate_metrics"]["mean_rms"])
        parallel_rms = float(parallel.metadata["best_candidate_metrics"]["mean_rms"])
        self.assertLessEqual(abs(single_rms - parallel_rms), max(single_rms, np.finfo(float).eps) * 0.001)
        self.assertEqual(single.metadata["quality"], parallel.metadata["quality"])
        self.assertEqual(single.metadata["best_stage"], parallel.metadata["best_stage"])

    def test_cancelled_job_produces_no_result(self) -> None:
        """계산 시작 전 취소 요청이 성공 결과로 노출되지 않는지 확인합니다."""
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            make_job(directory, "TRIANGLE_MESH", "TRIANGLE_MESH")
            (directory / "cancel.request").touch()
            self.assertEqual(worker.main(["--job-dir", str(directory), "--native-worker", NATIVE_WORKER]), 0)
            self.assertFalse((directory / "result.json").exists())
            self.assertFalse((directory / "result.npz").exists())
            self.assertEqual(contract.read_json_object(directory / "error.json")["status"], "CANCELLED")

    def test_disjoint_shape_and_degenerate_contact_are_rejected(self) -> None:
        """입체 점군과 평면의 작은 접촉을 정렬 성공으로 반환하지 않는지 확인합니다."""
        for half_size in (0.03, 2.0):
            with self.subTest(half_size=half_size), tempfile.TemporaryDirectory() as temporary:
                directory = Path(temporary)
                manifest, _delta = make_job(directory, "COLMAP_POINT_CLOUD", "TRIANGLE_MESH")
                axis = np.linspace(-1.0, 1.0, 9)
                volume = np.stack(np.meshgrid(axis, axis, axis, indexing="ij"), axis=-1).reshape(-1, 3)
                plane = np.array(((-half_size, -half_size, 0), (half_size, -half_size, 0),
                                  (half_size, half_size, 0), (-half_size, half_size, 0)), dtype=np.float64)
                triangles = np.array(((0, 1, 2), (0, 2, 3)), dtype=np.uint32)
                volume.astype("<f8").tofile(directory / "moving.bin")
                plane.astype("<f8").tofile(directory / "reference.bin")
                triangles.astype("<u4").tofile(directory / "reference-triangles.bin")
                manifest["geometry"]["moving"]["point_count"] = len(volume)
                manifest["geometry"]["reference"]["point_count"] = len(plane)
                manifest["geometry"]["reference"]["triangle_count"] = len(triangles)
                contract.write_json_atomic(directory / "manifest.json", manifest)
                with self.assertRaises(contract.CcCoreNumericalError):
                    solver.solve_cccore_job(directory, NATIVE_WORKER)
                self.assertFalse((directory / "result.json").exists())

    def test_unrelated_reference_part_does_not_require_reverse_overlap(self) -> None:
        """복수 기준점의 먼 부분이 이동→기준 정상 대응을 강제로 거부하지 않게 합니다."""
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            manifest, _expected = make_job(directory, "COLMAP_POINT_CLOUD", "COLMAP_POINT_CLOUD")
            reference = np.fromfile(directory / "reference.bin", dtype="<f8").reshape(-1, 3)
            combined = np.vstack((reference, reference + np.array((100.0, 70.0, 30.0))))
            combined.astype("<f8").tofile(directory / "reference.bin")
            manifest["geometry"]["reference"]["point_count"] = len(combined)
            contract.write_json_atomic(directory / "manifest.json", manifest)
            result = solver.solve_cccore_job(directory, NATIVE_WORKER)
            self.assertEqual(result.metadata["quality"], "PASS")
            self.assertEqual(set(result.metadata["best_candidate_metrics"]["directions"]), {"moving_to_reference"})
            self.assertAlmostEqual(result.metadata["best_candidate_metrics"]["correspondence_coverage"]["valid_ratio"], 1.0)
            self.assertEqual(result.metadata["quality_policy"]["primary_direction"], "moving_to_reference")

    def test_large_world_coordinates_keep_local_accuracy(self) -> None:
        """큰 원점에서도 정규화된 실제 표면 정렬의 국소 정확도가 유지되는지 확인합니다."""
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            _manifest, expected = make_job(directory, "COLMAP_POINT_CLOUD", "TRIANGLE_MESH", offset=1.0e9)
            result = solver.solve_cccore_job(directory, NATIVE_WORKER)
            vertices, _triangles = box_mesh()
            source = vertices + 1.0e9
            np.testing.assert_allclose(
                solver.transform_positions(source, result.delta_4x4),
                solver.transform_positions(source, expected), atol=5.0e-5, rtol=0.0,
            )
            self.assertGreater(result.metadata["relative_improvement"], 0.99)

    def test_initial_scale_gap_without_overlap_is_rejected_in_both_directions(self) -> None:
        """초기 배율 차이로 실제 겹침을 확보하지 못하면 정·역방향 모두 결과를 만들지 않습니다."""
        for moving_kind, reference_kind in (
            ("COLMAP_POINT_CLOUD", "TRIANGLE_MESH"), ("TRIANGLE_MESH", "COLMAP_POINT_CLOUD"),
        ):
            for target in (0.85, 1.15):
                with self.subTest(mode=(moving_kind, reference_kind), target=target), tempfile.TemporaryDirectory() as temporary:
                    directory = Path(temporary)
                    make_job(directory, moving_kind, reference_kind, scale=target)
                    expected_error = "겹침 표본" if target == 0.85 else "가까운 대응 비율"
                    with self.assertRaisesRegex(contract.CcCoreError, expected_error):
                        solver.solve_cccore_job(directory, NATIVE_WORKER)
                    self.assertFalse((directory / "result.json").exists())
                    self.assertFalse((directory / "result.npz").exists())

    @unittest.skipUnless(os.name == "nt", "Windows 자식 프로세스 수명 회귀입니다.")
    def test_normal_session_close_response_exit_repeat(self) -> None:
        """실제 소형 세션의 정상 close 응답과 exit 0 경계를 반복해 모두 수용합니다."""
        parameters = contract.resolve_parameters({"cc_max_threads": 1})
        points = np.asarray(box_mesh()[0], dtype=np.float64)
        for attempt in range(16):
            with self.subTest(attempt=attempt), tempfile.TemporaryDirectory() as temporary:
                runner = solver.NativeRunner(Path(temporary), NATIVE_WORKER, parameters)
                runner.start()
                data = runner.geometry("data", points)
                model = runner.geometry("model", points)
                runner.call("distance", data, model, stage="PREPARING")
                runner.close()
                self.assertTrue(runner.closed)
                self.assertEqual(runner.session_exit_reason, "closed")

    @unittest.skipUnless(os.name == "nt", "Windows 자식 프로세스 수명 회귀입니다.")
    def test_actual_start_and_close_cancellation_boundaries_repeat(self) -> None:
        """실제 세션의 start·close 취소 경계를 반복해 모두 취소 상태로 분류합니다."""
        from time import monotonic, sleep

        class Lifetime:
            """이미 종료된 start 경계 프로세스의 Job Object 생성을 생략합니다."""

            @staticmethod
            def close() -> None:
                """테스트용 수명 객체를 닫습니다."""

        parameters = contract.resolve_parameters({"cc_max_threads": 1})
        for boundary in ("start", "close"):
            for attempt in range(4):
                with self.subTest(boundary=boundary, attempt=attempt), tempfile.TemporaryDirectory() as temporary:
                    directory = Path(temporary)
                    runner = solver.NativeRunner(directory, NATIVE_WORKER, parameters)
                    cancel_path = directory / "cancel.request"
                    if boundary == "start":
                        real_popen = solver.subprocess.Popen

                        def popen_and_cancel(*arguments, **keywords):
                            """실제 세션을 시작한 직후 취소하고 종료 파일 공개까지 기다립니다."""
                            process = real_popen(*arguments, **keywords)
                            cancel_path.touch()
                            session_result = directory / "native" / "session-result.json"
                            deadline = monotonic() + 5.0
                            while not session_result.is_file() and process.poll() is None and monotonic() < deadline:
                                sleep(0.005)
                            process.wait(timeout=5.0)
                            return process

                        with (
                            mock.patch.object(solver.subprocess, "Popen", side_effect=popen_and_cancel),
                            mock.patch.object(solver, "_ChildLifetime", return_value=Lifetime()),
                            self.assertRaises(contract.CcCoreCancelled),
                        ):
                            runner.start()
                    else:
                        runner.start()
                        cancel_path.touch()
                        self.assertIsNotNone(runner.process)
                        runner.process.wait(timeout=5.0)
                        with self.assertRaises(contract.CcCoreCancelled):
                            runner.close()
                    session_result = contract.read_json_object(directory / "native" / "session-result.json")
                    self.assertEqual(session_result["status"], "cancelled")
                    self.assertTrue(runner.closed)

    @unittest.skipUnless(os.name == "nt", "Windows 자식 프로세스 수명 회귀입니다.")
    def test_running_cancel_and_forced_parent_exit_reap_native_child(self) -> None:
        """실행 중 취소와 부모 강제 종료가 실제 네이티브 자식을 남기지 않는지 확인합니다."""
        from time import monotonic, sleep
        for force_exit in (False, True):
            with self.subTest(force_exit=force_exit), tempfile.TemporaryDirectory() as temporary:
                directory = Path(temporary)
                manifest, _delta = make_job(directory, "TRIANGLE_MESH", "TRIANGLE_MESH")
                manifest["parameters"]["cc_mesh_sample_count"] = 200000
                contract.write_json_atomic(directory / "manifest.json", manifest)
                process = subprocess.Popen(
                    [sys.executable, "-B", "-X", "utf8", str(MODULE_ROOT / "icp_cccore_worker.py"),
                     "--job-dir", str(directory), "--native-worker", NATIVE_WORKER],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
                child_pids = []
                try:
                    deadline = monotonic() + 20.0
                    while monotonic() < deadline and process.poll() is None:
                        if (directory / "native/command-0003.accepted.json").is_file():
                            child_pids = native_child_pids(process.pid)
                            if child_pids:
                                break
                        sleep(0.02)
                    self.assertTrue(child_pids, "실제 정렬 자식 프로세스를 확인하지 못했습니다.")
                    # 자식이 작업 객체에 연결될 짧은 구간 뒤에 실제 중단을 요청합니다.
                    sleep(0.05)
                    if force_exit:
                        process.terminate()
                    else:
                        (directory / "cancel.request").touch()
                    process.wait(timeout=10.0)
                    deadline = monotonic() + 5.0
                    while any(pid_is_running(pid) for pid in child_pids) and monotonic() < deadline:
                        sleep(0.02)
                    self.assertFalse(any(pid_is_running(pid) for pid in child_pids))
                    self.assertFalse((directory / "result.json").exists())
                    if not force_exit:
                        error_payload = contract.read_json_object(directory / "error.json")
                        self.assertEqual(error_payload["status"], "CANCELLED", error_payload)
                finally:
                    if process.poll() is None:
                        process.terminate()
                        process.wait(timeout=10.0)

    @unittest.skipUnless(os.name == "nt", "Windows 네이티브 충돌 회귀입니다.")
    def test_native_worker_crash_fails_without_partial_result_or_fallback(self) -> None:
        """실행 중 네이티브 자식 충돌을 실패로 끝내고 부분 성공 결과를 남기지 않습니다."""
        from time import monotonic, sleep
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            manifest, _delta = make_job(directory, "TRIANGLE_MESH", "TRIANGLE_MESH")
            manifest["parameters"]["cc_mesh_sample_count"] = 200000
            contract.write_json_atomic(directory / "manifest.json", manifest)
            process = subprocess.Popen(
                [sys.executable, "-B", "-X", "utf8", str(MODULE_ROOT / "icp_cccore_worker.py"),
                 "--job-dir", str(directory), "--native-worker", NATIVE_WORKER],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            child_pids = []
            try:
                deadline = monotonic() + 20.0
                while monotonic() < deadline and process.poll() is None:
                    child_pids = native_child_pids(process.pid)
                    if child_pids and (directory / "native/command-0001.accepted.json").is_file():
                        break
                    sleep(0.02)
                self.assertEqual(len(child_pids), 1, "충돌시킬 단일 네이티브 자식을 확인하지 못했습니다.")
                os.kill(child_pids[0], 9)
                process.wait(timeout=10.0)
                self.assertEqual(process.returncode, 1)
                self.assertFalse((directory / "result.json").exists())
                self.assertFalse((directory / "result.npz").exists())
                self.assertEqual(contract.read_json_object(directory / "error.json")["status"], "FAILED")
            finally:
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=10.0)

    def test_missing_engine_reports_failure_without_fallback(self) -> None:
        """네이티브 실행 실패를 다른 정렬 엔진으로 대체하지 않는지 확인합니다."""
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            make_job(directory, "COLMAP_POINT_CLOUD", "COLMAP_POINT_CLOUD")
            self.assertEqual(worker.main(["--job-dir", str(directory), "--native-worker", str(directory / "missing.exe")]), 1)
            self.assertFalse((directory / "result.json").exists())
            self.assertEqual(contract.read_json_object(directory / "error.json")["status"], "FAILED")


if __name__ == "__main__":
    unittest.main(verbosity=2)
