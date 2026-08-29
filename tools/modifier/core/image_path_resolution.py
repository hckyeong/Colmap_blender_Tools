"""Camera Review용 이미지 경로를 안전하고 결정론적으로 해석합니다."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath


EXACT = "EXACT"
FALLBACK = "FALLBACK"
MISSING = "MISSING"
AMBIGUOUS = "AMBIGUOUS"

_PATH_STATUSES = frozenset((EXACT, FALLBACK, MISSING, AMBIGUOUS))


@dataclass(frozen=True)
class ImagePathResolution:
    """COLMAP 이미지 이름 하나의 경로 해석 결과입니다."""

    resolved_image_path: Path | None
    path_status: str

    def __post_init__(self) -> None:
        if self.path_status not in _PATH_STATUSES:
            raise ValueError(f"알 수 없는 이미지 경로 상태입니다: {self.path_status}")
        has_path = self.resolved_image_path is not None
        if has_path != (self.path_status in (EXACT, FALLBACK)):
            raise ValueError("이미지 경로 상태와 해석된 경로가 일치하지 않습니다.")

    @property
    def path(self) -> Path | None:
        """짧은 이름으로 해석된 경로를 반환합니다."""
        return self.resolved_image_path

    @property
    def status(self) -> str:
        """짧은 이름으로 경로 상태를 반환합니다."""
        return self.path_status


def _unsafe_image_name(image_name: str) -> bool:
    normalized = image_name.replace("\\", "/")
    posix_path = PurePosixPath(normalized)
    windows_path = PureWindowsPath(image_name)
    return (
        not image_name
        or "\x00" in image_name
        or posix_path.is_absolute()
        or windows_path.is_absolute()
        or bool(windows_path.drive)
        or bool(windows_path.root)
        or ".." in posix_path.parts
        or not posix_path.parts
    )


def _resolve_within_root(root: Path, path: Path, image_name: str) -> Path:
    try:
        resolved = path.resolve()
    except (OSError, RuntimeError) as exc:
        raise ValueError(
            f"COLMAP 이미지 경로를 안전하게 해석할 수 없습니다: {image_name}"
        ) from exc
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError(
            f"COLMAP 이미지 경로가 Images Directory 밖을 가리킵니다: {image_name}"
        ) from exc
    return resolved


def _stem_key(stem: str) -> str:
    """현재 운영체제의 파일명 대소문자 정책으로 stem 비교 키를 만듭니다."""
    return os.path.normcase(stem)


def _candidate_sort_key(path: Path) -> tuple[str, str]:
    return _stem_key(path.name), path.name


def resolve_image_path(
    images_directory: Path | str,
    image_name: str,
) -> ImagePathResolution:
    """COLMAP 상대 이미지 이름을 Images Directory 안에서 해석합니다.

    정확한 파일을 우선하고, 없을 때에만 같은 상대 폴더에서 동일 stem의 일반
    파일을 찾습니다. 절대경로, 상위 경로 이동과 심볼릭 링크 탈출은 오류로
    처리합니다.
    """

    if not isinstance(image_name, str) or _unsafe_image_name(image_name):
        raise ValueError(f"안전하지 않은 COLMAP 이미지 경로입니다: {image_name}")

    try:
        root = Path(images_directory).expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ValueError(
            f"Images Directory를 안전하게 해석할 수 없습니다: {images_directory}"
        ) from exc
    if not root.is_dir():
        raise ValueError(f"Images Directory가 존재하지 않습니다: {root}")

    normalized = image_name.replace("\\", "/")
    relative_path = PurePosixPath(normalized)
    target = root.joinpath(*relative_path.parts)
    resolved_target = _resolve_within_root(root, target, image_name)

    if target.is_file():
        return ImagePathResolution(resolved_target, EXACT)

    parent = target.parent
    resolved_parent = _resolve_within_root(root, parent, image_name)
    if not resolved_parent.is_dir():
        return ImagePathResolution(None, MISSING)

    requested_stem = _stem_key(Path(relative_path.name).stem)
    candidates: list[Path] = []
    try:
        directory_entries = sorted(resolved_parent.iterdir(), key=_candidate_sort_key)
    except OSError as exc:
        raise ValueError(
            f"이미지 후보 폴더를 읽을 수 없습니다: {resolved_parent}"
        ) from exc

    for candidate in directory_entries:
        if _stem_key(candidate.stem) != requested_stem:
            continue
        resolved_candidate = _resolve_within_root(root, candidate, image_name)
        if candidate.is_file():
            candidates.append(resolved_candidate)

    if not candidates:
        return ImagePathResolution(None, MISSING)
    if len(candidates) > 1:
        return ImagePathResolution(None, AMBIGUOUS)
    return ImagePathResolution(candidates[0], FALLBACK)


resolve_colmap_image_path = resolve_image_path


__all__ = [
    "AMBIGUOUS",
    "EXACT",
    "FALLBACK",
    "MISSING",
    "ImagePathResolution",
    "resolve_colmap_image_path",
    "resolve_image_path",
]
