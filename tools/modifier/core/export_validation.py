"""COLMAP Export source, 경로와 Reconstruction의 사전 조건을 검사합니다."""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path

from .fingerprint import Sha256FileJob, sha256_file
from .integrity import validate_reconstruction
from .layout import resolve_layout
from .model import Reconstruction, ResolvedLayout, SourceDescriptor
from .export_types import (
    ExportPlan,
    SourceFileRecord,
    SourceManifest,
    ValidatedRuntimeToken,
)

_REPARSE_ATTRIBUTE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
_STEMS_BY_LAYOUT = {
    "legacy": ("cameras", "images", "points3D"),
    "modern": ("rigs", "cameras", "frames", "images", "points3D"),
}
_ALL_MODEL_STEMS = ("rigs", "cameras", "frames", "images", "points3D")


def _absolute_path(path: Path | str) -> Path:
    return Path(os.path.abspath(os.fspath(Path(path).expanduser())))


def _is_link_or_reparse(path: Path) -> bool:
    if path.is_symlink():
        return True
    is_junction = getattr(os.path, "isjunction", None)
    if is_junction is not None:
        try:
            if is_junction(path):
                return True
        except OSError:
            return True
    try:
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
    except OSError:
        return False
    return bool(attributes & _REPARSE_ATTRIBUTE)


def ensure_path_has_no_links(path: Path | str, context: str) -> Path:
    """존재하는 모든 경로 구성요소에서 symlink·junction·reparse를 거부합니다."""

    absolute = _absolute_path(path)
    parts = absolute.parts
    if not parts:
        raise ValueError(f"{context} 경로가 비어 있습니다.")
    current = Path(parts[0])
    if _is_link_or_reparse(current):
        raise ValueError(f"{context} 경로에 link 또는 reparse point가 있습니다: {current}")
    for part in parts[1:]:
        current = current / part
        if _is_link_or_reparse(current):
            raise ValueError(f"{context} 경로에 link 또는 reparse point가 있습니다: {current}")
        if not current.exists():
            break
    return absolute


def _require_within(child: Path, parent: Path, context: str) -> Path:
    try:
        return child.relative_to(parent)
    except ValueError as exc:
        raise ValueError(f"{context} 경로가 허용된 root 밖에 있습니다: {child}") from exc


def layout_from_source(source: SourceDescriptor) -> ResolvedLayout:
    """SourceDescriptor의 고정 format/layout을 현재 파일 집합과 대조합니다."""

    input_format = source.input_format.lower()
    layout_name = source.layout.lower()
    if input_format not in {"text", "binary"}:
        raise ValueError(f"지원하지 않는 Export 형식입니다: {source.input_format}")
    if layout_name not in _STEMS_BY_LAYOUT:
        raise ValueError(f"지원하지 않는 Export layout입니다: {source.layout}")

    model_directory = ensure_path_has_no_links(source.model_directory, "Model Directory")
    if not model_directory.is_dir():
        raise ValueError(f"Model Directory가 존재하지 않습니다: {model_directory}")
    resolved = resolve_layout(model_directory, input_format.upper())
    if resolved.input_format != input_format or resolved.layout != layout_name:
        raise ValueError(
            "현재 source의 형식 또는 layout이 Import 시점과 다릅니다. 다시 Import하십시오."
        )

    expected_extension = "txt" if input_format == "text" else "bin"
    expected_names = tuple(
        f"{stem}.{expected_extension}" for stem in _STEMS_BY_LAYOUT[layout_name]
    )
    if tuple(path.name for path in resolved.selected_files) != expected_names:
        raise ValueError("Export 대상 파일 집합이 예상한 COLMAP layout과 다릅니다.")

    descriptor_files = tuple(_absolute_path(path) for path in source.selected_files)
    resolved_files = tuple(_absolute_path(path) for path in resolved.selected_files)
    if descriptor_files != resolved_files:
        raise ValueError("현재 Export 대상 파일 목록이 Import 시점과 다릅니다.")
    for path in resolved_files:
        ensure_path_has_no_links(path, f"Source 파일 {path.name}")
        if not path.is_file():
            raise ValueError(f"Source 파일이 존재하지 않습니다: {path}")
        if path.parent != model_directory:
            raise ValueError(f"Source 파일이 Model Directory 바로 아래에 있지 않습니다: {path}")
    return ResolvedLayout(
        model_directory=model_directory,
        input_format=input_format,
        layout=layout_name,
        selected_files=resolved_files,
    )


