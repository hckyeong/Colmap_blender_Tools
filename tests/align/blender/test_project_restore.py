"""저장된 COLMAP 작업 상태의 자동 복원·rollback·Relink를 검증합니다."""

from __future__ import annotations

from dataclasses import replace
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

import bpy


def _add_source_path() -> None:
    """애드온 소스 경로를 Blender 테스트 모듈 탐색 경로에 추가합니다."""

    source_path = Path(__file__).resolve().parents[3]
    if str(source_path) not in sys.path:
        sys.path.insert(0, str(source_path))


def _write_text_sparse_model(directory: Path, *, point_offset: float) -> None:
    """복원 경로가 실제로 읽을 최소 text COLMAP sparse model을 생성합니다."""

    directory.mkdir(parents=True, exist_ok=True)
    (directory / "cameras.txt").write_text(
        "# Camera list\n1 SIMPLE_PINHOLE 640 480 500 320 240\n",
        encoding="utf-8",
    )
    (directory / "images.txt").write_text(
        "# Image list\n"
        "11 1 0 0 0 0 0 0 1 image_001.jpg\n"
        "0.0 0.0 -1\n",
        encoding="utf-8",
    )
    (directory / "points3D.txt").write_text(
        "# 3D point list\n"
        f"101 {point_offset:.6f} 0.0 0.0 255 0 0 0.1\n"
        f"205 {point_offset + 2.0:.6f} 1.0 -3.0 0 128 255 0.2\n",
        encoding="utf-8",
    )


def _run_import_until_terminal(job, runtime) -> str:
    """테스트 Runtime에서 기존 단계형 ImportJob을 완료 또는 실패까지 진행합니다."""

    for _ in range(128):
        result = job.step(runtime)
        if result != "RUNNING":
            return result
    raise AssertionError("테스트 ImportJob이 제한 tick 안에 끝나지 않았습니다.")


def _drain_restore_timer(runtime) -> None:
    """Blender 이벤트 루프 대신 예약 callback을 직접 호출해 자동 복원을 끝까지 진행합니다."""

    callback = runtime.project_restore_timer
    if not callable(callback):
        raise AssertionError("자동 복원 timer가 예약되지 않았습니다.")
    try:
        for _ in range(512):
            if callback() is None:
                return
        raise AssertionError("프로젝트 자동 복원이 제한 tick 안에 끝나지 않았습니다.")
    finally:
        try:
            if bpy.app.timers.is_registered(callback):
                bpy.app.timers.unregister(callback)
        except (ReferenceError, RuntimeError, ValueError):
            pass


def _remove_owned_project_texts() -> None:
    """테스트가 기록한 애드온 소유 프로젝트 Text만 안전하게 정리합니다."""

    from tools.align.constants import PROJECT_TEXT_OWNER_KEY, PROJECT_TEXT_OWNER_VALUE

    for text in tuple(bpy.data.texts):
        if text.get(PROJECT_TEXT_OWNER_KEY) == PROJECT_TEXT_OWNER_VALUE:
            bpy.data.texts.remove(text)


def _origin_runtime(first_source: Path, second_source: Path):
    """A/P·확정 변환·provenance·완료 marker를 가진 실제 저장 Runtime을 구성합니다."""

    from tools.align.alignment.types import AlignmentProvenanceRecord, Sim3Transform
    from tools.align import importer
    from tools.align.importer import create_import_job
    from tools.align.state import (
        MarkerPair,
        MarkerSelectionSnapshot,
        RuntimeState,
        SelectedPoint,
    )

    runtime = RuntimeState()
    original_prepare_gpu = importer._prepare_gpu_batch_for_model
    try:
        importer._prepare_gpu_batch_for_model = lambda _model_id, _model: None
        first_job = create_import_job(
            first_source,
            input_format="TEXT",
            model_id_factory=lambda: "model-a",
            display_name_hint="anchor",
        )
        second_job = create_import_job(
            second_source,
            input_format="TEXT",
            axis_preset="CUSTOM",
            axis_forward="X",
            axis_up="Z",
            display_point_limit=10,
            model_id_factory=lambda: "model-p",
            display_name_hint="moving",
        )
        assert _run_import_until_terminal(first_job, runtime) == "FINISHED"
        assert _run_import_until_terminal(second_job, runtime) == "FINISHED"
    finally:
        importer._prepare_gpu_batch_for_model = original_prepare_gpu

    runtime.model_catalog.set_model_visibility("model-a", False)
    runtime.model_repository.set_alignment_transform(
        "model-p",
        Sim3Transform(
            matrix_float64=(
                (1.0, 0.0, 0.0, 5.0),
                (0.0, 1.0, 0.0, -2.0),
                (0.0, 0.0, 1.0, 3.0),
                (0.0, 0.0, 0.0, 1.0),
            )
        ),
    )
    runtime.model_catalog.set_alignment_provenance(
        "model-p",
        AlignmentProvenanceRecord(
            model_id="model-p",
            method="MARKER",
            marker_rms=0.01,
            marker_maximum_error=0.02,
        ),
    )
    runtime.marker_selection.restore_snapshot(
        MarkerSelectionSnapshot(
            phase="IDLE",
            next_pair_id=2,
            active_pair_id=1,
            completed_pairs=(
                MarkerPair(
                    pair_id=1,
                    reference=SelectedPoint(
                        model_role="REFERENCE",
                        model_id="model-a",
                        point3d_id=101,
                        local_position=(0.0, 0.0, 0.0),
                    ),
                    moving=SelectedPoint(
                        model_role="MOVING",
                        model_id="model-p",
                        point3d_id=101,
                        local_position=(10.0, 0.0, 0.0),
                    ),
                ),
            ),
            draft=None,
            message="세션 전용 문구는 저장하지 않습니다.",
        )
    )
    return runtime


