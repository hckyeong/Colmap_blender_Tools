"""구조 통합 backend의 worker 수명주기·취소·최종 확정을 Blender에서 검사합니다."""

from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from typing import Any, Iterator

import numpy as np


def _add_source_path() -> None:
    """애드온 소스 디렉터리를 Blender Python 모듈 탐색 경로에 추가합니다."""

    source_path = Path(__file__).resolve().parents[3]
    source_text = str(source_path)
    if source_text not in sys.path:
        sys.path.insert(0, source_text)


class _FakeWorkerProcess:
    """실제 Blender 실행 없이 backend의 Popen 수명주기를 재현합니다."""

    def __init__(self, *, return_code: int | None = None) -> None:
        """원하는 실행·종료 상태와 호출 횟수를 초기화합니다."""

        self.return_code = return_code
        self.pid = 4321
        self.terminate_calls = 0
        self.kill_calls = 0

    def poll(self) -> int | None:
        """backend가 읽는 현재 종료 코드를 반환합니다."""

        return self.return_code

    def wait(self, timeout: float | None = None) -> int:
        """실행 중이면 Popen과 같이 timeout 예외를 내고 종료 코드를 반환합니다."""

        if self.return_code is None:
            raise subprocess.TimeoutExpired("fake-worker", timeout)
        return self.return_code

    def terminate(self) -> None:
        """기본 취소 경로에서는 worker가 즉시 종료된 것으로 표시합니다."""

        self.terminate_calls += 1
        self.return_code = -15

    def kill(self) -> None:
        """강제 종료 경로도 테스트에서 명시적으로 관찰할 수 있게 합니다."""

        self.kill_calls += 1
        self.return_code = -9


@contextmanager
def _patched_popen(process: _FakeWorkerProcess) -> Iterator[list[tuple[tuple[Any, ...], dict[str, Any]]]]:
    """backend 모듈의 Popen만 짧게 대역으로 교체하고 실행 인자를 보관합니다."""

    from tools.align.merge_export import backend as backend_module

    calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    original = backend_module.subprocess.Popen

    def launch(*arguments: Any, **keywords: Any) -> _FakeWorkerProcess:
        """실제 프로세스 대신 미리 준비한 worker handle을 반환합니다."""

        calls.append((arguments, keywords))
        return process

    backend_module.subprocess.Popen = launch
    try:
        yield calls
    finally:
        backend_module.subprocess.Popen = original


def _snapshot(
    root: Path,
    *,
    job_id: str,
    output_format: str = "BINARY",
    overwrite_existing_output: bool = False,
) -> object:
    """backend만 검증할 한 개 Anchor source snapshot을 만듭니다."""

    from tools.align.constants import (
        MERGE_EXPORT_ALGORITHM_VERSION,
        MERGE_EXPORT_PROTOCOL_VERSION,
    )
    from tools.align.merge_export.types import (
        MergeExportSnapshot,
        MergeOutputFormat,
        MergeSourceModelSnapshot,
    )

    source_root = root / "source"
    source_root.mkdir()
    source = MergeSourceModelSnapshot(
        model_id="anchor",
        display_name="Anchor",
        dataset_root=source_root,
        source_path=source_root,
        source_format="binary",
        catalog_load_order=0,
        geometry_revision=0,
        is_anchor=True,
        display_matrix_float64=np.identity(4, dtype=np.float64),
        display_fingerprint="display-fingerprint",
        alignment_matrix_float64=np.identity(4, dtype=np.float64),
        alignment_fingerprint="alignment-fingerprint",
        expected_source_fingerprint="0" * 64,
    )
    return MergeExportSnapshot(
        job_id=job_id,
        protocol_version=MERGE_EXPORT_PROTOCOL_VERSION,
        algorithm_version=MERGE_EXPORT_ALGORITHM_VERSION,
        output_root=root / "merged-colmap",
        anchor_model_id="anchor",
        source_models=(source,),
        runtime_fingerprint="runtime-fingerprint",
        output_format=MergeOutputFormat(output_format),
        overwrite_existing_output=overwrite_existing_output,
    )


def _backend() -> object:
    """대역 Popen과 함께 쓸 실제 파일 경로의 backend를 반환합니다."""

    from tools.align.merge_export.backend import MergeExportBackend

    return MergeExportBackend(blender_binary_path=Path(sys.executable))


def _issue(code: str) -> object:
    """worker 결과 파일에 넣을 최소 구조화 오류를 만듭니다."""

    from tools.align.merge_export.types import MergeIssue

    return MergeIssue(
        code=code,
        cause="테스트 원인",
        impact="테스트 영향",
        remedy="테스트 해결 방법",
    )


def _windows_progress_race_error(winerror: int) -> PermissionError:
    """Windows의 progress.json 접근 거부·공유 위반 오류를 재현합니다."""

    return PermissionError(None, "테스트용 progress 파일 경합", None, winerror)


