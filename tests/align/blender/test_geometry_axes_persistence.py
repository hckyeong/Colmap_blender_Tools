"""형상별 현재 축의 저장 왕복·구버전 화면·복원 실패 원자성을 검증합니다."""

from __future__ import annotations

from dataclasses import replace
import hashlib
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from unittest.mock import patch

import numpy as np


def _bootstrap() -> None:
    """실험 소스와 실제 가져오기·복원 도우미를 모듈 경로에 연결합니다."""

    for directory in (Path(__file__).resolve().parents[3], Path(__file__).resolve().parent):
        if str(directory) not in sys.path:
            sys.path.insert(0, str(directory))


def _set_axes(runtime):
    """주모델과 연결 메시를 서로 다른 축·추가 회전으로 확정합니다."""

    from tools.align.display_axes import create_model_display_transform

    main = create_model_display_transform("CUSTOM", "X", "Z", rotation_xyz_degrees=(21.0, -13.5, 87.0))
    companion = create_model_display_transform("CUSTOM", "-Z", "Y", rotation_xyz_degrees=(-39.0, 8.25, 14.0))
    runtime.model_repository.set_display_transform("owner", main)
    runtime.model_repository.set_companion_display_transform("owner", companion)
    return main, companion


def _assert_transform(actual, expected) -> None:
    """재생성한 축의 입력 필드·행렬·최종 지문이 모두 같음을 확인합니다."""

    assert actual.preset_id == expected.preset_id
    assert actual.forward_axis == expected.forward_axis
    assert actual.up_axis == expected.up_axis
    assert actual.rotation_xyz_degrees == expected.rotation_xyz_degrees
    assert actual.fingerprint == expected.fingerprint
    assert np.array_equal(actual.matrix_float64, expected.matrix_float64)