def test_multi_model_restore_applies_only_completed_state() -> None:
    """두 실제 모델을 안정 ID·A/P·변환·marker까지 모두 복원하는지 확인합니다."""

    from tools.align import importer
    from tools.align.project_persistence.runtime import (
        build_project_state_snapshot,
        schedule_project_restore_if_available,
    )
    from tools.align.project_persistence.store import write_active_project_state
    from tools.align.state import RuntimeState

    _remove_owned_project_texts()
    with TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        first_source = root / "anchor" / "sparse"
        second_source = root / "moving" / "sparse"
        _write_text_sparse_model(first_source, point_offset=0.0)
        _write_text_sparse_model(second_source, point_offset=10.0)
        origin = _origin_runtime(first_source, second_source)
        snapshot = build_project_state_snapshot(origin)
        write_active_project_state(snapshot)

        restored = RuntimeState()
        original_prepare_gpu = importer._prepare_gpu_batch_for_model
        try:
            importer._prepare_gpu_batch_for_model = lambda _model_id, _model: None
            assert schedule_project_restore_if_available(restored) is True
            _drain_restore_timer(restored)
        finally:
            importer._prepare_gpu_batch_for_model = original_prepare_gpu

    assert restored.project_restore.phase == "IDLE"
    assert tuple(restored.model_repository.models) == ("model-a", "model-p")
    assert tuple(restored.model_catalog.model_order) == ("model-a", "model-p")
    assert restored.model_catalog.models["model-a"].is_anchor is True
    assert restored.model_catalog.models["model-a"].is_visible is False
    assert restored.model_catalog.models["model-p"].registration_status == "PENDING"
    assert restored.model_catalog.models["model-p"].alignment_provenance.method == "MARKER"
    transform = restored.model_repository.alignment_transform_for_model_id("model-p")
    assert tuple(float(value) for value in transform.translation_float64) == (5.0, -2.0, 3.0)
    assert restored.model_repository.source_metadata_for_model_id("model-p").display_point_limit == 10
    assert restored.marker_selection.phase == "IDLE"
    assert restored.marker_selection.draft is None
    assert restored.marker_selection.active_pair_id == 1
    restored_pair = restored.marker_selection.completed_pairs[0]
    assert restored_pair.reference.local_position == (0.0, 0.0, 0.0)
    assert restored_pair.moving.local_position == (10.0, 0.0, 0.0)
    assert restored.import_job is None
    assert restored.undo_journal.can_undo is False
    assert restored.project_persistence_dirty is False
    _remove_owned_project_texts()


