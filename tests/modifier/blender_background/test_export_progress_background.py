from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import bpy


PACKAGE_PARENT = Path(__file__).resolve().parents[3]
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))

from tools import modifier as blender_colmap
from tools.modifier.blender import operators_export_progress
from tools.modifier.blender.export_progress_session import (
    active_export_progress_session,
    clear_export_progress_sessions,
    create_export_progress_session,
    export_progress_session_count,
)
from tools.modifier.blender.operators_export_progress import (
    COLMAP_OT_export_progress,
)
from tools.modifier.blender.progress_overlay import (
    MAXIMUM_DETAIL_LINES,
    OUTER_BORDER_CORNER_RADIUS,
    OVERLAY_CORNER_RADIUS,
    PROGRESS_CORNER_RADIUS,
    ProgressOverlayView,
    compact_path,
    rounded_rectangle_vertices,
)
from tools.modifier.blender.operators_camera_edit_mode import (
    COLMAP_OT_toggle_edit_camera_mode,
)
from tools.modifier.blender.operators_camera_review import (
    COLMAP_OT_review_apply_pending_filters,
    COLMAP_OT_review_filter_current,
)
from tools.modifier.blender.operators_import import COLMAP_OT_import
from tools.modifier.blender.operators_point_edit_mode import (
    COLMAP_OT_toggle_edit_point_mode,
)
from tools.modifier.blender.operators_point_selection import (
    COLMAP_OT_remove,
    COLMAP_OT_reset_edits,
    COLMAP_OT_select_observing_cameras,
    COLMAP_OT_undo_last_edit,
)
from tools.modifier.blender.runtime_state import (
    DatasetRuntimeState,
    install_state,
    validated_runtime_token,
)
from tools.modifier.core.edit_history import serialize_history
from tools.modifier.core.edit_journal import EditJournal, EditJournalCheckpoint
from tools.modifier.core.edit_replay import history_hash
from tools.modifier.core.edit_types import (
    EDIT_SCHEMA_VERSION,
    REMOVE_POINTS_OPERATION,
    EditOperation,
)
from tools.modifier.core.export_progress import (
    ExportPhase,
    ExportProgressSnapshot,
)
from tools.modifier.core.model import Reconstruction, SourceDescriptor


def assert_true(condition, message):
    if not condition:
        raise AssertionError(message)


def _snapshot(phase, fraction):
    return ExportProgressSnapshot(
        phase=phase,
        overall_fraction=fraction,
        phase_fraction=fraction,
        completed_units=int(fraction * 10),
        total_units=10,
        message=phase.name,
        elapsed_seconds=0.0,
        can_cancel=False,
    )


class _FakeJob:
    def __init__(self, *, fail=False):
        self.done = False
        self.failed = False
        self.error = None
        self.progress = _snapshot(ExportPhase.CREATED, 0.0)
        self.result = SimpleNamespace(backup_snapshot=None)
        self.advance_count = 0
        self.abort_count = 0
        self._fail = fail

    def advance(self, work_budget_ms=None):
        assert_true(
            work_budget_ms == 25.0,
            "Modal Operator가 정해진 Core 작업 budget을 전달하지 않았습니다.",
        )
        self.advance_count += 1
        if self._fail:
            self.failed = True
            self.error = RuntimeError("의도한 Export 실패")
            self.progress = _snapshot(ExportPhase.FAILED, 0.2)
            raise self.error
        if self.advance_count == 1:
            self.progress = _snapshot(ExportPhase.WRITE_STAGING, 0.2)
        else:
            self.done = True
            self.progress = _snapshot(ExportPhase.DONE, 1.0)
        return self.progress

    def abort(self):
        self.abort_count += 1


class _Event:
    """Blender 5.2 Event 계약처럼 제품 코드에 필요한 type만 제공합니다."""

    __slots__ = ("type",)

    def __init__(self, event_type):
        self.type = event_type


class _FakeWindowManager:
    def __init__(self):
        self.timer = object()
        self.added_timers = []
        self.removed_timers = []
        self.modal_handlers = []
        self.windows = ()

    def event_timer_add(self, interval, *, window):
        self.added_timers.append((interval, window))
        return self.timer

    def event_timer_remove(self, timer):
        self.removed_timers.append(timer)

    def modal_handler_add(self, operator):
        self.modal_handlers.append(operator)