def _write_worker_result(job: object, result: object, *, error: bool = False) -> None:
    """worker와 같은 원자 JSON 방식으로 result 또는 error 파일을 기록합니다."""

    from tools.align.merge_export import backend as backend_module

    target = job.error_path if error else job.result_path
    backend_module._write_json_atomically(target, result.to_mapping())


def _write_preflight(job: object, preflight: object) -> None:
    """worker가 남긴 preflight JSON을 원자 기록합니다."""

    from tools.align.merge_export import backend as backend_module

    backend_module._write_json_atomically(job.preflight_path, preflight.to_mapping())


def _write_worker_progress(job: object, progress: object) -> None:
    """backend의 마지막 정상 진행률 검사용 progress.json을 기록합니다."""

    from tools.align.merge_export import backend as backend_module

    backend_module._write_json_atomically(job.progress_path, progress.to_mapping())


def _create_complete_candidate(candidate_root: Path, *, output_format: str = "BINARY") -> None:
    """finalize 경계 검사용 최소 sparse·images·audit candidate를 만듭니다."""

    from tools.align.merge_export import backend as backend_module

    from tools.align.merge_export.types import MergeOutputFormat

    for relative_path in backend_module._required_candidate_files(MergeOutputFormat(output_format)):
        path = candidate_root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    (candidate_root / "images").mkdir(exist_ok=True)


def test_backend_starts_worker_with_strict_manifest_and_safe_command() -> None:
    """backend가 wrapper manifest와 shell=False Blender 명령을 고정하는지 확인합니다."""

    with tempfile.TemporaryDirectory(prefix="colmap_align_backend_") as temporary_directory:
        root = Path(temporary_directory).resolve()
        process = _FakeWorkerProcess()
        backend = _backend()
        with _patched_popen(process) as calls:
            job = backend.start(_snapshot(root, job_id="backend-start"))
            manifest = json.loads(job.manifest_path.read_text(encoding="utf-8"))
            assert set(manifest) == {"snapshot", "preflight_only"}
            assert manifest["preflight_only"] is False
            assert manifest["snapshot"]["candidate_root"] == str(job.candidate_root)
            assert manifest["snapshot"]["output_format"] == "BINARY"
            assert job.candidate_root.name == ".colmap_align_export_backend-start"
            assert len(calls) == 1
            command, keywords = calls[0]
            assert command[0][1:4] == ["--background", "--factory-startup", "--python"]
            assert command[0][-2:] == ["--job-dir", str(job.job_directory)]
            assert keywords["shell"] is False
            assert keywords["cwd"] == str(job.job_directory)
            assert keywords["stdin"] is subprocess.DEVNULL
            assert keywords["env"]["PYTHONNOUSERSITE"] == "1"
            backend.invalidate(job)
        assert not job.job_directory.exists()


def test_existing_output_requires_explicit_overwrite_approval() -> None:
    """기존 output은 snapshot의 명시 승인 없이는 worker 시작 전 거부되는지 확인합니다."""

    from tools.align.merge_export.backend import MergeExportBackendError

    with tempfile.TemporaryDirectory(prefix="colmap_align_backend_") as temporary_directory:
        root = Path(temporary_directory).resolve()
        snapshot = _snapshot(root, job_id="backend-overwrite-rejected")
        snapshot.output_root.mkdir()
        try:
            _backend().start(snapshot)
        except MergeExportBackendError as error:
            assert "승인" in str(error)
        else:
            raise AssertionError("사용자 승인 없는 기존 출력 덮어쓰기가 허용되었습니다.")


def test_overwrite_finalize_replaces_only_after_candidate_success() -> None:
    """승인한 기존 출력은 candidate 검증 뒤에만 교체되고 임시 백업이 제거되는지 확인합니다."""

    from tools.align.merge_export.types import MergeExportResult, MergePhase
    from tools.align.merge_export.worker import _safe_candidate_root

    with tempfile.TemporaryDirectory(prefix="colmap_align_backend_") as temporary_directory:
        root = Path(temporary_directory).resolve()
        snapshot = _snapshot(
            root,
            job_id="backend-overwrite-success",
            output_format="TEXT",
            overwrite_existing_output=True,
        )
        snapshot.output_root.mkdir()
        sentinel = snapshot.output_root / "old-result.txt"
        sentinel.write_text("기존 결과", encoding="utf-8")
        process = _FakeWorkerProcess()
        backend = _backend()
        with _patched_popen(process):
            job = backend.start(snapshot)
            assert sentinel.is_file()
            assert _safe_candidate_root(job.snapshot) == job.candidate_root
            _create_complete_candidate(job.candidate_root, output_format="TEXT")
            result = MergeExportResult(
                job_id=job.job_id,
                phase=MergePhase.WAITING_FOR_FINALIZE,
                runtime_fingerprint=job.snapshot.runtime_fingerprint,
                candidate_root=job.candidate_root,
                output_root=job.snapshot.output_root,
            )
            _write_worker_result(job, result)
            process.return_code = 0
            assert backend.poll(job).phase is MergePhase.WAITING_FOR_FINALIZE
            completed = backend.finalize(
                job,
                current_runtime_fingerprint=job.snapshot.runtime_fingerprint,
            )
            assert completed.phase is MergePhase.COMPLETED
            assert not sentinel.exists()
            assert (snapshot.output_root / "sparse" / "0" / "points3D.txt").is_file()
            assert not (root / f".colmap_align_previous_{job.job_id}").exists()


