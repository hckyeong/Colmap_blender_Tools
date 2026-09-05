"""CC 승인 출처의 저장 호환과 형상 축 의존성·Undo 복원을 검증합니다."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory

import numpy as np


def _add_test_paths() -> None:
    """실험 소스와 실제 가져오기·축 편집 회귀 자료를 연결합니다."""
    folder = Path(__file__).resolve().parent
    for path in (folder.parents[2], folder):
        if str(path) not in sys.path:
            sys.path.insert(0, str(path))


@contextmanager
def _project_fixture(*, companions: bool = True):
    """실험 출력 폴더에서 실제 원본을 읽고 생성한 프로젝트 Text만 정리합니다."""
    from test_companion_marker_persistence import _origin
    from test_project_restore import _remove_owned_project_texts

    outputs = Path(__file__).resolve().parents[4] / "outputs"
    with TemporaryDirectory(prefix="cc-persistence-", dir=outputs) as directory:
        root = Path(directory).resolve()
        assert root.parent == outputs.resolve()
        try:
            yield _origin(root, companions=companions)
        finally:
            _remove_owned_project_texts()


def _v3_engine_metadata(source: str) -> dict:
    """실제 v3 저장 계약의 전체 감사 필드와 논리·물리 호출 연결을 구성합니다."""
    from tools.align.alignment.icp_cccore_contract import (
        CC_NATIVE_COUNTER_FIELDS, CC_NATIVE_OPERATION_SEQUENCES, CC_NATIVE_OPERATION_STAGES,
        CC_NATIVE_TIMING_FIELDS,
    )
    geometry_mode = "MESH_TO_MESH" if source == "COMPANION" else "POINT_TO_POINT"
    operations = CC_NATIVE_OPERATION_SEQUENCES[geometry_mode]
    stages = CC_NATIVE_OPERATION_STAGES[geometry_mode]
    if geometry_mode == "MESH_TO_MESH":
        native_logical_indexes = (1, 2, 3, 6)
        cache_sources = {4: 1, 5: 2, 7: 1, 8: 2}
        data_ids = (
            "baseline-moving-evaluation", "baseline-reference-inverse-evaluation",
            "registration-data", "rigid-moving-evaluation",
            "rigid-reference-inverse-evaluation", "registration-rigid-data",
            "scale-refine-moving-evaluation", "scale-refine-reference-inverse-evaluation",
        )
        model_ids = (
            "reference-model", "moving-model", "reference-model", "reference-model",
            "moving-model", "reference-model", "reference-model", "moving-model",
        )
    else:
        native_logical_indexes = tuple(range(1, 7))
        cache_sources = {}
        data_ids = (
            "spacing-query", "baseline-moving-evaluation", "registration-data",
            "rigid-moving-evaluation", "registration-rigid-data", "scale-refine-moving-evaluation",
        )
        model_ids = ("spacing-model",) + ("reference-model",) * 5
    physical_for_logical = {logical: index for index, logical in enumerate(native_logical_indexes, 1)}
    evaluation_keys = {
        index: (f"{index:064x}" if operation == "distance" and stage != "PREPARING" else "")
        for index, (operation, stage) in enumerate(zip(operations, stages), 1)
    }
    if geometry_mode == "MESH_TO_MESH":
        evaluation_keys[4] = evaluation_keys[1]
        evaluation_keys[5] = evaluation_keys[2]
        evaluation_keys[7] = evaluation_keys[1]
        evaluation_keys[8] = evaluation_keys[2]
    timings_zero = {name: 0.0 for name in CC_NATIVE_TIMING_FIELDS}
    counters_zero = {name: 0 for name in CC_NATIVE_COUNTER_FIELDS}
    seen = set()
    native_details = {}
    geometry_hits = {}
    for logical_index in native_logical_indexes:
        physical_sequence = physical_for_logical[logical_index]
        loads = 0
        hits = 0
        for geometry_id in (data_ids[logical_index - 1], model_ids[logical_index - 1]):
            if geometry_id in seen:
                hits += 1
                geometry_hits[geometry_id] = geometry_hits.get(geometry_id, 0) + 1
            else:
                seen.add(geometry_id)
                loads += 1
                geometry_hits.setdefault(geometry_id, 0)
        elapsed = physical_sequence / 10.0
        native_details[logical_index] = {
            "physical_sequence": physical_sequence, "elapsed_seconds": elapsed,
            "roundtrip_seconds": elapsed + 0.01, "protocol_overhead_seconds": 0.01,
            "timings": {**timings_zero, "kernel_seconds": elapsed},
            **counters_zero, "geometry_load_count": loads, "geometry_cache_hits": hits,
        }
    logical_operations = []
    for logical_index, (operation, stage, data_id, model_id) in enumerate(
        zip(operations, stages, data_ids, model_ids), 1,
    ):
        if logical_index in native_details:
            detail = native_details[logical_index]
            physical_sequence = detail["physical_sequence"]
            source_logical_index = logical_index
            execution_source = "native_session"
        else:
            source_logical_index = cache_sources[logical_index]
            source_detail = native_details[source_logical_index]
            detail = {
                "physical_sequence": None, "elapsed_seconds": 0.0,
                "roundtrip_seconds": source_detail["roundtrip_seconds"],
                "protocol_overhead_seconds": source_detail["protocol_overhead_seconds"],
                "timings": dict(timings_zero), **counters_zero,
            }
            physical_sequence = None
            execution_source = "exact_evaluation_cache"
        logical_operations.append({
            "operation": operation, "index": logical_index, "sequence": logical_index,
            "physical_sequence": physical_sequence,
            "source_sequence": physical_for_logical[source_logical_index],
            "source_logical_index": source_logical_index, "stage": stage,
            "execution_source": execution_source,
            "evaluation_key_sha256": evaluation_keys[logical_index],
            "elapsed_seconds": detail["elapsed_seconds"],
            "roundtrip_seconds": detail["roundtrip_seconds"],
            "protocol_overhead_seconds": detail["protocol_overhead_seconds"],
            "requested_max_threads": 31, "effective_max_threads": 31,
            "data_geometry_id": data_id, "model_geometry_id": model_id,
            "timings": detail["timings"],
            **{name: detail[name] for name in CC_NATIVE_COUNTER_FIELDS},
        })
    physical_native_calls = []
    for logical_index in native_logical_indexes:
        logical = logical_operations[logical_index - 1]
        physical_sequence = logical["physical_sequence"]
        physical_native_calls.append({
            **{
                key: value for key, value in logical.items()
                if key not in {"index", "sequence", "physical_sequence", "source_sequence", "source_logical_index"}
            },
            "index": physical_sequence, "sequence": physical_sequence, "logical_index": logical_index,
        })
    geometry_statistics = []
    for geometry_id in sorted(seen):
        cache_scope = "SESSION" if geometry_id in {"reference-model", "moving-model"} else "COMMAND"
        geometry_statistics.append({
            "geometry_id": geometry_id, "point_count": 8,
            "triangle_count": 2 if geometry_id.endswith("-model") and source == "COMPANION" else 0,
            "load_count": 1, "cache_hits": geometry_hits[geometry_id],
            "binary_read_seconds": 0.001, "conversion_validation_seconds": 0.001,
            "mesh_load_seconds": 0.001 if geometry_id.endswith("-model") and source == "COMPANION" else 0.0,
            "estimated_bytes": 256, "cache_scope": cache_scope,
            "released_after_command": cache_scope == "COMMAND",
        })
    physical_roundtrip = sum(item["roundtrip_seconds"] for item in physical_native_calls)
    protocol_overhead = sum(item["protocol_overhead_seconds"] for item in physical_native_calls)
    total_alignment = physical_roundtrip + 0.5
    session_statistics = {
        name: sum(item[name] for item in physical_native_calls) for name in CC_NATIVE_COUNTER_FIELDS
    }
    session_statistics.update({
        "peak_working_set_bytes": 1048576,
        "cache_peak_estimated_bytes": len(geometry_statistics) * 256,
        "cache_resident_estimated_bytes": sum(
            item["estimated_bytes"] for item in geometry_statistics if item["cache_scope"] == "SESSION"
        ),
    })
    protocol_statistics = {
        "physical_roundtrip_seconds": physical_roundtrip,
        "protocol_overhead_seconds": protocol_overhead,
        "protocol_overhead_ratio_of_roundtrip": protocol_overhead / physical_roundtrip,
        "protocol_overhead_fraction_of_total_alignment": protocol_overhead / total_alignment,
    }
    metadata = {
        "core_commit": "6f" * 20, "native_worker_sha256": "7a" * 32,
        "native_build_identity": {"protocol_version": "cccore-native-v3", "worker_sha256": "7a" * 32},
        "quality_policy": {"selection_metric": "MEAN_DIRECTIONAL_RMS", "trimmed_fraction": 0.9},
        "geometry_mode": geometry_mode, "reverse_registration": False,
        "normalization": {"center": [1.0, -2.0, 3.0], "scale": 0.25},
        "parameters": {"scale_refine_max_relative_change": 0.1, "overlap_ratio": 0.9, "max_threads": 31},
        "diagnostics": {"설명": "v3 단일 세션 정렬"},
        "registration_stages": [{"stage": "rigid"}, {"stage": "scale_refine"}],
        "best_stage": "baseline", "solver_stop_reason": "NO_MEAN_RMS_IMPROVEMENT",
        "geometry_provenance": {"moving_source": source, "reference_source": source},
        "initial_alignment_4x4": np.identity(4).tolist(),
        "baseline_metrics": {"trimmed_rms_90": 0.0125},
        "best_candidate_metrics": {"trimmed_rms_90": 0.0125},
        "relative_improvement": 0.0, "review_reasons": ["회귀 fixture"],
        "quality": "REVIEW", "user_review_required": True, "elapsed_seconds": total_alignment,
        "thread_backend": "oneTBB", "tbb_version": "2022.0.0",
        "requested_max_threads": 31, "effective_max_threads": 31, "hardware_thread_count": 32,
        "distance_multithread_support": True, "octree_multithread_support": True,
        "native_process_count": 1, "logical_operations": logical_operations,
        "native_calls": logical_operations, "physical_native_calls": physical_native_calls,
        "physical_native_command_count": len(physical_native_calls),
        "exact_evaluation_cache": {
            "policy": "BIT_EXACT_MATRIX_AND_EVALUATION_CONTRACT",
            "entry_count": 2 if cache_sources else 3, "hit_count": len(cache_sources),
            "miss_count": 2 if cache_sources else 3,
        },
        "process_startup_seconds": 0.02, "session_elapsed_seconds": physical_roundtrip + 0.1,
        "session_exit_reason": "closed", "session_statistics": session_statistics,
        "geometry_statistics": geometry_statistics, "session_protocol_statistics": protocol_statistics,
        **protocol_statistics, "geometry_input_fingerprint": "83" * 32,
    }
    from tools.align.alignment.icp_cccore_runtime import CCCORE_PROVENANCE_METADATA_FIELDS
    assert set(metadata) == set(CCCORE_PROVENANCE_METADATA_FIELDS)
    return metadata


def _cc_provenance(
    runtime, *, source: str = "COMPANION", marker_dependencies=(),
    algorithm_version: str = "cccore-icp-v3",
):
    """실제 확정 C를 참조하는 CC 정렬 승인 출처를 버전별 계약으로 구성합니다."""
    from tools.align.alignment.types import AlignmentProvenanceRecord

    metadata = _v3_engine_metadata(source) if algorithm_version == "cccore-icp-v3" else {
        "core_commit": "6f" * 20,
        "native_worker_sha256": "7a" * 32,
        "geometry_mode": "MESH_TO_MESH" if source == "COMPANION" else "POINT_TO_POINT",
        "reverse_registration": False,
        "normalization": {"center": [1.0, -2.0, 3.0], "scale": 0.25},
        "parameters": {"scale_refine_max_relative_change": 0.1, "overlap_ratio": 0.9},
        "geometry_input_fingerprint": "83" * 32,
        "diagnostics": {"설명": "연결 메시를 사용한 정밀 정렬"},
    }
    return AlignmentProvenanceRecord(
        model_id="moving", method="ICP", backend_id="CCCORELIB", algorithm_version=algorithm_version,
        input_fingerprint="91" * 32,
        result_fingerprint=runtime.model_repository.alignment_transform_for_model_id("moving").fingerprint,
        icp_rmse=0.0125, icp_status="REVIEW", committed_at_utc="2026-09-03T08:00:00Z",
        marker_geometry_dependencies=marker_dependencies,
        solver_geometry_dependencies=(("reference", source), ("moving", source)),
        engine_metadata_json=json.dumps(metadata, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
    )


def _old_provenance(runtime, *, marker_dependencies=()):
    """구버전 Small GICP가 기록하던 기존 품질·출처 필드만 구성합니다."""
    from tools.align.alignment.types import AlignmentProvenanceRecord

    return AlignmentProvenanceRecord(
        model_id="moving", method="ICP", backend_id="SMALL_GICP", algorithm_version="small-gicp-v1",
        input_fingerprint="old-input", result_fingerprint=runtime.model_repository.alignment_transform_for_model_id("moving").fingerprint,
        marker_rms=0.03, marker_maximum_error=0.08, icp_fitness=0.97, icp_rmse=0.025,
        icp_status="PASS", committed_at_utc="2026-08-29T10:00:00Z",
        marker_geometry_dependencies=marker_dependencies,
    )


def _set_distinct_axes_and_alignment(runtime) -> None:
    """주 형상·연결 형상 축과 공통 C를 서로 다른 비항등 값으로 확정합니다."""
    from tools.align.alignment.types import create_sim3_transform
    from tools.align.display_axes import create_model_display_transform

    repository = runtime.model_repository
    for index, model_id in enumerate(("reference", "moving"), 1):
        repository.set_display_transform(model_id, create_model_display_transform(
            "CUSTOM", "X", "Z", rotation_xyz_degrees=(13.0 * index, -21.0, 17.0)))
        if model_id in repository.companion_meshes:
            repository.set_companion_display_transform(model_id, create_model_display_transform(
                "CUSTOM", "-Z", "Y", rotation_xyz_degrees=(-19.0, 7.0 * index, 31.0)))
        repository.set_alignment_transform(model_id, create_sim3_transform(
            np.eye(3), 1.0 + 0.25 * index, np.asarray((4.0 * index, -3.0, 7.0))))


def _assert_restored_geometry(origin, restored) -> None:
    """원본·현재 축·공통 C·표시·마커가 재가져오기 뒤 그대로인지 확인합니다."""
    from test_geometry_axes_persistence import _assert_transform

    assert tuple(restored.model_repository.models) == tuple(origin.model_repository.models)
    assert tuple(restored.model_repository.companion_meshes) == tuple(origin.model_repository.companion_meshes)
    for model_id in origin.model_repository.models:
        first = origin.model_repository
        second = restored.model_repository
        np.testing.assert_array_equal(first.models[model_id].points.positions, second.models[model_id].points.positions)
        np.testing.assert_array_equal(first.alignment_transform_for_model_id(model_id).matrix_float64,
                                      second.alignment_transform_for_model_id(model_id).matrix_float64)
        _assert_transform(second.display_transform_for_model_id(model_id), first.display_transform_for_model_id(model_id))
        if model_id in first.companion_meshes:
            np.testing.assert_array_equal(first.companion_meshes[model_id].vertices_float64,
                                          second.companion_meshes[model_id].vertices_float64)
            np.testing.assert_array_equal(first.companion_meshes[model_id].triangles_uint32,
                                          second.companion_meshes[model_id].triangles_uint32)
            _assert_transform(second.companion_display_transform_for_model_id(model_id),
                              first.companion_display_transform_for_model_id(model_id))
            assert second.companion_sources[model_id] == first.companion_sources[model_id]
        old_record = origin.model_catalog.models[model_id]
        new_record = restored.model_catalog.models[model_id]
        assert (new_record.registration_status, new_record.is_visible, new_record.show_points, new_record.show_surface) == (
            old_record.registration_status, old_record.is_visible, old_record.show_points, old_record.show_surface)
        assert new_record.alignment_provenance == old_record.alignment_provenance
    assert restored.marker_selection.completed_pairs == origin.marker_selection.completed_pairs


def test_cc_mesh_approval_roundtrip_uses_real_project_snapshot() -> None:
    """메시 정렬 승인 출처가 실제 프로젝트 저장·재가져오기에서 원본과 함께 보존됩니다."""
    from test_companion_persistence import _restore, _write_snapshot
    from tools.align.constants import PROJECT_PERSISTENCE_SCHEMA_VERSION
    from tools.align.project_persistence import decode_project_state, encode_project_state
    from tools.align.project_persistence.types import ProjectStateSnapshot

    with _project_fixture() as origin:
        _set_distinct_axes_and_alignment(origin)
        provenance = _cc_provenance(origin)
        origin.model_catalog.set_alignment_provenance("moving", provenance)
        origin.model_catalog.promote_model_for_icp_commit("moving")
        for model_id in ("reference", "moving"):
            origin.model_catalog.set_geometry_visibility(model_id, show_surface=True)
        origin.model_catalog.set_model_visibility("reference", False)
        snapshot, _text = _write_snapshot(origin)
        assert isinstance(snapshot, ProjectStateSnapshot)
        assert snapshot.schema_version == PROJECT_PERSISTENCE_SCHEMA_VERSION == 8
        payload = encode_project_state(snapshot)
        document = json.loads(payload)
        saved = document["models"][1]["catalog"]["alignment_provenance"]
        assert saved["backend_id"] == "CCCORELIB" and saved["icp_fitness"] is None
        assert saved["algorithm_version"] == "cccore-icp-v3"
        from tools.align.alignment.icp_cccore_runtime import CCCORE_PROVENANCE_METADATA_FIELDS
        audit = saved["engine_metadata"]
        assert set(audit) == set(CCCORE_PROVENANCE_METADATA_FIELDS)
        assert audit["geometry_mode"] == "MESH_TO_MESH"
        from tools.align.alignment import icp_cccore_contract as contract
        logical = contract.validate_native_call_summaries(
            audit["logical_operations"], geometry_mode=audit["geometry_mode"],
            requested_max_threads=audit["requested_max_threads"],
            effective_max_threads=audit["effective_max_threads"],
        )
        assert audit["native_calls"] == audit["logical_operations"]
        contract.validate_exact_evaluation_cache_statistics(audit["exact_evaluation_cache"], logical)
        physical = contract.validate_physical_native_call_summaries(
            audit["physical_native_calls"], logical,
            requested_max_threads=audit["requested_max_threads"],
            effective_max_threads=audit["effective_max_threads"],
        )
        contract.validate_native_session_aggregates(
            audit["session_statistics"], audit["geometry_statistics"], physical,
        )
        contract.validate_session_protocol_statistics(
            audit["session_protocol_statistics"], physical,
            total_alignment_seconds=audit["elapsed_seconds"],
        )
        assert sorted(saved["solver_geometry_dependencies"]) == [["moving", "COMPANION"], ["reference", "COMPANION"]]
        assert not any(key in payload for key in ("vertices_float64", "triangles_uint32", "positions_float64"))
        decoded = decode_project_state(payload)
        assert decoded.models[1].catalog.alignment_provenance == provenance
        assert decoded.models == snapshot.models and decoded.markers == snapshot.markers
        restored = _restore()
        _assert_restored_geometry(origin, restored)
        restored_provenance = restored.model_catalog.models["moving"].alignment_provenance
        assert restored_provenance.algorithm_version == "cccore-icp-v3"
        assert json.loads(restored_provenance.engine_metadata_json) == saved["engine_metadata"]
        assert restored_provenance.engine_metadata_json == provenance.engine_metadata_json
        assert restored_provenance.geometry_dependencies == provenance.solver_geometry_dependencies


def test_cc_v1_v2_provenance_survives_snapshot_encode_decode_and_restore() -> None:
    """CC v1·v2 승인 기록을 실제 프로젝트 snapshot 인코딩·디코딩·복원에서 각각 보존합니다."""
    from test_companion_persistence import _restore, _write_snapshot
    from tools.align.project_persistence import decode_project_state, encode_project_state

    for algorithm_version in ("cccore-icp-v1", "cccore-icp-v2"):
        with _project_fixture() as origin:
            _set_distinct_axes_and_alignment(origin)
            provenance = _cc_provenance(origin, algorithm_version=algorithm_version)
            origin.model_catalog.set_alignment_provenance("moving", provenance)
            origin.model_catalog.promote_model_for_icp_commit("moving")
            snapshot, _text = _write_snapshot(origin)
            payload = encode_project_state(snapshot)
            decoded = decode_project_state(payload)
            decoded_provenance = decoded.models[1].catalog.alignment_provenance
            assert decoded_provenance == provenance
            assert decoded_provenance.algorithm_version == algorithm_version
            restored = _restore()
            _assert_restored_geometry(origin, restored)
            restored_provenance = restored.model_catalog.models["moving"].alignment_provenance
            assert restored_provenance == provenance
            assert restored_provenance.algorithm_version == algorithm_version


def test_schema_six_upgrades_to_current_and_preserves_old_engine() -> None:
    """실제 v6 원문을 현재 schema로 올려 C·축·마커·Small GICP 출처를 유지합니다."""
    from test_companion_marker_persistence import _install_pair, _write_document
    from test_companion_persistence import _legacy_payload, _restore, _write_snapshot
    from tools.align.constants import PROJECT_PERSISTENCE_SCHEMA_VERSION
    from tools.align.project_persistence import decode_project_state, encode_project_state
    from tools.align.project_persistence.codec import project_state_schema_version

    with _project_fixture() as origin:
        _set_distinct_axes_and_alignment(origin)
        _install_pair(origin, reference_companion=True, moving_companion=True)
        provenance = _old_provenance(origin, marker_dependencies=(("moving", "COMPANION"), ("reference", "COMPANION")))
        origin.model_catalog.set_alignment_provenance("moving", provenance)
        origin.model_catalog.set_model_visibility("reference", False)
        origin.model_catalog.set_geometry_visibility("reference", show_surface=True)
        origin.model_catalog.set_geometry_visibility("moving", show_surface=True)
        snapshot, text = _write_snapshot(origin)
        document = json.loads(_legacy_payload(snapshot, 6))
        for model in document["models"]:
            saved = model["catalog"]["alignment_provenance"]
            if saved:
                assert "solver_geometry_dependencies" not in saved and "engine_metadata" not in saved
        _write_document(text, document)
        assert project_state_schema_version(text.as_string()) == 6
        decoded = decode_project_state(text.as_string())
        assert decoded.schema_version == PROJECT_PERSISTENCE_SCHEMA_VERSION
        assert tuple(model.model_id for model in decoded.models) == tuple(model.model_id for model in snapshot.models)
        assert all(not model.has_embedded_obj and not model.has_embedded_companion_obj for model in decoded.models)
        assert tuple(model.current_axes for model in decoded.models) == tuple(model.current_axes for model in snapshot.models)
        assert tuple(model.companion_axes for model in decoded.models) == tuple(model.companion_axes for model in snapshot.models)
        assert decoded.markers == snapshot.markers
        restored = _restore()
        _assert_restored_geometry(origin, restored)
        read_provenance = restored.model_catalog.models["moving"].alignment_provenance
        assert read_provenance.backend_id == "SMALL_GICP" and read_provenance.icp_fitness == 0.97
        assert read_provenance.engine_metadata_json == "" and read_provenance.solver_geometry_dependencies == ()


def test_versions_one_through_five_keep_legacy_alignment_and_markers() -> None:
    """v1~v5 원문의 기존 엔진·C·점군 마커가 CC 전환 뒤에도 실제 복원됩니다."""
    from test_companion_marker_persistence import _install_pair
    from test_companion_persistence import _legacy_payload, _restore, _write_snapshot
    from tools.align.alignment.types import create_sim3_transform
    from tools.align.constants import PROJECT_PERSISTENCE_SCHEMA_VERSION, PROJECT_TEXT_SCHEMA_KEY
    from tools.align.project_persistence import decode_project_state

    with _project_fixture(companions=False) as origin:
        _install_pair(origin, reference_companion=False, moving_companion=False)
        origin.model_repository.set_alignment_transform("moving", create_sim3_transform(
            np.eye(3), 1.6, np.asarray((12.0, -7.0, 4.0))))
        provenance = _old_provenance(origin)
        origin.model_catalog.set_alignment_provenance("moving", provenance)
        for version in range(1, 6):
            snapshot, text = _write_snapshot(origin)
            payload = _legacy_payload(snapshot, version)
            decoded = decode_project_state(payload)
            assert decoded.schema_version == PROJECT_PERSISTENCE_SCHEMA_VERSION
            assert decoded.models[1].catalog.alignment_provenance == provenance
            assert decoded.models[1].alignment_matrix_float64 == snapshot.models[1].alignment_matrix_float64
            text.clear()
            text.write(payload)
            text[PROJECT_TEXT_SCHEMA_KEY] = version
            restored = _restore()
            _assert_restored_geometry(origin, restored)
            assert restored.model_repository.companion_meshes == {}


def _common_alignments(runtime) -> dict[str, np.ndarray]:
    """축 편집 전후 비교에 사용할 소유자별 확정 C를 복사합니다."""
    repository = runtime.model_repository
    return {model_id: repository.alignment_transform_for_model_id(model_id).matrix_float64.copy()
            for model_id in repository.models}


def _assert_common_alignments(runtime, before) -> None:
    """품질 무효화와 Undo/Redo가 모든 소유자의 공통 C를 변경하지 않는지 확인합니다."""
    for model_id, matrix in before.items():
        np.testing.assert_array_equal(runtime.model_repository.alignment_transform_for_model_id(model_id).matrix_float64, matrix)


def _change_axis(runtime, model_id: str, source: str) -> None:
    """실제 축 편집 시작·갱신·확정 API로 지정한 형상 축 하나만 바꿉니다."""
    from test_geometry_axes_runtime import _axes
    from tools.align.axis_settings import begin_axis_edit, update_axis_edit, commit_axis_edit

    repository = runtime.model_repository
    token = begin_axis_edit(runtime, model_id)
    replacement = _axes(47.0)
    main = replacement[0] if source == "MAIN" else repository.display_transform_for_model_id(model_id)
    companion = replacement[1] if source == "COMPANION" else repository.companion_display_transform_for_model_id(model_id)
    update_axis_edit(runtime, token, main_transform=main, companion_transform=companion)
    assert commit_axis_edit(runtime, token)


def test_reference_companion_invalidates_cc_quality_and_undo_restores_engine() -> None:
    """A 연결 축 변경은 P 품질만 무효화하고 Undo/Redo로 CC 출처까지 함께 복원합니다."""
    from test_geometry_axes_runtime import _fixture
    from tools.align import session

    with _fixture() as runtime:
        provenance = _cc_provenance(runtime)
        runtime.model_catalog.set_alignment_provenance("moving", provenance)
        before_c = _common_alignments(runtime)
        before_mesh = runtime.model_repository.companion_display_transform_for_model_id("reference")
        reference_provenance = runtime.model_catalog.models["reference"].alignment_provenance
        runtime.icp_alignment.result = object()
        assert runtime.marker_selection.completed_pairs == []
        assert runtime.model_catalog.models["moving"].show_points
        _change_axis(runtime, "reference", "COMPANION")
        changed = runtime.model_catalog.models["moving"].alignment_provenance
        assert changed.method == "MANUAL" and changed.icp_rmse is None and changed.icp_status is None
        assert changed.solver_geometry_dependencies == provenance.solver_geometry_dependencies
        assert changed.engine_metadata_json == "" and changed.backend_id == ""
        assert runtime.model_catalog.models["reference"].alignment_provenance is reference_provenance
        assert runtime.icp_alignment.result is None
        _assert_common_alignments(runtime, before_c)
        assert session.undo_session_state()
        assert runtime.model_catalog.models["moving"].alignment_provenance is provenance
        assert runtime.model_repository.companion_display_transform_for_model_id("reference") is before_mesh
        assert json.loads(runtime.model_catalog.models["moving"].alignment_provenance.engine_metadata_json)["geometry_mode"] == "MESH_TO_MESH"
        _assert_common_alignments(runtime, before_c)
        assert session.redo_session_state()
        assert runtime.model_catalog.models["moving"].alignment_provenance is changed
        _assert_common_alignments(runtime, before_c)


def test_marker_and_solver_dependencies_are_both_observed() -> None:
    """마커에만 남은 A 축 의존성도 정밀 정렬 출처와 합쳐 품질을 무효화합니다."""
    from test_geometry_axes_runtime import _fixture

    with _fixture() as runtime:
        provenance = replace(_cc_provenance(runtime, marker_dependencies=(("reference", "COMPANION"),)),
                             solver_geometry_dependencies=(("moving", "COMPANION"),))
        assert provenance.geometry_dependencies == (("moving", "COMPANION"), ("reference", "COMPANION"))
        runtime.model_catalog.set_alignment_provenance("moving", provenance)
        before_c = _common_alignments(runtime)
        _change_axis(runtime, "reference", "COMPANION")
        changed = runtime.model_catalog.models["moving"].alignment_provenance
        assert changed.method == "MANUAL"
        assert changed.marker_geometry_dependencies == provenance.marker_geometry_dependencies
        assert changed.solver_geometry_dependencies == provenance.solver_geometry_dependencies
        _assert_common_alignments(runtime, before_c)


def test_unused_companion_axes_keep_point_quality_and_cached_result() -> None:
    """점군 정렬에 쓰이지 않은 A 또는 P 연결 축은 품질과 완료 결과를 유지합니다."""
    from test_geometry_axes_runtime import _fixture

    for owner_id in ("reference", "moving"):
        with _fixture() as runtime:
            provenance = _cc_provenance(runtime, source="MAIN")
            runtime.model_catalog.set_alignment_provenance("moving", provenance)
            cached = object()
            runtime.icp_alignment.result = cached
            before_c = _common_alignments(runtime)
            _change_axis(runtime, owner_id, "COMPANION")
            assert runtime.model_catalog.models["moving"].alignment_provenance is provenance
            assert runtime.icp_alignment.result is cached
            _assert_common_alignments(runtime, before_c)


def test_unused_main_axis_keeps_related_quality_and_existing_owner_policy() -> None:
    """미사용 A 주축은 종속 P 품질을 유지하고 자기 주축 변경의 기존 무효화는 보존합니다."""
    from test_geometry_axes_runtime import _fixture

    for owner_id in ("reference", "moving"):
        with _fixture() as runtime:
            provenance = _cc_provenance(runtime)
            runtime.model_catalog.set_alignment_provenance("moving", provenance)
            runtime.icp_alignment.result = object()
            before_c = _common_alignments(runtime)
            _change_axis(runtime, owner_id, "MAIN")
            assert runtime.model_catalog.models[owner_id].alignment_provenance.method == "MANUAL"
            if owner_id == "reference":
                assert runtime.model_catalog.models["moving"].alignment_provenance is provenance
            else:
                assert runtime.model_catalog.models["moving"].alignment_provenance.engine_metadata_json == ""
            assert runtime.icp_alignment.result is None
            _assert_common_alignments(runtime, before_c)


def test_engine_metadata_rejects_nonobjects_nonfinite_and_oversize() -> None:
    """새 엔진 출처 필드는 객체 형식·유한 수치·저장 크기 제한을 지킵니다."""
    from tools.align.alignment.types import AlignmentProvenanceRecord

    for metadata in ("[]", '{"오차": NaN}', json.dumps({"설명": "가" * 90000}, ensure_ascii=False)):
        try:
            AlignmentProvenanceRecord(model_id="moving", method="ICP", engine_metadata_json=metadata)
        except ValueError:
            pass
        else:
            raise AssertionError("잘못된 엔진 메타데이터가 허용되었습니다.")
    provenance = AlignmentProvenanceRecord(
        model_id="moving", method="ICP", engine_metadata_json=' { "설명" : "검증", "배율": 1.05 } ',
        marker_geometry_dependencies=(("moving", "MAIN"),),
        solver_geometry_dependencies=(("reference", "COMPANION"), ("moving", "MAIN")))
    assert provenance.geometry_dependencies == (("moving", "MAIN"), ("reference", "COMPANION"))
    assert provenance.to_mapping()["engine_metadata"] == {"설명": "검증", "배율": 1.05}


def main() -> None:
    """실제 Blender에서 CC 저장·축 의존성의 독립 회귀를 모두 실행합니다."""
    _add_test_paths()
    tests = tuple(value for name, value in globals().items() if name.startswith("test_") and callable(value))
    for test in tests:
        test()
    print(f"CC 저장·축 의존성: {len(tests)}개 검사 통과")


if __name__ == "__main__":
    main()