class _FakeOperator:
    def __init__(self, job_id):
        self.job_id = job_id
        self.reports = []

    def report(self, levels, message):
        self.reports.append((set(levels), message))

    def _fail(self, context, session, exc):
        return COLMAP_OT_export_progress._fail(self, context, session, exc)

    def _finish(self, context, session):
        return COLMAP_OT_export_progress._finish(self, context, session)


def _session_for(fake_job):
    state = SimpleNamespace(
        dataset_uuid="progress-test",
        current_reconstruction=object(),
    )
    plan = SimpleNamespace(
        source=SimpleNamespace(model_directory=Path("C:/dataset/sparse/0")),
        backup_enabled=True,
        runtime_token=object(),
    )
    with patch(
        "tools.modifier.core.export_job.ExportTransactionJob",
        return_value=fake_job,
    ):
        return create_export_progress_session(
            bpy.context.scene,
            state,
            plan,
        )


def _operator_context():
    window_manager = _FakeWindowManager()
    return (
        SimpleNamespace(
            scene=bpy.context.scene,
            window=object(),
            window_manager=window_manager,
        ),
        window_manager,
    )


def _mutation_poll_results(context):
    return {
        "Import": COLMAP_OT_import.poll(context),
        "Edit Point": COLMAP_OT_toggle_edit_point_mode.poll(context),
        "Edit Camera": COLMAP_OT_toggle_edit_camera_mode.poll(context),
        "Select Observing": COLMAP_OT_select_observing_cameras.poll(context),
        "Remove": COLMAP_OT_remove.poll(context),
        "Undo": COLMAP_OT_undo_last_edit.poll(context),
        "Reset": COLMAP_OT_reset_edits.poll(context),
        "Camera Filter": COLMAP_OT_review_filter_current.poll(context),
        "Apply Pending": COLMAP_OT_review_apply_pending_filters.poll(context),
    }


