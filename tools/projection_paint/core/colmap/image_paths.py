"""COLMAP 이미지 상대경로를 Reparse Point 없이 안전하게 해석합니다."""

from __future__ import annotations

import os
import stat
from collections.abc import Generator
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Iterable

from .errors import ColmapError
from .io_common import (
    CancelCheck,
    check_cancel,
    consume_generator,
    identity_from_stat,
)
from .model import (
    DEFAULT_LIMITS,
    ImagePathEntry,
    ImagePathManifest,
    ImagePoseRecord,
    ParserLimits,
    SourceFileIdentity,
)

EXACT = "EXACT"
FALLBACK = "FALLBACK"
MISSING = "MISSING"
AMBIGUOUS = "AMBIGUOUS"

_WINDOWS_RESERVED = frozenset(
    {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{index}" for index in range(1, 10)),
        *(f"LPT{index}" for index in range(1, 10)),
    }
)
_FALLBACK_EXTENSIONS = frozenset(
    {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".exr", ".bmp", ".webp"}
)


def _is_reparse(path: Path) -> bool:
    """Path가 Symlink, Junction 또는 Reparse Point인지 반환합니다."""
    try:
        if path.is_symlink():
            return True
        is_junction = getattr(os.path, "isjunction", None)
        if is_junction is not None and is_junction(path):
            return True
        result = os.lstat(path)
        attributes = getattr(result, "st_file_attributes", 0)
        flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        return bool(flag and attributes & flag)
    except OSError:
        return True


def _validate_component(component: str, image_name: str) -> None:
    """이미지 상대경로 구성요소의 Windows 위험 이름을 거부합니다."""
    if (
        not component
        or component in {".", ".."}
        or component.endswith((" ", "."))
        or ":" in component
        or any(ord(character) < 32 for character in component)
    ):
        raise ColmapError(f"안전하지 않은 이미지 경로입니다: {image_name}")
    stem = component.split(".", 1)[0].rstrip(" .").upper()
    if stem in _WINDOWS_RESERVED:
        raise ColmapError(
            f"Windows 예약 장치 이름을 사용한 이미지 경로입니다: {image_name}"
        )


def normalize_relative_image_path(image_name: str) -> str:
    """COLMAP 이미지 이름을 검증된 POSIX 상대경로로 정규화합니다."""
    if not isinstance(image_name, str) or not image_name or "\x00" in image_name:
        raise ColmapError(f"안전하지 않은 이미지 경로입니다: {image_name!r}")
    if image_name.startswith(("\\\\?\\", "\\\\.\\", "//?/")):
        raise ColmapError(f"Windows Device 경로는 허용하지 않습니다: {image_name}")
    normalized = image_name.replace("\\", "/")
    posix_path = PurePosixPath(normalized)
    windows_path = PureWindowsPath(image_name)
    if (
        posix_path.is_absolute()
        or windows_path.is_absolute()
        or bool(windows_path.drive)
        or bool(windows_path.root)
        or ".." in posix_path.parts
        or not posix_path.parts
    ):
        raise ColmapError(f"절대경로 또는 경로 순회는 허용하지 않습니다: {image_name}")
    for component in posix_path.parts:
        _validate_component(component, image_name)
    return PurePosixPath(*posix_path.parts).as_posix()


def _file_identity(path: Path) -> SourceFileIdentity:
    """일반 파일을 Handle로 열어 안정성 비교용 Identity를 반환합니다."""
    try:
        with path.open("rb") as stream:
            result = os.fstat(stream.fileno())
    except OSError as exc:
        raise ColmapError(f"이미지 파일을 열 수 없습니다: {path}") from exc
    return identity_from_stat(path, result)


