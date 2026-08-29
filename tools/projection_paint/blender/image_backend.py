"""관리 Paint Image의 SINGLE·UDIM 백엔드 계약을 제공합니다."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import shutil
import tempfile
from typing import Any
import uuid

from .udim_setup import (
    STAGING_DIRECTORY_KEY,
    UDIMSetupError,
    new_tiled_paint_image,
    paint_staging_paths,
    remove_paint_staging_path,
)


IMAGE_BACKEND_KEY = "pp_image_backend"
SINGLE_BACKEND = "SINGLE"
UDIM_BACKEND = "UDIM"
IMAGE_BACKENDS = (SINGLE_BACKEND, UDIM_BACKEND)


class PaintImageContractError(RuntimeError):
    """관리 Paint Image 백엔드 계약 위반을 알립니다."""


@dataclass(frozen=True, slots=True)
class PaintImageContract:
    """Image 백엔드와 논리 타일별 해상도를 불변 값으로 보관합니다."""

    backend: str
    tile_numbers: tuple[int, ...]
    tile_sizes: tuple[tuple[int, int], ...]


def _normalized_backend(value: object) -> str:
    """외부 백엔드 값을 지원 Enum 문자열로 정규화합니다."""

    backend = str(value or "").upper()
    if backend not in IMAGE_BACKENDS:
        raise PaintImageContractError(f"지원하지 않는 Paint Image 백엔드입니다: {backend or 'EMPTY'}")
    return backend


def _normalized_numbers(tile_numbers: tuple[int, ...]) -> tuple[int, ...]:
    """논리 타일 번호를 중복 없는 오름차순 값으로 검증합니다."""

    numbers = tuple(sorted(int(number) for number in tile_numbers))
    if not numbers or numbers[0] != 1001 or len(set(numbers)) != len(numbers):
        raise PaintImageContractError("논리 타일 집합은 중복 없이 1001부터 시작해야 합니다.")
    return numbers


def backend_from_layout(layout_or_kind: Any) -> str:
    """입력 Layout 종류를 SINGLE 또는 UDIM 백엔드로 변환합니다."""

    kind = str(getattr(layout_or_kind, "kind", layout_or_kind) or "")
    if kind in {"SINGLE_1001", "UV_ONLY"}:
        return SINGLE_BACKEND
    if kind in {"EXISTING_UDIM", "MULTI_MATERIAL_TILES"}:
        return UDIM_BACKEND
    raise PaintImageContractError(f"지원하지 않는 Paint Layout 종류입니다: {kind or 'EMPTY'}")


def backend_from_target(
    target: Any,
    image: Any | None = None,
    *,
    allow_legacy: bool = True,
) -> str:
    """Target의 명시 백엔드를 읽고 구형 TILED Image를 UDIM으로 분류합니다."""

    explicit = str(target.get(IMAGE_BACKEND_KEY, "") or "")
    if explicit:
        return _normalized_backend(explicit)
    if not allow_legacy:
        raise PaintImageContractError("관리 Target의 Paint Image 백엔드 기록이 없습니다.")
    if image is not None and str(getattr(image, "source", "") or "") == "TILED":
        return UDIM_BACKEND
    kind = str(target.get("pp_udim_kind", "") or "")
    return backend_from_layout(kind)


def image_contract(image: Any) -> PaintImageContract:
    """Image source를 권위로 백엔드·논리 타일·해상도 계약을 읽습니다."""

    if image is None:
        raise PaintImageContractError("관리 Paint Image가 없습니다.")
    source = str(getattr(image, "source", "") or "")
    if source == "FILE":
        size = tuple(int(value) for value in image.size)
        if len(size) != 2 or min(size) <= 0:
            raise PaintImageContractError("SINGLE Image 해상도가 유효하지 않습니다.")
        return PaintImageContract(SINGLE_BACKEND, (1001,), (size,))
    if source == "TILED":
        tiles = tuple(sorted(image.tiles, key=lambda tile: int(tile.number)))
        numbers = tuple(int(tile.number) for tile in tiles)
        _normalized_numbers(numbers)
        sizes = tuple(tuple(int(value) for value in tile.size) for tile in tiles)
        if any(len(size) != 2 or min(size) <= 0 for size in sizes):
            raise PaintImageContractError("UDIM Image 타일 해상도가 유효하지 않습니다.")
        return PaintImageContract(UDIM_BACKEND, numbers, sizes)
    raise PaintImageContractError(
        f"관리 Paint Image source가 FILE 또는 TILED가 아닙니다: {source or 'UNKNOWN'}"
    )


def validate_image_contract(image: Any, expected: PaintImageContract) -> PaintImageContract:
    """Image의 백엔드·논리 타일·타일별 해상도가 기대 계약과 같은지 검증합니다."""

    normalized = PaintImageContract(
        _normalized_backend(expected.backend),
        _normalized_numbers(expected.tile_numbers),
        tuple((int(size[0]), int(size[1])) for size in expected.tile_sizes),
    )
    if len(normalized.tile_numbers) != len(normalized.tile_sizes):
        raise PaintImageContractError("기대 Image 계약의 타일 번호와 해상도 수가 다릅니다.")
    if normalized.backend == SINGLE_BACKEND and normalized.tile_numbers != (1001,):
        raise PaintImageContractError("SINGLE Image의 논리 타일은 1001 하나여야 합니다.")
    actual = image_contract(image)
    if actual != normalized:
        raise PaintImageContractError(
            "관리 Paint Image 계약이 다릅니다: "
            f"기대 {normalized}, 실제 {actual}"
        )
    return actual


def _new_single_paint_image(
    name: str,
    size: tuple[int, int],
    color: tuple[float, float, float, float],
):
    """애드온 소유 PNG를 거쳐 FILE·Packed SINGLE Image를 생성합니다."""

    import bpy

    width, height = int(size[0]), int(size[1])
    if min(width, height) <= 0:
        raise PaintImageContractError("SINGLE Paint Image 해상도는 0보다 커야 합니다.")
    staging_root = Path(tempfile.gettempdir()) / "blender_projection_paint_staging"
    staging_root.mkdir(parents=True, exist_ok=True)
    root = staging_root / uuid.uuid4().hex
    root.mkdir(parents=False, exist_ok=False)
    path = root / "paint.png"
    image = None
    completed = False
    try:
        image = bpy.data.images.new(
            name=str(name),
            width=width,
            height=height,
            alpha=True,
            float_buffer=False,
            tiled=False,
        )
        image.use_fake_user = True
        image.generated_color = color
        image.filepath_raw = str(path)
        image.filepath = str(path)
        image.file_format = "PNG"
        image[STAGING_DIRECTORY_KEY] = str(root)
        image.save()
        image.source = "FILE"
        image.reload()
        if str(image.source) != "FILE" or tuple(int(value) for value in image.size) != (width, height):
            raise PaintImageContractError("SINGLE Paint Image를 FILE source로 준비하지 못했습니다.")
        image.pack()
        if image.packed_file is None:
            raise PaintImageContractError("SINGLE Paint Image를 Pack하지 못했습니다.")
        validate_image_contract(
            image,
            PaintImageContract(SINGLE_BACKEND, (1001,), ((width, height),)),
        )
        image.use_fake_user = False
        completed = True
        return image
    except Exception:
        if image is not None:
            try:
                image.use_fake_user = False
                if int(image.users) == 0:
                    bpy.data.images.remove(image)
            except (AttributeError, ReferenceError, RuntimeError, TypeError):
                pass
        raise
    finally:
        if not completed:
            shutil.rmtree(root, ignore_errors=True)


def new_managed_paint_image(
    backend: str,
    name: str,
    size: tuple[int, int],
    tile_numbers: tuple[int, ...],
    color: tuple[float, float, float, float],
):
    """요청 백엔드에 맞는 Packed 관리 Paint Image를 생성합니다."""

    resolved = _normalized_backend(backend)
    numbers = _normalized_numbers(tile_numbers)
    if resolved == SINGLE_BACKEND:
        if numbers != (1001,):
            raise PaintImageContractError("SINGLE Paint Image는 논리 타일 1001만 지원합니다.")
        return _new_single_paint_image(name, size, color)
    try:
        image = new_tiled_paint_image(name, size, numbers, color)
    except UDIMSetupError as exc:
        raise PaintImageContractError(str(exc)) from exc
    validate_image_contract(
        image,
        PaintImageContract(
            UDIM_BACKEND,
            numbers,
            tuple((int(size[0]), int(size[1])) for _number in numbers),
        ),
    )
    return image


def managed_staging_paths(image: Any) -> tuple[Path, tuple[Path, ...]]:
    """SINGLE과 UDIM 관리 Image의 검증된 Staging 경로를 반환합니다."""

    contract = image_contract(image)
    if contract.backend == UDIM_BACKEND:
        try:
            return paint_staging_paths(image)
        except UDIMSetupError as exc:
            raise PaintImageContractError(str(exc)) from exc
    import bpy

    raw_root = str(image.get(STAGING_DIRECTORY_KEY, "") or "")
    if not raw_root:
        raise PaintImageContractError("관리 SINGLE Image의 Staging 경로가 없습니다.")
    root = Path(raw_root).resolve()
    allowed = (Path(tempfile.gettempdir()) / "blender_projection_paint_staging").resolve()
    path = Path(bpy.path.abspath(str(image.filepath_raw or image.filepath))).resolve()
    if root == allowed or allowed not in root.parents or path.parent != root or path.name != "paint.png":
        raise PaintImageContractError("관리 SINGLE Image의 Staging 경로 소유권이 유효하지 않습니다.")
    return root, (path,)


def remove_managed_staging(image: Any) -> None:
    """검증된 SINGLE 또는 UDIM 관리 Image의 Staging만 제거합니다."""

    try:
        root, _paths = managed_staging_paths(image)
    except (AttributeError, ReferenceError, RuntimeError, TypeError, PaintImageContractError):
        return
    remove_paint_staging_path(root)


__all__ = [
    "IMAGE_BACKEND_KEY",
    "IMAGE_BACKENDS",
    "PaintImageContract",
    "PaintImageContractError",
    "SINGLE_BACKEND",
    "UDIM_BACKEND",
    "backend_from_layout",
    "backend_from_target",
    "image_contract",
    "managed_staging_paths",
    "new_managed_paint_image",
    "remove_managed_staging",
    "validate_image_contract",
]
