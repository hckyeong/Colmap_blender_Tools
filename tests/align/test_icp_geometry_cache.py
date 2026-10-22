"""새 점 기반 엔진의 형상 준비 캐시와 원본 지문·수명·메모리 경계를 검증합니다."""

from __future__ import annotations

import gc
import hashlib
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import weakref

import numpy as np


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.align.alignment import icp_geometry as geometry


def _transform(matrix=None):
    """순수 테스트에서 현재 행렬과 내용 지문을 함께 제공하는 변환을 만듭니다."""
    matrix = np.eye(4) if matrix is None else np.asarray(matrix, dtype=np.float64)
    return SimpleNamespace(matrix_float64=matrix, fingerprint=hashlib.sha256(matrix.tobytes()).hexdigest())


def _mesh(model_id, *, positions=None, triangles=None):
    """원본 미참조 정점과 퇴화면을 포함한 독립 메시를 같은 출처 이름으로 만듭니다."""
    if positions is None:
        positions = np.asarray(((0., 0., 0.), (2., 0., 0.), (0., 2., 0.),
                                (0., 0., 3.), (90., 90., 90.)), dtype=np.float64)
    if triangles is None:
        triangles = np.asarray(((0, 1, 2), (0, 0, 2), (0, 1, 3)), dtype=np.uint32)
    return SimpleNamespace(model_id=model_id, geometry_kind="TRIANGLE_MESH", geometry_revision=1,
                           source_path="동일원본.obj", vertices_float64=positions, triangles_uint32=triangles)


class _Repository:
    """실제 저장소와 같은 슬롯·현재 변환 조회와 약한 참조 수명을 제공합니다."""

    def __init__(self):
        """서로 독립된 두 원본과 현재 축·정렬 상태를 구성합니다."""
        self.models = {name: _mesh(name) for name in ("이동", "기준")}
        self.companion_meshes = {}
        self.source_metadata = {}
        self.companion_sources = {}
        self.displays = {name: _transform() for name in self.models}
        self.alignments = {name: _transform() for name in self.models}

    def display_transform_for_model_id(self, model_id):
        """주 형상 슬롯의 현재 표시 변환을 반환합니다."""
        return self.displays[model_id]

    def companion_display_transform_for_model_id(self, model_id):
        """연결 형상 슬롯의 현재 표시 변환을 반환합니다."""
        return self.displays[model_id]

    def alignment_transform_for_model_id(self, model_id):
        """소유자의 현재 확정 정렬을 반환합니다."""
        return self.alignments[model_id]


class _Catalog:
    """선택된 이동 형상과 전체 기준 레이어를 반환하는 최소 카탈로그입니다."""

    def __init__(self):
        """메시 두 개의 역할과 선택 상태를 구성합니다."""
        self.selected_moving_model_ids = ("이동",)
        self.models = {name: SimpleNamespace(model_id=name, display_name=name,
            geometry_kind="TRIANGLE_MESH", show_points=False, show_surface=True) for name in ("이동", "기준")}
        self.reference_ids = ("기준",)

    def active_models(self):
        """숨김 상태와 무관한 기준 레이어 전체를 순서대로 반환합니다."""
        return tuple(self.models[name] for name in self.reference_ids)


def _runtime():
    """블렌더를 불러오지 않는 원본·선택·마커 테스트 런타임을 만듭니다."""
    return SimpleNamespace(model_repository=_Repository(), model_catalog=_Catalog(),
                           marker_selection=SimpleNamespace(completed_pairs=[]))


def _snapshot(runtime, *, point_kd_input=True):
    """공개 입력 준비 경계에 현재 이동 정렬과 전체 기준 목록을 전달합니다."""
    return geometry.create_cccore_geometry_snapshot(runtime, moving_model_id="이동",
        reference_model_ids=runtime.model_catalog.reference_ids,
        initial_alignment_float64=runtime.model_repository.alignments["이동"].matrix_float64,
        point_kd_input=point_kd_input)