def test_overwrite_finalize_preserves_output_changed_during_worker() -> None:
    """사용자 확인 뒤 기존 폴더가 바뀌면 새 candidate를 폐기하고 현재 출력을 보존하는지 확인합니다."""

    from tools.align.merge_export.types import MergeExportResult, MergePhase

    with tempfile.TemporaryDirectory(prefix="colmap_align_backend_") as temporary_directory:
        root = Path(temporary_directory).resolve()
        snapshot = _snapshot(
            root,
            job_id="backend-overwrite-stale-output",
            overwrite_existing_output=True,
        )
        snapshot.output_root.mkdir()
        sentinel = snapshot.output_root / "old-result.txt"
        sentinel.write_text("기존 결과", encoding="utf-8")
        process = _FakeWorkerProcess()
        backend = _backend()
        with _patched_popen(process):
            job = backend.start(snapshot)
            _create_complete_candidate(job.candidate_root)
            result = MergeExportResult(
                job_id=job.job_id,
                phase=MergePhase.WAITING_FOR_FINALIZE,
                runtime_fingerprint=job.snapshot.runtime_fingerprint,
                candidate_root=job.candidate_root,
                output_root=job.snapshot.output_root,
            )
            _write_worker_result(job, result)
            sentinel.write_text("작업 중 변경된 기존 결과", encoding="utf-8")
            process.return_code = 0
            assert backend.poll(job).phase is MergePhase.WAITING_FOR_FINALIZE
            failed = backend.finalize(
                job,
                current_runtime_fingerprint=job.snapshot.runtime_fingerprint,
            )
            assert failed.phase is MergePhase.FAILED
            assert failed.error is not None and failed.error.code == "FINALIZE_FAILED"
            assert sentinel.read_text(encoding="utf-8") == "작업 중 변경된 기존 결과"
            assert not job.candidate_root.exists()


def test_overwrite_cancel_keeps_existing_output() -> None:
    """승인 후 worker를 취소해도 기존 정상 출력은 그대로 유지되는지 확인합니다."""

    from tools.align.merge_export.types import MergePhase

    with tempfile.TemporaryDirectory(prefix="colmap_align_backend_") as temporary_directory:
        root = Path(temporary_directory).resolve()
        snapshot = _snapshot(
            root,
            job_id="backend-overwrite-cancel",
            overwrite_existing_output=True,
        )
        snapshot.output_root.mkdir()
        sentinel = snapshot.output_root / "old-result.txt"
        sentinel.write_text("기존 결과", encoding="utf-8")
        process = _FakeWorkerProcess()
        backend = _backend()
        with _patched_popen(process):
            job = backend.start(snapshot)
            _create_complete_candidate(job.candidate_root)
            assert backend.cancel(job) is True
            process.return_code = -15
            cancelled = backend.poll(job)
            assert cancelled.phase is MergePhase.CANCELLED
            assert sentinel.read_text(encoding="utf-8") == "기존 결과"
            assert not job.candidate_root.exists()


def test_preflight_error_json_is_reported_as_not_ready_preflight() -> None:
    """preflight-only 오류는 FAILED 파일이어도 UI에 준비되지 않은 검사 결과로 보이는지 확인합니다."""

    from tools.align.merge_export.types import MergeExportPreflightResult, MergeExportResult, MergePhase

    with tempfile.TemporaryDirectory(prefix="colmap_align_backend_") as temporary_directory:
        root = Path(temporary_directory).resolve()
        process = _FakeWorkerProcess()
        backend = _backend()
        with _patched_popen(process):
            job = backend.start_preflight(_snapshot(root, job_id="backend-preflight-error"))
            issue = _issue("PREFLIGHT_INVALID")
            preflight = MergeExportPreflightResult(
                job_id=job.job_id,
                runtime_fingerprint=job.snapshot.runtime_fingerprint,
                included_model_ids=("anchor",),
                errors=(issue,),
            )
            result = MergeExportResult(
                job_id=job.job_id,
                phase=MergePhase.FAILED,
                runtime_fingerprint=job.snapshot.runtime_fingerprint,
                candidate_root=job.candidate_root,
                output_root=job.snapshot.output_root,
                error=issue,
            )
            _write_preflight(job, preflight)
            _write_worker_result(job, result, error=True)
            process.return_code = 1
            polled = backend.poll(job)
            assert polled.phase is MergePhase.PREFLIGHT_READY
            assert polled.preflight is not None and polled.preflight.is_ready is False
            assert polled.result is not None and polled.result.phase is MergePhase.FAILED
            assert polled.error is not None and polled.error.code == "PREFLIGHT_INVALID"
            backend.invalidate(job)