def reconstruction_counts(
    reconstruction: Reconstruction,
) -> tuple[int, int, int, int, int]:
    """Runtime token에서 전체 순회 없이 비교할 모델 레코드 수를 반환합니다."""

    return (
        len(reconstruction.cameras),
        len(reconstruction.images),
        len(reconstruction.points3d),
        len(reconstruction.rigs),
        len(reconstruction.frames),
    )


def assert_runtime_token_matches(
    token: ValidatedRuntimeToken,
    source: SourceDescriptor,
    reconstruction: Reconstruction,
) -> None:
    """검증된 Runtime과 실제 Export 입력이 같은 객체·revision인지 확인합니다."""

    if not isinstance(token, ValidatedRuntimeToken):
        raise ValueError("Export Runtime token이 유효하지 않습니다.")
    if token.dataset_uuid != source.dataset_uuid:
        raise ValueError("Export Runtime token의 Dataset UUID가 source와 다릅니다.")
    if token.source_fingerprint != source.source_fingerprint:
        raise ValueError("Export Runtime token의 source fingerprint가 source와 다릅니다.")
    if token.edit_revision != token.index_revision:
        raise ValueError("Export Runtime 모델과 관계 인덱스 revision이 다릅니다.")
    if token.reconstruction_identity != id(reconstruction):
        raise ValueError("확인 이후 Export Reconstruction 객체가 변경되었습니다.")
    if token.model_counts != reconstruction_counts(reconstruction):
        raise ValueError("확인 이후 Export Reconstruction 레코드 수가 변경되었습니다.")


def _manifest_fingerprint(
    input_format: str,
    layout: str,
    records: tuple[SourceFileRecord, ...],
) -> str:
    digest = hashlib.sha256()
    digest.update(f"format={input_format}\nlayout={layout}\n".encode("utf-8"))
    for record in records:
        item = (
            f"{record.relative_path.as_posix()}\0{record.size}\0"
            f"{record.sha256}\n"
        )
        digest.update(item.encode("utf-8"))
    return digest.hexdigest()


