"""COLMAP Export Transaction을 UI tick 사이에서 재진입 가능하게 실행합니다."""

from __future__ import annotations

import math
import tempfile
import time
from dataclasses import replace
from pathlib import Path

from . import export_transaction as transaction
from .export_progress import ExportPhase, ExportProgressSnapshot
from .export_types import (
    BackupResult,
    ExportPlan,
    ExportResult,
    ExportTransactionError,
    SourceManifest,
)
from .model import LoadedReconstruction, Reconstruction
from .export_staging_jobs import StagingReadJob, StagingWriteJob
from .export_integrity_job import ReconstructionValidationJob
from .export_validation import LayoutManifestJob

_PHASE_RANGES: dict[ExportPhase, tuple[float, float]] = {
    ExportPhase.CREATED: (0.0, 0.0),
    ExportPhase.PREPARE: (0.0, 0.01),
    ExportPhase.COPY_SOURCE: (0.01, 0.02),
    ExportPhase.WRITE_STAGING: (0.02, 0.26),
    ExportPhase.PARSE_STAGING: (0.26, 0.65),
    ExportPhase.VALIDATE_STAGING: (0.65, 0.92),
    ExportPhase.COMPARE_STAGING: (0.92, 0.97),
    ExportPhase.HASH_STAGING: (0.97, 0.99),
    ExportPhase.COMMIT: (0.99, 0.995),
    ExportPhase.BUILD_RESULT: (0.995, 0.998),
    ExportPhase.CLEANUP: (0.998, 1.0),
    ExportPhase.DONE: (1.0, 1.0),
    ExportPhase.FAILED: (0.0, 1.0),
}

_PHASE_MESSAGES: dict[ExportPhase, str] = {
    ExportPhase.CREATED: "Export 작업 생성 완료",
    ExportPhase.PREPARE: "Export 경로와 Runtime 상태 확인 중",
    ExportPhase.COPY_SOURCE: "원본 rollback 자료 복사 중",
    ExportPhase.WRITE_STAGING: "Staging COLMAP 파일 작성 중",
    ExportPhase.PARSE_STAGING: "Staging COLMAP 파일 해석 중",
    ExportPhase.VALIDATE_STAGING: "Staging Reconstruction 무결성 검사 중",
    ExportPhase.COMPARE_STAGING: "Runtime과 Staging 데이터 비교 중",
    ExportPhase.HASH_STAGING: "Staging 파일 SHA-256 계산 중",
    ExportPhase.COMMIT: "검증된 Staging 파일로 원본 교체 중",
    ExportPhase.BUILD_RESULT: "Export 결과 구성 중",
    ExportPhase.CLEANUP: "Export 임시 자료 정리 중",
    ExportPhase.DONE: "COLMAP Export 완료",
    ExportPhase.FAILED: "COLMAP Export 실패",
}

_COMPARE_FIELDS = ("cameras", "images", "points3d", "rigs", "frames")