def test_cancelled_error_json_and_cancelled_exit_without_result_are_user_cancellation() -> None:
    """CANCELLED error.json과 강제 종료 뒤 무결과 상태를 모두 사용자 취소로 구분합니다."""

    from tools.align.merge_export.types import MergeExportResult, MergePhase

    with tempfile.TemporaryDirectory(prefix="colmap_align_backend_") as temporary_directory:
        root = Path(temporary_directory).resolve()
        process = _FakeWorkerProcess()
        backend = _backend()
        with _patched_popen(process):
            job = backend.start(_snapshot(root, job_id="backend-cancelled-error"))
            issue = _issue("EXPORT_CANCELLED")
            result = MergeExportResult(
                job_id=job.job_id,
                phase=MergePhase.CANCELLED,
                runtime_fingerprint=job.snapshot.runtime_fingerprint,
                candidate_root=job.candidate_root,
                output_root=job.snapshot.output_root,
                error=issue,
            )
            _write_worker_result(job, result, error=True)
            process.return_code = 0
            polled = backend.poll(job)
            assert polled.phase is MergePhase.CANCELLED
            assert polled.error is not None and polled.error.code == "EXPORT_CANCELLED"
            assert job.cleaned is True

    with tempfile.TemporaryDirectory(prefix="colmap_align_backend_") as temporary_directory:
        root = Path(temporary_directory).resolve()
        process = _FakeWorkerProcess()
        backend = _backend()
        with _patched_popen(process):
            job = backend.start(_snapshot(root, job_id="backend-cancelled-no-result"))
            _create_complete_candidate(job.candidate_root)
            assert backend.cancel(job) is True
            process.return_code = -15
            polled = backend.poll(job)
            assert polled.phase is MergePhase.CANCELLED
            assert polled.error is not None and polled.error.code == "CANCELLED_WITHOUT_RESULT"
            assert job.cleaned is True
            assert not job.candidate_root.exists()


def test_terminal_result_waits_for_worker_exit_and_finalize_never_promotes_early() -> None:
    """result.json이 먼저 보여도 종료 전에는 running이며 출력 승격을 막는지 확인합니다."""

    from tools.align.merge_export.backend import MergeExportBackendError
    from tools.align.merge_export.types import MergeExportResult, MergePhase

    with tempfile.TemporaryDirectory(prefix="colmap_align_backend_") as temporary_directory:
        root = Path(temporary_directory).resolve()
        process = _FakeWorkerProcess()
        backend = _backend()
        with _patched_popen(process):
            job = backend.start(_snapshot(root, job_id="backend-terminal-race"))
            _create_complete_candidate(job.candidate_root)
            result = MergeExportResult(
                job_id=job.job_id,
                phase=MergePhase.WAITING_FOR_FINALIZE,
                runtime_fingerprint=job.snapshot.runtime_fingerprint,
                candidate_root=job.candidate_root,
                output_root=job.snapshot.output_root,
                counts={"images": 1},
            )
            _write_worker_result(job, result)

            running = backend.poll(job)
            assert running.phase is MergePhase.EXPORT_RUNNING
            assert job.result is not None and job.result.phase is MergePhase.WAITING_FOR_FINALIZE
            early = backend.finalize(
                job,
                current_runtime_fingerprint=job.snapshot.runtime_fingerprint,
            )
            assert early.phase is MergePhase.FAILED
            assert early.error is not None and early.error.code == "WORKER_STILL_RUNNING"
            assert job.candidate_root.is_dir()
            assert not job.snapshot.output_root.exists()

            process.return_code = 0
            waiting = backend.poll(job)
            assert waiting.phase is MergePhase.WAITING_FOR_FINALIZE

            original_cleanup = backend._cleanup_terminal_job

            def failing_cleanup(_job: object, *, remove_candidate: bool) -> None:
                """승격 뒤 임시 정리 실패가 완료 결과를 바꾸지 않는지 재현합니다."""

                raise MergeExportBackendError("테스트용 cleanup 실패")

            backend._cleanup_terminal_job = failing_cleanup
            try:
                completed = backend.finalize(
                    job,
                    current_runtime_fingerprint=job.snapshot.runtime_fingerprint,
                )
            finally:
                backend._cleanup_terminal_job = original_cleanup
            assert completed.phase is MergePhase.COMPLETED
            assert completed.output_root.is_dir()
            assert not job.candidate_root.exists()
            backend.invalidate(job)


