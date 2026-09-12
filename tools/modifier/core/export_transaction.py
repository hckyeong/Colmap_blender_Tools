"""검증된 staging, 파일별 교체와 역순 rollback으로 COLMAP source를 갱신합니다."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from .binary_io import read_model_binary
from .binary_writer import _write_model_binary_validated
from .export_backup import create_backup_snapshot
from .export_types import ExportPlan, ExportResult, SourceManifest
from .export_validation import (
    assert_runtime_token_matches,
    capture_layout_manifest,
)
from .integrity import validate_reconstruction as _validate_reconstruction
from .model import Reconstruction, ResolvedLayout
from .text_io import read_model_text
from .text_writer import _write_model_text_validated
from .export_staging_jobs import StagingReadJob, StagingWriteJob
from .export_integrity_job import ReconstructionValidationJob


def validate_reconstruction(
    model: Reconstruction,
    *,
    incremental: bool = False,
) -> tuple[str, ...] | ReconstructionValidationJob:
    """기존 동기 검사와 Export 전용 증분 검사의 patch 경계를 공유합니다."""

    if incremental:
        return ReconstructionValidationJob(model)
    return _validate_reconstruction(model)


def _read_model(
    directory: Path,
    input_format: str,
    layout: str,
    *,
    incremental: bool = False,
) -> Reconstruction | StagingReadJob:
    modern = layout == "modern"
    if incremental:
        return StagingReadJob(
            directory,
            input_format=input_format,
            modern=modern,
        )
    if input_format == "binary":
        return read_model_binary(directory, modern)
    return read_model_text(directory, modern)


def _layout_in(directory: Path, plan: ExportPlan) -> ResolvedLayout:
    return ResolvedLayout(
        model_directory=directory,
        input_format=plan.output_format,
        layout=plan.source_layout.layout,
        selected_files=tuple(
            directory / relative_path for relative_path in plan.target_relative_paths
        ),
    )


def _copy_rollback_file(source: Path, destination: Path) -> None:
    """테스트에서 rollback 원본 확보 실패를 주입할 수 있는 경계입니다."""

    shutil.copy2(source, destination)


def _replace_staged_file(source: Path, destination: Path) -> None:
    """테스트에서 각 commit 지점 실패를 주입할 수 있는 경계입니다."""

    os.replace(source, destination)


def _restore_original_file(
    rollback_source: Path,
    destination: Path,
    restore_directory: Path,
) -> None:
    """rollback 자료를 소비하지 않고 복원 후보를 만든 뒤 원자 교체합니다."""

    restore_candidate = restore_directory / f"{destination.name}.restore"
    if restore_candidate.exists():
        restore_candidate.unlink()
    shutil.copyfile(rollback_source, restore_candidate)
    os.replace(restore_candidate, destination)


def _cleanup_transaction_directory(path: Path) -> None:
    """성공 후 정리 실패를 테스트할 수 있는 transaction 정리 경계입니다."""

    shutil.rmtree(path)


def _write_staging(
    directory: Path,
    plan: ExportPlan,
    model: Reconstruction,
    *,
    incremental: bool = False,
) -> StagingWriteJob | None:
    modern = plan.source_layout.layout == "modern"
    if incremental:
        return StagingWriteJob(
            directory,
            model,
            input_format=plan.output_format,
            modern=modern,
        )
    if plan.output_format == "binary":
        paths = _write_model_binary_validated(directory, model, modern)
    else:
        paths = _write_model_text_validated(directory, model, modern)
    for path in paths:
        with path.open("rb+") as stream:
            os.fsync(stream.fileno())
    return None


def _prepare_rollback(
    rollback_directory: Path,
    plan: ExportPlan,
) -> tuple[tuple[Path, Path], ...]:
    copied = []
    for source_path in plan.affected_paths:
        if not source_path.exists():
            continue
        destination = rollback_directory / source_path.name
        _copy_rollback_file(source_path, destination)
        copied.append((source_path, destination))
    return tuple(copied)


def _prepare_staging(
    staging_directory: Path,
    plan: ExportPlan,
    reconstruction: Reconstruction,
) -> tuple[SourceManifest, tuple[str, ...]]:
    _write_staging(staging_directory, plan, reconstruction)
    staging_model = _read_model(
        staging_directory,
        plan.output_format,
        plan.source_layout.layout,
    )
    if not isinstance(staging_model, Reconstruction):
        raise RuntimeError("동기 Staging parser가 Reconstruction을 반환하지 않았습니다.")
    warnings = validate_reconstruction(staging_model)
    if staging_model != reconstruction:
        raise ValueError("Staging을 재읽은 결과가 Export Reconstruction과 다릅니다.")
    staging_manifest = capture_layout_manifest(_layout_in(staging_directory, plan))
    return staging_manifest, warnings


def _remove_obsolete_file(path: Path) -> None:
    """형식 전환 commit에서 반대 형식 파일을 제거하는 실패 주입 경계입니다."""

    path.unlink()


def _rollback_export_changes(
    plan: ExportPlan,
    restore_directory: Path,
    replaced_files: tuple[Path, ...],
    rollback_files: tuple[tuple[Path, Path], ...],
) -> None:
    target_path_set = set(plan.target_paths)
    affected_path_set = set(plan.affected_paths)
    if any(path not in target_path_set for path in replaced_files):
        raise ValueError("Export rollback 교체 경로가 검증된 대상과 다릅니다.")
    if any(
        original_path not in affected_path_set
        for original_path, _rollback_path in rollback_files
    ):
        raise ValueError("Export rollback 원본 경로가 검증된 대상과 다릅니다.")
    original_path_set = {
        original_path for original_path, _rollback_path in rollback_files
    }
    for destination in reversed(replaced_files):
        if destination in original_path_set:
            continue
        if destination.exists():
            destination.unlink()
    for destination, rollback_source in reversed(rollback_files):
        _restore_original_file(
            rollback_source,
            destination,
            restore_directory,
        )


def _rollback_replaced_files(
    plan: ExportPlan,
    rollback_directory: Path,
    restore_directory: Path,
    replaced_files: tuple[Path, ...],
) -> None:
    """기존 같은 형식 Export rollback 호출을 호환합니다."""

    _rollback_export_changes(
        plan,
        restore_directory,
        replaced_files,
        tuple(
            (path, rollback_directory / path.name)
            for path in plan.affected_paths
            if (rollback_directory / path.name).is_file()
        ),
    )


def _cleanup_after_failure(transaction_directory: Path | None) -> Path | None:
    if transaction_directory is None or not transaction_directory.exists():
        return None
    try:
        _cleanup_transaction_directory(transaction_directory)
    except OSError:
        return transaction_directory
    return None


def execute_export(
    plan: ExportPlan,
    reconstruction: Reconstruction,
    *,
    dataset_transform=None,
) -> ExportResult:
    """선택적인 데이터셋 변환을 포함해 동일한 출력 Job을 동기로 실행합니다."""

    # 순환 import를 피하면서 증분 실행과 동기 실행이 같은 구현을 사용하게 합니다.
    from .export_job import ExportTransactionJob

    from .dataset_transform import IDENTITY_TRANSFORM

    job = ExportTransactionJob(
        plan, reconstruction,
        dataset_transform=IDENTITY_TRANSFORM if dataset_transform is None else dataset_transform,
    )
    while not job.done:
        job.advance(work_budget_ms=None)
    return job.result