blender_colmap.register()
try:
    assert_true(
        OVERLAY_CORNER_RADIUS == 8.0
        and OUTER_BORDER_CORNER_RADIUS == 10.0
        and PROGRESS_CORNER_RADIUS == 6.0,
        "Export Overlay 외곽 반지름이 8px 계약과 다릅니다.",
    )
    rounded_vertices = rounded_rectangle_vertices(
        10.0,
        20.0,
        100.0,
        40.0,
        12.0,
        corner_segments=4,
    )
    assert_true(
        len(rounded_vertices) == 22,
        "라운드 사각형의 중심·외곽 정점 수가 예상과 다릅니다.",
    )
    assert_true(
        rounded_vertices[0] == (60.0, 40.0)
        and rounded_vertices[1] == rounded_vertices[-1],
        "라운드 사각형의 중심 또는 TRI_FAN 폐합점이 올바르지 않습니다.",
    )
    assert_true(
        all(
            10.0 - 1e-9 <= vertex[0] <= 110.0 + 1e-9
            and 20.0 - 1e-9 <= vertex[1] <= 60.0 + 1e-9
            for vertex in rounded_vertices
        ),
        "라운드 사각형 정점이 지정된 경계를 벗어났습니다.",
    )
    narrow_vertices = rounded_rectangle_vertices(
        0.0,
        0.0,
        4.0,
        20.0,
        6.0,
    )
    assert_true(
        narrow_vertices
        and all(
            -1e-9 <= vertex[0] <= 4.0 + 1e-9
            and -1e-9 <= vertex[1] <= 20.0 + 1e-9
            for vertex in narrow_vertices
        ),
        "폭이 좁은 진행률 표시에서 반지름 제한이 적용되지 않았습니다.",
    )
    valid_view = ProgressOverlayView(
        title="COLMAP Export",
        phase="Writing staging files",
        fraction=0.25,
        completed_units=1,
        total_units=4,
        elapsed_seconds=0.5,
        detail_lines=(
            "Processed: 1 / 4    Elapsed: 0.5s",
            "Target: C:/dataset/sparse/0",
            "Backup: Enabled",
        ),
    )
    assert_true(
        valid_view.fraction == 0.25
        and len(valid_view.detail_lines) == MAXIMUM_DETAIL_LINES,
        "공통 Overlay View가 유효한 불변 입력을 보존하지 못했습니다.",
    )
    invalid_view_inputs = (
        {"fraction": float("nan")},
        {"fraction": -0.1},
        {"fraction": 1.1},
        {"completed_units": -1},
        {"total_units": -1},
        {"completed_units": 5},
        {"elapsed_seconds": float("inf")},
        {"title": "COLMAP 내보내기"},
        {"phase": "내보내기 준비"},
        {"detail_lines": ("1", "2", "3", "4")},
        {"detail_lines": ["mutable"]},
    )
    for replacements in invalid_view_inputs:
        arguments = {
            "title": valid_view.title,
            "phase": valid_view.phase,
            "fraction": valid_view.fraction,
            "completed_units": valid_view.completed_units,
            "total_units": valid_view.total_units,
            "elapsed_seconds": valid_view.elapsed_seconds,
            "detail_lines": valid_view.detail_lines,
        }
        arguments.update(replacements)
        try:
            ProgressOverlayView(**arguments)
        except ValueError:
            pass
        else:
            raise AssertionError(
                f"잘못된 공통 Overlay View를 허용했습니다: {replacements}"
            )
    assert_true(
        compact_path(Path("C:/dataset/sparse/0"), maximum=66)
        == "C:\\dataset\\sparse\\0"
        or compact_path(Path("C:/dataset/sparse/0"), maximum=66)
        == "C:/dataset/sparse/0",
        "짧은 경로를 불필요하게 축약했습니다.",
    )
    long_path = "C:/" + "long_directory/" * 10 + "sparse/0"
    compacted = compact_path(long_path, maximum=24)
    assert_true(
        len(compacted) == 24
        and compacted.startswith("…")
        and compacted.endswith("sparse/0"),
        "긴 경로가 뒤쪽을 보존하며 축약되지 않았습니다.",
    )

    settings = bpy.context.scene.colmap_import
    settings.dataset_root = "C:/guard_dataset"
    settings.dataset_uuid = "guard-dataset"
    settings.active_model_directory = "C:/guard_dataset/sparse/0"
    settings.edit_revision = 1
    settings.review_total_count = 1
    settings.review_current_index = 0
    settings.review_pending_filter_status = "READY"
    settings.review_pending_camera_count = 1
    baseline_polls = _mutation_poll_results(bpy.context)
    assert_true(
        not baseline_polls["Undo"]
        and all(
            available
            for name, available in baseline_polls.items()
            if name != "Undo"
        ),
        f"Export 비활성 기준 poll이 예상과 다릅니다: {baseline_polls}",
    )

    # 정상 작업은 첫 TIMER 전 실행되지 않고 두 tick 뒤 성공 정리되어야 합니다.
    success_job = _FakeJob()
    success_session = _session_for(success_job)
    export_view = operators_export_progress._export_progress_overlay_view(
        success_session,
    )
    assert_true(
        export_view.title == "COLMAP Export"
        and export_view.phase == "Preparing export"
        and export_view.fraction == 0.0
        and export_view.detail_lines[1]
        == "Target: C:\\dataset\\sparse\\0"
        and export_view.detail_lines[2].startswith("Backup: Enabled"),
        "Export Snapshot이 공통 Overlay View로 정확히 변환되지 않았습니다.",
    )
    settings.export_running = True
    blocked_polls = _mutation_poll_results(bpy.context)
    assert_true(
        not any(blocked_polls.values()),
        f"Export 중 상태 변경 Operator가 활성 상태입니다: {blocked_polls}",
    )
    try:
        _session_for(_FakeJob())
    except RuntimeError:
        pass
    else:
        raise AssertionError("활성 Export가 있는데 중복 Job 등록을 허용했습니다.")

    success_context, success_wm = _operator_context()
    success_operator = _FakeOperator(success_session.job_id)
    assert_true(
        COLMAP_OT_export_progress.invoke(
            success_operator,
            success_context,
            None,
        )
        == {"RUNNING_MODAL"},
        "Export 진행 Operator가 Modal 상태로 시작되지 않았습니다.",
    )
    assert_true(
        success_job.advance_count == 0,
        "진행 UI를 그릴 첫 기회 전에 Core Job을 실행했습니다.",
    )
    for event_type in ("ESC", "RIGHTMOUSE", "LEFTMOUSE"):
        assert_true(
            COLMAP_OT_export_progress.modal(
                success_operator,
                success_context,
                _Event(event_type),
            )
            == {"RUNNING_MODAL"}
            and success_job.advance_count == 0,
            f"{event_type} 입력이 Export를 중단하거나 Core Job을 진행했습니다.",
        )
    with (
        patch.object(
            operators_export_progress,
            "assert_export_runtime_token",
            return_value=success_session.state,
        ),
        patch.object(
            operators_export_progress,
            "finish_export_success",
            return_value=(),
        ),
    ):
        assert_true(
            COLMAP_OT_export_progress.modal(
                success_operator,
                success_context,
                _Event("TIMER"),
            )
            == {"RUNNING_MODAL"},
            "첫 Core 단계 뒤 Modal 실행이 유지되지 않았습니다.",
        )
        assert_true(
            COLMAP_OT_export_progress.modal(
                success_operator,
                success_context,
                _Event("TIMER"),
            )
            == {"RUNNING_MODAL"},
            "Runtime 반영 뒤 100% 화면을 그릴 기회를 유지하지 않았습니다.",
        )
        assert_true(
            success_session.runtime_committed
            and success_session.completion_result is success_job.result,
            "Core 완료 뒤 Blender Runtime 완료 상태를 세션에 기록하지 않았습니다.",
        )
        assert_true(
            COLMAP_OT_export_progress.modal(
                success_operator,
                success_context,
                _Event("TIMER"),
            )
            == {"FINISHED"},
            "100% 화면 표시 뒤 성공 상태로 종료되지 않았습니다.",
        )
    assert_true(
        export_progress_session_count() == 0
        and active_export_progress_session() is None
        and not settings.export_running,
        "성공 뒤 Registry 또는 export_running 상태가 남았습니다.",
    )
    assert_true(
        success_wm.removed_timers == [success_wm.timer],
        "성공 뒤 Event Timer가 정확히 한 번 제거되지 않았습니다.",
    )
    assert_true(
        _mutation_poll_results(bpy.context) == baseline_polls,
        "Export 성공 뒤 상태 변경 Operator poll이 복원되지 않았습니다.",
    )

    # Core 예외는 실패 Status를 남긴 뒤 같은 수명주기 경계로 정리해야 합니다.
    failure_job = _FakeJob(fail=True)
    failure_session = _session_for(failure_job)
    settings.export_running = True
    assert_true(
        not any(_mutation_poll_results(bpy.context).values()),
        "실패 시나리오의 Export 중 상태 변경 Operator가 활성 상태입니다.",
    )
    failure_context, failure_wm = _operator_context()
    failure_operator = _FakeOperator(failure_session.job_id)
    assert_true(
        COLMAP_OT_export_progress.invoke(
            failure_operator,
            failure_context,
            None,
        )
        == {"RUNNING_MODAL"},
        "실패 시나리오의 Modal Operator를 시작하지 못했습니다.",
    )
    with patch.object(
        operators_export_progress,
        "assert_export_runtime_token",
        return_value=failure_session.state,
    ):
        assert_true(
            COLMAP_OT_export_progress.modal(
                failure_operator,
                failure_context,
                _Event("TIMER"),
            )
            == {"CANCELLED"},
            "Core 예외가 Modal 실패 상태로 전환되지 않았습니다.",
        )
    assert_true(
        export_progress_session_count() == 0
        and not settings.export_running
        and settings.export_status.startswith("Export failed:"),
        "실패 뒤 Registry, 실행 잠금 또는 Status가 정리되지 않았습니다.",
    )
    assert_true(
        failure_wm.removed_timers == [failure_wm.timer],
        "실패 뒤 Event Timer가 정확히 한 번 제거되지 않았습니다.",
    )
    assert_true(
        _mutation_poll_results(bpy.context) == baseline_polls,
        "Export 실패 뒤 상태 변경 Operator poll이 복원되지 않았습니다.",
    )

    # Old Job 자체 입력은 그대로여도 새 Runtime 설치를 tick 경계에서 막습니다.
    source = SourceDescriptor(
        dataset_root=Path("C:/guard_dataset"),
        sparse_root=Path("C:/guard_dataset/sparse"),
        model_directory=Path("C:/guard_dataset/sparse/0"),
        images_directory=Path("C:/guard_dataset/images"),
        input_format="text",
        layout="legacy",
        selected_files=(),
        source_fingerprint="a" * 64,
        dataset_uuid="8a04913d-e149-4539-965f-8d486217d2d2",
        schema_version=1,
    )
    operation = EditOperation(
        schema_version=EDIT_SCHEMA_VERSION,
        operation_id="guard-operation",
        operation_type=REMOVE_POINTS_OPERATION,
        dataset_uuid=source.dataset_uuid,
        source_fingerprint=source.source_fingerprint,
        requested_point3d_ids=(1,),
        requested_image_ids=(),
        removed_point3d_ids=(1,),
        removed_image_ids=(),
        removed_camera_ids=(),
        removed_frame_ids=(),
        removed_rig_ids=(),
        previous_revision=0,
        resulting_revision=1,
        created_at_utc="2026-07-26T00:00:00Z",
    )
    old_reconstruction = Reconstruction()
    old_operations = (operation,)
    old_runtime = DatasetRuntimeState(
        dataset_uuid=source.dataset_uuid,
        source=source,
        baseline_reconstruction=old_reconstruction,
        current_reconstruction=old_reconstruction,
        indexes=SimpleNamespace(),
        operations=old_operations,
        edit_revision=1,
        index_revision=1,
        history_hash=history_hash(old_operations),
        point_id_order=(),
        image_path_manifest=None,
    )
    old_journal = EditJournal(source.dataset_uuid, source.source_fingerprint)
    old_journal.restore_checkpoint(
        EditJournalCheckpoint(
            revision=1,
            history=old_operations,
            undo_stack=(),
            redo_stack=(),
        )
    )
    install_state(
        old_runtime,
        scene=bpy.context.scene,
        journal=old_journal,
    )
    settings.dataset_uuid = source.dataset_uuid
    settings.source_fingerprint = source.source_fingerprint
    settings.active_model_directory = str(source.model_directory)
    settings.active_input_format = source.input_format
    settings.active_layout = source.layout
    settings.edit_history_json = serialize_history(old_operations)
    settings.edit_revision = 1
    settings.history_hash = old_runtime.history_hash

    runtime_swap_job = _FakeJob()
    runtime_swap_plan = SimpleNamespace(
        source=SimpleNamespace(model_directory=source.model_directory),
        backup_enabled=True,
        runtime_token=validated_runtime_token(old_runtime),
    )
    with patch(
        "tools.modifier.core.export_job.ExportTransactionJob",
        return_value=runtime_swap_job,
    ):
        runtime_swap_session = create_export_progress_session(
            bpy.context.scene,
            old_runtime,
            runtime_swap_plan,
        )
    settings.export_running = True
    runtime_swap_context, runtime_swap_wm = _operator_context()
    runtime_swap_operator = _FakeOperator(runtime_swap_session.job_id)
    assert_true(
        COLMAP_OT_export_progress.invoke(
            runtime_swap_operator,
            runtime_swap_context,
            None,
        )
        == {"RUNNING_MODAL"},
        "Runtime 교체 회귀 시나리오의 Modal Operator를 시작하지 못했습니다.",
    )
    replacement_runtime = replace(
        old_runtime,
        baseline_reconstruction=Reconstruction(),
        current_reconstruction=Reconstruction(),
    )
    install_state(replacement_runtime, scene=bpy.context.scene)
    assert_true(
        COLMAP_OT_export_progress.modal(
            runtime_swap_operator,
            runtime_swap_context,
            _Event("TIMER"),
        )
        == {"CANCELLED"},
        "새 Runtime 설치를 감지한 Export가 실패 상태로 전환되지 않았습니다.",
    )
    assert_true(
        runtime_swap_job.advance_count == 0
        and runtime_swap_job.abort_count == 1
        and export_progress_session_count() == 0
        and not settings.export_running,
        "Runtime 교체를 Core advance 전에 차단하거나 abort 정리하지 못했습니다.",
    )
    assert_true(
        runtime_swap_wm.removed_timers == [runtime_swap_wm.timer],
        "Runtime 교체 실패 뒤 Event Timer가 남았습니다.",
    )

    # Extension 해제와 같은 강제 정리에서는 Core abort와 UI 정리를 모두 수행합니다.
    pending_job = _FakeJob()
    pending_session = _session_for(pending_job)
    settings.export_running = True
    pending_context, pending_wm = _operator_context()
    pending_operator = _FakeOperator(pending_session.job_id)
    assert_true(
        COLMAP_OT_export_progress.invoke(
            pending_operator,
            pending_context,
            None,
        )
        == {"RUNNING_MODAL"},
        "강제 정리 시나리오의 Modal Operator를 시작하지 못했습니다.",
    )
    clear_export_progress_sessions()
    assert_true(
        pending_job.abort_count == 1
        and export_progress_session_count() == 0
        and not settings.export_running,
        "강제 정리가 Core Job abort 또는 Registry 잠금을 정리하지 못했습니다.",
    )
    assert_true(
        pending_wm.removed_timers == [pending_wm.timer],
        "강제 정리 뒤 Event Timer가 남았습니다.",
    )
finally:
    clear_export_progress_sessions()
    blender_colmap.unregister()

print("BLENDER_COLMAP_BACKGROUND_EXPORT_PROGRESS_OK")