def test_stale_finalize_never_creates_output_and_removes_candidate() -> None:
    """Runtime fingerprint가 바뀌면 candidate만 폐기하고 STALE를 반환하는지 확인합니다."""

    from tools.align.merge_export.types import MergeExportResult, MergePhase

    with tempfile.TemporaryDirectory(prefix="colmap_align_backend_") as temporary_directory:
        root = Path(temporary_directory).resolve()
        process = _FakeWorkerProcess(return_code=0)
        backend = _backend()
        with _patched_popen(process):
            job = backend.start(_snapshot(root, job_id="backend-stale"))
            _create_complete_candidate(job.candidate_root)
            result = MergeExportResult(
                job_id=job.job_id,
                phase=MergePhase.WAITING_FOR_FINALIZE,
                runtime_fingerprint=job.snapshot.runtime_fingerprint,
                candidate_root=job.candidate_root,
                output_root=job.snapshot.output_root,
            )
            _write_worker_result(job, result)
            assert backend.poll(job).phase is MergePhase.WAITING_FOR_FINALIZE
            stale = backend.finalize(job, current_runtime_fingerprint="changed-runtime")
            assert stale.phase is MergePhase.STALE
            assert stale.error is not None and stale.error.code == "RUNTIME_STALE"
            assert not job.snapshot.output_root.exists()
            assert not job.candidate_root.exists()
            assert not job.job_directory.exists()
            assert job.cleaned is True


def test_mismatched_worker_result_is_protocol_failure() -> None:
    """다른 job ID를 가리키는 worker 결과는 출력 확정 없이 protocol 실패가 되는지 확인합니다."""

    from tools.align.merge_export.types import MergeExportResult, MergePhase

    with tempfile.TemporaryDirectory(prefix="colmap_align_backend_") as temporary_directory:
        root = Path(temporary_directory).resolve()
        process = _FakeWorkerProcess()
        backend = _backend()
        with _patched_popen(process):
            job = backend.start(_snapshot(root, job_id="backend-mismatch"))
            result = MergeExportResult(
                job_id="different-job",
                phase=MergePhase.WAITING_FOR_FINALIZE,
                runtime_fingerprint=job.snapshot.runtime_fingerprint,
                candidate_root=job.candidate_root,
                output_root=job.snapshot.output_root,
            )
            _write_worker_result(job, result)
            process.return_code = 1
            polled = backend.poll(job)
            assert polled.phase is MergePhase.FAILED
            assert polled.error is not None and polled.error.code == "WORKER_PROTOCOL"
            assert job.cleaned is True
            assert not job.snapshot.output_root.exists()


def test_worker_retries_windows_progress_file_races() -> None:
    """WinError 5·공유 위반 뒤에는 progress 원자 기록만 제한 재시도하는지 확인합니다."""

    from tools.align.merge_export import worker as worker_module
    from tools.align.merge_export.types import MergeStage
    from tools.align.merge_export.worker import WorkerJobFiles

    with tempfile.TemporaryDirectory(prefix="colmap_align_worker_progress_") as temporary_directory:
        root = Path(temporary_directory).resolve()
        files = WorkerJobFiles(root)
        snapshot = _snapshot(root, job_id="worker-progress-retry")
        original_write = worker_module._atomic_write_json
        for winerror in (5, 32):
            attempts = 0

            def write_with_race(path: Path, payload: object) -> None:
                """처음 두 progress 기록만 Windows 파일 경합으로 실패시킵니다."""

                nonlocal attempts
                if path == files.progress_path and attempts < 2:
                    attempts += 1
                    raise _windows_progress_race_error(winerror)
                attempts += 1
                original_write(path, payload)

            worker_module._atomic_write_json = write_with_race
            try:
                worker_module._emit_progress(
                    files,
                    snapshot,
                    MergeStage.SNAPSHOT_INPUTS,
                    0.0,
                    "경합 재시도 검사",
                    force=True,
                )
            finally:
                worker_module._atomic_write_json = original_write
            assert attempts == 3
            progress = json.loads(files.progress_path.read_text(encoding="utf-8"))
            assert progress["job_id"] == snapshot.job_id
            assert progress["stage"] == MergeStage.SNAPSHOT_INPUTS.value
            files.progress_path.unlink()