def test_second_model_fingerprint_failure_rolls_back_entire_restore() -> None:
    """두 번째 원본의 지문 불일치가 첫 모델까지 남기지 않고 전체 rollback하는지 확인합니다."""

    from tools.align import importer
    from tools.align.project_persistence.runtime import (
        build_project_state_snapshot,
        schedule_project_restore_if_available,
    )
    from tools.align.project_persistence.store import write_active_project_state
    from tools.align.state import RuntimeState

    _remove_owned_project_texts()
    with TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        first_source = root / "anchor" / "sparse"
        second_source = root / "moving" / "sparse"
        _write_text_sparse_model(first_source, point_offset=0.0)
        _write_text_sparse_model(second_source, point_offset=10.0)
        origin = _origin_runtime(first_source, second_source)
        original_snapshot = build_project_state_snapshot(origin)
        changed_second_source = replace(
            original_snapshot.models[1].source,
            source_fingerprint="0" * 64,
        )
        broken_snapshot = replace(
            original_snapshot,
            models=(
                original_snapshot.models[0],
                replace(original_snapshot.models[1], source=changed_second_source),
            ),
        )
        write_active_project_state(broken_snapshot)

        restored = RuntimeState()
        original_prepare_gpu = importer._prepare_gpu_batch_for_model
        try:
            importer._prepare_gpu_batch_for_model = lambda _model_id, _model: None
            assert schedule_project_restore_if_available(restored) is True
            _drain_restore_timer(restored)
        finally:
            importer._prepare_gpu_batch_for_model = original_prepare_gpu

    assert restored.project_restore.phase == "NEEDS_RELINK"
    assert restored.project_restore.issues[0].model_id == "model-p"
    assert restored.model_repository.models == {}
    assert restored.model_repository.source_metadata == {}
    assert restored.model_catalog.models == {}
    assert restored.marker_selection.completed_pairs == []
    assert restored.point_clouds == {}
    assert restored.gpu_batches == {}
    _remove_owned_project_texts()


def test_missing_source_relink_and_stale_timer_are_safe() -> None:
    """누락 source의 모델별 Relink와 파일 교체 뒤 stale callback 무시를 함께 검증합니다."""

    from tools.align import importer
    from tools.align.project_persistence.runtime import (
        build_project_state_snapshot,
        cancel_project_restore_for_file_load,
        relink_project_source,
        retry_project_restore,
        schedule_project_restore_if_available,
        sync_project_state_if_dirty,
    )
    from tools.align.project_persistence.store import write_active_project_state
    from tools.align.model_repository import canonical_source_path
    from tools.align.state import RuntimeState

    _remove_owned_project_texts()
    with TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        first_source = root / "anchor" / "sparse"
        second_source = root / "moving" / "sparse"
        _write_text_sparse_model(first_source, point_offset=0.0)
        _write_text_sparse_model(second_source, point_offset=10.0)
        origin = _origin_runtime(first_source, second_source)
        original_snapshot = build_project_state_snapshot(origin)
        missing_path = root / "missing" / "sparse"
        missing_second_source = replace(
            original_snapshot.models[1].source,
            sparse_path_absolute=str(missing_path),
            sparse_path_relative="",
        )
        missing_snapshot = replace(
            original_snapshot,
            models=(
                original_snapshot.models[0],
                replace(original_snapshot.models[1], source=missing_second_source),
            ),
        )
        write_active_project_state(missing_snapshot)

        restored = RuntimeState()
        assert schedule_project_restore_if_available(restored) is False
        assert restored.project_restore.phase == "NEEDS_RELINK"
        assert restored.project_restore.issues[0].model_id == "model-p"
        relink_project_source("model-p", str(second_source), restored)
        original_prepare_gpu = importer._prepare_gpu_batch_for_model
        try:
            importer._prepare_gpu_batch_for_model = lambda _model_id, _model: None
            assert retry_project_restore(restored) is True
            _drain_restore_timer(restored)
        finally:
            importer._prepare_gpu_batch_for_model = original_prepare_gpu

        assert restored.project_restore.phase == "IDLE"
        assert tuple(restored.model_repository.models) == ("model-a", "model-p")
        assert restored.project_persistence_dirty is True
        assert (
            restored.model_repository.source_metadata_for_model_id("model-p").selected_root_absolute
            == canonical_source_path(second_source)
        )
        assert sync_project_state_if_dirty(runtime=restored) is True

        stale_runtime = RuntimeState()
        assert schedule_project_restore_if_available(stale_runtime) is True
        stale_callback = stale_runtime.project_restore_timer
        cancel_project_restore_for_file_load(stale_runtime)
        assert stale_callback() is None
        assert stale_runtime.model_repository.models == {}
        assert stale_runtime.project_restore_timer is None
        try:
            if bpy.app.timers.is_registered(stale_callback):
                bpy.app.timers.unregister(stale_callback)
        except (ReferenceError, RuntimeError, ValueError):
            pass
    _remove_owned_project_texts()


def run_all() -> None:
    """자동 복원 성공·rollback·Relink와 stale 방지 회귀를 순서대로 실행합니다."""

    _add_source_path()
    test_multi_model_restore_applies_only_completed_state()
    test_second_model_fingerprint_failure_rolls_back_entire_restore()
    test_missing_source_relink_and_stale_timer_are_safe()
    print("test_project_restore: 통과")


if __name__ == "__main__":
    run_all()
