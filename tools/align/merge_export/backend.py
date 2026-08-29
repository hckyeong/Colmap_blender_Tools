"""구조 통합 worker의 작업 폴더·프로세스·후보 출력 수명주기를 관리합니다."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from time import monotonic
from typing import Any, Mapping, TextIO
from uuid import uuid4

from ..constants import (
    MERGE_EXPORT_ALGORITHM_VERSION,
    MERGE_EXPORT_OUTPUT_CANDIDATE_PREFIX,
    MERGE_EXPORT_PROTOCOL_VERSION,
)
from ..logging_utils import get_logger
from .types import (
    MergeExportError,
    MergeExportPollResult,
    MergeExportPreflightResult,
    MergeExportProgress,
    MergeExportResult,
    MergeExportSnapshot,
    MergeIssue,
    MergeOutputFormat,
    MergePhase,
    MergeProtocolError,
)


_LOGGER = get_logger("merge_export.backend")
_JOB_DIRECTORY_PREFIX = "colmap_align_merge_job_"
_CANCEL_FILE_NAME = "cancel.request"
_MANIFEST_FILE_NAME = "manifest.json"
_PROGRESS_FILE_NAME = "progress.json"
_PREFLIGHT_FILE_NAME = "preflight.json"
_RESULT_FILE_NAME = "result.json"
_ERROR_FILE_NAME = "error.json"
_WORKER_LOG_FILE_NAME = "worker.log"
_FAILURE_DIAGNOSTIC_FILE_NAME = "colmap_align_merge_export_last_failure.json"
_MAX_JSON_BYTES = 8 * 1024 * 1024
_MAX_WORKER_LOG_TAIL_BYTES = 64 * 1024
_CANCEL_GRACE_SECONDS = 2.0
_TERMINATE_GRACE_SECONDS = 2.0
_PROCESS_WAIT_SECONDS = 1.0
_SAFE_JOB_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_PREVIOUS_OUTPUT_PREFIX = ".colmap_align_previous_"
_MANIFEST_KEYS = frozenset({"snapshot", "preflight_only"})
_PROGRESS_KEYS = frozenset({"job_id", "stage", "fraction", "message", "counts"})
_PREFLIGHT_KEYS = frozenset(
    {
        "job_id",
        "runtime_fingerprint",
        "included_model_ids",
        "excluded_model_ids",
        "duplicate_image_group_count",
        "warnings",
        "errors",
    }
)
_RESULT_KEYS = frozenset(
    {
        "job_id",
        "phase",
        "runtime_fingerprint",
        "candidate_root",
        "output_root",
        "counts",
        "warnings",
        "error",
    }
)
_REQUIRED_SPARSE_FILES = {
    MergeOutputFormat.TEXT: tuple(
        Path("sparse") / "0" / filename
        for filename in ("rigs.txt", "cameras.txt", "frames.txt", "images.txt", "points3D.txt")
    ),
    MergeOutputFormat.BINARY: tuple(
        Path("sparse") / "0" / filename
        for filename in ("rigs.bin", "cameras.bin", "frames.bin", "images.bin", "points3D.bin")
    ),
}
_REQUIRED_COMMON_CANDIDATE_FILES = (
    Path("merge_report.json"),
    Path("merge_id_map.npz"),
)
_REQUIRED_CANDIDATE_DIRECTORIES = (Path("images"),)

_OutputTreeSignature = tuple[tuple[str, str, int, int], ...]


def _required_candidate_files(output_format: MergeOutputFormat) -> tuple[Path, ...]:
    """선택한 sparse 형식과 공통 감사 파일의 필수 후보 경로를 반환합니다."""

    return _REQUIRED_SPARSE_FILES[output_format] + _REQUIRED_COMMON_CANDIDATE_FILES


class MergeExportBackendError(MergeExportError):
    """메인 Blender가 worker 작업을 안전하게 관리할 수 없을 때 발생합니다."""


class _ProgressReadSyntaxError(MergeProtocolError):
    """실행 중 다음 poll에서 복구할 수 있는 progress 구문·접근 오류입니다."""


@dataclass(slots=True)
class MergeExportJob:
    """한 worker 프로세스와 그 전용 작업·후보 경로를 묶습니다."""

    job_id: str
    snapshot: MergeExportSnapshot
    job_directory: Path
    candidate_root: Path
    process: subprocess.Popen[Any]
    preflight_only: bool
    worker_log_handle: TextIO
    backend_token: object
    existing_output_signature: _OutputTreeSignature | None = None
    cancel_requested_at: float | None = None
    terminate_requested_at: float | None = None
    result: MergeExportResult | None = None
    preflight: MergeExportPreflightResult | None = None
    last_progress: MergeExportProgress | None = None
    progress_read_warning_count: int = 0
    last_progress_warning_signature: str | None = None
    last_progress_parse_error_type: str = ""
    cleaned: bool = False

    @property
    def manifest_path(self) -> Path:
        """worker가 읽을 immutable manifest 경로를 반환합니다."""

        return self.job_directory / _MANIFEST_FILE_NAME

    @property
    def progress_path(self) -> Path:
        """worker가 원자 갱신하는 진행률 파일 경로를 반환합니다."""

        return self.job_directory / _PROGRESS_FILE_NAME

    @property
    def preflight_path(self) -> Path:
        """preflight-only worker의 최종 검사 결과 경로를 반환합니다."""

        return self.job_directory / _PREFLIGHT_FILE_NAME

    @property
    def result_path(self) -> Path:
        """worker 성공 결과 경로를 반환합니다."""

        return self.job_directory / _RESULT_FILE_NAME

    @property
    def error_path(self) -> Path:
        """worker 실패 결과 경로를 반환합니다."""

        return self.job_directory / _ERROR_FILE_NAME

    @property
    def cancel_path(self) -> Path:
        """worker가 확인할 취소 sentinel 경로를 반환합니다."""

        return self.job_directory / _CANCEL_FILE_NAME

    @property
    def worker_log_path(self) -> Path:
        """worker 표준 출력과 오류를 합쳐 보관하는 경로를 반환합니다."""

        return self.job_directory / _WORKER_LOG_FILE_NAME


def _json_constant_error(value: str) -> None:
    """NaN·Infinity 같은 JSON 표준 밖 상수를 즉시 거부합니다."""

    raise ValueError(f"허용하지 않는 JSON 상수입니다: {value}")


def _write_json_atomically(path: Path, value: Mapping[str, Any]) -> None:
    """worker가 부분 manifest를 읽지 않도록 작은 JSON 객체를 원자 기록합니다."""

    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        payload = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        with temporary.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except (OSError, TypeError, ValueError) as error:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise MergeExportBackendError(f"구조 통합 작업 JSON을 기록하지 못했습니다: {path}") from error


def _read_json_mapping(path: Path, *, label: str, required_keys: frozenset[str]) -> Mapping[str, Any]:
    """worker가 원자 기록한 제한 크기 JSON 객체와 정확한 키 집합을 확인합니다."""

    try:
        size = path.stat().st_size
    except OSError as error:
        raise MergeProtocolError(f"{label} 파일 상태를 읽지 못했습니다.") from error
    if size < 1 or size > _MAX_JSON_BYTES:
        raise MergeProtocolError(f"{label} 파일 크기가 허용 범위를 벗어났습니다.")
    try:
        with path.open("r", encoding="utf-8") as stream:
            value = json.load(stream, parse_constant=_json_constant_error)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise MergeProtocolError(f"{label} JSON 형식이 올바르지 않습니다.") from error
    if not isinstance(value, Mapping):
        raise MergeProtocolError(f"{label}은(는) JSON 객체여야 합니다.")
    actual_keys = frozenset(value)
    if actual_keys != required_keys:
        missing = ", ".join(sorted(required_keys.difference(actual_keys)))
        unexpected = ", ".join(sorted(actual_keys.difference(required_keys)))
        details = []
        if missing:
            details.append(f"누락: {missing}")
        if unexpected:
            details.append(f"예상 밖: {unexpected}")
        raise MergeProtocolError(f"{label} 키 집합이 프로토콜과 다릅니다. {' / '.join(details)}")
    return value


def _read_progress_json_mapping(path: Path) -> Mapping[str, Any]:
    """progress의 일시 구문 오류와 완성 객체의 의미 오류를 분리해 읽습니다."""

    try:
        size = path.stat().st_size
    except (FileNotFoundError, OSError) as error:
        raise _ProgressReadSyntaxError("구조 통합 progress 파일을 일시적으로 읽지 못했습니다.") from error
    if size == 0:
        raise _ProgressReadSyntaxError("구조 통합 progress 파일이 일시적으로 비어 있습니다.")
    if size > _MAX_JSON_BYTES:
        raise MergeProtocolError("구조 통합 progress 파일 크기가 허용 범위를 벗어났습니다.")
    try:
        with path.open("r", encoding="utf-8") as stream:
            value = json.load(stream, parse_constant=_json_constant_error)
    except (FileNotFoundError, OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise _ProgressReadSyntaxError("구조 통합 progress JSON을 일시적으로 읽지 못했습니다.") from error
    except ValueError as error:
        raise MergeProtocolError("구조 통합 progress에 비유한 JSON 수치가 있습니다.") from error
    if not isinstance(value, Mapping):
        raise MergeProtocolError("구조 통합 progress은(는) JSON 객체여야 합니다.")
    actual_keys = frozenset(value)
    if actual_keys != _PROGRESS_KEYS:
        missing = ", ".join(sorted(_PROGRESS_KEYS.difference(actual_keys)))
        unexpected = ", ".join(sorted(actual_keys.difference(_PROGRESS_KEYS)))
        details = []
        if missing:
            details.append(f"누락: {missing}")
        if unexpected:
            details.append(f"예상 밖: {unexpected}")
        raise MergeProtocolError(
            f"구조 통합 progress 키 집합이 프로토콜과 다릅니다. {' / '.join(details)}"
        )
    return value


def _failure_diagnostic_path() -> Path:
    """마지막 Export 실패 하나만 보존할 운영체제 임시 JSON 경로를 반환합니다."""

    return Path(tempfile.gettempdir()) / _FAILURE_DIAGNOSTIC_FILE_NAME


def _resolved_path(path: Path, *, label: str) -> Path:
    """상대·심볼릭 경로 혼동 없이 비교할 절대 경로를 반환합니다."""

    try:
        return path.resolve(strict=False)
    except OSError as error:
        raise MergeExportBackendError(f"{label} 경로를 확인하지 못했습니다: {path}") from error


def _is_within(path: Path, parent: Path) -> bool:
    """해결된 path가 parent 자신 또는 그 하위인지 안전하게 확인합니다."""

    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _issue(code: str, cause: str, impact: str, remedy: str) -> MergeIssue:
    """backend 경계 오류를 UI가 바로 표시할 구조화 진단으로 만듭니다."""

    return MergeIssue(code=code, cause=cause, impact=impact, remedy=remedy)


class MergeExportBackend:
    """숨김 Blender worker와 형제 candidate를 한 작업 단위로 안전하게 관리합니다."""

    def __init__(self, *, blender_binary_path: str | os.PathLike[str] | None = None) -> None:
        """기본 Blender 경로 또는 테스트·배포용 명시 경로를 보관합니다."""

        self._blender_binary_path = (
            Path(blender_binary_path) if blender_binary_path is not None else None
        )
        self._token = object()

    def failure_diagnostic_path(self) -> Path | None:
        """실제로 존재하는 마지막 실패 진단 파일 경로만 반환합니다."""

        path = _failure_diagnostic_path()
        return path if path.is_file() else None

    def start_preflight(self, snapshot: MergeExportSnapshot) -> MergeExportJob:
        """파일 기록 없이 preflight만 실행하는 worker 작업을 시작합니다."""

        return self.start(snapshot, preflight_only=True)

    def start(
        self,
        snapshot: MergeExportSnapshot,
        preflight_only: bool = False,
    ) -> MergeExportJob:
        """검증된 snapshot을 전용 job 폴더에 기록하고 숨김 Blender worker를 시작합니다."""

        if not isinstance(snapshot, MergeExportSnapshot):
            raise TypeError("구조 통합 시작에는 MergeExportSnapshot이 필요합니다.")
        if not isinstance(preflight_only, bool):
            raise TypeError("preflight_only는 bool이어야 합니다.")
        self._validate_snapshot_contract(snapshot)
        output_root = self._validate_output_root(snapshot)
        existing_output_signature = (
            self._output_tree_signature(output_root) if output_root.exists() else None
        )
        candidate_root = self._candidate_root_for(snapshot.job_id, output_root)
        if candidate_root.exists():
            raise MergeExportBackendError("이 작업 ID의 구조 통합 후보 폴더가 이미 존재합니다.")

        prepared_snapshot = replace(
            snapshot,
            output_root=output_root,
            candidate_root=candidate_root,
        )
        job_directory = self._create_job_directory(prepared_snapshot.job_id)
        try:
            manifest = {
                "snapshot": prepared_snapshot.to_mapping(),
                "preflight_only": preflight_only,
            }
            _write_json_atomically(job_directory / _MANIFEST_FILE_NAME, manifest)
            self._validate_written_manifest(job_directory / _MANIFEST_FILE_NAME, prepared_snapshot, preflight_only)
            log_handle = (job_directory / _WORKER_LOG_FILE_NAME).open(
                "w",
                encoding="utf-8",
                newline="\n",
            )
            process = self._launch_worker(job_directory, log_handle)
        except Exception:
            try:
                log_handle.close()  # type: ignore[name-defined]
            except (NameError, OSError):
                pass
            self._remove_job_directory(job_directory)
            raise

        job = MergeExportJob(
            job_id=prepared_snapshot.job_id,
            snapshot=prepared_snapshot,
            job_directory=job_directory,
            candidate_root=candidate_root,
            process=process,
            preflight_only=preflight_only,
            worker_log_handle=log_handle,
            backend_token=self._token,
            existing_output_signature=existing_output_signature,
        )
        _LOGGER.info(
            "event=merge_export_worker_started job_id=%s process_id=%s preflight_only=%s",
            job.job_id,
            getattr(process, "pid", "unknown"),
            preflight_only,
        )
        return job

    def poll(self, job: MergeExportJob) -> MergeExportPollResult:
        """terminal을 우선 검증하고 실행 중 progress만 best-effort 상태로 반환합니다."""

        self._require_job(job)
        if job.cleaned:
            return self._failed_poll(job, "JOB_CLEANED", "이미 정리된 구조 통합 작업입니다.")
        try:
            terminal = self._read_terminal_result(job)
        except (MergeProtocolError, MergeExportBackendError) as error:
            self._stop_worker_for_protocol_failure(job)
            return self._terminal_failure(job, "WORKER_PROTOCOL", str(error))

        if terminal is not None:
            result, preflight = terminal
            job.result = result
            job.preflight = preflight
            return self._poll_terminal_result(job, result, preflight, job.last_progress)

        worker_running = self._is_process_running(job)
        if worker_running:
            try:
                progress = self._read_progress(job)
            except (MergeProtocolError, MergeExportBackendError) as error:
                self._stop_worker_for_protocol_failure(job)
                return self._terminal_failure(job, "WORKER_PROTOCOL", str(error))
            if job.cancel_requested_at is not None:
                self._advance_cancel(job)
                return MergeExportPollResult(
                    phase=MergePhase.CANCEL_PENDING,
                    progress=progress,
                )
            return MergeExportPollResult(
                phase=(
                    MergePhase.PREFLIGHT_RUNNING
                    if job.preflight_only
                    else MergePhase.EXPORT_RUNNING
                ),
                progress=progress,
            )

        if job.cancel_requested_at is not None:
            return self._terminal_cancelled(
                job,
                "CANCELLED_WITHOUT_RESULT",
                "취소 요청 뒤 worker가 최종 결과 파일을 기록하지 않고 종료되었습니다.",
                progress=job.last_progress,
            )
        return self._terminal_failure(
            job,
            "WORKER_EXITED",
            "구조 통합 worker가 완료 결과를 기록하지 않고 종료되었습니다.",
        )

    def cancel(self, job: MergeExportJob) -> bool:
        """한 번만 cancel sentinel을 원자 생성하고 poll이 종료를 회수하게 둡니다."""

        self._require_job(job)
        if job.cleaned or not self._is_process_running(job) or job.cancel_requested_at is not None:
            return False
        temporary = job.cancel_path.with_name(f".{_CANCEL_FILE_NAME}.{uuid4().hex}.tmp")
        try:
            with temporary.open("x", encoding="utf-8", newline="\n") as stream:
                stream.write("cancel\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, job.cancel_path)
        except OSError as error:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            raise MergeExportBackendError("구조 통합 취소 요청을 기록하지 못했습니다.") from error
        job.cancel_requested_at = monotonic()
        _LOGGER.info("event=merge_export_cancel_requested job_id=%s", job.job_id)
        return True

    def finalize(
        self,
        job: MergeExportJob,
        *,
        current_runtime_fingerprint: str,
    ) -> MergeExportResult:
        """stale 재검사 뒤 검증된 형제 candidate를 새 output root로 한 번만 승격합니다."""

        self._require_job(job)
        if job.preflight_only:
            return self._finalize_failure(job, "PREFLIGHT_ONLY", "사전 검사 작업은 최종 출력으로 승격할 수 없습니다.")
        if self._is_process_running(job):
            return self._result_with_issue(
                job,
                phase=MergePhase.FAILED,
                issue=_issue(
                    "WORKER_STILL_RUNNING",
                    "worker가 아직 종료되지 않아 결과 파일이 최종 확정 가능한 상태가 아닙니다.",
                    "실행 중인 worker 후보를 최종 출력으로 승격하지 않았습니다.",
                    "poll에서 WAITING_FOR_FINALIZE 상태가 확인된 뒤 다시 확정하십시오.",
                ),
            )
        result = job.result
        if result is None:
            try:
                terminal = self._read_terminal_result(job)
            except (MergeProtocolError, MergeExportBackendError) as error:
                return self._finalize_failure(job, "WORKER_PROTOCOL", str(error))
            if terminal is None:
                return self._finalize_failure(job, "RESULT_MISSING", "최종 확정 전에 worker 성공 결과가 필요합니다.")
            result, _preflight = terminal
            job.result = result
        if not result.is_success:
            return self._finalize_failure(job, "RESULT_NOT_READY", "최종 확정 가능한 worker 성공 결과가 아닙니다.")
        if not isinstance(current_runtime_fingerprint, str) or not current_runtime_fingerprint.strip():
            return self._finalize_failure(job, "FINGERPRINT_INVALID", "현재 Runtime fingerprint가 비어 있거나 올바르지 않습니다.")
        if current_runtime_fingerprint.strip() != job.snapshot.runtime_fingerprint:
            stale = self._result_with_issue(
                job,
                phase=MergePhase.STALE,
                issue=_issue(
                    "RUNTIME_STALE",
                    "구조 통합 worker 실행 뒤 A 그룹 또는 확정 변환이 변경되었습니다.",
                    "이전 입력으로 계산한 후보 결과를 최종 출력으로 사용할 수 없습니다.",
                    "현재 상태에서 사전 검사와 통합 내보내기를 다시 실행하십시오.",
                ),
                warnings=result.warnings,
                counts=result.counts,
            )
            self._preserve_failure_diagnostic(
                job,
                code="RUNTIME_STALE",
                cause=stale.error.cause if stale.error is not None else "Runtime 결과가 stale 상태입니다.",
            )
            self._cleanup_terminal_job(job, remove_candidate=True)
            return stale
        try:
            self._validate_candidate_for_finalize(job, result)
            self._promote_candidate(job)
        except (OSError, MergeExportBackendError) as error:
            return self._finalize_failure(job, "FINALIZE_FAILED", str(error))

        completed = MergeExportResult(
            job_id=job.job_id,
            phase=MergePhase.COMPLETED,
            runtime_fingerprint=job.snapshot.runtime_fingerprint,
            candidate_root=None,
            output_root=job.snapshot.output_root,
            counts=result.counts,
            warnings=result.warnings,
            error=None,
        )
        job.result = completed
        self._remove_failure_diagnostic()
        try:
            self._cleanup_terminal_job(job, remove_candidate=False)
        except MergeExportBackendError:
            # 출력 승격 뒤 임시 폴더 정리 실패는 완료 결과를 실패로 바꾸지 않습니다.
            _LOGGER.exception("event=merge_export_finalize_cleanup_failed job_id=%s", job.job_id)
        _LOGGER.info("event=merge_export_finalized job_id=%s output=%s", job.job_id, completed.output_root)
        return completed

    def invalidate(self, job: MergeExportJob) -> None:
        """수명주기 종료 때 해당 worker만 회수하고 검증된 후보·job 폴더만 정리합니다."""

        self._require_job(job)
        if job.cleaned:
            return
        self._stop_worker(job)
        self._cleanup_terminal_job(job, remove_candidate=True)

    def _validate_snapshot_contract(self, snapshot: MergeExportSnapshot) -> None:
        """현재 addon protocol·안전한 job ID·backend 전용 candidate 생성을 확인합니다."""

        if snapshot.protocol_version != MERGE_EXPORT_PROTOCOL_VERSION:
            raise MergeExportBackendError("구조 통합 snapshot protocol version이 현재 구현과 다릅니다.")
        if snapshot.algorithm_version != MERGE_EXPORT_ALGORITHM_VERSION:
            raise MergeExportBackendError("구조 통합 snapshot algorithm version이 현재 구현과 다릅니다.")
        if not _SAFE_JOB_ID.fullmatch(snapshot.job_id):
            raise MergeExportBackendError("구조 통합 job ID에는 안전한 영문·숫자·밑줄·하이픈만 사용할 수 있습니다.")
        if snapshot.candidate_root is not None:
            raise MergeExportBackendError("구조 통합 candidate 경로는 backend만 만들 수 있습니다.")

    def _validate_output_root(self, snapshot: MergeExportSnapshot) -> Path:
        """output root와 사용자 덮어쓰기 승인이 원본 보호 규칙을 지키는지 확인합니다."""

        raw_output_root = Path(snapshot.output_root)
        if not raw_output_root.is_absolute():
            raise MergeExportBackendError("구조 통합 출력 경로는 절대 경로여야 합니다.")
        output_root = _resolved_path(raw_output_root, label="구조 통합 출력")
        parent = output_root.parent
        if not parent.is_dir():
            raise MergeExportBackendError("구조 통합 출력의 부모 폴더가 존재하지 않거나 폴더가 아닙니다.")
        if output_root.exists():
            if not snapshot.overwrite_existing_output:
                raise MergeExportBackendError(
                    "구조 통합 출력 폴더가 이미 존재하며 사용자 덮어쓰기 승인이 없습니다."
                )
            if output_root.is_symlink() or not output_root.is_dir():
                raise MergeExportBackendError(
                    "덮어쓸 구조 통합 출력은 심볼릭 링크가 아닌 폴더여야 합니다."
                )
        for source in snapshot.source_models:
            source_root = _resolved_path(Path(source.dataset_root), label="COLMAP Dataset Root")
            if _is_within(output_root, source_root):
                raise MergeExportBackendError(
                    "구조 통합 출력은 원본 COLMAP Dataset Root 또는 그 하위에 만들 수 없습니다."
                )
        return output_root

    def _output_tree_signature(self, output_root: Path) -> _OutputTreeSignature:
        """승인한 기존 출력이 finalize 전까지 바뀌지 않았는지 비교할 메타데이터를 만듭니다."""

        if output_root.is_symlink() or not output_root.is_dir():
            raise MergeExportBackendError("기존 출력은 심볼릭 링크가 아닌 폴더여야 합니다.")
        entries: list[tuple[str, str, int, int]] = []
        try:
            root_stat = output_root.stat()
            entries.append((".", "directory", 0, int(root_stat.st_mtime_ns)))
            descendants = sorted(
                output_root.rglob("*"),
                key=lambda path: path.relative_to(output_root).as_posix(),
            )
            for path in descendants:
                relative = path.relative_to(output_root).as_posix()
                if path.is_symlink():
                    raise MergeExportBackendError(
                        f"기존 출력 안의 심볼릭 링크는 안전하게 덮어쓸 수 없습니다: {relative}"
                    )
                stat = path.stat()
                if path.is_dir():
                    entries.append((relative, "directory", 0, int(stat.st_mtime_ns)))
                elif path.is_file():
                    entries.append(
                        (relative, "file", int(stat.st_size), int(stat.st_mtime_ns))
                    )
                else:
                    raise MergeExportBackendError(
                        f"기존 출력 안에 지원하지 않는 파일 유형이 있습니다: {relative}"
                    )
        except OSError as error:
            raise MergeExportBackendError(
                "덮어쓸 기존 출력 폴더의 상태를 확인하지 못했습니다."
            ) from error
        return tuple(entries)

    def _candidate_root_for(self, job_id: str, output_root: Path) -> Path:
        """output parent 안의 같은 볼륨 전용 후보 경로를 결정적으로 반환합니다."""

        candidate = _resolved_path(
            output_root.parent / f"{MERGE_EXPORT_OUTPUT_CANDIDATE_PREFIX}{job_id}",
            label="구조 통합 후보",
        )
        if candidate.parent != output_root.parent or candidate.name != f"{MERGE_EXPORT_OUTPUT_CANDIDATE_PREFIX}{job_id}":
            raise MergeExportBackendError("구조 통합 후보 경로가 출력 부모의 전용 형제 경로가 아닙니다.")
        return candidate

    def _previous_output_root_for(self, job: MergeExportJob) -> Path:
        """현재 job이 덮어쓸 기존 출력을 잠시 보존할 형제 백업 경로를 반환합니다."""

        output_root = job.snapshot.output_root
        previous = _resolved_path(
            output_root.parent / f"{_PREVIOUS_OUTPUT_PREFIX}{job.job_id}",
            label="기존 구조 통합 출력 백업",
        )
        expected_name = f"{_PREVIOUS_OUTPUT_PREFIX}{job.job_id}"
        if previous.parent != output_root.parent or previous.name != expected_name:
            raise MergeExportBackendError("기존 출력 백업 경로가 허용된 형제 경로가 아닙니다.")
        return previous

    def _create_job_directory(self, job_id: str) -> Path:
        """운영체제 임시 영역에 한 job 전용 작업 폴더를 만듭니다."""

        try:
            return Path(tempfile.mkdtemp(prefix=f"{_JOB_DIRECTORY_PREFIX}{job_id}_"))
        except OSError as error:
            raise MergeExportBackendError("구조 통합 worker 작업 폴더를 만들지 못했습니다.") from error

    def _validate_written_manifest(
        self,
        manifest_path: Path,
        snapshot: MergeExportSnapshot,
        preflight_only: bool,
    ) -> None:
        """시작 전 manifest가 wrapper·snapshot 계약 그대로 기록됐는지 재확인합니다."""

        mapping = _read_json_mapping(
            manifest_path,
            label="구조 통합 manifest",
            required_keys=_MANIFEST_KEYS,
        )
        if not isinstance(mapping["preflight_only"], bool) or mapping["preflight_only"] is not preflight_only:
            raise MergeProtocolError("구조 통합 manifest preflight_only 값이 올바르지 않습니다.")
        parsed = MergeExportSnapshot.from_mapping(mapping["snapshot"])
        if parsed.to_mapping() != snapshot.to_mapping():
            raise MergeProtocolError("구조 통합 manifest snapshot이 시작 입력과 다릅니다.")

    def _blender_binary(self) -> Path:
        """현재 Blender 실행 파일 또는 명시 경로가 실제 파일인지 확인합니다."""

        candidate = self._blender_binary_path
        if candidate is None:
            try:
                import bpy
            except ImportError as error:
                raise MergeExportBackendError("구조 통합 worker에는 Blender Python 환경이 필요합니다.") from error
            candidate = Path(str(bpy.app.binary_path))
        binary = _resolved_path(candidate, label="Blender 실행 파일")
        if not binary.is_file():
            raise MergeExportBackendError("구조 통합 worker용 Blender 실행 파일을 찾을 수 없습니다.")
        return binary

    def _worker_script(self) -> Path:
        """설치된 addon의 전용 worker 진입 스크립트를 반환합니다."""

        worker = _resolved_path(Path(__file__).with_name("worker.py"), label="구조 통합 worker")
        if not worker.is_file():
            raise MergeExportBackendError("구조 통합 worker.py를 찾을 수 없습니다.")
        return worker

    def _launch_worker(self, job_directory: Path, log_handle: TextIO) -> subprocess.Popen[Any]:
        """shell 없이 숨은 Blender background worker를 시작합니다."""

        command = [
            str(self._blender_binary()),
            "--background",
            "--factory-startup",
            "--python",
            str(self._worker_script()),
            "--",
            "--job-dir",
            str(job_directory),
        ]
        environment = os.environ.copy()
        environment["PYTHONNOUSERSITE"] = "1"
        try:
            return subprocess.Popen(
                command,
                cwd=str(job_directory),
                stdin=subprocess.DEVNULL,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                shell=False,
                env=environment,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError as error:
            raise MergeExportBackendError("구조 통합 Blender worker를 시작하지 못했습니다.") from error

    def _read_progress(self, job: MergeExportJob) -> MergeExportProgress | None:
        """실행 중 progress 구문 오류에는 마지막 정상값을 쓰고 의미 오류는 거부합니다."""

        try:
            mapping = _read_progress_json_mapping(job.progress_path)
        except _ProgressReadSyntaxError as error:
            cause = error.__cause__
            if isinstance(cause, FileNotFoundError) and job.last_progress is None:
                return None
            self._record_progress_read_warning(job, error)
            return job.last_progress
        progress = MergeExportProgress.from_mapping(mapping)
        if progress.job_id != job.job_id:
            raise MergeProtocolError("구조 통합 progress job ID가 현재 작업과 다릅니다.")
        if job.last_progress_parse_error_type:
            _LOGGER.info(
                "event=merge_export_progress_read_recovered job_id=%s warning_count=%s",
                job.job_id,
                job.progress_read_warning_count,
            )
            job.last_progress_parse_error_type = ""
        job.last_progress = progress
        return progress

    def _record_progress_read_warning(
        self,
        job: MergeExportJob,
        error: _ProgressReadSyntaxError,
    ) -> None:
        """같은 progress 구문 오류는 한 번만 경고하고 누적 횟수만 보관합니다."""

        cause = error.__cause__
        error_type = type(cause if cause is not None else error).__name__
        signature = f"{error_type}:{error}"
        job.progress_read_warning_count += 1
        job.last_progress_parse_error_type = error_type
        if signature == job.last_progress_warning_signature:
            return
        job.last_progress_warning_signature = signature
        _LOGGER.warning(
            "event=merge_export_progress_read_deferred job_id=%s error_type=%s warning_count=%s",
            job.job_id,
            error_type,
            job.progress_read_warning_count,
        )

    def _read_terminal_result(
        self,
        job: MergeExportJob,
    ) -> tuple[MergeExportResult, MergeExportPreflightResult | None] | None:
        """result/error의 상호 배타성과 job·경로·preflight 계약을 엄격히 확인합니다."""

        has_result = job.result_path.is_file()
        has_error = job.error_path.is_file()
        if has_result and has_error:
            raise MergeProtocolError("구조 통합 worker가 result.json과 error.json을 함께 기록했습니다.")
        if not has_result and not has_error:
            return None
        path = job.result_path if has_result else job.error_path
        label = "구조 통합 result" if has_result else "구조 통합 error"
        mapping = _read_json_mapping(path, label=label, required_keys=_RESULT_KEYS)
        result = MergeExportResult.from_mapping(mapping)
        self._validate_result_identity(job, result)
        if has_error:
            if result.phase not in {MergePhase.FAILED, MergePhase.CANCELLED}:
                raise MergeProtocolError("구조 통합 error.json의 phase는 FAILED 또는 CANCELLED여야 합니다.")
            if result.error is None:
                raise MergeProtocolError("구조 통합 error.json에는 오류 진단이 필요합니다.")
        elif result.phase not in {MergePhase.PREFLIGHT_READY, MergePhase.WAITING_FOR_FINALIZE}:
            raise MergeProtocolError("구조 통합 result.json에는 성공 phase만 기록할 수 있습니다.")
        preflight: MergeExportPreflightResult | None = None
        needs_preflight = result.phase is MergePhase.PREFLIGHT_READY
        has_preflight_failure = (
            job.preflight_only
            and result.phase is MergePhase.FAILED
            and job.preflight_path.is_file()
        )
        if needs_preflight or has_preflight_failure:
            if not job.preflight_only:
                raise MergeProtocolError("전체 내보내기 worker가 PREFLIGHT_READY로 종료했습니다.")
            if not job.preflight_path.is_file():
                raise MergeProtocolError("preflight-only worker가 preflight.json을 기록하지 않았습니다.")
            preflight_mapping = _read_json_mapping(
                job.preflight_path,
                label="구조 통합 preflight",
                required_keys=_PREFLIGHT_KEYS,
            )
            preflight = MergeExportPreflightResult.from_mapping(preflight_mapping)
            if preflight.job_id != job.job_id or preflight.runtime_fingerprint != job.snapshot.runtime_fingerprint:
                raise MergeProtocolError("구조 통합 preflight의 job ID 또는 Runtime fingerprint가 다릅니다.")
        if result.phase is MergePhase.PREFLIGHT_READY:
            if preflight is None:
                raise MergeProtocolError("preflight-only worker가 preflight 결과를 기록하지 않았습니다.")
            if not preflight.is_ready:
                raise MergeProtocolError("준비되지 않은 preflight는 FAILED error.json으로 종료해야 합니다.")
        elif result.phase is MergePhase.WAITING_FOR_FINALIZE:
            if job.preflight_only:
                raise MergeProtocolError("preflight-only worker가 output candidate 성공 결과를 기록했습니다.")
            if not result.is_success:
                raise MergeProtocolError("최종 확정 대기 결과에는 오류가 없어야 합니다.")
        elif result.phase not in {MergePhase.FAILED, MergePhase.CANCELLED}:
            raise MergeProtocolError("구조 통합 worker 결과 phase가 현재 protocol에 없습니다.")
        return result, preflight

    def _validate_result_identity(self, job: MergeExportJob, result: MergeExportResult) -> None:
        """worker 결과가 현재 job·snapshot·전용 candidate만 가리키는지 확인합니다."""

        if result.job_id != job.job_id:
            raise MergeProtocolError("구조 통합 결과 job ID가 현재 작업과 다릅니다.")
        if result.runtime_fingerprint != job.snapshot.runtime_fingerprint:
            raise MergeProtocolError("구조 통합 결과 Runtime fingerprint가 현재 작업과 다릅니다.")
        if _resolved_path(result.output_root, label="구조 통합 결과 출력") != job.snapshot.output_root:
            raise MergeProtocolError("구조 통합 결과 output_root가 현재 작업과 다릅니다.")
        if result.candidate_root is not None and _resolved_path(
            result.candidate_root,
            label="구조 통합 결과 후보",
        ) != job.candidate_root:
            raise MergeProtocolError("구조 통합 결과 candidate_root가 현재 작업과 다릅니다.")
        if result.phase is MergePhase.WAITING_FOR_FINALIZE and result.candidate_root is None:
            raise MergeProtocolError("최종 확정 대기 결과에 candidate_root가 없습니다.")

    def _poll_terminal_result(
        self,
        job: MergeExportJob,
        result: MergeExportResult,
        preflight: MergeExportPreflightResult | None,
        progress: MergeExportProgress | None,
    ) -> MergeExportPollResult:
        """검증된 terminal worker 결과를 UI phase로 변환하고 안전하게 정리합니다."""

        if self._is_process_running(job):
            return MergeExportPollResult(
                phase=(
                    MergePhase.PREFLIGHT_RUNNING
                    if job.preflight_only
                    else MergePhase.EXPORT_RUNNING
                ),
                progress=progress,
            )
        if result.phase is MergePhase.PREFLIGHT_READY:
            return MergeExportPollResult(
                phase=MergePhase.PREFLIGHT_READY,
                progress=progress,
                preflight=preflight,
                result=result,
            )
        if (
            job.preflight_only
            and result.phase is MergePhase.FAILED
            and preflight is not None
            and not preflight.is_ready
        ):
            self._preserve_failure_diagnostic(
                job,
                code=(result.error.code if result.error is not None else "PREFLIGHT_INVALID"),
                cause=(result.error.cause if result.error is not None else "구조 통합 사전 검사에 실패했습니다."),
            )
            return MergeExportPollResult(
                phase=MergePhase.PREFLIGHT_READY,
                progress=progress,
                preflight=preflight,
                result=result,
                error=result.error,
            )
        if result.phase is MergePhase.WAITING_FOR_FINALIZE:
            if not job.candidate_root.is_dir():
                return self._terminal_failure(
                    job,
                    "CANDIDATE_MISSING",
                    "worker가 최종 확정 대기 결과를 기록했지만 후보 폴더가 없습니다.",
                )
            return MergeExportPollResult(
                phase=MergePhase.WAITING_FOR_FINALIZE,
                progress=progress,
                result=result,
            )
        if result.phase is MergePhase.CANCELLED:
            self._preserve_failure_diagnostic(
                job,
                code=(result.error.code if result.error is not None else "EXPORT_CANCELLED"),
                cause=(result.error.cause if result.error is not None else "구조 통합 내보내기를 취소했습니다."),
            )
            self._cleanup_if_worker_stopped(job, remove_candidate=True)
            return MergeExportPollResult(
                phase=MergePhase.CANCELLED,
                progress=progress,
                result=result,
                error=result.error,
            )
        self._preserve_failure_diagnostic(
            job,
            code=(result.error.code if result.error is not None else "EXPORT_WORKER_FAILED"),
            cause=(result.error.cause if result.error is not None else "구조 통합 worker가 실패했습니다."),
        )
        self._cleanup_if_worker_stopped(job, remove_candidate=True)
        return MergeExportPollResult(
            phase=MergePhase.FAILED,
            progress=progress,
            result=result,
            error=result.error,
        )

    def _advance_cancel(self, job: MergeExportJob) -> None:
        """grace를 넘긴 취소 요청만 해당 worker 프로세스 종료로 단계적으로 올립니다."""

        if not self._is_process_running(job) or job.cancel_requested_at is None:
            return
        now = monotonic()
        if job.terminate_requested_at is None and now - job.cancel_requested_at >= _CANCEL_GRACE_SECONDS:
            try:
                job.process.terminate()
                job.terminate_requested_at = now
            except OSError as error:
                raise MergeExportBackendError("취소된 구조 통합 worker를 종료하지 못했습니다.") from error
        elif job.terminate_requested_at is not None and now - job.terminate_requested_at >= _TERMINATE_GRACE_SECONDS:
            try:
                job.process.kill()
            except OSError as error:
                raise MergeExportBackendError("종료되지 않은 구조 통합 worker를 강제 종료하지 못했습니다.") from error

    def _is_process_running(self, job: MergeExportJob) -> bool:
        """Popen의 현재 종료 코드를 읽어 worker가 살아 있는지 반환합니다."""

        try:
            return job.process.poll() is None
        except OSError as error:
            raise MergeExportBackendError("구조 통합 worker 상태를 읽지 못했습니다.") from error

    def _stop_worker_for_protocol_failure(self, job: MergeExportJob) -> None:
        """손상된 worker 출력 뒤에는 프로세스를 회수한 다음에만 후보를 정리합니다."""

        try:
            self._stop_worker(job)
        except MergeExportBackendError:
            _LOGGER.exception("event=merge_export_protocol_worker_stop_failed job_id=%s", job.job_id)

    def _stop_worker(self, job: MergeExportJob) -> None:
        """실행 중인 해당 worker만 terminate·kill 순서로 회수합니다."""

        if not self._is_process_running(job):
            return
        try:
            job.process.terminate()
            job.process.wait(timeout=_PROCESS_WAIT_SECONDS)
            return
        except subprocess.TimeoutExpired:
            pass
        except OSError as error:
            raise MergeExportBackendError("구조 통합 worker를 종료하지 못했습니다.") from error
        try:
            job.process.kill()
            job.process.wait(timeout=_PROCESS_WAIT_SECONDS)
        except subprocess.TimeoutExpired as error:
            raise MergeExportBackendError("구조 통합 worker가 강제 종료 뒤에도 남아 있습니다.") from error
        except OSError as error:
            raise MergeExportBackendError("구조 통합 worker를 강제 종료하지 못했습니다.") from error

    def _diagnostic_file_summary(self, path: Path) -> Mapping[str, Any]:
        """작은 통신 파일의 존재·크기·SHA-256만 실패 진단용으로 반환합니다."""

        try:
            stat = path.stat()
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                while True:
                    chunk = stream.read(64 * 1024)
                    if not chunk:
                        break
                    digest.update(chunk)
            return {
                "exists": True,
                "size_bytes": int(stat.st_size),
                "sha256": digest.hexdigest(),
                "read_error_type": None,
            }
        except OSError as error:
            return {
                "exists": False,
                "size_bytes": None,
                "sha256": None,
                "read_error_type": type(error).__name__,
            }

    def _worker_log_tail(self, job: MergeExportJob) -> str:
        """worker 로그의 마지막 최대 64KB만 UTF-8 대체 디코딩해 반환합니다."""

        try:
            job.worker_log_handle.flush()
        except OSError:
            pass
        try:
            with job.worker_log_path.open("rb") as stream:
                stream.seek(0, os.SEEK_END)
                size = stream.tell()
                stream.seek(max(0, size - _MAX_WORKER_LOG_TAIL_BYTES), os.SEEK_SET)
                payload = stream.read(_MAX_WORKER_LOG_TAIL_BYTES)
        except OSError as error:
            return f"worker log를 읽지 못했습니다: {type(error).__name__}"
        return payload.decode("utf-8", errors="replace")

    def _preserve_failure_diagnostic(
        self,
        job: MergeExportJob,
        *,
        code: str,
        cause: str,
    ) -> Path | None:
        """정리 전에 마지막 실패 하나의 bounded 통신·로그 진단을 원자 기록합니다."""

        progress = job.last_progress
        try:
            return_code = job.process.poll()
        except OSError:
            return_code = None
        payload = {
            "schema_version": 1,
            "job_id": job.job_id,
            "created_at_utc": datetime.now(UTC).isoformat(),
            "failure_code": str(code),
            "cause": str(cause),
            "worker_return_code": return_code,
            "last_progress": (
                None
                if progress is None
                else {
                    "stage": progress.stage.value,
                    "fraction": progress.fraction,
                }
            ),
            "progress_read_warning_count": job.progress_read_warning_count,
            "progress_parse_error_type": job.last_progress_parse_error_type or None,
            "progress_file": self._diagnostic_file_summary(job.progress_path),
            "worker_log_tail": self._worker_log_tail(job),
        }
        target = _failure_diagnostic_path()
        try:
            _write_json_atomically(target, payload)
        except MergeExportBackendError:
            _LOGGER.exception(
                "event=merge_export_failure_diagnostic_write_failed job_id=%s path=%s",
                job.job_id,
                target,
            )
            return None
        return target

    def _remove_failure_diagnostic(self) -> None:
        """정상 Export 확정 뒤 이전 실패 진단 파일 하나를 제거합니다."""

        try:
            _failure_diagnostic_path().unlink(missing_ok=True)
        except OSError:
            _LOGGER.exception(
                "event=merge_export_failure_diagnostic_cleanup_failed path=%s",
                _failure_diagnostic_path(),
            )

    def _validate_candidate_for_finalize(self, job: MergeExportJob, result: MergeExportResult) -> None:
        """rename 직전에 후보·출력 경로와 최소 sparse output 계약을 다시 확인합니다."""

        if result.candidate_root is None or _resolved_path(result.candidate_root, label="최종 후보") != job.candidate_root:
            raise MergeExportBackendError("최종 확정 결과의 후보 경로가 현재 작업과 다릅니다.")
        if (
            not self._is_expected_candidate(job, job.candidate_root)
            or job.candidate_root.is_symlink()
            or not job.candidate_root.is_dir()
        ):
            raise MergeExportBackendError("최종 확정 후보 폴더가 전용 형제 경로가 아니거나 존재하지 않습니다.")
        output_root = job.snapshot.output_root
        if job.existing_output_signature is None:
            if output_root.exists():
                raise MergeExportBackendError(
                    "최종 출력 폴더가 작업 중 새로 생겨 사용자 승인 없이 덮어쓸 수 없습니다."
                )
        else:
            if not job.snapshot.overwrite_existing_output:
                raise MergeExportBackendError("현재 작업에 기존 출력 덮어쓰기 승인이 없습니다.")
            if not output_root.exists():
                raise MergeExportBackendError("승인한 기존 출력 폴더가 작업 중 사라졌습니다.")
            if self._output_tree_signature(output_root) != job.existing_output_signature:
                raise MergeExportBackendError(
                    "승인한 기존 출력 폴더가 작업 중 변경되어 덮어쓰기를 중단했습니다."
                )
        missing = [
            str(relative)
            for relative in _required_candidate_files(job.snapshot.output_format)
            if not (job.candidate_root / relative).is_file()
        ]
        if missing:
            raise MergeExportBackendError(
                "최종 확정 후보에 필수 COLMAP 출력이 없습니다: " + ", ".join(missing)
            )
        invalid_directories = [
            str(relative)
            for relative in _REQUIRED_CANDIDATE_DIRECTORIES
            if not (job.candidate_root / relative).is_dir()
            or (job.candidate_root / relative).is_symlink()
        ]
        if invalid_directories:
            raise MergeExportBackendError(
                "최종 확정 후보에 안전한 필수 COLMAP 폴더가 없습니다: "
                + ", ".join(invalid_directories)
            )

    def _promote_candidate(self, job: MergeExportJob) -> None:
        """검증된 후보를 승격하고, 덮어쓰기일 때 기존 출력을 실패 복구 가능하게 교체합니다."""

        output_root = job.snapshot.output_root
        if job.existing_output_signature is None:
            os.replace(job.candidate_root, output_root)
            return

        previous_root = self._previous_output_root_for(job)
        if previous_root.exists():
            raise MergeExportBackendError("현재 작업의 기존 출력 백업 경로가 이미 존재합니다.")
        os.replace(output_root, previous_root)
        try:
            os.replace(job.candidate_root, output_root)
        except OSError as promotion_error:
            try:
                os.replace(previous_root, output_root)
            except OSError as restore_error:
                raise MergeExportBackendError(
                    "새 출력 승격과 기존 출력 복원에 모두 실패했습니다. "
                    f"기존 결과는 다음 백업 경로에 남아 있습니다: {previous_root}"
                ) from restore_error
            raise MergeExportBackendError(
                "새 출력 승격에 실패해 기존 출력 폴더를 복원했습니다."
            ) from promotion_error

        try:
            self._remove_previous_output(job, previous_root)
        except MergeExportBackendError:
            # 새 출력 확정 뒤 기존 백업 정리 실패는 정상 결과를 되돌리지 않습니다.
            _LOGGER.exception(
                "event=merge_export_previous_output_cleanup_failed job_id=%s backup=%s",
                job.job_id,
                previous_root,
            )

    def _remove_previous_output(self, job: MergeExportJob, previous_root: Path) -> None:
        """사용자가 승인했고 현재 job이 직접 만든 기존 출력 백업만 제거합니다."""

        expected = self._previous_output_root_for(job)
        if _resolved_path(previous_root, label="기존 출력 백업 정리 대상") != expected:
            raise MergeExportBackendError("검증되지 않은 기존 출력 백업은 정리할 수 없습니다.")
        if not previous_root.exists():
            return
        if previous_root.is_symlink() or not previous_root.is_dir():
            raise MergeExportBackendError("기존 출력 백업이 안전한 폴더가 아닙니다.")
        if self._output_tree_signature(previous_root) != job.existing_output_signature:
            raise MergeExportBackendError("기존 출력 백업이 교체 과정에서 변경되어 자동 삭제하지 않습니다.")
        try:
            shutil.rmtree(previous_root)
        except OSError as error:
            raise MergeExportBackendError("교체가 끝난 기존 출력 백업을 정리하지 못했습니다.") from error

    def _is_expected_candidate(self, job: MergeExportJob, path: Path) -> bool:
        """정리 대상이 현재 output parent의 backend 전용 candidate인지 확인합니다."""

        expected = self._candidate_root_for(job.job_id, job.snapshot.output_root)
        return _resolved_path(path, label="후보 정리 대상") == expected

    def _terminal_failure(self, job: MergeExportJob, code: str, cause: str) -> MergeExportPollResult:
        """worker·protocol 실패를 오류 result로 고정하고 종료된 작업만 안전하게 정리합니다."""

        diagnostic_path = self._preserve_failure_diagnostic(job, code=code, cause=cause)
        remedy = (
            f"진단 파일을 확인한 뒤 새 작업으로 다시 실행하십시오: {diagnostic_path}"
            if diagnostic_path is not None
            else "입력 COLMAP 데이터와 출력 경로를 확인한 뒤 새 작업으로 다시 실행하십시오."
        )
        result = self._result_with_issue(
            job,
            phase=MergePhase.FAILED,
            issue=_issue(
                code,
                cause,
                "불완전하거나 검증되지 않은 COLMAP 후보는 최종 출력으로 확정하지 않았습니다.",
                remedy,
            ),
        )
        job.result = result
        self._cleanup_if_worker_stopped(job, remove_candidate=True)
        return MergeExportPollResult(
            phase=MergePhase.FAILED,
            result=result,
            error=result.error,
        )

    def _terminal_cancelled(
        self,
        job: MergeExportJob,
        code: str,
        cause: str,
        *,
        progress: MergeExportProgress | None = None,
    ) -> MergeExportPollResult:
        """취소 요청 뒤 무결과 종료도 사용자 취소로 정리해 실패와 구분합니다."""

        self._preserve_failure_diagnostic(job, code=code, cause=cause)
        result = self._result_with_issue(
            job,
            phase=MergePhase.CANCELLED,
            issue=_issue(
                code,
                cause,
                "정상 출력 폴더는 만들지 않으며 중간 candidate만 정리합니다.",
                "필요하면 입력 상태를 확인한 뒤 새 구조 통합 작업을 시작하십시오.",
            ),
        )
        job.result = result
        self._cleanup_if_worker_stopped(job, remove_candidate=True)
        return MergeExportPollResult(
            phase=MergePhase.CANCELLED,
            progress=progress,
            result=result,
            error=result.error,
        )

    def _failed_poll(self, job: MergeExportJob, code: str, cause: str) -> MergeExportPollResult:
        """이미 정리된 handle에도 UI가 일관된 실패 결과를 읽을 수 있게 합니다."""

        result = self._result_with_issue(
            job,
            phase=MergePhase.FAILED,
            issue=_issue(
                code,
                cause,
                "현재 job의 출력 상태를 더 이상 신뢰할 수 없습니다.",
                "새 구조 통합 작업을 시작하십시오.",
            ),
        )
        return MergeExportPollResult(phase=MergePhase.FAILED, result=result, error=result.error)

    def _finalize_failure(self, job: MergeExportJob, code: str, cause: str) -> MergeExportResult:
        """finalize 실패에서도 candidate와 job folder를 남기지 않는 terminal result를 만듭니다."""

        self._stop_worker_for_protocol_failure(job)
        diagnostic_path = self._preserve_failure_diagnostic(job, code=code, cause=cause)
        remedy = (
            f"진단 파일을 확인한 뒤 새 작업으로 다시 실행하십시오: {diagnostic_path}"
            if diagnostic_path is not None
            else "출력 경로를 확인한 뒤 새 작업으로 다시 실행하십시오."
        )
        failed = self._result_with_issue(
            job,
            phase=MergePhase.FAILED,
            issue=_issue(
                code,
                cause,
                "후보 출력은 최종 COLMAP 폴더로 승격되지 않았습니다.",
                remedy,
            ),
        )
        self._cleanup_if_worker_stopped(job, remove_candidate=True)
        return failed

    def _result_with_issue(
        self,
        job: MergeExportJob,
        *,
        phase: MergePhase,
        issue: MergeIssue,
        warnings: tuple[MergeIssue, ...] = (),
        counts: Mapping[str, int] | None = None,
    ) -> MergeExportResult:
        """backend 자체 terminal 결과도 worker 결과와 같은 작은 타입으로 반환합니다."""

        return MergeExportResult(
            job_id=job.job_id,
            phase=phase,
            runtime_fingerprint=job.snapshot.runtime_fingerprint,
            candidate_root=None,
            output_root=job.snapshot.output_root,
            counts={} if counts is None else counts,
            warnings=warnings,
            error=issue,
        )

    def _cleanup_if_worker_stopped(self, job: MergeExportJob, *, remove_candidate: bool) -> None:
        """실행이 끝난 경우에만 로그 handle·전용 후보·job 폴더를 정리합니다."""

        if job.cleaned or self._is_process_running(job):
            return
        self._cleanup_terminal_job(job, remove_candidate=remove_candidate)

    def _cleanup_terminal_job(self, job: MergeExportJob, *, remove_candidate: bool) -> None:
        """종료된 job의 안전한 경로만 지우며 최종 output root는 절대 건드리지 않습니다."""

        if job.cleaned:
            return
        if self._is_process_running(job):
            raise MergeExportBackendError("실행 중인 구조 통합 worker의 파일은 정리할 수 없습니다.")
        try:
            job.worker_log_handle.close()
        except OSError:
            pass
        if remove_candidate:
            self._remove_candidate(job)
        self._remove_job_directory(job.job_directory)
        job.cleaned = True

    def _remove_candidate(self, job: MergeExportJob) -> None:
        """현재 job의 예상 candidate만 재귀 삭제해 실패·취소 부분 출력을 제거합니다."""

        candidate = job.candidate_root
        if not candidate.exists():
            return
        if not self._is_expected_candidate(job, candidate):
            raise MergeExportBackendError("검증되지 않은 후보 폴더는 정리할 수 없습니다.")
        try:
            if candidate.is_symlink():
                candidate.unlink()
            elif candidate.is_dir():
                shutil.rmtree(candidate)
            else:
                raise MergeExportBackendError("구조 통합 후보 경로가 폴더가 아닙니다.")
        except OSError as error:
            raise MergeExportBackendError("구조 통합 후보 폴더를 정리하지 못했습니다.") from error

    def _remove_job_directory(self, job_directory: Path) -> None:
        """mkdtemp로 만든 전용 job directory만 정리합니다."""

        if not job_directory.exists():
            return
        if not job_directory.name.startswith(_JOB_DIRECTORY_PREFIX):
            raise MergeExportBackendError("검증되지 않은 구조 통합 작업 폴더는 정리할 수 없습니다.")
        try:
            shutil.rmtree(job_directory)
        except OSError as error:
            raise MergeExportBackendError("구조 통합 worker 작업 폴더를 정리하지 못했습니다.") from error

    def _require_job(self, job: MergeExportJob) -> None:
        """다른 backend나 임의 객체가 cleanup 경로에 들어오는 일을 막습니다."""

        if not isinstance(job, MergeExportJob):
            raise TypeError("구조 통합 backend에는 MergeExportJob이 필요합니다.")
        if job.backend_token is not self._token:
            raise MergeExportBackendError("다른 backend가 만든 구조 통합 job은 사용할 수 없습니다.")


__all__ = (
    "MergeExportBackend",
    "MergeExportBackendError",
    "MergeExportJob",
)