def test_worker_progress_race_does_not_hide_terminal_failure() -> None:
    """모든 progress 재시도가 실패해도 worker 본체 오류의 error.json은 남는지 확인합니다."""

    from tools.align.merge_export import worker as worker_module
    from tools.align.merge_export.types import MergePhase
    from tools.align.merge_export.worker import WorkerJobFiles

    with tempfile.TemporaryDirectory(prefix="colmap_align_worker_progress_") as temporary_directory:
        root = Path(temporary_directory).resolve()
        job_directory = root / "job"
        job_directory.mkdir()
        files = WorkerJobFiles(job_directory)
        snapshot = _snapshot(root, job_id="worker-progress-terminal")
        worker_module._atomic_write_json(
            files.manifest_path,
            {"snapshot": snapshot.to_mapping(), "preflight_only": False},
        )
        original_write = worker_module._atomic_write_json
        original_load_sources = worker_module._fingerprint_and_load_sources
        attempts = 0

        def always_racing_progress(path: Path, payload: object) -> None:
            """progress.json만 계속 공유 위반으로 실패시키고 terminal JSON은 기록합니다."""

            nonlocal attempts
            if path == files.progress_path:
                attempts += 1
                raise _windows_progress_race_error(32)
            original_write(path, payload)

        def fail_export_body(*arguments: object, **keywords: object) -> object:
            """진행률과 무관한 실제 worker 본체 실패를 재현합니다."""

            del arguments, keywords
            raise RuntimeError("테스트용 내보내기 본체 실패")

        worker_module._atomic_write_json = always_racing_progress
        worker_module._fingerprint_and_load_sources = fail_export_body
        try:
            result = worker_module.run_worker_job(job_directory)
        finally:
            worker_module._atomic_write_json = original_write
            worker_module._fingerprint_and_load_sources = original_load_sources
        assert result.phase is MergePhase.FAILED
        assert result.error is not None
        assert result.error.cause == "테스트용 내보내기 본체 실패"
        assert attempts == 9
        terminal = json.loads(files.error_path.read_text(encoding="utf-8"))
        assert terminal["phase"] == MergePhase.FAILED.value
        assert terminal["error"]["cause"] == "테스트용 내보내기 본체 실패"


def test_backend_keeps_last_progress_for_syntax_errors_and_rejects_semantic_errors() -> None:
    """실행 중 progress 구문 오류는 복구하고 완성 객체의 의미 오류는 거부하는지 확인합니다."""

    from tools.align.merge_export.types import MergeExportProgress, MergeProtocolError, MergeStage

    with tempfile.TemporaryDirectory(prefix="colmap_align_backend_progress_") as temporary_directory:
        root = Path(temporary_directory).resolve()
        process = _FakeWorkerProcess()
        backend = _backend()
        with _patched_popen(process):
            job = backend.start(_snapshot(root, job_id="backend-progress-cache"))
            expected = MergeExportProgress(
                job_id=job.job_id,
                stage=MergeStage.SNAPSHOT_INPUTS,
                fraction=0.1,
                message="마지막 정상 진행률",
            )
            _write_worker_progress(job, expected)
            assert backend._read_progress(job) == expected

            for malformed in (b"", b"{", b'{"message":"\xed\xa0'):
                job.progress_path.write_bytes(malformed)
                assert backend._read_progress(job) == expected
            assert job.progress_read_warning_count == 3

            recovered = MergeExportProgress(
                job_id=job.job_id,
                stage=MergeStage.FINGERPRINT_SOURCES,
                fraction=0.2,
                message="복구된 진행률",
            )
            _write_worker_progress(job, recovered)
            assert backend._read_progress(job) == recovered
            assert job.last_progress_warning_signature is not None
            assert job.last_progress_parse_error_type == ""

            missing_key = expected.to_mapping()
            del missing_key["counts"]
            from tools.align.merge_export import backend as backend_module

            backend_module._write_json_atomically(job.progress_path, missing_key)
            try:
                backend._read_progress(job)
            except MergeProtocolError:
                pass
            else:
                raise AssertionError("필수 키가 없는 progress.json이 허용되었습니다.")

            wrong_job = MergeExportProgress(
                job_id="different-job",
                stage=MergeStage.SNAPSHOT_INPUTS,
                fraction=0.2,
                message="다른 작업 진행률",
            )
            _write_worker_progress(job, wrong_job)
            try:
                backend._read_progress(job)
            except MergeProtocolError:
                pass
            else:
                raise AssertionError("다른 job ID의 progress.json이 허용되었습니다.")

            invalid_stage = expected.to_mapping()
            invalid_stage["stage"] = "UNKNOWN_STAGE"
            backend_module._write_json_atomically(job.progress_path, invalid_stage)
            try:
                backend._read_progress(job)
            except MergeProtocolError:
                pass
            else:
                raise AssertionError("알 수 없는 progress stage가 허용되었습니다.")

            invalid_count = expected.to_mapping()
            for invalid_value in (-1, 1.5):
                invalid_count["counts"] = {"images": invalid_value}
                backend_module._write_json_atomically(job.progress_path, invalid_count)
                try:
                    backend._read_progress(job)
                except MergeProtocolError:
                    pass
                else:
                    raise AssertionError("잘못된 progress count가 허용되었습니다.")

            job.progress_path.write_text(
                '{"counts":{},"fraction":NaN,"job_id":"backend-progress-cache",'
                '"message":"비유한 진행률","stage":"SNAPSHOT_INPUTS"}',
                encoding="utf-8",
            )
            try:
                backend._read_progress(job)
            except MergeProtocolError:
                pass
            else:
                raise AssertionError("비유한 progress fraction이 허용되었습니다.")
            backend.invalidate(job)


