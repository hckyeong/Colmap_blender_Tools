"""선택된 source 파일 집합의 내용 지문을 계산합니다."""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from .model import ResolvedLayout


@dataclass(frozen=True, slots=True)
class SourceFileSignature:
    """해시 완료 시점의 source 파일 식별 정보와 정규 상대경로를 고정합니다."""

    relative_path: str
    size: int
    mtime_ns: int
    st_dev: int | None = None
    st_ino: int | None = None


def _file_signature(layout: ResolvedLayout, path: Path) -> SourceFileSignature:
    """현재 파일 상태를 복원 중 재검증할 수 있는 불변 서명으로 변환합니다."""

    stat = path.stat()
    return SourceFileSignature(
        relative_path=path.relative_to(layout.model_directory).as_posix(),
        size=int(stat.st_size),
        mtime_ns=int(stat.st_mtime_ns),
        st_dev=(int(stat.st_dev) if hasattr(stat, "st_dev") else None),
        st_ino=(int(stat.st_ino) if hasattr(stat, "st_ino") else None),
    )


class Sha256FileJob:
    """파일 SHA-256을 1 MiB byte chunk로 계산합니다."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._stream: BinaryIO | None = path.open("rb")
        self._digest = hashlib.sha256()
        self._completed_bytes = 0
        self._total_bytes = path.stat().st_size
        self._result: str | None = None

    @property
    def done(self) -> bool:
        return self._result is not None

    @property
    def completed_bytes(self) -> int:
        return self._completed_bytes

    @property
    def total_bytes(self) -> int:
        return self._total_bytes

    @property
    def result(self) -> str:
        if self._result is None:
            raise RuntimeError("완료되지 않은 SHA-256 Job의 결과를 읽을 수 없습니다.")
        return self._result

    def advance(self, work_budget_ms: float | None = None) -> None:
        if self.done:
            raise RuntimeError("완료된 SHA-256 Job을 다시 실행할 수 없습니다.")
        deadline = (
            None
            if work_budget_ms is None
            else time.perf_counter() + (work_budget_ms / 1000.0)
        )
        while True:
            chunk = self._stream.read(1024 * 1024)
            if not chunk:
                self._stream.close()
                self._stream = None
                self._result = self._digest.hexdigest()
                return
            self._digest.update(chunk)
            self._completed_bytes += len(chunk)
            if deadline is not None and time.perf_counter() >= deadline:
                return

    def close(self) -> None:
        if self._stream is not None:
            self._stream.close()
            self._stream = None


class SourceFingerprintJob:
    """선택된 source 파일의 지문을 파일·byte chunk 단위로 계산합니다."""

    def __init__(self, layout: ResolvedLayout) -> None:
        """선택 파일과 파일별 Signature를 계산할 증분 상태를 준비합니다."""

        self._layout = layout
        self._digest = hashlib.sha256()
        self._digest.update(
            f"format={layout.input_format}\nlayout={layout.layout}\n".encode("utf-8")
        )
        self._file_index = 0
        self._file_job: Sha256FileJob | None = None
        self._file_signature: SourceFileSignature | None = None
        self._file_signatures: list[SourceFileSignature] = []
        self._completed_file_bytes = 0
        self._total_bytes = sum(path.stat().st_size for path in layout.selected_files)
        self._result: str | None = None

    @property
    def done(self) -> bool:
        return self._result is not None

    @property
    def completed_bytes(self) -> int:
        current = self._file_job.completed_bytes if self._file_job is not None else 0
        return min(self._total_bytes, self._completed_file_bytes + current)

    @property
    def total_bytes(self) -> int:
        return self._total_bytes

    @property
    def result(self) -> str:
        if self._result is None:
            raise RuntimeError("완료되지 않은 source fingerprint Job의 결과를 읽을 수 없습니다.")
        return self._result

    @property
    def signature_snapshot(self) -> tuple[SourceFileSignature, ...]:
        """완료된 전체 해시와 같은 파일 상태의 불변 서명 묶음을 반환합니다."""

        if not self.done:
            raise RuntimeError("완료되지 않은 source fingerprint Job의 서명을 읽을 수 없습니다.")
        return tuple(self._file_signatures)

    def advance(self, work_budget_ms: float | None = None) -> None:
        """시간 예산 안에서 전체 지문과 파일별 Signature 계산을 전진시킵니다."""

        if self.done:
            raise RuntimeError("완료된 source fingerprint Job을 다시 실행할 수 없습니다.")
        deadline = (
            None
            if work_budget_ms is None
            else time.perf_counter() + (work_budget_ms / 1000.0)
        )
        try:
            while self._file_index < len(self._layout.selected_files):
                path = self._layout.selected_files[self._file_index]
                if self._file_job is None:
                    self._file_signature = _file_signature(self._layout, path)
                    self._file_job = Sha256FileJob(path)

                remaining_budget = (
                    None
                    if deadline is None
                    else max(0.0, (deadline - time.perf_counter()) * 1000.0)
                )
                self._file_job.advance(remaining_budget)
                if not self._file_job.done:
                    return

                current_signature = _file_signature(self._layout, path)
                if current_signature != self._file_signature:
                    raise ValueError(
                        "COLMAP source 파일을 읽는 동안 파일이 변경되었습니다: "
                        f"{path}"
                    )
                relative = self._file_signature.relative_path
                size = self._file_signature.size
                item = f"{relative}\0{size}\0{self._file_job.result}\n"
                self._digest.update(item.encode("utf-8"))
                self._completed_file_bytes += size
                self._file_signatures.append(current_signature)
                self._file_job.close()
                self._file_job = None
                self._file_signature = None
                self._file_index += 1
                if deadline is not None and time.perf_counter() >= deadline:
                    return

            verify_source_signatures(self._layout, tuple(self._file_signatures))
            self._result = self._digest.hexdigest()
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        """열린 파일 해시 Stream과 미완료 Signature 상태를 정리합니다."""

        if self._file_job is not None:
            self._file_job.close()
            self._file_job = None
        self._file_signature = None


def verify_source_signatures(
    layout: ResolvedLayout,
    expected: tuple[SourceFileSignature, ...],
) -> None:
    """전체 내용을 다시 읽지 않고 현재 source 파일 서명을 snapshot과 비교합니다."""

    if len(expected) != len(layout.selected_files):
        raise ValueError("COLMAP source 파일 서명 수가 현재 layout과 다릅니다.")
    for path, expected_signature in zip(
        layout.selected_files,
        expected,
        strict=True,
    ):
        try:
            current_signature = _file_signature(layout, path)
        except OSError as exc:
            raise ValueError(
                f"COLMAP source 파일 상태를 확인할 수 없습니다: {path}"
            ) from exc
        if current_signature != expected_signature:
            raise ValueError(
                "COLMAP source 파일을 읽는 동안 파일이 변경되었습니다: "
                f"{path}"
            )


def sha256_file(
    path: Path,
    *,
    incremental: bool = False,
) -> str | Sha256FileJob:
    job = Sha256FileJob(path)
    if incremental:
        return job
    try:
        while not job.done:
            job.advance()
        return job.result
    finally:
        job.close()


def source_fingerprint(
    layout: ResolvedLayout,
    *,
    incremental: bool = False,
) -> str | SourceFingerprintJob:
    """기존 동기 API와 증분 Job이 같은 지문 계산 구현을 사용합니다."""

    job = SourceFingerprintJob(layout)
    if incremental:
        return job
    try:
        while not job.done:
            job.advance()
        return job.result
    finally:
        job.close()


__all__ = [
    "Sha256FileJob",
    "SourceFileSignature",
    "SourceFingerprintJob",
    "sha256_file",
    "source_fingerprint",
    "verify_source_signatures",
]
