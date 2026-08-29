"""COLMAP Export Core의 검증 계획과 결과 자료구조입니다."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .model import LoadedReconstruction, ResolvedLayout, SourceDescriptor


@dataclass(frozen=True)
class ValidatedRuntimeToken:
    """편집 완료 경계에서 검증된 Runtime Reconstruction의 불변 식별값입니다."""

    dataset_uuid: str
    source_fingerprint: str
    edit_revision: int
    index_revision: int
    history_hash: str
    reconstruction_identity: int
    model_counts: tuple[int, int, int, int, int]


@dataclass(frozen=True)
class SourceFileRecord:
    """트랜잭션 시작 시 한 번 고정한 source 파일의 내용과 stat 정보입니다."""

    relative_path: Path
    absolute_path: Path
    size: int
    mtime_ns: int
    sha256: str


@dataclass(frozen=True)
class SourceManifest:
    """Export 전체에서 재사용하는 선택 sparse 파일 집합의 증거입니다."""

    input_format: str
    layout: str
    source_fingerprint: str
    files: tuple[SourceFileRecord, ...]


@dataclass(frozen=True)
class ExportPlan:
    """검증 시점에 고정한 source와 transaction 경로 계약입니다."""

    source: SourceDescriptor
    source_layout: ResolvedLayout
    output_format: str
    expected_source_fingerprint: str
    backup_enabled: bool
    backup_root: Path
    model_relative_path: Path
    transaction_parent: Path
    target_paths: tuple[Path, ...]
    target_relative_paths: tuple[Path, ...]
    obsolete_paths: tuple[Path, ...]
    affected_paths: tuple[Path, ...]
    runtime_token: ValidatedRuntimeToken


@dataclass(frozen=True)
class BackupResult:
    """단순 파일 복사로 확정한 영구 backup snapshot입니다."""

    snapshot_path: Path
    model_directory: Path
    source_fingerprint: str
    files: tuple[Path, ...]
    rollback_files: tuple[tuple[Path, Path], ...] = ()


@dataclass(frozen=True)
class ExportResult:
    """성공한 Export의 최종 source와 선택적 backup 정보입니다."""

    source_fingerprint: str
    backup_snapshot: Path | None
    loaded: LoadedReconstruction
    replaced_files: tuple[Path, ...]
    removed_files: tuple[Path, ...] = ()
    cleanup_warnings: tuple[str, ...] = ()


class ExportTransactionError(RuntimeError):
    """실패 단계와 자동 복구 결과를 보존하는 Export 오류입니다."""

    def __init__(
        self,
        message: str,
        *,
        stage: str,
        source_changed: bool,
        replaced_files: tuple[Path, ...] = (),
        removed_files: tuple[Path, ...] = (),
        rollback_attempted: bool = False,
        rollback_succeeded: bool | None = None,
        backup_snapshot: Path | None = None,
        recovery_directory: Path | None = None,
    ) -> None:
        super().__init__(message)
        self.stage = stage
        self.source_changed = source_changed
        self.replaced_files = replaced_files
        self.removed_files = removed_files
        self.rollback_attempted = rollback_attempted
        self.rollback_succeeded = rollback_succeeded
        self.backup_snapshot = backup_snapshot
        self.recovery_directory = recovery_directory
