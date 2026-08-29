"""COLMAP 이미지 이름을 Images Directory 내부의 안전한 경로로 해석합니다."""

from __future__ import annotations

import os
import time
from pathlib import Path, PurePosixPath, PureWindowsPath
from collections.abc import Sequence

from .model import ImagePathManifest


def resolve_image_path(images_directory: Path | str, image_name: str) -> Path:
    base = Path(images_directory).expanduser().resolve()
    normalized = image_name.replace("\\", "/")
    posix_path = PurePosixPath(normalized)
    windows_path = PureWindowsPath(image_name)
    if (
        not image_name
        or posix_path.is_absolute()
        or windows_path.is_absolute()
        or windows_path.drive
        or ".." in posix_path.parts
    ):
        raise ValueError(f"안전하지 않은 COLMAP 이미지 경로입니다: {image_name}")
    target = (base / Path(*posix_path.parts)).resolve()
    try:
        if os.path.commonpath((str(base), str(target))) != str(base):
            raise ValueError(f"Images Directory 밖을 가리키는 이미지 경로입니다: {image_name}")
    except ValueError as exc:
        raise ValueError(f"Images Directory 밖을 가리키는 이미지 경로입니다: {image_name}") from exc
    return target


def validate_image_files(
    images_directory: Path | str,
    image_names: Sequence[str],
    *,
    incremental: bool = False,
) -> tuple[Path, ...] | "ImagePathValidationJob":
    """기존 동기 API와 증분 Job이 같은 이미지 검증 구현을 사용합니다."""

    job = ImagePathValidationJob(images_directory, image_names)
    if incremental:
        return job
    while not job.done:
        job.advance()
    return job.result


class ImagePathValidationJob:
    """COLMAP Image 이름을 경로 단위로 안전하게 검증합니다."""

    _MAX_PATHS_PER_ADVANCE = 256

    def __init__(
        self,
        images_directory: Path | str,
        image_names: Sequence[str],
    ) -> None:
        self._base = Path(images_directory).expanduser().resolve()
        if not self._base.is_dir():
            raise ValueError(f"Images Directory가 존재하지 않습니다: {self._base}")
        self._image_names = tuple(image_names)
        self._index = 0
        self._missing: list[str] = []
        self._resolved_paths: list[Path] = []
        self._result: tuple[Path, ...] | None = None

    @property
    def done(self) -> bool:
        return self._result is not None

    @property
    def completed_units(self) -> int:
        return self._index

    @property
    def total_units(self) -> int:
        return len(self._image_names)

    @property
    def result(self) -> tuple[Path, ...]:
        if self._result is None:
            raise RuntimeError("완료되지 않은 이미지 경로 검사 Job의 결과를 읽을 수 없습니다.")
        return self._result

    def advance(self, work_budget_ms: float | None = None) -> None:
        if self.done:
            raise RuntimeError("완료된 이미지 경로 검사 Job을 다시 실행할 수 없습니다.")
        deadline = (
            None
            if work_budget_ms is None
            else time.perf_counter() + (work_budget_ms / 1000.0)
        )
        processed = 0
        while self._index < len(self._image_names):
            name = self._image_names[self._index]
            target = resolve_image_path(self._base, name)
            if not target.is_file():
                self._missing.append(name)
            self._resolved_paths.append(target)
            self._index += 1
            processed += 1
            if processed >= self._MAX_PATHS_PER_ADVANCE:
                return
            if deadline is not None and time.perf_counter() >= deadline:
                return

        if self._missing:
            shown = ", ".join(self._missing[:5])
            suffix = (
                f" 외 {len(self._missing) - 5}개"
                if len(self._missing) > 5
                else ""
            )
            raise ValueError(
                "Images Directory에서 이미지 파일을 찾지 못했습니다: "
                f"{shown}{suffix}"
            )
        self._result = tuple(self._resolved_paths)

def validate_image_path_manifest_current(manifest: ImagePathManifest) -> None:
    """Loader 검증 뒤 이미지의 존재 여부와 canonical 경로를 다시 확인합니다."""

    missing: list[str] = []
    changed: list[str] = []
    for entry in manifest.entries.values():
        stored_path = entry.resolved_image_path
        try:
            current_path = stored_path.resolve(strict=True)
        except (OSError, RuntimeError):
            missing.append(entry.image_name)
            continue
        if current_path != stored_path or not current_path.is_file():
            changed.append(entry.image_name)

    if missing:
        shown = ", ".join(missing[:5])
        suffix = f" 외 {len(missing) - 5}개" if len(missing) > 5 else ""
        raise ValueError(f"Images Directory에서 이미지 파일을 찾지 못했습니다: {shown}{suffix}")
    if changed:
        shown = ", ".join(changed[:5])
        suffix = f" 외 {len(changed) - 5}개" if len(changed) > 5 else ""
        raise ValueError(
            "Loader 검증 뒤 이미지 canonical 경로가 변경되었습니다: "
            f"{shown}{suffix}"
        )
