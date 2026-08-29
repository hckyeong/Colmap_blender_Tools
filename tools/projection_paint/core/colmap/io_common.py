"""COLMAP Source 파일의 취소와 Identity 검증을 공통 처리합니다."""

from __future__ import annotations

import os
from pathlib import Path
from collections.abc import Generator
from typing import Any, BinaryIO, Callable

from .errors import ColmapError, ImportCancelled
from .model import ParserLimits, SourceFileIdentity

CancelCheck = Callable[[], bool] | None


def consume_generator(generator: Generator[Any, None, Any]) -> Any:
    """증분 Generator를 끝까지 실행하고 최종 반환값을 돌려줍니다."""

    while True:
        try:
            next(generator)
        except StopIteration as completed:
            return completed.value


def check_cancel(cancel_check: CancelCheck) -> None:
    """취소 요청이 있으면 Import 중단 예외를 발생시킵니다."""
    if cancel_check is not None and cancel_check():
        raise ImportCancelled("COLMAP Import 준비가 취소되었습니다.")


def canonical_source_path(path: Path | str) -> Path:
    """존재하는 Source 파일 경로를 정규화해 반환합니다."""
    try:
        resolved = Path(path).expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ColmapError(f"COLMAP Source 파일을 찾을 수 없습니다: {path}") from exc
    if not resolved.is_file():
        raise ColmapError(f"COLMAP Source가 일반 파일이 아닙니다: {resolved}")
    return resolved


def identity_from_stat(path: Path, stat_result: os.stat_result) -> SourceFileIdentity:
    """OS Stat 결과를 비교 가능한 Source Identity로 변환합니다."""
    return SourceFileIdentity(
        path=path,
        size=int(stat_result.st_size),
        mtime_ns=int(stat_result.st_mtime_ns),
        device=int(stat_result.st_dev),
        inode=int(stat_result.st_ino),
    )


def identity_from_stream(path: Path, stream: BinaryIO) -> SourceFileIdentity:
    """열린 File Handle에서 현재 Source Identity를 읽습니다."""
    return identity_from_stat(path, os.fstat(stream.fileno()))


def open_source(
    path: Path | str,
    limits: ParserLimits,
) -> tuple[Path, BinaryIO, SourceFileIdentity]:
    """크기 상한을 검증한 Source File Handle을 엽니다."""
    resolved = canonical_source_path(path)
    try:
        stream = resolved.open("rb")
    except OSError as exc:
        raise ColmapError(f"COLMAP Source 파일을 열 수 없습니다: {resolved}") from exc
    try:
        identity = identity_from_stream(resolved, stream)
        if identity.size > limits.max_source_file_bytes:
            raise ColmapError(
                f"COLMAP Source 파일이 안전 상한을 초과했습니다: "
                f"{resolved} ({identity.size} bytes)"
            )
    except Exception:
        stream.close()
        raise
    return resolved, stream, identity


def ensure_stream_unchanged(
    path: Path,
    stream: BinaryIO,
    expected: SourceFileIdentity,
) -> None:
    """Parsing 전후 열린 Source File Handle의 Identity가 같은지 확인합니다."""
    current = identity_from_stream(path, stream)
    if not expected.same_file(current):
        raise ColmapError(f"Parsing 중 COLMAP Source가 변경되었습니다: {path}")