def _payload(document) -> str:
    """검사 대상으로 바꾼 JSON의 전체 지문을 올바르게 다시 계산합니다."""

    document.pop("payload_fingerprint", None)
    document["payload_fingerprint"] = hashlib.sha256(
        json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return json.dumps(document, ensure_ascii=False)


def test_independent_axes_roundtrip_preserves_source_and_prepares_final_axes() -> None:
    """최초 축 검증 뒤 서로 다른 현재 축을 복원하고 GPU 준비 전에 적용합니다."""

    from test_companion_persistence import _dataset, _origin, _restore, _write_snapshot
    from tools.align.project_persistence import runtime as persistence
    from tools.align.project_persistence import decode_project_state, encode_project_state

    with TemporaryDirectory() as directory:
        root = Path(directory) / "dataset"
        obj_path = _dataset(root)
        origin = _origin(root)
        original_metadata = origin.model_repository.source_metadata_for_model_id("owner")
        original_bytes = obj_path.read_bytes()
        main, companion = _set_axes(origin)
        snapshot, _text = _write_snapshot(origin)
        assert origin.model_repository.source_metadata_for_model_id("owner") == original_metadata
        assert snapshot.models[0].source.axis_preset == original_metadata.axis_preset
        assert snapshot.models[0].source.display_transform_fingerprint == original_metadata.display_transform_fingerprint
        decoded = decode_project_state(encode_project_state(snapshot))
        assert decoded.models[0].current_axes == snapshot.models[0].current_axes
        assert decoded.models[0].companion_axes == snapshot.models[0].companion_axes
        checked = []
        original_validation = persistence._validate_restored_import

        def validate_initial_axes(runtime, job, model) -> None:
            """복원 가져오기가 최초 축과 메타데이터로 완료된 뒤 검증됨을 확인합니다."""

            original_validation(runtime, job, model)
            metadata = runtime.model_repository.source_metadata_for_model_id("owner")
            assert metadata == original_metadata
            assert runtime.model_repository.display_transform_for_model_id("owner").fingerprint == metadata.display_transform_fingerprint
            checked.append("original")

        def verify_gpu_input(runtime) -> None:
            """GPU 준비 경계에서 확정할 현재 축과 원본 출처를 확인합니다."""

            _assert_transform(runtime.model_repository.display_transform_for_model_id("owner"), main)
            _assert_transform(runtime.model_repository.companion_display_transform_for_model_id("owner"), companion)
            assert runtime.model_repository.source_metadata_for_model_id("owner") == original_metadata
            checked.append("gpu")

        with patch.object(persistence, "_validate_restored_import", side_effect=validate_initial_axes), \
             patch.object(persistence, "_prepare_restored_gpu_state", side_effect=verify_gpu_input):
            restored = _restore()
        assert checked == ["original", "gpu"]
        assert restored.model_repository.source_metadata_for_model_id("owner") == original_metadata
        assert obj_path.read_bytes() == original_bytes
        assert "axis_settings" not in encode_project_state(snapshot)



def test_active_preview_saves_only_committed_axes_across_file_reopen() -> None:
    """활성 축 미리보기 중 실제 blend를 저장·재열기해도 확정 축만 복원합니다."""

    import bpy
    from test_companion_persistence import _dataset, _origin, _restore, _write_snapshot
    from tools.align.axis_settings import begin_axis_edit, effective_axis_transform, update_axis_edit
    from tools.align.display_axes import create_model_display_transform
    from tools.align.project_persistence import encode_project_state

    with TemporaryDirectory() as directory:
        root = Path(directory)
        _dataset(root / "dataset")
        origin = _origin(root / "dataset")
        main, companion = _set_axes(origin)
        origin.session_active = True
        token = begin_axis_edit(origin, "owner")
        draft_main = create_model_display_transform("BLENDER", "Y", "Z", rotation_xyz_degrees=(9.0, 18.0, 27.0))
        draft_companion = create_model_display_transform("CUSTOM", "Y", "-X", rotation_xyz_degrees=(3.0, 6.0, 12.0))
        update_axis_edit(origin, token, main_transform=draft_main, companion_transform=draft_companion)
        _assert_transform(effective_axis_transform(origin, "owner"), draft_main)
        _assert_transform(effective_axis_transform(origin, "owner", companion=True), draft_companion)
        snapshot, _text = _write_snapshot(origin)
        model = snapshot.models[0]
        assert model.current_axes.display_transform_fingerprint == main.fingerprint
        assert model.companion_axes.display_transform_fingerprint == companion.fingerprint
        payload = encode_project_state(snapshot)
        assert token not in payload
        assert draft_main.fingerprint not in payload
        assert draft_companion.fingerprint not in payload
        assert origin.axis_settings.token == token
        project = root / "axes.blend"
        assert bpy.ops.wm.save_as_mainfile(filepath=str(project), check_existing=False) == {"FINISHED"}
        assert bpy.ops.wm.open_mainfile(filepath=str(project), load_ui=False) == {"FINISHED"}
        restored = _restore()
        _assert_transform(restored.model_repository.display_transform_for_model_id("owner"), main)
        _assert_transform(restored.model_repository.companion_display_transform_for_model_id("owner"), companion)
        assert restored.axis_settings.is_active is False


def test_v4_companion_keeps_saved_owner_axes() -> None:
    """v4 연결 메시를 저장 소유자의 축으로 복원해 새 가져오기 기본값을 소급하지 않습니다."""

    from test_companion_persistence import _dataset, _legacy_payload, _origin, _restore, _write_snapshot
    from tools.align.constants import PROJECT_TEXT_SCHEMA_KEY
    from tools.align.display_axes import create_model_display_transform
    from tools.align.project_persistence import decode_project_state

    with TemporaryDirectory() as directory:
        root = Path(directory) / "dataset"
        _dataset(root)
        origin = _origin(root)
        snapshot, text = _write_snapshot(origin)
        source = snapshot.models[0].source
        saved_owner = create_model_display_transform(source.axis_preset, source.axis_forward, source.axis_up)
        payload = _legacy_payload(snapshot, 4)
        migrated = decode_project_state(payload)
        assert migrated.models[0].current_axes == migrated.models[0].companion_axes
        assert migrated.models[0].companion_axes.rotation_xyz_degrees == (0.0, 0.0, 0.0)
        text.clear()
        text.write(payload)
        text[PROJECT_TEXT_SCHEMA_KEY] = 4
        restored = _restore()
        _assert_transform(restored.model_repository.display_transform_for_model_id("owner"), saved_owner)
        _assert_transform(restored.model_repository.companion_display_transform_for_model_id("owner"), saved_owner)


def test_standalone_obj_current_axes_roundtrip() -> None:
    """독립 OBJ의 현재 축만 변경해 왕복하며 최초 가져오기 축과 연결 없음 상태를 유지합니다."""

    from test_companion_persistence import _restore, _write_snapshot
    from test_project_restore import _run_import_until_terminal
    from tools.align.display_axes import create_model_display_transform
    from tools.align.mesh_importer import create_mesh_import_job
    from tools.align.state import RuntimeState

    with TemporaryDirectory() as directory:
        source = Path(directory) / "standalone.obj"
        source.write_text("v 0 0 0\nv 2 0 0\nv 0 3 1\nf 1 2 3\n", encoding="utf-8")
        origin = RuntimeState()
        job = create_mesh_import_job(
            source, axis_preset="BLENDER", axis_forward="Y", axis_up="Z",
            model_id_factory=lambda: "mesh", prepare_gpu=False,
        )
        assert _run_import_until_terminal(job, origin) == "FINISHED", job.error_message
        metadata = origin.model_repository.source_metadata_for_model_id("mesh")
        transform = create_model_display_transform("CUSTOM", "-X", "Y", rotation_xyz_degrees=(13.25, -24.0, 90.0))
        origin.model_repository.set_display_transform("mesh", transform)
        snapshot, _text = _write_snapshot(origin)
        assert snapshot.models[0].companion_axes is None
        assert snapshot.models[0].companion_source is None
        restored = _restore()
        _assert_transform(restored.model_repository.display_transform_for_model_id("mesh"), transform)
        assert restored.model_repository.source_metadata_for_model_id("mesh") == metadata
        assert restored.model_repository.companion_display_transforms == {}


def test_invalid_rotation_and_axis_structure_reject_before_restore() -> None:
    """잘못된 회전값과 형상별 축 구조를 활성 Text·현재 상태 변경 없이 거부합니다."""

    from test_companion_persistence import _dataset, _origin, _write_snapshot
    from tools.align.project_persistence import ProjectPersistenceCodecError, decode_project_state, encode_project_state
    from tools.align.project_persistence.runtime import schedule_project_restore_if_available
    from tools.align.state import RuntimeState

    with TemporaryDirectory() as directory:
        root = Path(directory) / "dataset"
        _dataset(root)
        origin = _origin(root)
        snapshot, text = _write_snapshot(origin)
        good_payload = text.as_string()
        for field in ("current_axes", "companion_axes"):
            for rotation in (None, [], [0.0, 0.0], [0.0, True, 0.0], [0.0, "3", 0.0], [0.0, float("inf"), 0.0]):
                document = json.loads(good_payload)
                document["models"][0][field]["rotation_xyz_degrees"] = rotation
                payload = _payload(document)
                text.clear()
                text.write(payload)
                target = RuntimeState()
                previous = (target.model_repository, target.model_catalog, target.axis_settings)
                assert schedule_project_restore_if_available(target) is False
                assert target.project_restore.phase == "FAILED"
                assert (target.model_repository, target.model_catalog, target.axis_settings) == previous
                assert target.project_restore.staging_runtime is None
                assert text.as_string() == payload
        model = snapshot.models[0]
        invalid_models = (
            replace(model, current_axes=None),
            replace(model, companion_axes=None),
            replace(model, companion_source=None),
            replace(model, current_axes=replace(model.current_axes, rotation_xyz_degrees=None)),
        )
        for invalid in invalid_models:
            try:
                encode_project_state(replace(snapshot, models=(invalid,)))
            except ProjectPersistenceCodecError:
                pass
            else:
                raise AssertionError("손상된 현재 축 snapshot을 수용했습니다.")
        for field in ("current_axes", "companion_axes"):
            document = json.loads(good_payload)
            document["models"][0].pop(field)
            try:
                decode_project_state(_payload(document))
            except ProjectPersistenceCodecError:
                pass
            else:
                raise AssertionError("필수 현재 축 필드가 없는 v5 문서를 수용했습니다.")


def test_transform_mismatch_preserves_existing_runtime() -> None:
    """최종 지문·축이 잘못된 복원을 중단하고 기존 모델·축·편집 객체를 보존합니다."""

    from test_companion_persistence import _dataset, _origin, _write_snapshot
    from test_project_restore import _drain_restore_timer
    from tools.align.project_persistence import runtime as persistence

    with TemporaryDirectory() as directory:
        root = Path(directory)
        _dataset(root / "existing")
        _dataset(root / "incoming")
        existing = _origin(root / "existing")
        _set_axes(existing)
        incoming = _origin(root / "incoming")
        snapshot, _text = _write_snapshot(incoming)
        original = snapshot.models[0]
        invalid_models = (
            replace(original, current_axes=replace(original.current_axes, display_transform_fingerprint="0" * 32)),
            replace(original, companion_axes=replace(original.companion_axes, display_transform_fingerprint="0" * 32)),
            replace(original, current_axes=replace(original.current_axes, axis_forward=original.current_axes.axis_up)),
            replace(original, companion_axes=replace(original.companion_axes, preset_id="잘못된 프리셋")),
        )
        fields = (
            "model_repository", "model_catalog", "marker_selection", "axis_settings",
            "point_clouds", "gpu_batches", "undo_journal",
        )
        previous = {field: getattr(existing, field) for field in fields}
        for invalid in invalid_models:
            state = existing.project_restore
            state.snapshot = replace(snapshot, models=(invalid,))
            state.project_uuid = snapshot.project_uuid
            state.generation += 1
            state.phase = "RESTORING"
            state.current_model_index = 0
            persistence._new_restore_staging(existing)
            staged_repository = state.staging_runtime.model_repository
            persistence._schedule_restore_timer(existing)
            _drain_restore_timer(existing)
            assert state.phase == "FAILED", state.last_error
            assert state.staging_runtime is None
            assert staged_repository.models == {}
            assert staged_repository.companion_display_transforms == {}
            assert all(getattr(existing, field) is value for field, value in previous.items())
            assert existing.import_job is None


def run_all() -> None:
    """형상별 축 저장·복원의 핵심 왕복·호환·원자성 회귀를 실행합니다."""

    _bootstrap()
    from test_project_restore import _remove_owned_project_texts

    try:
        test_independent_axes_roundtrip_preserves_source_and_prepares_final_axes()
        test_v4_companion_keeps_saved_owner_axes()
        test_standalone_obj_current_axes_roundtrip()
        test_invalid_rotation_and_axis_structure_reject_before_restore()
        test_transform_mismatch_preserves_existing_runtime()
        test_active_preview_saves_only_committed_axes_across_file_reopen()
    finally:
        _remove_owned_project_texts()
    print("test_geometry_axes_persistence: 6개 통과")


if __name__ == "__main__":
    run_all()
