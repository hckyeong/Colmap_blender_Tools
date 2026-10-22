"""선택된 COLMAP source 파일 집합의 내용 지문을 증분 계산합니다."""

from __future__ import annotations

import hashlib
from pathlib import Path
from time import perf_counter
from typing import BinaryIO

from .discovery import SparseModelLayout


_READ_CHUNK_SIZE = 1024 * 1024


class Sha256FileJob:
    """한 파일의 SHA-256을 고정 크기 byte chunk로 계산합니다."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._stream: BinaryIO | None = path.open("rb")
        self._digest = hashlib.sha256()
        self._completed_bytes = 0
        self._total_bytes = path.stat().st_size
        self._result: str | None = None

    @property
    def done(self) -> bool:
        """파일 지문 계산이 끝났는지 반환합니다."""
        return self._result is not None

    @property
    def completed_bytes(self) -> int:
        """현재까지 처리한 byte 수를 반환합니다."""
        return self._completed_bytes

    @property
    def total_bytes(self) -> int:
        """파일 전체 byte 수를 반환합니다."""
        return self._total_bytes

    @property
    def result(self) -> str:
        """완료된 SHA-256 문자열을 반환합니다."""
        if self._result is None:
            raise RuntimeError("완료되지 않은 SHA-256 작업의 결과를 읽을 수 없습니다.")
        return self._result

    def step(self, time_budget_seconds: float | None = None) -> None:
        """시간 예산 안에서 파일 chunk를 읽고 지문 상태를 진행합니다."""
        if self.done:
            raise RuntimeError("완료된 SHA-256 작업을 다시 실행할 수 없습니다.")
        deadline = (
            None
            if time_budget_seconds is None
            else perf_counter() + max(0.0, float(time_budget_seconds))
        )
        if self._stream is None:
            raise RuntimeError("닫힌 SHA-256 파일을 읽을 수 없습니다.")
        while True:
            chunk = self._stream.read(_READ_CHUNK_SIZE)
            if not chunk:
                self.close()
                self._result = self._digest.hexdigest()
                return
            self._digest.update(chunk)
            self._completed_bytes += len(chunk)
            if deadline is not None and perf_counter() >= deadline:
                return

    def close(self) -> None:
        """열려 있는 파일 핸들을 한 번만 닫습니다."""
        if self._stream is not None:
            self._stream.close()
            self._stream = None


class SourceFingerprintJob:
    """선택된 COLMAP 파일의 SHA-256 집합 지문을 파일 단위로 계산합니다."""

    def __init__(self, layout: SparseModelLayout) -> None:
        if not isinstance(layout, SparseModelLayout):
            raise TypeError("SourceFingerprintJob에는 SparseModelLayout이 필요합니다.")
        self._layout = layout
        self._files = layout.selected_files
        self._digest = hashlib.sha256()
        self._digest.update(
            f"format={layout.selected_format}\nlayout={layout.layout_kind}\n".encode("utf-8")
        )
        self._file_index = 0
        self._file_job: Sha256FileJob | None = None
        self._file_stat: tuple[int, int] | None = None
        self._file_signatures: list[tuple[int, int]] = []
        self._completed_file_bytes = 0
        self._total_bytes = sum(path.stat().st_size for path in self._files)
        self._result: str | None = None

    @property
    def done(self) -> bool:
        """전체 파일 집합 지문 계산이 끝났는지 반환합니다."""
        return self._result is not None

    @property
    def completed_bytes(self) -> int:
        """전체 파일 중 현재까지 처리한 byte 수를 반환합니다."""
        current = self._file_job.completed_bytes if self._file_job is not None else 0
        return min(self._total_bytes, self._completed_file_bytes + current)

    @property
    def total_bytes(self) -> int:
        """선택된 파일의 전체 byte 수를 반환합니다."""
        return self._total_bytes

    @property
    def result(self) -> str:
        """완료된 파일 집합 SHA-256 문자열을 반환합니다."""
        if self._result is None:
            raise RuntimeError("완료되지 않은 source 지문 작업의 결과를 읽을 수 없습니다.")
        return self._result

    def step(self, time_budget_seconds: float | None = None) -> None:
        """시간 예산 안에서 현재 source 파일 지문을 증분 계산합니다."""
        if self.done:
            raise RuntimeError("완료된 source 지문 작업을 다시 실행할 수 없습니다.")
        deadline = (
            None
            if time_budget_seconds is None
            else perf_counter() + max(0.0, float(time_budget_seconds))
        )
        try:
            while self._file_index < len(self._files):
                path = self._files[self._file_index]
                if self._file_job is None:
                    stat = path.stat()
                    self._file_stat = (stat.st_size, stat.st_mtime_ns)
                    self._file_job = Sha256FileJob(path)

                remaining_budget = (
                    None if deadline is None else max(0.0, deadline - perf_counter())
                )
                self._file_job.step(remaining_budget)
                if not self._file_job.done:
                    return

                current_stat = path.stat()
                current_signature = (current_stat.st_size, current_stat.st_mtime_ns)
                if current_signature != self._file_stat:
                    raise ValueError(
                        f"COLMAP source 파일을 읽는 동안 파일이 변경되었습니다: {path}"
                    )
                relative = path.relative_to(self._layout.source_path).as_posix()
                size = self._file_stat[0]
                item = f"{relative}\0{size}\0{self._file_job.result}\n"
                self._digest.update(item.encode("utf-8"))
                self._completed_file_bytes += size
                self._file_signatures.append(current_signature)
                self._file_job.close()
                self._file_job = None
                self._file_stat = None
                self._file_index += 1
                if deadline is not None and perf_counter() >= deadline:
                    return

            for path, expected_signature in zip(
                self._files,
                self._file_signatures,
                strict=True,
            ):
                stat = path.stat()
                if (stat.st_size, stat.st_mtime_ns) != expected_signature:
                    raise ValueError(
                        f"COLMAP source 파일을 읽는 동안 파일이 변경되었습니다: {path}"
                    )
            self._result = self._digest.hexdigest()
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        """현재 파일 작업과 열린 핸들을 안전하게 정리합니다."""
        if self._file_job is not None:
            self._file_job.close()
            self._file_job = None
        self._file_stat = None


def source_fingerprint(layout: SparseModelLayout) -> str:
    """동기 호출에서 증분 작업을 끝까지 실행해 source 지문을 반환합니다."""
    job = SourceFingerprintJob(layout)
    try:
        while not job.done:
            job.step()
        return job.result
    finally:
        job.close()


__all__ = ("Sha256FileJob", "SourceFingerprintJob", "source_fingerprint")