class ImagePathResolver:
    """한 번 정규화한 Images Root에서 IMAGE_ID 경로를 해석합니다."""

    __slots__ = ("root", "_directory_cache")

    def __init__(self, images_root: Path | str) -> None:
        """Images Root의 존재와 Reparse Point 여부를 검증합니다."""
        requested = Path(images_root).expanduser()
        if _is_reparse(requested):
            raise ColmapError(
                f"Images Root Symlink/Junction/Reparse Point는 허용하지 않습니다: "
                f"{requested}"
            )
        try:
            root = requested.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise ColmapError(f"Images Root를 찾을 수 없습니다: {images_root}") from exc
        if not root.is_dir():
            raise ColmapError(f"Images Root가 Directory가 아닙니다: {root}")
        self.root = root
        self._directory_cache: dict[Path, tuple[Path, ...]] = {}

    def _ensure_components_safe(self, relative_path: str) -> Path:
        """Root 아래 경로 구성요소에 Reparse Point가 없는지 확인합니다."""
        current = self.root
        for component in PurePosixPath(relative_path).parts:
            current = current / component
            if os.path.lexists(current) and _is_reparse(current):
                raise ColmapError(
                    f"이미지 경로의 Symlink/Junction/Reparse Point는 "
                    f"허용하지 않습니다: {current}"
                )
        return current

    def _canonical_inside_root(self, candidate: Path, image_name: str) -> Path:
        """후보의 Canonical 경로가 Images Root 내부인지 확인합니다."""
        try:
            resolved = candidate.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise ColmapError(
                f"이미지 경로를 안전하게 해석할 수 없습니다: {image_name}"
            ) from exc
        try:
            resolved.relative_to(self.root)
        except ValueError as exc:
            raise ColmapError(
                f"이미지 경로가 Images Root 밖을 가리킵니다: {image_name}"
            ) from exc
        return resolved

    def _fallback_candidates(self, parent: Path, stem: str) -> tuple[Path, ...]:
        """Parent별 Cache에서 허용 확장자의 동일 Stem 후보를 반환합니다."""
        cached = self._directory_cache.get(parent)
        if cached is None:
            try:
                entries = tuple(Path(entry.path) for entry in os.scandir(parent))
            except OSError as exc:
                raise ColmapError(f"이미지 후보 Directory를 읽을 수 없습니다: {parent}") from exc
            safe_entries: list[Path] = []
            for entry in entries:
                if _is_reparse(entry):
                    continue
                try:
                    if entry.is_file() and entry.suffix.casefold() in _FALLBACK_EXTENSIONS:
                        safe_entries.append(entry)
                except OSError:
                    continue
            cached = tuple(
                sorted(safe_entries, key=lambda item: item.name.casefold())
            )
            self._directory_cache[parent] = cached
        return tuple(
            candidate
            for candidate in cached
            if candidate.stem.casefold() == stem.casefold()
        )

    def resolve(
        self,
        image_name: str,
        *,
        allow_extension_fallback: bool = False,
    ) -> tuple[Path | None, str, str]:
        """이미지 이름을 Exact 우선의 Canonical 경로로 해석합니다."""
        normalized = normalize_relative_image_path(image_name)
        candidate = self._ensure_components_safe(normalized)
        if candidate.is_file():
            resolved = self._canonical_inside_root(candidate, image_name)
            if _is_reparse(candidate):
                raise ColmapError(f"Reparse 이미지 파일은 허용하지 않습니다: {candidate}")
            return resolved, EXACT, normalized
        if not allow_extension_fallback:
            return None, MISSING, normalized
        parent = candidate.parent
        if not parent.is_dir() or _is_reparse(parent):
            return None, MISSING, normalized
        candidates = self._fallback_candidates(parent, candidate.stem)
        if not candidates:
            return None, MISSING, normalized
        if len(candidates) > 1:
            return None, AMBIGUOUS, normalized
        resolved = self._canonical_inside_root(candidates[0], image_name)
        return resolved, FALLBACK, normalized


def resolve_image_path(
    images_root: Path | str,
    image_name: str,
    *,
    allow_extension_fallback: bool = False,
) -> tuple[Path | None, str]:
    """단일 이미지 이름을 검증된 Canonical 경로와 상태로 해석합니다."""
    resolver = ImagePathResolver(images_root)
    path, status, _normalized = resolver.resolve(
        image_name,
        allow_extension_fallback=allow_extension_fallback,
    )
    return path, status


