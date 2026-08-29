"""단일 관리 재질에 사용할 UDIM 입력을 분석하고 준비합니다."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import math
from pathlib import Path
import re
import shutil
import tempfile
from typing import Any
import uuid


class UDIMSetupError(RuntimeError):
    """지원하지 않는 UDIM 입력 구조를 알립니다."""


_UVTILE_RE = re.compile(
    r"u(?P<u>[1-9][0-9]*)_v(?P<v>[1-9][0-9]*)",
    re.IGNORECASE,
)
_UV_EPSILON = 1.0e-5

UDIM_KIND_KEY = "pp_udim_kind"
UDIM_TILES_KEY = "pp_udim_tiles"
UDIM_SIGNATURE_KEY = "pp_udim_signature"
UDIM_UV_KEY = "pp_udim_uv"
UDIM_SOURCE_UV_KEY = "pp_udim_source_uv"
UDIM_MANAGED_SLOT_KEY = "pp_udim_managed_slot"
ORIGINAL_ACTIVE_SLOT_KEY = "pp_original_active_material_slot"
ORIGINAL_FACE_ATTRIBUTE = "pp_original_material_index"
PAINT_UV_NAME = "PP_UDIM"
STAGING_DIRECTORY_KEY = "pp_udim_staging_directory"


@dataclass(frozen=True, slots=True)
class UDIMTile:
    """하나의 UDIM 타일 번호와 UV 오프셋을 보관합니다."""

    number: int
    offset_u: int
    offset_v: int
    width: int = 0
    height: int = 0


@dataclass(frozen=True, slots=True)
class UDIMSource:
    """사용 재질 슬롯과 원본 이미지의 타일 대응을 보관합니다."""

    slot_index: int
    material: Any
    image: Any | None
    tile: UDIMTile
    needs_offset: bool = False


@dataclass(frozen=True, slots=True)
class UDIMLayout:
    """입력 재질을 정규화한 단일 UDIM 전환 계획을 보관합니다."""

    kind: str
    tiles: tuple[UDIMTile, ...]
    sources: tuple[UDIMSource, ...] = ()
    source_uv_name: str = ""
    source_pattern: str = ""
    signature: str = ""

    @property
    def tile_numbers(self) -> tuple[int, ...]:
        """오름차순 UDIM 타일 번호를 반환합니다."""
        return tuple(tile.number for tile in self.tiles)

    @property
    def slot_tiles(self) -> dict[int, UDIMTile]:
        """원본 재질 슬롯별 UDIM 타일을 반환합니다."""
        return {source.slot_index: source.tile for source in self.sources}


def udim_number(u: int, v: int) -> int:
    """1부터 시작하는 UV 타일 좌표를 Blender UDIM 번호로 변환합니다."""
    if u < 1 or v < 1:
        raise UDIMSetupError("UV 타일 좌표는 1 이상이어야 합니다.")
    return 1001 + (u - 1) + (v - 1) * 10


def tile_from_uvtile_name(filepath: str) -> UDIMTile | None:
    """파일명에서 u1_v1 형식의 UDIM 타일 정보를 읽습니다."""
    match = _UVTILE_RE.search(Path(filepath).name)
    if match is None:
        return None
    u, v = int(match.group("u")), int(match.group("v"))
    return UDIMTile(udim_number(u, v), u - 1, v - 1)


def _udim_tile(number: int, width: int = 0, height: int = 0) -> UDIMTile:
    """Blender UDIM 번호를 내부 타일 값으로 변환합니다."""
    if int(number) < 1001:
        raise UDIMSetupError("UDIM 타일 번호는 1001 이상이어야 합니다.")
    offset = int(number) - 1001
    return UDIMTile(int(number), offset % 10, offset // 10, width, height)


def _active_principled(material: Any) -> Any:
    """지원되는 유일한 활성 Principled BSDF를 반환합니다."""
    if material is None or not material.use_nodes or material.node_tree is None:
        raise UDIMSetupError("사용 재질은 Node 기반 Principled BSDF여야 합니다.")
    outputs = tuple(
        node
        for node in material.node_tree.nodes
        if node.type == "OUTPUT_MATERIAL" and bool(node.is_active_output)
    )
    if len(outputs) != 1:
        raise UDIMSetupError("사용 재질마다 활성 Material Output이 하나여야 합니다.")
    surface = outputs[0].inputs.get("Surface")
    if surface is None or len(surface.links) != 1:
        raise UDIMSetupError("Material Output은 Principled BSDF에 직접 연결되어야 합니다.")
    shader = surface.links[0].from_node
    if shader.type != "BSDF_PRINCIPLED":
        raise UDIMSetupError("사용 재질은 Principled BSDF 구조여야 합니다.")
    return shader


def _base_image_node(material: Any) -> Any | None:
    """Principled Base Color에 직접 연결된 Image Texture 노드를 반환합니다."""
    shader = _active_principled(material)
    base = shader.inputs.get("Base Color")
    if base is None:
        raise UDIMSetupError("Principled BSDF Base Color 입력이 없습니다.")
    if len(base.links) == 0:
        return None
    if len(base.links) != 1 or base.links[0].from_node.type != "TEX_IMAGE":
        raise UDIMSetupError("Base Color는 Image Texture에 직접 연결되어야 합니다.")
    node = base.links[0].from_node
    if node.image is None:
        raise UDIMSetupError("Base Color Image Texture에 이미지가 없습니다.")
    return node


def _socket_default(socket: Any) -> tuple[Any, ...] | float | int | str | bool | None:
    """Shader 입력 기본값을 비교 가능한 값으로 변환합니다."""
    value = getattr(socket, "default_value", None)
    if value is None:
        return None
    if isinstance(value, (str, bool, int, float)):
        return value
    try:
        return tuple(round(float(item), 8) for item in value)
    except (TypeError, ValueError):
        return str(value)


def _shader_signature(material: Any) -> tuple[Any, ...]:
    """Base Image만 제외한 지원 Shader 구조의 비교 서명을 만듭니다."""
    shader = _active_principled(material)
    image_node = _base_image_node(material)
    values = []
    for socket in shader.inputs:
        if socket.name == "Base Color":
            values.append((socket.name, _socket_default(socket)))
            continue
        if len(socket.links):
            raise UDIMSetupError(
                "다중 재질은 Base Color 외 입력에 연결된 Shader 구조를 지원하지 않습니다."
            )
        values.append((socket.name, _socket_default(socket)))
    image_settings = (
        str(image_node.extension),
        str(image_node.interpolation),
        str(image_node.projection),
        round(float(image_node.projection_blend), 8),
        str(image_node.image.colorspace_settings.name),
        str(image_node.image.alpha_mode),
    )
    return tuple(values), image_settings


def _absolute_image_path(image: Any) -> Path:
    """Blender 상대 경로를 해석한 실제 Source Image 경로를 반환합니다."""
    import bpy

    raw = str(image.filepath or image.filepath_raw or "")
    if not raw:
        raise UDIMSetupError("Base Color Image에 원본 파일 경로가 없습니다.")
    path = Path(bpy.path.abspath(raw)).resolve()
    if not path.is_file():
        raise UDIMSetupError(f"Base Color 원본 파일을 찾을 수 없습니다: {path}")
    return path


def _uvtile_pattern(path: Path) -> tuple[str, UDIMTile]:
    """원본 파일 경로를 공통 UVTILE 토큰 패턴으로 변환합니다."""
    matches = tuple(_UVTILE_RE.finditer(path.name))
    if len(matches) != 1:
        raise UDIMSetupError("파일명에는 u1_v1 형식의 타일 표기가 정확히 하나여야 합니다.")
    match = matches[0]
    tile = tile_from_uvtile_name(path.name)
    if tile is None:
        raise UDIMSetupError("파일명에서 UDIM 타일 번호를 결정하지 못했습니다.")
    filename = f"{path.name[:match.start()]}<UVTILE>{path.name[match.end():]}"
    return str(path.with_name(filename)), tile


def _used_slot_indices(target: Any) -> tuple[int, ...]:
    """실제 폴리곤이 참조하는 재질 슬롯만 오름차순으로 반환합니다."""
    indices = tuple(sorted({int(polygon.material_index) for polygon in target.data.polygons}))
    if not indices:
        raise UDIMSetupError("Paint Target Mesh에 폴리곤이 없습니다.")
    return indices


def _slot_uv_values(target: Any, slot_index: int) -> tuple[tuple[float, float], ...]:
    """한 재질 슬롯을 사용하는 모든 Loop의 활성 UV 좌표를 반환합니다."""
    uv = target.data.uv_layers.active
    if uv is None:
        raise UDIMSetupError("활성 UV Map이 없습니다.")
    values = tuple(
        (float(uv.data[index].uv.x), float(uv.data[index].uv.y))
        for polygon in target.data.polygons
        if int(polygon.material_index) == int(slot_index)
        for index in polygon.loop_indices
    )
    if not values:
        raise UDIMSetupError("사용 재질 슬롯에 대응하는 UV 좌표가 없습니다.")
    if any(not math.isfinite(u) or not math.isfinite(v) for u, v in values):
        raise UDIMSetupError("UV 좌표에 NaN 또는 Inf가 포함되어 있습니다.")
    return values


def _inside_tile(values: tuple[tuple[float, float], ...], tile: UDIMTile) -> bool:
    """모든 UV 좌표가 지정 타일의 허용 경계 안에 있는지 확인합니다."""
    return all(
        tile.offset_u - _UV_EPSILON <= u <= tile.offset_u + 1.0 + _UV_EPSILON
        and tile.offset_v - _UV_EPSILON <= v <= tile.offset_v + 1.0 + _UV_EPSILON
        for u, v in values
    )


def _layout_signature(
    kind: str,
    source_uv_name: str,
    sources: tuple[UDIMSource, ...],
) -> str:
    """Layout 재검증에 사용할 결정적 SHA-256 서명을 만듭니다."""
    parts = [kind, source_uv_name]
    parts.extend(
        f"{source.slot_index}:{source.tile.number}:{int(source.needs_offset)}"
        for source in sources
    )
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def _single_layout(
    target: Any,
    kind: str,
    slot_index: int,
    material: Any | None,
    image: Any | None,
    tiles: tuple[UDIMTile, ...],
) -> UDIMLayout:
    """단일 재질·UV 전용 입력의 Layout을 검증해 만듭니다."""
    values = _slot_uv_values(target, slot_index)
    if kind in {"SINGLE_1001", "UV_ONLY"}:
        if not _inside_tile(values, tiles[0]):
            raise UDIMSetupError("일반 Texture 또는 UV 전용 입력의 UV는 0..1 안에 있어야 합니다.")
    elif not all(any(_inside_tile((value,), tile) for tile in tiles) for value in values):
        raise UDIMSetupError("기존 UDIM UV와 Source Image 타일 집합이 일치하지 않습니다.")
    source = UDIMSource(slot_index, material, image, tiles[0], False)
    source_uv_name = str(target.data.uv_layers.active.name)
    sources = (source,)
    return UDIMLayout(
        kind,
        tiles,
        sources,
        source_uv_name,
        "",
        _layout_signature(kind, source_uv_name, sources),
    )


def analyze_udim_source(target: Any) -> UDIMLayout:
    """일반 Texture·기존 UDIM·타일 재질·UV 전용 입력을 분류합니다."""
    used_slots = _used_slot_indices(target)
    slot_count = len(target.material_slots)
    if any(index < 0 or index >= max(slot_count, 1) for index in used_slots):
        raise UDIMSetupError("폴리곤이 존재하지 않는 재질 슬롯을 참조합니다.")
    materials = tuple(
        target.material_slots[index].material if index < slot_count else None
        for index in used_slots
    )
    source_uv_name = str(target.data.uv_layers.active.name)
    if len(used_slots) == 1 and materials[0] is None:
        return _single_layout(
            target,
            "UV_ONLY",
            used_slots[0],
            None,
            None,
            (UDIMTile(1001, 0, 0),),
        )
    if any(material is None for material in materials):
        raise UDIMSetupError("여러 재질 입력에는 비어 있는 사용 재질 슬롯이 없어야 합니다.")
    image_nodes = tuple(_base_image_node(material) for material in materials)
    images = tuple(node.image if node is not None else None for node in image_nodes)
    if len(used_slots) == 1:
        material, image = materials[0], images[0]
        if image is None:
            return _single_layout(
                target,
                "UV_ONLY",
                used_slots[0],
                material,
                None,
                (UDIMTile(1001, 0, 0),),
            )
        if str(image.source) == "TILED":
            tiles = tuple(
                sorted(
                    (
                        _udim_tile(
                            int(tile.number),
                            int(tile.size[0]),
                            int(tile.size[1]),
                        )
                        for tile in image.tiles
                    ),
                    key=lambda tile: tile.number,
                )
            )
            if not tiles or tiles[0].number != 1001:
                raise UDIMSetupError("기존 UDIM Source에는 1001 타일이 있어야 합니다.")
            return _single_layout(
                target,
                "EXISTING_UDIM",
                used_slots[0],
                material,
                image,
                tiles,
            )
        size = tuple(int(value) for value in image.size)
        tile = UDIMTile(1001, 0, 0, size[0], size[1])
        return _single_layout(
            target,
            "SINGLE_1001",
            used_slots[0],
            material,
            image,
            (tile,),
        )
    if any(image is None for image in images):
        raise UDIMSetupError("여러 사용 재질은 모두 직접 Base Color Image가 필요합니다.")
    signatures = tuple(_shader_signature(material) for material in materials)
    if len(set(signatures)) != 1:
        raise UDIMSetupError("여러 사용 재질은 Base Color Image 외 Shader 구조가 같아야 합니다.")
    paths = tuple(_absolute_image_path(image) for image in images)
    patterns_and_tiles = tuple(_uvtile_pattern(path) for path in paths)
    patterns = tuple(value[0] for value in patterns_and_tiles)
    if len({pattern.casefold() for pattern in patterns}) != 1:
        raise UDIMSetupError("여러 Base Color 파일이 하나의 UVTILE 패턴으로 묶이지 않습니다.")
    tiles_without_size = tuple(value[1] for value in patterns_and_tiles)
    if len({tile.number for tile in tiles_without_size}) != len(tiles_without_size):
        raise UDIMSetupError("사용 재질과 UDIM 타일이 1:1로 대응하지 않습니다.")
    if 1001 not in {tile.number for tile in tiles_without_size}:
        raise UDIMSetupError("다중 재질 Source에는 u1_v1(1001) 파일이 있어야 합니다.")
    sources = []
    for slot_index, material, image, raw_tile in zip(
        used_slots,
        materials,
        images,
        tiles_without_size,
    ):
        size = tuple(int(value) for value in image.size)
        tile = UDIMTile(
            raw_tile.number,
            raw_tile.offset_u,
            raw_tile.offset_v,
            size[0],
            size[1],
        )
        values = _slot_uv_values(target, slot_index)
        local = _inside_tile(values, UDIMTile(1001, 0, 0))
        placed = _inside_tile(values, tile)
        if not local and not placed:
            raise UDIMSetupError(
                f"재질 슬롯 {slot_index}의 UV가 로컬 0..1 또는 대응 UDIM 타일 안에 없습니다."
            )
        sources.append(UDIMSource(slot_index, material, image, tile, local and not placed))
    ordered_sources = tuple(sorted(sources, key=lambda source: source.tile.number))
    tiles = tuple(source.tile for source in ordered_sources)
    kind = "MULTI_MATERIAL_TILES"
    return UDIMLayout(
        kind,
        tiles,
        ordered_sources,
        source_uv_name,
        patterns[0],
        _layout_signature(kind, source_uv_name, ordered_sources),
    )


def paint_image_size(
    layout: UDIMLayout,
    width: int,
    height: int,
    *,
    use_base_resolution: bool,
) -> tuple[int, int]:
    """Layout Source 또는 UI 값에서 공통 Paint 타일 해상도를 결정합니다."""
    if int(width) <= 0 or int(height) <= 0:
        raise UDIMSetupError("Paint Image 해상도는 0보다 커야 합니다.")
    if not use_base_resolution:
        return int(width), int(height)
    sizes = {
        (int(tile.width), int(tile.height))
        for tile in layout.tiles
        if int(tile.width) > 0 and int(tile.height) > 0
    }
    if not sizes:
        return int(width), int(height)
    if len(sizes) != 1:
        raise UDIMSetupError(
            "Base Resolution은 모든 Source UDIM 타일 해상도가 같을 때만 사용할 수 있습니다."
        )
    return next(iter(sizes))


def prepare_udim_source_image(layout: UDIMLayout, name: str) -> tuple[Any | None, bool]:
    """다중 파일 Source만 새 Tiled Image로 묶고 차용 여부를 함께 반환합니다."""
    if layout.kind == "UV_ONLY":
        return None, False
    if layout.kind in {"SINGLE_1001", "EXISTING_UDIM"}:
        return layout.sources[0].image, False
    import bpy

    first = next(source for source in layout.sources if source.tile.number == 1001)
    image = bpy.data.images.load(str(_absolute_image_path(first.image)), check_existing=False)
    try:
        image.name = str(name)
        image.filepath = str(layout.source_pattern)
        image.filepath_raw = str(layout.source_pattern)
        image.source = "TILED"
        existing = {int(tile.number) for tile in image.tiles}
        for number in layout.tile_numbers:
            if number not in existing:
                image.tiles.new(int(number))
        image.reload()
        actual = tuple(sorted(int(tile.number) for tile in image.tiles))
        if actual != layout.tile_numbers:
            raise UDIMSetupError("Source Tiled Image의 UDIM 타일 집합이 예상과 다릅니다.")
        for tile, expected in zip(
            sorted(image.tiles, key=lambda item: int(item.number)),
            layout.tiles,
        ):
            size = tuple(int(value) for value in tile.size)
            if size != (expected.width, expected.height):
                raise UDIMSetupError(
                    f"Source UDIM {expected.number} 해상도가 원본 이미지와 다릅니다."
                )
        return image, True
    except Exception:
        try:
            if image.users == 0:
                bpy.data.images.remove(image)
        except (AttributeError, ReferenceError, RuntimeError, TypeError):
            pass
        raise


def new_tiled_paint_image(
    name: str,
    size: tuple[int, int],
    tile_numbers: tuple[int, ...],
    color: tuple[float, float, float, float],
):
    """실제 픽셀 타일과 Packed 파일을 가진 Tiled 이미지를 생성합니다."""
    import bpy

    numbers = tuple(sorted(int(number) for number in tile_numbers))
    if not numbers or len(set(numbers)) != len(numbers) or numbers[0] != 1001:
        raise UDIMSetupError("UDIM 타일 집합은 중복 없이 1001부터 시작해야 합니다.")
    if min(int(size[0]), int(size[1])) <= 0:
        raise UDIMSetupError("UDIM Paint 타일 해상도는 0보다 커야 합니다.")
    staging_root = Path(tempfile.gettempdir()) / "blender_projection_paint_staging"
    staging_root.mkdir(parents=True, exist_ok=True)
    root = staging_root / uuid.uuid4().hex
    root.mkdir(parents=False, exist_ok=False)
    image = None
    completed = False
    try:
        image = bpy.data.images.new(
            name=name,
            width=int(size[0]),
            height=int(size[1]),
            alpha=True,
            float_buffer=False,
            tiled=True,
        )
        image.use_fake_user = True
        image.tiles[0].generated_color = color
        with bpy.context.temp_override(edit_image=image):
            for number in numbers[1:]:
                result = bpy.ops.image.tile_add(
                    number=number,
                    fill=True,
                    color=color,
                    width=int(size[0]),
                    height=int(size[1]),
                    alpha=True,
                )
                if "FINISHED" not in result:
                    raise UDIMSetupError(f"UDIM {number} 픽셀 타일 생성에 실패했습니다.")
        actual = tuple(int(tile.number) for tile in image.tiles)
        if actual != numbers or any(tuple(tile.size) != tuple(size) for tile in image.tiles):
            raise UDIMSetupError("UDIM 타일 픽셀 버퍼를 완전하게 생성하지 못했습니다.")
        image.filepath_raw = str(root / "paint.<UDIM>.png")
        image.filepath = str(root / "paint.<UDIM>.png")
        image.file_format = "PNG"
        image[STAGING_DIRECTORY_KEY] = str(root)
        image.save()
        image.pack()
        if len(image.packed_files) != len(numbers):
            raise UDIMSetupError("UDIM 타일을 모두 Pack하지 못했습니다.")
        image.use_fake_user = False
        completed = True
        return image
    except Exception:
        if image is not None:
            try:
                image.use_fake_user = False
                if image.users == 0:
                    bpy.data.images.remove(image)
            except (AttributeError, ReferenceError, RuntimeError, TypeError):
                pass
        raise
    finally:
        if not completed:
            shutil.rmtree(root, ignore_errors=True)


def _image_pixel_samples(image: Any) -> tuple[tuple[float, ...], ...]:
    """대형 배열을 만들지 않고 RGBA 버퍼의 대표 위치를 검증용으로 읽습니다."""
    count = len(image.pixels)
    if count < 4 or count % 4:
        raise UDIMSetupError("Image RGBA 픽셀 버퍼가 유효하지 않습니다.")
    pixel_count = count // 4
    indices = tuple(
        sorted(
            {
                0,
                max(0, pixel_count // 4),
                max(0, pixel_count // 2),
                max(0, (pixel_count * 3) // 4),
                pixel_count - 1,
            }
        )
    )
    return tuple(
        tuple(float(value) for value in image.pixels[index * 4 : index * 4 + 4])
        for index in indices
    )


def _copy_dirty_pixels_in_chunks(source: Any, replacement: Any) -> None:
    """Dirty 단일 Image를 전체 크기 tuple 없이 제한된 RGBA 조각으로 복사합니다."""
    source_count = len(source.pixels)
    if source_count != len(replacement.pixels):
        raise UDIMSetupError("FILE과 Tiled Image의 픽셀 버퍼 크기가 다릅니다.")
    chunk_values = 4 * 1024 * 1024
    for start in range(0, source_count, chunk_values):
        end = min(start + chunk_values, source_count)
        replacement.pixels[start:end] = source.pixels[start:end]
    replacement.update()


def copy_single_file_to_tiled_image(source: Any, name: str):
    """단일 FILE Image를 원본 불변 상태로 새 Tiled 1001 Image에 복제합니다."""
    import bpy

    if source is None or str(getattr(source, "source", "")) != "FILE":
        raise UDIMSetupError("Tiled 변환 Source는 단일 FILE Image여야 합니다.")
    size = tuple(int(value) for value in getattr(source, "size", ()))
    if len(size) != 2 or min(size) <= 0:
        raise UDIMSetupError("Tiled 변환 Source Image 해상도가 유효하지 않습니다.")
    source_state = (
        str(source.source),
        str(source.filepath),
        str(source.filepath_raw),
        len(tuple(getattr(source, "packed_files", ()))),
        bool(source.is_dirty),
        str(source.colorspace_settings.name),
        str(source.alpha_mode),
    )
    source_samples = _image_pixel_samples(source)
    replacement = None
    staging_path = ""
    working = None
    try:
        replacement = new_tiled_paint_image(
            str(name),
            size,
            (1001,),
            (0.0, 0.0, 0.0, 0.0),
        )
        staging_path = str(replacement.get(STAGING_DIRECTORY_KEY, "") or "")
        replacement.colorspace_settings.name = str(source.colorspace_settings.name)
        replacement.alpha_mode = str(source.alpha_mode)
        replacement.file_format = "PNG"
        _root, paths = paint_staging_paths(replacement)
        if len(paths) != 1:
            raise UDIMSetupError("단일 1001 Tiled Staging 경로를 확정하지 못했습니다.")
        if bool(source.is_dirty):
            replacement.unpack(method="REMOVE")
            _copy_dirty_pixels_in_chunks(source, replacement)
            replacement.save()
            replacement.pack()
        else:
            working = source.copy()
            working.filepath_raw = str(paths[0])
            working.filepath = str(paths[0])
            working.file_format = "PNG"
            working.save()
            bpy.data.images.remove(working)
            working = None
            replacement.unpack(method="REMOVE")
            replacement.reload()
            replacement.pack()
        if (
            str(replacement.source) != "TILED"
            or tuple(int(tile.number) for tile in replacement.tiles) != (1001,)
            or tuple(int(value) for value in replacement.size) != size
            or len(tuple(replacement.packed_files)) != 1
            or bool(replacement.is_dirty)
        ):
            raise UDIMSetupError("새 Tiled 1001 Image의 타일·해상도·Pack 계약이 유효하지 않습니다.")
        replacement_samples = _image_pixel_samples(replacement)
        tolerance = (1.0 / 255.0) + 1.0e-6
        if any(
            abs(before - after) > tolerance
            for before_rgba, after_rgba in zip(source_samples, replacement_samples)
            for before, after in zip(before_rgba, after_rgba)
        ):
            raise UDIMSetupError("FILE에서 Tiled Image로 이전한 대표 픽셀이 다릅니다.")
        if source_state != (
            str(source.source),
            str(source.filepath),
            str(source.filepath_raw),
            len(tuple(getattr(source, "packed_files", ()))),
            bool(source.is_dirty),
            str(source.colorspace_settings.name),
            str(source.alpha_mode),
        ):
            raise UDIMSetupError("Tiled Image 준비 중 기존 FILE Image 상태가 변경되었습니다.")
        replacement.use_fake_user = bool(getattr(source, "use_fake_user", False))
        return replacement
    except Exception:
        if working is not None:
            try:
                bpy.data.images.remove(working)
            except (AttributeError, ReferenceError, RuntimeError, TypeError):
                pass
        if replacement is not None:
            try:
                if int(replacement.users) == int(bool(replacement.use_fake_user)):
                    replacement.use_fake_user = False
                    bpy.data.images.remove(replacement)
            except (AttributeError, ReferenceError, RuntimeError, TypeError):
                pass
        if staging_path:
            remove_paint_staging_path(staging_path)
        raise


def paint_staging_paths(image: Any) -> tuple[Path, tuple[Path, ...]]:
    """관리 UDIM Image의 애드온 소유 Staging 경로를 검증해 반환합니다."""
    import bpy

    raw_root = str(image.get(STAGING_DIRECTORY_KEY, "") or "")
    if not raw_root:
        raise UDIMSetupError("관리 UDIM Image의 Staging 경로가 없습니다.")
    root = Path(raw_root).resolve()
    allowed = (Path(tempfile.gettempdir()) / "blender_projection_paint_staging").resolve()
    if root == allowed or allowed not in root.parents:
        raise UDIMSetupError("관리 UDIM Image의 Staging 경로 소유권이 유효하지 않습니다.")
    pattern = Path(bpy.path.abspath(str(image.filepath_raw or image.filepath))).resolve()
    if pattern.parent != root or "<UDIM>" not in pattern.name:
        raise UDIMSetupError("관리 UDIM Image 경로가 Staging 타일 패턴과 다릅니다.")
    paths = tuple(
        Path(str(pattern).replace("<UDIM>", str(int(tile.number))))
        for tile in image.tiles
    )
    return root, paths


def remove_paint_staging(image: Any) -> None:
    """삭제할 관리 Image의 검증된 Staging 디렉터리만 정리합니다."""
    try:
        root, _paths = paint_staging_paths(image)
    except (AttributeError, ReferenceError, RuntimeError, TypeError, UDIMSetupError):
        return
    remove_paint_staging_path(root)


def remove_paint_staging_path(path: str | Path) -> None:
    """검증된 애드온 소유 UDIM Staging 디렉터리를 제거합니다."""
    root = Path(path).resolve()
    allowed = (Path(tempfile.gettempdir()) / "blender_projection_paint_staging").resolve()
    if root == allowed or allowed not in root.parents:
        return
    shutil.rmtree(root, ignore_errors=True)


def build_paint_uv(target: Any, layout: UDIMLayout):
    """원본 UV를 복사하고 필요한 재질 타일에만 UDIM 오프셋을 적용합니다."""
    mesh = target.data
    source = mesh.uv_layers.get(layout.source_uv_name)
    if source is None:
        raise UDIMSetupError("분석한 원본 UV Map을 찾을 수 없습니다.")
    if mesh.uv_layers.get(PAINT_UV_NAME) is not None:
        raise UDIMSetupError("기존 PP_UDIM UV는 관리 재진입 외에는 재사용하지 않습니다.")
    uv = mesh.uv_layers.new(name=PAINT_UV_NAME, do_init=True)
    source_by_slot = {source.slot_index: source for source in layout.sources}
    try:
        for polygon in mesh.polygons:
            mapping = source_by_slot.get(int(polygon.material_index))
            if mapping is None:
                raise UDIMSetupError("사용 폴리곤 재질의 UDIM 타일 대응이 없습니다.")
            offset_u = mapping.tile.offset_u if mapping.needs_offset else 0
            offset_v = mapping.tile.offset_v if mapping.needs_offset else 0
            for loop_index in polygon.loop_indices:
                value = source.data[loop_index].uv
                uv.data[loop_index].uv = (
                    float(value.x) + offset_u,
                    float(value.y) + offset_v,
                )
        mesh.uv_layers.active = uv
        uv.active_render = True
        return uv
    except Exception:
        mesh.uv_layers.remove(uv)
        raise


def write_original_face_indices(target: Any, values: tuple[int, ...]) -> Any:
    """원본 폴리곤 재질 인덱스를 Face Attribute에 기록합니다."""
    mesh = target.data
    if mesh.attributes.get(ORIGINAL_FACE_ATTRIBUTE) is not None:
        raise UDIMSetupError("기존 원본 재질 인덱스 Attribute가 있어 자동 변환을 중지했습니다.")
    attribute = mesh.attributes.new(
        name=ORIGINAL_FACE_ATTRIBUTE,
        type="INT",
        domain="FACE",
    )
    try:
        for item, value in zip(attribute.data, values):
            item.value = int(value)
        return attribute
    except Exception:
        mesh.attributes.remove(attribute)
        raise


def layout_from_target(target: Any) -> tuple[str, tuple[int, ...], str]:
    """관리 Target의 영구 UDIM 속성을 검증 가능한 값으로 읽습니다."""
    kind = str(target.get(UDIM_KIND_KEY, "") or "")
    numbers = tuple(int(number) for number in target.get(UDIM_TILES_KEY, ()))
    signature = str(target.get(UDIM_SIGNATURE_KEY, "") or "")
    if not kind or not numbers or numbers[0] != 1001 or len(set(numbers)) != len(numbers):
        raise UDIMSetupError("관리 Target의 UDIM Layout 속성이 유효하지 않습니다.")
    if not signature:
        raise UDIMSetupError("관리 Target의 UDIM Layout 서명이 없습니다.")
    return kind, numbers, signature


__all__ = [
    "ORIGINAL_ACTIVE_SLOT_KEY",
    "ORIGINAL_FACE_ATTRIBUTE",
    "PAINT_UV_NAME",
    "STAGING_DIRECTORY_KEY",
    "UDIMLayout",
    "UDIMSetupError",
    "UDIMSource",
    "UDIMTile",
    "UDIM_KIND_KEY",
    "UDIM_MANAGED_SLOT_KEY",
    "UDIM_SIGNATURE_KEY",
    "UDIM_SOURCE_UV_KEY",
    "UDIM_TILES_KEY",
    "UDIM_UV_KEY",
    "analyze_udim_source",
    "build_paint_uv",
    "copy_single_file_to_tiled_image",
    "layout_from_target",
    "new_tiled_paint_image",
    "paint_image_size",
    "paint_staging_paths",
    "prepare_udim_source_image",
    "remove_paint_staging",
    "remove_paint_staging_path",
    "tile_from_uvtile_name",
    "udim_number",
    "write_original_face_indices",
]
