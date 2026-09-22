"""COLMAP 소유 연결 OBJ의 버전 호환·표시 왕복·복원 원자성을 검증합니다."""

from __future__ import annotations

from dataclasses import replace
import hashlib
import json
from pathlib import Path
import shutil
import sys
from tempfile import TemporaryDirectory
from unittest.mock import patch

import bpy
import numpy as np


def _bootstrap() -> None:
    """현재 소스와 공용 복원 검사 도우미를 모듈 검색 경로에 등록합니다."""

    for directory in (Path(__file__).resolve().parents[3], Path(__file__).resolve().parent):
        if str(directory) not in sys.path:
            sys.path.insert(0, str(directory))


def _dataset(root: Path) -> Path:
    """COLMAP과 재질 참조가 있는 최소 연결 OBJ를 같은 데이터셋에 만듭니다."""

    from test_project_restore import _write_text_sparse_model

    _write_text_sparse_model(root / "sparse" / "0", point_offset=0.0)
    mesh = root / "surface.obj"
    mesh.write_text(
        "mtllib surface.mtl\nv 0 0 0\nv 1 0 0\nv 0 1 0\n"
        "usemtl sample\nf 1 2 3\n", encoding="utf-8",
    )
    (root / "surface.mtl").write_text("newmtl sample\nmap_Kd image.png\n", encoding="utf-8")
    (root / "image.png").write_bytes("재질을 읽지 않는 검사".encode("utf-8"))
    return mesh


def _origin(root: Path, *, companion: bool = True, model_id: str = "owner"):
    """GPU 생성 없이 실제 COLMAP·선택 연결 OBJ를 읽은 정상 Runtime을 반환합니다."""

    from test_project_restore import _run_import_until_terminal
    from tools.align.importer import create_import_job
    from tools.align.state import RuntimeState

    runtime = RuntimeState()
    job = create_import_job(
        root, input_format="TEXT", model_id_factory=lambda: model_id,
        import_companion_obj=companion, prepare_gpu=False,
    )
    assert _run_import_until_terminal(job, runtime) == "FINISHED", job.error_message
    return runtime


def _write_snapshot(runtime):
    """현재 Runtime을 유일한 활성 프로젝트 Text로 저장합니다."""

    from test_project_restore import _remove_owned_project_texts
    from tools.align.project_persistence.runtime import build_project_state_snapshot
    from tools.align.project_persistence.store import write_active_project_state

    _remove_owned_project_texts()
    snapshot = build_project_state_snapshot(runtime)
    return snapshot, write_active_project_state(snapshot)


def _restore():
    """실제 복원 타이머를 끝까지 진행하고 유휴 상태의 Runtime을 반환합니다."""

    from test_project_restore import _drain_restore_timer
    from tools.align.project_persistence.runtime import schedule_project_restore_if_available
    from tools.align.state import RuntimeState

    runtime = RuntimeState()
    assert schedule_project_restore_if_available(runtime), runtime.project_restore.last_error
    _drain_restore_timer(runtime)
    assert runtime.project_restore.phase == "IDLE", runtime.project_restore.last_error
    return runtime