class ExportTransactionJob:
    """기존 Transaction 안전 계약을 단계별 `advance()` 호출로 실행합니다."""

    def __init__(
        self,
        plan: ExportPlan,
        reconstruction: Reconstruction,
    ) -> None:
        if not isinstance(plan, ExportPlan):
            raise ValueError("유효한 ExportPlan이 필요합니다.")
        self._plan = plan
        self._reconstruction = reconstruction
        self._phase = ExportPhase.CREATED
        self._started_at = time.monotonic()
        self._progress = self._snapshot(
            ExportPhase.CREATED,
            phase_fraction=0.0,
            completed_units=0,
            total_units=0,
        )
        self._result: ExportResult | None = None
        self._error: ExportTransactionError | None = None
        self._stage = "validation"
        self._transaction_directory: Path | None = None
        self._staging_directory: Path | None = None
        self._restore_directory: Path | None = None
        self._rollback_directory: Path | None = None
        self._rollback_files: tuple[tuple[Path, Path], ...] = ()
        self._backup_result: BackupResult | None = None
        self._staging_model: Reconstruction | None = None
        self._staging_manifest: SourceManifest | None = None
        self._staging_warnings: tuple[str, ...] = ()
        self._loaded: LoadedReconstruction | None = None
        self._replaced: list[Path] = []
        self._removed: list[Path] = []
        self._cleanup_warnings: list[str] = []
        self._write_job: StagingWriteJob | None = None
        self._read_job: StagingReadJob | None = None
        self._validation_job: ReconstructionValidationJob | None = None
        self._compare_job: _ReconstructionEqualityJob | None = None
        self._manifest_job: LayoutManifestJob | None = None

    @property
    def done(self) -> bool:
        return self._phase is ExportPhase.DONE

    @property
    def failed(self) -> bool:
        return self._phase is ExportPhase.FAILED

    @property
    def progress(self) -> ExportProgressSnapshot:
        return self._progress

    @property
    def result(self) -> ExportResult:
        if not self.done or self._result is None:
            raise RuntimeError("완료되지 않은 Export Job의 결과를 읽을 수 없습니다.")
        return self._result

    @property
    def error(self) -> ExportTransactionError | None:
        return self._error

    def advance(
        self,
        work_budget_ms: float | None = None,
    ) -> ExportProgressSnapshot:
        """다음 안전 경계까지 실행하고 현재 진행 상태를 반환합니다."""

        self._validate_budget(work_budget_ms)
        if self.done:
            raise RuntimeError("완료된 Export Job을 다시 실행할 수 없습니다.")
        if self.failed:
            raise RuntimeError("실패한 Export Job을 다시 실행할 수 없습니다.")

        try:
            self._advance_current_phase(work_budget_ms)
        except Exception as exc:
            try:
                self._close_incremental_resources()
            except Exception:
                # 원래 Export 실패 원인과 rollback 정보를 우선 보존합니다.
                pass
            error = self._convert_failure(exc)
            self._mark_failed(error)
            if error is exc:
                raise
            raise error from exc
        return self._progress

    def abort(self) -> None:
        """Blender 강제 종료 경계에서 미완료 Transaction을 안전하게 정리합니다.

        정상 사용자 취소 기능이 아니라 Operator cancel, Add-on unregister와
        파일 load 같은 생명주기 경계용입니다. 여러 번 호출해도 추가 작업을
        수행하지 않습니다. 이미 commit한 파일이 있으면 기존 rollback 계약으로
        원본을 복원합니다.
        """

        if self.done or self.failed:
            return
        self._close_incremental_resources()
        self._stage = "abort"
        error = self._convert_failure(
            RuntimeError("완료되지 않은 Export Job이 강제 종료되었습니다.")
        )
        self._mark_failed(error)
        if error.source_changed:
            raise error

    def _close_incremental_resources(self) -> None:
        if self._write_job is not None:
            self._write_job.close()
        if self._read_job is not None:
            self._read_job.close()
        if self._manifest_job is not None:
            self._manifest_job.close()

    @staticmethod
    def _validate_budget(work_budget_ms: float | None) -> None:
        if work_budget_ms is None:
            return
        if (
            isinstance(work_budget_ms, bool)
            or not isinstance(work_budget_ms, (int, float))
            or not math.isfinite(work_budget_ms)
            or work_budget_ms <= 0.0
        ):
            raise ValueError("Export 작업 budget은 0보다 큰 유한한 수치여야 합니다.")

    def _elapsed_seconds(self) -> float:
        return max(0.0, time.monotonic() - self._started_at)

    def _mark_failed(self, error: ExportTransactionError) -> None:
        self._error = error
        previous_fraction = self._progress.overall_fraction
        self._phase = ExportPhase.FAILED
        self._progress = ExportProgressSnapshot(
            phase=ExportPhase.FAILED,
            overall_fraction=previous_fraction,
            phase_fraction=0.0,
            completed_units=0,
            total_units=0,
            message=_PHASE_MESSAGES[ExportPhase.FAILED],
            elapsed_seconds=self._elapsed_seconds(),
            can_cancel=False,
        )

    def _snapshot(
        self,
        phase: ExportPhase,
        *,
        phase_fraction: float,
        completed_units: int,
        total_units: int,
    ) -> ExportProgressSnapshot:
        start, end = _PHASE_RANGES[phase]
        overall = start + ((end - start) * phase_fraction)
        if hasattr(self, "_progress"):
            overall = max(overall, self._progress.overall_fraction)
        return ExportProgressSnapshot(
            phase=phase,
            overall_fraction=overall,
            phase_fraction=phase_fraction,
            completed_units=completed_units,
            total_units=total_units,
            message=_PHASE_MESSAGES[phase],
            elapsed_seconds=self._elapsed_seconds(),
            can_cancel=False,
        )

    def _enter_phase(
        self,
        phase: ExportPhase,
        *,
        completed_units: int = 0,
        total_units: int = 0,
        phase_fraction: float = 0.0,
    ) -> None:
        self._phase = phase
        self._progress = self._snapshot(
            phase,
            phase_fraction=phase_fraction,
            completed_units=completed_units,
            total_units=total_units,
        )

    def _advance_current_phase(self, work_budget_ms: float | None) -> None:
        if self._phase is ExportPhase.CREATED:
            self._enter_phase(ExportPhase.PREPARE)
            return
        if self._phase is ExportPhase.PREPARE:
            self._prepare()
            self._enter_phase(
                ExportPhase.COPY_SOURCE,
                total_units=len(self._plan.affected_paths),
            )
            return
        if self._phase is ExportPhase.COPY_SOURCE:
            self._copy_source()
            self._enter_phase(
                ExportPhase.WRITE_STAGING,
                total_units=self._model_work_units(),
            )
            return
        if self._phase is ExportPhase.WRITE_STAGING:
            self._write_staging_step(work_budget_ms)
            return
        if self._phase is ExportPhase.PARSE_STAGING:
            self._parse_staging_step(work_budget_ms)
            return
        if self._phase is ExportPhase.VALIDATE_STAGING:
            self._validate_staging_step(work_budget_ms)
            return
        if self._phase is ExportPhase.COMPARE_STAGING:
            self._compare_staging_step(work_budget_ms)
            return
        if self._phase is ExportPhase.HASH_STAGING:
            self._hash_staging_step(work_budget_ms)
            return
        if self._phase is ExportPhase.COMMIT:
            self._commit()
            self._enter_phase(ExportPhase.BUILD_RESULT)
            return
        if self._phase is ExportPhase.BUILD_RESULT:
            self._build_result()
            self._enter_phase(ExportPhase.CLEANUP)
            return
        if self._phase is ExportPhase.CLEANUP:
            self._cleanup_success()
            self._enter_phase(
                ExportPhase.DONE,
                completed_units=1,
                total_units=1,
                phase_fraction=1.0,
            )
            return
        raise RuntimeError(f"실행할 수 없는 Export 단계입니다: {self._phase.name}")

    def _prepare(self) -> None:
        transaction.assert_runtime_token_matches(
            self._plan.runtime_token,
            self._plan.source,
            self._reconstruction,
        )
        self._transaction_directory = Path(
            tempfile.mkdtemp(
                prefix=".blender_colmap_export_",
                dir=self._plan.transaction_parent,
            )
        )
        self._staging_directory = self._transaction_directory / "staging"
        self._restore_directory = self._transaction_directory / "restore"
        self._staging_directory.mkdir()
        self._restore_directory.mkdir()

    def _copy_source(self) -> None:
        if self._transaction_directory is None:
            raise RuntimeError("Export transaction 경로가 준비되지 않았습니다.")
        if self._plan.backup_enabled:
            self._stage = "backup"
            self._backup_result = transaction.create_backup_snapshot(self._plan)
            self._rollback_files = self._backup_result.rollback_files
            return
        self._stage = "rollback preparation"
        self._rollback_directory = self._transaction_directory / "rollback"
        self._rollback_directory.mkdir()
        self._rollback_files = transaction._prepare_rollback(
            self._rollback_directory,
            self._plan,
        )

    def _write_staging_step(self, work_budget_ms: float | None) -> None:
        self._stage = "staging"
        if self._staging_directory is None:
            raise RuntimeError("Staging 경로가 준비되지 않았습니다.")
        if self._write_job is None:
            write_job = transaction._write_staging(
                self._staging_directory,
                self._plan,
                self._reconstruction,
                incremental=True,
            )
            if not isinstance(write_job, StagingWriteJob):
                raise RuntimeError("증분 Staging Writer Job을 만들 수 없습니다.")
            self._write_job = write_job
        self._write_job.advance(work_budget_ms)
        if self._write_job.done:
            self._enter_phase(
                ExportPhase.PARSE_STAGING,
                total_units=len(self._plan.target_relative_paths),
            )
            return
        total = self._write_job.total_units
        completed = self._write_job.completed_units
        self._enter_phase(
            ExportPhase.WRITE_STAGING,
            completed_units=completed,
            total_units=total,
            phase_fraction=completed / total if total else 1.0,
        )

    def _parse_staging_step(self, work_budget_ms: float | None) -> None:
        if self._staging_directory is None:
            raise RuntimeError("Staging 경로가 준비되지 않았습니다.")
        if self._read_job is None:
            read_job = transaction._read_model(
                self._staging_directory,
                self._plan.output_format,
                self._plan.source_layout.layout,
                incremental=True,
            )
            if not isinstance(read_job, StagingReadJob):
                raise RuntimeError("증분 Staging Parser Job을 만들 수 없습니다.")
            self._read_job = read_job
        self._read_job.advance(work_budget_ms)
        if self._read_job.done:
            self._staging_model = self._read_job.result
            self._enter_phase(
                ExportPhase.VALIDATE_STAGING,
                total_units=self._model_work_units(),
            )
            return
        total = self._read_job.total_units
        completed = self._read_job.completed_units
        self._enter_phase(
            ExportPhase.PARSE_STAGING,
            completed_units=completed,
            total_units=total,
            phase_fraction=completed / total if total else 1.0,
        )

    def _validate_staging_step(self, work_budget_ms: float | None) -> None:
        if self._staging_model is None:
            raise RuntimeError("해석된 Staging Reconstruction이 없습니다.")
        if self._validation_job is None:
            validation_job = transaction.validate_reconstruction(
                self._staging_model,
                incremental=True,
            )
            if not isinstance(validation_job, ReconstructionValidationJob):
                raise RuntimeError("증분 무결성 검사 Job을 만들 수 없습니다.")
            self._validation_job = validation_job
        self._validation_job.advance(work_budget_ms)
        if self._validation_job.done:
            self._staging_warnings = self._validation_job.result
            self._enter_phase(
                ExportPhase.COMPARE_STAGING,
                total_units=max(
                    1,
                    sum(self._plan.runtime_token.model_counts),
                ),
            )
            return
        self._enter_phase(
            ExportPhase.VALIDATE_STAGING,
            completed_units=self._validation_job.completed_units,
            total_units=self._validation_job.total_units,
            phase_fraction=self._validation_job.fraction,
        )

    def _compare_staging_step(self, work_budget_ms: float | None) -> None:
        if self._staging_model is None:
            raise RuntimeError("비교할 Staging Reconstruction이 없습니다.")
        if self._compare_job is None:
            self._compare_job = _ReconstructionEqualityJob(
                self._staging_model,
                self._reconstruction,
            )
        self._compare_job.advance(work_budget_ms)
        if self._compare_job.done:
            self._enter_phase(
                ExportPhase.HASH_STAGING,
                total_units=self._staging_total_bytes(),
            )
            return
        total = self._compare_job.total_units
        completed = self._compare_job.completed_units
        self._enter_phase(
            ExportPhase.COMPARE_STAGING,
            completed_units=completed,
            total_units=total,
            phase_fraction=completed / total if total else 1.0,
        )

    def _hash_staging_step(self, work_budget_ms: float | None) -> None:
        if self._staging_directory is None:
            raise RuntimeError("Staging 경로가 준비되지 않았습니다.")
        if self._manifest_job is None:
            manifest_job = transaction.capture_layout_manifest(
                transaction._layout_in(self._staging_directory, self._plan),
                incremental=True,
            )
            if not isinstance(manifest_job, LayoutManifestJob):
                raise RuntimeError("증분 Staging manifest Job을 만들 수 없습니다.")
            self._manifest_job = manifest_job
        self._manifest_job.advance(work_budget_ms)
        if self._manifest_job.done:
            self._staging_manifest = self._manifest_job.result
            self._enter_phase(
                ExportPhase.COMMIT,
                total_units=len(self._plan.target_relative_paths),
            )
            return
        total = self._manifest_job.total_bytes
        completed = self._manifest_job.completed_bytes
        self._enter_phase(
            ExportPhase.HASH_STAGING,
            completed_units=completed,
            total_units=total,
            phase_fraction=completed / total if total else 1.0,
        )

    def _commit(self) -> None:
        self._stage = "commit"
        if self._staging_directory is None:
            raise RuntimeError("Staging 경로가 준비되지 않았습니다.")
        # Modal tick 사이에 Runtime 객체의 identity나 레코드 수가 바뀌었으면
        # 단 하나의 원본 파일도 교체하기 전에 stale staging을 거부합니다.
        transaction.assert_runtime_token_matches(
            self._plan.runtime_token,
            self._plan.source,
            self._reconstruction,
        )
        # 부분 교체 상태를 UI tick 사이에 노출하지 않도록 commit은 분할하지 않습니다.
        for destination, relative_path in zip(
            self._plan.target_paths,
            self._plan.target_relative_paths,
            strict=True,
        ):
            transaction._replace_staged_file(
                self._staging_directory / relative_path,
                destination,
            )
            self._replaced.append(destination)
        for path in self._plan.obsolete_paths:
            if not path.exists():
                continue
            transaction._remove_obsolete_file(path)
            self._removed.append(path)

    def _build_result(self) -> None:
        if self._staging_manifest is None:
            raise ValueError("검증된 staging manifest를 확인할 수 없습니다.")
        exported_source = replace(
            self._plan.source,
            input_format=self._plan.output_format,
            selected_files=self._plan.target_paths,
            source_fingerprint=self._staging_manifest.source_fingerprint,
        )
        self._loaded = LoadedReconstruction(
            reconstruction=self._reconstruction,
            source=exported_source,
            warnings=self._staging_warnings,
        )

    def _cleanup_success(self) -> None:
        if (
            self._transaction_directory is not None
            and self._transaction_directory.exists()
        ):
            try:
                transaction._cleanup_transaction_directory(
                    self._transaction_directory
                )
            except OSError as exc:
                self._cleanup_warnings.append(
                    "Export는 성공했지만 임시 transaction 경로를 정리하지 "
                    f"못했습니다: {self._transaction_directory} ({exc})"
                )
        if self._loaded is None:
            raise RuntimeError("Export 성공 결과를 구성할 수 없습니다.")
        self._result = ExportResult(
            source_fingerprint=self._loaded.source.source_fingerprint,
            backup_snapshot=(
                self._backup_result.snapshot_path
                if self._backup_result is not None
                else None
            ),
            loaded=self._loaded,
            replaced_files=tuple(self._replaced),
            removed_files=tuple(self._removed),
            cleanup_warnings=tuple(self._cleanup_warnings),
        )

    def _model_work_units(self) -> int:
        # 진행 표시를 위해 Point2D·Track 전체를 다시 순회하지 않습니다.
        return max(1, sum(self._plan.runtime_token.model_counts))

    def _staging_total_bytes(self) -> int:
        if self._staging_directory is None:
            return 0
        return sum(
            (self._staging_directory / relative_path).stat().st_size
            for relative_path in self._plan.target_relative_paths
            if (self._staging_directory / relative_path).is_file()
        )

    def _convert_failure(self, exc: Exception) -> ExportTransactionError:
        if isinstance(exc, ExportTransactionError):
            return exc
        replaced_tuple = tuple(self._replaced)
        removed_tuple = tuple(self._removed)
        backup_snapshot = (
            self._backup_result.snapshot_path
            if self._backup_result is not None
            else None
        )
        if replaced_tuple or removed_tuple:
            if (
                self._restore_directory is None
                or self._transaction_directory is None
                or not self._rollback_files
            ):
                return ExportTransactionError(
                    f"Export {self._stage} 실패 뒤 자동 rollback 자료를 확인할 수 "
                    f"없습니다: {exc}",
                    stage=self._stage,
                    source_changed=True,
                    replaced_files=replaced_tuple,
                    removed_files=removed_tuple,
                    rollback_attempted=False,
                    rollback_succeeded=False,
                    backup_snapshot=backup_snapshot,
                    recovery_directory=self._transaction_directory,
                )
            try:
                transaction._rollback_export_changes(
                    self._plan,
                    self._restore_directory,
                    replaced_tuple,
                    self._rollback_files,
                )
            except Exception as rollback_exc:
                return ExportTransactionError(
                    f"Export {self._stage} 실패 후 rollback도 실패했습니다: "
                    f"{rollback_exc}",
                    stage=self._stage,
                    source_changed=True,
                    replaced_files=replaced_tuple,
                    removed_files=removed_tuple,
                    rollback_attempted=True,
                    rollback_succeeded=False,
                    backup_snapshot=backup_snapshot,
                    recovery_directory=self._transaction_directory,
                )
            recovery_directory = transaction._cleanup_after_failure(
                self._transaction_directory
            )
            return ExportTransactionError(
                f"Export {self._stage}에 실패했으며 원본 source를 rollback했습니다: "
                f"{exc}",
                stage=self._stage,
                source_changed=False,
                replaced_files=replaced_tuple,
                removed_files=removed_tuple,
                rollback_attempted=True,
                rollback_succeeded=True,
                backup_snapshot=backup_snapshot,
                recovery_directory=recovery_directory,
            )

        recovery_directory = transaction._cleanup_after_failure(
            self._transaction_directory
        )
        return ExportTransactionError(
            f"Export {self._stage}에 실패했으며 source 파일은 교체되지 않았습니다: "
            f"{exc}",
            stage=self._stage,
            source_changed=False,
            replaced_files=(),
            rollback_attempted=False,
            rollback_succeeded=None,
            backup_snapshot=backup_snapshot,
            recovery_directory=recovery_directory,
        )


