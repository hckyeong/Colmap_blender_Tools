"""표준 Dataset Root에서 Sparse와 Images Directory를 결정적으로 찾습니다."""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path

from .errors import ColmapError
from .layout import resolve_layout


@dataclass(frozen=True, slots=True)
class DiscoveryResult:
    """자동 탐색된 Sparse와 Images 후보 및 상태를 보관합니다."""

    sparse_directory: Path | None
    images_directory: Path | None
    sparse_candidates: tuple[Path, ...]
    images_candidates: tuple[Path, ...]
    status: str


def _is_reparse(path: Path) -> bool:
    """Path가 Symlink, Junction 또는 Reparse Point인지 반환합니다."""
    try:
        if path.is_symlink():
            return True
        is_junction = getattr(os.path, "isjunction", None)
        if is_junction is not None and is_junction(path):
            return True
        attributes = getattr(os.lstat(path), "st_file_attributes", 0)
        flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        return bool(flag and attributes & flag)
    except OSError:
        return True


def _safe_directories(parent: Path) -> tuple[Path, ...]:
    """직속 하위의 비 Reparse Directory를 정렬해 반환합니다."""
    result: list[Path] = []
    try:
        entries = tuple(os.scandir(parent))
    except OSError:
        return ()
    for entry in entries:
        path = Path(entry.path)
        try:
            if entry.is_dir(follow_symlinks=False) and not _is_reparse(path):
                result.append(path.resolve(strict=True))
        except OSError:
            continue
    return tuple(sorted(result, key=lambda item: str(item).casefold()))


def discover_dataset(
    dataset_root: Path | str,
    input_format: str = "AUTO",
) -> DiscoveryResult:
    """표준 위치를 우선해 Camera/Image Dataset 경로를 탐색합니다."""
    try:
        root = Path(dataset_root).expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ColmapError(f"Dataset Root를 찾을 수 없습니다: {dataset_root}") from exc
    if not root.is_dir():
        raise ColmapError(f"Dataset Root가 Directory가 아닙니다: {root}")

    sparse_parent = root / "sparse"
    candidates = [root]
    if sparse_parent.is_dir() and not _is_reparse(sparse_parent):
        candidates.append(sparse_parent.resolve(strict=True))
        candidates.extend(_safe_directories(sparse_parent))

    valid_sparse: list[Path] = []
    for candidate in candidates:
        try:
            resolve_layout(candidate, input_format)
        except ColmapError:
            continue
        if candidate not in valid_sparse:
            valid_sparse.append(candidate)

    standard_zero = (root / "sparse" / "0").resolve()
    standard_sparse = (root / "sparse").resolve()
    if standard_zero in valid_sparse:
        selected_sparse = standard_zero
    elif standard_sparse in valid_sparse:
        selected_sparse = standard_sparse
    else:
        selected_sparse = valid_sparse[0] if len(valid_sparse) == 1 else None

    image_candidates = tuple(
        directory
        for directory in (root / "images",)
        if directory.is_dir() and not _is_reparse(directory)
    )
    selected_images = (
        image_candidates[0] if len(image_candidates) == 1 else None
    )
    status_parts = []
    status_parts.append(
        "Sparse Directory를 찾았습니다."
        if selected_sparse is not None
        else f"Sparse 후보가 {len(valid_sparse)}개이므로 직접 선택해야 합니다."
    )
    status_parts.append(
        "Images Directory를 찾았습니다."
        if selected_images is not None
        else "Images Directory를 직접 선택해야 합니다."
    )
    return DiscoveryResult(
        sparse_directory=selected_sparse,
        images_directory=selected_images,
        sparse_candidates=tuple(valid_sparse),
        images_candidates=image_candidates,
        status=" ".join(status_parts),
    )
