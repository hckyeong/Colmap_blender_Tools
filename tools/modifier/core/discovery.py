"""차상위 Dataset Root에서 sparse와 images 후보를 결정적으로 탐색합니다."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path

from .binary_io import read_images_binary
from .layout import resolve_layout
from .paths import validate_image_files
from .text_io import read_images_text


@dataclass(frozen=True)
class DiscoveryResult:
    sparse_directory: Path | None
    images_directory: Path | None
    status: str
    sparse_candidates: tuple[Path, ...]
    images_candidates: tuple[Path, ...]


def _directories_to_depth_two(root: Path) -> list[Path]:
    def safe_children(parent: Path) -> list[Path]:
        children = []
        for path in parent.iterdir():
            if not path.is_dir() or path.is_symlink():
                continue
            if hasattr(os.path, "isjunction") and os.path.isjunction(path):
                continue
            resolved = path.resolve()
            try:
                if os.path.commonpath((str(root), str(resolved))) != str(root):
                    continue
            except ValueError:
                continue
            children.append(resolved)
        return sorted(children, key=str)

    result = [root]
    first_level = safe_children(root)
    result.extend(first_level)
    for directory in first_level:
        result.extend(safe_children(directory))
    return result


def discover_dataset(dataset_root: Path | str, input_format: str = "AUTO") -> DiscoveryResult:
    root = Path(dataset_root).expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"Dataset Root가 존재하지 않습니다: {root}")

    directories = _directories_to_depth_two(root)
    sparse_candidates: list[Path] = []
    resolved_by_path = {}
    for directory in directories:
        try:
            resolved = resolve_layout(directory, input_format)
        except ValueError:
            continue
        sparse_candidates.append(directory)
        resolved_by_path[directory] = resolved

    sparse_candidates = sorted(set(sparse_candidates), key=str)
    standard_sparse_zero = (root / "sparse" / "0").resolve()
    standard_sparse = (root / "sparse").resolve()
    if standard_sparse_zero in sparse_candidates:
        sparse_directory = standard_sparse_zero
    elif standard_sparse in sparse_candidates:
        sparse_directory = standard_sparse
    else:
        sparse_directory = sparse_candidates[0] if len(sparse_candidates) == 1 else None

    named_images = [directory for directory in directories if directory.name.lower() == "images"]
    valid_images: list[Path] = []
    standard_images = (root / "images").resolve()
    standard_sparse_selected = sparse_directory in {
        standard_sparse_zero,
        standard_sparse,
    }
    standard_images_only = named_images == [standard_images]
    if standard_sparse_selected and standard_images_only:
        # 표준 Dataset Root는 Loader가 전체 모델과 이미지 파일을 검증하므로
        # Discovery에서는 images parser와 동일 파일 검사를 반복하지 않습니다.
        valid_images = [standard_images]
    elif sparse_directory is not None:
        layout = resolved_by_path[sparse_directory]
        if layout.input_format == "binary":
            images = read_images_binary(sparse_directory / "images.bin")
        else:
            images = read_images_text(sparse_directory / "images.txt")
        names = [image.name for image in images.values()]
        for candidate in named_images:
            try:
                validate_image_files(candidate, names)
            except ValueError:
                continue
            valid_images.append(candidate)
    else:
        valid_images = named_images

    valid_images = sorted(set(valid_images), key=str)
    if standard_images in valid_images:
        images_directory = standard_images
    else:
        images_directory = valid_images[0] if len(valid_images) == 1 else None

    messages = []
    if not sparse_candidates:
        messages.append("Sparse 후보를 찾지 못했습니다.")
    elif sparse_directory is None:
        messages.append(f"Sparse 후보가 {len(sparse_candidates)}개입니다. 직접 선택하십시오.")
    elif len(sparse_candidates) > 1:
        messages.append("여러 후보 중 표준 sparse/0 Directory를 선택했습니다.")
    else:
        messages.append("Sparse Directory를 찾았습니다.")
    if not valid_images:
        messages.append("검증된 Images 후보를 찾지 못했습니다.")
    elif images_directory is None:
        messages.append(f"Images 후보가 {len(valid_images)}개입니다. 직접 선택하십시오.")
    elif len(valid_images) > 1:
        messages.append("여러 후보 중 표준 images Directory를 선택했습니다.")
    else:
        messages.append("Images Directory를 찾았습니다.")

    return DiscoveryResult(
        sparse_directory,
        images_directory,
        " ".join(messages),
        tuple(sparse_candidates),
        tuple(valid_images),
    )