class _ReconstructionEqualityJob:
    """Reconstruction dict equality를 레코드 단위 budget으로 확인합니다."""

    def __init__(
        self,
        staging: Reconstruction,
        expected: Reconstruction,
    ) -> None:
        self._staging = staging
        self._expected = expected
        self._field_index = 0
        self._iterator = None
        self._current_records = None
        self._inner_index = 0
        self._completed_units = 0
        self._total_units = max(
            1,
            sum(
                len(getattr(staging, field_name))
                for field_name in _COMPARE_FIELDS
            )
            + sum(len(image.points2d) for image in staging.images.values())
            + sum(len(point.track) for point in staging.points3d.values()),
        )
        self._done = False

    @property
    def done(self) -> bool:
        return self._done

    @property
    def completed_units(self) -> int:
        return self._total_units if self._done else self._completed_units

    @property
    def total_units(self) -> int:
        return self._total_units

    def advance(self, work_budget_ms: float | None) -> None:
        if self._done:
            raise RuntimeError("완료된 Reconstruction 비교 Job을 다시 실행할 수 없습니다.")
        deadline = (
            None
            if work_budget_ms is None
            else time.perf_counter() + (work_budget_ms / 1000.0)
        )
        processed = 0
        while not self._done:
            if self._current_records is not None:
                staging_record, expected_record = self._current_records
                field_name = _COMPARE_FIELDS[self._field_index]
                staging_values = (
                    staging_record.points2d
                    if field_name == "images"
                    else staging_record.track
                )
                expected_values = (
                    expected_record.points2d
                    if field_name == "images"
                    else expected_record.track
                )
                end = min(self._inner_index + 2_048, len(staging_values))
                if staging_values[self._inner_index:end] != expected_values[
                    self._inner_index:end
                ]:
                    raise ValueError(
                        "Staging을 재읽은 결과가 Export Reconstruction과 다릅니다."
                    )
                count = end - self._inner_index
                self._inner_index = end
                self._completed_units += count
                processed += count
                if self._inner_index == len(staging_values):
                    self._current_records = None
                    self._inner_index = 0
                    self._completed_units += 1
                    processed += 1
                if (
                    deadline is not None
                    and processed > 0
                    and time.perf_counter() >= deadline
                ):
                    break
                continue

            if self._iterator is None:
                if self._field_index == len(_COMPARE_FIELDS):
                    self._done = True
                    break
                field_name = _COMPARE_FIELDS[self._field_index]
                staging_mapping = getattr(self._staging, field_name)
                expected_mapping = getattr(self._expected, field_name)
                if len(staging_mapping) != len(expected_mapping):
                    raise ValueError(
                        "Staging을 재읽은 결과가 Export Reconstruction과 다릅니다."
                    )
                self._iterator = iter(staging_mapping.items())

            try:
                key, record = next(self._iterator)
            except StopIteration:
                self._iterator = None
                self._field_index += 1
                continue

            field_name = _COMPARE_FIELDS[self._field_index]
            expected_mapping = getattr(self._expected, field_name)
            if key not in expected_mapping:
                raise ValueError(
                    "Staging을 재읽은 결과가 Export Reconstruction과 다릅니다."
                )
            expected_record = expected_mapping[key]
            if field_name == "images":
                if (
                    record.image_id,
                    record.qvec,
                    record.tvec,
                    record.camera_id,
                    record.name,
                    len(record.points2d),
                ) != (
                    expected_record.image_id,
                    expected_record.qvec,
                    expected_record.tvec,
                    expected_record.camera_id,
                    expected_record.name,
                    len(expected_record.points2d),
                ):
                    raise ValueError(
                        "Staging을 재읽은 결과가 Export Reconstruction과 다릅니다."
                    )
                self._current_records = (record, expected_record)
            elif field_name == "points3d":
                if (
                    record.point3d_id,
                    record.xyz,
                    record.rgb,
                    record.error,
                    len(record.track),
                ) != (
                    expected_record.point3d_id,
                    expected_record.xyz,
                    expected_record.rgb,
                    expected_record.error,
                    len(expected_record.track),
                ):
                    raise ValueError(
                        "Staging을 재읽은 결과가 Export Reconstruction과 다릅니다."
                    )
                self._current_records = (record, expected_record)
            else:
                if expected_record != record:
                    raise ValueError(
                        "Staging을 재읽은 결과가 Export Reconstruction과 다릅니다."
                    )
                self._completed_units += 1
                processed += 1
            if (
                deadline is not None
                and processed > 0
                and time.perf_counter() >= deadline
            ):
                break
