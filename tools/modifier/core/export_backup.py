"""원본 sparse 파일을 단순 복사한 영구 backup snapshot을 생성합니다."""

from __future__ import annotations

import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .export_types import BackupResult, ExportPlan
from .export_validation import ensure_path_has_no_links


def _copy_backup_file(source: Path, destination: Path) -> None:
    """테스트에서 복사 실패를 주입할 수 있는 backup 파일 경계입니다."""

    shutil.copy2(source, destination)


def _snapshot_base_name(plan: ExportPlan) -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{timestamp}_{plan.expected_source_fingerprint[:8]}"


def _unique_snapshot_path(backup_root: Path, base_name: str) -> Path:
    candidate = backup_root / base_name
    if not candidate.exists():
        return candidate
    for index in range(1, 10_000):
        candidate = backup_root / f"{base_name}_{index:02d}"
        if not candidate.exists():
            return candidate
    raise ValueError("고유한 Backup snapshot 경로를 만들 수 없습니다.")


def create_backup_snapshot(
    plan: ExportPlan,
) -> BackupResult:
    """Export 직전 source 파일을 sibling sparse_backup으로 단순 복사합니다."""

    if not plan.backup_enabled:
        raise ValueError("Backup이 비활성화된 ExportPlan으로 snapshot을 만들 수 없습니다.")

    backup_root_created = False
    if not plan.backup_root.exists():
        plan.backup_root.mkdir()
        backup_root_created = True
    ensure_path_has_no_links(plan.backup_root, "Backup Root")
    if not plan.backup_root.is_dir():
        raise ValueError(f"Backup Root가 디렉터리가 아닙니다: {plan.backup_root}")

    staging_root = Path(
        tempfile.mkdtemp(prefix=".blender_colmap_snapshot_", dir=plan.backup_root)
    )
    try:
        snapshot_model_directory = staging_root / plan.model_relative_path
        snapshot_model_directory.mkdir(parents=True, exist_ok=True)
        source_path_set = set(plan.source_layout.selected_files)
        copied_source_relative_paths = []
        rollback_records = []
        for source_path in plan.affected_paths:
            if not source_path.exists():
                continue
            relative_path = Path(source_path.name)
            if source_path in source_path_set:
                destination = snapshot_model_directory / relative_path
                copied_source_relative_paths.append(relative_path)
            else:
                destination = (
                    snapshot_model_directory
                    / ".format_conversion_rollback"
                    / relative_path
                )
                destination.parent.mkdir(exist_ok=True)
            _copy_backup_file(source_path, destination)
            rollback_records.append(
                (source_path, destination.relative_to(staging_root))
            )

        final_path = _unique_snapshot_path(plan.backup_root, _snapshot_base_name(plan))
        # 기존 snapshot을 절대 교체하지 않도록 목적지 존재 여부를 다시 확인합니다.
        if final_path.exists():
            raise ValueError(f"Backup snapshot 경로가 이미 존재합니다: {final_path}")
        staging_root.rename(final_path)
        final_model_directory = final_path / plan.model_relative_path
        final_files = tuple(
            final_model_directory / relative_path
            for relative_path in copied_source_relative_paths
        )
        rollback_files = tuple(
            (source_path, final_path / backup_relative_path)
            for source_path, backup_relative_path in rollback_records
        )
        return BackupResult(
            snapshot_path=final_path,
            model_directory=final_model_directory,
            source_fingerprint=plan.expected_source_fingerprint,
            files=final_files,
            rollback_files=rollback_files,
        )
    except Exception:
        if staging_root.exists():
            shutil.rmtree(staging_root, ignore_errors=True)
        if backup_root_created:
            try:
                plan.backup_root.rmdir()
            except OSError:
                pass
        raise
