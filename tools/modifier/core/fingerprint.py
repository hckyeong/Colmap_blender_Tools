"""선택된 source 파일 집합의 내용 지문을 계산합니다."""

from __future__ import annotations

import hashlib
import time
from pathlib import Path
from typing import BinaryIO

from .model import ResolvedLayout


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
        self._layout = layout
        self._digest = hashlib.sha256()
        self._digest.update(
            f"format={layout.input_format}\nlayout={layout.layout}\n".encode("utf-8")
        )
        self._file_index = 0
        self._file_job: Sha256FileJob | None = None
        self._file_stat: tuple[int, int] | None = None
        self._file_signatures: list[tuple[int, int]] = []
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

    def advance(self, work_budget_ms: float | None = None) -> None:
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
                    stat = path.stat()
                    self._file_stat = (stat.st_size, stat.st_mtime_ns)
                    self._file_job = Sha256FileJob(path)

                remaining_budget = (
                    None
                    if deadline is None
                    else max(0.0, (deadline - time.perf_counter()) * 1000.0)
                )
                self._file_job.advance(remaining_budget)
                if not self._file_job.done:
                    return

                current_stat = path.stat()
                current_signature = (current_stat.st_size, current_stat.st_mtime_ns)
                if current_signature != self._file_stat:
                    raise ValueError(
                        "COLMAP source 파일을 읽는 동안 파일이 변경되었습니다: "
                        f"{path}"
                    )
                relative = path.relative_to(
                    self._layout.model_directory
                ).as_posix()
                size = self._file_stat[0]
                item = f"{relative}\0{size}\0{self._file_job.result}\n"
                self._digest.update(item.encode("utf-8"))
                self._completed_file_bytes += size
                self._file_signatures.append(current_signature)
                self._file_job.close()
                self._file_job = None
                self._file_stat = None
                self._file_index += 1
                if deadline is not None and time.perf_counter() >= deadline:
                    return

            for path, expected_signature in zip(
                self._layout.selected_files,
                self._file_signatures,
                strict=True,
            ):
                stat = path.stat()
                if (stat.st_size, stat.st_mtime_ns) != expected_signature:
                    raise ValueError(
                        "COLMAP source 파일을 읽는 동안 파일이 변경되었습니다: "
                        f"{path}"
                    )
            self._result = self._digest.hexdigest()
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        if self._file_job is not None:
            self._file_job.close()
            self._file_job = None
        self._file_stat = None


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
    "SourceFingerprintJob",
    "sha256_file",
    "source_fingerprint",
]