class LayoutManifestJob:
    """선택 파일 manifest를 byte chunk SHA-256으로 생성합니다."""

    def __init__(self, layout: ResolvedLayout) -> None:
        self._layout = layout
        self._index = 0
        self._records: list[SourceFileRecord] = []
        self._current_path: Path | None = None
        self._current_before = None
        self._hash_job: Sha256FileJob | None = None
        self._result: SourceManifest | None = None
        self._completed_bytes = 0
        self._total_bytes = sum(path.stat().st_size for path in layout.selected_files)

    @property
    def done(self) -> bool:
        return self._result is not None

    @property
    def result(self) -> SourceManifest:
        if self._result is None:
            raise RuntimeError("완료되지 않은 manifest Job의 결과를 읽을 수 없습니다.")
        return self._result

    @property
    def completed_bytes(self) -> int:
        current = self._hash_job.completed_bytes if self._hash_job else 0
        return self._completed_bytes + current

    @property
    def total_bytes(self) -> int:
        return self._total_bytes

    def advance(self, work_budget_ms: float | None = None) -> None:
        if self.done:
            raise RuntimeError("완료된 manifest Job을 다시 실행할 수 없습니다.")
        if self._hash_job is None:
            if self._index == len(self._layout.selected_files):
                records = tuple(self._records)
                self._result = SourceManifest(
                    input_format=self._layout.input_format,
                    layout=self._layout.layout,
                    source_fingerprint=_manifest_fingerprint(
                        self._layout.input_format,
                        self._layout.layout,
                        records,
                    ),
                    files=records,
                )
                return
            self._current_path = _absolute_path(
                self._layout.selected_files[self._index]
            )
            self._current_before = self._current_path.stat()
            hash_job = sha256_file(self._current_path, incremental=True)
            if not isinstance(hash_job, Sha256FileJob):
                raise RuntimeError("증분 SHA-256 Job을 만들 수 없습니다.")
            self._hash_job = hash_job

        self._hash_job.advance(work_budget_ms)
        if not self._hash_job.done:
            return
        path = self._current_path
        before = self._current_before
        after = path.stat()
        if (before.st_size, before.st_mtime_ns) != (
            after.st_size,
            after.st_mtime_ns,
        ):
            raise ValueError(f"Manifest 생성 중 파일이 변경되었습니다: {path}")
        self._records.append(
            SourceFileRecord(
                relative_path=path.relative_to(self._layout.model_directory),
                absolute_path=path,
                size=after.st_size,
                mtime_ns=after.st_mtime_ns,
                sha256=self._hash_job.result,
            )
        )
        self._completed_bytes += after.st_size
        self._index += 1
        self._current_path = None
        self._current_before = None
        self._hash_job = None

    def close(self) -> None:
        if self._hash_job is not None:
            self._hash_job.close()


def capture_layout_manifest(
    layout: ResolvedLayout,
    *,
    incremental: bool = False,
) -> SourceManifest | LayoutManifestJob:
    """선택 파일을 각각 한 번 hash하여 재사용 가능한 manifest를 만듭니다."""

    job = LayoutManifestJob(layout)
    if incremental:
        return job
    try:
        while not job.done:
            job.advance()
        return job.result
    finally:
        job.close()


def capture_source_manifest(plan: ExportPlan) -> SourceManifest:
    """Export 시작 시 source 전체 byte 검사를 정확히 한 번 수행합니다."""

    current_layout = layout_from_source(plan.source)
    if current_layout != plan.source_layout:
        raise ValueError("Export 대상 layout이 계획 생성 이후 변경되었습니다.")
    manifest = capture_layout_manifest(current_layout)
    if not isinstance(manifest, SourceManifest):
        raise RuntimeError("동기 source manifest 생성 결과가 올바르지 않습니다.")
    if manifest.source_fingerprint != plan.expected_source_fingerprint:
        raise ValueError("COLMAP source 파일이 Import 이후 변경되었습니다. 다시 Import하십시오.")
    return manifest


def assert_source_stat_current(plan: ExportPlan, manifest: SourceManifest) -> None:
    """Commit 직전에 source의 경로·크기·mtime이 그대로인지 확인합니다."""

    current_layout = layout_from_source(plan.source)
    if current_layout != plan.source_layout:
        raise ValueError("Export 대상 layout이 검증 이후 변경되었습니다.")
    if (
        manifest.input_format != current_layout.input_format
        or manifest.layout != current_layout.layout
        or len(manifest.files) != len(current_layout.selected_files)
    ):
        raise ValueError("Source manifest가 현재 Export layout과 일치하지 않습니다.")
    for path, record in zip(
        current_layout.selected_files,
        manifest.files,
        strict=True,
    ):
        absolute_path = _absolute_path(path)
        if (
            absolute_path != record.absolute_path
            or absolute_path.relative_to(current_layout.model_directory)
            != record.relative_path
        ):
            raise ValueError("Export 대상 파일 경로가 manifest 생성 이후 변경되었습니다.")
        current = absolute_path.stat()
        if (
            current.st_size != record.size
            or current.st_mtime_ns != record.mtime_ns
        ):
            raise ValueError(
                f"COLMAP source 파일이 Export 도중 변경되었습니다: {absolute_path.name}"
            )


