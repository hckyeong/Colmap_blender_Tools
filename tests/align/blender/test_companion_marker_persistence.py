"""연결 메시 마커 v6 저장·복원과 구버전 보존 경계를 실제 원본으로 검증합니다."""

from __future__ import annotations

from dataclasses import replace
import hashlib
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory

import numpy as np


def _bootstrap() -> None:
    """현재 소스와 기존 실제 가져오기·복원 도우미를 검사 경로에 등록합니다."""

    for directory in (Path(__file__).resolve().parents[3], Path(__file__).resolve().parent):
        if str(directory) not in sys.path:
            sys.path.insert(0, str(directory))


def _payload(document: dict) -> str:
    """검사 목적으로 바꾼 저장 원문의 전체 지문을 정확히 다시 계산합니다."""

    document.pop("payload_fingerprint", None)
    canonical = json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    document["payload_fingerprint"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return json.dumps(document, ensure_ascii=False)


def _origin(root: Path, *, companions: bool = True):
    """실제 COLMAP 두 레이어와 선택적 연결 메시를 같은 런타임에 가져옵니다."""

    from test_companion_persistence import _dataset
    from test_project_restore import _run_import_until_terminal
    from tools.align.importer import create_import_job
    from tools.align.state import RuntimeState

    runtime = RuntimeState()
    for model_id in ("reference", "moving"):
        _dataset(root / model_id)
        job = create_import_job(
            root / model_id, input_format="TEXT", model_id_factory=lambda value=model_id: value,
            import_companion_obj=companions, prepare_gpu=False,
        )
        assert _run_import_until_terminal(job, runtime) == "FINISHED", job.error_message
    return runtime


def _marker(model_id: str, role: str, *, companion: bool, source_id: int | None = None):
    """점군 점 또는 연결 메시 삼각면의 검증 가능한 원본 좌표 마커를 만듭니다."""

    from tools.align.state import SelectedPoint

    return SelectedPoint(
        model_role=role,
        model_id=model_id,
        point_kind="MESH_SURFACE" if companion else "COLMAP_POINT",
        point3d_id=(0 if companion else 101) if source_id is None else source_id,
        source_point_id=(0 if companion else 101) if source_id is None else source_id,
        local_position=(0.25, 0.25, 0.0) if companion else (0.0, 0.0, 0.0),
        geometry_source="COMPANION" if companion else "MAIN",
    )


def _install_pair(runtime, *, reference_companion: bool, moving_companion: bool) -> None:
    """원본 좌표를 사용하는 완료 마커 한 쌍을 유휴 상태에 설치합니다."""

    from tools.align.state import MarkerPair, MarkerSelectionSnapshot

    runtime.marker_selection.restore_snapshot(MarkerSelectionSnapshot(
        phase="IDLE", next_pair_id=2, active_pair_id=1, draft=None, message="",
        completed_pairs=(MarkerPair(
            pair_id=1,
            reference=_marker("reference", "REFERENCE", companion=reference_companion),
            moving=_marker("moving", "MOVING", companion=moving_companion),
        ),),
    ))


def _write_document(text, document: dict) -> None:
    """활성 프로젝트의 검사 원문과 원본 스키마 표시를 함께 기록합니다."""

    from tools.align.constants import PROJECT_TEXT_SCHEMA_KEY

    text.clear()
    text.write(_payload(document))
    text[PROJECT_TEXT_SCHEMA_KEY] = document["schema_version"]


def _stage_restore(runtime, snapshot) -> None:
    """기존 세션 교체 경계에 복원 스냅샷을 준비하고 실제 단계형 복원을 실행합니다."""

    from test_project_restore import _drain_restore_timer
    from tools.align.project_persistence import runtime as persistence

    state = runtime.project_restore
    state.snapshot = snapshot
    state.project_uuid = snapshot.project_uuid
    state.generation += 1
    state.phase = "RESTORING"
    state.current_model_index = 0
    persistence._new_restore_staging(runtime)
    persistence._schedule_restore_timer(runtime)
    _drain_restore_timer(runtime)


def test_companion_surface_roundtrip_preserves_raw_coordinates() -> None:
    """서로 다른 형상 축·정렬·표시에도 연결 면 마커가 원본 좌표와 출처로 복원됩니다."""

    from test_companion_persistence import _restore, _write_snapshot
    from tools.align.alignment.types import Sim3Transform
    from tools.align.constants import PROJECT_PERSISTENCE_SCHEMA_VERSION
    from tools.align.display_axes import create_model_display_transform
    from tools.align.project_persistence import decode_project_state, encode_project_state

    with TemporaryDirectory() as directory:
        origin = _origin(Path(directory))
        for reference_companion, moving_companion in ((True, True), (True, False), (False, True)):
            _install_pair(origin, reference_companion=reference_companion, moving_companion=moving_companion)
            original_pair = origin.marker_selection.completed_pairs[0]
            for model_id in ("reference", "moving"):
                origin.model_repository.set_companion_display_transform(model_id, create_model_display_transform(
                    "CUSTOM", "-X", "Z", rotation_xyz_degrees=(13.0, 27.0, -19.0),
                ))
            matrix = np.eye(4, dtype=np.float64)
            matrix[:3, 3] = (11.0, -8.0, 2.0)
            origin.model_repository.set_alignment_transform("moving", Sim3Transform(matrix_float64=matrix))
            origin.model_catalog.set_model_visibility("reference", False)
            snapshot, _text = _write_snapshot(origin)
            payload = encode_project_state(snapshot)
            assert snapshot.schema_version == PROJECT_PERSISTENCE_SCHEMA_VERSION
            assert "draft" not in payload
            assert "geometry_lock" not in payload
            decoded = decode_project_state(payload)
            assert decoded.markers.completed_pairs[0].reference.geometry_source == original_pair.reference.geometry_source
            restored = _restore()
            assert restored.marker_selection.completed_pairs[0] == original_pair
            assert tuple(restored.model_repository.models) == ("reference", "moving")
            assert tuple(restored.model_repository.companion_meshes) == ("reference", "moving")
            assert restored.model_catalog.models["reference"].is_visible is False
            assert np.array_equal(restored.model_repository.alignment_transform_for_model_id("moving").matrix_float64, matrix)


def test_versions_one_through_five_default_to_main() -> None:
    """과거 모든 스키마의 완료 점군 마커가 주형상 출처로 복원됩니다."""

    from test_companion_persistence import _legacy_payload, _restore, _write_snapshot
    from tools.align.constants import PROJECT_TEXT_SCHEMA_KEY
    from tools.align.project_persistence import decode_project_state

    with TemporaryDirectory() as directory:
        origin = _origin(Path(directory), companions=False)
        _install_pair(origin, reference_companion=False, moving_companion=False)
        for version in range(1, 6):
            snapshot, text = _write_snapshot(origin)
            payload = _legacy_payload(snapshot, version)
            decoded = decode_project_state(payload)
            assert all(point.geometry_source == "MAIN" for pair in decoded.markers.completed_pairs for point in (pair.reference, pair.moving))
            text.clear()
            text.write(payload)
            text[PROJECT_TEXT_SCHEMA_KEY] = version
            restored = _restore()
            assert restored.marker_selection.completed_pairs == origin.marker_selection.completed_pairs
            assert restored.model_repository.companion_meshes == {}


def test_legacy_visibility_migration_preserves_axes_and_hidden_state() -> None:
    """동시 표시·전체 숨김을 마커 우선순위로 정규화하며 기존 축과 정렬을 보존합니다."""

    from test_companion_persistence import _legacy_payload, _write_snapshot
    from tools.align.project_persistence import decode_project_state

    with TemporaryDirectory() as directory:
        origin = _origin(Path(directory))
        for with_markers in (False, True):
            if with_markers:
                _install_pair(origin, reference_companion=False, moving_companion=False)
            snapshot, _text = _write_snapshot(origin)
            for version in (4, 5):
                for points, surface in ((True, True), (False, False), (True, False), (False, True)):
                    for visible in (True, False):
                        document = json.loads(_legacy_payload(snapshot, version))
                        for model in document["models"]:
                            model["catalog"].update(show_points=points, show_surface=surface, is_visible=visible)
                        decoded = decode_project_state(_payload(document))
                        for saved, upgraded in zip(snapshot.models, decoded.models):
                            expected_points = points if points != surface else with_markers
                            assert (upgraded.catalog.show_points, upgraded.catalog.show_surface) == (expected_points, not expected_points)
                            assert upgraded.catalog.is_visible == (visible and (points or surface))
                            assert upgraded.alignment_matrix_float64 == saved.alignment_matrix_float64
                            assert upgraded.source == saved.source
                            if version == 5:
                                assert upgraded.current_axes == saved.current_axes
                                assert upgraded.companion_axes == saved.companion_axes


def test_legacy_mixed_role_markers_are_restored_and_resaved() -> None:
    """과거 같은 역할의 점군·독립 OBJ 마커를 충돌 안내 상태로 보존하고 다시 저장합니다."""

    from test_companion_persistence import _legacy_payload, _restore, _write_snapshot
    from test_project_restore import _run_import_until_terminal
    from tools.align.constants import PROJECT_TEXT_SCHEMA_KEY
    from tools.align.mesh_importer import create_mesh_import_job
    from tools.align.project_persistence import decode_project_state, encode_project_state
    from tools.align.project_persistence.runtime import build_project_state_snapshot
    from tools.align.state import MarkerPair, MarkerSelectionSnapshot, SelectedPoint

    with TemporaryDirectory() as directory:
        root = Path(directory)
        origin = _origin(root)
        mesh_path = root / "standalone.obj"
        mesh_path.write_text("v 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n", encoding="utf-8")
        job = create_mesh_import_job(mesh_path, model_id_factory=lambda: "mesh", prepare_gpu=False)
        assert _run_import_until_terminal(job, origin) == "FINISHED", job.error_message
        origin.model_catalog.models["mesh"] = replace(origin.model_catalog.models["mesh"], registration_status="REGISTERED")
        _install_pair(origin, reference_companion=False, moving_companion=False)
        first = origin.marker_selection.completed_pairs[0]
        second = MarkerPair(
            pair_id=2,
            reference=SelectedPoint(model_role="REFERENCE", model_id="mesh", point_kind="MESH_SURFACE", point3d_id=0, source_point_id=0, local_position=(0.2, 0.3, 0.0)),
            moving=SelectedPoint(model_role="MOVING", model_id="moving", point3d_id=205, source_point_id=205, local_position=(2.0, 1.0, -3.0)),
        )
        origin.marker_selection.restore_snapshot(MarkerSelectionSnapshot(
            phase="IDLE", next_pair_id=3, active_pair_id=2, completed_pairs=(first, second), draft=None, message="",
        ))
        snapshot, text = _write_snapshot(origin)
        payload = _legacy_payload(snapshot, 5)
        decoded = decode_project_state(payload)
        assert len(decoded.markers.completed_pairs) == 2
        text.clear()
        text.write(payload)
        text[PROJECT_TEXT_SCHEMA_KEY] = 5
        restored = _restore()
        assert tuple(restored.marker_selection.completed_pairs) == (first, second)
        assert restored.marker_selection.geometry_conflict_message()
        resaved = decode_project_state(encode_project_state(build_project_state_snapshot(restored)))
        assert len(resaved.markers.completed_pairs) == 2
        assert resaved.markers.completed_pairs == decoded.markers.completed_pairs


def test_invalid_marker_reference_never_replaces_current_runtime() -> None:
    """잘못된 형상 출처·참조·면 좌표 복원은 기존 세션을 부분 교체하지 않습니다."""

    from test_companion_persistence import _write_snapshot
    from tools.align.project_persistence import ProjectPersistenceCodecError, decode_project_state
    from tools.align.project_persistence.runtime import schedule_project_restore_if_available
    from tools.align.state import RuntimeState

    with TemporaryDirectory() as directory:
        root = Path(directory)
        incoming = _origin(root / "incoming")
        _install_pair(incoming, reference_companion=True, moving_companion=True)
        existing = _origin(root / "existing")
        _snapshot, text = _write_snapshot(incoming)
        valid = text.as_string()
        original = {name: getattr(existing, name) for name in (
            "model_repository", "model_catalog", "marker_selection", "axis_settings", "point_clouds", "gpu_batches", "undo_journal",
        )}
        mutations = (
            {"geometry_source": "OTHER"}, {"geometry_source": ""}, {"geometry_source": None},
            {"geometry_source": "MAIN"}, {"point_kind": "MESH_VERTEX"},
            {"source_point_id": 999}, {"local_position": [4.0, 4.0, 0.0]},
        )
        for mutation in mutations:
            document = json.loads(valid)
            document["markers"]["completed_pairs"][0]["reference"].update(mutation)
            _write_document(text, document)
            try:
                snapshot = decode_project_state(text.as_string())
            except ProjectPersistenceCodecError:
                empty_target = RuntimeState()
                assert schedule_project_restore_if_available(empty_target) is False
                assert empty_target.project_restore.phase == "FAILED"
            else:
                _stage_restore(existing, snapshot)
                assert existing.project_restore.phase == "FAILED", existing.project_restore.last_error
                assert existing.project_restore.staging_runtime is None
            assert all(getattr(existing, name) is value for name, value in original.items())
            saved_marker = json.loads(text.as_string())["markers"]["completed_pairs"][0]["reference"]
            assert all(saved_marker[key] == value for key, value in mutation.items())
        document = json.loads(valid)
        document["markers"]["completed_pairs"][0]["reference"].pop("geometry_source")
        _write_document(text, document)
        assert schedule_project_restore_if_available(RuntimeState()) is False
        assert all(getattr(existing, name) is value for name, value in original.items())


def test_missing_or_changed_external_companion_restores_embedded_markers() -> None:
    """연결 OBJ 외부 파일이 누락·변경돼도 내장 표면과 마커를 복원합니다."""

    from test_companion_persistence import _write_snapshot

    with TemporaryDirectory() as directory:
        root = Path(directory)
        incoming = _origin(root / "incoming")
        _install_pair(incoming, reference_companion=True, moving_companion=True)
        existing = _origin(root / "existing")
        snapshot, _text = _write_snapshot(incoming)
        embedded_uuid = snapshot.models[0].companion_source.embedded_obj_asset_uuid
        source = root / "incoming" / "reference" / "surface.obj"
        original_bytes = source.read_bytes()
        source.unlink()
        _stage_restore(existing, snapshot)
        assert existing.project_restore.phase == "IDLE", existing.project_restore.last_error
        assert existing.model_repository.companion_meshes["reference"].embedded_asset_uuid == embedded_uuid
        assert existing.marker_selection.completed_pairs[0].reference.geometry_source == "COMPANION"
        source.write_bytes(original_bytes.replace(b"v 1 0 0", b"v 2 0 0"))
        _stage_restore(existing, snapshot)
        assert existing.project_restore.phase == "IDLE", existing.project_restore.last_error
        assert existing.model_repository.companion_meshes["reference"].embedded_asset_uuid == embedded_uuid
        assert existing.marker_selection.completed_pairs[0].reference.geometry_source == "COMPANION"


def test_committed_geometry_dependencies_roundtrip_without_markers() -> None:
    """마커가 정리된 확정 ICP의 형상 의존성을 저장·복원하고 잘못된 출처를 거부합니다."""

    from test_companion_persistence import _restore, _write_snapshot
    from tools.align.alignment.types import AlignmentProvenanceRecord
    from tools.align.project_persistence import ProjectPersistenceCodecError, decode_project_state

    with TemporaryDirectory() as directory:
        origin = _origin(Path(directory))
        dependencies = (("reference", "COMPANION"), ("moving", "MAIN"), ("deleted-reference", "COMPANION"))
        origin.model_catalog.set_alignment_provenance("moving", AlignmentProvenanceRecord(
            model_id="moving", method="ICP", marker_geometry_dependencies=dependencies,
        ))
        _snapshot, text = _write_snapshot(origin)
        restored = _restore()
        assert restored.marker_selection.completed_pairs == []
        assert restored.model_catalog.models["moving"].alignment_provenance.marker_geometry_dependencies == tuple(sorted(dependencies))
        document = json.loads(text.as_string())
        for invalid in ([["reference", "OTHER"]], [["reference"]], [["reference", "MAIN"], ["reference", "MAIN"]]):
            document["models"][1]["catalog"]["alignment_provenance"]["marker_geometry_dependencies"] = invalid
            try:
                decode_project_state(_payload(document))
            except ProjectPersistenceCodecError:
                pass
            else:
                raise AssertionError("잘못된 확정 마커 형상 의존성을 수용했습니다.")


def run_all() -> None:
    """연결 마커 저장·구버전 보존·복원 원자성의 핵심 회귀를 실행합니다."""

    _bootstrap()
    from test_project_restore import _remove_owned_project_texts

    checks = (
        test_companion_surface_roundtrip_preserves_raw_coordinates,
        test_versions_one_through_five_default_to_main,
        test_legacy_visibility_migration_preserves_axes_and_hidden_state,
        test_legacy_mixed_role_markers_are_restored_and_resaved,
        test_invalid_marker_reference_never_replaces_current_runtime,
        test_missing_or_changed_external_companion_restores_embedded_markers,
        test_committed_geometry_dependencies_roundtrip_without_markers,
    )
    try:
        for check in checks:
            check()
    finally:
        _remove_owned_project_texts()
    print(f"test_companion_marker_persistence: {len(checks)}개 통과")


if __name__ == "__main__":
    run_all()
