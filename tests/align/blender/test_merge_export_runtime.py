"""구조 통합 내보내기 Runtime·포함 규칙·입력 잠금을 검증합니다."""

from __future__ import annotations

from pathlib import Path
import sys


def _add_source_path() -> None:
    """애드온 소스 디렉터리를 테스트 모듈 탐색 경로에 추가합니다."""

    source_path = Path(__file__).resolve().parents[3]
    source_text = str(source_path)
    if source_text not in sys.path:
        sys.path.insert(0, source_text)


def test_active_group_inclusion_ignores_visibility_and_provenance() -> None:
    """A 그룹 소속만 포함하고 표시·감사 기록은 품질 판정에 쓰지 않는지 검증합니다."""

    from tools.align.alignment.types import AlignmentProvenanceRecord
    from tools.align.constants import DEMO_MODEL_1_ID, DEMO_MODEL_2_ID
    from tools.align.state import ModelCatalogState

    catalog = ModelCatalogState()
    catalog.reset_demo_catalog()
    assert catalog.set_model_visibility(DEMO_MODEL_1_ID, False) is True
    catalog.set_alignment_provenance(
        DEMO_MODEL_2_ID,
        AlignmentProvenanceRecord(model_id=DEMO_MODEL_2_ID, method="MANUAL"),
    )

    assert tuple(record.model_id for record in catalog.active_models()) == (
        DEMO_MODEL_1_ID,
        DEMO_MODEL_2_ID,
    )
    assert catalog.models[DEMO_MODEL_1_ID].is_visible is False
    assert catalog.models[DEMO_MODEL_2_ID].alignment_provenance is not None


def test_anchor_and_manual_provenance_are_snapshot_safe() -> None:
    """첫 Anchor와 수동 A 편입 provenance가 카탈로그 snapshot에 남는지 검증합니다."""

    from tools.align.alignment.types import AlignmentProvenanceRecord
    from tools.align.constants import MODEL_ACTIVATION_SOURCE_MANUAL
    from tools.align.state import ModelCatalogState

    catalog = ModelCatalogState()
    anchor = catalog.add_imported_model("anchor", "Anchor", select_after_import=False)
    catalog.add_imported_model("moving", "Moving", select_after_import=True)
    assert anchor.alignment_provenance is not None
    assert anchor.alignment_provenance.method == "ANCHOR"
    assert catalog.promote_model_to_active(
        "moving",
        source=MODEL_ACTIVATION_SOURCE_MANUAL,
        has_marker_draft=False,
        completed_pair_count=0,
    ) is True
    catalog.set_alignment_provenance(
        "moving",
        AlignmentProvenanceRecord(model_id="moving", method="MANUAL"),
    )
    snapshot = catalog.snapshot()
    catalog.set_alignment_provenance(
        "moving",
        AlignmentProvenanceRecord(model_id="moving", method="UNKNOWN"),
    )
    catalog.restore_snapshot(snapshot)

    restored = catalog.models["moving"].alignment_provenance
    assert restored is not None and restored.method == "MANUAL"


def test_manual_group_promotion_provenance_survives_undo_redo() -> None:
    """수동 A 편입의 provenance가 그룹 전환 Undo/Redo와 함께 복원되는지 검증합니다."""

    from tools.align.constants import DEMO_MODEL_3_ID, MODEL_STATUS_REGISTERED
    from tools.align.operators.model_group import _commit_group_change
    from tools.align.state import RUNTIME

    class RecordingOperator:
        """UI 보고 호출만 받는 최소 오퍼레이터 대역입니다."""

        def report(self, _level: set[str], _message: str) -> None:
            """테스트에서는 상태줄 문구를 별도로 저장하지 않습니다."""

    catalog = RUNTIME.model_catalog
    RUNTIME.undo_journal.clear()
    RUNTIME.marker_selection.clear()
    RUNTIME.merge_export.clear()
    catalog.reset_demo_catalog()
    try:
        assert _commit_group_change(
            RecordingOperator(),
            DEMO_MODEL_3_ID,
            allow_demote=False,
        ) == {"FINISHED"}
        promoted = catalog.models[DEMO_MODEL_3_ID]
        assert promoted.registration_status == MODEL_STATUS_REGISTERED
        assert promoted.alignment_provenance is not None
        assert promoted.alignment_provenance.method == "MANUAL"

        assert RUNTIME.undo_journal.undo_committed(catalog) is True
        assert catalog.models[DEMO_MODEL_3_ID].alignment_provenance is None

        assert RUNTIME.undo_journal.redo_committed(catalog) is True
        restored = catalog.models[DEMO_MODEL_3_ID]
        assert restored.alignment_provenance is not None
        assert restored.alignment_provenance.method == "MANUAL"
    finally:
        RUNTIME.undo_journal.clear()
        RUNTIME.merge_export.clear()
        catalog.clear()