def assert_export_source_current(plan: ExportPlan) -> str:
    """호환 API: source manifest를 새로 캡처하여 현재 fingerprint를 반환합니다."""

    return capture_source_manifest(plan).source_fingerprint


def prepare_export_plan(
    source: SourceDescriptor,
    *,
    backup_enabled: bool,
    runtime_token: ValidatedRuntimeToken,
    output_format: str | None = None,
) -> ExportPlan:
    """경로와 Runtime token만 고정하고 source byte 검사는 실행 시점으로 미룹니다."""

    if not isinstance(source, SourceDescriptor):
        raise ValueError("Export source가 유효한 SourceDescriptor가 아닙니다.")
    if not isinstance(backup_enabled, bool):
        raise ValueError("Backup 설정은 bool이어야 합니다.")
    if not isinstance(runtime_token, ValidatedRuntimeToken):
        raise ValueError("Export Runtime token이 유효하지 않습니다.")
    if runtime_token.dataset_uuid != source.dataset_uuid:
        raise ValueError("Export Runtime token의 Dataset UUID가 source와 다릅니다.")
    if runtime_token.source_fingerprint != source.source_fingerprint:
        raise ValueError("Export Runtime token의 source fingerprint가 source와 다릅니다.")
    if runtime_token.edit_revision != runtime_token.index_revision:
        raise ValueError("Export Runtime 모델과 관계 인덱스 revision이 다릅니다.")
    normalized_output_format = (
        source.input_format.lower()
        if output_format is None or str(output_format).upper() == "SOURCE"
        else str(output_format).lower()
    )
    if normalized_output_format not in {"text", "binary"}:
        raise ValueError(
            f"지원하지 않는 Export 출력 형식입니다: {output_format}"
        )

    dataset_root = ensure_path_has_no_links(source.dataset_root, "Dataset Root")
    sparse_root = ensure_path_has_no_links(source.sparse_root, "Sparse Root")
    model_directory = ensure_path_has_no_links(source.model_directory, "Model Directory")
    images_directory = ensure_path_has_no_links(source.images_directory, "Images Directory")
    if not dataset_root.is_dir():
        raise ValueError(f"Dataset Root가 존재하지 않습니다: {dataset_root}")
    if not sparse_root.is_dir():
        raise ValueError(f"Sparse Root가 존재하지 않습니다: {sparse_root}")
    if not images_directory.is_dir():
        raise ValueError(f"Images Directory가 존재하지 않습니다: {images_directory}")
    model_relative_path = _require_within(
        model_directory,
        sparse_root,
        "Model Directory",
    )
    if any(part in {"", ".", ".."} for part in model_relative_path.parts):
        if model_relative_path != Path("."):
            raise ValueError("Model Directory의 sparse 상대경로가 안전하지 않습니다.")

    source_layout = layout_from_source(source)
    if source_layout.model_directory != model_directory:
        raise ValueError("SourceDescriptor의 Model Directory가 일관되지 않습니다.")
    if not source.source_fingerprint or len(source.source_fingerprint) != 64:
        raise ValueError("Import source fingerprint가 유효하지 않습니다.")
    try:
        int(source.source_fingerprint, 16)
    except ValueError as exc:
        raise ValueError("Import source fingerprint가 SHA-256 형식이 아닙니다.") from exc

    transaction_parent = ensure_path_has_no_links(sparse_root.parent, "Transaction Parent")
    if not transaction_parent.is_dir():
        raise ValueError(f"Transaction Parent가 존재하지 않습니다: {transaction_parent}")
    if os.stat(model_directory).st_dev != os.stat(transaction_parent).st_dev:
        raise ValueError("Staging과 rollback을 source와 같은 파일시스템에 만들 수 없습니다.")

    backup_root = _absolute_path(sparse_root.parent / "sparse_backup")
    if backup_enabled:
        ensure_path_has_no_links(backup_root, "Backup Root")
        if backup_root.exists() and not backup_root.is_dir():
            raise ValueError(f"Backup Root가 디렉터리가 아닙니다: {backup_root}")
        if backup_root == sparse_root:
            raise ValueError("Backup Root는 Sparse Root와 달라야 합니다.")
        try:
            backup_root.relative_to(sparse_root)
        except ValueError:
            pass
        else:
            raise ValueError("Backup Root를 Sparse Root 내부에 만들 수 없습니다.")
        try:
            sparse_root.relative_to(backup_root)
        except ValueError:
            pass
        else:
            raise ValueError("Sparse Root를 Backup Root 내부에서 사용할 수 없습니다.")

    output_extension = (
        "txt" if normalized_output_format == "text" else "bin"
    )
    target_relative_paths = tuple(
        Path(f"{stem}.{output_extension}")
        for stem in _STEMS_BY_LAYOUT[source_layout.layout]
    )
    target_paths = tuple(
        model_directory / relative_path
        for relative_path in target_relative_paths
    )
    for path in target_paths:
        ensure_path_has_no_links(path, f"Export 대상 파일 {path.name}")
        if path.exists() and not path.is_file():
            raise ValueError(
                f"Export 대상 경로가 파일이 아닙니다: {path}"
            )

    obsolete_paths: tuple[Path, ...] = ()
    if normalized_output_format != source_layout.input_format:
        source_extension = (
            "txt" if source_layout.input_format == "text" else "bin"
        )
        obsolete_candidates = [
            model_directory / f"{stem}.{source_extension}"
            for stem in _ALL_MODEL_STEMS
        ]
        if source_layout.layout == "legacy":
            obsolete_candidates.extend(
                model_directory / f"{stem}.{output_extension}"
                for stem in ("rigs", "frames")
            )
        target_path_set = set(target_paths)
        obsolete_paths = tuple(
            path
            for path in dict.fromkeys(obsolete_candidates)
            if path not in target_path_set
        )
        for path in obsolete_paths:
            ensure_path_has_no_links(path, f"정리 대상 파일 {path.name}")
            if path.exists() and not path.is_file():
                raise ValueError(
                    f"정리 대상 경로가 파일이 아닙니다: {path}"
                )

    affected_paths = tuple(
        dict.fromkeys((*target_paths, *obsolete_paths))
    )
    return ExportPlan(
        source=source,
        source_layout=source_layout,
        output_format=normalized_output_format,
        expected_source_fingerprint=source.source_fingerprint,
        backup_enabled=backup_enabled,
        backup_root=backup_root,
        model_relative_path=model_relative_path,
        transaction_parent=transaction_parent,
        target_paths=target_paths,
        target_relative_paths=target_relative_paths,
        obsolete_paths=obsolete_paths,
        affected_paths=affected_paths,
        runtime_token=runtime_token,
    )


def validate_export_request(
    source: SourceDescriptor,
    reconstruction: Reconstruction,
    *,
    backup_enabled: bool,
    output_format: str | None = None,
) -> ExportPlan:
    """독립 Core 호출용 checked 경계로 모델을 한 번 검증해 계획을 만듭니다."""

    if not isinstance(reconstruction, Reconstruction):
        raise ValueError("Export 대상이 유효한 Reconstruction이 아닙니다.")
    validate_reconstruction(reconstruction)
    token = ValidatedRuntimeToken(
        dataset_uuid=source.dataset_uuid,
        source_fingerprint=source.source_fingerprint,
        edit_revision=0,
        index_revision=0,
        history_hash="core-validated",
        reconstruction_identity=id(reconstruction),
        model_counts=reconstruction_counts(reconstruction),
    )
    return prepare_export_plan(
        source,
        backup_enabled=backup_enabled,
        runtime_token=token,
        output_format=output_format,
    )