def test_terminal_result_has_priority_over_malformed_progress() -> None:
    """정상 terminal이 있으면 손상된 progress를 읽지 않고 결과를 처리하는지 확인합니다."""

    from tools.align.merge_export.types import MergeExportResult, MergePhase

    with tempfile.TemporaryDirectory(prefix="colmap_align_backend_terminal_priority_") as temporary_directory:
        root = Path(temporary_directory).resolve()
        process = _FakeWorkerProcess(return_code=0)
        backend = _backend()
        with _patched_popen(process):
            job = backend.start(_snapshot(root, job_id="terminal-priority"))
            _create_complete_candidate(job.candidate_root)
            result = MergeExportResult(
                job_id=job.job_id,
                phase=MergePhase.WAITING_FOR_FINALIZE,
                runtime_fingerprint=job.snapshot.runtime_fingerprint,
                candidate_root=job.candidate_root,
                output_root=job.snapshot.output_root,
            )
            _write_worker_result(job, result)
            job.progress_path.write_text("{", encoding="utf-8")
            polled = backend.poll(job)
            assert polled.phase is MergePhase.WAITING_FOR_FINALIZE
            completed = backend.finalize(
                job,
                current_runtime_fingerprint=job.snapshot.runtime_fingerprint,
            )
            assert completed.phase is MergePhase.COMPLETED


def test_atomic_progress_replacement_survives_concurrent_reads() -> None:
    """실제 thread의 원자 progress 교체와 backend 읽기를 반복해 파일 경합을 검증합니다."""

    from tools.align.merge_export import worker as worker_module
    from tools.align.merge_export.types import MergeStage
    from tools.align.merge_export.worker import WorkerJobFiles

    with tempfile.TemporaryDirectory(prefix="colmap_align_backend_progress_stress_") as temporary_directory:
        root = Path(temporary_directory).resolve()
        process = _FakeWorkerProcess()
        backend = _backend()
        with _patched_popen(process):
            job = backend.start(_snapshot(root, job_id="progress-stress"))
            errors: list[BaseException] = []
            files = WorkerJobFiles(job.job_directory)

            def replace_progress() -> None:
                """유효한 progress 원자 교체를 빠르게 반복합니다."""

                try:
                    for index in range(200):
                        worker_module._emit_progress(
                            files,
                            job.snapshot,
                            MergeStage.COPY_IMAGES,
                            index / 199,
                            "동시 교체 검사",
                            counts={"image_files": index},
                            force=True,
                        )
                except BaseException as error:
                    errors.append(error)

            writer = threading.Thread(target=replace_progress, daemon=True)
            writer.start()
            while writer.is_alive():
                backend._read_progress(job)
            writer.join()
            assert not errors, repr(errors)
            worker_module._emit_progress(
                files,
                job.snapshot,
                MergeStage.COPY_IMAGES,
                1.0,
                "동시 교체 완료",
                counts={"image_files": 199},
                force=True,
            )
            progress = backend._read_progress(job)
            assert progress is not None
            assert progress.fraction == 1.0
            assert progress.counts["image_files"] == 199
            backend.invalidate(job)


def test_progress_write_frequency_is_time_bounded_and_file_errors_are_nonfatal() -> None:
    """반복 count와 progress 파일 오류가 worker 본체 결과에 영향을 주지 않는지 확인합니다."""

    from tools.align.merge_export import worker as worker_module
    from tools.align.merge_export.types import MergeStage
    from tools.align.merge_export.worker import WorkerJobFiles

    with tempfile.TemporaryDirectory(prefix="colmap_align_worker_throttle_") as temporary_directory:
        root = Path(temporary_directory).resolve()
        snapshot = _snapshot(root, job_id="worker-throttle")
        original_write = worker_module._atomic_write_json
        original_monotonic = worker_module.monotonic
        written: list[dict[str, Any]] = []
        clock = 10.0

        def record_write(_path: Path, payload: object) -> None:
            """실제 파일 대신 progress payload와 기록 횟수만 보관합니다."""

            written.append(dict(payload))

        worker_module._atomic_write_json = record_write
        worker_module.monotonic = lambda: clock
        try:
            for count in (1, 594, 10_000):
                written.clear()
                files = WorkerJobFiles(root)
                worker_module._emit_progress(
                    files,
                    snapshot,
                    MergeStage.COPY_IMAGES,
                    0.87,
                    "복사 시작",
                    counts={"image_files": 0, "total_image_files": count},
                )
                for image_index in range(1, count + 1):
                    worker_module._emit_progress(
                        files,
                        snapshot,
                        MergeStage.COPY_IMAGES,
                        0.87 + 0.10 * (image_index / count),
                        "복사 중",
                        counts={"image_files": image_index, "total_image_files": count},
                    )
                worker_module._emit_progress(
                    files,
                    snapshot,
                    MergeStage.COPY_IMAGES,
                    0.97,
                    "복사 완료",
                    counts={"image_files": count, "total_image_files": count},
                    force=True,
                )
                assert len(written) == 2
                assert written[-1]["counts"]["image_files"] == count
        finally:
            worker_module._atomic_write_json = original_write
            worker_module.monotonic = original_monotonic

        files = WorkerJobFiles(root)

        def fail_progress_write(_path: Path, _payload: object) -> None:
            """공유 위반이 아닌 일반 파일 오류도 보조 progress에서만 실패시킵니다."""

            raise OSError("테스트용 progress 기록 실패")

        worker_module._atomic_write_json = fail_progress_write
        try:
            worker_module._emit_progress(
                files,
                snapshot,
                MergeStage.SNAPSHOT_INPUTS,
                0.0,
                "기록 실패 허용",
                force=True,
            )
        finally:
            worker_module._atomic_write_json = original_write
        assert files.progress_write_count == 0


