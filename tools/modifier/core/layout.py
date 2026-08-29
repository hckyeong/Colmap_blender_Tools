"""입력 형식과 legacy/modern 파일 집합을 결정합니다."""

from __future__ import annotations

from pathlib import Path

from .model import ResolvedLayout

_LEGACY_STEMS = ("cameras", "images", "points3D")
_MODERN_EXTRA_STEMS = ("rigs", "frames")


def _complete(directory: Path, extension: str, stems: tuple[str, ...]) -> bool:
    return all((directory / f"{stem}.{extension}").is_file() for stem in stems)


def resolve_layout(model_directory: Path | str, input_format: str = "AUTO") -> ResolvedLayout:
    directory = Path(model_directory).expanduser().resolve()
    if not directory.is_dir():
        raise ValueError(f"Sparse Directory가 존재하지 않습니다: {directory}")

    requested = input_format.upper()
    if requested not in {"AUTO", "BINARY", "TEXT"}:
        raise ValueError(f"지원하지 않는 Input Format입니다: {input_format}")

    candidates: list[str] = []
    if _complete(directory, "bin", _LEGACY_STEMS):
        candidates.append("binary")
    if _complete(directory, "txt", _LEGACY_STEMS):
        candidates.append("text")

    if requested == "AUTO":
        if not candidates:
            raise ValueError(f"완전한 COLMAP TXT/BIN 3파일 집합을 찾지 못했습니다: {directory}")
        selected_format = "binary" if "binary" in candidates else "text"
    else:
        selected_format = requested.lower()
        if selected_format not in candidates:
            raise ValueError(
                f"요청한 {requested} COLMAP 3파일 집합이 완전하지 않습니다: {directory}"
            )

    extension = "bin" if selected_format == "binary" else "txt"
    modern_exists = [
        (directory / f"{stem}.{extension}").is_file() for stem in _MODERN_EXTRA_STEMS
    ]
    if any(modern_exists) and not all(modern_exists):
        raise ValueError(
            f"modern layout의 rigs.{extension}와 frames.{extension} 중 하나가 누락되었습니다: "
            f"{directory}"
        )

    layout = "modern" if all(modern_exists) else "legacy"
    stems = (
        ("rigs", "cameras", "frames", "images", "points3D")
        if layout == "modern"
        else _LEGACY_STEMS
    )
    selected_files = tuple(directory / f"{stem}.{extension}" for stem in stems)
    return ResolvedLayout(directory, selected_format, layout, selected_files)


def sparse_root_for(model_directory: Path) -> Path:
    parent = model_directory.parent
    if model_directory.name.isdigit() and parent.name.lower() == "sparse":
        return parent
    return model_directory