def build_image_path_manifest_steps(
    images_root: Path | str,
    images: Iterable[ImagePoseRecord],
    *,
    allow_extension_fallback: bool = False,
    limits: ParserLimits = DEFAULT_LIMITS,
    cancel_check: CancelCheck = None,
) -> Generator[tuple[int, int], None, ImagePathManifest]:
    """Image 경로와 파일 Identity를 증분 검증하며 처리량을 전달합니다."""
    resolver = ImagePathResolver(images_root)
    entries: dict[int, ImagePathEntry] = {}
    canonical_owners: dict[str, int] = {}
    identity_owners: dict[tuple[int, int], int] = {}
    ordered = sorted(images, key=lambda item: item.image_id)
    total = len(ordered)
    yield 0, total
    for index, image in enumerate(ordered):
        if index % limits.cancel_check_interval == 0:
            check_cancel(cancel_check)
        resolved, status, normalized = resolver.resolve(
            image.image_name,
            allow_extension_fallback=allow_extension_fallback,
        )
        if resolved is None:
            raise ColmapError(
                f"IMAGE_ID {image.image_id}의 이미지 경로를 확정하지 못했습니다: "
                f"{image.image_name} ({status})"
            )
        identity = _file_identity(resolved)
        canonical_key = os.path.normcase(str(resolved)).casefold()
        previous_id = canonical_owners.get(canonical_key)
        if previous_id is not None:
            raise ColmapError(
                f"서로 다른 IMAGE_ID가 같은 Canonical 파일을 가리킵니다: "
                f"{previous_id}, {image.image_id}"
            )
        file_key = (identity.device, identity.inode)
        if identity.inode != 0 and file_key in identity_owners:
            raise ColmapError(
                f"서로 다른 IMAGE_ID가 같은 파일 Identity를 가리킵니다: "
                f"{identity_owners[file_key]}, {image.image_id}"
            )
        canonical_owners[canonical_key] = image.image_id
        if identity.inode != 0:
            identity_owners[file_key] = image.image_id
        entries[image.image_id] = ImagePathEntry(
            image_id=image.image_id,
            image_name=image.image_name,
            normalized_relative_path=normalized,
            resolved_path=resolved,
            path_status=status,
            file_identity=identity,
        )
        if (index + 1) % limits.cancel_check_interval == 0:
            yield index + 1, total
    check_cancel(cancel_check)
    return ImagePathManifest(images_root=resolver.root, entries=entries)


def build_image_path_manifest(
    images_root: Path | str,
    images: Iterable[ImagePoseRecord],
    *,
    allow_extension_fallback: bool = False,
    limits: ParserLimits = DEFAULT_LIMITS,
    cancel_check: CancelCheck = None,
) -> ImagePathManifest:
    """증분 Image 경로 검사를 동기 완료해 Manifest를 반환합니다."""

    return consume_generator(
        build_image_path_manifest_steps(
            images_root,
            images,
            allow_extension_fallback=allow_extension_fallback,
            limits=limits,
            cancel_check=cancel_check,
        )
    )


def revalidate_image_path_entry(
    manifest: ImagePathManifest,
    image_id: int,
) -> ImagePathEntry:
    """실제 Source Load 직전에 한 IMAGE_ID의 경로와 Identity를 재검증합니다."""
    try:
        expected = manifest.entries[image_id]
    except KeyError as exc:
        raise ColmapError(f"Path Manifest에 IMAGE_ID가 없습니다: {image_id}") from exc
    resolver = ImagePathResolver(manifest.images_root)
    current_path, status, normalized = resolver.resolve(
        expected.image_name,
        allow_extension_fallback=expected.path_status == FALLBACK,
    )
    if (
        current_path is None
        or current_path != expected.resolved_path
        or status != expected.path_status
        or normalized != expected.normalized_relative_path
    ):
        raise ColmapError(
            f"IMAGE_ID {image_id}의 Canonical 이미지 경로가 변경되었습니다."
        )
    current_identity = _file_identity(current_path)
    if not expected.file_identity.same_file(current_identity):
        raise ColmapError(
            f"IMAGE_ID {image_id}의 이미지 파일 Identity가 변경되었습니다."
        )
    return expected
