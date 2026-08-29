"""Camera, Image와 Point3D 파일로 안전한 COLMAP 입력 Layout을 결정합니다."""

from __future__ import annotations

from pathlib import Path

from .errors import ColmapError
from .model import ResolvedLayout

_FORMATS = {
    "BINARY": ("bin", "binary"),
    "TEXT": ("txt", "text"),
}


def _model_paths(directory: Path, extension: str) -> tuple[Path, Path, Path]:
    """지정 확장자의 Camera, Image와 Point3D 경로를 반환합니다."""
    return (
        directory / f"cameras.{extension}",
        directory / f"images.{extension}",
        directory / f"points3D.{extension}",
    )


def _model_complete(directory: Path, extension: str) -> bool:
    """지정 확장자의 Reconstruction 파일이 모두 일반 파일인지 반환합니다."""
    return all(path.is_file() for path in _model_paths(directory, extension))


def resolve_layout(
    model_directory: Path | str,
    input_format: str = "AUTO",
) -> ResolvedLayout:
    """BIN 우선 정책으로 완전한 COLMAP Reconstruction을 선택합니다."""
    try:
        directory = Path(model_directory).expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ColmapError(
            f"Sparse Directory를 찾을 수 없습니다: {model_directory}"
        ) from exc
    if not directory.is_dir():
        raise ColmapError(f"Sparse Directory가 아닙니다: {directory}")

    requested = input_format.upper()
    if requested not in {"AUTO", *_FORMATS}:
        raise ColmapError(f"지원하지 않는 Input Format입니다: {input_format}")

    complete = {
        format_name: _model_complete(directory, extension)
        for format_name, (extension, _label) in _FORMATS.items()
    }
    if requested == "AUTO":
        if complete["BINARY"]:
            selected = "BINARY"
        elif complete["TEXT"]:
            selected = "TEXT"
        else:
            raise ColmapError(
                f"완전한 cameras/images/points3D BIN 또는 TXT 세트를 찾지 못했습니다: "
                f"{directory}"
            )
    else:
        selected = requested
        if not complete[selected]:
            extension = _FORMATS[selected][0]
            raise ColmapError(
                f"요청한 cameras/images/points3D.{extension} 세트가 "
                f"완전하지 않습니다: {directory}"
            )

    extension, label = _FORMATS[selected]
    cameras_path, images_path, points3d_path = _model_paths(directory, extension)
    return ResolvedLayout(
        model_directory=directory,
        input_format=label,
        cameras_path=cameras_path.resolve(strict=True),
        images_path=images_path.resolve(strict=True),
        points3d_path=points3d_path.resolve(strict=True),
    )


def sparse_root_for(model_directory: Path | str) -> Path:
    """sparse/숫자 구조이면 sparse Root를, 아니면 입력 경로를 반환합니다."""
    directory = Path(model_directory).expanduser().resolve()
    parent = directory.parent
    if directory.name.isdigit() and parent.name.casefold() == "sparse":
        return parent
    return directory
