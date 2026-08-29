"""Dataset Root에서 COLMAP sparse model 폴더와 입력 형식을 결정합니다."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from typing import Final, Literal, TypeAlias

from ..logging_utils import get_logger
from .errors import ColmapDiscoveryError


LOGGER = get_logger("colmap_io.discovery")
REQUIRED_STEMS: Final[tuple[str, ...]] = ("cameras", "images", "points3D")
MODERN_EXTRA_STEMS: Final[tuple[str, ...]] = ("rigs", "frames")
SourceFormat: TypeAlias = Literal["binary", "text"]
InputFormat: TypeAlias = Literal["AUTO", "BINARY", "TEXT"]


@dataclass(frozen=True, slots=True)
class SparseModelLayout:
    """선택된 sparse model 폴더와 실제 읽을 파일 경로를 보관합니다."""

    source_path: Path
    selected_format: SourceFormat
    cameras_path: Path
    images_path: Path
    points3d_path: Path
    rigs_path: Path | None = None
    frames_path: Path | None = None

    @property
    def source_format(self) -> SourceFormat:
        """호출부 호환을 위해 선택한 파일 형식을 반환합니다."""
        return self.selected_format

    @property
    def layout_kind(self) -> str:
        """rigs·frames 포함 여부를 legacy 또는 modern 이름으로 반환합니다."""
        return "modern" if self.rigs_path is not None else "legacy"

    @property
    def selected_files(self) -> tuple[Path, ...]:
        """가져오기와 원본 지문 검사에 쓰는 파일을 COLMAP 순서로 반환합니다."""
        if self.layout_kind == "modern":
            return (
                self.rigs_path,
                self.cameras_path,
                self.frames_path,
                self.images_path,
                self.points3d_path,
            )
        return (self.cameras_path, self.images_path, self.points3d_path)


def _normalize_input_format(input_format: str) -> InputFormat:
    """공개 입력 형식을 대문자로 정규화하고 지원 여부를 검사합니다."""
    requested = str(input_format).upper()
    if requested not in {"AUTO", "BINARY", "TEXT"}:
        raise ColmapDiscoveryError(f"지원하지 않는 COLMAP 입력 형식입니다: {input_format}")
    return requested


def _format_suffix(source_format: SourceFormat) -> str:
    """내부 형식 이름을 COLMAP 파일 확장자로 변환합니다."""
    return ".bin" if source_format == "binary" else ".txt"


def _file_path(directory: Path, stem: str, source_format: SourceFormat) -> Path:
    """파일 stem과 선택 형식으로 절대 후보 경로를 만듭니다."""
    return directory / f"{stem}{_format_suffix(source_format)}"


def _complete(directory: Path, source_format: SourceFormat, stems: tuple[str, ...]) -> bool:
    """지정 형식의 파일 stem 집합이 모두 일반 파일인지 반환합니다."""
    return all(_file_path(directory, stem, source_format).is_file() for stem in stems)


def _resolve_layout_format(directory: Path, input_format: InputFormat) -> SourceFormat:
    """완전한 3파일 집합에서 요청 형식을 선택하고 AUTO에서는 BIN을 우선합니다."""
    candidates: list[SourceFormat] = []
    if _complete(directory, "binary", REQUIRED_STEMS):
        candidates.append("binary")
    if _complete(directory, "text", REQUIRED_STEMS):
        candidates.append("text")

    if input_format == "AUTO":
        if not candidates:
            raise ColmapDiscoveryError(
                f"완전한 COLMAP TXT/BIN 3파일 집합을 찾지 못했습니다: {directory}"
            )
        return "binary" if "binary" in candidates else "text"

    selected_format: SourceFormat = "binary" if input_format == "BINARY" else "text"
    if selected_format not in candidates:
        raise ColmapDiscoveryError(
            f"요청한 {input_format} COLMAP 3파일 집합이 완전하지 않습니다: {directory}"
        )
    return selected_format


def _resolve_modern_files(
    directory: Path,
    selected_format: SourceFormat,
) -> tuple[Path | None, Path | None]:
    """선택 형식의 rigs·frames를 한 쌍으로만 허용해 layout을 확정합니다."""
    paths = tuple(_file_path(directory, stem, selected_format) for stem in MODERN_EXTRA_STEMS)
    exists = tuple(path.is_file() for path in paths)
    if any(exists) and not all(exists):
        suffix = _format_suffix(selected_format)
        raise ColmapDiscoveryError(
            f"modern layout의 rigs{suffix}와 frames{suffix} 중 하나가 누락되었습니다: "
            f"{directory}"
        )
    if all(exists):
        return paths
    return None, None


def _normalize_source_path(path: str | Path) -> Path:
    """사용자 경로를 절대 정규화하고 탐색 시작 폴더 여부를 확인합니다."""
    source_path = Path(path).expanduser().resolve(strict=False)
    if not source_path.exists():
        raise ColmapDiscoveryError(f"Dataset Root가 존재하지 않습니다: {source_path}")
    if not source_path.is_dir():
        raise ColmapDiscoveryError(f"Dataset Root는 폴더여야 합니다: {source_path}")
    return source_path


def _directories_to_depth_two(root: Path) -> tuple[Path, ...]:
    """Dataset Root와 최대 두 단계 하위의 안전한 실제 폴더만 반환합니다."""

    def safe_children(parent: Path) -> list[Path]:
        """링크·junction·Root 밖 경로를 제외하고 자식 폴더를 정렬합니다."""
        children: list[Path] = []
        try:
            candidates = tuple(parent.iterdir())
        except OSError as error:
            raise ColmapDiscoveryError(
                f"COLMAP Dataset 하위 폴더를 탐색할 수 없습니다: {parent}"
            ) from error
        for path in candidates:
            if not path.is_dir() or path.is_symlink():
                continue
            if hasattr(os.path, "isjunction") and os.path.isjunction(path):
                continue
            try:
                resolved = path.resolve()
                if os.path.commonpath((str(root), str(resolved))) != str(root):
                    continue
            except (OSError, RuntimeError, ValueError):
                continue
            children.append(resolved)
        return sorted(children, key=lambda value: str(value).casefold())

    directories = [root]
    first_level = safe_children(root)
    directories.extend(first_level)
    for directory in first_level:
        directories.extend(safe_children(directory))
    return tuple(directories)


def _resolve_model_directory(
    dataset_root: Path,
    input_format: InputFormat,
) -> tuple[Path, SourceFormat]:
    """기준 Importer와 같은 sparse/0 우선순위로 단일 모델을 확정합니다."""
    candidates: list[tuple[Path, SourceFormat]] = []
    for directory in _directories_to_depth_two(dataset_root):
        try:
            selected_format = _resolve_layout_format(directory, input_format)
            _resolve_modern_files(directory, selected_format)
        except ColmapDiscoveryError:
            continue
        candidates.append((directory, selected_format))

    standard_sparse_zero = (dataset_root / "sparse" / "0").resolve()
    standard_sparse = (dataset_root / "sparse").resolve()
    by_path = {path: selected_format for path, selected_format in candidates}
    if standard_sparse_zero in by_path:
        return standard_sparse_zero, by_path[standard_sparse_zero]
    if standard_sparse in by_path:
        return standard_sparse, by_path[standard_sparse]
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        raise ColmapDiscoveryError(
            "Dataset Root의 두 단계 하위에서 완전한 COLMAP sparse model을 찾지 못했습니다."
        )
    paths = ", ".join(str(path) for path, _selected_format in candidates)
    raise ColmapDiscoveryError(
        f"Sparse 후보가 {len(candidates)}개입니다. 가져올 모델 폴더를 직접 선택하십시오: {paths}"
    )


def _create_sparse_model_layout(
    dataset_root: Path,
    input_format: InputFormat,
) -> SparseModelLayout:
    """탐색 기준 폴더에서 단일 sparse model의 읽기 경로 묶음을 생성합니다."""
    source_path, selected_format = _resolve_model_directory(dataset_root, input_format)
    rigs_path, frames_path = _resolve_modern_files(source_path, selected_format)
    return SparseModelLayout(
        source_path=source_path,
        selected_format=selected_format,
        cameras_path=_file_path(source_path, "cameras", selected_format),
        images_path=_file_path(source_path, "images", selected_format),
        points3d_path=_file_path(source_path, "points3D", selected_format),
        rigs_path=rigs_path,
        frames_path=frames_path,
    )


def _create_standard_dataset_layout(
    dataset_root: Path,
    input_format: InputFormat,
) -> SparseModelLayout:
    """Dataset Root 바로 아래의 sparse/0 또는 sparse 모델만 우선순위대로 확인합니다."""
    for source_path in (dataset_root / "sparse" / "0", dataset_root / "sparse"):
        try:
            selected_format = _resolve_layout_format(source_path, input_format)
            rigs_path, frames_path = _resolve_modern_files(source_path, selected_format)
        except ColmapDiscoveryError:
            continue
        return SparseModelLayout(
            source_path=source_path.resolve(),
            selected_format=selected_format,
            cameras_path=_file_path(source_path, "cameras", selected_format),
            images_path=_file_path(source_path, "images", selected_format),
            points3d_path=_file_path(source_path, "points3D", selected_format),
            rigs_path=rigs_path,
            frames_path=frames_path,
        )
    raise ColmapDiscoveryError(
        f"표준 sparse/0 또는 sparse 모델을 찾지 못했습니다: {dataset_root}"
    )


def _is_safe_images_directory(dataset_root: Path) -> bool:
    """Dataset Root의 images가 링크가 아닌 실제 폴더인지 확인합니다."""
    images_path = dataset_root / "images"
    if not images_path.is_dir() or images_path.is_symlink():
        return False
    return not (hasattr(os.path, "isjunction") and os.path.isjunction(images_path))


def _matches_standard_dataset_root(
    dataset_root: Path,
    selected_path: Path,
    source_path: Path,
) -> bool:
    """표준 sparse 구조와 선택 위치를 근거로 실제 Dataset Root인지 판정합니다."""
    try:
        source_relative = source_path.relative_to(dataset_root)
        selected_relative = selected_path.relative_to(dataset_root)
    except ValueError:
        return False
    if not source_relative.parts or source_relative.parts[0].casefold() != "sparse":
        return False
    if not selected_relative.parts:
        return True
    selected_top_level = selected_relative.parts[0].casefold()
    return selected_top_level == "sparse" or _is_safe_images_directory(dataset_root)


def discover_sparse_model_with_dataset_root(
    path: str | Path,
    input_format: str = "AUTO",
) -> tuple[Path, SparseModelLayout]:
    """선택한 하위 폴더에서 가장 가까운 표준 Dataset Root와 sparse model을 찾습니다."""
    selected_path = _normalize_source_path(path)
    requested_format = _normalize_input_format(input_format)
    LOGGER.info(
        "event=dataset_root_discovery_started stage=file_check selected=%s "
        "requested_format=%s",
        selected_path,
        requested_format,
    )
    for candidate_root in (selected_path, *selected_path.parents):
        try:
            layout = _create_standard_dataset_layout(candidate_root, requested_format)
        except ColmapDiscoveryError:
            continue
        if not _matches_standard_dataset_root(
            candidate_root,
            selected_path,
            layout.source_path,
        ):
            continue
        LOGGER.info(
            "event=dataset_root_discovery_completed stage=file_check selected=%s "
            "dataset_root=%s source=%s format=%s layout=%s",
            selected_path,
            candidate_root,
            layout.source_path,
            layout.selected_format,
            layout.layout_kind,
        )
        return candidate_root, layout

    try:
        selected_layout = _create_sparse_model_layout(selected_path, requested_format)
    except ColmapDiscoveryError:
        LOGGER.warning(
            "event=dataset_root_discovery_failed stage=file_check selected=%s",
            selected_path,
        )
        raise
    LOGGER.info(
        "event=dataset_root_discovery_completed stage=file_check selected=%s "
        "dataset_root=%s source=%s format=%s layout=%s fallback=true",
        selected_path,
        selected_path,
        selected_layout.source_path,
        selected_layout.selected_format,
        selected_layout.layout_kind,
    )
    return selected_path, selected_layout


def discover_sparse_model(
    path: str | Path,
    input_format: str = "AUTO",
) -> SparseModelLayout:
    """Dataset Root에서 기준 Importer와 같은 규칙으로 sparse model을 찾습니다."""
    dataset_root = _normalize_source_path(path)
    requested_format = _normalize_input_format(input_format)
    LOGGER.info(
        "event=file_discovery_started stage=file_check source=%s requested_format=%s",
        dataset_root,
        requested_format,
    )
    try:
        layout = _create_sparse_model_layout(dataset_root, requested_format)
    except ColmapDiscoveryError:
        LOGGER.warning("event=file_discovery_failed stage=file_check source=%s", dataset_root)
        raise
    if layout.source_path != dataset_root:
        LOGGER.info(
            "event=file_discovery_resolved stage=file_check selected_root=%s source=%s",
            dataset_root,
            layout.source_path,
        )
    LOGGER.info(
        "event=file_discovery_completed stage=file_check format=%s layout=%s",
        layout.selected_format,
        layout.layout_kind,
    )
    return layout