def test_runtime_state_marks_only_worker_phases_as_input_locked() -> None:
    """완료 결과가 아닌 실행 중 preflight·worker·finalize만 입력을 잠그는지 검증합니다."""

    from tools.align.constants import (
        MERGE_EXPORT_PHASE_CANCEL_PENDING,
        MERGE_EXPORT_PHASE_COMPLETED,
        MERGE_EXPORT_PHASE_EXPORT_RUNNING,
        MERGE_EXPORT_PHASE_PREFLIGHT_RUNNING,
        MERGE_EXPORT_PHASE_WAITING_FOR_FINALIZE,
    )
    from tools.align.state import MergeExportRuntimeState

    state = MergeExportRuntimeState()
    for phase in (
        MERGE_EXPORT_PHASE_PREFLIGHT_RUNNING,
        MERGE_EXPORT_PHASE_EXPORT_RUNNING,
        MERGE_EXPORT_PHASE_CANCEL_PENDING,
        MERGE_EXPORT_PHASE_WAITING_FOR_FINALIZE,
    ):
        state.phase = phase
        assert state.is_running is True
    state.phase = MERGE_EXPORT_PHASE_COMPLETED
    assert state.is_running is False


def test_point2d_offset_overflow_explains_cause_impact_and_remedy() -> None:
    """POINT2D offset 범위 초과가 사용자 조치가 있는 문구로 바뀌는지 검증합니다."""

    from tools.align.operators.export_model import format_merge_export_error

    cause, impact, remedy = format_merge_export_error(
        "통합 이미지의 POINT2D_IDX가 uint32 범위를 벗어납니다."
    )
    assert "특징점 수" in cause
    assert "offset" in impact
    assert "source 수" in remedy


def test_poll_exception_invalidates_worker_and_preserves_original_error() -> None:
    """poll 예외가 후보 정리 뒤 원래 오류 문구와 유휴 참조만 남기는지 검증합니다."""

    from tools.align.constants import MERGE_EXPORT_PHASE_EXPORT_RUNNING, MERGE_EXPORT_PHASE_FAILED
    from tools.align.operators.export_model import _poll_merge_export_job_timer
    from tools.align.state import RUNTIME

    class ExplodingBackend:
        """poll 예외와 invalidate 호출을 재현하는 작은 backend 대역입니다."""

        def __init__(self) -> None:
            """정리 요청을 확인할 빈 기록을 만듭니다."""

            self.invalidated_jobs: list[object] = []

        def poll(self, _job: object) -> object:
            """원래 poll 오류를 의도적으로 발생시킵니다."""

            raise RuntimeError("예상한 poll 오류")

        def invalidate(self, job: object) -> None:
            """정리 요청을 기록한 뒤 별도 정리 오류도 재현합니다."""

            self.invalidated_jobs.append(job)
            raise RuntimeError("예상한 정리 오류")

    state = RUNTIME.merge_export
    state.clear()
    backend = ExplodingBackend()
    job = object()
    state.backend = backend
    state.job = job
    state.phase = MERGE_EXPORT_PHASE_EXPORT_RUNNING
    try:
        assert _poll_merge_export_job_timer() is None
        assert backend.invalidated_jobs == [job]
        assert state.backend is None
        assert state.job is None
        assert state.phase == MERGE_EXPORT_PHASE_FAILED
        assert state.last_error == "예상한 poll 오류"
        assert state.is_running is False
    finally:
        state.clear()
        RUNTIME.clear_merge_export_timer()


def run_all() -> None:
    """Blender background runner가 호출할 구조 통합 Runtime 검증 묶음을 실행합니다."""

    _add_source_path()
    test_active_group_inclusion_ignores_visibility_and_provenance()
    test_anchor_and_manual_provenance_are_snapshot_safe()
    test_manual_group_promotion_provenance_survives_undo_redo()
    test_runtime_state_marks_only_worker_phases_as_input_locked()
    test_point2d_offset_overflow_explains_cause_impact_and_remedy()
    test_poll_exception_invalidates_worker_and_preserves_original_error()
    print("test_merge_export_runtime: 통과")


if __name__ == "__main__":
    run_all()