def _legacy_payload(snapshot, schema_version: int) -> str:
    """현재 스냅샷에서 중심 변환과 내장 식별자를 제외해 실제 v1~v7 원문을 재현합니다."""

    from tools.align.project_persistence import encode_project_state

    document = json.loads(encode_project_state(snapshot))
    document["schema_version"] = schema_version
    document.pop("payload_fingerprint")
    for model in document["models"]:
        for name in ("current_axes", "companion_axes"):
            if name in model:
                for field in ("translation_xyz", "scale_xyz", "pivot_source"):
                    model[name].pop(field, None)
        model.pop("embedded_obj_asset_uuid", None)
        model.pop("embedded_obj_schema_version", None)
        model.pop("embedded_obj_data_name_hint", None)
        model.pop("embedded_obj_geometry_revision", None)
        companion = model.get("companion_source")
        if companion is not None:
            companion.pop("embedded_obj_asset_uuid", None)
            companion.pop("embedded_obj_schema_version", None)
            companion.pop("embedded_obj_data_name_hint", None)
            companion.pop("embedded_obj_geometry_revision", None)
            if not companion["source_fingerprint"]:
                companion["source_fingerprint"] = hashlib.sha256(
                    Path(companion["path_absolute"]).read_bytes()
                ).hexdigest()
        source = model["source"]
        if not source["source_fingerprint"] and source.get("kind") == "OBJ":
            source["source_fingerprint"] = hashlib.sha256(
                Path(source["path_absolute"]).read_bytes()
            ).hexdigest()
        if schema_version < 5:
            model.pop("current_axes", None)
            model.pop("companion_axes", None)
        if schema_version >= 4:
            continue
        model.pop("companion_source", None)
        model["catalog"].pop("show_points")
        model["catalog"].pop("show_surface")
        source = model["source"]
        selected_root = source.pop("selected_root_absolute")
        source.pop("selected_root_relative")
        if schema_version == 1:
            model.pop("geometry_kind")
            source.pop("kind")
            source["selected_root_absolute"] = selected_root
            source["sparse_path_absolute"] = source.pop("path_absolute")
            source["sparse_path_relative"] = source.pop("path_relative")
            source["display_point_limit"] = source.pop("display_vertex_limit")
    for pair in document["markers"]["completed_pairs"]:
        for point in (pair["reference"], pair["moving"]):
            if schema_version < 6:
                point.pop("geometry_source", None)
            if schema_version < 3:
                point.pop("local_position", None)
            if schema_version == 1:
                point["point3d_id"] = point.pop("source_point_id")
                point.pop("point_kind", None)
    document["payload_fingerprint"] = hashlib.sha256(
        json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return json.dumps(document, ensure_ascii=False)


def test_companion_visibility_and_transform_roundtrip() -> None:
    """단일 형상 선택·정렬을 왕복하며 복원 중 재질 읽기와 GPU 생성을 금지합니다."""

    from tools.align.alignment.types import Sim3Transform
    from tools.align.constants import PROJECT_PERSISTENCE_SCHEMA_VERSION
    from tools.align.mesh_io import obj_reader
    from tools.align import importer, mesh_importer
    from tools.align.project_persistence import decode_project_state, encode_project_state

    with TemporaryDirectory() as directory:
        root = Path(directory) / "dataset"
        _dataset(root)
        with patch.object(obj_reader, "resolve_obj_materials", side_effect=AssertionError("재질 읽기 금지")), \
             patch.object(importer, "_prepare_gpu_batch_for_model", side_effect=AssertionError("복원 GPU 생성 금지")), \
             patch.object(mesh_importer, "_prepare_gpu_batch_for_model", side_effect=AssertionError("복원 GPU 생성 금지")):
            origin = _origin(root)
            matrix = np.eye(4, dtype=np.float64)
            matrix[:3, 3] = (4.0, -2.0, 1.0)
            origin.model_repository.set_alignment_transform("owner", Sim3Transform(matrix_float64=matrix))
            for show_points, show_surface in ((False, True), (True, False)):
                origin.model_catalog.set_geometry_visibility("owner", show_points=show_points, show_surface=show_surface)
                snapshot, _text = _write_snapshot(origin)
                document = json.loads(encode_project_state(snapshot))
                model = document["models"][0]
                assert document["schema_version"] == PROJECT_PERSISTENCE_SCHEMA_VERSION
                assert len(document["models"]) == 1
                assert set(model["companion_source"]) == {
                    "path_absolute", "path_relative", "source_fingerprint", "geometry_only",
                    "embedded_obj_asset_uuid", "embedded_obj_schema_version",
                    "embedded_obj_data_name_hint", "embedded_obj_geometry_revision",
                }
                assert "positions_float64" not in json.dumps(document)
                decoded = decode_project_state(encode_project_state(snapshot))
                assert decoded.models[0].companion_source == snapshot.models[0].companion_source
                restored = _restore()
                assert tuple(restored.model_repository.models) == ("owner",)
                assert tuple(restored.model_catalog.models) == ("owner",)
                assert tuple(restored.model_repository.companion_meshes) == ("owner",)
                assert tuple(restored.model_repository.companion_sources) == ("owner",)
                assert restored.model_repository.companion_mesh_for_model_id("owner").model_id == "owner"
                record = restored.model_catalog.models["owner"]
                assert (record.show_points, record.show_surface) == (show_points, show_surface)
                assert np.array_equal(restored.model_repository.alignment_transform_for_model_id("owner").matrix_float64, matrix)
                assert restored.model_repository.companion_mesh_for_model_id("owner").materials == ()


def test_legacy_and_unconnected_projects_never_discover_sibling_obj() -> None:
    """과거 세 버전과 연결 없는 v4를 복원해도 같은 폴더 OBJ를 자동 추가하지 않습니다."""

    from tools.align.constants import PROJECT_TEXT_SCHEMA_KEY
    from tools.align.project_persistence import decode_project_state

    with TemporaryDirectory() as directory:
        root = Path(directory) / "dataset"
        _dataset(root)
        origin = _origin(root, companion=False)
        origin.model_catalog.set_model_visibility("owner", False)
        snapshot, _text = _write_snapshot(origin)
        current = _restore()
        assert current.model_repository.companion_meshes == {}
        for version in (1, 2, 3):
            _saved, text = _write_snapshot(origin)
            payload = _legacy_payload(_saved, version)
            decoded = decode_project_state(payload)
            assert decoded.models[0].companion_source is None
            assert decoded.models[0].catalog.show_points is True
            assert decoded.models[0].catalog.show_surface is False
            text.clear()
            text.write(payload)
            text[PROJECT_TEXT_SCHEMA_KEY] = version
            restored = _restore()
            assert restored.model_repository.companion_meshes == {}
            assert restored.model_repository.companion_sources == {}
            assert restored.model_catalog.models["owner"].show_points is True


def test_moved_project_uses_same_relative_dataset() -> None:
    """이동한 blend는 COLMAP만 상대 복원하고 내장 OBJ의 최초 출처는 provenance로 유지합니다."""

    from tools.align.model_repository import canonical_source_path
    from tools.align.project_persistence.store import write_active_project_state
    from tools.align.project_persistence.runtime import build_project_state_snapshot

    with TemporaryDirectory() as directory:
        temporary_root = Path(directory)
        original = temporary_root / "original"
        moved = temporary_root / "moved"
        root = original / "dataset"
        source = _dataset(root)
        origin = _origin(root)
        project_file = original / "work.blend"
        bpy.ops.wm.save_as_mainfile(filepath=str(project_file), check_existing=False)
        snapshot = build_project_state_snapshot(origin)
        assert snapshot.models[0].source.selected_root_relative == "dataset"
        assert snapshot.models[0].companion_source.path_relative.replace("\\", "/") == "dataset/surface.obj"
        write_active_project_state(snapshot)
        bpy.ops.wm.save_as_mainfile(filepath=str(project_file), check_existing=False)
        shutil.copytree(original, moved)
        source.write_text(source.read_text(encoding="utf-8") + "# 변경된 이전 파일\n", encoding="utf-8")
        bpy.ops.wm.open_mainfile(filepath=str(moved / "work.blend"), load_ui=False)
        restored = _restore()
        metadata = restored.model_repository.source_metadata_for_model_id("owner")
        companion = restored.model_repository.companion_sources["owner"]
        assert metadata.selected_root_absolute == canonical_source_path(moved / "dataset")
        assert companion.source_path_absolute == canonical_source_path(root / "surface.obj")


def test_missing_and_stale_companion_restore_from_embedded_mesh() -> None:
    """연결 OBJ가 누락·변경되어도 내장 Mesh로 같은 표면을 복원하는지 확인합니다."""

    from test_project_restore import _drain_restore_timer
    from tools.align.project_persistence.runtime import (
        schedule_project_restore_if_available,
    )
    from tools.align.state import RuntimeState

    with TemporaryDirectory() as directory:
        root = Path(directory) / "dataset"
        source = _dataset(root)
        origin = _origin(root)
        snapshot, text = _write_snapshot(origin)
        original_payload = text.as_string()
        preserved_repository = origin.model_repository
        preserved_catalog = origin.model_catalog
        source.rename(root / "different.obj")
        for missing in (True, False):
            if not missing:
                source.write_text("v 0 0 0\nv 2 0 0\nv 0 2 0\nf 1 2 3\n", encoding="utf-8")
            restored = RuntimeState()
            assert schedule_project_restore_if_available(restored) is True
            _drain_restore_timer(restored)
            assert restored.project_restore.phase == "IDLE", restored.project_restore.last_error
            assert tuple(restored.model_repository.models) == ("owner",)
            companion = restored.model_repository.companion_meshes["owner"]
            assert companion.embedded_asset_uuid == snapshot.models[0].companion_source.embedded_obj_asset_uuid
            assert text.as_string() == original_payload
            assert schedule_project_restore_if_available(origin) is False
            assert origin.model_repository is preserved_repository
            assert origin.model_catalog is preserved_catalog
            assert origin.model_repository.companion_sources["owner"].source_fingerprint == snapshot.models[0].companion_source.source_fingerprint


def test_partial_restore_cancel_and_stale_callback_preserve_public_state() -> None:
    """확정 전 연결 모델을 공개하지 않고 취소·파일교체 시 임시 상태를 모두 폐기합니다."""

    from test_project_restore import _run_import_until_terminal
    from tools.align.importer import create_import_job
    from tools.align.project_persistence.runtime import (
        cancel_project_restore, cancel_project_restore_for_file_load, schedule_project_restore_if_available,
    )
    from tools.align.state import RuntimeState

    with TemporaryDirectory() as directory:
        root = Path(directory)
        _dataset(root / "first")
        _dataset(root / "second")
        origin = _origin(root / "first")
        job = create_import_job(root / "second", input_format="TEXT", model_id_factory=lambda: "second", prepare_gpu=False)
        assert _run_import_until_terminal(job, origin) == "FINISHED"
        _write_snapshot(origin)
        for cancel in (cancel_project_restore, cancel_project_restore_for_file_load):
            restored = RuntimeState()
            assert schedule_project_restore_if_available(restored)
            callback = restored.project_restore_timer
            for _ in range(200):
                assert callback() is not None
                if restored.project_restore.current_model_index == 1:
                    break
            assert restored.project_restore.current_model_index == 1
            staging = restored.project_restore.staging_runtime
            assert tuple(staging.model_repository.companion_meshes) == ("owner",)
            assert restored.model_repository.models == {}
            assert restored.model_repository.companion_meshes == {}
            cancel(restored)
            assert callback() is None
            assert restored.project_restore.staging_runtime is None
            assert staging.model_repository.models == {}
            assert staging.model_repository.companion_meshes == {}
            assert restored.model_repository.models == {}
            assert restored.import_job is None


def test_codec_rejects_unsafe_or_non_boolean_companion_fields() -> None:
    """형상 전용 정책·경로·표시 bool을 손상시킨 연결 snapshot을 저장 전에 거부합니다."""

    from tools.align.project_persistence import ProjectPersistenceCodecError, encode_project_state

    with TemporaryDirectory() as directory:
        root = Path(directory) / "dataset"
        _dataset(root)
        snapshot, _text = _write_snapshot(_origin(root))
        model = snapshot.models[0]
        invalid_models = (
            replace(model, companion_source=replace(model.companion_source, geometry_only=False)),
            replace(model, companion_source=replace(model.companion_source, path_absolute="relative.obj")),
            replace(model, companion_source=replace(model.companion_source, source_fingerprint="x" * 64)),
            replace(model, catalog=replace(model.catalog, show_points=1)),
            replace(model, catalog=replace(model.catalog, show_surface="true")),
        )
        for invalid in invalid_models:
            try:
                encode_project_state(replace(snapshot, models=(invalid,)))
            except ProjectPersistenceCodecError:
                pass
            else:
                raise AssertionError("손상된 연결 OBJ 저장값을 수용했습니다.")


def test_late_companion_change_does_not_affect_embedded_restore() -> None:
    """첫 모델 복원 뒤 두 번째 외부 OBJ가 바뀌어도 내장 표면으로 전체 복원합니다."""

    from test_project_restore import _drain_restore_timer, _run_import_until_terminal
    from tools.align.importer import create_import_job
    from tools.align.project_persistence.runtime import schedule_project_restore_if_available
    from tools.align.state import RuntimeState

    with TemporaryDirectory() as directory:
        root = Path(directory)
        _dataset(root / "first")
        second_obj = _dataset(root / "second")
        origin = _origin(root / "first")
        job = create_import_job(root / "second", input_format="TEXT", model_id_factory=lambda: "second", prepare_gpu=False)
        assert _run_import_until_terminal(job, origin) == "FINISHED"
        _snapshot, text = _write_snapshot(origin)
        payload = text.as_string()
        restored = RuntimeState()
        assert schedule_project_restore_if_available(restored)
        callback = restored.project_restore_timer
        for _ in range(200):
            assert callback() is not None
            if restored.project_restore.current_model_index == 1:
                break
        assert restored.project_restore.current_model_index == 1
        staged_repository = restored.project_restore.staging_runtime.model_repository
        assert tuple(staged_repository.companion_meshes) == ("owner",)
        second_obj.write_text(second_obj.read_text(encoding="utf-8") + "# 복원 중 변경\n", encoding="utf-8")
        _drain_restore_timer(restored)
        assert restored.project_restore.phase == "IDLE", restored.project_restore.last_error
        assert restored.project_restore.staging_runtime is None
        assert tuple(restored.model_repository.models) == ("owner", "second")
        assert tuple(restored.model_repository.companion_meshes) == ("owner", "second")
        assert text.as_string() == payload


def test_gpu_failure_and_cancel_preserve_existing_runtime() -> None:
    """확정 GPU 오류와 취소가 기존 모델·축 편집·표시·캐시 객체를 보존합니다."""

    from tools.align.project_persistence import runtime as persistence

    with TemporaryDirectory() as directory:
        root = Path(directory)
        _dataset(root / "existing")
        _dataset(root / "incoming")
        existing = _origin(root / "existing")
        incoming = _origin(root / "incoming", model_id="incoming")
        existing.gpu_batches["검사용 기존 캐시"] = object()
        field_names = (
            "model_repository", "model_catalog", "marker_selection", "marker_alignment",
            "layer_transform", "axis_settings", "icp_alignment", "merge_export", "obj_export", "undo_journal",
            "point_clouds", "gpu_batches",
        )
        previous = {name: getattr(existing, name) for name in field_names}
        existing.project_restore.staging_runtime = incoming
        existing.project_restore.phase = "RESTORING"
        with patch.object(persistence, "_prepare_restored_gpu_state", side_effect=RuntimeError("GPU 준비 실패 검사")):
            try:
                persistence._commit_restore_staging(existing, incoming)
            except RuntimeError as error:
                assert str(error) == "GPU 준비 실패 검사"
            else:
                raise AssertionError("GPU 준비 오류를 전달하지 않았습니다.")
        assert all(getattr(existing, name) is value for name, value in previous.items())
        assert tuple(existing.model_repository.companion_meshes) == ("owner",)
        assert "검사용 기존 캐시" in existing.gpu_batches
        assert persistence.cancel_project_restore(existing) is True
        assert all(getattr(existing, name) is value for name, value in previous.items())
        assert tuple(existing.model_repository.companion_meshes) == ("owner",)
        assert incoming.model_repository.models == {}
        assert incoming.model_repository.companion_meshes == {}


def run_all() -> None:
    """연결 OBJ 저장·복원의 실제 파일 및 임시 상태 경계 회귀를 실행합니다."""

    _bootstrap()
    from test_project_restore import _remove_owned_project_texts

    try:
        test_companion_visibility_and_transform_roundtrip()
        test_legacy_and_unconnected_projects_never_discover_sibling_obj()
        test_missing_and_stale_companion_restore_from_embedded_mesh()
        test_partial_restore_cancel_and_stale_callback_preserve_public_state()
        test_codec_rejects_unsafe_or_non_boolean_companion_fields()
        test_late_companion_change_does_not_affect_embedded_restore()
        test_gpu_failure_and_cancel_preserve_existing_runtime()
        test_moved_project_uses_same_relative_dataset()
    finally:
        _remove_owned_project_texts()
    print("test_companion_persistence: 8개 통과")


if __name__ == "__main__":
    run_all()