class PointKdGeometryCacheTests(unittest.TestCase):
    """같은 내용의 변경과 별칭 수정까지 포함하여 캐시가 원본 검사를 약화하지 않는지 확인합니다."""

    def setUp(self):
        """각 검사에 다른 불변 사본 캐시를 연결합니다."""
        self.cache = geometry._PreparedMeshCache()
        self.cache_patch = patch.object(geometry, "_POINT_KD_PREPARED_MESH_CACHE", self.cache)
        self.cache_patch.start()
        self.addCleanup(self.cache_patch.stop)

    def test_warm_mesh_preparation_reuses_cleanup_but_hashes_all_original_arrays(self):
        """반복 준비는 정리만 생략하고 시작·완료 지문은 원본 전체를 계속 읽습니다."""
        runtime = _runtime()
        original = runtime.model_repository.models["이동"]
        positions, triangles = original.vertices_float64.copy(), original.triangles_uint32.copy()
        first = _snapshot(runtime)
        with patch.object(geometry, "_validate_raw_geometry", side_effect=AssertionError("정리 재실행")):
            with patch.object(geometry, "_array_fingerprint", wraps=geometry._array_fingerprint) as hashes:
                second = _snapshot(runtime)
                self.assertEqual(hashes.call_count, 4)
                self.assertIsNone(geometry.geometry_snapshot_validation_message(
                    runtime, second.without_arrays(), point_kd_input=True))
                self.assertEqual(hashes.call_count, 8)
        np.testing.assert_array_equal(first.moving.positions_float64, second.moving.positions_float64)
        np.testing.assert_array_equal(second.moving.positions_float64, positions[:4])
        np.testing.assert_array_equal(second.moving.triangles_uint32, triangles[[0, 2]])
        self.assertEqual((second.provenance.moving.point_count, second.provenance.moving.triangle_count), (5, 3))
        np.testing.assert_array_equal(original.vertices_float64, positions)
        np.testing.assert_array_equal(original.triangles_uint32, triangles)
        self.assertTrue(original.vertices_float64.flags.writeable)
        for entry in self.cache.entries.values():
            for array in (entry.positions_float64, entry.triangles_uint32):
                with self.assertRaises(ValueError):
                    array.setflags(write=True)

    def test_readonly_flag_reset_and_alias_changes_invalidate_cleanup_and_result(self):
        """읽기 전용 소유 배열을 다시 수정해도 전체 해시가 변경을 찾아 재정리합니다."""
        runtime = _runtime()
        model = runtime.model_repository.models["이동"]
        alias = model.vertices_float64.view()
        model.vertices_float64.setflags(write=False)
        first = _snapshot(runtime)
        alias[0, 0] = .25
        self.assertIsNotNone(geometry.geometry_snapshot_validation_message(runtime, first, point_kd_input=True))
        with patch.object(geometry, "_validate_raw_geometry", wraps=geometry._validate_raw_geometry) as cleanup:
            second = _snapshot(runtime)
            self.assertEqual(cleanup.call_count, 1)
        self.assertEqual(second.moving.positions_float64[0, 0], .25)
        model.triangles_uint32[1] = (0, 2, 3)
        with patch.object(geometry, "_validate_raw_geometry", wraps=geometry._validate_raw_geometry) as cleanup:
            third = _snapshot(runtime)
            self.assertEqual(cleanup.call_count, 1)
        self.assertEqual(len(third.moving.triangles_uint32), 3)

    def test_reload_revision_and_equal_content_array_replacement_are_isolated(self):
        """모델 재로드·내용이 같은 배열 교체·리비전 변경을 이전 슬롯의 결과와 격리합니다."""
        for change in ("모델", "배열", "리비전"):
            with self.subTest(change=change):
                runtime = _runtime()
                _snapshot(runtime)
                model = runtime.model_repository.models["이동"]
                if change == "모델":
                    runtime.model_repository.models["이동"] = _mesh("이동",
                        positions=model.vertices_float64, triangles=model.triangles_uint32)
                elif change == "배열":
                    model.vertices_float64 = model.vertices_float64.copy()
                else:
                    model.geometry_revision += 1
                with patch.object(geometry, "_validate_raw_geometry", wraps=geometry._validate_raw_geometry) as cleanup:
                    _snapshot(runtime)
                    self.assertEqual(cleanup.call_count, 1)

    def test_duplicate_import_with_identical_path_and_buffers_uses_an_independent_slot(self):
        """같은 경로·버퍼를 가진 별도 가져오기라도 소유자별 정리 항목을 새로 만듭니다."""
        runtime = _runtime()
        _snapshot(runtime)
        original = runtime.model_repository.models["기준"]
        repository, catalog = runtime.model_repository, runtime.model_catalog
        repository.models["복제"] = _mesh("복제", positions=original.vertices_float64,
                                          triangles=original.triangles_uint32)
        repository.displays["복제"], repository.alignments["복제"] = _transform(), _transform()
        catalog.models["복제"] = SimpleNamespace(model_id="복제", display_name="복제",
            geometry_kind="TRIANGLE_MESH", show_points=False, show_surface=True)
        catalog.reference_ids = ("기준", "복제")
        with patch.object(geometry, "_validate_raw_geometry", wraps=geometry._validate_raw_geometry) as cleanup:
            snapshot = _snapshot(runtime)
            self.assertEqual(cleanup.call_count, 1)
        self.assertEqual(snapshot.reference_combined.owner_model_ids, ("기준", "복제"))
        self.assertEqual(len(self.cache.entries), 3)

    def test_main_and_companion_slots_do_not_share_a_preparation_entry(self):
        """같은 소유자·원본 버퍼라도 주 형상과 연결 형상의 출처와 캐시를 구분합니다."""
        runtime = _runtime()
        first = _snapshot(runtime)
        repository, record = runtime.model_repository, runtime.model_catalog.models["이동"]
        original = repository.models["이동"]
        repository.models["이동"] = SimpleNamespace(model_id="이동", geometry_kind="COLMAP_POINT_CLOUD",
            geometry_revision=1, source_path="동일원본", points=SimpleNamespace(positions=np.eye(3)))
        repository.companion_meshes["이동"] = original
        record.geometry_kind = "COLMAP_POINT_CLOUD"
        with patch.object(geometry, "_validate_raw_geometry", wraps=geometry._validate_raw_geometry) as cleanup:
            second = _snapshot(runtime)
            self.assertEqual(cleanup.call_count, 1)
        self.assertEqual(second.moving.geometry_source, "COMPANION")
        self.assertIsNotNone(geometry.geometry_snapshot_validation_message(
            runtime, first, verify_content=False, point_kd_input=True))
        repository.models["이동"].points.positions[0, 0] += 1.
        self.assertIsNone(geometry.geometry_snapshot_validation_message(runtime, second, point_kd_input=True))

    def test_equal_content_restoration_in_a_new_repository_does_not_reuse_old_entries(self):
        """저장 복원처럼 ID·내용이 같아도 새 저장소의 모델 수명으로 독립 준비합니다."""
        previous, restored = _runtime(), _runtime()
        _snapshot(previous)
        with patch.object(geometry, "_validate_raw_geometry", wraps=geometry._validate_raw_geometry) as cleanup:
            _snapshot(restored)
            self.assertEqual(cleanup.call_count, 2)
        self.assertEqual(len(self.cache.entries), 4)
        del previous
        gc.collect()
        self.assertEqual(len(self.cache.entries), 2)

    def test_axes_and_current_alignment_recompute_coordinates_and_invalidate_metadata(self):
        """원본 정리를 재사용하더라도 축별 배율과 현재 정렬은 매번 새 좌표에 반영합니다."""
        runtime = _runtime()
        first = _snapshot(runtime)
        display = np.diag((2., 3., 4., 1.))
        alignment = np.eye(4)
        alignment[:3, 3] = (7., 8., 9.)
        runtime.model_repository.displays["이동"] = _transform(display)
        runtime.model_repository.alignments["이동"] = _transform(alignment)
        self.assertIsNotNone(geometry.geometry_snapshot_validation_message(runtime, first, point_kd_input=True))
        with patch.object(geometry, "_validate_raw_geometry", side_effect=AssertionError("정리 재실행")):
            second = _snapshot(runtime)
        expected = runtime.model_repository.models["이동"].vertices_float64[:4] @ display[:3, :3].T
        expected += alignment[:3, 3]
        np.testing.assert_array_equal(second.moving.positions_float64, expected)
        self.assertIsNone(geometry.geometry_snapshot_validation_message(runtime, second, point_kd_input=True))

    def test_noncanonical_dtype_does_not_reuse_a_truncated_index_fingerprint(self):
        """정수 축소 때 같은 해시가 되는 범위 밖 인덱스도 원본 검증을 우회하지 않습니다."""
        runtime = _runtime()
        model = runtime.model_repository.models["이동"]
        model.triangles_uint32 = model.triangles_uint32.astype(np.int64)
        _snapshot(runtime)
        model.triangles_uint32[0, 0] += 2**32
        with self.assertRaisesRegex(ValueError, "정점 범위"):
            _snapshot(runtime)

    def test_cache_has_memory_and_entry_limits_and_oversized_results_are_not_stored(self):
        """메모리·슬롯 수 상한을 넘는 항목을 퇴출하고 단독 초과 결과는 보관하지 않습니다."""
        runtime = _runtime()
        with patch.object(geometry, "_POINT_KD_CACHE_MAX_BYTES", 120):
            _snapshot(runtime)
            self.assertLessEqual(self.cache.byte_count, 120)
            self.assertEqual(len(self.cache.entries), 1)
        self.cache = geometry._PreparedMeshCache()
        with patch.object(geometry, "_POINT_KD_PREPARED_MESH_CACHE", self.cache):
            with patch.object(geometry, "_POINT_KD_CACHE_MAX_ENTRIES", 1):
                _snapshot(runtime)
                self.assertEqual(len(self.cache.entries), 1)
            with patch.object(geometry, "_POINT_KD_CACHE_MAX_BYTES", 1):
                self.cache._prune()
                self.cache._discard(next(iter(self.cache.entries)))
                _snapshot(runtime)
                self.assertFalse(self.cache.entries)
                self.assertEqual(self.cache.byte_count, 0)

    def test_repository_and_original_array_destruction_release_owned_buffers(self):
        """저장소 또는 교체된 원본 배열의 소멸이 캐시 사본의 수명을 종료합니다."""
        runtime = _runtime()
        _snapshot(runtime)
        model = runtime.model_repository.models["이동"]
        model.vertices_float64 = model.vertices_float64.copy()
        gc.collect()
        self.assertEqual(len(self.cache.entries), 1)
        repository_ref = weakref.ref(runtime.model_repository)
        del runtime
        gc.collect()
        self.assertIsNone(repository_ref())
        self.assertFalse(self.cache.entries)
        self.assertEqual(self.cache.byte_count, 0)

    def test_immutable_original_buffers_are_not_retained_by_prepared_copies(self):
        """이미 불변인 원본도 캐시가 별도 사본을 소유하여 원본 배열의 수명을 연장하지 않습니다."""
        runtime = _runtime()
        model = runtime.model_repository.models["이동"]
        model.vertices_float64 = geometry._immutable_array(model.vertices_float64[:4], "<f8")
        model.triangles_uint32 = geometry._immutable_array(model.triangles_uint32[[0, 2]], "<u4")
        original_ref = weakref.ref(model.triangles_uint32)
        _snapshot(runtime)
        model.triangles_uint32 = model.triangles_uint32.copy()
        gc.collect()
        self.assertIsNone(original_ref())
        self.assertEqual(len(self.cache.entries), 1)

    def test_existing_cccore_path_keeps_its_original_cleanup_policy(self):
        """기존 경로는 새 준비 캐시를 채우거나 조회하지 않고 매번 원본을 검증합니다."""
        runtime = _runtime()
        _snapshot(runtime)
        with patch.object(geometry, "_validate_raw_geometry", wraps=geometry._validate_raw_geometry) as cleanup:
            _snapshot(runtime, point_kd_input=False)
            _snapshot(runtime, point_kd_input=False)
            self.assertEqual(cleanup.call_count, 4)

    def test_system_hash_is_used_only_for_new_engine_start_and_completion(self):
        """새 경로의 시작·완료만 시스템 해시를 사용하며 기존 지문과 캐시 결과는 완전히 같습니다."""
        runtime = _runtime()
        legacy = _snapshot(runtime, point_kd_input=False)
        with patch.object(geometry, "array_sha256_fingerprint", wraps=geometry.array_sha256_fingerprint) as system_hash:
            current = _snapshot(runtime)
            self.assertEqual(system_hash.call_count, 4)
            self.assertIsNone(geometry.geometry_snapshot_validation_message(runtime, current, point_kd_input=True))
            self.assertEqual(system_hash.call_count, 8)
            self.assertIsNone(geometry.geometry_snapshot_validation_message(
                runtime, current, verify_content=False, point_kd_input=True))
            self.assertEqual(system_hash.call_count, 8)
        self.assertEqual(current.provenance, legacy.provenance)
        self.assertEqual(current.input_fingerprint, legacy.input_fingerprint)
        self.assertEqual(current.moving.source_fingerprint, legacy.moving.source_fingerprint)
        with patch.object(geometry, "array_sha256_fingerprint", side_effect=AssertionError("기존 경로 시스템 해시 금지")):
            repeated = _snapshot(runtime, point_kd_input=False)
            self.assertIsNone(geometry.geometry_snapshot_validation_message(runtime, repeated))

    def test_system_hash_failure_blocks_preparation_and_completion_without_fallback(self):
        """시스템 해시 실패를 명확히 전달하고 완료 검사나 원본 검사를 묵시적으로 통과시키지 않습니다."""
        from tools.align.alignment.icp_array_hash import ArrayHashError

        runtime = _runtime()
        snapshot = _snapshot(runtime)
        with patch.object(geometry, "array_sha256_fingerprint", side_effect=ArrayHashError("시스템 원본 검사 실패")):
            with self.assertRaisesRegex(ArrayHashError, "시스템 원본 검사 실패"):
                _snapshot(runtime)
            message = geometry.geometry_snapshot_validation_message(runtime, snapshot, point_kd_input=True)
            self.assertIn("시스템 원본 검사 실패", message)

    def test_system_point_cloud_hash_and_unused_mesh_vertex_changes_preserve_full_provenance(self):
        """점군과 미참조 메시 정점까지 전체 원본 해시를 유지하며 계산용 형상과 원본 지문을 구분합니다."""
        runtime = _runtime()
        first = _snapshot(runtime)
        runtime.model_repository.models["이동"].vertices_float64[-1, -1] += .5
        self.assertIsNotNone(geometry.geometry_snapshot_validation_message(runtime, first, point_kd_input=True))
        second = _snapshot(runtime)
        np.testing.assert_array_equal(first.moving.positions_float64, second.moving.positions_float64)
        self.assertNotEqual(first.provenance.moving.positions_fingerprint, second.provenance.moving.positions_fingerprint)
        for name, mesh in tuple(runtime.model_repository.models.items()):
            runtime.model_repository.models[name] = SimpleNamespace(model_id=name, geometry_kind="COLMAP_POINT_CLOUD",
                geometry_revision=1, source_path=mesh.source_path, points=SimpleNamespace(positions=mesh.vertices_float64))
            record = runtime.model_catalog.models[name]
            record.geometry_kind, record.show_points, record.show_surface = "COLMAP_POINT_CLOUD", True, False
        legacy = _snapshot(runtime, point_kd_input=False)
        with patch.object(geometry, "array_sha256_fingerprint", wraps=geometry.array_sha256_fingerprint) as system_hash:
            current = _snapshot(runtime)
            self.assertEqual(system_hash.call_count, 2)
            self.assertIsNone(geometry.geometry_snapshot_validation_message(runtime, current, point_kd_input=True))
            self.assertEqual(system_hash.call_count, 4)
        self.assertEqual(legacy.provenance, current.provenance)
        self.assertEqual(legacy.input_fingerprint, current.input_fingerprint)


if __name__ == "__main__":
    unittest.main()