def test_failure_diagnostic_is_bounded_and_success_removes_it() -> None:
    """실패 진단은 한 파일·64KB 로그 꼬리로 제한되고 정상 확정 뒤 삭제되는지 확인합니다."""

    from tools.align.merge_export import backend as backend_module
    from tools.align.merge_export.types import MergeExportProgress, MergeExportResult, MergePhase, MergeStage

    diagnostic_path = backend_module._failure_diagnostic_path()
    diagnostic_path.unlink(missing_ok=True)
    with tempfile.TemporaryDirectory(prefix="colmap_align_backend_diagnostic_") as temporary_directory:
        root = Path(temporary_directory).resolve()
        process = _FakeWorkerProcess()
        backend = _backend()
        with _patched_popen(process):
            job = backend.start(_snapshot(root, job_id="diagnostic-failure"))
            progress = MergeExportProgress(
                job_id=job.job_id,
                stage=MergeStage.COPY_IMAGES,
                fraction=0.9,
                message="마지막 정상 진행률",
            )
            _write_worker_progress(job, progress)
            assert backend.poll(job).progress == progress
            job.worker_log_handle.write("L" * (80 * 1024))
            job.worker_log_handle.flush()
            job.progress_path.write_text("{", encoding="utf-8")
            assert backend.poll(job).progress == progress
            process.return_code = 1
            failed = backend.poll(job)
            assert failed.phase is MergePhase.FAILED
            assert diagnostic_path.is_file()
            diagnostic = json.loads(diagnostic_path.read_text(encoding="utf-8"))
            assert diagnostic["job_id"] == job.job_id
            assert diagnostic["progress_read_warning_count"] == 1
            assert diagnostic["last_progress"]["stage"] == MergeStage.COPY_IMAGES.value
            assert len(diagnostic["worker_log_tail"].encode("utf-8")) <= 64 * 1024
            assert not job.job_directory.exists()

        success_process = _FakeWorkerProcess(return_code=0)
        success_backend = _backend()
        with _patched_popen(success_process):
            success_root = root / "success"
            success_root.mkdir()
            success_job = success_backend.start(
                _snapshot(success_root, job_id="diagnostic-success")
            )
            _create_complete_candidate(success_job.candidate_root)
            result = MergeExportResult(
                job_id=success_job.job_id,
                phase=MergePhase.WAITING_FOR_FINALIZE,
                runtime_fingerprint=success_job.snapshot.runtime_fingerprint,
                candidate_root=success_job.candidate_root,
                output_root=success_job.snapshot.output_root,
            )
            _write_worker_result(success_job, result)
            assert success_backend.poll(success_job).phase is MergePhase.WAITING_FOR_FINALIZE
            completed = success_backend.finalize(
                success_job,
                current_runtime_fingerprint=success_job.snapshot.runtime_fingerprint,
            )
            assert completed.phase is MergePhase.COMPLETED
            assert not diagnostic_path.exists()


def run_all() -> None:
    """backend 합성 검사를 Blender 한 프로세스에서 순서대로 실행합니다."""

    _add_source_path()
    test_backend_starts_worker_with_strict_manifest_and_safe_command()
    test_existing_output_requires_explicit_overwrite_approval()
    test_overwrite_finalize_replaces_only_after_candidate_success()
    test_overwrite_finalize_preserves_output_changed_during_worker()
    test_overwrite_cancel_keeps_existing_output()
    test_preflight_error_json_is_reported_as_not_ready_preflight()
    test_cancelled_error_json_and_cancelled_exit_without_result_are_user_cancellation()
    test_terminal_result_waits_for_worker_exit_and_finalize_never_promotes_early()
    test_stale_finalize_never_creates_output_and_removes_candidate()
    test_mismatched_worker_result_is_protocol_failure()
    test_worker_retries_windows_progress_file_races()
    test_worker_progress_race_does_not_hide_terminal_failure()
    test_backend_keeps_last_progress_for_syntax_errors_and_rejects_semantic_errors()
    test_terminal_result_has_priority_over_malformed_progress()
    test_atomic_progress_replacement_survives_concurrent_reads()
    test_progress_write_frequency_is_time_bounded_and_file_errors_are_nonfatal()
    test_failure_diagnostic_is_bounded_and_success_removes_it()
    print("test_merge_export_backend: 통과")


if __name__ == "__main__":
    run_all()
