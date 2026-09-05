"""COLMAP 루트 바로 아래의 안전한 연결 OBJ 후보만 탐색합니다."""

from __future__ import annotations

import os
from pathlib import Path
import stat


def _is_link_or_junction(status: os.stat_result) -> bool:
    """심볼릭 링크와 Windows 재분석 지점을 일반 파일 후보에서 제외합니다."""
    return stat.S_ISLNK(status.st_mode) or bool(
        getattr(status, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    )


def _dataset_root_path(dataset_root: str | Path) -> Path:
    """루트가 접근 가능한 실제 폴더인지 확인하고 절대 경로로 반환합니다."""
    root = Path(dataset_root).expanduser()
    try:
        status = root.lstat()
        if _is_link_or_junction(status) or not stat.S_ISDIR(status.st_mode):
            raise ValueError(f"연결 OBJ의 데이터 루트는 실제 폴더여야 합니다: {root}")
        return root.resolve(strict=True)
    except OSError as error:
        raise ValueError(f"연결 OBJ의 데이터 루트에 접근할 수 없습니다: {root}") from error


def discover_companion_obj_candidates(dataset_root: str | Path) -> tuple[Path, ...]:
    """루트 직하의 실제 OBJ 파일만 경로순으로 반환하고 접근 오류는 알립니다."""
    root = _dataset_root_path(dataset_root)
    candidates: list[Path] = []
    try:
        for path in root.iterdir():
            if path.suffix.casefold() != ".obj":
                continue
            status = path.lstat()
            if _is_link_or_junction(status) or not stat.S_ISREG(status.st_mode):
                continue
            resolved = path.resolve(strict=True)
            if resolved.parent == root:
                candidates.append(resolved)
    except OSError as error:
        raise ValueError(f"연결 OBJ 후보를 탐색할 수 없습니다: {root}") from error
    return tuple(sorted(candidates, key=lambda path: (path.name.casefold(), path.name)))


def resolve_companion_obj_path(
    dataset_root: str | Path,
    selected_path: str | Path = "",
) -> Path | None:
    """명시 선택을 검증하거나 단일 후보를 자동 선택하고 복수 후보는 선택을 요구합니다."""
    root = _dataset_root_path(dataset_root)
    candidates = discover_companion_obj_candidates(root)
    if str(selected_path).strip():
        selected = Path(selected_path).expanduser()
        if not selected.is_absolute():
            selected = root / selected
        selected = Path(os.path.abspath(selected))
        if selected.parent != root or selected not in candidates:
            raise ValueError(f"선택한 연결 OBJ는 데이터 루트 직하의 실제 후보여야 합니다: {selected}")
        return selected
    if len(candidates) > 1:
        raise ValueError("데이터 루트에 연결 OBJ 후보가 여러 개 있습니다. 사용할 OBJ 파일을 선택하십시오.")
    return candidates[0] if candidates else None


__all__ = ("discover_companion_obj_candidates", "resolve_companion_obj_path")
